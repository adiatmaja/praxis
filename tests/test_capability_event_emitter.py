"""Tests for CapabilityEventEmitter dual-write behaviour (S1).

Verifies that emitting a capability event writes a row to the
capability_events table AND publishes to the EventBus.
"""

from __future__ import annotations

from orchestrator.core.capability_events import (
    CapabilityEventEmitter,
    LeafValidatedEvent,
    OutcomeRecordedEvent,
)
from orchestrator.core.event_bus import EventBus
from orchestrator.database import Database


async def _make_db(tmp_path):
    """Create and initialize a temporary database."""

    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'emitter.db'}")
    await db.initialize()
    return db


async def test_emit_writes_row_to_database(tmp_path):
    """emit() persists a record to the capability_events table."""
    db = await _make_db(tmp_path)
    bus = EventBus()
    emitter = CapabilityEventEmitter(db, bus)

    event = LeafValidatedEvent(plan_id="plan-1", leaf_slug="task-a")
    row_id = await emitter.emit(event)

    assert row_id is not None

    row = await db.fetch_one("SELECT * FROM capability_events WHERE id = ?", (row_id,))
    assert row is not None
    assert row["schema_version"] == 1
    assert row["event_type"] == "leaf_validated"
    assert row["plan_id"] == "plan-1"
    assert "leaf_slug" in row["payload"]

    await db.close()


async def test_emit_publishes_to_event_bus(tmp_path):
    """emit() broadcasts the event to the EventBus."""
    db = await _make_db(tmp_path)
    bus = EventBus()
    queue = bus.subscribe()
    emitter = CapabilityEventEmitter(db, bus)

    event = LeafValidatedEvent(plan_id="plan-1", leaf_slug="task-a")
    await emitter.emit(event)

    payload = queue.get_nowait()
    assert payload["type"] == "capability.leaf_validated"
    assert payload["plan_id"] == "plan-1"

    await db.close()


async def test_emit_outcome_recorded_extracts_task_id(tmp_path):
    """emit() stores task_id for OutcomeRecordedEvent."""
    db = await _make_db(tmp_path)
    bus = EventBus()
    emitter = CapabilityEventEmitter(db, bus)

    event = OutcomeRecordedEvent(
        plan_id="plan-5",
        task_id="task-1",
        outcome_id="outcome-1",
    )
    row_id = await emitter.emit(event)

    row = await db.fetch_one("SELECT * FROM capability_events WHERE id = ?", (row_id,))
    assert row is not None
    assert row["event_type"] == "outcome_recorded"
    assert row["plan_id"] == "plan-5"
    assert row["task_id"] == "task-1"

    await db.close()
