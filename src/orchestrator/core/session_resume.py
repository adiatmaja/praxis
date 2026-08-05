"""Decide whether a re-dispatch may resume the worker's previous session.

Resume is safe only when the worker's restored memory matches the tree it will
be handed. See docs/superpowers/specs/2026-08-05-worker-session-resume-design.md.
"""

from __future__ import annotations

from typing import Any

from orchestrator.core.clarification_states import RESUMABLE_CLARIFICATION_STATES


def resolve_resume_session(task: dict[str, Any], harness: str) -> str | None:
    """Return the session id to replay, or None to start cold.

    Args:
        task: Task row, as returned by ``TaskQueue.get_task``.
        harness: Harness about to be spawned for this dispatch.

    Returns:
        The stored session id when all three replay conditions hold, else None.
    """
    session_id = task.get("worker_session_id")
    # An id is only ever stored after the previous turn's checkpoint push
    # succeeded, so its absence means there is nothing safe to resume onto.
    if not session_id:
        return None
    # A conversation id minted by one harness is meaningless to the other.
    if task.get("worker_session_harness") != harness:
        return None
    if task.get("clarification_state") not in RESUMABLE_CLARIFICATION_STATES:
        return None
    return str(session_id)
