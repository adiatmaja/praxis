"""Application configuration settings."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

from orchestrator.core.settings_file import load_yaml_settings


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
    host: str = "0.0.0.0"  # noqa: S104  # nosec B104 — intentional, overridden by compose/Caddy
    port: int = 8080
    # Git author identity agent containers commit under. Kept neutral (no Praxis
    # footprint in the commit profile); override via GIT_AUTHOR_NAME /
    # GIT_AUTHOR_EMAIL env. Defaults to the operator's own identity.
    git_author_name: str = "Johannes Baptista Adiatmaja Pambudi"
    git_author_email: str = "adi@pambudi.com"
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

    def callback_url(self) -> str:
        """Resolve the agent-done callback URL (port-derived when unset)."""
        if self.agent_callback_url:
            return self.agent_callback_url
        return f"http://host.docker.internal:{self.port}/api/internal/agent-done"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    def __init__(
        self, *args: Any, yaml_path: str = "config/praxis.yaml", **kwargs: Any
    ) -> None:
        """Overlay YAML defaults beneath explicit kwargs; env vars still win."""
        yaml_defaults = load_yaml_settings(yaml_path)
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
