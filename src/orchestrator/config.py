"""Application configuration settings."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from orchestrator.core.settings_file import config_file_path, load_yaml_settings


def _default_brainstorm_workspace() -> str:
    """Return a cross-platform temporary workspace path for context clones."""
    return str(Path(tempfile.gettempdir()) / "praxis-brainstorm")


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
    loop_interval: int = 30
    callback_grace: int = 5
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
    # credentials.  The user populates it once with an interactive `agy login`
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

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

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
        # Only inject YAML values for keys not already set via environment variables.
        # pydantic-settings uses uppercase env var names (no prefix configured).
        filtered = {
            k: v for k, v in yaml_defaults.items() if k.upper() not in os.environ
        }
        # Explicit kwargs passed by caller override YAML defaults.
        merged = {**filtered, **kwargs}
        # The YAML may carry nested config consumed elsewhere (e.g. capability,
        # escalation read via EffectiveSettings); drop keys that are not Settings
        # fields so pydantic's extra="forbid" does not reject them.
        known_fields = set(type(self).model_fields)
        merged = {k: v for k, v in merged.items() if k in known_fields or k in kwargs}
        super().__init__(*args, **merged)
