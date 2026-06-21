# Multi-Harness Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a Praxis user choose, per-project, which implementation harness runs their tasks (Aider, OpenCode, or OpenHands), and surface an "About" panel describing each harness (description, what's unique, pros/cons, when-to-pick, and the Praxis recommendation).

**Architecture:** A new in-code **harness registry** (`core/harnesses.py`) is the single source of truth for both behavior (Docker image, capabilities) and presentation (About content). The `projects` table gains a `harness` column (default `"aider"`, fully backward-compatible). `AgentManager.spawn_agent` selects the Docker image from the registry by harness id and sets a **harness-agnostic env contract** (`HARNESS`, `MODEL`, `OPENAI_API_BASE`, plus the existing repo/branch/callback vars). Each harness gets its own Dockerfile sharing the same entrypoint contract — same env vars in, same JSON callback out. A `GET /api/harnesses` endpoint exposes the registry to the dashboard, which renders a harness dropdown and About cards.

**Tech Stack:** Python 3.11, FastAPI, pydantic, aiosqlite (raw SQL), Docker SDK for Python, pytest (`asyncio_mode = "auto"`), single-file HTML/JS dashboard (`web/index.html`).

---

## Background & Orientation (read before starting)

You are working in `C:\working-space\praxis` on Windows (PowerShell). This is a Docker-based AI agent orchestrator. Read `CLAUDE.md` and `CLAUDE.local.md` at the repo root first — they contain critical gotchas. Key facts you need:

- **Run everything via uv:** `uv run pytest ...`, `uv run ruff ...`, `uv run mypy ...`.
- **ruff subcommand is `ruff format`** (not `ruff fmt`).
- **Tests:** `uv run pytest --cov=orchestrator --cov-report=term-missing -v`. Current suite: 156 tests passing, mypy clean, 88%+ coverage. Do not regress.
- **Source layout:** code under `src/orchestrator/`, package import root is `orchestrator.*` (because `[tool.setuptools.packages.find]` has `where = ["src"]`).
- **No ORM.** Raw SQL via `aiosqlite`. Migrations are `CREATE TABLE IF NOT EXISTS` strings in `MIGRATIONS` plus idempotent `ALTER TABLE ... ADD COLUMN` calls wrapped in `contextlib.suppress(Exception)` (see `database.py:146-150`).
- **DB resets:** delete `data/orchestrator.db` to start fresh; lifespan recreates tables and seeds the default user.
- **Type style:** `X | Y` unions, built-in generics (`list[str]`), `from __future__ import annotations` at top of every module, Google-style docstrings, line length 88.

### Current Aider-only integration (what you are generalizing)

- `src/orchestrator/core/agent_manager.py` — `AgentManager`. Constant `AGENT_IMAGE = "aider-agent:latest"`. `spawn_agent(...)` builds an `environment` dict and calls `self._client.containers.run(image=AGENT_IMAGE, ...)`. The env dict currently includes `AIDER_MODEL = f"openai/{model_name}"`.
- `src/orchestrator/core/orchestrator.py:96-104` — calls `self._agents.spawn_agent(...)` inside `dispatch_pending_tasks`, passing `model_name=project["model_name"]`. The `project` dict is a full DB row.
- `docker/aider-agent/entrypoint.sh` — clones repo, creates `BASE_BRANCH` then `BRANCH`, runs `aider --message "$TASK_PROMPT" --model "$AIDER_MODEL" ...`, pushes, creates PR with `gh`, and POSTs a JSON callback to `CALLBACK_URL`. This is the **contract every harness must honor**.

### The shared entrypoint contract (every harness image MUST follow)

Input env vars (set by `AgentManager.spawn_agent`):
- `REPO_URL`, `BRANCH`, `BASE_BRANCH`, `TASK_PROMPT`, `GH_TOKEN`, `CALLBACK_URL`, `TASK_ID` — unchanged from today.
- `OPENAI_API_BASE` — e.g. `http://host.docker.internal:1234/v1`.
- **NEW** `MODEL` — the raw model name (e.g. `qwen3-32b`), with no `openai/` prefix. Each entrypoint adds whatever prefix its harness needs.
- **NEW** `HARNESS` — the harness id (informational/logging).

Required behavior: configure git auth via `GH_TOKEN`, clone, create `BASE_BRANCH` (with the existing push-or-fetch race handling), create `BRANCH`, run the harness against `TASK_PROMPT`, **ensure changes are committed**, push `BRANCH`, create a PR with `gh`, and POST the callback `{"task_id","run_id","status","pr_url"}` on EXIT (success or failure).

---

## File Structure

**Create:**
- `src/orchestrator/core/harnesses.py` — `HarnessSpec` dataclass + `REGISTRY` + helpers (`get_harness`, `default_harness_id`, `list_harnesses`).
- `src/orchestrator/api/harnesses.py` — `GET /api/harnesses` router.
- `tests/test_harnesses.py` — registry unit tests.
- `tests/test_api_harnesses.py` — endpoint integration tests.
- `docker/opencode-agent/Dockerfile`, `docker/opencode-agent/entrypoint.sh`.
- `docker/openhands-agent/Dockerfile`, `docker/openhands-agent/entrypoint.sh`.

**Modify:**
- `src/orchestrator/database.py` — add `harness` column (migration + idempotent ALTER).
- `src/orchestrator/models/schemas.py` — `harness` field on `ProjectCreate`/`ProjectUpdate`/`ProjectResponse` with registry-backed validation.
- `src/orchestrator/api/projects.py` — persist `harness` on create.
- `src/orchestrator/core/agent_manager.py` — `spawn_agent` gains `harness` param; image + env from registry.
- `src/orchestrator/core/orchestrator.py:96-104` — pass `harness=project["harness"]`.
- `src/orchestrator/main.py` — register the harnesses router.
- `docker/aider-agent/entrypoint.sh` — consume generic `MODEL` instead of `AIDER_MODEL`.
- `tests/test_agent_manager.py` — update for new param/env.
- `web/index.html` — harness dropdown in project form + About panel.
- `CLAUDE.md` — gotchas + structure updates.

---

## Task 1: Harness registry

**Files:**
- Create: `src/orchestrator/core/harnesses.py`
- Test: `tests/test_harnesses.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_harnesses.py
"""Harness registry unit tests."""
# ruff: noqa: S101

from __future__ import annotations

import pytest

from orchestrator.core.harnesses import (
    REGISTRY,
    HarnessSpec,
    default_harness_id,
    get_harness,
    list_harnesses,
)


@pytest.mark.unit
def test_registry_contains_expected_harnesses() -> None:
    assert set(REGISTRY) == {"aider", "opencode", "openhands"}


@pytest.mark.unit
def test_default_is_aider() -> None:
    assert default_harness_id() == "aider"
    assert REGISTRY["aider"].image == "aider-agent:latest"


@pytest.mark.unit
def test_exactly_one_recommended() -> None:
    recommended = [h for h in REGISTRY.values() if h.recommended]
    assert len(recommended) == 1


@pytest.mark.unit
def test_get_harness_returns_spec() -> None:
    spec = get_harness("opencode")
    assert isinstance(spec, HarnessSpec)
    assert spec.id == "opencode"
    assert spec.image == "opencode-agent:latest"


@pytest.mark.unit
def test_get_unknown_harness_raises() -> None:
    with pytest.raises(KeyError):
        get_harness("nope")


@pytest.mark.unit
def test_every_spec_has_about_content() -> None:
    for spec in REGISTRY.values():
        assert spec.description
        assert spec.uniqueness
        assert spec.pros
        assert spec.cons
        assert spec.when_to_pick


@pytest.mark.unit
def test_list_harnesses_is_serializable() -> None:
    items = list_harnesses()
    assert all(isinstance(item, dict) for item in items)
    assert {"id", "display_name", "pros", "cons"} <= set(items[0])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_harnesses.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.core.harnesses'`

- [ ] **Step 3: Write the implementation**

```python
# src/orchestrator/core/harnesses.py
"""Implementation-harness registry: behavior + presentation metadata.

Single source of truth for which Docker image runs a project's tasks and for
the user-facing "About" content (description, pros/cons, when-to-pick).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class HarnessSpec:
    """A selectable implementation harness."""

    id: str
    display_name: str
    image: str
    description: str
    uniqueness: str
    when_to_pick: str
    pros: tuple[str, ...]
    cons: tuple[str, ...]
    maturity: str
    recommended: bool = False
    supports_local_llm: bool = True
    does_own_git: bool = True
    notes: tuple[str, ...] = field(default_factory=tuple)


REGISTRY: dict[str, HarnessSpec] = {
    "aider": HarnessSpec(
        id="aider",
        display_name="Aider",
        image="aider-agent:latest",
        description=(
            "A lean, terminal-first pair-programming agent. Edits multiple "
            "files via diffs and commits to git automatically. Praxis's "
            "original and default harness."
        ),
        uniqueness=(
            "Native, first-class git integration: every change is an "
            "auto-commit, so the orchestrator's branch/PR flow needs no extra "
            "wiring. Smallest, fastest image."
        ),
        when_to_pick=(
            "Default choice. Best for focused, well-scoped tasks on small-to-"
            "medium repos and when you want minimal moving parts and fast runs."
        ),
        pros=(
            "Auto-commits — zero git glue required",
            "Mature and stable (in production since 2023)",
            "Lightweight image, fast cold start",
            "Excellent diff/edit quality on scoped tasks",
        ),
        cons=(
            "Less autonomous on large multi-step tasks",
            "No built-in sandboxed code execution or browsing",
            "Single-shot --message runs; limited self-correction",
        ),
        maturity="stable",
        recommended=True,
    ),
    "opencode": HarnessSpec(
        id="opencode",
        display_name="OpenCode",
        image="opencode-agent:latest",
        description=(
            "A provider-agnostic, terminal-first coding agent (the most "
            "popular open-source Claude Code alternative). Runs headless via "
            "`opencode run` and works with any OpenAI-compatible endpoint."
        ),
        uniqueness=(
            "Truly model-agnostic with a polished agent loop and strong tool "
            "use; the de facto community default. Mixes/swaps providers freely."
        ),
        when_to_pick=(
            "When you want the most actively-developed, capable general agent "
            "and your local model is strong. Good middle ground between Aider's "
            "minimalism and OpenHands' heavyweight autonomy."
        ),
        pros=(
            "Largest community / most active development",
            "Strong agentic loop and tool use",
            "Works with any OpenAI-compatible local model",
            "Headless `opencode run` mode suits automation",
        ),
        cons=(
            "Does not auto-commit — entrypoint must stage & commit changes",
            "Larger image than Aider",
            "Config-driven provider setup adds a step",
        ),
        maturity="active",
        does_own_git=False,
        notes=(
            "Praxis's entrypoint runs `git add -A && git commit` after the "
            "agent because OpenCode edits files but does not commit.",
        ),
    ),
    "openhands": HarnessSpec(
        id="openhands",
        display_name="OpenHands",
        image="openhands-agent:latest",
        description=(
            "A fully autonomous software-engineering agent (formerly "
            "OpenDevin) that runs in a sandboxed runtime able to execute code, "
            "browse, and edit end-to-end. Strongest headless/CI story."
        ),
        uniqueness=(
            "Goes beyond editing: it can run the code it writes, inspect "
            "output, and iterate autonomously across many steps in a sandbox."
        ),
        when_to_pick=(
            "For large, open-ended, multi-step tasks where the agent benefits "
            "from running tests/commands and self-correcting. Pick when task "
            "complexity justifies the heavier runtime."
        ),
        pros=(
            "Most autonomous — executes & verifies its own changes",
            "Best for complex, exploratory, multi-file work",
            "Model-agnostic via LiteLLM (OpenAI-compatible local models work)",
            "Strong headless batch mode",
        ),
        cons=(
            "Heaviest image and slowest runs",
            "Needs a sandbox runtime (local runtime, or a mounted Docker "
            "socket for the container runtime)",
            "Higher token usage from multi-step loops",
            "Does not auto-commit — entrypoint must stage & commit changes",
        ),
        maturity="active",
        does_own_git=False,
        notes=(
            "Runs headless via `python -m openhands.core.main -t \"$TASK\" "
            "--override-with-envs`. Praxis uses the local runtime to avoid "
            "Docker-in-Docker; if unavailable, mount /var/run/docker.sock.",
        ),
    ),
}


def default_harness_id() -> str:
    """The harness assigned to projects that don't specify one."""

    return "aider"


def get_harness(harness_id: str) -> HarnessSpec:
    """Return the spec for ``harness_id`` or raise ``KeyError``."""

    return REGISTRY[harness_id]


def list_harnesses() -> list[dict[str, Any]]:
    """Return all specs as serializable dicts (recommended first)."""

    specs = sorted(REGISTRY.values(), key=lambda s: (not s.recommended, s.id))
    return [asdict(spec) for spec in specs]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_harnesses.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/harnesses.py tests/test_harnesses.py
git commit -m "feat: add harness registry (behavior + about content)"
```

---

## Task 2: Add `harness` column to projects

**Files:**
- Modify: `src/orchestrator/database.py:25-43` (projects CREATE TABLE) and `:146-150` (idempotent ALTERs)
- Test: `tests/test_database.py` (create if absent)

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_database.py  (append if file exists; otherwise create with this header)
"""Database migration tests."""
# ruff: noqa: S101

from __future__ import annotations

import uuid

import pytest

from orchestrator.database import Database


@pytest.mark.unit
async def test_projects_table_has_harness_column(tmp_path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'mig.db'}")
    await db.initialize()
    try:
        await db.execute(
            "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
            ("u1", "admin", "h"),
        )
        pid = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO projects (id, user_id, name, repo_url, model_name, "
            "harness) VALUES (?, ?, ?, ?, ?, ?)",
            (pid, "u1", "p", "https://x/y", "m", "opencode"),
        )
        row = await db.fetch_one(
            "SELECT harness FROM projects WHERE id = ?", (pid,)
        )
        assert row is not None
        assert row["harness"] == "opencode"
    finally:
        await db.close()


@pytest.mark.unit
async def test_harness_defaults_to_aider(tmp_path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'mig2.db'}")
    await db.initialize()
    try:
        await db.execute(
            "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
            ("u1", "admin", "h"),
        )
        pid = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO projects (id, user_id, name, repo_url, model_name) "
            "VALUES (?, ?, ?, ?, ?)",
            (pid, "u1", "p", "https://x/y", "m"),
        )
        row = await db.fetch_one(
            "SELECT harness FROM projects WHERE id = ?", (pid,)
        )
        assert row is not None
        assert row["harness"] == "aider"
    finally:
        await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_database.py -v`
Expected: FAIL with `sqlite3.OperationalError: table projects has no column named harness`

- [ ] **Step 3: Edit the projects CREATE TABLE migration**

In `src/orchestrator/database.py`, in the `projects` migration string, add the `harness` column right after the `model_name` line:

```python
        model_name TEXT NOT NULL DEFAULT '',
        harness TEXT NOT NULL DEFAULT 'aider',
        agent_model TEXT,
```

- [ ] **Step 4: Add idempotent ALTER for existing databases**

In `src/orchestrator/database.py`, update the idempotent-column loop (currently `for column in ("agent_model", "agent_model_effort"):`) so existing DBs gain the column with the correct default. Replace that loop with:

```python
        for column, ddl in (
            ("agent_model", "agent_model TEXT"),
            ("agent_model_effort", "agent_model_effort TEXT"),
            ("harness", "harness TEXT NOT NULL DEFAULT 'aider'"),
        ):
            with contextlib.suppress(Exception):
                await connection.execute(
                    f"ALTER TABLE projects ADD COLUMN {ddl}"  # noqa: S608
                )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_database.py -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/database.py tests/test_database.py
git commit -m "feat: add harness column to projects table"
```

---

## Task 3: Harness field on project schemas

**Files:**
- Modify: `src/orchestrator/models/schemas.py` (`ProjectCreate`, `ProjectUpdate`, `ProjectResponse`)
- Test: `tests/test_schemas.py` (create if absent)

**Depends on:** Task 1

- [ ] **Step 1: Write the failing test**

```python
# tests/test_schemas.py  (append if file exists; otherwise create with this header)
"""Schema validation tests for harness field."""
# ruff: noqa: S101

from __future__ import annotations

import pytest
from pydantic import ValidationError

from orchestrator.models.schemas import ProjectCreate, ProjectUpdate


@pytest.mark.unit
def test_project_create_defaults_harness_to_aider() -> None:
    p = ProjectCreate(name="p", repo_url="https://x/y", model_name="m")
    assert p.harness == "aider"


@pytest.mark.unit
def test_project_create_accepts_valid_harness() -> None:
    p = ProjectCreate(
        name="p", repo_url="https://x/y", model_name="m", harness="opencode"
    )
    assert p.harness == "opencode"


@pytest.mark.unit
def test_project_create_rejects_unknown_harness() -> None:
    with pytest.raises(ValidationError):
        ProjectCreate(
            name="p", repo_url="https://x/y", model_name="m", harness="bogus"
        )


@pytest.mark.unit
def test_project_update_rejects_unknown_harness() -> None:
    with pytest.raises(ValidationError):
        ProjectUpdate(harness="bogus")


@pytest.mark.unit
def test_project_update_allows_none_harness() -> None:
    assert ProjectUpdate(harness=None).harness is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_schemas.py -v`
Expected: FAIL with `ValidationError ... extra fields not permitted` / `unexpected keyword argument 'harness'`

- [ ] **Step 3: Add the field + validator to schemas**

In `src/orchestrator/models/schemas.py`, add this import near the top (after the existing pydantic import):

```python
from orchestrator.core.harnesses import REGISTRY, default_harness_id
```

In `ProjectCreate`, add the field (after `agent_model_effort`):

```python
    harness: str = Field(default_factory=default_harness_id)
```

and add this validator method to `ProjectCreate`:

```python
    @field_validator("harness")
    @classmethod
    def validate_harness(cls, value: str) -> str:
        if value not in REGISTRY:
            allowed = ", ".join(sorted(REGISTRY))
            msg = f"harness must be one of: {allowed}"
            raise ValueError(msg)
        return value
```

In `ProjectUpdate`, add the optional field (after `agent_model_effort`):

```python
    harness: str | None = None
```

and add this validator method to `ProjectUpdate`:

```python
    @field_validator("harness")
    @classmethod
    def validate_optional_harness(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in REGISTRY:
            allowed = ", ".join(sorted(REGISTRY))
            msg = f"harness must be one of: {allowed}"
            raise ValueError(msg)
        return value
```

In `ProjectResponse`, add the field (after `agent_model_effort`):

```python
    harness: str = "aider"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_schemas.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/models/schemas.py tests/test_schemas.py
git commit -m "feat: add validated harness field to project schemas"
```

---

## Task 4: Persist harness on project create

**Files:**
- Modify: `src/orchestrator/api/projects.py:35-56` (INSERT statement)
- Test: `tests/test_api_projects.py` (append)

**Depends on:** Task 2, Task 3

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_projects.py  (append; reuse existing client/auth fixtures)
# ruff: noqa: S101


def test_create_project_persists_harness(client, auth_headers) -> None:
    resp = client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "harness-proj",
            "repo_url": "https://github.com/u/r",
            "model_name": "qwen3-32b",
            "harness": "opencode",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["harness"] == "opencode"


def test_create_project_defaults_harness_aider(client, auth_headers) -> None:
    resp = client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "default-harness",
            "repo_url": "https://github.com/u/r",
            "model_name": "qwen3-32b",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["harness"] == "aider"
```

> Note: confirm the actual fixture names in `tests/test_api_projects.py` / `tests/conftest.py`. If the existing tests use a different client/header fixture name (e.g. `test_client`, `headers`), match them.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_projects.py -k harness -v`
Expected: FAIL — `harness` not persisted (response defaults to "aider" even when "opencode" sent, or KeyError).

- [ ] **Step 3: Add `harness` to the INSERT**

In `src/orchestrator/api/projects.py`, update the INSERT in `create_project`. Add `harness` to the column list and a `?` placeholder, and add `body.harness` to the values tuple:

```python
    await db.execute(
        """INSERT INTO projects
           (id, user_id, name, repo_url, default_branch, approval_gate,
            confidence_threshold, max_retries, max_improvement_cycles,
            lm_studio_url, model_name, harness, agent_model, agent_model_effort)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            project_id,
            user["id"],
            body.name,
            body.repo_url,
            body.default_branch,
            body.approval_gate,
            body.confidence_threshold,
            body.max_retries,
            body.max_improvement_cycles,
            body.lm_studio_url,
            body.model_name,
            body.harness,
            body.agent_model,
            body.agent_model_effort,
        ),
    )
```

(The PATCH `update_project` needs no change — it builds its SET clause dynamically from `body.model_dump(exclude_none=True)`, so `harness` is handled automatically once it's on `ProjectUpdate`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_api_projects.py -k harness -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run the full project API suite for regressions**

Run: `uv run pytest tests/test_api_projects.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/api/projects.py tests/test_api_projects.py
git commit -m "feat: persist harness selection on project create"
```

---

## Task 5: `GET /api/harnesses` endpoint

**Files:**
- Create: `src/orchestrator/api/harnesses.py`
- Modify: `src/orchestrator/main.py` (register router)
- Test: `tests/test_api_harnesses.py`

**Depends on:** Task 1

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_harnesses.py
"""Harness catalog endpoint tests."""
# ruff: noqa: S101

from __future__ import annotations


def test_list_harnesses_requires_auth(client) -> None:
    resp = client.get("/api/harnesses")
    assert resp.status_code in (401, 403)


def test_list_harnesses_returns_catalog(client, auth_headers) -> None:
    resp = client.get("/api/harnesses", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    ids = {h["id"] for h in data}
    assert ids == {"aider", "opencode", "openhands"}


def test_recommended_harness_is_first(client, auth_headers) -> None:
    resp = client.get("/api/harnesses", headers=auth_headers)
    data = resp.json()
    assert data[0]["recommended"] is True


def test_each_harness_has_about_fields(client, auth_headers) -> None:
    resp = client.get("/api/harnesses", headers=auth_headers)
    for h in resp.json():
        for key in (
            "display_name",
            "description",
            "uniqueness",
            "when_to_pick",
            "pros",
            "cons",
        ):
            assert h[key]
```

> Match the actual `client` / `auth_headers` fixture names used in the existing `tests/test_api_*.py` files.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_harnesses.py -v`
Expected: FAIL with 404 (route not registered).

- [ ] **Step 3: Create the router**

```python
# src/orchestrator/api/harnesses.py
"""Harness catalog REST endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from orchestrator.api.auth import verify_token
from orchestrator.core.harnesses import list_harnesses


router = APIRouter(tags=["harnesses"], dependencies=[Depends(verify_token)])


@router.get("/harnesses")
async def get_harnesses() -> list[dict[str, Any]]:
    """Return the harness catalog (recommended first) for the UI About panel."""

    return list_harnesses()
```

- [ ] **Step 4: Register the router in `main.py`**

In `src/orchestrator/main.py`, find where the other API routers are included (e.g. `app.include_router(projects.router, prefix="/api")`). Add an import alongside the existing `from orchestrator.api import ...` block:

```python
from orchestrator.api import harnesses as harnesses_api
```

and register it next to the others, matching the existing prefix convention:

```python
app.include_router(harnesses_api.router, prefix="/api")
```

> Read `main.py` first to copy the exact import style and prefix used by sibling routers (some projects import the module, some import `router`). Mirror what's there.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_api_harnesses.py -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/api/harnesses.py src/orchestrator/main.py tests/test_api_harnesses.py
git commit -m "feat: expose GET /api/harnesses catalog endpoint"
```

---

## Task 6: AgentManager selects image + env by harness

**Files:**
- Modify: `src/orchestrator/core/agent_manager.py`
- Test: `tests/test_agent_manager.py`

**Depends on:** Task 1

- [ ] **Step 1: Update the existing tests to the new contract**

In `tests/test_agent_manager.py`, the two spawn tests must pass a `harness` and assert the new env contract. Replace the body of `test_spawn_agent` so its `spawn_agent(...)` call includes `harness="aider"`, and add assertions; then replace `test_spawn_agent_sets_correct_env` to assert generic `MODEL`/`HARNESS` and image selection. Use these:

```python
@pytest.mark.unit
@patch("orchestrator.core.agent_manager.docker")
def test_spawn_agent(mock_docker: MagicMock) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    container = _mock_container()
    mock_client.containers.run.return_value = container

    manager = AgentManager(
        lm_studio_url="http://host.docker.internal:1234",
        github_token="ghp_test",
    )
    result = manager.spawn_agent(
        task_id="task-1",
        repo_url="https://github.com/user/repo.git",
        branch="agent/login",
        base_branch="plan/2026-06-01-auth",
        task_prompt="Build login page",
        model_name="deepseek-coder-v2",
        callback_url="http://orchestrator:8080/api/internal/agent-done",
        harness="aider",
    )

    assert result == container.id
    call_kwargs = mock_client.containers.run.call_args.kwargs
    assert call_kwargs["image"] == "aider-agent:latest"
    assert call_kwargs["detach"] is True
    assert call_kwargs["auto_remove"] is False
    assert call_kwargs["environment"]["TASK_PROMPT"] == "Build login page"


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.docker")
def test_spawn_agent_sets_correct_env(mock_docker: MagicMock) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.run.return_value = _mock_container()

    manager = AgentManager(
        lm_studio_url="http://localhost:9999", github_token="ghp_abc"
    )
    manager.spawn_agent(
        task_id="task-2",
        repo_url="git@github.com:user/repo.git",
        branch="agent/signup",
        base_branch="plan/2026-06-01-auth",
        task_prompt="Build signup flow",
        model_name="qwen3-32b",
        callback_url="http://orchestrator:8080/api/internal/agent-done",
        harness="opencode",
    )

    env = mock_client.containers.run.call_args.kwargs["environment"]
    assert env["REPO_URL"] == "git@github.com:user/repo.git"
    assert env["BRANCH"] == "agent/signup"
    assert env["BASE_BRANCH"] == "plan/2026-06-01-auth"
    assert env["OPENAI_API_BASE"] == "http://localhost:9999/v1"
    assert env["MODEL"] == "qwen3-32b"
    assert env["HARNESS"] == "opencode"
    assert (
        mock_client.containers.run.call_args.kwargs["image"]
        == "opencode-agent:latest"
    )


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.docker")
def test_spawn_agent_defaults_to_aider(mock_docker: MagicMock) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.run.return_value = _mock_container()

    manager = AgentManager(
        lm_studio_url="http://localhost:1234", github_token="ghp_x"
    )
    manager.spawn_agent(
        task_id="t3",
        repo_url="https://github.com/u/r.git",
        branch="agent/x",
        base_branch="main",
        task_prompt="do it",
        model_name="m",
        callback_url="http://o/cb",
    )
    assert (
        mock_client.containers.run.call_args.kwargs["image"]
        == "aider-agent:latest"
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_agent_manager.py -v`
Expected: FAIL — `spawn_agent() got an unexpected keyword argument 'harness'` and `KeyError: 'MODEL'`.

- [ ] **Step 3: Update `spawn_agent`**

In `src/orchestrator/core/agent_manager.py`, add the registry import at the top:

```python
from orchestrator.core.harnesses import REGISTRY, default_harness_id
```

Remove the module-level `AGENT_IMAGE = "aider-agent:latest"` constant (image now comes from the registry). Replace the `spawn_agent` signature and body with:

```python
    def spawn_agent(
        self,
        task_id: str,
        repo_url: str,
        branch: str,
        base_branch: str,
        task_prompt: str,
        model_name: str,
        callback_url: str,
        harness: str | None = None,
    ) -> str:
        harness_id = harness or default_harness_id()
        spec = REGISTRY[harness_id]
        environment = {
            "REPO_URL": repo_url,
            "BRANCH": branch,
            "BASE_BRANCH": base_branch,
            "TASK_PROMPT": task_prompt,
            "OPENAI_API_BASE": f"{self._lm_studio_url}/v1",
            "MODEL": model_name,
            "HARNESS": harness_id,
            "GH_TOKEN": self._github_token,
            "CALLBACK_URL": callback_url,
            "TASK_ID": task_id,
        }
        container = self._client.containers.run(
            image=spec.image,
            name=f"aider-agent-{task_id[:8]}",
            environment=environment,
            detach=True,
            auto_remove=False,
            network_mode="host",
        )
        logger.info(
            "Spawned %s container %s for task %s on branch %s",
            harness_id,
            container.id[:12],
            task_id,
            branch,
        )
        return str(container.id)
```

> The container name keeps the `aider-agent-` prefix so `list_agent_containers`' filter (`{"name": "aider-agent-"}`) still matches all harness containers. Leaving the prefix is intentional — do not change it unless you also update `list_agent_containers`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_agent_manager.py -v`
Expected: PASS (all, including the 3 spawn tests)

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/agent_manager.py tests/test_agent_manager.py
git commit -m "feat: select agent image and env by harness"
```

---

## Task 7: Orchestrator passes the project's harness

**Files:**
- Modify: `src/orchestrator/core/orchestrator.py:96-104`
- Test: `tests/test_orchestrator.py` (append; match existing dispatch-test patterns)

**Depends on:** Task 6

- [ ] **Step 1: Write the failing test**

Read `tests/test_orchestrator.py` first to copy the existing harness/mocks used for `dispatch_pending_tasks` (it mocks `self._agents`, `self._tq`, `self._bus`). Add a test asserting the harness is forwarded. Adapt fixture/setup to match the file; the assertion is the key part:

```python
# tests/test_orchestrator.py  (append, adapting to existing setup helpers)
# ruff: noqa: S101


async def test_dispatch_forwards_project_harness(make_orchestrator) -> None:
    """spawn_agent receives the project's configured harness."""
    orch, mocks = make_orchestrator()  # adapt to the file's existing helper
    project = {
        "id": "p1",
        "repo_url": "https://github.com/u/r.git",
        "default_branch": "main",
        "model_name": "qwen3-32b",
        "harness": "openhands",
    }
    mocks.tq.get_plan.return_value = {"plan_branch_name": "plan/x"}
    mocks.tq.get_dispatchable_tasks.return_value = [
        {"id": "t1", "branch_name": "agent/t1", "title": "T", "description": "D"}
    ]

    await orch.dispatch_pending_tasks("plan1", project)

    kwargs = mocks.agents.spawn_agent.call_args.kwargs
    assert kwargs["harness"] == "openhands"
```

> If `test_orchestrator.py` has no `make_orchestrator` helper, build the orchestrator the same way the existing dispatch tests do (instantiate with mocked `_agents`, `_tq`, `_bus`) and assert `spawn_agent.call_args.kwargs["harness"]`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_orchestrator.py -k harness -v`
Expected: FAIL with `KeyError: 'harness'` in the assertion (harness not passed).

- [ ] **Step 3: Pass `harness` in `dispatch_pending_tasks`**

In `src/orchestrator/core/orchestrator.py`, add `harness=project["harness"]` to the `spawn_agent` call:

```python
            container_id = self._agents.spawn_agent(
                task_id=task["id"],
                repo_url=project["repo_url"],
                branch=task["branch_name"],
                base_branch=plan["plan_branch_name"] or project["default_branch"],
                task_prompt=prompt,
                model_name=project["model_name"],
                callback_url="http://host.docker.internal:8080/api/internal/agent-done",
                harness=project["harness"],
            )
```

> `project` is a full DB row, so `project["harness"]` is always present after Task 2's migration. Existing rows default to `"aider"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_orchestrator.py -k harness -v`
Expected: PASS

- [ ] **Step 5: Run the full orchestrator suite**

Run: `uv run pytest tests/test_orchestrator.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/core/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: dispatch tasks using the project's selected harness"
```

---

## Task 8: Make the Aider entrypoint consume generic `MODEL`

**Files:**
- Modify: `docker/aider-agent/entrypoint.sh`

**Depends on:** Task 6

- [ ] **Step 1: Update the required-vars check and model usage**

In `docker/aider-agent/entrypoint.sh`, replace the `AIDER_MODEL` required-var line:

```bash
: "${AIDER_MODEL:?AIDER_MODEL is required}"
```

with:

```bash
: "${MODEL:?MODEL is required}"
```

Update the startup echo line `echo "Model: ${AIDER_MODEL}"` to:

```bash
echo "Model: openai/${MODEL}"
```

And in the `aider` invocation, replace `--model "${AIDER_MODEL}"` with:

```bash
    --model "openai/${MODEL}" \
```

- [ ] **Step 2: Verify the script is syntactically valid**

Run: `bash -n docker/aider-agent/entrypoint.sh`
Expected: no output (exit 0).

- [ ] **Step 3: Commit**

```bash
git add docker/aider-agent/entrypoint.sh
git commit -m "refactor: aider entrypoint consumes generic MODEL env var"
```

---

## Task 9: OpenCode agent image

**Files:**
- Create: `docker/opencode-agent/Dockerfile`
- Create: `docker/opencode-agent/entrypoint.sh`

**Depends on:** Task 6

- [ ] **Step 1: Create the entrypoint**

```bash
# docker/opencode-agent/entrypoint.sh
#!/bin/bash
set -euo pipefail

: "${REPO_URL:?REPO_URL is required}"
: "${BRANCH:?BRANCH is required}"
: "${BASE_BRANCH:?BASE_BRANCH is required}"
: "${TASK_PROMPT:?TASK_PROMPT is required}"
: "${OPENAI_API_BASE:?OPENAI_API_BASE is required}"
: "${MODEL:?MODEL is required}"
: "${GH_TOKEN:?GH_TOKEN is required}"
: "${CALLBACK_URL:?CALLBACK_URL is required}"
: "${TASK_ID:?TASK_ID is required}"

WORKSPACE="/home/agent/workspace"
STATUS="completed"
PR_URL=""

json_escape() {
    python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'
}

send_callback() {
    local pr_json="null"
    local run_json="null"
    if [ -n "${PR_URL}" ]; then
        pr_json=$(printf "%s" "${PR_URL}" | json_escape)
    fi
    if [ -n "${RUN_ID:-}" ]; then
        run_json=$(printf "%s" "${RUN_ID}" | json_escape)
    fi
    curl -s -X POST "${CALLBACK_URL}" \
        -H "Content-Type: application/json" \
        -d "{\"task_id\":\"${TASK_ID}\",\"run_id\":${run_json},\"status\":\"${STATUS}\",\"pr_url\":${pr_json}}" \
        || echo "WARNING: Failed to send callback"
}

cleanup() {
    local exit_status=$?
    if [ "${exit_status}" -ne 0 ]; then
        STATUS="failed"
    fi
    send_callback
    exit "${exit_status}"
}
trap cleanup EXIT

echo "=== OpenCode agent starting ==="
echo "Repo: ${REPO_URL}"
echo "Branch: ${BRANCH}  Base: ${BASE_BRANCH}  Model: ${MODEL}"

echo "--- Configuring git auth ---"
git config --global credential.helper '!f() { echo "username=x-access-token"; echo "password=${GH_TOKEN}"; }; f'

echo "--- Cloning repository ---"
git clone "${REPO_URL}" "${WORKSPACE}"
cd "${WORKSPACE}"
git config user.email "agent@orchestrator.local"
git config user.name "AI Agent"

echo "--- Creating branch ${BRANCH} from ${BASE_BRANCH} ---"
if git rev-parse --verify "origin/${BASE_BRANCH}" >/dev/null 2>&1; then
    git checkout -b "${BASE_BRANCH}" "origin/${BASE_BRANCH}"
else
    echo "Base branch not on remote; creating from default"
    git checkout -b "${BASE_BRANCH}"
    if ! git push -u origin "${BASE_BRANCH}" 2>/dev/null; then
        echo "Push failed (branch may exist), fetching"
        git fetch origin "${BASE_BRANCH}"
        git reset --hard "origin/${BASE_BRANCH}"
    fi
fi
git checkout -b "${BRANCH}"

echo "--- Writing OpenCode config (OpenAI-compatible local provider) ---"
mkdir -p "${HOME}/.config/opencode"
cat > "${HOME}/.config/opencode/opencode.json" <<EOF
{
  "\$schema": "https://opencode.ai/config.json",
  "provider": {
    "lmstudio": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "LM Studio (local)",
      "options": { "baseURL": "${OPENAI_API_BASE}", "apiKey": "not-needed" },
      "models": { "${MODEL}": { "name": "${MODEL}" } }
    }
  }
}
EOF

echo "--- Running OpenCode (headless) ---"
export OPENAI_API_KEY="${OPENAI_API_KEY:-not-needed}"
opencode run --model "lmstudio/${MODEL}" "${TASK_PROMPT}"

echo "--- Committing changes (OpenCode does not auto-commit) ---"
git add -A
if git diff --cached --quiet; then
    echo "No changes produced by OpenCode"
    STATUS="failed"
    exit 1
fi
git commit -m "agent: ${BRANCH}"

echo "--- Pushing branch ---"
git push -u origin "${BRANCH}"

echo "--- Creating PR ---"
PR_URL=$(gh pr create \
    --title "agent: ${BRANCH}" \
    --body "Automated implementation by OpenCode agent.

Task: ${TASK_PROMPT:0:500}

---
Generated by Praxis (harness: opencode)" \
    --base "${BASE_BRANCH}" \
    --head "${BRANCH}")

echo "PR created: ${PR_URL}"
echo "=== OpenCode agent completed ==="
```

- [ ] **Step 2: Create the Dockerfile**

```dockerfile
# docker/opencode-agent/Dockerfile
FROM node:20-bookworm-slim

# git, gh, curl, python3 for the entrypoint helpers
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git curl ca-certificates gnupg python3 \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Install OpenCode CLI globally
RUN npm install -g opencode-ai

# Non-root agent user; workspace under its home (matches aider-agent pattern)
RUN useradd -m -s /bin/bash agent
USER agent
WORKDIR /home/agent

COPY --chown=agent:agent entrypoint.sh /home/agent/entrypoint.sh
RUN chmod +x /home/agent/entrypoint.sh

ENTRYPOINT ["/home/agent/entrypoint.sh"]
```

> The npm package name for OpenCode is `opencode-ai`. If the build fails to find it, check the current install instructions at https://opencode.ai/docs and adjust the install line only — keep everything else.

- [ ] **Step 3: Verify entrypoint syntax**

Run: `bash -n docker/opencode-agent/entrypoint.sh`
Expected: no output (exit 0).

- [ ] **Step 4: Build the image**

Run: `docker build -t opencode-agent:latest -f docker/opencode-agent/Dockerfile docker/opencode-agent/`
Expected: image builds successfully (`opencode-agent:latest`).

> If Docker is not available on this machine, skip the build and note it in the handoff; the file content is still correct and committable.

- [ ] **Step 5: Commit**

```bash
git add docker/opencode-agent/Dockerfile docker/opencode-agent/entrypoint.sh
git commit -m "feat: add opencode-agent image and entrypoint"
```

---

## Task 10: OpenHands agent image

**Files:**
- Create: `docker/openhands-agent/Dockerfile`
- Create: `docker/openhands-agent/entrypoint.sh`

**Depends on:** Task 6

- [ ] **Step 1: Create the entrypoint**

```bash
# docker/openhands-agent/entrypoint.sh
#!/bin/bash
set -euo pipefail

: "${REPO_URL:?REPO_URL is required}"
: "${BRANCH:?BRANCH is required}"
: "${BASE_BRANCH:?BASE_BRANCH is required}"
: "${TASK_PROMPT:?TASK_PROMPT is required}"
: "${OPENAI_API_BASE:?OPENAI_API_BASE is required}"
: "${MODEL:?MODEL is required}"
: "${GH_TOKEN:?GH_TOKEN is required}"
: "${CALLBACK_URL:?CALLBACK_URL is required}"
: "${TASK_ID:?TASK_ID is required}"

WORKSPACE="/home/agent/workspace"
STATUS="completed"
PR_URL=""

json_escape() {
    python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'
}

send_callback() {
    local pr_json="null"
    local run_json="null"
    if [ -n "${PR_URL}" ]; then
        pr_json=$(printf "%s" "${PR_URL}" | json_escape)
    fi
    if [ -n "${RUN_ID:-}" ]; then
        run_json=$(printf "%s" "${RUN_ID}" | json_escape)
    fi
    curl -s -X POST "${CALLBACK_URL}" \
        -H "Content-Type: application/json" \
        -d "{\"task_id\":\"${TASK_ID}\",\"run_id\":${run_json},\"status\":\"${STATUS}\",\"pr_url\":${pr_json}}" \
        || echo "WARNING: Failed to send callback"
}

cleanup() {
    local exit_status=$?
    if [ "${exit_status}" -ne 0 ]; then
        STATUS="failed"
    fi
    send_callback
    exit "${exit_status}"
}
trap cleanup EXIT

echo "=== OpenHands agent starting ==="
echo "Repo: ${REPO_URL}"
echo "Branch: ${BRANCH}  Base: ${BASE_BRANCH}  Model: ${MODEL}"

echo "--- Configuring git auth ---"
git config --global credential.helper '!f() { echo "username=x-access-token"; echo "password=${GH_TOKEN}"; }; f'

echo "--- Cloning repository ---"
git clone "${REPO_URL}" "${WORKSPACE}"
cd "${WORKSPACE}"
git config user.email "agent@orchestrator.local"
git config user.name "AI Agent"

echo "--- Creating branch ${BRANCH} from ${BASE_BRANCH} ---"
if git rev-parse --verify "origin/${BASE_BRANCH}" >/dev/null 2>&1; then
    git checkout -b "${BASE_BRANCH}" "origin/${BASE_BRANCH}"
else
    echo "Base branch not on remote; creating from default"
    git checkout -b "${BASE_BRANCH}"
    if ! git push -u origin "${BASE_BRANCH}" 2>/dev/null; then
        echo "Push failed (branch may exist), fetching"
        git fetch origin "${BASE_BRANCH}"
        git reset --hard "origin/${BASE_BRANCH}"
    fi
fi
git checkout -b "${BRANCH}"

echo "--- Running OpenHands (headless, local runtime) ---"
# LiteLLM openai-compatible config via env; --override-with-envs applies them.
export LLM_MODEL="openai/${MODEL}"
export LLM_BASE_URL="${OPENAI_API_BASE}"
export LLM_API_KEY="${OPENAI_API_KEY:-not-needed}"
export RUNTIME="local"
python3 -m openhands.core.main -t "${TASK_PROMPT}" --override-with-envs

echo "--- Committing changes (OpenHands may not commit) ---"
git add -A
if git diff --cached --quiet; then
    echo "No changes produced by OpenHands"
    STATUS="failed"
    exit 1
fi
git commit -m "agent: ${BRANCH}" || echo "Nothing to commit (already committed)"

echo "--- Pushing branch ---"
git push -u origin "${BRANCH}"

echo "--- Creating PR ---"
PR_URL=$(gh pr create \
    --title "agent: ${BRANCH}" \
    --body "Automated implementation by OpenHands agent.

Task: ${TASK_PROMPT:0:500}

---
Generated by Praxis (harness: openhands)" \
    --base "${BASE_BRANCH}" \
    --head "${BRANCH}")

echo "PR created: ${PR_URL}"
echo "=== OpenHands agent completed ==="
```

- [ ] **Step 2: Create the Dockerfile**

```dockerfile
# docker/openhands-agent/Dockerfile
FROM python:3.12-slim-bookworm

# git, gh, curl for entrypoint; build basics for any wheels
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git curl ca-certificates gnupg build-essential \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Install OpenHands (headless core).
RUN pip install --no-cache-dir openhands-ai

RUN useradd -m -s /bin/bash agent
USER agent
WORKDIR /home/agent

COPY --chown=agent:agent entrypoint.sh /home/agent/entrypoint.sh
RUN chmod +x /home/agent/entrypoint.sh

ENTRYPOINT ["/home/agent/entrypoint.sh"]
```

> OpenHands pip package is `openhands-ai`; the headless entrypoint is `python -m openhands.core.main`. If the package/module name has changed, verify against https://docs.openhands.dev and adjust the install + run lines only.

- [ ] **Step 3: Verify entrypoint syntax**

Run: `bash -n docker/openhands-agent/entrypoint.sh`
Expected: no output (exit 0).

- [ ] **Step 4: Build the image**

Run: `docker build -t openhands-agent:latest -f docker/openhands-agent/Dockerfile docker/openhands-agent/`
Expected: image builds (`openhands-agent:latest`).

> OpenHands' default container runtime needs Docker socket access (Docker-in-Docker). This entrypoint sets `RUNTIME=local` to run in-process and avoid that. If `RUNTIME=local` is unsupported in the installed version, the operator must mount `/var/run/docker.sock` when spawning — document this in the handoff rather than blocking. If Docker is unavailable here, skip the build and note it.

- [ ] **Step 5: Commit**

```bash
git add docker/openhands-agent/Dockerfile docker/openhands-agent/entrypoint.sh
git commit -m "feat: add openhands-agent image and entrypoint"
```

---

## Task 11: Dashboard — harness dropdown + About panel

**Files:**
- Modify: `web/index.html`
- Test: manual (single-file HTML; no JS test harness in repo)

**Depends on:** Task 4, Task 5

- [ ] **Step 1: Read the project form and API helper**

Open `web/index.html` and locate (a) the project-create form where `model_name` is entered, (b) the JS `fetch` helper used for authenticated API calls (it attaches the bearer token). Mirror those patterns exactly — do not introduce a new fetch style.

- [ ] **Step 2: Add a harness `<select>` to the project form**

Next to the existing `model_name` input, add:

```html
<label for="project-harness">Harness</label>
<select id="project-harness" name="harness">
  <!-- options populated from /api/harnesses -->
</select>
<button type="button" id="harness-about-btn">About harnesses</button>
```

- [ ] **Step 3: Populate the dropdown and include harness on submit**

In the form's init code, fetch the catalog and fill the select (recommended option labeled). Add a module-level cache `let HARNESSES = [];`:

```javascript
async function loadHarnesses() {
  HARNESSES = await apiFetch('/api/harnesses');   // use the existing helper name
  const sel = document.getElementById('project-harness');
  sel.innerHTML = '';
  for (const h of HARNESSES) {
    const opt = document.createElement('option');
    opt.value = h.id;
    opt.textContent = h.recommended
      ? `${h.display_name} (recommended)`
      : h.display_name;
    sel.appendChild(opt);
  }
}
```

Call `loadHarnesses()` wherever the project form is initialized. In the create-project submit handler, add `harness` to the JSON body:

```javascript
harness: document.getElementById('project-harness').value,
```

- [ ] **Step 4: Render the About panel**

Add a modal/panel container and a renderer driven by the cached `HARNESSES`:

```html
<div id="harness-about" class="modal" hidden>
  <div class="modal-body" id="harness-about-body"></div>
  <button type="button" id="harness-about-close">Close</button>
</div>
```

```javascript
function renderHarnessAbout() {
  const body = document.getElementById('harness-about-body');
  body.innerHTML = HARNESSES.map(h => `
    <section class="harness-card">
      <h3>${h.display_name}${h.recommended ? ' ⭐ recommended' : ''}
        <small>(${h.maturity})</small></h3>
      <p>${h.description}</p>
      <p><strong>What's unique:</strong> ${h.uniqueness}</p>
      <p><strong>When to pick:</strong> ${h.when_to_pick}</p>
      <div class="cols">
        <div><strong>Pros</strong><ul>${
          h.pros.map(p => `<li>${p}</li>`).join('')
        }</ul></div>
        <div><strong>Cons</strong><ul>${
          h.cons.map(c => `<li>${c}</li>`).join('')
        }</ul></div>
      </div>
    </section>`).join('');
  document.getElementById('harness-about').hidden = false;
}
document.getElementById('harness-about-btn')
  .addEventListener('click', renderHarnessAbout);
document.getElementById('harness-about-close')
  .addEventListener('click', () => {
    document.getElementById('harness-about').hidden = true;
  });
```

> Match the existing CSS class naming/theme in `index.html` (dark/light). Reuse existing modal styles if the dashboard already has a modal; only add `.harness-card`/`.cols` rules if needed.

- [ ] **Step 5: Manual verification**

Start the server, open the dashboard, confirm: the harness dropdown lists all three (Aider marked recommended), "About harnesses" shows cards with pros/cons/when-to-pick, and creating a project with OpenCode selected persists (re-open / GET the project shows `harness: "opencode"`).

```bash
uv run uvicorn orchestrator.main:app --host 127.0.0.1 --port 8080 --reload
```

Then browse to http://127.0.0.1:8080. (Token must match `AUTH_TOKEN` in `.env`.)

- [ ] **Step 6: Commit**

```bash
git add web/index.html
git commit -m "feat: harness dropdown and About panel in dashboard"
```

---

## Task 12: Documentation update

**Files:**
- Modify: `CLAUDE.md`

**Depends on:** Task 9, Task 10, Task 11

- [ ] **Step 1: Update the project structure + gotchas**

In `CLAUDE.md`:

- Under the `docker/` section of the Project Structure tree, add `opencode-agent/` and `openhands-agent/` alongside `aider-agent/`.
- Add a new "Harnesses" note under Key Design Decisions:

```markdown
- **Pluggable harnesses** — `core/harnesses.py` is the registry (image + About
  content) for Aider, OpenCode, OpenHands. Projects pick one via the `harness`
  column (default `aider`). `AgentManager.spawn_agent` selects the image and
  sets a harness-agnostic env contract (`HARNESS`, `MODEL`, `OPENAI_API_BASE`,
  + repo/branch/callback vars). Each `docker/<harness>-agent/` image honors the
  same entrypoint contract.
```

- Add to Gotchas:

```markdown
- **Harness images are standalone** — build each directly, none are in
  docker-compose:
  `docker build -t opencode-agent:latest -f docker/opencode-agent/Dockerfile docker/opencode-agent/`
  (same for `openhands-agent`).
- **OpenCode/OpenHands don't auto-commit** — unlike Aider, their entrypoints run
  `git add -A && git commit` after the agent. A run that produces no changes is
  marked `failed`.
- **OpenHands needs a sandbox runtime** — the image uses `RUNTIME=local` to avoid
  Docker-in-Docker. If unsupported, mount `/var/run/docker.sock` when spawning.
- **Generic MODEL env var** — all harness entrypoints consume `MODEL` (raw model
  name); each adds its own provider prefix (Aider/OpenHands use `openai/`,
  OpenCode uses an `lmstudio/` config provider).
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document multi-harness support"
```

---

## Task 13: Full verification

**Files:** none (verification only)

**Depends on:** Task 1–12

- [ ] **Step 1: Run the full test suite with coverage**

Run: `uv run pytest --cov=orchestrator --cov-report=term-missing -v`
Expected: all tests PASS (prior 156 + new harness/schema/db/api tests), coverage ≥ 80%.

- [ ] **Step 2: Lint + format check**

Run:
```bash
uv run ruff format --check src/ tests/
uv run ruff check src/ tests/
```
Expected: both clean (run `uv run ruff format src/ tests/` and `uv run ruff check --fix src/ tests/` to fix, then re-check).

- [ ] **Step 3: Type check**

Run: `uv run mypy src/orchestrator/ --ignore-missing-imports`
Expected: no errors (matches the existing "mypy clean" baseline).

- [ ] **Step 4: Final commit (only if fixes were applied)**

```bash
git add -A
git commit -m "chore: lint/format/type fixes for multi-harness support"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1 (registry), Task 2 (DB column) — no dependencies, run in parallel.
- **Wave 2:** Task 3 (schemas — needs Task 1), Task 5 (`/api/harnesses` — needs Task 1), Task 6 (AgentManager — needs Task 1).
- **Wave 3:** Task 4 (project create — needs Task 2, Task 3), Task 7 (orchestrator dispatch — needs Task 6), Task 8 (aider entrypoint — needs Task 6), Task 9 (opencode image — needs Task 6), Task 10 (openhands image — needs Task 6).
- **Wave 4:** Task 11 (dashboard — needs Task 4, Task 5).
- **Wave 5:** Task 12 (docs — needs Task 9, Task 10, Task 11).
- **Wave 6:** Task 13 (full verification — needs all).

---

## Notes & Risks

- **External CLI drift:** OpenCode (`opencode-ai` npm, `opencode run`) and OpenHands (`openhands-ai` pip, `python -m openhands.core.main`, `--override-with-envs`) invocation details were captured June 2026. If a Docker build or run fails on the install/run lines, verify against the current docs (opencode.ai/docs, docs.openhands.dev) and adjust **only** those lines — the env contract, git flow, and callback must stay identical across harnesses.
- **Bake-off is intentionally out of scope:** per the design decision, the comparison is analytical (the registry content). No empirical benchmark harness is built here.
- **Backward compatibility:** existing projects and DBs get `harness = 'aider'` automatically; the `spawn_agent` `harness` param defaults to aider; all current behavior is preserved.
- **Container name prefix** stays `aider-agent-<id>` for all harnesses so `list_agent_containers` keeps working. If you ever rename it, update the filter in `agent_manager.py` too.
