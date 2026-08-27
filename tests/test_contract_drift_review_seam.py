"""The contract-drift finding must actually be WRITTEN by a real review.

The pure rules live in ``tests/test_contract_drift.py``. This covers the seam,
which is where this class of feature goes inert: a correct predicate and a
correct renderer, wired to a query that never runs (see ``docs/gotchas.md``,
"A new surface's QUERY is the seam that goes inert").
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.core.capability_events import CapabilityEventEmitter
from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus


# The plan authorises ONE path and names a second as its contract - the shape
# of the round-7 fabrication, reduced to the two lines that matter.
_PLAN_DOCUMENT = """\
# Plan: build the thing

`src/test_thing.py` is the acceptance bar. Do not edit, weaken or delete it.

## Task 1: build it

Files: `src/thing.py`

Steps:
- Implement everything the acceptance bar asks for.

Acceptance: `python -m pytest src/test_thing.py -q` passes.
"""


def _diff(*paths: str) -> str:
    return "\n".join(f"--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n-old\n+new" for p in paths)


def _make_opus(verdict: str = "pass") -> AsyncMock:
    opus = AsyncMock()
    opus.is_available = AsyncMock(return_value=True)
    opus.review_diff = AsyncMock(return_value={"verdict": verdict, "feedback": "ok"})
    return opus


def _make_git(diff: str) -> MagicMock:
    git = MagicMock()
    git.extract_pr_number = AsyncMock(return_value=42)
    git.repo_slug = MagicMock(return_value="u/repo")
    git.clone_pr_head = AsyncMock()
    git.get_pr_diff = AsyncMock(return_value=diff)
    git.comment_on_pr = AsyncMock()
    git.merge_pr = AsyncMock()
    return git


async def _seed(db: Database, *, pending_input: str | None) -> tuple[TaskQueue, str]:
    tq = TaskQueue(db)
    await db.execute(
        "INSERT OR IGNORE INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u1", "User", "hash"),
    )
    await db.execute(
        """INSERT OR IGNORE INTO projects
           (id, user_id, name, repo_url, model_name, max_retries, auto_merge,
            agent_model, harness)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "proj-drift",
            "u1",
            "P",
            "https://github.com/u/repo",
            "qwen3",
            3,
            0,
            "qwen3",
            "opencode",
        ),
    )
    opus_plan = {
        "plan_summary": "Test",
        "plan_slug": "test",
        "tasks": [
            {
                "title": "Build it",
                "slug": "build-it",
                "description": "Build it",
                "depends_on": [],
                "plan_text": "Goal: build it\nFiles: src/thing.py",
            }
        ],
    }
    await db.execute(
        """INSERT OR IGNORE INTO plans
           (id, project_id, source, opus_plan, status, plan_branch_name,
            pending_input)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            "plan-drift",
            "proj-drift",
            "execute-plan",
            json.dumps(opus_plan),
            "active",
            "plan/2026-08-27-test",
            pending_input,
        ),
    )
    await db.execute(
        """INSERT OR IGNORE INTO tasks
           (id, plan_id, title, description, branch_name, pr_url, status, attempt)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "task-drift",
            "plan-drift",
            "Build it",
            "Build it",
            "agent/build-it",
            "https://github.com/u/repo/pull/42",
            TaskStatus.REVIEWING,
            1,
        ),
    )
    return tq, "task-drift"


async def _review(db: Database, diff: str, pending_input: str | None) -> Any:
    tq, task_id = await _seed(db, pending_input=pending_input)
    bus = EventBus()
    orch = Orchestrator(
        task_queue=tq,
        agent_manager=MagicMock(),
        opus_bridge=_make_opus(),
        git_ops=_make_git(diff),
        event_bus=bus,
    )
    orch._emitter = CapabilityEventEmitter(db, bus)  # type: ignore[attr-defined]
    orch._start_monitor = lambda *_: None  # type: ignore[assignment]
    project = await db.fetch_one("SELECT * FROM projects WHERE id = ?", ("proj-drift",))
    assert project is not None
    await orch.review_task(task_id, project)
    row = await db.fetch_one(
        "SELECT contract_drift FROM tasks WHERE id = ?", (task_id,)
    )
    assert row is not None
    return row["contract_drift"]


@pytest.mark.asyncio
async def test_review_records_the_contract_file_the_diff_edited(tmp_path):
    """A PASS'd review still records that the diff touched the plan's bar.

    This is the whole feature: the verdict is ``pass`` and correct, because the
    reviewer grades against the leaf's ``plan_text`` - and the human at the
    merge gate now has the one fact that verdict cannot carry.
    """
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'seam.db').as_posix()}")
    await db.initialize()
    try:
        stored = await _review(
            db,
            _diff("src/thing.py", "src/test_thing.py"),
            json.dumps({"plan": _PLAN_DOCUMENT, "model": "qwen3"}),
        )
    finally:
        await db.close()

    assert stored is not None, "the review wrote no drift row at all"
    payload = json.loads(stored)
    assert payload["gradable"] is True
    assert payload["named_not_authorised"] == ["src/test_thing.py"]
    assert "acceptance contract" in payload["summary"]


@pytest.mark.asyncio
async def test_review_records_a_clean_result_distinguishably(tmp_path):
    """Staying inside the authorised path is RECORDED, not left NULL.

    A NULL means "never computed", so a clean review has to write something or
    the merge gate cannot tell a checked task from an unchecked one.
    """
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'clean.db').as_posix()}")
    await db.initialize()
    try:
        stored = await _review(
            db,
            _diff("src/thing.py"),
            json.dumps({"plan": _PLAN_DOCUMENT, "model": "qwen3"}),
        )
    finally:
        await db.close()

    payload = json.loads(stored)
    assert payload["gradable"] is True
    assert payload["named_not_authorised"] == []
    assert payload["unmentioned"] == []


@pytest.mark.asyncio
async def test_review_of_a_task_with_no_plan_document_says_so(tmp_path):
    """A bare dispatch has no plan document; the row says that, in words.

    Not a NULL and not a clean result: the reason is stored so every surface
    can render "not graded, and here is why" rather than inventing either.
    """
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'nodoc.db').as_posix()}")
    await db.initialize()
    try:
        stored = await _review(db, _diff("src/thing.py"), None)
    finally:
        await db.close()

    payload = json.loads(stored)
    assert payload["gradable"] is False
    assert "not graded" in payload["summary"]


# ---------------------------------------------------------------------------
# The surfaces a human actually reads
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_approvals_summary_carries_the_finding():
    """``summarize_pending`` feeds the API, the digest, MCP and the dashboard.

    Testing the reader rather than only the writer: a correct predicate wired
    to a query that never selects the column is exactly how this class of
    feature ships inert.
    """
    from orchestrator.core.approvals import summarize_pending

    payload = json.dumps(
        {
            "gradable": True,
            "why_not": "",
            "named_not_authorised": ["src/test_thing.py"],
            "unmentioned": [],
            "summary": "Plan paths: this diff edits src/test_thing.py",
        }
    )
    summary = summarize_pending(
        [
            {
                "id": "t1",
                "title": "T",
                "status": TaskStatus.PASSED,
                "branch_name": "agent/t",
                "pr_url": "https://x/pull/1",
                "updated_at": "2026-08-27T00:00:00+00:00",
                "review_feedback": "fine",
                "contract_drift": payload,
            }
        ]
    )

    drift = summary["tasks"][0]["contract_drift"]
    assert drift is not None, "the merge-gate reader dropped the finding"
    assert drift["named_not_authorised"] == ["src/test_thing.py"]


@pytest.mark.unit
def test_a_task_that_was_never_checked_is_not_reported_as_clean():
    """NULL means "not checked" and must survive as ``None`` to the surfaces."""
    from orchestrator.core.approvals import summarize_pending

    summary = summarize_pending(
        [
            {
                "id": "t1",
                "title": "T",
                "status": TaskStatus.PASSED,
                "branch_name": "agent/t",
                "pr_url": "https://x/pull/1",
                "updated_at": "2026-08-27T00:00:00+00:00",
                "review_feedback": "fine",
                "contract_drift": None,
            }
        ]
    )

    assert summary["tasks"][0]["contract_drift"] is None


@pytest.mark.unit
def test_mcp_poll_task_summary_warns_on_the_strong_tier_only():
    """An assistant relaying "awaiting_merge" must relay this with it.

    The weak tier stays out of the one-line summary: a new sibling file is the
    normal output of the decompose prompt's own sizing rule, and warning about
    it in the sentence an assistant repeats to a human would bury the tier that
    caught a real fabrication.
    """
    from mcp_server.server import _task_summary

    strong = _task_summary(
        {
            "title": "T",
            "status": "passed",
            "contract_drift": {"named_not_authorised": ["src/test_thing.py"]},
        }
    )
    weak = _task_summary(
        {
            "title": "T",
            "status": "passed",
            "contract_drift": {
                "named_not_authorised": [],
                "unmentioned": ["src/__init__.py"],
            },
        }
    )

    assert "src/test_thing.py" in strong
    assert "never authorised" in strong
    assert "WARNING" not in weak
