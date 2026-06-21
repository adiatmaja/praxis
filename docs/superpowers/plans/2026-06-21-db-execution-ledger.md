# DB → Execution Ledger + Config to YAML Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) tracking. **Execute only after Spec 1 (`feat/unified-plan-lifecycle`) is merged to `main` and this branch is rebased on it** — this plan relies on the `spec_path` / `plan_path` columns and `core/plan_derive.py` that Spec 1 introduces.

**Goal:** Shrink the DB to a thin execution ledger (drop the redundant free-text `plans.spec` content column now living in markdown) and move global orchestrator settings to a git-trackable `config/praxis.yaml`, keeping SQLite for genuine runtime state.

**Architecture:** Markdown docs are the source of truth (Spec 1). The `plans.spec` free-text column is redundant and is dropped via an SQLite table-rebuild, after a no-data-loss backfill that writes any orphaned legacy spec text to a spec doc and sets `spec_path`. Global settings load from YAML with environment overrides on top. Projects stay DB-backed.

**Tech Stack:** Python 3.11, FastAPI, aiosqlite, pyyaml, pytest + pytest-asyncio.

---

## Important Scope Correction vs the Design Spec

The design spec proposed dropping **both** `plans.spec` and `plans.opus_plan`. During planning we found `opus_plan` is **runtime state**, not content: `TaskQueue.get_dispatchable_tasks` (`core/task_queue.py:151-154`) reads `opus_plan` to resolve task `depends_on` ordering at dispatch time. Dropping it would break dependency-aware dispatch. **Therefore this plan drops only `plans.spec`** (pure redundant content) and keeps `opus_plan` as the runtime task graph. Update the spec's "Components" note accordingly during execution.

---

## Context for a Zero-Context Engineer

- DB schema + migrations: `src/orchestrator/database.py` — `MIGRATIONS` tuple; `initialize()` runs them then does additive `ALTER TABLE ... ADD COLUMN` under `contextlib.suppress`. SQLite has no robust `DROP COLUMN`; use the **table-rebuild** pattern (create new table, `INSERT ... SELECT`, drop old, rename).
- `plans` columns after Spec 1: `id, project_id, spec, opus_plan, plan_branch_name, source, confidence, confidence_reason, status, created_at, spec_path, plan_path`.
- Settings: `src/orchestrator/config.py` (`Settings(BaseSettings)` via pydantic-settings; reads env + `.env`). The dashboard settings popup persists overrides to the `settings_overrides` table via `api/settings.py`.
- `brainstorm.write_and_commit(repo_url, path, content)` writes+commits a doc to the target repo (use for backfill).
- Tests: `tests/conftest.py` provides `db`, `client`, `auth_headers`, `test_settings`. Run `uv run pytest tests/<f>::<t> -v`. Commit per task, message `<type>: <desc>`, no Co-Authored-By trailer.

---

## File Structure

- **Create** `src/orchestrator/core/settings_file.py` — load `config/praxis.yaml`, overlay env. One responsibility.
- **Create** `tests/test_settings_file.py`.
- **Create** `config/praxis.yaml` — default global settings, git-tracked.
- **Modify** `src/orchestrator/database.py` — backfill + drop `plans.spec` via table-rebuild.
- **Modify** `tests/test_database.py` — assert `spec` dropped, no data loss.
- **Modify** `src/orchestrator/config.py` — layer YAML beneath env defaults.
- **Modify** `tests/test_config.py`.

---

## Task 1: YAML settings loader

**Files:**
- Create: `src/orchestrator/core/settings_file.py`
- Create: `config/praxis.yaml`
- Test: `tests/test_settings_file.py`

**Depends on:** None

- [ ] **Step 1: Failing test** — create `tests/test_settings_file.py`:

```python
from orchestrator.core.settings_file import load_yaml_settings


def test_load_defaults(tmp_path):
    p = tmp_path / "praxis.yaml"
    p.write_text("loop_interval: 5\ncallback_grace: 5\n", encoding="utf-8")
    cfg = load_yaml_settings(str(p), env={})
    assert cfg["loop_interval"] == 5


def test_env_overrides_yaml(tmp_path):
    p = tmp_path / "praxis.yaml"
    p.write_text("loop_interval: 5\n", encoding="utf-8")
    cfg = load_yaml_settings(str(p), env={"PRAXIS_LOOP_INTERVAL": "9"})
    assert cfg["loop_interval"] == 9


def test_missing_file_returns_empty(tmp_path):
    assert load_yaml_settings(str(tmp_path / "nope.yaml"), env={}) == {}


def test_malformed_yaml_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("loop_interval: : :\n", encoding="utf-8")
    import pytest
    with pytest.raises(ValueError):
        load_yaml_settings(str(p), env={})
```

- [ ] **Step 2: Run → fail** — `uv run pytest tests/test_settings_file.py -v` → ImportError.

- [ ] **Step 3: Implement** — `src/orchestrator/core/settings_file.py`:

```python
"""Load global orchestrator settings from YAML, overlaid by environment vars."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


_ENV_PREFIX = "PRAXIS_"


def load_yaml_settings(path: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Return YAML settings with PRAXIS_* env vars overriding matching keys."""
    env = os.environ.copy() if env is None else env
    file = Path(path)
    data: dict[str, Any] = {}
    if file.is_file():
        try:
            loaded = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            message = f"Invalid YAML in {path}: {exc}"
            raise ValueError(message) from exc
        if not isinstance(loaded, dict):
            message = f"{path} must contain a mapping"
            raise ValueError(message)
        data = loaded
    for key, raw in env.items():
        if key.startswith(_ENV_PREFIX):
            name = key[len(_ENV_PREFIX):].lower()
            data[name] = _coerce(raw)
    return data


def _coerce(value: str) -> Any:
    if value.isdigit():
        return int(value)
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    return value
```

- [ ] **Step 4: Create the default config** — `config/praxis.yaml`:

```yaml
# Global Praxis orchestrator settings. PRAXIS_<KEY> env vars override these.
loop_interval: 30      # seconds between orchestration loop passes
callback_grace: 5      # seconds to wait before reconciling a missing callback
```

- [ ] **Step 5: Run → pass** — `uv run pytest tests/test_settings_file.py -v` → 4 passed.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/core/settings_file.py config/praxis.yaml tests/test_settings_file.py
git commit -m "feat: YAML global settings loader with env overrides"
```

---

## Task 2: Layer YAML beneath Settings defaults

**Files:**
- Modify: `src/orchestrator/config.py`
- Test: `tests/test_config.py`

**Depends on:** Task 1

- [ ] **Step 1: Failing test** — append to `tests/test_config.py`:

```python
def test_yaml_provides_defaults(tmp_path, monkeypatch):
    cfg = tmp_path / "praxis.yaml"
    cfg.write_text("loop_interval: 7\n", encoding="utf-8")
    monkeypatch.setenv("AUTH_TOKEN", "x")
    monkeypatch.setenv("GITHUB_TOKEN", "y")
    from orchestrator.config import Settings
    s = Settings(_env_file=None, yaml_path=str(cfg))
    assert s.loop_interval == 7
```

- [ ] **Step 2: Run → fail** — `uv run pytest tests/test_config.py::test_yaml_provides_defaults -v`.

- [ ] **Step 3: Implement** — in `src/orchestrator/config.py`, add a `loop_interval`/`callback_grace` field if absent and a constructor hook that applies YAML defaults before env. Minimal approach — add a classmethod/factory:

```python
# at top
from orchestrator.core.settings_file import load_yaml_settings

# inside Settings (add fields if not present)
    loop_interval: int = 30
    callback_grace: int = 5

    def __init__(self, *args, yaml_path: str = "config/praxis.yaml", **kwargs):
        yaml_defaults = load_yaml_settings(yaml_path)
        merged = {**yaml_defaults, **kwargs}
        super().__init__(*args, **merged)
```

(If `Settings` already defines these fields elsewhere, only add the `__init__` YAML merge. Env still wins because pydantic-settings reads env after explicit kwargs only where kwargs are unset; verify with the test — if env precedence breaks, pass `yaml_defaults` via `_env_file`-style defaults instead.)

- [ ] **Step 4: Run → pass** — `uv run pytest tests/test_config.py -v`.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/config.py tests/test_config.py
git commit -m "feat: layer YAML defaults beneath Settings"
```

---

## Task 3: Backfill + drop `plans.spec`

**Files:**
- Modify: `src/orchestrator/database.py`
- Test: `tests/test_database.py`

**Depends on:** None (but only correct once Spec 1's `spec_path`/`plan_path` exist — run after Spec 1 merge)

The migration: (a) for any `plans` row whose `spec_path` is NULL, keep its `spec` text safe by leaving the column until a backfill writes it out (handled at app level, Task 4); (b) rebuild the `plans` table without the `spec` column. Guard the rebuild so it only runs when the `spec` column still exists.

- [ ] **Step 1: Failing test** — append to `tests/test_database.py`:

```python
async def test_plans_spec_column_dropped(db):
    rows = await db.fetch_all("PRAGMA table_info(plans)")
    names = {r["name"] for r in rows}
    assert "spec" not in names
    assert "spec_path" in names and "plan_path" in names
    assert "opus_plan" in names  # retained: runtime task graph
```

- [ ] **Step 2: Run → fail** — `uv run pytest tests/test_database.py::test_plans_spec_column_dropped -v`.

- [ ] **Step 3: Implement** — in `database.py` `initialize()`, after the additive ALTER loop, add a guarded table-rebuild:

```python
        cols = {
            row["name"]
            for row in await (await connection.execute("PRAGMA table_info(plans)")).fetchall()
        }
        if "spec" in cols:
            await connection.executescript(
                """
                CREATE TABLE plans_new (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    opus_plan TEXT,
                    plan_branch_name TEXT,
                    source TEXT NOT NULL DEFAULT 'user',
                    confidence REAL,
                    confidence_reason TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    spec_path TEXT,
                    plan_path TEXT,
                    FOREIGN KEY (project_id) REFERENCES projects (id)
                );
                INSERT INTO plans_new
                    (id, project_id, opus_plan, plan_branch_name, source,
                     confidence, confidence_reason, status, created_at,
                     spec_path, plan_path)
                SELECT id, project_id, opus_plan, plan_branch_name, source,
                       confidence, confidence_reason, status, created_at,
                       spec_path, plan_path
                FROM plans;
                DROP TABLE plans;
                ALTER TABLE plans_new RENAME TO plans;
                """
            )
```

> `create_plan` (`core/task_queue.py:33`) still inserts into `spec`. Update it to drop the `spec` column from its INSERT and instead accept/ignore the summary, OR keep `create_plan` writing `plan_summary` into a retained column. Since `spec` is removed, change `create_plan`'s INSERT to omit `spec` and store the summary only where needed (the Run view derives display text from `plan_path`). Update the signature to `create_plan(project_id, summary=None, ...)` and stop writing `spec`.

- [ ] **Step 4: Update `create_plan`** — in `core/task_queue.py`, change the INSERT to not reference `spec`:

```python
    async def create_plan(self, project_id, summary=None, source="user",
                          confidence=None, confidence_reason=None) -> str:
        plan_id = str(uuid.uuid4())
        await self._db.execute(
            """INSERT INTO plans
               (id, project_id, source, confidence, confidence_reason)
               VALUES (?, ?, ?, ?, ?)""",
            (plan_id, project_id, source, confidence, confidence_reason),
        )
        return plan_id
```

Update callers (`api/plans.py` create_plan endpoint, `api/plans.py` promote, orchestrator autonomous path) to the new signature.

- [ ] **Step 5: Run → pass** — `uv run pytest tests/test_database.py tests/test_task_queue.py tests/test_api_plans.py -v`.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/database.py src/orchestrator/core/task_queue.py src/orchestrator/api/plans.py tests/test_database.py
git commit -m "refactor: drop redundant plans.spec column (markdown is truth)"
```

---

## Task 4: Backfill orphaned legacy specs before drop

**Files:**
- Modify: `src/orchestrator/main.py` (lifespan: one-time backfill before schema rebuild) OR a small `core/backfill.py`
- Test: `tests/test_backfill.py` (new)

**Depends on:** Task 3

To avoid data loss for pre-Spec-1 rows that have `spec` text but no `spec_path`, run a backfill at startup *before* the drop: write each such spec's text to `docs/superpowers/specs/<id>-legacy.md` in its project repo and set `spec_path`. Because Task 3 drops the column at init, this backfill must run as a guarded step that reads `spec` while it still exists.

- [ ] **Step 1: Failing test** — create `tests/test_backfill.py`:

```python
from orchestrator.core.backfill import backfill_legacy_specs


async def test_backfill_writes_spec_doc_and_sets_path(db, mocker):
    import uuid
    pid, plid = str(uuid.uuid4()), str(uuid.uuid4())
    await db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url, model_name) "
        "VALUES (?, ?, ?, ?, ?)",
        (pid, "u", "p", "https://example.com/r.git", "m"),
    )
    # legacy row: spec text, no spec_path (simulate pre-Spec-1)
    await db.execute(
        "INSERT INTO plans (id, project_id, spec, source) VALUES (?, ?, ?, 'user')",
        (plid, pid, "legacy spec body"),
    )
    fake_bs = mocker.Mock()
    fake_bs.write_and_commit.return_value = {"status": "committed"}
    await backfill_legacy_specs(db, fake_bs)
    row = await db.fetch_one("SELECT spec_path FROM plans WHERE id = ?", (plid,))
    assert row["spec_path"] is not None
    fake_bs.write_and_commit.assert_called_once()
```

> Note: this test requires the `spec` column to exist. Run it against a DB initialized **before** Task 3's drop runs, or gate the drop behind an env flag in tests. Simplest: have `initialize()` call `backfill_legacy_specs` itself right before the rebuild (Step 3), and test the function in isolation with a manually-created `spec` column.

- [ ] **Step 2: Run → fail** — `uv run pytest tests/test_backfill.py -v`.

- [ ] **Step 3: Implement** — `src/orchestrator/core/backfill.py`:

```python
"""One-time backfill: persist legacy plans.spec text to repo spec docs."""

from __future__ import annotations

from typing import Any

from orchestrator.database import Database


async def backfill_legacy_specs(db: Database, brainstorm: Any) -> int:
    """For rows with spec text but no spec_path, write a spec doc + set spec_path."""
    cols = {r["name"] for r in await db.fetch_all("PRAGMA table_info(plans)")}
    if "spec" not in cols:
        return 0
    rows = await db.fetch_all(
        "SELECT p.id, p.spec, pr.repo_url FROM plans p "
        "JOIN projects pr ON pr.id = p.project_id "
        "WHERE p.spec_path IS NULL AND p.spec IS NOT NULL AND p.spec != ''"
    )
    count = 0
    for row in rows:
        path = f"docs/superpowers/specs/{row['id']}-legacy.md"
        content = f"# Legacy Spec\n\n{row['spec']}\n"
        brainstorm.write_and_commit(row["repo_url"], path, content)
        await db.execute(
            "UPDATE plans SET spec_path = ? WHERE id = ?", (path, row["id"])
        )
        count += 1
    return count
```

- [ ] **Step 4: Wire into `database.initialize()`** — call `backfill_legacy_specs` **before** the Task 3 table-rebuild, passing a BrainstormManager. Since `Database` shouldn't import `BrainstormManager`, do the backfill in `main.py` lifespan before any plan rebuild instead: run `initialize()` (which now defers the drop behind a flag), then backfill, then a second `finalize_schema()` that drops the column. Pragmatic alternative: keep the drop in `initialize()` but make `main.py` lifespan call `backfill_legacy_specs(db, brainstorm)` on the **first** boot before constructing routers — acceptable because on a fresh DB there are no legacy rows. Document the chosen order in a comment.

- [ ] **Step 5: Run → pass** — `uv run pytest tests/test_backfill.py -v`.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/core/backfill.py tests/test_backfill.py src/orchestrator/main.py
git commit -m "feat: backfill legacy spec text to repo docs before column drop"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1
- **Wave 2:** Task 2 (Task 1), Task 3 (independent of 1/2 but run after to avoid churn)
- **Wave 3:** Task 4 (Task 3)

## Post-Implementation

- [ ] Full suite green, coverage ≥ 80%, ruff + mypy clean.
- [ ] Update `CLAUDE.md`: `plans.spec` removed; global settings in `config/praxis.yaml`; backfill behavior.
- [ ] PR `feat/db-execution-ledger` → `main`; squash-merge.
