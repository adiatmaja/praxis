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

from pydantic import ValidationError

from orchestrator.core.leaf_templates import render_template_block
from orchestrator.models.schemas import CapabilityProfile, LeafTask


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

HARD CONSTRAINTS (non-negotiable):
- Each leaf touches at most {max_files_touched} files.
- Each leaf adds or changes approximately ~{max_loc_delta} lines of code.
- Each leaf has no more than {max_checklist_items} checklist items.
- Dependency depth no deeper than {max_dep_depth}.
- Every leaf MUST include a "verification" naming a RUNNABLE command, plus the
  outcome that proves it passed. Put the command inside ONE pair of backticks
  and write the outcome as plain text around it, like
  "Run `pytest tests/test_client.py::test_retry` and confirm it passes".
  Praxis runs that command itself, so this matters:
  - Exactly ONE backticked span in the string. Two commands in one
    verification cannot be run; use the single command that proves the leaf.
  - The span must START with the program: `pytest -q`, `npm test`,
    `python -m pytest tests/`, `./scripts/check.sh`. Backticks around a FILE
    PATH ("confirm `src/client.py` defines it") are not a command and are
    ignored -- name the command that checks the file instead.
  - A verification that only describes looking at something
    ("check it visually", "inspect the output", "review the diff") is REJECTED.
{escalate_block}
{leaf_type_block}

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

Write every leaf's "description" and checklist for the WEAKEST model that
might implement it. A stronger model loses nothing from this style; a weaker
one loses the whole task without it:
- Imperative and concrete: name exact file paths, symbols, and commands.
  "Add retry_on_429() to src/client.py" beats "improve error handling".
- One action per checklist item; no item that hides several edits.
- State the expected output or format explicitly; show a short example when
  the format matters. Never rely on the worker inferring it.
- Each leaf must be understandable ALONE: the worker sees only its own leaf,
  never the rest of the plan, so no "as described above" or references to
  other leaves' content.

For every leaf you MUST also include "plan_text": this leaf's contract. It is a
LABELLED SKELETON, not a free-form excerpt, and it carries the verbatim source
lines INSIDE that skeleton. Write the required section labels at the start of a
line, then under "Steps" paste the lines of the plan that define this leaf --
exact function/type signatures, API shapes, and named requirements -- copied,
not paraphrased, so a reviewer checks the implementation against the original
contract rather than against a summary.

Both halves are graded, and only that one shape satisfies both:
- plan_text that is ONLY the verbatim excerpt is REJECTED: it has no labels.
- plan_text that only PARAPHRASES the plan is warned: it is not the contract.
- Labels, with the source lines under "Steps", passes both. Do that.

For every leaf you MUST also include:
- "files": list of file paths this leaf will touch.
- "task_type": one of "feature", "bugfix", "refactor", "test", "chore".
- "estimated_loc": integer estimate of lines added or changed.
- "verification": ONE backticked runnable command plus the outcome that proves
  it passed (see the HARD constraint above; the example below is the shape).
- "leaf_type": one of the leaf types listed above.

Set "depends_on" to the ids of any leaves whose output this leaf builds on (e.g.
a leaf that edits a file another leaf creates). Only truly independent leaves get
an empty list. Do NOT add a dependency edge merely to impose an order on independent work: over-serializing the graph inflates dependency depth and can cause the whole plan to be rejected. A leaf depends on another ONLY when it genuinely consumes that leaf's output (edits a file the other creates, imports a symbol the other defines).

Two leaves that touch the SAME FILE are never independent, and this is the one
case where you must impose an order. Either merge them into a single leaf (see
sizing rule 2), or give the later one a dependency edge on the earlier. Two
leaves listing the same path in "files" with no edge between them is warned.

If a leaf cannot be completed by this model (too complex for its parameter count,
or irreducibly large), set "needs_stronger_model": true.

Respond with ONLY valid JSON. The "plan_text" below is a worked example of the
required shape, not a placeholder: labels at the start of a line, and the
plan's own lines copied under "Steps".
{{
  "tasks": [
    {{"id": "t1", "title": "Add retry_on_429 to the HTTP client",
      "description": "Add a 429-aware retry wrapper to src/client.py.",
      "plan_text": "Goal: `src/client.py` exposes `retry_on_429`.\\nFiles: src/client.py, tests/test_client.py\\nSteps:\\nAdd `retry_on_429(fn, *, attempts: int = 3) -> Callable` to `src/client.py`.\\nRetry on HTTP 429 with exponential backoff starting at 2 seconds.\\nRaise the last error once attempts are exhausted, never swallow it.\\nCover the exhausted-attempts path in `tests/test_client.py`.\\nAcceptance: `pytest tests/test_client.py::test_retry_on_429` passes.",
      "depends_on": [], "checklist": [{{"text": "Add retry_on_429 to src/client.py"}}],
      "needs_stronger_model": false,
      "files": ["src/client.py", "tests/test_client.py"],
      "task_type": "feature",
      "leaf_type": "function_add",
      "estimated_loc": 85,
      "verification": "Run `pytest tests/test_client.py::test_retry_on_429` and confirm it passes"}}
  ]
}}
"""

# Rendered only when the profile actually names escalate types. The HARD rule
# `_check_escalate_mismatch` rejects a leaf of one of these types that did not
# set needs_stronger_model, so a prompt that never states the list asks for
# something it then rejects, and the re-ask carries no way to satisfy it.
_ESCALATE_BLOCK = """- These task types are BEYOND this worker and MUST set
  "needs_stronger_model": true: {types}. A leaf whose "task_type" is one of
  them with "needs_stronger_model": false is rejected automatically.
"""


def _render_escalate_block(profile: CapabilityProfile) -> str:
    """Render the escalate-task-type rule, or nothing when the list is empty.

    Args:
        profile: The worker's capability profile.

    Returns:
        The rendered rule lines, or ``""`` when no type escalates.  An empty
        string keeps a stock install's prompt exactly as it was, so the block
        can never invent a constraint the validator will not enforce.
    """
    types = [str(t) for t in getattr(profile, "escalate_task_types", []) or []]
    if not types:
        return ""
    return _ESCALATE_BLOCK.format(types=", ".join(f'"{t}"' for t in types))


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
        max_files_touched=profile.max_files_touched,
        max_loc_delta=profile.max_loc_delta,
        max_checklist_items=profile.max_checklist_items,
        max_dep_depth=profile.max_dep_depth,
        escalate_block=_render_escalate_block(profile),
        leaf_type_block=render_template_block(),
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
    leaves = []
    for t in tasks:
        if not isinstance(t, dict) or "id" not in t or "title" not in t:
            raise PlanReviewError(f"task missing id/title: {t}")  # noqa: EM102
        try:
            leaf = LeafTask.model_validate(t)
        except ValidationError as exc:
            msg = f"invalid leaf task: {exc}"
            raise PlanReviewError(msg) from exc
        leaves.append(leaf.model_dump())
    return {"tasks": leaves}
