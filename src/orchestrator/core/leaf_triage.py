"""Brain triage of a leaf that failed twice on worker-attributable grounds.

One brain call, deterministic bounds around it.  ADaPT (arXiv 2311.05772) says
decompose a subtask only when the executor fails it; runtime-structured
decomposition (arXiv 2605.15425) says isolate the failed unit rather than
re-running the plan.  This module is where both become behavior.

Contract: build an evidence pack, ask ``leaf_failure_triage`` for a
``TriageDecision``, re-ask exactly once on malformed output, then fall back to
``human``.  Downgrade any decision the caller cannot honour (escalation ladder
exhausted, split would breach ``max_leaves_per_plan``).  Never guess.

Exactly one failure leaves here as an exception rather than a decision: a
subscription throttle.  ``human`` is terminal and irreversible, so answering it
for a limit that clears by itself in five hours converts a self-healing wait
into a permanent human gate on a leaf that still had retries.  A throttle is
not a triage outcome, and the caller defers instead.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError

from orchestrator.core.llm_router import ProviderRateLimitError
from orchestrator.models.schemas import CapabilityProfile, TriageDecision


logger = logging.getLogger(__name__)

# Per-attempt diff cap. The evidence pack is a prompt, not an archive; a full
# failing diff for two attempts can exceed the brain's window on its own.
_DIFF_CHARS_PER_ATTEMPT = 6000

# Verify-output tail cap, matching the review path's _VERIFY_OUTPUT_MAX spirit.
_VERIFY_TAIL_CHARS = 1500

# Total brain attempts: the first ask plus exactly one informed re-ask.
_TRIAGE_ATTEMPTS = 2


class TriageParseError(Exception):
    """Raised when the triage brain returns an unusable response."""


@dataclass
class TriageEvidence:
    """Everything the triage brain is allowed to see about a failed leaf."""

    task_slug: str
    leaf_type: str
    plan_text: str
    profile: CapabilityProfile
    attempts: list[dict[str, Any]] = field(default_factory=list)
    difficulty_score: float | None = None
    remaining_leaf_budget: int = 0
    escalation_available: bool = True


_PROMPT = """A leaf task failed twice under a worker model. Decide what to do next.

Leaf: {task_slug}
Declared leaf type: {leaf_type}

Worker capability profile:
- name: {model_name}
- parameters (billions): {parameter_count_b}
- context window (tokens): {context_window}
- strengths: {strengths}
- weaknesses: {weaknesses}
- hard limits: at most {max_files_touched} files, about {max_loc_delta} LOC, \
dependency depth at most {max_dep_depth}

Pre-dispatch difficulty score for this leaf: {difficulty_score}

The leaf's VERBATIM contract:
---
{plan_text}
---

Failure evidence:
{attempts_block}

DECIDE exactly one of:
- "retry": the leaf is correctly sized and the failure was incidental. You may
  supply "refined_prompt" with the one correction the worker needs.
- "split": the leaf is too large or mixes independent concerns. Supply
  "children": between 2 and 4 replacement leaves.
- "escalate": the leaf is correctly sized but beyond this worker's capability.
  The same leaf is re-dispatched to a stronger implementer.
- "human": none of the above is safe; a person must look.

HARD RULES (violating any of these invalidates your answer):
- A split must produce between 2 and 4 children.
- Split children may not split again: size each child so it succeeds first try.
- At most {remaining_leaf_budget} more leaves may be added to this plan.
- Every child MUST satisfy the leaf standard: a "plan_text" containing
  line-leading Goal, Files, Steps, and Acceptance sections; a "files" list; a
  "verification" string over 40 characters naming a runnable command; a
  "leaf_type"; and "estimated_loc".
- Children inherit the parent's dependencies automatically. Only set a child's
  "depends_on" to the ids of its SIBLINGS.
{escalation_rule}

Respond with ONLY valid JSON:
{{
  "decision": "retry" | "split" | "escalate" | "human",
  "reason": "one or two sentences of evidence-grounded justification",
  "children": null,
  "refined_prompt": null
}}
"""

_ESCALATION_ALLOWED = (
    "- Escalation is available: a stronger implementer has not been tried yet."
)
_ESCALATION_EXHAUSTED = (
    '- The escalation ladder is exhausted. Do NOT answer "escalate"; answer '
    '"split", "retry", or "human".'
)


def _unknown(value: Any) -> str:
    """Render a missing measurement as "unknown", never as a number.

    An absent measurement printed as ``None`` reads as noise; printed as ``0``
    it reads as a positive claim, and zero files touched is the signature of a
    worker that did nothing, which pushes the triage decision toward escalate
    or human. The brain is entitled to know the difference between "nothing
    changed" and "nobody looked".

    Args:
        value: The measurement, or None.

    Returns:
        The value as a string, or ``"unknown (not measured)"``.
    """
    return "unknown (not measured)" if value is None else str(value)


def _render_attempt(attempt: dict[str, Any]) -> str:
    diff = str(attempt.get("diff") or "")
    if len(diff) > _DIFF_CHARS_PER_ATTEMPT:
        diff = diff[:_DIFF_CHARS_PER_ATTEMPT] + "\n... (diff truncated)"
    tail = str(attempt.get("verify_tail") or "")
    if len(tail) > _VERIFY_TAIL_CHARS:
        tail = tail[-_VERIFY_TAIL_CHARS:]
    return (
        f"Attempt {attempt.get('attempt')}:\n"
        f"  files touched: {_unknown(attempt.get('files_touched'))}\n"
        f"  LOC delta: {_unknown(attempt.get('loc_delta'))}\n"
        f"  verify exit code: {_unknown(attempt.get('verify_exit_code'))}\n"
        f"  verify output tail:\n{tail}\n"
        f"  reviewer verdict reason: {attempt.get('review_reason')}\n"
        f"  diff:\n{diff}\n"
    )


def build_triage_prompt(evidence: TriageEvidence) -> str:
    """Render the triage prompt from the evidence pack.

    ``str.format`` substitutes in a single pass, so braces inside a diff, a
    verify tail, or a ``plan_text`` stay literal and are never re-read as
    replacement fields.  Nothing here may format an already-substituted string.
    """
    profile = evidence.profile
    return _PROMPT.format(
        task_slug=evidence.task_slug,
        leaf_type=evidence.leaf_type,
        model_name=profile.model_name,
        parameter_count_b=profile.parameter_count_b,
        context_window=profile.context_window,
        strengths=profile.strengths,
        weaknesses=profile.weaknesses,
        max_files_touched=profile.max_files_touched,
        max_loc_delta=profile.max_loc_delta,
        max_dep_depth=profile.max_dep_depth,
        difficulty_score=(
            "not scored"
            if evidence.difficulty_score is None
            else f"{evidence.difficulty_score:.2f}"
        ),
        plan_text=evidence.plan_text,
        attempts_block="\n".join(_render_attempt(a) for a in evidence.attempts),
        remaining_leaf_budget=evidence.remaining_leaf_budget,
        escalation_rule=(
            _ESCALATION_ALLOWED
            if evidence.escalation_available
            else _ESCALATION_EXHAUSTED
        ),
    )


def parse_triage_response(raw: str) -> TriageDecision:
    """Parse the brain's response into a validated TriageDecision.

    Tolerates a leading sentence of reasoning and a ```json fence, matching
    ``plan_review.parse_review_response``.

    Raises:
        TriageParseError: On invalid JSON or a decision that fails validation.
    """
    text = (raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    elif not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    try:
        data = json.loads(text)
    except ValueError as exc:
        message = f"triage response not valid JSON: {exc}"
        raise TriageParseError(message) from exc
    try:
        return TriageDecision.model_validate(data)
    except ValidationError as exc:
        message = f"triage response failed validation: {exc}"
        raise TriageParseError(message) from exc


def _downgrade(decision: TriageDecision, evidence: TriageEvidence) -> TriageDecision:
    """Reduce a decision the caller cannot honour to one it can.

    escalate with an exhausted ladder becomes human.  split that would breach
    the plan's leaf ceiling becomes escalate, or human when escalation is also
    unavailable.  Fail closed in every direction.

    Every branch returns a decision with no children, so the replacement can
    never itself fail ``TriageDecision`` validation.
    """
    if decision.decision == "escalate" and not evidence.escalation_available:
        return TriageDecision(
            decision="human",
            reason=(
                f"escalation ladder exhausted; original triage reason: {decision.reason}"
            ),
        )
    if decision.decision == "split":
        children = decision.children or []
        if len(children) > evidence.remaining_leaf_budget:
            fallback: Literal["escalate", "human"] = (
                "escalate" if evidence.escalation_available else "human"
            )
            return TriageDecision(
                decision=fallback,
                reason=(
                    f"split of {len(children)} children exceeds the plan's "
                    f"remaining leaf budget of {evidence.remaining_leaf_budget}; "
                    f"original triage reason: {decision.reason}"
                ),
            )
    return decision


async def triage_leaf(
    evidence: TriageEvidence,
    router: Any,
    project_id: str | None,
) -> TriageDecision:
    """Ask the brain what to do with a twice-failed leaf.

    Exactly one informed re-ask on malformed output, then ``human``.  A router
    exception (provider down, auth failure) also falls back to ``human``, and so
    does an evidence pack this module cannot even render: triage is an
    optimization over the existing retry path and must never be able to wedge a
    task, so only ONE path out of here raises.

    That path is a subscription throttle, and it is carved out of "never
    raises" on purpose.  ``human`` is applied by failing the task terminally,
    so answering it for a five-hour limit trades a wait that ends by itself for
    a block that ends only when a person notices -- strictly worse than the
    unwedged behaviour the blanket catch exists to guarantee.  The caller
    leaves the leaf REVIEWING and triages it again once the limit clears.

    Args:
        evidence: The bounded evidence pack.
        router: An ``LLMRouter``-compatible object with ``run(call_site, prompt,
            project_id)``.  Pass the ``OpusBridge`` (see
            ``opus_bridge.parking_brain_runner``): the bare router cannot park
            ``opus_state``, so the throttle raised here would be one nothing
            downstream can wait out.
        project_id: Project scope for router resolution.

    Returns:
        A validated, downgraded-if-necessary ``TriageDecision``.

    Raises:
        ProviderRateLimitError: The brain is throttled.  Not a decision; the
            caller must defer without consuming the leaf's one triage call.
    """
    try:
        base_prompt = build_triage_prompt(evidence)
    except Exception:  # noqa: BLE001 - triage must never wedge a task
        logger.exception(
            "Could not render the triage prompt for %s; parking for a human",
            evidence.task_slug,
        )
        return TriageDecision(
            decision="human",
            reason="the triage evidence pack was unrenderable; see orchestrator logs",
        )
    prompt = base_prompt
    last_error = ""
    for attempt in range(1, _TRIAGE_ATTEMPTS + 1):
        try:
            raw = await router.run("leaf_failure_triage", prompt, project_id=project_id)
        except ProviderRateLimitError:
            # Ahead of the blanket catch below, and the only thing that gets
            # past it. A throttle is the one failure this system already knows
            # how to wait out; answering "human" for it would fail the leaf
            # terminally and no clock brings it back.
            logger.warning(
                "The triage brain is rate limited for %s; deferring rather "
                "than deciding, so the leaf is triaged again once the limit "
                "clears",
                evidence.task_slug,
            )
            raise
        except Exception:  # noqa: BLE001 - triage must never wedge a task
            logger.exception(
                "Triage brain call failed for %s; parking for a human",
                evidence.task_slug,
            )
            return TriageDecision(
                decision="human",
                reason="the triage brain call failed; see orchestrator logs",
            )
        try:
            return _downgrade(parse_triage_response(raw), evidence)
        except TriageParseError as exc:
            last_error = str(exc)
            logger.warning(
                "Triage parse failed for %s (attempt %d/%d): %s",
                evidence.task_slug,
                attempt,
                _TRIAGE_ATTEMPTS,
                exc,
            )
            if attempt < _TRIAGE_ATTEMPTS:
                prompt = (
                    f"{base_prompt}\n\n"
                    "Your previous answer was rejected with this validation "
                    f"error:\n{last_error}\n"
                    "Respond again with ONLY the corrected JSON object."
                )
    return TriageDecision(
        decision="human",
        reason=f"triage output was unparseable twice: {last_error}",
    )
