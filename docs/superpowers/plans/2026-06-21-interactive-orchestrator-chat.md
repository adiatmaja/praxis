# Interactive Orchestrator Chat Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an in-dashboard interactive Claude (Opus) chat per project — read-only file tree on the left, streamed multi-turn chat on the right — where Opus reads the repo with its native tools and dispatches Praxis harness subagents (Aider/OpenCode/OpenHands) to do the actual coding via an MCP tool.

**Architecture:** A new `ChatManager` clones the project repo into `data/chat-workdirs/{project_id}/`, then per user message spawns `claude -p <msg> --resume <session_id> --output-format stream-json --mcp-config <cfg> --dangerously-skip-permissions` with `cwd` = the clone. Opus reads code natively; a standalone stdio **MCP server** (`orchestrator.mcp_server`) exposes `dispatch_agent` / `check_task_status` / `list_active_tasks`. `dispatch_agent` creates a single-task plan exactly like `Orchestrator.create_improvement_plan` does, and the existing autonomous `run_loop` dispatches → reviews → merges it. Dispatch honors the project `approval_gate`: gate off → plan `ACTIVE` (auto-dispatched), gate on → plan `PENDING` (held for the existing Approve button). Streaming reuses the existing `EventBus` + `/api/events` SSE. Conversation persists to two new SQLite tables, resumable via `claude --resume`.

**Tech Stack:** Python 3.11, FastAPI, aiosqlite (raw SQL), `claude -p` CLI (stream-json), MCP Python SDK (`mcp`), Docker SDK (existing), single-file HTML/JS dashboard.

---

## Context for the implementer (read before starting)

You have **zero prior context**, so here is everything you need:

- **No ORM.** SQLite via `orchestrator.database.Database` (`fetch_one`, `fetch_all`, `execute`). Migrations are inline `CREATE TABLE IF NOT EXISTS` strings appended to the `MIGRATIONS` tuple in `src/orchestrator/database.py`.
- **App wiring** lives in `src/orchestrator/main.py` `lifespan()`: managers are attached to `app.state.*`, routers are imported and `app.include_router(...)` at module bottom. Most routers are mounted with `prefix="/api"`.
- **Auth:** every JSON endpoint depends on `verify_token` from `orchestrator.api.auth` (Bearer). SSE uses `verify_event_token` (Bearer OR `?token=`).
- **EventBus** (`core/event_bus.py`) is in-memory pub/sub. `event_bus.publish({"type": ..., ...})` fans out to SSE subscribers on `GET /api/events`. Events SHOULD include a `project_id` so the browser can filter. The frontend opens `new EventSource(API + "/api/events?token=" + token)`.
- **The dispatch pattern to mirror** is `Orchestrator.create_improvement_plan` in `core/orchestrator.py:372-408`. It calls `task_queue.create_plan(...)` then builds an `opus_plan` dict `{"plan_summary","plan_slug","tasks":[{title,slug,description,depends_on}]}` and calls `task_queue.activate_plan(plan_id, opus_plan, branch)` which inserts ACTIVE + task rows. The autonomous loop (`process_plan_once`, `core/orchestrator.py:410-451`) then dispatches and reviews. **You do not call `spawn_agent` yourself** — creating the plan is enough.
- **The approval-gate hold:** `process_plan_once:420-425` currently only holds PENDING plans when `source == "autonomous"`. Task 1 generalizes this so ANY PENDING plan that already has an `opus_plan` is held (chat plans included), instead of being re-planned by Opus.
- **Cross-process DB:** the MCP server is a separate process spawned by `claude`. It opens the **same** `data/orchestrator.db` (SQLite WAL handles concurrent readers/writers). It is bound to one project via the `PRAXIS_PROJECT_ID` env var. It must NOT touch Docker.
- **`claude` invocation precedent:** `core/context_sync.py:_run_revise` already runs `claude -p <prompt> --dangerously-skip-permissions` via `asyncio.create_subprocess_exec` with a `cwd`. Reuse this shape; add streaming + mcp-config.
- **Token-safe clone:** use `clone_with_token(repo_url, dest, token)` from `core/git_ops.py`. For refresh use plain `git -C <dir> fetch` + `reset --hard origin/<default_branch>`.
- **Commands:** tests `uv run pytest --cov=orchestrator -v`; lint `uv run ruff check --fix src/ tests/` + `uv run ruff format src/ tests/`; types `uv run mypy src/orchestrator/ --ignore-missing-imports`. Run all three before each commit.
- **Add the `mcp` dependency** to `pyproject.toml` (Task 2).

---

## File Structure

| File | Responsibility |
|------|----------------|
| `src/orchestrator/database.py` (modify) | Add `chat_sessions` + `chat_messages` migrations |
| `src/orchestrator/core/orchestrator.py` (modify) | Generalize PENDING-with-opus_plan hold in `process_plan_once` |
| `src/orchestrator/core/chat_dispatch.py` (create) | Pure, testable `create_chat_dispatch()` — builds the single-task plan honoring the gate |
| `src/orchestrator/mcp_server.py` (create) | Standalone stdio MCP server exposing the 3 tools; thin wrapper over `chat_dispatch` + DB reads |
| `src/orchestrator/core/chat_manager.py` (create) | Clone/refresh workdir; spawn streaming `claude`; parse NDJSON; persist messages; publish events |
| `src/orchestrator/api/chat.py` (create) | `GET/POST /api/projects/{id}/chat`, `GET .../tree`, `GET .../file` |
| `src/orchestrator/main.py` (modify) | Construct `ChatManager`, include `chat` router |
| `pyproject.toml` (modify) | Add `mcp` dependency |
| `web/index.html` (modify) | New "Claude Code" nav view: file tree + chat pane |
| `tests/test_chat_dispatch.py` (create) | Unit tests for gate on/off plan creation |
| `tests/test_chat_manager.py` (create) | Unit tests for NDJSON parsing + session resume + persistence |
| `tests/test_api_chat.py` (create) | Integration tests for chat/tree/file endpoints |
| `.gitignore` (modify) | Ignore `data/chat-workdirs/` |

---

## Task 1: Database tables + generalize the approval-gate hold

**Files:**
- Modify: `src/orchestrator/database.py` (append to `MIGRATIONS`)
- Modify: `src/orchestrator/core/orchestrator.py:420-425`
- Test: `tests/test_chat_schema.py` (create)

**Depends on:** None

- [ ] **Step 1: Write the failing test**

Create `tests/test_chat_schema.py`:

```python
"""Schema + approval-gate-hold tests for chat dispatch."""

from __future__ import annotations

import pytest

from orchestrator.database import Database


@pytest.mark.asyncio
async def test_chat_tables_exist() -> None:
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.initialize()
    # Inserting into both tables must succeed (tables created by migrations).
    await db.execute(
        "INSERT INTO chat_sessions (id, project_id, workdir, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("s1", "p1", "/tmp/wd", "2026-06-21", "2026-06-21"),
    )
    await db.execute(
        "INSERT INTO chat_messages (id, session_id, role, content, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("m1", "s1", "user", "hello", "2026-06-21"),
    )
    rows = await db.fetch_all("SELECT id FROM chat_messages WHERE session_id = ?", ("s1",))
    assert [r["id"] for r in rows] == ["m1"]
    await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_chat_schema.py -v`
Expected: FAIL — `sqlite3.OperationalError: no such table: chat_sessions`

- [ ] **Step 3: Add the migrations**

In `src/orchestrator/database.py`, append these two strings to the `MIGRATIONS` tuple (after the `settings_overrides` block, before the closing `)`):

```python
    """
    CREATE TABLE IF NOT EXISTS chat_sessions (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        claude_session_id TEXT,
        workdir TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (project_id) REFERENCES projects (id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chat_messages (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        tool_calls_json TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (session_id) REFERENCES chat_sessions (id)
    )
    """,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_chat_schema.py -v`
Expected: PASS

- [ ] **Step 5: Generalize the approval-gate hold**

In `src/orchestrator/core/orchestrator.py`, find this block in `process_plan_once` (around line 420):

```python
        if (
            plan["status"] == PlanStatus.PENDING
            and plan["source"] == "autonomous"
            and plan["opus_plan"] is not None
        ):
            return
```

Replace it with (drop the `source == "autonomous"` condition so chat plans are also held instead of re-planned):

```python
        if (
            plan["status"] == PlanStatus.PENDING
            and plan["opus_plan"] is not None
        ):
            # A pre-planned PENDING plan (autonomous improvement or chat
            # dispatch awaiting approval) must NOT be re-sent to Opus for
            # planning — it already has its task graph. Hold until approved.
            return
```

This is safe: user-submitted plans are PENDING with `opus_plan IS NULL`, so they still flow to `plan_and_activate`.

- [ ] **Step 6: Verify existing orchestrator tests still pass**

Run: `uv run pytest tests/test_orchestrator.py -v`
Expected: PASS (no regressions). If a test asserted the old source-specific behavior, update it to construct a PENDING plan with `opus_plan` set and assert it is held.

- [ ] **Step 7: Lint, type-check, commit**

```bash
uv run ruff check --fix src/ tests/ && uv run ruff format src/ tests/
uv run mypy src/orchestrator/ --ignore-missing-imports
git add src/orchestrator/database.py src/orchestrator/core/orchestrator.py tests/test_chat_schema.py
git commit -m "feat: chat tables + hold pre-planned PENDING plans"
```

---

## Task 2: Chat dispatch core + MCP server

**Files:**
- Create: `src/orchestrator/core/chat_dispatch.py`
- Create: `src/orchestrator/mcp_server.py`
- Modify: `pyproject.toml` (add `mcp` dependency)
- Test: `tests/test_chat_dispatch.py`

**Depends on:** Task 1

- [ ] **Step 1: Write the failing test**

Create `tests/test_chat_dispatch.py`:

```python
"""Tests for chat-driven single-task plan creation honoring the approval gate."""

from __future__ import annotations

import json

import pytest

from orchestrator.core.chat_dispatch import create_chat_dispatch
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import PlanStatus


async def _seed_project(db: Database, *, approval_gate: int) -> str:
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u1", "admin", "t"),
    )
    await db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url, approval_gate, model_name) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("p1", "u1", "demo", "https://github.com/x/y", approval_gate, "qwen"),
    )
    return "p1"


@pytest.mark.asyncio
async def test_dispatch_gate_off_creates_active_plan() -> None:
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.initialize()
    project_id = await _seed_project(db, approval_gate=0)
    tq = TaskQueue(db)

    result = await create_chat_dispatch(
        tq, project_id, "Add health endpoint", "Implement GET /health returning ok"
    )

    plan = await tq.get_plan(result["plan_id"])
    assert plan is not None
    assert plan["status"] == PlanStatus.ACTIVE
    assert plan["source"] == "chat"
    opus_plan = json.loads(plan["opus_plan"])
    assert len(opus_plan["tasks"]) == 1
    assert opus_plan["tasks"][0]["title"] == "Add health endpoint"
    assert result["status"] == "dispatched"
    assert result["awaiting_approval"] is False
    await db.close()


@pytest.mark.asyncio
async def test_dispatch_gate_on_creates_pending_plan() -> None:
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.initialize()
    project_id = await _seed_project(db, approval_gate=1)
    tq = TaskQueue(db)

    result = await create_chat_dispatch(
        tq, project_id, "Add health endpoint", "Implement GET /health returning ok"
    )

    plan = await tq.get_plan(result["plan_id"])
    assert plan is not None
    assert plan["status"] == PlanStatus.PENDING
    assert result["status"] == "awaiting_approval"
    assert result["awaiting_approval"] is True
    # Task row was still created (held until approval).
    tasks = await tq.get_tasks_for_plan(result["plan_id"])
    assert len(tasks) == 1
    await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_chat_dispatch.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'orchestrator.core.chat_dispatch'`

- [ ] **Step 3: Implement `create_chat_dispatch`**

Create `src/orchestrator/core/chat_dispatch.py`:

```python
"""Create a single-task plan from a chat dispatch, honoring the approval gate.

This mirrors ``Orchestrator.create_improvement_plan`` but for a task Opus
decided on interactively. The existing autonomous loop dispatches and reviews
the resulting plan; we only create it here.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from orchestrator.core.task_queue import TaskQueue
from orchestrator.models.schemas import PlanStatus


logger = logging.getLogger(__name__)


def _slugify(text: str) -> str:
    """Return a url-safe slug derived from ``text``."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:50] or "task"


async def create_chat_dispatch(
    task_queue: TaskQueue,
    project_id: str,
    title: str,
    description: str,
) -> dict[str, Any]:
    """Create a one-task plan for a chat-dispatched implementation task.

    When the project's ``approval_gate`` is off the plan is left ACTIVE so the
    orchestration loop dispatches it immediately. When on, the plan is set
    PENDING (with its task graph already attached) so it appears under the
    existing Approve button and is held by ``process_plan_once``.

    Returns a dict with ``plan_id``, ``task_id``, ``status``
    ('dispatched' | 'awaiting_approval'), and ``awaiting_approval`` (bool).
    """
    project = await task_queue.get_project(project_id)
    if project is None:
        message = f"Project {project_id} not found"
        raise ValueError(message)

    slug = _slugify(title)
    plan_id = await task_queue.create_plan(project_id, spec=description, source="chat")
    today = datetime.now(UTC).date().isoformat()
    opus_plan = {
        "plan_summary": title,
        "plan_slug": f"chat-{today}-{slug}",
        "tasks": [
            {
                "title": title,
                "slug": slug,
                "description": description,
                "depends_on": [],
            }
        ],
    }
    branch = f"plan/{today}-chat-{slug}"
    await task_queue.activate_plan(plan_id, opus_plan, branch)

    gate_on = bool(project["approval_gate"])
    if gate_on:
        await task_queue.update_plan_status(plan_id, PlanStatus.PENDING)

    tasks = await task_queue.get_tasks_for_plan(plan_id)
    task_id = tasks[0]["id"] if tasks else ""
    logger.info(
        "Chat dispatch created plan %s (gate_on=%s) for project %s",
        plan_id,
        gate_on,
        project_id,
    )
    return {
        "plan_id": plan_id,
        "task_id": task_id,
        "status": "awaiting_approval" if gate_on else "dispatched",
        "awaiting_approval": gate_on,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_chat_dispatch.py -v`
Expected: PASS (both tests)

- [ ] **Step 5: Add the `mcp` dependency**

In `pyproject.toml`, add `"mcp>=1.0"` to the `dependencies` list (the main `[project]` dependencies, not dev). Then:

Run: `uv sync`
Expected: resolves and installs `mcp`.

- [ ] **Step 6: Implement the MCP server**

Create `src/orchestrator/mcp_server.py`:

```python
"""Standalone stdio MCP server exposing dispatch/status tools to interactive Opus.

Spawned by the `claude` chat process (see ChatManager). Bound to one project via
PRAXIS_PROJECT_ID. Opens the same SQLite DB as the orchestrator (WAL handles
concurrent access) and creates chat-dispatch plans the autonomous loop picks up.
It never touches Docker.

Run as: python -m orchestrator.mcp_server
Required env: PRAXIS_PROJECT_ID, DATABASE_URL (defaults to the app default).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from orchestrator.core.chat_dispatch import create_chat_dispatch
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("praxis.mcp")

PROJECT_ID = os.environ["PRAXIS_PROJECT_ID"]
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "sqlite+aiosqlite:///data/orchestrator.db"
)

mcp = FastMCP("praxis")


async def _task_queue() -> tuple[Database, TaskQueue]:
    db = Database(DATABASE_URL)
    await db.initialize()
    return db, TaskQueue(db)


@mcp.tool()
async def dispatch_agent(task_title: str, description: str) -> str:
    """Dispatch a Praxis coding subagent (Aider/OpenCode/OpenHands) to implement
    a task on its own branch. The subagent (a local model) writes the code; it
    is then reviewed and merged automatically. You (Opus) do not write code.

    Args:
        task_title: Short imperative title, e.g. "Add input validation".
        description: Detailed, self-contained instructions for the coding agent.

    Returns a status line: dispatched (task_id) or awaiting approval.
    """
    db, tq = await _task_queue()
    try:
        result = await create_chat_dispatch(
            tq, PROJECT_ID, task_title, description
        )
    finally:
        await db.close()
    if result["awaiting_approval"]:
        return (
            f"Task created (id={result['task_id']}) and is AWAITING USER APPROVAL "
            f"before the subagent starts. Do not wait on its result yet."
        )
    return (
        f"Dispatched subagent for task id={result['task_id']}. It is now running. "
        f"Use check_task_status to poll it."
    )


@mcp.tool()
async def check_task_status(task_id: str) -> str:
    """Return the current status of a dispatched task and any review feedback."""
    db, tq = await _task_queue()
    try:
        task = await tq.get_task(task_id)
    finally:
        await db.close()
    if task is None:
        return f"No task with id={task_id}."
    return json.dumps(
        {
            "task_id": task["id"],
            "title": task["title"],
            "status": task["status"],
            "pr_url": task["pr_url"],
            "review_feedback": task["review_feedback"],
        }
    )


@mcp.tool()
async def list_active_tasks() -> str:
    """List all tasks for this project that are not yet merged."""
    db, tq = await _task_queue()
    try:
        plans = await tq.get_plans_for_project(PROJECT_ID)
        rows: list[dict[str, Any]] = []
        for plan in plans:
            for task in await tq.get_tasks_for_plan(plan["id"]):
                if task["status"] != "merged":
                    rows.append(
                        {
                            "task_id": task["id"],
                            "title": task["title"],
                            "status": task["status"],
                        }
                    )
    finally:
        await db.close()
    return json.dumps(rows)


def main() -> None:
    """Run the MCP server over stdio."""
    logger.info("Starting Praxis MCP server for project %s", PROJECT_ID)
    mcp.run()


if __name__ == "__main__":
    main()
```

- [ ] **Step 7: Smoke-test the server imports and registers tools**

Run: `uv run python -c "import os; os.environ['PRAXIS_PROJECT_ID']='x'; import orchestrator.mcp_server as m; print(sorted(t.name for t in __import__('asyncio').get_event_loop().run_until_complete(m.mcp.list_tools())))"`
Expected: prints `['check_task_status', 'dispatch_agent', 'list_active_tasks']`
(If the FastMCP API differs in the installed `mcp` version, adjust the introspection call — the goal is only to confirm the three tools register. Do not change tool names.)

- [ ] **Step 8: Lint, type-check, commit**

```bash
uv run ruff check --fix src/ tests/ && uv run ruff format src/ tests/
uv run mypy src/orchestrator/ --ignore-missing-imports
git add src/orchestrator/core/chat_dispatch.py src/orchestrator/mcp_server.py pyproject.toml tests/test_chat_dispatch.py
git commit -m "feat: chat dispatch core + MCP server (dispatch/status tools)"
```

---

## Task 3: ChatManager — clone, stream `claude`, persist, publish

**Files:**
- Create: `src/orchestrator/core/chat_manager.py`
- Test: `tests/test_chat_manager.py`

**Depends on:** Task 1

- [ ] **Step 1: Write the failing test**

Create `tests/test_chat_manager.py`:

```python
"""Unit tests for ChatManager NDJSON parsing, session resume, and persistence."""

from __future__ import annotations

import json

import pytest

from orchestrator.core.chat_manager import ChatManager, ParsedEvent
from orchestrator.core.event_bus import EventBus
from orchestrator.database import Database


def _mgr(db: Database) -> ChatManager:
    return ChatManager(
        db=db,
        event_bus=EventBus(),
        github_token="t",
        workdir_base="/tmp/praxis-chat-test",
    )


def test_parse_system_line_extracts_session_id() -> None:
    mgr = _mgr.__wrapped__ if hasattr(_mgr, "__wrapped__") else None  # noqa: F841
    cm = ChatManager(db=None, event_bus=EventBus(), github_token="t", workdir_base="/tmp")  # type: ignore[arg-type]
    line = json.dumps({"type": "system", "subtype": "init", "session_id": "abc123"})
    event = cm.parse_stream_line(line)
    assert event == ParsedEvent(kind="session", session_id="abc123", text="", tool=None)


def test_parse_assistant_text_line() -> None:
    cm = ChatManager(db=None, event_bus=EventBus(), github_token="t", workdir_base="/tmp")  # type: ignore[arg-type]
    line = json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello"}]}}
    )
    event = cm.parse_stream_line(line)
    assert event is not None
    assert event.kind == "assistant_text"
    assert event.text == "Hello"


def test_parse_tool_use_line() -> None:
    cm = ChatManager(db=None, event_bus=EventBus(), github_token="t", workdir_base="/tmp")  # type: ignore[arg-type]
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "dispatch_agent", "input": {"task_title": "x"}}
                ]
            },
        }
    )
    event = cm.parse_stream_line(line)
    assert event is not None
    assert event.kind == "tool_use"
    assert event.tool == "dispatch_agent"


def test_parse_result_line() -> None:
    cm = ChatManager(db=None, event_bus=EventBus(), github_token="t", workdir_base="/tmp")  # type: ignore[arg-type]
    line = json.dumps({"type": "result", "subtype": "success", "result": "done", "session_id": "s9"})
    event = cm.parse_stream_line(line)
    assert event is not None
    assert event.kind == "result"
    assert event.session_id == "s9"


def test_parse_ignores_blank_and_unknown() -> None:
    cm = ChatManager(db=None, event_bus=EventBus(), github_token="t", workdir_base="/tmp")  # type: ignore[arg-type]
    assert cm.parse_stream_line("") is None
    assert cm.parse_stream_line("   ") is None
    assert cm.parse_stream_line(json.dumps({"type": "whatever"})) is None


@pytest.mark.asyncio
async def test_ensure_session_creates_row_once() -> None:
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.initialize()
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES ('u1','a','t')"
    )
    await db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url) "
        "VALUES ('p1','u1','d','https://github.com/x/y')"
    )
    cm = _mgr(db)
    s1 = await cm.ensure_session("p1", workdir="/tmp/wd")
    s2 = await cm.ensure_session("p1", workdir="/tmp/wd")
    assert s1 == s2  # one session per project
    await db.close()


@pytest.mark.asyncio
async def test_persist_and_load_messages() -> None:
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.initialize()
    await db.execute("INSERT INTO users (id, name, token_hash) VALUES ('u1','a','t')")
    await db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url) "
        "VALUES ('p1','u1','d','https://github.com/x/y')"
    )
    cm = _mgr(db)
    session_id = await cm.ensure_session("p1", workdir="/tmp/wd")
    await cm.persist_message(session_id, "user", "hi", None)
    await cm.persist_message(session_id, "assistant", "hello", [{"tool": "dispatch_agent"}])
    history = await cm.load_history("p1")
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[1]["tool_calls"] == [{"tool": "dispatch_agent"}]
    await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_chat_manager.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'orchestrator.core.chat_manager'`

- [ ] **Step 3: Implement ChatManager**

Create `src/orchestrator/core/chat_manager.py`:

```python
"""Interactive Opus chat sessions: clone repo, stream `claude`, persist, publish."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess  # noqa: S404
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestrator.core.event_bus import EventBus
from orchestrator.core.git_ops import clone_with_token
from orchestrator.database import Database


logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are the orchestrator for a coding project. You can READ the repository "
    "with your native tools (Read, Glob, Grep) but you must NOT edit files "
    "yourself. To implement changes, call the dispatch_agent tool, which spawns a "
    "local coding subagent that writes and commits the code on its own branch; the "
    "work is then reviewed and merged automatically. Use check_task_status and "
    "list_active_tasks to track dispatched work."
)


@dataclass(frozen=True)
class ParsedEvent:
    """A normalized event parsed from one line of `claude` stream-json output."""

    kind: str  # 'session' | 'assistant_text' | 'tool_use' | 'result'
    session_id: str
    text: str
    tool: str | None


class ChatManager:
    """Manages per-project interactive Opus chat sessions."""

    def __init__(
        self,
        db: Database,
        event_bus: EventBus,
        github_token: str,
        workdir_base: str,
    ) -> None:
        self._db = db
        self._bus = event_bus
        self._token = github_token
        self._base = Path(workdir_base)

    # ----- parsing (pure, unit-tested) -------------------------------------

    def parse_stream_line(self, line: str) -> ParsedEvent | None:
        """Parse one NDJSON line from `claude --output-format stream-json`.

        Returns a ParsedEvent for the line types we surface, or None for blank
        lines and event types we ignore.
        """
        line = line.strip()
        if not line:
            return None
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return None

        kind = obj.get("type")
        if kind == "system" and obj.get("session_id"):
            return ParsedEvent("session", obj["session_id"], "", None)
        if kind == "result":
            return ParsedEvent(
                "result", obj.get("session_id", ""), str(obj.get("result", "")), None
            )
        if kind == "assistant":
            content = obj.get("message", {}).get("content", [])
            for block in content:
                if block.get("type") == "text":
                    return ParsedEvent("assistant_text", "", block.get("text", ""), None)
                if block.get("type") == "tool_use":
                    return ParsedEvent("tool_use", "", "", block.get("name"))
        return None

    # ----- persistence -----------------------------------------------------

    async def ensure_session(self, project_id: str, workdir: str) -> str:
        """Return the (single) chat session id for a project, creating it once."""
        row = await self._db.fetch_one(
            "SELECT id FROM chat_sessions WHERE project_id = ?", (project_id,)
        )
        if row is not None:
            return str(row["id"])
        session_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "INSERT INTO chat_sessions (id, project_id, workdir, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, project_id, workdir, now, now),
        )
        return session_id

    async def _get_claude_session_id(self, session_id: str) -> str | None:
        row = await self._db.fetch_one(
            "SELECT claude_session_id FROM chat_sessions WHERE id = ?", (session_id,)
        )
        return None if row is None else row["claude_session_id"]

    async def _set_claude_session_id(self, session_id: str, claude_sid: str) -> None:
        await self._db.execute(
            "UPDATE chat_sessions SET claude_session_id = ?, updated_at = ? WHERE id = ?",
            (claude_sid, datetime.now(UTC).isoformat(), session_id),
        )

    async def persist_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls: list[dict[str, Any]] | None,
    ) -> None:
        """Persist one chat message."""
        await self._db.execute(
            "INSERT INTO chat_messages (id, session_id, role, content, tool_calls_json, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                session_id,
                role,
                content,
                json.dumps(tool_calls) if tool_calls is not None else None,
                datetime.now(UTC).isoformat(),
            ),
        )

    async def load_history(self, project_id: str) -> list[dict[str, Any]]:
        """Return the full message history for a project's chat session."""
        session = await self._db.fetch_one(
            "SELECT id FROM chat_sessions WHERE project_id = ?", (project_id,)
        )
        if session is None:
            return []
        rows = await self._db.fetch_all(
            "SELECT role, content, tool_calls_json FROM chat_messages "
            "WHERE session_id = ? ORDER BY created_at, rowid",
            (session["id"],),
        )
        return [
            {
                "role": r["role"],
                "content": r["content"],
                "tool_calls": json.loads(r["tool_calls_json"])
                if r["tool_calls_json"]
                else None,
            }
            for r in rows
        ]

    # ----- workdir ---------------------------------------------------------

    def ensure_workdir(self, project_id: str, repo_url: str, default_branch: str) -> str:
        """Clone the repo once into the per-project workdir, or fetch+reset if present."""
        workdir = self._base / project_id
        if (workdir / ".git").is_dir():
            subprocess.run(  # noqa: S603, S607
                ["git", "-C", str(workdir), "fetch", "--depth", "1", "origin", default_branch],
                check=True, capture_output=True, text=True,
            )
            subprocess.run(  # noqa: S603, S607
                ["git", "-C", str(workdir), "reset", "--hard", f"origin/{default_branch}"],
                check=True, capture_output=True, text=True,
            )
        else:
            workdir.parent.mkdir(parents=True, exist_ok=True)
            clone_with_token(repo_url, str(workdir), self._token, depth=50)
        return str(workdir)

    def _mcp_config(self, project_id: str) -> str:
        """Write an mcp-config JSON binding the Praxis MCP server to this project.

        Returns the path to the config file.
        """
        workdir = self._base / project_id
        cfg_path = workdir / ".praxis-mcp.json"
        cfg = {
            "mcpServers": {
                "praxis": {
                    "command": "python",
                    "args": ["-m", "orchestrator.mcp_server"],
                    "env": {"PRAXIS_PROJECT_ID": project_id},
                }
            }
        }
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        return str(cfg_path)

    # ----- the turn --------------------------------------------------------

    async def run_turn(self, project_id: str, message: str) -> None:
        """Run one chat turn: spawn streaming `claude`, relay events, persist.

        Publishes EventBus events of type 'chat_delta' (assistant text), 'chat_tool'
        (tool_use), and 'chat_done' (turn complete). All carry project_id.
        """
        project = await self._db.fetch_one(
            "SELECT repo_url, default_branch FROM projects WHERE id = ?", (project_id,)
        )
        if project is None:
            message_err = f"Project {project_id} not found"
            raise ValueError(message_err)

        workdir = self.ensure_workdir(
            project_id, project["repo_url"], project["default_branch"] or "main"
        )
        session_id = await self.ensure_session(project_id, workdir)
        await self.persist_message(session_id, "user", message, None)

        claude_sid = await self._get_claude_session_id(session_id)
        cfg = self._mcp_config(project_id)
        args = [
            "claude", "-p", message,
            "--output-format", "stream-json",
            "--verbose",
            "--append-system-prompt", SYSTEM_PROMPT,
            "--mcp-config", cfg,
            "--dangerously-skip-permissions",
        ]
        if claude_sid:
            args += ["--resume", claude_sid]

        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        assistant_text: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        assert proc.stdout is not None
        async for raw in proc.stdout:
            event = self.parse_stream_line(raw.decode(errors="replace"))
            if event is None:
                continue
            if event.kind == "session" and event.session_id:
                await self._set_claude_session_id(session_id, event.session_id)
            elif event.kind == "assistant_text":
                assistant_text.append(event.text)
                self._bus.publish(
                    {"type": "chat_delta", "project_id": project_id, "text": event.text}
                )
            elif event.kind == "tool_use":
                tool_calls.append({"tool": event.tool})
                self._bus.publish(
                    {"type": "chat_tool", "project_id": project_id, "tool": event.tool}
                )
            elif event.kind == "result":
                if event.session_id:
                    await self._set_claude_session_id(session_id, event.session_id)

        await proc.wait()
        full = "".join(assistant_text)
        await self.persist_message(
            session_id, "assistant", full, tool_calls or None
        )
        self._bus.publish(
            {"type": "chat_done", "project_id": project_id, "content": full}
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_chat_manager.py -v`
Expected: PASS (all parsing + session + persistence tests)

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check --fix src/ tests/ && uv run ruff format src/ tests/
uv run mypy src/orchestrator/ --ignore-missing-imports
git add src/orchestrator/core/chat_manager.py tests/test_chat_manager.py
git commit -m "feat: ChatManager streaming + persistence"
```

---

## Task 4: Chat / tree / file API endpoints

**Files:**
- Create: `src/orchestrator/api/chat.py`
- Modify: `src/orchestrator/main.py` (construct ChatManager + include router)
- Modify: `src/orchestrator/config.py` (add `chat_workdir_base` setting)
- Modify: `.gitignore`
- Test: `tests/test_api_chat.py`

**Depends on:** Task 3

- [ ] **Step 1: Add the config setting**

In `src/orchestrator/config.py`, add inside `Settings` (after `memory_md_path`):

```python
    chat_workdir_base: str = "data/chat-workdirs"
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_api_chat.py`. This uses the existing test fixtures (`client`, `auth_headers`, a seeded project) from `tests/conftest.py`. Inspect `conftest.py` first; the fixtures below assume a `client` TestClient with `app.state.db` seeded with project id `"p1"`. Adapt fixture names to match conftest if they differ.

```python
"""Integration tests for the chat / tree / file endpoints."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def test_tree_lists_files(client, auth_headers, tmp_path, monkeypatch):
    # Point the chat workdir at a fake repo with a known file.
    repo = tmp_path / "p1"
    (repo / ".git").mkdir(parents=True)
    (repo / "README.md").write_text("hello", encoding="utf-8")
    client.app.state.chat_manager._base = tmp_path

    resp = client.get("/api/projects/p1/tree", headers=auth_headers)
    assert resp.status_code == 200
    names = [n["path"] for n in resp.json()["entries"]]
    assert "README.md" in names
    assert ".git" not in names  # .git is hidden


def test_file_returns_content(client, auth_headers, tmp_path):
    repo = tmp_path / "p1"
    repo.mkdir(parents=True)
    (repo / "README.md").write_text("hello world", encoding="utf-8")
    client.app.state.chat_manager._base = tmp_path

    resp = client.get("/api/projects/p1/file?path=README.md", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["content"] == "hello world"


def test_file_rejects_path_traversal(client, auth_headers, tmp_path):
    repo = tmp_path / "p1"
    repo.mkdir(parents=True)
    client.app.state.chat_manager._base = tmp_path

    resp = client.get("/api/projects/p1/file?path=../../etc/passwd", headers=auth_headers)
    assert resp.status_code == 400


def test_chat_get_history_empty(client, auth_headers):
    resp = client.get("/api/projects/p1/chat", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["messages"] == []


def test_chat_post_requires_auth(client):
    resp = client.post("/api/projects/p1/chat", json={"message": "hi"})
    assert resp.status_code in (401, 403)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_api_chat.py -v`
Expected: FAIL — 404s (routes not registered) / missing `chat_manager`.

- [ ] **Step 4: Implement the endpoints**

Create `src/orchestrator/api/chat.py`:

```python
"""Interactive chat + read-only file tree endpoints."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from orchestrator.api.auth import verify_token


router = APIRouter(tags=["chat"])

_IGNORED = {".git", "node_modules", "__pycache__", ".venv"}


class ChatMessage(BaseModel):
    message: str


def _project_workdir(request: Request, project_id: str) -> Path:
    base = Path(request.app.state.chat_manager._base)
    return base / project_id


async def _require_project(request: Request, project_id: str) -> dict[str, Any]:
    project = await request.app.state.db.fetch_one(
        "SELECT id, repo_url, default_branch FROM projects WHERE id = ?", (project_id,)
    )
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )
    return project


@router.get("/projects/{project_id}/tree")
async def get_tree(
    project_id: str, request: Request, _: None = Depends(verify_token)
) -> dict[str, Any]:
    """Return a flat, sorted list of repo-relative file paths (read-only)."""
    await _require_project(request, project_id)
    workdir = _project_workdir(request, project_id)
    if not workdir.is_dir():
        return {"entries": []}
    entries: list[dict[str, Any]] = []
    for path in sorted(workdir.rglob("*")):
        rel = path.relative_to(workdir)
        if any(part in _IGNORED for part in rel.parts):
            continue
        entries.append({"path": str(rel).replace("\\", "/"), "is_dir": path.is_dir()})
    return {"entries": entries}


@router.get("/projects/{project_id}/file")
async def get_file(
    project_id: str,
    request: Request,
    path: str = Query(...),
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    """Return the raw text content of a repo file. Path is confined to the clone."""
    await _require_project(request, project_id)
    workdir = _project_workdir(request, project_id).resolve()
    target = (workdir / path).resolve()
    if not str(target).startswith(str(workdir)):
        raise HTTPException(status_code=400, detail="Invalid path")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = "[binary file]"
    return {"path": path, "content": content}


@router.get("/projects/{project_id}/chat")
async def get_chat(
    project_id: str, request: Request, _: None = Depends(verify_token)
) -> dict[str, Any]:
    """Return persisted chat history for the project."""
    await _require_project(request, project_id)
    messages = await request.app.state.chat_manager.load_history(project_id)
    return {"messages": messages}


@router.post("/projects/{project_id}/chat")
async def post_chat(
    project_id: str,
    body: ChatMessage,
    request: Request,
    _: None = Depends(verify_token),
) -> dict[str, str]:
    """Accept a user message and run one chat turn in the background.

    Streamed assistant output is published over /api/events (type chat_delta /
    chat_tool / chat_done), so this returns immediately with 'accepted'.
    """
    await _require_project(request, project_id)
    chat_manager = request.app.state.chat_manager

    async def _runner() -> None:
        try:
            await chat_manager.run_turn(project_id, body.message)
        except Exception as exc:  # noqa: BLE001 - surface to UI, don't crash
            request.app.state.event_bus.publish(
                {"type": "chat_error", "project_id": project_id, "error": str(exc)}
            )

    asyncio.create_task(_runner())
    return {"status": "accepted"}
```

- [ ] **Step 5: Wire ChatManager + router into main.py**

In `src/orchestrator/main.py`, inside `lifespan` after the `context_sync` block, add:

```python
    from orchestrator.core.chat_manager import ChatManager

    app.state.chat_manager = ChatManager(
        db=database,
        event_bus=app.state.event_bus,
        github_token=settings.github_token,
        workdir_base=settings.chat_workdir_base,
    )
```

Then with the other router imports near the bottom add:

```python
from orchestrator.api.chat import router as chat_router  # noqa: E402
```

And with the other `include_router` calls:

```python
app.include_router(chat_router, prefix="/api")
```

- [ ] **Step 6: Ignore the workdir**

In `.gitignore`, add:

```
data/chat-workdirs/
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_api_chat.py -v`
Expected: PASS. If fixtures differ, align with `tests/conftest.py` (do not weaken the assertions).

- [ ] **Step 8: Full suite + lint + types + commit**

```bash
uv run pytest --cov=orchestrator -v
uv run ruff check --fix src/ tests/ && uv run ruff format src/ tests/
uv run mypy src/orchestrator/ --ignore-missing-imports
git add src/orchestrator/api/chat.py src/orchestrator/main.py src/orchestrator/config.py .gitignore tests/test_api_chat.py
git commit -m "feat: chat/tree/file API endpoints"
```

---

## Task 5: Web UI — "Claude Code" view

**Files:**
- Modify: `web/index.html`
- Test: manual (documented below)

**Depends on:** Task 4

- [ ] **Step 1: Add the nav item**

In `web/index.html`, after the `memory` nav button (around line 921), add a new nav button following the exact existing pattern:

```html
      <button class="nav-item" type="button" data-view="claudecode" onclick="switchView('claudecode')"><span class="nav-icon"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="5 4 1 8 5 12"/><polyline points="11 4 15 8 11 12"/></svg></span>Claude Code</button>
```

- [ ] **Step 2: Render the view**

Find `switchView(name)` (around line 1169) and the place where each view renders into `#view-container`. Following the pattern used by the `memory` view (search for `switchView('memory')` handling and its render function, e.g. `loadMemory()`), add a `claudecode` branch that calls a new `loadClaudeCode()` and wire it into the same dispatch the other views use.

Add this render function in the `<script>` block (near `loadMemory`):

```javascript
    let ccProjectId = null;

    async function loadClaudeCode() {
      const container = document.getElementById("view-container");
      if (!projects || !projects.length) { projects = await api("GET", "/api/projects"); }
      const options = projects.map(p => `<option value="${p.id}">${p.name}</option>`).join("");
      container.innerHTML = `
        <div class="cc-layout" style="display:grid;grid-template-columns:260px 1fr;gap:12px;height:100%;">
          <aside class="cc-tree" style="overflow:auto;border-right:1px solid var(--border);padding:8px;">
            <select id="cc-project" onchange="ccSelectProject(this.value)" style="width:100%;margin-bottom:8px;">
              <option value="">Select project…</option>${options}
            </select>
            <div id="cc-tree-list"></div>
          </aside>
          <main class="cc-chat" style="display:flex;flex-direction:column;height:100%;">
            <div id="cc-messages" style="flex:1;overflow:auto;padding:12px;"></div>
            <pre id="cc-file" style="display:none;max-height:40%;overflow:auto;background:var(--code-bg);padding:8px;"></pre>
            <form id="cc-form" onsubmit="ccSend(event)" style="display:flex;gap:8px;padding:8px;border-top:1px solid var(--border);">
              <input id="cc-input" placeholder="Ask Opus to read the repo or dispatch a subagent…" style="flex:1;" autocomplete="off"/>
              <button type="submit">Send</button>
            </form>
          </main>
        </div>`;
    }

    async function ccSelectProject(pid) {
      ccProjectId = pid || null;
      if (!ccProjectId) return;
      const tree = await api("GET", "/api/projects/" + ccProjectId + "/tree");
      document.getElementById("cc-tree-list").innerHTML = tree.entries
        .filter(e => !e.is_dir)
        .map(e => `<div class="cc-file-item" style="cursor:pointer;padding:2px 4px;font-size:12px;" onclick="ccOpenFile('${e.path}')">${e.path}</div>`)
        .join("");
      const hist = await api("GET", "/api/projects/" + ccProjectId + "/chat");
      const box = document.getElementById("cc-messages");
      box.innerHTML = "";
      hist.messages.forEach(m => ccAppend(m.role, m.content));
    }

    async function ccOpenFile(path) {
      const data = await api("GET", "/api/projects/" + ccProjectId + "/file?path=" + encodeURIComponent(path));
      const pre = document.getElementById("cc-file");
      pre.style.display = "block";
      pre.textContent = data.content;
    }

    function ccAppend(role, text) {
      const box = document.getElementById("cc-messages");
      const div = document.createElement("div");
      div.className = "cc-msg cc-" + role;
      div.style.cssText = "margin:6px 0;white-space:pre-wrap;";
      div.innerHTML = `<strong>${role === "user" ? "You" : "Opus"}:</strong> <span class="cc-text"></span>`;
      div.querySelector(".cc-text").textContent = text;
      box.appendChild(div);
      box.scrollTop = box.scrollHeight;
      return div.querySelector(".cc-text");
    }

    let ccStreamSpan = null;
    async function ccSend(ev) {
      ev.preventDefault();
      if (!ccProjectId) { alert("Select a project first"); return; }
      const input = document.getElementById("cc-input");
      const text = input.value.trim();
      if (!text) return;
      input.value = "";
      ccAppend("user", text);
      ccStreamSpan = ccAppend("assistant", "");
      await api("POST", "/api/projects/" + ccProjectId + "/chat", { message: text });
    }
```

- [ ] **Step 3: Handle chat SSE events**

Find the global SSE handler (the `EventSource` `onmessage`/`addEventListener` setup around line 1727-1740 / 1978). The events arrive as JSON with a `type` field. Add handling so chat events update the streaming span. In the function that processes incoming events (where other `data.type` cases are handled), add:

```javascript
        if (data.type === "chat_delta" && data.project_id === ccProjectId) {
          if (ccStreamSpan) ccStreamSpan.textContent += data.text;
          const box = document.getElementById("cc-messages");
          if (box) box.scrollTop = box.scrollHeight;
        } else if (data.type === "chat_tool" && data.project_id === ccProjectId) {
          if (ccStreamSpan) ccStreamSpan.textContent += `\n[dispatching: ${data.tool}]\n`;
        } else if (data.type === "chat_error" && data.project_id === ccProjectId) {
          if (ccStreamSpan) ccStreamSpan.textContent += `\n[error: ${data.error}]`;
          ccStreamSpan = null;
        } else if (data.type === "chat_done" && data.project_id === ccProjectId) {
          ccStreamSpan = null;
        }
```

(If the dashboard registers named SSE event types via `addEventListener("<type>", ...)` rather than a single `onmessage` switch, register listeners for `chat_delta`, `chat_tool`, `chat_error`, and `chat_done` mirroring how `agent_log` is registered — match the existing convention.)

- [ ] **Step 4: Manual verification**

Start the server: `uv run uvicorn orchestrator.main:app --host 127.0.0.1 --port 8080`
1. Open http://127.0.0.1:8080, click **Claude Code**.
2. Select a project — the file tree populates; clicking a file shows its content.
3. Type "List the top-level files and tell me what this project does." — assistant text streams in token-by-token.
4. Type "Dispatch a subagent to add a CONTRIBUTING.md." — a `[dispatching: dispatch_agent]` marker appears; confirm a new task shows in the **Tasks** view (PENDING awaiting approval if the project's gate is on, IN_PROGRESS if off).

Document the result (pass/fail per step) in the commit message body.

- [ ] **Step 5: Commit**

```bash
git add web/index.html
git commit -m "feat: Claude Code dashboard view (file tree + interactive chat)"
```

---

## Task 6: Docs + gotchas

**Files:**
- Modify: `CLAUDE.md` (Gotchas + Project Structure)
- Modify: `docs/architecture.md`

**Depends on:** Task 5

- [ ] **Step 1: Update CLAUDE.md Project Structure**

Add the new files to the structure tree in `CLAUDE.md`: `api/chat.py`, `core/chat_manager.py`, `core/chat_dispatch.py`, and `mcp_server.py`.

- [ ] **Step 2: Add gotchas**

Append these bullets to the `## Gotchas` section of `CLAUDE.md`:

```markdown
- **Interactive chat = read + dispatch only** — the Claude Code view runs an
  interactive `claude` session with `cwd` = a per-project clone at
  `data/chat-workdirs/{project_id}/`. Opus reads code with native tools and calls
  the `dispatch_agent` MCP tool; it never edits files. Implementation is done by
  harness subagents via the normal task pipeline.
- **Chat dispatch reuses the autonomous loop** — `dispatch_agent` creates a
  single-task plan (`core/chat_dispatch.py`) exactly like `create_improvement_plan`;
  the existing `run_loop` dispatches/reviews/merges it. Gate off → ACTIVE
  (auto-dispatch), gate on → PENDING (existing Approve button).
- **`process_plan_once` holds any PENDING plan with an `opus_plan`** — not just
  autonomous ones. This stops chat/improvement plans from being re-planned by Opus.
- **MCP server is a separate process** — `orchestrator.mcp_server` is spawned by
  `claude` (stdio), bound to one project via `PRAXIS_PROJECT_ID`, and opens the same
  SQLite DB (WAL). It must never import Docker.
- **Chat needs `--dangerously-skip-permissions`** — like Context Sync, the headless
  `claude` turn cannot answer interactive tool-permission prompts.
```

- [ ] **Step 3: Update architecture.md**

Add a short "Interactive Orchestrator Chat" subsection to `docs/architecture.md` describing the ChatManager → streaming `claude` → MCP `dispatch_agent` → autonomous loop flow, referencing the spec at `docs/superpowers/specs/2026-06-21-interactive-orchestrator-chat-design.md`.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md docs/architecture.md
git commit -m "docs: document interactive orchestrator chat"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1 (no dependencies)
- **Wave 2:** Task 2 (depends on Task 1), Task 3 (depends on Task 1) — independent of each other, run in parallel
- **Wave 3:** Task 4 (depends on Task 3)
- **Wave 4:** Task 5 (depends on Task 4)
- **Wave 5:** Task 6 (depends on Task 5)

Note: Task 4's endpoints only require ChatManager (Task 3). The MCP server (Task 2) is exercised at runtime by `claude`, not imported by the API, so Task 4 does not depend on Task 2 — but Task 2 must be merged before the manual end-to-end dispatch check in Task 5 Step 4 will work.

---

## Notes / Risks

- **`claude` stream-json shape may vary by CLI version.** The parser in
  `ChatManager.parse_stream_line` tolerates unknown event types (returns `None`).
  If session-id capture fails, verify the real event shape with
  `claude -p "hi" --output-format stream-json --verbose` and adjust `parse_stream_line`
  (keep the `ParsedEvent` interface stable so tests hold).
- **MCP SDK API drift.** `FastMCP` is from the `mcp` package; if `mcp.run()` or the
  `@mcp.tool()` decorator differ in the installed version, adapt the wrapper but keep
  the three tool names (`dispatch_agent`, `check_task_status`, `list_active_tasks`)
  and their signatures unchanged — the system prompt and tests depend on them.
- **Concurrency:** one in-flight turn per project is assumed. A second POST while a
  turn streams will start a parallel `claude`; acceptable for v1 (single user). If it
  becomes a problem, add a per-project asyncio lock in `ChatManager`.
- **`python` on PATH inside the container.** The mcp-config launches `python -m
  orchestrator.mcp_server`; ensure the orchestrator image's `python` resolves to the
  venv with the package installed (it does in the existing Dockerfile). Adjust the
  `command` to `sys.executable` if needed.
```
