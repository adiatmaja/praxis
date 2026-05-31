# Plan 5: Web Dashboard, SSE Streaming, Autonomous Improvement Loop

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the single-file web dashboard with real-time SSE log streaming, the orchestration loop that ties planning → dispatch → review → merge together, and the autonomous improvement cycle with confidence scoring and approval gates.

**Architecture:** Single HTML file served by FastAPI's `StaticFiles`. SSE via `sse-starlette`. The orchestration loop runs as a background task using `asyncio.create_task`. The improvement loop is a method on a new `Orchestrator` class that coordinates all core modules.

**Tech Stack:** Python 3.11, FastAPI, sse-starlette, asyncio, HTML/CSS/JS (single file)

---

## Full Project Context

This is **Plan 5 of 5** (final) for the AI Agent Orchestrator.

**What Plans 1-4 built:**
- `src/orchestrator/config.py` — `Settings(auth_token, github_token, database_url, lm_studio_url, host, port)`
- `src/orchestrator/database.py` — `Database` with async SQLite
- `src/orchestrator/models/schemas.py` — All Pydantic models including `TaskStatus`, `PlanStatus`, `OpusStatus`, `OpusPlanPayload`, `OpusReviewPayload`, `OpusImprovementPayload`
- `src/orchestrator/core/task_queue.py` — `TaskQueue(db)` — plan/task CRUD, state transitions, dependency-aware dispatch, `all_tasks_done()`, `get_dispatchable_tasks()`
- `src/orchestrator/core/git_ops.py` — `GitOps(github_token)` — `clone_repo()`, `create_branch()`, `push_branch()`, `create_pr()`, `merge_pr()`, `comment_on_pr()`, `get_pr_diff()`, `extract_pr_number()`
- `src/orchestrator/core/opus_bridge.py` — `OpusBridge(db)` — `plan_spec()`, `review_diff()`, `analyze_improvements()`, `is_available()`, `queue_action()`, `get_queued_actions()`, `clear_queued_actions()`
- `src/orchestrator/core/agent_manager.py` — `AgentManager(lm_studio_url, github_token)` — `spawn_agent()`, `get_container_status()`, `get_container_logs()`, `stop_agent()`, `cleanup_container()`, `list_agent_containers()`
- `src/orchestrator/api/` — REST endpoints: projects, plans, tasks, system, internal callback
- `src/orchestrator/main.py` — FastAPI app with lifespan, core objects in `app.state`
- `cli/main.py` — Typer CLI client
- `docker/` — Orchestrator + Aider agent Dockerfiles, docker-compose.yml, Caddyfile

**Key API state objects (set in main.py lifespan):**
- `app.state.db` — Database instance
- `app.state.settings` — Settings instance
- `app.state.task_queue` — TaskQueue instance
- `app.state.opus_bridge` — OpusBridge instance
- `app.state.agent_manager` — AgentManager instance

**Callback flow:** Agent containers call `POST /api/internal/agent-done` with `{task_id, run_id, status, pr_url}`. This sets task to `REVIEWING` status.

**Rate limit handling:** `OpusBridge._check_and_handle_rate_limit()` sets `opus_state.status = 'rate_limited'`, `resume_at = now + 5h1m`. `OpusBridge.is_available()` checks if limit has expired.

**Per-project settings from DB:**
- `approval_gate` (bool, default True)
- `confidence_threshold` (float, default 0.7)
- `max_retries` (int, default 3)
- `max_improvement_cycles` (int, default 5)

**Design spec:** `docs/superpowers/specs/2026-06-01-praxis-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `src/orchestrator/core/orchestrator.py` | Create | Main orchestration loop: plan → dispatch → review → merge → improve |
| `src/orchestrator/api/events.py` | Create | SSE event streaming endpoint |
| `src/orchestrator/core/event_bus.py` | Create | In-memory event bus for SSE broadcasting |
| `web/index.html` | Create | Single-file web dashboard |
| `src/orchestrator/main.py` | Modify | Add static files mount, start orchestration loop, add event bus |
| `tests/test_orchestrator.py` | Create | Orchestration loop tests |
| `tests/test_event_bus.py` | Create | Event bus tests |

---

### Task 1: Event Bus

**Files:**
- Create: `src/orchestrator/core/event_bus.py`
- Create: `tests/test_event_bus.py`

**Depends on:** None

- [ ] **Step 1: Write failing tests**

Create file `tests/test_event_bus.py`:
```python
import asyncio

import pytest

from orchestrator.core.event_bus import EventBus


@pytest.mark.unit
class TestEventBus:
    async def test_subscribe_and_receive(self) -> None:
        bus = EventBus()
        queue = bus.subscribe()
        bus.publish({"type": "task_started", "task_id": "t1"})
        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert event["type"] == "task_started"
        assert event["task_id"] == "t1"

    async def test_multiple_subscribers(self) -> None:
        bus = EventBus()
        q1 = bus.subscribe()
        q2 = bus.subscribe()
        bus.publish({"type": "test", "data": "hello"})
        e1 = await asyncio.wait_for(q1.get(), timeout=1.0)
        e2 = await asyncio.wait_for(q2.get(), timeout=1.0)
        assert e1["data"] == "hello"
        assert e2["data"] == "hello"

    async def test_unsubscribe(self) -> None:
        bus = EventBus()
        queue = bus.subscribe()
        bus.unsubscribe(queue)
        bus.publish({"type": "test"})
        assert queue.empty()

    async def test_publish_with_no_subscribers(self) -> None:
        bus = EventBus()
        bus.publish({"type": "test"})  # Should not raise

    async def test_subscriber_count(self) -> None:
        bus = EventBus()
        q1 = bus.subscribe()
        q2 = bus.subscribe()
        assert bus.subscriber_count == 2
        bus.unsubscribe(q1)
        assert bus.subscriber_count == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_event_bus.py -v
```

Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement event bus**

Create file `src/orchestrator/core/event_bus.py`:
```python
import asyncio
import logging
from datetime import datetime, timezone


logger = logging.getLogger(__name__)


class EventBus:
    """In-memory pub/sub event bus for SSE broadcasting."""

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(queue)
        logger.debug("New subscriber, total: %d", len(self._subscribers))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)
            logger.debug("Subscriber removed, total: %d", len(self._subscribers))

    def publish(self, event: dict) -> None:
        if "timestamp" not in event:
            event["timestamp"] = datetime.now(timezone.utc).isoformat()
        for queue in self._subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("Subscriber queue full, dropping event")

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_event_bus.py -v
```

Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/event_bus.py tests/test_event_bus.py
git commit -m "feat: add in-memory event bus for SSE broadcasting"
```

---

### Task 2: SSE Events Endpoint

**Files:**
- Create: `src/orchestrator/api/events.py`
- Modify: `src/orchestrator/main.py`

**Depends on:** Task 1

- [ ] **Step 1: Implement SSE events endpoint**

Create file `src/orchestrator/api/events.py`:
```python
import asyncio
import json

from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse

from orchestrator.api.auth import verify_token


router = APIRouter(tags=["events"], dependencies=[Depends(verify_token)])


@router.get("/events")
async def global_events(request: Request) -> EventSourceResponse:
    event_bus = request.app.state.event_bus

    async def event_generator():
        queue = event_bus.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield {
                        "event": event.get("type", "message"),
                        "data": json.dumps(event),
                    }
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": ""}
        finally:
            event_bus.unsubscribe(queue)

    return EventSourceResponse(event_generator())


@router.get("/tasks/{task_id}/logs")
async def task_logs(request: Request, task_id: str) -> EventSourceResponse:
    event_bus = request.app.state.event_bus

    async def log_generator():
        queue = event_bus.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    if event.get("type") == "agent_log" and event.get("task_id") == task_id:
                        yield {
                            "event": "log",
                            "data": json.dumps(event),
                        }
                    elif event.get("type") in ("task_completed", "task_failed") and event.get("task_id") == task_id:
                        yield {
                            "event": event["type"],
                            "data": json.dumps(event),
                        }
                        break
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": ""}
        finally:
            event_bus.unsubscribe(queue)

    return EventSourceResponse(log_generator())
```

- [ ] **Step 2: Update main.py to add event bus and events router**

Read `src/orchestrator/main.py` first, then add these changes:

After `from orchestrator.core.opus_bridge import OpusBridge` add:
```python
from orchestrator.core.event_bus import EventBus
```

Inside the lifespan function, after `app.state.agent_manager = ...`, add:
```python
    app.state.event_bus = EventBus()
```

After the existing router imports, add:
```python
from orchestrator.api.events import router as events_router  # noqa: E402
```

After `app.include_router(internal_router, ...)`, add:
```python
app.include_router(events_router, prefix="/api")
```

At the end of the file (after the health endpoint), add static file serving:
```python
from pathlib import Path

web_dir = Path(__file__).parent.parent.parent / "web"
if web_dir.exists():
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")
```

- [ ] **Step 3: Commit**

```bash
git add src/orchestrator/api/events.py src/orchestrator/main.py
git commit -m "feat: add SSE events endpoint and event bus to app state"
```

---

### Task 3: Orchestration Loop

**Files:**
- Create: `src/orchestrator/core/orchestrator.py`
- Create: `tests/test_orchestrator.py`

**Depends on:** Task 1

- [ ] **Step 1: Write failing tests**

Create file `tests/test_orchestrator.py`:
```python
import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.event_bus import EventBus
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus, PlanStatus


async def _setup(db: Database) -> tuple[TaskQueue, str, str]:
    """Create user, project, plan with tasks. Return (tq, plan_id, task_id)."""
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u1", "User", "hash"),
    )
    await db.execute(
        """INSERT INTO projects (id, user_id, name, repo_url, model_name, max_retries)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("p1", "u1", "App", "https://github.com/u/a", "deepseek", 3),
    )
    tq = TaskQueue(db)
    plan_id = await tq.create_plan("p1", "Build auth")
    opus_plan = {
        "plan_summary": "Auth",
        "plan_slug": "auth",
        "tasks": [
            {"title": "Login", "slug": "login", "description": "Build login", "depends_on": []},
        ],
    }
    await tq.activate_plan(plan_id, opus_plan, "plan/2026-06-01-auth")
    tasks = await tq.get_tasks_for_plan(plan_id)
    return tq, plan_id, tasks[0]["id"]


@pytest.mark.integration
class TestOrchestrationDispatch:
    async def test_dispatch_pending_tasks(self, db: Database) -> None:
        tq, plan_id, task_id = await _setup(db)
        event_bus = EventBus()
        mock_agent_manager = MagicMock()
        mock_agent_manager.spawn_agent.return_value = "container-123"

        project = await db.fetch_one("SELECT * FROM projects WHERE id = 'p1'")
        orch = Orchestrator(
            task_queue=tq,
            agent_manager=mock_agent_manager,
            opus_bridge=AsyncMock(),
            git_ops=AsyncMock(),
            event_bus=event_bus,
        )
        await orch.dispatch_pending_tasks(plan_id, project)
        mock_agent_manager.spawn_agent.assert_called_once()
        task = await tq.get_task(task_id)
        assert task["status"] == TaskStatus.IN_PROGRESS

    async def test_dispatch_skips_non_pending(self, db: Database) -> None:
        tq, plan_id, task_id = await _setup(db)
        await tq.update_task_status(task_id, TaskStatus.IN_PROGRESS)
        event_bus = EventBus()
        mock_agent_manager = MagicMock()

        project = await db.fetch_one("SELECT * FROM projects WHERE id = 'p1'")
        orch = Orchestrator(
            task_queue=tq,
            agent_manager=mock_agent_manager,
            opus_bridge=AsyncMock(),
            git_ops=AsyncMock(),
            event_bus=event_bus,
        )
        await orch.dispatch_pending_tasks(plan_id, project)
        mock_agent_manager.spawn_agent.assert_not_called()


@pytest.mark.integration
class TestOrchestrationReview:
    async def test_review_pass_merges(self, db: Database) -> None:
        tq, plan_id, task_id = await _setup(db)
        await tq.update_task_status(task_id, TaskStatus.IN_PROGRESS)
        await tq.update_task_status(task_id, TaskStatus.REVIEWING)
        await tq.set_task_pr_url(task_id, "https://github.com/u/a/pull/1")

        event_bus = EventBus()
        mock_opus = AsyncMock()
        mock_opus.review_diff.return_value = {
            "verdict": "pass",
            "feedback": "Looks good",
            "issues": [],
        }
        mock_opus.is_available.return_value = True
        mock_git = AsyncMock()
        mock_git.get_pr_diff.return_value = "diff content"
        mock_git.extract_pr_number = AsyncMock(return_value=1)

        project = await db.fetch_one("SELECT * FROM projects WHERE id = 'p1'")
        orch = Orchestrator(
            task_queue=tq,
            agent_manager=MagicMock(),
            opus_bridge=mock_opus,
            git_ops=mock_git,
            event_bus=event_bus,
        )
        await orch.review_task(task_id, project)
        task = await tq.get_task(task_id)
        assert task["status"] == TaskStatus.MERGED
        mock_git.merge_pr.assert_called_once()

    async def test_review_fail_retries(self, db: Database) -> None:
        tq, plan_id, task_id = await _setup(db)
        await tq.update_task_status(task_id, TaskStatus.IN_PROGRESS)
        await tq.update_task_status(task_id, TaskStatus.REVIEWING)
        await tq.set_task_pr_url(task_id, "https://github.com/u/a/pull/1")

        event_bus = EventBus()
        mock_opus = AsyncMock()
        mock_opus.review_diff.return_value = {
            "verdict": "fail",
            "feedback": "Missing validation",
            "issues": ["No email check"],
        }
        mock_opus.is_available.return_value = True
        mock_git = AsyncMock()
        mock_git.get_pr_diff.return_value = "diff content"
        mock_git.extract_pr_number = AsyncMock(return_value=1)

        project = await db.fetch_one("SELECT * FROM projects WHERE id = 'p1'")
        orch = Orchestrator(
            task_queue=tq,
            agent_manager=MagicMock(),
            opus_bridge=mock_opus,
            git_ops=mock_git,
            event_bus=event_bus,
        )
        await orch.review_task(task_id, project)
        task = await tq.get_task(task_id)
        assert task["status"] == TaskStatus.PENDING
        assert task["attempt"] == 2
        mock_git.comment_on_pr.assert_called_once()

    async def test_review_fail_max_retries_exhausted(self, db: Database) -> None:
        tq, plan_id, task_id = await _setup(db)
        # Set attempt to max
        await db.execute("UPDATE tasks SET attempt = 3 WHERE id = ?", (task_id,))
        await tq.update_task_status(task_id, TaskStatus.IN_PROGRESS)
        await tq.update_task_status(task_id, TaskStatus.REVIEWING)
        await tq.set_task_pr_url(task_id, "https://github.com/u/a/pull/1")

        event_bus = EventBus()
        mock_opus = AsyncMock()
        mock_opus.review_diff.return_value = {
            "verdict": "fail",
            "feedback": "Still broken",
            "issues": ["Bug"],
        }
        mock_opus.is_available.return_value = True
        mock_git = AsyncMock()
        mock_git.get_pr_diff.return_value = "diff"
        mock_git.extract_pr_number = AsyncMock(return_value=1)

        project = await db.fetch_one("SELECT * FROM projects WHERE id = 'p1'")
        orch = Orchestrator(
            task_queue=tq,
            agent_manager=MagicMock(),
            opus_bridge=mock_opus,
            git_ops=mock_git,
            event_bus=event_bus,
        )
        await orch.review_task(task_id, project)
        task = await tq.get_task(task_id)
        # Should stay failed, not retried
        assert task["status"] == TaskStatus.FAILED


@pytest.mark.integration
class TestImprovementLoop:
    async def test_triggers_improvement_when_all_done(self, db: Database) -> None:
        tq, plan_id, task_id = await _setup(db)
        await tq.update_task_status(task_id, TaskStatus.IN_PROGRESS)
        await tq.update_task_status(task_id, TaskStatus.REVIEWING)
        await tq.update_task_status(task_id, TaskStatus.PASSED)
        await tq.update_task_status(task_id, TaskStatus.MERGED)

        event_bus = EventBus()
        mock_opus = AsyncMock()
        mock_opus.analyze_improvements.return_value = {
            "confidence": 0.85,
            "reason": "Missing tests",
            "proposed_tasks": [
                {"title": "Add tests", "slug": "improve-tests", "description": "Write tests"},
            ],
        }
        mock_opus.is_available.return_value = True

        project = await db.fetch_one("SELECT * FROM projects WHERE id = 'p1'")
        orch = Orchestrator(
            task_queue=tq,
            agent_manager=MagicMock(),
            opus_bridge=mock_opus,
            git_ops=AsyncMock(),
            event_bus=event_bus,
        )
        result = await orch.check_improvements(plan_id, project)
        assert result is not None
        assert result["confidence"] == 0.85

    async def test_no_improvement_below_threshold(self, db: Database) -> None:
        tq, plan_id, task_id = await _setup(db)
        await tq.update_task_status(task_id, TaskStatus.IN_PROGRESS)
        await tq.update_task_status(task_id, TaskStatus.REVIEWING)
        await tq.update_task_status(task_id, TaskStatus.PASSED)
        await tq.update_task_status(task_id, TaskStatus.MERGED)

        event_bus = EventBus()
        mock_opus = AsyncMock()
        mock_opus.analyze_improvements.return_value = {
            "confidence": 0.3,
            "reason": "Minor cosmetic changes",
            "proposed_tasks": [],
        }
        mock_opus.is_available.return_value = True

        project = await db.fetch_one("SELECT * FROM projects WHERE id = 'p1'")
        orch = Orchestrator(
            task_queue=tq,
            agent_manager=MagicMock(),
            opus_bridge=mock_opus,
            git_ops=AsyncMock(),
            event_bus=event_bus,
        )
        result = await orch.check_improvements(plan_id, project)
        assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_orchestrator.py -v
```

Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement orchestrator**

Create file `src/orchestrator/core/orchestrator.py`:
```python
import json
import logging
from datetime import date

from orchestrator.core.agent_manager import AgentManager
from orchestrator.core.event_bus import EventBus
from orchestrator.core.git_ops import GitOps
from orchestrator.core.opus_bridge import OpusBridge
from orchestrator.core.task_queue import TaskQueue
from orchestrator.models.schemas import PlanStatus, TaskStatus


logger = logging.getLogger(__name__)


class Orchestrator:
    """Coordinates the full plan → dispatch → review → merge → improve loop."""

    def __init__(
        self,
        task_queue: TaskQueue,
        agent_manager: AgentManager,
        opus_bridge: OpusBridge,
        git_ops: GitOps,
        event_bus: EventBus,
    ) -> None:
        self._tq = task_queue
        self._agent = agent_manager
        self._opus = opus_bridge
        self._git = git_ops
        self._bus = event_bus

    # --- Planning ---

    async def plan_and_activate(
        self, plan_id: str, project: dict
    ) -> None:
        plan = await self._tq.get_plan(plan_id)
        if plan is None:
            raise ValueError(f"Plan {plan_id} not found")

        self._bus.publish({"type": "planning_started", "plan_id": plan_id})

        if not await self._opus.is_available():
            await self._opus.queue_action({
                "action": "plan", "plan_id": plan_id, "project_id": project["id"]
            })
            self._bus.publish({"type": "opus_queued", "plan_id": plan_id})
            return

        opus_plan = await self._opus.plan_spec(plan["spec"], project["repo_url"])
        today = date.today().isoformat()
        branch_name = f"plan/{today}-{opus_plan['plan_slug']}"

        await self._tq.activate_plan(plan_id, opus_plan, branch_name)
        self._bus.publish({
            "type": "plan_activated",
            "plan_id": plan_id,
            "task_count": len(opus_plan["tasks"]),
        })
        logger.info(
            "Plan %s activated with %d tasks", plan_id, len(opus_plan["tasks"])
        )

    # --- Dispatch ---

    async def dispatch_pending_tasks(
        self, plan_id: str, project: dict
    ) -> None:
        dispatchable = await self._tq.get_dispatchable_tasks(plan_id)
        plan = await self._tq.get_plan(plan_id)
        if plan is None:
            return

        for task in dispatchable:
            callback_url = f"http://host.docker.internal:{project.get('port', 8080)}/api/internal/agent-done"
            container_id = self._agent.spawn_agent(
                task_id=task["id"],
                repo_url=project["repo_url"],
                branch=task["branch_name"],
                base_branch=plan["plan_branch_name"],
                task_prompt=task["description"],
                model_name=project["model_name"],
                callback_url=callback_url,
            )
            await self._tq.update_task_status(task["id"], TaskStatus.IN_PROGRESS)
            await self._tq.create_agent_run(task["id"], container_id)
            self._bus.publish({
                "type": "agent_dispatched",
                "task_id": task["id"],
                "branch": task["branch_name"],
                "container_id": container_id[:12],
            })
            logger.info(
                "Dispatched agent for task %s on branch %s",
                task["id"], task["branch_name"],
            )

    # --- Review ---

    async def review_task(self, task_id: str, project: dict) -> None:
        task = await self._tq.get_task(task_id)
        if task is None or task["status"] != TaskStatus.REVIEWING:
            return

        if not await self._opus.is_available():
            await self._opus.queue_action({
                "action": "review", "task_id": task_id, "project_id": project["id"]
            })
            self._bus.publish({"type": "opus_queued", "task_id": task_id})
            return

        pr_number = await self._git.extract_pr_number(task["pr_url"])
        diff = await self._git.get_pr_diff(".", pr_number)
        review = await self._opus.review_diff(diff, task["description"])

        self._bus.publish({
            "type": "review_completed",
            "task_id": task_id,
            "verdict": review["verdict"],
            "feedback": review["feedback"],
        })

        if review["verdict"] == "pass":
            await self._tq.update_task_status(task_id, TaskStatus.PASSED)
            await self._git.merge_pr(".", pr_number)
            await self._tq.update_task_status(task_id, TaskStatus.MERGED)
            logger.info("Task %s passed review, merged", task_id)
        else:
            await self._git.comment_on_pr(
                ".", pr_number, f"**Review Feedback:**\n\n{review['feedback']}\n\n**Issues:**\n" +
                "\n".join(f"- {issue}" for issue in review["issues"])
            )
            await self._tq.fail_task(task_id, review["feedback"])

            if task["attempt"] < project["max_retries"]:
                await self._tq.retry_task(task_id)
                logger.info(
                    "Task %s failed review (attempt %d/%d), will retry",
                    task_id, task["attempt"], project["max_retries"],
                )
            else:
                logger.warning(
                    "Task %s failed review, max retries exhausted", task_id
                )

    # --- Improvement ---

    async def check_improvements(
        self, plan_id: str, project: dict
    ) -> dict | None:
        if not await self._tq.all_tasks_done(plan_id):
            return None

        if not await self._opus.is_available():
            await self._opus.queue_action({
                "action": "improve", "plan_id": plan_id, "project_id": project["id"]
            })
            return None

        plan = await self._tq.get_plan(plan_id)
        if plan is None:
            return None

        summary = f"Project: {project['name']}\nRepo: {project['repo_url']}\nCompleted plan: {plan['spec']}"
        analysis = await self._opus.analyze_improvements(summary)

        threshold = project["confidence_threshold"]
        if analysis["confidence"] < threshold:
            self._bus.publish({
                "type": "improvement_skipped",
                "plan_id": plan_id,
                "confidence": analysis["confidence"],
                "reason": analysis["reason"],
            })
            logger.info(
                "Improvement confidence %.2f below threshold %.2f, skipping",
                analysis["confidence"], threshold,
            )
            return None

        self._bus.publish({
            "type": "improvement_proposed",
            "plan_id": plan_id,
            "confidence": analysis["confidence"],
            "reason": analysis["reason"],
            "task_count": len(analysis["proposed_tasks"]),
        })
        return analysis

    async def create_improvement_plan(
        self, project_id: str, analysis: dict
    ) -> str:
        plan_id = await self._tq.create_plan(
            project_id,
            spec=analysis["reason"],
            source="autonomous",
            confidence=analysis["confidence"],
            confidence_reason=analysis["reason"],
        )
        today = date.today().isoformat()
        opus_plan = {
            "plan_summary": analysis["reason"],
            "plan_slug": f"improve-{today}",
            "tasks": [
                {**t, "depends_on": []} for t in analysis["proposed_tasks"]
            ],
        }
        branch = f"plan/{today}-improve"
        await self._tq.activate_plan(plan_id, opus_plan, branch)
        self._bus.publish({
            "type": "improvement_plan_created",
            "plan_id": plan_id,
            "source": "autonomous",
        })
        return plan_id
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_orchestrator.py -v
```

Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: add orchestration loop with plan, dispatch, review, and improvement"
```

---

### Task 4: Web Dashboard

**Files:**
- Create: `web/index.html`

**Depends on:** None

- [ ] **Step 1: Create the single-file web dashboard**

Create file `web/index.html`:
```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI Agent Orchestrator</title>
<style>
  :root {
    --bg: #0f0f1a;
    --surface: #1a1a2e;
    --border: #2a2a3e;
    --text: #e0e0e0;
    --text-dim: #888;
    --primary: #6366f1;
    --success: #22c55e;
    --warning: #f59e0b;
    --danger: #ef4444;
    --info: #3b82f6;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
  }
  header {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 16px 24px;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  header h1 { font-size: 18px; font-weight: 600; }
  .status-bar {
    display: flex;
    gap: 16px;
    align-items: center;
    font-size: 13px;
  }
  .status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    display: inline-block;
    margin-right: 4px;
  }
  .status-dot.available { background: var(--success); }
  .status-dot.rate_limited { background: var(--warning); }
  .status-dot.resuming { background: var(--info); }

  .container { max-width: 1200px; margin: 0 auto; padding: 24px; }

  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 20px;
    margin-bottom: 16px;
  }
  .card h2 { font-size: 16px; margin-bottom: 12px; }
  .card h3 { font-size: 14px; color: var(--text-dim); margin-bottom: 8px; }

  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  @media (max-width: 768px) { .grid { grid-template-columns: 1fr; } }

  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid var(--border); }
  th { color: var(--text-dim); font-weight: 500; }
  tr:hover { background: rgba(255,255,255,0.02); }

  .badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
  }
  .badge.pending { background: rgba(99,102,241,0.2); color: var(--primary); }
  .badge.in_progress { background: rgba(59,130,246,0.2); color: var(--info); }
  .badge.reviewing { background: rgba(245,158,11,0.2); color: var(--warning); }
  .badge.passed, .badge.merged, .badge.completed { background: rgba(34,197,94,0.2); color: var(--success); }
  .badge.failed, .badge.rejected { background: rgba(239,68,68,0.2); color: var(--danger); }
  .badge.active { background: rgba(59,130,246,0.2); color: var(--info); }

  button {
    background: var(--primary);
    color: white;
    border: none;
    padding: 8px 16px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 13px;
  }
  button:hover { opacity: 0.9; }
  button.danger { background: var(--danger); }
  button.success { background: var(--success); }

  input, textarea, select {
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--border);
    padding: 8px 12px;
    border-radius: 6px;
    font-size: 13px;
    width: 100%;
  }
  textarea { min-height: 80px; resize: vertical; font-family: inherit; }

  .form-group { margin-bottom: 12px; }
  .form-group label { display: block; font-size: 12px; color: var(--text-dim); margin-bottom: 4px; }

  .log-viewer {
    background: #000;
    color: #0f0;
    font-family: monospace;
    font-size: 12px;
    padding: 12px;
    border-radius: 6px;
    max-height: 400px;
    overflow-y: auto;
    white-space: pre-wrap;
    word-break: break-all;
  }

  .tabs { display: flex; gap: 0; margin-bottom: 16px; }
  .tab {
    padding: 8px 16px;
    cursor: pointer;
    border-bottom: 2px solid transparent;
    color: var(--text-dim);
    font-size: 13px;
  }
  .tab.active { color: var(--primary); border-bottom-color: var(--primary); }

  .improvement-card {
    border-left: 3px solid var(--warning);
    padding: 16px;
    margin: 12px 0;
  }
  .confidence {
    font-size: 24px;
    font-weight: 700;
    color: var(--warning);
  }

  .hidden { display: none; }
</style>
</head>
<body>
<header>
  <h1>AI Agent Orchestrator</h1>
  <div class="status-bar">
    <span>Opus: <span class="status-dot" id="opus-dot"></span><span id="opus-status">-</span></span>
    <span>Agents: <span id="agent-count">0</span></span>
    <span>Queue: <span id="queue-count">0</span></span>
  </div>
</header>

<div class="container">
  <div class="tabs">
    <div class="tab active" onclick="showTab('projects')">Projects</div>
    <div class="tab" onclick="showTab('plans')">Plans</div>
    <div class="tab" onclick="showTab('tasks')">Tasks</div>
    <div class="tab" onclick="showTab('logs')">Live Logs</div>
  </div>

  <!-- Projects Tab -->
  <div id="tab-projects">
    <div class="grid">
      <div class="card">
        <h2>Add Project</h2>
        <div class="form-group">
          <label>Name</label>
          <input id="proj-name" placeholder="My App">
        </div>
        <div class="form-group">
          <label>GitHub Repo URL</label>
          <input id="proj-repo" placeholder="https://github.com/user/repo">
        </div>
        <div class="form-group">
          <label>Model Name</label>
          <input id="proj-model" placeholder="deepseek-coder-v2">
        </div>
        <button onclick="addProject()">Add Project</button>
      </div>
      <div class="card">
        <h2>Projects</h2>
        <table>
          <thead><tr><th>Name</th><th>Repo</th><th>Gate</th><th></th></tr></thead>
          <tbody id="projects-list"></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- Plans Tab -->
  <div id="tab-plans" class="hidden">
    <div class="grid">
      <div class="card">
        <h2>Submit Spec</h2>
        <div class="form-group">
          <label>Project</label>
          <select id="plan-project"></select>
        </div>
        <div class="form-group">
          <label>Specification</label>
          <textarea id="plan-spec" placeholder="Describe what you want to build..."></textarea>
        </div>
        <button onclick="submitPlan()">Submit</button>
      </div>
      <div class="card">
        <h2>Plans</h2>
        <table>
          <thead><tr><th>Spec</th><th>Source</th><th>Status</th><th>Confidence</th><th></th></tr></thead>
          <tbody id="plans-list"></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- Tasks Tab -->
  <div id="tab-tasks" class="hidden">
    <div class="card">
      <h2>Tasks</h2>
      <div class="form-group">
        <label>Filter by Plan</label>
        <select id="task-plan" onchange="loadTasks()"></select>
      </div>
      <table>
        <thead><tr><th>Title</th><th>Branch</th><th>Status</th><th>Attempt</th><th>PR</th><th></th></tr></thead>
        <tbody id="tasks-list"></tbody>
      </table>
    </div>
  </div>

  <!-- Logs Tab -->
  <div id="tab-logs" class="hidden">
    <div class="card">
      <h2>Live Event Stream</h2>
      <div class="log-viewer" id="log-output">Connecting...</div>
    </div>
  </div>
</div>

<script>
const API = window.location.origin;
let TOKEN = localStorage.getItem('orchestrator_token') || '';
if (!TOKEN) {
  TOKEN = prompt('Enter your API token:') || '';
  localStorage.setItem('orchestrator_token', TOKEN);
}

const headers = () => ({
  'Authorization': `Bearer ${TOKEN}`,
  'Content-Type': 'application/json',
});

async function api(method, path, body) {
  const opts = { method, headers: headers() };
  if (body) opts.body = JSON.stringify(body);
  const resp = await fetch(`${API}${path}`, opts);
  if (!resp.ok) throw new Error(`${resp.status}: ${await resp.text()}`);
  return resp.json();
}

function showTab(name) {
  document.querySelectorAll('[id^="tab-"]').forEach(el => el.classList.add('hidden'));
  document.getElementById(`tab-${name}`).classList.remove('hidden');
  document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
  event.target.classList.add('active');
  if (name === 'projects') loadProjects();
  if (name === 'plans') { loadProjects(); loadAllPlans(); }
  if (name === 'tasks') loadAllPlans();
  if (name === 'logs') connectSSE();
}

function badge(status) {
  return `<span class="badge ${status}">${status}</span>`;
}

// --- Projects ---
async function loadProjects() {
  const projects = await api('GET', '/api/projects');
  const list = document.getElementById('projects-list');
  const select = document.getElementById('plan-project');
  list.innerHTML = projects.map(p => `
    <tr>
      <td>${p.name}</td>
      <td style="font-size:11px">${p.repo_url}</td>
      <td>${p.approval_gate ? 'ON' : 'OFF'}</td>
      <td><button onclick="selectProject('${p.id}')" style="font-size:11px;padding:4px 8px">Select</button></td>
    </tr>
  `).join('');
  select.innerHTML = projects.map(p => `<option value="${p.id}">${p.name}</option>`).join('');
}

async function addProject() {
  await api('POST', '/api/projects', {
    name: document.getElementById('proj-name').value,
    repo_url: document.getElementById('proj-repo').value,
    model_name: document.getElementById('proj-model').value,
  });
  document.getElementById('proj-name').value = '';
  document.getElementById('proj-repo').value = '';
  document.getElementById('proj-model').value = '';
  loadProjects();
}

function selectProject(id) {
  document.getElementById('plan-project').value = id;
  showTab('plans');
}

// --- Plans ---
async function loadAllPlans() {
  const projects = await api('GET', '/api/projects');
  const planSelect = document.getElementById('task-plan');
  let allPlans = [];
  for (const p of projects) {
    const plans = await api('GET', `/api/projects/${p.id}/plans`);
    allPlans = allPlans.concat(plans.map(pl => ({ ...pl, projectName: p.name })));
  }
  const list = document.getElementById('plans-list');
  list.innerHTML = allPlans.map(p => `
    <tr>
      <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis">${p.spec}</td>
      <td>${p.source}</td>
      <td>${badge(p.status)}</td>
      <td>${p.confidence !== null ? (p.confidence * 100).toFixed(0) + '%' : '-'}</td>
      <td>
        ${p.status === 'pending' && p.source === 'autonomous' ? `
          <button class="success" onclick="approvePlan('${p.id}')" style="font-size:11px;padding:4px 8px">Approve</button>
          <button class="danger" onclick="rejectPlan('${p.id}')" style="font-size:11px;padding:4px 8px">Reject</button>
        ` : ''}
      </td>
    </tr>
  `).join('');
  planSelect.innerHTML = allPlans.map(p => `<option value="${p.id}">${p.spec.slice(0, 40)}</option>`).join('');
}

async function submitPlan() {
  const projectId = document.getElementById('plan-project').value;
  const spec = document.getElementById('plan-spec').value;
  await api('POST', `/api/projects/${projectId}/plans`, { spec });
  document.getElementById('plan-spec').value = '';
  loadAllPlans();
}

async function approvePlan(id) { await api('POST', `/api/plans/${id}/approve`); loadAllPlans(); }
async function rejectPlan(id) { await api('POST', `/api/plans/${id}/reject`); loadAllPlans(); }

// --- Tasks ---
async function loadTasks() {
  const planId = document.getElementById('task-plan').value;
  if (!planId) return;
  const tasks = await api('GET', `/api/plans/${planId}/tasks`);
  const list = document.getElementById('tasks-list');
  list.innerHTML = tasks.map(t => `
    <tr>
      <td>${t.title}</td>
      <td style="font-size:11px">${t.branch_name}</td>
      <td>${badge(t.status)}</td>
      <td>${t.attempt}</td>
      <td>${t.pr_url ? `<a href="${t.pr_url}" target="_blank" style="color:var(--info)">PR</a>` : '-'}</td>
      <td>${t.status === 'in_progress' ? `<button class="danger" onclick="stopTask('${t.id}')" style="font-size:11px;padding:4px 8px">Stop</button>` : ''}</td>
    </tr>
  `).join('');
}

async function stopTask(id) { await api('POST', `/api/tasks/${id}/stop`); loadTasks(); }

// --- SSE ---
let eventSource = null;
function connectSSE() {
  if (eventSource) eventSource.close();
  const logEl = document.getElementById('log-output');
  logEl.textContent = 'Connecting...\n';
  eventSource = new EventSource(`${API}/api/events?token=${TOKEN}`);
  eventSource.onopen = () => { logEl.textContent += 'Connected.\n'; };
  eventSource.onmessage = (e) => {
    logEl.textContent += e.data + '\n';
    logEl.scrollTop = logEl.scrollHeight;
  };
  eventSource.addEventListener('ping', () => {});
  eventSource.onerror = () => {
    logEl.textContent += 'Connection lost. Reconnecting...\n';
  };
  // Listen for all event types
  ['plan_activated', 'agent_dispatched', 'review_completed', 'improvement_proposed',
   'task_completed', 'task_failed', 'opus_queued', 'agent_log'].forEach(type => {
    eventSource.addEventListener(type, (e) => {
      const data = JSON.parse(e.data);
      const line = `[${type}] ${JSON.stringify(data)}\n`;
      logEl.textContent += line;
      logEl.scrollTop = logEl.scrollHeight;
    });
  });
}

// --- Status polling ---
async function pollStatus() {
  try {
    const data = await api('GET', '/api/status');
    const dot = document.getElementById('opus-dot');
    const statusEl = document.getElementById('opus-status');
    dot.className = `status-dot ${data.opus_state.status}`;
    statusEl.textContent = data.opus_state.status;
    document.getElementById('agent-count').textContent = data.active_agents;
    document.getElementById('queue-count').textContent = data.opus_state.queued_count;
  } catch (e) { /* ignore polling errors */ }
}
setInterval(pollStatus, 5000);

// Init
loadProjects();
pollStatus();
</script>
</body>
</html>
```

- [ ] **Step 2: Commit**

```bash
git add web/index.html
git commit -m "feat: add single-file web dashboard with SSE live logs"
```

---

### Task 5: Wire Orchestrator Into Main App

**Files:**
- Modify: `src/orchestrator/main.py`

**Depends on:** Task 2, Task 3

- [ ] **Step 1: Read current main.py**

Read `src/orchestrator/main.py` to see the current state.

- [ ] **Step 2: Add orchestrator and git_ops to app state**

In the lifespan function, after `app.state.event_bus = EventBus()`, add:
```python
    from orchestrator.core.git_ops import GitOps
    from orchestrator.core.orchestrator import Orchestrator

    git_ops = GitOps(github_token=settings.github_token)
    app.state.git_ops = git_ops
    app.state.orchestrator = Orchestrator(
        task_queue=app.state.task_queue,
        agent_manager=app.state.agent_manager,
        opus_bridge=app.state.opus_bridge,
        git_ops=git_ops,
        event_bus=app.state.event_bus,
    )
```

- [ ] **Step 3: Commit**

```bash
git add src/orchestrator/main.py
git commit -m "feat: wire orchestrator and git_ops into app state"
```

---

### Task 6: Run Full Test Suite + Lint

**Files:** None (verification only)

**Depends on:** Task 1, Task 2, Task 3, Task 4, Task 5

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
git diff --cached --quiet || git commit -m "style: final formatting and lint fixes"
```

- [ ] **Step 4: Final verification — Docker build**

```bash
docker build -t orchestrator:latest -f docker/orchestrator/Dockerfile .
docker build -t aider-agent:latest -f docker/aider-agent/Dockerfile docker/aider-agent/
```

Expected: Both images build successfully

---

## Parallel Execution Map

- **Wave 1:** Task 1 (Event Bus), Task 4 (Web Dashboard) — both independent
- **Wave 2:** Task 2 (SSE endpoint — depends on Task 1), Task 3 (Orchestrator — depends on Task 1)
- **Wave 3:** Task 5 (Wire into main — depends on Task 2, Task 3)
- **Wave 4:** Task 6 (Verification — depends on all)
