# Decomposition Constraints & Validator (Plan 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn Praxis's capability-aware decomposition from prose guidance into an enforced contract: inject hard numeric limits into the decompose prompt (F2), add a deterministic leaf validator with an informed re-decompose repair loop (F3), fix the wave-scheduler dangling-dependency deadlock, add supply-chain diff gates (F15), and wire the first production caller of the capability decision-log emitters (S1).

**Architecture:** All decomposition runs through the single function `core/execute_plan_decompose.decompose_plan` (its only caller is `core/orchestrator.py:124`). This plan adds a pure `core/leaf_validator.py` gate called inside that function after `normalize_slugs`/`drop_verification_only_leaves`, restructures the existing 2-attempt parse loop into a validate-and-repair loop, extends `CapabilityProfile` with numeric limits, extends the decompose prompt, and threads an optional `CapabilityEventEmitter` (already defined in `core/capability_events.py`, currently unwired) for decision records. Supply-chain gates extend the existing `core/diff_guard.py` and hook into the pass→merge branch of `orchestrator_review.review_task`.

**Tech Stack:** Python 3.11, Pydantic v2, pytest (`asyncio_mode = "auto"`), aiosqlite. No new dependencies. No agent-image rebuild (no `docker/*/entrypoint.sh` changes).

---

## Background the engineer needs (read before starting)

- **Run tests:** `uv run pytest <path> -v`. Full suite: `uv run pytest --cov=orchestrator -q`. Lint/format/type: `uv run ruff format src/ tests/ && uv run ruff check --fix src/ tests/ && uv run mypy src/orchestrator/ --ignore-missing-imports`.
- **No em dashes** anywhere (prose, code comments, commit messages). Use commas/colons/semicolons.
- **Error-string lint:** ruff `EM101/EM102` require exception messages not be inline literals or f-strings. Existing code silences these with `# noqa: EM101` / `# noqa: EM102` at the `raise` line. Follow that exact pattern (see `plan_review.py:142`).
- **The decompose data shape** (`opus_plan`) is `{"tasks": [ {leaf_dict}, ... ], ...}`. After `normalize_slugs`, every leaf has a unique `"slug"` and `"depends_on"` holds slugs (not brain ids). Leaf dicts come from `LeafTask.model_dump()` (see `models/schemas.py:83`), so they already contain: `id, title, description, plan_text, depends_on, checklist (list of {"text": ...}), needs_stronger_model, files (list[str]), task_type, estimated_loc, verification`.
- **`CapabilityProfile`** lives at `models/schemas.py:59`. It is resolved by `effective_settings.capability_profile(project_id, model)` which merges `config/praxis.yaml` `capability.default` over pydantic defaults. Older configs will not carry the new limit fields, so the validator MUST read limits via `getattr(profile, name, DEFAULT)` (duck-typed test fakes rely on this too).
- **Tiering decision (locked in brainstorming):** structural rules are HARD (fail the plan closed after the informed round); heuristic rules are SOFT (trigger one informed re-decompose, then degrade to a warning that still dispatches). See each rule's tier in Task 4.
- **`decompose_plan` currently has ONE caller:** `core/orchestrator.py:124`. `api/execute_plan.py` only imports `branch_slug, normalize_slugs`, not `decompose_plan`. So new optional params (`plan_id`, `emitter`) only need wiring at that one call site.

---

## File Structure

- **Create** `src/orchestrator/core/leaf_validator.py` — pure, no-LLM validation of a normalized `opus_plan` against a profile and the source plan. Exposes `Violation`, `ValidationResult`, `validate_leaves`, `format_violations_feedback`.
- **Create** `tests/test_leaf_validator.py` — unit tests per rule and tiering.
- **Modify** `src/orchestrator/core/token_budget.py` — add shared `WORKER_RESERVE_FRACTION = 0.6` constant.
- **Modify** `src/orchestrator/core/worker_bible.py` — point its `reserve_fraction` default at the shared constant.
- **Modify** `src/orchestrator/models/schemas.py` — add numeric-limit fields to `CapabilityProfile`.
- **Modify** `config/praxis.yaml` — add the limit defaults under `capability.default`.
- **Modify** `src/orchestrator/core/plan_review.py` — `HARD CONSTRAINTS` block + extended JSON shape in `_PROMPT`; render limits in `build_review_prompt`.
- **Modify** `src/orchestrator/core/execute_plan_decompose.py` — replace `_LEAF_BUDGET_FRACTION` with the shared constant; restructure into a validate-and-repair loop; emit S1 events; add `plan_id`/`emitter` params.
- **Modify** `src/orchestrator/core/orchestrator.py` — construct a `CapabilityEventEmitter`; pass `plan_id`/`emitter` to `decompose_plan`.
- **Modify** `src/orchestrator/core/task_queue.py` — defensive dangling-dep check in `get_dispatchable_tasks`.
- **Modify** `src/orchestrator/core/diff_guard.py` — add `added_dependencies` and `detect_secrets`.
- **Modify** `src/orchestrator/core/orchestrator_review.py` — force human gate on supply-chain hits in the pass branch.
- **Modify** existing tests as noted (extend `_FakeProfile`, decompose tests).

---

### Task 1: Unify the budget fraction behind one shared constant

**Files:**
- Modify: `src/orchestrator/core/token_budget.py`
- Modify: `src/orchestrator/core/worker_bible.py:39`
- Modify: `src/orchestrator/core/execute_plan_decompose.py:27` (remove `_LEAF_BUDGET_FRACTION`; final use is rewired in Task 5)
- Test: `tests/test_token_budget.py`

**Depends on:** None

Context: `execute_plan_decompose._LEAF_BUDGET_FRACTION = 0.4` and `worker_bible.reserve_fraction = 0.6` encode the same policy (injected context uses `1 - 0.6 = 0.4` of the window) in two places. Collapse to one constant. Numerically identical, so behavior does not change.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_token_budget.py`:

```python
from orchestrator.core.token_budget import WORKER_RESERVE_FRACTION


def test_worker_reserve_fraction_is_single_source():
    # Injected context uses 1 - reserve of the window.
    assert WORKER_RESERVE_FRACTION == 0.6
    window = 10000
    per_leaf_budget = int(window * (1.0 - WORKER_RESERVE_FRACTION))
    assert per_leaf_budget == 4000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_token_budget.py::test_worker_reserve_fraction_is_single_source -v`
Expected: FAIL with `ImportError: cannot import name 'WORKER_RESERVE_FRACTION'`

- [ ] **Step 3: Add the constant and rewire callers**

In `src/orchestrator/core/token_budget.py`, above `def fit_sections`, add:

```python
# Fraction of a worker's context window reserved for its own reasoning and
# edits. Injected context (Bible sections, per-leaf budget) uses ``1 - this``.
# Single source of truth: both worker_bible.fit_sections and the decompose
# per-leaf budget derive from this so the two never drift.
WORKER_RESERVE_FRACTION: float = 0.6
```

Change the `fit_sections` signature default to reference it:

```python
def fit_sections(
    sections: list[Section],
    context_window: int,
    reserve_fraction: float = WORKER_RESERVE_FRACTION,
) -> list[Section]:
```

In `src/orchestrator/core/worker_bible.py`, change line 39 `reserve_fraction: float = 0.6` to import and use the constant:

```python
from orchestrator.core.token_budget import Section, WORKER_RESERVE_FRACTION, fit_sections
```

and set the dataclass field default to `reserve_fraction: float = WORKER_RESERVE_FRACTION`.

In `src/orchestrator/core/execute_plan_decompose.py`, delete the `_LEAF_BUDGET_FRACTION = 0.4` line (and its comment at lines 26-27). Add the import:

```python
from orchestrator.core.token_budget import WORKER_RESERVE_FRACTION
```

Leave the `per_leaf_budget` computation at line 214 for Task 5 to rewire; to keep this task green in isolation, temporarily change line 214 to:

```python
    per_leaf_budget = int(profile.context_window * (1 - WORKER_RESERVE_FRACTION))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_token_budget.py tests/test_worker_bible.py tests/test_execute_plan_decompose.py -v`
Expected: PASS (all existing decompose/bible tests still pass; `per_leaf_budget` value is unchanged because `0.4 == 1 - 0.6`).

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/token_budget.py src/orchestrator/core/worker_bible.py src/orchestrator/core/execute_plan_decompose.py tests/test_token_budget.py
git commit -m "refactor: unify worker reserve fraction behind one constant (F2)"
```

---

### Task 2: Add numeric limit fields to CapabilityProfile

**Files:**
- Modify: `src/orchestrator/models/schemas.py:59-71` (the `CapabilityProfile` class)
- Modify: `config/praxis.yaml` (`capability.default` block)
- Test: `tests/test_schemas.py` (or create `tests/test_capability_profile.py` if `test_schemas.py` has no natural home; check first with `grep -n CapabilityProfile tests/test_schemas.py`)

**Depends on:** None

Context: F2 needs per-model numeric limits the validator (F3) mirrors 1:1. They live on the profile so they resolve through the existing YAML/override path. Defaults protect configs that predate this field.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_schemas.py`:

```python
from orchestrator.models.schemas import CapabilityProfile


def test_capability_profile_has_numeric_limits_with_defaults():
    profile = CapabilityProfile(model_name="m", parameter_count_b=30, context_window=8192)
    assert profile.max_files_touched == 5
    assert profile.max_loc_delta == 300
    assert profile.max_checklist_items == 12
    assert profile.max_dep_depth == 3
    assert profile.escalate_task_types == []


def test_capability_profile_limits_are_overridable():
    profile = CapabilityProfile(
        model_name="m",
        parameter_count_b=30,
        context_window=8192,
        max_files_touched=3,
        escalate_task_types=["migration"],
    )
    assert profile.max_files_touched == 3
    assert profile.escalate_task_types == ["migration"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_schemas.py::test_capability_profile_has_numeric_limits_with_defaults -v`
Expected: FAIL with `AttributeError: 'CapabilityProfile' object has no attribute 'max_files_touched'`

- [ ] **Step 3: Add the fields**

In `src/orchestrator/models/schemas.py`, inside `CapabilityProfile` (after `max_task_complexity: str = "medium"`), add:

```python
    # Hard numeric limits mirrored 1:1 by core/leaf_validator (F2/F3).
    max_files_touched: int = 5
    max_loc_delta: int = 300
    max_checklist_items: int = 12
    max_dep_depth: int = 3
    # task_types that must be flagged needs_stronger_model (escalated), e.g.
    # ["migration"] for a small local worker.
    escalate_task_types: list[str] = Field(default_factory=list)
```

Verify `Field` is already imported in this module (it is, used elsewhere). If not, add `from pydantic import BaseModel, ConfigDict, Field, model_validator`.

In `config/praxis.yaml`, extend the `capability.default` block so operators see the knobs:

```yaml
capability:
  default:
    parameter_count_b: 30
    context_window: 8192
    strengths: "single-file edits, adding tests, small bug fixes"
    weaknesses: "multi-file refactors, novel architecture, large context"
    max_task_complexity: "medium"
    max_files_touched: 5
    max_loc_delta: 300
    max_checklist_items: 12
    max_dep_depth: 3
    escalate_task_types: []
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_schemas.py -k capability_profile -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/models/schemas.py config/praxis.yaml tests/test_schemas.py
git commit -m "feat: add numeric capability limits to CapabilityProfile (F2)"
```

---

### Task 3: Inject HARD CONSTRAINTS + extended leaf shape into the decompose prompt

**Files:**
- Modify: `src/orchestrator/core/plan_review.py` (`_PROMPT`, `build_review_prompt`)
- Test: `tests/test_plan_review.py`

**Depends on:** Task 2

Context: The prompt must state the limits as an explicit rejection contract and ask for the extended leaf fields (`files`, `task_type`, `estimated_loc`, `verification`), so the brain produces them and the validator can enforce them.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_plan_review.py`:

```python
from orchestrator.core.plan_review import build_review_prompt
from orchestrator.models.schemas import CapabilityProfile


def test_prompt_includes_hard_constraints_and_extended_shape():
    profile = CapabilityProfile(
        model_name="m",
        parameter_count_b=30,
        context_window=8192,
        max_files_touched=4,
        max_loc_delta=250,
        max_checklist_items=10,
        max_dep_depth=2,
    )
    prompt = build_review_prompt("PLAN BODY", profile, "no history", 3200)
    assert "HARD CONSTRAINTS" in prompt
    assert "at most 4 files" in prompt
    assert "250" in prompt
    assert "at most 10 checklist" in prompt
    assert "at most 2 deep" in prompt
    # Extended leaf fields requested:
    assert '"files"' in prompt
    assert '"task_type"' in prompt
    assert '"estimated_loc"' in prompt
    assert '"verification"' in prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_plan_review.py::test_prompt_includes_hard_constraints_and_extended_shape -v`
Expected: FAIL on the first missing assertion (`HARD CONSTRAINTS`).

- [ ] **Step 3: Extend the prompt**

In `src/orchestrator/core/plan_review.py`, insert a HARD CONSTRAINTS block into `_PROMPT` immediately after the existing line
`Hard limit: each leaf task's full context must fit ~{per_leaf_token_budget} tokens.`
(keep the `{{ }}` doubling rules for the JSON example intact):

```
HARD CONSTRAINTS (leaves violating these are rejected automatically and sent
back for one revision):
- touch at most {max_files_touched} files (list them in "files")
- change at most ~{max_loc_delta} lines of code per leaf ("estimated_loc")
- at most {max_checklist_items} checklist items per leaf
- dependency chains at most {max_dep_depth} deep; every id in "depends_on"
  MUST name another leaf in this same response (no dangling references)
- every leaf MUST include a concrete "verification" (>40 chars): a runnable
  command, a test path, or an observable behavior a reviewer can check

For every leaf you MUST also provide:
- "files": the exact source and test files this leaf creates or edits
- "task_type": one of feature|test|refactor|docs|migration|config
- "estimated_loc": approximate number of lines changed
- "verification": how to confirm the leaf works
```

Update the JSON example at the bottom of `_PROMPT` so it shows the extended shape:

```
Respond with ONLY valid JSON:
{{
  "tasks": [
    {{"id": "t1", "title": "...", "description": "...", "plan_text": "...",
      "files": ["src/x.py", "tests/test_x.py"], "task_type": "feature",
      "estimated_loc": 120,
      "verification": "uv run pytest tests/test_x.py passes",
      "depends_on": [], "checklist": [{{"text": "..."}}],
      "needs_stronger_model": false}}
  ]
}}
```

In `build_review_prompt`, add the new format fields (they come off the profile):

```python
    return _PROMPT.format(
        model_name=profile.model_name,
        parameter_count_b=profile.parameter_count_b,
        context_window=profile.context_window,
        strengths=profile.strengths,
        weaknesses=profile.weaknesses,
        max_task_complexity=profile.max_task_complexity,
        max_files_touched=profile.max_files_touched,
        max_loc_delta=profile.max_loc_delta,
        max_checklist_items=profile.max_checklist_items,
        max_dep_depth=profile.max_dep_depth,
        history_summary=history_summary,
        per_leaf_token_budget=per_leaf_token_budget,
        plan_text=plan_text,
    )
```

Note: `build_review_prompt` is typed `profile: CapabilityProfile`, so these attributes are guaranteed present. (Duck-typed fakes are only used against `decompose_plan`, not `build_review_prompt`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_plan_review.py -v`
Expected: PASS (new test plus existing parse tests).

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/plan_review.py tests/test_plan_review.py
git commit -m "feat: inject hard constraints + extended leaf shape into decompose prompt (F2)"
```

---

### Task 4: Create the deterministic leaf validator

**Files:**
- Create: `src/orchestrator/core/leaf_validator.py`
- Test: `tests/test_leaf_validator.py`

**Depends on:** Task 2

Context: A pure gate, in the spirit of `verify_gate.py`, run before anything expensive. It classifies each violation as HARD (structural, fail-closed) or SOFT (heuristic, informed re-decompose then warn). Reads profile limits via `getattr(..., DEFAULT)` so older/duck-typed profiles still work.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_leaf_validator.py`:

```python
"""Tests for the deterministic leaf validator (F3)."""

from __future__ import annotations

from orchestrator.core.leaf_validator import (
    ValidationResult,
    format_violations_feedback,
    validate_leaves,
)
from orchestrator.models.schemas import CapabilityProfile


def _profile(**kw: object) -> CapabilityProfile:
    base = dict(model_name="m", parameter_count_b=30, context_window=8192)
    base.update(kw)
    return CapabilityProfile(**base)  # type: ignore[arg-type]


def _leaf(slug: str, **kw: object) -> dict:
    leaf = {
        "slug": slug,
        "title": slug,
        "description": "d",
        "plan_text": "d",
        "depends_on": [],
        "checklist": [{"text": "step"}],
        "files": ["src/a.py"],
        "task_type": "feature",
        "estimated_loc": 50,
        "verification": "uv run pytest tests/test_a.py passes and returns 0",
        "needs_stronger_model": False,
    }
    leaf.update(kw)
    return leaf


def test_clean_plan_has_no_violations():
    plan = "line one\nline two"
    opus_plan = {"tasks": [_leaf("a", plan_text="line one\nline two")]}
    result = validate_leaves(opus_plan, _profile(), plan)
    assert isinstance(result, ValidationResult)
    assert result.hard == []
    assert result.soft == []
    assert result.dispatchable is True


def test_dangling_dependency_is_hard():
    opus_plan = {"tasks": [_leaf("a", depends_on=["ghost"])]}
    result = validate_leaves(opus_plan, _profile(), "plan")
    assert any(v.rule_id == "dangling_dep" for v in result.hard)
    assert result.dispatchable is False


def test_dependency_cycle_is_hard():
    opus_plan = {
        "tasks": [_leaf("a", depends_on=["b"]), _leaf("b", depends_on=["a"])]
    }
    result = validate_leaves(opus_plan, _profile(), "plan")
    assert any(v.rule_id == "dep_cycle" for v in result.hard)


def test_dependency_depth_exceeded_is_hard():
    opus_plan = {
        "tasks": [
            _leaf("a"),
            _leaf("b", depends_on=["a"]),
            _leaf("c", depends_on=["b"]),
        ]
    }
    result = validate_leaves(opus_plan, _profile(max_dep_depth=1), "plan")
    assert any(v.rule_id == "dep_depth" for v in result.hard)


def test_too_many_files_is_hard():
    opus_plan = {"tasks": [_leaf("a", files=["s/1", "s/2", "s/3"])]}
    result = validate_leaves(opus_plan, _profile(max_files_touched=2), "plan")
    v = next(v for v in result.hard if v.rule_id == "max_files")
    assert v.measured == 3
    assert v.limit == 2


def test_estimated_loc_over_limit_is_hard():
    opus_plan = {"tasks": [_leaf("a", estimated_loc=999)]}
    result = validate_leaves(opus_plan, _profile(max_loc_delta=300), "plan")
    assert any(v.rule_id == "max_loc" for v in result.hard)


def test_missing_verification_is_hard():
    opus_plan = {"tasks": [_leaf("a", verification="")]}
    result = validate_leaves(opus_plan, _profile(), "plan")
    assert any(v.rule_id == "verification" for v in result.hard)


def test_trivial_verification_is_hard():
    opus_plan = {"tasks": [_leaf("a", verification="it works")]}
    result = validate_leaves(opus_plan, _profile(), "plan")
    assert any(v.rule_id == "verification" for v in result.hard)


def test_escalate_task_type_without_flag_is_hard():
    opus_plan = {
        "tasks": [_leaf("a", task_type="migration", needs_stronger_model=False)]
    }
    result = validate_leaves(opus_plan, _profile(escalate_task_types=["migration"]), "plan")
    assert any(v.rule_id == "escalate_mismatch" for v in result.hard)


def test_non_verbatim_plan_text_is_soft():
    plan = "def foo(x: int) -> str:\n    return str(x)"
    opus_plan = {"tasks": [_leaf("a", plan_text="does some conversion, roughly")]}
    result = validate_leaves(opus_plan, _profile(), plan)
    assert result.hard == []
    assert any(v.rule_id == "plan_text_verbatim" for v in result.soft)
    assert result.dispatchable is True  # soft only


def test_file_overlap_without_edge_is_soft():
    opus_plan = {
        "tasks": [
            _leaf("a", files=["src/shared.py"]),
            _leaf("b", files=["src/shared.py"]),
        ]
    }
    result = validate_leaves(opus_plan, _profile(), "plan")
    assert any(v.rule_id == "file_overlap" for v in result.soft)


def test_file_overlap_with_edge_is_allowed():
    opus_plan = {
        "tasks": [
            _leaf("a", files=["src/shared.py"]),
            _leaf("b", files=["src/shared.py"], depends_on=["a"]),
        ]
    }
    result = validate_leaves(opus_plan, _profile(), "plan")
    assert not any(v.rule_id == "file_overlap" for v in result.soft)


def test_oversized_checklist_is_soft():
    big = [{"text": f"s{i}"} for i in range(20)]
    opus_plan = {"tasks": [_leaf("a", checklist=big)]}
    result = validate_leaves(opus_plan, _profile(max_checklist_items=12), "plan")
    assert any(v.rule_id == "checklist_size" for v in result.soft)


def test_vague_phrase_is_soft():
    opus_plan = {"tasks": [_leaf("a", description="handle edge cases as needed")]}
    result = validate_leaves(opus_plan, _profile(), "plan")
    assert any(v.rule_id == "vague_phrase" for v in result.soft)


def test_format_feedback_lists_each_violation():
    opus_plan = {"tasks": [_leaf("a", files=["1", "2", "3"], depends_on=["ghost"])]}
    result = validate_leaves(opus_plan, _profile(max_files_touched=2), "plan")
    feedback = format_violations_feedback(result)
    assert "max_files" in feedback
    assert "dangling_dep" in feedback
    assert "a" in feedback  # the leaf slug
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_leaf_validator.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.core.leaf_validator'`

- [ ] **Step 3: Implement the validator**

Create `src/orchestrator/core/leaf_validator.py`:

```python
"""Deterministic quality gate for a decomposed plan (F3).

Pure functions, no LLM. Called by ``decompose_plan`` after ``normalize_slugs``
and ``drop_verification_only_leaves``. Same spirit as ``core/verify_gate``: a
cheap mechanical check before anything expensive runs.

Violations are tiered:

* HARD  -> structural, unambiguous. The plan is failed closed if any remain
          after one informed re-decompose round.
* SOFT  -> heuristic. Trigger one informed re-decompose; if still present the
          plan proceeds with the violations attached as warnings.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any


# Profile-limit fallbacks (mirror the CapabilityProfile field defaults) so a
# duck-typed or pre-migration profile still validates.
_DEFAULT_MAX_FILES = 5
_DEFAULT_MAX_LOC = 300
_DEFAULT_MAX_CHECKLIST = 12
_DEFAULT_MAX_DEP_DEPTH = 3

_MIN_VERIFICATION_LEN = 40
# A verification string is "runnable" if it names a command, a path, a test, or
# an observable outcome. Conservative: any one signal is enough.
_VERIFICATION_SIGNAL_RE = re.compile(
    r"""
      \b(pytest|mypy|ruff|uv\s+run|curl|make|npm|go\s+test)\b   # a command
    | [\w/\.\-]+\.(py|md|ya?ml|toml|js|ts|json)\b               # a file path
    | \b(returns?|raises?|exits?|responds?|passes|status)\b     # an outcome
    | \b\d{3}\b                                                  # a status code
    """,
    re.VERBOSE | re.IGNORECASE,
)

_VAGUE_PHRASE_RE = re.compile(
    r"\b(as\s+needed|appropriately|handle\s+edge\s+cases|etc\.?|and\s+so\s+on|"
    r"where\s+appropriate|as\s+necessary)\b",
    re.IGNORECASE,
)

# Fraction of a leaf's plan_text lines that must fuzzy-match a source-plan line.
_VERBATIM_THRESHOLD = 0.70
_VERBATIM_LINE_RATIO = 0.85


@dataclass(frozen=True)
class Violation:
    """One rule failure against one leaf."""

    rule_id: str
    leaf_slug: str
    measured: int | str | None = None
    limit: int | str | None = None
    message: str = ""


@dataclass(frozen=True)
class ValidationResult:
    """Tiered result of validating a decomposed plan."""

    hard: list[Violation] = field(default_factory=list)
    soft: list[Violation] = field(default_factory=list)

    @property
    def dispatchable(self) -> bool:
        """True when no HARD violations remain (SOFT are warnings)."""
        return not self.hard

    @property
    def clean(self) -> bool:
        """True when there are no violations at all."""
        return not self.hard and not self.soft


def _limit(profile: Any, name: str, default: int) -> int:
    value = getattr(profile, name, default)
    return int(value) if value is not None else default


def _slug(task: dict[str, Any]) -> str:
    return str(task.get("slug") or task.get("id") or task.get("title") or "<leaf>")


def _dep_depth(slug: str, edges: dict[str, list[str]], seen: frozenset[str]) -> int:
    """Longest dependency chain rooted at ``slug`` (cycles clamp at current depth)."""
    deps = [d for d in edges.get(slug, []) if d in edges and d not in seen]
    if not deps:
        return 0
    return 1 + max(_dep_depth(d, edges, seen | {slug}) for d in deps)


def _has_cycle(edges: dict[str, list[str]]) -> bool:
    color: dict[str, int] = {}  # 0=visiting, 1=done

    def visit(node: str) -> bool:
        color[node] = 0
        for nxt in edges.get(node, []):
            if nxt not in edges:
                continue  # dangling handled separately
            state = color.get(nxt)
            if state == 0:
                return True
            if state is None and visit(nxt):
                return True
        color[node] = 1
        return False

    return any(color.get(n) is None and visit(n) for n in edges)


def _is_verbatim(plan_text: str, source_plan: str) -> bool:
    lines = [ln.strip() for ln in plan_text.splitlines() if ln.strip()]
    if not lines:
        return False
    source_lines = [ln.strip() for ln in source_plan.splitlines() if ln.strip()]
    matched = 0
    for line in lines:
        for src in source_lines:
            if line in src or src in line:
                matched += 1
                break
            if difflib.SequenceMatcher(None, line, src).ratio() >= _VERBATIM_LINE_RATIO:
                matched += 1
                break
    return matched / len(lines) >= _VERBATIM_THRESHOLD


def validate_leaves(
    opus_plan: dict[str, Any], profile: Any, source_plan: str
) -> ValidationResult:
    """Validate a normalized opus_plan against a profile and the source plan.

    Args:
        opus_plan: ``{"tasks": [...]}`` with slugs assigned and depends_on as slugs.
        profile: CapabilityProfile (or duck-typed) carrying numeric limits.
        source_plan: The externally-authored plan text (for the verbatim check).

    Returns:
        A ValidationResult partitioning violations into HARD and SOFT tiers.
    """
    tasks: list[dict[str, Any]] = opus_plan.get("tasks", [])
    hard: list[Violation] = []
    soft: list[Violation] = []

    max_files = _limit(profile, "max_files_touched", _DEFAULT_MAX_FILES)
    max_loc = _limit(profile, "max_loc_delta", _DEFAULT_MAX_LOC)
    max_checklist = _limit(profile, "max_checklist_items", _DEFAULT_MAX_CHECKLIST)
    max_depth = _limit(profile, "max_dep_depth", _DEFAULT_MAX_DEP_DEPTH)
    escalate_types = set(getattr(profile, "escalate_task_types", []) or [])

    slugs = {_slug(t) for t in tasks}
    edges: dict[str, list[str]] = {
        _slug(t): [str(d) for d in (t.get("depends_on") or [])] for t in tasks
    }

    # HARD: dangling deps.
    for t in tasks:
        slug = _slug(t)
        for dep in t.get("depends_on") or []:
            if str(dep) not in slugs:
                hard.append(
                    Violation("dangling_dep", slug, str(dep), None,
                              f"depends_on '{dep}' names no leaf in this plan")
                )

    # HARD: cycle.
    if _has_cycle(edges):
        hard.append(Violation("dep_cycle", "<plan>", None, None,
                              "dependency graph contains a cycle"))

    # HARD: depth (skip if cyclic; depth is meaningless then).
    if not _has_cycle(edges):
        for slug in edges:
            depth = _dep_depth(slug, edges, frozenset())
            if depth > max_depth:
                hard.append(Violation("dep_depth", slug, depth, max_depth,
                                      f"dependency chain depth {depth} > {max_depth}"))

    # Per-leaf HARD checks.
    for t in tasks:
        slug = _slug(t)
        files = t.get("files") or []
        if len(files) > max_files:
            hard.append(Violation("max_files", slug, len(files), max_files,
                                  f"touches {len(files)} files, limit {max_files}"))
        loc = t.get("estimated_loc")
        if isinstance(loc, int) and loc > max_loc:
            hard.append(Violation("max_loc", slug, loc, max_loc,
                                  f"estimated_loc {loc} > {max_loc}"))
        verification = str(t.get("verification") or "")
        if (
            len(verification) < _MIN_VERIFICATION_LEN
            or not _VERIFICATION_SIGNAL_RE.search(verification)
        ):
            hard.append(Violation("verification", slug, len(verification),
                                  _MIN_VERIFICATION_LEN,
                                  "verification missing, too short, or not runnable"))
        task_type = str(t.get("task_type") or "")
        if task_type in escalate_types and not t.get("needs_stronger_model"):
            hard.append(Violation("escalate_mismatch", slug, task_type, None,
                                  f"task_type '{task_type}' must set needs_stronger_model"))

    # SOFT: plan_text verbatim fidelity.
    for t in tasks:
        slug = _slug(t)
        if not _is_verbatim(str(t.get("plan_text") or ""), source_plan):
            soft.append(Violation("plan_text_verbatim", slug, None, None,
                                  "plan_text is not a verbatim excerpt of the source plan"))

    # SOFT: file overlap without a dependency edge.
    file_owners: dict[str, list[str]] = {}
    for t in tasks:
        for f in t.get("files") or []:
            file_owners.setdefault(str(f), []).append(_slug(t))
    for f, owners in file_owners.items():
        for i in range(len(owners)):
            for j in range(i + 1, len(owners)):
                a, b = owners[i], owners[j]
                if b in edges.get(a, []) or a in edges.get(b, []):
                    continue
                soft.append(Violation("file_overlap", a, f, None,
                                      f"leaves {a} and {b} both edit {f} with no dep edge"))

    # SOFT: oversized checklist + vague phrasing.
    for t in tasks:
        slug = _slug(t)
        checklist = t.get("checklist") or []
        if len(checklist) > max_checklist:
            soft.append(Violation("checklist_size", slug, len(checklist), max_checklist,
                                  f"{len(checklist)} checklist items > {max_checklist}"))
        text = " ".join(
            str(t.get(k) or "") for k in ("title", "description", "plan_text")
        )
        if _VAGUE_PHRASE_RE.search(text):
            soft.append(Violation("vague_phrase", slug, None, None,
                                  "contains vague phrasing without specifics"))

    return ValidationResult(hard=hard, soft=soft)


def format_violations_feedback(result: ValidationResult) -> str:
    """Render violations as an instruction block appended to a re-decompose prompt."""
    lines = ["Your previous decomposition violated these constraints. Fix them:"]
    for v in result.hard:
        lines.append(f"- [MUST FIX] leaf '{v.leaf_slug}' rule={v.rule_id}: {v.message}")
    for v in result.soft:
        lines.append(f"- [improve] leaf '{v.leaf_slug}' rule={v.rule_id}: {v.message}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_leaf_validator.py -v`
Expected: PASS (all cases).

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/leaf_validator.py tests/test_leaf_validator.py
git commit -m "feat: add deterministic tiered leaf validator (F3)"
```

---

### Task 5: Wire validate-and-repair loop into decompose_plan

**Files:**
- Modify: `src/orchestrator/core/execute_plan_decompose.py` (`decompose_plan` body + signature)
- Modify: `tests/test_execute_plan_decompose.py` (extend `_FakeProfile`; add repair/fail-closed tests)

**Depends on:** Task 1, Task 4

Context: Replace the parse-only retry loop with parse -> normalize -> drop -> validate. On any violation with a round remaining, re-invoke the brain with `format_violations_feedback` appended. After the informed round: HARD violations remaining -> emit nothing here (emitter added in Task 9) and raise `PlanReviewError("plan_rejected: ...")` (caller sets plan error + FAILED); only SOFT remaining -> attach `opus_plan["validation_warnings"]` and proceed. New optional params `plan_id` and `emitter` are added now but only USED in Task 9 (keep them inert here so this task's diff stays focused).

- [ ] **Step 1: Write the failing tests**

First, extend `_FakeProfile` in `tests/test_execute_plan_decompose.py` so the validator's `getattr` reads real numbers (add these class attributes):

```python
class _FakeProfile:
    context_window = 8192
    model_name = "test-model"
    parameter_count_b = 7.0
    strengths = "coding"
    weaknesses = "math"
    max_task_complexity = "medium"
    max_files_touched = 5
    max_loc_delta = 300
    max_checklist_items = 12
    max_dep_depth = 3
    escalate_task_types: list[str] = []
```

Then add tests (note: every `raw` JSON must now include `verification` long enough to pass the HARD verification rule, else existing happy-path tests would start failing; update the shared `raw` fixtures in this file to include a valid `verification` and `files` on each task):

```python
class _SequenceRouter:
    """Returns a scripted response per call so we can test the repair round."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str]] = []

    async def run(self, call_site: str, prompt: str, project_id: Any = None) -> str:
        self.calls.append((call_site, prompt))
        idx = min(len(self.calls) - 1, len(self._responses) - 1)
        return self._responses[idx]


_GOOD_LEAF = (
    '{"tasks":[{"id":"t1","title":"A","description":"add a thing",'
    '"plan_text":"add a thing","files":["src/a.py"],"task_type":"feature",'
    '"estimated_loc":40,'
    '"verification":"uv run pytest tests/test_a.py passes and returns 0",'
    '"depends_on":[]}]}'
)


async def test_decompose_repairs_on_soft_violation_then_proceeds():
    # First draft: non-verbatim plan_text (soft). Second draft: clean.
    bad = (
        '{"tasks":[{"id":"t1","title":"A","description":"vague stuff",'
        '"plan_text":"totally different words here","files":["src/a.py"],'
        '"task_type":"feature","estimated_loc":40,'
        '"verification":"uv run pytest tests/test_a.py passes and returns 0",'
        '"depends_on":[]}]}'
    )
    router = _SequenceRouter([bad, _GOOD_LEAF])
    opus_plan = await decompose_plan(
        plan="add a thing",
        model="qwen3.6-27b",
        context=None,
        router=router,
        effective_settings=_FakeEffective(),
        project_id="p1",
    )
    assert len(router.calls) == 2  # informed re-decompose happened
    assert opus_plan["tasks"]
    assert "validation_warnings" not in opus_plan  # second draft was clean


async def test_decompose_fails_closed_on_persistent_hard_violation():
    # Dangling dep persists across both drafts -> plan_rejected.
    dangling = (
        '{"tasks":[{"id":"t1","title":"A","description":"d","plan_text":"d",'
        '"files":["src/a.py"],"task_type":"feature","estimated_loc":40,'
        '"verification":"uv run pytest tests/test_a.py passes and returns 0",'
        '"depends_on":["ghost"]}]}'
    )
    router = _SequenceRouter([dangling, dangling])
    with pytest.raises(PlanReviewError, match="plan_rejected"):
        await decompose_plan(
            plan="add a thing",
            model="qwen3.6-27b",
            context=None,
            router=router,
            effective_settings=_FakeEffective(),
            project_id="p1",
        )


async def test_decompose_keeps_soft_warnings_when_unrepaired():
    # Soft violation persists across both drafts -> proceed with warnings.
    soft = (
        '{"tasks":[{"id":"t1","title":"A","description":"d",'
        '"plan_text":"nowhere near the source text","files":["src/a.py"],'
        '"task_type":"feature","estimated_loc":40,'
        '"verification":"uv run pytest tests/test_a.py passes and returns 0",'
        '"depends_on":[]}]}'
    )
    router = _SequenceRouter([soft, soft])
    opus_plan = await decompose_plan(
        plan="add a thing",
        model="qwen3.6-27b",
        context=None,
        router=router,
        effective_settings=_FakeEffective(),
        project_id="p1",
    )
    assert opus_plan["validation_warnings"]
```

Also update the existing `raw` string literals in this file (in `test_decompose_plan_returns_normalized_opus_plan`, `_threads_context`, `_no_context`, and any flaky-router test) so each task dict carries `"files": ["src/x.py"]`, `"task_type": "feature"`, `"estimated_loc": 40`, and a `"verification"` string of the form `"uv run pytest tests/test_x.py passes and returns 0"`. Otherwise those tasks now trip the HARD `verification` rule.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_execute_plan_decompose.py -v`
Expected: the new tests FAIL (`_SequenceRouter`/repair not wired; `validation_warnings` missing).

- [ ] **Step 3: Rewrite the decompose_plan loop**

In `src/orchestrator/core/execute_plan_decompose.py`, add imports near the top:

```python
from orchestrator.core.leaf_validator import (
    format_violations_feedback,
    validate_leaves,
)
```

Change the `decompose_plan` signature to add the two inert-for-now params (place them at the end so existing keyword callers are unaffected):

```python
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
```

Replace the body from `profile = ...` through the `normalize_slugs`/`drop_verification_only_leaves` block (current lines 213-241) with:

```python
    profile = await effective_settings.capability_profile(project_id=None, model=model)
    per_leaf_budget = int(profile.context_window * (1 - WORKER_RESERVE_FRACTION))
    history = summarize_outcomes([])
    base_prompt = build_review_prompt(plan, profile, history, per_leaf_budget)

    prompt = base_prompt
    last_parse_exc: PlanReviewError | None = None
    opus_plan: dict[str, Any] | None = None
    result: Any = None

    for attempt in range(1, _DECOMPOSE_ATTEMPTS + 1):
        raw = await router.run("plan_review", prompt, project_id=project_id)
        try:
            candidate = parse_review_response(raw)
        except PlanReviewError as exc:
            last_parse_exc = exc
            logger.warning(
                "Decomposition parse failed (attempt %d/%d): %s",
                attempt,
                _DECOMPOSE_ATTEMPTS,
                exc,
            )
            continue

        normalize_slugs(candidate)
        drop_verification_only_leaves(candidate)
        result = validate_leaves(candidate, profile, plan)
        opus_plan = candidate

        if result.clean:
            break
        if attempt < _DECOMPOSE_ATTEMPTS:
            logger.info(
                "Decomposition attempt %d had %d hard / %d soft violations; "
                "requesting informed re-decompose.",
                attempt,
                len(result.hard),
                len(result.soft),
            )
            prompt = base_prompt + "\n\n" + format_violations_feedback(result)
            continue
        # Final attempt: stop looping, decide below.

    if opus_plan is None:
        raise (
            last_parse_exc
            if last_parse_exc is not None
            else PlanReviewError(  # noqa: EM101
                "decomposition failed with no parseable output"
            )
        )

    if result is not None and result.hard:
        summary = "; ".join(
            f"{v.leaf_slug}:{v.rule_id}" for v in result.hard
        )
        message = f"plan_rejected: {summary}"
        raise PlanReviewError(message)  # noqa: EM101

    if result is not None and result.soft:
        opus_plan["validation_warnings"] = [
            {"leaf": v.leaf_slug, "rule": v.rule_id, "message": v.message}
            for v in result.soft
        ]
```

Leave the existing tail of the function (the `count_plan_tasks` decompose_warning block, then `scrub_context` threading of `context_text`/`repo_memory`, then `return opus_plan`) exactly as-is; it runs after the new block.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_execute_plan_decompose.py tests/test_decompose_golden.py -v`
Expected: PASS (new repair/fail-closed tests plus updated existing tests).

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/execute_plan_decompose.py tests/test_execute_plan_decompose.py
git commit -m "feat: validate-and-repair loop with fail-closed hard gate in decompose_plan (F3)"
```

---

### Task 6: Defensive dangling-dependency check in the wave scheduler

**Files:**
- Modify: `src/orchestrator/core/task_queue.py:232-257` (`get_dispatchable_tasks`)
- Test: `tests/test_task_queue.py`

**Depends on:** None

Context (known live bug, roadmap 2.3): if a leaf's `depends_on` names a slug not present in `slug_to_task`, the `all(... == MERGED)` check evaluates over a `{}.get("status")` that is never `MERGED`, so the task is never dispatchable and the wave silently deadlocks. The validator (Task 4) rejects dangling deps at decompose time, but this is the belt-and-suspenders backstop for any graph that reaches the scheduler with a bad edge. Fail loudly instead of hanging.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_task_queue.py` (follow the file's existing fixture style for creating a plan + tasks; inspect the top of the file for the `TaskQueue`/`Database` fixtures already in use):

```python
async def test_dispatchable_raises_on_dangling_dependency(task_queue, seeded_plan):
    """A depends_on slug with no matching task must fail loudly, not deadlock."""
    # seeded_plan must persist an opus_plan whose task references a missing slug.
    # Build a minimal plan with one task depending on a nonexistent slug.
    plan_id = seeded_plan["id"]
    opus_plan = {
        "tasks": [
            {"slug": "real-task", "title": "Real", "depends_on": ["ghost-slug"]}
        ]
    }
    await task_queue._db.execute(
        "UPDATE plans SET opus_plan = ? WHERE id = ?",
        (__import__("json").dumps(opus_plan), plan_id),
    )
    # Insert the single backing task row so slug_to_task maps real-task.
    await task_queue._db.execute(
        "INSERT INTO tasks (id, plan_id, title, description, branch_name, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("t-real", plan_id, "Real", "d", "agent/real-task", "pending"),
    )
    with pytest.raises(ValueError, match="dangling dependency"):
        await task_queue.get_dispatchable_tasks(plan_id)
```

Note: if `test_task_queue.py` has no `seeded_plan` fixture, construct the plan inline using the same helpers other tests in that file use (search for `create_plan(` / `activate_plan(` usages and mirror them). Keep the assertion: `get_dispatchable_tasks` raises `ValueError` matching `dangling dependency`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_task_queue.py::test_dispatchable_raises_on_dangling_dependency -v`
Expected: FAIL (currently returns `[]`, does not raise).

- [ ] **Step 3: Add the defensive check**

In `get_dispatchable_tasks`, after building `slug_to_task` (current line 240-244) and before the dispatch loop, insert:

```python
        known_slugs = set(slug_to_task)
        for task_data in opus_plan["tasks"]:
            for dep in task_data.get("depends_on", []):
                if dep not in known_slugs:
                    message = (
                        f"plan {plan_id} has a dangling dependency: task "
                        f"{task_data.get('slug')!r} depends on unknown slug {dep!r}"
                    )
                    logger.error(message)
                    raise ValueError(message)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_task_queue.py -v`
Expected: PASS (new test plus existing scheduler tests, whose graphs have no dangling deps).

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/task_queue.py tests/test_task_queue.py
git commit -m "fix: fail loudly on dangling depends_on in wave scheduler (roadmap 2.3)"
```

---

### Task 7: Supply-chain detection helpers in diff_guard

**Files:**
- Modify: `src/orchestrator/core/diff_guard.py`
- Test: `tests/test_diff_guard.py`

**Depends on:** None

Context (F15): a local worker prompted with repo context is a supply-chain surface. Two pure detectors over a unified diff: added dependencies in manifests/lockfiles, and secrets by known prefix or keyword-assignment (NOT generic entropy, which would false-positive on lockfile hashes).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_diff_guard.py`:

```python
from orchestrator.core.diff_guard import added_dependencies, detect_secrets


def test_added_dependency_in_pyproject_is_flagged():
    diff = (
        "--- a/pyproject.toml\n+++ b/pyproject.toml\n"
        '+    "requests>=2.31",\n'
    )
    assert "pyproject.toml" in added_dependencies(diff)


def test_added_dependency_in_package_json_is_flagged():
    diff = (
        "--- a/package.json\n+++ b/package.json\n"
        '+    "left-pad": "^1.3.0",\n'
    )
    assert "package.json" in added_dependencies(diff)


def test_non_manifest_change_is_not_flagged_as_dependency():
    diff = "--- a/src/app.py\n+++ b/src/app.py\n+import os\n"
    assert added_dependencies(diff) == []


def test_removed_dependency_line_is_not_flagged():
    diff = "--- a/pyproject.toml\n+++ b/pyproject.toml\n-    \"requests>=2.31\",\n"
    assert added_dependencies(diff) == []


def test_detect_secrets_finds_private_key():
    diff = "--- a/x\n+++ b/x\n+-----BEGIN RSA PRIVATE KEY-----\n"
    assert detect_secrets(diff)


def test_detect_secrets_finds_keyword_assignment():
    diff = '--- a/x\n+++ b/x\n+api_key = "abcd1234abcd1234abcd1234"\n'
    assert detect_secrets(diff)


def test_detect_secrets_ignores_lockfile_hashes():
    # A resolved sha256 in a lockfile must NOT be treated as a secret.
    diff = (
        "--- a/uv.lock\n+++ b/uv.lock\n"
        '+hash = "sha256:3f786850e387550fdab836ed7e6dc881de23001b"\n'
    )
    assert detect_secrets(diff) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_diff_guard.py -k "added_dependency or detect_secrets" -v`
Expected: FAIL with `ImportError: cannot import name 'added_dependencies'`

- [ ] **Step 3: Implement the detectors**

Append to `src/orchestrator/core/diff_guard.py` (add `_NEW_B = re.compile(r"^\+\+\+ b/(.+)$")` near the existing `_OLD` regex at the top):

```python
_NEW_B = re.compile(r"^\+\+\+ b/(.+)$")

# Files whose added lines represent dependency changes.
_MANIFEST_SUFFIXES = (
    "pyproject.toml",
    "package.json",
    "package-lock.json",
    "poetry.lock",
    "uv.lock",
    "yarn.lock",
    "go.mod",
    "go.sum",
    "cargo.toml",
    "cargo.lock",
)
# An added manifest line that looks like a package requirement.
_DEP_LINE_RE = re.compile(
    r"""^\+\s*
        (?:["']?[A-Za-z0-9][\w.\-]*["']?\s*(?:[:=]|==|>=|<=|~=|>|<|@)   # name + op
        | [A-Za-z0-9][\w.\-]*\s+v?\d)                                    # go.mod style
    """,
    re.VERBOSE,
)

# Secret signatures: known token prefixes and keyword assignments only. NOT a
# generic entropy scan, which false-positives on lockfile digests.
_SECRET_RES = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(
        r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"\s]{16,}['\"]"
    ),
)


def _is_manifest(path: str | None) -> bool:
    return bool(path) and path.lower().endswith(_MANIFEST_SUFFIXES)


def added_dependencies(diff: str) -> list[str]:
    """Return manifest/lockfile paths that gained a dependency-looking line.

    Only added lines (``+``, not ``+++``) inside a recognized manifest count.

    Args:
        diff: Unified diff text.

    Returns:
        Sorted unique list of flagged manifest paths.
    """
    flagged: set[str] = set()
    current: str | None = None
    for line in diff.splitlines():
        m = _NEW_B.match(line)
        if m:
            current = m.group(1)
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if _is_manifest(current) and line.startswith("+") and _DEP_LINE_RE.match(line):
            assert current is not None
            flagged.add(current)
    return sorted(flagged)


def detect_secrets(diff: str) -> list[str]:
    """Return added diff lines that match a known secret signature.

    Args:
        diff: Unified diff text.

    Returns:
        List of the offending added lines (stripped of the leading ``+``).
    """
    hits: list[str] = []
    for line in diff.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        body = line[1:]
        if any(rgx.search(body) for rgx in _SECRET_RES):
            hits.append(body.strip())
    return hits
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_diff_guard.py -v`
Expected: PASS (new tests plus the existing `destructive_deletions` tests).

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/diff_guard.py tests/test_diff_guard.py
git commit -m "feat: detect added dependencies and secrets in diffs (F15)"
```

---

### Task 8: Force the human merge gate on supply-chain hits

**Files:**
- Modify: `src/orchestrator/core/orchestrator_review.py` (imports + the `verdict == "pass"` branch near line 167)
- Test: `tests/test_orchestrator_review.py` (or the review test module; confirm with `grep -rln "review_task" tests/`)

**Depends on:** Task 7

Context (F15): even on a PASS verdict, a diff that adds a dependency or contains a secret must never auto-merge; it parks for explicit human approval with an annotated reason.

- [ ] **Step 1: Write the failing test**

Locate the existing review-task test module (`grep -rln "async def test_review_task" tests/`). Add a test that mirrors the module's existing setup for a PASS verdict with `auto_merge` eligible, but whose diff adds a dependency, and asserts the task is parked (`mark_passed`) rather than merged (`merge_pr` NOT called). Use the module's established mocking style (the review tests already stub `self._git`, `self._opus`, `self._tq`). Skeleton:

```python
async def test_pass_with_added_dependency_parks_instead_of_merging(review_fixture):
    # Arrange: verdict PASS, auto_merge eligible, diff adds a dependency.
    orch, mocks = review_fixture
    mocks.opus.review_diff.return_value = {"verdict": "pass", "feedback": "ok"}
    mocks.git.get_pr_diff.return_value = (
        "--- a/pyproject.toml\n+++ b/pyproject.toml\n+    \"evil>=1.0\",\n"
    )
    # ... project configured with auto_merge=True, non-protected base ...

    await orch.review_task(task_id)

    mocks.git.merge_pr.assert_not_called()
    mocks.tq.mark_passed.assert_called_once()
    feedback = mocks.tq.mark_passed.call_args.args[1]
    assert "supply-chain" in feedback
```

Adapt names to the module's actual fixtures. The invariant under test: PASS + auto_merge_eligible + supply-chain hit => `merge_pr` NOT called, `mark_passed` called, feedback mentions `supply-chain`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_orchestrator_review.py -k supply_chain -v`
Expected: FAIL (currently auto-merges because the guard does not exist).

- [ ] **Step 3: Wire the guard**

In `src/orchestrator/core/orchestrator_review.py`, extend the diff_guard import (line 18):

```python
from orchestrator.core.diff_guard import (
    added_dependencies,
    destructive_deletions,
    detect_secrets,
)
```

In the `if verdict == "pass":` block (line 167), compute the supply-chain hits and gate the auto-merge:

```python
        if verdict == "pass":
            supply_chain = added_dependencies(diff) + detect_secrets(diff)
            base_branch = plan.get("plan_branch_name") if plan else None
            if not supply_chain and auto_merge_eligible(project, base_branch):
                await self._git.merge_pr(".", pr_number, repo=repo)
                await self._tq.mark_merged(task_id)
                await self._sync_plan_checkbox(task)
                self._bus.publish(
                    {
                        "type": "task_completed",
                        "task_id": task_id,
                        "pr_url": task["pr_url"],
                    }
                )
                return
            if supply_chain:
                feedback = (
                    f"[supply-chain] Forcing human review: {supply_chain}. "
                    "A dependency change or secret was detected; this must not "
                    "auto-merge. " + (feedback or "")
                )
                self._bus.publish(
                    {
                        "type": "task_supply_chain_gate",
                        "task_id": task_id,
                        "flags": supply_chain,
                    }
                )
            # Default: park the reviewed PR for explicit human approval.
            await self._tq.mark_passed(task_id, feedback)
            self._bus.publish(
                {
                    "type": "task_awaiting_merge",
                    "task_id": task_id,
                    "pr_url": task["pr_url"],
                    "verdict": verdict,
                    "review_summary": feedback,
                    "branch": task["branch_name"],
                }
            )
            return
```

(This replaces the existing pass-branch body from line 167 through its `return`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_orchestrator_review.py -v`
Expected: PASS (new test plus existing review tests; the existing auto-merge tests use diffs with no dependency/secret lines, so `supply_chain` is empty and they still merge).

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/orchestrator_review.py tests/test_orchestrator_review.py
git commit -m "feat: force human merge gate on dependency/secret diffs (F15)"
```

---

### Task 9: Wire the capability decision-log emitters (S1)

**Files:**
- Modify: `src/orchestrator/core/execute_plan_decompose.py` (emit inside `decompose_plan`)
- Modify: `src/orchestrator/core/orchestrator.py` (construct emitter, pass `plan_id`/`emitter`)
- Test: `tests/test_execute_plan_decompose.py`

**Depends on:** Task 5

Context (S1): `CapabilityEventEmitter` (in `core/capability_events.py`) and the `capability_events` table exist but have no production caller. This is the first. Emit `decompose_input` once before the brain call, `leaf_validated`/`leaf_rejected` per leaf from the final validation result, and `plan_rejected` on fail-closed. The emitter is None-safe: unit tests and any caller without a plan_id skip emission.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_execute_plan_decompose.py`:

```python
class _RecordingEmitter:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> str:
        self.events.append(event)
        return "row-id"


async def test_decompose_emits_decision_records():
    router = _SequenceRouter([_GOOD_LEAF])
    emitter = _RecordingEmitter()
    await decompose_plan(
        plan="add a thing",
        model="qwen3.6-27b",
        context=None,
        router=router,
        effective_settings=_FakeEffective(),
        project_id="p1",
        plan_id="plan-1",
        emitter=emitter,
    )
    types = [e.event_type for e in emitter.events]
    assert "decompose_input" in types
    assert "leaf_validated" in types


async def test_decompose_emits_plan_rejected_on_hard_fail():
    dangling = (
        '{"tasks":[{"id":"t1","title":"A","description":"d","plan_text":"d",'
        '"files":["src/a.py"],"task_type":"feature","estimated_loc":40,'
        '"verification":"uv run pytest tests/test_a.py passes and returns 0",'
        '"depends_on":["ghost"]}]}'
    )
    router = _SequenceRouter([dangling, dangling])
    emitter = _RecordingEmitter()
    with pytest.raises(PlanReviewError, match="plan_rejected"):
        await decompose_plan(
            plan="add a thing",
            model="qwen3.6-27b",
            context=None,
            router=router,
            effective_settings=_FakeEffective(),
            project_id="p1",
            plan_id="plan-1",
            emitter=emitter,
        )
    assert any(e.event_type == "plan_rejected" for e in emitter.events)
    assert any(e.event_type == "leaf_rejected" for e in emitter.events)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_execute_plan_decompose.py -k "decision_records or plan_rejected" -v`
Expected: FAIL (no events recorded).

- [ ] **Step 3: Emit events in decompose_plan**

In `src/orchestrator/core/execute_plan_decompose.py`, add imports:

```python
import hashlib

from orchestrator.core.capability_events import (
    DecomposeInputEvent,
    LeafRejectedEvent,
    LeafValidatedEvent,
    PlanRejectedEvent,
)
```

Add a module-level helper near the top:

```python
def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
```

In `decompose_plan`, right after computing `base_prompt` (and before the loop), emit the input event when possible:

```python
    _emit = emitter is not None and plan_id is not None
    if _emit:
        await emitter.emit(
            DecomposeInputEvent(
                plan_id=plan_id,
                model_name=model,
                per_leaf_budget=per_leaf_budget,
                profile_summary=(
                    f"{profile.model_name}:{profile.parameter_count_b}b:"
                    f"ctx{profile.context_window}"
                ),
                history_summary_hash=_hash(history),
                plan_hash=_hash(plan),
            )
        )
```

After the loop resolves (`opus_plan is not None`), before the HARD-fail raise, emit per-leaf verdicts from the final `result`. Replace the HARD-fail and SOFT blocks from Task 5 with:

```python
    if _emit and result is not None:
        rejected_slugs = {v.leaf_slug for v in result.hard} | {
            v.leaf_slug for v in result.soft
        }
        for v in result.hard:
            await emitter.emit(
                LeafRejectedEvent(
                    plan_id=plan_id,
                    leaf_slug=v.leaf_slug,
                    rule_id=v.rule_id,
                    measured=v.measured,
                    limit=v.limit,
                )
            )
        for v in result.soft:
            await emitter.emit(
                LeafRejectedEvent(
                    plan_id=plan_id,
                    leaf_slug=v.leaf_slug,
                    rule_id=v.rule_id,
                    measured=v.measured,
                    limit=v.limit,
                )
            )
        for task in opus_plan["tasks"]:
            slug = str(task.get("slug", ""))
            if slug and slug not in rejected_slugs:
                await emitter.emit(
                    LeafValidatedEvent(plan_id=plan_id, leaf_slug=slug)
                )

    if result is not None and result.hard:
        summary = "; ".join(f"{v.leaf_slug}:{v.rule_id}" for v in result.hard)
        if _emit:
            await emitter.emit(
                PlanRejectedEvent(
                    plan_id=plan_id,
                    violations=[f"{v.leaf_slug}:{v.rule_id}" for v in result.hard],
                    rounds=_DECOMPOSE_ATTEMPTS,
                )
            )
        message = f"plan_rejected: {summary}"
        raise PlanReviewError(message)  # noqa: EM101

    if result is not None and result.soft:
        opus_plan["validation_warnings"] = [
            {"leaf": v.leaf_slug, "rule": v.rule_id, "message": v.message}
            for v in result.soft
        ]
```

(Note: `LeafRejectedEvent` is emitted for both hard and soft rejections so the decision log records every rule that fired; a leaf that only tripped soft rules is still both "rejected" for that rule and, because it is in `rejected_slugs`, not double-counted as validated.)

- [ ] **Step 4: Wire the emitter in the orchestrator**

In `src/orchestrator/core/orchestrator.py`, add the import near the other core imports:

```python
from orchestrator.core.capability_events import CapabilityEventEmitter
```

In `Orchestrator.__init__`, after `self._bus = event_bus` (line 45), construct the emitter from the db behind the task queue and the bus:

```python
        # First production caller of the capability decision log (S1). The
        # emitter is None-safe downstream, but we always have a db + bus here.
        self._capability_emitter = CapabilityEventEmitter(task_queue._db, event_bus)
```

At the `decompose_plan(...)` call (line 124), pass the new args:

```python
            opus_plan = await decompose_plan(
                plan=payload["plan"],
                model=payload["model"],
                context=payload.get("context"),
                router=self._llm_router,
                effective_settings=self._effective_settings,
                project_id=project["id"],
                local_context=payload.get("local_context"),
                plan_id=plan_id,
                emitter=self._capability_emitter,
            )
```

Note: `task_queue._db` is the established way the orchestrator reaches the shared `Database` (the queue holds it as `self._db`). If a cleaner accessor exists by the time you implement this, prefer it; otherwise `_db` matches current internal usage.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_execute_plan_decompose.py tests/test_orchestrator.py -v`
Expected: PASS. Then run the capability-events tests to confirm no regression: `uv run pytest tests/test_capability_event_emitter.py tests/test_database_capability_events.py -v`

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/core/execute_plan_decompose.py src/orchestrator/core/orchestrator.py tests/test_execute_plan_decompose.py
git commit -m "feat: wire capability decision-log emitters for decompose/validate (S1)"
```

---

### Task 10: Full-suite verification, docs, and final commit

**Files:**
- Modify: `CLAUDE.md` (Gotchas index)
- Modify: `docs/superpowers/ROADMAP.md` (mark Plan 2 status)
- Modify: `docs/gotchas.md` (narrative entry, kept in sync with CLAUDE.md per project convention)

**Depends on:** Task 1, Task 2, Task 3, Task 4, Task 5, Task 6, Task 7, Task 8, Task 9

- [ ] **Step 1: Run the full gate**

Run:
```bash
uv run ruff format src/ tests/
uv run ruff check --fix src/ tests/
uv run mypy src/orchestrator/ --ignore-missing-imports
uv run pytest --cov=orchestrator --cov-report=term-missing -q
```
Expected: ruff clean, mypy clean, all tests pass, coverage does not drop below the CI floor (`--cov-fail-under=80`). Fix any failures before proceeding (do not paper over them).

- [ ] **Step 2: Add the gotcha index lines**

In `CLAUDE.md`, under the Gotchas condensed index, add:

```markdown
- **Decomposition is fail-closed on structural violations** (`core/leaf_validator.py`, F3) — HARD rules (dangling/cyclic/too-deep deps, too many files, oversize LOC, missing/trivial verification, escalate-type without the flag) reject the plan after one informed re-decompose; SOFT rules (plan_text non-verbatim, file overlap, vague phrasing, oversized checklist) inform one re-decompose then degrade to `opus_plan["validation_warnings"]`. Validator limits come off `CapabilityProfile` (F2), read via `getattr` so old profiles still validate.
- **One budget fraction** — `WORKER_RESERVE_FRACTION = 0.6` in `core/token_budget.py` is the single source for both `worker_bible` and the decompose per-leaf budget (`1 - 0.6`); do not reintroduce a second literal.
- **Wave scheduler fails loudly on dangling deps** — `get_dispatchable_tasks` raises `ValueError` on an unknown `depends_on` slug instead of silently deadlocking (roadmap 2.3 bug).
- **Supply-chain diff gate** (`core/diff_guard.added_dependencies`/`detect_secrets`, F15) — a PASS verdict still parks (never auto-merges) when the diff adds a manifest dependency or matches a secret signature.
- **Capability emitters are live for decompose/validate** (`decompose_input`/`leaf_validated`/`leaf_rejected`/`plan_rejected`, S1) — constructed in `Orchestrator.__init__`, threaded into `decompose_plan`; None-safe so unit tests skip. `task_split`/`task_escalated`/`outcome_recorded` remain stubbed until Plans 3/5.
```

Mirror the same entries as a short narrative paragraph in `docs/gotchas.md` (project convention: CLAUDE.md keeps the one-line index, `docs/gotchas.md` keeps the full narrative; keep both in sync).

In `docs/superpowers/ROADMAP.md`, change the Plan 2 row status from `TODO (next)` to `DONE (code)` and update Plan 3 to `TODO (next)`; update the `Last updated` date and the honest-baseline bullets that this plan closes (prose-only sizing rules, dangling-dep bug, shape-only validation).

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/gotchas.md docs/superpowers/ROADMAP.md
git commit -m "docs: record F2/F3/F15/S1 gotchas + advance roadmap to Plan 3"
```

- [ ] **Step 4: Push to main (only after the full gate is green)**

```bash
git push origin main
```

Note: no agent-image rebuild is required (no `docker/*/entrypoint.sh` changed). If any entrypoint was touched during implementation, rebuild the 3 harness images per the CLAUDE.md gotcha before pushing.

---

## Parallel Execution Map

- **Wave 1:** Task 1 (budget constant), Task 2 (profile fields), Task 6 (scheduler fix), Task 7 (diff_guard detectors) — no dependencies, run in parallel.
- **Wave 2:** Task 3 (prompt, depends on Task 2), Task 4 (validator, depends on Task 2), Task 8 (review gate, depends on Task 7).
- **Wave 3:** Task 5 (decompose repair loop, depends on Task 1 + Task 4).
- **Wave 4:** Task 9 (emitter wiring, depends on Task 5).
- **Wave 5:** Task 10 (full-suite verification + docs, depends on all).

Note for the executor: Task 5 rewrites the same `decompose_plan` body that Task 9 further edits, and Task 5 updates shared `raw` fixtures in `tests/test_execute_plan_decompose.py`. Run Task 5 fully (green) before Task 9. Tasks 1, 2, 6, 7 touch disjoint files and are safe to parallelize; if using subagent-driven-development in one worktree, still serialize commits.

---

## Notes / Risks

- **Tiering keeps dogfood runs alive:** only unambiguous structural problems (Task 4 HARD rules) can fail a plan closed. Heuristic checks (verbatim fidelity, overlap, vagueness) inform one re-decompose then warn, so a stochastic brain draw cannot brick a run.
- **Existing decompose fixtures must gain `verification`/`files`:** the new HARD `verification` rule means every task dict in `tests/test_execute_plan_decompose.py` needs a runnable `verification` and a `files` entry, or previously-passing happy-path tests will start failing. Task 5 Step 1 calls this out explicitly.
- **`_LEAF_BUDGET_FRACTION` removal is behavior-neutral:** `0.4 == 1 - 0.6`, verified by Task 1's test.
- **Emitter reaches the DB via `task_queue._db`:** matches current internal orchestrator usage; swap for a public accessor only if one exists.
- **Secret detection is signature-based, not entropy-based:** deliberately, so lockfile digests (sha256 hashes) do not trip it (Task 7 has an explicit test for this).
- **No new DB migration:** the `capability_events` table (migration 0004) and profile-as-YAML already exist; this plan adds no columns.
```