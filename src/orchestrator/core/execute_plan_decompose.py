"""Shared brain-driven decomposition of an externally-authored plan.

Extracted from api/execute_plan.py so the endpoint (fast path) and the
orchestration loop (async path) run one identical implementation.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any

from orchestrator.core.capability_events import (
    DecomposeInputEvent,
    LeafDifficultyScoredEvent,
    LeafRejectedEvent,
    LeafRejectedPredispatchEvent,
    LeafValidatedEvent,
    PlanRejectedEvent,
)
from orchestrator.core.capability_history import (
    fetch_recent_outcomes,
    summarize_outcomes,
)
from orchestrator.core.context_scrub import scrub_context
from orchestrator.core.difficulty import (
    DEFAULT_BIAS,
    DEFAULT_WEIGHTS,
    build_scorer,
    extract_features,
)
from orchestrator.core.leaf_validator import (
    # Imported rather than recomputed so the scorer's dep_depth feature and the
    # validator's dep_depth rule can never disagree. Same precedent as
    # core/difficulty.py importing _RUNNABLE_SIGNAL from this module.
    _max_dep_depth as max_dep_depth,
)
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
# F3 validation and the difficulty gate SHARE this budget; neither gets rounds
# of its own, or a pathological plan could re-invoke the brain without bound.
_DECOMPOSE_ATTEMPTS = 2

# Gate thresholds used when the caller's settings object cannot supply them.
# They mirror EffectiveSettings.difficulty_config's own defaults: a settings
# shim without the method must mean "gate on the defaults", never "no gate".
_DEFAULT_REJECT_BELOW = 0.35
_DEFAULT_FLAG_BELOW = 0.55

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


@dataclass(frozen=True)
class _LeafScore:
    """One leaf's predicted success plus the evidence behind the prediction."""

    p_success: float
    features: dict[str, float]
    failing_features: list[str]


def _pass_rate(runs: list[dict[str, Any]]) -> float | None:
    """Observed pass rate across attributable outcome rows, or None if empty.

    Args:
        runs: Outcome rows from ``fetch_recent_outcomes`` (already filtered to
            passes and worker-attributable failures).

    Returns:
        The pass rate in [0, 1], or None so the scorer uses its neutral prior.
    """
    if not runs:
        return None
    passes = sum(1 for r in runs if r.get("outcome") == "pass")
    return passes / len(runs)


async def _resolve_difficulty_config(effective_settings: Any) -> dict[str, Any]:
    """Resolve the difficulty gate's weights, bias, and thresholds.

    ``EffectiveSettings`` always supplies ``difficulty_config``; a caller
    passing a narrower settings shim may not.  The gate then runs on the module
    defaults, which are the numbers ``EffectiveSettings`` itself returns when no
    YAML override is set.  A missing config must never silently mean no gate.

    Args:
        effective_settings: Settings object, with or without the method.

    Returns:
        A dict carrying ``weights``, ``bias``, ``reject_below``, ``flag_below``,
        with any key the caller omitted filled from the defaults.
    """
    defaults: dict[str, Any] = {
        "weights": DEFAULT_WEIGHTS,
        "bias": DEFAULT_BIAS,
        "reject_below": _DEFAULT_REJECT_BELOW,
        "flag_below": _DEFAULT_FLAG_BELOW,
    }
    getter = getattr(effective_settings, "difficulty_config", None)
    if getter is None:
        return defaults
    config = await getter()
    if not isinstance(config, dict):
        logger.warning(
            "difficulty_config returned %s, not a dict; gating on module defaults.",
            type(config).__name__,
        )
        return defaults
    return {**defaults, **config}


def _score_leaves(
    leaves: list[LeafTask],
    profile: Any,
    config: dict[str, Any],
    history_rate: float | None,
) -> dict[str, _LeafScore]:
    """Score every leaf and name the features dragging each one down.

    A feature is "failing" when its contribution to the logit is negative, so
    the re-ask feedback names the actual cause rather than restating the score.
    They are ordered worst contribution first: the brain should fix the biggest
    drag, and an alphabetical order buries it behind whatever sorts earlier.

    Args:
        leaves: The parsed leaves, after F3 has seen them.
        profile: The worker capability profile supplying the denominators.
        config: Resolved difficulty config (weights, bias, thresholds).
        history_rate: Observed pass rate, or None for the neutral prior.

    Returns:
        ``{leaf id: _LeafScore}``, one entry per leaf.
    """
    scorer = build_scorer(config)
    # Same merge build_scorer applies, so the contributions explaining a score
    # are computed with the same weights that produced it.
    weights = {**DEFAULT_WEIGHTS, **(config.get("weights") or {})}
    depths = max_dep_depth(leaves)
    scored: dict[str, _LeafScore] = {}
    for leaf in leaves:
        features = extract_features(
            leaf,
            profile,
            dep_depth=depths.get(leaf.id, 0),
            historical_success=history_rate,
        )
        vector = features.as_vector()
        contributions = sorted(
            ((name, weights.get(name, 0.0) * value) for name, value in vector.items()),
            key=lambda item: item[1],
        )
        scored[leaf.id] = _LeafScore(
            p_success=scorer.score(features),
            features=vector,
            failing_features=[name for name, weighted in contributions if weighted < 0],
        )
    return scored


def _format_difficulty_feedback(
    too_hard: list[str],
    scored: dict[str, _LeafScore],
) -> str:
    """Render the re-ask critique for leaves the gate predicts will fail."""
    named = "; ".join(
        f"{slug} (p_success {scored[slug].p_success:.2f}; worst features: "
        f"{', '.join(scored[slug].failing_features) or 'none'})"
        for slug in too_hard
    )
    return (
        "DIFFICULTY REJECTION: these leaves are predicted to fail this worker: "
        f"{named}. Re-decompose them smaller: fewer files, a smaller LOC "
        "estimate, a shorter plan_text, a runnable acceptance command, and a "
        "specific leaf_type rather than 'generic'."
    )


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
    db: Any = None,
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
        db: Optional Database to fetch recent outcomes for history.

    Returns:
        A normalized ``{"tasks": [...]}`` dict where each task has a ``slug``,
        a ``difficulty_score``, a ``difficulty_flagged`` boolean, and
        ``depends_on`` holding slugs (not brain ids). Verification-only leaves
        are removed before the dict is returned.

    Raises:
        PlanReviewError: If the brain output cannot be parsed, if HARD
            validation violations remain after the informed re-decompose round,
            or if a leaf still scores below ``reject_below`` after it.
    """
    profile = await effective_settings.capability_profile(project_id=None, model=model)
    per_leaf_budget = int(profile.context_window * (1 - WORKER_RESERVE_FRACTION))
    if db is not None:
        runs = await fetch_recent_outcomes(
            db, model_name=model, project_id=project_id, limit=100
        )
    else:
        runs = []
    history = summarize_outcomes(runs)
    prompt = build_review_prompt(plan, profile, history, per_leaf_budget)
    difficulty_config = await _resolve_difficulty_config(effective_settings)
    history_rate = _pass_rate(runs)

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
    scored: dict[str, _LeafScore] = {}

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

        # The difficulty gate runs AFTER F3, on the same leaves, inside the
        # same round budget. F3 asks "is this leaf well formed"; the gate asks
        # "will this worker finish it". A leaf must clear both to dispatch.
        scored = _score_leaves(leaves, profile, difficulty_config, history_rate)
        too_hard = [
            leaf.id
            for leaf in leaves
            if scored[leaf.id].p_success < difficulty_config["reject_below"]
        ]

        if validation_result.clean and not too_hard:
            break

        if attempt < _DECOMPOSE_ATTEMPTS:
            # One informed re-ask carrying BOTH critiques. Giving the gate its
            # own rounds would let a pathological plan loop the brain past F3's
            # cap; feeding back only one critique burns the shared round on
            # half the information and the other gate fails again next round.
            feedback_parts: list[str] = []
            if not validation_result.clean:
                feedback_parts.append(format_violations_feedback(validation_result))
                violation_detail = "; ".join(
                    f"[{v.rule}] {v.task_id}: {v.message}"
                    for v in (*validation_result.hard, *validation_result.soft)
                )
                logger.warning(
                    "Decomposition validation failed (attempt %d/%d); "
                    "re-invoking brain with feedback. Violations: %s",
                    attempt,
                    _DECOMPOSE_ATTEMPTS,
                    violation_detail,
                )
            if too_hard:
                feedback_parts.append(_format_difficulty_feedback(too_hard, scored))
                logger.warning(
                    "Difficulty gate rejected %d leaf/leaves (attempt %d/%d): %s",
                    len(too_hard),
                    attempt,
                    _DECOMPOSE_ATTEMPTS,
                    ", ".join(too_hard),
                )
            prompt = "\n\n".join([prompt, *feedback_parts])
            continue

        if too_hard:
            if emitter is not None and plan_id is not None:
                for slug in too_hard:
                    await emitter.emit(
                        LeafRejectedPredispatchEvent(
                            plan_id=plan_id,
                            leaf_slug=slug,
                            p_success=scored[slug].p_success,
                            failing_features=scored[slug].failing_features,
                        )
                    )
            msg = (
                "plan_rejected: difficulty gate rejected "
                f"{', '.join(too_hard)} after {attempt} rounds"
            )
            raise PlanReviewError(msg)

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
            if leaf.id in rejected_slugs:
                hard_for_leaf = [
                    v for v in validation_result.hard if v.task_id == leaf.id
                ]
                for hv in hard_for_leaf:
                    await emitter.emit(
                        LeafRejectedEvent(
                            plan_id=plan_id,
                            leaf_slug=leaf.id,
                            rule_id=hv.rule,
                        )
                    )
            else:
                await emitter.emit(
                    LeafValidatedEvent(
                        plan_id=plan_id,
                        leaf_slug=leaf.id,
                    )
                )

    # Every surviving leaf carries its score onward: a leaf between the two
    # thresholds still dispatches, but flagged, and downstream triage reads the
    # flag off the task rather than re-deriving it.
    flag_below = difficulty_config["flag_below"]
    for task in opus_plan["tasks"]:
        leaf_score = scored.get(task["slug"])
        if leaf_score is None:
            continue
        flagged = leaf_score.p_success < flag_below
        task["difficulty_score"] = leaf_score.p_success
        task["difficulty_flagged"] = flagged
        if emitter is not None and plan_id is not None:
            await emitter.emit(
                LeafDifficultyScoredEvent(
                    plan_id=plan_id,
                    leaf_slug=task["slug"],
                    p_success=leaf_score.p_success,
                    features=leaf_score.features,
                    flagged=flagged,
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
