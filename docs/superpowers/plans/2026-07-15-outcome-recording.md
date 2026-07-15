# Outcome Recording (F5-recording + S11) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record a `task_outcomes` row at every terminal review verdict, feed a scoped summary of past outcomes back into every future decomposition, and thread `plan_id`/`task_id`/`run_id` through orchestrator logs.

**Architecture:** This is Plan 3 of the capability-engine roadmap (`docs/superpowers/specs/2026-07-11-capability-engine-roadmap.md`, feature F5-recording + contract S11). Pure instrumentation: it does NOT change dispatch, review, or merge behavior. A new `task_outcomes` table is written by `core/outcome_recorder.record_outcome`, called from `ReviewMixin.review_task` at each terminal verdict. Diff size is measured by `core/diff_stats.diff_stats`. `core/capability_history.fetch_recent_outcomes` queries the table scoped `(model_name, project_id)` with fallback `(model_name, *)`, filtered to worker-attributable rows via the existing S6 taxonomy (`core/failure_taxonomy.counts_against_worker`), and its result replaces the hardcoded `summarize_outcomes([])` in `decompose_plan`. S11 adds a `core/log_context.py` `LoggerAdapter` helper. Wilson-bound learned limits and `GET /api/capability/{model}` are explicitly OUT of scope (Plan 6).

**Tech Stack:** Python 3.11, aiosqlite (raw SQL, versioned `Migration` framework in `database.py`), Pydantic event models (`core/capability_events.py`), pytest with `asyncio_mode = "auto"`.

**Decomposition note (kept shallow on purpose):** the writer and its call-site wiring are combined in one leaf each (Task 4, Task 5) so the authored dependency graph stays at depth 2 (max chain: migration -> writer+wiring -> docs). This respects the F2 `max_dep_depth` constraint with headroom.

---

## Scope boundary (read before starting)

IN scope (Plan 3 only):
- `task_outcomes` table (migration 5).
- `diff_stats(diff)` measurement helper.
- `record_outcome(...)` writer + `OutcomeRecordedEvent` emission (the event model already exists in `core/capability_events.py`).
- Recording wired into `review_task` at every terminal verdict.
- `fetch_recent_outcomes(...)` query + wiring into `decompose_plan` (replaces `summarize_outcomes([])`).
- S11 correlation-ID `LoggerAdapter` helper + application at one review hot path.
- Docs: CLAUDE.md gotcha index lines, `docs/architecture.md` note, ROADMAP status flip.

OUT of scope (do NOT implement; later plans own these):
- Wilson score lower bound / learned effective limits (Plan 6).
- `GET /api/capability/{model}` endpoint or dashboard panel (Plan 6).
- `source='benchmark'` producers. The column exists, defaults to `'run'`; do not add a benchmark writer.
- Token-count instrumentation (Plan 4). `context_tokens_est` is recorded as `None` when unknown.
- Split/escalate producers (Plan 5). No Plan 3 code path writes `escalated`/`superseded`.

## Pinned inter-leaf contracts (honor these signatures verbatim)

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
) -> str: ...

# core/capability_history.py
async def fetch_recent_outcomes(
    db: Any,
    model_name: str | None,
    project_id: str | None,
    limit: int = 100,
) -> list[dict]: ...

# core/log_context.py
def task_logger(
    base: logging.Logger,
    *,
    plan_id: str | None = None,
    task_id: str | None = None,
    run_id: str | None = None,
) -> logging.LoggerAdapter: ...
```

---

### Task 1: `task_outcomes` migration

**Files:**
- Modify: `src/orchestrator/database.py`
- Test: `tests/test_database_migrations.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test** asserting a `task_outcomes` table exists after `Database.connect()` + `initialize()`, with columns id, task_id, project_id, model_name, harness, task_type, files_touched, loc_delta, context_tokens_est, attempt, outcome, failure_class, split_depth, source, created_at. Confirm the connection accessor name against the class before using it.

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
    cols = {
        r["name"]
        for r in await db.fetch_all("PRAGMA table_info(task_outcomes)")
    }
    assert {
        "id", "task_id", "project_id", "model_name", "harness", "task_type",
        "files_touched", "loc_delta", "context_tokens_est", "attempt",
        "outcome", "failure_class", "split_depth", "source", "created_at",
    } <= cols
    await db.close()
```

- [ ] **Step 2: Run it, confirm FAIL** — `uv run pytest tests/test_database_migrations.py -v` (no such table).

- [ ] **Step 3: Add the migration** in `database.py` after `_migration_0004_capability_events`:

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

Append to `MIGRATIONS`: `Migration(5, "add task_outcomes calibration table", _migration_0005_task_outcomes),`.

- [ ] **Step 4: Run it, confirm PASS.**
- [ ] **Step 5: Commit** — `git commit -m "feat: add task_outcomes calibration table (F5-recording)"`.

---

### Task 2: `diff_stats` measurement helper

**Files:**
- Create: `src/orchestrator/core/diff_stats.py`
- Test: `tests/test_diff_stats.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test:**

```python
# tests/test_diff_stats.py
from orchestrator.core.diff_stats import diff_stats


def test_empty_diff_is_zero():
    assert diff_stats("") == (0, 0)


def test_counts_files_and_loc():
    diff = (
        "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n"
        "@@ -0,0 +1,2 @@\n+line one\n+line two\n"
        "diff --git a/bar.py b/bar.py\n--- a/bar.py\n+++ b/bar.py\n"
        "@@ -1,1 +0,0 @@\n-gone\n"
    )
    assert diff_stats(diff) == (2, 3)


def test_ignores_file_headers_in_loc():
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"
    assert diff_stats(diff) == (1, 2)


def test_new_file_from_dev_null_counts_once():
    diff = "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+hello\n"
    assert diff_stats(diff) == (1, 1)
```

- [ ] **Step 2: Run it, confirm FAIL** (module not found).
- [ ] **Step 3: Implement:**

```python
# src/orchestrator/core/diff_stats.py
"""Measure a unified git diff's size (files touched, lines changed) for F5."""

from __future__ import annotations


def diff_stats(diff: str) -> tuple[int, int]:
    """Return (files_touched, loc_delta) from a unified git diff.

    files_touched: number of ``+++ `` target-file headers.
    loc_delta: added + removed content lines (single leading ``+``/``-``),
    excluding the ``+++``/``---`` file headers.
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

- [ ] **Step 4: Run it, confirm PASS.**
- [ ] **Step 5: Commit** — `git commit -m "feat: add diff_stats helper for outcome measurement"`.

---

### Task 3: `log_context` correlation-ID helper (S11)

**Files:**
- Create: `src/orchestrator/core/log_context.py`
- Test: `tests/test_log_context.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test:**

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

- [ ] **Step 2: Run it, confirm FAIL** (module not found).
- [ ] **Step 3: Implement:**

```python
# src/orchestrator/core/log_context.py
"""Correlation-ID logging (S11): prefix each line with plan/task/run ids."""

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
    """Wrap ``base`` so each line is prefixed with the supplied ids (None omitted)."""
    return _IdAdapter(base, {"plan": plan_id, "task": task_id, "run": run_id})
```

- [ ] **Step 4: Run it, confirm PASS.**
- [ ] **Step 5: Commit** — `git commit -m "feat: correlation-ID logging adapter (S11)"`.

---

### Task 4: `record_outcome` writer + wire into `review_task`

**Files:**
- Create: `src/orchestrator/core/outcome_recorder.py`
- Modify: `src/orchestrator/core/orchestrator_review.py`
- Test: `tests/test_outcome_recorder.py`, `tests/test_outcome_recording_review.py` (+ a shared factory in `tests/conftest.py` if none exists)

**Depends on:** Task 1, Task 2

- [ ] **Step 1: Write the failing writer test:**

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
        db, task_id="t1", plan_id="p1", project_id="proj1",
        model_name="qwen3.6-27b", harness="opencode", task_type="feature",
        files_touched=3, loc_delta=42, context_tokens_est=None,
        attempt=1, outcome="pass", failure_class=None,
    )
    row = await db.fetch_one("SELECT * FROM task_outcomes WHERE id = ?", (row_id,))
    assert row["outcome"] == "pass"
    assert row["files_touched"] == 3
    assert row["source"] == "run"
    assert row["split_depth"] == 0


@pytest.mark.asyncio
async def test_verify_fail_counts_against_worker(db):
    em = _CapturingEmitter()
    await record_outcome(
        db, task_id="t2", plan_id="p1", project_id="proj1",
        model_name="m", harness="opencode", task_type="refactor",
        files_touched=1, loc_delta=5, context_tokens_est=None,
        attempt=2, outcome="fail", failure_class="verify_fail", emitter=em,
    )
    assert em.events[0].event_type == "outcome_recorded"
    assert em.events[0].counts_against_worker is True


@pytest.mark.asyncio
async def test_provider_error_does_not_count(db):
    em = _CapturingEmitter()
    await record_outcome(
        db, task_id="t3", plan_id="p1", project_id="proj1",
        model_name="m", harness="opencode", task_type="feature",
        files_touched=0, loc_delta=0, context_tokens_est=None,
        attempt=1, outcome="fail", failure_class="provider_error", emitter=em,
    )
    assert em.events[0].counts_against_worker is False
```

- [ ] **Step 2: Run it, confirm FAIL** (module not found).
- [ ] **Step 3: Implement the writer** (pinned signature):

```python
# src/orchestrator/core/outcome_recorder.py
"""Record a task_outcomes row at every terminal review verdict (F5-recording).

Attribution is decided ONLY by the shared S6 taxonomy
(``core/failure_taxonomy.counts_against_worker``), never ad-hoc checks.
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

    Never raises into the caller: DB/emit failures are logged and swallowed.
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
        logger.warning("record_outcome: insert failed for %s: %s", task_id, exc)
        return row_id

    against = bool(failure_class) and counts_against_worker(failure_class)
    if emitter is not None and plan_id is not None:
        try:
            await emitter.emit(
                OutcomeRecordedEvent(
                    plan_id=plan_id, task_id=task_id, outcome_id=row_id,
                    failure_class=failure_class, counts_against_worker=against,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("record_outcome: emit failed for %s: %s", task_id, exc)
    return row_id
```

Note: `counts_against_worker` raises `ValueError` on an unknown string, so the `bool(failure_class) and ...` guard is required; a `None` class (a plain pass) yields `against = False`.

- [ ] **Step 4: Run the writer test, confirm PASS.**

- [ ] **Step 5: Write the failing review-wiring test.** Reuse the ReviewMixin stub pattern from `tests/test_orchestrator_review.py` (put a shared factory in `tests/conftest.py` if none exists). Assert: a PASS verdict writes a row with `outcome='pass'` and correct `files_touched`; a FAIL verdict writes `outcome='fail'`, `failure_class='fixable_in_place'`; a verify-gate failure writes `failure_class='verify_fail'`.

```python
# tests/test_outcome_recording_review.py
import pytest


@pytest.mark.asyncio
async def test_pass_verdict_records_pass(review_orchestrator_factory):
    orch, db, task_id, project = await review_orchestrator_factory(
        verdict="pass", diff="+++ b/f.py\n@@ -0,0 +1 @@\n+x\n"
    )
    await orch.review_task(task_id, project)
    row = await db.fetch_one("SELECT * FROM task_outcomes WHERE task_id = ?", (task_id,))
    assert row["outcome"] == "pass"
    assert row["files_touched"] == 1


@pytest.mark.asyncio
async def test_fail_verdict_records_fixable(review_orchestrator_factory):
    orch, db, task_id, project = await review_orchestrator_factory(
        verdict="fail", diff="+++ b/f.py\n@@ -1 +1 @@\n-a\n+b\n"
    )
    await orch.review_task(task_id, project)
    row = await db.fetch_one("SELECT * FROM task_outcomes WHERE task_id = ?", (task_id,))
    assert row["outcome"] == "fail"
    assert row["failure_class"] == "fixable_in_place"


@pytest.mark.asyncio
async def test_verify_gate_fail_records_verify_fail(review_orchestrator_factory):
    orch, db, task_id, project = await review_orchestrator_factory(
        verify_gate_fails=True, verify_cmd="pytest -q"
    )
    await orch.review_task(task_id, project)
    row = await db.fetch_one("SELECT * FROM task_outcomes WHERE task_id = ?", (task_id,))
    assert row["failure_class"] == "verify_fail"
```

- [ ] **Step 6: Run it, confirm FAIL** (no rows).

- [ ] **Step 7: Wire into `review_task`** (`orchestrator_review.py`). Add imports:

```python
from orchestrator.core.diff_stats import diff_stats
from orchestrator.core.failure_taxonomy import FailureClass
from orchestrator.core.outcome_recorder import record_outcome
```

Initialize `task_type_for_outcome = None` before the `if plan is not None:` block; inside it, alongside `plan_text_for_review = plan_task.get("plan_text")`, add `task_type_for_outcome = plan_task.get("task_type")`. After `feedback` is computed and before the `verdict == "pass"` branch, add:

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

Call `await _record("pass", None)` at each of the three pass terminal points (supply-chain park before `mark_passed`; auto-merge before `merge_pr`; parked-pass before `mark_passed`). In the fail path, right before `comment_on_pr`, add:

```python
        fail_class = (
            FailureClass.VERIFY_FAIL.value
            if "Automated verification failed" in feedback
            else FailureClass.FIXABLE_IN_PLACE.value
        )
        await _record("fail", fail_class)
```

Record exactly once per invocation. Do NOT record in `approve_task_merge`/`reject_task_merge` (human actions).

Also add S11 proof-of-use at the top of `review_task` after the task is fetched+validated:

```python
        from orchestrator.core.log_context import task_logger
        log = task_logger(logger, plan_id=task.get("plan_id"), task_id=task_id)
        log.info("reviewing task (pr=%s)", task.get("pr_url"))
```

- [ ] **Step 8: Run both test files, confirm PASS** (new + pre-existing review tests).
- [ ] **Step 9: Commit** — `git commit -m "feat: record task outcomes at every terminal review verdict"`.

---

### Task 5: `fetch_recent_outcomes` query + wire into `decompose_plan`

**Files:**
- Modify: `src/orchestrator/core/capability_history.py` (ADD `fetch_recent_outcomes`, leave `summarize_outcomes` untouched)
- Modify: `src/orchestrator/core/execute_plan_decompose.py`, `src/orchestrator/core/orchestrator.py`
- Test: `tests/test_capability_history.py`, `tests/test_execute_plan_decompose.py`

**Depends on:** Task 1

- [ ] **Step 1: Write the failing query tests** (insert rows via `record_outcome`): scopes by (model, project); falls back to (model, *) when the project has no rows; excludes non-attributable failures (provider_error dropped, verify_fail kept); respects `limit`.

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
    await _add(hist_db, model_name="m", project_id="proj")
    await _add(hist_db, model_name="other", project_id="proj")
    rows = await fetch_recent_outcomes(hist_db, model_name="m", project_id="proj")
    assert [r["model_name"] for r in rows] == ["m"]


@pytest.mark.asyncio
async def test_fetch_falls_back_to_model_wide(hist_db):
    await _add(hist_db, model_name="m", project_id="otherproj")
    rows = await fetch_recent_outcomes(hist_db, model_name="m", project_id="proj")
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_fetch_excludes_non_attributable_fail(hist_db):
    await _add(hist_db, model_name="m", project_id="proj", outcome="fail", failure_class="provider_error")
    await _add(hist_db, model_name="m", project_id="proj", outcome="fail", failure_class="verify_fail")
    rows = await fetch_recent_outcomes(hist_db, model_name="m", project_id="proj")
    assert {r["failure_class"] for r in rows} == {"verify_fail"}


@pytest.mark.asyncio
async def test_fetch_respects_limit(hist_db):
    for _ in range(5):
        await _add(hist_db, model_name="m", project_id="proj")
    rows = await fetch_recent_outcomes(hist_db, model_name="m", project_id="proj", limit=2)
    assert len(rows) == 2
```

- [ ] **Step 2: Run it, confirm FAIL** (`fetch_recent_outcomes` undefined).
- [ ] **Step 3: Implement in `capability_history.py`:**

```python
from typing import Any

from orchestrator.core.failure_taxonomy import FailureClass, counts_against_worker


_ATTRIBUTABLE_FAIL_CLASSES: tuple[str, ...] = tuple(
    fc.value for fc in FailureClass if counts_against_worker(fc)
)


async def fetch_recent_outcomes(
    db: Any,
    model_name: str | None,
    project_id: str | None,
    limit: int = 100,
) -> list[dict]:
    """Recent worker-attributable task_outcomes rows, newest first.

    Scoped ``(model_name, project_id)``; falls back to ``(model_name, *)`` when
    the project has no rows. A ``pass`` always counts; a ``fail`` counts only if
    its ``failure_class`` counts against the worker (S6). ``ORDER BY created_at
    DESC LIMIT`` makes the caller's summary recency-weighted.
    """
    placeholders = ",".join("?" for _ in _ATTRIBUTABLE_FAIL_CLASSES)
    clause = (
        f"(outcome = 'pass' OR "
        f"(outcome = 'fail' AND failure_class IN ({placeholders})))"
    )

    async def _query(scoped: bool) -> list[dict]:
        params: list[Any] = [model_name]
        sql = "SELECT * FROM task_outcomes WHERE model_name = ? AND source = 'run' "
        if scoped:
            sql += "AND project_id = ? "
            params.append(project_id)
        sql += f"AND {clause} "
        params.extend(_ATTRIBUTABLE_FAIL_CLASSES)
        sql += "ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in await db.fetch_all(sql, tuple(params))]

    if project_id is not None:
        scoped_rows = await _query(scoped=True)
        if scoped_rows:
            return scoped_rows
    return await _query(scoped=False)
```

Do NOT change `summarize_outcomes`; its keys (outcome/task_type/files_touched/loc_delta) already match these rows.

- [ ] **Step 4: Run it, confirm PASS.**

- [ ] **Step 5: Write the failing decompose-wiring test.** With a real Database holding one recorded `pass` outcome for model "m", `decompose_plan(..., db=db)` (spy on `build_review_prompt`) passes a `history` NOT containing "no prior run history" and containing "feature". Reuse the router/effective_settings stubs from the other tests in the file.

- [ ] **Step 6: Run it, confirm FAIL** (`db` is not a param -> TypeError).

- [ ] **Step 7: Thread `db`.** In `execute_plan_decompose.py` add `db: Any = None` after `emitter` in the `decompose_plan` signature. Replace `history = summarize_outcomes([])` with:

```python
    if db is not None:
        runs = await fetch_recent_outcomes(
            db, model_name=model, project_id=project_id, limit=100
        )
    else:
        runs = []
    history = summarize_outcomes(runs)
```

Update the import to `from orchestrator.core.capability_history import (fetch_recent_outcomes, summarize_outcomes)`. In `orchestrator.py` at the `decompose_plan(...)` call, add `db=self._tq._db,`.

- [ ] **Step 8: Run the full decompose test file, confirm PASS** (existing tests unaffected: `db` defaults to None).
- [ ] **Step 9: Commit** — `git commit -m "feat: feed recorded outcomes into decomposition history slot"`.

---

### Task 6: Docs — CLAUDE.md gotchas, architecture note, ROADMAP flip

**Files:**
- Modify: `CLAUDE.md`, `docs/architecture.md`, `docs/superpowers/ROADMAP.md`

**Depends on:** Task 4, Task 5

- [ ] **Step 1:** Add two gotcha-index lines to `CLAUDE.md`:
  - Outcome recording is fire-and-forget: `core/outcome_recorder.record_outcome` writes one `task_outcomes` row per terminal `review_task` verdict and swallows its own DB/emit errors; attribution is decided ONLY by `core/failure_taxonomy.counts_against_worker` (S6); `provider_error` and human merge-gate rejections never count against the worker.
  - Decomposition history is real now: `decompose_plan(db=...)` feeds `fetch_recent_outcomes` (scoped `(model, project)` -> `(model, *)`, worker-attributable rows only) into the prompt history slot; Wilson-bound learned limits + `GET /api/capability` remain Plan 6.

- [ ] **Step 2:** Add a short "Capability engine: outcome recording (F5)" note to `docs/architecture.md` (review verdict -> `record_outcome` -> `task_outcomes` row measured by `diff_stats` -> next `decompose_plan` reads `fetch_recent_outcomes` into its history slot; 2-4 sentences, no length bloat).

- [ ] **Step 3:** In `docs/superpowers/ROADMAP.md` flip Plan 3 status to `DONE` and update the Honest-baseline `summarize_outcomes([])` bullet to say outcomes are now recorded and fed back (F5-recording landed; F1 overlay still pending).

- [ ] **Step 4: Verify** — `uv run pytest -q` (nothing importable broke).
- [ ] **Step 5: Commit** — `git commit -m "docs: outcome-recording gotchas, architecture note, roadmap flip"`.

---

## Parallel Execution Map

- **Wave 1:** Task 1, Task 2, Task 3 (no dependencies).
- **Wave 2:** Task 4 (depends on Task 1, Task 2), Task 5 (depends on Task 1).
- **Wave 3:** Task 6 (depends on Task 4, Task 5).

Authored max dependency depth = 2 (migration -> writer+wiring -> docs), well inside the F2 `max_dep_depth=3` limit.

---

## Notes

- **No agent image rebuild required.** All changes are orchestrator-side Python; no harness entrypoint is touched.
- **Back-compat via defaults.** `decompose_plan`'s new `db` defaults to `None` (old behavior); `record_outcome` swallows its own errors.
- **Full-suite gate before the integration PR:** `uv run pytest --cov=orchestrator`, `uv run ruff format`, `uv run ruff check --fix`, `uv run mypy src/orchestrator/ --ignore-missing-imports`.
