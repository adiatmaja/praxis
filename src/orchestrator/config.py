"""Application configuration settings."""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from orchestrator.core.settings_file import (
    config_file_path,
    env_overlay_keys,
    load_yaml_settings,
)


logger = logging.getLogger(__name__)

#: Keys already reported as taking their value from the dotenv file rather than
#: the settings YAML.  Module state because ``Settings`` is constructed more
#: than once per process and a per-construction line would repeat forever.
_LOGGED_DOTENV_OVERRIDES: set[str] = set()


def _default_brainstorm_workspace() -> str:
    """Return a cross-platform temporary workspace path for context clones."""
    return str(Path(tempfile.gettempdir()) / "praxis-brainstorm")


def _dotenv_paths(kwargs: dict[str, Any], configured: Any) -> list[Path]:
    """Return the dotenv files this ``Settings`` construction will actually read.

    Mirrors pydantic-settings' own resolution: an explicit ``_env_file`` kwarg
    wins over the model config, and an explicit ``None`` means "read no dotenv
    file at all", which is how tests isolate themselves from the operator's
    real one.

    Args:
        kwargs: Keyword arguments handed to ``Settings(...)``.
        configured: The ``env_file`` value from the model config, used when
            the caller passed no ``_env_file``.

    Returns:
        Existing dotenv paths, in the order pydantic-settings reads them.
    """
    chosen = kwargs.get("_env_file", configured)
    if chosen is None:
        return []
    entries = [chosen] if isinstance(chosen, str | Path) else list(chosen)
    return [Path(entry) for entry in entries if entry and Path(entry).is_file()]


def _dotenv_keys(kwargs: dict[str, Any], configured: Any, encoding: str) -> set[str]:
    """Return the variable names the operator's dotenv file(s) define.

    These count as ENVIRONMENT for precedence purposes.  The dotenv file is
    where ``praxis init`` records the operator's answers and where every setup
    document tells a newcomer to put them, so a value there losing to a
    git-tracked defaults file is the documented order (environment, then the
    settings file, then the built-in default) reported wrongly.

    Args:
        kwargs: Keyword arguments handed to ``Settings(...)``.
        configured: The ``env_file`` value from the model config.
        encoding: Text encoding for the dotenv file.

    Returns:
        Upper-case variable names defined by the dotenv file(s).  Empty when
        no dotenv file is read, or when one cannot be read -- a settings load
        must not fail on an operator file that pydantic-settings is about to
        report on its own terms a moment later.
    """
    names: set[str] = set()
    for path in _dotenv_paths(kwargs, configured):
        try:
            names |= {key.upper() for key in dotenv_values(path, encoding=encoding)}
        except OSError:  # pragma: no cover - unreadable file, pydantic reports it
            continue
    return names


def _log_dotenv_overrides(
    yaml_defaults: dict[str, Any],
    filtered: dict[str, Any],
    overlaid: set[str],
) -> None:
    """Say once, per key, that a dotenv value is beating the settings file.

    The precedence fix above removes one silence and would otherwise open
    another in the opposite direction: an operator who edits the settings file
    for a key their dotenv file also names now gets their edit ignored, and
    that must not be silent either.  Only the dotenv case is reported.  A real
    environment variable winning is the documented order and surprises nobody.

    Args:
        yaml_defaults: Everything the settings file (plus the PRAXIS_ overlay)
            offered.
        filtered: What survived the environment check and will be applied.
        overlaid: Settings names supplied by ``PRAXIS_*`` environment vars.
    """
    for key in yaml_defaults:
        if key in filtered or key in overlaid or key.upper() in os.environ:
            continue
        if key in _LOGGED_DOTENV_OVERRIDES:
            continue
        _LOGGED_DOTENV_OVERRIDES.add(key)
        logger.info(
            "%s is set in the dotenv file and also in %s; the dotenv value wins. "
            "Remove it from the dotenv file to let the settings file decide.",
            key.upper(),
            config_file_path(),
        )


class Settings(BaseSettings):
    """Runtime settings sourced from environment variables."""

    auth_token: str
    github_token: str | None = None
    github_app_id: str | None = None
    # PEM contents OR a path to a PEM file holding the App private key.
    github_app_private_key: str | None = None
    github_app_installation_id: int | None = None
    database_url: str = "sqlite+aiosqlite:///data/orchestrator.db"
    lm_studio_url: str = "http://host.docker.internal:1234"
    agent_model: str = "claude-opus-4-8"
    agent_model_effort: str | None = None
    default_worker_harness: str = "opencode"
    default_worker_model: str = ""
    host: str = "0.0.0.0"  # noqa: S104  # nosec B104 — intentional, overridden by compose/Caddy
    port: int = 8080
    # Git author identity agent containers commit under. Kept neutral (no Praxis
    # footprint in the commit profile); override via GIT_AUTHOR_NAME /
    # GIT_AUTHOR_EMAIL env. Defaults to a generic bot identity so AI-generated
    # commits are never mis-attributed to the repo owner.
    git_author_name: str = "praxis-agent"
    git_author_email: str = "praxis-agent@users.noreply.github.com"
    docs_root: str = "docs"
    brainstorm_workspace: str = _default_brainstorm_workspace()
    memory_md_path: str = "docs/MEMORY.md"
    loop_interval: int = 5
    callback_grace: int = 5
    # Wall-clock ceiling on ONE agent run, in minutes. Nothing bounded a worker
    # before this: the cap of 3 attempts was the only limit, so a harness that
    # never reported spent a real, finite resource (a subscription, an API
    # budget, somebody's GPU) until a person noticed. Generous on purpose - a
    # legitimate run of 32 minutes was measured on a squeezed context window -
    # and 0 or less disables the bound entirely, which is a supported state for
    # anyone whose workers are expected to run longer than an hour.
    worker_timeout_minutes: int = 60
    # URL agent containers POST their completion callback to. Reachable from
    # inside a container, so it uses host.docker.internal and must match the
    # port the orchestrator actually listens on. None => derived from `port`.
    agent_callback_url: str | None = None
    # Shared secret sent by agent containers in the X-Praxis-Callback-Token
    # header.  When unset, the orchestrator derives the secret from auth_token
    # at startup so existing single-node deployments work without configuration.
    # Set explicitly (e.g. via INTERNAL_CALLBACK_SECRET env var) to use a
    # dedicated secret instead.
    internal_callback_secret: str | None = None
    # Human-reachable base URL for the dashboard (e.g. when the container's
    # internal port differs from the Docker-host-mapped port).  When set,
    # REST responses use this value for ``dashboard_url`` instead of
    # ``http://localhost:{port}/``.  The MCP client already overrides
    # ``dashboard_url`` with its own ``PRAXIS_BASE_URL``, so this is mainly
    # needed when callers consume the REST API directly.
    public_url: str | None = None
    # Name of the Docker VOLUME that holds the agy (Antigravity/Gemini) OAuth
    # credentials.  The user populates it once by STARTING an interactive `agy`
    # session against the empty volume, which is what triggers the OAuth flow:
    # there is no `agy login` subcommand
    # (see docs/deployment.md); the orchestrator then mounts it read-write into
    # every agy agent container at /home/agent/.gemini so fresh worker processes
    # can authenticate and refresh tokens.  A named volume (not a host path) is
    # used deliberately: the credentials are Linux-native and identical on every
    # OS, so setup is cross-platform.  Set via GEMINI_CREDS_VOLUME env var.
    # When empty, the agy harness proceeds without mounting credentials (a
    # warning is logged) so unconfigured setups do not crash.
    gemini_creds_volume: str = "praxis-gemini-creds"
    # Named Docker volume holding OpenCode session state, mounted read-write at
    # /home/agent/.local/share/opencode so a re-dispatched worker can resume its
    # conversation with `opencode run --session <id>`. Unlike the agy creds
    # volume this needs no interactive seeding; Docker creates it on first use.
    # Empty disables persistence: workers then always start cold, never error.
    opencode_sessions_volume: str = "praxis-opencode-sessions"
    worker_reasoning_effort: str = Field(
        default="none",
        description=(
            "Thinking effort sent to harnesses praxis drives through a request "
            "option (currently OpenCode). Harnesses that encode effort in the "
            "model string (agy) ignore this. One of: none, low, medium, high."
        ),
    )
    # Admit a LOCAL filesystem path as a project's repo_url (the local git
    # backend: a bind-mounted bare repo, no GitHub credential, no PR object).
    # OFF by default because it lets an authenticated caller point the
    # orchestrator at any path the container can reach, which is the right
    # trade for a single-operator box and the wrong one for a shared host.
    # Turn it on in the mounted praxis.yaml (a restart, not a rebuild) to run
    # the benchmark or to evaluate Praxis with zero GitHub credentials.
    allow_local_repo_paths: bool = False

    def dashboard_url(self) -> str:
        """Return the human-reachable dashboard URL for use in API responses.

        Prefers ``public_url`` when configured (e.g. ``PUBLIC_URL`` env var),
        and falls back to ``http://localhost:{port}/``.
        """
        if self.public_url:
            return self.public_url.rstrip("/") + "/"
        return f"http://localhost:{self.port}/"

    def callback_url(self) -> str:
        """Resolve the agent-done callback URL (port-derived when unset)."""
        if self.agent_callback_url:
            return self.agent_callback_url
        return f"http://host.docker.internal:{self.port}/api/internal/agent-done"

    # extra="ignore", deliberately: docker-compose.yml mounts ./.env at
    # /app/.env for the env_drift doctor check, so pydantic-settings parses the
    # operator's whole dotenv file. With the pydantic-settings default of
    # "forbid", a single container-only variable an operator adds there aborts
    # startup and the container restart-loops, with a traceback that names the
    # key but never says .env is the source. Trade-off: a typo in a real key
    # (e.g. AUTHH_TOKEN) is now silently ignored and nothing catches it.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    def __init__(self, *args: Any, yaml_path: str | None = None, **kwargs: Any) -> None:
        """Overlay YAML defaults beneath explicit kwargs; env vars still win.

        Args:
            *args: Positional arguments forwarded to ``BaseSettings``.
            yaml_path: Explicit settings-file path, mainly for tests.  When
                None the path is resolved by ``config_file_path()`` at CALL
                time, never frozen into this signature's default: an
                import-time default would ignore ``PRAXIS_CONFIG_PATH`` and
                silently keep reading the image-baked copy.
            **kwargs: Field values that override the YAML defaults.
        """
        yaml_defaults = load_yaml_settings(
            yaml_path if yaml_path is not None else config_file_path()
        )
        # Only inject YAML values for keys the environment has not already set.
        # pydantic-settings uses uppercase env var names (no prefix configured).
        #
        # "The environment" includes the operator's dotenv file, and that is
        # not a detail. Injecting a YAML value here makes it an init KWARG,
        # which outranks every pydantic-settings source including the dotenv
        # one, so a key present in BOTH files silently took the YAML value.
        # Measured 2026-08-21: LOOP_INTERVAL=0 in .env left the container
        # running at the YAML's 5 and said nothing anywhere. It also split the
        # two ways of running the product apart, because compose forwards
        # DEFAULT_WORKER_HARNESS and DEFAULT_WORKER_MODEL as real environment
        # variables: the same dotenv file that `praxis init` writes won inside
        # a container and lost under a bare uvicorn, with no way to tell.
        #
        # A PRAXIS_-prefixed variable is exempt, and has to be: those are not
        # in `yaml_defaults` because the file holds them, they are there
        # because the overlay put them there, so treating one as "shadowed by
        # the dotenv file" would drop a real environment variable in favour of
        # a lower-precedence source.
        config = type(self).model_config
        env_names = set(os.environ) | _dotenv_keys(
            kwargs,
            config.get("env_file"),
            str(config.get("env_file_encoding") or "utf-8"),
        )
        overlaid = env_overlay_keys()
        filtered = {
            k: v
            for k, v in yaml_defaults.items()
            if k in overlaid or k.upper() not in env_names
        }
        _log_dotenv_overrides(yaml_defaults, filtered, overlaid)
        # Explicit kwargs passed by caller override YAML defaults.
        merged = {**filtered, **kwargs}
        # Filter to prevent source-directive kwargs (_env_file, _secrets_dir, etc.)
        # from being passed to pydantic-settings as if they were field values.
        # pydantic-settings intercepts these underscore-prefixed kwargs before
        # validation and uses them to control where configuration is loaded from.
        # If load_yaml_settings folds an arbitrary env var as a lowercase key
        # (e.g. PRAXIS_ENV_FILE -> env_file) and we pass it through as a field,
        # it could silently redirect settings reads. The "or k in kwargs" clause
        # is load-bearing: it allows callers to explicitly pass _env_file=None,
        # which is how tests isolate themselves from the real ./.env file.
        known_fields = set(type(self).model_fields)
        merged = {k: v for k, v in merged.items() if k in known_fields or k in kwargs}
        super().__init__(*args, **merged)
