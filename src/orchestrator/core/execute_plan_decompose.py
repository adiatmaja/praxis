"""Shared brain-driven decomposition of an externally-authored plan.

Extracted from api/execute_plan.py so the endpoint (fast path) and the
orchestration loop (async path) run one identical implementation.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from orchestrator.core.capability_history import summarize_outcomes
from orchestrator.core.context_scrub import scrub_context
from orchestrator.core.plan_derive import slugify
from orchestrator.core.plan_review import (
    PlanReviewError,
    build_review_prompt,
    parse_review_response,
)


logger = logging.getLogger(__name__)

# Fraction of the model's context window reserved for a single leaf's context.
_LEAF_BUDGET_FRACTION = 0.4

# Total brain-decomposition attempts. High-effort models occasionally emit
# unparseable output; one retry self-heals a stochastic bad draw. The brain is
# a subscription CLI call (no per-call dollar cost), so a single retry is cheap.
_DECOMPOSE_ATTEMPTS = 2


def normalize_slugs(opus_plan: dict[str, Any]) -> None:
    """Add a unique ``slug`` to each task and remap ``depends_on`` ids -> slugs."""
    id_to_slug: dict[str, str] = {}
    seen: set[str] = set()
    for task in opus_plan["tasks"]:
        slug = slugify(str(task.get("title") or task.get("id") or "task"))
        while slug in seen:
            slug = f"{slug}-{uuid.uuid4().hex[:4]}"
        seen.add(slug)
        task["slug"] = slug
        if "id" in task:
            id_to_slug[str(task["id"])] = slug
    for task in opus_plan["tasks"]:
        deps = task.get("depends_on") or []
        task["depends_on"] = [id_to_slug.get(str(d), str(d)) for d in deps]


def branch_slug(text: str) -> str:
    """Build a short branch-safe slug from free text plus a uniqueness suffix."""
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "plan"
    return f"{base}-{uuid.uuid4().hex[:6]}"


async def decompose_plan(
    plan: str,
    model: str,
    context: str | None,
    router: Any,
    effective_settings: Any,
    project_id: str | None,
) -> dict[str, Any]:
    """Capability-review a plan into a normalized opus_plan task graph.

    Args:
        plan: The externally-authored plan text to decompose.
        model: Local worker model name (used for capability profiling).
        context: Optional caller-supplied context to thread onto each leaf.
        router: LLMRouter-compatible object with ``run(call_site, prompt, project_id)``
        effective_settings: Object with ``capability_profile(project_id, model)``
            returning a profile with a ``context_window`` attribute.
        project_id: Project id for router routing context; may be None.

    Returns:
        A normalized ``{"tasks": [...]}`` dict where each task has a ``slug``
        and ``depends_on`` holds slugs (not brain ids).

    Raises:
        PlanReviewError: If the brain output cannot be parsed.
    """
    profile = await effective_settings.capability_profile(project_id=None, model=model)
    per_leaf_budget = int(profile.context_window * _LEAF_BUDGET_FRACTION)
    history = summarize_outcomes([])
    prompt = build_review_prompt(plan, profile, history, per_leaf_budget)

    last_exc: PlanReviewError | None = None
    for attempt in range(1, _DECOMPOSE_ATTEMPTS + 1):
        raw = await router.run("plan_review", prompt, project_id=project_id)
        try:
            opus_plan = parse_review_response(raw)
            break
        except PlanReviewError as exc:
            last_exc = exc
            logger.warning(
                "Decomposition parse failed (attempt %d/%d): %s",
                attempt,
                _DECOMPOSE_ATTEMPTS,
                exc,
            )
    else:
        raise (
            last_exc
            if last_exc is not None
            else PlanReviewError(  # noqa: EM101
                "decomposition failed with no parseable output"
            )
        )
    normalize_slugs(opus_plan)

    scrubbed_context = scrub_context(context)
    if scrubbed_context is not None:
        for task in opus_plan["tasks"]:
            task.setdefault("context_text", scrubbed_context)
    return opus_plan
