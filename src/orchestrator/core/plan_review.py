"""Brain-driven capability-aware decomposition of an external plan.

Unlike core/plan_derive (deterministic extraction, never the brain), this is a
reasoning pass: it judges each task against the local model's capability profile
and token budget, splits oversized tasks into do-able leaves with checklists,
and flags genuinely-too-hard leaves as needs_stronger_model. The brain call
itself is made by the caller via core/llm_router; this module owns the prompt
and the strict response parsing (a malformed response must never pass silently).
"""

from __future__ import annotations

import json
import re

from orchestrator.models.schemas import CapabilityProfile


class PlanReviewError(Exception):
    """Raised when the review brain returns an unusable response."""


_PROMPT = """You decompose an implementation plan so a LOCAL model can execute it.

Local model capability:
- name: {model_name}
- parameters (billions): {parameter_count_b}
- context window (tokens): {context_window}
- strengths: {strengths}
- weaknesses: {weaknesses}
- max task complexity: {max_task_complexity}

{history_summary}

Hard limit: each leaf task's full context must fit ~{per_leaf_token_budget} tokens.

Plan to decompose:
{plan_text}

Use the FEWEST leaves that each fit the per-leaf token budget and the model's
capability. A leaf must be a cohesive, independently-reviewable unit of work,
NOT the smallest mechanical fragment possible. For every leaf provide an ordered
checklist of concrete steps.

Sizing rules (apply in order):
1. Keep an implementation and its tests TOGETHER in the same leaf whenever they
   fit the budget. Do NOT create separate test-only leaves for code defined in
   another leaf -- this causes duplicate/overlapping tests and
   tests-without-implementation lint failures.
2. Do not split a single small module or class across multiple leaves just to
   make each leaf tiny. Group tightly-coupled edits to the same file or type
   into one leaf when they fit the budget.
3. Do not create "skeleton/scaffold" leaves whose imports or stubs only make
   sense once a later leaf lands -- this causes unused-import lint failures
   (F401) that the verify gate will reject.
4. Only split when a unit genuinely exceeds the token budget, exceeds the
   model's max complexity, or when parts are truly independent (different
   subsystems, no shared state).

For every leaf you MUST also include "plan_text": the VERBATIM excerpt of the
plan that defines this leaf's contract -- exact function/type signatures, API
shapes, and named requirements. Do not paraphrase; copy the relevant lines so a
reviewer can check the implementation against the original contract, not a summary.

Set "depends_on" to the ids of any leaves whose output this leaf builds on (e.g.
a leaf that edits a file another leaf creates). Only truly independent leaves get
an empty list.

If a leaf cannot be completed by this model (too complex for its parameter count,
or irreducibly large), set "needs_stronger_model": true.

Respond with ONLY valid JSON:
{{
  "tasks": [
    {{"id": "t1", "title": "...", "description": "...", "plan_text": "...",
      "depends_on": [], "checklist": [{{"text": "..."}}],
      "needs_stronger_model": false}}
  ]
}}
"""


def build_review_prompt(
    plan_text: str,
    profile: CapabilityProfile,
    history_summary: str,
    per_leaf_token_budget: int,
) -> str:
    """Render the decomposition prompt for the review brain.

    Args:
        plan_text: The externally-authored plan to decompose.
        profile: The local model's capability profile.
        history_summary: Compact summary of prior run outcomes (or a sentinel).
        per_leaf_token_budget: Approximate token budget each leaf must fit.

    Returns:
        The rendered prompt string.
    """
    return _PROMPT.format(
        model_name=profile.model_name,
        parameter_count_b=profile.parameter_count_b,
        context_window=profile.context_window,
        strengths=profile.strengths,
        weaknesses=profile.weaknesses,
        max_task_complexity=profile.max_task_complexity,
        history_summary=history_summary,
        per_leaf_token_budget=per_leaf_token_budget,
        plan_text=plan_text,
    )


def parse_review_response(raw: str) -> dict:
    """Parse and validate the brain's JSON into an opus_plan dict.

    Args:
        raw: The brain's raw text response.

    Returns:
        A normalized ``{"tasks": [...]}`` dict.

    Raises:
        PlanReviewError: on invalid JSON, missing/empty tasks, or bad shape.
    """
    raw = raw.strip()
    # The brain (esp. Sonnet under --effort high) often prepends a sentence of
    # reasoning before the JSON and/or wraps it in a ```json fence. Extract the
    # JSON object robustly regardless of surrounding prose: prefer a fenced
    # block anywhere, else slice from the first "{" to the last "}".
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1).strip()
    elif not raw.startswith("{"):
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            raw = raw[start : end + 1]
    try:
        data = json.loads(raw)
    except (ValueError, IndexError) as exc:
        raise PlanReviewError(f"review response not valid JSON: {exc}") from exc  # noqa: EM102
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list) or not tasks:
        raise PlanReviewError("review response had no tasks")  # noqa: EM101
    for t in tasks:
        if "id" not in t or "title" not in t:
            raise PlanReviewError(f"task missing id/title: {t}")  # noqa: EM102
        t.setdefault("description", t["title"])
        t.setdefault("plan_text", t["description"])
        t.setdefault("depends_on", [])
        t.setdefault("checklist", [{"text": t["title"]}])
        t.setdefault("needs_stronger_model", False)
    return {"tasks": tasks}
