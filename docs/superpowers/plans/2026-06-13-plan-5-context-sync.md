# Context Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After a plan finishes executing, auto-draft updates to the target repo's `CLAUDE.md` and `MEMORY.md`, present the diff on a new Memory page, and commit only on human approval. Keep the project's living docs fresh.

**Architecture:** A `ContextSync` service clones the target repo, runs `claude -p` with `claude-md-management:revise-claude-md` (subscription) to rewrite `CLAUDE.md` + `docs/MEMORY.md` in the working tree without committing, captures the `git diff` as a draft, and holds it pending approval. Plan completion triggers a draft automatically (trigger model A). The Memory page renders current files + the proposed diff with Approve / Edit / Sync-now. Approval commits and pushes the held workspace.

**Tech Stack:** Python 3.11, FastAPI, asyncio subprocess, `claude -p` (subscription, superpowers/claude-md-management), `GitOps` clone/commit/push, single-file HTML dashboard.

**Plan-level dependency:** Plan 4 (per-session repo clone + commit/push helpers in `BrainstormManager`/`GitOps`) — `ContextSync` reuses the same token-auth clone/commit pattern.

---

### Task 1: Settings — MEMORY.md path

**Files:**
- Modify: `src/orchestrator/config.py`
- Test: `tests/test_config.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py  (add)
def test_memory_md_path_default():
    s = Settings(auth_token="x", github_token="y", _env_file=None)
    assert s.memory_md_path == "docs/MEMORY.md"
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_config.py::test_memory_md_path_default -v`
Expected: FAIL — no `memory_md_path`.

- [ ] **Step 3: Add the setting**

In `src/orchestrator/config.py` add: `memory_md_path: str = "docs/MEMORY.md"`.

- [ ] **Step 4: Run tests and confirm pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/config.py tests/test_config.py
git commit -m "feat: add memory_md_path setting (docs/MEMORY.md in target repo)"
```

---

### Task 2: `ContextSync.draft` — clone, revise, capture diff

**Files:**
- Create: `src/orchestrator/core/context_sync.py`
- Test: `tests/test_context_sync.py`

**Depends on:** Task 1

- [ ] **Step 1: Write the failing test**

```python
# tests/test_context_sync.py
import asyncio
from orchestrator.core.context_sync import ContextSync

def test_draft_runs_revise_and_captures_diff(mocker, tmp_path):
    cs = ContextSync(workspace_base=str(tmp_path), github_token="t",
                     memory_md_path="docs/MEMORY.md")
    mocker.patch.object(cs, "_clone_repo")
    mocker.patch.object(cs, "_run_revise", new=mocker.AsyncMock())
    mocker.patch.object(cs, "_git_diff", return_value="+ new line")
    draft = asyncio.get_event_loop().run_until_complete(
        cs.draft(repo_url="https://x/y", summary="merged plan z"))
    assert draft["diff"] == "+ new line"
    assert draft["draft_id"] in cs._drafts
    cs._run_revise.assert_awaited()
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_context_sync.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `draft`**

```python
# src/orchestrator/core/context_sync.py
"""Auto-draft CLAUDE.md / MEMORY.md updates after plan completion."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import uuid
from pathlib import Path


logger = logging.getLogger(__name__)

REVISE_PROMPT = (
    "Use the claude-md-management:revise-claude-md skill. Update CLAUDE.md and {memory_path} "
    "to reflect the work just completed. Do NOT commit — only edit the files in place. "
    "Summary of completed work:\n\n{summary}"
)


class ContextSync:
    """Drafts and (on approval) commits CLAUDE.md / MEMORY.md updates."""

    def __init__(self, workspace_base: str, github_token: str, memory_md_path: str) -> None:
        self._base = workspace_base
        self._token = github_token
        self._memory_path = memory_md_path
        self._drafts: dict[str, dict] = {}

    def _clone_repo(self, repo_url: str, dest: str) -> None:
        authed = repo_url.replace("https://", f"https://x-access-token:{self._token}@")
        subprocess.run(["git", "clone", "--depth", "20", authed, dest], check=True)

    async def _run_revise(self, workspace: str, summary: str) -> None:
        prompt = REVISE_PROMPT.format(memory_path=self._memory_path, summary=summary)
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt, "--dangerously-skip-permissions",
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

    def _git_diff(self, workspace: str) -> str:
        result = subprocess.run(["git", "-C", workspace, "diff"],
                                capture_output=True, text=True, check=True)
        return result.stdout

    async def draft(self, repo_url: str, summary: str) -> dict:
        draft_id = uuid.uuid4().hex
        workspace = str(Path(self._base) / f"ctx-{draft_id}")
        Path(workspace).mkdir(parents=True, exist_ok=True)
        self._clone_repo(repo_url, workspace)
        await self._run_revise(workspace, summary)
        diff = self._git_diff(workspace)
        self._drafts[draft_id] = {"workspace": workspace, "repo_url": repo_url, "diff": diff}
        return {"draft_id": draft_id, "diff": diff}
```

- [ ] **Step 4: Run tests and confirm pass**

Run: `uv run pytest tests/test_context_sync.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/context_sync.py tests/test_context_sync.py
git commit -m "feat: ContextSync draft (clone, revise, capture diff)"
```

---

### Task 3: `ContextSync.approve` and `current`

**Files:**
- Modify: `src/orchestrator/core/context_sync.py`
- Test: `tests/test_context_sync.py`

**Depends on:** Task 2

- [ ] **Step 1: Write the failing test**

```python
# tests/test_context_sync.py  (add)
def test_approve_commits_and_pushes(mocker, tmp_path):
    from orchestrator.core.context_sync import ContextSync
    cs = ContextSync(str(tmp_path), "t", "docs/MEMORY.md")
    ws = tmp_path / "ws"; ws.mkdir()
    cs._drafts["d1"] = {"workspace": str(ws), "repo_url": "https://x/y", "diff": "+x"}
    run = mocker.patch("subprocess.run")
    cs.approve("d1")
    cmds = [c.args[0] for c in run.call_args_list]
    assert any("commit" in c for c in cmds)
    assert any("push" in c for c in cmds)
    assert "d1" not in cs._drafts
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_context_sync.py::test_approve_commits_and_pushes -v`
Expected: FAIL — `approve` undefined.

- [ ] **Step 3: Implement `approve` and `current`**

```python
# add to ContextSync in src/orchestrator/core/context_sync.py
    def approve(self, draft_id: str) -> dict:
        draft = self._drafts.pop(draft_id)
        ws = draft["workspace"]
        subprocess.run(["git", "-C", ws, "add", "-A"], check=True)
        subprocess.run(["git", "-C", ws, "commit", "-m",
                        "docs: sync CLAUDE.md and MEMORY.md"], check=True)
        subprocess.run(["git", "-C", ws, "push"], check=True)
        return {"status": "committed", "draft_id": draft_id}

    def current(self, repo_url: str) -> dict:
        draft_id = uuid.uuid4().hex
        ws = str(Path(self._base) / f"read-{draft_id}")
        Path(ws).mkdir(parents=True, exist_ok=True)
        self._clone_repo(repo_url, ws)
        def _read(rel: str) -> str:
            p = Path(ws) / rel
            return p.read_text(encoding="utf-8") if p.is_file() else ""
        return {"claude_md": _read("CLAUDE.md"), "memory_md": _read(self._memory_path)}
```

- [ ] **Step 4: Run tests and confirm pass**

Run: `uv run pytest tests/test_context_sync.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/context_sync.py tests/test_context_sync.py
git commit -m "feat: ContextSync approve (commit/push) and current"
```

---

### Task 4: Context API

**Files:**
- Create: `src/orchestrator/api/context.py`
- Modify: `src/orchestrator/main.py` (construct `ContextSync`, register router)
- Test: `tests/test_api_context.py`

**Depends on:** Task 3

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_context.py
def test_sync_endpoint_drafts(client, auth_headers, seeded_project, mocker):
    mocker.patch.object(client.app.state.context_sync, "draft",
                        new=mocker.AsyncMock(return_value={"draft_id": "d1", "diff": "+x"}))
    r = client.post(f"/api/projects/{seeded_project}/context-sync",
                    headers=auth_headers, json={"summary": "did stuff"})
    assert r.status_code == 200
    assert r.json()["draft_id"] == "d1"

def test_approve_endpoint(client, auth_headers, mocker):
    mocker.patch.object(client.app.state.context_sync, "approve",
                        return_value={"status": "committed", "draft_id": "d1"})
    r = client.post("/api/context-drafts/d1/approve", headers=auth_headers)
    assert r.status_code == 200
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_api_context.py -v`
Expected: FAIL — routes 404.

- [ ] **Step 3: Implement the router**

```python
# src/orchestrator/api/context.py
"""Context Sync API: current files, draft, approve."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from orchestrator.api.auth import verify_token


router = APIRouter(tags=["context"])


class SyncRequest(BaseModel):
    summary: str = ""


@router.get("/api/projects/{project_id}/context")
async def get_context(project_id: str, request: Request,
                      _: None = Depends(verify_token)) -> dict:
    db = request.app.state.db
    project = await db.fetch_one("SELECT repo_url FROM projects WHERE id = ?", (project_id,))
    return request.app.state.context_sync.current(project["repo_url"])


@router.post("/api/projects/{project_id}/context-sync")
async def sync_context(project_id: str, body: SyncRequest, request: Request,
                       _: None = Depends(verify_token)) -> dict:
    db = request.app.state.db
    project = await db.fetch_one("SELECT repo_url FROM projects WHERE id = ?", (project_id,))
    return await request.app.state.context_sync.draft(project["repo_url"], body.summary)


@router.post("/api/context-drafts/{draft_id}/approve")
async def approve_draft(draft_id: str, request: Request,
                        _: None = Depends(verify_token)) -> dict:
    return request.app.state.context_sync.approve(draft_id)
```

- [ ] **Step 4: Construct + register in `main.py`**

In lifespan:

```python
    from orchestrator.core.context_sync import ContextSync
    app.state.context_sync = ContextSync(
        workspace_base=settings.brainstorm_workspace,
        github_token=settings.github_token,
        memory_md_path=settings.memory_md_path,
    )
```

With the other routers:

```python
    from orchestrator.api import context as context_api
    app.include_router(context_api.router)
```

- [ ] **Step 5: Run tests and confirm pass**

Run: `uv run pytest tests/test_api_context.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/api/context.py src/orchestrator/main.py tests/test_api_context.py
git commit -m "feat: add Context Sync API"
```

---

### Task 5: Auto-draft on plan completion

**Files:**
- Modify: `src/orchestrator/core/orchestrator.py` (plan-completion hook)
- Test: `tests/test_orchestrator.py`

**Depends on:** Task 4

- [ ] **Step 1: Write the failing test**

```python
# tests/test_orchestrator.py  (add)
async def test_plan_completion_triggers_context_draft(orchestrator, mocker):
    draft = mocker.patch.object(orchestrator._context_sync, "draft",
                                new=mocker.AsyncMock(return_value={"draft_id": "d1", "diff": "x"}))
    await orchestrator.on_plan_completed(plan_id="p1")  # all tasks merged
    draft.assert_awaited()
```

(Adapt to the existing `orchestrator` fixture; seed a plan whose tasks are all MERGED.)

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_orchestrator.py::test_plan_completion_triggers_context_draft -v`
Expected: FAIL — no `on_plan_completed` / no `_context_sync`.

- [ ] **Step 3: Wire ContextSync into the orchestrator and call it on completion**

Pass `context_sync` into `Orchestrator.__init__` (from `main.py`, like `opus_bridge`). Add:

```python
    async def on_plan_completed(self, plan_id: str) -> None:
        plan = await self._tq.get_plan(plan_id)
        project = await self._tq.get_project(plan["project_id"])
        summary = f"Completed plan: {plan.get('plan_branch_name') or plan_id}"
        draft = await self._context_sync.draft(project["repo_url"], summary)
        self._bus.publish({"type": "context_draft_ready",
                           "project_id": project["id"], "draft_id": draft["draft_id"]})
```

Call `on_plan_completed` where the loop detects all of a plan's tasks have reached MERGED (next to the existing completion/status-transition logic). Use the actual task-queue accessors that exist (`get_plan`/`get_project` or their equivalents).

- [ ] **Step 4: Run tests and confirm pass**

Run: `uv run pytest tests/test_orchestrator.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/orchestrator.py src/orchestrator/main.py tests/test_orchestrator.py
git commit -m "feat: auto-draft context sync on plan completion"
```

---

### Task 6: Memory page

**Files:**
- Modify: `web/index.html` (nav item, render, diff view, approve/edit/sync, consume `context_draft_ready`)

**Depends on:** Task 4, Task 5

- [ ] **Step 1: Add the nav item**

After the Plan Docs nav button (added in Plan 3), add:

```html
      <button class="nav-item" type="button" data-view="memory" onclick="switchView('memory')"><span class="nav-icon">M</span>Memory</button>
```

- [ ] **Step 2: Render current files + a Sync-now control**

Add a `renderMemory()` that picks the active project, GETs `/api/projects/{id}/context`, and shows `CLAUDE.md` and `MEMORY.md` (rendered/editable), each with a "last synced" hint, plus a **Sync now** button calling `/api/projects/{id}/context-sync`. Route it from `switchView`.

```javascript
    async function renderMemory() {
      const pid = activeProjectId();
      const ctx = await api("GET", "/api/projects/" + pid + "/context");
      document.getElementById("view-container").innerHTML =
        '<div class="list-header"><h2>Memory</h2>' +
        '<button class="btn" type="button" onclick="syncContext()">Sync now</button></div>' +
        '<div class="mem-pane"><h3>CLAUDE.md</h3><pre class="mem-doc">' + esc(ctx.claude_md) + '</pre></div>' +
        '<div class="mem-pane"><h3>MEMORY.md</h3><pre class="mem-doc">' + esc(ctx.memory_md) + '</pre></div>' +
        '<div id="ctx-draft"></div>';
    }

    async function syncContext() {
      const pid = activeProjectId();
      const draft = await api("POST", "/api/projects/" + pid + "/context-sync", { summary: "manual sync" });
      showDraft(draft);
    }

    function showDraft(draft) {
      document.getElementById("ctx-draft").innerHTML =
        '<div class="mem-pane"><h3>Proposed changes</h3><pre class="mem-diff">' + esc(draft.diff || "(no changes)") + '</pre>' +
        (draft.draft_id ? '<button class="btn btn-primary" onclick="approveDraft(\'' + esc(draft.draft_id) + '\')">Approve &amp; commit</button>' : "") +
        '</div>';
    }

    async function approveDraft(id) {
      await api("POST", "/api/context-drafts/" + id + "/approve");
      renderMemory();
    }
```

- [ ] **Step 3: Surface auto-drafts via SSE**

In the SSE dispatcher add:

```javascript
      if (data.type === "context_draft_ready" && currentView === "memory") {
        api("GET", "/api/context-drafts/" + data.draft_id).catch(() => {});
        // simplest: prompt a refresh banner; or re-fetch the draft diff to show
        showDraft({ draft_id: data.draft_id, diff: "" });
      }
```

(If a `GET /api/context-drafts/{id}` is desired to fetch the stored diff, add it to `api/context.py` returning `self._drafts[id]["diff"]`; optional polish.)

- [ ] **Step 4: Add minimal styles**

```css
    .mem-pane { padding: 12px 20px; border-bottom: 1px solid var(--border); }
    .mem-doc { background: var(--panel-alt); padding: 10px; border-radius: 6px; max-height: 320px; overflow: auto; white-space: pre-wrap; font-size: 12px; }
    .mem-diff { background: var(--log-bg); color: var(--log-text); padding: 10px; border-radius: 6px; max-height: 320px; overflow: auto; white-space: pre-wrap; font-size: 12px; }
```

- [ ] **Step 5: Verify in the browser**

Start the server, open **Memory**, confirm CLAUDE.md / MEMORY.md render. Click **Sync now**; confirm a proposed diff appears and **Approve & commit** pushes it (verify the commit in the target repo). Complete a plan and confirm a `context_draft_ready` event surfaces a draft automatically.

- [ ] **Step 6: Commit**

```bash
git add web/index.html
git commit -m "feat(ui): add Memory page with sync + approve"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1
- **Wave 2:** Task 2 (Task 1)
- **Wave 3:** Task 3 (Task 2)
- **Wave 4:** Task 4 (Task 3)
- **Wave 5:** Task 5 (Task 4)
- **Wave 6:** Task 6 (Task 4, Task 5)

## Notes

- **Trigger model A** — drafts are produced automatically on plan completion but never
  committed without explicit approval on the Memory page.
- **MEMORY.md lives in the target repo** (`memory_md_path`, default `docs/MEMORY.md`), so each
  isolated agent clone gets it as context — reinforcing Plan 4's full-context-plans rule.
- `ContextSync` reuses the token-auth clone pattern from Plan 4; if that is refactored into a
  shared `GitOps` helper, point both at it (DRY).
- Per LLM policy, revision uses subscription `claude -p` with `claude-md-management:revise-claude-md`
  — no API key.
- Verify the `claude-md-management` skill name/availability against the installed plugins
  during implementation (same caveat as Plan 4's superpowers install).
