"""Tests for worker session resume (spec 2026-08-05)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

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
    """Garbage must exit non-zero with empty stdout, never crash the entrypoint."""
    result = _run_extractor(OPENCODE_EXTRACTOR, "not json at all")
    assert result.returncode != 0
    assert result.stdout.strip() == ""


def test_opencode_extractor_is_silent_on_empty_list():
    """No sessions is a normal outcome, not an error to surface."""
    result = _run_extractor(OPENCODE_EXTRACTOR, "[]")
    assert result.returncode != 0
    assert result.stdout.strip() == ""


def test_opencode_extractor_is_silent_on_empty_stdin():
    """A blank pipe is a distinct, realistic failure mode.

    If the upstream `opencode session list` call itself fails and produces
    no output, this script sees an empty stdin, not garbage text or an
    empty JSON list. It must exit non-zero with empty stdout too.
    """
    result = _run_extractor(OPENCODE_EXTRACTOR, "")
    assert result.returncode != 0
    assert result.stdout.strip() == ""
