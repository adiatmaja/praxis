# Worker-Quality Gates & Operational Visibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Praxis trustworthy to run less-supervised by adding a deterministic mechanical verification gate before the brain review, closing the spec-fidelity blind spot, surfacing stalled/failed plans instead of failing silently, and stamping the running build so a stale server is visible.

**Architecture:** Four independent themes, all additive. (A) A per-project `verify_cmd` is run against the cloned PR head inside `review_task` *before* the brain call — a non-zero exit hard-fails the task deterministically and cheaply (no brain tokens). (B) The capability-decomposition brain is asked to emit a verbatim `plan_text` contract per leaf and real `depends_on` edges, so the reviewer checks the diff against the original API contract, not the task blurb. (C) A `plan_stalled` / `plan_completed_with_failures` event is emitted so a wedged or partially-failed plan is never silent. (D) A build/commit stamp is exposed on `/health` and `/api/status`.

**Tech Stack:** Python 3.11, FastAPI, aiosqlite (raw SQL, no ORM), pytest + pytest-asyncio (`asyncio_mode = "auto"`), `asyncio.create_subprocess_shell` for the gate.

---

## Context for the implementer (read first)

You have zero prior context; here is everything you need.

**Why this plan exists.** A live end-to-end run (2026-07-01) dispatched a hard async-scheduler
plan to a local 27B model. Findings: (1) the reviewer approved a scaffold that had silently
**dropped `AbortSignal` from a `run(signal)` interface** because it reviewed the diff against
the task title, not the plan's API contract; (2) nothing ever compiled or ran the code, so
type/compile/test failures could only be caught by a language model reading a diff; (3) a
server running **stale code** (started before a feature merged) silently ran old behavior with
no version signal; (4) a plan **wedged on a terminally-failed task** with no surfaced error.

**Key existing facts (already true — do NOT re-implement):**
- `review_task` (`src/orchestrator/core/orchestrator.py:247-348`) already clones the PR head
  into a `tempfile.TemporaryDirectory()` (`checkout`), fetches the diff, and calls
  `self._opus.review_diff(diff, task_desc, ..., plan_text=plan_text_for_review, cwd=checkout)`.
  `review_diff` already accepts and uses `plan_text`. The gap is that `plan_text_for_review`
  is resolved from the opus_plan task's `plan_text` key (`orchestrator.py:289`), which the
  decomposition brain **never emits**, so it is always `None` for execute-plan runs.
- The decomposition prompt + parser live in `src/orchestrator/core/plan_review.py`. The prompt
  (`_PROMPT`, lines 22-51) shows `"depends_on": []` and has no `plan_text` field. The parser
  `parse_review_response` (lines 84-116) `setdefault`s missing fields.
- Adding a project column is an additive `ALTER TABLE ... ADD COLUMN` inside
  `Database.initialize()` (`src/orchestrator/database.py`), each wrapped in
  `contextlib.suppress(Exception)`. The `auto_merge` column added the exact same way is the
  pattern to copy (`projects` tuple in the additive-column loop, plus the `CREATE TABLE`).
- Project schema fields live in `src/orchestrator/models/schemas.py`: `ProjectCreate`
  (line 71), `ProjectUpdate`, `ProjectResponse`. `auto_merge: bool = False` is the field to
  mirror for a new `verify_cmd: str | None = None`.
- `api/projects.py` `create_project` INSERTs the column list explicitly and `update_project`
  builds a dynamic `SET`. Follow the `auto_merge` precedent already present there.
- `EventBus.publish` is `self._bus.publish({...})`; consumed by the SSE stream at `/api/events`.
- `/health` is in `src/orchestrator/main.py:196-200`. `/api/status` is in
  `src/orchestrator/api/system.py:159-208`.
- `process_plan_once` (`orchestrator.py:671-702`) is the per-plan loop pass; at line ~700 it
  calls `all_tasks_done(plan_id)` and marks the plan `COMPLETED`.
- Tests use in-memory SQLite fixtures in `tests/conftest.py` (`client`, `auth_headers`,
  `task_queue`, seeded project/plan/task). Match neighboring test names; do not invent DB plumbing.

**Conventions:** ruff format, line length 88, `X | Y` unions, Google-style docstrings,
`logging` not `print`, catch specific exceptions, `raise ... from`. Run tests with
`uv run pytest`. Format with `uv run ruff format src/ tests/` and `uv run ruff check --fix src/ tests/`.
Type-check with `uv run mypy src/orchestrator/ --ignore-missing-imports`.

**Security note (call out in docs):** `verify_cmd` runs an operator-configured shell command on
the orchestrator host. It is trusted config (set by the person running Praxis), never taken from
an untrusted PR. Recommend running the orchestrator itself containerized. This is documented in
Task 9, not left implicit.

---

## File Structure

- **Create** `src/orchestrator/core/verify_gate.py` — `run_verify(checkout_dir, verify_cmd, timeout)` pure subprocess runner returning `(passed, output)`. No orchestrator state.
- **Create** `src/orchestrator/core/build_info.py` — resolves the running commit SHA + process start time once at import.
- **Create** `tests/test_verify_gate.py`, `tests/test_build_info.py`.
- **Modify** `src/orchestrator/database.py` — add `projects.verify_cmd` column.
- **Modify** `src/orchestrator/models/schemas.py` — `verify_cmd` on ProjectCreate/Update/Response.
- **Modify** `src/orchestrator/api/projects.py` — persist `verify_cmd` on create + update.
- **Modify** `src/orchestrator/core/plan_review.py` — prompt emits `plan_text` + real `depends_on`; parser defaults `plan_text`.
- **Modify** `src/orchestrator/core/orchestrator.py` — run verify gate before brain review; emit stall/partial-failure event.
- **Modify** `src/orchestrator/main.py` + `src/orchestrator/api/system.py` — build stamp on `/health` and `/api/status`.
- **Modify** `.env.example`, `docs/deployment.md`, `CLAUDE.md` — `verify_cmd` + build stamp + security note.

---

### Task 1: Build/commit stamp module

**Files:**
- Create: `src/orchestrator/core/build_info.py`
- Test: `tests/test_build_info.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_build_info.py
"""Tests for the build-info stamp."""

from __future__ import annotations

from orchestrator.core import build_info


def test_build_stamp_has_expected_keys() -> None:
    stamp = build_info.build_stamp()
    assert set(stamp) == {"commit", "started_at"}
    assert isinstance(stamp["commit"], str) and stamp["commit"]
    assert isinstance(stamp["started_at"], str) and stamp["started_at"]


def test_env_override_wins(monkeypatch) -> None:
    monkeypatch.setenv("PRAXIS_BUILD_SHA", "deadbeef")
    assert build_info._resolve_commit() == "deadbeef"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_build_info.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.core.build_info'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/orchestrator/core/build_info.py
"""Expose the running build's commit SHA and process start time.

Lets an operator tell at a glance whether the live server is running current
code. A server started before a feature merged silently runs stale behavior;
stamping the commit on /health and /api/status makes that visible.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime


def _resolve_commit() -> str:
    """Return the build commit: env override, else `git rev-parse`, else 'unknown'."""
    env = os.environ.get("PRAXIS_BUILD_SHA")
    if env:
        return env
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


_STARTED_AT = datetime.now(UTC).isoformat()
_COMMIT = _resolve_commit()


def build_stamp() -> dict[str, str]:
    """Return `{"commit": <sha>, "started_at": <iso8601>}` for the running process."""
    return {"commit": _COMMIT, "started_at": _STARTED_AT}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_build_info.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/build_info.py tests/test_build_info.py
git commit -m "feat: add build-info stamp (commit sha + start time)"
```

---

### Task 2: Expose build stamp on /health and /api/status

**Files:**
- Modify: `src/orchestrator/main.py:196-200`
- Modify: `src/orchestrator/api/system.py:194-208` (the `system_status` return dict)
- Test: `tests/test_api_system.py` (create if absent), `tests/test_main.py` if health is tested there

**Depends on:** Task 1

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_system.py  (add; reuse existing client/auth_headers fixtures)
def test_health_includes_build(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "build" in body
    assert body["build"]["commit"]
    assert body["build"]["started_at"]


def test_status_includes_build(client, auth_headers):
    resp = client.get("/api/status", headers=auth_headers)
    assert resp.status_code == 200
    assert "build" in resp.json()
```

> If `/api/status` tests in this repo deadlock on the unmocked claude-CLI probe
> (a known pre-existing issue), mock `orchestrator.api.system._probe_claude_cli`
> and `_probe_provider` to return quickly in this test, mirroring any existing
> status test that already does so.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_system.py -k build -v`
Expected: FAIL — `KeyError: 'build'` / assertion error (no `build` key).

- [ ] **Step 3: Implementation**

In `src/orchestrator/main.py`, add the import near the other `orchestrator.*` imports and update `health`:

```python
from orchestrator.core.build_info import build_stamp  # noqa: E402


@app.get("/health")
async def health() -> dict[str, object]:
    """Healthcheck endpoint (includes the running build stamp)."""

    return {"status": "ok", "build": build_stamp()}
```

In `src/orchestrator/api/system.py`, add the import at the top:

```python
from orchestrator.core.build_info import build_stamp
```

and add one key to the `system_status` return dict (after `"providers": providers,`):

```python
        "providers": providers,
        "build": build_stamp(),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_api_system.py -k build -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/main.py src/orchestrator/api/system.py tests/test_api_system.py
git commit -m "feat: surface build stamp on /health and /api/status"
```

---

### Task 3: Verify-gate subprocess runner

**Files:**
- Create: `src/orchestrator/core/verify_gate.py`
- Test: `tests/test_verify_gate.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_verify_gate.py
"""Tests for the deterministic mechanical verification gate."""

from __future__ import annotations

import sys

import pytest

from orchestrator.core.verify_gate import run_verify


@pytest.mark.asyncio
async def test_run_verify_passes_on_zero_exit(tmp_path) -> None:
    cmd = f'"{sys.executable}" -c "print(\'ok\')"'
    passed, output = await run_verify(str(tmp_path), cmd)
    assert passed is True
    assert "ok" in output


@pytest.mark.asyncio
async def test_run_verify_fails_on_nonzero_exit(tmp_path) -> None:
    cmd = f'"{sys.executable}" -c "import sys; print(\'boom\'); sys.exit(1)"'
    passed, output = await run_verify(str(tmp_path), cmd)
    assert passed is False
    assert "boom" in output


@pytest.mark.asyncio
async def test_run_verify_times_out(tmp_path) -> None:
    cmd = f'"{sys.executable}" -c "import time; time.sleep(5)"'
    passed, output = await run_verify(str(tmp_path), cmd, timeout=0.5)
    assert passed is False
    assert "timed out" in output.lower()


@pytest.mark.asyncio
async def test_run_verify_truncates_long_output(tmp_path) -> None:
    cmd = f'"{sys.executable}" -c "print(\'x\' * 20000)"'
    passed, output = await run_verify(str(tmp_path), cmd)
    assert passed is True
    assert len(output) < 12000
    assert "truncated" in output.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_verify_gate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.core.verify_gate'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/orchestrator/core/verify_gate.py
"""Run an operator-configured verification command against a PR checkout.

This is the deterministic gate that runs BEFORE the brain review: a non-zero
exit (failed typecheck / tests / lint) fails the task cheaply and reliably,
catching the compile/test failure class that a language model reviewing a diff
misses. The command is trusted operator config, never taken from a PR.
"""

from __future__ import annotations

import asyncio

_MAX_OUTPUT = 8000


def _truncate(text: str) -> str:
    if len(text) <= _MAX_OUTPUT:
        return text
    half = _MAX_OUTPUT // 2
    return f"{text[:half]}\n...[truncated]...\n{text[-half:]}"


async def run_verify(
    checkout_dir: str, verify_cmd: str, timeout: float = 600.0
) -> tuple[bool, str]:
    """Run ``verify_cmd`` in ``checkout_dir``; return (passed, combined_output).

    Args:
        checkout_dir: Working directory (a checked-out PR head).
        verify_cmd: Shell command string (e.g. ``npx tsc --noEmit && npm test``).
        timeout: Seconds before the command is killed and reported as failed.

    Returns:
        (True, output) on exit 0; (False, output) on non-zero exit or timeout.
    """
    proc = await asyncio.create_subprocess_shell(
        verify_cmd,
        cwd=checkout_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return False, f"verify command timed out after {timeout:.0f}s"
    text = _truncate(out.decode(errors="replace"))
    return proc.returncode == 0, text
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_verify_gate.py -v`
Expected: PASS (pass / fail / timeout / truncate all green)

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/verify_gate.py tests/test_verify_gate.py
git commit -m "feat: add deterministic verify-gate subprocess runner"
```

---

### Task 4: Add projects.verify_cmd column

**Files:**
- Modify: `src/orchestrator/database.py` (additive-column loop + `CREATE TABLE ... projects`)
- Test: `tests/test_database.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_database.py  (add to existing file)
import pytest

from orchestrator.database import Database


@pytest.mark.asyncio
async def test_verify_cmd_column_exists(tmp_path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'v.db'}")
    await db.initialize()
    try:
        cols = {row["name"] for row in await db.fetch_all("PRAGMA table_info(projects)")}
        assert "verify_cmd" in cols
    finally:
        await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_database.py::test_verify_cmd_column_exists -v`
Expected: FAIL on `assert "verify_cmd" in cols`

- [ ] **Step 3: Implementation**

In `src/orchestrator/database.py`, add `"verify_cmd TEXT"` to the `projects` tuple in the
additive-column loop (the same tuple that already lists `"auto_merge INTEGER NOT NULL DEFAULT 0"`):

```python
            (
                "projects",
                (
                    "agent_model TEXT",
                    "agent_model_effort TEXT",
                    "harness TEXT NOT NULL DEFAULT 'opencode'",
                    "auto_merge INTEGER NOT NULL DEFAULT 0",
                    "verify_cmd TEXT",
                ),
            ),
```

And add `verify_cmd TEXT` to the `CREATE TABLE IF NOT EXISTS projects` statement (after the
`auto_merge INTEGER NOT NULL DEFAULT 0,` line):

```python
        auto_merge INTEGER NOT NULL DEFAULT 0,
        verify_cmd TEXT,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_database.py::test_verify_cmd_column_exists -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/database.py tests/test_database.py
git commit -m "feat: add projects.verify_cmd column"
```

---

### Task 5: verify_cmd on project schemas + API

**Files:**
- Modify: `src/orchestrator/models/schemas.py` (ProjectCreate ~line 71, ProjectUpdate, ProjectResponse)
- Modify: `src/orchestrator/api/projects.py` (create INSERT + update SET)
- Test: `tests/test_api_projects.py`

**Depends on:** Task 4

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_projects.py  (add)
def test_create_project_verify_cmd_defaults_none(client, auth_headers):
    resp = client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "vcrepo",
            "repo_url": "https://github.com/user/vcrepo",
            "model_name": "qwen3-32b",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["verify_cmd"] is None


def test_update_project_verify_cmd(client, auth_headers, seeded_project_id):
    resp = client.patch(
        f"/api/projects/{seeded_project_id}",
        headers=auth_headers,
        json={"verify_cmd": "npx tsc --noEmit && npm test"},
    )
    assert resp.status_code == 200
    assert resp.json()["verify_cmd"] == "npx tsc --noEmit && npm test"
```

> Use whatever seeded-project fixture the other tests in this file use; match its name.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_projects.py -k verify_cmd -v`
Expected: FAIL — `verify_cmd` missing from response / 422 on PATCH.

- [ ] **Step 3: Implementation**

In `schemas.py`, add to `ProjectCreate` (after `auto_merge: bool = False`):

```python
    verify_cmd: str | None = None
```

Add to `ProjectUpdate` (after its `auto_merge: bool | None = None`):

```python
    verify_cmd: str | None = None
```

Add to `ProjectResponse` (after its `auto_merge: bool = False`):

```python
    verify_cmd: str | None = None
```

In `api/projects.py` `create_project`, add `verify_cmd` to the INSERT column list and bind
`body.verify_cmd` in the values tuple (mirror exactly how `auto_merge`/`body.auto_merge` is
placed — add the column name in the SQL and the value in the same position of the tuple).

For `update_project`: if it iterates `body.model_dump(exclude_unset=True)`, no change is
needed beyond the schema field. If it has an explicit allow-list of updatable columns, add
`"verify_cmd"` to it. Read the function and follow its existing pattern (the `auto_merge`
field is already handled there — copy that).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_api_projects.py -k verify_cmd -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/models/schemas.py src/orchestrator/api/projects.py tests/test_api_projects.py
git commit -m "feat: expose verify_cmd on project create/update/response"
```

---

### Task 6: Run the verify gate before brain review

**Files:**
- Modify: `src/orchestrator/core/orchestrator.py:291-312` (the `with tempfile...` review block)
- Test: `tests/test_orchestrator.py`

**Depends on:** Task 3, Task 5

- [ ] **Step 1: Write the failing test**

```python
# tests/test_orchestrator.py  (add; reuse the existing review-task build helpers)
import pytest

from orchestrator.models.schemas import TaskStatus


@pytest.mark.asyncio
async def test_verify_gate_failure_fails_without_brain(orch_passing_review, monkeypatch):
    """A non-zero verify_cmd fails the task and never calls the brain."""
    orch, mocks = orch_passing_review(auto_merge=0, base_branch="plan/x")
    mocks.project["verify_cmd"] = "whatever"

    async def fake_run_verify(checkout_dir, verify_cmd, timeout=600.0):
        return False, "tsc: error TS2554: Expected 1 arguments, but got 0."

    monkeypatch.setattr(
        "orchestrator.core.orchestrator.run_verify", fake_run_verify
    )
    await orch.review_task(mocks.task_id, mocks.project)

    mocks.opus.review_diff.assert_not_called()
    mocks.git.merge_pr.assert_not_called()
    task = await orch._tq.get_task(mocks.task_id)
    assert task["status"] in (TaskStatus.FAILED, TaskStatus.PENDING)
    assert "TS2554" in (task["review_feedback"] or "")


@pytest.mark.asyncio
async def test_verify_gate_pass_proceeds_to_brain(orch_passing_review, monkeypatch):
    orch, mocks = orch_passing_review(auto_merge=0, base_branch="plan/x")
    mocks.project["verify_cmd"] = "whatever"

    async def fake_run_verify(checkout_dir, verify_cmd, timeout=600.0):
        return True, "all good"

    monkeypatch.setattr(
        "orchestrator.core.orchestrator.run_verify", fake_run_verify
    )
    await orch.review_task(mocks.task_id, mocks.project)
    mocks.opus.review_diff.assert_called_once()
```

> Reuse the `orch_passing_review` fixture from the merge-gate plan's Task 5 (seeds a plan
> with `plan_branch_name`, a task in `REVIEWING` with a `pr_url`; stubs git clone/diff/merge
> and `opus.review_diff`→pass; captures `bus.publish`). Ensure `mocks.project` is a mutable
> dict so the test can set `verify_cmd`. If `orch_passing_review` does not stub
> `git.clone_pr_head` to yield a checkout dir, stub it so `checkout` is not None.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_orchestrator.py -k verify_gate -v`
Expected: FAIL — `run_verify` not imported / brain still called.

- [ ] **Step 3: Implementation**

In `orchestrator.py`, add the import near the other `core` imports at the top:

```python
from orchestrator.core.verify_gate import run_verify
```

Inside `review_task`, in the `with tempfile.TemporaryDirectory() as _checkout_dir:` block,
replace the section that computes `diff` and `review` (currently lines ~302-310) with a
version that runs the gate first and short-circuits:

```python
            verify_cmd = project.get("verify_cmd")
            review: dict[str, Any] | None = None
            if verify_cmd and checkout is not None:
                passed, gate_output = await run_verify(checkout, verify_cmd)
                if not passed:
                    review = {
                        "verdict": "fail",
                        "feedback": (
                            "Automated verification failed before review "
                            f"(`{verify_cmd}`):\n\n{gate_output}"
                        ),
                    }

            diff = await self._git.get_pr_diff(".", pr_number, repo=repo)
            if review is None:
                review = await self._opus.review_diff(
                    diff,
                    task["description"] or task["title"],
                    model=project.get("agent_model"),
                    effort=project.get("agent_model_effort"),
                    plan_text=plan_text_for_review,
                    cwd=checkout,
                )
```

The existing lines after the block (`verdict = str(review["verdict"]).lower()` etc.) are
unchanged — `review` is now always a dict by the time they run. `Any` is already imported.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_orchestrator.py -k verify_gate -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: run deterministic verify gate before brain review"
```

---

### Task 7: Decomposition emits per-leaf contract (plan_text) + real dependencies

**Files:**
- Modify: `src/orchestrator/core/plan_review.py` (`_PROMPT` lines 22-51, `parse_review_response` lines 84-116)
- Test: `tests/test_plan_review.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_plan_review.py  (add; reuse the file's profile fixture if present)
from orchestrator.core.plan_review import build_review_prompt, parse_review_response
from orchestrator.models.schemas import CapabilityProfile


def _profile() -> CapabilityProfile:
    return CapabilityProfile(
        model_name="qwen3.6-27b", parameter_count_b=27.0, context_window=32768
    )


def test_prompt_requests_plan_text_and_dependencies():
    prompt = build_review_prompt("PLAN BODY", _profile(), "no history", 12000)
    assert "plan_text" in prompt
    # The prompt must instruct real dependency edges, not an empty example only.
    assert "depends_on" in prompt
    assert "verbatim" in prompt.lower() or "exact" in prompt.lower()


def test_parser_defaults_plan_text_to_description():
    raw = '{"tasks": [{"id": "t1", "title": "A", "description": "do A"}]}'
    plan = parse_review_response(raw)
    assert plan["tasks"][0]["plan_text"] == "do A"


def test_parser_preserves_supplied_plan_text():
    raw = (
        '{"tasks": [{"id": "t1", "title": "A", "description": "do A", '
        '"plan_text": "run(signal: AbortSignal): Promise<T>"}]}'
    )
    plan = parse_review_response(raw)
    assert "AbortSignal" in plan["tasks"][0]["plan_text"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_plan_review.py -k "plan_text or dependencies" -v`
Expected: FAIL — prompt lacks `plan_text`/verbatim instruction; parser has no `plan_text` key.

- [ ] **Step 3: Implementation**

In `plan_review.py`, replace the instruction paragraph and the JSON schema block of `_PROMPT`
(the text from "Split the plan..." through the closing `}}`) with:

```python
Split the plan into the SMALLEST leaf tasks this local model can each complete
on its own. For every leaf provide an ordered checklist of concrete steps.

For every leaf you MUST also include "plan_text": the VERBATIM excerpt of the
plan that defines this leaf's contract — exact function/type signatures, API
shapes, and named requirements. Do not paraphrase; copy the relevant lines so a
reviewer can check the implementation against the original contract, not a summary.

Set "depends_on" to the ids of any leaves whose output this leaf builds on (e.g.
a leaf that edits a file another leaf creates, or tests that need an
implementation). Only truly independent leaves get an empty list.

If a leaf cannot be split small enough for this model (too complex for its
parameter count, or irreducibly large), set "needs_stronger_model": true.

Respond with ONLY valid JSON:
{{
  "tasks": [
    {{"id": "t1", "title": "...", "description": "...", "plan_text": "...",
      "depends_on": [], "checklist": [{{"text": "..."}}],
      "needs_stronger_model": false}}
  ]
}}
```

In `parse_review_response`, add a `plan_text` default inside the per-task loop (next to the
other `setdefault`s):

```python
        t.setdefault("description", t["title"])
        t.setdefault("plan_text", t["description"])
        t.setdefault("depends_on", [])
        t.setdefault("checklist", [{"text": t["title"]}])
        t.setdefault("needs_stronger_model", False)
```

Note: `review_task` already reads `plan_task.get("plan_text")` from the stored `opus_plan`
JSON and passes it to `review_diff`, so no orchestrator change is needed — populating the
field here is what makes the reviewer see the contract.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_plan_review.py -k "plan_text or dependencies" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/plan_review.py tests/test_plan_review.py
git commit -m "feat: decomposition emits per-leaf contract (plan_text) + real deps"
```

---

### Task 8: Surface stalled / partially-failed plans

**Files:**
- Modify: `src/orchestrator/core/orchestrator.py:696-702` (end of `process_plan_once`)
- Test: `tests/test_orchestrator.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_orchestrator.py  (add)
import pytest

from orchestrator.models.schemas import TaskStatus


@pytest.mark.asyncio
async def test_stalled_plan_emits_event(orch_with_stalled_plan):
    """A plan with pending tasks but nothing running/dispatchable emits plan_stalled."""
    orch, mocks = orch_with_stalled_plan()  # 1 failed task, 1 pending, none running
    await orch.process_plan_once(mocks.plan_id, mocks.project)
    assert any(e["type"] == "plan_stalled" for e in mocks.published)
```

> Build `orch_with_stalled_plan` from the existing orchestrator test helpers: seed an ACTIVE
> plan with `opus_plan` set (so it is not re-planned), one task `FAILED` (terminal, attempts
> exhausted) and one task `PENDING` whose `depends_on` includes the failed task's slug so it
> is not dispatchable. Stub `dispatch_pending_tasks` to a no-op and `all_tasks_done`→False.
> Capture `bus.publish` into `mocks.published`. Confirm no `aider`/docker calls are made.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_orchestrator.py -k stalled_plan -v`
Expected: FAIL — no `plan_stalled` event is emitted.

- [ ] **Step 3: Implementation**

At the end of `process_plan_once`, after the `dispatch_pending_tasks` + review loop and the
`all_tasks_done` check, add a stall detector. Replace the tail of the method (from
`if await self._tq.all_tasks_done(plan_id):` onward) with:

```python
        tasks = await self._tq.get_tasks_for_plan(plan_id)
        if await self._tq.all_tasks_done(plan_id):
            await self._tq.update_plan_status(plan_id, PlanStatus.COMPLETED)
            failed = [t["id"] for t in tasks if t["status"] == TaskStatus.FAILED]
            if failed:
                self._bus.publish(
                    {
                        "type": "plan_completed_with_failures",
                        "plan_id": plan_id,
                        "failed_task_ids": failed,
                    }
                )
            try:
                await self._maybe_sync_context(plan_id, project)
            except Exception:  # noqa: BLE001 - context sync is best-effort
                logger.exception("context sync after plan completion failed")
            return

        # Not done, but nothing is running and nothing new was dispatched: a plan
        # wedged behind a terminally-failed dependency. Surface it, don't stay silent.
        active = [
            t
            for t in tasks
            if t["status"] in (TaskStatus.IN_PROGRESS, TaskStatus.REVIEWING)
        ]
        pending = [t for t in tasks if t["status"] == TaskStatus.PENDING]
        blocked_by_failure = [t["id"] for t in tasks if t["status"] == TaskStatus.FAILED]
        if not active and pending and blocked_by_failure:
            self._bus.publish(
                {
                    "type": "plan_stalled",
                    "plan_id": plan_id,
                    "pending_task_ids": [t["id"] for t in pending],
                    "failed_task_ids": blocked_by_failure,
                }
            )
```

> Preserve whatever the method currently does inside the `all_tasks_done` branch (context
> sync, logging). The lines above show the shape; keep any existing post-completion calls that
> are already there (e.g. `_maybe_sync_context` if that is the real method name — match the
> existing code, do not invent a name).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_orchestrator.py -k stalled_plan -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: emit plan_stalled / plan_completed_with_failures events"
```

---

### Task 9: Docs — verify_cmd, build stamp, security note

**Files:**
- Modify: `.env.example`, `docs/deployment.md`, `CLAUDE.md`

**Depends on:** Task 2, Task 5, Task 6

- [ ] **Step 1: `.env.example`**

Add near the top-level config comments:

```bash
# Optional: stamp a build id on /health and /api/status (else derived from git).
# PRAXIS_BUILD_SHA=

# Per-project `verify_cmd` (set via the API/dashboard, not here) runs an
# operator-configured shell command against each PR checkout BEFORE the brain
# review. A non-zero exit fails the task deterministically. It is TRUSTED config
# — never taken from a PR. Prefer running the orchestrator itself in a container.
```

- [ ] **Step 2: `docs/deployment.md`**

Add a "Verification gate & build visibility" section documenting: (a) `verify_cmd` per
project (example `npx tsc --noEmit && npm test`), that it runs before the brain review and
hard-fails on non-zero, and the trust/security note; (b) the `build` object on `/health` and
`/api/status` and how to use it to confirm the live server runs current code (restart after deploy).

- [ ] **Step 3: `CLAUDE.md`** — add two Gotchas:

```markdown
- **Mechanical verify gate runs before the brain** — if a project sets
  `verify_cmd`, `review_task` runs it against the cloned PR head first
  (`core/verify_gate.py`); a non-zero exit fails the task with the command
  output as feedback and never spends brain tokens. Trusted operator config,
  not taken from the PR. Harness-agnostic (runs orchestrator-side, not in the
  agent container).
- **Build stamp on /health + /api/status** — `core/build_info.py` exposes the
  running commit + start time so a stale server (started before a feature merged)
  is visible. Restart after deploy; `PRAXIS_BUILD_SHA` overrides the git-derived sha.
- **Decomposition emits per-leaf `plan_text`** — the capability-review brain now
  copies the verbatim contract (signatures/API) into each leaf's `plan_text`, which
  `review_task` feeds to `review_diff`; without it the reviewer checked diffs against
  the task blurb and missed spec drift (e.g. a dropped `AbortSignal` param).
```

- [ ] **Step 4: Commit**

```bash
git add .env.example docs/deployment.md CLAUDE.md
git commit -m "docs: document verify_cmd gate, build stamp, and security note"
```

---

### Task 10: Full-suite verification

**Files:** none (verification only)

**Depends on:** Task 1-9

- [ ] **Step 1: Full suite with coverage**

Run: `uv run pytest --cov=orchestrator --cov-report=term-missing -v`
Expected: all green, coverage not below the current ~88% baseline.

- [ ] **Step 2: Lint, format, type-check**

```bash
uv run ruff format src/ tests/
uv run ruff check --fix src/ tests/
uv run mypy src/orchestrator/ --ignore-missing-imports
```
Expected: no errors.

- [ ] **Step 3: Commit any fixups**

```bash
git add -A
git commit -m "chore: format and lint worker-quality-gate changes" || echo "nothing to commit"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1 (build_info), Task 3 (verify_gate), Task 4 (DB column), Task 7 (decomposition prompt), Task 8 (stall events) — no dependencies, run in parallel.
- **Wave 2:** Task 2 (build stamp endpoints, needs Task 1), Task 5 (verify_cmd schema/API, needs Task 4) — parallel.
- **Wave 3:** Task 6 (wire verify gate into review, needs Task 3 + Task 5).
- **Wave 4:** Task 9 (docs, needs Task 2 + Task 5 + Task 6).
- **Wave 5:** Task 10 (full-suite verification, needs all).

---

## Notes

- **The verify gate is the single biggest trust win** and is deliberately orchestrator-side
  (in `review_task`), so it works identically for every harness (opencode default, aider,
  openhands) and needs no entrypoint changes.
- **plan_text closes the fidelity blind spot with a one-field change** because `review_task`
  already threads `plan_text` into `review_diff`; the field was simply never populated by the
  capability-decomposition brain.
- **Dashboard wiring for the new SSE events (`plan_stalled`,
  `plan_completed_with_failures`) is out of scope** — the events are the contract; a banner in
  `web/index.html` can follow as a separate UI task.
- **Reuse existing fixtures** (`orch_passing_review`, seeded project/plan/task, `client`,
  `auth_headers`). Do not invent new DB plumbing; match neighboring test names.
