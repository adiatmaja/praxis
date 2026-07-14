"""Shared brain-driven decomposition of an externally-authored plan.

Extracted from api/execute_plan.py so the endpoint (fast path) and the
orchestration loop (async path) run one identical implementation.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from typing import Any

from orchestrator.core.capability_events import (
    DecomposeInputEvent,
    LeafRejectedEvent,
    LeafValidatedEvent,
    PlanRejectedEvent,
)
from orchestrator.core.capability_history import summarize_outcomes
from orchestrator.core.context_scrub import scrub_context
from orchestrator.core.leaf_validator import (
    format_violations_feedback,
    validate_leaves,
)
from orchestrator.core.plan_derive import slugify
from orchestrator.core.plan_review import (
    PlanReviewError,
    build_review_prompt,
    parse_review_response,
)
from orchestrator.core.token_budget import WORKER_RESERVE_FRACTION
from orchestrator.models.schemas import LeafTask


logger = logging.getLogger(__name__)

# Total brain-decomposition attempts. High-effort models occasionally emit
# unparseable output; one retry self-heals a stochastic bad draw. The brain is
# a subscription CLI call (no per-call dollar cost), so a single retry is cheap.
_DECOMPOSE_ATTEMPTS = 2

# Patterns that identify a leaf as verification-only (no source edits expected).
# Conservative: only match when the entire purpose of the task is running checks.
_VERIFY_ONLY_RE: re.Pattern[str] = re.compile(
    r"""
    run\s+the\s+(test\s+)?suite         # "run the test suite" / "run the suite"
    | run\s+(pytest|mypy|ruff)\b        # "run pytest", "run mypy", "run ruff"
    | verification[-\s]only             # "verification only" / "verification-only"
    | commit\s+only\s+if               # "commit only if formatting changed"
    | no\s+(source|code)\s+changes?     # "no source changes" / "no code change"
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Guard: if a leaf also contains any of these phrases, it is real implementation
# work even if a verify-only phrase appears incidentally.
_REAL_WORK_RE: re.Pattern[str] = re.compile(
    r"""
    implement | add\s+test | write\s+test | create\s+test
    | build | refactor | fix | update | migrate | introduce
    | new\s+(class|function|module|endpoint|feature)
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Sibling guard: a leaf that names a concrete docs/file edit is real work even
# when it also runs the suite (e.g. a terminal "finalize" step that updates
# CLAUDE.md AND runs the full gate). Conservative: only match when the leaf
# clearly names a doc/file to edit, not incidental mentions.
_DOCS_EDIT_RE: re.Pattern[str] = re.compile(
    r"""
    \bCLAUDE\.md\b
    | \bREADME\b
    | \bdocs/                                # a docs/ path
    | \bgotcha\b
    | \bdocumentation\b
    | (update|edit|add)[^.\n]*\.md\b         # "update ... foo.md"
    | add[^.\n]*\bline(s)?\s+to\b            # "add 3 lines to ..."
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Header pattern for authored "### Task N" leaves in an externally-authored plan.
_PLAN_TASK_HEADER_RE: re.Pattern[str] = re.compile(
    r"^###\s+Task\s+\d+", re.IGNORECASE | re.MULTILINE
)


def count_plan_tasks(plan: str) -> int:
    """Count ``### Task N`` markdown headers in an authored plan.

    Matches headers case-insensitively and allows optional trailing text after
    the number (e.g. ``### Task 8: Finalize``).

    Args:
        plan: The externally-authored plan text.

    Returns:
        The number of ``### Task N`` headers found (0 if none).
    """
    return len(_PLAN_TASK_HEADER_RE.findall(plan))


def _is_verification_only(task: dict[str, Any]) -> bool:
    """Return True when a task is purely verification with no source edits expected.

    Args:
        task: A task dict with at least ``title``, ``description``, and
            ``plan_text`` keys (any may be absent or None).

    Returns:
        True if the task matches a verification-only pattern and does NOT also
        match a real-implementation guard phrase.
    """
    text = " ".join(
        str(task.get(field) or "") for field in ("title", "description", "plan_text")
    )
    if not _VERIFY_ONLY_RE.search(text):
        return False
    if _REAL_WORK_RE.search(text):
        return False
    return not _DOCS_EDIT_RE.search(text)


def drop_verification_only_leaves(opus_plan: dict[str, Any]) -> dict[str, Any]:
    """Remove verification-only tasks from an opus_plan and repair depends_on refs.

    Verification is already covered mechanically by ``core/verify_gate.py`` plus
    per-leaf Opus review, so a brain-emitted leaf whose sole job is "run the test
    suite and commit only if formatting changed" will never produce a git diff.
    The agent entrypoint treats no-diff as failure and the orchestrator re-dispatches
    up to three times before marking the task ``failed``. Dropping these leaves at
    decompose time avoids the wasted retries.

    After dropping, any ``depends_on`` reference to a removed slug is also removed
    so the wave scheduler does not deadlock waiting for a task that no longer exists.

    Args:
        opus_plan: A normalized ``{"tasks": [...]}`` dict (slugs already assigned).

    Returns:
        The same dict mutated in place (also returned for convenience).
    """
    tasks: list[dict[str, Any]] = opus_plan.get("tasks", [])
    dropped_slugs: set[str] = set()

    retained: list[dict[str, Any]] = []
    for task in tasks:
        if _is_verification_only(task):
            slug = task.get("slug", task.get("title", "<unknown>"))
            logger.info(
                "Dropping verification-only leaf %r; "
                "coverage is handled by verify_gate + Opus review.",
                slug,
            )
            dropped_slugs.add(str(slug))
        else:
            retained.append(task)

    if dropped_slugs:
        for task in retained:
            original_deps: list[str] = task.get("depends_on") or []
            task["depends_on"] = [d for d in original_deps if d not in dropped_slugs]

    opus_plan["tasks"] = retained
    return opus_plan


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
    local_context: str | None = None,
    plan_id: str | None = None,
    emitter: Any = None,
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
        local_context: Optional local context to thread onto each leaf as repo_memory.
        plan_id: Plan identifier (reserved for capability event wiring).
        emitter: Capability event emitter (reserved for capability event wiring).

    Returns:
        A normalized ``{"tasks": [...]}`` dict where each task has a ``slug``
        and ``depends_on`` holds slugs (not brain ids). Verification-only leaves
        are removed before the dict is returned.

    Raises:
        PlanReviewError: If the brain output cannot be parsed, or if HARD
            validation violations remain after the informed re-decompose round.
    """
    profile = await effective_settings.capability_profile(project_id=None, model=model)
    per_leaf_budget = int(profile.context_window * (1 - WORKER_RESERVE_FRACTION))
    history = summarize_outcomes([])
    prompt = build_review_prompt(plan, profile, history, per_leaf_budget)

    if emitter is not None and plan_id is not None:
        await emitter.emit(
            DecomposeInputEvent(
                plan_id=plan_id,
                model_name=model,
                per_leaf_budget=per_leaf_budget,
                profile_summary=f"{profile.model_name}/{profile.context_window}",
                history_summary_hash=hashlib.sha256(history.encode()).hexdigest()[:16],
                plan_hash=hashlib.sha256(plan.encode()).hexdigest()[:16],
            )
        )

    last_exc: PlanReviewError | None = None
    opus_plan: dict[str, Any] | None = None

    for attempt in range(1, _DECOMPOSE_ATTEMPTS + 1):
        raw = await router.run("plan_review", prompt, project_id=project_id)
        try:
            opus_plan = parse_review_response(raw)
        except PlanReviewError as exc:
            last_exc = exc
            logger.warning(
                "Decomposition parse failed (attempt %d/%d): %s",
                attempt,
                _DECOMPOSE_ATTEMPTS,
                exc,
            )
            continue

        normalize_slugs(opus_plan)
        drop_verification_only_leaves(opus_plan)

        # Sync id -> slug so validation rules (which compare depends_on
        # against task ids) see consistent identifiers.
        for task in opus_plan["tasks"]:
            task["id"] = task["slug"]

        leaves = [LeafTask.model_validate(t) for t in opus_plan["tasks"]]
        validation_result = validate_leaves(opus_plan, profile, plan, leaves)

        if validation_result.clean:
            break

        if attempt < _DECOMPOSE_ATTEMPTS:
            feedback = format_violations_feedback(validation_result)
            prompt = f"{prompt}\n\n{feedback}"
            logger.warning(
                "Decomposition validation failed (attempt %d/%d); "
                "re-invoking brain with feedback.",
                attempt,
                _DECOMPOSE_ATTEMPTS,
            )
            continue

        if validation_result.hard:
            hard_msgs = "; ".join(
                f"[{v.rule}] {v.task_id}: {v.message}" for v in validation_result.hard
            )
            if emitter is not None and plan_id is not None:
                await emitter.emit(
                    PlanRejectedEvent(
                        plan_id=plan_id,
                        violations=[
                            f"[{v.rule}] {v.task_id}: {v.message}"
                            for v in validation_result.hard
                        ],
                        rounds=attempt,
                    )
                )
            msg = f"plan_rejected: {hard_msgs}"
            raise PlanReviewError(msg)

        opus_plan["validation_warnings"] = [
            {"rule": v.rule, "task_id": v.task_id, "message": v.message}
            for v in validation_result.soft
        ]
        break

    if opus_plan is None:
        raise (
            last_exc
            if last_exc is not None
            else PlanReviewError(  # noqa: EM101
                "decomposition failed with no parseable output"
            )
        )

    # Emit per-leaf capability events from the final validation result.
    if emitter is not None and plan_id is not None:
        rejected_slugs = {v.task_id for v in validation_result.hard}
        for leaf in leaves:
            if leaf.slug in rejected_slugs:
                hard_for_leaf = [
                    v for v in validation_result.hard if v.task_id == leaf.slug
                ]
                for hv in hard_for_leaf:
                    await emitter.emit(
                        LeafRejectedEvent(
                            plan_id=plan_id,
                            leaf_slug=leaf.slug,
                            rule_id=hv.rule,
                        )
                    )
            else:
                await emitter.emit(
                    LeafValidatedEvent(
                        plan_id=plan_id,
                        leaf_slug=leaf.slug,
                    )
                )

    authored_count = count_plan_tasks(plan)
    leaf_count = len(opus_plan["tasks"])
    if authored_count > 0 and leaf_count < authored_count:
        logger.warning(
            "Decomposition emitted fewer leaves (%d) than the plan's authored "
            "task count (%d); a leaf may have been silently dropped.",
            leaf_count,
            authored_count,
        )
        opus_plan["decompose_warning"] = {
            "authored_task_count": authored_count,
            "leaf_count": leaf_count,
        }

    scrubbed_context = scrub_context(context)
    if scrubbed_context is not None:
        for task in opus_plan["tasks"]:
            task.setdefault("context_text", scrubbed_context)
    scrubbed_local = scrub_context(local_context)
    if scrubbed_local is not None:
        for task in opus_plan["tasks"]:
            task.setdefault("repo_memory", scrubbed_local)
    return opus_plan
