# Configurable Agent Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user choose the Claude reasoning model (and effort) per project with a global default, and actually pass `--model` into every `claude -p` reasoning call (today it passes none, so the setting is inert).

**Architecture:** `OpusBridge` gets a global default model/effort at construction (from `Settings`). Its reasoning methods (`plan_spec`, `review_diff`, `analyze_improvements`) accept an optional per-call `model`/`effort`; `_run_claude_raw` appends `--model` (and effort flag if the CLI supports it) when a model resolves. The orchestrator passes the project's `agent_model`; `None` falls back to the bridge default. New nullable `projects` columns + API fields + a UI selector expose the choice.

**Tech Stack:** Python 3.11, FastAPI, pydantic v2, aiosqlite (raw SQL), `claude -p` CLI; single-file HTML dashboard.

---

### Task 1: Config — replace stale default, add effort

**Files:**
- Modify: `src/orchestrator/config.py:15`
- Test: `tests/test_config.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py  (add to existing file)
def test_agent_model_default_is_opus_4_8():
    s = Settings(auth_token="x", github_token="y", _env_file=None)
    assert s.agent_model == "claude-opus-4-8"
    assert s.agent_model_effort is None
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_config.py::test_agent_model_default_is_opus_4_8 -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'agent_model'`.

- [ ] **Step 3: Update config**

In `src/orchestrator/config.py`, replace the line `agent_model_name: str = "claude-opus-4-6"` with:

```python
    agent_model: str = "claude-opus-4-8"
    agent_model_effort: str | None = None
```

- [ ] **Step 4: Fix the one existing reference**

In `src/orchestrator/api/system.py` (line ~73), change `settings.agent_model_name` to `settings.agent_model`.

- [ ] **Step 5: Run tests and confirm pass**

Run: `uv run pytest tests/test_config.py -v && uv run pytest tests/test_api_system.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/config.py src/orchestrator/api/system.py tests/test_config.py
git commit -m "feat: add configurable agent_model/effort settings (default opus-4-8)"
```

---

### Task 2: Database — add nullable project columns

**Files:**
- Modify: `src/orchestrator/database.py` (projects CREATE TABLE near line 25 + add idempotent ALTERs)
- Test: `tests/test_database.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_database.py  (add)
async def test_projects_have_agent_model_columns(db):
    cols = [r["name"] for r in await db.fetch_all("PRAGMA table_info(projects)")]
    assert "agent_model" in cols
    assert "agent_model_effort" in cols
```

(Use the existing `db` fixture from `tests/conftest.py`. If `fetch_all` returns tuples in your fixture, adapt to `r[1]`.)

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_database.py::test_projects_have_agent_model_columns -v`
Expected: FAIL — assertion error, columns missing.

- [ ] **Step 3: Add the columns to the CREATE TABLE**

In `src/orchestrator/database.py`, in the `CREATE TABLE IF NOT EXISTS projects` block, after the `model_name TEXT NOT NULL DEFAULT '',` line add:

```sql
        agent_model TEXT,
        agent_model_effort TEXT,
```

- [ ] **Step 4: Add idempotent migration for existing DBs**

After the schema `executescript` runs (where the other `CREATE TABLE` statements are applied), add a guarded migration so already-created databases gain the columns:

```python
        for column in ("agent_model", "agent_model_effort"):
            try:
                await self.execute(f"ALTER TABLE projects ADD COLUMN {column} TEXT")
            except Exception:  # noqa: BLE001 - column already exists
                pass
```

(Place this next to where the schema is initialized — same method that runs the CREATE TABLE script.)

- [ ] **Step 5: Run tests and confirm pass**

Run: `uv run pytest tests/test_database.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/database.py tests/test_database.py
git commit -m "feat: add nullable agent_model columns to projects"
```

---

### Task 3: Schemas — expose the fields on the API models

**Files:**
- Modify: `src/orchestrator/models/schemas.py` (`ProjectCreate` ~53, `ProjectUpdate` ~82, `ProjectResponse` ~126)
- Test: `tests/test_schemas.py` (create if absent)

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_schemas.py
from orchestrator.models.schemas import ProjectCreate, ProjectResponse

def test_project_create_accepts_agent_model():
    p = ProjectCreate(name="r", repo_url="u", model_name="m", agent_model="claude-sonnet-4-6")
    assert p.agent_model == "claude-sonnet-4-6"
    assert p.agent_model_effort is None

def test_project_create_agent_model_optional():
    p = ProjectCreate(name="r", repo_url="u", model_name="m")
    assert p.agent_model is None
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_schemas.py -v`
Expected: FAIL — `ProjectCreate` has no `agent_model` (extra field forbidden or attribute missing).

- [ ] **Step 3: Add the fields**

In `ProjectCreate` and `ProjectResponse` add:

```python
    agent_model: str | None = None
    agent_model_effort: str | None = None
```

In `ProjectUpdate` add the same two optional fields (already-optional update model).

- [ ] **Step 4: Run tests and confirm pass**

Run: `uv run pytest tests/test_schemas.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/models/schemas.py tests/test_schemas.py
git commit -m "feat: expose agent_model fields on project schemas"
```

---

### Task 4: Persist + return the fields in the projects API

**Files:**
- Modify: `src/orchestrator/api/projects.py` (create handler + update handler ~94 + response mapping)
- Test: `tests/test_api_projects.py`

**Depends on:** Task 2, Task 3

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_projects.py  (add)
def test_create_project_persists_agent_model(client, auth_headers):
    r = client.post("/api/projects", headers=auth_headers, json={
        "name": "r", "repo_url": "https://x/y", "model_name": "qwen",
        "agent_model": "claude-sonnet-4-6", "agent_model_effort": "low",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["agent_model"] == "claude-sonnet-4-6"
    assert body["agent_model_effort"] == "low"
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_api_projects.py::test_create_project_persists_agent_model -v`
Expected: FAIL — response lacks `agent_model` / value not persisted.

- [ ] **Step 3: Thread the fields through INSERT, UPDATE, and the response mapping**

In the create handler's `INSERT INTO projects (...)`, add the `agent_model, agent_model_effort` columns and bind `payload.agent_model, payload.agent_model_effort`. In the update handler (line ~94), include them in the set of mutable fields (same pattern as `model_name`). Wherever a project row is mapped to `ProjectResponse`, pass the two new columns through.

- [ ] **Step 4: Run tests and confirm pass**

Run: `uv run pytest tests/test_api_projects.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/api/projects.py tests/test_api_projects.py
git commit -m "feat: persist and return agent_model in projects API"
```

---

### Task 5: OpusBridge — accept a default + per-call model and pass --model

**Files:**
- Modify: `src/orchestrator/core/opus_bridge.py` (`__init__` ~92, `_run_claude_raw` ~95, `_run_claude` ~137, `plan_spec`/`review_diff`/`analyze_improvements` ~159-172)
- Test: `tests/test_opus_bridge.py`

**Depends on:** Task 1

- [ ] **Step 1: Write the failing test**

```python
# tests/test_opus_bridge.py  (add)
import asyncio
from orchestrator.core.opus_bridge import OpusBridge

def test_run_claude_raw_passes_model(mocker):
    bridge = OpusBridge(db=mocker.MagicMock(), default_model="claude-opus-4-8")
    captured = {}
    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        proc = mocker.MagicMock()
        async def communicate():
            return (b"ok", b"")
        proc.communicate = communicate
        proc.returncode = 0
        return proc
    mocker.patch("asyncio.create_subprocess_exec", side_effect=fake_exec)
    asyncio.get_event_loop().run_until_complete(bridge._run_claude_raw("hi"))
    assert "--model" in captured["args"]
    assert "claude-opus-4-8" in captured["args"]
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_opus_bridge.py::test_run_claude_raw_passes_model -v`
Expected: FAIL — `OpusBridge.__init__` got unexpected keyword `default_model`, or `--model` not in args.

- [ ] **Step 3: Update `__init__` and `_run_claude_raw`**

Change the constructor and raw runner:

```python
    def __init__(
        self,
        db: Database,
        default_model: str | None = None,
        default_effort: str | None = None,
    ) -> None:
        self._db = db
        self._default_model = default_model
        self._default_effort = default_effort

    async def _run_claude_raw(
        self,
        prompt: str,
        model: str | None = None,
        effort: str | None = None,
    ) -> tuple[int, str, str]:
        resolved_model = model or self._default_model
        resolved_effort = effort or self._default_effort
        args: list[str] = ["claude", "-p", prompt, "--output-format", "text"]
        if resolved_model:
            args += ["--model", resolved_model]
        # NOTE: verify the real claude -p effort flag during implementation; if none
        # exists, delete this block and drop effort end-to-end (see spec open detail).
        if resolved_effort:
            args += ["--reasoning-effort", resolved_effort]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return (proc.returncode or 0, stdout.decode().strip(), stderr.decode().strip())
```

- [ ] **Step 4: Thread model/effort through `_run_claude` and callers**

```python
    async def _run_claude(
        self, prompt: str, model: str | None = None, effort: str | None = None
    ) -> str:
        code, stdout, stderr = await self._run_claude_raw(prompt, model, effort)
        if await self._check_and_handle_rate_limit(code, stdout, stderr):
            raise RuntimeError("Opus rate limited")
        if code != 0:
            raise RuntimeError(f"claude -p failed (exit {code}): {stderr}")
        return stdout
```

Add `model: str | None = None, effort: str | None = None` params to `plan_spec`, `review_diff`, and `analyze_improvements`, and pass them to `self._run_claude(prompt, model, effort)`.

- [ ] **Step 5: Run tests and confirm pass**

Run: `uv run pytest tests/test_opus_bridge.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/core/opus_bridge.py tests/test_opus_bridge.py
git commit -m "feat: OpusBridge passes --model with default + per-call override"
```

---

### Task 6: Wire the default at construction and the per-project override at call sites

**Files:**
- Modify: `src/orchestrator/main.py` (where `OpusBridge(...)` is constructed)
- Modify: `src/orchestrator/core/orchestrator.py:49` (plan_spec call), `:121` (review_diff), `:186` (analyze_improvements)
- Test: `tests/test_orchestrator.py`

**Depends on:** Task 4, Task 5

- [ ] **Step 1: Write the failing test**

```python
# tests/test_orchestrator.py  (add)
async def test_plan_uses_project_agent_model(orchestrator, mocker):
    # project fixture row carries agent_model; assert it is forwarded to plan_spec
    spy = mocker.spy(orchestrator._opus, "plan_spec")
    await orchestrator.plan_and_activate(plan_id="p1")  # use existing helper/fixture
    _, kwargs = spy.call_args
    assert kwargs.get("model") == "claude-sonnet-4-6"
```

(Adapt to the existing `orchestrator` fixture and the method name used in `tests/test_orchestrator.py`; seed the project row with `agent_model="claude-sonnet-4-6"`.)

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_orchestrator.py::test_plan_uses_project_agent_model -v`
Expected: FAIL — `plan_spec` called without `model`.

- [ ] **Step 3: Construct the bridge with the global default**

In `src/orchestrator/main.py`, where `OpusBridge(database)` is constructed, change it to:

```python
    opus_bridge = OpusBridge(
        database,
        default_model=settings.agent_model,
        default_effort=settings.agent_model_effort,
    )
```

- [ ] **Step 4: Forward the per-project model at each call site**

In `src/orchestrator/core/orchestrator.py`:

- Line ~49: `opus_plan = await self._opus.plan_spec(plan["spec"], project["repo_url"], model=project.get("agent_model"), effort=project.get("agent_model_effort"))`
- Line ~121: add `model=project.get("agent_model"), effort=project.get("agent_model_effort")` to the `review_diff(...)` call (ensure the `project` row is in scope; fetch it if needed).
- Line ~186: add the same kwargs to the `analyze_improvements(...)` call.

`None` values fall back to the bridge default — no further logic needed.

- [ ] **Step 5: Run tests and confirm pass**

Run: `uv run pytest tests/test_orchestrator.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/main.py src/orchestrator/core/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: forward per-project agent_model into Opus reasoning calls"
```

---

### Task 7: UI — model selector (presets + custom) in project settings

**Files:**
- Modify: `web/index.html` (project create/edit form)

**Depends on:** Task 4

- [ ] **Step 1: Add the selector markup to the project form**

In the project form (where `model_name` is entered), add a preset dropdown and a custom field:

```html
<div class="formrow">
  <label>Agent model (reasoning)</label>
  <select id="pf-agent-model-preset" onchange="onAgentModelPreset(this.value)">
    <option value="">Global default</option>
    <option value="claude-opus-4-8|">Opus (claude-opus-4-8)</option>
    <option value="claude-opus-4-8|low">Opus — low effort</option>
    <option value="claude-sonnet-4-6|">Sonnet (claude-sonnet-4-6)</option>
    <option value="claude-haiku-4-5|">Haiku (claude-haiku-4-5)</option>
    <option value="custom">Custom…</option>
  </select>
  <input id="pf-agent-model" placeholder="custom model id" style="display:none;margin-top:6px;">
</div>
```

- [ ] **Step 2: Add the preset handler and include the value on submit**

```javascript
function onAgentModelPreset(value) {
  const custom = document.getElementById("pf-agent-model");
  if (value === "custom") { custom.style.display = "block"; return; }
  custom.style.display = "none";
  const [model, effort] = value.split("|");
  custom.dataset.model = model || "";
  custom.dataset.effort = effort || "";
}
```

In the project submit handler, include in the POST/PATCH body:

```javascript
agent_model: (document.getElementById("pf-agent-model").style.display === "block"
  ? document.getElementById("pf-agent-model").value
  : document.getElementById("pf-agent-model").dataset.model) || null,
agent_model_effort: document.getElementById("pf-agent-model").dataset.effort || null,
```

- [ ] **Step 3: Verify end-to-end in the browser**

Start the server, create a project with "Sonnet" selected, then `GET /api/projects` and confirm `agent_model: "claude-sonnet-4-6"`. Select "Global default" and confirm it persists as `null`.

- [ ] **Step 4: Commit**

```bash
git add web/index.html
git commit -m "feat(ui): add agent model selector to project settings"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1, Task 2, Task 3 (no dependencies — run in parallel)
- **Wave 2:** Task 4 (Task 2, Task 3), Task 5 (Task 1)
- **Wave 3:** Task 6 (Task 4, Task 5), Task 7 (Task 4)

## Notes

- The implement-step model (`projects.model_name` via LM Studio) is untouched.
- Per LLM policy, no Anthropic API key path is added — model selection only changes the
  `--model` argument to the subscription `claude -p` CLI.
- The effort flag is unverified; Task 5 Step 3 flags it. If `claude -p` has no effort flag,
  drop `agent_model_effort` from config/schema/DB/UI in a follow-up — the model selection
  still stands on its own.
