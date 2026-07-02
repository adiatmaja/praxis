"""Tests for legacy spec backfill."""

from __future__ import annotations

import uuid

from orchestrator.core.backfill import backfill_legacy_specs


async def _make_legacy_plans_table(db):
    # The post-Spec-2 db fixture has a plans table without `spec`. Recreate it
    # WITH spec to simulate a pre-Spec-1 database for this isolated test.
    await db.execute("DROP TABLE IF EXISTS plans")
    await db.execute(
        """
        CREATE TABLE plans (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            spec TEXT,
            opus_plan TEXT,
            source TEXT NOT NULL DEFAULT 'user',
            status TEXT NOT NULL DEFAULT 'pending',
            spec_path TEXT,
            plan_path TEXT
        )
        """
    )


async def test_backfill_writes_spec_doc_and_sets_path(db, mocker):
    await _make_legacy_plans_table(db)
    pid, plid = str(uuid.uuid4()), str(uuid.uuid4())
    await db.execute(
        "INSERT OR IGNORE INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u", "test", "hash"),
    )
    await db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url, model_name) "
        "VALUES (?, ?, ?, ?, ?)",
        (pid, "u", "p", "https://example.com/r.git", "m"),
    )
    await db.execute(
        "INSERT INTO plans (id, project_id, spec, source) VALUES (?, ?, ?, 'user')",
        (plid, pid, "legacy spec body"),
    )
    fake_bs = mocker.Mock()
    fake_bs.write_and_commit = mocker.AsyncMock(return_value={"status": "committed"})
    count = await backfill_legacy_specs(db, fake_bs)
    assert count == 1
    row = await db.fetch_one("SELECT spec_path FROM plans WHERE id = ?", (plid,))
    assert row["spec_path"] is not None
    fake_bs.write_and_commit.assert_called_once()


async def test_backfill_noop_when_no_spec_column(db, mocker):
    # The normal post-Spec-2 plans table has no `spec` column -> returns 0.
    fake_bs = mocker.Mock()
    count = await backfill_legacy_specs(db, fake_bs)
    assert count == 0
    fake_bs.write_and_commit.assert_not_called()
