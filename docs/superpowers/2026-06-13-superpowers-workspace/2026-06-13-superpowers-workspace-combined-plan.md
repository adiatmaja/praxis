# Superpowers Workspace — Combined Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Execute **Part 1 → Part 5 in order**; within each part follow that part's own Parallel Execution Map.

**Goal:** Turn Praxis into a Superpowers-driven spec → plan → execute → learn workspace, plus configurable agent models. This file merges all five plans into one executable document.

**Specs:** [Superpowers Workspace](specs/2026-06-13-superpowers-workspace-design.md) · [Configurable Agent Model](specs/2026-06-13-configurable-agent-model-design.md)

**Tech Stack:** Python 3.11, FastAPI, aiosqlite (raw SQL), `claude -p` (subscription) with superpowers/claude-md-management plugins, LM Studio (local OSS fallback), EventBus SSE, Docker, single-file HTML dashboard.

---

## Global Execution Order

| Part | Unit | Tasks | Depends on |
|------|------|-------|------------|
| 1 | A — Dashboard layout fix | 2 | — |
| 2 | Configurable agent model | 7 | — |
| 3 | B — Docs-aware Specs/Plans views | 8 | Part 2 |
| 4 | C — Superpowers lifecycle | 9 | Part 3 |
| 5 | D — Context Sync | 6 | Part 4 |

```
  Part 1 (A) ── independent
  Part 2 (model) ── independent
  Part 3 (B) ── needs Part 2
  Part 4 (C) ── needs Part 3
  Part 5 (D) ── needs Part 4
```

Parts 1 and 2 can run in parallel; 3→4→5 are a chain. Task numbers restart per part (e.g. "Part 3, Task 1"); each part's intra-plan waves are in its own Parallel Execution Map below.

## Cross-cutting principles (all parts)

- Subscription `claude -p` + local OSS (LM Studio) only — **never** an Anthropic API key.
- Docs own spec/plan content; SQLite owns runtime state + a thin index.
- Plans are fully self-contained (each task runs in a fresh, memoryless container).
- Human review gates: spec commit, plan trigger, context-sync approval.

## Verify-during-implementation flags

- `claude -p` reasoning-**effort** flag (Part 2) — confirm it exists or drop effort.
- `claude -p --output-format stream-json` event **schema** (Part 4) — confirm field shapes.
- `claude plugin install` subcommand + `superpowers` / `claude-md-management` skill names
  (Parts 4–5) — confirm against the installed CLI/plugins.


---

## Part 1: Dashboard Layout Fix (Unit A)



**Goal:** Stop the dashboard from rendering the health bar as a side column, so the health bar is a full-width top bar and the swim lanes fill the content area with no empty gutter.

**Architecture:** The dashboard view injects its HTML into `#view-container`, which carries the `.master-detail` class (`display:flex`, default row direction). That makes the health bar and `.dashboard-body` side-by-side flex items. Fix: wrap the dashboard's own markup in a dedicated column flex shell (`.dashboard-shell`) so it stacks vertically regardless of the container's flex direction, and verify with a Playwright layout assertion.

**Tech Stack:** Single-file HTML/CSS/JS dashboard (`web/index.html`); Playwright (already installed) for the layout verification; FastAPI server for serving the page.

---

### Task 1: Add a Playwright layout check that fails on the current bug

**Files:**
- Create: `tests/visual/check_dashboard_layout.mjs`
- Create: `tests/visual/package.json`

**Depends on:** None

This is a runnable assertion, not a pytest test — the bug is a CSS/DOM layout issue. It drives a headless browser against a running server, sets the dev token, and asserts the health bar spans the full content width and sits above the lanes with no left gutter.

- [ ] **Step 1: Create the verification script**

```javascript
// tests/visual/check_dashboard_layout.mjs
// Usage: node check_dashboard_layout.mjs
// Requires the server running at BASE (default http://127.0.0.1:8080)
// and AUTH token in TOKEN (default local-dev-token-praxis).
import { chromium } from 'playwright';

const BASE = process.env.BASE || 'http://127.0.0.1:8080';
const TOKEN = process.env.TOKEN || 'local-dev-token-praxis';

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await ctx.newPage();
await page.goto(BASE, { waitUntil: 'domcontentloaded' });
await page.evaluate(t => localStorage.setItem('praxis_token', t), TOKEN);
await page.reload({ waitUntil: 'domcontentloaded' });
await page.waitForSelector('.health-bar', { timeout: 10000 });
await page.waitForTimeout(2000);

const m = await page.evaluate(() => {
  const hb = document.querySelector('.health-bar').getBoundingClientRect();
  const body = document.querySelector('.dashboard-body').getBoundingClientRect();
  const container = document.getElementById('view-container').getBoundingClientRect();
  const lanes = document.querySelector('.dashboard-lanes')?.getBoundingClientRect() || null;
  return { hb, body, container, lanes };
});

const errors = [];
// Health bar must span (almost) the full content width, not a ~430px column.
if (m.hb.width < m.container.width * 0.9) {
  errors.push(`health bar width ${Math.round(m.hb.width)} < 90% of container ${Math.round(m.container.width)} (rendered as a column)`);
}
// Health bar must sit ABOVE the body, not beside it.
if (m.hb.bottom > m.body.top + 2) {
  errors.push(`health bar bottom ${Math.round(m.hb.bottom)} overlaps body top ${Math.round(m.body.top)} (side-by-side, not stacked)`);
}
// Lanes must start near the left edge of the content area (no big gutter).
if (m.lanes && m.lanes.left - m.container.left > 24) {
  errors.push(`lanes left gutter ${Math.round(m.lanes.left - m.container.left)}px too large`);
}

await browser.close();
if (errors.length) {
  console.error('LAYOUT CHECK FAILED:\n- ' + errors.join('\n- '));
  process.exit(1);
}
console.log('LAYOUT CHECK PASSED');
```

- [ ] **Step 2: Create a minimal package.json so the script can resolve Playwright**

```json
{
  "name": "praxis-visual-checks",
  "private": true,
  "type": "module",
  "dependencies": { "playwright": "1.60.0" }
}
```

- [ ] **Step 3: Install Playwright for the check**

Run: `cd tests/visual && npm install && npx playwright install chromium`
Expected: installs without error; `node_modules/playwright` exists.

- [ ] **Step 4: Start the server in another terminal**

Run: `uv run uvicorn orchestrator.main:app --host 127.0.0.1 --port 8080`
Expected: `Application startup complete` and `{"status":"ok"}` from `curl http://127.0.0.1:8080/health`.

- [ ] **Step 5: Run the check and confirm it FAILS on the current layout**

Run: `cd tests/visual && node check_dashboard_layout.mjs`
Expected: FAIL — `LAYOUT CHECK FAILED` with "health bar width ... (rendered as a column)" (requires at least one active/pending plan in the DB so the dashboard renders lanes; if the DB is empty, seed one via the API first).

- [ ] **Step 6: Commit the failing check**

```bash
git add tests/visual/check_dashboard_layout.mjs tests/visual/package.json
git commit -m "test: add failing dashboard layout check"
```

---

### Task 2: Wrap the dashboard in a column shell so it stacks

**Files:**
- Modify: `web/index.html:374` (add `.dashboard-shell` CSS near `.dashboard-body`)
- Modify: `web/index.html:1106-1111` (`renderDashboard()` innerHTML assembly)

**Depends on:** Task 1

- [ ] **Step 1: Add the `.dashboard-shell` style**

In `web/index.html`, immediately before the existing `.dashboard-body` rule (currently at line 374), add:

```css
    .dashboard-shell { flex: 1; min-height: 0; display: flex; flex-direction: column; }
```

This gives the dashboard its own vertical stacking context, independent of `.master-detail`'s row direction.

- [ ] **Step 2: Wrap the rendered markup in the shell**

In `renderDashboard()` (currently lines 1106-1111), replace:

```javascript
      const container = document.getElementById("view-container");
      container.innerHTML =
        renderHealthBar(attentionCount) +
        '<div class="dashboard-body">' +
          '<div class="dashboard-lanes">' + bodyHtml + '</div>' +
          '<div class="side-panel" id="dashboard-side-panel"></div>' +
        '</div>';
```

with:

```javascript
      const container = document.getElementById("view-container");
      container.innerHTML =
        '<div class="dashboard-shell">' +
          renderHealthBar(attentionCount) +
          '<div class="dashboard-body">' +
            '<div class="dashboard-lanes">' + bodyHtml + '</div>' +
            '<div class="side-panel" id="dashboard-side-panel"></div>' +
          '</div>' +
        '</div>';
```

- [ ] **Step 3: Re-run the layout check and confirm it PASSES**

Run: `cd tests/visual && node check_dashboard_layout.mjs`
Expected: `LAYOUT CHECK PASSED`.

- [ ] **Step 4: Visually confirm dark theme and the 768px breakpoint**

Run the server, open `http://127.0.0.1:8080`, toggle the theme (the theme button), and resize the window below 768px. Expected: health bar is a full-width top bar in both themes; lanes start at the left edge; side panel still opens to the right (and overlays full-screen below 768px per the existing `@media` rule at `web/index.html:496`).

- [ ] **Step 5: Commit the fix**

```bash
git add web/index.html
git commit -m "fix(ui): stack dashboard health bar above lanes (kill empty gutter)"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1 (no dependencies)
- **Wave 2:** Task 2 (depends on Task 1)

All tasks are sequential — the check must exist and fail before the fix makes it pass.

## Notes

- The `.master-detail` class is shared by other views (Projects, Plans, Tasks); do **not**
  change its `display`/`flex-direction` — that would break those views. The fix is scoped to
  the dashboard via the new `.dashboard-shell` wrapper.
- If `data/orchestrator.db` is empty, the dashboard shows the idle state and the check's lane
  assertion is skipped (it guards on `.dashboard-lanes` existing). Seed at least one
  active/pending plan to exercise the full layout (see `CLAUDE.local.md` curl examples).

---

## Part 2: Configurable Agent Model



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

---

## Part 3: Docs-Aware Specs & Plans Views (Unit B)



**Goal:** Make `docs/` the source of truth for specs and plans: scan markdown, classify each file (deterministic markers first, Haiku fallback), parse plan checklists into progress, index it in SQLite, and surface Specs/Plans views with progress bars and a Refresh button.

**Architecture:** A `DocIndexer` service walks a configurable docs root, hashes each file, skips unchanged ones, classifies the rest (markers → Haiku via `OpusBridge` → local LM Studio fallback), parses `- [ ]`/`- [x]` into progress, and upserts a thin `doc_index` row. A `/api/docs` router lists/serves/refreshes. The dashboard gains Specs and Plans views.

**Tech Stack:** Python 3.11, FastAPI, aiosqlite (raw SQL), `claude -p --model claude-haiku-4-5` (subscription, via `OpusBridge`), LM Studio fallback (OpenAI-compatible), single-file HTML dashboard.

**Plan-level dependency:** Plan 2 (OpusBridge accepts a per-call `model`) — the Haiku classification call reuses it.

---

### Task 1: `doc_index` table

**Files:**
- Modify: `src/orchestrator/database.py` (append to `MIGRATIONS`, ends line 93)
- Test: `tests/test_database.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_database.py  (add)
async def test_doc_index_table_exists(db):
    rows = await db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='doc_index'"
    )
    assert len(rows) == 1
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_database.py::test_doc_index_table_exists -v`
Expected: FAIL — table missing.

- [ ] **Step 3: Append the migration**

In `src/orchestrator/database.py`, add this string as the last element of the `MIGRATIONS` tuple (before the closing `)` on line 93):

```python
    """
    CREATE TABLE IF NOT EXISTS doc_index (
        path TEXT PRIMARY KEY,
        category TEXT NOT NULL,
        title TEXT,
        content_hash TEXT NOT NULL,
        branch TEXT,
        done_count INTEGER NOT NULL DEFAULT 0,
        total_count INTEGER NOT NULL DEFAULT 0,
        classified_by TEXT NOT NULL DEFAULT 'marker',
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
```

- [ ] **Step 4: Run tests and confirm pass**

Run: `uv run pytest tests/test_database.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/database.py tests/test_database.py
git commit -m "feat: add doc_index table"
```

---

### Task 2: Pure markdown helpers (hash, title, checklist progress)

**Files:**
- Create: `src/orchestrator/core/markdown_utils.py`
- Test: `tests/test_markdown_utils.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_markdown_utils.py
from orchestrator.core.markdown_utils import content_hash, extract_title, checklist_progress

def test_content_hash_stable():
    assert content_hash("abc") == content_hash("abc")
    assert content_hash("abc") != content_hash("abd")

def test_extract_title_from_h1():
    assert extract_title("# My Spec\n\nbody") == "My Spec"

def test_extract_title_none_when_absent():
    assert extract_title("no heading here") is None

def test_checklist_progress_counts_checkboxes():
    md = "- [x] done\n- [ ] todo\n- [X] also done\nnot a box"
    assert checklist_progress(md) == (2, 3)

def test_checklist_progress_zero_when_none():
    assert checklist_progress("plain text") == (0, 0)
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_markdown_utils.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the helpers**

```python
# src/orchestrator/core/markdown_utils.py
"""Pure helpers for parsing markdown docs."""

from __future__ import annotations

import hashlib
import re


_CHECKBOX = re.compile(r"^\s*-\s\[( |x|X)\]\s", re.MULTILINE)
_H1 = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def content_hash(text: str) -> str:
    """Return a stable hex digest of the file content."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_title(text: str) -> str | None:
    """Return the first H1 heading text, or None."""
    match = _H1.search(text)
    return match.group(1) if match else None


def checklist_progress(text: str) -> tuple[int, int]:
    """Return (done, total) markdown task checkboxes."""
    boxes = _CHECKBOX.findall(text)
    done = sum(1 for state in boxes if state in ("x", "X"))
    return done, len(boxes)
```

- [ ] **Step 4: Run tests and confirm pass**

Run: `uv run pytest tests/test_markdown_utils.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/markdown_utils.py tests/test_markdown_utils.py
git commit -m "feat: add markdown parsing helpers"
```

---

### Task 3: Deterministic classifier

**Files:**
- Modify: `src/orchestrator/core/markdown_utils.py`
- Test: `tests/test_markdown_utils.py`

**Depends on:** Task 2

Returns `"spec"`, `"plan"`, or `None` (ambiguous → needs LLM).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_markdown_utils.py  (add)
from orchestrator.core.markdown_utils import classify_by_marker

def test_classify_plan_dir():
    assert classify_by_marker("docs/superpowers/plans/x.md", "# x") == "plan"

def test_classify_spec_dir():
    assert classify_by_marker("docs/superpowers/specs/x.md", "# x") == "spec"

def test_classify_plan_by_checklist():
    assert classify_by_marker("docs/notes/x.md", "## Tasks\n- [ ] do it") == "plan"

def test_classify_frontmatter_type():
    assert classify_by_marker("docs/x.md", "---\ntype: spec\n---\n# x") == "spec"

def test_classify_ambiguous_returns_none():
    assert classify_by_marker("docs/random.md", "just prose") is None
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_markdown_utils.py -v`
Expected: FAIL — `classify_by_marker` undefined.

- [ ] **Step 3: Implement it**

```python
# add to src/orchestrator/core/markdown_utils.py
_FRONTMATTER_TYPE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
_TYPE_LINE = re.compile(r"^type:\s*(spec|plan)\s*$", re.MULTILINE)
_TASKS_HEADING = re.compile(r"^##\s+Tasks\b", re.MULTILINE | re.IGNORECASE)


def classify_by_marker(path: str, text: str) -> str | None:
    """Deterministic classification; None when ambiguous."""
    fm = _FRONTMATTER_TYPE.search(text)
    if fm:
        type_match = _TYPE_LINE.search(fm.group(1))
        if type_match:
            return type_match.group(1)
    normalized = path.replace("\\", "/")
    if "/plans/" in normalized:
        return "plan"
    if "/specs/" in normalized:
        return "spec"
    if _TASKS_HEADING.search(text) and checklist_progress(text)[1] > 0:
        return "plan"
    return None
```

- [ ] **Step 4: Run tests and confirm pass**

Run: `uv run pytest tests/test_markdown_utils.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/markdown_utils.py tests/test_markdown_utils.py
git commit -m "feat: add deterministic doc classifier"
```

---

### Task 4: Haiku classification with local fallback

**Files:**
- Modify: `src/orchestrator/core/opus_bridge.py` (add `classify_doc`)
- Test: `tests/test_opus_bridge.py`

**Depends on:** Plan 2 Task 5 (per-call `model` support)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_opus_bridge.py  (add)
import asyncio

def test_classify_doc_uses_haiku(mocker):
    from orchestrator.core.opus_bridge import OpusBridge
    bridge = OpusBridge(db=mocker.MagicMock())
    run = mocker.patch.object(
        bridge, "_run_claude", new=mocker.AsyncMock(return_value="plan")
    )
    result = asyncio.get_event_loop().run_until_complete(
        bridge.classify_doc("some ambiguous markdown")
    )
    assert result == "plan"
    assert run.call_args.kwargs.get("model") == "claude-haiku-4-5"

def test_classify_doc_normalizes_unexpected_to_other(mocker):
    from orchestrator.core.opus_bridge import OpusBridge
    bridge = OpusBridge(db=mocker.MagicMock())
    mocker.patch.object(bridge, "_run_claude", new=mocker.AsyncMock(return_value="garbage"))
    result = asyncio.get_event_loop().run_until_complete(bridge.classify_doc("x"))
    assert result == "other"
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_opus_bridge.py -v`
Expected: FAIL — `classify_doc` undefined.

- [ ] **Step 3: Implement `classify_doc`**

```python
# add to OpusBridge in src/orchestrator/core/opus_bridge.py
    CLASSIFY_PROMPT = (
        "Classify this markdown document as exactly one word: 'spec', 'plan', or 'other'. "
        "A spec describes WHAT to build; a plan is a step-by-step implementation checklist. "
        "Reply with only the single word.\n\n---\n{text}"
    )

    async def classify_doc(self, text: str) -> str:
        """Classify ambiguous markdown via Haiku; returns spec|plan|other."""
        prompt = self.CLASSIFY_PROMPT.format(text=text[:4000])
        raw = (await self._run_claude(prompt, model="claude-haiku-4-5")).strip().lower()
        for category in ("spec", "plan", "other"):
            if category in raw:
                return category
        return "other"
```

(Local LM Studio fallback is wired in Task 5, where the indexer catches a classification error and falls back — keeping the bridge focused on the subscription path.)

- [ ] **Step 4: Run tests and confirm pass**

Run: `uv run pytest tests/test_opus_bridge.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/opus_bridge.py tests/test_opus_bridge.py
git commit -m "feat: add Haiku doc classification to OpusBridge"
```

---

### Task 5: `DocIndexer` service (scan, cache, classify, upsert)

**Files:**
- Create: `src/orchestrator/core/doc_indexer.py`
- Modify: `src/orchestrator/config.py` (add `docs_root`)
- Test: `tests/test_doc_indexer.py`

**Depends on:** Task 1, Task 3, Task 4

- [ ] **Step 1: Add the `docs_root` setting**

In `src/orchestrator/config.py` add: `docs_root: str = "docs"`.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_doc_indexer.py
import pytest
from orchestrator.core.doc_indexer import DocIndexer

@pytest.fixture
def docs_dir(tmp_path):
    (tmp_path / "specs").mkdir()
    (tmp_path / "plans").mkdir()
    (tmp_path / "specs" / "a.md").write_text("# Spec A\n\nwhat to build", encoding="utf-8")
    (tmp_path / "plans" / "b.md").write_text("# Plan B\n- [x] one\n- [ ] two", encoding="utf-8")
    return tmp_path

async def test_scan_indexes_specs_and_plans(db, docs_dir, mocker):
    classifier = mocker.AsyncMock()  # not called — both are marker-classified
    indexer = DocIndexer(db=db, docs_root=str(docs_dir), classify=classifier)
    await indexer.scan()
    rows = {r["path"]: r for r in await db.fetch_all("SELECT * FROM doc_index")}
    assert any(r["category"] == "spec" for r in rows.values())
    plan = next(r for r in rows.values() if r["category"] == "plan")
    assert (plan["done_count"], plan["total_count"]) == (1, 2)
    classifier.assert_not_awaited()

async def test_scan_skips_unchanged(db, docs_dir, mocker):
    indexer = DocIndexer(db=db, docs_root=str(docs_dir), classify=mocker.AsyncMock())
    first = await indexer.scan()
    second = await indexer.scan()
    assert first["scanned"] >= 2
    assert second["reused"] >= 2

async def test_scan_calls_classifier_for_ambiguous(db, tmp_path, mocker):
    (tmp_path / "loose.md").write_text("# Loose\n\nambiguous prose", encoding="utf-8")
    classify = mocker.AsyncMock(return_value="spec")
    indexer = DocIndexer(db=db, docs_root=str(tmp_path), classify=classify)
    await indexer.scan()
    classify.assert_awaited()
```

- [ ] **Step 3: Run it and confirm it fails**

Run: `uv run pytest tests/test_doc_indexer.py -v`
Expected: FAIL — module not found.

- [ ] **Step 4: Implement the indexer**

```python
# src/orchestrator/core/doc_indexer.py
"""Scan a docs root, classify markdown, and index it in SQLite."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from orchestrator.core.markdown_utils import (
    checklist_progress,
    classify_by_marker,
    content_hash,
    extract_title,
)
from orchestrator.database import Database


logger = logging.getLogger(__name__)

Classifier = Callable[[str], Awaitable[str]]


class DocIndexer:
    """Walks docs_root, classifies markdown, upserts doc_index rows."""

    def __init__(self, db: Database, docs_root: str, classify: Classifier) -> None:
        self._db = db
        self._root = Path(docs_root)
        self._classify = classify

    async def scan(self) -> dict[str, int]:
        scanned = reused = 0
        existing = {
            row["path"]: row["content_hash"]
            for row in await self._db.fetch_all("SELECT path, content_hash FROM doc_index")
        }
        if not self._root.exists():
            return {"scanned": 0, "reused": 0}
        for file in sorted(self._root.rglob("*.md")):
            rel = str(file.relative_to(self._root.parent)).replace("\\", "/")
            text = file.read_text(encoding="utf-8")
            digest = content_hash(text)
            if existing.get(rel) == digest:
                reused += 1
                continue
            category = classify_by_marker(rel, text)
            classified_by = "marker"
            if category is None:
                try:
                    category = await self._classify(text)
                    classified_by = "haiku"
                except Exception as exc:  # noqa: BLE001 - LLM/CLI failure
                    logger.warning("Classification failed for %s: %s", rel, exc)
                    category = "other"
                    classified_by = "fallback"
            done, total = checklist_progress(text)
            await self._upsert(rel, category, extract_title(text), digest, done, total, classified_by)
            scanned += 1
        return {"scanned": scanned, "reused": reused}

    async def _upsert(self, path, category, title, digest, done, total, by) -> None:
        await self._db.execute(
            """
            INSERT INTO doc_index (path, category, title, content_hash, done_count,
                                   total_count, classified_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(path) DO UPDATE SET
                category=excluded.category, title=excluded.title,
                content_hash=excluded.content_hash, done_count=excluded.done_count,
                total_count=excluded.total_count, classified_by=excluded.classified_by,
                updated_at=CURRENT_TIMESTAMP
            """,
            (path, category, title, digest, done, total, by),
        )
```

- [ ] **Step 5: Run tests and confirm pass**

Run: `uv run pytest tests/test_doc_indexer.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/core/doc_indexer.py src/orchestrator/config.py tests/test_doc_indexer.py
git commit -m "feat: add DocIndexer scan/cache/classify/upsert"
```

---

### Task 6: `/api/docs` router

**Files:**
- Create: `src/orchestrator/api/docs.py`
- Modify: `src/orchestrator/models/schemas.py` (add `DocResponse`)
- Test: `tests/test_api_docs.py`

**Depends on:** Task 5

- [ ] **Step 1: Add the response schema**

```python
# add to src/orchestrator/models/schemas.py
class DocResponse(BaseModel):
    path: str
    category: str
    title: str | None = None
    branch: str | None = None
    done_count: int = 0
    total_count: int = 0
    classified_by: str = "marker"
    updated_at: str | None = None
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_api_docs.py
def test_list_docs_filtered(client, auth_headers, db_with_docs):
    r = client.get("/api/docs?category=plan", headers=auth_headers)
    assert r.status_code == 200
    assert all(d["category"] == "plan" for d in r.json())

def test_refresh_docs(client, auth_headers):
    r = client.post("/api/docs/refresh", headers=auth_headers)
    assert r.status_code == 200
    assert "scanned" in r.json()
```

(Add a `db_with_docs` fixture in `tests/conftest.py` that inserts two `doc_index` rows — one spec, one plan.)

- [ ] **Step 3: Run it and confirm it fails**

Run: `uv run pytest tests/test_api_docs.py -v`
Expected: FAIL — 404, route not registered.

- [ ] **Step 4: Implement the router**

```python
# src/orchestrator/api/docs.py
"""Docs index API: list, raw content, refresh."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from orchestrator.api.auth import verify_token
from orchestrator.models.schemas import DocResponse


router = APIRouter(prefix="/api/docs", tags=["docs"])


@router.get("", response_model=list[DocResponse])
async def list_docs(request: Request, category: str | None = None,
                    _: None = Depends(verify_token)) -> list[dict]:
    db = request.app.state.db
    if category:
        return await db.fetch_all(
            "SELECT * FROM doc_index WHERE category = ? ORDER BY updated_at DESC",
            (category,),
        )
    return await db.fetch_all("SELECT * FROM doc_index ORDER BY updated_at DESC")


@router.post("/refresh")
async def refresh_docs(request: Request, _: None = Depends(verify_token)) -> dict:
    return await request.app.state.doc_indexer.scan()


@router.get("/raw")
async def raw_doc(request: Request, path: str, _: None = Depends(verify_token)) -> dict:
    settings = request.app.state.settings
    root = Path(settings.docs_root).parent
    target = (root / path).resolve()
    if not str(target).startswith(str(root.resolve())) or not target.is_file():
        raise HTTPException(status_code=404, detail="doc not found")
    return {"path": path, "content": target.read_text(encoding="utf-8")}
```

(Match the auth dependency to how `api/projects.py` imports it — use the same `verify_token`/`Depends` symbol that the existing routers use.)

- [ ] **Step 5: Register the router**

In `src/orchestrator/main.py`, where other routers are included (the `app.include_router(...)` block), add:

```python
    from orchestrator.api import docs as docs_api
    app.include_router(docs_api.router)
```

- [ ] **Step 6: Run tests and confirm pass**

Run: `uv run pytest tests/test_api_docs.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator/api/docs.py src/orchestrator/models/schemas.py src/orchestrator/main.py tests/test_api_docs.py tests/conftest.py
git commit -m "feat: add /api/docs list/raw/refresh router"
```

---

### Task 7: Wire the indexer into startup

**Files:**
- Modify: `src/orchestrator/main.py` (lifespan, after `OpusBridge` is constructed, ~line 56)
- Test: `tests/test_main_lifespan.py` (or extend existing startup test)

**Depends on:** Task 5, Task 6

- [ ] **Step 1: Write the failing test**

```python
# tests/test_main_lifespan.py  (add)
def test_app_has_doc_indexer(client):
    # client fixture builds the app via lifespan
    assert client.app.state.doc_indexer is not None
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_main_lifespan.py::test_app_has_doc_indexer -v`
Expected: FAIL — `app.state` has no `doc_indexer`.

- [ ] **Step 3: Construct the indexer and run an initial scan**

In `src/orchestrator/main.py` lifespan, after `app.state.opus_bridge = OpusBridge(...)`:

```python
    from orchestrator.core.doc_indexer import DocIndexer
    app.state.doc_indexer = DocIndexer(
        db=database,
        docs_root=settings.docs_root,
        classify=app.state.opus_bridge.classify_doc,
    )
    try:
        await app.state.doc_indexer.scan()
    except Exception as exc:  # noqa: BLE001 - non-fatal at startup
        logger.warning("Initial doc scan failed: %s", exc)
```

- [ ] **Step 4: Run tests and confirm pass**

Run: `uv run pytest tests/test_main_lifespan.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/main.py tests/test_main_lifespan.py
git commit -m "feat: wire DocIndexer into startup with initial scan"
```

---

### Task 8: Specs & Plans dashboard views

**Files:**
- Modify: `web/index.html` (nav items ~510-514, `switchView`, new render functions)

**Depends on:** Task 6

- [ ] **Step 1: Add nav items**

After the Plans nav button (`web/index.html:512`), add:

```html
      <button class="nav-item" type="button" data-view="specs" onclick="switchView('specs')"><span class="nav-icon">F</span>Specs</button>
      <button class="nav-item" type="button" data-view="docplans" onclick="switchView('docplans')"><span class="nav-icon">N</span>Plan Docs</button>
```

- [ ] **Step 2: Add render functions and route them in `switchView`**

In the `switchView` dispatch, add cases for `specs` and `docplans` calling `renderDocs("spec")` / `renderDocs("plan")`. Add:

```javascript
    async function renderDocs(category) {
      const docs = await api("GET", "/api/docs?category=" + category);
      const rows = docs.map(d => {
        const pct = d.total_count ? Math.round(100 * d.done_count / d.total_count) : 0;
        const bar = category === "plan"
          ? '<div class="doc-bar"><div class="doc-bar-fill" style="width:' + pct + '%"></div></div>'
            + '<span class="doc-bar-label">' + d.done_count + '/' + d.total_count + '</span>'
          : "";
        const llm = d.classified_by === "haiku" ? ' <span class="doc-tag">haiku</span>' : "";
        return '<div class="row" onclick="openDoc(\'' + esc(d.path) + '\')">' +
          '<div class="row-main"><div class="row-name">' + esc(d.title || d.path) + '</div>' +
          '<div class="row-sub">' + esc(d.path) + llm + '</div></div>' + bar + '</div>';
      }).join("");
      document.getElementById("view-container").innerHTML =
        '<div class="list-header"><h2>' + (category === "spec" ? "Specs" : "Plan Docs") + '</h2>' +
        '<button class="btn" type="button" onclick="refreshDocs()">Refresh</button></div>' +
        '<div class="list">' + (rows || '<div class="empty">No ' + category + ' docs found</div>') + '</div>';
    }

    async function refreshDocs() {
      await api("POST", "/api/docs/refresh");
      const v = currentView === "specs" ? "spec" : "plan";
      renderDocs(v);
    }

    async function openDoc(path) {
      const doc = await api("GET", "/api/docs/raw?path=" + encodeURIComponent(path));
      alert(doc.content.slice(0, 4000));  // v1: simple viewer; Unit C replaces with editor
    }
```

- [ ] **Step 3: Add minimal styles**

In the `<style>` block add:

```css
    .doc-bar { width: 120px; height: 6px; background: var(--border-subtle); border-radius: 3px; overflow: hidden; }
    .doc-bar-fill { height: 100%; background: var(--badge-passed-text); }
    .doc-bar-label { font-size: 11px; color: var(--text-faint); margin-left: 8px; }
    .doc-tag { font-size: 10px; color: var(--text-faint); border: 1px solid var(--border); border-radius: 4px; padding: 0 4px; }
    .list-header { display: flex; align-items: center; justify-content: space-between; padding: 16px 20px; border-bottom: 1px solid var(--border); }
```

- [ ] **Step 4: Verify in the browser**

Start the server, open the dashboard, click **Specs** and **Plan Docs**. Expected: the specs/plans under `docs/` appear; plan rows show progress bars (e.g. the plan files from this epic); Refresh re-scans; Haiku-classified files (if any) show a `haiku` tag.

- [ ] **Step 5: Commit**

```bash
git add web/index.html
git commit -m "feat(ui): add Specs and Plan Docs views with progress + refresh"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1, Task 2 (no dependencies)
- **Wave 2:** Task 3 (Task 2), Task 4 (Plan 2 Task 5)
- **Wave 3:** Task 5 (Task 1, Task 3, Task 4)
- **Wave 4:** Task 6 (Task 5)
- **Wave 5:** Task 7 (Task 5, Task 6), Task 8 (Task 6)

## Notes

- **Scan root is configurable** (`docs_root`, default `docs`). Indexing each *target repo's*
  `docs/` (after Unit C writes specs into cloned repos) is a follow-up tie-in with Plan 4 — out
  of scope here so this plan ships standalone.
- Local LM Studio fallback for classification lives in `DocIndexer.scan` (the `except` →
  `fallback` path). A richer LM Studio call can replace the `"other"` fallback later without
  changing the interface.
- Per LLM policy, classification uses `claude -p --model claude-haiku-4-5` (subscription) only —
  no API key.
- The simple `alert()` doc viewer in Task 8 is intentional for v1; Unit C (Plan 4) replaces it
  with the spec editor / chat refine flow.

---

## Part 4: Superpowers Lifecycle (Unit C)



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

---

## Part 5: Context Sync (Unit D)



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
