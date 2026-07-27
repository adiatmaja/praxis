# Auto-Delegate Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a toggleable "auto-delegate mode" so the brain automatically hands every implementation task to a configurable default worker (reference: Gemini 3.6 Flash High via the agy harness), with single-branch discipline and automatic branch cleanup, making Praxis usable for everyday development.

**Architecture:** Config follows Praxis's existing layering (env > YAML > field default via `Settings`, runtime overrides in `settings_overrides` via `EffectiveSettings`). A global default worker (harness + model) makes dispatches model-argument-free. A Praxis-owned `auto_delegate.enabled` toggle is exposed over REST/CLI/dashboard/MCP; the brain reads it and delegates instead of editing. In mode, worker commits onto a single caller-named branch (no `plan/`/`agent/*` sprawl), one PR is reused, and cleanup happens via delete-on-merge (already in `merge_pr`) plus a stale-branch sweeper on the reconcile loop.

**Tech Stack:** Python 3.11, FastAPI, aiosqlite (raw SQL, no ORM), Typer + rich CLI, pytest (`asyncio_mode = "auto"`), no-build HTML/CSS/JS dashboard, `gh` CLI for git ops.

**Spec:** `docs/superpowers/specs/2026-07-27-auto-delegate-mode-design.md`

---

## Context for workers (read before starting)

You have zero prior conversation context. Key facts about this codebase:

- **Run tests:** `uv run pytest <path> -v`. **Format:** `uv run ruff format src/ tests/`. **Lint:** `uv run ruff check --fix src/ tests/`. **Types:** `uv run mypy src/orchestrator/ --ignore-missing-imports`. Line length 88. Use `X | Y` unions, built-in generics, Google docstrings, `logging` not `print`.
- **`ruff format`, NOT `ruff fmt`.**
- Tests use `asyncio_mode = "auto"` — `async def test_*` functions run directly, no decorator needed.
- **Settings** (`src/orchestrator/config.py`): a `pydantic-settings` `BaseSettings` subclass. YAML defaults from `config/praxis.yaml` are overlaid beneath env vars in `Settings.__init__`; precedence is **env > YAML > field default**. `extra="forbid"` is in effect, so a YAML key that is NOT a `Settings` field is dropped unless consumed elsewhere. Env var names are UPPERCASE field names (no prefix) for the pydantic layer, and `PRAXIS_<UPPER>` for the YAML loader (`core/settings_file.py`).
- **`config/praxis.yaml`** holds git-tracked defaults. `core/settings_file.load_yaml_settings(path)` reads it and overlays `PRAXIS_*` env vars (coercing `true`/`false`/digits).
- **`settings_overrides`** table: `key TEXT PRIMARY KEY, value TEXT, updated_at`. `EffectiveSettings.set_override(key, value)` upserts (value=None deletes); `EffectiveSettings._get_override(key)` returns the value or None (empty string treated as unset). `EDITABLE_KEYS: frozenset[str]` (in `core/effective_settings.py`) whitelists which keys `PUT /api/settings` may touch.
- **DB object** (`request.app.state.db`): `await db.fetch_one(sql, params)`, `await db.fetch_all(sql, params)`, `await db.execute(sql, params)`. Rows are mapping-like (`row["col"]`).
- **App state**: `request.app.state.settings` (Settings), `request.app.state.effective_settings` (EffectiveSettings), `request.app.state.db`, `request.app.state.orchestrator`.
- **Projects table** columns (relevant): `id, user_id, name, repo_url, default_branch, approval_gate, auto_merge, verify_cmd, confidence_threshold, max_retries, max_improvement_cycles, lm_studio_url, model_name, harness, agent_model, agent_model_effort`.
- **Harness registry** (`core/harnesses.py`): `REGISTRY: dict[str, HarnessSpec]` (`opencode`, `agy`), `default_harness_id() -> "opencode"`, `get_harness(id)`.
- **git_ops** (`core/git_ops.py`, class `GitOps`): `merge_pr(workspace, pr_number, repo=None)` already runs `gh pr merge --squash --delete-branch` with a transient-retry guard (only retries on transient stderr, otherwise raises — so it does NOT delete on a hard error). `remote_branch_exists(repo_url, branch)`, `create_pr(...)`, `push_branch(workspace, branch)`.
- **agy entrypoint** (`docker/agy-agent/entrypoint.sh`): requires `BRANCH` + `BASE_BRANCH` env. Today it does `git checkout -b "${BRANCH}"` from `origin/${BASE_BRANCH}` (line ~118), `git push -u --force origin "${BRANCH}"` (~269), and reuses an existing PR via `gh pr view "${BRANCH}"` (~273). The opencode entrypoint (`docker/opencode-agent/entrypoint.sh`) mirrors this contract. **Rebuild harness images after ANY entrypoint change:** `docker build -t agy-agent:latest -f docker/agy-agent/Dockerfile docker/agy-agent/` (and `opencode-agent`).
- **CLI** (`src/cli/main.py`): Typer app `app = typer.Typer(...)`, entrypoint `orchestrator-cli`. Talks to REST via httpx using `ORCHESTRATOR_URL` (default `http://localhost:8080`) and `ORCHESTRATOR_TOKEN`.
- **MCP server** (`src/mcp_server/`): `client.py` (`PraxisClient`, thin httpx wrapper), `server.py` (tool definitions). Read-back tools wrap existing REST endpoints client-side.
- **Never override or expose `auth_token` / `github_token`.**
- **Reference worker string:** `"Gemini 3.6 Flash (High)"` is passed verbatim as `MODEL` to `agy --model` by the entrypoint — no code needs to parse it.

---

## File Structure

| File | Responsibility |
|------|---------------|
| `src/orchestrator/config.py` (modify) | Add `default_worker_harness` + `default_worker_model` Settings fields. |
| `config/praxis.yaml` (modify) | Ship this repo's defaults: `agy` + `Gemini 3.6 Flash (High)`. |
| `src/orchestrator/models/schemas.py` (modify) | Make `ProjectCreate.model_name` + `harness` optional (fall back to global default). |
| `src/orchestrator/api/projects.py` (modify) | Resolve omitted `model_name`/`harness` from the global default worker at create time. |
| `src/orchestrator/core/effective_settings.py` (modify) | `auto_delegate_enabled()` + `auto_delegate_worker()` resolvers. |
| `src/orchestrator/api/settings.py` (modify) | `GET`/`PUT /api/settings/auto-delegate`. |
| `src/cli/main.py` (modify) | `praxis mode on|off|status`. |
| `src/mcp_server/client.py` + `server.py` (modify) | `get_mode` MCP read tool. |
| `src/orchestrator/core/branch_sweeper.py` (create) | Pure logic: given remote branches + run ledger, decide which are dead. |
| `src/orchestrator/core/orchestrator_reconcile.py` (modify) | Call the sweeper each reconcile pass. |
| `docker/agy-agent/entrypoint.sh` + `docker/opencode-agent/entrypoint.sh` (modify) | Single-branch mode: reuse existing remote work branch, non-force push. |
| `web/app.js` + `web/index.html` (modify) | Settings switch showing mode + resolved worker. |
| `CLAUDE.md` (modify) | Brain convention: honor auto-delegate mode. |

---

## Task 1: Global default worker settings

**Files:**
- Modify: `src/orchestrator/config.py:31-32` (add fields near `agent_model`)
- Modify: `config/praxis.yaml`
- Test: `tests/test_config_default_worker.py` (create)

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_default_worker.py
"""Global default worker settings resolve via env > YAML > field default."""

from __future__ import annotations

from orchestrator.config import Settings


def test_default_worker_field_defaults(tmp_path):
    # No YAML, no env -> built-in defaults (opencode, unset model).
    empty_yaml = tmp_path / "praxis.yaml"
    empty_yaml.write_text("", encoding="utf-8")
    s = Settings(_env_file=None, yaml_path=str(empty_yaml), auth_token="t")
    assert s.default_worker_harness == "opencode"
    assert s.default_worker_model == ""


def test_yaml_overrides_field_default(tmp_path):
    yaml_file = tmp_path / "praxis.yaml"
    yaml_file.write_text(
        'default_worker_harness: agy\n'
        'default_worker_model: "Gemini 3.6 Flash (High)"\n',
        encoding="utf-8",
    )
    s = Settings(_env_file=None, yaml_path=str(yaml_file), auth_token="t")
    assert s.default_worker_harness == "agy"
    assert s.default_worker_model == "Gemini 3.6 Flash (High)"


def test_env_overrides_yaml(tmp_path, monkeypatch):
    yaml_file = tmp_path / "praxis.yaml"
    yaml_file.write_text("default_worker_harness: agy\n", encoding="utf-8")
    monkeypatch.setenv("DEFAULT_WORKER_HARNESS", "opencode")
    s = Settings(_env_file=None, yaml_path=str(yaml_file), auth_token="t")
    assert s.default_worker_harness == "opencode"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config_default_worker.py -v`
Expected: FAIL — `Settings` has no attribute `default_worker_harness`.

- [ ] **Step 3: Add the fields**

In `src/orchestrator/config.py`, after line 32 (`agent_model_effort: str | None = None`), add:

```python
    # Default worker (harness + model) used when a project or dispatch omits one.
    # Fallback-only: explicit per-project values always win. The built-in default
    # stays opencode so fresh installs are unchanged; this repo's config/praxis.yaml
    # overrides to agy + Gemini for the auto-delegate reference setup.
    default_worker_harness: str = "opencode"
    default_worker_model: str = ""
```

- [ ] **Step 4: Add the YAML defaults**

Append to `config/praxis.yaml`:

```yaml
# Auto-delegate reference worker (this repo dogfoods agy/Gemini as the default worker).
default_worker_harness: agy
default_worker_model: "Gemini 3.6 Flash (High)"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_config_default_worker.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/config.py config/praxis.yaml tests/test_config_default_worker.py
git commit -m "feat(config): add global default worker harness + model"
```

---

## Task 2: Project creation falls back to the default worker

**Files:**
- Modify: `src/orchestrator/models/schemas.py` (`ProjectCreate`, around lines 122-145)
- Modify: `src/orchestrator/api/projects.py:61-86` (`create_project`)
- Test: `tests/test_api_projects_default_worker.py` (create)

**Depends on:** Task 1

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_projects_default_worker.py
"""Omitted model_name/harness fall back to the global default worker."""

from __future__ import annotations


def test_create_project_without_model_uses_default(client, auth_headers, monkeypatch):
    # The test app's Settings should carry default_worker_model. See conftest;
    # if the fixture app uses a Settings without it, set it on app.state.settings.
    client.app.state.settings.default_worker_model = "Gemini 3.6 Flash (High)"
    client.app.state.settings.default_worker_harness = "agy"
    resp = client.post(
        "/api/projects",
        headers=auth_headers,
        json={"name": "no-model", "repo_url": "https://github.com/o/r"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["model_name"] == "Gemini 3.6 Flash (High)"
    assert body["harness"] == "agy"


def test_explicit_model_still_wins(client, auth_headers):
    client.app.state.settings.default_worker_model = "Gemini 3.6 Flash (High)"
    resp = client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "explicit",
            "repo_url": "https://github.com/o/r",
            "model_name": "qwen3.6-27b",
            "harness": "opencode",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["model_name"] == "qwen3.6-27b"
    assert body["harness"] == "opencode"
```

> Note: this test relies on the existing `client` / `auth_headers` fixtures in `tests/conftest.py` and on preflight being mocked/skipped there (project-create tests already pass today, so follow their pattern — grep `test_create_project` in the existing test suite for the exact fixture usage).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_projects_default_worker.py -v`
Expected: FAIL — validation error (model_name required) or wrong resolved value.

- [ ] **Step 3: Make the schema fields optional**

In `src/orchestrator/models/schemas.py`, in `ProjectCreate`:
- Change `model_name: str` (line ~127) to `model_name: str | None = None`.
- Change `harness: str = Field(default_factory=default_harness_id)` (line ~138) to `harness: str | None = None`.
- The existing `@field_validator("harness")` (line ~140) must tolerate `None`: at the top of the validator body add `if value is None: return None` before the `REGISTRY` membership check.

- [ ] **Step 4: Resolve defaults in the endpoint**

In `src/orchestrator/api/projects.py`, inside `create_project`, immediately after `settings = request.app.state.settings` (line 47), add:

```python
    resolved_model = body.model_name or settings.default_worker_model
    resolved_harness = body.harness or settings.default_worker_harness
    if not resolved_model:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="model_name is required and no default_worker_model is configured",
        )
```

Then in the `INSERT` params (lines 68-85), replace `body.model_name` with `resolved_model` and `body.harness` with `resolved_harness`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_api_projects_default_worker.py tests/test_api_projects.py -v`
Expected: PASS (new tests + existing project tests still green).

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/models/schemas.py src/orchestrator/api/projects.py tests/test_api_projects_default_worker.py
git commit -m "feat(projects): fall back to global default worker when model/harness omitted"
```

---

## Task 3: Auto-delegate toggle resolvers in EffectiveSettings

**Files:**
- Modify: `src/orchestrator/core/effective_settings.py`
- Test: `tests/test_effective_settings_auto_delegate.py` (create)

**Depends on:** Task 1

- [ ] **Step 1: Write the failing test**

```python
# tests/test_effective_settings_auto_delegate.py
"""Auto-delegate toggle + worker resolution via EffectiveSettings."""

from __future__ import annotations

import pytest

from orchestrator.config import Settings
from orchestrator.core.effective_settings import EffectiveSettings


@pytest.fixture
async def es(db):  # `db` is the shared in-memory DB fixture from conftest
    settings = Settings(_env_file=None, auth_token="t")
    settings.default_worker_harness = "agy"
    settings.default_worker_model = "Gemini 3.6 Flash (High)"
    return EffectiveSettings(db, settings)


async def test_disabled_by_default(es):
    assert await es.auto_delegate_enabled() is False


async def test_enable_and_read(es):
    await es.set_override("auto_delegate.enabled", "true")
    assert await es.auto_delegate_enabled() is True


async def test_worker_reflects_default(es):
    worker = es.auto_delegate_worker()
    assert worker == {"harness": "agy", "model": "Gemini 3.6 Flash (High)"}
```

> Check `EffectiveSettings.__init__` signature and the `db` fixture name in `tests/conftest.py` before finalizing the fixture; mirror an existing `EffectiveSettings` test (e.g. `tests/test_effective_settings_chains.py`).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_effective_settings_auto_delegate.py -v`
Expected: FAIL — no `auto_delegate_enabled` / `auto_delegate_worker`.

- [ ] **Step 3: Implement the resolvers**

In `src/orchestrator/core/effective_settings.py`, add methods to the `EffectiveSettings` class:

```python
    async def auto_delegate_enabled(self) -> bool:
        """Return True when auto-delegate mode is toggled on."""
        return (await self._get_override("auto_delegate.enabled")) == "true"

    def auto_delegate_worker(self) -> dict[str, str]:
        """Return the worker used in auto-delegate mode (the global default)."""
        return {
            "harness": self._settings.default_worker_harness,
            "model": self._settings.default_worker_model,
        }
```

> Confirm the attribute holding `Settings` on the instance (grep `self._settings` vs `self.settings` in the file) and match it.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_effective_settings_auto_delegate.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/effective_settings.py tests/test_effective_settings_auto_delegate.py
git commit -m "feat(settings): auto-delegate toggle + worker resolvers"
```

---

## Task 4: Auto-delegate REST endpoints

**Files:**
- Modify: `src/orchestrator/api/settings.py`
- Test: `tests/test_api_auto_delegate.py` (create)

**Depends on:** Task 3

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_auto_delegate.py
"""REST surface for the auto-delegate toggle."""

from __future__ import annotations


def test_get_defaults_disabled(client, auth_headers):
    resp = client.get("/api/settings/auto-delegate", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert set(body["worker"]) == {"harness", "model"}


def test_put_enables(client, auth_headers):
    resp = client.put(
        "/api/settings/auto-delegate",
        headers=auth_headers,
        json={"enabled": True},
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True
    # Round-trips through the store.
    again = client.get("/api/settings/auto-delegate", headers=auth_headers)
    assert again.json()["enabled"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_auto_delegate.py -v`
Expected: FAIL — 404 (route missing).

- [ ] **Step 3: Implement the endpoints**

In `src/orchestrator/api/settings.py`, add (after the existing imports add `from pydantic import BaseModel` is already present):

```python
class AutoDelegatePut(BaseModel):
    enabled: bool


@router.get("/settings/auto-delegate")
async def get_auto_delegate(request: Request) -> dict[str, Any]:
    """Return auto-delegate mode state and the resolved default worker."""
    es = cast(EffectiveSettings, request.app.state.effective_settings)
    return {
        "enabled": await es.auto_delegate_enabled(),
        "worker": es.auto_delegate_worker(),
    }


@router.put("/settings/auto-delegate")
async def put_auto_delegate(
    request: Request, body: AutoDelegatePut
) -> dict[str, Any]:
    """Toggle auto-delegate mode on or off."""
    es = cast(EffectiveSettings, request.app.state.effective_settings)
    await es.set_override("auto_delegate.enabled", "true" if body.enabled else None)
    return {
        "enabled": await es.auto_delegate_enabled(),
        "worker": es.auto_delegate_worker(),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_api_auto_delegate.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/api/settings.py tests/test_api_auto_delegate.py
git commit -m "feat(api): auto-delegate GET/PUT endpoints"
```

---

## Task 5: `praxis mode` CLI command

**Files:**
- Modify: `src/cli/main.py`
- Test: `tests/test_cli_mode.py` (create)

**Depends on:** Task 4

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_mode.py
"""`praxis mode` CLI wraps the auto-delegate REST endpoints."""

from __future__ import annotations

from typer.testing import CliRunner

from cli.main import app


def test_mode_status(monkeypatch):
    runner = CliRunner()
    captured = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"enabled": True, "worker": {"harness": "agy", "model": "Gemini 3.6 Flash (High)"}}

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **k):
            captured["get"] = url
            return FakeResp()

        def put(self, url, **k):
            captured["put"] = (url, k.get("json"))
            return FakeResp()

    monkeypatch.setattr("cli.main.httpx.Client", FakeClient)
    result = runner.invoke(app, ["mode", "status"])
    assert result.exit_code == 0
    assert "enabled" in result.stdout.lower() or "agy" in result.stdout
    assert captured["get"].endswith("/api/settings/auto-delegate")


def test_mode_on(monkeypatch):
    runner = CliRunner()
    # ...reuse FakeClient pattern; assert PUT body {"enabled": True}
```

> Inspect `src/cli/main.py` for the exact httpx usage pattern (client construction, base URL, auth header helper) and mirror it — the fake above assumes `cli.main.httpx.Client`; adapt the monkeypatch target to whatever the module actually calls.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli_mode.py -v`
Expected: FAIL — no `mode` command.

- [ ] **Step 3: Implement the command**

In `src/cli/main.py`, add a `mode` sub-command that accepts `on|off|status` and calls the REST endpoint using the module's existing base-URL + auth-header helpers:

```python
@app.command()
def mode(action: str = typer.Argument(..., help="on | off | status")) -> None:
    """Turn auto-delegate mode on/off or show its status."""
    action = action.lower()
    if action not in {"on", "off", "status"}:
        typer.echo("action must be one of: on, off, status")
        raise typer.Exit(code=2)
    url = f"{_base_url()}/api/settings/auto-delegate"  # use the module's URL helper
    with httpx.Client() as c:
        if action == "status":
            resp = c.get(url, headers=_auth_headers())
        else:
            resp = c.put(url, headers=_auth_headers(), json={"enabled": action == "on"})
        resp.raise_for_status()
        data = resp.json()
    typer.echo(
        f"auto-delegate: {'ON' if data['enabled'] else 'OFF'} "
        f"(worker: {data['worker']['harness']} / {data['worker']['model'] or 'unset'})"
    )
```

> Replace `_base_url()` / `_auth_headers()` with the actual helper names in `cli/main.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli_mode.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/cli/main.py tests/test_cli_mode.py
git commit -m "feat(cli): praxis mode on|off|status"
```

---

## Task 6: MCP `get_mode` read tool

**Files:**
- Modify: `src/mcp_server/client.py`, `src/mcp_server/server.py`
- Test: `tests/test_mcp_get_mode.py` (create)

**Depends on:** Task 4

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mcp_get_mode.py
"""MCP get_mode surfaces the auto-delegate state to an external brain."""

from __future__ import annotations

import pytest

from mcp_server.client import PraxisClient


@pytest.mark.asyncio
async def test_client_get_mode(monkeypatch):
    async def fake_get(self, path):
        assert path == "/api/settings/auto-delegate"
        return {"enabled": True, "worker": {"harness": "agy", "model": "Gemini 3.6 Flash (High)"}}

    monkeypatch.setattr(PraxisClient, "_get", fake_get, raising=False)
    client = PraxisClient(base_url="http://x", token="t")
    result = await client.get_mode()
    assert result["enabled"] is True
```

> Match `PraxisClient`'s actual internal request helper name (grep `async def _get`/`_request` in `client.py`) and mirror how `get_project`/`list_projects` are implemented — `get_mode` is the same shape (thin wrapper over an existing GET).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_get_mode.py -v`
Expected: FAIL — no `get_mode`.

- [ ] **Step 3: Implement client method + server tool**

In `client.py`, add (mirroring `get_project`):

```python
    async def get_mode(self) -> dict:
        """Return auto-delegate mode state {enabled, worker:{harness,model}}."""
        return await self._get("/api/settings/auto-delegate")
```

In `server.py`, register a `get_mode` tool that calls `client.get_mode()` and returns the JSON, following the exact registration pattern used by `get_project`/`list_projects`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_get_mode.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mcp_server/client.py src/mcp_server/server.py tests/test_mcp_get_mode.py
git commit -m "feat(mcp): get_mode tool exposes auto-delegate state to the brain"
```

---

## Task 7: Stale-branch sweeper (pure logic)

**Files:**
- Create: `src/orchestrator/core/branch_sweeper.py`
- Test: `tests/test_branch_sweeper.py` (create)

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_branch_sweeper.py
"""Pure decision logic for which remote branches are safe to delete."""

from __future__ import annotations

from orchestrator.core.branch_sweeper import dead_branches


def test_keeps_protected_and_live():
    branches = ["main", "master", "release-1", "agent/live", "agent/failed", "plan/merged"]
    open_pr_branches = {"agent/live"}
    terminal_failed = {"agent/failed"}
    merged_plan = {"plan/merged"}
    result = dead_branches(
        branches,
        open_pr_branches=open_pr_branches,
        terminal_failed=terminal_failed,
        merged_plan=merged_plan,
    )
    assert set(result) == {"agent/failed", "plan/merged"}


def test_never_touches_unknown_branches():
    # A branch with no ledger evidence is left alone (fail-safe).
    result = dead_branches(
        ["feat/mystery"],
        open_pr_branches=set(),
        terminal_failed=set(),
        merged_plan=set(),
    )
    assert result == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_branch_sweeper.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement the logic**

```python
# src/orchestrator/core/branch_sweeper.py
"""Decide which remote branches are provably dead and safe to delete.

Fail-safe by construction: a branch is only returned when the run ledger proves
it is dead (a terminal-failed agent branch, or an already-merged plan branch)
AND it has no open PR. Protected branches and branches with no ledger evidence
are never returned.
"""

from __future__ import annotations

from collections.abc import Iterable

_PROTECTED_PREFIXES = ("main", "master", "release")


def _is_protected(branch: str) -> bool:
    return any(branch == p or branch.startswith(p) for p in _PROTECTED_PREFIXES)


def dead_branches(
    branches: Iterable[str],
    *,
    open_pr_branches: set[str],
    terminal_failed: set[str],
    merged_plan: set[str],
) -> list[str]:
    """Return branches safe to delete.

    Args:
        branches: All remote branch names.
        open_pr_branches: Branches that still have an open PR (never deleted).
        terminal_failed: agent/* branches whose run reached a terminal failed state.
        merged_plan: plan/* branches whose integration PR already merged.
    """
    dead: list[str] = []
    for b in branches:
        if _is_protected(b) or b in open_pr_branches:
            continue
        if b in terminal_failed or b in merged_plan:
            dead.append(b)
    return dead
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_branch_sweeper.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/branch_sweeper.py tests/test_branch_sweeper.py
git commit -m "feat(git): pure stale-branch decision logic"
```

---

## Task 8: Wire the sweeper into the reconcile loop

**Files:**
- Modify: `src/orchestrator/core/orchestrator_reconcile.py`
- Test: `tests/test_reconcile_sweeper.py` (create)

**Depends on:** Task 7

- [ ] **Step 1: Read the reconcile loop first**

Open `src/orchestrator/core/orchestrator_reconcile.py`. Find `reconcile_runs` (the periodic backstop). Identify: how it enumerates projects, how it queries `agent_runs`/`tasks` for terminal states, and the `GitOps` instance it uses. The sweeper call must be additive and best-effort (never raise into the loop).

- [ ] **Step 2: Write the failing test**

```python
# tests/test_reconcile_sweeper.py
"""The reconcile pass invokes the branch sweeper and deletes only dead branches."""

from __future__ import annotations

import pytest

from orchestrator.core import orchestrator_reconcile as rec


@pytest.mark.asyncio
async def test_sweep_deletes_only_dead(monkeypatch):
    deleted = []

    async def fake_list_remote_branches(repo_url):
        return ["main", "agent/failed", "agent/live", "plan/merged"]

    async def fake_delete_remote_branch(repo_url, branch):
        deleted.append(branch)

    # Build the minimal ledger inputs the sweep helper expects (see Step 3).
    ledger = {
        "open_pr_branches": {"agent/live"},
        "terminal_failed": {"agent/failed"},
        "merged_plan": {"plan/merged"},
    }
    await rec.sweep_dead_branches(
        repo_url="https://github.com/o/r",
        list_remote_branches=fake_list_remote_branches,
        delete_remote_branch=fake_delete_remote_branch,
        ledger=ledger,
    )
    assert set(deleted) == {"agent/failed", "plan/merged"}
```

> Adapt the helper signature to the real `GitOps` methods available. If `GitOps` lacks `list_remote_branches` / `delete_remote_branch`, add thin wrappers in `git_ops.py` (`git ls-remote --heads` to list; `gh api -X DELETE repos/{slug}/git/refs/heads/{branch}` or `git push origin --delete` to delete) with their own unit tests mirroring existing `remote_branch_exists`.

- [ ] **Step 3: Implement `sweep_dead_branches` + call it from `reconcile_runs`**

Add a module-level `async def sweep_dead_branches(...)` to `orchestrator_reconcile.py` that: lists remote branches, calls `branch_sweeper.dead_branches(...)` with the ledger sets, and deletes each result best-effort (log + swallow per-branch errors). Then, near the end of `reconcile_runs`, build the ledger sets from the DB (open PRs from tasks with an open PR url; `terminal_failed` = agent branches of tasks in a terminal FAILED state; `merged_plan` = plan branches whose plan is MERGED/complete) and `await sweep_dead_branches(...)` inside a `try/except` that logs and continues.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_reconcile_sweeper.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/orchestrator_reconcile.py src/orchestrator/core/git_ops.py tests/test_reconcile_sweeper.py
git commit -m "feat(reconcile): sweep dead agent/plan branches each pass"
```

---

## Task 9: Single-branch mode in the harness entrypoints

**Files:**
- Modify: `docker/agy-agent/entrypoint.sh`, `docker/opencode-agent/entrypoint.sh`
- Test: `tests/test_entrypoint_single_branch.py` (create — static assertions on the script)

**Depends on:** None

- [ ] **Step 1: Write the failing test (static contract check)**

```python
# tests/test_entrypoint_single_branch.py
"""Entrypoints honor SINGLE_BRANCH: reuse existing remote branch, no force clobber."""

from __future__ import annotations

from pathlib import Path

import pytest

ENTRYPOINTS = [
    "docker/agy-agent/entrypoint.sh",
    "docker/opencode-agent/entrypoint.sh",
]


@pytest.mark.parametrize("path", ENTRYPOINTS)
def test_single_branch_reuses_existing_remote(path):
    script = Path(path).read_text(encoding="utf-8")
    # Guarded on the SINGLE_BRANCH flag.
    assert "SINGLE_BRANCH" in script
    # When reusing, it checks out the existing remote work branch rather than
    # always recreating it from BASE_BRANCH.
    assert 'origin/${BRANCH}' in script or "origin/$BRANCH" in script
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_entrypoint_single_branch.py -v`
Expected: FAIL — `SINGLE_BRANCH` not referenced.

- [ ] **Step 3: Edit the entrypoints**

In each entrypoint, at the branch-creation block (agy `~line 106-118`), replace the unconditional `git checkout -b "${BRANCH}"` with SINGLE_BRANCH-aware logic:

```bash
if [ "${SINGLE_BRANCH:-0}" = "1" ] && git rev-parse --verify "origin/${BRANCH}" >/dev/null 2>&1; then
    echo "--- Single-branch mode: reusing existing origin/${BRANCH} ---"
    git checkout -b "${BRANCH}" "origin/${BRANCH}"
else
    echo "--- Creating branch ${BRANCH} from ${BASE_BRANCH} ---"
    git checkout -b "${BRANCH}"
fi
```

And at the push step (agy `~line 269`), use a non-force push when reusing a shared branch so accumulated commits are preserved:

```bash
if [ "${SINGLE_BRANCH:-0}" = "1" ]; then
    git push -u origin "${BRANCH}"
else
    git push -u --force origin "${BRANCH}"
fi
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_entrypoint_single_branch.py -v`
Expected: PASS (both entrypoints).

- [ ] **Step 5: Rebuild the images (manual, note in commit)**

```bash
docker build -t agy-agent:latest -f docker/agy-agent/Dockerfile docker/agy-agent/
docker build -t opencode-agent:latest -f docker/opencode-agent/Dockerfile docker/opencode-agent/
```

- [ ] **Step 6: Commit**

```bash
git add docker/agy-agent/entrypoint.sh docker/opencode-agent/entrypoint.sh tests/test_entrypoint_single_branch.py
git commit -m "feat(harness): SINGLE_BRANCH mode reuses work branch, non-force push"
```

---

## Task 10: Pass SINGLE_BRANCH when auto-delegate mode is on

**Files:**
- Modify: `src/orchestrator/core/agent_manager.py` (env contract), and the dispatch call site that builds spawn env
- Test: `tests/test_agent_manager_single_branch.py` (create)

**Depends on:** Task 3, Task 9

- [ ] **Step 1: Read the spawn env builder**

In `src/orchestrator/core/agent_manager.py`, find where the `environment` dict is built (it sets `MODEL`, `BRANCH`, `BASE_BRANCH`, etc. — around line 192). Add a parameter so callers can request single-branch mode.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_agent_manager_single_branch.py
"""spawn_agent sets SINGLE_BRANCH=1 when requested."""

from __future__ import annotations

from orchestrator.core.agent_manager import AgentManager


def test_env_contains_single_branch(monkeypatch):
    # Use the same construction pattern as existing agent_manager tests
    # (grep tests/test_agent_manager*.py). Assert the built env dict carries
    # SINGLE_BRANCH="1" when single_branch=True is passed, and omits/zeros it
    # otherwise. If spawn is hard to unit-test, factor the env-building into a
    # pure helper `build_env(..., single_branch: bool)` and test that helper.
    ...
```

> Prefer extracting a pure `build_spawn_env(...)` helper if the current `spawn_agent` is not unit-testable without Docker. Test the helper.

- [ ] **Step 3: Thread the flag**

- Add `single_branch: bool = False` to the spawn env builder; when True set `environment["SINGLE_BRANCH"] = "1"`.
- At the dispatch call site (the code that calls `spawn_agent` for a task), resolve `await effective_settings.auto_delegate_enabled()` and pass `single_branch=that`. When single-branch, set `BRANCH` to the task's caller-named working branch (the dispatch `branch`/base), NOT a fresh `agent/{slug}`, and `BASE_BRANCH` to the project `default_branch` (e.g. `main`).

> Grep the dispatch path (`orchestrator_dispatch.py`, `mcp__praxis__dispatch_task` handler / `api/tasks.py`) to find where `BRANCH` is currently generated as `agent/{slug}` and branch on the auto-delegate flag there.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_agent_manager_single_branch.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/agent_manager.py src/orchestrator/core/orchestrator_dispatch.py tests/test_agent_manager_single_branch.py
git commit -m "feat(dispatch): use single work branch when auto-delegate mode is on"
```

---

## Task 11: Dashboard switch, CLAUDE.md convention, docs

**Files:**
- Modify: `web/index.html`, `web/app.js`
- Modify: `CLAUDE.md`
- Modify: `docs/superpowers/specs/2026-07-11-capability-engine-roadmap.md` (add the CADMAS-CTX follow-up entry)
- Test: manual (dashboard) + `tests/test_docs_convention.py` (create — asserts CLAUDE.md carries the convention)

**Depends on:** Task 4

- [ ] **Step 1: Write the failing test**

```python
# tests/test_docs_convention.py
"""CLAUDE.md documents the auto-delegate brain convention."""

from pathlib import Path


def test_claude_md_has_auto_delegate_convention():
    text = Path("CLAUDE.md").read_text(encoding="utf-8")
    assert "auto-delegate" in text.lower()
    assert "/api/settings/auto-delegate" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_docs_convention.py -v`
Expected: FAIL.

- [ ] **Step 3: Add the CLAUDE.md convention**

Add a short subsection to `CLAUDE.md` (near "Key Design Decisions"):

```markdown
## Auto-Delegate Mode (daily-dev)

When Praxis auto-delegate mode is ON (`GET /api/settings/auto-delegate` → `enabled:true`),
the brain does NOT edit code directly. For each implementation task it designs the worker
prompt, calls the MCP `dispatch_task` (which uses the global default worker — reference:
Gemini 3.6 Flash High via agy), then reviews the resulting PR. Planning, prompt design, and
review stay with the brain. Mode is sequential (one delegate in flight at a time) and uses a
single caller-named work branch; dead branches are swept by the reconcile loop. Toggle:
`praxis mode on|off|status`.
```

- [ ] **Step 4: Dashboard switch**

In `web/index.html` add a labeled toggle in the Settings area; in `web/app.js` wire it to `GET`/`PUT /api/settings/auto-delegate`, displaying the resolved worker (`harness / model`). Follow the existing settings-tab fetch/render pattern in `app.js`. Verify manually against the running dashboard.

- [ ] **Step 5: Roadmap follow-up entry**

Add a short "Auto-delegate: capability-calibrated delegation (CADMAS-CTX)" follow-up to the capability-engine roadmap spec, pointing at `should_delegate()` as the seam and arXiv 2604.17950 (Beta posterior per model×task_type×project + `Score = μ − λσ`; note non-stationarity needs recency weighting).

- [ ] **Step 6: Run tests + format + full suite**

```bash
uv run pytest tests/test_docs_convention.py -v
uv run ruff format src/ tests/ && uv run ruff check --fix src/ tests/
uv run mypy src/orchestrator/ --ignore-missing-imports
uv run pytest -q
```

Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add web/index.html web/app.js CLAUDE.md docs/superpowers/specs/2026-07-11-capability-engine-roadmap.md tests/test_docs_convention.py
git commit -m "feat(ui/docs): auto-delegate dashboard switch, CLAUDE.md convention, roadmap follow-up"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1, Task 7, Task 9 (no dependencies — run in parallel)
- **Wave 2:** Task 2 (Task 1), Task 3 (Task 1), Task 8 (Task 7)
- **Wave 3:** Task 4 (Task 3), Task 10 (Task 3, Task 9)
- **Wave 4:** Task 5 (Task 4), Task 6 (Task 4), Task 11 (Task 4)

---

## Notes

- **Scope guard:** this plan does NOT implement capability calibration. The delegate decision is implicitly "always delegate while mode is on"; the `should_delegate()` seam + CADMAS-CTX Beta-posterior scoring is a separate roadmap plan (memory `cadmas-ctx-calibration-reference`).
- **Sequential enforcement is v1-deferred (explicit):** the spec calls for queueing a second concurrent delegate. This plan does NOT add a hard in-flight queue; it relies on the brain dispatching sequentially (daily-dev is conversational) plus single-branch reuse + non-force push (Task 9) so accumulated commits survive. A true concurrency gate (reject/queue a second in-flight delegate on the same work branch) is a small follow-up task if concurrent dispatch ever happens in practice. Flagged here so it is not mistaken for an oversight.
- **Image rebuilds:** Task 9 changes entrypoints — the harness images MUST be rebuilt (Task 9 Step 5) or a stale image silently ignores `SINGLE_BRANCH`.
- **Dogfood target:** after merge, register the praxis repo as a project (`harness: agy`, `model_name: "Gemini 3.6 Flash (High)"`, approval gate ON) and dogfood this exact plan with the Gemini 3.6 Flash High worker.
- **Verify_cmd reminder:** ensure the dogfood project sets `verify_cmd` (pytest + ruff + mypy + bandit) so the loop runs the gates, per prior dogfood lessons.
