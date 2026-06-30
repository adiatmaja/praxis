# MCP Orchestration Guide Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve a static markdown guide as an MCP resource (`praxis://guide/orchestration`) that teaches an orchestrating agent both when to delegate to Praxis and how to drive its MCP tools.

**Architecture:** A markdown file shipped inside the `mcp_server` package holds the guide content. A small package-relative loader reads it (CWD-independent). A FastMCP `@mcp.resource` wrapper delegates to the loader, mirroring the existing `*_impl` + thin-tool-wrapper pattern in `server.py`. `pyproject.toml` is updated so the `.md` ships as package data. Tests assert the resource is registered, returns non-empty markdown, names every MCP tool (drift guard), and loads regardless of CWD.

**Tech Stack:** Python 3.11, FastMCP (`mcp>=1.2`), `importlib.resources`, pytest (`asyncio_mode=auto`), setuptools package-data, ruff, mypy.

---

## Context for a fresh session

Read these before starting — they are the ground truth this plan builds on:

- **Spec:** `docs/superpowers/specs/2026-06-29-mcp-orchestration-guide-design.md` (this plan implements it).
- **MCP server:** `src/mcp_server/server.py` — registers `FastMCP("praxis")` as module-level `mcp`, defines `*_impl(client, ...)` functions, and thin `@mcp.tool()` wrappers that build `PraxisClient.from_env()` and delegate. The 5 live tools: `dispatch_task`, `poll_task`, `list_providers`, `get_task_logs`, `cancel_task`. Follow this exact split: a plain testable function + a thin decorated wrapper.
- **MCP client:** `src/mcp_server/client.py` — `PraxisClient`, `PraxisClientError(code, message)`. Error codes surfaced to the caller: `auth_error`, `not_found`, `validation_error`, `connection_error`, `wrong_service`, `request_error`, `config_error`. The guide's troubleshooting section must use these exact codes.
- **Tests pattern:** `tests/test_mcp_server.py` imports `from mcp_server import server` and calls the `*_impl` functions directly with a `FakeClient`. New resource tests follow the same import style but need no client.
- **Packaging:** `pyproject.toml` uses `[tool.setuptools.packages.find]` with `where = ["src"]`. There is currently NO package-data config — non-`.py` files are not shipped by default, so this plan adds it.
- **`execute_plan`:** a sixth tool defined by Spec 2 (`2026-06-29-capability-aware-execution-design.md`), not yet implemented. The guide documents it as part of the decision tree. The drift-guard test (Task 4) must NOT assert `execute_plan` is a registered tool yet (it is not); it asserts the guide *mentions* it as text. See Task 4 for the exact split.

### Conventions (from CLAUDE.md / rules)
- `ruff format` (NOT `ruff fmt`). Run: `uv run ruff format src/ tests/` then `uv run ruff check --fix src/ tests/`.
- Type annotations on every signature; `X | Y` unions; built-in generics.
- Google-style docstrings; no `print()`.
- No em dashes in prose/docs/commits (use comma/colon/semicolon).
- Commit messages end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Run tests: `uv run pytest tests/test_mcp_resources.py -v`. Full suite: `uv run pytest --cov=orchestrator --cov-report=term-missing -v` (note: coverage targets `orchestrator`; `mcp_server` is a sibling package, so also run the targeted file).

---

## File Structure

- `src/mcp_server/resources/__init__.py` (new, empty) — makes `resources` an importable subpackage so `importlib.resources` can address it.
- `src/mcp_server/resources/orchestration_guide.md` (new) — the guide content (6 sections).
- `src/mcp_server/server.py` (modify) — add `load_orchestration_guide()` loader + `@mcp.resource(...)` wrapper.
- `pyproject.toml` (modify) — ship `*.md` package data.
- `tests/test_mcp_resources.py` (new) — resource registration, content, drift guard, CWD-independence.

---

### Task 1: Package-relative content loader

**Files:**
- Create: `src/mcp_server/resources/__init__.py` (empty)
- Create: `src/mcp_server/resources/orchestration_guide.md` (minimal stub for now; full content in Task 3)
- Modify: `src/mcp_server/server.py`
- Test: `tests/test_mcp_resources.py`

**Depends on:** None

- [ ] **Step 1: Create the empty subpackage marker**

Create `src/mcp_server/resources/__init__.py` with a single line:

```python
"""Packaged static resources served by the Praxis MCP server."""
```

- [ ] **Step 2: Create a minimal stub content file**

Create `src/mcp_server/resources/orchestration_guide.md` with placeholder content (replaced in Task 3):

```markdown
# Praxis Orchestration Guide

(stub - full content added in Task 3)
```

- [ ] **Step 3: Write the failing test**

Create `tests/test_mcp_resources.py`:

```python
"""Unit tests for the MCP orchestration-guide resource."""

from __future__ import annotations

import pytest

from mcp_server import server


@pytest.mark.unit
def test_load_orchestration_guide_returns_nonempty_markdown() -> None:
    text = server.load_orchestration_guide()
    assert isinstance(text, str)
    assert text.strip()
    assert text.lstrip().startswith("#")
```

- [ ] **Step 4: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_resources.py::test_load_orchestration_guide_returns_nonempty_markdown -v`
Expected: FAIL with `AttributeError: module 'mcp_server.server' has no attribute 'load_orchestration_guide'`

- [ ] **Step 5: Implement the loader**

In `src/mcp_server/server.py`, add the import near the top (after the existing imports):

```python
from importlib import resources
```

Then add the loader function (place it above the `# --- FastMCP registration` section):

```python
def load_orchestration_guide() -> str:
    """Read the packaged orchestration-guide markdown, CWD-independent.

    Returns:
        The full markdown content of the orchestration guide.
    """
    return (
        resources.files("mcp_server.resources")
        .joinpath("orchestration_guide.md")
        .read_text(encoding="utf-8")
    )
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_resources.py::test_load_orchestration_guide_returns_nonempty_markdown -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/mcp_server/resources/__init__.py src/mcp_server/resources/orchestration_guide.md src/mcp_server/server.py tests/test_mcp_resources.py
git commit -m "feat(mcp): add package-relative orchestration-guide loader"
```

---

### Task 2: Register the FastMCP resource

**Files:**
- Modify: `src/mcp_server/server.py`
- Test: `tests/test_mcp_resources.py`

**Depends on:** Task 1

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_resources.py`:

```python
@pytest.mark.unit
async def test_orchestration_guide_resource_registered() -> None:
    resources_list = await server.mcp.list_resources()
    uris = {str(r.uri) for r in resources_list}
    assert "praxis://guide/orchestration" in uris


@pytest.mark.unit
async def test_orchestration_guide_resource_reads_content() -> None:
    contents = await server.mcp.read_resource("praxis://guide/orchestration")
    # FastMCP returns an iterable of content parts; join their text.
    text = "".join(part.content for part in contents)
    assert text.strip()
    assert text.lstrip().startswith("#")
```

Note: `read_resource` returns an iterable of `ReadResourceContents` (each has a `.content` str). If the installed `mcp` version differs, adapt the extraction to whatever carries the text, but keep the assertion that non-empty markdown comes back.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_resources.py -k resource -v`
Expected: FAIL (resource not registered; URI not present)

- [ ] **Step 3: Register the resource**

In `src/mcp_server/server.py`, after the existing `@mcp.tool()` definitions, add:

```python
@mcp.resource("praxis://guide/orchestration")
def orchestration_guide() -> str:
    """Workflow guide for an agent orchestrating Praxis over MCP.

    Covers when to delegate to Praxis and how to drive its tools: tool
    selection, what context to pass, polling cadence, task statuses, and
    troubleshooting. For live provider/model state, call list_providers.
    """
    return load_orchestration_guide()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_resources.py -k resource -v`
Expected: PASS

If `read_resource`/`list_resources` are not coroutines in the installed `mcp` version, drop `async`/`await` accordingly and re-run. Confirm the actual API once with:
`uv run python -c "from mcp_server import server; import inspect; print(inspect.signature(server.mcp.read_resource))"`

- [ ] **Step 5: Commit**

```bash
git add src/mcp_server/server.py tests/test_mcp_resources.py
git commit -m "feat(mcp): serve orchestration guide as praxis://guide/orchestration"
```

---

### Task 3: Write the full guide content

**Files:**
- Modify: `src/mcp_server/resources/orchestration_guide.md`

**Depends on:** Task 1

- [ ] **Step 1: Replace the stub with the full guide**

Overwrite `src/mcp_server/resources/orchestration_guide.md` with the content below. It is the canonical guide; keep all six section headings and ensure every tool name appears verbatim (`dispatch_task`, `poll_task`, `list_providers`, `get_task_logs`, `cancel_task`, `execute_plan`) so the Task 4 drift guard passes.

````markdown
# Praxis Orchestration Guide

You are an agent connected to Praxis over MCP. Praxis is an AI agent orchestrator:
you plan and reason, Praxis runs a local-LLM worker that implements the change in a
one-shot Docker container (clone, implement, commit, open a PR), and Praxis's own
brain then reviews the PR and merges it on pass (re-dispatching on fail). This guide
explains when to hand work to Praxis and how to drive its tools.

For live data (which worker models exist, whether brain providers are authenticated),
call `list_providers`. This guide is static and does not embed that state.

## 1. When to delegate to Praxis

Delegate implementation that is bulk, parallelizable, or lower-novelty: it runs on the
local model and conserves your own subscription budget. Keep work that is high-novelty,
architectural, or ambiguous in your own session, where your stronger reasoning matters.

The flow is asynchronous and one-shot. You get a `task_id` back immediately, the worker
runs in the background, and you poll for the result. The MCP connection is blind between
calls: nothing streams to you, so you must poll.

## 2. Picking the tool

- `dispatch_task` - one self-contained change you have already sized small. Use when you
  can describe a single task ("add input validation to the registration endpoint"). No
  capability gating is applied; you are asserting it is worker-sized.
- `execute_plan` - a whole externally-authored plan (for example, a plan you wrote in
  another session). Praxis capability-gates the plan against the local model and
  decomposes it into do-able leaves, flagging any leaf too hard as
  `needs_stronger_model`. Use this instead of many `dispatch_task` calls when handing
  over a multi-task plan.
- `list_providers` - call first to see available worker models and brain/provider auth
  status before dispatching.
- `poll_task`, `get_task_logs`, `cancel_task` - lifecycle and triage (sections 4 and 6).

## 3. What context to pass

Both `dispatch_task` and `execute_plan` take an optional `context` field. Pass a focused,
task-relevant slice: conventions, architecture notes, and the relevant plan slice that
help implement THIS task. Do not paste your whole memory tree.

Never include secrets, tokens, or `.env` values. They are redacted server-side, but keep
them out of the context anyway.

## 4. Polling cadence

After dispatching, poll `poll_task(task_id)` at a reasonable interval. Do not spin in a
tight loop: work typically takes minutes (clone, implement, review). Each poll returns
the current `status`, the `pr_url` once a PR exists, the `review` feedback once reviewed,
and a `dashboard_url`. The `dashboard_url` is the rich human view with live logs if you
or your user want to watch in a browser.

## 5. Reading statuses

The task moves through this state machine:

`pending -> in_progress -> reviewing -> passed -> merged`

- `pending` / `in_progress` - queued or being implemented by the worker. Keep polling.
- `reviewing` - the worker opened a PR; Praxis's brain is reviewing it. Keep polling.
- `passed` - review passed; Praxis squash-merges. Usually transient before `merged`.
- `merged` - done. The change is on the base branch; read `pr_url` for the record.
- `failed` - a run failed review or produced no usable change. Praxis automatically
  re-dispatches up to the project's max_retries before the task goes terminal. Inspect
  with `get_task_logs` if it stays failed.
- `blocked` / `needs_stronger_model` (via `execute_plan`) - Praxis judged the task too
  hard for the local model and did not ship guesswork. Revise the task into smaller
  pieces, accept the project's escalation outcome, or handle it yourself.

## 6. Troubleshooting

- `get_task_logs(task_id)` - returns the concatenated worker run logs. Use it to see why
  a task is wedged or repeatedly failing.
- `cancel_task(task_id)` - stops a running task's containers and marks it failed. Use it
  to abandon a runaway or mis-dispatched task.

Tools return `{"error": code, "message": ...}` on failure instead of raising. Codes:

- `connection_error` - Praxis is unreachable. Confirm the server is running.
- `wrong_service` - `PRAXIS_BASE_URL` points at something that is not Praxis (it
  answered HTML, not JSON). Fix the URL/port to match Praxis's `PORT`.
- `auth_error` - bad or missing token. Check `PRAXIS_AUTH_TOKEN`.
- `config_error` - `PRAXIS_AUTH_TOKEN` is not set at all.
- `validation_error` - the request body was rejected. Check required fields.
- `not_found` - the `task_id` does not exist.
- `request_error` - an unclassified non-2xx response; read `message`.
````

- [ ] **Step 2: Run the existing tests to confirm nothing broke**

Run: `uv run pytest tests/test_mcp_resources.py -v`
Expected: PASS (content is non-empty markdown starting with `#`)

- [ ] **Step 3: Commit**

```bash
git add src/mcp_server/resources/orchestration_guide.md
git commit -m "docs(mcp): write full orchestration guide content"
```

---

### Task 4: Tool-name drift guard

**Files:**
- Test: `tests/test_mcp_resources.py`

**Depends on:** Task 2, Task 3

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_resources.py`:

```python
@pytest.mark.unit
async def test_guide_names_every_registered_tool() -> None:
    """Every live MCP tool must be documented, so the guide cannot drift."""
    text = server.load_orchestration_guide()
    tools = await server.mcp.list_tools()
    for tool in tools:
        assert tool.name in text, f"guide omits tool {tool.name}"


@pytest.mark.unit
def test_guide_mentions_execute_plan_even_before_implemented() -> None:
    """execute_plan is documented as part of the decision tree (Spec 2)."""
    text = server.load_orchestration_guide()
    assert "execute_plan" in text
```

Rationale for the split: `test_guide_names_every_registered_tool` iterates the *actually
registered* tools, so it stays correct whether or not `execute_plan` exists yet. The
second test pins the forward-looking `execute_plan` mention as text. When Spec 2 adds the
real `execute_plan` tool, the first test will then also cover it automatically.

- [ ] **Step 2: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_resources.py -k "drift or tool or execute" -v`
Expected: PASS (the guide content from Task 3 names all five live tools plus `execute_plan`)

If it FAILS naming a tool, the guide content (Task 3) is missing that tool name; add it. This is the guard working as intended.

- [ ] **Step 3: Commit**

```bash
git add tests/test_mcp_resources.py
git commit -m "test(mcp): guard guide against tool-name drift"
```

---

### Task 5: Ship the markdown as package data

**Files:**
- Modify: `pyproject.toml`
- Test: `tests/test_mcp_resources.py`

**Depends on:** Task 1

- [ ] **Step 1: Write the failing test (CWD-independence)**

Append to `tests/test_mcp_resources.py`:

```python
import os
from pathlib import Path


@pytest.mark.unit
def test_guide_loads_regardless_of_cwd(tmp_path: Path) -> None:
    """The loader resolves the file via the package, not the working dir."""
    original = Path.cwd()
    os.chdir(tmp_path)
    try:
        text = server.load_orchestration_guide()
        assert text.strip()
    finally:
        os.chdir(original)
```

- [ ] **Step 2: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_resources.py::test_guide_loads_regardless_of_cwd -v`
Expected: PASS already, because `importlib.resources` resolves via the package, not CWD. This test locks in that behavior so a future refactor to a relative-path read cannot regress it.

- [ ] **Step 3: Add package-data config so installed wheels carry the .md**

In `pyproject.toml`, after the `[tool.setuptools.packages.find]` block (around line 41), add:

```toml
[tool.setuptools.package-data]
mcp_server = ["resources/*.md"]
```

This ensures `uv pip install`/wheel builds include the markdown. In editable/dev installs the file is already present on disk, so tests pass without it, but the config is required for a real install (and for the Docker image).

- [ ] **Step 4: Verify the build includes the data file**

Run: `uv run python -c "from importlib import resources; print(resources.files('mcp_server.resources').joinpath('orchestration_guide.md').is_file())"`
Expected: `True`

- [ ] **Step 5: Run the full resource test file**

Run: `uv run pytest tests/test_mcp_resources.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml tests/test_mcp_resources.py
git commit -m "build(mcp): ship orchestration guide as package data"
```

---

### Task 6: Final verification (lint, types, full suite)

**Files:** none (verification only)

**Depends on:** Task 1, Task 2, Task 3, Task 4, Task 5

- [ ] **Step 1: Format and lint**

Run:
```bash
uv run ruff format src/ tests/
uv run ruff check --fix src/ tests/
```
Expected: no remaining errors.

- [ ] **Step 2: Type check**

Run: `uv run mypy src/mcp_server/ --ignore-missing-imports`
Expected: no errors. (If `mcp.server.fastmcp` resource/list APIs are untyped, the `--ignore-missing-imports` flag and existing patterns in `server.py` already absorb that.)

- [ ] **Step 3: Run the full test suite**

Run: `uv run pytest --cov=orchestrator --cov-report=term-missing -v`
Then the targeted MCP file (not covered by the `orchestrator` cov target):
`uv run pytest tests/test_mcp_resources.py tests/test_mcp_server.py tests/test_mcp_client.py -v`
Expected: all PASS.

- [ ] **Step 4: Commit any formatting changes**

```bash
git add -A
git commit -m "chore(mcp): format and lint orchestration-guide resource" || echo "nothing to commit"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1 (loader + stub), Task 3 (full content — depends only on Task 1's file existing; can be written concurrently once the file/path exist)
- **Wave 2:** Task 2 (register resource — depends on Task 1), Task 5 (package data — depends on Task 1)
- **Wave 3:** Task 4 (drift guard — depends on Task 2 + Task 3)
- **Wave 4:** Task 6 (final verification — depends on all)

Note: Task 3 technically only needs the file created in Task 1 Step 2. If running strictly in parallel, let Task 1 finish its Steps 1-2 (file creation) before Task 3 overwrites it. The safe serialization is Task 1 -> {Task 2, Task 3, Task 5} -> Task 4 -> Task 6.

## Appendix: Verifying the installed `mcp` resource API

FastMCP's resource read/list API has varied across `mcp` releases. Before writing the
Task 2 tests, confirm the actual shapes in the installed version:

```bash
uv run python -c "from mcp_server import server; import asyncio; print(asyncio.run(server.mcp.list_resources()))"
uv run python -c "from mcp_server import server; import inspect; print([m for m in dir(server.mcp) if 'resource' in m])"
```

Adapt the `read_resource` content-extraction in Task 2 Step 1 to whatever the installed
version returns (a list of content parts with a text/`.content` attribute). The invariant
to test is: the registered resource returns the non-empty markdown from
`load_orchestration_guide()`.
