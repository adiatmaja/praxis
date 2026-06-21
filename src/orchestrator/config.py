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
    github_token: str
    database_url: str = "sqlite+aiosqlite:///data/orchestrator.db"
    lm_studio_url: str = "http://host.docker.internal:1234"
    agent_model: str = "claude-opus-4-8"
    agent_model_effort: str | None = None
    host: str = "0.0.0.0"  # noqa: S104
    port: int = 8080
    docs_root: str = "docs"
    brainstorm_workspace: str = _default_brainstorm_workspace()
    memory_md_path: str = "docs/MEMORY.md"
    loop_interval: int = 30
    callback_grace: int = 5

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    def __init__(self, *args: Any, yaml_path: str = "config/praxis.yaml", **kwargs: Any) -> None:
        """Overlay YAML defaults beneath explicit kwargs; env vars still win."""
        yaml_defaults = load_yaml_settings(yaml_path)
        # Only inject YAML values for keys not already set via environment variables.
        # pydantic-settings uses uppercase env var names (no prefix configured).
        filtered = {k: v for k, v in yaml_defaults.items() if k.upper() not in os.environ}
        # Explicit kwargs passed by caller override YAML defaults.
        merged = {**filtered, **kwargs}
        super().__init__(*args, **merged)
