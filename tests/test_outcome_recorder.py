"""Tests for core/outcome_recorder.record_outcome."""

from __future__ import annotations

from orchestrator.core.capability_events import (
    CapabilityEventEmitter,
)
from orchestrator.core.event_bus import EventBus
from orchestrator.core.outcome_recorder import record_outcome
from orchestrator.database import Database


async def _make_db(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'recorder.db'}")
    await db.initialize()
    return db


async def test_record_outcome_inserts_row(tmp_path):
    """record_outcome inserts a row into task_outcomes and returns row_id."""
    db = await _make_db(tmp_path)
    try:
        row_id = await record_outcome(
            db=db,
            task_id="task-1",
            plan_id="plan-1",
            project_id="proj-1",
            model_name="qwen3",
            harness="opencode",
            task_type="feature",
            files_touched=3,
            loc_delta=120,
            context_tokens_est=None,
            attempt=1,
            outcome="pass",
            failure_class=None,
            emitter=None,
        )

        assert row_id is not None
        row = await db.fetch_one("SELECT * FROM task_outcomes WHERE id = ?", (row_id,))
        assert row is not None
        assert row["outcome"] == "pass"
        assert row["files_touched"] == 3
        assert row["source"] == "run"
        assert row["split_depth"] == 0
        assert row["failure_class"] is None
    finally:
        await db.close()


async def test_record_outcome_verify_fail_emits_event(tmp_path):
    """failure_class='verify_fail' emits one OutcomeRecordedEvent with counts_against_worker=True."""
    db = await _make_db(tmp_path)
    bus = EventBus()
    queue = bus.subscribe()
    emitter = CapabilityEventEmitter(db, bus)

    try:
        row_id = await record_outcome(
            db=db,
            task_id="task-2",
            plan_id="plan-2",
            project_id="proj-1",
            model_name="qwen3",
            harness="opencode",
            task_type=None,
            files_touched=1,
            loc_delta=50,
            context_tokens_est=None,
            attempt=2,
            outcome="fail",
            failure_class="verify_fail",
            emitter=emitter,
        )

        assert row_id is not None

        # Check DB row
        row = await db.fetch_one("SELECT * FROM task_outcomes WHERE id = ?", (row_id,))
        assert row is not None
        assert row["outcome"] == "fail"
        assert row["failure_class"] == "verify_fail"

        # Check event bus
        event = queue.get_nowait()
        assert event["type"] == "capability.outcome_recorded"
        assert event["plan_id"] == "plan-2"
        assert event["task_id"] == "task-2"
        assert event["counts_against_worker"] is True
    finally:
        await db.close()


async def test_record_outcome_provider_error_no_count(tmp_path):
    """failure_class='provider_error' emits counts_against_worker=False."""
    db = await _make_db(tmp_path)
    bus = EventBus()
    queue = bus.subscribe()
    emitter = CapabilityEventEmitter(db, bus)

    try:
        row_id = await record_outcome(
            db=db,
            task_id="task-3",
            plan_id="plan-3",
            project_id="proj-1",
            model_name="qwen3",
            harness="opencode",
            task_type=None,
            files_touched=0,
            loc_delta=0,
            context_tokens_est=None,
            attempt=1,
            outcome="fail",
            failure_class="provider_error",
            emitter=emitter,
        )

        assert row_id is not None

        event = queue.get_nowait()
        assert event["type"] == "capability.outcome_recorded"
        assert event["counts_against_worker"] is False
    finally:
        await db.close()


async def test_record_outcome_no_emitter_no_event(tmp_path):
    """When emitter is None, no event is published (even though DB row is written)."""
    db = await _make_db(tmp_path)
    bus = EventBus()
    queue = bus.subscribe()

    try:
        row_id = await record_outcome(
            db=db,
            task_id="task-4",
            plan_id="plan-4",
            project_id="proj-1",
            model_name="qwen3",
            harness="opencode",
            task_type=None,
            files_touched=2,
            loc_delta=80,
            context_tokens_est=None,
            attempt=1,
            outcome="pass",
            failure_class=None,
            emitter=None,
        )

        assert row_id is not None
        row = await db.fetch_one("SELECT * FROM task_outcomes WHERE id = ?", (row_id,))
        assert row is not None

        # Bus should be empty
        assert queue.empty()
    finally:
        await db.close()


async def test_record_outcome_no_plan_id_no_event(tmp_path):
    """When plan_id is None, no event is published."""
    db = await _make_db(tmp_path)
    bus = EventBus()
    queue = bus.subscribe()
    emitter = CapabilityEventEmitter(db, bus)

    try:
        row_id = await record_outcome(
            db=db,
            task_id="task-5",
            plan_id=None,
            project_id="proj-1",
            model_name="qwen3",
            harness="opencode",
            task_type=None,
            files_touched=1,
            loc_delta=30,
            context_tokens_est=None,
            attempt=1,
            outcome="pass",
            failure_class=None,
            emitter=emitter,
        )

        assert row_id is not None
        assert queue.empty()
    finally:
        await db.close()


async def test_record_outcome_never_raises(tmp_path):
    """record_outcome never raises into the caller, even on DB errors."""
    db = await _make_db(tmp_path)
    try:
        # Pass an invalid failure_class string that would make
        # counts_against_worker raise ValueError.
        row_id = await record_outcome(
            db=db,
            task_id="task-6",
            plan_id="plan-6",
            project_id="proj-1",
            model_name="qwen3",
            harness="opencode",
            task_type=None,
            files_touched=0,
            loc_delta=0,
            context_tokens_est=None,
            attempt=1,
            outcome="fail",
            failure_class="unknown_bad_string",
            emitter=None,
        )
        # Should return a row_id even though the failure_class is bogus
        assert row_id is not None
    finally:
        await db.close()
