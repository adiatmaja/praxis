# Refactor: Orchestrator Split, Dashboard Modularization, Migrations, Naming Debt

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pay down the four structural debts identified in the 2026-07-02 code review: split the 1055-line `core/orchestrator.py` into focused modules, extract CSS/JS out of the 3.6K-line `web/index.html`, introduce a versioned SQLite migration framework, and fix naming debt (container prefix, `token_hash` semantics).

**Architecture:** All work is behavior-preserving refactoring guarded by the existing 507-test suite. The orchestrator split uses mixin classes so the public `Orchestrator` API and all call sites stay identical. The dashboard split keeps the no-build-step constraint (plain `<link>` + `<script src>`, no bundler). The migration framework is a `PRAGMA user_version` ordered-step list layered on top of the existing idempotent `CREATE TABLE IF NOT EXISTS` baseline.

**Tech Stack:** Python 3.11, FastAPI, aiosqlite (raw SQL, no ORM), pytest (asyncio_mode=auto), ruff, mypy. Dashboard is plain HTML/CSS/JS served by FastAPI `StaticFiles`.

---

## Context for a fresh session (read this first)

You are working in `C:\working-space\praxis` (repo: https://github.com/adiatmaja/praxis.git, branch off `main`). Praxis is an AI agent orchestrator: a FastAPI backend dispatches coding tasks to Docker agent containers and routes planning/review calls to LLM provider CLIs. Read `CLAUDE.md` at the repo root before starting; its Gotchas section is load-bearing.

Facts this plan relies on (verified 2026-07-02):

- `src/orchestrator/core/orchestrator.py` is 1055 lines, one `Orchestrator` class holding dispatch, review, merge, reconcile, improvement, and loop logic. Method map (line numbers from 2026-07-02, treat as anchors not gospel):
  - loop/planning core: `__init__` (38), `plan_and_activate` (75), `process_plan_once` (689), `shutdown` (998), `run_once` (1008), `run_loop` (1023)
  - dispatch: `dispatch_pending_tasks` (107), `_build_worker_bible` (199)
  - review/merge: `review_task` (248), `approve_task_merge` (388), `reject_task_merge` (422), `_sync_plan_checkbox` (467), `on_plan_completed` (575), `_classify_pr_failure` (988)
  - reconcile/monitor: `_safe_logs` (769), `reconcile_runs` (778), `_start_monitor` (812), `monitor_run` (817), `_reconcile_exited` (863), `_fail_orphan` (891), `_resolve_failed_run` (900), `_decide_escalation` (969)
  - improvement: `check_improvements` (596), `create_improvement_plan` (652)
- Tests patch module-level names on the orchestrator module: `orchestrator.core.orchestrator.clone_with_token`, `orchestrator.core.orchestrator.commit_and_push`, `orchestrator.core.orchestrator.run_verify` (all in `tests/test_orchestrator.py`). When the code that calls these moves to a new module, the patch targets MUST move with it or the mocks silently stop applying.
- `web/index.html` is 3598 lines: one `<style>` block starting at line 10, one `<script>` block starting at line 1224 and running to the end. It is served by `app.mount("/", StaticFiles(directory=str(web_dir), html=True))` in `src/orchestrator/main.py:204-206`, so sibling files in `web/` are served automatically at `/styles.css`, `/app.js`.
- `src/orchestrator/database.py` (280 lines) creates 8 tables via inline `CREATE TABLE IF NOT EXISTS` in `initialize()` (line 145), plus a conditional legacy rebuild of `plans` (drops the old `spec` column) guarded by `PRAGMA table_info(plans)`. There is no schema version tracking today.
- Container names are `aider-agent-{task_id[:8]}` regardless of harness (`src/orchestrator/core/agent_manager.py`, also the `list_agent_containers` name filter). Tests: `tests/test_agent_manager.py`.
- `users.token_hash` stores the RAW auth token, not a hash (documented in `src/orchestrator/api/auth.py` docstring). We are NOT renaming the column in this plan (schema churn for zero behavior gain); we are documenting it and adding the migration framework that would make a future rename safe.

Verification commands (run from repo root; all must pass before every commit):

```bash
uv run pytest -q                     # 507+ tests, ~35s
uv run ruff format --check src/ tests/
uv run ruff check src/ tests/
uv run mypy src/orchestrator/ --ignore-missing-imports
```

Hard rules for every task:

1. **Move code verbatim.** No renames, no "improvements", no reordering of logic inside moved methods. The diff for a move task should be imports + file boundaries only.
2. Run the full suite after every extraction, not just at the end.
3. One commit per task, conventional commit format (`refactor:`, `feat:`, `docs:`). Do not add AI attribution lines to commit messages.
4. If a step's line anchors don't match (the file has drifted), locate the method by name, not by line.

## Non-Goals (explicitly out of scope)

- Renaming `opus_bridge.py` / `OpusBridge`: it is referenced across docs, tests, and memory notes; module-only rename is churn without clarity gain. Task 12 documents the provider-agnostic reality instead.
- Renaming the `users.token_hash` column: needs a table rebuild for zero behavior change. The migration framework (Tasks 8-9) makes this cheap later.
- Further splitting `web/app.js` into ES modules: do the safe two-step extraction first (Tasks 6-7); finer module splits are a follow-up once the extraction has soaked.
- Any behavior change whatsoever.

---

### Task 1: Extract ReconcileMixin (`orchestrator_reconcile.py`)

**Files:**
- Create: `src/orchestrator/core/orchestrator_reconcile.py`
- Modify: `src/orchestrator/core/orchestrator.py`
- Tests affected: `tests/test_orchestrator.py` (no patch-target changes needed for this task; reconcile code patches nothing module-level)

**Depends on:** None

- [ ] **Step 1: Confirm the reconcile method set and its module-level dependencies**

Run:
```bash
grep -n "_safe_logs\|reconcile_runs\|_start_monitor\|monitor_run\|_reconcile_exited\|_fail_orphan\|_resolve_failed_run\|_decide_escalation" src/orchestrator/core/orchestrator.py
```
Expected: definitions around lines 769-987 plus call sites in `dispatch_pending_tasks` (`self._start_monitor`) and `run_once` (`self.reconcile_runs`). Also check which imports these methods use (e.g. `asyncio`, `contextlib`, `datetime`, `TaskStatus`); you will need exactly those in the new module.

- [ ] **Step 2: Create the new module with the mixin skeleton**

Create `src/orchestrator/core/orchestrator_reconcile.py`:

```python
"""Agent-run reconciliation and live-log monitoring.

Extracted verbatim from core/orchestrator.py (2026-07-02 refactor). This is a
mixin: it is only ever mixed into ``Orchestrator`` and reads attributes set in
``Orchestrator.__init__``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from orchestrator.models.schemas import TaskStatus


if TYPE_CHECKING:
    from orchestrator.core.agent_manager import AgentManager
    from orchestrator.core.event_bus import EventBus
    from orchestrator.core.task_queue import TaskQueue


logger = logging.getLogger(__name__)


class ReconcileMixin:
    """Reconciliation half of the Orchestrator (see class Orchestrator)."""

    # Attributes provided by Orchestrator.__init__ (declared for mypy only).
    if TYPE_CHECKING:
        _queue: TaskQueue
        _agent_manager: AgentManager | None
        _event_bus: EventBus
        _monitor_tasks: dict[str, asyncio.Task[None]]
```

IMPORTANT: the attribute names above are illustrative. Open `Orchestrator.__init__` and copy the REAL attribute names the moved methods reference (`grep -o "self\._[a-z_]*" ` on the moved bodies). Declare exactly those. Adjust the import list to what the moved bodies actually use; delete unused ones (ruff will flag them).

- [ ] **Step 3: Move the eight methods verbatim**

Cut these methods (whole bodies, decorators, docstrings) from `orchestrator.py` and paste them unchanged inside `ReconcileMixin`: `_safe_logs`, `reconcile_runs`, `_start_monitor`, `monitor_run`, `_reconcile_exited`, `_fail_orphan`, `_resolve_failed_run`, `_decide_escalation`.

- [ ] **Step 4: Make Orchestrator inherit the mixin**

In `orchestrator.py`:

```python
from orchestrator.core.orchestrator_reconcile import ReconcileMixin


class Orchestrator(ReconcileMixin):
    ...
```

Remove imports that are now unused in `orchestrator.py` (ruff check will list them).

- [ ] **Step 5: Verify**

Run: `uv run pytest -q && uv run ruff format --check src/ tests/ && uv run ruff check src/ tests/ && uv run mypy src/orchestrator/ --ignore-missing-imports`
Expected: all pass. If mypy complains about attributes on the mixin, add them to the `TYPE_CHECKING` declarations block, do not restructure.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/core/orchestrator_reconcile.py src/orchestrator/core/orchestrator.py
git commit -m "refactor: extract ReconcileMixin from orchestrator.py"
```

---

### Task 2: Extract ReviewMixin (`orchestrator_review.py`)

**Files:**
- Create: `src/orchestrator/core/orchestrator_review.py`
- Modify: `src/orchestrator/core/orchestrator.py`
- Modify: `tests/test_orchestrator.py` (patch targets MOVE, see Step 4)

**Depends on:** Task 1

- [ ] **Step 1: Move the six methods verbatim**

Same mixin pattern as Task 1. Create `src/orchestrator/core/orchestrator_review.py` with class `ReviewMixin`, module docstring "PR review, merge approval, and plan-completion handling", and move these verbatim: `review_task`, `approve_task_merge`, `reject_task_merge`, `_sync_plan_checkbox`, `on_plan_completed`, `_classify_pr_failure`.

Bring along the module-level imports these bodies use. Critically, that includes (check the real list):

```python
from orchestrator.core.git_ops import ...   # whatever review_task uses, e.g. clone_with_token, commit_and_push
from orchestrator.core.verify_gate import run_verify
from orchestrator.core.merge_policy import auto_merge_eligible
from orchestrator.core.diff_guard import destructive_deletions
```

- [ ] **Step 2: Add the mixin to the class**

```python
class Orchestrator(DispatchMixin, ReviewMixin, ReconcileMixin):  # DispatchMixin comes in Task 3; at this point: (ReviewMixin, ReconcileMixin)
```

- [ ] **Step 3: Run tests, expect targeted failures**

Run: `uv run pytest tests/test_orchestrator.py -q`
Expected: tests that patch `orchestrator.core.orchestrator.run_verify`, `...clone_with_token`, `...commit_and_push` now FAIL (the real functions run or mocks are not seen), because `review_task` now looks these names up in `orchestrator_review`'s namespace. If they unexpectedly pass, check whether `orchestrator.py` still imports those names; it must not (dead import would keep the old patch target working while the code ignores it, which is worse).

- [ ] **Step 4: Repoint the patch targets**

In `tests/test_orchestrator.py`, replace every occurrence:

```
orchestrator.core.orchestrator.run_verify        -> orchestrator.core.orchestrator_review.run_verify
orchestrator.core.orchestrator.clone_with_token  -> orchestrator.core.orchestrator_review.clone_with_token
orchestrator.core.orchestrator.commit_and_push   -> orchestrator.core.orchestrator_review.commit_and_push
```

(If Step 1 revealed that some of these are called from a method that did NOT move, keep that one pointing at its actual calling module. Patch where the name is looked up.)

- [ ] **Step 5: Verify**

Run the full verification block (pytest + ruff format + ruff check + mypy). Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/core/orchestrator_review.py src/orchestrator/core/orchestrator.py tests/test_orchestrator.py
git commit -m "refactor: extract ReviewMixin from orchestrator.py"
```

---

### Task 3: Extract DispatchMixin (`orchestrator_dispatch.py`)

**Files:**
- Create: `src/orchestrator/core/orchestrator_dispatch.py`
- Modify: `src/orchestrator/core/orchestrator.py`
- Tests affected: `tests/test_orchestrator.py`, `tests/test_worker_bible.py` (check for patch targets on names used by `dispatch_pending_tasks` / `_build_worker_bible`, e.g. `detect_context_limit`, `build_implementer_prompt`, `build_bible`; repoint any that move, same procedure as Task 2 Step 4)

**Depends on:** Task 2

- [ ] **Step 1: Move `dispatch_pending_tasks` and `_build_worker_bible` verbatim** into class `DispatchMixin` in the new module (docstring: "Task dispatch: spawning agent containers with prompt, bible, and budget"). Bring the imports the bodies use (`detect_context_limit`, `build_implementer_prompt`, `BibleSources`, `build_bible`, `ContextBudgetExceeded`, `render_handover`, `ChecklistItem`, `TaskStatus`, plus stdlib).

- [ ] **Step 2: Update the class line**

```python
class Orchestrator(DispatchMixin, ReviewMixin, ReconcileMixin):
```

- [ ] **Step 3: Find and repoint moved patch targets**

Run: `grep -n "orchestrator\.core\.orchestrator\." tests/*.py`
Expected: any hits referring to names now imported only by `orchestrator_dispatch` must be repointed to `orchestrator.core.orchestrator_dispatch.<name>`.

- [ ] **Step 4: Verify** (full block). Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add -A src/orchestrator/core/ tests/
git commit -m "refactor: extract DispatchMixin from orchestrator.py"
```

---

### Task 4: Extract ImprovementMixin (`orchestrator_improve.py`)

**Files:**
- Create: `src/orchestrator/core/orchestrator_improve.py`
- Modify: `src/orchestrator/core/orchestrator.py`

**Depends on:** Task 3

- [ ] **Step 1: Move `check_improvements` and `create_improvement_plan` verbatim** into class `ImprovementMixin` (docstring: "Autonomous improvement loop: confidence-gated follow-up plan creation"). Same mixin pattern, same import-carrying rule, same patch-target grep as Task 3 Step 3.

- [ ] **Step 2: Final class line and sanity check on what remains**

```python
class Orchestrator(DispatchMixin, ReviewMixin, ReconcileMixin, ImprovementMixin):
```

`orchestrator.py` should now contain only: module docstring, `__init__`, `plan_and_activate`, `process_plan_once`, `shutdown`, `run_once`, `run_loop`. Run `wc -l src/orchestrator/core/orchestrator.py`; expected under ~400 lines.

- [ ] **Step 3: Verify** (full block). Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add -A src/orchestrator/core/ tests/
git commit -m "refactor: extract ImprovementMixin; orchestrator.py is now the loop core"
```

---

### Task 5: Update CLAUDE.md and architecture docs for the split

**Files:**
- Modify: `CLAUDE.md` (Project Structure tree + the gotcha "orchestrator.py contains the improvement loop")
- Modify: `docs/architecture.md` (Components section if it names orchestrator.py responsibilities)

**Depends on:** Task 4

- [ ] **Step 1: Update the CLAUDE.md project tree** to list the four new `orchestrator_*.py` modules with one-line descriptions, and REPLACE the now-false gotcha "orchestrator.py contains the improvement loop — there is no separate core/improvement.py" with:

```markdown
- **Orchestrator is split across mixins** — `core/orchestrator.py` holds only the
  loop core (`__init__`, `plan_and_activate`, `process_plan_once`, `run_once`,
  `run_loop`, `shutdown`). Dispatch, review/merge, reconcile, and improvement live
  in `core/orchestrator_{dispatch,review,reconcile,improve}.py` as mixins on the
  single `Orchestrator` class. Tests patch module-level helpers (e.g. `run_verify`,
  `clone_with_token`) on the MIXIN module that calls them, not on
  `core.orchestrator`.
```

- [ ] **Step 2: Verify docs mention no stale paths**: `grep -n "improvement.py\|orchestrator.py contains" CLAUDE.md docs/*.md`. Expected: only the new wording.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/architecture.md
git commit -m "docs: document orchestrator mixin split"
```

---

### Task 6: Extract dashboard CSS to `web/styles.css`

**Files:**
- Create: `web/styles.css`
- Modify: `web/index.html` (style block starts at line 10)

**Depends on:** None (independent of Tasks 1-5)

- [ ] **Step 1: Move the CSS**

Cut everything between `<style>` and `</style>` in `web/index.html` (the block starting at line 10) into a new file `web/styles.css`, verbatim. Replace the block with:

```html
<link rel="stylesheet" href="/styles.css">
```

- [ ] **Step 2: Verify it serves and renders**

`StaticFiles` on `web/` serves siblings automatically; no backend change is needed. Run:

```bash
uv run uvicorn orchestrator.main:app --port 12323 &
sleep 3
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:12323/styles.css   # expect 200
curl -s http://127.0.0.1:12323/ | grep -c "stylesheet"                      # expect 1
```

Then open http://127.0.0.1:12323 in a browser (or via the Playwright MCP if available: navigate + screenshot) and confirm the dashboard is styled (dark theme, cards, nav) and the theme toggle still works. An unstyled white page means the link path is wrong.

- [ ] **Step 3: Commit**

```bash
git add web/styles.css web/index.html
git commit -m "refactor: extract dashboard CSS to web/styles.css"
```

---

### Task 7: Extract dashboard JS to `web/app.js`

**Files:**
- Create: `web/app.js`
- Modify: `web/index.html` (script block starts at line 1224, runs to end of file)

**Depends on:** Task 6

- [ ] **Step 1: Move the JS**

Cut everything between `<script>` and `</script>` (the block from line 1224 to the end) into `web/app.js`, verbatim. Replace with:

```html
<script src="/app.js" defer></script>
```

Use `defer` (NOT `type="module"`): the code is one big top-level script with functions referenced from inline `onclick=` attributes in the HTML; module scope would hide them from the global scope and break every button. Keeping it a classic deferred script is the behavior-preserving choice.

- [ ] **Step 2: Check for inline-handler dependencies**

Run: `grep -c "onclick=\|onchange=\|oninput=\|onsubmit=" web/index.html`
If greater than 0 (expected), the globals requirement above is confirmed; do not convert to a module in this plan.

- [ ] **Step 3: Verify in the browser**

Start the server as in Task 6. Confirm: dashboard loads, project list renders (API calls fire, check the network tab or server logs), theme toggle works, opening a view (Plans, Settings) works, no console errors. If the Playwright MCP is available, drive one full view-switch and screenshot. JS regressions here are silent, so do not skip the click-through.

- [ ] **Step 4: Update CLAUDE.md**

The tech-stack table row "Web UI | Single-file HTML/CSS/JS (`web/index.html`)" becomes "Web UI | No-build HTML/CSS/JS (`web/index.html` + `styles.css` + `app.js`)". Update the same phrase anywhere else it appears (`grep -rn "single-file" CLAUDE.md docs/ README.md`).

- [ ] **Step 5: Commit**

```bash
git add web/ CLAUDE.md docs/ README.md
git commit -m "refactor: extract dashboard JS to web/app.js (no-build, classic script)"
```

---

### Task 8: Migration framework: failing tests first

**Files:**
- Create: `tests/test_migrations.py`
- Test target: `src/orchestrator/database.py` (implementation comes in Task 9)

**Depends on:** None (independent of Tasks 1-7)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_migrations.py`:

```python
"""Versioned-migration framework tests (PRAGMA user_version based)."""

import pytest

from orchestrator.database import CURRENT_SCHEMA_VERSION, MIGRATIONS, Database


@pytest.mark.unit
async def test_fresh_db_lands_on_current_version(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/fresh.db")
    await db.initialize()
    row = await db.fetch_one("PRAGMA user_version")
    assert row[0] == CURRENT_SCHEMA_VERSION
    await db.close()


@pytest.mark.unit
async def test_initialize_is_idempotent(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/idem.db")
    await db.initialize()
    await db.close()
    db2 = Database(f"sqlite+aiosqlite:///{tmp_path}/idem.db")
    await db2.initialize()  # must not re-run migrations or error
    row = await db2.fetch_one("PRAGMA user_version")
    assert row[0] == CURRENT_SCHEMA_VERSION
    await db2.close()


@pytest.mark.unit
async def test_pending_migration_applies_in_order(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/mig.db")
    await db.initialize()
    # Simulate an old database: roll the version marker back.
    await db.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION - 1}")
    await db.close()

    db2 = Database(f"sqlite+aiosqlite:///{tmp_path}/mig.db")
    await db2.initialize()  # must re-apply only the last migration, harmlessly
    row = await db2.fetch_one("PRAGMA user_version")
    assert row[0] == CURRENT_SCHEMA_VERSION
    await db2.close()


@pytest.mark.unit
def test_migrations_are_contiguous_from_one():
    versions = [m.version for m in MIGRATIONS]
    assert versions == list(range(1, CURRENT_SCHEMA_VERSION + 1))
```

Note: `Database.fetch_one` may return a Row; if `row[0]` fails, adapt to the accessor the class provides (check `fetch_one`'s return in `database.py:260`). Also note migration steps must therefore be idempotent or guarded; the framework contract (Task 9) requires each step to be safe to re-run.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_migrations.py -q`
Expected: FAIL with `ImportError: cannot import name 'CURRENT_SCHEMA_VERSION'`.

- [ ] **Step 3: Commit the red tests**

```bash
git add tests/test_migrations.py
git commit -m "test: add failing tests for versioned migration framework"
```

---

### Task 9: Migration framework: implementation

**Files:**
- Modify: `src/orchestrator/database.py`

**Depends on:** Task 8

- [ ] **Step 1: Add the framework**

In `src/orchestrator/database.py`, add near the top (after the CREATE TABLE constants):

```python
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    """One ordered, idempotent schema step.

    ``apply`` receives the open connection. Steps MUST be safe to re-run
    (guard with PRAGMA table_info / IF NOT EXISTS), because a crash between
    apply and the version bump replays the step on next startup.
    """

    version: int
    description: str
    apply: Callable[[aiosqlite.Connection], Awaitable[None]]


async def _migration_0001_baseline(connection: aiosqlite.Connection) -> None:
    """Baseline marker for databases created before versioning existed.

    All tables are created by the idempotent CREATE TABLE IF NOT EXISTS block
    in ``initialize`` and the legacy conditional rebuilds; this step only
    exists so pre-framework databases converge on version 1.
    """


MIGRATIONS: list[Migration] = [
    Migration(1, "baseline: schema as of 2026-07-02", _migration_0001_baseline),
]

CURRENT_SCHEMA_VERSION = MIGRATIONS[-1].version
```

- [ ] **Step 2: Apply pending migrations at the end of `initialize()`**

At the end of `Database.initialize()` (after the existing CREATE TABLE block and the legacy `plans` rebuild), add:

```python
        cursor = await connection.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        current = int(row[0]) if row else 0
        for migration in MIGRATIONS:
            if migration.version <= current:
                continue
            logger.info(
                "Applying schema migration %d: %s",
                migration.version,
                migration.description,
            )
            await migration.apply(connection)
            await connection.execute(f"PRAGMA user_version = {migration.version}")
            await connection.commit()
```

(Match the actual local variable name for the connection in `initialize()`; add a module logger if the file lacks one.)

- [ ] **Step 3: Run the new tests, then the full suite**

Run: `uv run pytest tests/test_migrations.py -q` then `uv run pytest -q`
Expected: all pass, including `tests/test_database.py` (the existing legacy-rebuild tests must be untouched by the framework).

- [ ] **Step 4: Document the pattern**

In `CLAUDE.md`, extend the "No ORM" key-design bullet with:

```markdown
  Schema changes now go through the versioned migration list in `database.py`
  (`MIGRATIONS` + `PRAGMA user_version`, applied at the end of `initialize()`).
  Add a new `Migration(n, desc, fn)` instead of another ad-hoc conditional
  rebuild; steps must be idempotent (re-run safe).
```

- [ ] **Step 5: Verify** (full block: pytest, ruff format, ruff check, mypy). Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/database.py CLAUDE.md
git commit -m "feat: versioned SQLite migration framework (PRAGMA user_version)"
```

---

### Task 10: Rename agent container prefix to `praxis-agent-`

**Files:**
- Modify: `src/orchestrator/core/agent_manager.py` (name assembly in `spawn_agent`, filter in `list_agent_containers`)
- Modify: `tests/test_agent_manager.py`
- Modify: `CLAUDE.md` (the gotcha mentioning `aider-agent-{task_id[:8]}`)

**Depends on:** None

- [ ] **Step 1: Find every occurrence**

Run: `grep -rn "aider-agent-" src/ tests/ CLAUDE.md docs/`
Expected: `agent_manager.py` (2 sites: `container_name = f"aider-agent-{task_id[:8]}"` and the `filters={"name": "aider-agent-"}`), `tests/test_agent_manager.py`, one CLAUDE.md gotcha. Note: the Docker IMAGE tag `aider-agent:latest` is a different string and must NOT change.

- [ ] **Step 2: Rename with a back-compat filter**

In `agent_manager.py` set `container_name = f"praxis-agent-{task_id[:8]}"`. In `list_agent_containers`, keep discovery of old containers so reconcile can clean up runs spawned before the upgrade:

```python
        containers = self._client.containers.list(
            all=True,
            filters={"name": "praxis-agent-"},
        )
        # Back-compat: containers spawned before the 2026-07 rename.
        containers += self._client.containers.list(
            all=True,
            filters={"name": "aider-agent-"},
        )
```

Also check `_remove_existing_container` callers: it takes the new name, nothing else to change.

- [ ] **Step 3: Update tests and docs**: repoint assertions in `tests/test_agent_manager.py` to `praxis-agent-`; add one test asserting `list_agent_containers` queries both prefixes (assert on the mock's `list` call args). Update the CLAUDE.md gotcha text.

- [ ] **Step 4: Verify** (full block). Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/agent_manager.py tests/test_agent_manager.py CLAUDE.md
git commit -m "refactor: harness-neutral agent container prefix praxis-agent-"
```

---

### Task 11: Fix the double `import docker` and the from_env type-ignore

**Files:**
- Modify: `src/orchestrator/core/agent_manager.py:8-11,70`

**Depends on:** Task 10 (same file; avoid conflicts)

- [ ] **Step 1: Deduplicate the import**

The file currently has `import docker.errors` (line 8) and a second `import docker` (line 11). Replace both with a single:

```python
import docker
import docker.errors
```

kept in one sorted block. Then try removing the `# type: ignore[attr-defined]` on `docker.from_env()` and run mypy; if mypy (with `--ignore-missing-imports`) no longer errors, delete the ignore (the config has `warn_unused_ignores`, so a stale ignore is itself an error). If mypy still can't see `from_env`, keep the ignore and move on.

- [ ] **Step 2: Verify** (full block). Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add src/orchestrator/core/agent_manager.py
git commit -m "chore: clean up docker import duplication in agent_manager"
```

---

### Task 12: Document naming decisions kept as-is

**Files:**
- Modify: `src/orchestrator/core/opus_bridge.py` (module docstring only)
- Modify: `CLAUDE.md`

**Depends on:** None

- [ ] **Step 1: Clarify opus_bridge's provider-agnostic reality in its docstring**

Prepend to the existing module docstring in `opus_bridge.py` (keep whatever follows):

```python
"""Brain-call bridge (historically Opus-only, now provider-agnostic).

The name is legacy: since the Spec-3 LLM router, this module routes
plan/review/improve calls to whichever provider the call-site resolves to
(claude / codex / local). It is kept as ``opus_bridge`` to avoid churning
tests, docs, and operator muscle memory; treat "opus" here as "the brain".
"""
```

- [ ] **Step 2: Record both kept-names in CLAUDE.md gotchas**

Add one gotcha bullet:

```markdown
- **Two names are legacy on purpose** — `core/opus_bridge.py` is the
  provider-agnostic brain bridge (see its docstring), and `users.token_hash`
  stores the RAW v1 auth token, not a hash (see `api/auth.py`). Renames were
  evaluated (2026-07-02 refactor) and deliberately skipped as churn; a future
  `token_hash` rename should ride the migration framework in `database.py`.
```

- [ ] **Step 3: Verify** (full block; docstring change can still break a doctest-style assertion, so run everything). Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add src/orchestrator/core/opus_bridge.py CLAUDE.md
git commit -m "docs: document deliberate legacy names (opus_bridge, token_hash)"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1 (orchestrator split start), Task 6 (CSS extract), Task 8 (migration tests), Task 10 (container prefix), Task 12 (naming docs) — independent files, run in parallel
- **Wave 2:** Task 2 (depends on Task 1), Task 7 (depends on Task 6), Task 9 (depends on Task 8), Task 11 (depends on Task 10)
- **Wave 3:** Task 3 (depends on Task 2)
- **Wave 4:** Task 4 (depends on Task 3)
- **Wave 5:** Task 5 (depends on Task 4)

Caution for parallel dispatch: Tasks 1-4 all edit `src/orchestrator/core/orchestrator.py` and MUST stay sequential. Task 5 and Task 12 both edit CLAUDE.md; if dispatched to concurrent agents in one working tree they will clobber each other, so serialize any two tasks that share a file (or use isolated worktrees).

## Final integration check

After all waves:

```bash
uv run pytest -q                                  # expect 511+ passing (507 + 4 migration tests)
uv run ruff format --check src/ tests/ && uv run ruff check src/ tests/
uv run mypy src/orchestrator/ --ignore-missing-imports
wc -l src/orchestrator/core/orchestrator.py       # expect < 400
```

Then start the server and click through the dashboard once (Task 7 Step 3 procedure). The dashboard has no automated tests; the manual click-through is the acceptance gate for Tasks 6-7.
