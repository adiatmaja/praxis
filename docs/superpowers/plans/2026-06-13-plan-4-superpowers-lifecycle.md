# Superpowers Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the interactive Create-Spec chat (`superpowers:brainstorming`), gated plan generation (`superpowers:writing-plans`) with spec edit + notes, and execution checkbox sync — all via subscription `claude -p`.

**Architecture:** A `BrainstormSession` runs brainstorming as a headless multi-turn relay: each user turn is `claude -p --resume <session_id> --output-format stream-json --dangerously-skip-permissions`, executed in the orchestrator container (which already runs `claude`) with cwd set to a per-session clone of the target repo. Streamed assistant chunks are published to the existing `EventBus`/SSE so the dashboard chat updates live. The session stops when it has written + committed the spec. Plan generation is a one-shot `claude -p` running `writing-plans`. As tasks merge, the matching plan-file checkbox flips and `doc_index` re-scans.

**Tech Stack:** Python 3.11, FastAPI, asyncio subprocess, `claude -p` (subscription) with the superpowers plugin, `EventBus` SSE, `GitOps` (git/gh CLI), single-file HTML dashboard.

**Plan-level dependency:** Plan 3 (DocIndexer, `/api/docs`) — specs/plans land in `docs/` and are surfaced by the views built there.

---

### Task 1: Make claude + superpowers available to brainstorming

**Files:**
- Modify: `docker/orchestrator/Dockerfile`
- Create: `tests/test_brainstorm_env.py`

**Depends on:** None

The orchestrator image already invokes `claude` for Opus. Brainstorming additionally needs the superpowers plugin installed and a writable per-session workspace dir.

- [ ] **Step 1: Write the failing test (asserts the workspace base is configurable)**

```python
# tests/test_brainstorm_env.py
def test_brainstorm_workspace_setting_default():
    from orchestrator.config import Settings
    s = Settings(auth_token="x", github_token="y", _env_file=None)
    assert s.brainstorm_workspace == "/tmp/praxis-brainstorm"
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_brainstorm_env.py -v`
Expected: FAIL — no `brainstorm_workspace` setting.

- [ ] **Step 3: Add the setting**

In `src/orchestrator/config.py` add: `brainstorm_workspace: str = "/tmp/praxis-brainstorm"`.

- [ ] **Step 4: Install the superpowers plugin in the orchestrator image**

In `docker/orchestrator/Dockerfile`, after `claude` is installed, add (adjust to the project's plugin install mechanism):

```dockerfile
# Install superpowers skills for in-product brainstorming/writing-plans
RUN claude plugin install superpowers || echo "superpowers install deferred to runtime"
```

(If the plugin must be mounted instead of installed, document the mount in `docker-compose.yml`. Verify the exact `claude plugin` subcommand against the installed CLI during implementation.)

- [ ] **Step 5: Run tests and confirm pass**

Run: `uv run pytest tests/test_brainstorm_env.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/config.py docker/orchestrator/Dockerfile tests/test_brainstorm_env.py
git commit -m "feat: add brainstorm workspace setting + superpowers in orchestrator image"
```

---

### Task 2: `BrainstormSession` — start a session and clone the repo

**Files:**
- Create: `src/orchestrator/core/brainstorm.py`
- Test: `tests/test_brainstorm.py`

**Depends on:** Task 1

- [ ] **Step 1: Write the failing test**

```python
# tests/test_brainstorm.py
import asyncio
from orchestrator.core.brainstorm import BrainstormSession

def test_session_has_id_and_workspace(tmp_path):
    s = BrainstormSession(session_id="abc", workspace=str(tmp_path / "abc"))
    assert s.session_id == "abc"
    assert s.workspace.endswith("abc")

def test_build_args_includes_resume_and_flags():
    s = BrainstormSession(session_id="abc", workspace="/ws")
    args = s._build_args("hello", resume=True)
    assert "--resume" in args and "abc" in args
    assert "--dangerously-skip-permissions" in args
    assert "--output-format" in args and "stream-json" in args
    assert "-p" in args and "hello" in args
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_brainstorm.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the session skeleton**

```python
# src/orchestrator/core/brainstorm.py
"""Headless multi-turn brainstorming session over claude -p."""

from __future__ import annotations

import logging


logger = logging.getLogger(__name__)

BRAINSTORM_BOOTSTRAP = (
    "Use the superpowers:brainstorming skill to design a spec interactively. "
    "Ask the user one question at a time. When the design is approved, write the spec to "
    "docs/superpowers/specs/<date>-<slug>-design.md, commit it, and STOP — do NOT proceed to "
    "writing-plans. The user's opening request follows:\n\n{request}"
)


class BrainstormSession:
    """One interactive brainstorming conversation."""

    def __init__(self, session_id: str, workspace: str) -> None:
        self.session_id = session_id
        self.workspace = workspace

    def _build_args(self, message: str, *, resume: bool) -> list[str]:
        args = [
            "claude", "-p", message,
            "--output-format", "stream-json",
            "--dangerously-skip-permissions",
        ]
        if resume:
            args += ["--resume", self.session_id]
        return args
```

- [ ] **Step 4: Run tests and confirm pass**

Run: `uv run pytest tests/test_brainstorm.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/brainstorm.py tests/test_brainstorm.py
git commit -m "feat: add BrainstormSession arg construction"
```

---

### Task 3: Stream parsing — turn stream-json into chat events

**Files:**
- Modify: `src/orchestrator/core/brainstorm.py`
- Test: `tests/test_brainstorm.py`

**Depends on:** Task 2

- [ ] **Step 1: Write the failing test**

```python
# tests/test_brainstorm.py  (add)
from orchestrator.core.brainstorm import parse_stream_line

def test_parse_assistant_text():
    line = '{"type":"assistant","message":{"content":[{"type":"text","text":"Hi there"}]}}'
    assert parse_stream_line(line) == {"kind": "text", "text": "Hi there"}

def test_parse_result_marks_done():
    line = '{"type":"result","session_id":"abc","is_error":false}'
    assert parse_stream_line(line) == {"kind": "result", "session_id": "abc"}

def test_parse_unknown_returns_none():
    assert parse_stream_line('{"type":"system"}') is None
    assert parse_stream_line("not json") is None
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_brainstorm.py -v`
Expected: FAIL — `parse_stream_line` undefined.

- [ ] **Step 3: Implement the parser**

```python
# add to src/orchestrator/core/brainstorm.py
import json


def parse_stream_line(line: str) -> dict | None:
    """Map one claude -p stream-json line to a chat event, or None to ignore.

    NOTE: verify field names against the installed claude version's stream-json schema
    during implementation; adjust the extraction below if the shape differs.
    """
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    kind = obj.get("type")
    if kind == "assistant":
        parts = obj.get("message", {}).get("content", [])
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
        return {"kind": "text", "text": text} if text else None
    if kind == "result":
        return {"kind": "result", "session_id": obj.get("session_id")}
    return None
```

- [ ] **Step 4: Run tests and confirm pass**

Run: `uv run pytest tests/test_brainstorm.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/brainstorm.py tests/test_brainstorm.py
git commit -m "feat: parse claude stream-json into chat events"
```

---

### Task 4: Run a turn — subprocess, publish chunks, capture session id

**Files:**
- Modify: `src/orchestrator/core/brainstorm.py`
- Test: `tests/test_brainstorm.py`

**Depends on:** Task 3

- [ ] **Step 1: Write the failing test**

```python
# tests/test_brainstorm.py  (add)
import asyncio

def test_run_turn_publishes_text(mocker):
    from orchestrator.core.brainstorm import BrainstormSession
    published = []
    bus = mocker.MagicMock()
    bus.publish = lambda e: published.append(e)
    s = BrainstormSession(session_id="abc", workspace="/ws", event_bus=bus)

    async def fake_lines():
        yield '{"type":"assistant","message":{"content":[{"type":"text","text":"Q1?"}]}}'
        yield '{"type":"result","session_id":"abc","is_error":false}'

    mocker.patch.object(s, "_stream_lines", return_value=fake_lines())
    asyncio.get_event_loop().run_until_complete(s.run_turn("hello", resume=False))
    texts = [e for e in published if e.get("type") == "brainstorm_message"]
    assert any(e["text"] == "Q1?" for e in texts)
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_brainstorm.py -v`
Expected: FAIL — `event_bus`/`run_turn`/`_stream_lines` undefined.

- [ ] **Step 3: Implement subprocess streaming + publish**

```python
# add to src/orchestrator/core/brainstorm.py
import asyncio
from collections.abc import AsyncIterator


class BrainstormSession:  # extend the existing class
    def __init__(self, session_id, workspace, event_bus=None) -> None:
        self.session_id = session_id
        self.workspace = workspace
        self._bus = event_bus

    async def _stream_lines(self, args: list[str]) -> AsyncIterator[str]:
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=self.workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdout is not None
        async for raw in proc.stdout:
            yield raw.decode().strip()
        await proc.wait()

    async def run_turn(self, message: str, *, resume: bool) -> None:
        args = self._build_args(message, resume=resume)
        async for line in self._stream_lines(args):
            event = parse_stream_line(line)
            if event is None:
                continue
            if event["kind"] == "text" and self._bus:
                self._bus.publish({
                    "type": "brainstorm_message",
                    "session_id": self.session_id,
                    "text": event["text"],
                })
            elif event["kind"] == "result":
                if event.get("session_id"):
                    self.session_id = event["session_id"]
                if self._bus:
                    self._bus.publish({
                        "type": "brainstorm_turn_done",
                        "session_id": self.session_id,
                    })
```

(Merge this into the single `BrainstormSession` class defined across Tasks 2–4 — keep `_build_args` from Task 2; don't duplicate the class.)

- [ ] **Step 4: Run tests and confirm pass**

Run: `uv run pytest tests/test_brainstorm.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/brainstorm.py tests/test_brainstorm.py
git commit -m "feat: stream brainstorm turns to the event bus"
```

---

### Task 5: `BrainstormManager` — clone repo, hold sessions, commit/push spec

**Files:**
- Modify: `src/orchestrator/core/brainstorm.py` (add manager)
- Test: `tests/test_brainstorm.py`

**Depends on:** Task 4

- [ ] **Step 1: Write the failing test**

```python
# tests/test_brainstorm.py  (add)
def test_manager_starts_session(mocker, tmp_path):
    from orchestrator.core.brainstorm import BrainstormManager
    mgr = BrainstormManager(workspace_base=str(tmp_path), event_bus=mocker.MagicMock(),
                            github_token="t")
    mocker.patch.object(mgr, "_clone_repo")
    sid = mgr.create_session(repo_url="https://x/y")
    assert sid in mgr._sessions
    mgr._clone_repo.assert_called_once()
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_brainstorm.py -v`
Expected: FAIL — `BrainstormManager` undefined.

- [ ] **Step 3: Implement the manager**

```python
# add to src/orchestrator/core/brainstorm.py
import subprocess
import uuid
from pathlib import Path


class BrainstormManager:
    """Owns active brainstorming sessions and their cloned workspaces."""

    def __init__(self, workspace_base: str, event_bus, github_token: str) -> None:
        self._base = workspace_base
        self._bus = event_bus
        self._token = github_token
        self._sessions: dict[str, BrainstormSession] = {}
        self._started: dict[str, bool] = {}

    def _clone_repo(self, repo_url: str, dest: str) -> None:
        authed = repo_url.replace("https://", f"https://x-access-token:{self._token}@")
        subprocess.run(["git", "clone", "--depth", "50", authed, dest], check=True)

    def create_session(self, repo_url: str) -> str:
        session_id = uuid.uuid4().hex
        workspace = str(Path(self._base) / session_id)
        Path(workspace).mkdir(parents=True, exist_ok=True)
        self._clone_repo(repo_url, workspace)
        self._sessions[session_id] = BrainstormSession(session_id, workspace, self._bus)
        self._started[session_id] = False
        return session_id

    async def send(self, session_id: str, message: str) -> None:
        session = self._sessions[session_id]
        first = not self._started[session_id]
        payload = BRAINSTORM_BOOTSTRAP.format(request=message) if first else message
        self._started[session_id] = True
        await session.run_turn(payload, resume=not first)
```

- [ ] **Step 4: Run tests and confirm pass**

Run: `uv run pytest tests/test_brainstorm.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/brainstorm.py tests/test_brainstorm.py
git commit -m "feat: add BrainstormManager (clone, sessions, bootstrap)"
```

---

### Task 6: Create-Spec API

**Files:**
- Create: `src/orchestrator/api/specs.py`
- Modify: `src/orchestrator/main.py` (construct `BrainstormManager`, register router)
- Test: `tests/test_api_specs.py`

**Depends on:** Task 5

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_specs.py
def test_start_spec_session(client, auth_headers, seeded_project, mocker):
    mocker.patch.object(client.app.state.brainstorm, "create_session", return_value="sess1")
    r = client.post("/api/specs/sessions", headers=auth_headers,
                    json={"project_id": seeded_project, "message": "add login"})
    assert r.status_code == 200
    assert r.json()["session_id"] == "sess1"
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_api_specs.py -v`
Expected: FAIL — route 404.

- [ ] **Step 3: Implement the router**

```python
# src/orchestrator/api/specs.py
"""Interactive Create-Spec session API."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request

from orchestrator.api.auth import verify_token
from pydantic import BaseModel


router = APIRouter(prefix="/api/specs", tags=["specs"])


class StartSession(BaseModel):
    project_id: str
    message: str


class SessionMessage(BaseModel):
    message: str


@router.post("/sessions")
async def start_session(body: StartSession, request: Request,
                        _: None = Depends(verify_token)) -> dict:
    db = request.app.state.db
    project = await db.fetch_one("SELECT repo_url FROM projects WHERE id = ?", (body.project_id,))
    mgr = request.app.state.brainstorm
    session_id = mgr.create_session(repo_url=project["repo_url"])
    asyncio.create_task(mgr.send(session_id, body.message))  # streams via SSE
    return {"session_id": session_id}


@router.post("/sessions/{session_id}/message")
async def send_message(session_id: str, body: SessionMessage, request: Request,
                       _: None = Depends(verify_token)) -> dict:
    asyncio.create_task(request.app.state.brainstorm.send(session_id, body.message))
    return {"status": "accepted"}
```

- [ ] **Step 4: Construct the manager and register the router in `main.py`**

In lifespan (after the event bus exists):

```python
    from orchestrator.core.brainstorm import BrainstormManager
    app.state.brainstorm = BrainstormManager(
        workspace_base=settings.brainstorm_workspace,
        event_bus=app.state.event_bus,
        github_token=settings.github_token,
    )
```

And with the other `app.include_router(...)` calls:

```python
    from orchestrator.api import specs as specs_api
    app.include_router(specs_api.router)
```

- [ ] **Step 5: Run tests and confirm pass**

Run: `uv run pytest tests/test_api_specs.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/api/specs.py src/orchestrator/main.py tests/test_api_specs.py
git commit -m "feat: add Create-Spec session API"
```

---

### Task 7: Create-Spec chat UI

**Files:**
- Modify: `web/index.html` (Create Spec entry + chat panel; consume `brainstorm_message` SSE)

**Depends on:** Task 6

- [ ] **Step 1: Add a "+ Create Spec" action and a chat panel**

Add a button in the topbar that opens a chat modal/panel with a message list and an input box. On open, POST `/api/specs/sessions` with the selected project + first message.

- [ ] **Step 2: Render streamed assistant messages**

In the existing SSE handler (where events are dispatched), add:

```javascript
      if (data.type === "brainstorm_message") {
        appendChatMessage("assistant", data.text);
      } else if (data.type === "brainstorm_turn_done") {
        setChatInputEnabled(true);
      }
```

Implement `appendChatMessage(role, text)` (append a bubble to the chat list) and wire the input's submit to POST `/api/specs/sessions/{id}/message`, disabling input until `brainstorm_turn_done`.

- [ ] **Step 3: Verify end-to-end**

Start the server (Docker, so `claude` + superpowers are present), click **Create Spec**, send an opening request, and confirm the assistant's questions stream into the chat. When the flow finishes, confirm a new spec appears in the **Specs** view (Plan 3) after Refresh.

- [ ] **Step 4: Commit**

```bash
git add web/index.html
git commit -m "feat(ui): add Create-Spec chat panel"
```

---

### Task 8: Generate Plan from a spec (writing-plans) with edit + notes

**Files:**
- Modify: `src/orchestrator/api/specs.py` (add modify + plan endpoints)
- Modify: `src/orchestrator/core/brainstorm.py` (add `generate_plan` one-shot)
- Modify: `web/index.html` (edit spec + Create Plan with notes)
- Test: `tests/test_api_specs.py`

**Depends on:** Task 6

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_specs.py  (add)
def test_generate_plan_invokes_writing_plans(client, auth_headers, seeded_project, mocker):
    gen = mocker.patch.object(client.app.state.brainstorm, "generate_plan",
                              new=mocker.AsyncMock(return_value={"plan_path": "docs/superpowers/plans/x.md"}))
    r = client.post("/api/specs/plan", headers=auth_headers, json={
        "project_id": seeded_project,
        "spec_path": "docs/superpowers/specs/x-design.md",
        "notes": "reuse module Y",
    })
    assert r.status_code == 200
    gen.assert_awaited()
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_api_specs.py::test_generate_plan_invokes_writing_plans -v`
Expected: FAIL — route/`generate_plan` missing.

- [ ] **Step 3: Implement `generate_plan` (one-shot writing-plans)**

```python
# add to BrainstormManager in src/orchestrator/core/brainstorm.py
PLAN_BOOTSTRAP = (
    "Use the superpowers:writing-plans skill. Read the spec at {spec_path}. "
    "Produce a fully self-contained implementation plan — every task executes in a fresh "
    "container with zero prior context, so embed all needed file paths, background, and "
    "acceptance criteria per task. Honor these extra notes: {notes}. "
    "Write the plan to docs/superpowers/plans/ and commit it."
)

    async def generate_plan(self, repo_url: str, spec_path: str, notes: str) -> dict:
        session_id = uuid.uuid4().hex
        workspace = str(Path(self._base) / session_id)
        Path(workspace).mkdir(parents=True, exist_ok=True)
        self._clone_repo(repo_url, workspace)
        session = BrainstormSession(session_id, workspace, self._bus)
        prompt = PLAN_BOOTSTRAP.format(spec_path=spec_path, notes=notes or "none")
        await session.run_turn(prompt, resume=False)
        return {"status": "generated"}
```

- [ ] **Step 4: Add the API endpoints (modify spec + generate plan)**

```python
# add to src/orchestrator/api/specs.py
class ModifySpec(BaseModel):
    project_id: str
    spec_path: str
    content: str

class GeneratePlan(BaseModel):
    project_id: str
    spec_path: str
    notes: str = ""

@router.post("/modify")
async def modify_spec(body: ModifySpec, request: Request, _: None = Depends(verify_token)) -> dict:
    project = await request.app.state.db.fetch_one(
        "SELECT repo_url FROM projects WHERE id = ?", (body.project_id,))
    return await request.app.state.brainstorm.write_and_commit(
        project["repo_url"], body.spec_path, body.content)

@router.post("/plan")
async def generate_plan(body: GeneratePlan, request: Request, _: None = Depends(verify_token)) -> dict:
    project = await request.app.state.db.fetch_one(
        "SELECT repo_url FROM projects WHERE id = ?", (body.project_id,))
    return await request.app.state.brainstorm.generate_plan(
        project["repo_url"], body.spec_path, body.notes)
```

Add a `write_and_commit(repo_url, path, content)` helper to `BrainstormManager` that clones, writes the file, `git commit`s and pushes (reuse the clone + token-auth pattern from `_clone_repo`).

- [ ] **Step 5: Add UI — edit spec + Create Plan with notes**

In the **Specs** view (Plan 3), replace the `alert()` viewer with a panel that shows the spec markdown in an editable textarea (Save → `/api/specs/modify`) and a "Create Plan" action with a notes textarea (→ `/api/specs/plan`).

- [ ] **Step 6: Run tests and confirm pass**

Run: `uv run pytest tests/test_api_specs.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator/api/specs.py src/orchestrator/core/brainstorm.py web/index.html tests/test_api_specs.py
git commit -m "feat: generate plan from spec with edit + notes"
```

---

### Task 9: Sync plan-file checkboxes on task merge

**Files:**
- Modify: `src/orchestrator/core/orchestrator.py` (post-merge hook)
- Modify: `src/orchestrator/core/git_ops.py` (helper to flip a checklist line)
- Test: `tests/test_orchestrator.py`

**Depends on:** Plan 3 Task 5 (DocIndexer), Task 8

- [ ] **Step 1: Write the failing test**

```python
# tests/test_orchestrator.py  (add)
def test_flip_checkbox_marks_task_done():
    from orchestrator.core.git_ops import flip_checklist_item
    md = "## Tasks\n- [ ] Task 1: do thing\n- [ ] Task 2: other"
    out = flip_checklist_item(md, "Task 1: do thing")
    assert "- [x] Task 1: do thing" in out
    assert "- [ ] Task 2: other" in out
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_orchestrator.py::test_flip_checkbox_marks_task_done -v`
Expected: FAIL — `flip_checklist_item` undefined.

- [ ] **Step 3: Implement the pure helper**

```python
# add to src/orchestrator/core/git_ops.py
def flip_checklist_item(markdown: str, item_text: str) -> str:
    """Mark the matching `- [ ]` checklist line as done."""
    needle_unchecked = f"- [ ] {item_text}"
    needle_checked = f"- [x] {item_text}"
    return markdown.replace(needle_unchecked, needle_checked)
```

- [ ] **Step 4: Call it after a task merges and re-index**

In `orchestrator.py`, where a task transitions to MERGED, locate the plan file via `doc_index` (category `plan`, matching the plan), apply `flip_checklist_item` to the file in a fresh clone, commit/push, then `await doc_indexer.scan()` so progress bars update. Publish an event so the dashboard refreshes. (Wire `doc_indexer` into the orchestrator if not already available — pass it in `main.py` like `opus_bridge`.)

- [ ] **Step 5: Run tests and confirm pass**

Run: `uv run pytest tests/test_orchestrator.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/core/orchestrator.py src/orchestrator/core/git_ops.py tests/test_orchestrator.py
git commit -m "feat: flip plan checkboxes and re-index on task merge"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1
- **Wave 2:** Task 2 (Task 1)
- **Wave 3:** Task 3 (Task 2)
- **Wave 4:** Task 4 (Task 3)
- **Wave 5:** Task 5 (Task 4)
- **Wave 6:** Task 6 (Task 5)
- **Wave 7:** Task 7 (Task 6), Task 8 (Task 6)
- **Wave 8:** Task 9 (Task 8, Plan 3 Task 5)

## Notes

- **Skip-permissions containment:** brainstorming runs in the orchestrator container (itself a
  sandbox) with cwd = a per-session repo clone — not on a developer host. This satisfies the
  spec's isolation intent without cross-container stdin plumbing. If stronger isolation is
  later required, `BrainstormSession._stream_lines` can be swapped to exec inside a dedicated
  container without changing callers.
- **Verify against the installed CLI:** the exact `claude -p` stream-json event schema
  (Task 3) and the `claude plugin install` subcommand (Task 1) are version-sensitive — confirm
  during implementation and adjust the extraction/install accordingly.
- **Target-repo docs tie-in:** specs/plans are written into the *cloned target repo* and
  committed/pushed. To surface them in the dashboard, point Plan 3's `docs_root` at the active
  project workspace, or extend `DocIndexer` to scan per-project clones (follow-up).
- Per LLM policy, all reasoning uses subscription `claude -p` — no API key.
