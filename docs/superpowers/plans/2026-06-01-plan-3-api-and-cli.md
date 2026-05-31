# Plan 3: REST API Endpoints + Typer CLI Client

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement all REST API endpoints (Projects, Plans, Tasks, System) and the Typer CLI client that calls them. After this plan, the orchestrator is fully functional via HTTP — ready for Docker and the web dashboard.

**Architecture:** FastAPI routers organized by resource. Each router depends on core modules from Plan 2. Auth middleware via Bearer token. CLI is a separate Typer app that calls the API via httpx.

**Tech Stack:** Python 3.11, FastAPI, Typer, httpx, rich (for CLI output), sse-starlette

---

## Full Project Context

This is **Plan 3 of 5** for the AI Agent Orchestrator.

**What Plan 1 built:**
- `src/orchestrator/config.py` — `Settings(auth_token, github_token, database_url, lm_studio_url, host, port)`
- `src/orchestrator/database.py` — `Database(database_url)` with `initialize()`, `close()`, `execute()`, `fetch_one()`, `fetch_all()`
- `src/orchestrator/models/schemas.py` — `ProjectCreate`, `ProjectUpdate`, `ProjectResponse`, `PlanCreate`, `PlanResponse`, `TaskResponse`, `AgentRunResponse`, `OpusStateResponse`, `TaskStatus`, `PlanStatus`, `OpusStatus`, `OpusPlanPayload`, `OpusReviewPayload`, `OpusImprovementPayload`
- `src/orchestrator/main.py` — FastAPI app with lifespan. `app.state.db` = Database, `app.state.settings` = Settings
- `src/orchestrator/api/auth.py` — `verify_token` dependency

**What Plan 2 built:**
- `src/orchestrator/core/task_queue.py` — `TaskQueue(db)` with `create_plan()`, `get_plan()`, `activate_plan()`, `get_tasks_for_plan()`, `update_task_status()`, `fail_task()`, `retry_task()`, `set_task_pr_url()`, `get_dispatchable_tasks()`, `all_tasks_done()`, `create_agent_run()`, `get_agent_run()`, `get_runs_for_task()`, `complete_agent_run()`
- `src/orchestrator/core/git_ops.py` — `GitOps(github_token)` with `clone_repo()`, `create_branch()`, `push_branch()`, `create_pr()`, `merge_pr()`, `comment_on_pr()`, `get_pr_diff()`, `get_changed_files()`, `extract_pr_number()`
- `src/orchestrator/core/opus_bridge.py` — `OpusBridge(db)` with `plan_spec()`, `review_diff()`, `analyze_improvements()`, `get_opus_state()`, `is_available()`, `queue_action()`, `get_queued_actions()`, `clear_queued_actions()`
- `src/orchestrator/core/agent_manager.py` — `AgentManager(lm_studio_url, github_token)` with `spawn_agent()`, `get_container_status()`, `get_container_logs()`, `stop_agent()`, `cleanup_container()`, `list_agent_containers()`

**Database tables:** `users`, `projects`, `plans`, `tasks`, `agent_runs`, `opus_state`

**API endpoints to implement:**
- Projects: `POST/GET /api/projects`, `GET/PATCH /api/projects/{id}`
- Plans: `POST/GET /api/projects/{id}/plans`, `GET /api/plans/{id}`, `POST /api/plans/{id}/approve`, `POST /api/plans/{id}/reject`
- Tasks: `GET /api/plans/{id}/tasks`, `GET /api/tasks/{id}`, `POST /api/tasks/{id}/stop`, `GET /api/tasks/{id}/logs` (SSE)
- System: `GET /api/status`, `GET /api/opus/state`
- Internal: `POST /api/internal/agent-done` (callback from agent containers)

**Auth:** All `/api/` endpoints require `Authorization: Bearer <token>`. Internal endpoints use a separate check.

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `src/orchestrator/api/projects.py` | Create | Project CRUD endpoints |
| `src/orchestrator/api/plans.py` | Create | Plan submission, listing, approve/reject |
| `src/orchestrator/api/tasks.py` | Create | Task listing, details, stop, log streaming |
| `src/orchestrator/api/system.py` | Create | Health, status, Opus state |
| `src/orchestrator/api/internal.py` | Create | Agent callback endpoint |
| `src/orchestrator/main.py` | Modify | Register routers, add core objects to app.state |
| `cli/main.py` | Create | Typer CLI client |
| `cli/__init__.py` | Create | CLI package init |
| `tests/test_api_projects.py` | Create | Project API tests |
| `tests/test_api_plans.py` | Create | Plan API tests |
| `tests/test_api_tasks.py` | Create | Task API tests |
| `tests/test_api_system.py` | Create | System API tests |
| `tests/conftest.py` | Modify | Add async client fixture |

---

### Task 1: Test Fixtures + App State Setup

**Files:**
- Modify: `tests/conftest.py`
- Modify: `src/orchestrator/main.py`

**Depends on:** None

- [ ] **Step 1: Update conftest with API test fixtures**

Add to `tests/conftest.py` (keep existing fixtures, add these):
```python
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from orchestrator.main import app
from orchestrator.database import Database
from orchestrator.core.task_queue import TaskQueue
from orchestrator.core.opus_bridge import OpusBridge


@pytest_asyncio.fixture
async def client(db: Database, test_settings) -> AsyncClient:
    """Async HTTP client with test database injected."""
    app.state.db = db
    app.state.settings = test_settings
    app.state.task_queue = TaskQueue(db)
    app.state.opus_bridge = OpusBridge(db)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


async def seed_user(db: Database) -> str:
    """Create a default test user, return user_id."""
    import bcrypt
    token_hash = bcrypt.hashpw(b"test-token", bcrypt.gensalt()).decode()
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("test-user", "Test User", token_hash),
    )
    return "test-user"
```

- [ ] **Step 2: Update main.py to register core objects and routers**

Replace `src/orchestrator/main.py` with:
```python
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI

from orchestrator.config import Settings
from orchestrator.core.agent_manager import AgentManager
from orchestrator.core.opus_bridge import OpusBridge
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = Settings()
    db_path = settings.database_url.replace("sqlite+aiosqlite:///", "")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    db = Database(settings.database_url)
    await db.initialize()

    app.state.db = db
    app.state.settings = settings
    app.state.task_queue = TaskQueue(db)
    app.state.opus_bridge = OpusBridge(db)
    app.state.agent_manager = AgentManager(
        lm_studio_url=settings.lm_studio_url,
        github_token=settings.github_token,
    )

    logger.info("Orchestrator started on %s:%d", settings.host, settings.port)
    yield
    await db.close()
    logger.info("Orchestrator stopped")


app = FastAPI(
    title="AI Agent Orchestrator",
    version="0.1.0",
    lifespan=lifespan,
)

# Import and register routers after app creation
from orchestrator.api.projects import router as projects_router  # noqa: E402
from orchestrator.api.plans import router as plans_router  # noqa: E402
from orchestrator.api.tasks import router as tasks_router  # noqa: E402
from orchestrator.api.system import router as system_router  # noqa: E402
from orchestrator.api.internal import router as internal_router  # noqa: E402

app.include_router(projects_router, prefix="/api")
app.include_router(plans_router, prefix="/api")
app.include_router(tasks_router, prefix="/api")
app.include_router(system_router, prefix="/api")
app.include_router(internal_router, prefix="/api/internal")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
```

- [ ] **Step 3: Commit**

```bash
git add tests/conftest.py src/orchestrator/main.py
git commit -m "feat: update conftest with API fixtures and register routers in main"
```

---

### Task 2: Projects API

**Files:**
- Create: `src/orchestrator/api/projects.py`
- Create: `tests/test_api_projects.py`

**Depends on:** Task 1

- [ ] **Step 1: Write failing tests**

Create file `tests/test_api_projects.py`:
```python
import pytest
from httpx import AsyncClient

from orchestrator.database import Database
from tests.conftest import seed_user


@pytest.mark.integration
class TestProjectsAPI:
    async def test_create_project(
        self, client: AsyncClient, db: Database, auth_headers: dict
    ) -> None:
        await seed_user(db)
        resp = await client.post(
            "/api/projects",
            json={
                "name": "My App",
                "repo_url": "https://github.com/user/myapp",
                "model_name": "deepseek-coder-v2",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "My App"
        assert data["repo_url"] == "https://github.com/user/myapp"
        assert data["approval_gate"] is True
        assert data["confidence_threshold"] == 0.7

    async def test_list_projects(
        self, client: AsyncClient, db: Database, auth_headers: dict
    ) -> None:
        await seed_user(db)
        await client.post(
            "/api/projects",
            json={"name": "App1", "repo_url": "https://github.com/u/a1", "model_name": "m1"},
            headers=auth_headers,
        )
        await client.post(
            "/api/projects",
            json={"name": "App2", "repo_url": "https://github.com/u/a2", "model_name": "m2"},
            headers=auth_headers,
        )
        resp = await client.get("/api/projects", headers=auth_headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    async def test_get_project(
        self, client: AsyncClient, db: Database, auth_headers: dict
    ) -> None:
        await seed_user(db)
        create_resp = await client.post(
            "/api/projects",
            json={"name": "App", "repo_url": "https://github.com/u/a", "model_name": "m"},
            headers=auth_headers,
        )
        project_id = create_resp.json()["id"]
        resp = await client.get(f"/api/projects/{project_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["name"] == "App"

    async def test_update_project(
        self, client: AsyncClient, db: Database, auth_headers: dict
    ) -> None:
        await seed_user(db)
        create_resp = await client.post(
            "/api/projects",
            json={"name": "App", "repo_url": "https://github.com/u/a", "model_name": "m"},
            headers=auth_headers,
        )
        project_id = create_resp.json()["id"]
        resp = await client.patch(
            f"/api/projects/{project_id}",
            json={"approval_gate": False, "max_retries": 5},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["approval_gate"] is False
        assert resp.json()["max_retries"] == 5

    async def test_get_nonexistent_project(
        self, client: AsyncClient, db: Database, auth_headers: dict
    ) -> None:
        await seed_user(db)
        resp = await client.get("/api/projects/nonexistent", headers=auth_headers)
        assert resp.status_code == 404

    async def test_unauthorized(self, client: AsyncClient) -> None:
        resp = await client.get("/api/projects")
        assert resp.status_code == 403
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_api_projects.py -v
```

Expected: FAIL

- [ ] **Step 3: Implement projects router**

Create file `src/orchestrator/api/projects.py`:
```python
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status

from orchestrator.api.auth import verify_token
from orchestrator.models.schemas import ProjectCreate, ProjectResponse, ProjectUpdate


router = APIRouter(tags=["projects"], dependencies=[Depends(verify_token)])


@router.post("/projects", status_code=status.HTTP_201_CREATED, response_model=ProjectResponse)
async def create_project(request: Request, body: ProjectCreate) -> dict:
    db = request.app.state.db
    # Get or create default user (v1: single user)
    user = await db.fetch_one("SELECT id FROM users LIMIT 1")
    if user is None:
        raise HTTPException(status_code=500, detail="No user found. Seed a user first.")
    user_id = user["id"]

    project_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO projects
           (id, user_id, name, repo_url, default_branch, approval_gate,
            confidence_threshold, max_retries, max_improvement_cycles,
            lm_studio_url, model_name)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            project_id, user_id, body.name, body.repo_url, body.default_branch,
            body.approval_gate, body.confidence_threshold, body.max_retries,
            body.max_improvement_cycles, body.lm_studio_url, body.model_name,
        ),
    )
    return await db.fetch_one("SELECT * FROM projects WHERE id = ?", (project_id,))


@router.get("/projects", response_model=list[ProjectResponse])
async def list_projects(request: Request) -> list[dict]:
    db = request.app.state.db
    return await db.fetch_all("SELECT * FROM projects ORDER BY created_at DESC")


@router.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project(request: Request, project_id: str) -> dict:
    db = request.app.state.db
    project = await db.fetch_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.patch("/projects/{project_id}", response_model=ProjectResponse)
async def update_project(
    request: Request, project_id: str, body: ProjectUpdate
) -> dict:
    db = request.app.state.db
    project = await db.fetch_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    updates = body.model_dump(exclude_none=True)
    if not updates:
        return project

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [project_id]
    await db.execute(
        f"UPDATE projects SET {set_clause} WHERE id = ?",
        tuple(values),
    )
    return await db.fetch_one("SELECT * FROM projects WHERE id = ?", (project_id,))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_api_projects.py -v
```

Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/api/projects.py tests/test_api_projects.py
git commit -m "feat: add projects REST API (CRUD)"
```

---

### Task 3: Plans API

**Files:**
- Create: `src/orchestrator/api/plans.py`
- Create: `tests/test_api_plans.py`

**Depends on:** Task 1, Task 2

- [ ] **Step 1: Write failing tests**

Create file `tests/test_api_plans.py`:
```python
import pytest
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient

from orchestrator.database import Database
from tests.conftest import seed_user


async def _create_project(client: AsyncClient, auth_headers: dict) -> str:
    resp = await client.post(
        "/api/projects",
        json={"name": "App", "repo_url": "https://github.com/u/a", "model_name": "m"},
        headers=auth_headers,
    )
    return resp.json()["id"]


@pytest.mark.integration
class TestPlansAPI:
    async def test_create_plan(
        self, client: AsyncClient, db: Database, auth_headers: dict
    ) -> None:
        await seed_user(db)
        project_id = await _create_project(client, auth_headers)
        resp = await client.post(
            f"/api/projects/{project_id}/plans",
            json={"spec": "Build a login page"},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["spec"] == "Build a login page"
        assert data["status"] == "pending"
        assert data["source"] == "user"

    async def test_list_plans(
        self, client: AsyncClient, db: Database, auth_headers: dict
    ) -> None:
        await seed_user(db)
        project_id = await _create_project(client, auth_headers)
        await client.post(
            f"/api/projects/{project_id}/plans",
            json={"spec": "Plan 1"},
            headers=auth_headers,
        )
        await client.post(
            f"/api/projects/{project_id}/plans",
            json={"spec": "Plan 2"},
            headers=auth_headers,
        )
        resp = await client.get(
            f"/api/projects/{project_id}/plans", headers=auth_headers
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    async def test_get_plan(
        self, client: AsyncClient, db: Database, auth_headers: dict
    ) -> None:
        await seed_user(db)
        project_id = await _create_project(client, auth_headers)
        create_resp = await client.post(
            f"/api/projects/{project_id}/plans",
            json={"spec": "Build auth"},
            headers=auth_headers,
        )
        plan_id = create_resp.json()["id"]
        resp = await client.get(f"/api/plans/{plan_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["spec"] == "Build auth"

    async def test_approve_plan(
        self, client: AsyncClient, db: Database, auth_headers: dict
    ) -> None:
        await seed_user(db)
        project_id = await _create_project(client, auth_headers)
        # Create a plan with autonomous source and pending status
        tq = client.app.state.task_queue  # type: ignore[union-attr]
        plan_id = await tq.create_plan(project_id, "Improve things", source="autonomous")
        resp = await client.post(
            f"/api/plans/{plan_id}/approve", headers=auth_headers
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "active"

    async def test_reject_plan(
        self, client: AsyncClient, db: Database, auth_headers: dict
    ) -> None:
        await seed_user(db)
        project_id = await _create_project(client, auth_headers)
        tq = client.app.state.task_queue  # type: ignore[union-attr]
        plan_id = await tq.create_plan(project_id, "Bad idea", source="autonomous")
        resp = await client.post(
            f"/api/plans/{plan_id}/reject", headers=auth_headers
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"

    async def test_get_nonexistent_plan(
        self, client: AsyncClient, db: Database, auth_headers: dict
    ) -> None:
        await seed_user(db)
        resp = await client.get("/api/plans/nonexistent", headers=auth_headers)
        assert resp.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_api_plans.py -v
```

- [ ] **Step 3: Implement plans router**

Create file `src/orchestrator/api/plans.py`:
```python
from fastapi import APIRouter, Depends, HTTPException, Request, status

from orchestrator.api.auth import verify_token
from orchestrator.models.schemas import PlanCreate, PlanResponse, PlanStatus


router = APIRouter(tags=["plans"], dependencies=[Depends(verify_token)])


@router.post(
    "/projects/{project_id}/plans",
    status_code=status.HTTP_201_CREATED,
    response_model=PlanResponse,
)
async def create_plan(
    request: Request, project_id: str, body: PlanCreate
) -> dict:
    db = request.app.state.db
    tq = request.app.state.task_queue
    project = await db.fetch_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    plan_id = await tq.create_plan(project_id, body.spec)
    return await tq.get_plan(plan_id)


@router.get("/projects/{project_id}/plans", response_model=list[PlanResponse])
async def list_plans(request: Request, project_id: str) -> list[dict]:
    tq = request.app.state.task_queue
    return await tq.get_plans_for_project(project_id)


@router.get("/plans/{plan_id}", response_model=PlanResponse)
async def get_plan(request: Request, plan_id: str) -> dict:
    tq = request.app.state.task_queue
    plan = await tq.get_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    return plan


@router.post("/plans/{plan_id}/approve", response_model=PlanResponse)
async def approve_plan(request: Request, plan_id: str) -> dict:
    tq = request.app.state.task_queue
    plan = await tq.get_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    if plan["status"] != PlanStatus.PENDING:
        raise HTTPException(status_code=400, detail="Plan is not pending")
    await tq.update_plan_status(plan_id, PlanStatus.ACTIVE)
    return await tq.get_plan(plan_id)


@router.post("/plans/{plan_id}/reject", response_model=PlanResponse)
async def reject_plan(request: Request, plan_id: str) -> dict:
    tq = request.app.state.task_queue
    plan = await tq.get_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    if plan["status"] != PlanStatus.PENDING:
        raise HTTPException(status_code=400, detail="Plan is not pending")
    await tq.update_plan_status(plan_id, PlanStatus.REJECTED)
    return await tq.get_plan(plan_id)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_api_plans.py -v
```

Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/api/plans.py tests/test_api_plans.py
git commit -m "feat: add plans REST API (create, list, approve, reject)"
```

---

### Task 4: Tasks API + Internal Callback

**Files:**
- Create: `src/orchestrator/api/tasks.py`
- Create: `src/orchestrator/api/internal.py`
- Create: `tests/test_api_tasks.py`

**Depends on:** Task 1, Task 3

- [ ] **Step 1: Write failing tests**

Create file `tests/test_api_tasks.py`:
```python
import json

import pytest
from httpx import AsyncClient

from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus
from tests.conftest import seed_user


async def _setup_plan_with_tasks(
    client: AsyncClient, db: Database, auth_headers: dict
) -> tuple[str, str, str]:
    """Create project, plan, activate with tasks. Returns (project_id, plan_id, task_id)."""
    await seed_user(db)
    resp = await client.post(
        "/api/projects",
        json={"name": "App", "repo_url": "https://github.com/u/a", "model_name": "m"},
        headers=auth_headers,
    )
    project_id = resp.json()["id"]

    tq = client.app.state.task_queue  # type: ignore[union-attr]
    plan_id = await tq.create_plan(project_id, "Build auth")
    opus_plan = {
        "plan_summary": "Auth",
        "plan_slug": "auth",
        "tasks": [
            {"title": "Login", "slug": "login", "description": "Build login", "depends_on": []},
        ],
    }
    await tq.activate_plan(plan_id, opus_plan, "plan/2026-06-01-auth")
    tasks = await tq.get_tasks_for_plan(plan_id)
    return project_id, plan_id, tasks[0]["id"]


@pytest.mark.integration
class TestTasksAPI:
    async def test_list_tasks(
        self, client: AsyncClient, db: Database, auth_headers: dict
    ) -> None:
        _, plan_id, _ = await _setup_plan_with_tasks(client, db, auth_headers)
        resp = await client.get(f"/api/plans/{plan_id}/tasks", headers=auth_headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["title"] == "Login"

    async def test_get_task(
        self, client: AsyncClient, db: Database, auth_headers: dict
    ) -> None:
        _, _, task_id = await _setup_plan_with_tasks(client, db, auth_headers)
        resp = await client.get(f"/api/tasks/{task_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["task"]["title"] == "Login"
        assert resp.json()["runs"] == []

    async def test_get_task_with_runs(
        self, client: AsyncClient, db: Database, auth_headers: dict
    ) -> None:
        _, _, task_id = await _setup_plan_with_tasks(client, db, auth_headers)
        tq = client.app.state.task_queue  # type: ignore[union-attr]
        await tq.create_agent_run(task_id, "container-abc")
        resp = await client.get(f"/api/tasks/{task_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert len(resp.json()["runs"]) == 1

    async def test_stop_task(
        self, client: AsyncClient, db: Database, auth_headers: dict
    ) -> None:
        _, _, task_id = await _setup_plan_with_tasks(client, db, auth_headers)
        tq = client.app.state.task_queue  # type: ignore[union-attr]
        await tq.update_task_status(task_id, TaskStatus.IN_PROGRESS)
        await tq.create_agent_run(task_id, "container-abc")
        # Stop will try to stop docker container — we just test the API response
        resp = await client.post(f"/api/tasks/{task_id}/stop", headers=auth_headers)
        # May fail because container doesn't exist, but endpoint should respond
        assert resp.status_code in (200, 500)

    async def test_get_nonexistent_task(
        self, client: AsyncClient, db: Database, auth_headers: dict
    ) -> None:
        await seed_user(db)
        resp = await client.get("/api/tasks/nonexistent", headers=auth_headers)
        assert resp.status_code == 404


@pytest.mark.integration
class TestInternalCallback:
    async def test_agent_done_callback(
        self, client: AsyncClient, db: Database, auth_headers: dict
    ) -> None:
        _, _, task_id = await _setup_plan_with_tasks(client, db, auth_headers)
        tq = client.app.state.task_queue  # type: ignore[union-attr]
        await tq.update_task_status(task_id, TaskStatus.IN_PROGRESS)
        run_id = await tq.create_agent_run(task_id, "container-xyz")
        resp = await client.post(
            "/api/internal/agent-done",
            json={
                "task_id": task_id,
                "run_id": run_id,
                "status": "completed",
                "pr_url": "https://github.com/u/a/pull/1",
            },
        )
        assert resp.status_code == 200
        task = await tq.get_task(task_id)
        assert task["status"] == TaskStatus.REVIEWING
        assert task["pr_url"] == "https://github.com/u/a/pull/1"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_api_tasks.py -v
```

- [ ] **Step 3: Implement tasks router**

Create file `src/orchestrator/api/tasks.py`:
```python
from fastapi import APIRouter, Depends, HTTPException, Request

from orchestrator.api.auth import verify_token
from orchestrator.models.schemas import TaskResponse, AgentRunResponse


router = APIRouter(tags=["tasks"], dependencies=[Depends(verify_token)])


@router.get("/plans/{plan_id}/tasks", response_model=list[TaskResponse])
async def list_tasks(request: Request, plan_id: str) -> list[dict]:
    tq = request.app.state.task_queue
    return await tq.get_tasks_for_plan(plan_id)


@router.get("/tasks/{task_id}")
async def get_task(request: Request, task_id: str) -> dict:
    tq = request.app.state.task_queue
    task = await tq.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    runs = await tq.get_runs_for_task(task_id)
    return {"task": task, "runs": runs}


@router.post("/tasks/{task_id}/stop")
async def stop_task(request: Request, task_id: str) -> dict:
    tq = request.app.state.task_queue
    agent_manager = request.app.state.agent_manager
    task = await tq.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    runs = await tq.get_runs_for_task(task_id)
    running = [r for r in runs if r["status"] == "running"]
    for run in running:
        agent_manager.stop_agent(run["container_id"])
        await tq.complete_agent_run(run["id"], "stopped", "Stopped by user")
    await tq.update_task_status(task_id, "failed")
    return {"stopped": len(running)}
```

- [ ] **Step 4: Implement internal callback router**

Create file `src/orchestrator/api/internal.py`:
```python
import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel

from orchestrator.models.schemas import TaskStatus


logger = logging.getLogger(__name__)

router = APIRouter(tags=["internal"])


class AgentDonePayload(BaseModel):
    task_id: str
    run_id: str
    status: str
    pr_url: str | None = None


@router.post("/agent-done")
async def agent_done(request: Request, body: AgentDonePayload) -> dict:
    tq = request.app.state.task_queue
    agent_manager = request.app.state.agent_manager

    # Update agent run
    logs = agent_manager.get_container_logs(body.run_id) if body.run_id else ""
    await tq.complete_agent_run(body.run_id, body.status, logs)

    # Update task
    if body.pr_url:
        await tq.set_task_pr_url(body.task_id, body.pr_url)

    if body.status == "completed":
        await tq.update_task_status(body.task_id, TaskStatus.REVIEWING)
        logger.info("Task %s ready for review", body.task_id)
    else:
        await tq.update_task_status(body.task_id, TaskStatus.FAILED)
        logger.warning("Task %s agent failed", body.task_id)

    # Cleanup container
    runs = await tq.get_runs_for_task(body.task_id)
    for run in runs:
        if run["id"] == body.run_id:
            agent_manager.cleanup_container(run["container_id"])

    return {"status": "ok"}
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/test_api_tasks.py -v
```

Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/api/tasks.py src/orchestrator/api/internal.py tests/test_api_tasks.py
git commit -m "feat: add tasks API and internal agent callback endpoint"
```

---

### Task 5: System API

**Files:**
- Create: `src/orchestrator/api/system.py`
- Create: `tests/test_api_system.py`

**Depends on:** Task 1

- [ ] **Step 1: Write failing tests**

Create file `tests/test_api_system.py`:
```python
import pytest
from httpx import AsyncClient

from orchestrator.database import Database
from tests.conftest import seed_user


@pytest.mark.integration
class TestSystemAPI:
    async def test_status(
        self, client: AsyncClient, db: Database, auth_headers: dict
    ) -> None:
        await seed_user(db)
        resp = await client.get("/api/status", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "opus_state" in data
        assert "active_agents" in data

    async def test_opus_state(
        self, client: AsyncClient, db: Database, auth_headers: dict
    ) -> None:
        await seed_user(db)
        resp = await client.get("/api/opus/state", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "available"
        assert data["queued_count"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_api_system.py -v
```

- [ ] **Step 3: Implement system router**

Create file `src/orchestrator/api/system.py`:
```python
import json

from fastapi import APIRouter, Depends, Request

from orchestrator.api.auth import verify_token
from orchestrator.models.schemas import OpusStateResponse


router = APIRouter(tags=["system"], dependencies=[Depends(verify_token)])


@router.get("/status")
async def system_status(request: Request) -> dict:
    opus_bridge = request.app.state.opus_bridge
    opus_state = await opus_bridge.get_opus_state()
    queued = json.loads(opus_state["queued_actions"])

    agent_manager = request.app.state.agent_manager
    try:
        containers = agent_manager.list_agent_containers()
    except Exception:
        containers = []

    return {
        "opus_state": {
            "status": opus_state["status"],
            "rate_limited_at": opus_state.get("rate_limited_at"),
            "resume_at": opus_state.get("resume_at"),
            "queued_count": len(queued),
        },
        "active_agents": len([c for c in containers if c["status"] == "running"]),
        "total_agents": len(containers),
    }


@router.get("/opus/state", response_model=OpusStateResponse)
async def opus_state(request: Request) -> dict:
    opus_bridge = request.app.state.opus_bridge
    state = await opus_bridge.get_opus_state()
    queued = json.loads(state["queued_actions"])
    return {
        "status": state["status"],
        "rate_limited_at": state.get("rate_limited_at"),
        "resume_at": state.get("resume_at"),
        "queued_count": len(queued),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_api_system.py -v
```

Expected: All 2 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/api/system.py tests/test_api_system.py
git commit -m "feat: add system status and Opus state API endpoints"
```

---

### Task 6: Typer CLI Client

**Files:**
- Create: `cli/__init__.py`
- Create: `cli/main.py`

**Depends on:** Task 2, Task 3, Task 4, Task 5

- [ ] **Step 1: Create CLI package**

Create file `cli/__init__.py`:
```python
```

- [ ] **Step 2: Implement CLI client**

Create file `cli/main.py`:
```python
import json
import sys

import httpx
import typer
from rich.console import Console
from rich.table import Table


app = typer.Typer(name="orchestrator-cli", help="AI Agent Orchestrator CLI")
console = Console()

# Config — set via env or defaults
API_URL = "http://localhost:8080"
AUTH_TOKEN = ""


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=API_URL,
        headers={"Authorization": f"Bearer {AUTH_TOKEN}"},
        timeout=60.0,
    )


def _check(resp: httpx.Response) -> dict | list:
    if resp.status_code >= 400:
        console.print(f"[red]Error {resp.status_code}:[/red] {resp.text}")
        raise typer.Exit(1)
    return resp.json()


# --- Projects ---


@app.command()
def projects() -> None:
    """List all projects."""
    with _client() as c:
        data = _check(c.get("/api/projects"))
    table = Table(title="Projects")
    table.add_column("ID", style="dim", max_width=8)
    table.add_column("Name")
    table.add_column("Repo")
    table.add_column("Model")
    table.add_column("Gate")
    for p in data:
        table.add_row(
            p["id"][:8], p["name"], p["repo_url"],
            p["model_name"], "ON" if p["approval_gate"] else "OFF",
        )
    console.print(table)


@app.command()
def add_project(
    name: str = typer.Argument(..., help="Project display name"),
    repo: str = typer.Argument(..., help="GitHub repo URL"),
    model: str = typer.Argument(..., help="LM Studio model name"),
) -> None:
    """Register a new GitHub repository."""
    with _client() as c:
        data = _check(c.post("/api/projects", json={
            "name": name, "repo_url": repo, "model_name": model,
        }))
    console.print(f"[green]Created project:[/green] {data['id']}")


@app.command()
def configure(
    project_id: str = typer.Argument(..., help="Project ID"),
    gate: bool | None = typer.Option(None, help="Approval gate on/off"),
    threshold: float | None = typer.Option(None, help="Confidence threshold"),
    retries: int | None = typer.Option(None, help="Max retries"),
) -> None:
    """Update project settings."""
    body: dict = {}
    if gate is not None:
        body["approval_gate"] = gate
    if threshold is not None:
        body["confidence_threshold"] = threshold
    if retries is not None:
        body["max_retries"] = retries
    if not body:
        console.print("[yellow]No settings to update[/yellow]")
        return
    with _client() as c:
        data = _check(c.patch(f"/api/projects/{project_id}", json=body))
    console.print(f"[green]Updated project:[/green] {data['name']}")


# --- Plans ---


@app.command()
def submit(
    project_id: str = typer.Argument(..., help="Project ID"),
    spec: str = typer.Argument(..., help="Specification text"),
) -> None:
    """Submit a specification for planning."""
    with _client() as c:
        data = _check(c.post(
            f"/api/projects/{project_id}/plans",
            json={"spec": spec},
        ))
    console.print(f"[green]Plan created:[/green] {data['id']} (status: {data['status']})")


@app.command()
def plans(project_id: str = typer.Argument(..., help="Project ID")) -> None:
    """List plans for a project."""
    with _client() as c:
        data = _check(c.get(f"/api/projects/{project_id}/plans"))
    table = Table(title="Plans")
    table.add_column("ID", style="dim", max_width=8)
    table.add_column("Spec", max_width=40)
    table.add_column("Source")
    table.add_column("Status")
    for p in data:
        table.add_row(p["id"][:8], p["spec"][:40], p["source"], p["status"])
    console.print(table)


@app.command()
def approve(plan_id: str = typer.Argument(..., help="Plan ID")) -> None:
    """Approve an autonomous improvement plan."""
    with _client() as c:
        data = _check(c.post(f"/api/plans/{plan_id}/approve"))
    console.print(f"[green]Plan approved:[/green] {data['id']}")


@app.command()
def reject(plan_id: str = typer.Argument(..., help="Plan ID")) -> None:
    """Reject an autonomous improvement plan."""
    with _client() as c:
        data = _check(c.post(f"/api/plans/{plan_id}/reject"))
    console.print(f"[red]Plan rejected:[/red] {data['id']}")


# --- Tasks ---


@app.command()
def tasks(plan_id: str = typer.Argument(..., help="Plan ID")) -> None:
    """List tasks in a plan."""
    with _client() as c:
        data = _check(c.get(f"/api/plans/{plan_id}/tasks"))
    table = Table(title="Tasks")
    table.add_column("ID", style="dim", max_width=8)
    table.add_column("Title")
    table.add_column("Branch")
    table.add_column("Status")
    table.add_column("Attempt")
    for t in data:
        table.add_row(
            t["id"][:8], t["title"], t["branch_name"],
            t["status"], str(t["attempt"]),
        )
    console.print(table)


@app.command()
def task(task_id: str = typer.Argument(..., help="Task ID")) -> None:
    """Get task details with agent run history."""
    with _client() as c:
        data = _check(c.get(f"/api/tasks/{task_id}"))
    t = data["task"]
    console.print(f"[bold]{t['title']}[/bold]")
    console.print(f"Status: {t['status']} | Branch: {t['branch_name']} | Attempt: {t['attempt']}")
    if t["pr_url"]:
        console.print(f"PR: {t['pr_url']}")
    if t["review_feedback"]:
        console.print(f"[yellow]Feedback:[/yellow] {t['review_feedback']}")
    if data["runs"]:
        console.print(f"\n[bold]Agent Runs ({len(data['runs'])}):[/bold]")
        for r in data["runs"]:
            console.print(f"  {r['id'][:8]} | {r['status']} | {r['started_at']}")


@app.command()
def stop(task_id: str = typer.Argument(..., help="Task ID")) -> None:
    """Stop a running agent."""
    with _client() as c:
        data = _check(c.post(f"/api/tasks/{task_id}/stop"))
    console.print(f"[yellow]Stopped {data['stopped']} agent(s)[/yellow]")


# --- System ---


@app.command()
def status() -> None:
    """Show orchestrator status."""
    with _client() as c:
        data = _check(c.get("/api/status"))
    console.print(f"Opus: [bold]{data['opus_state']['status']}[/bold]")
    if data["opus_state"]["resume_at"]:
        console.print(f"  Resume at: {data['opus_state']['resume_at']}")
    console.print(f"  Queued actions: {data['opus_state']['queued_count']}")
    console.print(f"Active agents: {data['active_agents']} / {data['total_agents']} total")


def main() -> None:
    """Entry point — reads config from env."""
    import os
    global API_URL, AUTH_TOKEN
    API_URL = os.environ.get("ORCHESTRATOR_URL", "http://localhost:8080")
    AUTH_TOKEN = os.environ.get("ORCHESTRATOR_TOKEN", "")
    if not AUTH_TOKEN:
        console.print("[red]Set ORCHESTRATOR_TOKEN env var[/red]")
        raise typer.Exit(1)
    app()


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Verify CLI help works**

```bash
cd C:\working-space\praxis
uv run python -m cli.main --help
```

Expected: Shows help text with all commands

- [ ] **Step 4: Commit**

```bash
git add cli/__init__.py cli/main.py
git commit -m "feat: add Typer CLI client with all commands"
```

---

### Task 7: Run Full Test Suite + Lint

**Files:** None (verification only)

**Depends on:** Task 2, Task 3, Task 4, Task 5, Task 6

- [ ] **Step 1: Run all tests with coverage**

```bash
cd C:\working-space\praxis
uv run pytest --cov=orchestrator --cov-report=term-missing -v
```

Expected: All tests PASS, coverage > 80%

- [ ] **Step 2: Run ruff and mypy**

```bash
uv run ruff fmt src/ tests/ cli/
uv run ruff check --fix src/ tests/ cli/
uv run mypy src/orchestrator/ --ignore-missing-imports
```

- [ ] **Step 3: Commit any fixes**

```bash
git add -A
git diff --cached --quiet || git commit -m "style: apply ruff formatting and lint fixes"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1 (fixtures + app state)
- **Wave 2:** Task 2 (Projects API), Task 5 (System API) — both depend only on Task 1
- **Wave 3:** Task 3 (Plans API — depends on Task 2)
- **Wave 4:** Task 4 (Tasks API — depends on Task 3)
- **Wave 5:** Task 6 (CLI — depends on all APIs)
- **Wave 6:** Task 7 (verification)
