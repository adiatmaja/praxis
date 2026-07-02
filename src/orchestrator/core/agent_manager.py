"""Docker container lifecycle management for harness agent containers.

Harness-agnostic: spawns whichever harness a project selects (OpenCode is the
default; Aider and OpenHands are also supported).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import docker.errors
import httpx

from orchestrator.core.github_credentials import (
    GitHubCredentialProvider,
    PatCredentialProvider,
)
from orchestrator.core.harnesses import REGISTRY, default_harness_id


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
    ) -> None:
        self._lm_studio_url = lm_studio_url
        self._effective_settings = effective_settings
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
        context_limit = await detect_context_limit(lm_studio_url, model_name)
        if context_limit is not None:
            environment["MODEL_CONTEXT_LIMIT"] = str(context_limit)
        container_name = f"praxis-agent-{task_id[:8]}"
        self._remove_existing_container(container_name)
        container = self._client.containers.run(
            image=spec.image,
            name=container_name,
            environment=environment,
            detach=True,
            auto_remove=False,
            extra_hosts={"host.docker.internal": "host-gateway"},
        )
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
