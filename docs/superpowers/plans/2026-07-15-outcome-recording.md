# Outcome Recording (F5-recording + S11) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record a `task_outcomes` row at every terminal review verdict, feed a scoped summary of past outcomes back into every future decomposition, and thread `plan_id`/`task_id`/`run_id` through orchestrator logs.

**Architecture:** This is Plan 3 of the capability-engine roadmap (`docs/superpowers/specs/2026-07-11-capability-engine-roadmap.md`, feature F5-recording + contract S11). It is pure instrumentation: it does NOT change dispatch, review, or merge behavior. A new `task_outcomes` table is written by `core/outcome_recorder.record_outcome`, called from `ReviewMixin.review_task` at each terminal verdict. Diff size is measured by the pure helper `core/diff_stats.diff_stats`. `core/capability_history.fetch_recent_outcomes` queries the table scoped `(model_name, project_id)` with fallback `(model_name, *)`, filtered to worker-attributable rows via the existing S6 taxonomy (`core/failure_taxonomy.counts_against_worker`), and its result replaces the hardcoded `summarize_outcomes([])` in `decompose_plan`. S11 adds a `core/log_context.py` `LoggerAdapter` helper. Wilson-bound learned limits and the `GET /api/capability/{model}` surface are explicitly OUT of scope (Plan 6).

**Tech Stack:** Python 3.11, aiosqlite (raw SQL, versioned `Migration` framework in `database.py`), Pydantic event models (`core/capability_events.py`), pytest with `asyncio_mode = "auto"`.

---

## Scope boundary (read before starting)

IN scope (Plan 3 only):
- `task_outcomes` table (migration 5).
- `diff_stats(diff)` measurement helper.
- `record_outcome(...)` writer + `OutcomeRecordedEvent` emission (the event model already exists in `core/capability_events.py`).
- Recording calls wired into `review_task` at every terminal verdict.
- `fetch_recent_outcomes(...)` query + wiring into `decompose_plan` (replaces `summarize_outcomes([])`).
- S11 correlation-ID `LoggerAdapter` helper + application at the review/dispatch hot paths.
- Docs: CLAUDE.md gotcha index lines, `docs/architecture.md` capability-engine dataflow note, ROADMAP status flip.

OUT of scope (do NOT implement; later plans own these):
- Wilson score lower bound / learned effective limits (Plan 6, F1 overlay).
- `GET /api/capability/{model}` endpoint or dashboard panel (Plan 6).
- `source='benchmark'` producers (`praxis calibrate`, Plan 6, F6). The column exists and defaults to `'run'`; do not add a benchmark writer.
- Token-count instrumentation / `llm_calls` table (Plan 4, F7). `context_tokens_est` is recorded as `None` when unknown; do not build a token counter.
- Split/escalate outcome producers (Plan 5, F4). `outcome='escalated'`/`'superseded'` values are allowed by the schema but no Plan 3 code path writes them.

## Pinned inter-leaf contracts (honor these signatures verbatim)

These are the exact signatures shared across tasks. Do NOT rename or re-arrange arguments; later tasks call them exactly as written here (per the Plan 2 plan-authoring finding: a prose-described contract drifts, a pinned one does not).

```python
# core/diff_stats.py
def diff_stats(diff: str) -> tuple[int, int]:
    """Return (files_touched, loc_delta) from a unified git diff."""

# core/outcome_recorder.py
async def record_outcome(
    db: Any,
    *,
    task_id: str,
    plan_id: str | None,
    project_id: str | None,
    model_name: str | None,
    harness: str | None,
    task_type: str | None,
    files_touched: int,
    loc_delta: int,
    context_tokens_est: int | None,
    attempt: int,
    outcome: str,                 # "pass" | "fail" | "escalated" | "superseded"
    failure_class: str | None,
    split_depth: int = 0,
    source: str = "run",
    emitter: Any = None,
) -> str:
    """Insert one task_outcomes row (returns its id); emit OutcomeRecordedEvent if emitter given."""

# core/capability_history.py
async def fetch_recent_outcomes(
    db: Any,
    model_name: str | None,
    project_id: str | None,
    limit: int = 100,
) -> list[dict]:
    """Recent worker-attributable task_outcomes rows, newest first, scoped
    (model_name, project_id) then falling back to (model_name, *)."""

# core/log_context.py
def task_logger(
    base: logging.Logger,
    *,
    plan_id: str | None = None,
    task_id: str | None = None,
    run_id: str | None = None,
) -> logging.LoggerAdapter:
    """Wrap base so every emitted line is prefixed with the given ids."""
```

## File Structure

- `src/orchestrator/database.py` — add migration 5 (`task_outcomes`) to the `MIGRATIONS` list.
- `src/orchestrator/core/diff_stats.py` (new) — pure `diff_stats` helper.
- `src/orchestrator/core/outcome_recorder.py` (new) — `record_outcome` writer + emission.
- `src/orchestrator/core/capability_history.py` (modify) — add `fetch_recent_outcomes`; keep `summarize_outcomes` unchanged.
- `src/orchestrator/core/orchestrator_review.py` (modify) — call `record_outcome` at terminal verdicts in `review_task`.
- `src/orchestrator/core/execute_plan_decompose.py` (modify) — accept optional `db`, replace `summarize_outcomes([])`.
- `src/orchestrator/core/orchestrator.py:140` (modify) — pass `db=self._tq._db` to `decompose_plan`.
- `src/orchestrator/core/log_context.py` (new) — `task_logger` LoggerAdapter helper.
- Tests: `tests/test_diff_stats.py`, `tests/test_outcome_recorder.py`, `tests/test_capability_history.py` (extend), `tests/test_orchestrator_review.py` (extend or new `tests/test_outcome_recording_review.py`), `tests/test_log_context.py`.
- Docs: `CLAUDE.md`, `docs/architecture.md`, `docs/superpowers/ROADMAP.md`.

---

### Task 1: `task_outcomes` migration

**Files:**
- Modify: `src/orchestrator/database.py` (add `_migration_0005_task_outcomes` + `Migration(5, ...)` entry)
- Test: `tests/test_database_migrations.py` (create if absent; otherwise extend)

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_database_migrations.py
import pytest
from orchestrator.database import Database


@pytest.mark.asyncio
async def test_task_outcomes_table_exists_after_init(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.connect()
    await db.initialize()
    row = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='task_outcomes'"
    )
    assert row is not None
    cur = await db._conn.execute("PRAGMA table_info(task_outcomes)")
    cols = {r[1] for r in await cur.fetchall()}
    assert {
        "id", "task_id", "project_id", "model_name", "harness", "task_type",
        "files_touched", "loc_delta", "context_tokens_est", "attempt",
        "outcome", "failure_class", "split_depth", "source", "created_at",
    } <= cols
    await db.close()
```

Note: confirm the `Database` connection accessor name (`db._conn` vs `db._connection`) against `database.py` before running; use whichever the class exposes. If a `plan_id` column is desired for querying, it is NOT in the roadmap schema — omit it (scope query by project_id + model_name).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_database_migrations.py::test_task_outcomes_table_exists_after_init -v`
Expected: FAIL (no such table `task_outcomes`).

- [ ] **Step 3: Add the migration**

In `src/orchestrator/database.py`, after `_migration_0004_capability_events`, add:

```python
async def _migration_0005_task_outcomes(connection: aiosqlite.Connection) -> None:
    """Add task_outcomes: per-task capability calibration evidence (F5-recording)."""
    await connection.execute(
        """
        CREATE TABLE IF NOT EXISTS task_outcomes (
            id TEXT PRIMARY KEY,
            task_id TEXT,
            project_id TEXT,
            model_name TEXT,
            harness TEXT,
            task_type TEXT,
            files_touched INTEGER,
            loc_delta INTEGER,
            context_tokens_est INTEGER,
            attempt INTEGER,
            outcome TEXT NOT NULL,
            failure_class TEXT,
            split_depth INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'run',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_outcomes_model_project "
        "ON task_outcomes (model_name, project_id, created_at)"
    )
```

Then append to the `MIGRATIONS` list:

```python
    Migration(
        5,
        "add task_outcomes calibration table",
        _migration_0005_task_outcomes,
    ),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_database_migrations.py::test_task_outcomes_table_exists_after_init -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/database.py tests/test_database_migrations.py
git commit -m "feat: add task_outcomes calibration table (F5-recording)"
```

---

### Task 2: `diff_stats` measurement helper

**Files:**
- Create: `src/orchestrator/core/diff_stats.py`
- Test: `tests/test_diff_stats.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_diff_stats.py
from orchestrator.core.diff_stats import diff_stats


def test_empty_diff_is_zero():
    assert diff_stats("") == (0, 0)


def test_counts_files_and_loc():
    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "index e69de29..4b825dc 100644\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+line one\n"
        "+line two\n"
        "diff --git a/bar.py b/bar.py\n"
        "--- a/bar.py\n"
        "+++ b/bar.py\n"
        "@@ -1,1 +0,0 @@\n"
        "-gone\n"
    )
    files, loc = diff_stats(diff)
    assert files == 2
    assert loc == 3  # +line one, +line two, -gone


def test_ignores_file_headers_in_loc():
    diff = (
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    files, loc = diff_stats(diff)
    assert files == 1
    assert loc == 2  # -old, +new; the +++/--- headers are NOT counted


def test_new_file_from_dev_null_counts_once():
    diff = (
        "--- /dev/null\n"
        "+++ b/new.py\n"
        "@@ -0,0 +1 @@\n"
        "+hello\n"
    )
    files, loc = diff_stats(diff)
    assert files == 1
    assert loc == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_diff_stats.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Write the implementation**

```python
# src/orchestrator/core/diff_stats.py
"""Measure a unified git diff's size (files touched, lines changed).

Used by F5 outcome recording: files_touched / loc_delta describe how large a
change the worker actually produced, tagged onto each task_outcomes row.
"""

from __future__ import annotations


def diff_stats(diff: str) -> tuple[int, int]:
    """Return (files_touched, loc_delta) from a unified git diff.

    files_touched: number of ``+++ `` target-file headers (one per changed file).
    loc_delta: total added + removed content lines (lines starting with a single
    ``+`` or ``-``), excluding the ``+++``/``---`` file headers themselves.

    Args:
        diff: A unified diff string (may be empty).

    Returns:
        (files_touched, loc_delta); (0, 0) for an empty or header-less diff.
    """
    files = 0
    loc = 0
    for line in diff.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            if line.startswith("+++ "):
                files += 1
            continue
        if line.startswith("+") or line.startswith("-"):
            loc += 1
    return files, loc
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_diff_stats.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/diff_stats.py tests/test_diff_stats.py
git commit -m "feat: add diff_stats helper for outcome measurement"
```

---

### Task 3: `record_outcome` writer + event emission

**Files:**
- Create: `src/orchestrator/core/outcome_recorder.py`
- Test: `tests/test_outcome_recorder.py`

**Depends on:** Task 1

- [ ] **Step 1: Write the failing test**

```python
# tests/test_outcome_recorder.py
import pytest
from orchestrator.database import Database
from orchestrator.core.outcome_recorder import record_outcome


class _CapturingEmitter:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)
        return "evt-id"


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "o.db"))
    await d.connect()
    await d.initialize()
    yield d
    await d.close()


@pytest.mark.asyncio
async def test_record_outcome_inserts_row(db):
    row_id = await record_outcome(
        db,
        task_id="t1", plan_id="p1", project_id="proj1",
        model_name="qwen3.6-27b", harness="opencode", task_type="feature",
        files_touched=3, loc_delta=42, context_tokens_est=None,
        attempt=1, outcome="pass", failure_class=None,
    )
    assert row_id
    row = await db.fetch_one(
        "SELECT * FROM task_outcomes WHERE id = ?", (row_id,)
    )
    assert row["outcome"] == "pass"
    assert row["files_touched"] == 3
    assert row["loc_delta"] == 42
    assert row["source"] == "run"
    assert row["split_depth"] == 0


@pytest.mark.asyncio
async def test_record_outcome_emits_event_with_attribution(db):
    emitter = _CapturingEmitter()
    await record_outcome(
        db,
        task_id="t2", plan_id="p1", project_id="proj1",
        model_name="qwen3.6-27b", harness="opencode", task_type="refactor",
        files_touched=1, loc_delta=5, context_tokens_est=None,
        attempt=2, outcome="fail", failure_class="verify_fail",
        emitter=emitter,
    )
    assert len(emitter.events) == 1
    ev = emitter.events[0]
    assert ev.event_type == "outcome_recorded"
    assert ev.task_id == "t2"
    assert ev.counts_against_worker is True  # verify_fail counts


@pytest.mark.asyncio
async def test_provider_error_fail_does_not_count_against_worker(db):
    emitter = _CapturingEmitter()
    await record_outcome(
        db,
        task_id="t3", plan_id="p1", project_id="proj1",
        model_name="qwen3.6-27b", harness="opencode", task_type="feature",
        files_touched=0, loc_delta=0, context_tokens_est=None,
        attempt=1, outcome="fail", failure_class="provider_error",
        emitter=emitter,
    )
    assert emitter.events[0].counts_against_worker is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_outcome_recorder.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Write the implementation**

```python
# src/orchestrator/core/outcome_recorder.py
"""Record a task_outcomes row at every terminal review verdict (F5-recording).

Attribution hygiene: whether a failure counts against the worker's capability
is decided by the shared S6 taxonomy (``core/failure_taxonomy``), never by
ad-hoc string checks here. The OutcomeRecordedEvent model already exists in
``core/capability_events`` (delivered by Plan 1).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from orchestrator.core.capability_events import OutcomeRecordedEvent
from orchestrator.core.failure_taxonomy import counts_against_worker


logger = logging.getLogger(__name__)


async def record_outcome(
    db: Any,
    *,
    task_id: str,
    plan_id: str | None,
    project_id: str | None,
    model_name: str | None,
    harness: str | None,
    task_type: str | None,
    files_touched: int,
    loc_delta: int,
    context_tokens_est: int | None,
    attempt: int,
    outcome: str,
    failure_class: str | None,
    split_depth: int = 0,
    source: str = "run",
    emitter: Any = None,
) -> str:
    """Insert one task_outcomes row (returns its id); emit OutcomeRecordedEvent.

    Never raises out of the calling path: DB or emit failures are logged and
    swallowed so recording can never wedge the review loop.
    """
    row_id = str(uuid.uuid4())
    try:
        await db.execute(
            """
            INSERT INTO task_outcomes
                (id, task_id, project_id, model_name, harness, task_type,
                 files_touched, loc_delta, context_tokens_est, attempt,
                 outcome, failure_class, split_depth, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_id, task_id, project_id, model_name, harness, task_type,
                files_touched, loc_delta, context_tokens_est, attempt,
                outcome, failure_class, split_depth, source,
                datetime.now(UTC).isoformat(),
            ),
        )
    except Exception as exc:  # noqa: BLE001 - recording must never wedge review
        logger.warning("record_outcome: DB insert failed for task %s: %s", task_id, exc)
        return row_id

    against = bool(failure_class) and counts_against_worker(failure_class)
    if emitter is not None and plan_id is not None:
        try:
            await emitter.emit(
                OutcomeRecordedEvent(
                    plan_id=plan_id,
                    task_id=task_id,
                    outcome_id=row_id,
                    failure_class=failure_class,
                    counts_against_worker=against,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("record_outcome: emit failed for task %s: %s", task_id, exc)
    return row_id
```

Note: `counts_against_worker` raises `ValueError` on an unknown class string. Guard as written (`bool(failure_class) and ...`); a `None` class means non-attributable (e.g. a plain pass) and yields `against = False`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_outcome_recorder.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/outcome_recorder.py tests/test_outcome_recorder.py
git commit -m "feat: record_outcome writer + OutcomeRecordedEvent emission"
```

---

### Task 4: `fetch_recent_outcomes` scoped query

**Files:**
- Modify: `src/orchestrator/core/capability_history.py` (add `fetch_recent_outcomes`; leave `summarize_outcomes` untouched)
- Test: `tests/test_capability_history.py` (extend)

**Depends on:** Task 1

- [ ] **Step 1: Write the failing test**

```python
# tests/test_capability_history.py  (append)
import pytest
from orchestrator.database import Database
from orchestrator.core.capability_history import fetch_recent_outcomes
from orchestrator.core.outcome_recorder import record_outcome


@pytest.fixture
async def hist_db(tmp_path):
    d = Database(str(tmp_path / "h.db"))
    await d.connect()
    await d.initialize()
    yield d
    await d.close()


async def _add(db, **kw):
    base = dict(
        task_id="t", plan_id="p", project_id="proj", model_name="m",
        harness="opencode", task_type="feature", files_touched=1, loc_delta=1,
        context_tokens_est=None, attempt=1, outcome="pass", failure_class=None,
    )
    base.update(kw)
    await record_outcome(db, **base)


@pytest.mark.asyncio
async def test_fetch_scopes_by_model_and_project(hist_db):
    await _add(hist_db, model_name="m", project_id="proj", task_type="feature")
    await _add(hist_db, model_name="other", project_id="proj")
    rows = await fetch_recent_outcomes(hist_db, model_name="m", project_id="proj")
    assert len(rows) == 1
    assert rows[0]["model_name"] == "m"


@pytest.mark.asyncio
async def test_fetch_falls_back_to_model_wide_when_no_project_rows(hist_db):
    # rows for model m but a DIFFERENT project
    await _add(hist_db, model_name="m", project_id="otherproj")
    rows = await fetch_recent_outcomes(hist_db, model_name="m", project_id="proj")
    assert len(rows) == 1  # fallback to (model, *)


@pytest.mark.asyncio
async def test_fetch_excludes_non_worker_attributable_failures(hist_db):
    await _add(hist_db, model_name="m", project_id="proj",
               outcome="fail", failure_class="provider_error")
    await _add(hist_db, model_name="m", project_id="proj",
               outcome="fail", failure_class="verify_fail")
    rows = await fetch_recent_outcomes(hist_db, model_name="m", project_id="proj")
    # provider_error excluded; verify_fail retained
    classes = {r["failure_class"] for r in rows}
    assert classes == {"verify_fail"}


@pytest.mark.asyncio
async def test_fetch_respects_limit(hist_db):
    for _ in range(5):
        await _add(hist_db, model_name="m", project_id="proj")
    rows = await fetch_recent_outcomes(hist_db, model_name="m", project_id="proj", limit=2)
    assert len(rows) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_capability_history.py -k fetch -v`
Expected: FAIL (`fetch_recent_outcomes` not defined).

- [ ] **Step 3: Write the implementation**

Append to `src/orchestrator/core/capability_history.py`:

```python
from typing import Any

from orchestrator.core.failure_taxonomy import FailureClass, counts_against_worker


# Worker-attributable failure classes, as string values, for the SQL filter.
_ATTRIBUTABLE_FAIL_CLASSES: tuple[str, ...] = tuple(
    fc.value for fc in FailureClass if counts_against_worker(fc)
)


async def fetch_recent_outcomes(
    db: Any,
    model_name: str | None,
    project_id: str | None,
    limit: int = 100,
) -> list[dict]:
    """Return recent worker-attributable task_outcomes rows, newest first.

    Scoped to ``(model_name, project_id)``; if that yields nothing, falls back
    to ``(model_name, *)`` so a fresh project still benefits from what the model
    achieved elsewhere. A ``pass`` row always counts as capability evidence; a
    ``fail`` row is included only when its ``failure_class`` counts against the
    worker (S6), so provider errors and human merge-gate rejections never teach
    the system "this model can't do X". Rows are ordered ``created_at DESC`` so
    the caller's summary is recency-weighted by truncation at ``limit``.
    """
    placeholders = ",".join("?" for _ in _ATTRIBUTABLE_FAIL_CLASSES)
    attributable_clause = (
        f"(outcome = 'pass' OR "
        f"(outcome = 'fail' AND failure_class IN ({placeholders})))"
    )

    async def _query(project_scoped: bool) -> list[dict]:
        params: list[Any] = [model_name]
        sql = (
            "SELECT * FROM task_outcomes "
            "WHERE model_name = ? AND source = 'run' "
        )
        if project_scoped:
            sql += "AND project_id = ? "
            params.append(project_id)
        sql += f"AND {attributable_clause} "
        params.extend(_ATTRIBUTABLE_FAIL_CLASSES)
        sql += "ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = await db.fetch_all(sql, tuple(params))
        return [dict(r) for r in rows]

    if project_id is not None:
        scoped = await _query(project_scoped=True)
        if scoped:
            return scoped
    return await _query(project_scoped=False)
```

Note: `summarize_outcomes` reads `outcome`, `task_type`, `files_touched`, `loc_delta` — all present on these rows, so the fetched dicts feed straight into it. Do not change `summarize_outcomes`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_capability_history.py -v`
Expected: PASS (existing + 4 new).

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/capability_history.py tests/test_capability_history.py
git commit -m "feat: fetch_recent_outcomes scoped, attribution-filtered query"
```

---

### Task 5: Wire recording into `review_task`

**Files:**
- Modify: `src/orchestrator/core/orchestrator_review.py` (`ReviewMixin.review_task`)
- Test: `tests/test_outcome_recording_review.py` (new)

**Depends on:** Task 2, Task 3

- [ ] **Step 1: Write the failing test**

Study the existing `tests/test_orchestrator_review.py` for the established `ReviewMixin` fixture/stub pattern (stub `_git`, `_opus`, `_tq`, `_bus`) and reuse it. The new test asserts a row is written on each terminal verdict.

```python
# tests/test_outcome_recording_review.py
import pytest
from orchestrator.database import Database
# Reuse whatever harness tests/test_orchestrator_review.py uses to build a
# ReviewMixin-bearing Orchestrator with stubbed collaborators. Import or
# replicate that builder here rather than hand-rolling a second one.


@pytest.mark.asyncio
async def test_pass_verdict_records_pass_outcome(review_orchestrator_factory):
    """A PASS review (parked or merged) writes an outcome row with outcome='pass'."""
    orch, db, task_id, project = await review_orchestrator_factory(
        verdict="pass", diff="+++ b/f.py\n@@ -0,0 +1 @@\n+x\n"
    )
    await orch.review_task(task_id, project)
    row = await db.fetch_one(
        "SELECT * FROM task_outcomes WHERE task_id = ?", (task_id,)
    )
    assert row is not None
    assert row["outcome"] == "pass"
    assert row["files_touched"] == 1


@pytest.mark.asyncio
async def test_fail_verdict_records_fail_with_class(review_orchestrator_factory):
    orch, db, task_id, project = await review_orchestrator_factory(
        verdict="fail", diff="+++ b/f.py\n@@ -1 +1 @@\n-a\n+b\n"
    )
    await orch.review_task(task_id, project)
    row = await db.fetch_one(
        "SELECT * FROM task_outcomes WHERE task_id = ?", (task_id,)
    )
    assert row["outcome"] == "fail"
    assert row["failure_class"] == "fixable_in_place"


@pytest.mark.asyncio
async def test_verify_gate_fail_records_verify_fail(review_orchestrator_factory):
    orch, db, task_id, project = await review_orchestrator_factory(
        verify_gate_fails=True, verify_cmd="pytest -q"
    )
    await orch.review_task(task_id, project)
    row = await db.fetch_one(
        "SELECT * FROM task_outcomes WHERE task_id = ?", (task_id,)
    )
    assert row["failure_class"] == "verify_fail"
```

If `test_orchestrator_review.py` has no reusable factory, add a `review_orchestrator_factory` fixture to `tests/conftest.py` that builds a real `Database` (connected + initialized), a `TaskQueue` over it, a seeded project + plan + task in `REVIEWING` with a `pr_url`, and an `Orchestrator` whose `_git`/`_opus` are stubs returning the requested diff and verdict. Keep the factory in one place so Task 5's tests and future review tests share it.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_outcome_recording_review.py -v`
Expected: FAIL (no rows written yet).

- [ ] **Step 3: Add recording to `review_task`**

In `orchestrator_review.py`, import at top:

```python
from orchestrator.core.diff_stats import diff_stats
from orchestrator.core.failure_taxonomy import FailureClass
from orchestrator.core.outcome_recorder import record_outcome
```

Inside `review_task`, after `plan_text_for_review` is resolved, also capture the leaf's `task_type` from the same `plan_task` dict:

```python
            plan_text_for_review = plan_task.get("plan_text")
            task_type_for_outcome = plan_task.get("task_type")
```

Initialize `task_type_for_outcome = None` before the `if plan is not None:` block so it is always bound.

Add a small local helper right before the `verdict == "pass"` branch (so both branches can call it). It computes stats once and records:

```python
        files_touched, loc_delta = diff_stats(diff)

        async def _record(outcome: str, failure_class: str | None) -> None:
            await record_outcome(
                self._tq._db,
                task_id=task_id,
                plan_id=task.get("plan_id"),
                project_id=project["id"],
                model_name=project.get("agent_model") or project.get("model_name"),
                harness=project.get("harness"),
                task_type=task_type_for_outcome,
                files_touched=files_touched,
                loc_delta=loc_delta,
                context_tokens_est=None,
                attempt=int(task["attempt"]),
                outcome=outcome,
                failure_class=failure_class,
                emitter=getattr(self, "_emitter", None),
            )
```

Then record at each terminal point:

- In the supply-chain block, right before `await self._tq.mark_passed(task_id, feedback)`:
  ```python
                await _record("pass", None)
  ```
- In the auto-merge block, before `await self._git.merge_pr(...)`:
  ```python
                await _record("pass", None)
  ```
- In the parked-pass block, before `await self._tq.mark_passed(task_id, feedback)`:
  ```python
            await _record("pass", None)
  ```
- In the fail path, right before `await self._git.comment_on_pr(...)`:
  ```python
        fail_class = (
            FailureClass.VERIFY_FAIL.value
            if (verify_cmd and checkout is not None and review is not None
                and "Automated verification failed" in feedback)
            else FailureClass.FIXABLE_IN_PLACE.value
        )
        await _record("fail", fail_class)
```

Note: recording happens exactly once per `review_task` invocation (one verdict per call). `_record` swallows its own errors (Task 3), so it can never wedge the merge/retry flow. Do not record in `approve_task_merge` or `reject_task_merge` (human actions, not review verdicts).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_outcome_recording_review.py tests/test_orchestrator_review.py -v`
Expected: PASS (new tests pass; pre-existing review tests still pass).

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/orchestrator_review.py tests/test_outcome_recording_review.py tests/conftest.py
git commit -m "feat: record task outcomes at every terminal review verdict"
```

---

### Task 6: Wire `fetch_recent_outcomes` into `decompose_plan`

**Files:**
- Modify: `src/orchestrator/core/execute_plan_decompose.py` (`decompose_plan` signature + history line)
- Modify: `src/orchestrator/core/orchestrator.py:140` (pass `db=self._tq._db`)
- Test: `tests/test_execute_plan_decompose.py` (extend)

**Depends on:** Task 4

- [ ] **Step 1: Write the failing test**

```python
# tests/test_execute_plan_decompose.py  (append)
import pytest
from orchestrator.database import Database
from orchestrator.core.execute_plan_decompose import decompose_plan
from orchestrator.core.outcome_recorder import record_outcome
# Reuse the existing router/effective_settings stubs already defined in this
# test module (they drive the other decompose_plan tests).


@pytest.mark.asyncio
async def test_decompose_passes_real_history_when_db_given(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "d.db"))
    await db.connect()
    await db.initialize()
    await record_outcome(
        db, task_id="t", plan_id="p", project_id="proj", model_name="m",
        harness="opencode", task_type="feature", files_touched=2, loc_delta=9,
        context_tokens_est=None, attempt=1, outcome="pass", failure_class=None,
    )

    captured = {}
    import orchestrator.core.execute_plan_decompose as mod

    real_build = mod.build_review_prompt

    def spy(plan, profile, history, budget):
        captured["history"] = history
        return real_build(plan, profile, history, budget)

    monkeypatch.setattr(mod, "build_review_prompt", spy)

    await decompose_plan(
        plan="### Task 1\ndo a thing",
        model="m",
        context=None,
        router=<existing stub router that returns a valid one-leaf plan>,
        effective_settings=<existing stub effective_settings>,
        project_id="proj",
        db=db,
    )
    assert "no prior run history" not in captured["history"]
    assert "feature" in captured["history"]
    await db.close()
```

Fill the `<existing stub ...>` placeholders with the concrete stubs already used by the other tests in this file (copy the pattern from `test_decompose_plan_returns_normalized_opus_plan`).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_execute_plan_decompose.py -k real_history -v`
Expected: FAIL (`decompose_plan` has no `db` param → `TypeError`).

- [ ] **Step 3: Thread `db` and use it**

In `execute_plan_decompose.py`, add `db: Any = None` to the `decompose_plan` signature (place it after `emitter`), and add its docstring line. Replace:

```python
    history = summarize_outcomes([])
```

with:

```python
    if db is not None:
        runs = await fetch_recent_outcomes(
            db, model_name=model, project_id=project_id, limit=100
        )
    else:
        runs = []
    history = summarize_outcomes(runs)
```

Update the import:

```python
from orchestrator.core.capability_history import (
    fetch_recent_outcomes,
    summarize_outcomes,
)
```

Then in `orchestrator.core.orchestrator.py` at the `decompose_plan(...)` call (line ~140), add:

```python
                emitter=self._emitter,
                db=self._tq._db,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_execute_plan_decompose.py -v`
Expected: PASS (existing tests still pass — `db` defaults to `None`, preserving `summarize_outcomes([])` behavior — plus the new one).

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/execute_plan_decompose.py src/orchestrator/core/orchestrator.py tests/test_execute_plan_decompose.py
git commit -m "feat: feed recorded outcomes into decomposition history slot"
```

---

### Task 7: S11 correlation-ID logging helper

**Files:**
- Create: `src/orchestrator/core/log_context.py`
- Modify: `src/orchestrator/core/orchestrator_review.py` (use `task_logger` in `review_task`)
- Test: `tests/test_log_context.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_log_context.py
import logging
from orchestrator.core.log_context import task_logger


def test_task_logger_prefixes_ids(caplog):
    base = logging.getLogger("praxis.test.logctx")
    adapter = task_logger(base, plan_id="p1", task_id="t1", run_id="r1")
    with caplog.at_level(logging.INFO, logger="praxis.test.logctx"):
        adapter.info("hello")
    assert "plan=p1" in caplog.text
    assert "task=t1" in caplog.text
    assert "run=r1" in caplog.text
    assert "hello" in caplog.text


def test_task_logger_omits_absent_ids(caplog):
    base = logging.getLogger("praxis.test.logctx2")
    adapter = task_logger(base, task_id="t9")
    with caplog.at_level(logging.INFO, logger="praxis.test.logctx2"):
        adapter.info("world")
    assert "task=t9" in caplog.text
    assert "plan=" not in caplog.text
    assert "run=" not in caplog.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_log_context.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Write the implementation**

```python
# src/orchestrator/core/log_context.py
"""Correlation-ID logging (S11).

A tiny LoggerAdapter so every orchestrator log line for a given task carries
its plan_id / task_id / run_id, without adopting a structlog stack.
Reconstructing one task's journey stops being a free-text grep.
"""

from __future__ import annotations

import logging


class _IdAdapter(logging.LoggerAdapter):
    def process(self, msg: str, kwargs: dict) -> tuple[str, dict]:
        ids = self.extra or {}
        parts = [f"{k}={v}" for k, v in ids.items() if v is not None]
        prefix = f"[{' '.join(parts)}] " if parts else ""
        return f"{prefix}{msg}", kwargs


def task_logger(
    base: logging.Logger,
    *,
    plan_id: str | None = None,
    task_id: str | None = None,
    run_id: str | None = None,
) -> logging.LoggerAdapter:
    """Wrap ``base`` so each line is prefixed with the supplied ids.

    Absent ids are omitted from the prefix. Keys render as ``plan=``/``task=``/``run=``.
    """
    return _IdAdapter(
        base,
        {"plan": plan_id, "task": task_id, "run": run_id},
    )
```

- [ ] **Step 4: Apply it at one hot path (proof-of-use)**

At the top of `review_task` in `orchestrator_review.py`, after `task` is fetched and validated, add:

```python
        log = task_logger(logger, plan_id=task.get("plan_id"), task_id=task_id)
        log.info("reviewing task (pr=%s)", task.get("pr_url"))
```

and import `from orchestrator.core.log_context import task_logger`. This is the single required call site for Plan 3; broader adoption is incremental and not required here.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_log_context.py tests/test_orchestrator_review.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/core/log_context.py src/orchestrator/core/orchestrator_review.py tests/test_log_context.py
git commit -m "feat: correlation-ID logging adapter (S11)"
```

---

### Task 8: Docs — CLAUDE.md gotcha index, architecture note, ROADMAP flip

**Files:**
- Modify: `CLAUDE.md` (Gotchas index)
- Modify: `docs/architecture.md` (capability-engine dataflow note)
- Modify: `docs/superpowers/ROADMAP.md` (Plan 3 status + Honest baseline)

**Depends on:** Task 5, Task 6, Task 7

- [ ] **Step 1: Add CLAUDE.md gotcha index lines**

In the `## Gotchas` condensed index of `CLAUDE.md`, add:

```markdown
- **Outcome recording is fire-and-forget** — `core/outcome_recorder.record_outcome` writes one `task_outcomes` row at every terminal `review_task` verdict and swallows its own DB/emit errors, so it can never wedge review. Failure attribution is decided ONLY by `core/failure_taxonomy.counts_against_worker` (S6), never ad-hoc string checks; `provider_error` and human merge-gate rejections never count against the worker.
- **Decomposition history is real now** — `decompose_plan(db=...)` feeds `fetch_recent_outcomes` (scoped `(model, project)` -> `(model, *)`, worker-attributable rows only) into the prompt's history slot; the old hardcoded `summarize_outcomes([])` no-op is gone when a `db` is threaded (the async loop path always threads it). Wilson-bound learned limits + `GET /api/capability` are still Plan 6, NOT this.
```

- [ ] **Step 2: Add architecture dataflow note**

In `docs/architecture.md`, add a short "Capability engine: outcome recording (F5)" note describing: review verdict -> `record_outcome` -> `task_outcomes` row (measured by `diff_stats`) -> next `decompose_plan` reads `fetch_recent_outcomes` into its history slot. Two to four sentences, no length bloat.

- [ ] **Step 3: Flip ROADMAP status**

In `docs/superpowers/ROADMAP.md`, change Plan 3 row status from `TODO (next)` to `DONE`, and in the "Honest baseline" section change the `execute_plan_decompose.py passes summarize_outcomes([])` bullet to reflect that outcomes are now recorded and fed back (F5-recording landed; F1 learned overlay still pending).

- [ ] **Step 4: Verify docs render / no broken references**

Run: `uv run pytest -q` (full suite, ensures nothing importable broke) and eyeball the three markdown files.
Expected: green suite; docs read cleanly.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md docs/architecture.md docs/superpowers/ROADMAP.md
git commit -m "docs: outcome-recording gotchas, architecture note, roadmap flip"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1 (migration), Task 2 (`diff_stats`), Task 7 (log_context) — no dependencies, run in parallel.
- **Wave 2:** Task 3 (`record_outcome`, depends on Task 1), Task 4 (`fetch_recent_outcomes`, depends on Task 1).
- **Wave 3:** Task 5 (wire into `review_task`, depends on Task 2 + Task 3), Task 6 (wire into `decompose_plan`, depends on Task 4).
- **Wave 4:** Task 8 (docs, depends on Task 5 + Task 6 + Task 7).

---

## Notes

- **No agent image rebuild required.** Every change is orchestrator-side Python; no harness entrypoint (`docker/*-agent/entrypoint.sh`) is touched, so the three agent images do not need rebuilding.
- **Back-compat is preserved by defaults.** `decompose_plan`'s new `db` defaults to `None` (old behavior), and `record_outcome` swallows its own errors, so partial adoption never breaks the loop.
- **Full-suite gate:** after Task 8, run `uv run pytest --cov=orchestrator --cov-report=term-missing` and `uv run ruff format src/ tests/ && uv run ruff check --fix src/ tests/` and `uv run mypy src/orchestrator/ --ignore-missing-imports` before opening the integration PR.
