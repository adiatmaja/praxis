# Capability Contracts Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the four foundation contracts (S2 `LeafTask`, S6 failure taxonomy, S9 status vocabulary, S1 capability decision records) as pure schemas plus infrastructure, with zero behavior change to the orchestration loop.

**Architecture:** Every seam the later capability-engine plans build on becomes a versioned Pydantic model or a single canonical enum module, each with fixture tests. `LeafTask` replaces the `setdefault`-shaped dict in `parse_review_response` while preserving today's exact output shape (new fields are additive and defaulted). The failure taxonomy and status vocabulary are new single-source modules that existing call sites import instead of re-declaring literals. The capability decision records get Pydantic models, a `capability_events` table added through the versioned migration framework, and a dual-writing emitter that is fully tested but has no production call site yet (Plans 2/3/5 wire the emitters in).

**Tech Stack:** Python 3.11, Pydantic v2, aiosqlite, pytest (`asyncio_mode = "auto"`), ruff (88 cols), mypy.

---

## Required reading before starting

- `docs/gotchas.md` — read the DB migration, schemas, and MCP sections before touching `database.py`, `models/schemas.py`, or `src/mcp_server/server.py`.
- `docs/superpowers/specs/2026-07-11-capability-engine-roadmap.md` — sections 2.3 (baseline), 6 (S1, S2, S6, S9), 8 (Plan 1 row).

## Scope guardrails (do not violate)

- **No behavior change to the loop.** `activate_plan`, `get_dispatchable_tasks`, dispatch, review, and merge must behave identically before and after this plan. New model fields are additive with defaults; new modules have no production caller except the MCP status-alias refactor (Task 4), which must be behavior-preserving.
- **Emitters are stubbed.** Task 7 delivers the emitter class and its tests only. Do NOT add `emit(...)` calls into `orchestrator_*.py`, `execute_plan_decompose.py`, or `plan_review.py`; those belong to Plans 2/3/5.
- **Every schema gets a fixture test.** TDD per task: failing test first.
- No em dashes in any prose, comment, docstring, or commit message. Use a comma, colon, semicolon, or "so"/"then".
- 88-char ruff line length; mypy clean (`uv run mypy src/orchestrator/ --ignore-missing-imports`).

## Drift notes discovered while verifying section 2.3

- `parse_review_response` currently defaults exactly five keys and preserves any extra keys the brain sends. `LeafTask` must reproduce both (defaults + pass-through) via `extra="allow"`, or the decompose output shape changes.
- `TaskStatus`/`PlanStatus` already exist as `StrEnum` in `schemas.py`; S9 is a *consolidation* (kill the duplicate literals in `server.py`/`app.js`), not a fresh definition. `SUPERSEDED` is added now so Plan 5 does not churn the enum, but nothing produces it yet.
- No `tasks.failure_class` column exists. Plan 1 delivers the *enum only*; the column is Plan 5. Do not add it here.
- `MIGRATIONS` ends at version 3; the new table is Migration 4. `CURRENT_SCHEMA_VERSION` derives from `MIGRATIONS[-1].version`, so it updates automatically.

---

## File Structure

- `src/orchestrator/models/schemas.py` — add `LeafTask` + `LeafChecklistItem` + `LEAF_SCHEMA_VERSION` (S2); add `TaskStatus.SUPERSEDED` (S9). Touched by Task 1 and Task 4 (serialized via dependency).
- `src/orchestrator/core/plan_review.py` — rewrite `parse_review_response` to build `LeafTask` (S2).
- `src/orchestrator/core/failure_taxonomy.py` — new; `FailureClass` enum + `counts_against_worker` (S6).
- `src/orchestrator/core/status_vocab.py` — new; canonical status groupings + MCP aliases (S9).
- `src/mcp_server/server.py` — import status groupings/aliases from `status_vocab` instead of local literals (S9).
- `src/orchestrator/core/capability_events.py` — new; decision-record Pydantic models (S1, Task 5) + `CapabilityEventEmitter` (S1, Task 7).
- `src/orchestrator/database.py` — add Migration 4 for `capability_events` (S1).
- `tests/fixtures/decompose/` — new golden fixtures (S2).
- `tests/test_*.py` — one test module per task.

---

### Task 1: LeafTask model replacing the setdefault dict (S2)

**Files:**
- Modify: `src/orchestrator/models/schemas.py` (add near `CapabilityProfile`, after line 71)
- Modify: `src/orchestrator/core/plan_review.py:113-152` (`parse_review_response`)
- Test: `tests/test_leaf_task.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_leaf_task.py
"""Fixture tests for the S2 LeafTask contract."""

from __future__ import annotations

import pytest

from orchestrator.core.plan_review import PlanReviewError, parse_review_response
from orchestrator.models.schemas import LEAF_SCHEMA_VERSION, LeafTask


def test_leaf_defaults_derive_from_title() -> None:
    leaf = LeafTask(id="t1", title="Add endpoint")
    assert leaf.schema_version == LEAF_SCHEMA_VERSION
    assert leaf.description == "Add endpoint"
    assert leaf.plan_text == "Add endpoint"
    assert leaf.checklist == [{"text": "Add endpoint"}] or [
        c.model_dump() for c in leaf.checklist
    ] == [{"text": "Add endpoint"}]
    assert leaf.depends_on == []
    assert leaf.needs_stronger_model is False
    # F2 fields exist as defaulted, frozen-now-populated-later contract slots:
    assert leaf.files == []
    assert leaf.task_type is None
    assert leaf.estimated_loc is None
    assert leaf.verification is None


def test_parse_review_response_shape_is_preserved() -> None:
    raw = (
        '{"tasks": [{"id": "t1", "title": "Add x", '
        '"depends_on": ["t0"], "needs_stronger_model": true, "extra": "keep"}]}'
    )
    out = parse_review_response(raw)
    task = out["tasks"][0]
    assert task["id"] == "t1"
    assert task["title"] == "Add x"
    assert task["description"] == "Add x"
    assert task["plan_text"] == "Add x"
    assert task["depends_on"] == ["t0"]
    assert task["checklist"] == [{"text": "Add x"}]
    assert task["needs_stronger_model"] is True
    # extra keys the brain sends are preserved (matches pre-LeafTask behavior):
    assert task["extra"] == "keep"
    # additive F2 + version slots now present:
    assert task["schema_version"] == LEAF_SCHEMA_VERSION
    assert task["files"] == []


def test_parse_review_response_rejects_missing_id() -> None:
    with pytest.raises(PlanReviewError):
        parse_review_response('{"tasks": [{"title": "no id"}]}')
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_leaf_task.py -v`
Expected: FAIL with `ImportError: cannot import name 'LeafTask'`.

- [ ] **Step 3: Add the LeafTask model to schemas.py**

Add the `model_validator` import to the existing pydantic import line:

```python
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
```

Insert after `CapabilityProfile` (after line 71, before `class ProjectCreate`):

```python
LEAF_SCHEMA_VERSION = 1


class LeafChecklistItem(BaseModel):
    """One ordered step inside a leaf's checklist."""

    text: str


class LeafTask(BaseModel):
    """Versioned decomposition-leaf contract (S2).

    Replaces the ad-hoc ``setdefault`` dict shaped in
    ``plan_review.parse_review_response``. ``extra="allow"`` preserves any
    keys the brain sends that are not modeled yet, matching the pre-model
    pass-through behavior. The F2 fields (``files``, ``task_type``,
    ``estimated_loc``, ``verification``) are additive contract slots frozen
    now and populated by Plan 2's prompt; defaulting them here changes no
    loop behavior.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: int = LEAF_SCHEMA_VERSION
    id: str
    title: str
    description: str = ""
    plan_text: str = ""
    depends_on: list[str] = Field(default_factory=list)
    checklist: list[LeafChecklistItem] = Field(default_factory=list)
    needs_stronger_model: bool = False
    files: list[str] = Field(default_factory=list)
    task_type: str | None = None
    estimated_loc: int | None = None
    verification: str | None = None

    @model_validator(mode="after")
    def _apply_title_defaults(self) -> LeafTask:
        """Reproduce the historical title-derived defaults exactly."""
        if not self.description:
            self.description = self.title
        if not self.plan_text:
            self.plan_text = self.description
        if not self.checklist:
            self.checklist = [LeafChecklistItem(text=self.title)]
        return self
```

- [ ] **Step 4: Rewrite parse_review_response to build LeafTask**

Replace the import block and the per-task loop in `src/orchestrator/core/plan_review.py`. Change the import at the top:

```python
from pydantic import ValidationError

from orchestrator.models.schemas import CapabilityProfile, LeafTask
```

Replace the loop body (lines 144-152, the `for t in tasks:` block and `return`) with:

```python
    leaves: list[dict] = []
    for t in tasks:
        if not isinstance(t, dict) or "id" not in t or "title" not in t:
            raise PlanReviewError(f"task missing id/title: {t}")  # noqa: EM102
        try:
            leaf = LeafTask.model_validate(t)
        except ValidationError as exc:
            raise PlanReviewError(f"invalid leaf task: {exc}") from exc  # noqa: EM102
        leaves.append(leaf.model_dump())
    return {"tasks": leaves}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_leaf_task.py tests/test_plan_review.py -v`
Expected: PASS. If `tests/test_plan_review.py` asserts the exact key set of a parsed task, update it to allow the additive `schema_version`/`files`/`task_type`/`estimated_loc`/`verification` keys (they are new but harmless).

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run ruff format src/orchestrator/models/schemas.py src/orchestrator/core/plan_review.py tests/test_leaf_task.py
uv run ruff check --fix src/orchestrator/models/schemas.py src/orchestrator/core/plan_review.py tests/test_leaf_task.py
uv run mypy src/orchestrator/ --ignore-missing-imports
git add src/orchestrator/models/schemas.py src/orchestrator/core/plan_review.py tests/test_leaf_task.py
git commit -m "feat: add versioned LeafTask contract replacing setdefault decomposition dict"
```

---

### Task 2: Golden-file fixtures for decomposition parsing (S2)

**Files:**
- Create: `tests/fixtures/decompose/sample_plan_response.json`
- Create: `tests/fixtures/decompose/expected_leaf_graph.json`
- Test: `tests/test_decompose_golden.py`

**Depends on:** Task 1

- [ ] **Step 1: Write the failing test**

```python
# tests/test_decompose_golden.py
"""Golden-file regression: sample brain response in, frozen leaf graph out.

A prompt change that breaks the parser or drops a field fails here in CI,
not on a live dispatch (S2).
"""

from __future__ import annotations

import json
from pathlib import Path

from orchestrator.core.plan_review import parse_review_response


FIXTURES = Path(__file__).parent / "fixtures" / "decompose"


def test_sample_plan_parses_to_expected_leaf_graph() -> None:
    raw = (FIXTURES / "sample_plan_response.json").read_text(encoding="utf-8")
    expected = json.loads(
        (FIXTURES / "expected_leaf_graph.json").read_text(encoding="utf-8")
    )
    assert parse_review_response(raw) == expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_decompose_golden.py -v`
Expected: FAIL with `FileNotFoundError`.

- [ ] **Step 3: Create the sample input fixture**

```json
// tests/fixtures/decompose/sample_plan_response.json
{
  "tasks": [
    {
      "id": "t1",
      "title": "Add config loader",
      "description": "Load YAML config and overlay env vars.",
      "plan_text": "def load_config(path: str) -> dict: ...",
      "depends_on": [],
      "checklist": [{"text": "write test"}, {"text": "implement"}],
      "needs_stronger_model": false
    },
    {
      "id": "t2",
      "title": "Wire config into settings"
    }
  ]
}
```

Note: remove the `//` comment line before saving; JSON does not allow comments. The file must be valid JSON starting with `{`.

- [ ] **Step 4: Create the expected output fixture**

```json
{
  "tasks": [
    {
      "schema_version": 1,
      "id": "t1",
      "title": "Add config loader",
      "description": "Load YAML config and overlay env vars.",
      "plan_text": "def load_config(path: str) -> dict: ...",
      "depends_on": [],
      "checklist": [{"text": "write test"}, {"text": "implement"}],
      "needs_stronger_model": false,
      "files": [],
      "task_type": null,
      "estimated_loc": null,
      "verification": null
    },
    {
      "schema_version": 1,
      "id": "t2",
      "title": "Wire config into settings",
      "description": "Wire config into settings",
      "plan_text": "Wire config into settings",
      "depends_on": [],
      "checklist": [{"text": "Wire config into settings"}],
      "needs_stronger_model": false,
      "files": [],
      "task_type": null,
      "estimated_loc": null,
      "verification": null
    }
  ]
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_decompose_golden.py -v`
Expected: PASS. If it fails on key ordering, note `dict ==` is order-independent so a mismatch is a real field difference; reconcile the expected file to the actual `model_dump()` output.

- [ ] **Step 6: Commit**

```bash
git add tests/fixtures/decompose/ tests/test_decompose_golden.py
git commit -m "test: add golden-file fixtures for decomposition leaf-graph parsing"
```

---

### Task 3: Failure taxonomy enum with attribution flag (S6)

**Files:**
- Create: `src/orchestrator/core/failure_taxonomy.py`
- Test: `tests/test_failure_taxonomy.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_failure_taxonomy.py
"""S6 shared failure taxonomy with attribution hygiene."""

from __future__ import annotations

import pytest

from orchestrator.core.failure_taxonomy import FailureClass, counts_against_worker


def test_all_expected_classes_exist() -> None:
    assert {c.value for c in FailureClass} == {
        "verify_fail",
        "fixable_in_place",
        "context_overflow",
        "too_broad",
        "needs_stronger_model",
        "worker_blocked",
        "provider_error",
    }


@pytest.mark.parametrize(
    ("failure_class", "expected"),
    [
        (FailureClass.VERIFY_FAIL, True),
        (FailureClass.FIXABLE_IN_PLACE, True),
        (FailureClass.CONTEXT_OVERFLOW, True),
        (FailureClass.TOO_BROAD, True),
        (FailureClass.NEEDS_STRONGER_MODEL, False),
        (FailureClass.WORKER_BLOCKED, False),
        (FailureClass.PROVIDER_ERROR, False),
    ],
)
def test_attribution_hygiene(failure_class: FailureClass, expected: bool) -> None:
    assert counts_against_worker(failure_class) is expected


def test_counts_against_worker_accepts_raw_string() -> None:
    assert counts_against_worker("verify_fail") is True
    assert counts_against_worker("provider_error") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_failure_taxonomy.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.core.failure_taxonomy'`.

- [ ] **Step 3: Write the module**

```python
# src/orchestrator/core/failure_taxonomy.py
"""Shared failure taxonomy (S6).

``failure_class`` values are produced by the reviewer, dispatch, and the
reconciler, and consumed by calibration attribution (F5). Keeping them as
string literals across four modules lets attribution silently rot, so they
live here once with the attribution rule attached to each member.
"""

from __future__ import annotations

from enum import StrEnum


class FailureClass(StrEnum):
    """Why a task terminally failed. One shared vocabulary, no local literals."""

    VERIFY_FAIL = "verify_fail"
    FIXABLE_IN_PLACE = "fixable_in_place"
    CONTEXT_OVERFLOW = "context_overflow"
    TOO_BROAD = "too_broad"
    NEEDS_STRONGER_MODEL = "needs_stronger_model"
    WORKER_BLOCKED = "worker_blocked"
    PROVIDER_ERROR = "provider_error"


# Attribution hygiene (F5): only these classes teach the calibration loop that
# the worker model struggled. Provider errors, infra flakes, escalation-routing
# decisions, and human merge-gate rejections must never do so.
_COUNTS_AGAINST_WORKER: frozenset[FailureClass] = frozenset(
    {
        FailureClass.VERIFY_FAIL,
        FailureClass.FIXABLE_IN_PLACE,
        FailureClass.CONTEXT_OVERFLOW,
        FailureClass.TOO_BROAD,
    }
)


def counts_against_worker(failure_class: FailureClass | str) -> bool:
    """Return True when this failure should count against the worker's capability.

    Args:
        failure_class: A ``FailureClass`` member or its raw string value.

    Returns:
        True only for worker-attributable classes; False for provider errors,
        blocks, and escalation-routing outcomes.
    """
    return FailureClass(failure_class) in _COUNTS_AGAINST_WORKER
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_failure_taxonomy.py -v`
Expected: PASS.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff format src/orchestrator/core/failure_taxonomy.py tests/test_failure_taxonomy.py
uv run ruff check --fix src/orchestrator/core/failure_taxonomy.py tests/test_failure_taxonomy.py
uv run mypy src/orchestrator/ --ignore-missing-imports
git add src/orchestrator/core/failure_taxonomy.py tests/test_failure_taxonomy.py
git commit -m "feat: add shared failure taxonomy enum with worker-attribution flag"
```

---

### Task 4: Canonical status vocabulary + MCP consolidation (S9)

**Files:**
- Modify: `src/orchestrator/models/schemas.py:19-29` (add `TaskStatus.SUPERSEDED`)
- Create: `src/orchestrator/core/status_vocab.py`
- Modify: `src/mcp_server/server.py:158-167` (import from `status_vocab`)
- Test: `tests/test_status_vocab.py`

**Depends on:** Task 1

- [ ] **Step 1: Write the failing test**

```python
# tests/test_status_vocab.py
"""S9 canonical status vocabulary: one source, MCP/REST/dashboard agree."""

from __future__ import annotations

import re
from pathlib import Path

from orchestrator.core import status_vocab
from orchestrator.models.schemas import TaskStatus


def test_superseded_is_frozen_now() -> None:
    assert TaskStatus.SUPERSEDED == "superseded"


def test_mcp_aliases_are_the_only_aliases() -> None:
    assert status_vocab.MCP_STATUS_ALIASES == {
        "passed": "awaiting_merge",
        "needs_clarification": "awaiting_clarification",
    }
    assert status_vocab.mcp_status("passed") == "awaiting_merge"
    assert status_vocab.mcp_status("in_progress") == "in_progress"
    assert status_vocab.mcp_status(None) is None


def test_terminal_includes_superseded() -> None:
    assert "superseded" in status_vocab.TERMINAL_STATUSES
    assert "merged" in status_vocab.TERMINAL_STATUSES
    assert "failed" in status_vocab.TERMINAL_STATUSES


def test_mcp_server_imports_the_canonical_vocabulary() -> None:
    from mcp_server import server

    assert server._TASK_STATUS_MAP is status_vocab.MCP_STATUS_ALIASES
    assert server._GATED_STATUSES is status_vocab.GATED_STATUSES
    assert server._TERMINAL_STATUSES is status_vocab.TERMINAL_STATUSES


def test_dashboard_uses_only_canonical_status_literals() -> None:
    """app.js must not compare against a status string outside the vocabulary."""
    app_js = (
        Path(__file__).parents[1] / "web" / "app.js"
    ).read_text(encoding="utf-8")
    known = (
        {s.value for s in TaskStatus}
        | set(status_vocab.CANONICAL_PLAN_STATUSES)
        | set(status_vocab.MCP_STATUS_ALIASES.values())
    )
    # Every literal compared with `.status === "..."` must be a known status.
    for match in re.finditer(r'\.status\s*===\s*"([a-z_]+)"', app_js):
        assert match.group(1) in known, f"unknown dashboard status: {match.group(1)}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_status_vocab.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.core.status_vocab'`.

- [ ] **Step 3: Add SUPERSEDED to TaskStatus**

In `src/orchestrator/models/schemas.py`, extend the `TaskStatus` enum (after `NEEDS_CLARIFICATION` on line 28):

```python
    NEEDS_CLARIFICATION = "needs_clarification"
    SUPERSEDED = "superseded"  # frozen for F4 (Plan 5); no producer yet
```

- [ ] **Step 4: Write status_vocab.py**

```python
# src/orchestrator/core/status_vocab.py
"""Canonical task/plan status vocabulary (S9).

``TaskStatus``/``PlanStatus`` in ``models.schemas`` are the source of truth.
The MCP surface renamed a couple of raw statuses for external agents, and the
groupings (gated, terminal) lived as scattered literals in ``mcp_server.server``
and ``web/app.js``. That drift already bit once, so all of it lives here now:
REST, MCP, and the dashboard render from this one module.
"""

from __future__ import annotations

from orchestrator.models.schemas import PlanStatus, TaskStatus


CANONICAL_TASK_STATUSES: frozenset[str] = frozenset(s.value for s in TaskStatus)
CANONICAL_PLAN_STATUSES: frozenset[str] = frozenset(s.value for s in PlanStatus)

# MCP presentation aliases: the ONLY renames applied on the MCP surface.
MCP_STATUS_ALIASES: dict[str, str] = {
    TaskStatus.PASSED.value: "awaiting_merge",
    TaskStatus.NEEDS_CLARIFICATION.value: "awaiting_clarification",
}

# Reviewed but not yet merged (human ``approve-merge`` pending). Includes the
# MCP alias so callers comparing already-aliased statuses still match.
GATED_STATUSES: frozenset[str] = frozenset(
    {TaskStatus.PASSED.value, MCP_STATUS_ALIASES[TaskStatus.PASSED.value]}
)

# Cannot progress further without intervention.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {
        TaskStatus.FAILED.value,
        TaskStatus.MERGED.value,
        TaskStatus.SUPERSEDED.value,
    }
)


def mcp_status(raw: str | None) -> str | None:
    """Map a raw DB status to its MCP-facing alias, if any.

    Args:
        raw: The raw ``tasks.status`` value, or None.

    Returns:
        The aliased status for MCP responses, the raw status when unaliased,
        or None when ``raw`` is None.
    """
    if raw is None:
        return None
    return MCP_STATUS_ALIASES.get(raw, raw)
```

- [ ] **Step 5: Point mcp_server/server.py at the vocabulary**

In `src/mcp_server/server.py`, replace the local literal definitions at lines 158-167 (`_TASK_STATUS_MAP = {...}`, `_GATED_STATUSES = ...`, `_TERMINAL_STATUSES = ...`). First add the import near the top of the file with the other imports:

```python
from orchestrator.core import status_vocab
```

Then replace the three local definitions with aliases to the canonical objects (keep the underscore names so existing call sites in `server.py` are untouched):

```python
_TASK_STATUS_MAP = status_vocab.MCP_STATUS_ALIASES
_GATED_STATUSES = status_vocab.GATED_STATUSES
_TERMINAL_STATUSES = status_vocab.TERMINAL_STATUSES
```

Verify the surrounding usages (`_TASK_STATUS_MAP.get(...)`, `row.get("status") in _GATED_STATUSES`, membership in `_TERMINAL_STATUSES`) still resolve; they do, since the object types (dict, frozenset) are unchanged.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_status_vocab.py tests/test_mcp_resources.py -v`
Expected: PASS. If `test_dashboard_uses_only_canonical_status_literals` fails, the dashboard already references a status the vocabulary does not model; inspect the offending literal in `web/app.js` and reconcile (it is almost certainly a real typo worth fixing, not a test to loosen).

- [ ] **Step 7: Lint, type-check, commit**

```bash
uv run ruff format src/orchestrator/models/schemas.py src/orchestrator/core/status_vocab.py src/mcp_server/server.py tests/test_status_vocab.py
uv run ruff check --fix src/orchestrator/models/schemas.py src/orchestrator/core/status_vocab.py src/mcp_server/server.py tests/test_status_vocab.py
uv run mypy src/orchestrator/ --ignore-missing-imports
git add src/orchestrator/models/schemas.py src/orchestrator/core/status_vocab.py src/mcp_server/server.py tests/test_status_vocab.py
git commit -m "refactor: freeze canonical status vocabulary and consolidate MCP status literals"
```

---

### Task 5: Capability decision-record models (S1)

**Files:**
- Create: `src/orchestrator/core/capability_events.py` (models only in this task)
- Test: `tests/test_capability_event_models.py`

**Depends on:** Task 3

- [ ] **Step 1: Write the failing test**

```python
# tests/test_capability_event_models.py
"""S1 capability decision-record models: one versioned schema per event."""

from __future__ import annotations

from orchestrator.core.capability_events import (
    CAPABILITY_EVENT_SCHEMA_VERSION,
    DecomposeInputEvent,
    LeafRejectedEvent,
    LeafValidatedEvent,
    OutcomeRecordedEvent,
    PlanRejectedEvent,
    TaskEscalatedEvent,
    TaskSplitEvent,
)


def test_every_event_carries_schema_version_and_type() -> None:
    events = [
        DecomposeInputEvent(
            plan_id="p1",
            model_name="qwen3.6-27b",
            per_leaf_budget=13107,
            profile_summary="27b/32k",
            history_summary_hash="deadbeef",
            plan_hash="cafe",
        ),
        LeafValidatedEvent(plan_id="p1", leaf_slug="add-x"),
        LeafRejectedEvent(
            plan_id="p1", leaf_slug="add-x", rule_id="max_files",
            measured=7, limit=4,
        ),
        PlanRejectedEvent(plan_id="p1", violations=["t3 touches 7 files"], rounds=2),
        TaskSplitEvent(
            plan_id="p1", parent_slug="big", child_slugs=["a", "b"],
            tightened_limits={"max_files_touched": 2},
        ),
        TaskEscalatedEvent(plan_id="p1", leaf_slug="hard", policy="brain"),
        OutcomeRecordedEvent(
            plan_id="p1", task_id="t1", outcome_id="o1",
            failure_class="verify_fail", counts_against_worker=True,
        ),
    ]
    for event in events:
        assert event.schema_version == CAPABILITY_EVENT_SCHEMA_VERSION
        assert event.event_type
        assert event.model_dump()["event_type"] == event.event_type


def test_event_types_are_distinct_and_canonical() -> None:
    from orchestrator.core.capability_events import CAPABILITY_EVENT_TYPES

    assert CAPABILITY_EVENT_TYPES == {
        "decompose_input",
        "leaf_validated",
        "leaf_rejected",
        "plan_rejected",
        "task_split",
        "task_escalated",
        "outcome_recorded",
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_capability_event_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.core.capability_events'`.

- [ ] **Step 3: Write the models module**

```python
# src/orchestrator/core/capability_events.py
"""Capability-engine decision records (S1).

Every capability-engine decision point (why a leaf was sized, rejected, split,
or escalated) emits a versioned, structured record instead of free-text
``logger.info``. Records are dual-written to the SSE event bus (live view) and
the ``capability_events`` table (queryable history). One Pydantic model per
event, each with a ``schema_version``.

The emitter (``CapabilityEventEmitter``) is delivered here but is STUBBED: no
production code calls it in this plan. Plans 2, 3, and 5 wire the emitters in
at the decompose/validate, outcome-record, and split/escalate seams.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


CAPABILITY_EVENT_SCHEMA_VERSION = 1


class _CapabilityEvent(BaseModel):
    """Base for all decision records; extra keys are rejected on purpose."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = CAPABILITY_EVENT_SCHEMA_VERSION


class DecomposeInputEvent(_CapabilityEvent):
    """Emitted by F2 when a decomposition brain call is prepared."""

    event_type: Literal["decompose_input"] = "decompose_input"
    plan_id: str
    model_name: str
    per_leaf_budget: int
    profile_summary: str
    history_summary_hash: str
    plan_hash: str


class LeafValidatedEvent(_CapabilityEvent):
    """Emitted by F3 when a leaf passes the deterministic validator."""

    event_type: Literal["leaf_validated"] = "leaf_validated"
    plan_id: str
    leaf_slug: str


class LeafRejectedEvent(_CapabilityEvent):
    """Emitted by F3 when a leaf violates a hard constraint."""

    event_type: Literal["leaf_rejected"] = "leaf_rejected"
    plan_id: str
    leaf_slug: str
    rule_id: str
    measured: int | str | None = None
    limit: int | str | None = None


class PlanRejectedEvent(_CapabilityEvent):
    """Emitted by F3 when validation fails closed after the informed rounds."""

    event_type: Literal["plan_rejected"] = "plan_rejected"
    plan_id: str
    violations: list[str]
    rounds: int


class TaskSplitEvent(_CapabilityEvent):
    """Emitted by F4 when a failed leaf is split into tighter sub-leaves."""

    event_type: Literal["task_split"] = "task_split"
    plan_id: str
    parent_slug: str
    child_slugs: list[str]
    tightened_limits: dict = Field(default_factory=dict)
    failure_evidence_ref: str | None = None


class TaskEscalatedEvent(_CapabilityEvent):
    """Emitted by F4 when a leaf is routed to a stronger seat."""

    event_type: Literal["task_escalated"] = "task_escalated"
    plan_id: str
    leaf_slug: str
    policy: str  # block | brain | paid_fallback


class OutcomeRecordedEvent(_CapabilityEvent):
    """Emitted by F5 at every terminal review verdict."""

    event_type: Literal["outcome_recorded"] = "outcome_recorded"
    plan_id: str
    task_id: str
    outcome_id: str
    failure_class: str | None = None
    counts_against_worker: bool = False


CapabilityEventModel = (
    DecomposeInputEvent
    | LeafValidatedEvent
    | LeafRejectedEvent
    | PlanRejectedEvent
    | TaskSplitEvent
    | TaskEscalatedEvent
    | OutcomeRecordedEvent
)

CAPABILITY_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "decompose_input",
        "leaf_validated",
        "leaf_rejected",
        "plan_rejected",
        "task_split",
        "task_escalated",
        "outcome_recorded",
    }
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_capability_event_models.py -v`
Expected: PASS.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff format src/orchestrator/core/capability_events.py tests/test_capability_event_models.py
uv run ruff check --fix src/orchestrator/core/capability_events.py tests/test_capability_event_models.py
uv run mypy src/orchestrator/ --ignore-missing-imports
git add src/orchestrator/core/capability_events.py tests/test_capability_event_models.py
git commit -m "feat: add versioned capability decision-record models"
```

---

### Task 6: capability_events table migration (S1)

**Files:**
- Modify: `src/orchestrator/database.py:148-178` (add Migration 4)
- Test: `tests/test_database_capability_events.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_database_capability_events.py
"""Migration 4 adds the capability_events decision-record table (S1)."""

from __future__ import annotations

from pathlib import Path

from orchestrator.database import CURRENT_SCHEMA_VERSION, Database


async def _fresh_db(tmp_path: Path) -> Database:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'orch.db'}")
    await db.initialize()
    return db


async def test_capability_events_table_exists(tmp_path: Path) -> None:
    db = await _fresh_db(tmp_path)
    row = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        ("capability_events",),
    )
    assert row is not None
    await db.close()


async def test_schema_version_is_current(tmp_path: Path) -> None:
    db = await _fresh_db(tmp_path)
    row = await db.fetch_one("PRAGMA user_version", ())
    assert list(row.values())[0] == CURRENT_SCHEMA_VERSION
    assert CURRENT_SCHEMA_VERSION >= 4
    await db.close()


async def test_capability_events_columns(tmp_path: Path) -> None:
    db = await _fresh_db(tmp_path)
    rows = await db.fetch_all("PRAGMA table_info(capability_events)", ())
    cols = {r["name"] for r in rows}
    assert cols == {
        "id",
        "schema_version",
        "event_type",
        "plan_id",
        "task_id",
        "payload",
        "created_at",
    }
    await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_database_capability_events.py -v`
Expected: FAIL: the table does not exist and `CURRENT_SCHEMA_VERSION` is 3.

- [ ] **Step 3: Add the migration**

In `src/orchestrator/database.py`, add the migration function after `_migration_0003_plan_error` (after line 161):

```python
async def _migration_0004_capability_events(connection: aiosqlite.Connection) -> None:
    """Add capability_events: versioned decision records (S1).

    Idempotent via CREATE TABLE IF NOT EXISTS, so a crash between apply and the
    version bump replays safely on next startup.
    """
    await connection.execute(
        """
        CREATE TABLE IF NOT EXISTS capability_events (
            id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            plan_id TEXT,
            task_id TEXT,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
```

Append the entry to the `MIGRATIONS` list (after the version-3 entry, before the closing bracket on line 176):

```python
    Migration(
        4,
        "add capability_events decision-record table",
        _migration_0004_capability_events,
    ),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_database_capability_events.py tests/test_database.py -v`
Expected: PASS. `CURRENT_SCHEMA_VERSION` is now 4 automatically (it reads `MIGRATIONS[-1].version`).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff format src/orchestrator/database.py tests/test_database_capability_events.py
uv run ruff check --fix src/orchestrator/database.py tests/test_database_capability_events.py
uv run mypy src/orchestrator/ --ignore-missing-imports
git add src/orchestrator/database.py tests/test_database_capability_events.py
git commit -m "feat: add capability_events table via migration 4"
```

---

### Task 7: Capability event emitter with bus + table dual-write (S1)

**Files:**
- Modify: `src/orchestrator/core/capability_events.py` (append the emitter)
- Test: `tests/test_capability_event_emitter.py`

**Depends on:** Task 5, Task 6

- [ ] **Step 1: Write the failing test**

```python
# tests/test_capability_event_emitter.py
"""S1 emitter dual-writes a decision record to the bus and the table."""

from __future__ import annotations

import json
from pathlib import Path

from orchestrator.core.capability_events import (
    CapabilityEventEmitter,
    LeafRejectedEvent,
)
from orchestrator.core.event_bus import EventBus
from orchestrator.database import Database


async def _db(tmp_path: Path) -> Database:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'orch.db'}")
    await db.initialize()
    return db


async def test_emit_writes_row_and_publishes(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    bus = EventBus()
    queue = bus.subscribe()
    emitter = CapabilityEventEmitter(db, bus)

    event = LeafRejectedEvent(
        plan_id="p1", leaf_slug="add-x", rule_id="max_files", measured=7, limit=4
    )
    row_id = await emitter.emit(event)

    row = await db.fetch_one(
        "SELECT * FROM capability_events WHERE id = ?", (row_id,)
    )
    assert row is not None
    assert row["event_type"] == "leaf_rejected"
    assert row["plan_id"] == "p1"
    assert row["task_id"] is None
    assert json.loads(row["payload"])["rule_id"] == "max_files"

    published = queue.get_nowait()
    assert published["type"] == "capability.leaf_rejected"
    assert published["leaf_slug"] == "add-x"
    await db.close()


async def test_emit_extracts_task_id_when_present(tmp_path: Path) -> None:
    from orchestrator.core.capability_events import OutcomeRecordedEvent

    db = await _db(tmp_path)
    emitter = CapabilityEventEmitter(db, EventBus())
    row_id = await emitter.emit(
        OutcomeRecordedEvent(
            plan_id="p1", task_id="t9", outcome_id="o1",
            failure_class="verify_fail", counts_against_worker=True,
        )
    )
    row = await db.fetch_one(
        "SELECT task_id FROM capability_events WHERE id = ?", (row_id,)
    )
    assert row["task_id"] == "t9"
    await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_capability_event_emitter.py -v`
Expected: FAIL with `ImportError: cannot import name 'CapabilityEventEmitter'`.

- [ ] **Step 3: Append the emitter to capability_events.py**

Add imports at the top of `src/orchestrator/core/capability_events.py` (below the existing `from typing import Literal`):

```python
import json
import uuid
from datetime import UTC, datetime

from orchestrator.core.event_bus import EventBus
from orchestrator.database import Database
```

Append at the end of the module:

```python
class CapabilityEventEmitter:
    """Dual-write a capability decision record to the bus and the table.

    STUBBED for Plan 1: instantiated and unit-tested here, but no production
    code path calls ``emit`` yet. Plans 2/3/5 inject an instance at the
    decompose, outcome-record, and split/escalate seams.
    """

    def __init__(self, db: Database, bus: EventBus) -> None:
        self._db = db
        self._bus = bus

    async def emit(self, event: CapabilityEventModel) -> str:
        """Persist the record and publish it live.

        Args:
            event: Any capability decision-record model.

        Returns:
            The generated ``capability_events.id`` primary key.
        """
        payload = event.model_dump()
        row_id = str(uuid.uuid4())
        plan_id = payload.get("plan_id")
        task_id = payload.get("task_id")
        await self._db.execute(
            """INSERT INTO capability_events
               (id, schema_version, event_type, plan_id, task_id, payload,
                created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                row_id,
                event.schema_version,
                event.event_type,
                plan_id,
                task_id,
                json.dumps(payload),
                datetime.now(UTC).isoformat(),
            ),
        )
        self._bus.publish({"type": f"capability.{event.event_type}", **payload})
        return row_id
```

Note: `event.event_type` is typed as a per-model `Literal`; accessing it on the `CapabilityEventModel` union is valid because every member defines it. If mypy complains about union attribute access, it will not here, since all union members declare `event_type` and `schema_version`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_capability_event_emitter.py -v`
Expected: PASS.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff format src/orchestrator/core/capability_events.py tests/test_capability_event_emitter.py
uv run ruff check --fix src/orchestrator/core/capability_events.py tests/test_capability_event_emitter.py
uv run mypy src/orchestrator/ --ignore-missing-imports
git add src/orchestrator/core/capability_events.py tests/test_capability_event_emitter.py
git commit -m "feat: add capability event emitter dual-writing to bus and table"
```

---

### Task 8: Full-suite verification, gotchas index, no-regression confirmation

**Files:**
- Modify: `CLAUDE.md` (Gotchas condensed index: add three entries)
- Test: none new; run the whole suite

**Depends on:** Task 4, Task 7

- [ ] **Step 1: Run the full suite with coverage**

Run: `uv run pytest --cov=orchestrator --cov-report=term-missing -v`
Expected: PASS, no drop below the CI floor (`--cov-fail-under=80`). Confirm no pre-existing test asserting the old decompose dict shape or the old MCP status literals now fails; if one does, it is exercising a contract this plan intentionally moved, so update the assertion to import from the new source (`LeafTask`, `status_vocab`), never to loosen it blindly.

- [ ] **Step 2: Confirm no production emitter caller exists (stubbed invariant)**

Run: `uv run python -c "import subprocess, sys; sys.exit(0)"` then grep:
Run: `git grep -n "CapabilityEventEmitter(" -- src/orchestrator | grep -v "capability_events.py"`
Expected: no output. The emitter must have no production instantiation in this plan.

- [ ] **Step 3: Add three gotcha index lines to CLAUDE.md**

In `CLAUDE.md`, under the condensed Gotchas index list, add:

```markdown
- **LeafTask is the decomposition contract** (`models/schemas.py`) - `parse_review_response`
  builds it; F2 fields (`files`/`task_type`/`estimated_loc`/`verification`) are additive
  and defaulted, golden fixtures in `tests/fixtures/decompose/` freeze the parse.
- **Status vocabulary is frozen in `core/status_vocab.py`** - MCP aliases + gated/terminal
  sets live there; REST/MCP/dashboard render from it. `superseded` is reserved for F4.
- **Capability decision records** (`core/capability_events.py`) dual-write to the bus +
  `capability_events` table; the emitter is stubbed (no production caller until Plans 2/3/5).
```

- [ ] **Step 4: Lint, commit**

```bash
uv run ruff format src/ tests/
uv run ruff check src/ tests/
git add CLAUDE.md
git commit -m "docs: index capability-contracts-foundation gotchas"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1 (LeafTask), Task 3 (failure taxonomy), Task 6 (migration) - no dependencies, run in parallel.
- **Wave 2:** Task 2 (golden fixtures, depends on Task 1), Task 4 (status vocab, depends on Task 1 for the `SUPERSEDED` enum edit in `schemas.py`), Task 5 (decision-record models, depends on Task 3).
- **Wave 3:** Task 7 (emitter, depends on Task 5 + Task 6).
- **Wave 4:** Task 8 (full-suite verification + gotchas, depends on Task 4 + Task 7).

Note on file overlap: Task 1 and Task 4 both edit `src/orchestrator/models/schemas.py`. The dependency (Task 4 after Task 1) serializes those edits so a worktree-isolated dispatch does not clobber. Task 5 and Task 7 both edit `src/orchestrator/core/capability_events.py`; Task 7 depends on Task 5 for the same reason.

---

## Self-Review

**Spec coverage (Plan 1 row of roadmap section 8):**
- S2 `LeafTask` model with `schema_version` replacing the setdefault dict - Task 1. Golden fixtures - Task 2. ✅
- S6 failure-taxonomy enum with `counts_against_worker` - Task 3. ✅
- S9 canonical `TaskStatus`/`PlanStatus` vocabulary frozen in one module, REST/MCP/dashboard render from the same enum, aliases documented - Task 4. ✅
- S1 decision-record Pydantic models (all seven: decompose_input, leaf_validated, leaf_rejected, plan_rejected, task_split, task_escalated, outcome_recorded) with `schema_version`, `capability_events` table via the versioned Migration list, event-bus wiring, emitters stubbed - Tasks 5, 6, 7. ✅

**Constraints:** pure schemas + infra, no loop behavior change (additive fields, behavior-preserving MCP refactor, stubbed emitter verified in Task 8 Step 2); TDD per task; every schema has a fixture test; migration uses `Migration` + `PRAGMA user_version` idempotently; 88-col ruff + mypy in every task; no em dashes. ✅

**Dependency correctness:** Task 2/4 read Task 1's `LeafTask`/enum; Task 5 references Task 3's `FailureClass` conceptually (string field, no hard import, so dependency is for file-serialization safety only, still correct to list); Task 7 reads Task 5's models and Task 6's table; Task 8 needs all behavior-bearing tasks merged. Map matches.

**Type consistency:** `LeafTask.checklist` is `list[LeafChecklistItem]`, `model_dump()` renders `[{"text": ...}]` matching the golden fixture and the old dict shape. `event_type`/`schema_version` are present on every event model and read by the emitter. `MCP_STATUS_ALIASES`/`GATED_STATUSES`/`TERMINAL_STATUSES` names are identical across `status_vocab.py`, the `server.py` aliases, and the tests.

**Placeholder scan:** every code step contains complete code; no "TBD"/"add validation"/"similar to Task N".
