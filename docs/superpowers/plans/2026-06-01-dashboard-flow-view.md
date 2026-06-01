# Dashboard Flow View — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a plan-centric "Dashboard" view to the Praxis web UI with live SSE updates, swim lanes per active plan, task detail side panel, and a task retry API endpoint.

**Architecture:** Single-file HTML dashboard (`web/index.html`) gets a new `renderDashboard()` view added to the existing `switchView()` system. One new backend endpoint (`POST /api/tasks/{id}/retry`) in FastAPI. All data comes from existing API endpoints; live updates via the existing SSE `/api/events` stream.

**Tech Stack:** Python 3.11, FastAPI, aiosqlite, vanilla HTML/CSS/JS (single-file), SSE via EventSource.

---

## Codebase Context for Agentic Workers

### Project structure
```
praxis/
├── src/orchestrator/           # FastAPI backend
│   ├── main.py                 # App + lifespan + router mounting
│   ├── config.py               # pydantic-settings
│   ├── database.py             # SQLite connection
│   ├── api/
│   │   ├── tasks.py            # Task endpoints (GET, stop)
│   │   ├── plans.py            # Plan endpoints (CRUD, approve, reject)
│   │   ├── projects.py         # Project endpoints
│   │   ├── system.py           # /api/status
│   │   ├── events.py           # SSE streaming
│   │   ├── internal.py         # Agent callback
│   │   └── auth.py             # Bearer token validation
│   ├── core/
│   │   ├── task_queue.py       # Plan/task lifecycle (has retry_task())
│   │   ├── event_bus.py        # In-memory pub/sub
│   │   ├── orchestrator.py     # Main loop
│   │   ├── opus_bridge.py      # Claude CLI bridge
│   │   ├── agent_manager.py    # Docker container lifecycle
│   │   └── git_ops.py          # Git operations
│   └── models/
│       └── schemas.py          # Pydantic models + enums
├── web/
│   └── index.html              # Single-file dashboard (~920 lines)
├── tests/
│   ├── conftest.py             # Shared fixtures
│   ├── test_api_tasks.py       # Task API tests
│   └── ...                     # Other test files
└── pyproject.toml
```

### Key conventions
- **Python**: PEP 8, type annotations on all signatures, `from __future__ import annotations`
- **Tests**: pytest with `asyncio_mode = "auto"`, markers `@pytest.mark.unit` / `@pytest.mark.integration`
- **Test client**: `httpx.AsyncClient` via `ASGITransport`, fixtures in `conftest.py`
- **API pattern**: `request.app.state.task_queue` for DB access, `request.app.state.event_bus` for events
- **Lint**: `ruff format src/ tests/` then `ruff check --fix src/ tests/`
- **Frontend**: All JS/CSS/HTML in one file (`web/index.html`), no build step, vanilla JS, CSS variables for theming

### Existing task statuses (from `schemas.py`)
```python
class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    REVIEWING = "reviewing"
    PASSED = "passed"
    FAILED = "failed"
    MERGED = "merged"

class PlanStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    REJECTED = "rejected"
```

### Existing sidebar nav items (from `web/index.html` line 354-358)
```html
<nav class="sidebar-nav" aria-label="Views">
  <div class="nav-section">Workspace</div>
  <button class="nav-item active" data-view="projects" onclick="switchView('projects')"><span class="nav-icon">P</span>Projects</button>
  <button class="nav-item" data-view="plans" onclick="switchView('plans')"><span class="nav-icon">S</span>Plans</button>
  <button class="nav-item" data-view="tasks" onclick="switchView('tasks')"><span class="nav-icon">T</span>Tasks</button>
  <button class="nav-item" data-view="logs" onclick="switchView('logs')"><span class="nav-icon">L</span>Live Logs</button>
</nav>
```

### Existing switchView function (from `web/index.html` line 484-505)
```javascript
async function switchView(name) {
  currentView = name;
  showingForm = false;
  document.querySelectorAll(".nav-item").forEach(el => el.classList.toggle("active", el.dataset.view === name));
  if (eventSource && name !== "logs") {
    eventSource.close();
    eventSource = null;
  }
  if (name === "projects") {
    setTopbar("Projects", "+ New Project");
    await loadProjects();
  } else if (name === "plans") {
    setTopbar("Plans", "+ Submit Spec");
    await loadPlans();
  } else if (name === "tasks") {
    setTopbar("Tasks", "Refresh");
    await renderTasks();
  } else if (name === "logs") {
    setTopbar("Live Logs", "Reconnect");
    renderLogs();
  }
}
```

### Existing SSE connection pattern (from `web/index.html` line 873-885)
```javascript
function openSse(path) {
  if (eventSource) eventSource.close();
  const logEl = document.getElementById("log-output");
  if (logEl) logEl.textContent = "";
  eventSource = new EventSource(API + path);
  eventSource.onopen = () => appendLog("connected");
  eventSource.onerror = () => appendLog("connection error");
  eventSource.onmessage = event => appendLog(event.data);
  ["plan_activated", "agent_dispatched", "review_completed", "improvement_proposed",
   "task_completed", "task_failed", "task_retry", "opus_queued", "log", "agent_log"].forEach(type => {
    eventSource.addEventListener(type, event => appendLog("[" + type + "] " + event.data));
  });
}
```

### Existing test helper for setting up a plan with task (from `tests/test_api_tasks.py`)
```python
async def _setup_plan_with_task(client, db, auth_headers) -> tuple[str, str]:
    await seed_user(db)
    project = await client.post(
        "/api/projects",
        json={"name": "App", "repo_url": "https://github.com/u/a", "model_name": "m"},
        headers=auth_headers,
    )
    queue = client.app.state.task_queue
    plan_id = await queue.create_plan(project.json()["id"], "Build auth")
    await queue.activate_plan(
        plan_id,
        {"plan_summary": "Auth", "plan_slug": "auth",
         "tasks": [{"title": "Login", "slug": "login", "description": "Build login", "depends_on": []}]},
        "plan/2026-06-01-auth",
    )
    task_id = (await queue.get_tasks_for_plan(plan_id))[0]["id"]
    return plan_id, task_id
```

### Existing `retry_task` method (from `src/orchestrator/core/task_queue.py` line 128-139)
```python
async def retry_task(self, task_id: str) -> None:
    task = await self.get_task(task_id)
    if task is None:
        message = f"Task {task_id} not found"
        raise ValueError(message)
    now = datetime.now(UTC).isoformat()
    await self._db.execute(
        """UPDATE tasks
           SET status = ?, attempt = ?, updated_at = ?
           WHERE id = ?""",
        (TaskStatus.PENDING, int(task["attempt"]) + 1, now, task_id),
    )
```

### Existing stop_task endpoint pattern (from `src/orchestrator/api/tasks.py` line 47-68)
```python
@router.post("/tasks/{task_id}/stop")
async def stop_task(request: Request, task_id: str) -> dict[str, int]:
    queue = request.app.state.task_queue
    task = await queue.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    # ... stop logic ...
    return {"stopped": stopped}
```

### How to run tests
```bash
uv run pytest --cov=orchestrator --cov-report=term-missing -v          # full suite
uv run pytest tests/test_api_tasks.py -v                                # single file
uv run pytest tests/test_api_tasks.py::test_retry_task_success -v       # single test
```

### How to lint
```bash
uv run ruff format src/ tests/
uv run ruff check --fix src/ tests/
```

---

## File Map

| Action | File | Responsibility |
|--------|------|---------------|
| Modify | `src/orchestrator/api/tasks.py` | Add `POST /tasks/{task_id}/retry` endpoint |
| Modify | `tests/test_api_tasks.py` | Add tests for retry endpoint |
| Modify | `web/index.html` | Add Dashboard view (CSS + HTML + JS) |

No new files are created. All changes modify existing files.

---

### Task 1: Backend — Task Retry Endpoint

**Files:**
- Modify: `src/orchestrator/api/tasks.py` (add new endpoint after the existing `stop_task` at line 47-68)
- Modify: `tests/test_api_tasks.py` (add test cases)

**Depends on:** None

- [ ] **Step 1: Write failing tests for the retry endpoint**

Add three test functions to `tests/test_api_tasks.py`. Place them after the existing `test_stop_task` function (after line 80). These tests reuse the existing `_setup_plan_with_task` helper and `seed_user` from conftest.

```python
@pytest.mark.integration
async def test_retry_task_success(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    _, task_id = await _setup_plan_with_task(client, db, auth_headers)
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    await queue.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    await queue.fail_task(task_id, "tests failing")

    response = await client.post(f"/api/tasks/{task_id}/retry", headers=auth_headers)
    task = await queue.get_task(task_id)

    assert response.status_code == 200
    assert response.json()["status"] == "pending"
    assert response.json()["attempt"] == 2
    assert task["status"] == TaskStatus.PENDING
    assert task["attempt"] == 2


@pytest.mark.integration
async def test_retry_task_not_failed_returns_409(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    _, task_id = await _setup_plan_with_task(client, db, auth_headers)

    response = await client.post(f"/api/tasks/{task_id}/retry", headers=auth_headers)

    assert response.status_code == 409
    assert "not failed" in response.json()["detail"].lower()


@pytest.mark.integration
async def test_retry_task_not_found_returns_404(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    await seed_user(db)

    response = await client.post("/api/tasks/nonexistent/retry", headers=auth_headers)

    assert response.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_api_tasks.py::test_retry_task_success tests/test_api_tasks.py::test_retry_task_not_failed_returns_409 tests/test_api_tasks.py::test_retry_task_not_found_returns_404 -v`

Expected: All 3 FAIL with 405 Method Not Allowed (endpoint doesn't exist yet).

- [ ] **Step 3: Implement the retry endpoint**

Add this function to `src/orchestrator/api/tasks.py` after the existing `stop_task` function (after line 68). The file already imports `APIRouter, Depends, HTTPException, Request, status` from fastapi and `TaskResponse, TaskStatus` from schemas. You also need to add `cast` and `TaskResponse` to the existing imports.

The complete file after modification should have these imports at the top:

```python
"""Task REST endpoints."""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status

from orchestrator.api.auth import verify_token
from orchestrator.models.schemas import TaskResponse, TaskStatus
```

Add this endpoint after the `stop_task` function:

```python
@router.post("/tasks/{task_id}/retry", response_model=TaskResponse)
async def retry_task(request: Request, task_id: str) -> dict[str, Any]:
    """Retry a failed task by resetting it to pending."""

    queue = request.app.state.task_queue
    task = await queue.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        )
    if task["status"] != TaskStatus.FAILED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Task is not failed — only failed tasks can be retried",
        )
    await queue.retry_task(task_id)

    event_bus = request.app.state.event_bus
    event_bus.publish({"type": "task_retry", "task_id": task_id})

    updated = await queue.get_task(task_id)
    return cast(dict[str, Any], updated)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_api_tasks.py -v`

Expected: All tests pass including the 3 new retry tests.

- [ ] **Step 5: Lint**

Run: `uv run ruff format src/orchestrator/api/tasks.py tests/test_api_tasks.py && uv run ruff check --fix src/orchestrator/api/tasks.py tests/test_api_tasks.py`

Expected: No errors.

- [ ] **Step 6: Run full test suite to check for regressions**

Run: `uv run pytest --cov=orchestrator --cov-report=term-missing -v`

Expected: All tests pass. Coverage should remain at or above 88%.

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator/api/tasks.py tests/test_api_tasks.py
git commit -m "$(cat <<'EOF'
feat(api): add POST /api/tasks/{id}/retry endpoint

Exposes the existing task_queue.retry_task() method via REST API.
Validates task is in failed state (409 if not), resets to pending,
increments attempt count, and publishes task_retry event to SSE bus.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Frontend — Dashboard CSS

**Files:**
- Modify: `web/index.html` (add CSS rules inside the existing `<style>` block)

**Depends on:** None

- [ ] **Step 1: Add dashboard-specific CSS**

In `web/index.html`, add these CSS rules **before** the closing `</style>` tag (which is at line 346). Insert them after the existing `@media (max-width: 768px)` block (after line 345, before line 346 `</style>`).

```css
    /* Dashboard view */
    .health-bar {
      display: flex;
      align-items: center;
      gap: 16px;
      padding: 10px 20px;
      background: var(--panel-alt);
      border-bottom: 1px solid var(--border);
      font-size: 12px;
      font-weight: 600;
      color: var(--text-muted);
      flex-wrap: wrap;
    }
    .health-item { display: flex; align-items: center; gap: 6px; }
    .health-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
    .health-dot.available { background: #22c55e; }
    .health-dot.rate_limited { background: #eab308; }
    .health-dot.resuming { background: #ef4444; }
    .health-attention {
      background: var(--badge-reviewing-bg);
      color: var(--badge-reviewing-text);
      padding: 2px 8px;
      border-radius: 4px;
      font-size: 11px;
    }
    .health-spacer { margin-left: auto; }

    .dashboard-body { flex: 1; overflow-y: auto; padding: 0; display: flex; }
    .dashboard-lanes { flex: 1; min-width: 0; overflow-y: auto; }

    .swim-lane { border-bottom: 1px solid var(--border); }
    .lane-header {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 12px 20px;
      flex-wrap: wrap;
    }
    .lane-spec { font-size: 13px; font-weight: 700; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
    .lane-meta { color: var(--text-faint); font-size: 11px; font-family: "Cascadia Mono", "SF Mono", Consolas, monospace; white-space: nowrap; }
    .lane-spec-full { padding: 0 20px 12px; font-size: 13px; line-height: 1.6; color: var(--text-muted); white-space: pre-wrap; }

    .lane-cards { display: flex; gap: 10px; padding: 0 20px 14px; overflow-x: auto; }
    .task-card {
      min-width: 170px;
      max-width: 220px;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 10px 12px;
      background: var(--panel);
      cursor: pointer;
      flex-shrink: 0;
      transition: border-color 0.15s;
    }
    .task-card:hover { border-color: var(--text-faint); }
    .task-card.selected { border-color: var(--selected-border); border-width: 2px; padding: 9px 11px; }
    .task-card.status-merged { border-left: 3px solid var(--badge-passed-text); opacity: 0.75; }
    .task-card.status-passed { border-left: 3px solid var(--badge-passed-text); }
    .task-card.status-reviewing { border-left: 3px solid var(--badge-reviewing-text); }
    .task-card.status-in_progress { border-left: 3px solid var(--badge-active-text); }
    .task-card.status-failed { border-left: 3px solid var(--badge-failed-text); background: color-mix(in srgb, var(--badge-failed-bg) 20%, var(--panel)); }
    .task-card.status-pending { border-left: 3px solid var(--text-faint); }
    .card-title { font-size: 12px; font-weight: 700; margin-top: 6px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .card-meta { font-size: 10px; color: var(--text-faint); margin-top: 4px; }
    .card-log-line { font-size: 10px; color: var(--badge-active-text); margin-top: 4px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: "Cascadia Mono", "SF Mono", Consolas, monospace; }

    @keyframes pulse-dot {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.3; }
    }
    .pulse-dot {
      display: inline-block;
      width: 6px;
      height: 6px;
      border-radius: 50%;
      animation: pulse-dot 1.5s ease-in-out infinite;
    }
    .pulse-dot.green { background: #22c55e; }
    .pulse-dot.amber { background: #eab308; }

    .completed-lane { opacity: 0.6; transition: opacity 0.2s; }
    .completed-lane.expanded { opacity: 1; }
    .completed-header {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 10px 20px;
      cursor: pointer;
      font-size: 13px;
    }
    .completed-header:hover { background: var(--hover-bg); }
    .disclosure { font-size: 10px; color: var(--text-faint); transition: transform 0.2s; }
    .disclosure.open { transform: rotate(90deg); }
    .completed-meta { color: var(--text-faint); font-size: 11px; }

    .side-panel {
      width: 0;
      overflow: hidden;
      border-left: 1px solid var(--border);
      background: var(--panel);
      display: flex;
      flex-direction: column;
      transition: width 0.2s ease;
      flex-shrink: 0;
    }
    .side-panel.open { width: 35%; min-width: 320px; }
    .side-panel-header {
      padding: 16px;
      border-bottom: 1px solid var(--border-subtle);
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 10px;
    }
    .side-panel-close {
      border: 0;
      background: transparent;
      color: var(--text-faint);
      cursor: pointer;
      font-size: 18px;
      line-height: 1;
      padding: 0 4px;
    }
    .side-panel-close:hover { color: var(--text); }
    .side-panel-content { flex: 1; overflow-y: auto; padding: 16px; }
    .side-panel-actions { padding: 12px 16px; border-top: 1px solid var(--border); display: flex; gap: 8px; flex-wrap: wrap; }
    .side-panel-log {
      height: 180px;
      overflow-y: auto;
      background: var(--log-bg);
      color: var(--log-text);
      border-radius: 6px;
      padding: 8px 10px;
      font-family: "Cascadia Mono", "SF Mono", Consolas, monospace;
      font-size: 11px;
      line-height: 1.4;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .dashboard-idle {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 60px 20px 20px;
      color: var(--text-faint);
      font-size: 14px;
    }

    @media (max-width: 768px) {
      .side-panel.open { width: 100%; min-width: 0; position: absolute; top: 0; left: 0; right: 0; bottom: 0; z-index: 10; }
      .lane-cards { flex-wrap: wrap; }
      .task-card { min-width: 140px; }
    }
```

- [ ] **Step 2: Verify by opening the file in browser**

Open `http://127.0.0.1:8080` (or just open the HTML file). The page should load without CSS errors. Existing views should look unchanged since the new classes aren't used yet.

- [ ] **Step 3: Commit**

```bash
git add web/index.html
git commit -m "$(cat <<'EOF'
feat(ui): add dashboard CSS for swim lanes, task cards, side panel

Adds CSS classes for the new Dashboard view: health bar, swim lanes,
task cards with status-colored borders, pulsing activity dots, side
panel with slide transition, completed plan rows, and idle state.
Existing views are unaffected.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Frontend — Dashboard Navigation + State

**Files:**
- Modify: `web/index.html` (sidebar HTML + JS state variables + switchView)

**Depends on:** Task 2

- [ ] **Step 1: Add Dashboard nav item to sidebar**

In `web/index.html`, find the sidebar nav section (line 353-358). Add the Dashboard button as the **first item** after the `<div class="nav-section">Workspace</div>`. The Dashboard button should be `active` by default (remove `active` from the Projects button).

Replace this block:

```html
    <nav class="sidebar-nav" aria-label="Views">
      <div class="nav-section">Workspace</div>
      <button class="nav-item active" type="button" data-view="projects" onclick="switchView('projects')"><span class="nav-icon">P</span>Projects</button>
      <button class="nav-item" type="button" data-view="plans" onclick="switchView('plans')"><span class="nav-icon">S</span>Plans</button>
      <button class="nav-item" type="button" data-view="tasks" onclick="switchView('tasks')"><span class="nav-icon">T</span>Tasks</button>
      <button class="nav-item" type="button" data-view="logs" onclick="switchView('logs')"><span class="nav-icon">L</span>Live Logs</button>
    </nav>
```

With:

```html
    <nav class="sidebar-nav" aria-label="Views">
      <div class="nav-section">Workspace</div>
      <button class="nav-item active" type="button" data-view="dashboard" onclick="switchView('dashboard')"><span class="nav-icon">D</span>Dashboard</button>
      <button class="nav-item" type="button" data-view="projects" onclick="switchView('projects')"><span class="nav-icon">P</span>Projects</button>
      <button class="nav-item" type="button" data-view="plans" onclick="switchView('plans')"><span class="nav-icon">S</span>Plans</button>
      <button class="nav-item" type="button" data-view="tasks" onclick="switchView('tasks')"><span class="nav-icon">T</span>Tasks</button>
      <button class="nav-item" type="button" data-view="logs" onclick="switchView('logs')"><span class="nav-icon">L</span>Live Logs</button>
    </nav>
```

- [ ] **Step 2: Add dashboard state variables**

In the `<script>` section, find the existing state variables block (around line 398-409). Add dashboard-specific state variables after `let showingForm = false;`:

```javascript
    let dashboardTasks = {};       // map: planId -> tasks[]
    let dashboardTaskLogs = {};    // map: taskId -> last log line
    let selectedDashboardTaskId = null;
    let expandedCompletedPlans = new Set();
    let expandedSpecs = new Set();
    let dashboardSseSource = null;
```

- [ ] **Step 3: Update switchView to handle dashboard**

Find the `switchView` function. Replace it entirely with this version that adds the `"dashboard"` case and keeps SSE alive for dashboard too:

```javascript
    async function switchView(name) {
      currentView = name;
      showingForm = false;
      document.querySelectorAll(".nav-item").forEach(el => el.classList.toggle("active", el.dataset.view === name));
      if (eventSource && name !== "logs" && name !== "dashboard") {
        eventSource.close();
        eventSource = null;
      }
      if (name === "dashboard") {
        setTopbar("Dashboard", "+ Submit Spec");
        await loadDashboard();
      } else if (name === "projects") {
        setTopbar("Projects", "+ New Project");
        await loadProjects();
      } else if (name === "plans") {
        setTopbar("Plans", "+ Submit Spec");
        await loadPlans();
      } else if (name === "tasks") {
        setTopbar("Tasks", "Refresh");
        await renderTasks();
      } else if (name === "logs") {
        setTopbar("Live Logs", "Reconnect");
        renderLogs();
      }
    }
```

- [ ] **Step 4: Update primaryAction to handle dashboard**

Find the `primaryAction` function. Add the dashboard case at the top of the if-chain:

```javascript
    function primaryAction() {
      if (currentView === "dashboard") {
        switchView("plans");
        showingForm = true;
        renderPlansView();
      } else if (currentView === "projects") {
        showingForm = true;
        renderProjectsView();
      } else if (currentView === "plans") {
        showingForm = true;
        renderPlansView();
      } else if (currentView === "tasks") {
        renderTasks();
      } else {
        connectEvents();
      }
    }
```

- [ ] **Step 5: Change default view to dashboard**

Find the initialization code near the bottom of the `<script>` block (around line 909-918). Change `switchView('projects')` to `switchView('dashboard')`:

```javascript
    ensureToken();
    switchView("dashboard").catch(error => {
      document.getElementById("view-container").innerHTML =
        '<div class="detail-empty" style="padding:40px;text-align:center;">' +
        '<div style="margin-bottom:8px;font-weight:700;">Connection failed</div>' +
        '<div style="color:var(--text-muted);font-size:12px;margin-bottom:16px;">' + esc(error.message) + '</div>' +
        '<button class="btn btn-primary" type="button" onclick="switchView(\'dashboard\')">Retry</button></div>';
    });
    pollStatus();
    window.setInterval(pollStatus, 5000);
```

- [ ] **Step 6: Add stub loadDashboard function**

Add this stub function in the `<script>` block, after the existing `renderLogs` / `buildTaskLogSources` / SSE functions (before the `pollStatus` function). This will be fully implemented in Task 4.

```javascript
    async function loadDashboard() {
      if (!projects.length) await loadProjects();
      if (!plans.length) await loadPlans();
      const container = document.getElementById("view-container");
      container.innerHTML = '<div class="dashboard-idle">Loading dashboard...</div>';
    }
```

- [ ] **Step 7: Verify the sidebar shows Dashboard and it loads**

Start the server: `uv run uvicorn orchestrator.main:app --host 127.0.0.1 --port 8080`

Open `http://127.0.0.1:8080`. Verify:
- Dashboard nav item appears first in sidebar with "D" icon
- Dashboard is the default active view
- Topbar shows "Dashboard" with "+ Submit Spec" button
- Clicking "+ Submit Spec" navigates to Plans view with form open
- Other nav items (Projects, Plans, Tasks, Logs) still work correctly

- [ ] **Step 8: Commit**

```bash
git add web/index.html
git commit -m "$(cat <<'EOF'
feat(ui): add Dashboard nav item and switchView routing

Dashboard is now the first sidebar item and default landing page.
The switchView function routes to loadDashboard() (stub). SSE
connection stays alive for both dashboard and logs views. Primary
action button navigates to Plans form for spec submission.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Frontend — Dashboard Rendering (Health Bar + Swim Lanes + Idle State)

**Files:**
- Modify: `web/index.html` (replace `loadDashboard` stub, add rendering functions)

**Depends on:** Task 3

- [ ] **Step 1: Implement loadDashboard and renderDashboard**

Replace the stub `loadDashboard` function (added in Task 3) with the full implementation. Add all the following functions in the `<script>` block, replacing the stub and adding new functions after it. Place these before the `pollStatus` function.

```javascript
    async function loadDashboard() {
      if (!projects.length) await loadProjects();
      if (!plans.length) await loadPlans();

      // Load tasks for active and recently completed plans
      const relevantPlans = plans.filter(p => p.status === "active" || p.status === "pending" || p.status === "completed");
      const completedPlans = relevantPlans.filter(p => p.status === "completed").slice(0, 5);
      const activePlans = relevantPlans.filter(p => p.status === "active" || p.status === "pending");
      const plansToLoad = [...activePlans, ...completedPlans];

      for (const plan of plansToLoad) {
        const parentPlan = plans.find(p => p.id === plan.id);
        const projectId = parentPlan ? parentPlan.project_id || parentPlan.projectId : null;
        try {
          dashboardTasks[plan.id] = await api("GET", "/api/plans/" + plan.id + "/tasks");
        } catch (e) {
          dashboardTasks[plan.id] = [];
        }
      }

      renderDashboard();
      connectDashboardSse();
    }

    function renderDashboard() {
      const container = document.getElementById("view-container");
      const activePlans = plans.filter(p => p.status === "active" || p.status === "pending");
      const completedPlans = plans.filter(p => p.status === "completed").slice(0, 5);

      // Compute attention count
      let attentionCount = 0;
      activePlans.forEach(p => {
        if (p.status === "pending" && p.source === "autonomous") attentionCount++;
      });
      Object.values(dashboardTasks).forEach(tasks => {
        tasks.forEach(t => { if (t.status === "failed") attentionCount++; });
      });

      const healthBar = renderHealthBar(attentionCount);
      const lanesHtml = activePlans.map(p => renderSwimLane(p)).join("");
      const completedHtml = completedPlans.length ?
        '<div style="padding:12px 20px 6px;"><div class="section-label">Recently Completed</div></div>' +
        completedPlans.map(p => renderCompletedLane(p)).join("") : "";
      const idleHtml = !activePlans.length ?
        '<div class="dashboard-idle"><div style="margin-bottom:8px;font-weight:600;">No active plans</div>' +
        '<div style="font-size:12px;">Submit a spec to start orchestrating</div></div>' : "";

      container.innerHTML =
        '<div style="display:flex;flex-direction:column;height:100%;">' +
          healthBar +
          '<div class="dashboard-body">' +
            '<div class="dashboard-lanes">' + lanesHtml + idleHtml + completedHtml + '</div>' +
            '<div class="side-panel" id="dashboard-side-panel"></div>' +
          '</div>' +
        '</div>';
    }

    function renderHealthBar(attentionCount) {
      const opusDot = document.getElementById("agent-dot");
      const opusStatus = opusDot ? (opusDot.className.includes("rate_limited") ? "rate_limited" : opusDot.className.includes("connected") ? "available" : "resuming") : "available";
      const agentCount = document.getElementById("stat-agents")?.textContent || "0";
      const queueCount = document.getElementById("stat-queue")?.textContent || "0";

      return '<div class="health-bar">' +
        '<div class="health-item"><span class="health-dot ' + esc(opusStatus) + '"></span>Opus: ' + esc(opusStatus.replace("_", " ")) + '</div>' +
        '<div class="health-item">Agents: ' + esc(agentCount) + '</div>' +
        (attentionCount > 0 ? '<span class="health-attention">' + attentionCount + ' attention</span>' : '') +
        '<span class="health-spacer"></span>' +
        '<div class="health-item">Queue: ' + esc(queueCount) + '</div>' +
      '</div>';
    }

    function renderSwimLane(plan) {
      const tasks = dashboardTasks[plan.id] || [];
      const statusOrder = ["merged", "passed", "reviewing", "in_progress", "failed", "pending"];
      const sortedTasks = [...tasks].sort((a, b) => statusOrder.indexOf(a.status) - statusOrder.indexOf(b.status));

      const isExpSpec = expandedSpecs.has(plan.id);
      const approveReject = (plan.status === "pending" && plan.source === "autonomous") ?
        '<button class="btn btn-compact" type="button" onclick="dashboardApprove(\'' + esc(plan.id) + '\')">Approve</button>' +
        '<button class="btn btn-compact btn-danger" type="button" onclick="dashboardReject(\'' + esc(plan.id) + '\')">Reject</button>' : "";
      const specToggle = '<button class="btn btn-compact" type="button" onclick="toggleSpec(\'' + esc(plan.id) + '\')">' + (isExpSpec ? "Hide Spec" : "View Spec") + '</button>';

      const cards = sortedTasks.map(t => renderTaskCard(t)).join("");

      return '<div class="swim-lane">' +
        '<div class="lane-header">' + badge(plan.status) +
          '<div class="lane-spec">' + esc(plan.spec).slice(0, 100) + '</div>' +
          '<span class="lane-meta">' + esc(plan.projectName) + (plan.plan_branch_name ? " &middot; " + esc(plan.plan_branch_name) : "") + '</span>' +
          specToggle + approveReject +
        '</div>' +
        (isExpSpec ? '<div class="lane-spec-full">' + esc(plan.spec) + '</div>' : "") +
        (cards ? '<div class="lane-cards">' + cards + '</div>' : '<div style="padding:0 20px 14px;color:var(--text-faint);font-size:12px;">No tasks yet — Opus is planning...</div>') +
      '</div>';
    }

    function renderTaskCard(task) {
      const isSelected = selectedDashboardTaskId === task.id;
      const logLine = dashboardTaskLogs[task.id] || "";
      const pulseDot = task.status === "in_progress" ? '<span class="pulse-dot green"></span> ' :
                       task.status === "reviewing" ? '<span class="pulse-dot amber"></span> ' : "";
      const retryBtn = task.status === "failed" ?
        '<div style="margin-top:6px;"><button class="btn btn-compact btn-danger" type="button" onclick="event.stopPropagation();dashboardRetry(\'' + esc(task.id) + '\')">Retry</button></div>' : "";
      const prLink = task.pr_url ?
        '<a href="' + esc(task.pr_url) + '" target="_blank" rel="noreferrer" onclick="event.stopPropagation()" style="color:var(--badge-active-text);text-decoration:none;">PR</a>' : "";

      return '<div class="task-card status-' + esc(task.status) + (isSelected ? ' selected' : '') +
        '" data-task-id="' + esc(task.id) + '" onclick="openDashboardTask(\'' + esc(task.id) + '\',\'' + esc(task.plan_id) + '\')">' +
        '<div style="display:flex;align-items:center;gap:6px;">' + pulseDot + badge(task.status) + '</div>' +
        '<div class="card-title">' + esc(task.title) + '</div>' +
        '<div class="card-meta">Attempt ' + esc(task.attempt) + (prLink ? " &middot; " + prLink : "") + '</div>' +
        (task.status === "in_progress" && logLine ? '<div class="card-log-line">' + esc(logLine) + '</div>' : "") +
        retryBtn +
      '</div>';
    }

    function renderCompletedLane(plan) {
      const isExp = expandedCompletedPlans.has(plan.id);
      const tasks = dashboardTasks[plan.id] || [];
      const mergedCount = tasks.filter(t => t.status === "merged").length;
      const totalCount = tasks.length;

      const cards = isExp ? '<div class="lane-cards">' + tasks.map(t => renderTaskCard(t)).join("") + '</div>' : "";

      return '<div class="completed-lane' + (isExp ? " expanded" : "") + '">' +
        '<div class="completed-header" onclick="toggleCompletedPlan(\'' + esc(plan.id) + '\')">' +
          '<span class="disclosure' + (isExp ? " open" : "") + '">&#9656;</span>' +
          badge("completed") +
          '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + esc(plan.spec).slice(0, 80) + '</span>' +
          '<span class="completed-meta">' + mergedCount + '/' + totalCount + ' merged</span>' +
          '<span class="completed-meta">' + timeAgo(plan.created_at) + '</span>' +
        '</div>' + cards +
      '</div>';
    }

    function timeAgo(isoString) {
      if (!isoString) return "";
      const diff = Date.now() - new Date(isoString).getTime();
      const mins = Math.floor(diff / 60000);
      if (mins < 1) return "just now";
      if (mins < 60) return mins + "m ago";
      const hrs = Math.floor(mins / 60);
      if (hrs < 24) return hrs + "h ago";
      return Math.floor(hrs / 24) + "d ago";
    }
```

- [ ] **Step 2: Add dashboard action handlers**

Add these functions right after the rendering functions above:

```javascript
    function toggleSpec(planId) {
      if (expandedSpecs.has(planId)) expandedSpecs.delete(planId);
      else expandedSpecs.add(planId);
      renderDashboard();
    }

    function toggleCompletedPlan(planId) {
      if (expandedCompletedPlans.has(planId)) expandedCompletedPlans.delete(planId);
      else expandedCompletedPlans.add(planId);
      renderDashboard();
    }

    async function dashboardApprove(planId) {
      await api("POST", "/api/plans/" + planId + "/approve");
      await loadDashboard();
    }

    async function dashboardReject(planId) {
      await api("POST", "/api/plans/" + planId + "/reject");
      await loadDashboard();
    }

    async function dashboardRetry(taskId) {
      await api("POST", "/api/tasks/" + taskId + "/retry");
      // Re-fetch tasks for the plan that owns this task
      for (const planId of Object.keys(dashboardTasks)) {
        const tasks = dashboardTasks[planId];
        if (tasks.some(t => t.id === taskId)) {
          dashboardTasks[planId] = await api("GET", "/api/plans/" + planId + "/tasks");
          break;
        }
      }
      renderDashboard();
    }
```

- [ ] **Step 3: Verify the dashboard renders**

Start the server and open `http://127.0.0.1:8080`. Verify:
- Health bar shows at top with Opus status, agent count, queue count
- If there are active plans, they appear as swim lanes with task cards
- If no active plans, "No active plans" idle message shows
- Completed plans show below with disclosure triangles
- Clicking a disclosure triangle expands/collapses the completed plan
- "View Spec" toggle works on active plan lanes

- [ ] **Step 4: Commit**

```bash
git add web/index.html
git commit -m "$(cat <<'EOF'
feat(ui): implement dashboard rendering with swim lanes and health bar

Adds loadDashboard(), renderDashboard(), renderSwimLane(),
renderTaskCard(), renderCompletedLane(), and all dashboard action
handlers (approve, reject, retry, toggle spec, toggle completed).
Health bar shows Opus status, agent count, and attention items.
Task cards are color-coded by status with pulsing dots for active work.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Frontend — Task Detail Side Panel

**Files:**
- Modify: `web/index.html` (add side panel rendering + open/close logic)

**Depends on:** Task 4

- [ ] **Step 1: Implement side panel open/close and rendering**

Add these functions in the `<script>` block, after the dashboard action handlers from Task 4:

```javascript
    function openDashboardTask(taskId, planId) {
      selectedDashboardTaskId = taskId;
      const tasks = dashboardTasks[planId] || [];
      const task = tasks.find(t => t.id === taskId);
      if (!task) return;

      const panel = document.getElementById("dashboard-side-panel");
      if (!panel) return;

      const stopBtn = task.status === "in_progress" ?
        '<button class="btn btn-compact btn-danger" type="button" onclick="dashboardStop(\'' + esc(task.id) + '\',\'' + esc(planId) + '\')">Stop</button>' : "";
      const retryBtn = task.status === "failed" ?
        '<button class="btn btn-compact btn-danger" type="button" onclick="dashboardRetry(\'' + esc(task.id) + '\')">Retry</button>' : "";
      const logsBtn = '<button class="btn btn-compact" type="button" onclick="viewTaskLogs(\'' + esc(task.id) + '\')">View Full Logs</button>';

      panel.innerHTML =
        '<div class="side-panel-header">' +
          '<div><div class="detail-title" style="font-size:14px;">' + esc(task.title) + '</div>' +
          '<div class="detail-subtitle">' + esc(task.branch_name) + '</div></div>' +
          '<button class="side-panel-close" type="button" onclick="closeDashboardPanel()">&times;</button>' +
        '</div>' +
        '<div class="side-panel-content">' +
          '<div class="detail-section"><div class="detail-section-title">Details</div><div class="detail-card">' +
            '<div class="detail-field"><span class="field-label">Status</span><span class="field-value">' + badge(task.status) + '</span></div>' +
            '<div class="detail-field"><span class="field-label">Attempt</span><span class="field-value">' + esc(task.attempt) + '</span></div>' +
            '<div class="detail-field"><span class="field-label">PR</span><span class="field-value">' +
              (task.pr_url ? '<a href="' + esc(task.pr_url) + '" target="_blank" rel="noreferrer">View PR</a>' : '-') + '</span></div>' +
            '<div class="detail-field"><span class="field-label">Created</span><span class="field-value">' + esc(task.created_at) + '</span></div>' +
            '<div class="detail-field"><span class="field-label">Updated</span><span class="field-value">' + esc(task.updated_at) + '</span></div>' +
          '</div></div>' +
          (task.description ? '<div class="detail-section"><div class="detail-section-title">Description</div>' +
            '<div class="detail-card" style="white-space:pre-wrap;font-size:12px;line-height:1.55;">' + esc(task.description) + '</div></div>' : '') +
          (task.review_feedback ? '<div class="detail-section"><div class="detail-section-title">Review Feedback</div>' +
            '<div class="detail-card" style="white-space:pre-wrap;font-size:11px;font-family:Cascadia Mono, SF Mono, Consolas, monospace;line-height:1.45;">' + esc(task.review_feedback) + '</div></div>' : '') +
          '<div class="detail-section"><div class="detail-section-title">Live Log</div>' +
            '<div class="side-panel-log" id="dashboard-log-tail"></div></div>' +
        '</div>' +
        '<div class="side-panel-actions">' + stopBtn + retryBtn + logsBtn + '</div>';

      panel.classList.add("open");

      // Re-render cards to show selection
      renderDashboard();
      // Re-open panel (renderDashboard resets the container)
      reopenDashboardPanel(task, planId);
    }

    function reopenDashboardPanel(task, planId) {
      // After renderDashboard, re-inject panel content since container was rebuilt
      const panel = document.getElementById("dashboard-side-panel");
      if (!panel) return;
      // Rebuild panel content
      openDashboardTask(task.id, planId);
    }
```

Wait — the above has a recursive call. Let me fix the approach. The `renderDashboard` function rebuilds the entire container including the side panel div. So instead of calling `renderDashboard()` inside `openDashboardTask`, we should separate the panel rendering from the full re-render. Here's the corrected approach:

Replace the `openDashboardTask` and remove `reopenDashboardPanel`. Use this version instead:

```javascript
    function openDashboardTask(taskId, planId) {
      selectedDashboardTaskId = taskId;
      const tasks = dashboardTasks[planId] || [];
      const task = tasks.find(t => t.id === taskId);
      if (!task) return;

      // Update card selection styling without full re-render
      document.querySelectorAll(".task-card").forEach(el => {
        el.classList.toggle("selected", el.dataset.taskId === taskId);
      });

      renderSidePanel(task);
    }

    function renderSidePanel(task) {
      const panel = document.getElementById("dashboard-side-panel");
      if (!panel) return;

      const stopBtn = task.status === "in_progress" ?
        '<button class="btn btn-compact btn-danger" type="button" onclick="dashboardStop(\'' + esc(task.id) + '\')">Stop</button>' : "";
      const retryBtn = task.status === "failed" ?
        '<button class="btn btn-compact btn-danger" type="button" onclick="dashboardRetry(\'' + esc(task.id) + '\')">Retry</button>' : "";
      const logsBtn = '<button class="btn btn-compact" type="button" onclick="viewTaskLogs(\'' + esc(task.id) + '\')">View Full Logs</button>';

      panel.innerHTML =
        '<div class="side-panel-header">' +
          '<div><div class="detail-title" style="font-size:14px;">' + esc(task.title) + '</div>' +
          '<div class="detail-subtitle">' + esc(task.branch_name) + '</div></div>' +
          '<button class="side-panel-close" type="button" onclick="closeDashboardPanel()">&times;</button>' +
        '</div>' +
        '<div class="side-panel-content">' +
          '<div class="detail-section"><div class="detail-section-title">Details</div><div class="detail-card">' +
            '<div class="detail-field"><span class="field-label">Status</span><span class="field-value">' + badge(task.status) + '</span></div>' +
            '<div class="detail-field"><span class="field-label">Attempt</span><span class="field-value">' + esc(task.attempt) + '</span></div>' +
            '<div class="detail-field"><span class="field-label">PR</span><span class="field-value">' +
              (task.pr_url ? '<a href="' + esc(task.pr_url) + '" target="_blank" rel="noreferrer">View PR</a>' : '-') + '</span></div>' +
            '<div class="detail-field"><span class="field-label">Created</span><span class="field-value">' + esc(task.created_at) + '</span></div>' +
            '<div class="detail-field"><span class="field-label">Updated</span><span class="field-value">' + esc(task.updated_at) + '</span></div>' +
          '</div></div>' +
          (task.description ? '<div class="detail-section"><div class="detail-section-title">Description</div>' +
            '<div class="detail-card" style="white-space:pre-wrap;font-size:12px;line-height:1.55;">' + esc(task.description) + '</div></div>' : '') +
          (task.review_feedback ? '<div class="detail-section"><div class="detail-section-title">Review Feedback</div>' +
            '<div class="detail-card" style="white-space:pre-wrap;font-size:11px;font-family:Cascadia Mono, SF Mono, Consolas, monospace;line-height:1.45;">' + esc(task.review_feedback) + '</div></div>' : '') +
          '<div class="detail-section"><div class="detail-section-title">Live Log</div>' +
            '<div class="side-panel-log" id="dashboard-log-tail"></div></div>' +
        '</div>' +
        '<div class="side-panel-actions">' + stopBtn + retryBtn + logsBtn + '</div>';

      panel.classList.add("open");
    }

    function closeDashboardPanel() {
      selectedDashboardTaskId = null;
      const panel = document.getElementById("dashboard-side-panel");
      if (panel) {
        panel.classList.remove("open");
        panel.innerHTML = "";
      }
      document.querySelectorAll(".task-card").forEach(el => el.classList.remove("selected"));
    }

    async function dashboardStop(taskId) {
      await api("POST", "/api/tasks/" + taskId + "/stop");
      for (const planId of Object.keys(dashboardTasks)) {
        const tasks = dashboardTasks[planId];
        if (tasks.some(t => t.id === taskId)) {
          dashboardTasks[planId] = await api("GET", "/api/plans/" + planId + "/tasks");
          break;
        }
      }
      closeDashboardPanel();
      renderDashboard();
    }
```

- [ ] **Step 2: Verify side panel works**

Start server, open dashboard. If you have tasks in the database:
- Click a task card → side panel slides in from right
- Panel shows task title, branch, status, attempt, description, review feedback, log area
- Click the X button → panel closes
- Stop/Retry buttons appear based on task status
- "View Full Logs" navigates to Logs view

- [ ] **Step 3: Commit**

```bash
git add web/index.html
git commit -m "$(cat <<'EOF'
feat(ui): add dashboard task detail side panel

Clicking a task card opens a 35% width slide-in panel showing task
metadata, description, review feedback, and live log area. Includes
Stop, Retry, and View Full Logs actions. Panel closes via X button.
Card selection is highlighted without full re-render.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Frontend — SSE Live Updates on Dashboard

**Files:**
- Modify: `web/index.html` (add SSE connection + event handlers for dashboard)

**Depends on:** Task 4, Task 5

- [ ] **Step 1: Implement dashboard SSE connection**

Add this function after the `closeDashboardPanel` function:

```javascript
    function connectDashboardSse() {
      if (eventSource) eventSource.close();
      eventSource = new EventSource(API + "/api/events?token=" + encodeURIComponent(ensureToken()));

      eventSource.onerror = () => {
        const bar = document.querySelector(".health-bar");
        if (bar && !bar.querySelector(".reconnecting")) {
          const span = document.createElement("span");
          span.className = "reconnecting";
          span.style.cssText = "color:var(--badge-reviewing-text);font-size:11px;";
          span.textContent = "reconnecting...";
          bar.appendChild(span);
        }
      };

      eventSource.onopen = () => {
        const recon = document.querySelector(".reconnecting");
        if (recon) recon.remove();
      };

      // Handle events that update dashboard state
      eventSource.addEventListener("agent_log", event => {
        try {
          const data = JSON.parse(event.data);
          const taskId = data.task_id;
          if (!taskId) return;

          // Update last log line on card
          const logText = data.logs || data.line || "";
          const lines = logText.split("\n").filter(l => l.trim());
          if (lines.length) {
            dashboardTaskLogs[taskId] = lines[lines.length - 1];
            // Update card log line in-place
            const cards = document.querySelectorAll(".task-card");
            cards.forEach(card => {
              if (card.dataset.taskId === taskId) {
                let logEl = card.querySelector(".card-log-line");
                if (!logEl) {
                  logEl = document.createElement("div");
                  logEl.className = "card-log-line";
                  card.appendChild(logEl);
                }
                logEl.textContent = lines[lines.length - 1];
              }
            });
          }

          // Append to side panel log if open for this task
          if (selectedDashboardTaskId === taskId) {
            const logTail = document.getElementById("dashboard-log-tail");
            if (logTail) {
              logTail.textContent += logText + "\n";
              logTail.scrollTop = logTail.scrollHeight;
            }
          }
        } catch (e) { /* ignore parse errors */ }
      });

      const refreshEvents = ["plan_activated", "agent_dispatched", "review_completed",
        "task_completed", "task_failed", "task_retry", "improvement_proposed"];
      refreshEvents.forEach(type => {
        eventSource.addEventListener(type, () => {
          if (currentView === "dashboard") loadDashboard();
        });
      });

      eventSource.addEventListener("opus_queued", event => {
        try {
          const data = JSON.parse(event.data);
          const queueEl = document.getElementById("stat-queue");
          if (queueEl && data.queued_count != null) queueEl.textContent = data.queued_count;
        } catch (e) { /* ignore */ }
      });
    }
```

- [ ] **Step 2: Update switchView to disconnect dashboard SSE when leaving**

The `switchView` function (modified in Task 3) already keeps SSE alive for dashboard. But when leaving dashboard, we should close it. The current logic is:

```javascript
if (eventSource && name !== "logs" && name !== "dashboard") {
  eventSource.close();
  eventSource = null;
}
```

This is correct — SSE stays alive for both `logs` and `dashboard`, and closes for other views. No change needed here.

- [ ] **Step 3: Verify live updates**

This requires a running orchestrator with actual agent activity, which may not be available locally. To verify the SSE connection:

1. Start the server
2. Open `http://127.0.0.1:8080` (dashboard view)
3. Open browser DevTools → Network tab → filter by "EventStream"
4. Verify an SSE connection to `/api/events` is established
5. You should see periodic `ping` events (every 30s)

If you have test data, verify:
- In-progress task cards show pulsing green dot
- When a task status changes, the dashboard auto-refreshes
- Side panel log tail updates in real-time for selected task

- [ ] **Step 4: Commit**

```bash
git add web/index.html
git commit -m "$(cat <<'EOF'
feat(ui): connect dashboard to SSE for live updates

Dashboard subscribes to /api/events on mount. agent_log events update
task card log lines in-place and append to side panel log tail.
Status-changing events (plan_activated, task_completed, task_failed,
review_completed, etc.) trigger a full dashboard reload. Reconnection
indicator shows in health bar on SSE disconnect.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Integration — Final Verification and Cleanup

**Files:**
- Modify: `web/index.html` (minor fixes if needed)

**Depends on:** Task 1, Task 6

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest --cov=orchestrator --cov-report=term-missing -v`

Expected: All tests pass. Coverage at or above 88%.

- [ ] **Step 2: Lint and format**

Run: `uv run ruff format src/ tests/ && uv run ruff check --fix src/ tests/`

Expected: No errors.

- [ ] **Step 3: Type check**

Run: `uv run mypy src/orchestrator/ --ignore-missing-imports`

Expected: No errors.

- [ ] **Step 4: Manual verification**

Start server: `uv run uvicorn orchestrator.main:app --host 127.0.0.1 --port 8080`

Verify the following checklist:

**Navigation:**
- [ ] Dashboard is the default landing page
- [ ] Dashboard nav item shows "D" icon, highlighted by default
- [ ] Clicking Projects/Plans/Tasks/Logs still works correctly
- [ ] Clicking back to Dashboard re-renders correctly
- [ ] "+ Submit Spec" navigates to Plans view with form open

**Health bar:**
- [ ] Shows Opus status with colored dot
- [ ] Shows agent count and queue count
- [ ] Attention badge appears when there are failed tasks or pending approvals
- [ ] Attention badge hidden when count is 0

**Swim lanes (if active plans exist):**
- [ ] Each active plan gets its own lane with header and task cards
- [ ] Task cards are ordered by status (merged → pending)
- [ ] Cards have correct color-coded left borders
- [ ] In-progress cards show pulsing green dot
- [ ] Reviewing cards show pulsing amber dot
- [ ] Failed cards show red tint and Retry button
- [ ] "View Spec" toggles full spec text

**Side panel:**
- [ ] Clicking a task card opens the side panel
- [ ] Panel shows task metadata, description, review feedback, log area
- [ ] X button closes the panel
- [ ] Stop/Retry buttons work correctly
- [ ] "View Full Logs" navigates to Logs view

**Completed plans:**
- [ ] Show below active lanes with disclosure triangles
- [ ] Collapsed by default with reduced opacity
- [ ] Clicking expands to show task cards
- [ ] Clicking again collapses

**Idle state:**
- [ ] When no active plans, shows "No active plans" message
- [ ] Health bar still visible
- [ ] Completed plans still show

**Theme:**
- [ ] Light theme renders correctly
- [ ] Dark theme renders correctly (toggle via T button)

**Retry endpoint:**
- [ ] `POST /api/tasks/{id}/retry` returns 200 for failed tasks
- [ ] Returns 409 for non-failed tasks
- [ ] Returns 404 for nonexistent tasks

- [ ] **Step 5: Commit if any fixes were needed**

```bash
git add -A
git commit -m "$(cat <<'EOF'
fix(ui): dashboard integration fixes

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

Only commit if changes were made. Skip if everything passed cleanly.

---

## Parallel Execution Map

- **Wave 1:** Task 1, Task 2 (no dependencies — run in parallel)
- **Wave 2:** Task 3 (depends on Task 2)
- **Wave 3:** Task 4 (depends on Task 3)
- **Wave 4:** Task 5 (depends on Task 4)
- **Wave 5:** Task 6 (depends on Task 4, Task 5)
- **Wave 6:** Task 7 (depends on Task 1, Task 6)
