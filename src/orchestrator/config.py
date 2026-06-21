"""Application configuration settings."""

from __future__ import annotations

import tempfile
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")
