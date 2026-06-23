# Praxis MCP-First Control Surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a stdio MCP server that lets an MCP client (e.g. Claude Code) dispatch implementation work to a non-Anthropic model running inside Praxis, by forwarding to the existing FastAPI REST API.

**Architecture:** A standalone `src/mcp_server/` package (Python MCP / FastMCP SDK) is launched by the MCP client as a stdio subprocess. Each tool forwards over HTTP (with a Bearer token read from env) to the Praxis REST API. The engine is unchanged except for **one thin new REST route** — `POST /api/dispatch` — that injects a single-task plan (Praxis has no direct single-task creation route today; tasks are only created via plan activation). The dashboard and CLI remain untouched, co-equal REST clients.

**Tech Stack:** Python 3.11, FastAPI, `mcp` (FastMCP) SDK, httpx, pytest + pytest-asyncio (`asyncio_mode = "auto"`), ruff, mypy.

---

## Background / Context for the Implementer

You are working in `C:\working-space\praxis` (Windows, PowerShell). This is an AI agent
orchestrator. Read these before starting:

- **Design spec:** `docs/superpowers/specs/2026-06-23-praxis-mcp-control-surface-design.md`
- **Project guide:** `CLAUDE.md` (gotchas), `CLAUDE.local.md` (testing/running)

### Key facts you need

- **No ORM** — raw SQL via `aiosqlite`. Source of truth for task creation is
  `TaskQueue.activate_plan(plan_id, opus_plan, plan_branch_name)` in
  `src/orchestrator/core/task_queue.py`. It reads `opus_plan["tasks"]`, where each task is
  `{"title": str, "description": str, "slug": str, "depends_on": list[str]}` and inserts a
  `tasks` row with `branch_name = f"agent/{slug}"`. It does **not** return the new task id —
  you fetch it afterward via `TaskQueue.get_tasks_for_plan(plan_id)`.
- **Projects are keyed loosely by `repo_url`** but there is no unique constraint. Columns
  include `id, user_id, name, repo_url, default_branch, approval_gate, confidence_threshold,
  max_retries, max_improvement_cycles, lm_studio_url, model_name, harness, agent_model,
  agent_model_effort`. A `default` user is auto-seeded at startup; in tests you seed one via
  the `seed_user(db)` helper in `tests/conftest.py`.
- **The orchestration loop auto-dispatches** pending tasks of `ACTIVE` plans. `activate_plan`
  sets the plan to `ACTIVE`, so once `POST /api/dispatch` activates a single-task plan, the
  loop will spawn the agent on the next pass. No extra trigger needed.
- **REST endpoints already present** (all under `/api`, all require `Authorization: Bearer`):
  - `GET /api/tasks/{task_id}` → `{"task": {...}, "runs": [{...}]}`. The task row has
    `status, pr_url, review_feedback, ...`; each run row has a `logs` text column.
  - `POST /api/tasks/{task_id}/stop` → `{"stopped": int}` (kills running containers, marks
    task FAILED).
  - `GET /api/status` → includes `providers` (list of `{name, cli_available, authenticated,
    login_hint}`), `subagent_model`, `lm_studio_url`.
  - `GET /api/lm-models` → `{"models": [str], "lm_studio_url": str, "connected": bool}`.
  - `GET /api/tasks/{task_id}/logs` exists but is an **SSE stream** — do NOT use it for the
    MCP tool. Derive logs from the `runs[].logs` returned by `GET /api/tasks/{task_id}`.
- **Routers** are registered in `src/orchestrator/main.py` with `app.include_router(...,
  prefix="/api")` near the bottom (after the lifespan, imports at lines ~163-188).
- **Test fixtures** (`tests/conftest.py`): `client` (httpx `AsyncClient` over `ASGITransport`,
  exposes `.app`), `auth_headers` → `{"Authorization": "Bearer test-auth"}`, `db`,
  `seed_user(db)`. Tests are async, `asyncio_mode = "auto"` (no `@pytest.mark.asyncio` needed).
- **Commands:** run tests `uv run pytest <path> -v`; lint `uv run ruff check src/ tests/`;
  format `uv run ruff format src/ tests/`; types `uv run mypy src/ --ignore-missing-imports`.

### Scope notes (read carefully — prevents over-building)

- **`review` opt-out is DESCOPED from v1.** The spec makes review a `dispatch_task`
  parameter defaulting to `true`. Enforcing `review=false` requires orchestrator-loop
  changes that are out of scope here. v1 **always** runs Praxis's review (the default), so
  the `review` parameter is omitted entirely from the tool and route. Note this as a
  follow-up in the docs task. Do not add a placeholder param.
- **`submit_spec` / `poll_plan` are DEFERRED** (later phase). Not in this plan.
- **Worker scope is today's path** — Aider harness + LM Studio model. No `spawn_agent`
  changes.
- **`dashboard_url`** is the Praxis base URL with a trailing slash (e.g.
  `http://localhost:8080/`). The single-file dashboard has no per-task deep-link route yet;
  base URL is the honest v1 value. Do not invent a hash route.

---

## File Structure

**Create:**
- `src/orchestrator/api/dispatch.py` — the thin `POST /api/dispatch` route (single-task plan injection).
- `src/mcp_server/__init__.py` — package marker.
- `src/mcp_server/client.py` — `PraxisClient` httpx wrapper: env config, Bearer, error translation.
- `src/mcp_server/server.py` — FastMCP server; 5 tools delegating to testable async functions.
- `src/mcp_server/__main__.py` — `python -m mcp_server` entry point.
- `tests/test_api_dispatch.py` — tests for the new REST route.
- `tests/test_mcp_client.py` — unit tests for `PraxisClient` (httpx `MockTransport`).
- `tests/test_mcp_server.py` — unit tests for the 5 tool functions (mocked client).

**Modify:**
- `src/orchestrator/main.py` — register the dispatch router.
- `src/orchestrator/models/schemas.py` — add `DispatchRequest` / `DispatchResponse`.
- `pyproject.toml` — add `mcp` dep + `praxis-mcp` console script.
- `README.md` — MCP usage section + CC config example (docs task).

---

### Task 1: Add MCP dependency and console-script entry point

**Files:**
- Modify: `pyproject.toml`

**Depends on:** None

- [ ] **Step 1: Add the `mcp` dependency**

In `pyproject.toml`, add `"mcp>=1.2"` to the `[project].dependencies` list (after
`"sse-starlette>=2.0",`):

```toml
    "sse-starlette>=2.0",
    "mcp>=1.2",
```

- [ ] **Step 2: Add the console script**

In `[project.scripts]`, add the `praxis-mcp` entry below `orchestrator-cli`:

```toml
[project.scripts]
orchestrator-cli = "cli.main:app"
praxis-mcp = "mcp_server.__main__:main"
```

- [ ] **Step 3: Sync the environment**

Run: `uv sync --extra dev`
Expected: completes without error; `mcp` is installed.

- [ ] **Step 4: Verify the SDK imports**

Run: `uv run python -c "from mcp.server.fastmcp import FastMCP; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml
git commit -m "build: add mcp SDK dependency and praxis-mcp entry point"
```

---

### Task 2: Add `DispatchRequest` / `DispatchResponse` schemas

**Files:**
- Modify: `src/orchestrator/models/schemas.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_dispatch.py` with just this schema import test for now:

```python
"""Tests for the /api/dispatch route and its schemas."""

from __future__ import annotations


def test_dispatch_schemas_importable() -> None:
    from orchestrator.models.schemas import DispatchRequest, DispatchResponse

    req = DispatchRequest(
        repo_url="https://github.com/u/r",
        instructions="add input validation",
        model="qwen3-32b",
    )
    assert req.harness is None
    assert req.branch is None

    resp = DispatchResponse(
        task_id="t1",
        plan_id="p1",
        project_id="pr1",
        status="queued",
        dashboard_url="http://localhost:8080/",
    )
    assert resp.status == "queued"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_dispatch.py::test_dispatch_schemas_importable -v`
Expected: FAIL with `ImportError: cannot import name 'DispatchRequest'`.

- [ ] **Step 3: Add the schemas**

Append to `src/orchestrator/models/schemas.py` (end of file):

```python
class DispatchRequest(BaseModel):
    """Request payload for MCP single-task dispatch."""

    repo_url: str
    instructions: str
    model: str
    harness: str | None = None
    branch: str | None = None
    name: str | None = None


class DispatchResponse(BaseModel):
    """Response for a dispatched single-task plan."""

    task_id: str
    plan_id: str
    project_id: str
    status: str
    dashboard_url: str
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_api_dispatch.py::test_dispatch_schemas_importable -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/models/schemas.py tests/test_api_dispatch.py
git commit -m "feat: add DispatchRequest/DispatchResponse schemas"
```

---

### Task 3: Implement the `POST /api/dispatch` route

**Files:**
- Create: `src/orchestrator/api/dispatch.py`
- Modify: `src/orchestrator/main.py`
- Test: `tests/test_api_dispatch.py`

**Depends on:** Task 2

- [ ] **Step 1: Write the failing test**

Add to `tests/test_api_dispatch.py`:

```python
import pytest
from httpx import AsyncClient

from orchestrator.database import Database
from tests.conftest import seed_user


@pytest.fixture
async def seeded_user(db: Database) -> str:
    return await seed_user(db)


async def test_dispatch_creates_project_plan_and_task(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str
) -> None:
    body = {
        "repo_url": "https://github.com/u/repo",
        "instructions": "Add a /health endpoint",
        "model": "qwen3-32b",
    }
    resp = await client.post("/api/dispatch", json=body, headers=auth_headers)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["task_id"]
    assert data["plan_id"]
    assert data["project_id"]
    assert data["status"] == "queued"
    assert data["dashboard_url"].startswith("http")

    # The task exists and is dispatchable.
    task_resp = await client.get(
        f"/api/tasks/{data['task_id']}", headers=auth_headers
    )
    assert task_resp.status_code == 200
    assert task_resp.json()["task"]["status"] == "pending"


async def test_dispatch_reuses_project_and_updates_model(
    client: AsyncClient, auth_headers: dict[str, str], seeded_user: str
) -> None:
    body = {
        "repo_url": "https://github.com/u/repo",
        "instructions": "task one",
        "model": "qwen3-32b",
    }
    first = await client.post("/api/dispatch", json=body, headers=auth_headers)
    project_id = first.json()["project_id"]

    body2 = {**body, "instructions": "task two", "model": "deepseek-coder-v2"}
    second = await client.post("/api/dispatch", json=body2, headers=auth_headers)
    assert second.json()["project_id"] == project_id  # reused, not duplicated

    proj = await client.get(
        f"/api/projects/{project_id}", headers=auth_headers
    )
    assert proj.json()["model_name"] == "deepseek-coder-v2"  # updated to latest


async def test_dispatch_requires_auth(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/dispatch",
        json={"repo_url": "x", "instructions": "y", "model": "z"},
    )
    assert resp.status_code == 401
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_api_dispatch.py -v`
Expected: the three new tests FAIL with 404 (route not found).

- [ ] **Step 3: Implement the route**

Create `src/orchestrator/api/dispatch.py`:

```python
"""MCP single-task dispatch endpoint.

Praxis has no direct single-task creation route; tasks are only created via
plan activation. This route injects a one-task plan so an MCP client can
dispatch implementation work without owning the planning step. The plan is
activated immediately (status ACTIVE), so the orchestration loop picks up the
task on its next pass.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from orchestrator.api.auth import verify_token
from orchestrator.core.harnesses import default_harness_id
from orchestrator.models.schemas import DispatchRequest, DispatchResponse


router = APIRouter(tags=["dispatch"], dependencies=[Depends(verify_token)])


def _slugify(text: str) -> str:
    """Build a short branch-safe slug from free text plus a uniqueness suffix."""
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "task"
    return f"{base}-{uuid.uuid4().hex[:6]}"


@router.post(
    "/dispatch",
    status_code=status.HTTP_201_CREATED,
    response_model=DispatchResponse,
)
async def dispatch_task(request: Request, body: DispatchRequest) -> dict[str, Any]:
    """Create-or-reuse a project, then activate a single-task plan."""

    db = request.app.state.db
    queue = request.app.state.task_queue
    settings = request.app.state.settings

    user = await db.fetch_one("SELECT id FROM users LIMIT 1")
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No user found. Seed a user first.",
        )

    harness = body.harness or default_harness_id()
    project = await db.fetch_one(
        "SELECT * FROM projects WHERE repo_url = ? ORDER BY rowid LIMIT 1",
        (body.repo_url,),
    )
    if project is None:
        project_id = str(uuid.uuid4())
        await db.execute(
            """INSERT INTO projects
               (id, user_id, name, repo_url, default_branch, approval_gate,
                model_name, harness)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                project_id,
                user["id"],
                body.name or body.repo_url.rstrip("/").split("/")[-1] or "mcp-project",
                body.repo_url,
                "main",
                False,  # MCP dispatch bypasses the approval gate: brain already planned
                body.model,
                harness,
            ),
        )
    else:
        project_id = project["id"]
        # Keep the worker model/harness in sync with this dispatch request.
        await db.execute(
            "UPDATE projects SET model_name = ?, harness = ? WHERE id = ?",
            (body.model, harness, project_id),
        )

    plan_id = await queue.create_plan(project_id, source="mcp")
    slug = _slugify(body.instructions)
    opus_plan = {
        "tasks": [
            {
                "title": body.instructions[:80],
                "description": body.instructions,
                "slug": slug,
                "depends_on": [],
            }
        ]
    }
    branch_name = body.branch or f"plan/mcp-{slug}"
    await queue.activate_plan(plan_id, opus_plan, branch_name)

    tasks = await queue.get_tasks_for_plan(plan_id)
    if not tasks:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Task activation produced no task",
        )

    base_url = f"http://localhost:{settings.port}/"
    return {
        "task_id": tasks[0]["id"],
        "plan_id": plan_id,
        "project_id": project_id,
        "status": "queued",
        "dashboard_url": base_url,
    }
```

> NOTE: `settings.port` is the configured server port. If `Settings` has no `port`
> attribute, use `getattr(settings, "port", 8080)`. Verify by grepping
> `src/orchestrator/config.py` for `port`; adjust the line accordingly.

- [ ] **Step 4: Register the router**

In `src/orchestrator/main.py`, add the import alongside the other `from orchestrator.api...`
imports (near line 163-174):

```python
from orchestrator.api.dispatch import router as dispatch_router  # noqa: E402
```

And register it with the other `app.include_router(..., prefix="/api")` calls (near line
180-188):

```python
app.include_router(dispatch_router, prefix="/api")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_api_dispatch.py -v`
Expected: all tests PASS.

- [ ] **Step 6: Lint, format, type-check**

Run:
```bash
uv run ruff format src/orchestrator/api/dispatch.py
uv run ruff check --fix src/orchestrator/api/dispatch.py
uv run mypy src/orchestrator/api/dispatch.py --ignore-missing-imports
```
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator/api/dispatch.py src/orchestrator/main.py tests/test_api_dispatch.py
git commit -m "feat: add POST /api/dispatch single-task injection route"
```

---

### Task 4: Implement `PraxisClient` (httpx wrapper + error translation)

**Files:**
- Create: `src/mcp_server/__init__.py`
- Create: `src/mcp_server/client.py`
- Test: `tests/test_mcp_client.py`

**Depends on:** None

- [ ] **Step 1: Create the package marker**

Create `src/mcp_server/__init__.py`:

```python
"""Praxis MCP server package."""
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_mcp_client.py`:

```python
"""Unit tests for PraxisClient using httpx MockTransport."""

from __future__ import annotations

import httpx
import pytest

from mcp_server.client import PraxisClient, PraxisClientError


def _client(handler: object, token: str = "tok") -> PraxisClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return PraxisClient(
        base_url="http://praxis.test",
        token=token,
        transport=transport,
    )


async def test_get_attaches_bearer_and_returns_json() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"ok": True})

    client = _client(handler)
    data = await client.get("/api/status")
    assert data == {"ok": True}
    assert seen["auth"] == "Bearer tok"


async def test_auth_error_maps_401() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "bad token"})

    client = _client(handler)
    with pytest.raises(PraxisClientError) as exc:
        await client.get("/api/status")
    assert exc.value.code == "auth_error"


async def test_not_found_maps_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "nope"})

    client = _client(handler)
    with pytest.raises(PraxisClientError) as exc:
        await client.get("/api/tasks/x")
    assert exc.value.code == "not_found"


async def test_validation_error_maps_422() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "bad"})

    client = _client(handler)
    with pytest.raises(PraxisClientError) as exc:
        await client.post("/api/dispatch", {"x": 1})
    assert exc.value.code == "validation_error"


async def test_connection_error_maps() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = _client(handler)
    with pytest.raises(PraxisClientError) as exc:
        await client.get("/api/status")
    assert exc.value.code == "connection_error"


def test_from_env_reads_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRAXIS_BASE_URL", "http://h:9000")
    monkeypatch.setenv("PRAXIS_AUTH_TOKEN", "secret")
    client = PraxisClient.from_env()
    assert client.base_url == "http://h:9000"
    assert client.token == "secret"


def test_from_env_missing_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRAXIS_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("PRAXIS_BASE_URL", "http://h:9000")
    with pytest.raises(PraxisClientError) as exc:
        PraxisClient.from_env()
    assert exc.value.code == "config_error"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mcp_server.client'`.

- [ ] **Step 4: Implement the client**

Create `src/mcp_server/client.py`:

```python
"""HTTP client wrapper that forwards MCP tool calls to the Praxis REST API.

Translates transport and HTTP-status failures into a single structured
``PraxisClientError`` so MCP tools can return actionable messages to the brain
instead of opaque stack traces.
"""

from __future__ import annotations

import os
from typing import Any

import httpx


class PraxisClientError(Exception):
    """A structured client error carrying a machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


_STATUS_CODES: dict[int, str] = {
    401: "auth_error",
    403: "auth_error",
    404: "not_found",
    422: "validation_error",
}


class PraxisClient:
    """Thin async HTTP client for the Praxis REST API with Bearer auth."""

    def __init__(
        self,
        base_url: str,
        token: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._transport = transport

    @classmethod
    def from_env(cls) -> PraxisClient:
        """Build a client from PRAXIS_BASE_URL + PRAXIS_AUTH_TOKEN env vars."""
        base_url = os.environ.get("PRAXIS_BASE_URL", "http://localhost:8080")
        token = os.environ.get("PRAXIS_AUTH_TOKEN")
        if not token:
            raise PraxisClientError(
                "config_error",
                "PRAXIS_AUTH_TOKEN is not set; the MCP server cannot authenticate.",
            )
        return cls(base_url=base_url, token=token)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def _request(
        self, method: str, path: str, json: dict[str, Any] | None = None
    ) -> Any:
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=30.0
            ) as client:
                response = await client.request(
                    method, url, headers=self._headers(), json=json
                )
        except httpx.ConnectError as exc:
            raise PraxisClientError(
                "connection_error",
                f"Cannot reach Praxis at {self.base_url}. Is the server running?",
            ) from exc
        except httpx.HTTPError as exc:
            raise PraxisClientError(
                "connection_error", f"HTTP transport error: {exc}"
            ) from exc

        if response.status_code >= 400:
            code = _STATUS_CODES.get(response.status_code, "request_error")
            detail = _safe_detail(response)
            raise PraxisClientError(
                code, f"Praxis returned {response.status_code}: {detail}"
            )
        if not response.content:
            return None
        return response.json()

    async def get(self, path: str) -> Any:
        return await self._request("GET", path)

    async def post(self, path: str, json: dict[str, Any] | None = None) -> Any:
        return await self._request("POST", path, json=json)


def _safe_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])
    return str(body)[:200]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_client.py -v`
Expected: all PASS.

- [ ] **Step 6: Lint, format, type-check**

Run:
```bash
uv run ruff format src/mcp_server/ tests/test_mcp_client.py
uv run ruff check --fix src/mcp_server/ tests/test_mcp_client.py
uv run mypy src/mcp_server/client.py --ignore-missing-imports
```
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add src/mcp_server/__init__.py src/mcp_server/client.py tests/test_mcp_client.py
git commit -m "feat: add PraxisClient REST wrapper with error translation"
```

---

### Task 5: Implement the five MCP tool functions

**Files:**
- Create: `src/mcp_server/server.py`
- Test: `tests/test_mcp_server.py`

**Depends on:** Task 4

The five tools delegate to plain module-level async functions that take a `PraxisClient`,
so they are unit-testable without a stdio transport. FastMCP wrappers are added in Task 6.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mcp_server.py`:

```python
"""Unit tests for the MCP tool functions with a fake PraxisClient."""

from __future__ import annotations

from typing import Any

import pytest

from mcp_server import server
from mcp_server.client import PraxisClientError


class FakeClient:
    def __init__(self, responses: dict[tuple[str, str], Any]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str, Any]] = []

    async def get(self, path: str) -> Any:
        self.calls.append(("GET", path, None))
        return self._responses[("GET", path)]

    async def post(self, path: str, json: Any = None) -> Any:
        self.calls.append(("POST", path, json))
        return self._responses[("POST", path)]


async def test_dispatch_task_forwards_and_returns_handle() -> None:
    client = FakeClient(
        {
            ("POST", "/api/dispatch"): {
                "task_id": "t1",
                "plan_id": "p1",
                "project_id": "pr1",
                "status": "queued",
                "dashboard_url": "http://localhost:8080/",
            }
        }
    )
    result = await server.dispatch_task_impl(
        client,
        repo_url="https://github.com/u/r",
        instructions="add X",
        model="qwen3-32b",
    )
    assert result["task_id"] == "t1"
    assert result["dashboard_url"].startswith("http")
    method, path, body = client.calls[0]
    assert (method, path) == ("POST", "/api/dispatch")
    assert body["model"] == "qwen3-32b"


async def test_poll_task_maps_status_and_pr() -> None:
    client = FakeClient(
        {
            ("GET", "/api/tasks/t1"): {
                "task": {
                    "status": "passed",
                    "pr_url": "https://github.com/u/r/pull/9",
                    "review_feedback": "looks good",
                },
                "runs": [],
            }
        }
    )
    result = await server.poll_task_impl(client, task_id="t1")
    assert result["status"] == "passed"
    assert result["pr_url"].endswith("/pull/9")
    assert result["review"] == "looks good"
    assert "dashboard_url" in result


async def test_list_providers_combines_status_and_models() -> None:
    client = FakeClient(
        {
            ("GET", "/api/status"): {
                "providers": [
                    {"name": "claude", "cli_available": True, "authenticated": True},
                    {"name": "codex", "cli_available": True, "authenticated": False},
                ],
                "lm_studio_url": "http://localhost:1234",
            },
            ("GET", "/api/lm-models"): {
                "models": ["qwen3-32b", "deepseek-coder-v2"],
                "connected": True,
            },
        }
    )
    result = await server.list_providers_impl(client)
    assert result["worker_models"] == ["qwen3-32b", "deepseek-coder-v2"]
    assert result["lm_studio_connected"] is True
    assert any(p["name"] == "claude" for p in result["brain_providers"])


async def test_get_task_logs_concatenates_runs() -> None:
    client = FakeClient(
        {
            ("GET", "/api/tasks/t1"): {
                "task": {"status": "in_progress"},
                "runs": [
                    {"id": "r1", "logs": "line one\n"},
                    {"id": "r2", "logs": "line two\n"},
                ],
            }
        }
    )
    result = await server.get_task_logs_impl(client, task_id="t1")
    assert "line one" in result["logs"]
    assert "line two" in result["logs"]


async def test_cancel_task_forwards_stop() -> None:
    client = FakeClient({("POST", "/api/tasks/t1/stop"): {"stopped": 1}})
    result = await server.cancel_task_impl(client, task_id="t1")
    assert result["stopped"] == 1
    assert result["status"] == "cancelled"


async def test_tool_error_is_returned_not_raised() -> None:
    class FailClient:
        async def get(self, path: str) -> Any:
            raise PraxisClientError("connection_error", "down")

        async def post(self, path: str, json: Any = None) -> Any:
            raise PraxisClientError("connection_error", "down")

    result = await server.poll_task_impl(FailClient(), task_id="t1")  # type: ignore[arg-type]
    assert result["error"] == "connection_error"
    assert "down" in result["message"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp_server.py -v`
Expected: FAIL with `AttributeError`/`ImportError` (functions not defined).

- [ ] **Step 3: Implement the tool functions**

Create `src/mcp_server/server.py`:

```python
"""Praxis MCP server: tool implementations + FastMCP registration.

Each ``*_impl`` function takes a PraxisClient and is independently testable.
The FastMCP tool wrappers (registered at module import) build a client from env
and delegate. Tools never raise to the MCP client; client errors are caught and
returned as ``{"error": code, "message": ...}`` so the brain can react.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from mcp_server.client import PraxisClient, PraxisClientError


def _error(exc: PraxisClientError) -> dict[str, Any]:
    return {"error": exc.code, "message": exc.message}


async def dispatch_task_impl(
    client: Any,
    repo_url: str,
    instructions: str,
    model: str,
    harness: str | None = None,
    branch: str | None = None,
) -> dict[str, Any]:
    """Dispatch a single implementation task to a non-Anthropic worker model."""
    payload: dict[str, Any] = {
        "repo_url": repo_url,
        "instructions": instructions,
        "model": model,
    }
    if harness is not None:
        payload["harness"] = harness
    if branch is not None:
        payload["branch"] = branch
    try:
        return await client.post("/api/dispatch", payload)
    except PraxisClientError as exc:
        return _error(exc)


async def poll_task_impl(client: Any, task_id: str) -> dict[str, Any]:
    """Return the current status, PR URL, and review of a dispatched task."""
    try:
        data = await client.get(f"/api/tasks/{task_id}")
    except PraxisClientError as exc:
        return _error(exc)
    task = data.get("task", {})
    return {
        "status": task.get("status"),
        "pr_url": task.get("pr_url"),
        "review": task.get("review_feedback"),
        "dashboard_url": _dashboard_url(client),
    }


async def list_providers_impl(client: Any) -> dict[str, Any]:
    """List brain providers and the worker models available to dispatch to."""
    try:
        status_data = await client.get("/api/status")
        models_data = await client.get("/api/lm-models")
    except PraxisClientError as exc:
        return _error(exc)
    return {
        "brain_providers": status_data.get("providers", []),
        "worker_models": models_data.get("models", []),
        "lm_studio_url": status_data.get("lm_studio_url"),
        "lm_studio_connected": models_data.get("connected", False),
    }


async def get_task_logs_impl(client: Any, task_id: str) -> dict[str, Any]:
    """Return concatenated agent-run logs for a task (inline failure triage)."""
    try:
        data = await client.get(f"/api/tasks/{task_id}")
    except PraxisClientError as exc:
        return _error(exc)
    runs = data.get("runs", [])
    logs = "".join(str(run.get("logs") or "") for run in runs)
    return {"task_id": task_id, "logs": logs}


async def cancel_task_impl(client: Any, task_id: str) -> dict[str, Any]:
    """Stop a running task's agent containers and mark it failed."""
    try:
        data = await client.post(f"/api/tasks/{task_id}/stop")
    except PraxisClientError as exc:
        return _error(exc)
    return {"status": "cancelled", "stopped": data.get("stopped", 0)}


def _dashboard_url(client: Any) -> str:
    base = getattr(client, "base_url", "").rstrip("/")
    return f"{base}/" if base else ""


# --- FastMCP registration -------------------------------------------------

mcp = FastMCP("praxis")


@mcp.tool()
async def dispatch_task(
    repo_url: str,
    instructions: str,
    model: str,
    harness: str | None = None,
    branch: str | None = None,
) -> dict[str, Any]:
    """Dispatch an implementation task to a non-Anthropic worker model inside Praxis.

    Returns a handle: {task_id, plan_id, project_id, status, dashboard_url}.
    Poll with poll_task. Praxis always runs its own review before merge.
    """
    return await dispatch_task_impl(
        PraxisClient.from_env(),
        repo_url=repo_url,
        instructions=instructions,
        model=model,
        harness=harness,
        branch=branch,
    )


@mcp.tool()
async def poll_task(task_id: str) -> dict[str, Any]:
    """Get the status, PR URL, and review of a dispatched task."""
    return await poll_task_impl(PraxisClient.from_env(), task_id=task_id)


@mcp.tool()
async def list_providers() -> dict[str, Any]:
    """List brain providers and the worker models available to dispatch to."""
    return await list_providers_impl(PraxisClient.from_env())


@mcp.tool()
async def get_task_logs(task_id: str) -> dict[str, Any]:
    """Return the agent-run logs for a task (for diagnosing a wedged/failed run)."""
    return await get_task_logs_impl(PraxisClient.from_env(), task_id=task_id)


@mcp.tool()
async def cancel_task(task_id: str) -> dict[str, Any]:
    """Stop a running task and mark it failed."""
    return await cancel_task_impl(PraxisClient.from_env(), task_id=task_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_server.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint, format, type-check**

Run:
```bash
uv run ruff format src/mcp_server/server.py tests/test_mcp_server.py
uv run ruff check --fix src/mcp_server/server.py tests/test_mcp_server.py
uv run mypy src/mcp_server/server.py --ignore-missing-imports
```
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add src/mcp_server/server.py tests/test_mcp_server.py
git commit -m "feat: add five MCP tool implementations (dispatch/poll/providers/logs/cancel)"
```

---

### Task 6: Add the stdio entry point and verify it boots

**Files:**
- Create: `src/mcp_server/__main__.py`

**Depends on:** Task 5

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mcp_server.py`:

```python
def test_main_callable_and_registers_tools() -> None:
    from mcp_server.__main__ import main

    assert callable(main)
    # The FastMCP instance should have the five tools registered.
    from mcp_server.server import mcp

    # FastMCP exposes registered tools via list_tools() (async) or _tool_manager.
    tool_names = {t.name for t in mcp._tool_manager.list_tools()}
    assert {
        "dispatch_task",
        "poll_task",
        "list_providers",
        "get_task_logs",
        "cancel_task",
    } <= tool_names
```

> NOTE: If `mcp._tool_manager.list_tools()` differs in the installed `mcp` version,
> discover the correct accessor with
> `uv run python -c "from mcp_server.server import mcp; print(dir(mcp))"` and adjust the
> test to assert the five names. Keep the assertion on the five tool names.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_server.py::test_main_callable_and_registers_tools -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mcp_server.__main__'`.

- [ ] **Step 3: Implement the entry point**

Create `src/mcp_server/__main__.py`:

```python
"""stdio entry point: ``python -m mcp_server`` / ``praxis-mcp``."""

from __future__ import annotations

from mcp_server.server import mcp


def main() -> None:
    """Run the Praxis MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_server.py::test_main_callable_and_registers_tools -v`
Expected: PASS.

- [ ] **Step 5: Smoke-test the console script resolves**

Run: `uv run python -c "import mcp_server.__main__ as m; print(callable(m.main))"`
Expected: prints `True`.

- [ ] **Step 6: Commit**

```bash
git add src/mcp_server/__main__.py tests/test_mcp_server.py
git commit -m "feat: add praxis-mcp stdio entry point"
```

---

### Task 7: Documentation — MCP usage and Claude Code config

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`

**Depends on:** Task 6

- [ ] **Step 1: Add an MCP section to README.md**

Add a new `## MCP Control Surface` section (place it after the existing usage/quick-start
content). Include exactly this content:

````markdown
## MCP Control Surface

Praxis can be driven as an **MCP server**, letting an MCP client (e.g. Claude Code) act as
the brain and dispatch implementation work to a **non-Anthropic** model running inside
Praxis. Claude Code's native subagents are model-locked to Claude; Praxis routes the work
to whatever model is loaded in LM Studio.

The MCP server is a thin stdio adapter over the REST API. The Praxis server must be running.

### Tools

| Tool | Purpose |
|------|---------|
| `dispatch_task(repo_url, instructions, model, harness?, branch?)` | Dispatch one task; returns `{task_id, dashboard_url, status}`. Praxis always runs its own review. |
| `poll_task(task_id)` | Get status, PR URL, review (and a dashboard link for wedged tasks). |
| `list_providers()` | List brain providers + worker models available to dispatch to. |
| `get_task_logs(task_id)` | Return agent-run logs for failure triage. |
| `cancel_task(task_id)` | Stop a running task. |

### Claude Code config

Add to your Claude Code MCP config (`.mcp.json` or user settings):

```json
{
  "mcpServers": {
    "praxis": {
      "command": "uv",
      "args": ["run", "praxis-mcp"],
      "env": {
        "PRAXIS_BASE_URL": "http://localhost:8080",
        "PRAXIS_AUTH_TOKEN": "your-auth-token"
      }
    }
  }
}
```

### Not in v1

- `dispatch_task` always runs Praxis's review; a `review=false` opt-out is a planned
  follow-up (requires orchestrator-loop changes).
- `submit_spec` / `poll_plan` (full autonomous-loop trigger) is deferred to a later phase.
- Worker models are LM-Studio-served only; arbitrary OpenAI-compatible endpoints and
  CLI-as-worker (codex/agy) are later phases.
````

- [ ] **Step 2: Add an MCP note to CLAUDE.md**

In `CLAUDE.md`, under the `## Project Structure` tree, add the `src/mcp_server/` package and
add a one-line entry to the `## Gotchas` section:

```markdown
- **MCP server is a separate package** — `src/mcp_server/` (stdio adapter, `praxis-mcp`
  entry point) forwards to the REST API via `PraxisClient`; it owns no engine logic. The
  only engine-side addition is `POST /api/dispatch` (`api/dispatch.py`), which injects a
  single-task plan because Praxis has no direct single-task creation route — tasks are
  created only via `TaskQueue.activate_plan`. MCP dispatch sets `approval_gate=False` on the
  auto-created project so the loop dispatches without a gate. v1 always runs review (no
  `review=false` opt-out yet).
```

- [ ] **Step 3: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: document MCP control surface and Claude Code config"
```

---

### Task 8: Full-suite verification

**Files:** none (verification only)

**Depends on:** Task 3, Task 5, Task 6, Task 7

- [ ] **Step 1: Run the full test suite with coverage**

Run: `uv run pytest --cov=orchestrator --cov=mcp_server --cov-report=term-missing -v`
Expected: all tests PASS, no regressions, new modules covered (target 80%+ on
`mcp_server/` and `api/dispatch.py`).

- [ ] **Step 2: Lint and format the whole tree**

Run:
```bash
uv run ruff format src/ tests/
uv run ruff check src/ tests/
```
Expected: no errors.

- [ ] **Step 3: Type-check**

Run: `uv run mypy src/ --ignore-missing-imports`
Expected: no new errors in `mcp_server/` or `api/dispatch.py`.

- [ ] **Step 4: Manual smoke test (optional, needs a running server)**

In one terminal: `uv run uvicorn orchestrator.main:app --host 127.0.0.1 --port 8080`
In another:
```bash
PRAXIS_BASE_URL=http://127.0.0.1:8080 PRAXIS_AUTH_TOKEN=<your-AUTH_TOKEN> \
  uv run python -c "import asyncio; from mcp_server.server import list_providers_impl; from mcp_server.client import PraxisClient; print(asyncio.run(list_providers_impl(PraxisClient.from_env())))"
```
Expected: prints `{brain_providers, worker_models, lm_studio_url, lm_studio_connected}`.

- [ ] **Step 5: Final commit (if any formatting changed)**

```bash
git add -A
git commit -m "chore: format and verify MCP control surface"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1 (deps), Task 2 (schemas), Task 4 (PraxisClient) — no dependencies, run in parallel.
- **Wave 2:** Task 3 (dispatch route — depends on Task 2), Task 5 (MCP tools — depends on Task 4).
- **Wave 3:** Task 6 (entry point — depends on Task 5).
- **Wave 4:** Task 7 (docs — depends on Task 6).
- **Wave 5:** Task 8 (full verification — depends on Tasks 3, 5, 6, 7).

> Note: Task 1 installs the `mcp` SDK that Task 5/6 import. Although Task 5 only *depends on*
> Task 4 for code, it cannot run its tests until Task 1 has installed `mcp`. If executing
> waves strictly in parallel, ensure Task 1 completes before Task 5's test step.

---

## Appendix: Design Decisions Recap

- **Planning ownership:** `dispatch_task` fires no `claude -p` planning — the MCP brain
  already planned. It injects a pre-made single-task `opus_plan`, reusing the exact same
  `TaskQueue.activate_plan` path the dashboard uses. (`submit_spec`, deferred, would inject a
  multi-task `opus_plan` via the same path.)
- **Async model:** handle + poll. `dispatch_task` never blocks; `poll_task` reflects engine
  state including wedged-task states surfaced by `reconcile_runs`. `dashboard_url` is the
  human-escalation hatch.
- **Transport:** stdio adapter → REST. Zero engine coupling; MCP is a third REST client
  alongside the dashboard and CLI.
- **Auth:** `PRAXIS_BASE_URL` + `PRAXIS_AUTH_TOKEN` env vars in the MCP client config;
  Bearer attached on every call. No secrets in tool args.
- **Worker scope:** existing Aider + LM Studio path, `spawn_agent` unchanged. "Any provider"
  = any LM-Studio-loaded (non-Claude) model.
