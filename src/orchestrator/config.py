"""Application configuration settings."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings sourced from environment variables."""

    auth_token: str
    github_token: str
    database_url: str = "sqlite+aiosqlite:///data/orchestrator.db"
    lm_studio_url: str = "http://host.docker.internal:1234"
    host: str = "0.0.0.0"
    port: int = 8080

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")
