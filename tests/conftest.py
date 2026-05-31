"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.config import Settings
from orchestrator.database import Database


@pytest.fixture
def test_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("AUTH_TOKEN", "test-auth")
    monkeypatch.setenv("GITHUB_TOKEN", "test-github")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    return Settings()


@pytest.fixture
async def db(test_settings: Settings) -> Database:
    database = Database(test_settings.database_url)
    await database.initialize()
    yield database
    await database.close()
