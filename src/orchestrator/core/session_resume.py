"""Decide whether a re-dispatch may resume the worker's previous session.

Resume is safe only when the worker's restored memory matches the tree it will
be handed. See docs/superpowers/specs/2026-08-05-worker-session-resume-design.md.
"""

from __future__ import annotations

from typing import Any


# States set by TaskQueue.record_clarification_answer once a blocked worker's
# question has been answered. Any other state means this is a failure retry,
# which deliberately rebuilds the branch from base.
RESUMABLE_CLARIFICATION_STATES = frozenset({"answered_by_brain", "resolved"})


def resolve_resume_session(task: dict[str, Any], harness: str) -> str | None:
    """Return the session id to replay, or None to start cold.

    Args:
        task: Task row, as returned by ``TaskQueue.get_task``.
        harness: Harness about to be spawned for this dispatch.

    Returns:
        The stored session id when all three replay conditions hold, else None.
    """
    session_id = task.get("worker_session_id")
    if not session_id:
        return None
    if task.get("worker_session_harness") != harness:
        return None
    if task.get("clarification_state") not in RESUMABLE_CLARIFICATION_STATES:
        return None
    return str(session_id)
