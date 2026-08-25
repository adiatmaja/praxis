"""Docker container lifecycle management for harness agent containers.

Harness-agnostic: spawns whichever harness a project selects (OpenCode is the
default; agy/Antigravity is the experimental Gemini-backed alternative).
"""

from __future__ import annotations

import logging
import re
import shutil
from typing import TYPE_CHECKING, Any

import docker.errors
import httpx

from orchestrator.core.git_backend import is_local_repo_url, local_repo_path
from orchestrator.core.github_credentials import (
    GitHubCredentialProvider,
    PatCredentialProvider,
)
from orchestrator.core.harnesses import (
    REGISTRY,
    default_harness_id,
    should_attempt_lm_studio_probe,
)
from orchestrator.core.worker_effort import resolve_worker_effort


# Minimum free disk space (in bytes) required before spawning an agent container.
# Three parallel clones of even a moderate repo can easily consume 1-3 GB of
# Docker graph-driver space; 2 GB gives a reasonable safety buffer.
_MIN_FREE_DISK_BYTES: int = 2 * 1024 * 1024 * 1024  # 2 GiB

# Where a local bare repo is bind-mounted inside the agent container. Fixed so
# the entrypoint can clone from a stable path regardless of the host layout.
LOCAL_REPO_MOUNT = "/srv/praxis-repo.git"

# The entrypoint hard-requires GH_TOKEN (`: "${GH_TOKEN:?...}"`). Local mode has
# no credential, so a placeholder satisfies the guard; the entrypoint skips
# every credential-helper and gh call when GIT_BACKEND=local.
_LOCAL_GH_TOKEN_PLACEHOLDER = "local-mode-no-token"  # nosec B105 - not a secret

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


def _opencode_session_volume_name(base_volume: str, task_id: str) -> str:
    """Derive a per-task Docker volume name for OpenCode session storage.

    Why per-task, not one shared volume: ``orchestrator_dispatch`` dispatches
    every currently-dispatchable task in a wave, bounded only by
    ``AgentManager._max_agent_concurrency`` (default 3), so two or more
    OpenCode containers for the SAME PROJECT can run concurrently. If they
    all mounted one shared volume, ``opencode session list --format json``
    inside container A would also see sessions created by container B, and
    ``docker/opencode-agent/extract_session.py``'s "newest by ``time.created``
    wins" heuristic would then attribute B's session id to A's task. A's next
    resume would then replay a DIFFERENT TASK's conversation into A's
    container. This is a real concurrency race that reproduces on any wave
    with 2+ concurrent OpenCode tasks, not a hypothetical edge case, so do
    not simplify this back to a single shared volume name.

    The name must still be deterministic per task id, because a re-dispatch
    of the SAME task (the resume path) must mount the SAME volume to find
    its own prior session.

    Args:
        base_volume: The configured base volume name
            (``Settings.opencode_sessions_volume``). Callers must treat an
            empty string as "session persistence disabled" and skip the
            mount entirely; this function returns "" unchanged in that case.
        task_id: The dispatched task's id. Task ids in this codebase are
            UUID4 strings, which are already legal Docker volume-name
            characters, but this function sanitizes rather than assuming
            that blindly, and truncates the task-id component to keep the
            resulting name reasonably short.

    Returns:
        The per-task volume name, or "" when ``base_volume`` is empty.
    """
    if not base_volume:
        return ""
    # Docker volume names must match [a-zA-Z0-9][a-zA-Z0-9_.-]*. The base
    # name is a validated config value and supplies the required leading
    # character, so only the task-id suffix needs sanitizing here.
    safe_id = re.sub(r"[^a-zA-Z0-9_.-]", "-", task_id)[:32] or "task"
    return f"{base_volume}-{safe_id}"


def local_repo_volume(repo_url: str) -> dict[str, dict[str, str]]:
    """Return the Docker volume mapping for a local bare repo, or {} for remote.

    The mount is read-write: a worker must both clone from and push back to
    the bare repo, and a read-only mount would let it clone successfully but
    fail on push deep inside the container with a confusing error.

    Args:
        repo_url: The project's configured repository URL or path.

    Returns:
        A single-entry Docker volumes dict keyed by host path, or {} when
        ``repo_url`` is not a local repo.
    """
    if not is_local_repo_url(repo_url):
        return {}
    return {local_repo_path(repo_url): {"bind": LOCAL_REPO_MOUNT, "mode": "rw"}}


def build_spawn_env(
    repo_url: str,
    branch: str,
    base_branch: str,
    task_prompt: str,
    container_lm_url: str,
    model_name: str,
    harness_id: str,
    gh_token: str,
    callback_url: str,
    task_id: str,
    git_author_name: str | None = None,
    git_author_email: str | None = None,
    callback_token: str | None = None,
    plan_path: str | None = None,
    plan_text: str | None = None,
    context_text: str | None = None,
    bible_text: str | None = None,
    task_summary: str | None = None,
    single_branch: bool = False,
    context_limit: int | None = None,
    worker_session_id: str | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, str]:
    """Build environment variables dictionary for spawned agent containers.

    Args:
        reasoning_effort: The operator's configured worker thinking-effort
            level (``Settings.worker_reasoning_effort``), or None for no
            preference. Resolved through ``core.worker_effort.resolve_worker_effort``
            against the harness's declared effort channel: harnesses driven
            through a request option (opencode) always get an explicit
            ``WORKER_REASONING_EFFORT`` (never omitted, since an absent value
            hands the level to the server, whose default is not stable and has
            inverted; see ``core.thinking``); harnesses that bake
            effort into the model string (agy) get no env var at all, since a
            var they silently ignore would be a lie.
    """
    local_mode = is_local_repo_url(repo_url)
    environment: dict[str, str] = {
        "REPO_URL": LOCAL_REPO_MOUNT if local_mode else repo_url,
        "BRANCH": branch,
        "BASE_BRANCH": base_branch,
        "TASK_PROMPT": task_prompt,
        "OPENAI_API_BASE": f"{container_lm_url}/v1",
        "MODEL": model_name,
        "HARNESS": harness_id,
        "GH_TOKEN": _LOCAL_GH_TOKEN_PLACEHOLDER if local_mode else (gh_token or ""),
        "CALLBACK_URL": callback_url,
        "TASK_ID": task_id,
        "GIT_BACKEND": "local" if local_mode else "github",
    }
    if git_author_name:
        environment["GIT_AUTHOR_NAME"] = git_author_name
    if git_author_email:
        environment["GIT_AUTHOR_EMAIL"] = git_author_email
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
    if single_branch:
        environment["SINGLE_BRANCH"] = "1"
    if context_limit is not None:
        environment["MODEL_CONTEXT_LIMIT"] = str(context_limit)
    if worker_session_id:
        # Presence of this var means BOTH "resume the conversation" and "reuse
        # the existing remote branch": memory and tree must move together.
        environment["WORKER_SESSION_ID"] = worker_session_id

    # Effort is resolved from the harness's DECLARED channel, so a harness that
    # bakes effort into its model string gets no env var rather than a variable
    # it would silently ignore. See core/worker_effort.py.
    effective_effort = resolve_worker_effort(harness_id, reasoning_effort)
    if effective_effort is not None:
        environment["WORKER_REASONING_EFFORT"] = effective_effort

    return environment


class AgentManager:
    """Manage harness agent Docker containers (OpenCode default, or agy)."""

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
        opencode_sessions_volume: str = "",
        worker_reasoning_effort: str = "none",
    ) -> None:
        self._lm_studio_url = lm_studio_url
        self._effective_settings = effective_settings
        self._git_author_name = git_author_name
        self._git_author_email = git_author_email
        self._gemini_creds_volume = gemini_creds_volume
        self._opencode_sessions_volume = opencode_sessions_volume
        # Sourced from Settings.worker_reasoning_effort at construction, the
        # same "restart-only, no live DB override" pattern as
        # gemini_creds_volume/opencode_sessions_volume above: baked in once
        # at startup, not re-read per spawn. See main.py's AgentManager(...)
        # call for where this is threaded from config.
        self._worker_reasoning_effort = worker_reasoning_effort
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
        single_branch: bool = False,
        worker_session_id: str | None = None,
        context_limit: int | None = None,
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
        gh_token = (
            ""
            if is_local_repo_url(repo_url)
            else await self._provider.token_for_repo(repo_url)
        )

        # A caller-resolved window WINS and skips the probe entirely. The
        # orchestrator has already run the full resolution (project column ->
        # declared -> probe -> unknown) to budget the pack it is handing us, and
        # resolving a second time here answered differently: a declared window
        # budgeted the Bible at, say, 128 000 while this method probed LM Studio,
        # missed, and gave the container no MODEL_CONTEXT_LIMIT at all, so
        # OpenCode compacted against its own built-in default instead.
        #
        # The fallback below keeps every other caller working. Its predicate is
        # shared with the budgeting path rather than duplicated: it used to read
        # ``harness_id != "agy"``, wrong twice over - it was the ONLY place that
        # knew agy should not be probed, so the budgeting path probed it, missed,
        # and fabricated 8192; and it happily probed LM Studio for an OpenCode
        # project the operator had pointed at a hosted provider. A missing
        # endpoint means no probe, and a probe that comes back with nothing
        # leaves the limit absent - never a substituted number.
        if context_limit is None and should_attempt_lm_studio_probe(
            harness_id, lm_studio_url
        ):
            context_limit = await detect_context_limit(lm_studio_url, model_name)

        environment = build_spawn_env(
            repo_url=repo_url,
            branch=branch,
            base_branch=base_branch,
            task_prompt=task_prompt,
            container_lm_url=container_lm_url,
            model_name=model_name,
            harness_id=harness_id,
            gh_token=gh_token,
            callback_url=callback_url,
            task_id=task_id,
            git_author_name=self._git_author_name,
            git_author_email=self._git_author_email,
            callback_token=callback_token,
            plan_path=plan_path,
            plan_text=plan_text,
            context_text=context_text,
            bible_text=bible_text,
            task_summary=task_summary,
            single_branch=single_branch,
            context_limit=context_limit,
            worker_session_id=worker_session_id,
            reasoning_effort=self._worker_reasoning_effort,
        )

        # Mount the agy OAuth credentials VOLUME. The credentials are Linux-native
        # (populated once by STARTING an interactive `agy` session against the
        # empty volume, which is what triggers the OAuth flow: there is no
        # `agy login` subcommand -- see docs/deployment.md)
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
                    "and authentication will fail. Run the one-time interactive "
                    "`agy` login described in docs/deployment.md "
                    "(there is no `agy login` subcommand)."
                )
        if harness_id == "opencode" and self._opencode_sessions_volume:
            # OpenCode keeps session state under XDG_DATA_HOME. Without this
            # mount it dies with the container and resume degrades to a cold
            # start (which is a supported outcome, not an error). The volume
            # is scoped PER TASK (see _opencode_session_volume_name) so that
            # concurrent containers in the same wave never share one session
            # store, which would otherwise let one container's session id
            # bleed into another task's resume.
            session_volume = _opencode_session_volume_name(
                self._opencode_sessions_volume, task_id
            )
            volumes[session_volume] = {
                "bind": "/home/agent/.local/share/opencode",
                "mode": "rw",
            }
        volumes.update(local_repo_volume(repo_url))

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
        return [
            {
                "id": container.id,
                "name": container.name,
                "status": container.status,
                "exit_code": container.attrs["State"]["ExitCode"],
            }
            for container in containers
        ]
