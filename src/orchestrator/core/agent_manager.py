"""Docker container lifecycle management for harness agent containers.

Harness-agnostic: spawns whichever harness a project selects (OpenCode is the
default; agy/Antigravity is the experimental Gemini-backed alternative).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import docker.errors
import httpx
from dotenv import dotenv_values

# ``path_is_under`` compares on path COMPONENTS, so ``/repos-scratch`` is not
# inside ``/repos``. It lives with the doctor row that reports the same
# two-namespace split this module has to translate; importing it keeps ONE
# implementation of the predicate rather than letting a spawn-side copy drift
# away from the diagnosis an operator was just shown.
from orchestrator.core.doctor_probes import path_is_under
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

# Name prefix every agent container carries, whichever harness it runs. Read by
# the concurrency cap and the listing below, so it is named once here.
_AGENT_NAME_PREFIX = "praxis-agent-"

#: Label every agent container carries, naming the Praxis stack that spawned
#: it. Container NAMES carry no stack identity -- ``praxis-agent-<task>`` is
#: the same string in every checkout -- and a Docker name filter is a
#: daemon-wide substring match, so without this a SECOND checkout's agents
#: counted against this one's concurrency cap and appeared on this one's
#: ``/api/status``. Two checkouts sharing one daemon is a documented hazard
#: here (see PRAXIS_CONTAINER_NAME in .env.example), and that variable is the
#: identity this repo already uses to tell them apart, so it is what the label
#: carries rather than a second naming scheme invented for this.
STACK_LABEL = "org.praxis.stack"

#: ``docker-compose.yml``'s ``container_name`` default, and therefore what
#: ``PRAXIS_CONTAINER_NAME`` falls back to. Two checkouts that both leave it
#: unset already fight over the container name itself, so counting their
#: agents together is the honest answer for them.
_DEFAULT_STACK_ID = "orchestrator"

#: Where compose's SUBSTITUTION variables reach this process from.
#: ``LOCAL_REPOS_PATH``, ``LOCAL_REPOS_HOST_PATH`` and ``PRAXIS_CONTAINER_NAME``
#: are read by compose ON THE HOST to build a volume mapping and a container
#: name, and compose forwards NONE of them into the container's environment, so
#: ``os.environ`` alone reports every containerized install as having none of
#: them set. ``docker-compose.yml`` mounts ``./.env`` at ``/app/.env``, which
#: is this relative path both in a container (CWD is ``/app``) and under a bare
#: ``uv run uvicorn`` from the repo root. Twin reader:
#: ``api/doctor._dotenv_on_disk``.
_COMPOSE_ENV_FILE = Path(".env")


class SpawnConfigurationError(RuntimeError):
    """A spawn cannot proceed, and no amount of retrying will change that.

    Distinct from the plain ``RuntimeError`` the disk and concurrency
    preflights raise: those describe a condition that clears on its own (space
    freed, a slot opened) and the dispatch loop is right to retry them on the
    next tick. This one describes the DEPLOYMENT's configuration, which no
    tick can change, so ``orchestrator_dispatch`` fails the task where a human
    will see it instead of logging "will retry next loop tick" every
    ``loop_interval`` forever.

    Subclasses ``RuntimeError`` so no existing caller loses its handling; the
    dispatch loop orders this clause FIRST for exactly that reason.
    """


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


def compose_variable(key: str, default: str = "") -> str:
    """Resolve a compose substitution variable the way compose itself would.

    Environment first, then the mounted ``.env``, then the default: the same
    precedence ``${VAR:-default}`` gives, so this can never resolve to a value
    compose would not have used. See :data:`_COMPOSE_ENV_FILE` for why the file
    is not optional here.

    Args:
        key: The variable name, e.g. ``LOCAL_REPOS_PATH``.
        default: What compose falls back to when neither source sets it.

    Returns:
        The resolved value, stripped, or ``default`` when nothing set it.
    """
    from_env = (os.environ.get(key) or "").strip()
    if from_env:
        return from_env
    try:
        on_disk = dotenv_values(_COMPOSE_ENV_FILE, encoding="utf-8")
    # An unreadable or non-UTF-8 operator file means "not configured", never a
    # crash: this runs while the AgentManager is being built during startup, so
    # raising here would restart-loop the container over a dotenv file
    # pydantic-settings is about to report on its own terms anyway.
    except (OSError, UnicodeDecodeError):  # pragma: no cover - operator file
        return default
    return (on_disk.get(key) or "").strip() or default


def host_bind_source(container_path: str, repos_path: str, host_path: str) -> str:
    """Translate a local repo path out of THIS namespace into the daemon's.

    A local project's ``repo_url`` is a path the orchestrator can see:
    ``preflight._preflight_local`` calls ``Path.exists()`` on it from inside
    the orchestrator container, and ``LocalGitBackend`` runs git against it
    there. The IDENTICAL string is then handed to the Docker daemon as an agent
    container's bind-mount SOURCE, and the daemon resolves bind sources in the
    HOST namespace, never this container's. ``docker-compose.yml`` mounts host
    ``LOCAL_REPOS_HOST_PATH`` at container ``LOCAL_REPOS_PATH``, so an operator
    who takes the documented escape hatch and sets those two to genuinely
    different strings had the untranslated path handed to the daemon, where it
    names nothing. Docker does not refuse a missing bind source: it CREATES it
    as an empty directory, so the worker clones nothing, exits, and no error is
    raised anywhere.

    Args:
        container_path: The repo path as this process sees it.
        repos_path: ``LOCAL_REPOS_PATH`` as configured, "" when unset.
        host_path: ``LOCAL_REPOS_HOST_PATH`` as configured, "" when unset.

    Returns:
        The path to hand the daemon as a bind source.

    Raises:
        SpawnConfigurationError: When the two namespaces genuinely differ and
            the repo sits outside the only prefix that can be translated
            between them. A refusal naming both namespaces is the whole point:
            the alternative is a mount that succeeds and produces an empty
            directory.
    """
    repos = repos_path.strip().rstrip("/\\")
    host = host_path.strip().rstrip("/\\")
    # One string serves both namespaces: nothing to translate, and this is the
    # documented normal case. compose defaults LOCAL_REPOS_HOST_PATH to
    # LOCAL_REPOS_PATH, so setting one variable (or neither) lands here, as
    # does a bare uvicorn, where this process and the daemon share a filesystem.
    if not repos or not host or os.path.normcase(repos) == os.path.normcase(host):
        return container_path

    trimmed = container_path.strip().rstrip("/\\")
    if not path_is_under(trimmed, repos):
        # The trailing `nosec B608` below is NOT decorative and is NOT on the
        # line you would expect. This is an operator-facing refusal message,
        # not SQL; bandit reaches for B608 only because a multi-line f-string
        # happens to read like query construction. It reports the finding
        # against the first INTERPOLATED line, so the directive has to sit
        # there: on the `message = (` line, or on a comment line of its own, it
        # is silently dead and the scan still fails. Verified by deleting each
        # placement and re-running, because bandit never warns about a nosec
        # that guards nothing. Annotated inline rather than adding B608 to the
        # pyproject skip list: the project builds one REAL f-string query
        # (core/approvals.py) and a blanket skip would stop bandit seeing it.
        message = (
            f"local repository path {container_path} is not under "  # nosec B608
            f"LOCAL_REPOS_PATH ({repos_path}), and this deployment sets "
            f"LOCAL_REPOS_HOST_PATH ({host_path}) to a different string, so "
            "there is no honest way to name this repo in the HOST namespace. "
            "That namespace is the one that decides: the path is checked HERE "
            "with Path.exists() inside the orchestrator container, and the "
            "identical string is handed to the Docker daemon as the agent "
            "container's bind-mount SOURCE, which the daemon resolves on the "
            "HOST. Docker CREATES a missing bind source as an empty directory "
            "rather than refusing, so an untranslated path spawns a worker "
            "that clones nothing and reports no error. Move the repo under "
            "LOCAL_REPOS_PATH and update the project's repo_url, or set both "
            "variables to ONE path valid in both namespaces (on Docker "
            "Desktop for Windows that is the VM share prefix, e.g. "
            "/run/desktop/mnt/host/c/Users/you/repos). Then `docker compose "
            "up -d`, never `restart`: a mount is baked in at container CREATE "
            "(see LOCAL_REPOS_PATH and LOCAL_REPOS_HOST_PATH in .env.example)."
        )
        raise SpawnConfigurationError(message)

    # ``normcase`` folds case and separators but never changes LENGTH, so the
    # prefix that ``path_is_under`` matched is exactly this many characters.
    suffix = trimmed[len(repos) :]
    if "\\" in host and "/" not in host:
        # A plain Windows bind source must not come back with a POSIX suffix
        # glued onto it; the suffix was taken from the container-side path.
        suffix = suffix.replace("/", "\\")
    return f"{host}{suffix}"


def local_repo_volume(
    repo_url: str, repos_path: str = "", host_path: str = ""
) -> dict[str, dict[str, str]]:
    """Return the Docker volume mapping for a local bare repo, or {} for remote.

    The mount is read-write: a worker must both clone from and push back to
    the bare repo, and a read-only mount would let it clone successfully but
    fail on push deep inside the container with a confusing error.

    Args:
        repo_url: The project's configured repository URL or path.
        repos_path: ``LOCAL_REPOS_PATH`` as configured, "" when unset. What
            THIS process sees.
        host_path: ``LOCAL_REPOS_HOST_PATH`` as configured, "" when unset.
            What the DAEMON sees. Both default to "" so a caller that knows of
            no namespace split gets the identity behaviour it always had.

    Returns:
        A single-entry Docker volumes dict keyed by the bind source in the
        DAEMON's namespace, or {} when ``repo_url`` is not a local repo.

    Raises:
        SpawnConfigurationError: See :func:`host_bind_source`.
    """
    if not is_local_repo_url(repo_url):
        return {}
    source = host_bind_source(local_repo_path(repo_url), repos_path, host_path)
    return {source: {"bind": LOCAL_REPO_MOUNT, "mode": "rw"}}


def _in_container() -> bool:
    """Best-effort detection of running inside a Docker container.

    Twin of ``api/doctor._in_container``, duplicated rather than imported
    because ``core`` must not depend on ``api``. Both answer the same question
    the same way; if one of them ever needs a better test, the other needs it
    too.
    """
    return Path("/.dockerenv").exists()


@dataclass(frozen=True)
class DiskHeadroom:
    """What the disk preflight measured, and what it could not see."""

    #: The path whose filesystem was measured. Named in every message this
    #: produces: a refusal quoting a figure from a filesystem it does not
    #: identify cannot be checked by the operator acting on it.
    path: str
    free_bytes: int
    #: False when this process is containerized. See
    #: :data:`_HOST_DISK_UNOBSERVABLE`.
    host_backing_store_visible: bool


#: What this guard can and cannot see from inside a container, shared by the
#: refusal and the start-up notice so the two cannot drift into describing the
#: same blindness in two ways.
#:
#: Measured live 2026-08-26 on one machine: 942.0 GiB free inside the
#: orchestrator container, 3.6 GiB free on the host drive backing it. The 2 GiB
#: floor is therefore unreachable in the containerized deployment, which is the
#: RECOMMENDED one, so this guard is inert exactly where it is deployed. It is
#: not fixable by measuring something else: the host's backing store is a
#: sparse disk image the VM cannot see the outside of. This repo's standing
#: rule for a value that cannot be established is to say so rather than
#: substitute a guess (``core/context_window``, ``core/verify_gate``), which
#: here means the guard reports the filesystem it DID measure and states the
#: one it cannot.
_HOST_DISK_UNOBSERVABLE = (
    "measured inside this container, on the container's own writable layer, "
    "which is the same Docker storage an agent container's layer lands on. "
    "The HOST disk behind that storage is not observable from in here: on a "
    "VM-backed daemon (Docker Desktop) it is a sparse disk image on the host, "
    "so free space here can read far larger than the host actually has, and "
    "this guard cannot see the host run out"
)

#: Set once the blindness notice above has been logged. Module state, matching
#: ``config._LOGGED_DOTENV_OVERRIDES``: the notice describes the DEPLOYMENT,
#: not the task, so repeating it per spawn is noise an operator learns to skip.
_LOGGED_HOST_DISK_BLIND: bool = False


def measure_disk_headroom() -> DiskHeadroom:
    """Measure free space on the filesystem this process writes to.

    ``tempfile.gettempdir()`` is a cross-platform stand-in for "where this
    process writes", which inside a container is its own writable layer on the
    Docker storage an agent container's layer also lands on. That is the
    closest observable proxy for the space three parallel clones consume; what
    it is NOT is the host's disk, which earlier wording claimed.

    Returns:
        The measurement, carrying the path it was taken on so every message
        built from it names the filesystem it came from.
    """
    path = tempfile.gettempdir()
    return DiskHeadroom(
        path=path,
        free_bytes=shutil.disk_usage(path).free,
        host_backing_store_visible=not _in_container(),
    )


def _note_host_disk_blindness(headroom: DiskHeadroom, required_bytes: int) -> None:
    """Say once per process that this guard cannot see the host's disk.

    On the PASSING path deliberately. The refusal below never fires in a
    containerized deployment, because the threshold is compared against a
    filesystem that reads hundreds of GiB free, so a statement carried only by
    the refusal is a statement nobody ever reads.
    """
    global _LOGGED_HOST_DISK_BLIND
    if headroom.host_backing_store_visible or _LOGGED_HOST_DISK_BLIND:
        return
    _LOGGED_HOST_DISK_BLIND = True
    logger.info(
        "Agent-spawn disk headroom is %s. %s currently reads %.1f GiB free, "
        "against a %.1f GiB floor.",
        _HOST_DISK_UNOBSERVABLE,
        headroom.path,
        headroom.free_bytes / (1024**3),
        required_bytes / (1024**3),
    )


def _disk_refusal(headroom: DiskHeadroom, required_bytes: int) -> str:
    """Build the low-disk refusal, naming the filesystem actually measured."""
    caveat = (
        ""
        if headroom.host_backing_store_visible
        else f" That figure was {_HOST_DISK_UNOBSERVABLE}."
    )
    return (
        f"Insufficient disk space on the filesystem backing {headroom.path}: "
        f"{headroom.free_bytes / (1024**3):.1f} GiB free, "
        f"{required_bytes / (1024**3):.1f} GiB required.{caveat} Free disk "
        "space before spawning more agent containers."
    )


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
        stack_id: str | None = None,
        local_repos_path: str | None = None,
        local_repos_host_path: str | None = None,
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
        # Compose substitution variables, resolved ONCE here rather than per
        # spawn, matching gemini_creds_volume above: both of the repos paths
        # only ever take effect through a bind mount, which is baked in at
        # container CREATE, so re-reading them per spawn could only report a
        # value the running container does not actually have. An explicit
        # argument wins so a caller (and a test) can state both namespaces
        # without a file on disk; None means "ask the deployment".
        self._stack_id = stack_id or compose_variable(
            "PRAXIS_CONTAINER_NAME", _DEFAULT_STACK_ID
        )
        self._local_repos_path = (
            compose_variable("LOCAL_REPOS_PATH")
            if local_repos_path is None
            else local_repos_path
        )
        self._local_repos_host_path = (
            compose_variable("LOCAL_REPOS_HOST_PATH")
            if local_repos_host_path is None
            else local_repos_host_path
        )
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

        # --- Disk-headroom preflight ---
        # Three parallel agent clones can exhaust the Docker graph-driver
        # volume and wedge the daemon. Fail fast when the filesystem this
        # process writes to is low, and say WHICH filesystem that was: the
        # figure is not the host's, and claiming it was made the guard's own
        # blind spot invisible (see _HOST_DISK_UNOBSERVABLE).
        headroom = measure_disk_headroom()
        _note_host_disk_blindness(headroom, self._min_free_disk_bytes)
        if headroom.free_bytes < self._min_free_disk_bytes:
            msg = _disk_refusal(headroom, self._min_free_disk_bytes)
            logger.error(msg)
            raise RuntimeError(msg)

        # --- Concurrent-agent cap ---
        # Count THIS STACK's running agent containers to prevent simultaneous
        # clones from saturating disk and RAM. Scoped by STACK_LABEL, not by
        # name alone: a name filter is a daemon-wide substring match and agent
        # names are identical across checkouts, so a second checkout's agents
        # produced "3 of 3 running" here from an orchestrator that owned none
        # of them. Agents spawned by a build older than the label are not
        # counted; they are a one-time transition, and the alternative (count
        # unlabelled ones too) is the cross-stack bug all over again.
        running_count = sum(
            1
            for c in self._client.containers.list(
                filters={
                    "name": _AGENT_NAME_PREFIX,
                    "label": f"{STACK_LABEL}={self._stack_id}",
                }
            )
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
        # The bind SOURCE goes to the daemon, which resolves it in the HOST
        # namespace; everything upstream of here resolved it in this
        # container's. See host_bind_source for what the untranslated string
        # did instead of failing.
        volumes.update(
            local_repo_volume(
                repo_url,
                repos_path=self._local_repos_path,
                host_path=self._local_repos_host_path,
            )
        )

        container_name = f"{_AGENT_NAME_PREFIX}{task_id[:8]}"
        self._remove_existing_container(container_name)
        run_kwargs: dict[str, object] = {
            "image": spec.image,
            "name": container_name,
            "environment": environment,
            "detach": True,
            "auto_remove": False,
            "extra_hosts": {"host.docker.internal": "host-gateway"},
            # The only thing that says whose agent this is. The concurrency cap
            # and the listing both filter on it; a container spawned without it
            # is invisible to its own orchestrator.
            "labels": {STACK_LABEL: self._stack_id},
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
        """Return this stack's agent containers, running and exited.

        Scoped by :data:`STACK_LABEL` for the same reason the concurrency cap
        is: this feeds ``/api/status``'s ``active_agents``/``total_agents``,
        and a daemon-wide name match reported a second checkout's agents as
        this install's. Containers from a build older than the label drop out
        of the listing, which is cosmetic and one-time; reporting another
        stack's agents as yours is neither.
        """
        containers = self._client.containers.list(
            all=True,
            filters={
                "name": _AGENT_NAME_PREFIX,
                "label": f"{STACK_LABEL}={self._stack_id}",
            },
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
