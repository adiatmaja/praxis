---
type: plan
spec_path: docs/superpowers/specs/2026-08-06-usable-praxis-spec.md
---

# Decomposition Standard v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn Praxis's one-shot static decomposition into an adaptive, typed, difficulty-aware engine: a codified leaf standard with typed templates, a fixed context-pack priority order, brain-driven triage that splits or escalates a twice-failed leaf instead of retrying it unchanged, and a pre-dispatch difficulty score that gates leaves before a container is ever spawned.

**Architecture:** Three deterministic layers wrapped around exactly two new brain calls. Phase A adds `core/leaf_templates.py` (single source of truth for per-type `plan_text` sections, read by both the decompose prompt and the F3 validator) and re-ranks `core/worker_bible.py` so edit locations and acceptance checks outrank narrative. Phase B adds `core/leaf_triage.py` (one `leaf_failure_triage` brain call, strict Pydantic output, hard bounds) plus `core/leaf_split.py` (pure graph rewiring) and `core/escalation.py` (dispatch-time model substitution, since the implement seat is spawn-baked and not router-driven). Phase C adds `core/difficulty.py`, a transparent hand-weighted logistic behind a `DifficultyScorer` protocol so the learned Beta-posterior scorer swaps in later without touching call sites. Every decision emits a versioned capability event.

**Tech Stack:** Python 3.11, Pydantic v2, aiosqlite (raw SQL, versioned `MIGRATIONS`), FastAPI SSE event bus, pytest with `asyncio_mode = "auto"`.

**Spec:** `docs/superpowers/specs/2026-08-06-usable-praxis-spec.md` (workstream A: sections 2.1 through 2.5; plan rows P1, P2, P3).

---

## Execution order across the three Usable-Praxis plans

The umbrella spec's dependency order is P1, P7, P4, P2, P3, P5, P6, P8. Consolidated into three plan docs, the phase order is:

1. **This plan, Phase A** (P1): standard doc, leaf types, context ordering
2. `2026-08-06-usable-praxis-product.md` **Phase A** (P7): compose, config mount, `praxis init` / `praxis doctor`, presets, digest
3. `2026-08-06-praxis-bench.md` **Phase A** (P4): local git backend
4. **This plan, Phase B** (P2): triage, split, escalation
5. **This plan, Phase C** (P3): difficulty scoring
6. `2026-08-06-praxis-bench.md` **Phase B** (P5): pilot
7. `2026-08-06-praxis-bench.md` **Phase C** (P6): full run and report
8. `2026-08-06-usable-praxis-product.md` **Phase B** (P8): docs restructure and launch

Per the spec's section 9 sequencing exemption, no dogfood run is required between plans. The per-phase test bars in spec section 6 still hold: every behavior lands with a mutation-checked test.

---

## Standing constraints (read before Task 1)

These are project gotchas that this plan touches directly. Full narrative in `docs/gotchas.md`.

- **Status vocabulary is frozen.** `TaskStatus.SUPERSEDED` **already exists** (`src/orchestrator/models/schemas.py:29`), is already in `core/status_vocab.py` `TERMINAL_STATUSES`, and is already asserted in `tests/test_schemas.py:248` and `tests/test_status_vocab.py`. This plan does **not** add the enum value; it makes SUPERSEDED actually reachable and audits every consumer (Task 13). Do not add a new status value.
- **Schema changes go through `MIGRATIONS`.** Add a `Migration(7, ...)` to the list in `src/orchestrator/database.py`; never write an ad-hoc conditional rebuild. Steps must be idempotent (guard with `PRAGMA table_info`).
- **The orchestrator is split across mixins.** Triage wiring lands in `core/orchestrator_review.py` (the `ReviewMixin`) and `core/orchestrator_dispatch.py` (the `DispatchMixin`). Tests patch module-level helpers on the **mixin module that calls them**, not on `core.orchestrator`.
- **`LeafTask` changes extend the golden fixtures.** `tests/fixtures/decompose/expected_leaf_graph.json` is asserted byte-equal by `tests/test_decompose_golden.py`. Any new `LeafTask` field with a default appears in `model_dump()` output and **will** break that fixture. Update it in the same commit.
- **`get_dispatchable_tasks` maps opus_plan tasks to DB rows POSITIONALLY** (`core/task_queue.py:285-289`, `zip` by index over `opus_plan["tasks"]` and `get_tasks_for_plan` ordered by `rowid`). Split children must be **appended** to both lists in the same order, and the superseded parent must **never be removed** from either, or every slug-to-task mapping after it shifts by one.
- **No em dashes** in any prose, doc, code comment, or commit message. Use a comma, colon, or semicolon.

---

## File Structure

### Phase A (P1)

| File | Responsibility |
|------|----------------|
| Create `docs/decomposition-standard.md` | The cited leaf-validity contract; referenced by the prompt, the validator, and the benchmark |
| Create `src/orchestrator/core/leaf_templates.py` | `LeafType` required-section table plus prompt-rendering helper; single source read by prompt and validator |
| Modify `src/orchestrator/models/schemas.py` | Add `LeafType` enum, `leaf_type` and `neighbor_contracts` fields on `LeafTask`, bump `LEAF_SCHEMA_VERSION` |
| Modify `src/orchestrator/core/plan_review.py` | Inject the leaf-type table and skeletons into `_PROMPT` |
| Modify `src/orchestrator/core/leaf_validator.py` | New HARD rule `leaf_template` |
| Modify `src/orchestrator/core/worker_bible.py` | Fixed context-pack priority order plus two new slots |
| Modify `src/orchestrator/core/orchestrator_dispatch.py` | Feed the two new Bible slots from the plan task |
| Modify `tests/fixtures/decompose/expected_leaf_graph.json` | Golden fixture carries the new fields |

### Phase B (P2)

| File | Responsibility |
|------|----------------|
| Modify `src/orchestrator/database.py` | `Migration(7, ...)`: triage and escalation columns on `tasks` |
| Modify `src/orchestrator/models/schemas.py` | `TriageDecision` |
| Create `src/orchestrator/core/leaf_triage.py` | Triage prompt build, strict parse, bound enforcement |
| Create `src/orchestrator/core/leaf_split.py` | Pure graph rewiring: parent to children, dependency remap |
| Create `src/orchestrator/core/escalation.py` | Ordered `implement_escalation` resolution, next-untried pair |
| Modify `src/orchestrator/core/llm_router.py` | `leaf_failure_triage` in `CALL_SITE_DEFAULTS` |
| Modify `src/orchestrator/core/roles.py` | `leaf_failure_triage` to role `plan` |
| Modify `src/orchestrator/core/task_queue.py` | `supersede_task`, `insert_split_children`, `set_task_implementer`, `record_triage_decision`, SUPERSEDED-aware queries |
| Modify `src/orchestrator/core/orchestrator_review.py` | Call triage on the second worker-attributable failure; act on the decision |
| Modify `src/orchestrator/core/orchestrator_dispatch.py` | Honour per-task implementer override at spawn |
| Modify `config/praxis.yaml` | `implement_escalation`, `max_leaves_per_plan` |

### Phase C (P3)

| File | Responsibility |
|------|----------------|
| Create `src/orchestrator/core/difficulty.py` | Feature extraction, `DifficultyScorer` protocol, `LogisticScorer` |
| Modify `src/orchestrator/core/capability_events.py` | `LeafDifficultyScoredEvent`, `LeafRejectedPredispatchEvent` |
| Modify `src/orchestrator/core/execute_plan_decompose.py` | Score after F3; reject or flag |
| Modify `src/orchestrator/core/effective_settings.py` | `difficulty_config()` accessor |
| Modify `config/praxis.yaml` | `difficulty_weights`, `difficulty_thresholds` |
| Modify `src/orchestrator/core/leaf_triage.py` | Fresh score in the triage input |

---

## Phase A: the standard, leaf types, and context ordering

### Task 1: Write the decomposition standard document

**Files:**
- Create: `docs/decomposition-standard.md`
- Test: `tests/test_decomposition_standard_doc.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

Create `tests/test_decomposition_standard_doc.py`:

```python
"""The decomposition standard doc is a contract, not prose.

The decompose prompt, the F3 validator, and the benchmark all cite it, so
its rule ids and leaf-type names must stay in lockstep with the code.
"""

from pathlib import Path

import pytest


DOC = Path(__file__).resolve().parents[1] / "docs" / "decomposition-standard.md"


@pytest.mark.unit
def test_standard_doc_exists():
    assert DOC.is_file(), "docs/decomposition-standard.md is missing"


@pytest.mark.unit
def test_standard_doc_cites_every_source():
    text = DOC.read_text(encoding="utf-8")
    for citation in (
        "2502.15964",   # MinionS
        "2605.14163",   # machine-checkable acceptance
        "2309.12499",   # CodePlan
        "2604.07789",   # ORACLE-SWE
        "2505.23419",   # SWE-bench Goes Live (numeric anchors)
        "2311.05772",   # ADaPT
        "2605.15425",   # runtime-structured decomposition
        "2511.09030",   # MAKER
        "2305.05176",   # FrugalGPT
    ):
        assert citation in text, f"standard doc is missing citation {citation}"


@pytest.mark.unit
def test_standard_doc_lists_every_leaf_type():
    from orchestrator.models.schemas import LeafType

    text = DOC.read_text(encoding="utf-8")
    for leaf_type in LeafType:
        assert leaf_type.value in text, (
            f"leaf type {leaf_type.value} is not documented in the standard"
        )


@pytest.mark.unit
def test_standard_doc_states_the_numeric_anchors_are_correlational():
    text = DOC.read_text(encoding="utf-8").lower()
    assert "correlational" in text
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_decomposition_standard_doc.py -v`
Expected: FAIL. `test_standard_doc_exists` fails on the missing file; `test_standard_doc_lists_every_leaf_type` fails with `ImportError: cannot import name 'LeafType'`.

- [ ] **Step 3: Write the document**

Create `docs/decomposition-standard.md`. Write exactly this content:

````markdown
# The Praxis Decomposition Standard

This document is the contract three things share: the decompose prompt in
`src/orchestrator/core/plan_review.py`, the deterministic leaf validator in
`src/orchestrator/core/leaf_validator.py`, and the benchmark in `bench/`. When a
rule changes here, all three change with it.

A **leaf** is one unit of work handed to one worker container in one dispatch. A
**plan** is a directed acyclic graph of leaves.

## 1. When a leaf is valid

A leaf is valid when all six rules hold.

| # | Rule | Source |
|---|------|--------|
| 1 | Its full context pack fits the worker's reliable context window with headroom (Praxis reserves 60 percent of the window for the worker's own reasoning and edits) | MinionS, arXiv 2502.15964 |
| 2 | Its instruction sequence is linear: no branching decision is left to the worker | MinionS; GPT-5-Nano regression finding |
| 3 | It has a machine-checkable acceptance signal: a test, a type-check, a build, or a subset of the project `verify_cmd` | arXiv 2605.14163; MAKER, arXiv 2511.09030 |
| 4 | It is scoped by dependency locality: the target location plus its direct callers and callees, not an arbitrary file count alone | CodePlan, arXiv 2309.12499 |
| 5 | Its context pack guarantees the edit location and a runnable acceptance check before any narrative context | ORACLE-SWE, arXiv 2604.07789 |
| 6 | Its `plan_text` is prescriptive and complete: the worker makes zero scoping judgments | Praxis verbatim-contract design, reinforced by arXiv 2603.14248 |

Rules 1, 3, 4, and 6 are enforced mechanically by `core/leaf_validator.py` (F3).
Rule 5 is enforced by the context-pack ordering in section 4. Rule 2 is enforced
by the leaf-type templates in section 3.

## 2. Numeric anchors

These come from SWE-bench Goes Live! (arXiv 2505.23419) and are
**correlational, not causal**. They set the direction of the default profile
limits in `config/praxis.yaml`, not a law.

| Gold-patch shape | Observed resolve rate for current agent and model combinations |
|------------------|-----------------------------------------------------------------|
| 1 file, under 5 lines | about 48 percent |
| 3 or more files, or 100 or more LOC | under 10 percent |
| 7 or more files | about 0 percent |

The current default profile (`capability.default` in `config/praxis.yaml`) is
`max_files_touched: 5`, `max_loc_delta: 300`, `max_checklist_items: 12`,
`max_dep_depth: 3`. These are hand-set. The Capability Calibration Loop will
replace them with learned per-(model, project) values derived from
`task_outcomes`; until it does, treat them as a starting prior.

## 3. Leaf types and their `plan_text` skeletons

Free-form decomposition invites free-form ambiguity. Fixed task-shape templates
outperform open-ended planning (Agentless, arXiv 2407.01489; CodeR, arXiv
2406.01304). Every leaf carries a `leaf_type`.

Every type requires these four sections in `plan_text`:

- `Goal`: one sentence, what is true when this leaf is done
- `Files`: the exact paths this leaf touches
- `Steps`: the ordered, non-branching instruction sequence
- `Acceptance`: the runnable check and its expected outcome

Types and their additional required sections:

| `leaf_type` | Additional required section | Use when |
|-------------|-----------------------------|----------|
| `bugfix_repro` | `Reproduction` (the command that fails before the fix) | Fixing observed wrong behavior |
| `function_add` | none | Adding a function or method inside an existing module |
| `endpoint_add` | none | Adding an API route or handler |
| `refactor_rename` | `Renames` (a table of old symbol to new symbol) | Moving or renaming symbols with no behavior change |
| `test_add` | none | Adding tests for code that already exists |
| `config_change` | none | Editing configuration, compose, or CI files |
| `doc_change` | none | Editing documentation only |
| `generic` | none | Nothing above fits |

`generic` is legal but carries a difficulty-score penalty: it signals the planner
could not shape the work, which is itself evidence the leaf is under-specified.

## 4. Context pack priority order

When the assembled context pack exceeds the worker's budget, sections are cut
from the bottom. This order is fixed and tested
(`tests/test_worker_bible_priority.py`).

1. The leaf `plan_text`, verbatim. Never trimmed. If it alone exceeds the
   budget the leaf is invalid and F3 or the difficulty gate must reject it.
2. Edit locations: file paths, symbol names, and the target regions themselves.
3. Acceptance: the verify command subset and its expected outcome.
4. Interface contracts of direct neighbors: signatures only, never bodies.
5. Working agreement and environment manifest.
6. Repo memory and narrative context. First to be cut.

Rationale: edit-location and runnable-test signals dominate the success
contribution; narrative contributes least (ORACLE-SWE, arXiv 2604.07789; Agent
Psychometrics feature ablation, arXiv 2604.00594).

Praxis adds two floors above this list that the literature does not cover: the
task `Goal` and the git-spine progress handover are never dropped. Dropping the
handover makes a re-dispatched worker redo completed work, which is a worse
failure than losing narrative.

## 5. Policy

Four policies follow from the standard. They are implemented in
`core/leaf_triage.py`, `core/leaf_split.py`, and `core/escalation.py`.

1. **Decompose adaptively.** The first decomposition is a hypothesis. Observed
   failure is the signal to split further (ADaPT, arXiv 2311.05772, plus 28 to 33
   percent over static plan-and-execute).
2. **Retry only the failed unit.** Never re-run a completed sibling because a
   later leaf failed. Static decomposition without this raises retry cost by 73
   percent versus a monolithic attempt (arXiv 2605.15425).
3. **Granularity scales inversely with worker capability**, and finer
   granularity must be paired with more verification, not less (MAKER, arXiv
   2511.09030).
4. **Escalate a leaf, not a plan.** After bounded worker-attributable failures
   the leaf moves to a stronger implementer (FrugalGPT cascade, arXiv 2305.05176).

## 6. Hard bounds

Adaptivity without bounds is an unbounded token spend. All of these are enforced
in code and covered by tests.

| Bound | Value | Enforced in |
|-------|-------|-------------|
| Triage brain calls per leaf | 1 for the leaf's whole lifetime | `tasks.triage_decision` presence check |
| Split generations | 1 (a split child may never split again) | `tasks.parent_task_id` presence check |
| Children per split | 2 to 4 | `core/leaf_triage.py` validation |
| Retry budget for split children | 2 attempts, not a fresh 3 | `core/leaf_split.py` |
| Leaves per plan | `max_leaves_per_plan`, default 24 | `core/leaf_triage.py` |
| Escalation attempts | the length of `implement_escalation` | `core/escalation.py` |
````

- [ ] **Step 4: Run the test to verify the doc tests pass**

Run: `uv run pytest tests/test_decomposition_standard_doc.py -v`
Expected: `test_standard_doc_exists`, `test_standard_doc_cites_every_source`, and `test_standard_doc_states_the_numeric_anchors_are_correlational` PASS. `test_standard_doc_lists_every_leaf_type` still FAILS with `ImportError` (Task 2 adds `LeafType`).

- [ ] **Step 5: Commit**

```bash
git add docs/decomposition-standard.md tests/test_decomposition_standard_doc.py
git commit -m "docs: add the decomposition standard as a cited contract

Codifies the six leaf-validity rules, the correlational numeric anchors,
the leaf-type skeletons, the context-pack priority order, the four
adaptive policies, and every hard bound. The decompose prompt, the F3
validator, and the benchmark all cite this file."
```

---

### Task 2: Add the `LeafType` enum and the new `LeafTask` fields

**Files:**
- Modify: `src/orchestrator/models/schemas.py:19-119`
- Modify: `tests/fixtures/decompose/expected_leaf_graph.json`
- Test: `tests/test_leaf_task.py`, `tests/test_decompose_golden.py`

**Depends on:** None

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_leaf_task.py`:

```python
@pytest.mark.unit
def test_leaf_type_enum_has_the_eight_standard_values():
    from orchestrator.models.schemas import LeafType

    assert {t.value for t in LeafType} == {
        "bugfix_repro",
        "function_add",
        "endpoint_add",
        "refactor_rename",
        "test_add",
        "config_change",
        "doc_change",
        "generic",
    }


@pytest.mark.unit
def test_leaf_task_defaults_leaf_type_to_generic():
    from orchestrator.models.schemas import LeafTask, LeafType

    leaf = LeafTask(id="t1", title="Add a helper")
    assert leaf.leaf_type is LeafType.GENERIC


@pytest.mark.unit
def test_leaf_task_accepts_a_declared_leaf_type():
    from orchestrator.models.schemas import LeafTask, LeafType

    leaf = LeafTask(id="t1", title="Fix the off-by-one", leaf_type="bugfix_repro")
    assert leaf.leaf_type is LeafType.BUGFIX_REPRO


@pytest.mark.unit
def test_leaf_task_rejects_an_unknown_leaf_type():
    import pytest as _pytest
    from pydantic import ValidationError

    from orchestrator.models.schemas import LeafTask

    with _pytest.raises(ValidationError):
        LeafTask(id="t1", title="x", leaf_type="not_a_real_type")


@pytest.mark.unit
def test_leaf_task_neighbor_contracts_defaults_to_none():
    from orchestrator.models.schemas import LeafTask

    assert LeafTask(id="t1", title="x").neighbor_contracts is None


@pytest.mark.unit
def test_leaf_schema_version_is_two():
    from orchestrator.models.schemas import LEAF_SCHEMA_VERSION

    assert LEAF_SCHEMA_VERSION == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_leaf_task.py -v -k "leaf_type or neighbor_contracts or schema_version"`
Expected: FAIL with `ImportError: cannot import name 'LeafType' from 'orchestrator.models.schemas'` and `assert 1 == 2`.

- [ ] **Step 3: Add the enum and the fields**

In `src/orchestrator/models/schemas.py`, add the enum immediately after the `CapabilityProfile` class (before `LEAF_SCHEMA_VERSION`):

```python
class LeafType(StrEnum):
    """Fixed task shapes a decomposed leaf may take.

    Free-form decomposition invites free-form ambiguity; fixed templates
    outperform open-ended planning (Agentless arXiv 2407.01489, CodeR
    arXiv 2406.01304).  Each type declares which ``plan_text`` sections are
    mandatory; see ``core/leaf_templates.REQUIRED_SECTIONS``.
    """

    BUGFIX_REPRO = "bugfix_repro"
    FUNCTION_ADD = "function_add"
    ENDPOINT_ADD = "endpoint_add"
    REFACTOR_RENAME = "refactor_rename"
    TEST_ADD = "test_add"
    CONFIG_CHANGE = "config_change"
    DOC_CHANGE = "doc_change"
    GENERIC = "generic"
```

Change the version constant:

```python
LEAF_SCHEMA_VERSION = 2
```

Add two fields to `LeafTask`, immediately after `verification: str | None = None`:

```python
    leaf_type: LeafType = LeafType.GENERIC
    neighbor_contracts: str | None = None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_leaf_task.py -v`
Expected: PASS.

- [ ] **Step 5: Repair the golden fixture**

Run: `uv run pytest tests/test_decompose_golden.py -v`
Expected: FAIL. `parse_review_response` now emits `leaf_type`, `neighbor_contracts`, and `schema_version: 2` in each dumped leaf, which the frozen fixture lacks.

Regenerate the fixture rather than hand-editing it:

```bash
uv run python - <<'PY'
import json
from pathlib import Path
from orchestrator.core.plan_review import parse_review_response

fixtures = Path("tests/fixtures/decompose")
raw = (fixtures / "sample_plan_response.json").read_text(encoding="utf-8")
out = parse_review_response(raw)
(fixtures / "expected_leaf_graph.json").write_text(
    json.dumps(out, indent=2) + "\n", encoding="utf-8"
)
PY
```

Then read the regenerated file and confirm by eye that every leaf now carries
`"schema_version": 2`, `"leaf_type": "generic"`, and `"neighbor_contracts": null`,
and that nothing else changed.

- [ ] **Step 6: Run the golden test to verify it passes**

Run: `uv run pytest tests/test_decompose_golden.py -v`
Expected: PASS.

- [ ] **Step 7: Mutation-check the type validation**

Temporarily change `leaf_type: LeafType = LeafType.GENERIC` to `leaf_type: str = "generic"`.
Run: `uv run pytest tests/test_leaf_task.py::test_leaf_task_rejects_an_unknown_leaf_type -v`
Expected: FAIL (a plain `str` accepts `"not_a_real_type"`). Restore the enum annotation and re-run to confirm PASS. If the test passed with the mutation in place it is vacuous; fix the test before continuing.

- [ ] **Step 8: Run the full suite**

Run: `uv run pytest -q`
Expected: all tests pass. If `tests/test_execute_plan_decompose.py` or `tests/test_leaf_validator.py` assert on exact leaf dicts, update those expectations to include the two new keys.

- [ ] **Step 9: Commit**

```bash
git add src/orchestrator/models/schemas.py tests/test_leaf_task.py tests/fixtures/decompose/expected_leaf_graph.json tests/test_decomposition_standard_doc.py
git commit -m "feat(schemas): add LeafType and the leaf_type/neighbor_contracts fields

Bumps LEAF_SCHEMA_VERSION to 2 and regenerates the decompose golden
fixture, which asserts the exact model_dump shape."
```

---

### Task 3: Add `core/leaf_templates.py` as the single source of per-type sections

**Files:**
- Create: `src/orchestrator/core/leaf_templates.py`
- Test: `tests/test_leaf_templates.py`

**Depends on:** Task 2

- [ ] **Step 1: Write the failing test**

Create `tests/test_leaf_templates.py`:

```python
"""The per-type section table is read by both the prompt and the validator.

If these drift, the brain is asked for one shape and graded on another.
"""

import pytest

from orchestrator.core.leaf_templates import (
    BASE_SECTIONS,
    REQUIRED_SECTIONS,
    missing_sections,
    render_template_block,
)
from orchestrator.models.schemas import LeafType


@pytest.mark.unit
def test_every_leaf_type_has_a_section_tuple():
    assert set(REQUIRED_SECTIONS) == set(LeafType)


@pytest.mark.unit
def test_every_type_requires_the_four_base_sections():
    for leaf_type, sections in REQUIRED_SECTIONS.items():
        assert set(BASE_SECTIONS).issubset(set(sections)), leaf_type


@pytest.mark.unit
def test_bugfix_repro_additionally_requires_reproduction():
    assert "Reproduction" in REQUIRED_SECTIONS[LeafType.BUGFIX_REPRO]


@pytest.mark.unit
def test_refactor_rename_additionally_requires_renames():
    assert "Renames" in REQUIRED_SECTIONS[LeafType.REFACTOR_RENAME]


@pytest.mark.unit
def test_generic_requires_only_the_base_sections():
    assert REQUIRED_SECTIONS[LeafType.GENERIC] == BASE_SECTIONS


@pytest.mark.unit
def test_missing_sections_accepts_markdown_headings():
    text = "## Goal\nx\n## Files\nsrc/a.py\n## Steps\n1. do it\n## Acceptance\n`pytest`"
    assert missing_sections(text, LeafType.FUNCTION_ADD) == []


@pytest.mark.unit
def test_missing_sections_accepts_bold_labels():
    text = "**Goal:** x\n**Files:** src/a.py\n**Steps:** 1. do\n**Acceptance:** `pytest`"
    assert missing_sections(text, LeafType.FUNCTION_ADD) == []


@pytest.mark.unit
def test_missing_sections_accepts_plain_colon_labels():
    text = "Goal: x\nFiles: src/a.py\nSteps: do it\nAcceptance: run `pytest`"
    assert missing_sections(text, LeafType.FUNCTION_ADD) == []


@pytest.mark.unit
def test_missing_sections_is_case_insensitive():
    text = "goal: x\nFILES: src/a.py\nsteps: do it\nacceptance: `pytest`"
    assert missing_sections(text, LeafType.FUNCTION_ADD) == []


@pytest.mark.unit
def test_missing_sections_reports_every_absent_section_in_order():
    text = "Goal: ship it"
    assert missing_sections(text, LeafType.FUNCTION_ADD) == [
        "Files",
        "Steps",
        "Acceptance",
    ]


@pytest.mark.unit
def test_missing_sections_reports_the_type_specific_extra():
    text = "Goal: x\nFiles: a.py\nSteps: do\nAcceptance: `pytest`"
    assert missing_sections(text, LeafType.BUGFIX_REPRO) == ["Reproduction"]


@pytest.mark.unit
def test_missing_sections_does_not_match_a_word_inside_prose():
    # "the goal of this" must not satisfy the Goal section requirement.
    text = "This describes the goal of this change and nothing else."
    assert "Goal" in missing_sections(text, LeafType.GENERIC)


@pytest.mark.unit
def test_render_template_block_names_every_type_and_its_extras():
    block = render_template_block()
    for leaf_type in LeafType:
        assert leaf_type.value in block
    assert "Reproduction" in block
    assert "Renames" in block
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_leaf_templates.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.core.leaf_templates'`.

- [ ] **Step 3: Write the module**

Create `src/orchestrator/core/leaf_templates.py`:

```python
"""Per-leaf-type ``plan_text`` section requirements.

Single source of truth read by two consumers that must never drift: the
decompose prompt in ``core/plan_review.py`` (which asks the brain for these
sections) and the F3 validator in ``core/leaf_validator.py`` (which grades the
brain's answer).  See ``docs/decomposition-standard.md`` section 3.
"""

from __future__ import annotations

import re

from orchestrator.models.schemas import LeafType


# Sections every leaf type must carry, in the order they should appear.
BASE_SECTIONS: tuple[str, ...] = ("Goal", "Files", "Steps", "Acceptance")

# Additional sections a specific type must carry, appended after the base four.
_EXTRA_SECTIONS: dict[LeafType, tuple[str, ...]] = {
    LeafType.BUGFIX_REPRO: ("Reproduction",),
    LeafType.REFACTOR_RENAME: ("Renames",),
}

REQUIRED_SECTIONS: dict[LeafType, tuple[str, ...]] = {
    leaf_type: BASE_SECTIONS + _EXTRA_SECTIONS.get(leaf_type, ())
    for leaf_type in LeafType
}

# What each type is for, rendered into the prompt so the brain can choose.
_TYPE_PURPOSE: dict[LeafType, str] = {
    LeafType.BUGFIX_REPRO: "fixing observed wrong behavior",
    LeafType.FUNCTION_ADD: "adding a function or method inside an existing module",
    LeafType.ENDPOINT_ADD: "adding an API route or handler",
    LeafType.REFACTOR_RENAME: "moving or renaming symbols with no behavior change",
    LeafType.TEST_ADD: "adding tests for code that already exists",
    LeafType.CONFIG_CHANGE: "editing configuration, compose, or CI files",
    LeafType.DOC_CHANGE: "editing documentation only",
    LeafType.GENERIC: "nothing above fits (scored with a difficulty penalty)",
}


def _section_pattern(section: str) -> re.Pattern[str]:
    """Match a section label at the start of a line, in the three legal forms.

    Legal forms are a markdown heading (``## Goal``), a bold label
    (``**Goal:**``), and a plain colon label (``Goal:``).  Anchoring to the
    start of a line is what stops the word appearing inside prose from
    satisfying the requirement.
    """
    name = re.escape(section)
    return re.compile(
        rf"^\s*(?:\#{{1,6}}\s*{name}\b|\*\*{name}\*\*\s*:?|{name}\s*:)",
        re.IGNORECASE | re.MULTILINE,
    )


def missing_sections(plan_text: str, leaf_type: LeafType) -> list[str]:
    """Return the required sections absent from ``plan_text``, in order.

    Args:
        plan_text: The leaf's verbatim contract text.
        leaf_type: The declared shape of the leaf.

    Returns:
        Section names that are required for this type but not present.  Empty
        when the leaf satisfies its template.
    """
    text = plan_text or ""
    return [
        section
        for section in REQUIRED_SECTIONS[leaf_type]
        if not _section_pattern(section).search(text)
    ]


def render_template_block() -> str:
    """Render the leaf-type table for injection into the decompose prompt."""
    lines = [
        "LEAF TYPES (pick exactly one per leaf, set it as \"leaf_type\"):",
    ]
    for leaf_type in LeafType:
        extras = _EXTRA_SECTIONS.get(leaf_type, ())
        extra_text = (
            f"; also requires: {', '.join(extras)}" if extras else ""
        )
        lines.append(
            f'- "{leaf_type.value}": {_TYPE_PURPOSE[leaf_type]}{extra_text}'
        )
    lines.append("")
    lines.append(
        "Every leaf's \"plan_text\" MUST contain these sections as line-leading "
        f"labels: {', '.join(BASE_SECTIONS)}. A leaf whose plan_text is missing "
        "a required section is rejected automatically and re-asked."
    )
    return "\n".join(lines)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_leaf_templates.py -v`
Expected: PASS (13 tests).

- [ ] **Step 5: Mutation-check the line anchoring**

Temporarily remove `^\s*` from the start of the pattern in `_section_pattern` (leaving `rf"(?:\#{{1,6}}..."`).
Run: `uv run pytest tests/test_leaf_templates.py::test_missing_sections_does_not_match_a_word_inside_prose -v`
Expected: FAIL (unanchored, "the goal of this" matches). Restore the anchor and re-run to confirm PASS.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/core/leaf_templates.py tests/test_leaf_templates.py
git commit -m "feat(leaf-templates): add the per-type plan_text section table

One source read by both the decompose prompt and the F3 validator, so the
shape the brain is asked for and the shape it is graded on cannot drift."
```

---

### Task 4: Inject the leaf-type table into the decompose prompt

**Files:**
- Modify: `src/orchestrator/core/plan_review.py:25-134`
- Test: `tests/test_plan_review.py` (create if absent)

**Depends on:** Task 3

- [ ] **Step 1: Write the failing test**

Create or append to `tests/test_plan_review.py`:

```python
import pytest

from orchestrator.core.plan_review import build_review_prompt
from orchestrator.models.schemas import CapabilityProfile, LeafType


def _profile() -> CapabilityProfile:
    return CapabilityProfile(
        model_name="test-model",
        parameter_count_b=30,
        context_window=8192,
    )


@pytest.mark.unit
def test_prompt_lists_every_leaf_type():
    prompt = build_review_prompt("plan body", _profile(), "(no history)", 3276)
    for leaf_type in LeafType:
        assert leaf_type.value in prompt


@pytest.mark.unit
def test_prompt_demands_the_base_sections():
    prompt = build_review_prompt("plan body", _profile(), "(no history)", 3276)
    for section in ("Goal", "Files", "Steps", "Acceptance"):
        assert section in prompt


@pytest.mark.unit
def test_prompt_json_example_carries_a_leaf_type_key():
    prompt = build_review_prompt("plan body", _profile(), "(no history)", 3276)
    assert '"leaf_type"' in prompt


@pytest.mark.unit
def test_prompt_still_carries_the_hard_constraint_numbers():
    profile = _profile()
    prompt = build_review_prompt("plan body", profile, "(no history)", 3276)
    assert str(profile.max_files_touched) in prompt
    assert str(profile.max_loc_delta) in prompt
    assert str(profile.max_dep_depth) in prompt
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_plan_review.py -v`
Expected: FAIL. `test_prompt_lists_every_leaf_type` fails on `bugfix_repro` not being in the prompt.

- [ ] **Step 3: Modify the prompt**

In `src/orchestrator/core/plan_review.py`, add the import at the top of the module (after the existing `from orchestrator.models.schemas import ...` line):

```python
from orchestrator.core.leaf_templates import render_template_block
```

In `_PROMPT`, insert a `{leaf_type_block}` placeholder immediately after the
`HARD CONSTRAINTS` block and before `Plan to decompose:`. The constraint block
ends with the `Every leaf MUST include a "verification" string >40 characters`
bullet; add these two lines after it:

```
{leaf_type_block}

```

In the `For every leaf you MUST also include:` list, add one bullet after the
`"verification"` bullet:

```
- "leaf_type": one of the leaf types listed above.
```

In the JSON example object, add `"leaf_type": "function_add",` immediately after
the `"task_type": "feature",` line.

In `build_review_prompt`, add the new format argument:

```python
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
        leaf_type_block=render_template_block(),
        plan_text=plan_text,
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_plan_review.py tests/test_leaf_templates.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/plan_review.py tests/test_plan_review.py
git commit -m "feat(decompose): require a leaf_type and its plan_text skeleton

The prompt now renders the leaf-type table from core/leaf_templates so the
brain is asked for exactly the sections F3 will grade."
```

---

### Task 5: Enforce the template as a HARD F3 rule

**Files:**
- Modify: `src/orchestrator/core/leaf_validator.py:441-479`
- Test: `tests/test_leaf_validator.py`

**Depends on:** Task 3, Task 4

- [ ] **Step 1: Write the failing test**

Append to `tests/test_leaf_validator.py`:

```python
@pytest.mark.unit
def test_leaf_template_rule_passes_a_complete_generic_leaf():
    from orchestrator.core.leaf_validator import validate_leaves
    from orchestrator.models.schemas import CapabilityProfile, LeafTask

    leaf = LeafTask(
        id="t1",
        title="Add helper",
        plan_text=(
            "## Goal\nAdd a helper.\n"
            "## Files\nsrc/a.py\n"
            "## Steps\n1. Write it.\n"
            "## Acceptance\nRun `uv run pytest tests/test_a.py`"
        ),
        verification="Run `uv run pytest tests/test_a.py` and confirm it passes",
        leaf_type="generic",
    )
    result = validate_leaves({}, CapabilityProfile(
        model_name="m", parameter_count_b=30, context_window=8192
    ), leaf.plan_text, [leaf])
    assert [v for v in result.hard if v.rule == "leaf_template"] == []


@pytest.mark.unit
def test_leaf_template_rule_hard_rejects_a_missing_section():
    from orchestrator.core.leaf_validator import validate_leaves
    from orchestrator.models.schemas import CapabilityProfile, LeafTask

    leaf = LeafTask(
        id="t1",
        title="Add helper",
        plan_text="## Goal\nAdd a helper.\n## Files\nsrc/a.py",
        verification="Run `uv run pytest tests/test_a.py` and confirm it passes",
        leaf_type="generic",
    )
    result = validate_leaves({}, CapabilityProfile(
        model_name="m", parameter_count_b=30, context_window=8192
    ), leaf.plan_text, [leaf])
    violations = [v for v in result.hard if v.rule == "leaf_template"]
    assert len(violations) == 1
    assert "Steps" in violations[0].message
    assert "Acceptance" in violations[0].message


@pytest.mark.unit
def test_leaf_template_rule_enforces_the_type_specific_section():
    from orchestrator.core.leaf_validator import validate_leaves
    from orchestrator.models.schemas import CapabilityProfile, LeafTask

    leaf = LeafTask(
        id="t1",
        title="Fix the crash",
        plan_text=(
            "## Goal\nStop the crash.\n"
            "## Files\nsrc/a.py\n"
            "## Steps\n1. Guard the None.\n"
            "## Acceptance\nRun `uv run pytest tests/test_a.py`"
        ),
        verification="Run `uv run pytest tests/test_a.py` and confirm it passes",
        leaf_type="bugfix_repro",
    )
    result = validate_leaves({}, CapabilityProfile(
        model_name="m", parameter_count_b=30, context_window=8192
    ), leaf.plan_text, [leaf])
    violations = [v for v in result.hard if v.rule == "leaf_template"]
    assert len(violations) == 1
    assert "Reproduction" in violations[0].message


@pytest.mark.unit
def test_leaf_template_violation_is_hard_not_soft():
    from orchestrator.core.leaf_validator import validate_leaves
    from orchestrator.models.schemas import CapabilityProfile, LeafTask

    leaf = LeafTask(
        id="t1",
        title="Add helper",
        plan_text="Goal: do a thing",
        verification="Run `uv run pytest` and confirm it passes cleanly",
        leaf_type="generic",
    )
    result = validate_leaves({}, CapabilityProfile(
        model_name="m", parameter_count_b=30, context_window=8192
    ), leaf.plan_text, [leaf])
    assert any(v.rule == "leaf_template" for v in result.hard)
    assert not any(v.rule == "leaf_template" for v in result.soft)
    assert result.dispatchable is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_leaf_validator.py -v -k leaf_template`
Expected: FAIL. All four fail because no `leaf_template` rule exists, so the violation lists are empty.

- [ ] **Step 3: Implement the rule**

In `src/orchestrator/core/leaf_validator.py`, add the import after the existing schema import:

```python
from orchestrator.core.leaf_templates import missing_sections
```

Add the rule function immediately after `_check_escalate_mismatch`:

```python
def _check_leaf_template(
    leaves: list[LeafTask],
    result: ValidationResult,
) -> None:
    """HARD: every leaf's plan_text carries its type's required sections.

    Rule 2 of the standard (no branching decision left to the worker) is
    enforced structurally: a leaf that does not state its Goal, Files, Steps,
    and Acceptance has left scoping judgment to the worker by omission.
    """
    for leaf in leaves:
        absent = missing_sections(leaf.plan_text, leaf.leaf_type)
        if absent:
            result.add(
                Violation(
                    rule="leaf_template",
                    task_id=leaf.id,
                    message=(
                        f"leaf_type '{leaf.leaf_type.value}' requires plan_text "
                        f"sections that are missing: {', '.join(absent)}"
                    ),
                )
            )
```

Register it in `validate_leaves`, in the HARD block after `_check_escalate_mismatch`:

```python
    _check_escalate_mismatch(leaves, profile, result)
    _check_leaf_template(leaves, result)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_leaf_validator.py -v`
Expected: PASS. Existing validator tests that build leaves with bare `plan_text` will now also emit a `leaf_template` violation; those tests assert on specific rules (`v.rule == "..."`) so they should be unaffected. If any test asserts `result.clean` or `result.dispatchable` on a leaf with unstructured `plan_text`, update that fixture's `plan_text` to carry the four base sections rather than weakening the rule.

- [ ] **Step 5: Mutation-check the severity**

Temporarily add `severity="soft"` to the `Violation(...)` in `_check_leaf_template`.
Run: `uv run pytest tests/test_leaf_validator.py::test_leaf_template_violation_is_hard_not_soft -v`
Expected: FAIL. Restore and re-run to confirm PASS.

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest -q`
Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator/core/leaf_validator.py tests/test_leaf_validator.py
git commit -m "feat(f3): hard-reject a leaf whose plan_text misses its template sections

Deterministic, fail-closed, shares the existing informed re-ask rounds."
```

---

### Task 6: Fix the context-pack priority order

**Files:**
- Modify: `src/orchestrator/core/worker_bible.py:31-87`
- Modify: `src/orchestrator/core/orchestrator_dispatch.py:267-319`
- Test: `tests/test_worker_bible_priority.py`

**Depends on:** Task 1

- [ ] **Step 1: Write the failing test**

Create `tests/test_worker_bible_priority.py`:

```python
"""The context pack is cut bottom-up, and the top three ranks are never cut.

Edit-location and runnable-acceptance signals dominate success contribution;
narrative contributes least (ORACLE-SWE arXiv 2604.07789).  See
docs/decomposition-standard.md section 4.
"""

import pytest

from orchestrator.core.worker_bible import BibleSources, build_bible


def _sources(context_window: int, filler: int = 200) -> BibleSources:
    """Bible sources whose droppable sections are individually large."""
    blob = "x" * filler
    return BibleSources(
        goal="Ship the widget.",
        handover="PROGRESS: nothing done yet.",
        context_window=context_window,
        plan_slice="## Goal\nShip it.\n## Files\nsrc/a.py\n## Steps\n1. go\n## Acceptance\n`pytest`",
        edit_locations="src/a.py::make_widget\nsrc/b.py::WidgetView",
        acceptance="Run `uv run pytest tests/test_widget.py`; expect 3 passed.",
        neighbor_contracts=f"def make_widget(name: str) -> Widget: ...\n{blob}",
        caller_context=f"The user asked for a widget.\n{blob}",
        repo_memory=f"This repo pins chromadb 0.5.0.\n{blob}",
        verify_cmd="uv run pytest",
    )


@pytest.mark.unit
def test_a_roomy_budget_keeps_every_section():
    bible = build_bible(_sources(context_window=200_000))
    assert "src/a.py::make_widget" in bible
    assert "def make_widget" in bible
    assert "The user asked for a widget." in bible
    assert "chromadb 0.5.0" in bible


@pytest.mark.unit
def test_repo_memory_is_cut_before_narrative_context():
    bible = build_bible(_sources(context_window=1400))
    assert "chromadb 0.5.0" not in bible
    assert "The user asked for a widget." in bible


@pytest.mark.unit
def test_narrative_context_is_cut_before_neighbor_contracts():
    bible = build_bible(_sources(context_window=1150))
    assert "The user asked for a widget." not in bible
    assert "def make_widget" in bible


@pytest.mark.unit
def test_plan_text_edit_locations_and_acceptance_survive_the_tightest_budget():
    bible = build_bible(_sources(context_window=900))
    # Rank 1: the leaf contract, verbatim.
    assert "## Steps" in bible
    # Rank 2: edit locations.
    assert "src/a.py::make_widget" in bible
    # Rank 3: the runnable acceptance check.
    assert "uv run pytest tests/test_widget.py" in bible
    # Ranks 4 to 6 are gone.
    assert "chromadb 0.5.0" not in bible
    assert "The user asked for a widget." not in bible


@pytest.mark.unit
def test_the_progress_handover_is_a_floor_section():
    bible = build_bible(_sources(context_window=900))
    assert "PROGRESS: nothing done yet." in bible


@pytest.mark.unit
def test_a_plan_text_that_alone_blows_the_budget_raises():
    from orchestrator.core.token_budget import ContextBudgetExceeded

    src = _sources(context_window=512)
    src.plan_slice = "y" * 400_000
    with pytest.raises(ContextBudgetExceeded):
        build_bible(src)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_worker_bible_priority.py -v`
Expected: FAIL with `TypeError: BibleSources.__init__() got an unexpected keyword argument 'edit_locations'`.

- [ ] **Step 3: Reorder and extend the Bible**

Replace the body of `src/orchestrator/core/worker_bible.py` from the
`@dataclass` line to the end of `build_bible` with:

```python
@dataclass
class BibleSources:
    """Raw inputs for the Bible.

    Field order mirrors the context-pack priority in
    ``docs/decomposition-standard.md`` section 4.  When the pack exceeds the
    worker's budget, sections are dropped from the bottom of that order; the
    leaf contract, its edit locations, and its acceptance check are floors and
    are never dropped.
    """

    goal: str
    handover: str
    context_window: int
    plan_slice: str | None = None
    edit_locations: str | None = None
    acceptance: str | None = None
    neighbor_contracts: str | None = None
    caller_context: str | None = None
    repo_memory: str | None = None
    review_feedback: str | None = None
    verify_cmd: str | None = None
    reserve_fraction: float = WORKER_RESERVE_FRACTION


# Priority ranks. Lower is kept longer; ``floor`` sections are never dropped.
# Ranks 1 to 3 of the standard (plan_text, edit locations, acceptance) are
# floors by construction. ``goal`` and ``handover`` are Praxis-specific floors:
# dropping the handover makes a re-dispatched worker redo completed work, which
# is a worse failure than losing narrative.
_P_GOAL = 0
_P_PLAN = 1
_P_EDITS = 2
_P_ACCEPT = 3
_P_FEEDBACK = 4
_P_HANDOVER = 5
_P_NEIGHBORS = 6
_P_AGREEMENT = 7
_P_CALLER = 8
_P_REPO = 9


def build_bible(src: BibleSources) -> str:
    """Return the assembled, scrubbed, budget-trimmed Bible markdown.

    Raises:
        ContextBudgetExceeded: If the floor sections alone exceed the budget.
            A leaf whose ``plan_slice`` alone overflows is invalid; F3 and the
            pre-dispatch difficulty gate exist to catch it earlier.
    """
    raw_sections: list[Section] = [
        Section("goal", f"# GOAL (do not lose this)\n{src.goal}", _P_GOAL, floor=True),
    ]
    if src.plan_slice:
        raw_sections.append(
            Section(
                "plan",
                f"# LEAF CONTRACT (verbatim, do not reinterpret)\n{src.plan_slice}",
                _P_PLAN,
                floor=True,
            )
        )
    if src.edit_locations:
        raw_sections.append(
            Section(
                "edits",
                f"# EDIT LOCATIONS\n{src.edit_locations}",
                _P_EDITS,
                floor=True,
            )
        )
    acceptance = src.acceptance or src.verify_cmd
    if acceptance:
        raw_sections.append(
            Section(
                "acceptance",
                "# ACCEPTANCE (run this before you finish)\n" f"{acceptance}",
                _P_ACCEPT,
                floor=True,
            )
        )
    if src.review_feedback:
        raw_sections.append(
            Section(
                "feedback",
                "# PREVIOUS ATTEMPT FEEDBACK (fix these before anything else)\n"
                f"{src.review_feedback}",
                _P_FEEDBACK,
                floor=True,
            )
        )
    raw_sections.append(Section("handover", src.handover, _P_HANDOVER, floor=True))
    if src.neighbor_contracts:
        raw_sections.append(
            Section(
                "neighbors",
                f"# NEIGHBOR INTERFACES (signatures only)\n{src.neighbor_contracts}",
                _P_NEIGHBORS,
            )
        )
    raw_sections.append(Section("agreement", _WORKING_AGREEMENT, _P_AGREEMENT))
    if src.caller_context:
        raw_sections.append(
            Section("caller", f"# CONTEXT\n{src.caller_context}", _P_CALLER)
        )
    if src.repo_memory:
        raw_sections.append(
            Section("repo", f"# REPO MEMORY\n{src.repo_memory}", _P_REPO)
        )

    for s in raw_sections:
        s.text = scrub_context(s.text) or s.text

    kept = fit_sections(raw_sections, src.context_window, src.reserve_fraction)
    return "\n\n".join(s.text for s in kept)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_worker_bible_priority.py -v`
Expected: PASS (6 tests). If a boundary test lands on the wrong side of a cut, adjust only the `context_window` numbers in `_sources(...)` calls in the test, never the priority constants.

- [ ] **Step 5: Feed the new slots from dispatch**

In `src/orchestrator/core/orchestrator_dispatch.py`, replace the `build_bible(BibleSources(...))` call at the end of `_build_worker_bible` with:

```python
        files = plan_task.get("files") or []
        edit_locations = "\n".join(str(f) for f in files) or None

        return build_bible(
            BibleSources(
                goal=goal,
                handover=handover,
                context_window=context_window,
                plan_slice=plan_task.get("plan_text"),
                # Rank 2 of the standard: where to edit, before any narrative.
                edit_locations=edit_locations,
                # Rank 3: the leaf's own acceptance check, falling back to the
                # project-wide verify command when the leaf declares none.
                acceptance=plan_task.get("verification") or project.get("verify_cmd"),
                # Rank 4: signatures of direct neighbors, optional.
                neighbor_contracts=plan_task.get("neighbor_contracts"),
                caller_context=plan_task.get("context_text"),
                # Client-gathered manifest of NON-committed context (gitignored
                # config shapes, user-scope conventions). Committed repo files
                # are still folded in separately by the entrypoint --read.
                repo_memory=plan_task.get("repo_memory"),
                review_feedback=task.get("review_feedback"),
                verify_cmd=project.get("verify_cmd"),
            )
        )
```

- [ ] **Step 6: Run the dispatch tests**

Run: `uv run pytest tests/test_orchestrator.py -v -k bible`
Expected: PASS. Existing assertions on Bible content still hold because every prior slot is still emitted; only the section headings for the plan slice changed (`# PLAN` became `# LEAF CONTRACT (verbatim, do not reinterpret)`). Update any test asserting the literal string `"# PLAN"` to the new heading.

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest -q`
Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add src/orchestrator/core/worker_bible.py src/orchestrator/core/orchestrator_dispatch.py tests/test_worker_bible_priority.py
git commit -m "feat(bible): fix the context-pack priority order and add two slots

Edit locations and the acceptance check are now floors ranked above the
progress handover, narrative context, and repo memory, which are cut first.
Matches docs/decomposition-standard.md section 4."
```

---

### Task 7: Close out Phase A

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Modify: `docs/gotchas.md`

**Depends on:** Task 1, Task 2, Task 3, Task 4, Task 5, Task 6

- [ ] **Step 1: Link the standard from the README**

In `README.md`, in the documentation links section, add:

```markdown
- **The decomposition standard:** [`docs/decomposition-standard.md`](docs/decomposition-standard.md): the cited rules that decide when a task is small enough for the model implementing it
```

- [ ] **Step 2: Add the gotchas**

Append to `docs/gotchas.md`:

```markdown
- **Leaf templates are enforced, not suggested**: `core/leaf_templates.py` is the
  single source of the per-`LeafType` `plan_text` section requirements, read by
  BOTH the decompose prompt (`core/plan_review.build_review_prompt`) and the F3
  validator (`core/leaf_validator._check_leaf_template`, a HARD rule). Adding a
  `LeafType` value without adding its entry to `REQUIRED_SECTIONS` raises a
  `KeyError` in `missing_sections`; the golden test
  `test_every_leaf_type_has_a_section_tuple` catches that at test time. Section
  matching is line-anchored on purpose: without `^`, the word "goal" appearing
  inside prose satisfies the Goal requirement and the rule becomes vacuous.
- **The context pack is cut bottom-up and ranks 1 to 3 are floors** 
  `core/worker_bible.build_bible` orders sections by the fixed ranks in
  `docs/decomposition-standard.md` section 4: leaf contract, edit locations,
  acceptance check, previous-attempt feedback, and progress handover are all
  `floor=True`; neighbor contracts, the working agreement, caller narrative, and
  repo memory are dropped in that order when the budget is tight. Do not
  "simplify" a floor back to a plain priority: a worker that loses its edit
  locations or its acceptance check has been handed a scoping judgment, which is
  exactly what the standard forbids.
- **`LEAF_SCHEMA_VERSION` is 2 and the golden fixture asserts `model_dump()`
  byte-for-byte**: any new `LeafTask` field, even one with a default, changes
  `parse_review_response` output and breaks
  `tests/fixtures/decompose/expected_leaf_graph.json`. Regenerate the fixture in
  the same commit rather than loosening the golden test.
```

- [ ] **Step 3: Add the CLAUDE.md index lines**

In `CLAUDE.md`, in the Gotchas index list, add three one-line entries mirroring
the three gotchas above (one line each, matching the existing terse style), and
add to the Documentation section:

```markdown
- **Decomposition standard (cited contract):** `docs/decomposition-standard.md`
```

- [ ] **Step 4: Verify the whole gate**

Run these four commands in order; every one must be clean before committing:

```bash
uv run ruff format src/ tests/
uv run ruff check --fix src/ tests/
uv run mypy src/ --ignore-missing-imports
uv run pytest --cov=orchestrator --cov-fail-under=80 -q
```

Expected: ruff reports no remaining issues, mypy reports `Success`, pytest passes with coverage at or above 80 percent.

- [ ] **Step 5: Commit**

```bash
git add README.md CLAUDE.md docs/gotchas.md
git commit -m "docs: index the decomposition standard and its two new gotchas"
```

**Phase A is complete.** Per the cross-plan execution order, the next work is
`2026-08-06-usable-praxis-product.md` Phase A, then `2026-08-06-praxis-bench.md`
Phase A, before returning here for Phase B.

---

## Phase B: adaptive split-on-failure and escalation

Phase B is written as its own task series below. It depends on Phase A being
merged, and on the product plan's Phase A only for convenience (`praxis doctor`
makes the dogfood loop faster), not for correctness.

### Task 8: Migration 7, the triage and escalation columns

**Files:**
- Modify: `src/orchestrator/database.py:212-258`
- Test: `tests/test_migrations.py`

**Depends on:** Task 7

- [ ] **Step 1: Write the failing test**

Append to `tests/test_migrations.py`:

```python
@pytest.mark.unit
async def test_migration_7_adds_the_triage_columns(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'm7.db'}")
    await db.connect()
    await db.initialize()
    rows = await db.fetch_all("PRAGMA table_info(tasks)")
    cols = {row["name"] for row in rows}
    for column in (
        "parent_task_id",
        "difficulty_score",
        "leaf_type",
        "triage_decision",
        "escalation_index",
        "implement_harness",
        "implement_model",
    ):
        assert column in cols, f"tasks.{column} missing after migration 7"
    await db.close()


@pytest.mark.unit
async def test_migration_7_is_idempotent(tmp_path):
    path = tmp_path / "m7-idem.db"
    for _ in range(2):
        db = Database(f"sqlite+aiosqlite:///{path}")
        await db.connect()
        await db.initialize()
        await db.close()
    db = Database(f"sqlite+aiosqlite:///{path}")
    await db.connect()
    row = await db.fetch_one("PRAGMA user_version")
    assert row is not None
    await db.close()


@pytest.mark.unit
async def test_escalation_index_defaults_to_zero(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'm7-def.db'}")
    await db.connect()
    await db.initialize()
    await db.execute(
        "INSERT INTO plans (id, project_id, source, status) "
        "VALUES ('p1', 'proj1', 'test', 'active')"
    )
    await db.execute(
        "INSERT INTO tasks (id, plan_id, title, description, branch_name) "
        "VALUES ('t1', 'p1', 'T', 'D', 'agent/t')"
    )
    row = await db.fetch_one("SELECT escalation_index FROM tasks WHERE id = 't1'")
    assert row is not None
    assert row["escalation_index"] == 0
    await db.close()


@pytest.mark.unit
def test_current_schema_version_is_seven():
    from orchestrator.database import CURRENT_SCHEMA_VERSION

    assert CURRENT_SCHEMA_VERSION == 7
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_migrations.py -v`
Expected: FAIL. `tasks.parent_task_id missing after migration 7` and `assert 6 == 7`.

- [ ] **Step 3: Write the migration**

In `src/orchestrator/database.py`, add the migration function immediately after
`_migration_0006_worker_session`:

```python
async def _migration_0007_leaf_triage(connection: aiosqlite.Connection) -> None:
    """Add the columns adaptive split-on-failure and escalation need.

    The umbrella spec's DDL names three columns (``parent_task_id``,
    ``difficulty_score``, ``leaf_type``).  Four more are required to enforce the
    spec's own hard bounds durably rather than in memory:

    - ``triage_decision``: presence enforces "one triage call per leaf lifetime".
    - ``escalation_index``: how many ``implement_escalation`` entries this leaf
      has already burned, so escalation stops at the list length.
    - ``implement_harness`` / ``implement_model``: the model that ACTUALLY
      implemented this attempt.  Outcome attribution must never credit the
      original worker with an escalated success, or the calibration loop learns
      lies.

    ``parent_task_id`` doubles as the one-split-generation guard: a task with a
    parent may never be split again.
    """
    cursor = await connection.execute("PRAGMA table_info(tasks)")
    cols = {row[1] for row in await cursor.fetchall()}
    additions = (
        ("parent_task_id", "TEXT"),
        ("difficulty_score", "REAL"),
        ("leaf_type", "TEXT"),
        ("triage_decision", "TEXT"),
        ("escalation_index", "INTEGER NOT NULL DEFAULT 0"),
        ("implement_harness", "TEXT"),
        ("implement_model", "TEXT"),
    )
    for name, decl in additions:
        if name not in cols:
            await connection.execute(f"ALTER TABLE tasks ADD COLUMN {name} {decl}")  # nosec B608 - fixed literal column names, no user input
    await connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks (parent_task_id)"
    )
```

Append to the `MIGRATIONS` list:

```python
    Migration(
        7,
        "add tasks triage/split/escalation columns for decomposition standard v2",
        _migration_0007_leaf_triage,
    ),
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_migrations.py tests/test_database.py -v`
Expected: PASS.

- [ ] **Step 5: Mutation-check idempotency**

Temporarily remove the `if name not in cols:` guard so every `ALTER TABLE` runs unconditionally.
Run: `uv run pytest tests/test_migrations.py::test_migration_7_is_idempotent -v`
Expected: FAIL with `sqlite3.OperationalError: duplicate column name: parent_task_id`. Restore the guard and re-run to confirm PASS.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/database.py tests/test_migrations.py
git commit -m "feat(db): migration 7, triage and escalation columns on tasks

Three columns from the spec DDL plus four the spec's own hard bounds
require durably: triage_decision (one triage per leaf), escalation_index
(bounded by the escalation list), and implement_harness/implement_model
so an escalated success is never attributed to the original worker."
```

---

### Task 9: The `TriageDecision` contract and its golden fixtures

**Files:**
- Modify: `src/orchestrator/models/schemas.py`
- Create: `tests/fixtures/triage/retry_decision.json`
- Create: `tests/fixtures/triage/split_decision.json`
- Create: `tests/fixtures/triage/escalate_decision.json`
- Create: `tests/fixtures/triage/human_decision.json`
- Test: `tests/test_triage_decision.py`

**Depends on:** Task 8

- [ ] **Step 1: Write the failing test**

Create `tests/test_triage_decision.py`:

```python
"""TriageDecision is a brain-output contract, so it gets golden fixtures.

Same discipline as LeafTask: a decision must round-trip through the model, so
extend these fixtures when you add a field.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from orchestrator.models.schemas import TriageDecision


FIXTURES = Path(__file__).parent / "fixtures" / "triage"


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    ["retry_decision", "split_decision", "escalate_decision", "human_decision"],
)
def test_golden_fixture_round_trips(name: str):
    raw = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    decision = TriageDecision.model_validate(raw)
    assert json.loads(decision.model_dump_json()) == raw


@pytest.mark.unit
def test_split_requires_children():
    with pytest.raises(ValidationError, match="children"):
        TriageDecision(decision="split", reason="too big")


@pytest.mark.unit
def test_split_rejects_fewer_than_two_children():
    child = {"id": "a", "title": "A", "plan_text": "Goal: a"}
    with pytest.raises(ValidationError):
        TriageDecision(decision="split", reason="too big", children=[child])


@pytest.mark.unit
def test_split_rejects_more_than_four_children():
    children = [
        {"id": f"c{i}", "title": f"C{i}", "plan_text": "Goal: c"} for i in range(5)
    ]
    with pytest.raises(ValidationError):
        TriageDecision(decision="split", reason="too big", children=children)


@pytest.mark.unit
def test_non_split_decisions_reject_children():
    child = {"id": "a", "title": "A", "plan_text": "Goal: a"}
    with pytest.raises(ValidationError):
        TriageDecision(decision="retry", reason="one more go", children=[child, child])


@pytest.mark.unit
def test_unknown_decision_is_rejected():
    with pytest.raises(ValidationError):
        TriageDecision(decision="give_up", reason="nope")


@pytest.mark.unit
def test_reason_is_required():
    with pytest.raises(ValidationError):
        TriageDecision(decision="human")
```

- [ ] **Step 2: Write the fixtures**

Create `tests/fixtures/triage/retry_decision.json`:

```json
{
  "decision": "retry",
  "reason": "The worker misread the acceptance command; the leaf itself is correctly sized.",
  "children": null,
  "refined_prompt": "Run `uv run pytest tests/test_widget.py -k make_widget`, not the whole suite."
}
```

Create `tests/fixtures/triage/escalate_decision.json`:

```json
{
  "decision": "escalate",
  "reason": "Both attempts produced syntactically valid but semantically wrong async code; this is a capability ceiling, not a sizing problem.",
  "children": null,
  "refined_prompt": null
}
```

Create `tests/fixtures/triage/human_decision.json`:

```json
{
  "decision": "human",
  "reason": "The leaf's acceptance test contradicts the plan contract; a human must resolve which is authoritative.",
  "children": null,
  "refined_prompt": null
}
```

Create `tests/fixtures/triage/split_decision.json`. Build it by dumping the model
so the embedded `LeafTask` shape matches `LEAF_SCHEMA_VERSION` exactly:

```bash
mkdir -p tests/fixtures/triage
uv run python - <<'PY'
import json
from pathlib import Path
from orchestrator.models.schemas import TriageDecision

decision = TriageDecision(
    decision="split",
    reason=(
        "The leaf touches the model, the API, and the dashboard in one unit; "
        "both failures were in the dashboard half while the model half was correct."
    ),
    children=[
        {
            "id": "add-widget-model-s1",
            "title": "Add the Widget model",
            "description": "Add the Widget pydantic model and its tests.",
            "plan_text": (
                "## Goal\nAdd the Widget model.\n"
                "## Files\nsrc/orchestrator/models/schemas.py\n"
                "## Steps\n1. Add class Widget.\n"
                "## Acceptance\nRun `uv run pytest tests/test_schemas.py`"
            ),
            "files": ["src/orchestrator/models/schemas.py"],
            "task_type": "feature",
            "estimated_loc": 30,
            "verification": "Run `uv run pytest tests/test_schemas.py` and confirm it passes",
            "leaf_type": "function_add",
        },
        {
            "id": "add-widget-model-s2",
            "title": "Render the Widget in the dashboard",
            "description": "Add the widget row to the dashboard task table.",
            "plan_text": (
                "## Goal\nRender the widget.\n"
                "## Files\nweb/app.js\n"
                "## Steps\n1. Add the row renderer.\n"
                "## Acceptance\nRun `uv run pytest tests/visual`"
            ),
            "depends_on": ["add-widget-model-s1"],
            "files": ["web/app.js"],
            "task_type": "feature",
            "estimated_loc": 25,
            "verification": "Run `uv run pytest tests/visual` and confirm it passes",
            "leaf_type": "function_add",
        },
    ],
)
Path("tests/fixtures/triage/split_decision.json").write_text(
    decision.model_dump_json(indent=2) + "\n", encoding="utf-8"
)
PY
```

This script will fail until Step 3 lands; run it after implementing the model.

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run pytest tests/test_triage_decision.py -v`
Expected: FAIL with `ImportError: cannot import name 'TriageDecision'`.

- [ ] **Step 4: Implement the model**

In `src/orchestrator/models/schemas.py`, add after the `LeafTask` class:

```python
class TriageDecision(BaseModel):
    """The brain's verdict on a leaf that failed twice.

    Contract from the Usable Praxis spec section 2.2.  Malformed output gets one
    re-ask with the validation errors (same pattern as F3's informed rounds); a
    second failure falls back to ``human``.  Fail closed, never guess.
    """

    model_config = ConfigDict(extra="forbid")

    decision: Literal["retry", "split", "escalate", "human"]
    reason: str
    children: list[LeafTask] | None = None
    refined_prompt: str | None = None

    @model_validator(mode="after")
    def _check_children_match_decision(self) -> TriageDecision:
        if self.decision == "split":
            if not self.children:
                message = "children are required when decision == 'split'"
                raise ValueError(message)
            if not 2 <= len(self.children) <= 4:
                message = (
                    "a split must produce between 2 and 4 children, "
                    f"got {len(self.children)}"
                )
                raise ValueError(message)
        elif self.children:
            message = f"children are only allowed when decision == 'split', not '{self.decision}'"
            raise ValueError(message)
        return self
```

Add `Literal` to the `typing` import at the top of the file if it is not already
imported.

- [ ] **Step 5: Generate the split fixture and run the tests**

Run the Step 2 heredoc to write `tests/fixtures/triage/split_decision.json`, then:

Run: `uv run pytest tests/test_triage_decision.py -v`
Expected: PASS (10 tests).

- [ ] **Step 6: Mutation-check the child-count bound**

Temporarily change `if not 2 <= len(self.children) <= 4:` to `if False:`.
Run: `uv run pytest tests/test_triage_decision.py -v -k children`
Expected: `test_split_rejects_fewer_than_two_children` and `test_split_rejects_more_than_four_children` FAIL. Restore and re-run to confirm PASS.

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator/models/schemas.py tests/test_triage_decision.py tests/fixtures/triage/
git commit -m "feat(schemas): add the TriageDecision brain-output contract

Strict Pydantic with golden fixtures for all four decisions; split is
bounded to 2 to 4 children at the model boundary, not at the call site."
```

---

### Task 10: Register the `leaf_failure_triage` call site

**Files:**
- Modify: `src/orchestrator/core/llm_router.py:62-111`
- Modify: `src/orchestrator/core/roles.py:7-19`
- Test: `tests/test_llm_router.py`, `tests/test_effective_settings_chains.py`

**Depends on:** Task 9

- [ ] **Step 1: Write the failing test**

Append to `tests/test_llm_router.py`:

```python
@pytest.mark.unit
def test_leaf_failure_triage_is_a_registered_call_site():
    from orchestrator.core.llm_router import CALL_SITE_DEFAULTS

    assert "leaf_failure_triage" in CALL_SITE_DEFAULTS
    cfg = CALL_SITE_DEFAULTS["leaf_failure_triage"]
    assert cfg["provider"] == "claude"
    assert cfg["model"] == "claude-sonnet-4-6"


@pytest.mark.unit
def test_every_call_site_default_has_a_role():
    from orchestrator.core.llm_router import CALL_SITE_DEFAULTS
    from orchestrator.core.roles import ROLE_OF_CALL_SITE

    unmapped = set(CALL_SITE_DEFAULTS) - set(ROLE_OF_CALL_SITE)
    assert unmapped == {"derive_tasks"}, (
        "every brain call-site except derive_tasks (deterministic, local-only) "
        f"must map to a role; unmapped: {sorted(unmapped)}"
    )
```

Note: verify the expected exception set before writing the assertion. Run
`uv run python -c "from orchestrator.core.llm_router import CALL_SITE_DEFAULTS; from orchestrator.core.roles import ROLE_OF_CALL_SITE; print(sorted(set(CALL_SITE_DEFAULTS) - set(ROLE_OF_CALL_SITE)))"`
and use the printed set as the expected value (it should be empty today, since
`derive_tasks` IS mapped, in which case assert `unmapped == set()`).

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_llm_router.py -v -k triage`
Expected: FAIL with `assert 'leaf_failure_triage' in {...}`.

- [ ] **Step 3: Register the call site**

In `src/orchestrator/core/llm_router.py`, add to `CALL_SITE_DEFAULTS`, after the
`plan_review` entry:

```python
    # Triage of a twice-failed leaf: read the failure evidence and decide
    # retry / split / escalate / human. Structured judgment over a bounded
    # evidence pack, same tier as plan_review; resolves through the plan seat's
    # fallback chain.
    "leaf_failure_triage": {
        "provider": "claude",
        "model": "claude-sonnet-4-6",
        "effort": "medium",
    },
```

In `src/orchestrator/core/roles.py`, add to `ROLE_OF_CALL_SITE`, after
`"plan_review": "plan",`:

```python
    "leaf_failure_triage": "plan",
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_llm_router.py tests/test_effective_settings_chains.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/llm_router.py src/orchestrator/core/roles.py tests/test_llm_router.py
git commit -m "feat(router): register the leaf_failure_triage call site under the plan role"
```

---

### Task 11: `core/leaf_split.py`, pure graph rewiring

**Files:**
- Create: `src/orchestrator/core/leaf_split.py`
- Test: `tests/test_leaf_split.py`

**Depends on:** Task 9

- [ ] **Step 1: Write the failing test**

Create `tests/test_leaf_split.py`:

```python
"""Split rewiring is pure and positional.

get_dispatchable_tasks maps opus_plan["tasks"] to DB rows BY INDEX, so children
must be APPENDED and the superseded parent must never be removed.
"""

import pytest

from orchestrator.core.leaf_split import child_slugs, rewire_plan_for_split
from orchestrator.models.schemas import LeafTask


def _plan() -> dict:
    return {
        "tasks": [
            {"id": "a", "slug": "a", "title": "A", "description": "A", "depends_on": []},
            {"id": "b", "slug": "b", "title": "B", "description": "B", "depends_on": ["a"]},
            {"id": "c", "slug": "c", "title": "C", "description": "C", "depends_on": ["b"]},
        ]
    }


def _children() -> list[LeafTask]:
    return [
        LeafTask(id="x1", title="B part one", plan_text="Goal: one"),
        LeafTask(id="x2", title="B part two", plan_text="Goal: two", depends_on=["x1"]),
    ]


@pytest.mark.unit
def test_child_slugs_are_parent_suffixed_and_ordered():
    assert child_slugs("my-leaf", 3) == ["my-leaf-s1", "my-leaf-s2", "my-leaf-s3"]


@pytest.mark.unit
def test_children_are_appended_not_inserted():
    plan = _plan()
    rewire_plan_for_split(plan, "b", _children())
    assert [t["slug"] for t in plan["tasks"]] == ["a", "b", "c", "b-s1", "b-s2"]


@pytest.mark.unit
def test_the_parent_is_never_removed():
    plan = _plan()
    rewire_plan_for_split(plan, "b", _children())
    assert any(t["slug"] == "b" for t in plan["tasks"])


@pytest.mark.unit
def test_children_inherit_the_parents_dependencies():
    plan = _plan()
    rewire_plan_for_split(plan, "b", _children())
    by_slug = {t["slug"]: t for t in plan["tasks"]}
    # First child inherits the parent's deps verbatim.
    assert by_slug["b-s1"]["depends_on"] == ["a"]
    # A child that declared an internal dependency keeps it, remapped to slugs,
    # in addition to the inherited parent deps.
    assert set(by_slug["b-s2"]["depends_on"]) == {"a", "b-s1"}


@pytest.mark.unit
def test_dependents_of_the_parent_now_depend_on_every_child():
    plan = _plan()
    rewire_plan_for_split(plan, "b", _children())
    by_slug = {t["slug"]: t for t in plan["tasks"]}
    assert set(by_slug["c"]["depends_on"]) == {"b-s1", "b-s2"}
    assert "b" not in by_slug["c"]["depends_on"]


@pytest.mark.unit
def test_children_carry_the_parent_slug_as_parent_slug():
    plan = _plan()
    rewire_plan_for_split(plan, "b", _children())
    by_slug = {t["slug"]: t for t in plan["tasks"]}
    assert by_slug["b-s1"]["parent_slug"] == "b"
    assert by_slug["b-s2"]["parent_slug"] == "b"


@pytest.mark.unit
def test_children_keep_their_leaf_contract_fields():
    plan = _plan()
    rewire_plan_for_split(plan, "b", _children())
    by_slug = {t["slug"]: t for t in plan["tasks"]}
    assert by_slug["b-s1"]["plan_text"] == "Goal: one"
    assert by_slug["b-s1"]["leaf_type"] == "generic"


@pytest.mark.unit
def test_an_unknown_parent_slug_raises():
    with pytest.raises(KeyError):
        rewire_plan_for_split(_plan(), "nope", _children())


@pytest.mark.unit
def test_rewiring_returns_the_children_in_append_order():
    plan = _plan()
    appended = rewire_plan_for_split(plan, "b", _children())
    assert [t["slug"] for t in appended] == ["b-s1", "b-s2"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_leaf_split.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.core.leaf_split'`.

- [ ] **Step 3: Write the module**

Create `src/orchestrator/core/leaf_split.py`:

```python
"""Pure graph rewiring for an adaptive leaf split.

Given a plan's ``opus_plan`` task list, a parent slug, and the brain's child
leaves, produce the rewired graph:

- children are APPENDED to the task list, never inserted;
- the parent stays in the list (it becomes SUPERSEDED, not deleted);
- children inherit the parent's ``depends_on``;
- any task that depended on the parent now depends on ALL children.

Appending and retaining the parent are load-bearing:
``TaskQueue.get_dispatchable_tasks`` maps ``opus_plan["tasks"]`` to DB rows by
LIST INDEX, so removing or reordering an entry shifts every mapping after it.
"""

from __future__ import annotations

from typing import Any

from orchestrator.models.schemas import LeafTask


def child_slugs(parent_slug: str, count: int) -> list[str]:
    """Return deterministic, collision-free slugs for a split's children.

    ``{parent-slug}-s1..sN``.  Deterministic so a re-run finds the same names,
    and unique because the parent slug is already unique within the plan.
    """
    return [f"{parent_slug}-s{index}" for index in range(1, count + 1)]


def rewire_plan_for_split(
    opus_plan: dict[str, Any],
    parent_slug: str,
    children: list[LeafTask],
) -> list[dict[str, Any]]:
    """Rewire ``opus_plan`` in place to replace a parent leaf with its children.

    Args:
        opus_plan: A normalized ``{"tasks": [...]}`` dict with slugs assigned.
        parent_slug: Slug of the leaf being split.  Must exist.
        children: 2 to 4 validated child leaves from the brain.

    Returns:
        The appended child task dicts, in append order.

    Raises:
        KeyError: If ``parent_slug`` is not present in the plan.
    """
    tasks: list[dict[str, Any]] = opus_plan["tasks"]
    parent = next((t for t in tasks if t.get("slug") == parent_slug), None)
    if parent is None:
        message = f"parent slug {parent_slug!r} not found in plan"
        raise KeyError(message)

    inherited: list[str] = list(parent.get("depends_on") or [])
    slugs = child_slugs(parent_slug, len(children))
    id_to_slug = {child.id: slug for child, slug in zip(children, slugs, strict=True)}

    appended: list[dict[str, Any]] = []
    for child, slug in zip(children, slugs, strict=True):
        data = child.model_dump(mode="json")
        internal_deps = [
            id_to_slug[dep] for dep in child.depends_on if dep in id_to_slug
        ]
        data["id"] = slug
        data["slug"] = slug
        data["parent_slug"] = parent_slug
        # Inherited parent deps first, then this child's own siblings; dedup
        # while preserving order so the graph stays stable across runs.
        merged: list[str] = []
        for dep in [*inherited, *internal_deps]:
            if dep not in merged:
                merged.append(dep)
        data["depends_on"] = merged
        tasks.append(data)
        appended.append(data)

    # Every task that depended on the parent now depends on ALL children: the
    # parent will never reach MERGED, so leaving the edge in place deadlocks
    # the wave scheduler.
    for task in tasks:
        if task.get("slug") in slugs:
            continue
        deps = task.get("depends_on") or []
        if parent_slug not in deps:
            continue
        rebuilt: list[str] = []
        for dep in deps:
            if dep == parent_slug:
                rebuilt.extend(s for s in slugs if s not in rebuilt)
            elif dep not in rebuilt:
                rebuilt.append(dep)
        task["depends_on"] = rebuilt

    return appended
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_leaf_split.py -v`
Expected: PASS (9 tests).

- [ ] **Step 5: Mutation-check the append invariant**

Temporarily replace `tasks.append(data)` with `tasks.insert(0, data)`.
Run: `uv run pytest tests/test_leaf_split.py::test_children_are_appended_not_inserted -v`
Expected: FAIL. Restore and re-run to confirm PASS.

- [ ] **Step 6: Mutation-check the dependent rewiring**

Temporarily comment out the whole `for task in tasks:` rewiring loop.
Run: `uv run pytest tests/test_leaf_split.py::test_dependents_of_the_parent_now_depend_on_every_child -v`
Expected: FAIL. Restore and re-run to confirm PASS.

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator/core/leaf_split.py tests/test_leaf_split.py
git commit -m "feat(split): add pure graph rewiring for an adaptive leaf split

Children append, the parent stays, dependents fan out to all children.
Appending is load-bearing: get_dispatchable_tasks maps opus_plan tasks to
DB rows by list index."
```

---

### Task 12: `core/escalation.py`, ordered implementer substitution

**Files:**
- Create: `src/orchestrator/core/escalation.py`
- Modify: `config/praxis.yaml`
- Modify: `src/orchestrator/core/effective_settings.py`
- Test: `tests/test_escalation.py`

**Depends on:** Task 8

- [ ] **Step 1: Write the failing test**

Create `tests/test_escalation.py`:

```python
"""Escalation is a dispatch-time model substitution, not a router fallback.

The implement seat is spawn-baked (see the role-model-fallback gotcha), so the
router cannot fall back for it.  Escalation walks an ordered config list and
stops at its length.
"""

import pytest

from orchestrator.core.escalation import EscalationPair, next_escalation


LADDER = [
    {"harness": "opencode", "model": "qwen3.6-27b-strong"},
    {"harness": "agy", "model": "gemini-3.6-flash-high"},
]


@pytest.mark.unit
def test_first_escalation_returns_the_first_pair():
    assert next_escalation(LADDER, 0) == EscalationPair("opencode", "qwen3.6-27b-strong")


@pytest.mark.unit
def test_second_escalation_returns_the_second_pair():
    assert next_escalation(LADDER, 1) == EscalationPair("agy", "gemini-3.6-flash-high")


@pytest.mark.unit
def test_escalation_is_exhausted_at_the_list_length():
    assert next_escalation(LADDER, 2) is None


@pytest.mark.unit
def test_an_empty_ladder_is_immediately_exhausted():
    assert next_escalation([], 0) is None


@pytest.mark.unit
def test_a_malformed_entry_is_skipped_not_fatal():
    ladder = [{"harness": "opencode"}, {"harness": "agy", "model": "g"}]
    assert next_escalation(ladder, 0) == EscalationPair("agy", "g")


@pytest.mark.unit
def test_a_negative_index_is_treated_as_zero():
    assert next_escalation(LADDER, -1) == EscalationPair("opencode", "qwen3.6-27b-strong")


@pytest.mark.unit
async def test_effective_settings_reads_the_ladder_from_yaml(test_db):
    from orchestrator.config import Settings
    from orchestrator.core.effective_settings import EffectiveSettings

    settings = EffectiveSettings(Settings(auth_token="t", _env_file=None), test_db)
    ladder = await settings.implement_escalation()
    assert isinstance(ladder, list)
    assert all("harness" in entry and "model" in entry for entry in ladder)
```

The `test_db` fixture already exists in `tests/conftest.py`; confirm its name by
reading that file before running, and use the project's in-memory database
fixture whatever it is called.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_escalation.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.core.escalation'`.

- [ ] **Step 3: Write the module**

Create `src/orchestrator/core/escalation.py`:

```python
"""Ordered implementer escalation for a leaf that failed on capability.

The implement seat is spawn-baked (the worker model is chosen when the
container is created), so escalation cannot ride the LLM router's role fallback
chain the way plan and review do.  It is a dispatch-time substitution: the task
carries an ``escalation_index``, and the next dispatch reads the pair at that
index from ``implement_escalation`` in ``config/praxis.yaml``.

Escalate a leaf, not a plan (FrugalGPT cascade economics, arXiv 2305.05176).
"""

from __future__ import annotations

from typing import Any, NamedTuple


class EscalationPair(NamedTuple):
    """One rung of the escalation ladder."""

    harness: str
    model: str


def next_escalation(
    ladder: list[dict[str, Any]], index: int
) -> EscalationPair | None:
    """Return the next untried (harness, model) pair, or None when exhausted.

    Malformed entries (missing ``harness`` or ``model``) are skipped rather than
    raising: a typo in operator YAML must degrade to "no further escalation",
    never wedge the review loop.

    Args:
        ladder: The ordered ``implement_escalation`` list from settings.
        index: How many rungs this leaf has already burned.

    Returns:
        The next usable pair, or None when the ladder is exhausted.
    """
    valid = [
        EscalationPair(str(entry["harness"]), str(entry["model"]))
        for entry in ladder
        if isinstance(entry, dict) and entry.get("harness") and entry.get("model")
    ]
    position = max(index, 0)
    if position >= len(valid):
        return None
    return valid[position]
```

- [ ] **Step 4: Add the config**

In `config/praxis.yaml`, after the `escalation:` block, add:

```yaml
# Ordered implementer ladder for a leaf the brain triages as "escalate".
# The implement seat is spawn-baked, so this is a dispatch-time substitution,
# not a router fallback chain. Escalation stops when this list is exhausted;
# the leaf then parks for a human.
implement_escalation:
  - harness: opencode
    model: "qwen3.6-27b"
  - harness: agy
    model: "Gemini 3.6 Flash (High)"

# Hard ceiling on leaves in one plan, counting split children. Adaptive
# splitting without this is an unbounded token spend.
max_leaves_per_plan: 24
```

- [ ] **Step 5: Add the settings accessor**

In `src/orchestrator/core/effective_settings.py`, add after `escalation_policy`:

```python
    async def implement_escalation(self) -> list[dict[str, Any]]:
        """Return the ordered implementer escalation ladder from YAML.

        Returns an empty list when unconfigured, which means "never escalate";
        a triage ``escalate`` decision then falls through to ``human``.
        """
        yaml_data = await self._get_yaml()
        ladder = yaml_data.get("implement_escalation") or []
        return list(ladder) if isinstance(ladder, list) else []

    async def max_leaves_per_plan(self) -> int:
        """Return the hard ceiling on total leaves in one plan (default 24)."""
        yaml_data = await self._get_yaml()
        raw = yaml_data.get("max_leaves_per_plan", 24)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 24
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_escalation.py tests/test_effective_settings.py -v`
Expected: PASS.

- [ ] **Step 7: Mutation-check the exhaustion bound**

Temporarily change `if position >= len(valid):` to `if position > len(valid):`.
Run: `uv run pytest tests/test_escalation.py::test_escalation_is_exhausted_at_the_list_length -v`
Expected: FAIL with `IndexError`. Restore and re-run to confirm PASS.

- [ ] **Step 8: Commit**

```bash
git add src/orchestrator/core/escalation.py src/orchestrator/core/effective_settings.py config/praxis.yaml tests/test_escalation.py
git commit -m "feat(escalation): add the ordered implementer ladder

Dispatch-time substitution because the implement seat is spawn-baked.
Adds implement_escalation and max_leaves_per_plan to config/praxis.yaml.
Note: config/praxis.yaml is baked into the orchestrator image today, so
this needs an image rebuild until the product plan mounts it."
```

---

### Task 13: `core/leaf_triage.py`, the triage brain call

**Files:**
- Create: `src/orchestrator/core/leaf_triage.py`
- Test: `tests/test_leaf_triage.py`

**Depends on:** Task 9, Task 10, Task 11, Task 12

- [ ] **Step 1: Write the failing test**

Create `tests/test_leaf_triage.py`:

```python
"""Triage is deterministic around exactly one brain call.

One re-ask on malformed output, then fall back to `human`. Never guess.
"""

import json

import pytest

from orchestrator.core.leaf_triage import (
    TriageEvidence,
    build_triage_prompt,
    parse_triage_response,
    triage_leaf,
)
from orchestrator.models.schemas import CapabilityProfile, TriageDecision


def _evidence(**overrides) -> TriageEvidence:
    base = {
        "task_slug": "add-widget",
        "leaf_type": "function_add",
        "plan_text": "## Goal\nAdd it.\n## Files\nsrc/a.py\n## Steps\n1. go\n## Acceptance\n`pytest`",
        "profile": CapabilityProfile(
            model_name="m", parameter_count_b=30, context_window=8192
        ),
        "attempts": [
            {
                "attempt": 1,
                "files_touched": 4,
                "loc_delta": 210,
                "diff": "diff --git a/src/a.py",
                "verify_exit_code": 1,
                "verify_tail": "3 failed",
                "review_reason": "Missing the AbortSignal parameter",
            },
            {
                "attempt": 2,
                "files_touched": 5,
                "loc_delta": 260,
                "diff": "diff --git a/src/a.py",
                "verify_exit_code": 1,
                "verify_tail": "2 failed",
                "review_reason": "Still missing the AbortSignal parameter",
            },
        ],
        "difficulty_score": 0.41,
        "remaining_leaf_budget": 20,
        "escalation_available": True,
    }
    base.update(overrides)
    return TriageEvidence(**base)


@pytest.mark.unit
def test_prompt_names_the_four_decisions():
    prompt = build_triage_prompt(_evidence())
    for decision in ("retry", "split", "escalate", "human"):
        assert f'"{decision}"' in prompt


@pytest.mark.unit
def test_prompt_states_the_hard_rules():
    prompt = build_triage_prompt(_evidence())
    assert "2 and 4" in prompt or "2 to 4" in prompt
    assert "may not split again" in prompt
    assert "20" in prompt  # remaining leaf budget


@pytest.mark.unit
def test_prompt_carries_the_verbatim_plan_text():
    ev = _evidence()
    assert ev.plan_text in build_triage_prompt(ev)


@pytest.mark.unit
def test_prompt_carries_every_attempt_reason():
    prompt = build_triage_prompt(_evidence())
    assert "Missing the AbortSignal parameter" in prompt
    assert "Still missing the AbortSignal parameter" in prompt


@pytest.mark.unit
def test_prompt_forbids_escalation_when_the_ladder_is_exhausted():
    prompt = build_triage_prompt(_evidence(escalation_available=False))
    assert "escalation ladder is exhausted" in prompt


@pytest.mark.unit
def test_prompt_caps_a_huge_diff():
    huge = "x" * 500_000
    ev = _evidence(
        attempts=[
            {
                "attempt": 1,
                "files_touched": 1,
                "loc_delta": 1,
                "diff": huge,
                "verify_exit_code": 1,
                "verify_tail": "1 failed",
                "review_reason": "nope",
            }
        ]
    )
    assert len(build_triage_prompt(ev)) < 100_000


@pytest.mark.unit
def test_parse_accepts_a_fenced_json_object():
    raw = '```json\n{"decision": "human", "reason": "unclear"}\n```'
    assert parse_triage_response(raw).decision == "human"


@pytest.mark.unit
def test_parse_accepts_prose_before_the_object():
    raw = 'Here is my call.\n{"decision": "escalate", "reason": "ceiling"}'
    assert parse_triage_response(raw).decision == "escalate"


@pytest.mark.unit
def test_parse_raises_on_a_malformed_object():
    from orchestrator.core.leaf_triage import TriageParseError

    with pytest.raises(TriageParseError):
        parse_triage_response("not json at all")


@pytest.mark.unit
def test_parse_raises_on_a_split_with_one_child():
    from orchestrator.core.leaf_triage import TriageParseError

    raw = json.dumps(
        {
            "decision": "split",
            "reason": "too big",
            "children": [{"id": "a", "title": "A", "plan_text": "Goal: a"}],
        }
    )
    with pytest.raises(TriageParseError):
        parse_triage_response(raw)


class _Router:
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    async def run(self, call_site, prompt, project_id=None, cwd=None):
        self.calls.append((call_site, prompt))
        return self.responses.pop(0)


@pytest.mark.unit
async def test_triage_uses_the_leaf_failure_triage_call_site():
    router = _Router('{"decision": "human", "reason": "unclear"}')
    await triage_leaf(_evidence(), router, project_id="p1")
    assert router.calls[0][0] == "leaf_failure_triage"


@pytest.mark.unit
async def test_triage_re_asks_once_on_malformed_output():
    router = _Router("garbage", '{"decision": "retry", "reason": "one more"}')
    decision = await triage_leaf(_evidence(), router, project_id="p1")
    assert decision.decision == "retry"
    assert len(router.calls) == 2
    assert "validation error" in router.calls[1][1].lower()


@pytest.mark.unit
async def test_triage_falls_back_to_human_after_two_bad_answers():
    router = _Router("garbage", "still garbage")
    decision = await triage_leaf(_evidence(), router, project_id="p1")
    assert decision.decision == "human"
    assert len(router.calls) == 2


@pytest.mark.unit
async def test_triage_downgrades_escalate_when_the_ladder_is_exhausted():
    router = _Router('{"decision": "escalate", "reason": "ceiling"}')
    decision = await triage_leaf(
        _evidence(escalation_available=False), router, project_id="p1"
    )
    assert decision.decision == "human"


@pytest.mark.unit
async def test_triage_downgrades_split_that_would_breach_the_leaf_ceiling():
    router = _Router(
        json.dumps(
            {
                "decision": "split",
                "reason": "too big",
                "children": [
                    {"id": "a", "title": "A", "plan_text": "Goal: a"},
                    {"id": "b", "title": "B", "plan_text": "Goal: b"},
                    {"id": "c", "title": "C", "plan_text": "Goal: c"},
                ],
            }
        )
    )
    decision = await triage_leaf(
        _evidence(remaining_leaf_budget=2), router, project_id="p1"
    )
    assert decision.decision == "escalate"


@pytest.mark.unit
async def test_triage_downgrades_split_to_human_when_escalation_is_also_gone():
    router = _Router(
        json.dumps(
            {
                "decision": "split",
                "reason": "too big",
                "children": [
                    {"id": "a", "title": "A", "plan_text": "Goal: a"},
                    {"id": "b", "title": "B", "plan_text": "Goal: b"},
                    {"id": "c", "title": "C", "plan_text": "Goal: c"},
                ],
            }
        )
    )
    decision = await triage_leaf(
        _evidence(remaining_leaf_budget=0, escalation_available=False),
        router,
        project_id="p1",
    )
    assert decision.decision == "human"


@pytest.mark.unit
async def test_a_router_exception_falls_back_to_human():
    class _Boom:
        async def run(self, *args, **kwargs):
            message = "provider down"
            raise RuntimeError(message)

    decision = await triage_leaf(_evidence(), _Boom(), project_id="p1")
    assert decision.decision == "human"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_leaf_triage.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.core.leaf_triage'`.

- [ ] **Step 3: Write the module**

Create `src/orchestrator/core/leaf_triage.py`:

```python
"""Brain triage of a leaf that failed twice on worker-attributable grounds.

One brain call, deterministic bounds around it.  ADaPT (arXiv 2311.05772) says
decompose a subtask only when the executor fails it; runtime-structured
decomposition (arXiv 2605.15425) says isolate the failed unit rather than
re-running the plan.  This module is where both become behavior.

Contract: build an evidence pack, ask ``leaf_failure_triage`` for a
``TriageDecision``, re-ask exactly once on malformed output, then fall back to
``human``.  Downgrade any decision the caller cannot honour (escalation ladder
exhausted, split would breach ``max_leaves_per_plan``).  Never guess.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

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
    "- The escalation ladder is exhausted. Do NOT answer \"escalate\"; answer "
    '"split", "retry", or "human".'
)


def _render_attempt(attempt: dict[str, Any]) -> str:
    diff = str(attempt.get("diff") or "")
    if len(diff) > _DIFF_CHARS_PER_ATTEMPT:
        diff = diff[:_DIFF_CHARS_PER_ATTEMPT] + "\n... (diff truncated)"
    tail = str(attempt.get("verify_tail") or "")
    if len(tail) > _VERIFY_TAIL_CHARS:
        tail = tail[-_VERIFY_TAIL_CHARS:]
    return (
        f"Attempt {attempt.get('attempt')}:\n"
        f"  files touched: {attempt.get('files_touched')}\n"
        f"  LOC delta: {attempt.get('loc_delta')}\n"
        f"  verify exit code: {attempt.get('verify_exit_code')}\n"
        f"  verify output tail:\n{tail}\n"
        f"  reviewer verdict reason: {attempt.get('review_reason')}\n"
        f"  diff:\n{diff}\n"
    )


def build_triage_prompt(evidence: TriageEvidence) -> str:
    """Render the triage prompt from the evidence pack."""
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


def _downgrade(
    decision: TriageDecision, evidence: TriageEvidence
) -> TriageDecision:
    """Reduce a decision the caller cannot honour to one it can.

    escalate with an exhausted ladder becomes human.  split that would breach
    the plan's leaf ceiling becomes escalate, or human when escalation is also
    unavailable.  Fail closed in every direction.
    """
    if decision.decision == "escalate" and not evidence.escalation_available:
        return TriageDecision(
            decision="human",
            reason=(
                "escalation ladder exhausted; original triage reason: "
                f"{decision.reason}"
            ),
        )
    if decision.decision == "split":
        children = decision.children or []
        if len(children) > evidence.remaining_leaf_budget:
            fallback = "escalate" if evidence.escalation_available else "human"
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
    exception (provider down, auth failure) also falls back to ``human``:
    triage is an optimization over the existing retry path and must never be
    able to wedge a task.

    Args:
        evidence: The bounded evidence pack.
        router: An ``LLMRouter``-compatible object with ``run(call_site, prompt,
            project_id)``.
        project_id: Project scope for router resolution.

    Returns:
        A validated, downgraded-if-necessary ``TriageDecision``.
    """
    prompt = build_triage_prompt(evidence)
    last_error = ""
    for attempt in range(1, _TRIAGE_ATTEMPTS + 1):
        try:
            raw = await router.run(
                "leaf_failure_triage", prompt, project_id=project_id
            )
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
                    f"{build_triage_prompt(evidence)}\n\n"
                    "Your previous answer was rejected with this validation "
                    f"error:\n{last_error}\n"
                    "Respond again with ONLY the corrected JSON object."
                )
    return TriageDecision(
        decision="human",
        reason=f"triage output was unparseable twice: {last_error}",
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_leaf_triage.py -v`
Expected: PASS (18 tests).

- [ ] **Step 5: Mutation-check the re-ask bound**

Temporarily change `_TRIAGE_ATTEMPTS = 2` to `_TRIAGE_ATTEMPTS = 5`.
Run: `uv run pytest tests/test_leaf_triage.py::test_triage_falls_back_to_human_after_two_bad_answers -v`
Expected: FAIL with `IndexError: pop from empty list` (the fake router only holds two responses). Restore to 2 and re-run to confirm PASS.

- [ ] **Step 6: Mutation-check the downgrade**

Temporarily make `_downgrade` return `decision` unchanged as its first statement.
Run: `uv run pytest tests/test_leaf_triage.py -v -k downgrade`
Expected: all three downgrade tests FAIL. Restore and re-run to confirm PASS.

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator/core/leaf_triage.py tests/test_leaf_triage.py
git commit -m "feat(triage): add the leaf_failure_triage brain call

One call, one informed re-ask, then human. Decisions the caller cannot
honour are downgraded rather than attempted: escalate with an exhausted
ladder becomes human, a split over the plan leaf ceiling becomes escalate."
```

---

### Task 14: TaskQueue methods for supersede, split insertion, and implementer override

**Files:**
- Modify: `src/orchestrator/core/task_queue.py`
- Test: `tests/test_task_queue_split.py`

**Depends on:** Task 8, Task 11

- [ ] **Step 1: Write the failing test**

Create `tests/test_task_queue_split.py`:

```python
"""Split insertion must preserve the positional opus_plan-to-row mapping.

get_dispatchable_tasks zips opus_plan["tasks"] against get_tasks_for_plan
(ordered by rowid) BY INDEX. Children therefore append to both, in the same
order, and the superseded parent is never removed from either.
"""

import json

import pytest

from orchestrator.core.task_queue import TaskQueue
from orchestrator.models.schemas import LeafTask, TaskStatus


async def _seed(db) -> tuple[TaskQueue, str, list[str]]:
    tq = TaskQueue(db)
    await db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url, default_branch) "
        "VALUES ('proj1', 'u1', 'p', 'https://github.com/o/r', 'main')"
    )
    plan_id = await tq.create_plan("proj1", "test")
    opus_plan = {
        "tasks": [
            {"id": "a", "slug": "a", "title": "A", "description": "A", "depends_on": []},
            {"id": "b", "slug": "b", "title": "B", "description": "B", "depends_on": ["a"]},
            {"id": "c", "slug": "c", "title": "C", "description": "C", "depends_on": ["b"]},
        ]
    }
    await tq.activate_plan(plan_id, opus_plan, "plan/x")
    rows = await tq.get_tasks_for_plan(plan_id)
    return tq, plan_id, [r["id"] for r in rows]


@pytest.mark.unit
async def test_supersede_sets_the_status_and_records_the_decision(test_db):
    tq, plan_id, ids = await _seed(test_db)
    await tq.supersede_task(ids[1], "split", "too big")
    task = await tq.get_task(ids[1])
    assert task["status"] == TaskStatus.SUPERSEDED
    assert task["triage_decision"] == "split"
    assert "too big" in task["review_feedback"]


@pytest.mark.unit
async def test_record_triage_decision_without_superseding(test_db):
    tq, plan_id, ids = await _seed(test_db)
    await tq.record_triage_decision(ids[1], "retry")
    task = await tq.get_task(ids[1])
    assert task["triage_decision"] == "retry"
    assert task["status"] != TaskStatus.SUPERSEDED


@pytest.mark.unit
async def test_insert_split_children_appends_rows_in_plan_order(test_db):
    tq, plan_id, ids = await _seed(test_db)
    children = [
        LeafTask(id="x1", title="B one", plan_text="Goal: one"),
        LeafTask(id="x2", title="B two", plan_text="Goal: two"),
    ]
    await tq.insert_split_children(plan_id, ids[1], "b", children)

    plan = await tq.get_plan(plan_id)
    graph = json.loads(plan["opus_plan"])
    rows = await tq.get_tasks_for_plan(plan_id)
    assert [t["slug"] for t in graph["tasks"]] == ["a", "b", "c", "b-s1", "b-s2"]
    assert [r["branch_name"] for r in rows] == [
        "agent/a",
        "agent/b",
        "agent/c",
        "agent/b-s1",
        "agent/b-s2",
    ]


@pytest.mark.unit
async def test_split_children_carry_the_parent_id(test_db):
    tq, plan_id, ids = await _seed(test_db)
    children = [
        LeafTask(id="x1", title="B one", plan_text="Goal: one"),
        LeafTask(id="x2", title="B two", plan_text="Goal: two"),
    ]
    await tq.insert_split_children(plan_id, ids[1], "b", children)
    rows = await tq.get_tasks_for_plan(plan_id)
    assert rows[3]["parent_task_id"] == ids[1]
    assert rows[4]["parent_task_id"] == ids[1]


@pytest.mark.unit
async def test_split_children_start_with_a_reduced_retry_budget(test_db):
    tq, plan_id, ids = await _seed(test_db)
    children = [
        LeafTask(id="x1", title="B one", plan_text="Goal: one"),
        LeafTask(id="x2", title="B two", plan_text="Goal: two"),
    ]
    await tq.insert_split_children(plan_id, ids[1], "b", children)
    rows = await tq.get_tasks_for_plan(plan_id)
    # attempt starts at 2, so with max_retries 3 a child gets 2 tries, not 3.
    assert rows[3]["attempt"] == 2
    assert rows[4]["attempt"] == 2


@pytest.mark.unit
async def test_split_children_carry_their_leaf_type(test_db):
    tq, plan_id, ids = await _seed(test_db)
    children = [
        LeafTask(id="x1", title="B one", plan_text="Goal: one", leaf_type="test_add"),
        LeafTask(id="x2", title="B two", plan_text="Goal: two"),
    ]
    await tq.insert_split_children(plan_id, ids[1], "b", children)
    rows = await tq.get_tasks_for_plan(plan_id)
    assert rows[3]["leaf_type"] == "test_add"
    assert rows[4]["leaf_type"] == "generic"


@pytest.mark.unit
async def test_a_superseded_parent_does_not_block_plan_completion(test_db):
    tq, plan_id, ids = await _seed(test_db)
    await tq.supersede_task(ids[1], "split", "too big")
    for task_id in (ids[0], ids[2]):
        await tq.update_task_status(task_id, TaskStatus.MERGED)
    assert await tq.all_tasks_done(plan_id) is True


@pytest.mark.unit
async def test_a_child_of_a_superseded_parent_is_dispatchable_after_its_deps_merge(
    test_db,
):
    tq, plan_id, ids = await _seed(test_db)
    children = [
        LeafTask(id="x1", title="B one", plan_text="Goal: one"),
        LeafTask(id="x2", title="B two", plan_text="Goal: two"),
    ]
    await tq.insert_split_children(plan_id, ids[1], "b", children)
    await tq.supersede_task(ids[1], "split", "too big")
    await tq.update_task_status(ids[0], TaskStatus.MERGED)
    dispatchable = await tq.get_dispatchable_tasks(plan_id)
    slugs = {t["branch_name"] for t in dispatchable}
    assert "agent/b-s1" in slugs
    assert "agent/b-s2" in slugs
    # The superseded parent is never dispatchable again.
    assert "agent/b" not in slugs


@pytest.mark.unit
async def test_set_task_implementer_persists_the_escalated_pair(test_db):
    tq, plan_id, ids = await _seed(test_db)
    await tq.set_task_implementer(ids[0], "agy", "gemini-3.6-flash-high", index=1)
    task = await tq.get_task(ids[0])
    assert task["implement_harness"] == "agy"
    assert task["implement_model"] == "gemini-3.6-flash-high"
    assert task["escalation_index"] == 1
```

Read `tests/conftest.py` first and use its actual database fixture name and its
project-seeding helper rather than the inline INSERT above if one exists.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_task_queue_split.py -v`
Expected: FAIL with `AttributeError: 'TaskQueue' object has no attribute 'supersede_task'`.

- [ ] **Step 3: Add the queue methods**

In `src/orchestrator/core/task_queue.py`, add these methods after `retry_task`:

```python
    async def record_triage_decision(self, task_id: str, decision: str) -> None:
        """Stamp the triage decision so a leaf is never triaged twice.

        Presence of ``triage_decision`` is the durable enforcement of the
        "one triage brain call per leaf lifetime" bound.
        """
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "UPDATE tasks SET triage_decision = ?, updated_at = ? WHERE id = ?",
            (decision, now, task_id),
        )

    async def supersede_task(self, task_id: str, decision: str, reason: str) -> None:
        """Retire a task that was replaced by split children.

        SUPERSEDED is terminal and counts as neither a success nor a failure in
        ``task_outcomes``; the split decision itself is the recorded event.  The
        worker session handle is dropped for the same reason it is on any other
        terminal transition: it can only ever be stale from here.
        """
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """UPDATE tasks
               SET status = ?, triage_decision = ?, review_feedback = ?,
                   updated_at = ?,
                   worker_session_id = NULL, worker_session_harness = NULL
               WHERE id = ?""",
            (TaskStatus.SUPERSEDED, decision, reason, now, task_id),
        )

    async def insert_split_children(
        self,
        plan_id: str,
        parent_task_id: str,
        parent_slug: str,
        children: list[Any],
    ) -> list[str]:
        """Rewire the plan graph and append one task row per split child.

        Children are APPENDED to both ``plans.opus_plan`` and the ``tasks``
        table, in the same order, and the parent row is left in place.
        ``get_dispatchable_tasks`` maps the two lists positionally, so any other
        ordering silently mis-associates every task after the parent.

        Children start at ``attempt = 2``, so with the default ``max_retries``
        of 3 they get two tries rather than a fresh three (spec bound: children
        inherit the remaining retry budget, they do not reset it).

        Args:
            plan_id: The plan owning the parent.
            parent_task_id: DB id of the leaf being split.
            parent_slug: Graph slug of the leaf being split.
            children: Validated ``LeafTask`` children from triage.

        Returns:
            The new task row ids, in append order.
        """
        from orchestrator.core.leaf_split import rewire_plan_for_split

        plan = await self.get_plan(plan_id)
        if plan is None or not plan.get("opus_plan"):
            message = f"plan {plan_id} has no task graph to split"
            raise ValueError(message)

        opus_plan = json.loads(plan["opus_plan"])
        appended = rewire_plan_for_split(opus_plan, parent_slug, children)

        await self._db.execute(
            "UPDATE plans SET opus_plan = ? WHERE id = ?",
            (json.dumps(opus_plan), plan_id),
        )

        new_ids: list[str] = []
        for child_data in appended:
            child_id = str(uuid.uuid4())
            await self._db.execute(
                """INSERT INTO tasks
                   (id, plan_id, title, description, branch_name,
                    parent_task_id, leaf_type, attempt)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 2)""",
                (
                    child_id,
                    plan_id,
                    child_data["title"],
                    child_data["description"],
                    f"agent/{child_data['slug']}",
                    parent_task_id,
                    child_data.get("leaf_type"),
                ),
            )
            new_ids.append(child_id)
        logger.info(
            "Split task %s into %d children on plan %s",
            parent_task_id,
            len(new_ids),
            plan_id,
        )
        return new_ids

    async def set_task_implementer(
        self, task_id: str, harness: str, model: str, index: int
    ) -> None:
        """Pin the implementer for this task's next dispatch (escalation).

        Outcome attribution reads these columns, so an escalated success is
        never credited to the original worker.
        """
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """UPDATE tasks
               SET implement_harness = ?, implement_model = ?,
                   escalation_index = ?, updated_at = ?
               WHERE id = ?""",
            (harness, model, index, now, task_id),
        )

    async def append_progress_note(self, task_id: str, note: str) -> None:
        """Append a block to the task's progress note (folded into the Bible)."""
        task = await self.get_task(task_id)
        if task is None:
            message = f"Task {task_id} not found"
            raise ValueError(message)
        existing = task.get("progress_note") or ""
        merged = f"{existing}\n\n{note}".strip()
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "UPDATE tasks SET progress_note = ?, updated_at = ? WHERE id = ?",
            (merged, now, task_id),
        )
```

Confirm `Any` is imported in the module's `typing` import (it already is, used
by the existing return annotations).

- [ ] **Step 4: Make SUPERSEDED a terminal, non-blocking status**

In the same file, change `all_tasks_done` to:

```python
    async def all_tasks_done(self, plan_id: str) -> bool:
        """True when every task is MERGED or SUPERSEDED.

        A SUPERSEDED parent was replaced by its split children; it will never
        reach MERGED, so treating it as outstanding would stop every split plan
        from ever completing.
        """
        tasks = await self.get_tasks_for_plan(plan_id)
        done = {TaskStatus.MERGED, TaskStatus.SUPERSEDED}
        return bool(tasks) and all(task["status"] in done for task in tasks)
```

`get_dispatchable_tasks` needs no change for the parent itself (it only
dispatches `PENDING` rows), but its dependency predicate must accept a
superseded dependency as satisfied, or a child that inherited a dependency on a
sibling-superseded leaf deadlocks. Change the predicate to:

```python
            dependencies = task_data.get("depends_on", [])
            if all(
                slug_to_task.get(dep, {}).get("status")
                in (TaskStatus.MERGED, TaskStatus.SUPERSEDED)
                for dep in dependencies
            ):
                dispatchable.append(task)
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_task_queue_split.py tests/test_orchestrator.py -v`
Expected: PASS.

- [ ] **Step 6: Mutation-check the positional invariant**

Temporarily change `tasks.append(data)` in `core/leaf_split.py` to
`tasks.insert(1, data)`.
Run: `uv run pytest tests/test_task_queue_split.py -v -k "appends_rows_in_plan_order or dispatchable_after"`
Expected: FAIL. Restore and re-run to confirm PASS.

- [ ] **Step 7: Mutation-check the reduced retry budget**

Temporarily change the child INSERT's trailing `2)` to `1)`.
Run: `uv run pytest tests/test_task_queue_split.py::test_split_children_start_with_a_reduced_retry_budget -v`
Expected: FAIL. Restore and re-run to confirm PASS.

- [ ] **Step 8: Commit**

```bash
git add src/orchestrator/core/task_queue.py tests/test_task_queue_split.py
git commit -m "feat(queue): add supersede, split insertion, and implementer pinning

Children append to both the graph and the task table so the positional
mapping in get_dispatchable_tasks stays valid; SUPERSEDED now counts as
done for all_tasks_done and as a satisfied dependency."
```

---

### Task 15: Audit every SUPERSEDED consumer

**Files:**
- Modify: `src/orchestrator/core/orchestrator_reconcile.py`
- Modify: `src/mcp_server/server.py`
- Modify: `web/app.js`
- Modify: `web/styles.css`
- Test: `tests/test_superseded_consumers.py`

**Depends on:** Task 14

- [ ] **Step 1: Enumerate the consumers**

Run this and read every hit before writing any code:

```bash
grep -rn "TaskStatus\." src/ | grep -v SUPERSEDED
grep -rn "merged\|failed\|terminal" src/mcp_server/server.py
grep -n "statusOrder" web/app.js
```

Every site that branches on "is this task finished" is a candidate. The known
set is: `TaskQueue.get_dispatchable_tasks` and `all_tasks_done` (both done in
Task 14), `ReconcileMixin.reconcile_runs`, the MCP `is_terminal_status` and
`derive_terminal_incomplete_state`, and the dashboard `statusOrder` map.
`core/branch_sweeper.dead_branches` needs no change: it already reclaims any
branch with no open PR and no live run, which is exactly right for an abandoned
split parent.

- [ ] **Step 2: Write the failing test**

Create `tests/test_superseded_consumers.py`:

```python
"""SUPERSEDED must be terminal everywhere, not just in status_vocab.

A status one consumer treats as terminal and another treats as in-flight is
how a split plan wedges. This is the sweep.
"""

from pathlib import Path

import pytest

from orchestrator.core.status_vocab import TERMINAL_STATUSES
from orchestrator.models.schemas import TaskStatus


REPO = Path(__file__).resolve().parents[1]


@pytest.mark.unit
def test_superseded_is_terminal_in_the_vocabulary():
    assert TaskStatus.SUPERSEDED.value in TERMINAL_STATUSES


@pytest.mark.unit
def test_mcp_treats_superseded_as_terminal():
    from mcp_server.server import is_terminal_status

    assert is_terminal_status("superseded") is True


@pytest.mark.unit
def test_mcp_terminal_incomplete_ignores_a_superseded_parent():
    from mcp_server.server import derive_terminal_incomplete_state

    tasks = [
        {"id": "1", "title": "A", "status": "merged"},
        {"id": "2", "title": "B", "status": "superseded"},
        {"id": "3", "title": "B1", "status": "merged"},
    ]
    # A superseded parent is not a failure, so the plan is cleanly complete.
    assert derive_terminal_incomplete_state("completed", tasks) is None


@pytest.mark.unit
def test_the_dashboard_status_map_includes_superseded():
    content = (REPO / "web" / "app.js").read_text(encoding="utf-8")
    assert "superseded" in content
```

Before running, read `src/mcp_server/server.py:168` (`is_terminal_status`) and
`:297` (`derive_terminal_incomplete_state`) and match the test's call signatures
to the real ones. Adjust the test to the real signatures; never change a
production signature to satisfy a test.

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run pytest tests/test_superseded_consumers.py -v`
Expected: at least `test_mcp_treats_superseded_as_terminal` FAILs.

- [ ] **Step 4: Fix each consumer**

- `src/mcp_server/server.py`: add `"superseded"` to the terminal set
  `is_terminal_status` checks; exclude superseded tasks from the failed count in
  `derive_terminal_incomplete_state`. If `_TASK_STATUS_MAP` does not pass
  unknown values through unchanged, add `"superseded": "superseded"`.
- `src/orchestrator/core/orchestrator_reconcile.py`: in `reconcile_runs`, once
  the run's task has been fetched, skip and close out any run whose task is
  `TaskStatus.SUPERSEDED`. A superseded parent's in-flight container is
  abandoned work, not a run to retry:

```python
                if task is not None and task["status"] == TaskStatus.SUPERSEDED:
                    # The leaf was replaced by split children; its container is
                    # abandoned work, not a run to retry.
                    await self._tq.finish_agent_run(run["id"], "stopped", "")
                    continue
```

  Read the module first and use its real helper name for finishing a run and its
  real loop variable names.
- `web/app.js`: add `superseded` to the `statusOrder` map.
- `web/styles.css`: add a muted badge rule for `.status-superseded`, matching the
  existing status badge rules.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_superseded_consumers.py tests/test_status_vocab.py tests/test_mcp_server.py -v`
Expected: PASS.

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest -q`
Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/ web/ tests/test_superseded_consumers.py
git commit -m "fix: treat SUPERSEDED as terminal in every consumer

Reconcile stops retrying an abandoned split parent's container, the MCP
surface stops counting a superseded parent as a partial-plan failure, and
the dashboard renders the status."
```

---

### Task 16: Wire triage into the review failure path

**Files:**
- Modify: `src/orchestrator/core/orchestrator_review.py:258-282`
- Modify: `tests/conftest.py`
- Test: `tests/test_orchestrator_triage.py`

**Depends on:** Task 13, Task 14, Task 15

- [ ] **Step 1: Add the two shared fixtures**

Append to `tests/conftest.py`. Model the construction on the existing
orchestrator setup in `tests/test_orchestrator.py`; read that file first and
reuse its stub shapes rather than inventing new ones.

```python
@pytest.fixture
async def captured_events(event_bus):
    """Collect every event published during a test."""
    seen: list[dict] = []
    event_bus.subscribe_sync(seen.append)  # match the real EventBus API
    return seen


@pytest.fixture
async def orchestrator_fixture(test_db, event_bus):
    """An Orchestrator with one task parked in REVIEWING and a failing review.

    Yields (orchestrator, task_id, project_dict).
    """
    from unittest.mock import AsyncMock

    from orchestrator.core.orchestrator import Orchestrator
    from orchestrator.core.task_queue import TaskQueue
    from orchestrator.models.schemas import TaskStatus

    tq = TaskQueue(test_db)
    await test_db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url, default_branch, "
        "model_name, harness, max_retries) VALUES "
        "('proj1', 'u1', 'p', 'https://github.com/o/r', 'main', "
        "'qwen3.6-27b', 'opencode', 3)"
    )
    plan_id = await tq.create_plan("proj1", "test")
    await tq.activate_plan(
        plan_id,
        {
            "tasks": [
                {
                    "id": "a",
                    "slug": "a",
                    "title": "A",
                    "description": "Add the widget",
                    "depends_on": [],
                    "plan_text": "## Goal\nAdd it.\n## Files\nsrc/a.py\n"
                    "## Steps\n1. go\n## Acceptance\n`pytest`",
                    "leaf_type": "function_add",
                }
            ]
        },
        "plan/x",
    )
    rows = await tq.get_tasks_for_plan(plan_id)
    task_id = rows[0]["id"]
    await tq.set_task_pr_url(task_id, "https://github.com/o/r/pull/1")
    await tq.update_task_status(task_id, TaskStatus.REVIEWING)

    git = AsyncMock()
    git.extract_pr_number.return_value = 1
    git.repo_slug.return_value = "o/r"
    git.get_pr_diff.return_value = "diff --git a/src/a.py b/src/a.py\n+x\n"
    git.clone_pr_head.side_effect = RuntimeError("no clone in tests")

    opus = AsyncMock()
    opus.is_available.return_value = True
    opus.review_diff.return_value = {"verdict": "fail", "feedback": "nope"}

    settings = AsyncMock()
    settings.implement_escalation.return_value = []
    settings.max_leaves_per_plan.return_value = 24

    orch = Orchestrator(
        task_queue=tq,
        agent_manager=AsyncMock(),
        opus_bridge=opus,
        git_ops=git,
        event_bus=event_bus,
        effective_settings=settings,
        llm_router=AsyncMock(),
    )
    project = await tq.get_project("proj1")
    return orch, task_id, project
```

Read `tests/conftest.py` for the real names of the database and event-bus
fixtures and the real `EventBus` subscribe API before writing this; substitute
the real names.

- [ ] **Step 2: Write the failing test**

Create `tests/test_orchestrator_triage.py`:

```python
"""Triage fires on the SECOND worker-attributable failure, once per leaf.

Failure 1 keeps the cheap existing retry-with-feedback.
"""

from unittest.mock import AsyncMock

import pytest

from orchestrator.models.schemas import LeafTask, TaskStatus, TriageDecision


def _children() -> list[LeafTask]:
    return [
        LeafTask(id="c1", title="One", plan_text="Goal: one"),
        LeafTask(id="c2", title="Two", plan_text="Goal: two"),
    ]


@pytest.mark.unit
async def test_first_failure_retries_without_calling_triage(orchestrator_fixture):
    orch, task_id, project = orchestrator_fixture
    orch._triage_leaf = AsyncMock()
    await orch.review_task(task_id, project)
    orch._triage_leaf.assert_not_awaited()
    task = await orch._tq.get_task(task_id)
    assert task["status"] == TaskStatus.PENDING
    assert task["attempt"] == 2


@pytest.mark.unit
async def test_second_failure_calls_triage(orchestrator_fixture):
    orch, task_id, project = orchestrator_fixture
    await orch._tq.retry_task(task_id)
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)
    orch._triage_leaf = AsyncMock(
        return_value=TriageDecision(decision="retry", reason="one more")
    )
    await orch.review_task(task_id, project)
    orch._triage_leaf.assert_awaited_once()


@pytest.mark.unit
async def test_triage_runs_at_most_once_per_leaf(orchestrator_fixture):
    orch, task_id, project = orchestrator_fixture
    await orch._tq.retry_task(task_id)
    await orch._tq.record_triage_decision(task_id, "retry")
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)
    orch._triage_leaf = AsyncMock()
    await orch.review_task(task_id, project)
    orch._triage_leaf.assert_not_awaited()


@pytest.mark.unit
async def test_split_supersedes_the_parent_and_inserts_children(orchestrator_fixture):
    orch, task_id, project = orchestrator_fixture
    await orch._tq.retry_task(task_id)
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)
    orch._triage_leaf = AsyncMock(
        return_value=TriageDecision(
            decision="split", reason="two concerns", children=_children()
        )
    )
    await orch.review_task(task_id, project)

    parent = await orch._tq.get_task(task_id)
    assert parent["status"] == TaskStatus.SUPERSEDED
    rows = await orch._tq.get_tasks_for_plan(parent["plan_id"])
    assert sum(1 for r in rows if r["parent_task_id"] == task_id) == 2


@pytest.mark.unit
async def test_split_publishes_a_task_split_event(orchestrator_fixture, captured_events):
    orch, task_id, project = orchestrator_fixture
    await orch._tq.retry_task(task_id)
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)
    orch._triage_leaf = AsyncMock(
        return_value=TriageDecision(
            decision="split", reason="two concerns", children=_children()
        )
    )
    await orch.review_task(task_id, project)
    assert any(e.get("type") == "task_split" for e in captured_events)


@pytest.mark.unit
async def test_a_split_child_may_never_split_again(orchestrator_fixture):
    orch, task_id, project = orchestrator_fixture
    await orch._tq._db.execute(
        "UPDATE tasks SET parent_task_id = 'some-parent' WHERE id = ?", (task_id,)
    )
    await orch._tq.retry_task(task_id)
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)
    orch._triage_leaf = AsyncMock(
        return_value=TriageDecision(
            decision="split", reason="two concerns", children=_children()
        )
    )
    await orch.review_task(task_id, project)
    task = await orch._tq.get_task(task_id)
    assert task["status"] == TaskStatus.FAILED
    rows = await orch._tq.get_tasks_for_plan(task["plan_id"])
    assert all(r["parent_task_id"] != task_id for r in rows)


@pytest.mark.unit
async def test_escalate_pins_the_next_implementer_and_requeues(orchestrator_fixture):
    orch, task_id, project = orchestrator_fixture
    await orch._tq.retry_task(task_id)
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)
    orch._effective_settings.implement_escalation.return_value = [
        {"harness": "agy", "model": "gemini-3.6-flash-high"}
    ]
    orch._triage_leaf = AsyncMock(
        return_value=TriageDecision(decision="escalate", reason="capability ceiling")
    )
    await orch.review_task(task_id, project)

    task = await orch._tq.get_task(task_id)
    assert task["status"] == TaskStatus.PENDING
    assert task["implement_harness"] == "agy"
    assert task["implement_model"] == "gemini-3.6-flash-high"
    assert task["escalation_index"] == 1


@pytest.mark.unit
async def test_escalate_with_an_empty_ladder_parks_terminal(orchestrator_fixture):
    orch, task_id, project = orchestrator_fixture
    await orch._tq.retry_task(task_id)
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)
    orch._effective_settings.implement_escalation.return_value = []
    orch._triage_leaf = AsyncMock(
        return_value=TriageDecision(decision="escalate", reason="ceiling")
    )
    await orch.review_task(task_id, project)
    task = await orch._tq.get_task(task_id)
    assert task["status"] == TaskStatus.FAILED


@pytest.mark.unit
async def test_human_decision_parks_terminal_with_the_reason(orchestrator_fixture):
    orch, task_id, project = orchestrator_fixture
    await orch._tq.retry_task(task_id)
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)
    orch._triage_leaf = AsyncMock(
        return_value=TriageDecision(decision="human", reason="ambiguous contract")
    )
    await orch.review_task(task_id, project)
    task = await orch._tq.get_task(task_id)
    assert task["status"] == TaskStatus.FAILED
    assert "ambiguous contract" in (task["review_feedback"] or "")


@pytest.mark.unit
async def test_retry_decision_threads_the_refined_prompt_into_progress_note(
    orchestrator_fixture,
):
    orch, task_id, project = orchestrator_fixture
    await orch._tq.retry_task(task_id)
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)
    orch._triage_leaf = AsyncMock(
        return_value=TriageDecision(
            decision="retry",
            reason="misread the command",
            refined_prompt="Run only tests/test_widget.py",
        )
    )
    await orch.review_task(task_id, project)
    task = await orch._tq.get_task(task_id)
    assert task["status"] == TaskStatus.PENDING
    assert "Run only tests/test_widget.py" in (task["progress_note"] or "")
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run pytest tests/test_orchestrator_triage.py -v`
Expected: FAIL. `test_second_failure_calls_triage` fails because `_triage_leaf`
is never awaited.

- [ ] **Step 4: Add the imports**

In `src/orchestrator/core/orchestrator_review.py`, add:

```python
from orchestrator.core.capability_events import TaskEscalatedEvent, TaskSplitEvent
from orchestrator.core.escalation import next_escalation
from orchestrator.core.leaf_triage import TriageEvidence, triage_leaf
from orchestrator.models.schemas import TaskStatus, TriageDecision
```

(extend the existing `from orchestrator.models.schemas import TaskStatus` line
rather than adding a second import of the same module).

- [ ] **Step 5: Replace the failure block in `review_task`**

Replace everything from `fail_class = (` to the end of `review_task` with:

```python
        fail_class = (
            FailureClass.VERIFY_FAIL.value
            if "Automated verification failed" in feedback
            else FailureClass.FIXABLE_IN_PLACE.value
        )
        await _record("fail", fail_class)
        await self._git.comment_on_pr(".", pr_number, feedback, repo=repo)
        await self._tq.fail_task(task_id, feedback)

        attempt = int(task["attempt"])
        max_retries = int(project["max_retries"])

        # Adaptive triage: the FIRST worker-attributable failure keeps the cheap
        # retry-with-feedback path (ADaPT: decompose only when the executor
        # actually fails). From the SECOND on, ask the brain whether the leaf
        # should be retried, split, escalated, or handed to a human. Bounded to
        # one triage call per leaf lifetime by tasks.triage_decision.
        already_triaged = bool(task.get("triage_decision"))
        if attempt >= 2 and not already_triaged and self._llm_router is not None:
            if await self._run_leaf_triage(
                task, project, plan, feedback, files_touched, loc_delta, diff
            ):
                return

        if attempt < max_retries:
            await self._tq.retry_task(task_id)
            self._bus.publish(
                {"type": "task_retry", "task_id": task_id, "attempt": attempt + 1}
            )
        else:
            self._bus.publish(
                {"type": "task_failed", "task_id": task_id, "feedback": feedback}
            )
```

- [ ] **Step 6: Add the triage handler**

Add these two methods to `ReviewMixin`, immediately after `review_task`:

```python
    async def _triage_leaf(
        self, evidence: TriageEvidence, project_id: str | None
    ) -> TriageDecision:
        """Seam for the triage brain call, so tests can substitute it."""
        return await triage_leaf(evidence, self._llm_router, project_id)

    async def _run_leaf_triage(
        self,
        task: dict[str, Any],
        project: dict[str, Any],
        plan: dict[str, Any] | None,
        feedback: str,
        files_touched: int,
        loc_delta: int,
        diff: str,
    ) -> bool:
        """Triage a twice-failed leaf and act on the decision.

        Returns:
            True when the decision was handled here, so the caller must NOT
            fall through to the plain retry path; False to keep the old
            behavior (no plan graph to work against).
        """
        if plan is None:
            return False

        task_id = task["id"]
        branch_name: str = task["branch_name"]
        task_slug = (
            branch_name[len("agent/") :]
            if branch_name.startswith("agent/")
            else branch_name
        )

        plan_task: dict[str, Any] = {}
        graph_task_count = 0
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            parsed = json.loads(plan.get("opus_plan") or "{}")
            graph_tasks = parsed.get("tasks", [])
            graph_task_count = len(graph_tasks)
            for candidate in graph_tasks:
                if candidate.get("slug") == task_slug:
                    plan_task = candidate
                    break

        settings = self._effective_settings
        profile = await settings.capability_profile(
            project_id=None, model=project.get("model_name")
        )
        ladder = await settings.implement_escalation()
        ceiling = await settings.max_leaves_per_plan()
        escalation_index = int(task.get("escalation_index") or 0)
        pair = next_escalation(ladder, escalation_index)
        # A split child may never split again (one generation), and escalation
        # needs an untried rung.
        is_split_child = task.get("parent_task_id") is not None

        evidence = TriageEvidence(
            task_slug=task_slug,
            leaf_type=str(
                task.get("leaf_type") or plan_task.get("leaf_type") or "generic"
            ),
            plan_text=str(plan_task.get("plan_text") or task["description"]),
            profile=profile,
            attempts=[
                {
                    "attempt": int(task["attempt"]),
                    "files_touched": files_touched,
                    "loc_delta": loc_delta,
                    "diff": diff,
                    "verify_exit_code": 1,
                    "verify_tail": feedback[-_VERIFY_OUTPUT_MAX:],
                    "review_reason": feedback,
                }
            ],
            difficulty_score=task.get("difficulty_score"),
            remaining_leaf_budget=(
                0 if is_split_child else max(int(ceiling) - graph_task_count, 0)
            ),
            escalation_available=pair is not None,
        )

        decision = await self._triage_leaf(evidence, project["id"])
        await self._tq.record_triage_decision(task_id, decision.decision)

        if decision.decision == "split" and not is_split_child and decision.children:
            children = decision.children
            child_ids = await self._tq.insert_split_children(
                plan["id"], task_id, task_slug, children
            )
            await self._tq.supersede_task(task_id, "split", decision.reason)
            slugs = [f"{task_slug}-s{i}" for i in range(1, len(children) + 1)]
            self._bus.publish(
                {
                    "type": "task_split",
                    "task_id": task_id,
                    "child_task_ids": child_ids,
                    "child_slugs": slugs,
                    "reason": decision.reason,
                }
            )
            emitter = getattr(self, "_emitter", None)
            if emitter is not None:
                await emitter.emit(
                    TaskSplitEvent(
                        plan_id=plan["id"],
                        parent_slug=task_slug,
                        child_slugs=slugs,
                        failure_evidence_ref=task_id,
                    )
                )
            return True

        if decision.decision == "escalate" and pair is not None:
            await self._tq.set_task_implementer(
                task_id, pair.harness, pair.model, escalation_index + 1
            )
            await self._tq.retry_task(task_id)
            self._bus.publish(
                {
                    "type": "task_escalated",
                    "task_id": task_id,
                    "from_model": project.get("model_name"),
                    "to_model": pair.model,
                    "to_harness": pair.harness,
                    "reason": decision.reason,
                }
            )
            emitter = getattr(self, "_emitter", None)
            if emitter is not None:
                await emitter.emit(
                    TaskEscalatedEvent(
                        plan_id=plan["id"],
                        leaf_slug=task_slug,
                        policy=f"{pair.harness}/{pair.model}",
                    )
                )
            return True

        if decision.decision == "retry":
            if decision.refined_prompt:
                await self._tq.append_progress_note(
                    task_id,
                    "TRIAGE CORRECTION (act on this now):\n"
                    f"{decision.refined_prompt}",
                )
            await self._tq.retry_task(task_id)
            self._bus.publish(
                {
                    "type": "task_retry",
                    "task_id": task_id,
                    "attempt": int(task["attempt"]) + 1,
                    "triage": "retry",
                }
            )
            return True

        # "human", or a split/escalate the caller cannot honour: park terminal.
        await self._tq.fail_task(task_id, f"Triage: {decision.reason}\n\n{feedback}")
        self._bus.publish(
            {
                "type": "task_failed",
                "task_id": task_id,
                "feedback": decision.reason,
                "triage": decision.decision,
            }
        )
        return True
```

- [ ] **Step 7: Run the test to verify it passes**

Run: `uv run pytest tests/test_orchestrator_triage.py -v`
Expected: PASS (10 tests).

- [ ] **Step 8: Mutation-check the once-per-leaf bound**

Temporarily change `already_triaged = bool(task.get("triage_decision"))` to
`already_triaged = False`.
Run: `uv run pytest tests/test_orchestrator_triage.py::test_triage_runs_at_most_once_per_leaf -v`
Expected: FAIL. Restore and re-run to confirm PASS.

- [ ] **Step 9: Mutation-check the one-split-generation bound**

Temporarily remove `and not is_split_child` from the split branch condition.
Run: `uv run pytest tests/test_orchestrator_triage.py::test_a_split_child_may_never_split_again -v`
Expected: FAIL. Restore and re-run to confirm PASS.

- [ ] **Step 10: Mutation-check the first-failure path**

Temporarily change `if attempt >= 2` to `if attempt >= 1`.
Run: `uv run pytest tests/test_orchestrator_triage.py::test_first_failure_retries_without_calling_triage -v`
Expected: FAIL. Restore and re-run to confirm PASS.

- [ ] **Step 11: Run the full suite**

Run: `uv run pytest -q`
Expected: all tests pass. Existing `test_orchestrator.py` review tests exercise
the first-failure path only (attempt 1), which is unchanged.

- [ ] **Step 12: Commit**

```bash
git add src/orchestrator/core/orchestrator_review.py tests/test_orchestrator_triage.py tests/conftest.py
git commit -m "feat(triage): split, escalate, or park a twice-failed leaf

Failure 1 keeps the cheap retry-with-feedback; failure 2 triages once per
leaf lifetime. Splits supersede the parent and fan its dependents out to
the children; escalations pin the next implementer pair."
```

---

### Task 17: Honour the pinned implementer at dispatch and in attribution

**Files:**
- Modify: `src/orchestrator/core/orchestrator_dispatch.py:99-160`
- Modify: `src/orchestrator/core/orchestrator_review.py:172-188`
- Test: `tests/test_dispatch_escalation.py`

**Depends on:** Task 16

- [ ] **Step 1: Write the failing test**

Create `tests/test_dispatch_escalation.py`:

```python
"""An escalated leaf spawns with the pinned pair, and its outcome credits it.

The implement seat is spawn-baked, so escalation only takes effect if dispatch
reads the pinned columns. Attribution must follow the same columns or the
calibration loop learns lies.
"""

import pytest

from orchestrator.models.schemas import TaskStatus


@pytest.mark.unit
async def test_dispatch_uses_the_project_defaults_when_nothing_is_pinned(
    orchestrator_fixture,
):
    orch, task_id, project = orchestrator_fixture
    await orch._tq.update_task_status(task_id, TaskStatus.PENDING)
    task = await orch._tq.get_task(task_id)
    orch._agents.spawn_agent.return_value = "container-1"
    await orch.dispatch_pending_tasks(task["plan_id"], project)
    kwargs = orch._agents.spawn_agent.await_args.kwargs
    assert kwargs["model_name"] == project["model_name"]
    assert kwargs["harness"] == project["harness"]


@pytest.mark.unit
async def test_dispatch_uses_the_pinned_pair_when_escalated(orchestrator_fixture):
    orch, task_id, project = orchestrator_fixture
    await orch._tq.update_task_status(task_id, TaskStatus.PENDING)
    await orch._tq.set_task_implementer(
        task_id, "agy", "gemini-3.6-flash-high", index=1
    )
    task = await orch._tq.get_task(task_id)
    orch._agents.spawn_agent.return_value = "container-1"
    await orch.dispatch_pending_tasks(task["plan_id"], project)
    kwargs = orch._agents.spawn_agent.await_args.kwargs
    assert kwargs["model_name"] == "gemini-3.6-flash-high"
    assert kwargs["harness"] == "agy"


@pytest.mark.unit
async def test_an_escalated_outcome_records_the_actual_implementer(
    orchestrator_fixture,
):
    orch, task_id, project = orchestrator_fixture
    await orch._tq.set_task_implementer(
        task_id, "agy", "gemini-3.6-flash-high", index=1
    )
    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "ok"}
    await orch.review_task(task_id, project)
    rows = await orch._tq._db.fetch_all(
        "SELECT * FROM task_outcomes WHERE task_id = ?", (task_id,)
    )
    assert rows
    assert rows[-1]["model_name"] == "gemini-3.6-flash-high"
    assert rows[-1]["harness"] == "agy"


@pytest.mark.unit
async def test_a_non_escalated_outcome_still_records_the_project_worker(
    orchestrator_fixture,
):
    orch, task_id, project = orchestrator_fixture
    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "ok"}
    await orch.review_task(task_id, project)
    rows = await orch._tq._db.fetch_all(
        "SELECT * FROM task_outcomes WHERE task_id = ?", (task_id,)
    )
    assert rows[-1]["harness"] == "opencode"
```

The `orchestrator_fixture` `_agents` is an `AsyncMock`, so `spawn_agent` is
already awaitable; set its `return_value` per test as shown.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_dispatch_escalation.py -v`
Expected: `test_dispatch_uses_the_pinned_pair_when_escalated` and
`test_an_escalated_outcome_records_the_actual_implementer` FAIL.

- [ ] **Step 3: Read the pinned pair at dispatch**

In `src/orchestrator/core/orchestrator_dispatch.py`, inside the
`for task in dispatchable:` loop, replace the line
`harness_id = project.get("harness") or default_harness_id()` with:

```python
            # An escalated leaf carries its own implementer: the implement seat
            # is spawn-baked, so escalation only takes effect here. Falling back
            # to the project defaults keeps every non-escalated dispatch
            # byte-identical to its pre-escalation behavior.
            harness_id = (
                task.get("implement_harness")
                or project.get("harness")
                or default_harness_id()
            )
            worker_model = task.get("implement_model") or project["model_name"]
```

and change the spawn argument `model_name=project["model_name"],` to
`model_name=worker_model,`.

- [ ] **Step 4: Attribute the outcome to the actual implementer**

In `src/orchestrator/core/orchestrator_review.py`, in the `_record` helper
inside `review_task`, change the two attribution arguments to:

```python
                # Attribution follows the model that ACTUALLY implemented this
                # attempt. Crediting the original worker with an escalated
                # success teaches the calibration loop a lie.
                model_name=(
                    task.get("implement_model")
                    or project.get("agent_model")
                    or project.get("model_name")
                ),
                harness=task.get("implement_harness") or project.get("harness"),
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_dispatch_escalation.py -v`
Expected: PASS.

- [ ] **Step 6: Mutation-check the attribution**

Temporarily revert `model_name=` to
`project.get("agent_model") or project.get("model_name")`.
Run: `uv run pytest tests/test_dispatch_escalation.py::test_an_escalated_outcome_records_the_actual_implementer -v`
Expected: FAIL. Restore and re-run to confirm PASS.

- [ ] **Step 7: Run the gate**

```bash
uv run ruff format src/ tests/
uv run ruff check --fix src/ tests/
uv run mypy src/ --ignore-missing-imports
uv run pytest --cov=orchestrator --cov-fail-under=80 -q
```

Expected: all clean.

- [ ] **Step 8: Commit**

```bash
git add src/orchestrator/core/orchestrator_dispatch.py src/orchestrator/core/orchestrator_review.py tests/test_dispatch_escalation.py
git commit -m "feat(escalation): spawn with the pinned implementer and attribute to it

Dispatch reads implement_harness/implement_model with a fallback to the
project defaults, so non-escalated dispatches are unchanged; the outcome
recorder follows the same two columns."
```

---

### Task 18: Close out Phase B

**Files:**
- Modify: `web/app.js`
- Modify: `docs/gotchas.md`
- Modify: `CLAUDE.md`

**Depends on:** Task 17

- [ ] **Step 1: Surface the two new events in the dashboard**

In `web/app.js`, in the SSE event handler, add `task_split` and `task_escalated`
cases that append an activity-feed line, matching the shape of the existing
`task_retry` case. A split line should read
`Split <slug> into <n> leaves: <reason>`; an escalation line should read
`Escalated <slug> to <to_harness>/<to_model>: <reason>`.

- [ ] **Step 2: Add the gotchas**

Append to `docs/gotchas.md`:

```markdown
- **Triage fires once per leaf, on the SECOND worker-attributable failure** 
  failure 1 keeps the cheap `retry_task` path; from failure 2 on,
  `ReviewMixin._run_leaf_triage` asks `leaf_failure_triage` for a
  `TriageDecision`. The bound is durable, not in-memory: `tasks.triage_decision`
  is stamped before the decision is acted on, and its presence blocks any later
  triage. `tasks.parent_task_id` is the one-split-generation guard, so a split
  child can never split again (the code also zeroes its remaining leaf budget so
  the brain is not even asked). A router exception or two unparseable answers
  both fall back to `human`: triage is an optimization over the existing retry
  path and must never be able to wedge a task. Provider errors never reach
  triage at all; they take the respawn-cap path and burn no retry.
- **Split children APPEND to both the graph and the task table, and the parent
  is never deleted**: `TaskQueue.get_dispatchable_tasks` maps
  `opus_plan["tasks"]` to `get_tasks_for_plan` rows BY LIST INDEX, so inserting
  a child anywhere but the end, or removing the superseded parent, silently
  re-associates every task after it with the wrong row. `core/leaf_split.py` is
  written around this invariant and `tests/test_leaf_split.py` mutation-checks
  it. A split parent goes to `SUPERSEDED`, which both `all_tasks_done` and the
  dependency predicate in `get_dispatchable_tasks` count as satisfied; without
  that a split plan can never complete and its children never dispatch. Children
  start at `attempt = 2`, so they inherit the remaining retry budget rather than
  resetting it.
- **Escalation is a dispatch-time substitution, never a router fallback**: the
  implement seat is spawn-baked, so `LLMRouter` cannot fall back for it.
  `core/escalation.next_escalation` walks the ordered `implement_escalation`
  list in `config/praxis.yaml` using `tasks.escalation_index`, and
  `DispatchMixin` reads `tasks.implement_harness`/`implement_model` at spawn.
  `record_outcome` reads the SAME two columns: crediting the original worker
  with an escalated success teaches the calibration loop a lie. `config/praxis.yaml`
  is baked into the orchestrator image until the product plan mounts it, so an
  escalation-ladder edit needs an orchestrator IMAGE REBUILD, not a restart.
```

- [ ] **Step 3: Add the CLAUDE.md index lines**

Add three matching one-line entries to the CLAUDE.md gotchas index, in the
project's existing terse style.

- [ ] **Step 4: Verify the gate**

```bash
uv run ruff format src/ tests/
uv run ruff check --fix src/ tests/
uv run mypy src/ --ignore-missing-imports
uv run pytest --cov=orchestrator --cov-fail-under=80 -q
```

- [ ] **Step 5: Commit**

```bash
git add web/app.js docs/gotchas.md CLAUDE.md
git commit -m "docs: index the triage, split, and escalation gotchas"
```

**Phase B is complete.** Continue directly to Phase C.

---

## Phase C: pre-dispatch difficulty scoring

### Task 19: `core/difficulty.py`, features and the transparent scorer

**Files:**
- Create: `src/orchestrator/core/difficulty.py`
- Modify: `config/praxis.yaml`
- Modify: `src/orchestrator/core/effective_settings.py`
- Test: `tests/test_difficulty.py`

**Depends on:** Task 18

- [ ] **Step 1: Write the failing test**

Create `tests/test_difficulty.py`:

```python
"""Pre-dispatch difficulty scoring: cheap features, transparent weights.

Evidence this is tractable: problem text plus repo state plus test features
predict success at AUC about 0.85 pre-execution (Agent Psychometrics, arXiv
2604.00594). The v1 scorer is an explicitly hand-weighted placeholder for the
learned Beta-posterior scorer (CADMAS-CTX, arXiv 2604.17950), which swaps in
behind the DifficultyScorer protocol.
"""

import pytest

from orchestrator.core.difficulty import (
    DEFAULT_BIAS,
    DEFAULT_WEIGHTS,
    DifficultyFeatures,
    LogisticScorer,
    extract_features,
)
from orchestrator.models.schemas import CapabilityProfile, LeafTask


def _profile() -> CapabilityProfile:
    return CapabilityProfile(
        model_name="m",
        parameter_count_b=30,
        context_window=8192,
        max_files_touched=5,
        max_loc_delta=300,
        max_dep_depth=3,
    )


def _leaf(**overrides) -> LeafTask:
    base = {
        "id": "t1",
        "title": "Add a helper",
        "plan_text": (
            "## Goal\nAdd it.\n## Files\nsrc/a.py\n## Steps\n1. go\n"
            "## Acceptance\nRun `uv run pytest tests/test_a.py`"
        ),
        "files": ["src/a.py"],
        "estimated_loc": 40,
        "verification": "Run `uv run pytest tests/test_a.py` and confirm it passes",
        "leaf_type": "function_add",
    }
    base.update(overrides)
    return LeafTask(**base)


@pytest.mark.unit
def test_features_count_declared_files():
    f = extract_features(_leaf(files=["a.py", "b.py", "c.py"]), _profile())
    assert f.files_touched == 3


@pytest.mark.unit
def test_loc_ratio_is_relative_to_the_profile_limit():
    f = extract_features(_leaf(estimated_loc=150), _profile())
    assert f.loc_ratio == pytest.approx(0.5)


@pytest.mark.unit
def test_a_missing_loc_estimate_is_treated_as_the_full_limit():
    """An unstated size is a worst case, never a free pass."""
    f = extract_features(_leaf(estimated_loc=None), _profile())
    assert f.loc_ratio == pytest.approx(1.0)


@pytest.mark.unit
def test_has_acceptance_is_true_for_a_runnable_verification():
    assert extract_features(_leaf(), _profile()).has_acceptance is True


@pytest.mark.unit
def test_has_acceptance_is_false_for_prose_only_verification():
    leaf = _leaf(verification="Look at the page and check it renders correctly")
    assert extract_features(leaf, _profile()).has_acceptance is False


@pytest.mark.unit
def test_generic_type_flag_is_set_only_for_generic():
    assert extract_features(_leaf(leaf_type="generic"), _profile()).generic_type is True
    assert extract_features(_leaf(), _profile()).generic_type is False


@pytest.mark.unit
def test_context_ratio_uses_the_plan_text_against_the_leaf_budget():
    long_leaf = _leaf(plan_text="x" * 40_000)
    f = extract_features(long_leaf, _profile())
    assert f.context_ratio > 1.0


@pytest.mark.unit
def test_historical_success_defaults_to_the_neutral_prior_without_history():
    f = extract_features(_leaf(), _profile(), historical_success=None)
    assert f.historical_success == pytest.approx(0.5)


@pytest.mark.unit
def test_dep_depth_comes_from_the_caller():
    f = extract_features(_leaf(), _profile(), dep_depth=2)
    assert f.dep_depth == 2


@pytest.mark.unit
def test_score_is_bounded_to_the_unit_interval():
    scorer = LogisticScorer(DEFAULT_WEIGHTS, DEFAULT_BIAS)
    extreme = DifficultyFeatures(
        files_touched=99,
        loc_ratio=99.0,
        dep_depth=99,
        has_acceptance=False,
        context_ratio=99.0,
        historical_success=0.0,
        repo_size_bucket=2,
        generic_type=True,
    )
    assert 0.0 <= scorer.score(extreme) <= 1.0


@pytest.mark.unit
def test_a_small_well_shaped_leaf_scores_above_the_flag_threshold():
    scorer = LogisticScorer(DEFAULT_WEIGHTS, DEFAULT_BIAS)
    good = extract_features(_leaf(), _profile(), dep_depth=0, historical_success=0.8)
    assert scorer.score(good) >= 0.55


@pytest.mark.unit
def test_an_oversized_leaf_scores_below_the_reject_threshold():
    scorer = LogisticScorer(DEFAULT_WEIGHTS, DEFAULT_BIAS)
    bad = extract_features(
        _leaf(
            files=["a.py", "b.py", "c.py", "d.py", "e.py", "f.py", "g.py"],
            estimated_loc=900,
            verification="Eyeball the dashboard",
            leaf_type="generic",
        ),
        _profile(),
        dep_depth=3,
        historical_success=0.15,
    )
    assert scorer.score(bad) < 0.35


@pytest.mark.unit
def test_more_files_never_raises_the_score():
    scorer = LogisticScorer(DEFAULT_WEIGHTS, DEFAULT_BIAS)
    small = extract_features(_leaf(files=["a.py"]), _profile())
    large = extract_features(_leaf(files=["a.py", "b.py", "c.py", "d.py"]), _profile())
    assert scorer.score(large) <= scorer.score(small)


@pytest.mark.unit
def test_losing_the_acceptance_check_never_raises_the_score():
    scorer = LogisticScorer(DEFAULT_WEIGHTS, DEFAULT_BIAS)
    with_check = extract_features(_leaf(), _profile())
    without = extract_features(_leaf(verification="Look at it"), _profile())
    assert scorer.score(without) <= scorer.score(with_check)


@pytest.mark.unit
def test_the_scorer_satisfies_the_protocol():
    from orchestrator.core.difficulty import DifficultyScorer

    scorer: DifficultyScorer = LogisticScorer(DEFAULT_WEIGHTS, DEFAULT_BIAS)
    assert callable(scorer.score)


@pytest.mark.unit
async def test_effective_settings_reads_the_weights_and_thresholds(test_db):
    from orchestrator.config import Settings
    from orchestrator.core.effective_settings import EffectiveSettings

    settings = EffectiveSettings(Settings(auth_token="t", _env_file=None), test_db)
    config = await settings.difficulty_config()
    assert set(config["weights"]) == set(DEFAULT_WEIGHTS)
    assert 0.0 < config["reject_below"] < config["flag_below"] < 1.0
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_difficulty.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.core.difficulty'`.

- [ ] **Step 3: Write the module**

Create `src/orchestrator/core/difficulty.py`:

```python
"""Pre-dispatch difficulty scoring for a decomposed leaf.

Predict, before spawning a container, whether a leaf is likely beyond the
worker; act on the prediction; record it so the Capability Calibration Loop can
learn real weights later.  Agent Psychometrics (arXiv 2604.00594) shows problem
text plus repo state plus test features predict success at AUC about 0.85
pre-execution, and that model and scaffold contribute additively.

The v1 scorer is a transparent hand-weighted logistic.  It is EXPLICITLY a
placeholder for the learned Beta-posterior scorer (CADMAS-CTX, arXiv
2604.17950); ``DifficultyScorer`` exists so the learned implementation swaps in
without touching a single call site.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Protocol

from orchestrator.core.token_budget import (
    WORKER_RESERVE_FRACTION,
    estimate_tokens,
)
from orchestrator.models.schemas import CapabilityProfile, LeafTask, LeafType


# A verification carries a machine-checkable acceptance signal when it names a
# runnable command. Deliberately the same shape as the F3 validator's
# _RUNNABLE_SIGNAL: one definition of "runnable" across the engine.
_RUNNABLE_SIGNAL = re.compile(
    r"`[^`]+`"
    r"|\b(pytest|uv\s+run|npm|pnpm|yarn|make|go\s+test|cargo|ruff|mypy|tox|"
    r"pre-commit|python3?\s+-m|bash\s|sh\s|\./)",
    re.IGNORECASE,
)

# Hand-set v1 weights, all in log-odds. Signs are the load-bearing part and are
# grounded: more files and more LOC lower success (SWE-bench Goes Live!, arXiv
# 2505.23419); a runnable acceptance check raises it (MAKER, arXiv 2511.09030);
# past success on this shape raises it. Magnitudes are calibration food for the
# benchmark, not claims.
DEFAULT_WEIGHTS: dict[str, float] = {
    "files_touched": -0.45,
    "loc_ratio": -1.10,
    "dep_depth": -0.35,
    "has_acceptance": 1.30,
    "context_ratio": -1.40,
    "historical_success": 2.20,
    "repo_size_bucket": -0.25,
    "generic_type": -0.60,
}

# Chosen so a one-file, well-shaped, acceptance-carrying leaf with a neutral
# history lands comfortably above the flag threshold.
DEFAULT_BIAS: float = 1.60

# Neutral prior when this model has no attributable history for this shape.
_NEUTRAL_HISTORY = 0.5


@dataclass(frozen=True)
class DifficultyFeatures:
    """Cheap, pre-execution features. Nothing here runs code or clones a repo."""

    files_touched: int
    loc_ratio: float
    dep_depth: int
    has_acceptance: bool
    context_ratio: float
    historical_success: float
    repo_size_bucket: int
    generic_type: bool

    def as_vector(self) -> dict[str, float]:
        """Return the features as a name-to-float map for the linear model."""
        return {
            "files_touched": float(self.files_touched),
            "loc_ratio": float(self.loc_ratio),
            "dep_depth": float(self.dep_depth),
            "has_acceptance": 1.0 if self.has_acceptance else 0.0,
            "context_ratio": float(self.context_ratio),
            "historical_success": float(self.historical_success),
            "repo_size_bucket": float(self.repo_size_bucket),
            "generic_type": 1.0 if self.generic_type else 0.0,
        }


class DifficultyScorer(Protocol):
    """Anything that turns features into P(success) in [0, 1]."""

    def score(self, features: DifficultyFeatures) -> float:
        """Return the predicted probability the worker completes this leaf."""
        ...


class LogisticScorer:
    """Transparent hand-weighted logistic. The v1 placeholder scorer."""

    def __init__(self, weights: dict[str, float], bias: float) -> None:
        self._weights = dict(weights)
        self._bias = bias

    def score(self, features: DifficultyFeatures) -> float:
        """Return sigma(bias + w . x), clamped to [0, 1] by construction."""
        vector = features.as_vector()
        logit = self._bias + sum(
            self._weights.get(name, 0.0) * value for name, value in vector.items()
        )
        # Guard the exponent so an extreme feature cannot raise OverflowError.
        logit = max(min(logit, 60.0), -60.0)
        return 1.0 / (1.0 + math.exp(-logit))


def extract_features(
    leaf: LeafTask,
    profile: CapabilityProfile,
    *,
    dep_depth: int = 0,
    historical_success: float | None = None,
    repo_size_bucket: int = 0,
) -> DifficultyFeatures:
    """Compute the v1 feature vector for one leaf.

    Args:
        leaf: The validated leaf task.
        profile: The worker's capability profile, supplying the denominators.
        dep_depth: This leaf's depth in the plan DAG (from the F3 depth map).
        historical_success: Observed pass rate for this (model, project, shape),
            or None for the neutral prior.
        repo_size_bucket: 0 for under 100 files, 1 for 100 to 500, 2 for 500+.

    Returns:
        The feature vector.  Nothing here runs a command or clones anything.
    """
    loc_limit = max(int(profile.max_loc_delta), 1)
    # An unstated size is a worst case, never a free pass: an unestimated leaf
    # is exactly the leaf the planner did not think about.
    estimated_loc = leaf.estimated_loc if leaf.estimated_loc is not None else loc_limit

    per_leaf_budget = max(
        int(profile.context_window * (1 - WORKER_RESERVE_FRACTION)), 1
    )
    context_tokens = estimate_tokens(leaf.plan_text or "")

    return DifficultyFeatures(
        files_touched=len(leaf.files),
        loc_ratio=estimated_loc / loc_limit,
        dep_depth=dep_depth,
        has_acceptance=bool(
            leaf.verification and _RUNNABLE_SIGNAL.search(leaf.verification)
        ),
        context_ratio=context_tokens / per_leaf_budget,
        historical_success=(
            _NEUTRAL_HISTORY if historical_success is None else historical_success
        ),
        repo_size_bucket=repo_size_bucket,
        generic_type=leaf.leaf_type is LeafType.GENERIC,
    )


def build_scorer(config: dict[str, Any]) -> DifficultyScorer:
    """Build the configured scorer from a settings dict.

    Unknown weight names in operator YAML are ignored rather than raising: a
    typo must degrade the score, never wedge decomposition.
    """
    weights = {**DEFAULT_WEIGHTS, **(config.get("weights") or {})}
    bias = float(config.get("bias", DEFAULT_BIAS))
    return LogisticScorer(weights, bias)
```

- [ ] **Step 4: Add the config**

In `config/praxis.yaml`, after the `max_leaves_per_plan` key, add:

```yaml
# Pre-dispatch difficulty scoring. PROVISIONAL hand-set weights (log-odds); the
# Capability Calibration Loop replaces them with learned per-(model, project)
# values. Signs are grounded in the literature; magnitudes are not claims.
# See docs/decomposition-standard.md section 2.
difficulty:
  bias: 1.60
  weights:
    files_touched: -0.45
    loc_ratio: -1.10
    dep_depth: -0.35
    has_acceptance: 1.30
    context_ratio: -1.40
    historical_success: 2.20
    repo_size_bucket: -0.25
    generic_type: -0.60
  # p_success below reject_below: the leaf goes back to the planner.
  # Between reject_below and flag_below: dispatch, but flagged and tightened.
  reject_below: 0.35
  flag_below: 0.55
```

- [ ] **Step 5: Add the settings accessor**

In `src/orchestrator/core/effective_settings.py`, add after `max_leaves_per_plan`:

```python
    async def difficulty_config(self) -> dict[str, Any]:
        """Return the difficulty scorer's weights, bias, and gate thresholds.

        Falls back to the module defaults key by key, so a partial YAML block
        (or none at all) still produces a usable scorer.
        """
        from orchestrator.core.difficulty import DEFAULT_BIAS, DEFAULT_WEIGHTS

        yaml_data = await self._get_yaml()
        raw = yaml_data.get("difficulty") or {}
        if not isinstance(raw, dict):
            raw = {}
        weights = {**DEFAULT_WEIGHTS}
        for name, value in (raw.get("weights") or {}).items():
            if name in weights:
                weights[name] = float(value)
        return {
            "weights": weights,
            "bias": float(raw.get("bias", DEFAULT_BIAS)),
            "reject_below": float(raw.get("reject_below", 0.35)),
            "flag_below": float(raw.get("flag_below", 0.55)),
        }
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `uv run pytest tests/test_difficulty.py -v`
Expected: PASS (16 tests). If
`test_a_small_well_shaped_leaf_scores_above_the_flag_threshold` or
`test_an_oversized_leaf_scores_below_the_reject_threshold` fails, adjust
`DEFAULT_BIAS` (and the matching `bias` in the YAML) until both hold. Do not
weaken the thresholds in the test: they are the spec's numbers.

- [ ] **Step 7: Mutation-check the monotonicity guards**

Temporarily flip `"files_touched": -0.45` to `+0.45`.
Run: `uv run pytest tests/test_difficulty.py::test_more_files_never_raises_the_score -v`
Expected: FAIL. Restore and re-run to confirm PASS. Repeat with
`"has_acceptance"` flipped to `-1.30` against
`test_losing_the_acceptance_check_never_raises_the_score`.

- [ ] **Step 8: Mutation-check the missing-estimate default**

Temporarily change the `estimated_loc` fallback from `loc_limit` to `0`.
Run: `uv run pytest tests/test_difficulty.py::test_a_missing_loc_estimate_is_treated_as_the_full_limit -v`
Expected: FAIL. Restore and re-run to confirm PASS.

- [ ] **Step 9: Commit**

```bash
git add src/orchestrator/core/difficulty.py src/orchestrator/core/effective_settings.py config/praxis.yaml tests/test_difficulty.py
git commit -m "feat(difficulty): add pre-dispatch leaf difficulty scoring

Cheap pre-execution features and a transparent hand-weighted logistic
behind a DifficultyScorer protocol, so the learned Beta-posterior scorer
swaps in without touching call sites. Weights are provisional."
```

---

### Task 20: Add the two new capability events

**Files:**
- Modify: `src/orchestrator/core/capability_events.py`
- Test: `tests/test_capability_event_models.py`

**Depends on:** Task 19

- [ ] **Step 1: Write the failing test**

Append to `tests/test_capability_event_models.py`:

```python
@pytest.mark.unit
def test_leaf_difficulty_scored_event_round_trips():
    from orchestrator.core.capability_events import LeafDifficultyScoredEvent

    event = LeafDifficultyScoredEvent(
        plan_id="p1",
        leaf_slug="add-widget",
        p_success=0.42,
        features={"files_touched": 2.0, "loc_ratio": 0.5},
    )
    payload = event.model_dump()
    assert payload["event_type"] == "leaf_difficulty_scored"
    assert payload["schema_version"] == 1
    assert LeafDifficultyScoredEvent.model_validate(payload) == event


@pytest.mark.unit
def test_leaf_rejected_predispatch_event_round_trips():
    from orchestrator.core.capability_events import LeafRejectedPredispatchEvent

    event = LeafRejectedPredispatchEvent(
        plan_id="p1",
        leaf_slug="add-widget",
        p_success=0.19,
        failing_features=["loc_ratio", "files_touched"],
    )
    payload = event.model_dump()
    assert payload["event_type"] == "leaf_rejected_predispatch"
    assert LeafRejectedPredispatchEvent.model_validate(payload) == event


@pytest.mark.unit
def test_both_new_types_are_in_the_registry():
    from orchestrator.core.capability_events import CAPABILITY_EVENT_TYPES

    assert "leaf_difficulty_scored" in CAPABILITY_EVENT_TYPES
    assert "leaf_rejected_predispatch" in CAPABILITY_EVENT_TYPES


@pytest.mark.unit
def test_the_registry_matches_the_union_members():
    """The frozenset and the union must never drift apart."""
    import typing

    from orchestrator.core.capability_events import (
        CAPABILITY_EVENT_TYPES,
        CapabilityEventModel,
    )

    members = typing.get_args(CapabilityEventModel)
    declared = {
        typing.get_args(m.model_fields["event_type"].annotation)[0] for m in members
    }
    assert declared == set(CAPABILITY_EVENT_TYPES)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_capability_event_models.py -v`
Expected: FAIL with `ImportError: cannot import name 'LeafDifficultyScoredEvent'`.

- [ ] **Step 3: Add the models**

In `src/orchestrator/core/capability_events.py`, add after `LeafRejectedEvent`:

```python
class LeafDifficultyScoredEvent(_CapabilityEvent):
    """Recorded for every leaf that passes F3 and gets a difficulty score.

    This is the calibration loop's training data: the feature vector and the
    prediction, joined later against the leaf's actual ``task_outcomes`` row.
    """

    event_type: Literal["leaf_difficulty_scored"] = "leaf_difficulty_scored"
    plan_id: str
    leaf_slug: str
    p_success: float
    features: dict[str, float] = Field(default_factory=dict)
    flagged: bool = False


class LeafRejectedPredispatchEvent(_CapabilityEvent):
    """Recorded when a leaf is rejected on its score before any container runs."""

    event_type: Literal["leaf_rejected_predispatch"] = "leaf_rejected_predispatch"
    plan_id: str
    leaf_slug: str
    p_success: float
    failing_features: list[str] = Field(default_factory=list)
```

Add both to the union:

```python
CapabilityEventModel = (
    DecomposeInputEvent
    | LeafValidatedEvent
    | LeafRejectedEvent
    | LeafDifficultyScoredEvent
    | LeafRejectedPredispatchEvent
    | PlanRejectedEvent
    | TaskSplitEvent
    | TaskEscalatedEvent
    | OutcomeRecordedEvent
)
```

and to the registry frozenset:

```python
        "leaf_difficulty_scored",
        "leaf_rejected_predispatch",
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_capability_event_models.py tests/test_capability_event_emitter.py -v`
Expected: PASS.

- [ ] **Step 5: Mutation-check the registry drift guard**

Temporarily remove `"leaf_difficulty_scored"` from `CAPABILITY_EVENT_TYPES`.
Run: `uv run pytest tests/test_capability_event_models.py::test_the_registry_matches_the_union_members -v`
Expected: FAIL. Restore and re-run to confirm PASS.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/core/capability_events.py tests/test_capability_event_models.py
git commit -m "feat(capability-events): add the two difficulty decision records

Plus a drift guard asserting the union and the type registry stay in sync."
```

---

### Task 21: Gate decomposition on the difficulty score

**Files:**
- Modify: `src/orchestrator/core/execute_plan_decompose.py:197-382`
- Test: `tests/test_execute_plan_difficulty.py`

**Depends on:** Task 19, Task 20

- [ ] **Step 1: Write the failing test**

Create `tests/test_execute_plan_difficulty.py`:

```python
"""Difficulty scoring runs after F3 and gates dispatch.

< reject_below: back to the planner with the failing features named, sharing
F3's 2-round budget. Between reject_below and flag_below: dispatch, flagged,
acceptance mandatory. >= flag_below: normal.
"""

import json
from unittest.mock import AsyncMock

import pytest

from orchestrator.core.execute_plan_decompose import decompose_plan
from orchestrator.core.plan_review import PlanReviewError
from orchestrator.models.schemas import CapabilityProfile


PLAN = """### Task 1: Add the widget

Add a widget to the module.
"""


def _leaf(**overrides) -> dict:
    base = {
        "id": "t1",
        "title": "Add the widget",
        "description": "Add a widget.",
        "plan_text": (
            "## Goal\nAdd a widget.\n## Files\nsrc/a.py\n## Steps\n1. go\n"
            "## Acceptance\nRun `uv run pytest tests/test_a.py`"
        ),
        "depends_on": [],
        "checklist": [{"text": "add it"}],
        "files": ["src/a.py"],
        "task_type": "feature",
        "estimated_loc": 40,
        "verification": "Run `uv run pytest tests/test_a.py` and confirm it passes",
        "leaf_type": "function_add",
    }
    base.update(overrides)
    return base


def _response(*leaves: dict) -> str:
    return json.dumps({"tasks": list(leaves)})


class _Router:
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    async def run(self, call_site, prompt, project_id=None, cwd=None):
        self.prompts.append(prompt)
        return self.responses[min(len(self.prompts) - 1, len(self.responses) - 1)]


def _settings() -> AsyncMock:
    settings = AsyncMock()
    settings.capability_profile.return_value = CapabilityProfile(
        model_name="m", parameter_count_b=30, context_window=8192
    )
    settings.difficulty_config.return_value = {
        "weights": __import__(
            "orchestrator.core.difficulty", fromlist=["DEFAULT_WEIGHTS"]
        ).DEFAULT_WEIGHTS,
        "bias": 1.60,
        "reject_below": 0.35,
        "flag_below": 0.55,
    }
    return settings


@pytest.mark.unit
async def test_a_healthy_leaf_dispatches_and_carries_its_score():
    router = _Router(_response(_leaf()))
    plan = await decompose_plan(
        PLAN, "m", None, router, _settings(), project_id="p1"
    )
    task = plan["tasks"][0]
    assert 0.0 <= task["difficulty_score"] <= 1.0
    assert task["difficulty_score"] >= 0.55
    assert task.get("difficulty_flagged") is False


@pytest.mark.unit
async def test_a_borderline_leaf_is_flagged_but_still_dispatched():
    borderline = _leaf(files=["a.py", "b.py", "c.py"], estimated_loc=220)
    router = _Router(_response(borderline))
    plan = await decompose_plan(
        PLAN, "m", None, router, _settings(), project_id="p1"
    )
    task = plan["tasks"][0]
    assert 0.35 <= task["difficulty_score"] < 0.55
    assert task["difficulty_flagged"] is True


@pytest.mark.unit
async def test_a_hopeless_leaf_is_re_asked_with_its_failing_features():
    hopeless = _leaf(
        files=["a.py", "b.py", "c.py", "d.py", "e.py"],
        estimated_loc=900,
        leaf_type="generic",
    )
    router = _Router(_response(hopeless), _response(_leaf()))
    plan = await decompose_plan(
        PLAN, "m", None, router, _settings(), project_id="p1"
    )
    assert len(router.prompts) == 2
    second = router.prompts[1]
    assert "difficulty" in second.lower()
    assert "loc_ratio" in second
    # The corrected second answer is what gets returned.
    assert plan["tasks"][0]["difficulty_score"] >= 0.55


@pytest.mark.unit
async def test_a_hopeless_leaf_twice_rejects_the_whole_plan():
    hopeless = _leaf(
        files=["a.py", "b.py", "c.py", "d.py", "e.py"],
        estimated_loc=900,
        leaf_type="generic",
    )
    router = _Router(_response(hopeless))
    with pytest.raises(PlanReviewError, match="difficulty"):
        await decompose_plan(PLAN, "m", None, router, _settings(), project_id="p1")


@pytest.mark.unit
async def test_scoring_emits_one_event_per_leaf():
    emitter = AsyncMock()
    router = _Router(_response(_leaf()))
    await decompose_plan(
        PLAN,
        "m",
        None,
        router,
        _settings(),
        project_id="p1",
        plan_id="plan1",
        emitter=emitter,
    )
    types = [
        call.args[0].event_type
        for call in emitter.emit.await_args_list
        if call.args
    ]
    assert "leaf_difficulty_scored" in types


@pytest.mark.unit
async def test_a_predispatch_rejection_emits_its_own_event():
    emitter = AsyncMock()
    hopeless = _leaf(
        files=["a.py", "b.py", "c.py", "d.py", "e.py"],
        estimated_loc=900,
        leaf_type="generic",
    )
    router = _Router(_response(hopeless))
    with pytest.raises(PlanReviewError):
        await decompose_plan(
            PLAN,
            "m",
            None,
            router,
            _settings(),
            project_id="p1",
            plan_id="plan1",
            emitter=emitter,
        )
    types = [
        call.args[0].event_type
        for call in emitter.emit.await_args_list
        if call.args
    ]
    assert "leaf_rejected_predispatch" in types
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_execute_plan_difficulty.py -v`
Expected: FAIL with `KeyError: 'difficulty_score'`.

- [ ] **Step 3: Add the scoring pass**

In `src/orchestrator/core/execute_plan_decompose.py`, add the imports:

```python
from orchestrator.core.capability_events import (
    DecomposeInputEvent,
    LeafDifficultyScoredEvent,
    LeafRejectedEvent,
    LeafRejectedPredispatchEvent,
    LeafValidatedEvent,
    PlanRejectedEvent,
)
from orchestrator.core.difficulty import build_scorer, extract_features
from orchestrator.core.leaf_validator import (
    _max_dep_depth,
    format_violations_feedback,
    validate_leaves,
)
```

`_max_dep_depth` is currently private. Rename it to `max_dep_depth` in
`core/leaf_validator.py` (updating its two internal call sites and any test
that references the old name) so importing it across modules is not reaching
into a private helper.

Add a module-level helper after `normalize_slugs`:

```python
def _score_leaves(
    leaves: list[LeafTask],
    profile: Any,
    config: dict[str, Any],
    history_rate: float | None,
) -> tuple[dict[str, float], dict[str, list[str]]]:
    """Score every leaf and name the features dragging each one down.

    Returns:
        ``(slug -> p_success, slug -> failing feature names)``.  A feature is
        "failing" when its contribution to the logit is negative, so the
        re-ask feedback names the actual cause rather than restating the score.
    """
    scorer = build_scorer(config)
    weights = config["weights"]
    depths = max_dep_depth(leaves)
    scores: dict[str, float] = {}
    culprits: dict[str, list[str]] = {}
    for leaf in leaves:
        features = extract_features(
            leaf,
            profile,
            dep_depth=depths.get(leaf.id, 0),
            historical_success=history_rate,
        )
        scores[leaf.id] = scorer.score(features)
        vector = features.as_vector()
        culprits[leaf.id] = sorted(
            name
            for name, value in vector.items()
            if weights.get(name, 0.0) * value < 0
        )
    return scores, culprits
```

Inside `decompose_plan`, after the `validation_result.clean` / hard-violation
handling and before the `break`, add the difficulty gate. Concretely, replace
the block that currently reads:

```python
        if validation_result.clean:
            break
```

with:

```python
        difficulty_config = await effective_settings.difficulty_config()
        history_rate = _pass_rate(runs)
        scores, culprits = _score_leaves(
            leaves, profile, difficulty_config, history_rate
        )
        reject_below = difficulty_config["reject_below"]
        too_hard = [slug for slug, p in scores.items() if p < reject_below]

        if validation_result.clean and not too_hard:
            break

        if too_hard and attempt < _DECOMPOSE_ATTEMPTS:
            named = "; ".join(
                f"{slug} (p_success {scores[slug]:.2f}; worst features: "
                f"{', '.join(culprits[slug]) or 'none'})"
                for slug in too_hard
            )
            prompt = (
                f"{prompt}\n\nDIFFICULTY REJECTION: these leaves are predicted to "
                f"fail this worker: {named}. Re-decompose them smaller: fewer "
                "files, a smaller LOC estimate, a runnable acceptance command, "
                "and a specific leaf_type rather than 'generic'."
            )
            logger.warning(
                "Difficulty gate rejected %d leaf/leaves (attempt %d/%d): %s",
                len(too_hard),
                attempt,
                _DECOMPOSE_ATTEMPTS,
                named,
            )
            continue

        if too_hard:
            if emitter is not None and plan_id is not None:
                for slug in too_hard:
                    await emitter.emit(
                        LeafRejectedPredispatchEvent(
                            plan_id=plan_id,
                            leaf_slug=slug,
                            p_success=scores[slug],
                            failing_features=culprits[slug],
                        )
                    )
            msg = (
                "plan_rejected: difficulty gate rejected "
                f"{', '.join(too_hard)} after {attempt} rounds"
            )
            raise PlanReviewError(msg)
```

The existing hard-violation branch below it is unchanged.

Add the pass-rate helper next to `_score_leaves`:

```python
def _pass_rate(runs: list[dict[str, Any]]) -> float | None:
    """Observed pass rate across attributable outcome rows, or None if empty."""
    if not runs:
        return None
    passes = sum(1 for r in runs if r.get("outcome") == "pass")
    return passes / len(runs)
```

Finally, after the per-leaf `LeafValidatedEvent` emission block, stamp the
scores onto the returned tasks and emit the scoring events:

```python
    flag_below = difficulty_config["flag_below"]
    for task in opus_plan["tasks"]:
        slug = task["slug"]
        p_success = scores.get(slug)
        if p_success is None:
            continue
        flagged = p_success < flag_below
        task["difficulty_score"] = p_success
        task["difficulty_flagged"] = flagged
        if emitter is not None and plan_id is not None:
            await emitter.emit(
                LeafDifficultyScoredEvent(
                    plan_id=plan_id,
                    leaf_slug=slug,
                    p_success=p_success,
                    features={},
                    flagged=flagged,
                )
            )
```

- [ ] **Step 4: Persist the score on the task row**

In `src/orchestrator/core/task_queue.py`, extend `activate_plan`'s INSERT to
carry the two new per-task fields:

```python
            await self._db.execute(
                """INSERT INTO tasks
                   (id, plan_id, title, description, branch_name,
                    difficulty_score, leaf_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id,
                    plan_id,
                    task_data["title"],
                    task_data["description"],
                    f"agent/{task_data['slug']}",
                    task_data.get("difficulty_score"),
                    task_data.get("leaf_type"),
                ),
            )
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_execute_plan_difficulty.py tests/test_execute_plan_decompose.py -v`
Expected: PASS. Existing decompose tests build minimal leaves; any that now
score below `reject_below` must have their fixture leaves given a `files` list,
an `estimated_loc`, a runnable `verification`, and a non-generic `leaf_type`.
That is the correct fix: those fixtures were describing invalid leaves.

- [ ] **Step 6: Mutation-check the reject gate**

Temporarily change `too_hard = [slug for slug, p in scores.items() if p < reject_below]`
to `too_hard = []`.
Run: `uv run pytest tests/test_execute_plan_difficulty.py -v -k hopeless`
Expected: both hopeless tests FAIL. Restore and re-run to confirm PASS.

- [ ] **Step 7: Mutation-check the shared round budget**

Temporarily change the re-ask guard from `attempt < _DECOMPOSE_ATTEMPTS` to
`True`.
Run: `uv run pytest tests/test_execute_plan_difficulty.py::test_a_hopeless_leaf_twice_rejects_the_whole_plan -v`
Expected: FAIL (the loop never raises). Restore and re-run to confirm PASS.

- [ ] **Step 8: Run the full suite**

Run: `uv run pytest -q`
Expected: all tests pass.

- [ ] **Step 9: Commit**

```bash
git add src/orchestrator/core/execute_plan_decompose.py src/orchestrator/core/leaf_validator.py src/orchestrator/core/task_queue.py tests/test_execute_plan_difficulty.py
git commit -m "feat(difficulty): gate decomposition on the predicted success rate

Runs after F3 and shares its 2-round informed re-ask budget. Below
reject_below the leaf goes back to the planner with its failing features
named; between the thresholds it dispatches flagged; the score is stamped
on the task row and emitted as a capability event."
```

---

### Task 22: Feed the fresh score into triage and tighten flagged leaves

**Files:**
- Modify: `src/orchestrator/core/orchestrator_dispatch.py`
- Test: `tests/test_dispatch_flagged_leaf.py`

**Depends on:** Task 21

- [ ] **Step 1: Write the failing test**

Create `tests/test_dispatch_flagged_leaf.py`:

```python
"""A flagged leaf dispatches with a tightened pack and a visible flag.

Spec 2.3: between reject_below and flag_below the context pack is tightened to
priority order, an acceptance check becomes mandatory, and the flag is visible
on SSE.
"""

import pytest

from orchestrator.models.schemas import TaskStatus


@pytest.mark.unit
async def test_a_flagged_leaf_publishes_the_flag_on_dispatch(
    orchestrator_fixture, captured_events
):
    orch, task_id, project = orchestrator_fixture
    await orch._tq._db.execute(
        "UPDATE tasks SET difficulty_score = 0.44 WHERE id = ?", (task_id,)
    )
    await orch._tq.update_task_status(task_id, TaskStatus.PENDING)
    task = await orch._tq.get_task(task_id)
    orch._agents.spawn_agent.return_value = "container-1"
    await orch.dispatch_pending_tasks(task["plan_id"], project)
    dispatched = [e for e in captured_events if e.get("type") == "agent_dispatched"]
    assert dispatched
    assert dispatched[-1]["difficulty_flagged"] is True
    assert dispatched[-1]["difficulty_score"] == pytest.approx(0.44)


@pytest.mark.unit
async def test_an_unflagged_leaf_reports_flagged_false(
    orchestrator_fixture, captured_events
):
    orch, task_id, project = orchestrator_fixture
    await orch._tq._db.execute(
        "UPDATE tasks SET difficulty_score = 0.82 WHERE id = ?", (task_id,)
    )
    await orch._tq.update_task_status(task_id, TaskStatus.PENDING)
    task = await orch._tq.get_task(task_id)
    orch._agents.spawn_agent.return_value = "container-1"
    await orch.dispatch_pending_tasks(task["plan_id"], project)
    dispatched = [e for e in captured_events if e.get("type") == "agent_dispatched"]
    assert dispatched[-1]["difficulty_flagged"] is False


@pytest.mark.unit
async def test_an_unscored_leaf_is_not_flagged(orchestrator_fixture, captured_events):
    """A pre-Phase-C task row has a NULL score and must dispatch normally."""
    orch, task_id, project = orchestrator_fixture
    await orch._tq.update_task_status(task_id, TaskStatus.PENDING)
    task = await orch._tq.get_task(task_id)
    orch._agents.spawn_agent.return_value = "container-1"
    await orch.dispatch_pending_tasks(task["plan_id"], project)
    dispatched = [e for e in captured_events if e.get("type") == "agent_dispatched"]
    assert dispatched[-1]["difficulty_flagged"] is False


@pytest.mark.unit
async def test_a_flagged_leaf_gets_a_mandatory_acceptance_line_in_its_bible(
    orchestrator_fixture,
):
    orch, task_id, project = orchestrator_fixture
    await orch._tq._db.execute(
        "UPDATE tasks SET difficulty_score = 0.44 WHERE id = ?", (task_id,)
    )
    await orch._tq.update_task_status(task_id, TaskStatus.PENDING)
    task = await orch._tq.get_task(task_id)
    orch._agents.spawn_agent.return_value = "container-1"
    await orch.dispatch_pending_tasks(task["plan_id"], project)
    bible = orch._agents.spawn_agent.await_args.kwargs["bible_text"]
    assert "ACCEPTANCE" in bible
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_dispatch_flagged_leaf.py -v`
Expected: FAIL with `KeyError: 'difficulty_flagged'` on the dispatch event.

- [ ] **Step 3: Publish the flag and tighten the pack**

In `src/orchestrator/core/orchestrator_dispatch.py`, inside the dispatch loop
before the `spawn_agent` call, add:

```python
            # A leaf scored between reject_below and flag_below dispatches, but
            # with the flag visible and its acceptance check mandatory: finer
            # granularity must be paired with MORE verification, not less
            # (MAKER, arXiv 2511.09030).
            score = task.get("difficulty_score")
            flag_below = 0.55
            if self._effective_settings is not None:
                config = await self._effective_settings.difficulty_config()
                flag_below = float(config["flag_below"])
            flagged = score is not None and float(score) < flag_below
```

and extend the `agent_dispatched` publish:

```python
            self._bus.publish(
                {
                    "type": "agent_dispatched",
                    "plan_id": plan_id,
                    "task_id": task["id"],
                    "run_id": run_id,
                    "container_id": container_id,
                    "difficulty_score": score,
                    "difficulty_flagged": flagged,
                }
            )
```

In `_build_worker_bible`, make the acceptance slot mandatory for a flagged leaf
by adding, immediately before the `return build_bible(...)`:

```python
        acceptance = plan_task.get("verification") or project.get("verify_cmd")
        if acceptance is None and task.get("difficulty_score") is not None:
            # A flagged leaf with no acceptance signal is exactly the shape the
            # difficulty gate is warning about; fall back to the project verify
            # command rather than shipping a pack with no runnable check.
            acceptance = project.get("verify_cmd")
```

and pass `acceptance=acceptance` rather than the inline expression.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_dispatch_flagged_leaf.py -v`
Expected: PASS.

- [ ] **Step 5: Mutation-check the flag boundary**

Temporarily change `flagged = score is not None and float(score) < flag_below`
to `flagged = False`.
Run: `uv run pytest tests/test_dispatch_flagged_leaf.py::test_a_flagged_leaf_publishes_the_flag_on_dispatch -v`
Expected: FAIL. Restore and re-run to confirm PASS.

- [ ] **Step 6: Surface the flag in the dashboard**

In `web/app.js`, in the `agent_dispatched` handler, render a warning marker on
the task row when `difficulty_flagged` is true, with the score in the tooltip.
Add a matching `.difficulty-flagged` rule in `web/styles.css`.

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator/core/orchestrator_dispatch.py web/app.js web/styles.css tests/test_dispatch_flagged_leaf.py
git commit -m "feat(difficulty): flag borderline leaves at dispatch

The score is published on agent_dispatched and rendered on the task row;
a flagged leaf never ships without an acceptance check in its pack."
```

---

### Task 23: Close out Phase C

**Files:**
- Modify: `docs/decomposition-standard.md`
- Modify: `docs/gotchas.md`
- Modify: `CLAUDE.md`
- Test: `tests/test_decomposition_standard_doc.py`

**Depends on:** Task 22

- [ ] **Step 1: Document the scorer in the standard**

Append a section 7 to `docs/decomposition-standard.md`:

````markdown
## 7. Pre-dispatch difficulty scoring

Every leaf that passes F3 is scored before any container is spawned. Features
are cheap and pre-execution: declared files touched, LOC estimate against the
profile limit, dependency depth, whether the acceptance check is runnable,
context-pack tokens against the worker's per-leaf budget, historical pass rate
for this model, repo size bucket, and whether the leaf type is `generic`.

Scoring is a transparent hand-weighted logistic in `src/orchestrator/core/difficulty.py`,
with weights in `config/praxis.yaml` under `difficulty:`. The weights are
PROVISIONAL. Their signs are grounded (more files and more LOC lower success,
per arXiv 2505.23419; a runnable acceptance check raises it, per arXiv
2511.09030); their magnitudes are not claims. The Capability Calibration Loop
replaces them with learned per-(model, project) Beta-posterior estimates
(CADMAS-CTX, arXiv 2604.17950), swapping in behind the `DifficultyScorer`
protocol without touching any call site.

Gates:

| `p_success` | Behavior |
|-------------|----------|
| below `reject_below` (0.35) | The leaf goes back to the planner with its failing features named. Shares F3's two informed rounds; a second failure rejects the whole plan. |
| `reject_below` to `flag_below` (0.35 to 0.55) | Dispatch proceeds, but the leaf is flagged: the acceptance check becomes mandatory in the context pack and the flag is visible on SSE and the dashboard. |
| at or above `flag_below` | Normal dispatch. |

The prediction, the feature vector, and any pre-dispatch rejection are all
recorded as capability events (`leaf_difficulty_scored`,
`leaf_rejected_predispatch`), joined later against the leaf's `task_outcomes`
row. That join is the calibration loop's training set.
````

- [ ] **Step 2: Extend the standard-doc test**

Append to `tests/test_decomposition_standard_doc.py`:

```python
@pytest.mark.unit
def test_standard_doc_documents_the_difficulty_thresholds():
    text = DOC.read_text(encoding="utf-8")
    assert "reject_below" in text
    assert "flag_below" in text
    assert "0.35" in text
    assert "0.55" in text


@pytest.mark.unit
def test_standard_doc_names_the_scorer_weights_as_provisional():
    text = DOC.read_text(encoding="utf-8")
    assert "PROVISIONAL" in text or "provisional" in text
```

Run: `uv run pytest tests/test_decomposition_standard_doc.py -v`
Expected: PASS.

- [ ] **Step 3: Add the gotcha**

Append to `docs/gotchas.md`:

```markdown
- **Difficulty scoring runs AFTER F3 and shares its round budget** 
  `execute_plan_decompose` scores every leaf that survives validation; a leaf
  under `difficulty.reject_below` sends the whole decomposition back to the
  brain with its failing feature names, using the SAME `_DECOMPOSE_ATTEMPTS`
  budget as the F3 informed re-ask. A second failure raises `PlanReviewError`
  and no invalid graph is ever dispatched. The v1 weights in
  `config/praxis.yaml` are hand-set and explicitly provisional: their SIGNS are
  literature-grounded, their magnitudes are not, and the Capability Calibration
  Loop replaces them behind the `DifficultyScorer` protocol. A leaf with no
  `estimated_loc` is scored as if it used the whole LOC budget, on purpose: an
  unstated size is the leaf the planner did not think about, not a free pass.
```

- [ ] **Step 4: Add the CLAUDE.md index line and retire the stale S1 note**

Add a one-line entry for the gotcha above. Also correct the existing CLAUDE.md
line that claims "the emitter is STILL a stub; no production caller constructs
`CapabilityEventEmitter(` yet": `Orchestrator.__init__` has constructed one
since Plan 3, and Phases B and C add `task_split`, `task_escalated`,
`leaf_difficulty_scored`, and `leaf_rejected_predispatch` as production
emissions. Replace it with a line stating that the S1 stub is retired.

- [ ] **Step 5: Run the whole gate**

```bash
uv run ruff format src/ tests/
uv run ruff check --fix src/ tests/
uv run mypy src/ --ignore-missing-imports
uv run pytest --cov=orchestrator --cov-report=term-missing --cov-fail-under=80 -q
```

Expected: ruff clean, mypy `Success`, pytest green at or above 80 percent.

- [ ] **Step 6: Rebuild and smoke-test the running orchestrator**

`config/praxis.yaml` is baked into the orchestrator image, so the new
`implement_escalation`, `max_leaves_per_plan`, and `difficulty` blocks need an
image rebuild (the product plan's Phase A retires this):

```bash
PRAXIS_BUILD_SHA=$(git rev-parse --short HEAD) docker compose -f docker-compose.yml -f docker-compose.local.yml up --build -d
curl -s http://localhost:12323/health
```

Expected: the health payload's build sha matches `git rev-parse --short HEAD`.
Then confirm migration 7 applied:

```bash
docker exec orchestrator python -c "import sqlite3; c=sqlite3.connect('data/orchestrator.db'); print(c.execute('PRAGMA user_version').fetchone())"
```

Expected: `(7,)`.

- [ ] **Step 7: Commit**

```bash
git add docs/decomposition-standard.md docs/gotchas.md CLAUDE.md tests/test_decomposition_standard_doc.py
git commit -m "docs: document difficulty scoring and retire the S1 stub note"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1, Task 2 (both `Depends on: None`)
- **Wave 2:** Task 3 (Task 2)
- **Wave 3:** Task 4 (Task 3), Task 6 (Task 1)
- **Wave 4:** Task 5 (Tasks 3, 4)
- **Wave 5:** Task 7 (Tasks 1 through 6), Phase A gate
- **Wave 6:** Task 8 (Task 7)
- **Wave 7:** Task 9 (Task 8), Task 12 (Task 8)
- **Wave 8:** Task 10 (Task 9), Task 11 (Task 9)
- **Wave 9:** Task 13 (Tasks 9, 10, 11, 12), Task 14 (Tasks 8, 11)
- **Wave 10:** Task 15 (Task 14)
- **Wave 11:** Task 16 (Tasks 13, 14, 15)
- **Wave 12:** Task 17 (Task 16)
- **Wave 13:** Task 18 (Task 17), Phase B gate
- **Wave 14:** Task 19 (Task 18)
- **Wave 15:** Task 20 (Task 19)
- **Wave 16:** Task 21 (Tasks 19, 20)
- **Wave 17:** Task 22 (Task 21)
- **Wave 18:** Task 23 (Task 22), Phase C gate

Tasks 2 and 1 are genuinely independent (Task 1's doc test for leaf types stays
red until Task 2 lands, which is stated in Task 1 Step 4). Tasks 12 and 9 touch
disjoint files and can run concurrently. Everything else is sequential because
it edits the same three modules.

## Definition of done for this plan

Mapped from the umbrella spec's section 10:

1. `docs/decomposition-standard.md` exists, is cited, is linked from the README,
   and F3 enforces its template and ordering rules. (Tasks 1, 3, 4, 5, 6, 7)
2. A leaf that fails twice is triaged; splits and escalations happen live and
   are visible as SSE events; every bound holds under a mutation-checked test.
   (Tasks 8 through 18)
3. Every dispatched leaf carries a difficulty score; scores, splits, and
   escalations land in `capability_events`. (Tasks 19 through 23)

Items 4 through 7 of the spec's definition of done belong to the benchmark and
product plans.

