"""Canonical vocabulary for the worker clarification-state lifecycle.

The four ``tasks.clarification_state`` values were previously written as bare
string literals, independently, at three different modules:
``TaskQueue.mark_needs_clarification`` (task_queue.py), the review loop's
``handle_clarification``/``_park_awaiting_human`` (orchestrator_review.py),
and the human ``POST /tasks/{id}/clarify`` endpoint (api/tasks.py). Two of
those same values are read by the worker-session-resume gate
(``core.session_resume.resolve_resume_session``), which only permits a
harness to replay its previous conversation once an answer has landed.

That gate held its own second, independent copy of the two post-answer
strings. If any write site's literal drifted (a rename, a typo) out of sync
with the gate's copy, ``resolve_resume_session`` would simply stop matching
and return ``None`` forever -- no exception, no failing test, no log line.
The resume feature would look like it had just quietly stopped triggering.
This module is the single source of truth so every site imports the same
four constants instead of re-typing them.
"""

from __future__ import annotations


# The worker asked a question and is waiting on an answer; nothing has
# resolved it yet. Written by ``TaskQueue.mark_needs_clarification``.
ASKED = "asked"

# The brain answered the question with confidence at or above the project's
# threshold. Written by ``ReviewMixin.handle_clarification`` via
# ``TaskQueue.record_clarification_answer``.
ANSWERED_BY_BRAIN = "answered_by_brain"

# The brain's answer was low-confidence, or the brain call itself failed;
# parked for a human to answer. Written by
# ``ReviewMixin._park_awaiting_human``.
AWAITING_HUMAN = "awaiting_human"

# A human answered directly via ``POST /tasks/{id}/clarify``. Written by
# ``api.tasks.clarify_task`` via ``TaskQueue.record_clarification_answer``.
RESOLVED = "resolved"

# Every value the `tasks.clarification_state` column can hold.
ALL_CLARIFICATION_STATES: frozenset[str] = frozenset(
    {ASKED, ANSWERED_BY_BRAIN, AWAITING_HUMAN, RESOLVED}
)

# Post-answer states: an answer has been recorded, whether by the brain or a
# human. `core.session_resume.resolve_resume_session` only treats a stored
# worker session as safe to replay when the task is in one of these states.
RESUMABLE_CLARIFICATION_STATES: frozenset[str] = frozenset(
    {ANSWERED_BY_BRAIN, RESOLVED}
)
