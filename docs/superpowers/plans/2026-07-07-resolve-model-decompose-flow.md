# Resolve-model-first Decompose Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two read-back MCP tools (`get_project`, `list_projects`) plus an orchestration-guide "resolve the worker model" section, so any Praxis-as-MCP client (Claude Code / Codex / Antigravity) can honor a project's already-configured worker model instead of hardcoding or re-asking.

**Architecture:** Follow the existing MCP pattern in `src/mcp_server/server.py` exactly: an independently-testable `*_impl(client, ...)` async function plus a thin `@mcp.tool()` wrapper that builds `PraxisClient.from_env()` and delegates. No new REST endpoint — `get_project_impl` fetches the existing `GET /api/projects` list and filters by `repo_url` client-side (mirroring how other `*_impl`s shape data client-side). The decompose engine is untouched.

**Tech Stack:** Python 3.11, FastMCP, httpx (via `PraxisClient`), pytest (`asyncio_mode = "auto"`).

**Spec:** `docs/superpowers/specs/2026-07-07-resolve-model-decompose-flow-design.md`

---

## Background an implementer needs (zero prior context assumed)

- **What Praxis is:** a Docker-based AI orchestrator. A subscription "brain" (Claude Opus via `claude -p`) plans and reviews; a local LLM implements. Orchestrating clients drive Praxis over **MCP**.
- **The MCP server** lives in `src/mcp_server/`. `server.py` holds tool functions; `client.py` is `PraxisClient`, a thin async httpx wrapper over the Praxis REST API with Bearer auth. Tools **never raise** to the MCP client — client errors are caught and returned as `{"error": code, "message": ...}` via the module-level `_error(exc)` helper.
- **Pattern for every tool:** a function named `<tool>_impl(client, ...)` (takes a `PraxisClient`-like object, independently testable) AND a `@mcp.tool()`-decorated wrapper named `<tool>` that calls `PraxisClient.from_env()` and delegates. Both live in `src/mcp_server/server.py`.
- **`PraxisClient` methods:** `await client.get(path)` and `await client.post(path, json)`. Both return parsed JSON (a dict or list) or raise `PraxisClientError(code, message)`.
- **The REST surface we reuse:** `GET /api/projects` (in `src/orchestrator/api/projects.py:66`) returns a **JSON list** of project rows. Each row includes at least: `id`, `name`, `repo_url`, `default_branch`, `approval_gate`, `model_name`, `harness`. There is NO by-`repo_url` lookup endpoint, so filtering happens client-side in `get_project_impl`.
- **Note on field name:** the project row uses `model_name` (not `model`). The MCP tool output should expose it as `model` for orchestrator ergonomics — map `row["model_name"] -> "model"` in the impl.
- **Guide file:** `src/mcp_server/resources/orchestration_guide.md`, loaded by `server.load_orchestration_guide()`.
- **Guide test constraint (IMPORTANT):** `tests/test_mcp_resources.py::test_guide_names_every_registered_tool` asserts every registered MCP tool name appears somewhere in the guide markdown. Therefore **the same task that registers a new tool must also add that tool's name to the guide**, or the whole test suite goes red. Each tool task below does this.
- **Test file:** add tests to `tests/test_mcp_server.py`. It defines a `FakeClient` (records calls, returns canned responses keyed by `(method, path)`). Reuse it.
- **Run all tests:** `uv run pytest tests/test_mcp_server.py tests/test_mcp_resources.py -v`
- **Lint/format before each commit:** `uv run ruff format src/ tests/` then `uv run ruff check --fix src/ tests/`.

---

### Task 1: `get_project` read-back tool

**Files:**
- Modify: `src/mcp_server/server.py` (add `get_project_impl` after `list_providers_impl`; add `@mcp.tool() get_project` wrapper near the other wrappers)
- Modify: `src/mcp_server/resources/orchestration_guide.md` (add the tool name so the naming test stays green)
- Test: `tests/test_mcp_server.py`

**Depends on:** None

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_server.py`:

```python
async def test_get_project_found_maps_model_name_to_model() -> None:
    client = FakeClient(
        {
            ("GET", "/api/projects"): [
                {
                    "id": "pr1",
                    "name": "telegram",
                    "repo_url": "https://github.com/u/r",
                    "default_branch": "main",
                    "approval_gate": True,
                    "model_name": "qwen3-32b",
                    "harness": "opencode",
                },
                {
                    "id": "pr2",
                    "name": "other",
                    "repo_url": "https://github.com/u/other",
                    "default_branch": "main",
                    "approval_gate": False,
                    "model_name": "qwen3-9b",
                    "harness": "aider",
                },
            ]
        }
    )
    result = await server.get_project_impl(client, repo_url="https://github.com/u/r")
    assert result["project_id"] == "pr1"
    assert result["model"] == "qwen3-32b"
    assert result["harness"] == "opencode"
    assert result["default_branch"] == "main"
    assert result["approval_gate"] is True


async def test_get_project_missing_returns_null_not_error() -> None:
    client = FakeClient({("GET", "/api/projects"): []})
    result = await server.get_project_impl(
        client, repo_url="https://github.com/u/never-seen"
    )
    assert result == {"project": None}


async def test_get_project_client_error_returns_error_shape() -> None:
    class ErrClient:
        async def get(self, path: str):
            raise PraxisClientError("connection_error", "down")

    result = await server.get_project_impl(
        ErrClient(), repo_url="https://github.com/u/r"
    )
    assert result == {"error": "connection_error", "message": "down"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp_server.py -k get_project -v`
Expected: FAIL with `AttributeError: module 'mcp_server.server' has no attribute 'get_project_impl'`

- [ ] **Step 3: Write the implementation**

In `src/mcp_server/server.py`, add after `list_providers_impl` (before `get_task_logs_impl`):

```python
async def get_project_impl(client: Any, repo_url: str) -> dict[str, Any]:
    """Return a repo's configured worker model + harness, or null when unknown.

    Praxis creates projects lazily (execute_plan / dispatch_task), so a repo it
    has never seen simply has no project yet. That is a normal state, returned as
    ``{"project": None}`` so the orchestrator can fall back to asking the human,
    NOT an error.
    """
    try:
        projects = await client.get("/api/projects")
    except PraxisClientError as exc:
        return _error(exc)
    rows = projects if isinstance(projects, list) else []
    for row in rows:
        if row.get("repo_url") == repo_url:
            return {
                "project_id": row.get("id"),
                "name": row.get("name"),
                "model": row.get("model_name"),
                "harness": row.get("harness"),
                "default_branch": row.get("default_branch"),
                "approval_gate": row.get("approval_gate"),
            }
    return {"project": None}
```

Then add the tool wrapper in the FastMCP registration block, after the `list_providers` wrapper:

```python
@mcp.tool()
async def get_project(repo_url: str) -> dict[str, Any]:
    """Read a repo's configured worker model + harness from Praxis.

    Returns {project_id, name, model, harness, default_branch, approval_gate}, or
    {"project": null} when Praxis has no project for that repo yet. Call this
    BEFORE execute_plan / dispatch_task: if a model is configured, reuse it; if
    null, ask the user which worker model to use (see list_providers).
    """
    return await get_project_impl(PraxisClient.from_env(), repo_url=repo_url)
```

- [ ] **Step 4: Keep the guide-naming test green**

In `src/mcp_server/resources/orchestration_guide.md`, add a one-line bullet naming the tool wherever tools are listed (a fuller section comes in Task 3). If a tool list exists, add:

```markdown
- `get_project` — read a repo's configured worker model + harness (or null if unknown).
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_server.py tests/test_mcp_resources.py -v`
Expected: PASS (including `test_guide_names_every_registered_tool`).

- [ ] **Step 6: Lint, format, commit**

```bash
uv run ruff format src/ tests/
uv run ruff check --fix src/ tests/
git add src/mcp_server/server.py src/mcp_server/resources/orchestration_guide.md tests/test_mcp_server.py
git commit -m "feat(mcp): add get_project read-back tool"
```

---

### Task 2: `list_projects` read-back tool

**Files:**
- Modify: `src/mcp_server/server.py` (add `list_projects_impl` + `@mcp.tool() list_projects`)
- Modify: `src/mcp_server/resources/orchestration_guide.md` (add the tool name)
- Test: `tests/test_mcp_server.py`

**Depends on:** Task 1 (edits the same two files; serialize to avoid conflicts)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_server.py`:

```python
async def test_list_projects_returns_slim_rows() -> None:
    client = FakeClient(
        {
            ("GET", "/api/projects"): [
                {
                    "id": "pr1",
                    "name": "telegram",
                    "repo_url": "https://github.com/u/r",
                    "model_name": "qwen3-32b",
                    "harness": "opencode",
                    "approval_gate": True,
                },
            ]
        }
    )
    result = await server.list_projects_impl(client)
    assert result["projects"] == [
        {
            "id": "pr1",
            "name": "telegram",
            "repo_url": "https://github.com/u/r",
            "model": "qwen3-32b",
            "harness": "opencode",
        }
    ]


async def test_list_projects_empty() -> None:
    client = FakeClient({("GET", "/api/projects"): []})
    result = await server.list_projects_impl(client)
    assert result == {"projects": []}


async def test_list_projects_client_error_returns_error_shape() -> None:
    class ErrClient:
        async def get(self, path: str):
            raise PraxisClientError("auth_error", "bad token")

    result = await server.list_projects_impl(ErrClient())
    assert result == {"error": "auth_error", "message": "bad token"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp_server.py -k list_projects -v`
Expected: FAIL with `AttributeError: module 'mcp_server.server' has no attribute 'list_projects_impl'`

- [ ] **Step 3: Write the implementation**

In `src/mcp_server/server.py`, add after `get_project_impl`:

```python
async def list_projects_impl(client: Any) -> dict[str, Any]:
    """List repos Praxis knows, each with its configured worker model + harness."""
    try:
        projects = await client.get("/api/projects")
    except PraxisClientError as exc:
        return _error(exc)
    rows = projects if isinstance(projects, list) else []
    return {
        "projects": [
            {
                "id": row.get("id"),
                "name": row.get("name"),
                "repo_url": row.get("repo_url"),
                "model": row.get("model_name"),
                "harness": row.get("harness"),
            }
            for row in rows
        ]
    }
```

Then add the tool wrapper after the `get_project` wrapper:

```python
@mcp.tool()
async def list_projects() -> dict[str, Any]:
    """List repos Praxis already knows, with each one's worker model + harness.

    Use this to discover which repo_url values Praxis is configured for instead
    of guessing. For a single repo's config, use get_project.
    """
    return await list_projects_impl(PraxisClient.from_env())
```

- [ ] **Step 4: Keep the guide-naming test green**

In `src/mcp_server/resources/orchestration_guide.md`, add:

```markdown
- `list_projects` — list repos Praxis knows, each with its configured model + harness.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_server.py tests/test_mcp_resources.py -v`
Expected: PASS.

- [ ] **Step 6: Lint, format, commit**

```bash
uv run ruff format src/ tests/
uv run ruff check --fix src/ tests/
git add src/mcp_server/server.py src/mcp_server/resources/orchestration_guide.md tests/test_mcp_server.py
git commit -m "feat(mcp): add list_projects read-back tool"
```

---

### Task 3: Orchestration-guide "resolve the worker model" section

**Files:**
- Modify: `src/mcp_server/resources/orchestration_guide.md`
- Test: `tests/test_mcp_resources.py`

**Depends on:** Task 1, Task 2 (the section references both new tools)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mcp_resources.py`:

```python
@pytest.mark.unit
def test_guide_documents_resolve_model_flow() -> None:
    """The guide must teach reading the configured model before dispatching."""
    guide = server.load_orchestration_guide()
    assert "Resolve the worker model" in guide
    assert "get_project" in guide
    assert "list_projects" in guide
    # The fallback path when no project is configured yet.
    assert "list_providers" in guide
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_resources.py -k resolve_model -v`
Expected: FAIL on `assert "Resolve the worker model" in guide`.

- [ ] **Step 3: Add the section to the guide**

First read the current file to place the section sensibly (ideally before the section that describes `execute_plan` / `dispatch_task`):

Run: `uv run python -c "import pathlib; print(pathlib.Path('src/mcp_server/resources/orchestration_guide.md').read_text(encoding='utf-8'))"`

Then insert this section (adjust surrounding heading level to match the file's existing style):

```markdown
## Resolve the worker model before dispatching

Praxis decomposes and gates a plan against the SPECIFIC local worker model that
will implement it, so pass the right `model`. Resolve it in this order:

1. Call `get_project(repo_url)`.
2. If it returns a `model`, reuse that value — the project is already configured.
3. If it returns `{"project": null}` (Praxis has never seen this repo), call
   `list_providers` to see the available worker models and ask the user which to
   use. Do not invent a model name.
4. Pass the resolved `model` to `execute_plan` (for a full plan) or
   `dispatch_task` (for a single task).
5. Watch progress with `poll_plan` (or `poll_task`) until terminal.

Use `list_projects` to discover which repos Praxis already knows instead of
guessing a `repo_url`.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_resources.py -v`
Expected: PASS (both `test_guide_documents_resolve_model_flow` and `test_guide_names_every_registered_tool`).

- [ ] **Step 5: Commit**

```bash
git add src/mcp_server/resources/orchestration_guide.md tests/test_mcp_resources.py
git commit -m "docs(mcp): teach resolve-model-before-dispatch flow in orchestration guide"
```

---

### Task 4: Update CLAUDE.md MCP-surface note + full-suite gate

**Files:**
- Modify: `CLAUDE.md` (the Gotchas line about the MCP server surface)

**Depends on:** Task 1, Task 2, Task 3

- [ ] **Step 1: Update the MCP note in CLAUDE.md**

Find the Gotchas line (currently): `- **MCP server is a separate package** (`src/mcp_server/`); only engine addition is `POST /api/dispatch`.`

Replace it with:

```markdown
- **MCP server is a separate package** (`src/mcp_server/`); read-back tools
  `get_project`/`list_projects` wrap `GET /api/projects` client-side (no new REST
  endpoint). Orchestrators resolve a repo's configured worker `model` via
  `get_project` before `execute_plan`/`dispatch_task` (see the orchestration guide).
```

- [ ] **Step 2: Run the full test suite**

Run: `uv run pytest --cov=orchestrator --cov-report=term-missing -q`
Expected: PASS, coverage >= 80% (unchanged; new code is fully tested).

- [ ] **Step 3: Type-check the changed package**

Run: `uv run mypy src/orchestrator/ --ignore-missing-imports`
Expected: no new errors.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: note MCP read-back tools + resolve-model flow in CLAUDE.md"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1 (no dependencies)
- **Wave 2:** Task 2 (depends on Task 1 — same files, serialized)
- **Wave 3:** Task 3 (depends on Task 1, Task 2)
- **Wave 4:** Task 4 (depends on Task 1, Task 2, Task 3)

All tasks are sequential because Tasks 1-3 all edit `src/mcp_server/server.py` and/or
`orchestration_guide.md`, and the guide-naming test couples tool registration to guide
content. Run them in order; do not parallelize.

## Notes

- **No engine change.** `decompose_plan()` and the capability profiling are untouched;
  this plan only adds read-back + guidance so callers pass the right `model`.
- **repo_url matching is exact**, mirroring `execute_plan`'s `WHERE repo_url = ?`
  lookup. If a project was created with a `.git` suffix and the caller omits it (or
  vice-versa), `get_project` returns `{"project": null}` and the orchestrator falls
  back to asking — acceptable and predictable. A normalization pass is explicitly out
  of scope (YAGNI) unless real usage shows it is needed.
- **Optional follow-up (deferred):** the client `decompose` skill, captured in
  `docs/superpowers/notes/decompose-skill-optional.md`.
```
