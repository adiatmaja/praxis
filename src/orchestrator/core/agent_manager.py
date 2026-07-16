"""Docker container lifecycle management for harness agent containers.

Harness-agnostic: spawns whichever harness a project selects (OpenCode is the
default; Aider and OpenHands are also supported).
"""

from __future__ import annotations

import logging
import shutil
from typing import TYPE_CHECKING, Any

import docker.errors
import httpx

from orchestrator.core.github_credentials import (
    GitHubCredentialProvider,
    PatCredentialProvider,
)
from orchestrator.core.harnesses import REGISTRY, default_harness_id


# Minimum free disk space (in bytes) required before spawning an agent container.
# Three parallel clones of even a moderate repo can easily consume 1-3 GB of
# Docker graph-driver space; 2 GB gives a reasonable safety buffer.
_MIN_FREE_DISK_BYTES: int = 2 * 1024 * 1024 * 1024  # 2 GiB

# Maximum number of concurrently running praxis-agent-* containers. Parallel
# clones exhaust disk and stall the Docker daemon when this is unconstrained.
# Override via the ``max_agent_concurrency`` constructor param.
_DEFAULT_MAX_AGENT_CONCURRENCY: int = 3


if TYPE_CHECKING:
    from orchestrator.core.effective_settings import EffectiveSettings


logger = logging.getLogger(__name__)


async def detect_context_limit(lm_studio_url: str, model_name: str) -> int | None:
    """Return the model's real loaded context window from LM Studio, or None.

    Queries LM Studio's native REST API (``/api/v0/models``), which reports
    ``loaded_context_length`` (the window the model is actually serving) and
    ``max_context_length``. We prefer the loaded value because that is the hard
    limit a worker will hit; the model id may advertise far more than is loaded.

    The value is detected per-model at spawn time — never hardcoded — so it
    tracks whatever the host has loaded. Best-effort: any failure (LM Studio
    down, model not listed, unexpected payload) returns None and the caller
    simply omits the limit rather than guessing.
    """
    base = lm_studio_url.rstrip("/")
    url = f"{base}/api/v0/models"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Could not detect context limit from %s: %s", url, exc)
        return None
    for model in payload.get("data", []):
        if model.get("id") != model_name:
            continue
        limit = model.get("loaded_context_length") or model.get("max_context_length")
        if isinstance(limit, int) and limit > 0:
            return limit
        return None
    logger.warning(
        "Model %s not found in %s; cannot detect context limit", model_name, url
    )
    return None


def _container_host_url(url: str) -> str:
    """Rewrite a host-loopback URL so it is reachable from inside a bridge container.

    Under host networking a container could reach the orchestrator's LM Studio on
    ``localhost``; under bridge networking ``localhost`` is the container itself, so
    loopback hosts must be swapped for ``host.docker.internal`` (mapped to the host
    gateway via ``extra_hosts``). Non-loopback hosts are returned unchanged.
    """
    for loopback in ("localhost", "127.0.0.1"):
        url = url.replace(f"//{loopback}:", "//host.docker.internal:").replace(
            f"//{loopback}/", "//host.docker.internal/"
        )
    return url


class AgentManager:
    """Manage harness agent Docker containers (OpenCode/Aider/OpenHands)."""

    def __init__(
        self,
        lm_studio_url: str,
        github_token: str | None = None,
        effective_settings: EffectiveSettings | None = None,
        credentials: GitHubCredentialProvider | str | None = None,
        git_author_name: str | None = None,
        git_author_email: str | None = None,
        max_agent_concurrency: int = _DEFAULT_MAX_AGENT_CONCURRENCY,
        min_free_disk_bytes: int = _MIN_FREE_DISK_BYTES,
        gemini_creds_volume: str = "",
    ) -> None:
        self._lm_studio_url = lm_studio_url
        self._effective_settings = effective_settings
        self._git_author_name = git_author_name
        self._git_author_email = git_author_email
        self._gemini_creds_volume = gemini_creds_volume
        self._max_agent_concurrency = max_agent_concurrency
        self._min_free_disk_bytes = min_free_disk_bytes
        if credentials is not None:
            if isinstance(credentials, str):
                self._provider: GitHubCredentialProvider = PatCredentialProvider(
                    credentials
                )
            else:
                self._provider = credentials
        elif github_token is not None:
            self._provider = PatCredentialProvider(github_token)
        else:
            self._provider = PatCredentialProvider("")
        self._client = docker.from_env()  # type: ignore[attr-defined]

    async def spawn_agent(
        self,
        task_id: str,
        repo_url: str,
        branch: str,
        base_branch: str,
        task_prompt: str,
        model_name: str,
        callback_url: str,
        harness: str | None = None,
        callback_token: str | None = None,
        plan_path: str | None = None,
        plan_text: str | None = None,
        context_text: str | None = None,
        bible_text: str | None = None,
        task_summary: str | None = None,
    ) -> str:
        harness_id = harness or default_harness_id()
        spec = REGISTRY[harness_id]

        # --- Host-disk headroom preflight ---
        # Three parallel agent clones can exhaust the Docker graph-driver volume
        # and wedge the daemon. Fail fast with a clear message when disk is low.
        # Use tempfile.gettempdir() as a cross-platform proxy for the data volume.
        import tempfile

        disk = shutil.disk_usage(tempfile.gettempdir())
        if disk.free < self._min_free_disk_bytes:
            free_gb = disk.free / (1024**3)
            needed_gb = self._min_free_disk_bytes / (1024**3)
            msg = (
                f"Insufficient host disk space: {free_gb:.1f} GiB free, "
                f"{needed_gb:.1f} GiB required. Free disk space before "
                "spawning more agent containers."
            )
            logger.error(msg)
            raise RuntimeError(msg)

        # --- Concurrent-agent cap ---
        # Count currently running praxis-agent-* containers to prevent
        # simultaneous clones from saturating disk and RAM.
        running_count = sum(
            1 for c in self._client.containers.list(filters={"name": "praxis-agent-"})
        )
        if running_count >= self._max_agent_concurrency:
            msg = (
                f"Concurrent agent cap reached ({running_count} of "
                f"{self._max_agent_concurrency} running). Task will be "
                "re-dispatched when a slot opens."
            )
            logger.warning(msg)
            raise RuntimeError(msg)

        if self._effective_settings is not None:
            lm_studio_url = await self._effective_settings.lm_studio_url()
        else:
            lm_studio_url = self._lm_studio_url
        container_lm_url = _container_host_url(lm_studio_url)
        gh_token = await self._provider.token_for_repo(repo_url)
        environment = {
            "REPO_URL": repo_url,
            "BRANCH": branch,
            "BASE_BRANCH": base_branch,
            "TASK_PROMPT": task_prompt,
            "OPENAI_API_BASE": f"{container_lm_url}/v1",
            "MODEL": model_name,
            "HARNESS": harness_id,
            "GH_TOKEN": gh_token,
            "CALLBACK_URL": callback_url,
            "TASK_ID": task_id,
        }
        # Commit author identity for the worker's git config (neutral, no Praxis
        # footprint). Omitted -> entrypoint falls back to its own default.
        if self._git_author_name:
            environment["GIT_AUTHOR_NAME"] = self._git_author_name
        if self._git_author_email:
            environment["GIT_AUTHOR_EMAIL"] = self._git_author_email
        if callback_token is not None:
            environment["CALLBACK_TOKEN"] = callback_token
        if plan_path is not None:
            environment["PLAN_PATH"] = plan_path
        if plan_text is not None:
            environment["PLAN_TEXT"] = plan_text
        if context_text is not None:
            environment["CONTEXT_TEXT"] = context_text
        if bible_text is not None:
            environment["BIBLE_TEXT"] = bible_text
        if task_summary is not None:
            # Clean, human-readable task text for the PR body (the wrapped
            # TASK_PROMPT starts with a generic preamble, not the instruction).
            environment["TASK_SUMMARY"] = task_summary
        # Detect the model's real context window so compaction-capable harnesses
        # (OpenCode) can trigger at the right threshold instead of sailing past
        # it into silent server-side truncation. Detected per-model, never
        # hardcoded; omitted when LM Studio can't be reached.
        # agy skips this: it talks to Google via OAuth, not LM Studio, so the
        # /api/v0/models probe is irrelevant and would only generate noise.
        if harness_id != "agy":
            context_limit = await detect_context_limit(lm_studio_url, model_name)
            if context_limit is not None:
                environment["MODEL_CONTEXT_LIMIT"] = str(context_limit)

        # Mount the agy OAuth credentials VOLUME. The credentials are Linux-native
        # (populated once by an interactive `agy login`, see docs/deployment.md)
        # and live in a named Docker volume, so the mount source is a volume NAME
        # resolved by the Docker daemon (not a host path). We mount it read-write
        # at /home/agent/.gemini so fresh worker processes both authenticate and
        # persist refreshed access tokens (tokens expire in ~1h).
        volumes: dict[str, dict[str, str]] = {}
        if harness_id == "agy":
            if self._gemini_creds_volume:
                volumes[self._gemini_creds_volume] = {
                    "bind": "/home/agent/.gemini",
                    "mode": "rw",
                }
            else:
                logger.warning(
                    "agy harness selected but GEMINI_CREDS_VOLUME is not set; "
                    "the container will start without Gemini OAuth credentials "
                    "and authentication will fail. Run the one-time `agy login` "
                    "setup described in docs/deployment.md."
                )

        container_name = f"praxis-agent-{task_id[:8]}"
        self._remove_existing_container(container_name)
        run_kwargs: dict[str, object] = {
            "image": spec.image,
            "name": container_name,
            "environment": environment,
            "detach": True,
            "auto_remove": False,
            "extra_hosts": {"host.docker.internal": "host-gateway"},
        }
        if volumes:
            run_kwargs["volumes"] = volumes
        container = self._client.containers.run(**run_kwargs)
        logger.info(
            "Spawned %s container %s for task %s on branch %s",
            harness_id,
            container.id[:12],
            task_id,
            branch,
        )
        return str(container.id)

    def _remove_existing_container(self, name: str) -> None:
        """Remove a leftover container with the given name, if any.

        Container names are derived from the task id, so a retried or
        re-dispatched task collides with the exited container from its previous
        run. Without removing it, ``containers.run`` raises a 409 Conflict and
        the agent never starts. Missing containers are ignored.
        """
        try:
            existing = self._client.containers.get(name)
        except docker.errors.NotFound:
            return
        try:
            existing.remove(force=True)
            logger.info("Removed stale container %s before re-spawning", name)
        except docker.errors.APIError as exc:
            logger.warning("Could not remove stale container %s: %s", name, exc)

    def get_container_status(self, container_id: str) -> dict[str, Any] | None:
        try:
            container = self._client.containers.get(container_id)
        except docker.errors.NotFound:
            return None
        return {
            "status": container.status,
            "exit_code": container.attrs["State"]["ExitCode"],
        }

    def get_container_logs(self, container_id: str, tail: int | str = 500) -> str:
        try:
            container = self._client.containers.get(container_id)
        except docker.errors.NotFound:
            return ""
        return str(container.logs(tail=tail).decode(errors="replace"))

    def stop_agent(self, container_id: str) -> None:
        try:
            container = self._client.containers.get(container_id)
        except docker.errors.NotFound:
            logger.warning("Container %s not found for stop", container_id[:12])
            return
        container.stop(timeout=30)
        logger.info("Stopped container %s", container_id[:12])

    def cleanup_container(self, container_id: str) -> None:
        try:
            container = self._client.containers.get(container_id)
        except docker.errors.NotFound:
            return
        container.remove(force=True)
        logger.info("Removed container %s", container_id[:12])

    def list_agent_containers(self) -> list[dict[str, Any]]:
        containers = self._client.containers.list(
            all=True,
            filters={"name": "praxis-agent-"},
        )
        # Back-compat: containers spawned before the 2026-07 rename.
        containers += self._client.containers.list(
            all=True,
            filters={"name": "aider-agent-"},
        )
        return [
            {
                "id": container.id,
                "name": container.name,
                "status": container.status,
                "exit_code": container.attrs["State"]["ExitCode"],
            }
            for container in containers
        ]
