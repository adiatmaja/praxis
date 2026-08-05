# Worker Session Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a blocked worker is answered and re-dispatched, resume its harness-native conversation and its checkpointed working tree instead of starting cold.

**Architecture:** The worker mints a session id on its first run and reports it on the existing `/api/internal/agent-done` callback, but only after successfully pushing a work-in-progress checkpoint to its branch. The orchestrator stores `(worker_session_id, worker_session_harness)` on the task and replays the id via `WORKER_SESSION_ID` on a clarification re-dispatch only. That env var also drives branch reuse, so restored memory always matches the restored tree. Every failure path degrades to today's cold start.

**Tech Stack:** Python 3.11, FastAPI, aiosqlite (raw SQL), Docker SDK, bash entrypoints, pytest.

**Spec:** `docs/superpowers/specs/2026-08-05-worker-session-resume-design.md`

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `src/orchestrator/database.py` | Migration 6 adding the two task columns | Modify |
| `src/orchestrator/config.py` | `opencode_sessions_volume` setting | Modify |
| `src/orchestrator/api/internal.py` | Accept + persist `session_id` on the callback | Modify |
| `src/orchestrator/core/task_queue.py` | `record_worker_session` / `clear_worker_session` | Modify |
| `src/orchestrator/core/agent_manager.py` | `WORKER_SESSION_ID` env + OpenCode volume mount | Modify |
| `src/orchestrator/core/orchestrator_dispatch.py` | Replay gate: decide whether to pass the id | Modify |
| `docker/opencode-agent/extract_session.py` | Parse `opencode session list --format json` | Create |
| `docker/agy-agent/extract_session.py` | Parse the agy `--output-format json` envelope | Create |
| `docker/opencode-agent/entrypoint.sh` | Capture, checkpoint, replay, branch reuse | Modify |
| `docker/agy-agent/entrypoint.sh` | Capture, checkpoint, replay, branch reuse | Modify |
| `docker-compose.yml` | `OPENCODE_SESSIONS_VOLUME` env passthrough | Modify |
| `tests/test_worker_session_resume.py` | Extractors, gate matrix, persistence, migration | Create |

The two extractors are separate files per harness rather than one shared module because the images are built from separate contexts (`docker.yml` builds each agent dir independently) and cannot import from a common parent.

---

### Task 1: Schema migration for the worker session handle

**Files:**
- Modify: `src/orchestrator/database.py:181-234`
- Test: `tests/test_worker_session_resume.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

Create `tests/test_worker_session_resume.py`:

```python
"""Tests for worker session resume (spec 2026-08-05)."""

from __future__ import annotations

import pytest

from orchestrator.database import CURRENT_SCHEMA_VERSION, Database


@pytest.mark.asyncio
async def test_migration_adds_worker_session_columns(tmp_path):
    """Migration 6 adds worker_session_id and worker_session_harness to tasks."""
    db = Database(f"sqlite:///{tmp_path / 'test.db'}")
    await db.initialize()
    connection = await db.connect()
    cursor = await connection.execute("PRAGMA table_info(tasks)")
    cols = {row[1] for row in await cursor.fetchall()}
    assert "worker_session_id" in cols
    assert "worker_session_harness" in cols
    await db.close()


@pytest.mark.asyncio
async def test_migration_is_idempotent(tmp_path):
    """Re-running initialize on an existing DB does not error."""
    url = f"sqlite:///{tmp_path / 'test.db'}"
    db = Database(url)
    await db.initialize()
    await db.close()

    again = Database(url)
    await again.initialize()
    connection = await again.connect()
    cursor = await connection.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    assert int(row[0]) == CURRENT_SCHEMA_VERSION
    await again.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_worker_session_resume.py -v`
Expected: FAIL, `assert "worker_session_id" in cols` raises AssertionError.

- [ ] **Step 3: Write minimal implementation**

Add after `_migration_0005_task_outcomes` in `src/orchestrator/database.py`:

```python
async def _migration_0006_worker_session(connection: aiosqlite.Connection) -> None:
    """Add tasks.worker_session_id / worker_session_harness for session resume.

    The harness is stored alongside the id because a project's harness can
    change between dispatches, and an agy conversation id is meaningless to
    OpenCode. Replay checks both.
    """
    cursor = await connection.execute("PRAGMA table_info(tasks)")
    cols = {row[1] for row in await cursor.fetchall()}
    if "worker_session_id" not in cols:
        await connection.execute("ALTER TABLE tasks ADD COLUMN worker_session_id TEXT")
    if "worker_session_harness" not in cols:
        await connection.execute(
            "ALTER TABLE tasks ADD COLUMN worker_session_harness TEXT"
        )
```

Append to the `MIGRATIONS` list:

```python
    Migration(
        6,
        "add tasks.worker_session_id/worker_session_harness for session resume",
        _migration_0006_worker_session,
    ),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_worker_session_resume.py -v`
Expected: PASS, 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/database.py tests/test_worker_session_resume.py
git commit -m "feat(db): migration 6 adds worker session handle columns to tasks"
```

---

### Task 2: OpenCode session-id extractor

**Files:**
- Create: `docker/opencode-agent/extract_session.py`
- Test: `tests/test_worker_session_resume.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

Append to `tests/test_worker_session_resume.py`:

```python
import json
import subprocess
import sys
from pathlib import Path


OPENCODE_EXTRACTOR = (
    Path(__file__).parent.parent / "docker" / "opencode-agent" / "extract_session.py"
)


def _run_extractor(script: Path, stdin_text: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script)],
        input=stdin_text,
        capture_output=True,
        text=True,
        check=False,
    )


def test_opencode_extractor_returns_single_session_id():
    """A fresh container has exactly one session; print its id."""
    payload = json.dumps([{"id": "ses_abc123", "title": "task"}])
    result = _run_extractor(OPENCODE_EXTRACTOR, payload)
    assert result.returncode == 0
    assert result.stdout.strip() == "ses_abc123"


def test_opencode_extractor_picks_newest_when_multiple():
    """A reused volume may hold several; the newest by `time.created` wins."""
    payload = json.dumps(
        [
            {"id": "ses_old", "time": {"created": 100}},
            {"id": "ses_new", "time": {"created": 200}},
        ]
    )
    result = _run_extractor(OPENCODE_EXTRACTOR, payload)
    assert result.returncode == 0
    assert result.stdout.strip() == "ses_new"


def test_opencode_extractor_is_silent_on_malformed_input():
    """Garbage must exit non-zero with empty stdout, never crash the entrypoint."""
    result = _run_extractor(OPENCODE_EXTRACTOR, "not json at all")
    assert result.returncode != 0
    assert result.stdout.strip() == ""


def test_opencode_extractor_is_silent_on_empty_list():
    """No sessions is a normal outcome, not an error to surface."""
    result = _run_extractor(OPENCODE_EXTRACTOR, "[]")
    assert result.returncode != 0
    assert result.stdout.strip() == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_worker_session_resume.py -k opencode_extractor -v`
Expected: FAIL, all four error because `extract_session.py` does not exist (non-zero exit, but `test_opencode_extractor_returns_single_session_id` fails its stdout assertion).

- [ ] **Step 3: Write minimal implementation**

Create `docker/opencode-agent/extract_session.py`:

```python
#!/usr/bin/env python3
"""Print the OpenCode session id from `opencode session list --format json`.

Reads JSON on stdin, prints one session id on stdout, exits 0. On any problem
(malformed JSON, no sessions, unexpected shape) prints nothing and exits 1, so
the entrypoint can treat session capture as best-effort and carry on.

A fresh container holds exactly one session. The newest-by-created ordering
only matters when the sessions volume is reused across tasks.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 1

    if isinstance(data, dict):
        data = data.get("sessions", [])
    if not isinstance(data, list) or not data:
        return 1

    def created(entry: object) -> float:
        if not isinstance(entry, dict):
            return -1.0
        time_block = entry.get("time")
        if isinstance(time_block, dict):
            try:
                return float(time_block.get("created", -1))
            except (TypeError, ValueError):
                return -1.0
        return -1.0

    newest = max(data, key=created)
    if not isinstance(newest, dict):
        return 1
    session_id = newest.get("id")
    if not isinstance(session_id, str) or not session_id:
        return 1

    print(session_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_worker_session_resume.py -k opencode_extractor -v`
Expected: PASS, 4 passed.

- [ ] **Step 5: Commit**

```bash
git add docker/opencode-agent/extract_session.py tests/test_worker_session_resume.py
git commit -m "feat(opencode-agent): add session id extractor"
```

---

### Task 3: agy conversation-id extractor

**Files:**
- Create: `docker/agy-agent/extract_session.py`
- Test: `tests/test_worker_session_resume.py`

**Depends on:** None

**IMPORTANT — verify before implementing.** The spec flags the agy JSON envelope field name for the response body as unverified. Run this once against the real image and record the actual keys:

```bash
docker run --rm -v praxis-gemini-creds:/home/agent/.gemini agy-agent:latest \
  agy --dangerously-skip-permissions --mode accept-edits \
      --output-format json --model "Gemini 3.6 Flash (High)" -p "say hi" | head -40
```

If the response body key is not one of `response` / `text` / `output`, add the real key to the `_RESPONSE_KEYS` tuple below and to the test fixture. If `--output-format json` produces no capturable stdout at all (the known non-TTY bug, upstream issue #76), STOP and report: Task 9 must then fall back to text mode with no session capture for agy, and this task is skipped.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_worker_session_resume.py`:

```python
AGY_EXTRACTOR = (
    Path(__file__).parent.parent / "docker" / "agy-agent" / "extract_session.py"
)


def test_agy_extractor_emits_conversation_id_and_response():
    """Line 1 is the conversation id; the rest is the response body."""
    payload = json.dumps(
        {"conversation_id": "conv_xyz", "response": "Status: BLOCKED\nneed the schema"}
    )
    result = _run_extractor(AGY_EXTRACTOR, payload)
    assert result.returncode == 0
    first, _, body = result.stdout.partition("\n")
    assert first.strip() == "conv_xyz"
    assert "Status: BLOCKED" in body


def test_agy_extractor_tolerates_missing_conversation_id():
    """Response text still flows through; the id line is empty."""
    payload = json.dumps({"response": "all done"})
    result = _run_extractor(AGY_EXTRACTOR, payload)
    assert result.returncode == 0
    first, _, body = result.stdout.partition("\n")
    assert first.strip() == ""
    assert "all done" in body


def test_agy_extractor_fails_on_malformed_input():
    """Garbage exits non-zero so the entrypoint falls back to text mode."""
    result = _run_extractor(AGY_EXTRACTOR, "<<not json>>")
    assert result.returncode != 0
    assert result.stdout.strip() == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_worker_session_resume.py -k agy_extractor -v`
Expected: FAIL, 3 failed, the script does not exist.

- [ ] **Step 3: Write minimal implementation**

Create `docker/agy-agent/extract_session.py`:

```python
#!/usr/bin/env python3
"""Split an agy `--output-format json` envelope into id + response text.

Reads the JSON envelope on stdin. Prints the conversation id on the FIRST line
(empty if absent) and the response body on the remaining lines, then exits 0.
On malformed input prints nothing and exits 1, so the entrypoint can fall back
to plain text mode.

Emitting the body on stdout keeps the existing `Status:` grep working unchanged
against the extractor's output.
"""

from __future__ import annotations

import json
import sys


_RESPONSE_KEYS = ("response", "text", "output", "content", "message")


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 1

    if not isinstance(data, dict):
        return 1

    conversation_id = data.get("conversation_id") or ""
    if not isinstance(conversation_id, str):
        conversation_id = ""

    body = ""
    for key in _RESPONSE_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value:
            body = value
            break

    print(conversation_id)
    if body:
        print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_worker_session_resume.py -k agy_extractor -v`
Expected: PASS, 3 passed.

- [ ] **Step 5: Commit**

```bash
git add docker/agy-agent/extract_session.py tests/test_worker_session_resume.py
git commit -m "feat(agy-agent): add conversation id extractor"
```

---

### Task 4: OpenCode sessions volume setting

**Files:**
- Modify: `src/orchestrator/config.py:71-74`
- Modify: `docker-compose.yml:65`
- Test: `tests/test_worker_session_resume.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

Append to `tests/test_worker_session_resume.py`:

```python
from orchestrator.config import Settings


def test_opencode_sessions_volume_has_default():
    """The OpenCode session volume mirrors the gemini creds volume pattern."""
    settings = Settings(
        _env_file=None,
        auth_token="test-token",
        github_token="test-gh-token",
    )
    assert settings.opencode_sessions_volume == "praxis-opencode-sessions"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_worker_session_resume.py -k sessions_volume -v`
Expected: FAIL, `AttributeError: 'Settings' object has no attribute 'opencode_sessions_volume'`.

- [ ] **Step 3: Write minimal implementation**

In `src/orchestrator/config.py`, directly after the `gemini_creds_volume` field:

```python
    # Named Docker volume holding OpenCode session state, mounted read-write at
    # /home/agent/.local/share/opencode so a re-dispatched worker can resume its
    # conversation with `opencode run --session <id>`. Unlike the agy creds
    # volume this needs no interactive seeding; Docker creates it on first use.
    # Empty disables persistence: workers then always start cold, never error.
    opencode_sessions_volume: str = "praxis-opencode-sessions"
```

In `docker-compose.yml`, directly after the `GEMINI_CREDS_VOLUME` line:

```yaml
      - OPENCODE_SESSIONS_VOLUME=${OPENCODE_SESSIONS_VOLUME:-praxis-opencode-sessions}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_worker_session_resume.py -k sessions_volume -v`
Expected: PASS, 1 passed.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/config.py docker-compose.yml tests/test_worker_session_resume.py
git commit -m "feat(config): add opencode_sessions_volume setting"
```

---

### Task 5: Persist the session id from the agent callback

**Files:**
- Modify: `src/orchestrator/api/internal.py:21-28`, `:90-92`
- Modify: `src/orchestrator/core/task_queue.py:225-230`
- Test: `tests/test_worker_session_resume.py`

**Depends on:** Task 1

- [ ] **Step 1: Write the failing test**

`tests/conftest.py` provides `db`, `client`, `auth_headers`, `seed_user` and `test_settings`, but no task-level fixtures, and tasks are only ever created through `TaskQueue.activate_plan`. So this file defines its own local fixtures with a direct INSERT. Append to `tests/test_worker_session_resume.py`:

```python
import uuid

from orchestrator.core.task_queue import TaskQueue


@pytest.fixture
def queue(db: Database) -> TaskQueue:
    return TaskQueue(db)


@pytest.fixture
async def task_row(db: Database) -> dict:
    """Insert a bare task row directly.

    Tasks normally arrive via activate_plan, which needs a project and a plan;
    none of that is relevant to the session handle, so we insert straight into
    the table and return the row.
    """
    task_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO tasks (id, plan_id, title, description, branch_name) "
        "VALUES (?, ?, ?, ?, ?)",
        (task_id, "plan-1", "t", "d", "agent/t"),
    )
    return {"id": task_id}


@pytest.mark.asyncio
async def test_record_worker_session_persists_id_and_harness(queue, task_row):
    """The handle is stored as a pair so replay can check the harness."""
    await queue.record_worker_session(task_row["id"], "conv_abc", "agy")
    task = await queue.get_task(task_row["id"])
    assert task["worker_session_id"] == "conv_abc"
    assert task["worker_session_harness"] == "agy"


@pytest.mark.asyncio
async def test_clear_worker_session_nulls_both_columns(queue, task_row):
    """A terminal task must never leave a replayable id behind."""
    await queue.record_worker_session(task_row["id"], "conv_abc", "agy")
    await queue.clear_worker_session(task_row["id"])
    task = await queue.get_task(task_row["id"])
    assert task["worker_session_id"] is None
    assert task["worker_session_harness"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_worker_session_resume.py -k worker_session -v`
Expected: FAIL, `AttributeError: 'TaskQueue' object has no attribute 'record_worker_session'`.

- [ ] **Step 3: Write minimal implementation**

In `src/orchestrator/core/task_queue.py`, directly after `set_task_pr_url`:

```python
    async def record_worker_session(
        self, task_id: str, session_id: str, harness: str
    ) -> None:
        """Store the worker's harness-native session handle for later resume.

        The harness is stored with the id because replay must refuse a handle
        minted by a different harness.
        """
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "UPDATE tasks SET worker_session_id = ?, worker_session_harness = ?, "
            "updated_at = ? WHERE id = ?",
            (session_id, harness, now, task_id),
        )

    async def clear_worker_session(self, task_id: str) -> None:
        """Drop the session handle so a terminal task never replays a stale id."""
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "UPDATE tasks SET worker_session_id = NULL, "
            "worker_session_harness = NULL, updated_at = ? WHERE id = ?",
            (now, task_id),
        )
```

In `src/orchestrator/api/internal.py`, add the field to `AgentDonePayload`:

```python
class AgentDonePayload(BaseModel):
    """Agent completion callback payload."""

    task_id: str
    run_id: str | None = None
    status: str
    pr_url: str | None = None
    question: str | None = None
    session_id: str | None = None
```

And directly after the existing `set_task_pr_url` block in `agent_done`:

```python
    if body.session_id:
        # The worker only reports a session id after its checkpoint is safely
        # pushed, so storing it here is what makes resume eligible next turn.
        project = None
        plan = await queue.get_plan(task["plan_id"])
        if plan:
            project = await queue.get_project(plan["project_id"])
        harness = (project or {}).get("harness") or "opencode"
        await queue.record_worker_session(body.task_id, body.session_id, harness)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_worker_session_resume.py -k worker_session -v`
Expected: PASS, 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/api/internal.py src/orchestrator/core/task_queue.py tests/test_worker_session_resume.py
git commit -m "feat(api): persist worker session handle from agent callback"
```

---

### Task 6: Thread WORKER_SESSION_ID and mount the OpenCode volume

**Files:**
- Modify: `src/orchestrator/core/agent_manager.py:92-150` (`build_spawn_env`), `:156-186` (`__init__`), `:188-205` (`spawn_agent` signature), `:282-296` (volumes)
- Modify: `src/orchestrator/main.py:124`
- Test: `tests/test_worker_session_resume.py`

**Depends on:** Task 4

- [ ] **Step 1: Write the failing test**

Append to `tests/test_worker_session_resume.py`:

```python
from orchestrator.core.agent_manager import build_spawn_env


def _base_env_kwargs(**overrides):
    kwargs = {
        "repo_url": "https://github.com/o/r",
        "branch": "agent/x",
        "base_branch": "plan/y",
        "task_prompt": "do the thing",
        "container_lm_url": "http://host.docker.internal:1234",
        "model_name": "qwen3",
        "harness_id": "opencode",
        "gh_token": "tok",
        "callback_url": "http://host:12323/api/internal/agent-done",
        "task_id": "t-1",
    }
    kwargs.update(overrides)
    return kwargs


def test_build_spawn_env_sets_worker_session_id_when_given():
    env = build_spawn_env(**_base_env_kwargs(worker_session_id="ses_abc"))
    assert env["WORKER_SESSION_ID"] == "ses_abc"


def test_build_spawn_env_omits_worker_session_id_when_absent():
    env = build_spawn_env(**_base_env_kwargs())
    assert "WORKER_SESSION_ID" not in env
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_worker_session_resume.py -k spawn_env -v`
Expected: FAIL, `TypeError: build_spawn_env() got an unexpected keyword argument 'worker_session_id'`.

- [ ] **Step 3: Write minimal implementation**

In `src/orchestrator/core/agent_manager.py`, add the parameter to `build_spawn_env` after `context_limit`:

```python
    worker_session_id: str | None = None,
```

and before the closing `return environment`:

```python
    if worker_session_id:
        # Presence of this var means BOTH "resume the conversation" and "reuse
        # the existing remote branch": memory and tree must move together.
        environment["WORKER_SESSION_ID"] = worker_session_id
```

Add the same parameter to `spawn_agent` after `single_branch: bool = False,`:

```python
        worker_session_id: str | None = None,
```

and pass it through in the `build_spawn_env(...)` call:

```python
            worker_session_id=worker_session_id,
```

Add the constructor parameter after `gemini_creds_volume: str = "",`:

```python
        opencode_sessions_volume: str = "",
```

and in the body after `self._gemini_creds_volume = gemini_creds_volume`:

```python
        self._opencode_sessions_volume = opencode_sessions_volume
```

Extend the volumes block, directly after the existing `if harness_id == "agy":` branch:

```python
        if harness_id == "opencode" and self._opencode_sessions_volume:
            # OpenCode keeps session state under XDG_DATA_HOME. Without this
            # mount it dies with the container and resume degrades to a cold
            # start (which is a supported outcome, not an error).
            volumes[self._opencode_sessions_volume] = {
                "bind": "/home/agent/.local/share/opencode",
                "mode": "rw",
            }
```

In `src/orchestrator/main.py`, next to `gemini_creds_volume=settings.gemini_creds_volume,`:

```python
            opencode_sessions_volume=settings.opencode_sessions_volume,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_worker_session_resume.py -k spawn_env -v && uv run pytest tests/test_agent_manager.py -v`
Expected: PASS for both, no regressions in the existing agent manager suite.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/agent_manager.py src/orchestrator/main.py tests/test_worker_session_resume.py
git commit -m "feat(agent-manager): thread WORKER_SESSION_ID and mount opencode sessions volume"
```

---

### Task 7: Replay gate in the dispatch loop

**Files:**
- Create: `src/orchestrator/core/session_resume.py`
- Modify: `src/orchestrator/core/orchestrator_dispatch.py:137-154`
- Test: `tests/test_worker_session_resume.py`

**Depends on:** Task 5, Task 6

The gate is a pure function in its own module so it is testable without a dispatch loop, a database, or Docker.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_worker_session_resume.py`:

```python
from orchestrator.core.session_resume import resolve_resume_session


def test_resume_allowed_after_brain_answered_clarification():
    task = {
        "worker_session_id": "ses_1",
        "worker_session_harness": "opencode",
        "clarification_state": "answered_by_brain",
    }
    assert resolve_resume_session(task, "opencode") == "ses_1"


def test_resume_allowed_after_human_resolved_clarification():
    task = {
        "worker_session_id": "ses_1",
        "worker_session_harness": "agy",
        "clarification_state": "resolved",
    }
    assert resolve_resume_session(task, "agy") == "ses_1"


def test_resume_refused_on_plain_failure_retry():
    """A retry rebuilds from base; restoring memory would contradict the tree."""
    task = {
        "worker_session_id": "ses_1",
        "worker_session_harness": "opencode",
        "clarification_state": None,
    }
    assert resolve_resume_session(task, "opencode") is None


def test_resume_refused_when_harness_changed():
    task = {
        "worker_session_id": "conv_1",
        "worker_session_harness": "agy",
        "clarification_state": "resolved",
    }
    assert resolve_resume_session(task, "opencode") is None


def test_resume_refused_without_stored_id():
    """No id means the previous turn's checkpoint push never succeeded."""
    task = {
        "worker_session_id": None,
        "worker_session_harness": "opencode",
        "clarification_state": "resolved",
    }
    assert resolve_resume_session(task, "opencode") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_worker_session_resume.py -k resume -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'orchestrator.core.session_resume'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/orchestrator/core/session_resume.py`:

```python
"""Decide whether a re-dispatch may resume the worker's previous session.

Resume is safe only when the worker's restored memory matches the tree it will
be handed. See docs/superpowers/specs/2026-08-05-worker-session-resume-design.md.
"""

from __future__ import annotations

from typing import Any


# States set by TaskQueue.record_clarification_answer once a blocked worker's
# question has been answered. Any other state means this is a failure retry,
# which deliberately rebuilds the branch from base.
RESUMABLE_CLARIFICATION_STATES = frozenset({"answered_by_brain", "resolved"})


def resolve_resume_session(task: dict[str, Any], harness: str) -> str | None:
    """Return the session id to replay, or None to start cold.

    Args:
        task: Task row, as returned by ``TaskQueue.get_task``.
        harness: Harness about to be spawned for this dispatch.

    Returns:
        The stored session id when all three replay conditions hold, else None.
    """
    session_id = task.get("worker_session_id")
    if not session_id:
        return None
    if task.get("worker_session_harness") != harness:
        return None
    if task.get("clarification_state") not in RESUMABLE_CLARIFICATION_STATES:
        return None
    return str(session_id)
```

In `src/orchestrator/core/orchestrator_dispatch.py`, add the import at the top of the module:

```python
from orchestrator.core.session_resume import resolve_resume_session
```

Directly before the `container_id = await self._agents.spawn_agent(` call:

```python
            harness_id = project.get("harness") or "opencode"
            resume_session = resolve_resume_session(task, harness_id)
```

Then change the two matching arguments in the `spawn_agent(...)` call:

```python
                    harness=harness_id,
```

and add after `single_branch=single_branch,`:

```python
                    worker_session_id=resume_session,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_worker_session_resume.py -k resume -v && uv run pytest tests/test_orchestrator_dispatch.py -v`
Expected: PASS for both. If `tests/test_orchestrator_dispatch.py` does not exist, run `uv run pytest tests/ -k dispatch -v` instead.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/session_resume.py src/orchestrator/core/orchestrator_dispatch.py tests/test_worker_session_resume.py
git commit -m "feat(dispatch): gate session resume to answered clarifications"
```

---

### Task 8: OpenCode entrypoint — checkpoint, capture, replay

**Files:**
- Modify: `docker/opencode-agent/entrypoint.sh:124-130` (branch reuse), `:203-239` (run + capture + checkpoint), `:41-81` (callback payload)
- Modify: `docker/opencode-agent/Dockerfile` (copy the extractor, pin `XDG_DATA_HOME`)

**Depends on:** Task 2

- [ ] **Step 1: Extend the callback payload**

In `send_callback`, after the `question_json` block:

```bash
    local session_json="null"
    if [ -n "${CAPTURED_SESSION_ID:-}" ]; then
        session_json=$(printf "%s" "${CAPTURED_SESSION_ID}" | json_escape)
    fi
```

and replace the `payload` assignment with:

```bash
    local payload="{\"task_id\":\"${TASK_ID}\",\"run_id\":${run_json},\"status\":\"${STATUS}\",\"pr_url\":${pr_json},\"question\":${question_json},\"session_id\":${session_json}}"
```

Add `CAPTURED_SESSION_ID=""` next to the other initializations at the top (near `QUESTION=""`).

- [ ] **Step 2: Reuse the branch when resuming**

Replace the branch-selection block at lines 124-130 with:

```bash
if { [ "${SINGLE_BRANCH:-0}" = "1" ] || [ -n "${WORKER_SESSION_ID:-}" ]; } \
    && git rev-parse --verify "origin/${BRANCH}" >/dev/null 2>&1; then
    # Reuse the existing remote branch. Required when resuming a session: the
    # restored conversation refers to edits checkpointed on this branch, so a
    # fresh branch cut from base would contradict the worker's memory.
    echo "--- Reusing existing origin/${BRANCH} ---"
    git checkout -b "${BRANCH}" "origin/${BRANCH}"
else
    echo "--- Creating branch ${BRANCH} from ${BASE_BRANCH} ---"
    git checkout -b "${BRANCH}"
fi
```

- [ ] **Step 3: Pass the session flag to opencode**

Replace the `opencode run` invocation (line 220) with:

```bash
OPENCODE_ARGS=(run --model "lmstudio/${MODEL}")
if [ -n "${WORKER_SESSION_ID:-}" ]; then
    echo "--- Resuming OpenCode session ${WORKER_SESSION_ID} ---"
    OPENCODE_ARGS+=(--session "${WORKER_SESSION_ID}")
fi

OUTPUT_LOG="$(mktemp)"
set +e
opencode "${OPENCODE_ARGS[@]}" "${EFFECTIVE_PROMPT}" 2>&1 | tee "${OUTPUT_LOG}"
opencode_rc="${PIPESTATUS[0]}"
set -e
if [ "${opencode_rc}" -ne 0 ] && [ -n "${WORKER_SESSION_ID:-}" ]; then
    # A stale or pruned session id must not fail the task. Retry once cold.
    echo "WARNING: resume with session ${WORKER_SESSION_ID} failed; retrying cold"
    set +e
    opencode run --model "lmstudio/${MODEL}" "${EFFECTIVE_PROMPT}" 2>&1 | tee "${OUTPUT_LOG}"
    opencode_rc="${PIPESTATUS[0]}"
    set -e
fi
if [ "${opencode_rc}" -ne 0 ]; then
    exit "${opencode_rc}"
fi
```

Delete the original three lines that created `OUTPUT_LOG` and ran `opencode run`, so the block above is the only invocation.

- [ ] **Step 4: Capture the session id**

Immediately after the run block:

```bash
echo "--- Capturing OpenCode session id (best effort) ---"
if CAPTURED_SESSION_ID=$(opencode session list --format json 2>/dev/null \
    | python3 /usr/local/bin/extract_session.py 2>/dev/null); then
    echo "Session id: ${CAPTURED_SESSION_ID}"
else
    CAPTURED_SESSION_ID=""
    echo "No session id captured; next dispatch will start cold"
fi
```

- [ ] **Step 5: Checkpoint work-in-progress on BLOCKED**

Replace the `if [ "${report_status}" = "BLOCKED" ] ...` block with:

```bash
if [ "${report_status}" = "BLOCKED" ] || [ "${report_status}" = "NEEDS_CONTEXT" ]; then
    echo "--- Worker reported ${report_status}; checkpointing WIP (no PR) ---"
    QUESTION=$(awk '/^Concerns/{flag=1;next}/^====/{flag=0}flag' "${OUTPUT_LOG}" \
        | sed '/^[[:space:]]*$/d')
    [ -z "${QUESTION}" ] && QUESTION="Worker reported ${report_status} without details."

    # Checkpoint so the resumed worker's tree matches its restored memory.
    # .praxis-bible.md is in .git/info/exclude, so `git add -A` cannot stage it.
    checkpoint_ok=1
    git add -A
    if ! git diff --cached --quiet; then
        git commit -m "wip: checkpoint before clarification (${BRANCH})"
    fi
    if [ "$(git rev-list --count "${BASE_BRANCH}..HEAD")" -gt 0 ]; then
        if ! git push -u origin "${BRANCH}"; then
            echo "WARNING: checkpoint push failed; suppressing session resume"
            checkpoint_ok=0
        fi
    fi
    # The invariant: only report a session id once its checkpoint is on the
    # remote. Otherwise the next turn must start cold and rebuild from base.
    if [ "${checkpoint_ok}" -ne 1 ]; then
        CAPTURED_SESSION_ID=""
    fi

    STATUS="needs_clarification"
    send_callback
    trap - EXIT
    exit 0
fi
```

- [ ] **Step 6: Bake the extractor into the image**

In `docker/opencode-agent/Dockerfile`, alongside the existing entrypoint COPY:

```dockerfile
COPY extract_session.py /usr/local/bin/extract_session.py
ENV XDG_DATA_HOME=/home/agent/.local/share
```

`XDG_DATA_HOME` is pinned so the session path the orchestrator mounts is not implementation-dependent.

- [ ] **Step 7: Verify the shell is valid and the checkpoint block runs**

Run:

```bash
bash -n docker/opencode-agent/entrypoint.sh
shellcheck docker/opencode-agent/entrypoint.sh
```

Expected: no output from either (`shellcheck` may warn SC2086 on pre-existing lines; do not fix unrelated warnings).

Then EXECUTE the checkpoint fragment against a scratch repo, because `bash -n` cannot catch runtime bugs like a `printf` leading-dash (a prior session shipped exactly that):

```bash
rm -rf /tmp/ckpt && mkdir -p /tmp/ckpt && cd /tmp/ckpt && git init -q . \
  && git config user.email a@b.c && git config user.name a \
  && git commit -q --allow-empty -m base && git checkout -qb feat \
  && echo change > f.txt \
  && git add -A && git diff --cached --quiet || git commit -q -m "wip: checkpoint before clarification (feat)" \
  && git rev-list --count master..HEAD
```

Expected: prints `1`. (Use `main..HEAD` if `git init` defaulted to `main`.)

- [ ] **Step 8: Commit**

```bash
git add docker/opencode-agent/entrypoint.sh docker/opencode-agent/Dockerfile
git commit -m "feat(opencode-agent): checkpoint on blocked, capture and replay session"
```

---

### Task 9: agy entrypoint — checkpoint, capture, replay

**Files:**
- Modify: `docker/agy-agent/entrypoint.sh:126-132` (branch reuse), `:236-259` (run + capture + checkpoint), `:43-83` (callback payload)
- Modify: `docker/agy-agent/Dockerfile` (copy the extractor)

**Depends on:** Task 3

If Task 3's verification step showed `--output-format json` yields no capturable stdout, implement ONLY steps 1, 2 and 5 here (checkpoint plus branch reuse), leave the invocation in text mode, and never set `CAPTURED_SESSION_ID`. Resume then stays OpenCode-only and this is recorded in the rollout notes.

- [ ] **Step 1: Extend the callback payload**

Identical to Task 8 Step 1, in `docker/agy-agent/entrypoint.sh`. In `send_callback`, after the `question_json` block:

```bash
    local session_json="null"
    if [ -n "${CAPTURED_SESSION_ID:-}" ]; then
        session_json=$(printf "%s" "${CAPTURED_SESSION_ID}" | json_escape)
    fi
```

Replace the `payload` assignment with:

```bash
    local payload="{\"task_id\":\"${TASK_ID}\",\"run_id\":${run_json},\"status\":\"${STATUS}\",\"pr_url\":${pr_json},\"question\":${question_json},\"session_id\":${session_json}}"
```

Add `CAPTURED_SESSION_ID=""` next to `QUESTION=""` at the top.

- [ ] **Step 2: Reuse the branch when resuming**

Replace the branch-selection block at lines 126-132 with:

```bash
if { [ "${SINGLE_BRANCH:-0}" = "1" ] || [ -n "${WORKER_SESSION_ID:-}" ]; } \
    && git rev-parse --verify "origin/${BRANCH}" >/dev/null 2>&1; then
    # Reuse the existing remote branch. Required when resuming a conversation:
    # the restored context refers to edits checkpointed on this branch.
    echo "--- Reusing existing origin/${BRANCH} ---"
    git checkout -b "${BRANCH}" "origin/${BRANCH}"
else
    echo "--- Creating branch ${BRANCH} from ${BASE_BRANCH} ---"
    git checkout -b "${BRANCH}"
fi
```

- [ ] **Step 3: Run agy in JSON mode and split the envelope**

Replace the `agy` invocation block (lines 236-245) with:

```bash
AGY_ARGS=(--dangerously-skip-permissions --mode accept-edits --print-timeout 30m
          --output-format json --model "${MODEL}")
if [ -n "${WORKER_SESSION_ID:-}" ]; then
    echo "--- Resuming agy conversation ${WORKER_SESSION_ID} ---"
    AGY_ARGS+=(--conversation "${WORKER_SESSION_ID}")
fi

RAW_LOG="$(mktemp)"
OUTPUT_LOG="$(mktemp)"
set +e
agy "${AGY_ARGS[@]}" -p "${EFFECTIVE_PROMPT}" > "${RAW_LOG}" 2>&1
agy_rc=$?
set -e
if [ "${agy_rc}" -ne 0 ] && [ -n "${WORKER_SESSION_ID:-}" ]; then
    # A stale or pruned conversation id must not fail the task. Retry once cold.
    echo "WARNING: resume with conversation ${WORKER_SESSION_ID} failed; retrying cold"
    AGY_ARGS=(--dangerously-skip-permissions --mode accept-edits --print-timeout 30m
              --output-format json --model "${MODEL}")
    set +e
    agy "${AGY_ARGS[@]}" -p "${EFFECTIVE_PROMPT}" > "${RAW_LOG}" 2>&1
    agy_rc=$?
    set -e
fi
cat "${RAW_LOG}"
if [ "${agy_rc}" -ne 0 ]; then
    exit "${agy_rc}"
fi

echo "--- Splitting agy JSON envelope (best effort) ---"
if SPLIT=$(python3 /usr/local/bin/extract_session.py < "${RAW_LOG}" 2>/dev/null); then
    CAPTURED_SESSION_ID=$(printf '%s' "${SPLIT}" | head -n1)
    printf '%s' "${SPLIT}" | tail -n +2 > "${OUTPUT_LOG}"
    echo "Conversation id: ${CAPTURED_SESSION_ID:-<none>}"
else
    # Envelope unparseable: fall back to treating raw output as the transcript,
    # exactly as before this feature existed.
    CAPTURED_SESSION_ID=""
    cp "${RAW_LOG}" "${OUTPUT_LOG}"
    echo "Envelope unparseable; continuing without conversation id"
fi
```

The existing `report_status` grep below reads `${OUTPUT_LOG}` and needs no change.

- [ ] **Step 4: Bake the extractor into the image**

In `docker/agy-agent/Dockerfile`, alongside the existing entrypoint COPY:

```dockerfile
COPY extract_session.py /usr/local/bin/extract_session.py
```

- [ ] **Step 5: Checkpoint work-in-progress on BLOCKED**

Replace the `if [ "${report_status}" = "BLOCKED" ] ...` block with:

```bash
if [ "${report_status}" = "BLOCKED" ] || [ "${report_status}" = "NEEDS_CONTEXT" ]; then
    echo "--- Worker reported ${report_status}; checkpointing WIP (no PR) ---"
    QUESTION=$(awk '/^Concerns/{flag=1;next}/^====/{flag=0}flag' "${OUTPUT_LOG}" \
        | sed '/^[[:space:]]*$/d')
    [ -z "${QUESTION}" ] && QUESTION="Worker reported ${report_status} without details."

    # Checkpoint so the resumed worker's tree matches its restored memory.
    # .praxis-bible.md is in .git/info/exclude, so `git add -A` cannot stage it.
    checkpoint_ok=1
    git add -A
    if ! git diff --cached --quiet; then
        git commit -m "wip: checkpoint before clarification (${BRANCH})"
    fi
    if [ "$(git rev-list --count "${BASE_BRANCH}..HEAD")" -gt 0 ]; then
        if ! git push -u origin "${BRANCH}"; then
            echo "WARNING: checkpoint push failed; suppressing session resume"
            checkpoint_ok=0
        fi
    fi
    # Only report a conversation id once its checkpoint is on the remote.
    if [ "${checkpoint_ok}" -ne 1 ]; then
        CAPTURED_SESSION_ID=""
    fi

    STATUS="needs_clarification"
    send_callback
    trap - EXIT
    exit 0
fi
```

- [ ] **Step 6: Verify the shell is valid**

Run:

```bash
bash -n docker/agy-agent/entrypoint.sh
shellcheck docker/agy-agent/entrypoint.sh
```

Expected: no output from either.

- [ ] **Step 7: Commit**

```bash
git add docker/agy-agent/entrypoint.sh docker/agy-agent/Dockerfile
git commit -m "feat(agy-agent): checkpoint on blocked, capture and replay conversation"
```

---

### Task 10: Clear the handle when a task goes terminal

**Files:**
- Modify: `src/orchestrator/core/task_queue.py:150-158` (`mark_merged`), `:160-167` (`fail_task`)
- Test: `tests/test_worker_session_resume.py`

**Depends on:** Task 5

Both terminal transitions live in `TaskQueue`, so the handle is cleared in the same UPDATE rather than in a second round trip from the review mixin.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_worker_session_resume.py`:

```python
@pytest.mark.asyncio
async def test_fail_task_clears_worker_session(queue, task_row):
    """A failed task must not leave a replayable handle behind."""
    await queue.record_worker_session(task_row["id"], "ses_1", "opencode")
    await queue.fail_task(task_row["id"], "gave up")
    task = await queue.get_task(task_row["id"])
    assert task["worker_session_id"] is None
    assert task["worker_session_harness"] is None


@pytest.mark.asyncio
async def test_mark_merged_clears_worker_session(queue, task_row):
    """A merged task's session is finished; drop the handle."""
    await queue.record_worker_session(task_row["id"], "ses_1", "opencode")
    await queue.mark_merged(task_row["id"])
    task = await queue.get_task(task_row["id"])
    assert task["worker_session_id"] is None
    assert task["worker_session_harness"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_worker_session_resume.py -k clears_worker_session -v`
Expected: FAIL, 2 failed with `assert 'ses_1' is None`.

- [ ] **Step 3: Write minimal implementation**

In `src/orchestrator/core/task_queue.py`, replace the body of `mark_merged`:

```python
    async def mark_merged(self, task_id: str) -> None:
        """Mark a task merged and stamp the approval time.

        Clears the worker session handle in the same statement: the task is
        terminal, so the handle can only ever be stale from here.
        """
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """UPDATE tasks
               SET status = ?, approved_at = ?, updated_at = ?,
                   worker_session_id = NULL, worker_session_harness = NULL
               WHERE id = ?""",
            (TaskStatus.MERGED, now, now, task_id),
        )
```

and the body of `fail_task`:

```python
    async def fail_task(self, task_id: str, feedback: str) -> None:
        """Mark a task failed and drop any worker session handle."""
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """UPDATE tasks
               SET status = ?, review_feedback = ?, updated_at = ?,
                   worker_session_id = NULL, worker_session_harness = NULL
               WHERE id = ?""",
            (TaskStatus.FAILED, feedback, now, task_id),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_worker_session_resume.py -v && uv run pytest tests/ -k "task_queue or review" -v`
Expected: PASS for both, no regressions.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/task_queue.py tests/test_worker_session_resume.py
git commit -m "feat(task-queue): clear worker session handle on terminal status"
```

---

### Task 11: Full verification, image rebuild, and docs

**Files:**
- Modify: `CLAUDE.md` (gotchas index)
- Modify: `docs/gotchas.md` (full narrative)
- Modify: `docs/deployment.md` (volume setup)

**Depends on:** Task 7, Task 8, Task 9, Task 10

- [ ] **Step 1: Run the full gate**

```bash
uv run ruff format src/ tests/
uv run ruff check --fix src/ tests/
uv run mypy src/orchestrator/ --ignore-missing-imports
uv run pytest --cov=orchestrator --cov-report=term-missing --cov-fail-under=80 -v
```

Expected: ruff clean, mypy clean, all tests pass, coverage at or above 80.

- [ ] **Step 2: Rebuild both agent images**

Entrypoint and Dockerfile changes do NOT hot-reload; a stale image runs silently.

```bash
docker build -t opencode-agent:latest -f docker/opencode-agent/Dockerfile docker/opencode-agent/
docker build -t agy-agent:latest -f docker/agy-agent/Dockerfile docker/agy-agent/
```

Verify the extractors actually landed in the images:

```bash
docker run --rm opencode-agent:latest cat /usr/local/bin/extract_session.py | head -3
docker run --rm agy-agent:latest cat /usr/local/bin/extract_session.py | head -3
```

Expected: the shebang and docstring of each script.

- [ ] **Step 3: Rebuild the orchestrator image**

`config/` is not mounted by the dev compose overlay and the orchestrator image bakes its config, so a settings addition needs a rebuild, not a restart.

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up --build -d
docker logs --tail 50 orchestrator
```

Expected: startup logs include `Applying schema migration 6: add tasks.worker_session_id/worker_session_harness for session resume`.

- [ ] **Step 4: Document the gotchas**

Add to the gotchas index in `CLAUDE.md`:

```markdown
- **Session resume is gated to answered clarifications** — `core/session_resume.resolve_resume_session`
  returns an id only when a stored handle, a matching harness, and a resolved clarification all
  line up. `WORKER_SESSION_ID` means BOTH "resume the conversation" AND "reuse the remote branch";
  they must move together or restored memory contradicts the tree. A worker reports its session id
  ONLY after its BLOCKED checkpoint is pushed, so a failed push silently forces the next turn cold.
  Entrypoint change: needs an agent IMAGE REBUILD.
```

Add the full narrative to `docs/gotchas.md`:

```markdown
### Worker session resume is deliberately narrow

A re-dispatched worker resumes its previous conversation only when
`core/session_resume.resolve_resume_session` says all three conditions hold: a session
handle is stored, its harness matches the one being spawned, and the task's
`clarification_state` is `answered_by_brain` or `resolved`. A plain failure retry never
resumes, because a retry rebuilds the branch from base and restored memory would then
describe a tree that no longer exists.

`WORKER_SESSION_ID` carries two meanings at once, and they cannot be separated. In both
entrypoints it means "resume the conversation" AND "reuse the existing remote branch
instead of cutting a fresh one from base". Splitting them reintroduces exactly the
memory/tree mismatch the feature exists to prevent.

The chain is anchored at the far end: a worker reports its session id ONLY after its
BLOCKED checkpoint has been pushed. A failed checkpoint push therefore blanks the id, and
the next turn silently starts cold. That is the intended degradation, not a bug, and it is
why `BLOCKED` now commits and pushes work-in-progress (still opening no PR) rather than
discarding the container's edits.

Capture is asymmetric on purpose. OpenCode is read back out of band with
`opencode session list --format json`, leaving the `opencode run` invocation and the
existing `Status:` grep untouched. agy has no session-list equivalent, so it runs with
`--output-format json` and the envelope is split by `extract_session.py` into an id line
plus the response body; an unparseable envelope falls back to raw text with no id.

Both extractors are baked into their images at `/usr/local/bin/extract_session.py`. Any
change here is an entrypoint or image change, so it needs an agent IMAGE REBUILD, not just
a src edit; a stale image runs silently.
```

Document creating the `praxis-opencode-sessions` volume in `docs/deployment.md` next to the existing agy creds volume setup. Unlike the agy volume it needs no interactive seeding: Docker creates it on first use, and an unset `OPENCODE_SESSIONS_VOLUME` means cold starts rather than an error.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md docs/gotchas.md docs/deployment.md
git commit -m "docs: document worker session resume gotchas and volume setup"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1, Task 2, Task 3, Task 4 (no dependencies, run in parallel)
- **Wave 2:** Task 5 (depends on Task 1), Task 6 (depends on Task 4), Task 8 (depends on Task 2), Task 9 (depends on Task 3)
- **Wave 3:** Task 7 (depends on Task 5, Task 6), Task 10 (depends on Task 5)
- **Wave 4:** Task 11 (depends on Task 7, Task 8, Task 9, Task 10)

---

## Notes

**Live verification is not optional.** Nothing in the unit suite proves resume works; it proves the plumbing is wired. The dogfood run must force a genuine `BLOCKED` (give a worker a task with a deliberately ambiguous requirement), answer it, and then confirm from the second run's logs that it printed `Resuming ... session` and did not re-derive the codebase from scratch. Confirm too that the checkpoint commit is present on the branch before the second turn starts.

**The agy JSON envelope is the one real unknown.** Task 3 opens with a probe step for exactly this reason. If it fails, the fallback (OpenCode-only resume, agy keeps checkpointing) is already specified in Task 9 and is a legitimate shipping outcome, not a blocked plan.

**Out of scope, by spec:** mid-run ask-back via an inbox, a live `opencode serve` channel, resume on failure retries, and pruning either session store.
