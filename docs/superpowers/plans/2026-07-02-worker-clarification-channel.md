# Worker Clarification Channel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give a blocked local-model worker a way to ask a question, have the main brain answer it autonomously when it can, and fall back to a human gate when it cannot, instead of silently guessing or failing three times.

**Architecture:** The worker prompt already asks for a `Status: BLOCKED | NEEDS_CONTEXT` line with a `Concerns:` explanation, but nothing parses it: the container's status is derived only from its shell exit code, so a blocked worker either PRs a wrong guess or is marked `FAILED` and re-dispatched with the identical prompt. This plan closes the loop end to end: (1) the agent entrypoint parses the FINAL REPORT, and on a blocked status sends the question in the callback instead of opening a PR; (2) a new `NEEDS_CLARIFICATION` task state parks the task without burning a retry; (3) the orchestration loop asks the brain to answer from the spec/plan context, and on a confident answer re-dispatches the worker with the answer injected (via `progress_note`, which already flows into the Static Bible), otherwise parks the task at a human gate surfaced over SSE, REST, and MCP `poll_task`.

**Why decomposition alone is not enough:** Brain-side plan decomposition is *static* (what the brain can see at plan time from the spec/plan/repo snapshot). Clarification needs are *dynamic* — two plausible existing patterns, a missing env var, a contract that contradicts existing code, or simply a weaker local model getting stuck. Decomposition *minimizes* questions; this channel handles the irreducible residual.

**Tech Stack:** Python 3.11, FastAPI, aiosqlite (raw SQL), bash (agent entrypoint), pytest + pytest-asyncio, MCP stdio server.

---

## Zero-Context Onboarding (read first if this is a fresh session)

You are working in `C:\working-space\praxis` (Windows, PowerShell default shell; Bash tool also available). This is **Praxis**, a Docker-based AI agent orchestrator: Claude (via `claude -p` CLI) plans and reviews; a local LLM (LM Studio + Aider in a Docker container) implements. The full architecture is in `CLAUDE.md` at the repo root — **read `CLAUDE.md` before starting**, especially the "Gotchas" section. Key facts you need for THIS plan:

**Environment & commands (all from repo root):**
```bash
uv venv && uv sync --extra dev          # one-time setup
uv run pytest -q                        # run the whole suite (baseline: ~517 tests, 89% cov)
uv run pytest tests/test_x.py -k name -v # single test
uv run ruff format src/ tests/          # format  (NOTE: the subcommand is `ruff format`, NOT `ruff fmt`)
uv run ruff check --fix src/ tests/     # lint + import sort
uv run mypy src/orchestrator/ --ignore-missing-imports
```

**Codebase facts that this plan relies on:**
- **No ORM.** All persistence is raw SQL via `aiosqlite` in `src/orchestrator/core/task_queue.py` and `src/orchestrator/database.py`. Additive nullable columns are added through the idempotent `ALTER TABLE ... ADD COLUMN` block in `database.py` (wrapped in `contextlib.suppress(Exception)`) — you do NOT need a new `Migration` for nullable columns (Task 2).
- **The Orchestrator is split across mixins.** `core/orchestrator.py` holds the loop core; review/merge live in `core/orchestrator_review.py` (`ReviewMixin`), dispatch in `core/orchestrator_dispatch.py`. They are mixed into one `Orchestrator` class, so `self._tq`, `self._opus`, `self._bus`, `self._git` are all available inside any mixin. **Tests patch module-level helpers on the MIXIN module that calls them, not on `core.orchestrator`.**
- **The Static Bible / progress handover.** `_build_worker_bible` (in `orchestrator_dispatch.py`) already folds `task["progress_note"]` into the worker's re-sent context via `render_handover`. This plan injects the clarification answer through `progress_note` **specifically** to avoid new plumbing — do not add a separate injection path.
- **The aider agent image is standalone and NOT in docker-compose.** After ANY `docker/aider-agent/entrypoint.sh` change you MUST rebuild it or a stale image silently runs old logic:
  `docker build -t aider-agent:latest -f docker/aider-agent/Dockerfile docker/aider-agent/` (Task 4).
- **Brain call-sites** are policy-routed in `core/llm_router.py` (`CALL_SITE_DEFAULTS`) and invoked from `core/opus_bridge.py`. When no router is wired, `OpusBridge` falls back to `_run_claude`; the unit tests exercise that fallback path (Task 6). All brain calls are subscription `claude -p` or local models — **never API keys** (LLM-invocation policy).
- **The callback contract.** Agent containers POST to `/api/internal/agent-done` (`api/internal.py`) with an `X-Praxis-Callback-Token` header; the payload is JSON built by hand in `entrypoint.sh`'s `send_callback`. Today `status` is only `completed`/`failed`; this plan adds `needs_clarification` + a `question` field (Tasks 4-5).
- **Fixtures live in `tests/conftest.py`** (in-memory SQLite, FastAPI `TestClient`, auth headers, seeded user/project/plan/task). Task steps reference fixture names like `seeded_task`, `orchestrator`, `project`, `client`, `auth_headers` — **before writing a test, open `tests/conftest.py` and the sibling test file to confirm the real fixture names and adapt**; the names in this plan are indicative, not guaranteed. If a needed fixture (e.g. `clarifying_task_awaiting_human`) does not exist, add it near the existing task-seeding fixture.

**Definition of done for the whole plan (run before opening the PR):**
```bash
uv run ruff format --check src/ tests/ && uv run ruff check src/ tests/
uv run mypy src/orchestrator/ --ignore-missing-imports
uv run pytest -q            # all green, coverage not below the 80% gate
```
Do NOT claim completion without pasting the passing output (verification-before-completion).

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `src/orchestrator/models/schemas.py` | Task state enum + callback/response payloads | Add `NEEDS_CLARIFICATION`, extend payloads |
| `src/orchestrator/database.py` | Task table columns | Add `clarification_*` columns (idempotent ALTER) |
| `src/orchestrator/core/task_queue.py` | Task lifecycle transitions | Add `mark_needs_clarification`, `record_clarification_answer` |
| `docker/aider-agent/entrypoint.sh` | Parse FINAL REPORT, callback with question | Parse status, skip PR on block |
| `src/orchestrator/api/internal.py` | Agent-done callback | Route blocked status to new state, store question |
| `src/orchestrator/core/opus_bridge.py` | Brain call-sites | Add `answer_clarification` |
| `src/orchestrator/core/llm_router.py` | Call-site routing policy | Add `answer_clarification` default |
| `src/orchestrator/core/orchestrator_review.py` | Loop-side handling | Add `handle_clarification` (mixin method) |
| `src/orchestrator/core/orchestrator.py` | Loop core | Scan `NEEDS_CLARIFICATION` tasks each pass |
| `src/orchestrator/api/tasks.py` | Human answer endpoint | `POST /api/tasks/{id}/clarify` |
| `src/mcp_server/server.py` | MCP status mapping | `poll_task` reports `awaiting_clarification` |
| `web/app.js` + `web/index.html` | Dashboard surfacing | Clarification banner + answer box |

The two agent harnesses that auto-commit (`opencode`, `openhands`) are out of scope for this plan; the channel is delivered on `aider` first (the harness used in the e2e walkthroughs). A follow-up note is added for parity.

---

### Task 1: Add `NEEDS_CLARIFICATION` task state

**Files:**
- Modify: `src/orchestrator/models/schemas.py:19-27`
- Test: `tests/test_schemas.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_schemas.py
from orchestrator.models.schemas import TaskStatus


def test_needs_clarification_status_exists():
    assert TaskStatus.NEEDS_CLARIFICATION == "needs_clarification"
    # It must be distinct from FAILED so a clarifying task does not burn a retry.
    assert TaskStatus.NEEDS_CLARIFICATION != TaskStatus.FAILED
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_schemas.py::test_needs_clarification_status_exists -v`
Expected: FAIL with `AttributeError: NEEDS_CLARIFICATION`

- [ ] **Step 3: Add the enum member**

In `src/orchestrator/models/schemas.py`, inside `class TaskStatus(StrEnum)`:

```python
class TaskStatus(StrEnum):
    """Task lifecycle status."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    REVIEWING = "reviewing"
    PASSED = "passed"
    FAILED = "failed"
    MERGED = "merged"
    NEEDS_CLARIFICATION = "needs_clarification"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_schemas.py::test_needs_clarification_status_exists -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/models/schemas.py tests/test_schemas.py
git commit -m "feat: add NEEDS_CLARIFICATION task state"
```

---

### Task 2: Add clarification columns to the tasks table

**Files:**
- Modify: `src/orchestrator/database.py:204-214`
- Test: `tests/test_database.py`

**Depends on:** None

The `tasks` table gains three columns. `clarification_question` holds the worker's question, `clarification_answer` holds the resolved answer, and `clarification_state` tracks the workflow (`asked` -> `answered_by_brain` | `awaiting_human` -> `resolved`). Columns are added through the existing idempotent `ALTER TABLE ... ADD COLUMN` block (wrapped in `contextlib.suppress(Exception)`), matching how `needs_stronger_model`/`progress_note` were added — no new migration is required for additive nullable columns.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_database.py
import pytest

from orchestrator.database import Database


@pytest.mark.asyncio
async def test_tasks_table_has_clarification_columns(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/t.db")
    await db.initialize()
    cursor = await db._require_connection().execute("PRAGMA table_info(tasks)")
    cols = {row["name"] for row in await cursor.fetchall()}
    await db.close()
    assert {"clarification_question", "clarification_answer", "clarification_state"} <= cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_database.py::test_tasks_table_has_clarification_columns -v`
Expected: FAIL — the three column names are missing from the set.

- [ ] **Step 3: Add the columns to the additive ALTER block**

In `src/orchestrator/database.py`, extend the `"tasks"` column tuple (currently ending at `"approved_at TEXT"`):

```python
            (
                "tasks",
                (
                    "needs_stronger_model INTEGER DEFAULT 0",
                    "escalation_state TEXT",
                    "escalated_to TEXT",
                    "checklist TEXT",
                    "progress_note TEXT",
                    "approved_at TEXT",
                    "clarification_question TEXT",
                    "clarification_answer TEXT",
                    "clarification_state TEXT",
                ),
            ),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_database.py::test_tasks_table_has_clarification_columns -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/database.py tests/test_database.py
git commit -m "feat: add clarification columns to tasks table"
```

---

### Task 3: TaskQueue transitions for clarification

**Files:**
- Modify: `src/orchestrator/core/task_queue.py:139-159`
- Test: `tests/test_task_queue.py`

**Depends on:** Task 1, Task 2

Two methods. `mark_needs_clarification` parks a task (status `NEEDS_CLARIFICATION`, records the question, sets `clarification_state='asked'`) WITHOUT touching `attempt` — this is the crucial difference from `fail_task`, so a question never consumes a retry. `record_clarification_answer` stores the answer, sets state, and folds the Q&A into `progress_note` (which `_build_worker_bible` already threads into the Static Bible via `render_handover`), then bumps the task back to `PENDING` for re-dispatch with `attempt` incremented.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_task_queue.py  (add to existing file)
import pytest

from orchestrator.models.schemas import TaskStatus


@pytest.mark.asyncio
async def test_mark_needs_clarification_parks_without_burning_attempt(task_queue, seeded_task):
    task_id = seeded_task["id"]
    before = (await task_queue.get_task(task_id))["attempt"]
    await task_queue.mark_needs_clarification(task_id, "Which config file holds the API base?")
    task = await task_queue.get_task(task_id)
    assert task["status"] == TaskStatus.NEEDS_CLARIFICATION
    assert task["clarification_question"] == "Which config file holds the API base?"
    assert task["clarification_state"] == "asked"
    assert task["attempt"] == before  # attempt is NOT incremented


@pytest.mark.asyncio
async def test_record_clarification_answer_requeues_with_progress_note(task_queue, seeded_task):
    task_id = seeded_task["id"]
    await task_queue.mark_needs_clarification(task_id, "Which config file?")
    await task_queue.record_clarification_answer(
        task_id, "Use config/praxis.yaml", state="answered_by_brain"
    )
    task = await task_queue.get_task(task_id)
    assert task["status"] == TaskStatus.PENDING
    assert task["clarification_answer"] == "Use config/praxis.yaml"
    assert task["clarification_state"] == "answered_by_brain"
    assert "Which config file?" in task["progress_note"]
    assert "Use config/praxis.yaml" in task["progress_note"]
    assert task["attempt"] == seeded_task["attempt"] + 1
```

If a `seeded_task` fixture does not exist, reuse the existing task-seeding fixture in `tests/conftest.py` (search for one that inserts a `tasks` row and returns it); adjust the parameter name to match.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_task_queue.py -k clarification -v`
Expected: FAIL with `AttributeError: 'TaskQueue' object has no attribute 'mark_needs_clarification'`

- [ ] **Step 3: Implement the two methods**

In `src/orchestrator/core/task_queue.py`, add after `fail_task` (line 146):

```python
    async def mark_needs_clarification(self, task_id: str, question: str) -> None:
        """Park a task that asked a question, WITHOUT consuming a retry attempt."""
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """UPDATE tasks
               SET status = ?, clarification_question = ?,
                   clarification_state = 'asked', updated_at = ?
               WHERE id = ?""",
            (TaskStatus.NEEDS_CLARIFICATION, question, now, task_id),
        )

    async def record_clarification_answer(
        self, task_id: str, answer: str, state: str
    ) -> None:
        """Store the answer, fold the Q&A into progress_note, requeue for dispatch.

        The Q&A is written to ``progress_note`` because ``_build_worker_bible``
        already threads that field into the Static Bible via ``render_handover``,
        so the answer reaches the re-dispatched worker with no extra plumbing.
        """
        task = await self.get_task(task_id)
        if task is None:
            message = f"Task {task_id} not found"
            raise ValueError(message)
        question = task.get("clarification_question") or "(question not recorded)"
        existing_note = task.get("progress_note") or ""
        qa_block = (
            f"ANSWER TO YOUR EARLIER QUESTION (act on this now):\n"
            f"Q: {question}\nA: {answer}"
        )
        merged_note = f"{existing_note}\n\n{qa_block}".strip()
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """UPDATE tasks
               SET status = ?, clarification_answer = ?, clarification_state = ?,
                   progress_note = ?, attempt = ?, updated_at = ?
               WHERE id = ?""",
            (
                TaskStatus.PENDING,
                answer,
                state,
                merged_note,
                int(task["attempt"]) + 1,
                now,
                task_id,
            ),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_task_queue.py -k clarification -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/task_queue.py tests/test_task_queue.py
git commit -m "feat: TaskQueue clarification park/answer transitions"
```

---

### Task 4: Parse the FINAL REPORT in the aider entrypoint

**Files:**
- Modify: `docker/aider-agent/entrypoint.sh:136-164`
- Test: `tests/test_entrypoint_clarification.py`

**Depends on:** None

The entrypoint tees aider's output to a file, then parses the last `Status:` line. On `BLOCKED`/`NEEDS_CONTEXT` it extracts the `Concerns:` block as the question, sets `STATUS="needs_clarification"` and `QUESTION`, and SKIPS `git push`/`gh pr create` (no wrong-guess PR). On any other status it pushes and opens the PR exactly as today. `send_callback` includes the question. Because `set -euo pipefail` is active, aider's real exit code is read from `${PIPESTATUS[0]}`.

- [ ] **Step 1: Write the failing test (shell parse logic in isolation)**

```python
# tests/test_entrypoint_clarification.py
import subprocess
from pathlib import Path

PARSE_SNIPPET = r"""
set -euo pipefail
OUTPUT_LOG="$1"
STATUS="completed"
QUESTION=""
report_status=$(grep -oE '^Status:[[:space:]]*[A-Z_]+' "$OUTPUT_LOG" | tail -n1 | sed -E 's/^Status:[[:space:]]*//') || true
case "${report_status}" in
    BLOCKED|NEEDS_CONTEXT)
        STATUS="needs_clarification"
        QUESTION=$(awk '/^Concerns/{flag=1;next}/^====/{flag=0}flag' "$OUTPUT_LOG" | sed '/^[[:space:]]*$/d')
        ;;
esac
printf 'STATUS=%s\n' "$STATUS"
printf 'QUESTION=%s\n' "$QUESTION"
"""


def _run(tmp_path: Path, report: str) -> str:
    log = tmp_path / "aider.log"
    log.write_text(report, encoding="utf-8")
    script = tmp_path / "parse.sh"
    script.write_text(PARSE_SNIPPET, encoding="utf-8")
    out = subprocess.run(
        ["bash", str(script), str(log)],
        capture_output=True, text=True, check=True,
    )
    return out.stdout


def test_blocked_report_yields_clarification_and_question(tmp_path):
    report = (
        "Status: NEEDS_CONTEXT\n\n"
        "Concerns (if Status is DONE_WITH_CONCERNS, BLOCKED, or NEEDS_CONTEXT):\n"
        "Two auth helpers exist; which should the new endpoint call?\n"
        "========================================================================\n"
    )
    out = _run(tmp_path, report)
    assert "STATUS=needs_clarification" in out
    assert "which should the new endpoint call?" in out


def test_done_report_stays_completed(tmp_path):
    out = _run(tmp_path, "Status: DONE\n")
    assert "STATUS=completed" in out
    assert "QUESTION=\n" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_entrypoint_clarification.py -v`
Expected: FAIL — the file does not exist yet (the test is written before we wire the snippet into the real entrypoint; this test locks the parse contract the entrypoint must satisfy).

Note for the implementer: if `bash` is unavailable on the runner, mark this test `@pytest.mark.skipif(shutil.which("bash") is None, ...)`. The CI `test` matrix includes `windows-latest`; guard accordingly.

- [ ] **Step 3: Wire the parse + conditional-PR logic into the entrypoint**

In `docker/aider-agent/entrypoint.sh`, first extend `send_callback`'s payload to carry the question (line 32):

```bash
    local question_json="null"
    if [ -n "${QUESTION:-}" ]; then
        question_json=$(printf "%s" "${QUESTION}" | json_escape)
    fi
    local payload="{\"task_id\":\"${TASK_ID}\",\"run_id\":${run_json},\"status\":\"${STATUS}\",\"pr_url\":${pr_json},\"question\":${question_json}}"
```

Then replace the aider invocation and the push/PR block (lines 136-164) with:

```bash
QUESTION=""
OUTPUT_LOG="$(mktemp)"

set +e
aider \
    --message "${TASK_PROMPT}" \
    --model "openai/${MODEL}" \
    --auto-commits \
    --yes-always \
    --no-auto-lint \
    --no-suggest-shell-commands \
    --no-show-model-warnings \
    --no-browser \
    --no-detect-urls \
    "${read_args[@]}" 2>&1 | tee "${OUTPUT_LOG}"
aider_rc="${PIPESTATUS[0]}"
set -e
if [ "${aider_rc}" -ne 0 ]; then
    exit "${aider_rc}"   # trap cleanup sends status=failed
fi

report_status=$(grep -oE '^Status:[[:space:]]*[A-Z_]+' "${OUTPUT_LOG}" \
    | tail -n1 | sed -E 's/^Status:[[:space:]]*//') || true

if [ "${report_status}" = "BLOCKED" ] || [ "${report_status}" = "NEEDS_CONTEXT" ]; then
    echo "--- Worker reported ${report_status}; sending clarification request (no PR) ---"
    QUESTION=$(awk '/^Concerns/{flag=1;next}/^====/{flag=0}flag' "${OUTPUT_LOG}" \
        | sed '/^[[:space:]]*$/d')
    [ -z "${QUESTION}" ] && QUESTION="Worker reported ${report_status} without details."
    STATUS="needs_clarification"
    send_callback
    trap - EXIT       # disable cleanup: callback already sent
    exit 0
fi

echo "--- Pushing branch ---"
git push -u origin "${BRANCH}"

echo "--- Creating PR ---"
PR_URL=$(gh pr create \
    --title "agent: ${BRANCH}" \
    --body "Automated implementation by AI Agent.

Task: ${TASK_SUMMARY:-${BRANCH}}

---
Generated by AI Agent Orchestrator" \
    --base "${BASE_BRANCH}" \
    --head "${BRANCH}")

echo "PR created: ${PR_URL}"
echo "=== Agent completed ==="
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_entrypoint_clarification.py -v`
Expected: PASS (2 passed, or skipped on Windows)

- [ ] **Step 5: Rebuild the standalone agent image and commit**

The aider image is NOT in docker-compose; a stale image silently runs old entrypoint logic (see CLAUDE.md gotcha). Rebuild:

```bash
docker build -t aider-agent:latest -f docker/aider-agent/Dockerfile docker/aider-agent/
git add docker/aider-agent/entrypoint.sh tests/test_entrypoint_clarification.py
git commit -m "feat: aider entrypoint parses FINAL REPORT and requests clarification"
```

---

### Task 5: Route the blocked callback to NEEDS_CLARIFICATION

**Files:**
- Modify: `src/orchestrator/api/internal.py:21-95`
- Test: `tests/test_api_internal.py`

**Depends on:** Task 1, Task 3, Task 4

`AgentDonePayload` gains an optional `question`. When `status == "needs_clarification"`, the callback records the question via `mark_needs_clarification` (no PR, no retry burned) instead of the binary completed/failed branch. Container logs are still checkpointed and the container cleaned up as before.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_internal.py  (add to existing file)
def test_agent_done_needs_clarification_parks_task(client, seeded_run, auth_callback_headers):
    task_id = seeded_run["task_id"]
    resp = client.post(
        "/api/internal/agent-done",
        headers=auth_callback_headers,
        json={
            "task_id": task_id,
            "run_id": seeded_run["run_id"],
            "status": "needs_clarification",
            "question": "Which config file holds the API base?",
        },
    )
    assert resp.status_code == 200
    task = client.get(f"/api/tasks/{task_id}", headers=auth_callback_headers).json()
    assert task["status"] == "needs_clarification"
    assert task["clarification_question"] == "Which config file holds the API base?"
```

Reuse whatever fixtures the existing `test_api_internal.py` uses for a seeded run + callback auth headers (search the file for the `agent-done` happy-path test and copy its fixture names).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_internal.py -k needs_clarification -v`
Expected: FAIL — task ends up `failed` (current binary branch) or 422 (unknown `question` field).

- [ ] **Step 3: Extend payload + callback branch**

In `src/orchestrator/api/internal.py`, add the field to `AgentDonePayload`:

```python
class AgentDonePayload(BaseModel):
    """Agent completion callback payload."""

    task_id: str
    run_id: str | None = None
    status: str
    pr_url: str | None = None
    question: str | None = None
```

Replace the completed/else branch (lines 84-91) with:

```python
    if body.status == "completed":
        await queue.update_task_status(body.task_id, TaskStatus.REVIEWING)
        logger.info("Task %s ready for review", body.task_id)
    elif body.status == "needs_clarification":
        question = body.question or "Worker reported a blocker without details."
        await queue.mark_needs_clarification(body.task_id, question)
        logger.info("Task %s is awaiting clarification", body.task_id)
    else:
        await queue.update_task_status(body.task_id, TaskStatus.FAILED)
        logger.warning(
            "Task %s agent finished with status %s", body.task_id, body.status
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_api_internal.py -k needs_clarification -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/api/internal.py tests/test_api_internal.py
git commit -m "feat: agent-done callback parks blocked worker for clarification"
```

---

### Task 6: Brain call-site to answer a clarification

**Files:**
- Modify: `src/orchestrator/core/llm_router.py:62-91`
- Modify: `src/orchestrator/core/opus_bridge.py` (add method + prompt template)
- Test: `tests/test_opus_bridge.py`

**Depends on:** None

A new `answer_clarification` call-site. Its policy default is Sonnet at medium effort — this is bounded reasoning over the task/plan text, not architecture, so Opus would be overkill; it is still a subscription-CLI call (never an API key), consistent with the LLM-invocation policy. The method returns `{"resolved": bool, "answer": str, "confidence": float}`; `resolved=false` means the brain cannot answer from the given context and the task must go to a human.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_opus_bridge.py  (add to existing file)
import pytest


@pytest.mark.asyncio
async def test_answer_clarification_returns_structured_verdict(opus_bridge, monkeypatch):
    async def fake_run_claude(prompt, model=None, effort=None, cwd=None):
        return '{"resolved": true, "answer": "Use config/praxis.yaml", "confidence": 0.9}'

    monkeypatch.setattr(opus_bridge, "_run_claude", fake_run_claude)
    result = await opus_bridge.answer_clarification(
        question="Which config file?",
        task_description="Add a setting",
        plan_text=None,
    )
    assert result["resolved"] is True
    assert result["answer"] == "Use config/praxis.yaml"
    assert 0.0 <= result["confidence"] <= 1.0
```

Use the same `opus_bridge` fixture the other `test_opus_bridge.py` tests use (it constructs `OpusBridge` with no router, so the `_run_claude` fallback path is exercised).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opus_bridge.py -k answer_clarification -v`
Expected: FAIL with `AttributeError: 'OpusBridge' object has no attribute 'answer_clarification'`

- [ ] **Step 3a: Add the router default**

In `src/orchestrator/core/llm_router.py`, add to `CALL_SITE_DEFAULTS`:

```python
    "answer_clarification": {
        "provider": "claude",
        "model": "claude-sonnet-4-6",
        "effort": "medium",
    },
```

- [ ] **Step 3b: Add the prompt template + method**

In `src/orchestrator/core/opus_bridge.py`, add a module-level template near the other `*_PROMPT_TEMPLATE` constants:

```python
CLARIFICATION_PROMPT_TEMPLATE = """\
A local coding model was implementing a task and stopped to ask a question.
Answer it ONLY if the answer is determinable from the task and plan context
below. If the answer requires information not present here (a human decision,
a missing credential, an undocumented business rule), do NOT guess.

Task description:
{task_description}

Plan context:
{plan_text}

The worker's question:
{question}

Respond with a single JSON object and nothing else:
{{"resolved": <true|false>, "answer": "<the answer, or why you cannot answer>", "confidence": <0.0-1.0>}}
"""
```

Add the method after `review_diff`:

```python
    async def answer_clarification(
        self,
        question: str,
        task_description: str,
        plan_text: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Attempt to answer a blocked worker's question from task/plan context."""
        prompt = CLARIFICATION_PROMPT_TEMPLATE.format(
            question=question,
            task_description=task_description,
            plan_text=(plan_text or "(no plan text was provided)"),
        )
        router: LLMRouter | None = getattr(self, "_router", None)
        if router is not None:
            raw = await router.run("answer_clarification", prompt, project_id)
        else:
            raw = await self._run_claude(prompt, model, effort)
        return self._extract_json(raw)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opus_bridge.py -k answer_clarification -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/llm_router.py src/orchestrator/core/opus_bridge.py tests/test_opus_bridge.py
git commit -m "feat: brain answer_clarification call-site"
```

---

### Task 7: Orchestrator handles clarifying tasks each loop pass

**Files:**
- Modify: `src/orchestrator/core/orchestrator_review.py` (add `handle_clarification`)
- Modify: `src/orchestrator/core/orchestrator.py` (scan NEEDS_CLARIFICATION in the loop)
- Test: `tests/test_orchestrator_clarification.py`

**Depends on:** Task 3, Task 6

`handle_clarification` runs when a task is `NEEDS_CLARIFICATION` with `clarification_state == 'asked'`. It resolves the task's `plan_text` (same slug lookup `review_task` uses), asks the brain, and: on `resolved` with `confidence >= project["confidence_threshold"]` it calls `record_clarification_answer(..., state="answered_by_brain")` (re-queues for dispatch with the answer injected) and publishes `clarification_resolved`; otherwise it sets state `awaiting_human` and publishes `task_needs_clarification` for the dashboard/MCP. If Opus is unavailable it queues the action, mirroring `review_task`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_orchestrator_clarification.py
import pytest

from orchestrator.models.schemas import TaskStatus


@pytest.mark.asyncio
async def test_confident_answer_requeues_task(orchestrator, clarifying_task, project, monkeypatch):
    async def fake_answer(**kwargs):
        return {"resolved": True, "answer": "Use config/praxis.yaml", "confidence": 0.9}

    monkeypatch.setattr(orchestrator._opus, "answer_clarification", fake_answer)
    monkeypatch.setattr(orchestrator._opus, "is_available", lambda: _true())
    await orchestrator.handle_clarification(clarifying_task["id"], project)
    task = await orchestrator._tq.get_task(clarifying_task["id"])
    assert task["status"] == TaskStatus.PENDING
    assert task["clarification_state"] == "answered_by_brain"


@pytest.mark.asyncio
async def test_unresolved_answer_parks_for_human(orchestrator, clarifying_task, project, monkeypatch):
    async def fake_answer(**kwargs):
        return {"resolved": False, "answer": "Needs a human decision", "confidence": 0.2}

    monkeypatch.setattr(orchestrator._opus, "answer_clarification", fake_answer)
    monkeypatch.setattr(orchestrator._opus, "is_available", lambda: _true())
    events = []
    orchestrator._bus.subscribe(lambda e: events.append(e))
    await orchestrator.handle_clarification(clarifying_task["id"], project)
    task = await orchestrator._tq.get_task(clarifying_task["id"])
    assert task["status"] == TaskStatus.NEEDS_CLARIFICATION
    assert task["clarification_state"] == "awaiting_human"
    assert any(e["type"] == "task_needs_clarification" for e in events)


async def _true():
    return True
```

Build `clarifying_task` by seeding a task then calling `mark_needs_clarification`. Match the `orchestrator`/`project` fixtures used in `tests/test_orchestrator_review.py`. If `EventBus` has no `subscribe`, assert on state transitions only and drop the event assertion (check the actual `event_bus.py` API before writing).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_orchestrator_clarification.py -v`
Expected: FAIL with `AttributeError: 'Orchestrator' object has no attribute 'handle_clarification'`

- [ ] **Step 3a: Add the mixin method**

In `src/orchestrator/core/orchestrator_review.py`, add to `ReviewMixin`:

```python
    async def handle_clarification(
        self, task_id: str, project: dict[str, Any]
    ) -> None:
        """Answer a blocked worker's question, or park it for a human."""
        task = await self._tq.get_task(task_id)
        if task is None:
            return
        if (
            task["status"] != TaskStatus.NEEDS_CLARIFICATION
            or task.get("clarification_state") != "asked"
        ):
            return
        if not await self._opus.is_available():
            await self._opus.queue_action(
                {
                    "action": "clarify",
                    "task_id": task_id,
                    "project_id": project["id"],
                }
            )
            self._bus.publish({"type": "opus_queued", "action": "clarify"})
            return

        # Resolve this task's plan_text (same slug lookup as review_task).
        plan_text: str | None = None
        plan = await self._tq.get_plan(task["plan_id"])
        if plan is not None:
            slug_to_plan_task: dict[str, dict[str, Any]] = {}
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                raw = plan.get("opus_plan")
                if raw:
                    for pt in json.loads(raw).get("tasks", []):
                        if isinstance(pt, dict) and "slug" in pt:
                            slug_to_plan_task[pt["slug"]] = pt
            branch_name = task["branch_name"]
            slug = (
                branch_name[len("agent/") :]
                if branch_name.startswith("agent/")
                else branch_name
            )
            plan_text = slug_to_plan_task.get(slug, {}).get("plan_text")

        result = await self._opus.answer_clarification(
            question=task["clarification_question"] or "",
            task_description=task["description"] or task["title"],
            plan_text=plan_text,
            model=project.get("agent_model"),
            effort=project.get("agent_model_effort"),
            project_id=project["id"],
        )
        threshold = float(project.get("confidence_threshold") or 0.7)
        resolved = bool(result.get("resolved")) and (
            float(result.get("confidence") or 0.0) >= threshold
        )
        answer = str(result.get("answer", ""))
        if resolved:
            await self._tq.record_clarification_answer(
                task_id, answer, state="answered_by_brain"
            )
            self._bus.publish(
                {"type": "clarification_resolved", "task_id": task_id, "answer": answer}
            )
        else:
            await self._tq._db.execute(
                "UPDATE tasks SET clarification_state = 'awaiting_human' WHERE id = ?",
                (task_id,),
            )
            self._bus.publish(
                {
                    "type": "task_needs_clarification",
                    "task_id": task_id,
                    "question": task["clarification_question"],
                    "brain_note": answer,
                }
            )
```

- [ ] **Step 3b: Scan clarifying tasks in the loop**

In `src/orchestrator/core/orchestrator.py`, find where `process_plan_once` iterates tasks and calls `review_task` for `REVIEWING` tasks. Add an adjacent branch so `asked` clarifications are handled each pass. Locate the loop over `get_tasks_for_plan` (search for `TaskStatus.REVIEWING`) and add:

```python
            if (
                task["status"] == TaskStatus.NEEDS_CLARIFICATION
                and task.get("clarification_state") == "asked"
            ):
                await self.handle_clarification(task["id"], project)
                continue
```

Place it in the same task loop that already dispatches `review_task`. Verify the exact surrounding structure before editing — do not assume line numbers.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_orchestrator_clarification.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/orchestrator_review.py src/orchestrator/core/orchestrator.py tests/test_orchestrator_clarification.py
git commit -m "feat: orchestrator answers or escalates worker clarifications"
```

---

### Task 8: Human answer endpoint

**Files:**
- Modify: `src/orchestrator/api/tasks.py`
- Test: `tests/test_api_tasks.py`

**Depends on:** Task 3

`POST /api/tasks/{id}/clarify` with `{"answer": "..."}` lets a human resolve an `awaiting_human` task: it calls `record_clarification_answer(..., state="resolved")`, which re-queues the task for dispatch with the answer injected. Rejects tasks not in `NEEDS_CLARIFICATION`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_tasks.py  (add to existing file)
def test_clarify_endpoint_requeues_task(client, auth_headers, clarifying_task_awaiting_human):
    task_id = clarifying_task_awaiting_human["id"]
    resp = client.post(
        f"/api/tasks/{task_id}/clarify",
        headers=auth_headers,
        json={"answer": "Use the yaml loader in settings_file.py"},
    )
    assert resp.status_code == 200
    task = client.get(f"/api/tasks/{task_id}", headers=auth_headers).json()
    assert task["status"] == "pending"
    assert task["clarification_state"] == "resolved"


def test_clarify_rejects_non_clarifying_task(client, auth_headers, seeded_task):
    resp = client.post(
        f"/api/tasks/{seeded_task['id']}/clarify",
        headers=auth_headers,
        json={"answer": "x"},
    )
    assert resp.status_code == 409
```

Seed `clarifying_task_awaiting_human` by inserting a task, calling `mark_needs_clarification`, then setting `clarification_state='awaiting_human'`. Match the router/auth fixtures already in `tests/test_api_tasks.py`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_tasks.py -k clarify -v`
Expected: FAIL with 404 (route missing)

- [ ] **Step 3: Add the endpoint**

In `src/orchestrator/api/tasks.py`, following the existing task-router patterns (reuse its `TaskQueue` dependency and auth guard):

```python
from pydantic import BaseModel

from orchestrator.models.schemas import TaskStatus


class ClarifyRequest(BaseModel):
    answer: str


@router.post("/tasks/{task_id}/clarify")
async def clarify_task(
    task_id: str,
    body: ClarifyRequest,
    request: Request,
    _: None = Depends(require_auth),
) -> dict[str, str]:
    queue = request.app.state.task_queue
    task = await queue.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"] != TaskStatus.NEEDS_CLARIFICATION:
        raise HTTPException(status_code=409, detail="Task is not awaiting clarification")
    answer = body.answer.strip()
    if not answer:
        raise HTTPException(status_code=422, detail="answer must not be empty")
    await queue.record_clarification_answer(task_id, answer, state="resolved")
    return {"status": "requeued"}
```

Align imports (`Request`, `Depends`, `HTTPException`, the auth dependency name) with the top of the existing `tasks.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_api_tasks.py -k clarify -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/api/tasks.py tests/test_api_tasks.py
git commit -m "feat: POST /api/tasks/{id}/clarify human answer endpoint"
```

---

### Task 9: Expose clarification in TaskResponse and MCP poll_task

**Files:**
- Modify: `src/orchestrator/models/schemas.py:250-268`
- Modify: `src/mcp_server/server.py`
- Test: `tests/test_api_tasks.py`, `tests/mcp/test_server.py`

**Depends on:** Task 1, Task 2

`TaskResponse` surfaces the clarification fields so the dashboard and API consumers can read the question. MCP `poll_task` maps `NEEDS_CLARIFICATION` to a `status: awaiting_clarification` with the question, so a main-brain MCP client relays it (mirroring how `PASSED` maps to `awaiting_merge`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_api_tasks.py
def test_task_response_includes_clarification_fields(client, auth_headers, clarifying_task_awaiting_human):
    task = client.get(
        f"/api/tasks/{clarifying_task_awaiting_human['id']}", headers=auth_headers
    ).json()
    assert "clarification_question" in task
    assert "clarification_state" in task
```

```python
# tests/mcp/test_server.py
@pytest.mark.asyncio
async def test_poll_task_reports_awaiting_clarification(mcp_client, monkeypatch):
    async def fake_get_task(task_id):
        return {
            "id": task_id,
            "status": "needs_clarification",
            "clarification_question": "Which auth helper?",
            "pr_url": None,
        }

    monkeypatch.setattr(mcp_client._client, "get_task", fake_get_task)
    result = await mcp_client.poll_task("t1")
    assert result["status"] == "awaiting_clarification"
    assert "Which auth helper?" in result["question"]
```

Match the actual MCP test fixtures/entry points in `tests/mcp/`; if `poll_task` is a tool function rather than a client method, adapt the call to how the existing poll test invokes it.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_api_tasks.py -k clarification_fields tests/mcp/test_server.py -k awaiting_clarification -v`
Expected: FAIL — fields absent from `TaskResponse`; poll returns raw `needs_clarification`.

- [ ] **Step 3a: Extend TaskResponse**

In `src/orchestrator/models/schemas.py`, add to `class TaskResponse`:

```python
    clarification_question: str | None = None
    clarification_answer: str | None = None
    clarification_state: str | None = None
```

- [ ] **Step 3b: Map in MCP poll_task**

In `src/mcp_server/server.py`, find the `poll_task` status mapping (where `passed` becomes `awaiting_merge`) and add:

```python
    if task["status"] == "needs_clarification":
        return {
            "status": "awaiting_clarification",
            "question": task.get("clarification_question") or "",
            "task_id": task["id"],
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_api_tasks.py -k clarification_fields tests/mcp/test_server.py -k awaiting_clarification -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/models/schemas.py src/mcp_server/server.py tests/test_api_tasks.py tests/mcp/test_server.py
git commit -m "feat: surface clarification in TaskResponse and MCP poll_task"
```

---

### Task 10: Dashboard clarification banner + answer box

**Files:**
- Modify: `web/app.js`
- Modify: `web/index.html`
- Test: manual (no JS test harness in repo)

**Depends on:** Task 8, Task 9

The dashboard already reacts to SSE events (e.g. `task_awaiting_merge`) and polls task state. Add handling for `task_needs_clarification`: render a banner on the task showing the question and the brain's note, with a textarea + Submit that POSTs to `/api/tasks/{id}/clarify`. On `clarification_resolved`, clear the banner (the brain answered autonomously; no human action needed).

- [ ] **Step 1: Add an SSE handler + render function in `web/app.js`**

Find the existing SSE `switch (event.type)` / event dispatch (search for `task_awaiting_merge`). Add cases:

```javascript
    case "task_needs_clarification":
      renderClarification(data.task_id, data.question, data.brain_note);
      break;
    case "clarification_resolved":
      clearClarification(data.task_id);
      break;
```

Add the helpers (place them next to the existing merge-approval render helpers, reusing the same DOM lookup + fetch-with-auth pattern already in the file):

```javascript
function renderClarification(taskId, question, brainNote) {
  const el = document.getElementById(`task-${taskId}`);
  if (!el) return;
  const box = document.createElement("div");
  box.className = "clarification-banner";
  box.innerHTML = `
    <p class="clarification-q"><strong>Worker needs clarification:</strong> ${escapeHtml(question)}</p>
    ${brainNote ? `<p class="clarification-note">Brain could not resolve: ${escapeHtml(brainNote)}</p>` : ""}
    <textarea id="clarify-input-${taskId}" placeholder="Answer the worker's question..."></textarea>
    <button onclick="submitClarification('${taskId}')">Submit answer</button>
  `;
  el.appendChild(box);
}

async function submitClarification(taskId) {
  const answer = document.getElementById(`clarify-input-${taskId}`).value.trim();
  if (!answer) return;
  await apiFetch(`/api/tasks/${taskId}/clarify`, {
    method: "POST",
    body: JSON.stringify({ answer }),
  });
  clearClarification(taskId);
}

function clearClarification(taskId) {
  const el = document.getElementById(`task-${taskId}`);
  if (!el) return;
  el.querySelectorAll(".clarification-banner").forEach((n) => n.remove());
}
```

Reuse the existing helpers for `escapeHtml` and the authenticated fetch wrapper (`apiFetch` or equivalent — match the real names in `app.js`). Do not invent new auth plumbing.

- [ ] **Step 2: Add minimal styles in `web/styles.css`**

```css
.clarification-banner { border: 1px solid var(--warn, #d08700); border-radius: 6px; padding: 8px; margin-top: 6px; }
.clarification-banner textarea { width: 100%; min-height: 3em; }
```

- [ ] **Step 3: Manually verify against a running server**

Run the server (`uv run uvicorn orchestrator.main:app --host 127.0.0.1 --port 8080`), open the dashboard, and publish a `task_needs_clarification` event (or drive a real blocked run). Confirm the banner renders, Submit POSTs to `/clarify`, and the banner clears. Screenshot for the PR.

- [ ] **Step 4: Commit**

```bash
git add web/app.js web/index.html web/styles.css
git commit -m "feat: dashboard clarification banner and answer box"
```

---

### Task 11: Docs — CLAUDE.md gotcha + workflow note

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/workflow.md`

**Depends on:** Task 1-10

- [ ] **Step 1: Add a gotcha to `CLAUDE.md`**

Under the Gotchas section, add:

```markdown
- **Blocked workers ask, they don't guess** — the aider entrypoint parses the
  FINAL REPORT; a `Status: BLOCKED`/`NEEDS_CONTEXT` sends the `Concerns:` text as
  a `question` in the agent-done callback and opens NO PR. The task parks at
  `NEEDS_CLARIFICATION` (does NOT burn a retry). The loop asks the brain
  (`answer_clarification`, Sonnet/medium) to answer from task+plan_text; a
  confident answer (>= project `confidence_threshold`) re-dispatches with the Q&A
  injected via `progress_note` (→ Static Bible), otherwise the task parks
  `awaiting_human` (SSE `task_needs_clarification`, `POST /api/tasks/{id}/clarify`,
  MCP `poll_task` → `awaiting_clarification`). Only the **aider** harness parses
  the report today; opencode/openhands parity is a follow-up.
```

- [ ] **Step 2: Add a short section to `docs/workflow.md`** describing the clarification loop with the state transitions `IN_PROGRESS -> NEEDS_CLARIFICATION -> (brain answers) PENDING | (awaiting_human) -> PENDING`.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/workflow.md
git commit -m "docs: document worker clarification channel"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1, Task 2, Task 4, Task 6 (no dependencies — run in parallel)
- **Wave 2:** Task 3 (Task 1, 2), Task 5 (Task 1, 3, 4 — needs Task 3 from this wave, so see note), Task 9 (Task 1, 2)
- **Wave 3:** Task 7 (Task 3, 6), Task 8 (Task 3)
- **Wave 4:** Task 10 (Task 8, 9)
- **Wave 5:** Task 11 (all)

Note: Task 5 depends on Task 3; run Task 3 first within Wave 2 (or push Task 5 to Wave 3). If executing strictly by wave, treat Task 3 as Wave 2 and Task 5 as Wave 3 to honor the dependency.

---

## Notes & Follow-ups

- **Harness parity:** only `aider` parses the FINAL REPORT here. `opencode`/`openhands` entrypoints `git add -A && git commit` unconditionally and have no report-parse step; a follow-up plan should teach them the same `Status:`-parse + no-PR-on-block behavior, or the channel silently degrades to guess-or-fail on those harnesses.
- **Reconcile interaction:** a container that dies before sending the clarification callback still falls through to `reconcile_runs` → `failed` (unchanged). That is acceptable — a lost question is a lost run, retried like any other. No change to reconcile needed.
- **Loop-guard:** `record_clarification_answer` bumps `attempt`, so a worker that re-asks the same question is still bounded by `max_retries` on the *review* path once it produces a PR; a worker that blocks every single dispatch will re-ask up to the brain each time. If this proves chatty in practice, add a per-task clarification cap (e.g. 2) as a follow-up — deliberately omitted from v1 to keep the state machine small.
- **Confidence source:** the gate reuses the project `confidence_threshold` already used for improvement plans. No new setting introduced.
```
