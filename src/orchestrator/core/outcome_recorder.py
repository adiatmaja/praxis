"""Fire-and-forget outcome recorder for capability calibration."""

from __future__ import annotations

import logging
import uuid

from orchestrator.core.capability_events import (
    CapabilityEventEmitter,
    OutcomeRecordedEvent,
)
from orchestrator.core.failure_taxonomy import counts_against_worker
from orchestrator.database import Database


logger = logging.getLogger(__name__)


async def record_outcome(
    db: Database,
    *,
    task_id: str,
    plan_id: str | None,
    project_id: str,
    model_name: str | None,
    harness: str | None,
    task_type: str | None,
    files_touched: int | None,
    loc_delta: int | None,
    context_tokens_est: int | None,
    attempt: int,
    outcome: str,
    failure_class: str | None,
    emitter: CapabilityEventEmitter | None,
) -> str:
    """Record a task outcome row and optionally emit a capability event.

    This is a fire-and-forget writer: it never raises into the caller.
    All exceptions are logged and swallowed.

    Returns:
        The ``row_id`` (UUID) of the inserted row.
    """

    row_id = str(uuid.uuid4())

    # Determine whether this failure counts against the worker.
    # counts_against_worker raises ValueError on unknown strings,
    # so the bool guard prevents a crash on bogus failure_class values.
    against = False
    try:
        against = bool(failure_class) and counts_against_worker(failure_class)
    except (ValueError, TypeError):
        against = False

    # Insert the outcome row.
    try:
        await db.execute(
            """
            INSERT INTO task_outcomes
                (id, task_id, project_id, model_name, harness, task_type,
                 files_touched, loc_delta, context_tokens_est, attempt,
                 outcome, failure_class, split_depth, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'run')
            """,
            (
                row_id,
                task_id,
                project_id,
                model_name,
                harness,
                task_type,
                files_touched,
                loc_delta,
                context_tokens_est,
                attempt,
                outcome,
                failure_class,
            ),
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to insert task_outcome row %s", row_id)

    # Emit capability event (only when emitter is available and plan_id is set).
    if emitter is not None and plan_id is not None:
        try:
            await emitter.emit(
                OutcomeRecordedEvent(
                    plan_id=plan_id,
                    task_id=task_id,
                    outcome_id=row_id,
                    failure_class=failure_class,
                    counts_against_worker=against,
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to emit OutcomeRecordedEvent for %s", row_id)

    return row_id
