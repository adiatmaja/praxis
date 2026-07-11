"""Versioned decision-record models for the capability engine.

These Pydantic models capture every capability-engine decision as an
immutable, version-stamped event.  The emitter that produces and
persists these events is delivered in Task 7 (module stubbed here).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.core.event_bus import EventBus
from orchestrator.database import Database


CAPABILITY_EVENT_SCHEMA_VERSION: int = 1


class _CapabilityEvent(BaseModel):
    """Base class for all capability-engine decision-record events."""

    model_config = ConfigDict(extra="forbid")
    schema_version: int = CAPABILITY_EVENT_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Individual event types
# ---------------------------------------------------------------------------


class DecomposeInputEvent(_CapabilityEvent):
    """Recorded when the brain receives a plan for decomposition."""

    event_type: Literal["decompose_input"] = "decompose_input"
    plan_id: str
    model_name: str
    per_leaf_budget: int
    profile_summary: str
    history_summary_hash: str
    plan_hash: str


class LeafValidatedEvent(_CapabilityEvent):
    """Recorded when a leaf task passes the capability gate."""

    event_type: Literal["leaf_validated"] = "leaf_validated"
    plan_id: str
    leaf_slug: str


class LeafRejectedEvent(_CapabilityEvent):
    """Recorded when a leaf task fails the capability gate."""

    event_type: Literal["leaf_rejected"] = "leaf_rejected"
    plan_id: str
    leaf_slug: str
    rule_id: str
    measured: int | str | None = None
    limit: int | str | None = None


class PlanRejectedEvent(_CapabilityEvent):
    """Recorded when the entire plan is rejected after review rounds."""

    event_type: Literal["plan_rejected"] = "plan_rejected"
    plan_id: str
    violations: list[str]
    rounds: int


class TaskSplitEvent(_CapabilityEvent):
    """Recorded when a task is split into smaller sub-tasks."""

    event_type: Literal["task_split"] = "task_split"
    plan_id: str
    parent_slug: str
    child_slugs: list[str]
    tightened_limits: dict[str, Any] = Field(default_factory=dict)
    failure_evidence_ref: str | None = None


class TaskEscalatedEvent(_CapabilityEvent):
    """Recorded when a task is escalated (e.g. needs a stronger model)."""

    event_type: Literal["task_escalated"] = "task_escalated"
    plan_id: str
    leaf_slug: str
    policy: str


class OutcomeRecordedEvent(_CapabilityEvent):
    """Recorded when a task outcome is logged for capability calibration."""

    event_type: Literal["outcome_recorded"] = "outcome_recorded"
    plan_id: str
    task_id: str
    outcome_id: str
    failure_class: str | None = None
    counts_against_worker: bool = False


# ---------------------------------------------------------------------------
# Union and registry
# ---------------------------------------------------------------------------

CapabilityEventModel = (
    DecomposeInputEvent
    | LeafValidatedEvent
    | LeafRejectedEvent
    | PlanRejectedEvent
    | TaskSplitEvent
    | TaskEscalatedEvent
    | OutcomeRecordedEvent
)

CAPABILITY_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "decompose_input",
        "leaf_validated",
        "leaf_rejected",
        "plan_rejected",
        "task_split",
        "task_escalated",
        "outcome_recorded",
    }
)


# ---------------------------------------------------------------------------
# Emitter
# ---------------------------------------------------------------------------


class CapabilityEventEmitter:
    """Dual-writes capability events to the database and the event bus."""

    def __init__(self, db: Database, bus: EventBus) -> None:
        self._db = db
        self._bus = bus

    async def emit(self, event: CapabilityEventModel) -> str:
        """Persist the event and broadcast it.

        Returns the row ID that was inserted.
        """
        payload = event.model_dump()
        row_id = str(uuid.uuid4())
        plan_id = payload.get("plan_id")
        task_id = payload.get("task_id")

        await self._db.execute(
            """
            INSERT INTO capability_events
                (id, schema_version, event_type, plan_id, task_id,
                 payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_id,
                event.schema_version,
                event.event_type,
                plan_id,
                task_id,
                json.dumps(payload),
                datetime.now(UTC).isoformat(),
            ),
        )

        self._bus.publish(
            {
                "type": f"capability.{event.event_type}",
                **payload,
            }
        )

        return row_id
