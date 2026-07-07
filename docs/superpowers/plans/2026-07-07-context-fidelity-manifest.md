# Context-Fidelity Manifest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the orchestrating MCP client pass a minimum-blocking, secret-scrubbed manifest of non-committed context (gitignored config shapes, user-scope conventions) that fills the worker Bible's currently-empty `repo_memory` slot.

**Architecture:** A new optional `local_context` field on the `dispatch_task` / `execute_plan` request surface is threaded, exactly like the existing `context`/`context_text` field, onto each decomposed leaf task as `repo_memory`. At dispatch time `orchestrator_dispatch._build_worker_bible` reads that value into `BibleSources.repo_memory` (previously hardcoded `None`). `build_bible` already scrubs and budgets every section, so the manifest lands in the droppable priority-9 `# REPO MEMORY` section for free. The engine's brain decomposition prompt is NOT changed.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, pytest (`asyncio_mode = "auto"`), FastMCP. Run everything with `uv run`.

---

## Background for the implementer (read before starting)

You are working in `C:\working-space\praxis`. Praxis delegates coding tasks to a local LLM
worker running in a one-shot Docker container: `git clone` -> implement -> commit -> PR ->
callback. The worker therefore only ever sees **committed** files. Gitignored files (`.env`,
local configs) and the operator's user-scope memory (`~/.claude/CLAUDE.md`) never travel.

The worker is briefed by a "Static Bible" assembled in `src/orchestrator/core/worker_bible.py`.
`BibleSources` has a `repo_memory` field meant for exactly this non-committed reference, but it
is always passed `None` today (`src/orchestrator/core/orchestrator_dispatch.py:183`).

There are two request entry points that produce worker tasks, and BOTH already thread a
`context` string onto each leaf task as `context_text` (which becomes the Bible's floor
`caller_context` section). This plan adds a parallel `local_context` string that becomes each
leaf's `repo_memory` (the Bible's droppable reference section):

- **`POST /api/dispatch`** (single task) — `src/orchestrator/api/dispatch.py`. Threads
  `context` at line ~263-265 onto `task_dict["context_text"]`.
- **`POST /api/execute-plan`** (full plan, async) — `src/orchestrator/api/execute_plan.py`
  persists a `pending_input` JSON blob; the orchestration loop later calls
  `decompose_plan(...)` in `src/orchestrator/core/execute_plan_decompose.py`, which threads
  `context` onto `task["context_text"]` at line ~113-116.

The MCP tools that call these endpoints live in `src/mcp_server/server.py` (both an
independently-testable `*_impl(client, ...)` function and a thin `@mcp.tool()` wrapper).

**Key facts:**
- `scrub_context(text: str | None, max_chars=...) -> str | None` lives in
  `src/orchestrator/core/context_scrub.py`. It returns `None` for `None`/empty input and
  strips credential-shaped tokens. Reuse it; do NOT write new scrubbing.
- `build_bible` (in `worker_bible.py`) already calls `scrub_context` on every section and
  budgets them, so once `repo_memory` is populated the manifest is scrubbed + droppable with
  no extra code. Do NOT modify `worker_bible.py` or `build_bible`.
- Tests run with `uv run pytest ...`. `asyncio_mode = "auto"` — async test functions need no
  decorator. Mark pure unit tests with `@pytest.mark.unit` where the surrounding file does.
- Lint/type after each task: `uv run ruff format src/ tests/ && uv run ruff check src/ tests/`
  and `uv run mypy src/orchestrator/ --ignore-missing-imports`.
- Never use em dashes in prose, comments, or commit messages (project convention). Use a
  comma, colon, or "so"/"then".

---

## File structure

| File | Change |
|------|--------|
| `src/orchestrator/models/schemas.py` | Add `local_context: str | None = None` to `DispatchRequest` and `ExecutePlanRequest`. |
| `src/orchestrator/api/dispatch.py` | Scrub `body.local_context` -> `task_dict["repo_memory"]`. |
| `src/orchestrator/core/execute_plan_decompose.py` | New `local_context` param on `decompose_plan`; thread scrubbed value onto each leaf's `repo_memory`. |
| `src/orchestrator/api/execute_plan.py` | Put `body.local_context` into the persisted `pending_input`. |
| `src/orchestrator/core/orchestrator.py` | Pass `local_context=payload.get("local_context")` into `decompose_plan`. |
| `src/orchestrator/core/orchestrator_dispatch.py` | Read `plan_task.get("repo_memory")` into `BibleSources.repo_memory` (replace `None`); update the stale comment. |
| `src/mcp_server/server.py` | Add `local_context` param to `dispatch_task_impl`, `execute_plan_impl`, and both `@mcp.tool()` wrappers. |
| `src/mcp_server/resources/orchestration_guide.md` | New "Gather local context before dispatching" section. |
| `CLAUDE.md` | New gotcha line. |
| Tests | `tests/test_execute_plan_decompose.py`, `tests/test_api_dispatch.py`, `tests/test_api_execute_plan.py`, `tests/test_worker_bible.py`, `tests/test_mcp_server.py`, `tests/test_mcp_resources.py`. |

---

### Task 1: Add `local_context` to the request schemas

**Files:**
- Modify: `src/orchestrator/models/schemas.py` (`DispatchRequest` ~line 382, `ExecutePlanRequest` ~line 470)

**Depends on:** None

- [ ] **Step 1: Write the failing test**

Add to `tests/test_api_dispatch.py` (a new test; imports likely already present, else add
`from orchestrator.models.schemas import DispatchRequest, ExecutePlanRequest`):

```python
@pytest.mark.unit
def test_dispatch_request_accepts_local_context():
    req = DispatchRequest(
        repo_url="https://github.com/x/y",
        instructions="do the thing",
        model="qwen3.6-27b",
        local_context="REDIS_URL = cache connection string",
    )
    assert req.local_context == "REDIS_URL = cache connection string"


@pytest.mark.unit
def test_dispatch_request_local_context_defaults_none():
    req = DispatchRequest(
        repo_url="https://github.com/x/y",
        instructions="do the thing",
        model="qwen3.6-27b",
    )
    assert req.local_context is None
```

And to `tests/test_api_execute_plan.py`:

```python
from orchestrator.models.schemas import ExecutePlanRequest


@pytest.mark.unit
def test_execute_plan_request_accepts_local_context():
    req = ExecutePlanRequest(
        repo_url="https://github.com/x/y",
        plan="step 1\nstep 2",
        model="qwen3.6-27b",
        local_context="config lives in config/local.yaml (keys: host, port)",
    )
    assert req.local_context == "config lives in config/local.yaml (keys: host, port)"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_api_dispatch.py::test_dispatch_request_accepts_local_context tests/test_api_execute_plan.py::test_execute_plan_request_accepts_local_context -v`
Expected: FAIL with a Pydantic `ValidationError` / unexpected-keyword or `AttributeError` (field not defined).

- [ ] **Step 3: Add the field to both models**

In `src/orchestrator/models/schemas.py`, inside `DispatchRequest`, directly AFTER the existing
`context` field and its docstring (the block ending at line ~400), add:

```python
    local_context: str | None = None
    """Minimum-blocking, secret-scrubbed manifest of NON-COMMITTED context the
    worker cannot see from a git clone (gitignored config shapes, user-scope
    conventions). Self-contained inline text, never a "read file X" pointer.
    Threaded onto each leaf's droppable repo_memory Bible slot. Include env var
    NAMES/shapes over live values; the worker writes code, it does not run it."""
```

In the same file, inside `ExecutePlanRequest`, directly AFTER the existing `context` field
(line ~478), add the identical field + docstring:

```python
    local_context: str | None = None
    """Minimum-blocking, secret-scrubbed manifest of NON-COMMITTED context the
    worker cannot see from a git clone (gitignored config shapes, user-scope
    conventions). Self-contained inline text, never a "read file X" pointer.
    Threaded onto each leaf's droppable repo_memory Bible slot. Include env var
    NAMES/shapes over live values; the worker writes code, it does not run it."""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_api_dispatch.py::test_dispatch_request_accepts_local_context tests/test_api_dispatch.py::test_dispatch_request_local_context_defaults_none tests/test_api_execute_plan.py::test_execute_plan_request_accepts_local_context -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Lint, format, commit**

```bash
uv run ruff format src/ tests/ && uv run ruff check src/ tests/
git add src/orchestrator/models/schemas.py tests/test_api_dispatch.py tests/test_api_execute_plan.py
git commit -m "feat: add local_context field to dispatch and execute-plan requests"
```

---

### Task 2: Thread `local_context` onto the leaf in `POST /api/dispatch`

**Files:**
- Modify: `src/orchestrator/api/dispatch.py` (~line 263-265, the `context_text` block)
- Test: `tests/test_api_dispatch.py`

**Depends on:** Task 1

- [ ] **Step 1: Write the failing test**

`tests/test_api_dispatch.py` already dispatches through the TestClient and inspects the created
plan's `opus_plan`. Find an existing passing dispatch test to copy the fixture/setup style, then
add a test that posts `local_context` and asserts the stored task carries `repo_memory`. Use the
same client/auth fixtures the file already uses (do not invent new ones):

```python
async def test_dispatch_threads_local_context_as_repo_memory(
    client, auth_headers, seeded_project
):
    resp = client.post(
        "/api/dispatch",
        headers=auth_headers,
        json={
            "repo_url": seeded_project["repo_url"],
            "instructions": "add a cache layer",
            "model": "qwen3.6-27b",
            "local_context": "REDIS_URL = the cache connection (name only, no value)",
        },
    )
    assert resp.status_code == 200, resp.text
    plan_id = resp.json()["plan_id"]
    # Read the persisted opus_plan and confirm the leaf got repo_memory.
    import json as _json
    from orchestrator.database import Database  # if a helper exists, prefer it

    # Simplest: fetch via the app's db on state (mirror how other tests read plans).
    db = client.app.state.db
    row = await db.fetch_one("SELECT opus_plan FROM plans WHERE id = ?", (plan_id,))
    tasks = _json.loads(row["opus_plan"])["tasks"]
    assert tasks[0]["repo_memory"] == (
        "REDIS_URL = the cache connection (name only, no value)"
    )
```

> NOTE for the implementer: match the ACTUAL fixture names and the ACTUAL way other tests in
> `tests/test_api_dispatch.py` read back a plan's `opus_plan`. If the file reads plans through a
> different helper (e.g. `queue.get_plan`), use that instead of raw SQL. The assertion (leaf has
> `repo_memory` == the scrubbed input) is the contract; the read mechanism should follow the file.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_dispatch.py::test_dispatch_threads_local_context_as_repo_memory -v`
Expected: FAIL with `KeyError: 'repo_memory'` (the leaf has no such key yet).

- [ ] **Step 3: Add the threading in the endpoint**

In `src/orchestrator/api/dispatch.py`, immediately AFTER the existing block:

```python
    scrubbed_context = scrub_context(body.context)
    if scrubbed_context is not None:
        task_dict["context_text"] = scrubbed_context
```

add:

```python
    scrubbed_local = scrub_context(body.local_context)
    if scrubbed_local is not None:
        task_dict["repo_memory"] = scrubbed_local
```

(`scrub_context` is already imported in this file, confirmed by the existing `context_text`
block. If not, add `from orchestrator.core.context_scrub import scrub_context`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_api_dispatch.py::test_dispatch_threads_local_context_as_repo_memory -v`
Expected: PASS.

- [ ] **Step 5: Lint, format, commit**

```bash
uv run ruff format src/ tests/ && uv run ruff check src/ tests/
git add src/orchestrator/api/dispatch.py tests/test_api_dispatch.py
git commit -m "feat: thread local_context onto dispatched leaf as repo_memory"
```

---

### Task 3: Thread `local_context` through `decompose_plan` (execute-plan path)

**Files:**
- Modify: `src/orchestrator/core/execute_plan_decompose.py` (`decompose_plan`, ~line 58-117)
- Test: `tests/test_execute_plan_decompose.py`

**Depends on:** Task 1

- [ ] **Step 1: Write the failing test**

Add to `tests/test_execute_plan_decompose.py` (reuse the existing `_FakeRouter`,
`_FakeProfile`, `_FakeEffective` in that file):

```python
async def test_decompose_plan_threads_local_context_as_repo_memory():
    raw = '{"tasks":[{"id":"t1","title":"X","description":"d","depends_on":[]}]}'
    router = _FakeRouter(raw)
    opus_plan = await decompose_plan(
        plan="do something",
        model="qwen3.6-27b",
        context=None,
        router=router,
        effective_settings=_FakeEffective(),
        project_id=None,
        local_context="config/local.yaml keys: host, port (values omitted)",
    )
    assert opus_plan["tasks"][0].get("repo_memory") == (
        "config/local.yaml keys: host, port (values omitted)"
    )


async def test_decompose_plan_no_local_context_skips_repo_memory():
    raw = '{"tasks":[{"id":"t1","title":"X","description":"d","depends_on":[]}]}'
    router = _FakeRouter(raw)
    opus_plan = await decompose_plan(
        plan="do something",
        model="qwen3.6-27b",
        context=None,
        router=router,
        effective_settings=_FakeEffective(),
        project_id=None,
        local_context=None,
    )
    assert "repo_memory" not in opus_plan["tasks"][0]


async def test_decompose_plan_scrubs_local_context_server_side():
    """Credential-shaped content in local_context is stripped by scrub_context."""
    raw = '{"tasks":[{"id":"t1","title":"X","description":"d","depends_on":[]}]}'
    router = _FakeRouter(raw)
    opus_plan = await decompose_plan(
        plan="do something",
        model="qwen3.6-27b",
        context=None,
        router=router,
        effective_settings=_FakeEffective(),
        project_id=None,
        local_context="token=ghp_abcdef1234567890abcdef1234567890abcd",
    )
    assert "ghp_abcdef" not in (opus_plan["tasks"][0].get("repo_memory") or "")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_execute_plan_decompose.py::test_decompose_plan_threads_local_context_as_repo_memory tests/test_execute_plan_decompose.py::test_decompose_plan_no_local_context_skips_repo_memory tests/test_execute_plan_decompose.py::test_decompose_plan_scrubs_local_context_server_side -v`
Expected: FAIL with `TypeError: decompose_plan() got an unexpected keyword argument 'local_context'`.

- [ ] **Step 3: Add the param and threading**

In `src/orchestrator/core/execute_plan_decompose.py`, change the `decompose_plan` signature to
add a trailing keyword param (keep it last so callers are unaffected):

```python
async def decompose_plan(
    plan: str,
    model: str,
    context: str | None,
    router: Any,
    effective_settings: Any,
    project_id: str | None,
    local_context: str | None = None,
) -> dict[str, Any]:
```

Then, at the END of the function, REPLACE the existing final block:

```python
    scrubbed_context = scrub_context(context)
    if scrubbed_context is not None:
        for task in opus_plan["tasks"]:
            task.setdefault("context_text", scrubbed_context)
    return opus_plan
```

with:

```python
    scrubbed_context = scrub_context(context)
    if scrubbed_context is not None:
        for task in opus_plan["tasks"]:
            task.setdefault("context_text", scrubbed_context)
    scrubbed_local = scrub_context(local_context)
    if scrubbed_local is not None:
        for task in opus_plan["tasks"]:
            task.setdefault("repo_memory", scrubbed_local)
    return opus_plan
```

Also add a one-line `Args:` entry for `local_context` in the docstring, mirroring the `context`
entry, describing it as the non-committed reference threaded onto each leaf's `repo_memory`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_execute_plan_decompose.py -v`
Expected: PASS (all existing tests plus the 2 new ones).

- [ ] **Step 5: Lint, format, commit**

```bash
uv run ruff format src/ tests/ && uv run ruff check src/ tests/
git add src/orchestrator/core/execute_plan_decompose.py tests/test_execute_plan_decompose.py
git commit -m "feat: thread local_context onto decomposed leaves as repo_memory"
```

---

### Task 4: Carry `local_context` through the execute-plan async path

**Files:**
- Modify: `src/orchestrator/api/execute_plan.py` (`pending_input` dict, ~line 132-139)
- Modify: `src/orchestrator/core/orchestrator.py` (`decompose_pending_execute_plan`, ~line 122-131)
- Test: `tests/test_api_execute_plan.py`

**Depends on:** Task 1, Task 3

- [ ] **Step 1: Write the failing test**

The execute-plan endpoint persists a `pending_input` JSON blob; the loop consumes it later. Test
that `local_context` survives into `pending_input`. Mirror the existing execute-plan endpoint
tests in `tests/test_api_execute_plan.py` for fixtures/auth:

```python
async def test_execute_plan_persists_local_context_in_pending_input(
    client, auth_headers
):
    import json as _json

    resp = client.post(
        "/api/execute-plan",
        headers=auth_headers,
        json={
            "repo_url": "https://github.com/x/y",
            "plan": "step 1\nstep 2",
            "model": "qwen3.6-27b",
            "local_context": "SAMPLE row shape: {id:int, name:str}",
        },
    )
    assert resp.status_code == 201, resp.text
    plan_id = resp.json()["plan_id"]
    db = client.app.state.db
    row = await db.fetch_one(
        "SELECT pending_input FROM plans WHERE id = ?", (plan_id,)
    )
    payload = _json.loads(row["pending_input"])
    assert payload["local_context"] == "SAMPLE row shape: {id:int, name:str}"
```

> NOTE: if the file reads the plan back via a queue helper rather than raw SQL, use that. The
> column is `pending_input` on the `plans` table.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_execute_plan.py::test_execute_plan_persists_local_context_in_pending_input -v`
Expected: FAIL with `KeyError: 'local_context'` (not in the persisted payload).

- [ ] **Step 3: Persist and consume the field**

In `src/orchestrator/api/execute_plan.py`, change the `pending_input` construction (currently):

```python
    pending_input = json.dumps(
        {
            "plan": body.plan,
            "model": body.model,
            "context": body.context,
            "branch": branch_name,
        }
    )
```

to include `local_context`:

```python
    pending_input = json.dumps(
        {
            "plan": body.plan,
            "model": body.model,
            "context": body.context,
            "local_context": body.local_context,
            "branch": branch_name,
        }
    )
```

In `src/orchestrator/core/orchestrator.py`, in `decompose_pending_execute_plan`, change the
`decompose_plan(...)` call to pass the new field (reading with `.get` so old queued blobs without
the key still work):

```python
            opus_plan = await decompose_plan(
                plan=payload["plan"],
                model=payload["model"],
                context=payload.get("context"),
                router=self._llm_router,
                effective_settings=self._effective_settings,
                project_id=project["id"],
                local_context=payload.get("local_context"),
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_api_execute_plan.py::test_execute_plan_persists_local_context_in_pending_input -v`
Expected: PASS.

- [ ] **Step 5: Lint, format, commit**

```bash
uv run ruff format src/ tests/ && uv run ruff check src/ tests/
git add src/orchestrator/api/execute_plan.py src/orchestrator/core/orchestrator.py tests/test_api_execute_plan.py
git commit -m "feat: carry local_context through the execute-plan async decompose path"
```

---

### Task 5: Feed the leaf's `repo_memory` into the Bible at dispatch

**Files:**
- Modify: `src/orchestrator/core/orchestrator_dispatch.py` (line 183 `repo_memory=None`; comment lines ~64-65 & 182-184)
- Test: `tests/test_worker_bible.py` (add an integration-style assertion) OR a focused test in the dispatch test module

**Depends on:** None (code change is self-contained; end-to-end value comes from Tasks 2-4)

- [ ] **Step 1: Write the failing test**

The cleanest unit target is `_build_worker_bible`, but it needs orchestrator wiring. Instead,
prove the contract at the `build_bible` seam plus a direct check that dispatch reads the key. Add
to `tests/test_worker_bible.py` a test that confirms a populated `repo_memory` reaches the
rendered Bible (this guards the whole point of the change):

```python
@pytest.mark.unit
def test_repo_memory_section_present_when_provided():
    src = BibleSources(
        goal="Do x",
        handover="# PROGRESS",
        repo_memory="# LOCAL CONTEXT\nREDIS_URL = cache connection (name only)",
        context_window=8000,
    )
    bible = build_bible(src)
    assert "# REPO MEMORY" in bible
    assert "REDIS_URL = cache connection (name only)" in bible
```

This test passes already against `build_bible` (it is not the regressed line). To guard the
actual `None` -> `plan_task.get("repo_memory")` change, add a focused test next to the dispatch
tests asserting the wiring. In `tests/test_orchestrator_dispatch.py` if it exists, else create it,
add:

```python
async def test_build_worker_bible_uses_plan_task_repo_memory(monkeypatch):
    """_build_worker_bible must forward plan_task['repo_memory'] into the Bible."""
    from orchestrator.core import orchestrator_dispatch as od

    captured = {}

    def _fake_build_bible(src):
        captured["repo_memory"] = src.repo_memory
        return "BIBLE"

    monkeypatch.setattr(od, "build_bible", _fake_build_bible)

    # Minimal stand-in orchestrator exposing just what _build_worker_bible reads.
    class _Stub(od.DispatchMixin):
        def __init__(self):
            self._effective_settings = None  # forces context_window fallback 8192

            class _Git:
                async def branch_commit_log(self, *a, **k):
                    return []

            self._git = _Git()

    stub = _Stub()
    task = {"title": "t", "description": "goal", "id": "1"}
    plan_task = {"repo_memory": "LOCAL CTX: config keys host, port"}
    project = {"model_name": "m"}
    out = await stub._build_worker_bible(task, plan_task, project, "main", "agent/x")
    assert out == "BIBLE"
    assert captured["repo_memory"] == "LOCAL CTX: config keys host, port"
```

> NOTE: `detect_context_limit` is only called when `lm_studio_url` is truthy; with
> `_effective_settings = None` the code takes the `8192` fallback and never touches LM Studio, so
> no network mock is needed. If import wiring differs, adjust the stub to satisfy exactly the
> attributes `_build_worker_bible` reads (`_effective_settings`, `_git`).

- [ ] **Step 2: Run tests to verify the wiring test fails**

Run: `uv run pytest tests/test_worker_bible.py::test_repo_memory_section_present_when_provided tests/test_orchestrator_dispatch.py::test_build_worker_bible_uses_plan_task_repo_memory -v`
Expected: the `worker_bible` test PASSES; the dispatch wiring test FAILS asserting
`captured["repo_memory"] is None` (current code hardcodes `repo_memory=None`).

- [ ] **Step 3: Make the dispatch change**

In `src/orchestrator/core/orchestrator_dispatch.py`, in `_build_worker_bible`, change:

```python
        return build_bible(
            BibleSources(
                goal=goal,
                handover=handover,
                context_window=context_window,
                plan_slice=plan_task.get("plan_text"),
                caller_context=plan_task.get("context_text"),
                repo_memory=None,  # repo files folded in by entrypoint --read
                review_feedback=task.get("review_feedback"),
            )
        )
```

to:

```python
        return build_bible(
            BibleSources(
                goal=goal,
                handover=handover,
                context_window=context_window,
                plan_slice=plan_task.get("plan_text"),
                caller_context=plan_task.get("context_text"),
                # Client-gathered manifest of NON-committed context (gitignored
                # config shapes, user-scope conventions). Committed repo files
                # are still folded in separately by the entrypoint --read.
                repo_memory=plan_task.get("repo_memory"),
                review_feedback=task.get("review_feedback"),
            )
        )
```

Also update the comment at lines ~64-65 that lists per-task plan hints so it mentions
`repo_memory` alongside `plan_path, plan_text, context_text`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_worker_bible.py tests/test_orchestrator_dispatch.py -v`
Expected: PASS.

- [ ] **Step 5: Lint, format, commit**

```bash
uv run ruff format src/ tests/ && uv run ruff check src/ tests/
git add src/orchestrator/core/orchestrator_dispatch.py tests/test_worker_bible.py tests/test_orchestrator_dispatch.py
git commit -m "feat: fill worker Bible repo_memory slot from the leaf's local_context"
```

---

### Task 6: Expose `local_context` on the MCP tools

**Files:**
- Modify: `src/mcp_server/server.py` (`dispatch_task_impl` ~36, `execute_plan_impl` ~66, `dispatch_task` wrapper ~211, `execute_plan` wrapper ~246)
- Test: `tests/test_mcp_server.py`

**Depends on:** Task 1 (payload key the REST layer accepts)

- [ ] **Step 1: Write the failing test**

`tests/test_mcp_server.py` tests the `*_impl` functions against a fake client that records the
posted payload. Copy the existing pattern for a `context`-forwarding test and add:

```python
async def test_dispatch_task_impl_forwards_local_context():
    from mcp_server.server import dispatch_task_impl

    class _FakeClient:
        def __init__(self):
            self.posted = None

        async def post(self, path, payload):
            self.posted = (path, payload)
            return {"task_id": "t1"}

    client = _FakeClient()
    await dispatch_task_impl(
        client,
        repo_url="https://github.com/x/y",
        instructions="do it",
        model="qwen3.6-27b",
        local_context="config keys: host, port (no values)",
    )
    assert client.posted[0] == "/api/dispatch"
    assert client.posted[1]["local_context"] == "config keys: host, port (no values)"


async def test_execute_plan_impl_forwards_local_context():
    from mcp_server.server import execute_plan_impl

    class _FakeClient:
        def __init__(self):
            self.posted = None

        async def post(self, path, payload):
            self.posted = (path, payload)
            return {"plan_id": "p1"}

    client = _FakeClient()
    await execute_plan_impl(
        client,
        repo_url="https://github.com/x/y",
        plan="step 1",
        model="qwen3.6-27b",
        local_context="SAMPLE shape: {id:int}",
    )
    assert client.posted[1]["local_context"] == "SAMPLE shape: {id:int}"


async def test_dispatch_task_impl_omits_local_context_when_none():
    from mcp_server.server import dispatch_task_impl

    class _FakeClient:
        def __init__(self):
            self.posted = None

        async def post(self, path, payload):
            self.posted = (path, payload)
            return {}

    client = _FakeClient()
    await dispatch_task_impl(
        client,
        repo_url="https://github.com/x/y",
        instructions="do it",
        model="qwen3.6-27b",
    )
    assert "local_context" not in client.posted[1]
```

> NOTE: match the ACTUAL fake-client signature used in `tests/test_mcp_server.py` (the real
> `PraxisClient.post(path, json)` takes the body as the 2nd positional arg). If existing tests
> name it `json=`, mirror that.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp_server.py::test_dispatch_task_impl_forwards_local_context tests/test_mcp_server.py::test_execute_plan_impl_forwards_local_context -v`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'local_context'`.

- [ ] **Step 3: Add the param to both `*_impl` functions and both wrappers**

In `src/mcp_server/server.py`:

In `dispatch_task_impl`, add `local_context: str | None = None,` to the signature (after
`context`), and after the existing `if context is not None: payload["context"] = context`, add:

```python
    if local_context is not None:
        payload["local_context"] = local_context
```

In `execute_plan_impl`, do the identical two edits (signature param + payload block after the
`context` block).

In the `@mcp.tool()` `dispatch_task` wrapper, add `local_context: str | None = None,` to the
signature (after `context`), pass `local_context=local_context,` into the `dispatch_task_impl(...)`
call, and add to the docstring:

```
    local_context: Optional NON-committed context the worker cannot see from a
    git clone (gitignored config shapes, user-scope conventions). Self-contained
    inline text, never a "read file X" pointer. Prefer env var NAMES/shapes over
    live values: the worker writes code, it does not run it.
```

In the `@mcp.tool()` `execute_plan` wrapper, do the identical signature param + forwarding +
docstring addition.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_server.py -v`
Expected: PASS (existing tests plus the 3 new ones).

- [ ] **Step 5: Lint, format, commit**

```bash
uv run ruff format src/ tests/ && uv run ruff check src/ tests/
git add src/mcp_server/server.py tests/test_mcp_server.py
git commit -m "feat: expose local_context on the dispatch_task and execute_plan MCP tools"
```

---

### Task 7: Document the workflow (orchestration guide + CLAUDE.md)

**Files:**
- Modify: `src/mcp_server/resources/orchestration_guide.md`
- Modify: `CLAUDE.md` (Gotchas index)
- Test: `tests/test_mcp_resources.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

`tests/test_mcp_resources.py` already asserts the guide contains sections. Add a content
assertion for the new section:

```python
def test_guide_has_gather_local_context_section():
    from mcp_server.server import orchestration_guide_text  # match the real accessor

    text = orchestration_guide_text()
    assert "Gather local context before dispatching" in text
    assert "does not run it" in text  # the names-over-values privacy principle
```

> NOTE: match how `tests/test_mcp_resources.py` currently loads the guide text (it may read the
> file directly via `importlib.resources` or call a helper). Use the SAME accessor the existing
> tests use, not a guessed function name.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_resources.py::test_guide_has_gather_local_context_section -v`
Expected: FAIL (section text absent).

- [ ] **Step 3: Add the guide section**

Append to `src/mcp_server/resources/orchestration_guide.md` a new section:

```markdown
## Gather local context before dispatching

The worker runs from a fresh `git clone`, so it never sees gitignored files
(`.env`, local config, data samples) or your user-scope memory
(`~/.claude/CLAUDE.md`). If the task needs any of that, pass it as `local_context`
on `execute_plan` / `dispatch_task`. It fills the worker's droppable "repo memory"
reference slot.

Rules for `local_context`:

1. **Self-contained.** Inline the actual information. Never write "read `.env`" or
   "see `~/.claude/CLAUDE.md`" - the worker's clone does not contain those files, so
   a pointer is a dead end.
2. **Minimum-blocking only.** Include something only if the code cannot be written
   correctly without it. If its absence would not break the implementation, leave it
   out (smaller payload, less leak surface, less context pressure).
3. **Names/shapes over values.** The worker writes code, it does not run it, so it
   almost never needs a live secret. Include the env var NAME and its purpose
   (`REDIS_URL` = cache connection), the config KEYS, or a MASKED sample row, not the
   real value. Include a real value only in the rare case the code cannot be written
   without it, and only that one.
4. **No secrets you do not need.** Tokens, passwords, and keys are omitted by default;
   Praxis also scrubs credential-shaped text server-side as a backstop.

Where it fits in the flow: resolve the worker model (`get_project`), then
`execute_plan` / `dispatch_task` with both `context` (task intent) and `local_context`
(environment reference), then `poll_plan` / `poll_task` until terminal.
```

- [ ] **Step 4: Add the CLAUDE.md gotcha**

In `CLAUDE.md`, under the Gotchas condensed index, add one line (keep the existing terse style,
no em dash):

```markdown
- **`local_context` fills the worker's `repo_memory` Bible slot** with client-gathered
  non-committed context (gitignored config shapes, user-scope conventions); minimum-blocking,
  names/shapes over values, never a "read file X" pointer. Threaded like `context_text`.
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_resources.py -v`
Expected: PASS.

- [ ] **Step 6: Lint, format, commit**

```bash
uv run ruff format src/ tests/ && uv run ruff check src/ tests/
git add src/mcp_server/resources/orchestration_guide.md CLAUDE.md tests/test_mcp_resources.py
git commit -m "docs: teach local_context manifest in the orchestration guide + gotcha"
```

---

### Task 8: Full-suite verification

**Files:** none (verification only)

**Depends on:** Task 1, Task 2, Task 3, Task 4, Task 5, Task 6, Task 7

- [ ] **Step 1: Run the whole suite with coverage**

Run: `uv run pytest --cov=orchestrator --cov-report=term-missing -q`
Expected: all tests PASS, coverage does not regress below the existing threshold (was 89%).

- [ ] **Step 2: Type check**

Run: `uv run mypy src/orchestrator/ --ignore-missing-imports`
Expected: no new errors.

- [ ] **Step 3: Lint + format check**

Run: `uv run ruff format --check src/ tests/ && uv run ruff check src/ tests/`
Expected: clean.

- [ ] **Step 4: Final commit (only if any formatting changed)**

```bash
git add -A
git commit -m "chore: format + lint pass for local_context manifest" || echo "nothing to commit"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1 (schemas), Task 5 (dispatch Bible wiring — self-contained), Task 7 (docs — self-contained)
- **Wave 2:** Task 2 (depends on Task 1), Task 3 (depends on Task 1), Task 6 (depends on Task 1)
- **Wave 3:** Task 4 (depends on Task 1, Task 3)
- **Wave 4:** Task 8 (depends on all)

> Note: Task 5 and Task 7 have no code dependency on Task 1 and can run in Wave 1. If the executor
> prefers strictly linear execution, run Tasks 1 through 8 in numeric order — the numeric order
> already respects every `Depends on:` annotation.

---

## Notes / invariants for the implementer

- **Do NOT touch** `src/orchestrator/core/worker_bible.py` or the brain decompose PROMPT
  (`build_review_prompt` / `plan_review.py`). The manifest is worker-facing reference only.
- **`.setdefault`, not `[...] =`** when threading onto leaves in `decompose_plan`, matching the
  existing `context_text` code, so a leaf that somehow already carries `repo_memory` is not
  clobbered.
- **`.get("local_context")`** when reading the persisted `pending_input` in the orchestrator, so
  plan blobs queued before this change (without the key) still decompose without a `KeyError`.
- **Scrubbing is not yours to reinvent.** `scrub_context` at the API boundary + `build_bible`'s
  per-section scrub give defense-in-depth already.
- If any test's fixture names or read-back helpers differ from what is sketched here, follow the
  ACTUAL conventions in that test file. The assertions (the contract) are what matter.
