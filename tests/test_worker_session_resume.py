"""Tests for worker session resume (spec 2026-08-05)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.config import Settings
from orchestrator.database import CURRENT_SCHEMA_VERSION, Database


OPENCODE_EXTRACTOR = (
    Path(__file__).parent.parent / "docker" / "opencode-agent" / "extract_session.py"
)


def _run_extractor(script: Path, stdin_text: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script)],
        input=stdin_text,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.asyncio
async def test_migration_adds_worker_session_columns(tmp_path) -> None:
    """Migration 6 adds worker_session_id and worker_session_harness to tasks."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await db.initialize()
    try:
        rows = await db.fetch_all("PRAGMA table_info(tasks)")
        cols = {row["name"] for row in rows}
        assert "worker_session_id" in cols
        assert "worker_session_harness" in cols
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_migration_is_idempotent(tmp_path) -> None:
    """Re-running initialize on an existing DB does not error."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    db = Database(url)
    await db.initialize()
    await db.close()

    again = Database(url)
    await again.initialize()
    try:
        row = await again.fetch_one("PRAGMA user_version")
        assert row is not None
        assert int(row["user_version"]) == CURRENT_SCHEMA_VERSION
    finally:
        await again.close()


@pytest.mark.asyncio
async def test_migration_0006_is_rerun_safe(tmp_path) -> None:
    """Migration 6's body tolerates re-running against an already-migrated table.

    Rolls PRAGMA user_version back below 6 so the next initialize() actually
    re-invokes migration 6's apply() against a tasks table that already has
    both columns (the crash-between-apply-and-version-bump replay scenario),
    instead of relying on the `version <= current` gate to skip it entirely.
    """
    url = f"sqlite+aiosqlite:///{tmp_path / 'rerun.db'}"
    db = Database(url)
    await db.initialize()
    # Roll the recorded version back below 6 so the next initialize()
    # re-runs migration 6's body against a table that already has both
    # columns, instead of skipping it via the `version <= current` gate.
    await db.execute("PRAGMA user_version = 5")
    await db.close()

    again = Database(url)
    await again.initialize()
    try:
        rows = await again.fetch_all("PRAGMA table_info(tasks)")
        names = [row["name"] for row in rows]
        assert names.count("worker_session_id") == 1
        assert names.count("worker_session_harness") == 1

        row = await again.fetch_one("PRAGMA user_version")
        assert row is not None
        assert int(row["user_version"]) == CURRENT_SCHEMA_VERSION
    finally:
        await again.close()


def test_opencode_extractor_returns_single_session_id():
    """A fresh container has exactly one session; print its id."""
    payload = json.dumps([{"id": "ses_abc123", "title": "task"}])
    result = _run_extractor(OPENCODE_EXTRACTOR, payload)
    assert result.returncode == 0
    assert result.stdout.strip() == "ses_abc123"


def test_opencode_extractor_picks_newest_when_multiple():
    """A reused volume may hold several; the newest by `time.created` wins."""
    payload = json.dumps(
        [
            {"id": "ses_old", "time": {"created": 100}},
            {"id": "ses_new", "time": {"created": 200}},
        ]
    )
    result = _run_extractor(OPENCODE_EXTRACTOR, payload)
    assert result.returncode == 0
    assert result.stdout.strip() == "ses_new"


def test_opencode_extractor_is_silent_on_malformed_input():
    """Garbage must exit 1 with empty stdout, never crash the entrypoint."""
    result = _run_extractor(OPENCODE_EXTRACTOR, "not json at all")
    assert result.returncode == 1
    assert result.stdout.strip() == ""


def test_opencode_extractor_is_silent_on_empty_list():
    """No sessions is a normal outcome, not an error to surface."""
    result = _run_extractor(OPENCODE_EXTRACTOR, "[]")
    assert result.returncode == 1
    assert result.stdout.strip() == ""


def test_opencode_extractor_unwraps_sessions_dict():
    """The CLI may wrap the list under a `sessions` key instead of a bare list."""
    payload = json.dumps({"sessions": [{"id": "ses_wrapped"}]})
    result = _run_extractor(OPENCODE_EXTRACTOR, payload)
    assert result.returncode == 0
    assert result.stdout.strip() == "ses_wrapped"


AGY_EXTRACTOR = (
    Path(__file__).parent.parent / "docker" / "agy-agent" / "extract_session.py"
)


def test_agy_extractor_emits_conversation_id_and_response():
    """Line 1 is the conversation id; the rest is the response body."""
    payload = json.dumps(
        {"conversation_id": "conv_xyz", "response": "Status: BLOCKED\nneed the schema"}
    )
    result = _run_extractor(AGY_EXTRACTOR, payload)
    assert result.returncode == 0
    first, _, body = result.stdout.partition("\n")
    assert first.strip() == "conv_xyz"
    assert "Status: BLOCKED" in body


def test_agy_extractor_tolerates_missing_conversation_id():
    """Response text still flows through; the id line is empty."""
    payload = json.dumps({"response": "all done"})
    result = _run_extractor(AGY_EXTRACTOR, payload)
    assert result.returncode == 0
    first, _, body = result.stdout.partition("\n")
    assert first.strip() == ""
    assert "all done" in body


def test_agy_extractor_fails_on_malformed_input():
    """Garbage exits 1 so the entrypoint falls back to text mode."""
    result = _run_extractor(AGY_EXTRACTOR, "<<not json>>")
    assert result.returncode == 1
    assert result.stdout.strip() == ""


def test_agy_extractor_fails_on_non_dict_input():
    """A JSON list or scalar envelope has no keys to read; treat as malformed."""
    result = _run_extractor(AGY_EXTRACTOR, "[1, 2, 3]")
    assert result.returncode == 1
    assert result.stdout.strip() == ""


def test_agy_extractor_ignores_non_string_conversation_id():
    """A non-string id (e.g. a stray int) is dropped rather than printed raw."""
    payload = json.dumps({"conversation_id": 12345, "response": "ok"})
    result = _run_extractor(AGY_EXTRACTOR, payload)
    assert result.returncode == 0
    first, _, body = result.stdout.partition("\n")
    assert first.strip() == ""
    assert "ok" in body


def test_agy_extractor_prints_id_only_when_no_response_key_matches():
    """No known response key present: id line still prints, body stays empty."""
    payload = json.dumps({"conversation_id": "conv_only"})
    result = _run_extractor(AGY_EXTRACTOR, payload)
    assert result.returncode == 0
    assert result.stdout == "conv_only\n"


def test_agy_extractor_falls_back_to_later_response_key():
    """`response` is absent; a later key in the fallback order still wins."""
    payload = json.dumps({"conversation_id": "conv_1", "output": "fallback body"})
    result = _run_extractor(AGY_EXTRACTOR, payload)
    assert result.returncode == 0
    first, _, body = result.stdout.partition("\n")
    assert first.strip() == "conv_1"
    assert "fallback body" in body


def test_opencode_sessions_volume_has_default():
    """The OpenCode session volume mirrors the gemini creds volume pattern."""
    settings = Settings(
        _env_file=None,
        auth_token="test-token",
        github_token="test-gh-token",
    )
    assert settings.opencode_sessions_volume == "praxis-opencode-sessions"


def test_opencode_sessions_volume_env_override(monkeypatch):
    """OPENCODE_SESSIONS_VOLUME env var overrides the default, per settings precedence."""
    monkeypatch.setenv("OPENCODE_SESSIONS_VOLUME", "custom-sessions-vol")
    settings = Settings(
        _env_file=None,
        auth_token="test-token",
        github_token="test-gh-token",
    )
    assert settings.opencode_sessions_volume == "custom-sessions-vol"
