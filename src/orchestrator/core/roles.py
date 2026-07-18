"""Frozen call-site-to-role mappings."""

from __future__ import annotations

MODEL_ROLES: tuple[str, ...] = ("plan", "review", "implement")
ROLE_OF_CALL_SITE: dict[str, str] = {
    "plan_spec": "plan",
    "plan_review": "plan",
    "derive_tasks": "plan",
    "analyze_improvements": "plan",
    "answer_clarification": "plan",
    "classify_doc": "plan",
    "context_sync": "plan",
    "brainstorm_run_turn": "plan",
    "brainstorm_generate_plan": "plan",
    "review_diff_first": "review",
    "review_diff_rereview": "review",
}
