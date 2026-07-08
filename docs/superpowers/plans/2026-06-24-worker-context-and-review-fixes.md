# Worker Context Injection & Review Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the Aider worker curated, secret-scrubbed implementation context (the gap that left it with "no memory of Claude Code"), and fix the reviewer so it inspects a real checkout instead of `/app` (the hole that let a destructive `.env.example` truncation merge).

**Architecture:** Two independent subsystems sharing the dispatch surface. **Epic A (context):** a new `context` field on `DispatchRequest` flows through a secret-scrubber into the agent container as `CONTEXT_TEXT`, written to `.praxis-context.md` and given to Aider via `--read`; the entrypoint also auto-`--read`s the cloned repo's own `CLAUDE.md`/`MEMORY.md`/`docs/*.md`. Aider stays GitHub-only — no local or gitignored files are ever mounted. **Epic B (review):** the review brain is spawned with `cwd` set to a fresh clone of the PR head (so its git/read tools work), the plan/spec text is injected into the review prompt, and a destructive-edit guardrail hard-blocks large deletions from existing files.

**Tech Stack:** Python 3.11, FastAPI, pytest (`asyncio_mode=auto`), Docker SDK, bash entrypoint, `gh`/`git` CLI.

---

## Background (read this first — assumes zero prior context)

Praxis is an AI agent orchestrator. A **brain** (Claude via `claude -p`, or other CLI/local providers through `core/llm_router.py`) plans and reviews; a **worker** (Aider in a Docker container) implements. The dispatch→implement→review→merge loop lives in `src/orchestrator/core/orchestrator.py`.

**How a task currently runs:**
1. An MCP client (e.g. Claude Code in another project) calls `POST /api/dispatch` (`src/orchestrator/api/dispatch.py`). This creates a one-task plan and activates it.
2. The orchestration loop's `dispatch_pending_tasks` (`orchestrator.py:124-161`) calls `AgentManager.spawn_agent` (`core/agent_manager.py:35`), which runs the `aider-agent:latest` container with env vars (`REPO_URL`, `BRANCH`, `TASK_PROMPT`, `PLAN_TEXT`, …).
3. The container's `docker/aider-agent/entrypoint.sh` **clones the repo from GitHub**, cuts a branch, runs Aider, pushes, and opens a PR. It calls back to `/api/internal/agent-done`.
4. `Orchestrator.review_task` (`orchestrator.py:163`) fetches the PR diff via `gh pr diff <n> --repo <slug>`, sends it to the brain's `review_diff`, then merges (squash) on "pass" or comments + retries on "fail".

**The two problems this plan fixes (both found in a real run):**

- **Problem 1 — the worker has no memory/context.** Aider only ever sees the cloned GitHub repo + the `TASK_PROMPT` + the plan file (`entrypoint.sh:102-109` `--read`). It has none of the orchestrating Claude Code session's knowledge (conventions, decisions, architecture intent). **Design decision already made:** keep Aider strictly GitHub-only — never mount local or gitignored files (`.env`, data dirs, secrets), because that is a security regression and those files are exactly what must not reach a remote container. Instead, add a curated, secret-scrubbed `context` string to the dispatch call that rides in as a read-only `.praxis-context.md`, plus auto-`--read` the repo's own committed `CLAUDE.md`/`MEMORY.md`/`docs/*.md`. Curation is the caller's job (the MCP tool docstring tells Claude Code to pass a task-relevant slice, not its whole memory tree); the server-side scrubber + size cap is the safety net.

- **Problem 2 — the reviewer runs in the wrong directory, so review is non-functional.** The brain subprocess is launched by `core/llm_router.py:165` (`create_subprocess_exec`) and `opus_bridge.py` with **no `cwd`**, so it inherits the orchestrator's own working dir (`/app` in the container). The brain has tool access and tries to verify the diff against a real checkout, finds `/app` is not a git repo, and falls back to a non-committal "pass" — which is how a destructive `.env.example` truncation merged unreviewed. The fix: clone the PR head into a temp dir and pass it as the brain's `cwd`; inject the plan/spec text into the review prompt; and add a deterministic guardrail that hard-blocks PRs deleting large chunks from existing files. Also: surface the worker's "0 commits produced" case as a clear, explained failure instead of a raw GraphQL error.

**Key conventions for this codebase (from CLAUDE.md):**
- Run everything with `uv run ...`. Tests: `uv run pytest ...`. Format: `uv run ruff format` (NOT `ruff fmt`). Lint: `uv run ruff check --fix`. Types: `uv run mypy src/orchestrator/ --ignore-missing-imports`.
- `pytest-asyncio` with `asyncio_mode = "auto"` — async test functions run without a decorator. Mark tests `@pytest.mark.unit` / `@pytest.mark.integration`.
- Python 3.11, `X | Y` unions, built-in generics, Google-style docstrings, `logging` (never `print`), raw SQL via aiosqlite (no ORM).
- **The `aider-agent:latest` image is standalone and NOT in docker-compose — you MUST rebuild it after any `entrypoint.sh` change**, or a stale image silently runs old logic: `docker build -t aider-agent:latest -f docker/aider-agent/Dockerfile docker/aider-agent/`.
- The brain prompt always goes to the CLI via **stdin**, never argv (a full diff overflows the Windows ~32K argv limit). Do not change that.
- Existing `plan_text`/`plan_path` plumbing is the exact precedent for the new `context_text`: schemas → dispatch task dict → `orchestrator.py` read → `spawn_agent` param → env var → entrypoint. Mirror it.

**Verify before starting:** open each file referenced in the File Structure table and confirm the line numbers still match (the codebase moves); the anchors quoted in tasks are from the state at plan-writing time (2026-06-24).

---

## File Structure

| File | Responsibility | Epic |
|------|----------------|------|
| `src/orchestrator/core/context_scrub.py` (new) | Pure secret-redaction + size-cap of caller-supplied context | A |
| `src/orchestrator/models/schemas.py` | Add `context` field + validator to `DispatchRequest` | A |
| `src/orchestrator/api/dispatch.py` | Scrub context, thread into the one-task plan | A |
| `src/mcp_server/server.py` | Expose `context` param + curation guidance in tool docstring | A |
| `src/orchestrator/core/agent_manager.py` | `context_text` param → `CONTEXT_TEXT` env | A |
| `src/orchestrator/core/orchestrator.py` | Read `context_text` from plan task, pass to `spawn_agent`; review-cwd + guardrail | A, B |
| `docker/aider-agent/entrypoint.sh` | `--read` the context file + auto-discovered repo context files | A |
| `src/orchestrator/core/llm_router.py` | Thread `cwd` into `create_subprocess_exec` | B |
| `src/orchestrator/core/opus_bridge.py` | Thread `cwd` through `review_diff`/`_run_claude_raw`; plan text in prompt | B |
| `src/orchestrator/core/git_ops.py` | `clone_branch` helper for PR-head checkout; `pr_diff_stat` for guardrail | B |

---

## Epic A — Curated Worker Context Injection

### Task 1: Secret scrubber

**Files:**
- Create: `src/orchestrator/core/context_scrub.py`
- Test: `tests/test_context_scrub.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_context_scrub.py
import pytest

from orchestrator.core.context_scrub import scrub_context


@pytest.mark.unit
def test_redacts_env_assignments():
    raw = "Use the db.\nAPI_KEY=ghp_abcdef1234567890abcdef1234567890abcd\nDone."
    out = scrub_context(raw)
    assert "ghp_abcdef1234567890abcdef1234567890abcd" not in out
    assert "[REDACTED]" in out
    assert "Use the db." in out  # non-secret prose preserved


@pytest.mark.unit
def test_redacts_known_token_shapes():
    raw = "token sk-ABCDEFGHIJKLMNOPQRSTUVWX and AKIAIOSFODNN7EXAMPLE here"
    out = scrub_context(raw)
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWX" not in out
    assert "AKIAIOSFODNN7EXAMPLE" not in out


@pytest.mark.unit
def test_redacts_private_key_block():
    raw = "-----BEGIN PRIVATE KEY-----\nMIIEv...\n-----END PRIVATE KEY-----"
    out = scrub_context(raw)
    assert "MIIEv" not in out
    assert "[REDACTED PRIVATE KEY]" in out


@pytest.mark.unit
def test_caps_size():
    raw = "x" * 50_000
    out = scrub_context(raw, max_chars=10_000)
    assert len(out) <= 10_100  # cap + truncation notice
    assert "truncated" in out.lower()


@pytest.mark.unit
def test_none_and_empty():
    assert scrub_context(None) is None
    assert scrub_context("   ") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_context_scrub.py -v`
Expected: FAIL with `ModuleNotFoundError: orchestrator.core.context_scrub`

- [ ] **Step 3: Write minimal implementation**

```python
# src/orchestrator/core/context_scrub.py
"""Redact secrets and cap the size of caller-supplied worker context.

The MCP caller (e.g. Claude Code) is asked to pass only task-relevant memory,
but this is the one untrusted-for-secrets channel that gets written into the
agent's repo clone. We never trust curation alone: redact obvious secret shapes
and hard-cap length before the text reaches the worker container.
"""

from __future__ import annotations

import re


_DEFAULT_MAX_CHARS = 12_000

# KEY=secret / KEY: secret on a single line (value looks secret-ish: long/opaque).
_ENV_ASSIGN = re.compile(
    r"(?im)^\s*([A-Z0-9_]{2,})\s*[=:]\s*\S{8,}\s*$",
)
# Common opaque token shapes.
_TOKEN_SHAPES = re.compile(
    r"(?x)"
    r"(ghp_[A-Za-z0-9]{20,})"           # GitHub PAT
    r"|(github_pat_[A-Za-z0-9_]{20,})"
    r"|(sk-[A-Za-z0-9]{16,})"           # OpenAI-style
    r"|(AKIA[0-9A-Z]{16})"              # AWS access key id
    r"|(xox[baprs]-[A-Za-z0-9-]{10,})"  # Slack
    r"|(eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"  # JWT
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)


def scrub_context(text: str | None, max_chars: int = _DEFAULT_MAX_CHARS) -> str | None:
    """Return ``text`` with secrets redacted and length capped, or None if empty.

    Args:
        text: Raw caller-supplied context, or None.
        max_chars: Hard cap on output length (excluding the truncation notice).

    Returns:
        Scrubbed text, or None when the input is None/blank.
    """
    if text is None or not text.strip():
        return None

    scrubbed = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", text)
    scrubbed = _TOKEN_SHAPES.sub("[REDACTED]", scrubbed)
    scrubbed = _ENV_ASSIGN.sub(
        lambda m: f"{m.group(1)}=[REDACTED]", scrubbed
    )

    if len(scrubbed) > max_chars:
        scrubbed = scrubbed[:max_chars] + "\n\n[context truncated by Praxis]"
    return scrubbed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_context_scrub.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/context_scrub.py tests/test_context_scrub.py
git commit -m "feat: add secret scrubber for worker context injection"
```

---

### Task 2: `context` field on DispatchRequest

**Files:**
- Modify: `src/orchestrator/models/schemas.py:350-411`
- Test: `tests/test_schemas.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_schemas.py  (append)
import pytest

from orchestrator.models.schemas import DispatchRequest


@pytest.mark.unit
def test_dispatch_request_accepts_context():
    req = DispatchRequest(
        repo_url="https://github.com/o/r",
        instructions="do x",
        model="qwen3",
        context="Conventions: use ruff.",
    )
    assert req.context == "Conventions: use ruff."


@pytest.mark.unit
def test_dispatch_request_context_defaults_none():
    req = DispatchRequest(
        repo_url="https://github.com/o/r", instructions="do x", model="qwen3"
    )
    assert req.context is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_schemas.py -k context -v`
Expected: FAIL with `TypeError`/`ValidationError` (unexpected keyword `context`)

- [ ] **Step 3: Add the field**

In `src/orchestrator/models/schemas.py`, inside `class DispatchRequest`, add after the `plan_text` line (`:360`):

```python
    context: str | None = None
    """Curated, task-relevant context for the worker (memory, conventions,
    architecture notes). Scrubbed of secrets and size-capped server-side. NOT
    a place for secret values - those are redacted on arrival."""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_schemas.py -k context -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/models/schemas.py tests/test_schemas.py
git commit -m "feat: add context field to DispatchRequest"
```

---

### Task 3: Wire context through dispatch + MCP tool

**Files:**
- Modify: `src/orchestrator/api/dispatch.py:192-207`
- Modify: `src/mcp_server/server.py` (dispatch tool definition)
- Test: `tests/test_api_dispatch.py`

**Depends on:** Task 1, Task 2

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_dispatch.py  (append; reuses existing client/auth fixtures)
import pytest


@pytest.mark.integration
async def test_dispatch_scrubs_and_stores_context(client, auth_headers, monkeypatch):
    captured = {}

    async def fake_activate(plan_id, opus_plan, branch_name):
        captured["opus_plan"] = opus_plan

    monkeypatch.setattr(
        client.app.state.task_queue, "activate_plan", fake_activate
    )
    # get_tasks_for_plan must still return a task so the endpoint returns 201.
    async def fake_get_tasks(plan_id):
        return [{"id": "t1"}]

    monkeypatch.setattr(
        client.app.state.task_queue, "get_tasks_for_plan", fake_get_tasks
    )

    resp = client.post(
        "/api/dispatch",
        headers=auth_headers,
        json={
            "repo_url": "https://github.com/o/r",
            "instructions": "do x",
            "model": "qwen3",
            "context": "Use ruff.\nAPI_KEY=ghp_abcdef1234567890abcdef1234567890abcd",
        },
    )
    assert resp.status_code == 201
    task = captured["opus_plan"]["tasks"][0]
    assert "Use ruff." in task["context_text"]
    assert "ghp_abcdef" not in task["context_text"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_dispatch.py -k scrubs -v`
Expected: FAIL with `KeyError: 'context_text'`

- [ ] **Step 3: Wire dispatch.py**

At top of `src/orchestrator/api/dispatch.py`, add the import:

```python
from orchestrator.core.context_scrub import scrub_context
```

In `dispatch_task`, after the `plan_text` block (`:202-203`), add:

```python
    scrubbed_context = scrub_context(body.context)
    if scrubbed_context is not None:
        task_dict["context_text"] = scrubbed_context
```

- [ ] **Step 4: Add the MCP tool param**

In `src/mcp_server/server.py`, find the `dispatch_task` tool's input schema/signature and add a `context` string parameter mirroring `plan_text`. Set its description to:

```
Optional curated context to brief the worker: task-relevant project memory,
conventions, and architecture notes that help implement THIS task. Pass a
focused slice, not your whole memory tree. Do NOT include secrets, tokens, or
.env values - they are redacted server-side, but keep them out anyway.
```

Forward it in the `PraxisClient.dispatch(...)` call as `context=context`. (Add the `context` kwarg to `PraxisClient.dispatch` in `src/mcp_server/client.py`, passing it straight into the JSON body.)

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_api_dispatch.py -k scrubs -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/api/dispatch.py src/mcp_server/server.py src/mcp_server/client.py tests/test_api_dispatch.py
git commit -m "feat: scrub and thread caller context through MCP dispatch"
```

---

### Task 4: `context_text` → container env

**Files:**
- Modify: `src/orchestrator/core/agent_manager.py:35-72`
- Modify: `src/orchestrator/core/orchestrator.py:134-148`
- Test: `tests/test_agent_manager.py`

**Depends on:** Task 3

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_manager.py  (append; reuses existing fake docker client fixture)
import pytest


@pytest.mark.unit
async def test_spawn_agent_sets_context_env(agent_manager, fake_docker):
    await agent_manager.spawn_agent(
        task_id="abcd1234",
        repo_url="https://github.com/o/r",
        branch="agent/x",
        base_branch="main",
        task_prompt="do x",
        model_name="qwen3",
        callback_url="http://cb/",
        context_text="Conventions: ruff.",
    )
    env = fake_docker.last_run_kwargs["environment"]
    assert env["CONTEXT_TEXT"] == "Conventions: ruff."
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_manager.py -k context_env -v`
Expected: FAIL with `TypeError: unexpected keyword argument 'context_text'`

- [ ] **Step 3: Add the param + env**

In `spawn_agent` signature (`:47`), after `plan_text: str | None = None,` add:

```python
        context_text: str | None = None,
```

After the `plan_text` env block (`:71-72`), add:

```python
        if context_text is not None:
            environment["CONTEXT_TEXT"] = context_text
```

In `src/orchestrator/core/orchestrator.py`, after the `plan_text` read (`:135`):

```python
            context_text: str | None = plan_task.get("context_text")
```

And in the `spawn_agent(...)` call, after `plan_text=plan_text,` (`:148`):

```python
                context_text=context_text,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_agent_manager.py -k context_env -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/agent_manager.py src/orchestrator/core/orchestrator.py tests/test_agent_manager.py
git commit -m "feat: pass curated context to agent container as CONTEXT_TEXT"
```

---

### Task 5: Entrypoint reads context + repo-local memory

**Files:**
- Modify: `docker/aider-agent/entrypoint.sh:102-121`

**Depends on:** Task 4

> No unit test framework for the bash entrypoint; verification is a manual container build + run. The image is standalone and MUST be rebuilt (CLAUDE.md gotcha).

- [ ] **Step 1: Extend the `--read` args block**

Replace the existing `read_args` block (`:102-109`) with:

```bash
# Build --read args so Aider has reference context (read-only) while implementing.
read_args=()

# 1. The plan file (existing behavior).
if [ -n "${PLAN_PATH:-}" ]; then
    read_args+=(--read "${PLAN_PATH}")
elif [ -n "${PLAN_TEXT:-}" ]; then
    printf "%s" "${PLAN_TEXT}" > /home/agent/workspace/.praxis-plan.md
    read_args+=(--read ".praxis-plan.md")
fi

# 2. Caller-curated, secret-scrubbed context from the orchestrator.
if [ -n "${CONTEXT_TEXT:-}" ]; then
    printf "%s" "${CONTEXT_TEXT}" > /home/agent/workspace/.praxis-context.md
    read_args+=(--read ".praxis-context.md")
fi

# 3. Repo-local project memory already committed in the clone (GitHub-only:
#    we never mount local or gitignored files). Best-effort; skip if absent.
for ctx in CLAUDE.md MEMORY.md AGENTS.md; do
    if [ -f "${WORKSPACE}/${ctx}" ]; then
        read_args+=(--read "${ctx}")
    fi
done
while IFS= read -r doc; do
    [ -n "${doc}" ] && read_args+=(--read "${doc}")
done < <(find "${WORKSPACE}/docs" -maxdepth 1 -name '*.md' 2>/dev/null | sed "s|${WORKSPACE}/||")
```

- [ ] **Step 2: Rebuild the standalone agent image**

```bash
docker build -t aider-agent:latest -f docker/aider-agent/Dockerfile docker/aider-agent/
```

Expected: build succeeds.

- [ ] **Step 3: Smoke-test the read args resolve**

Run a throwaway container with a fake workspace and `CONTEXT_TEXT` set, confirming `.praxis-context.md` is written and no `find` error aborts the script (entrypoint uses `set -euo pipefail`, so the `find ... 2>/dev/null` must not fail the run when `docs/` is absent).

```bash
docker run --rm -e CONTEXT_TEXT="hello" --entrypoint bash aider-agent:latest -c \
  'mkdir -p /home/agent/workspace && printf "%s" "$CONTEXT_TEXT" > /home/agent/workspace/.praxis-context.md && cat /home/agent/workspace/.praxis-context.md'
```

Expected: prints `hello`, exit 0.

- [ ] **Step 4: Commit**

```bash
git add docker/aider-agent/entrypoint.sh
git commit -m "feat: aider reads curated context and repo-local memory"
```

---

## Epic B — Review Correctness

### Task 6: Thread `cwd` into the brain subprocess

**Files:**
- Modify: `src/orchestrator/core/llm_router.py:141-172`
- Modify: `src/orchestrator/core/opus_bridge.py:127-187,224-245`
- Test: `tests/test_llm_router.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_llm_router.py  (append)
import pytest


@pytest.mark.unit
async def test_run_passes_cwd_to_subprocess(router, monkeypatch):
    seen = {}

    async def fake_exec(*argv, **kwargs):
        seen["cwd"] = kwargs.get("cwd")
        return _FakeProc(stdout=b'{"verdict":"pass"}', stderr=b"", returncode=0)

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    # router configured to a CLI (non-local) call site for this test
    await router.run("review_diff_first", "prompt", project_id=None, cwd="/tmp/checkout")
    assert seen["cwd"] == "/tmp/checkout"
```

(`_FakeProc` is a small stub exposing async `communicate()` returning `(stdout, stderr)` and a `returncode` attr — define it at the top of the test module if not already present.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_llm_router.py -k passes_cwd -v`
Expected: FAIL with `TypeError: run() got an unexpected keyword argument 'cwd'`

- [ ] **Step 3: Add `cwd` to `LLMRouter.run`**

In `src/orchestrator/core/llm_router.py`, change the signature (`:141`):

```python
    async def run(
        self, call_site: str, prompt: str, project_id: str | None, cwd: str | None = None
    ) -> str:
```

In the `create_subprocess_exec(...)` call (`:165`), add `cwd=cwd,`:

```python
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
```

(`cwd=None` keeps current behavior — inherit the orchestrator cwd. The `local` provider branch ignores `cwd`, which is correct: it makes an HTTP call.)

- [ ] **Step 4: Thread `cwd` through OpusBridge**

In `src/orchestrator/core/opus_bridge.py`, add `cwd: str | None = None` to `_run_claude_raw` (`:127`) and pass it into its own `create_subprocess_exec`. Add `cwd: str | None = None` to `review_diff` (`:224`) and forward it:

```python
        if router is not None:
            ...
            raw = await router.run(call_site, prompt, project_id, cwd=cwd)
        else:
            raw = await self._run_claude(prompt, model, effort, cwd=cwd)
```

(Add the matching `cwd` param to `_run_claude` so it forwards to `_run_claude_raw`.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_llm_router.py tests/test_opus_bridge.py -v`
Expected: PASS (existing tests still green; new one passes)

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/core/llm_router.py src/orchestrator/core/opus_bridge.py tests/test_llm_router.py
git commit -m "feat: allow brain subprocess to run in a given working directory"
```

---

### Task 7: Review against a real PR-head checkout

**Files:**
- Modify: `src/orchestrator/core/git_ops.py` (add `clone_pr_head`)
- Modify: `src/orchestrator/core/orchestrator.py:179-191`
- Test: `tests/test_orchestrator.py`

**Depends on:** Task 6

- [ ] **Step 1: Write the failing test**

```python
# tests/test_orchestrator.py  (append)
import pytest


@pytest.mark.unit
async def test_review_clones_pr_head_and_passes_cwd(orchestrator, monkeypatch):
    calls = {}

    async def fake_clone_pr_head(pr_url, dest):
        calls["cloned_to"] = dest
        return dest

    async def fake_review_diff(diff, desc, **kwargs):
        calls["cwd"] = kwargs.get("cwd")
        return {"verdict": "pass", "feedback": "ok"}

    monkeypatch.setattr(orchestrator._git, "clone_pr_head", fake_clone_pr_head)
    monkeypatch.setattr(orchestrator._opus, "review_diff", fake_review_diff)
    # ... set up a task in REVIEWING with a pr_url via existing fixtures ...
    await orchestrator.review_task(task_id, project)
    assert calls["cwd"] == calls["cloned_to"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_orchestrator.py -k clones_pr_head -v`
Expected: FAIL with `AttributeError: ... no attribute 'clone_pr_head'`

- [ ] **Step 3: Add `clone_pr_head` to GitOps**

In `src/orchestrator/core/git_ops.py`:

```python
    async def clone_pr_head(self, pr_url: str, dest: str) -> str:
        """Clone the repo and check out the PR's head ref into ``dest``.

        Gives the reviewer brain a real git checkout to reason against, instead
        of the orchestrator's own /app cwd. Returns ``dest`` on success.
        """
        repo = self.repo_slug(pr_url)
        pr_number = await self.extract_pr_number(pr_url)
        # Resolve the head ref via gh, then clone+fetch that PR.
        await self._run_checked(["git", "clone", self._auth_url(repo), dest])
        await self._run_checked(
            ["gh", "pr", "checkout", str(pr_number), "--repo", repo], cwd=dest
        )
        return dest
```

(`_auth_url` builds the token-authenticated HTTPS URL from the slug — reuse the existing token helper used by `clone_repo`/`commit_and_push`; if none exists, construct `https://x-access-token:{token}@github.com/{repo}.git`.)

- [ ] **Step 4: Use it in `review_task`**

In `src/orchestrator/core/orchestrator.py`, replace the diff/review block (`:185-191`) with:

```python
        import tempfile

        with tempfile.TemporaryDirectory() as checkout:
            try:
                await self._git.clone_pr_head(task["pr_url"], checkout)
            except Exception:  # noqa: BLE001 - degrade, never wedge review
                logger.exception("review: PR-head clone failed; diff-only review")
                checkout = None  # type: ignore[assignment]

            diff = await self._git.get_pr_diff(".", pr_number, repo=repo)
            review = await self._opus.review_diff(
                diff,
                task["description"] or task["title"],
                model=project.get("agent_model"),
                effort=project.get("agent_model_effort"),
                cwd=checkout,
            )
```

(Keep the rest of `review_task` — verdict handling, merge, comment, retry — unchanged, now dedented under the `with` only for the review call; merge/comment still use `cwd="."` with `--repo`.)

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_orchestrator.py -k clones_pr_head -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/core/git_ops.py src/orchestrator/core/orchestrator.py tests/test_orchestrator.py
git commit -m "fix: review against a real PR-head checkout, not /app"
```

---

### Task 8: Inject plan/spec into the review prompt

**Files:**
- Modify: `src/orchestrator/core/opus_bridge.py:51-67,224-245`
- Test: `tests/test_opus_bridge.py`

**Depends on:** Task 7

- [ ] **Step 1: Write the failing test**

```python
# tests/test_opus_bridge.py  (append)
import pytest


@pytest.mark.unit
async def test_review_prompt_includes_plan(opus_bridge, monkeypatch):
    captured = {}

    async def fake_raw(prompt, model, effort, cwd=None):
        captured["prompt"] = prompt
        return '{"verdict":"pass","feedback":"ok","issues":[]}'

    monkeypatch.setattr(opus_bridge, "_run_claude", fake_raw)
    await opus_bridge.review_diff(
        "diff", "task desc", plan_text="PLAN: restore the deleted file"
    )
    assert "PLAN: restore the deleted file" in captured["prompt"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opus_bridge.py -k includes_plan -v`
Expected: FAIL with `TypeError: review_diff() got an unexpected keyword argument 'plan_text'`

- [ ] **Step 3: Update template + signature**

Replace `REVIEW_PROMPT_TEMPLATE` (`:51-67`) with a version that includes a plan section and a checkout note:

```python
REVIEW_PROMPT_TEMPLATE = """You are a senior code reviewer. Review this PR diff for a task.

Task description: {task_description}

Plan / spec the change must satisfy:
{plan_text}

A clean checkout of the PR head is your current working directory; you may
inspect files with your tools to verify the diff in context. If git is
unavailable, review from the diff text alone - do NOT pass solely because you
could not verify.

Diff:
{diff}

Respond with ONLY valid JSON in this exact format:
{{
  "verdict": "pass" or "fail",
  "feedback": "summary of your review",
  "issues": ["list of specific issues if verdict is fail"]
}}

Pass if the code correctly implements the task and has no critical issues.
Fail if there are bugs, missing functionality, security problems, or it deletes
existing functionality/config the task did not ask to remove.
"""
```

Add `plan_text: str | None = None` to `review_diff` (`:224`) and format it in (`:233`):

```python
        prompt = REVIEW_PROMPT_TEMPLATE.format(
            diff=diff,
            task_description=task_description,
            plan_text=(plan_text or "(no plan text was provided)"),
        )
```

- [ ] **Step 4: Pass plan_text from `review_task`**

In `src/orchestrator/core/orchestrator.py` `review_task`, fetch the plan task's `plan_text`/`plan_path` content (the same `slug_to_plan_task` lookup used at dispatch) and pass `plan_text=...` into `review_diff`. If only `plan_path` is known, read it from the checkout created in Task 7.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_opus_bridge.py -k includes_plan -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/core/opus_bridge.py src/orchestrator/core/orchestrator.py tests/test_opus_bridge.py
git commit -m "feat: include plan/spec in the review prompt"
```

---

### Task 9: Destructive-edit guardrail

**Files:**
- Create: `src/orchestrator/core/diff_guard.py`
- Modify: `src/orchestrator/core/orchestrator.py` (review_task, before merge)
- Test: `tests/test_diff_guard.py`

**Depends on:** Task 7

- [ ] **Step 1: Write the failing test**

```python
# tests/test_diff_guard.py
import pytest

from orchestrator.core.diff_guard import destructive_deletions


@pytest.mark.unit
def test_flags_large_deletion_from_existing_file():
    diff = "\n".join(
        ["--- a/.env.example", "+++ b/.env.example"]
        + [f"-LINE{i}" for i in range(70)]
        + ["+KEY=1"]
    )
    flagged = destructive_deletions(diff, threshold=40)
    assert ".env.example" in flagged


@pytest.mark.unit
def test_ignores_new_file_and_small_edits():
    diff = "\n".join(
        ["--- /dev/null", "+++ b/new.py"] + [f"+LINE{i}" for i in range(70)]
    )
    assert destructive_deletions(diff, threshold=40) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_diff_guard.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# src/orchestrator/core/diff_guard.py
"""Flag PRs that delete large chunks from existing files.

A weak worker can silently truncate a config/source file. The reviewer brain
should catch this, but this deterministic guard is a cheap hard backstop.
"""

from __future__ import annotations

import re


_OLD = re.compile(r"^--- a/(.+)$")
_NEW_NULL = re.compile(r"^\+\+\+ b/.+$")


def destructive_deletions(diff: str, threshold: int = 40) -> list[str]:
    """Return paths from which more than ``threshold`` lines were removed.

    Only counts files that existed before (``--- a/...``, not ``/dev/null``).
    """
    removals: dict[str, int] = {}
    current: str | None = None
    for line in diff.splitlines():
        m = _OLD.match(line)
        if m:
            current = m.group(1)
            removals.setdefault(current, 0)
            continue
        if line.startswith("--- /dev/null"):
            current = None
            continue
        if current and line.startswith("-") and not line.startswith("---"):
            removals[current] += 1
    return [path for path, n in removals.items() if n > threshold]
```

- [ ] **Step 4: Enforce in `review_task`**

In `src/orchestrator/core/orchestrator.py`, after computing `diff` and before/alongside the verdict handling, add:

```python
        from orchestrator.core.diff_guard import destructive_deletions

        flagged = destructive_deletions(diff)
        if flagged and verdict == "pass":
            verdict = "fail"
            feedback = (
                "Hard-blocked: large deletions from existing file(s) "
                f"{flagged} not justified by the task. " + feedback
            )
```

(Place this after `verdict`/`feedback` are assigned from the review, so it can override a brain "pass".)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_diff_guard.py tests/test_orchestrator.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/core/diff_guard.py src/orchestrator/core/orchestrator.py tests/test_diff_guard.py
git commit -m "feat: hard-block PRs with large unrequested deletions"
```

---

### Task 10: Distinct "zero commits" failure

**Files:**
- Modify: `src/orchestrator/core/orchestrator.py` (`reconcile_runs` / agent-done handling)
- Test: `tests/test_orchestrator.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_orchestrator.py  (append)
import pytest


@pytest.mark.unit
async def test_empty_diff_failure_has_clear_message(orchestrator):
    msg = orchestrator._classify_pr_failure(
        "GraphQL: No commits between main and agent/x (createPullRequest)"
    )
    assert "zero commits" in msg.lower()
    assert "worker" in msg.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_orchestrator.py -k empty_diff -v`
Expected: FAIL with `AttributeError: ... '_classify_pr_failure'`

- [ ] **Step 3: Add the classifier and use it**

In `src/orchestrator/core/orchestrator.py`:

```python
    @staticmethod
    def _classify_pr_failure(raw: str) -> str:
        """Turn an opaque gh/GraphQL PR-create error into an explained failure."""
        if "No commits between" in raw or "no commits" in raw.lower():
            return (
                "Worker produced zero commits: the agent made no changes "
                "(model likely too weak for this task, or the plan was unclear). "
                f"Original error: {raw.strip()}"
            )
        return raw.strip()
```

Wherever a PR-create / agent failure surfaces an error string into `fail_task(...)`, route it through `self._classify_pr_failure(...)` first.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_orchestrator.py -k empty_diff -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: explain zero-commit worker failures clearly"
```

---

### Task 11: Document `branch` semantics (continue-on-PR is out of scope for v1)

**Files:**
- Modify: `src/orchestrator/models/schemas.py` (`branch` field docstring)
- Modify: `CLAUDE.md` (MCP dispatch gotcha)
- Modify: `README.md` (MCP tool table `:184` + "Not in v1" note `:246`)

**Depends on:** None

> A true "amend an existing PR" mode (`target_branch`/`pr_number`) is a larger change to the one-task-plan + entrypoint branch-cut flow. For this plan we make the current behavior explicit and unsurprising; the continue-mode is a tracked follow-up. We also surface both standing limitations (branch-is-base, GitHub-only/no gitignored files) to users in the README.

- [ ] **Step 1: Document the field**

In `DispatchRequest`, add to the `branch` field a docstring:

```python
    branch: str | None = None
    """Base branch for the dispatched task. The worker always cuts a NEW
    agent/<slug> branch from this and opens a NEW PR; passing an existing PR's
    head here does NOT push follow-up commits onto that PR. Re-dispatching
    always creates a fresh PR. (Continue-on-PR mode is a planned follow-up.)"""
```

- [ ] **Step 2: Add a CLAUDE.md gotcha**

Under the MCP section of `CLAUDE.md`, add a bullet:

```markdown
- **`dispatch` `branch` is always a base, never a target** — Praxis cuts a new
  `agent/<slug>` branch off it and opens a new PR. There is no amend-existing-PR
  mode yet; re-dispatch = new PR. Follow-up: add `target_branch`/`pr_number`.
```

- [ ] **Step 3: Update the README MCP table**

In `README.md`, update the `dispatch_task` row (`:184`) to advertise the new `context?` param:

```markdown
| `dispatch_task(repo_url, instructions, model, harness?, branch?, context?)` | Dispatch one task; returns `{task_id, dashboard_url, status}`. `context` is curated, secret-scrubbed reference text for the worker. Praxis always runs its own review. |
```

- [ ] **Step 4: Add the limitations to the README "Not in v1" note**

In `README.md`, extend the **"Not in v1"** note (`:246`) with:

```markdown
> **Limitations (by design):**
> - **The worker reads only from GitHub.** Local and gitignored files (`.env`,
>   data dirs, secrets) are never mounted into the coding agent. Give it
>   reference context via `dispatch_task`'s `context` field instead - it is
>   secret-scrubbed and size-capped before reaching the container.
> - **`branch` is a base, not a target.** Praxis cuts a new `agent/<slug>`
>   branch and opens a new PR; it cannot push follow-up commits onto an existing
>   PR. Re-dispatching always creates a fresh PR. (Continue-on-PR mode is planned.)
```

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/models/schemas.py CLAUDE.md README.md
git commit -m "docs: clarify dispatch branch semantics and worker context limits"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1, Task 2, Task 6, Task 10, Task 11 (no dependencies)
- **Wave 2:** Task 3 (Task 1, 2), Task 7 (Task 6)
- **Wave 3:** Task 4 (Task 3), Task 8 (Task 7), Task 9 (Task 7)
- **Wave 4:** Task 5 (Task 4)

---

## Final Verification

After all tasks:

```bash
uv run ruff format src/ tests/ && uv run ruff check --fix src/ tests/
uv run mypy src/orchestrator/ --ignore-missing-imports
uv run pytest --cov=orchestrator --cov-report=term-missing -v
docker build -t aider-agent:latest -f docker/aider-agent/Dockerfile docker/aider-agent/
```

Expected: format clean, mypy clean, all tests pass (coverage ≥ 80%), image builds.

## Notes / Out of Scope

- **Aider stays GitHub-only.** No local or gitignored files are ever mounted; the
  "local knowledge" gap is filled by the curated `CONTEXT_TEXT` (prose, scrubbed),
  not by shipping `.env`/data files into the container.
- **Continue-on-PR dispatch mode** (`target_branch`/`pr_number`) — tracked follow-up (Task 11).
- **Caller-side curation** is the primary control; the scrubber + size cap is the safety net.
