# Praxis E2E Fix Batch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the 9 highest-value defects surfaced by the 2026-07-03 full E2E run (`data/e2e-fable-run-2026-07-03.md`) so Praxis moves from "impressive demo with sharp edges" to "dependable tool."

**Architecture:** Praxis is a FastAPI monolith (`src/orchestrator/`) that runs an async orchestration loop (`core/orchestrator.py` + mixins) driving Docker agent containers, plus an MCP stdio adapter (`src/mcp_server/`) and a no-build dashboard (`web/`). This batch touches the MCP client, the execute-plan endpoint + loop, the dispatch/review mixins, an agent entrypoint/image, and the dashboard. Each task owns a disjoint set of files so tasks in the same wave never edit the same file (avoids the concurrent-clobber hazard).

**Tech Stack:** Python 3.11, FastAPI, aiosqlite (raw SQL), pytest + pytest-asyncio (`asyncio_mode = "auto"`), httpx, Docker SDK, bash entrypoints, vanilla JS/CSS.

---

## Background — read before starting

This plan was authored WITH the full E2E report open and the codebase inspected live (server run + Playwright). Every root cause below was confirmed against real code and, for UI items, against a live 2560px-wide dashboard screenshot.

**The 9 fixes (report §9 "Bugs found" + FIX-SESSION HANDOFF), in priority order:**

1. **MCP `execute_plan` silently loses work.** `src/mcp_server/client.py:66` hardcodes `timeout=30.0`; `POST /api/execute-plan` runs a multi-minute synchronous `claude -p` decomposition and persists the plan LAST (`api/execute_plan.py`). The MCP client disconnects at 30s → uvicorn cancels the handler before persistence → the finished decomposition is discarded with no row and no log; the error surfaces as `connection_error` with an EMPTY message; a retry double-spends the brain. **Fix:** make the endpoint async-return (persist a PENDING plan row first, return `plan_id` immediately, decompose in the orchestration loop), and give the client a distinct `timeout` error code with a non-empty message.
2. **Callback-failed tasks are never auto-retried.** `api/internal.py:97` marks a failed-callback task `FAILED` unconditionally; only the review-fail path (`orchestrator_review.py`) consumes the retry budget. A single callback failure wedges every downstream task with no event, indefinitely. **Fix:** on a failure callback, consume the retry budget (re-dispatch) exactly like the gate-fail path.
3. **`review_feedback` (incl. verify-gate output) never reaches retries.** `orchestrator_dispatch._build_worker_bible` folds only `progress_note` into the handover; the stored `review_feedback` is never surfaced, so every retry is a blind re-roll. `progress_note` is the proven-working channel (task 1 attempt 6). **Fix:** thread `task["review_feedback"]` into the Static Bible as a floor section.
4. **Commit the uncommitted entrypoint fixes + fix the empty-`AGENTS.md` leak.** `docker/opencode-agent/entrypoint.sh` has live-proven working-tree edits (commits-ahead-of-base detection for self-committing workers; force-push + `gh pr view` PR reuse on retries) that are NOT committed. Separately, the Bible-strip leaves an empty `AGENTS.md` in the PR because the emptiness guard (`[ ! -s ]`) misses a whitespace-only file.
5. **No dev tooling in the opencode worker image.** `docker/opencode-agent/Dockerfile` ships no ruff/mypy/pytest/uv, so the worker cannot self-lint what the verify gate lints → deterministic gate failures on trivial lint debt. **Fix:** install `uv` in the image.
6. **Plan completion is silent.** `orchestrator_review.on_plan_completed` drafts a context sync but opens no integration PR and emits no "epic ready to integrate" signal. **Fix:** emit a `plan_integration_ready` SSE event (with the plan branch + a compare URL) and best-effort open a plan-branch→default-branch PR.
7. **Orchestration-guide resource omits `awaiting_merge`.** `src/mcp_server/resources/orchestration_guide.md` documents `passed -> merged` and calls `passed` "usually transient", contradicting the real merge-gate default. **Fix:** document the `awaiting_merge` human-handoff state.
8. **Dashboard JS defects** (`web/app.js`): raw ANSI escapes rendered in logs; "Opus resuming" pill while opus is `available`; stale live-log phase text ("pushing branch") persists on merged cards; anonymous "COMPLETED" rows with no plan name (the `plans.spec` column was dropped in Spec 2, so `plan.spec` is now `undefined`).
9. **Dashboard CSS defects** (`web/styles.css`): lane cards clip past the right edge with a stray full-width horizontal scrollbar at wide viewports (the per-lane `overflow-x:auto` row is fragile — confirmed: 11 cards nearly exactly fill 2560px, lane 2 overflows by 1px); Live Activity prose text is black-on-black in the LIGHT theme (`.log-line-prose{color:var(--text)}` = `#171717` on `.side-panel-log{background:var(--log-bg)}` = `#111312`).

**Out of scope (deferred, do NOT do here):** containerizing the orchestrator as the default dev story (report item 12 — its own ops task); decomposition over-splitting (report §2 — a brain-prompt tuning task); a `poll_plan` MCP convenience tool; the pcllm gateway flakiness (infra, not Praxis).

**Conventions to follow (from CLAUDE.md):**
- Run everything with `uv run ...`. Tests: `uv run pytest ...`. Format: `uv run ruff format src/ tests/`. Lint: `uv run ruff check --fix src/ tests/`. Types: `uv run mypy src/orchestrator/ --ignore-missing-imports`.
- Tests use `asyncio_mode = "auto"` — `async def test_*` runs directly, no decorator needed.
- Tests patch module-level helpers on the MIXIN module that calls them (e.g. `orchestrator_review`, `orchestrator_dispatch`), NOT on `core.orchestrator`.
- No `print()` in product code — use `logging`. Python 3.11 unions (`X | Y`), built-in generics.
- **Never use em dashes in prose/commits** (user rule): use commas/colons/semicolons.
- Commit after each task. Do NOT push unless asked. Do NOT touch `data/e2e-*` artifacts or `data/orchestrator.db`.

**Existing test layout:** `tests/test_api_execute_plan.py`, `tests/test_api_internal.py`, `tests/test_orchestrator*.py`, `tests/test_worker_bible.py`, `tests/test_mcp_*` (check exact names with `ls tests/` before adding). Add new test files where a matching one does not already exist.

---

## File Structure

| File | Responsibility | Owned by |
|------|----------------|----------|
| `src/mcp_server/client.py` | Distinct `timeout` error code + non-empty message; raise default timeout | Task 1 |
| `src/orchestrator/api/execute_plan.py` | Async-return: persist PENDING plan, decompose in loop | Task 2 |
| `src/orchestrator/core/orchestrator.py` | Loop branch that decomposes pending execute-plan plans | Task 2 |
| `src/orchestrator/core/execute_plan_decompose.py` (new) | Pure decomposition helper reused by endpoint + loop | Task 2 |
| `src/orchestrator/database.py` | Migration 2: `plans.pending_input` column | Task 2 |
| `src/orchestrator/models/schemas.py` | `ExecutePlanResponse` async shape | Task 2 |
| `src/orchestrator/api/internal.py` | Failure callback consumes retry budget | Task 3 |
| `src/orchestrator/core/worker_bible.py` | `review_feedback` floor section | Task 4 |
| `src/orchestrator/core/orchestrator_dispatch.py` | Pass `task["review_feedback"]` into the Bible | Task 4 |
| `src/orchestrator/core/orchestrator_review.py` | `on_plan_completed` emits integration signal + PR | Task 5 |
| `src/orchestrator/core/git_ops.py` | `open_integration_pr` helper (best-effort) | Task 5 |
| `src/mcp_server/resources/orchestration_guide.md` | Document `awaiting_merge` | Task 6 |
| `docker/opencode-agent/entrypoint.sh` | Commit staged fixes + empty-AGENTS.md guard | Task 7 |
| `docker/opencode-agent/Dockerfile` | Install `uv` | Task 7 |
| `web/app.js` | ANSI strip, Opus pill, stale phase, COMPLETED name | Task 8 |
| `web/styles.css` | Lane wrap, log contrast | Task 9 |

No two tasks in the same wave share a file.

---

### Task 1: MCP client distinct timeout error + non-empty message

**Files:**
- Modify: `src/mcp_server/client.py:60-76`
- Test: `tests/test_mcp_client.py` (create if absent; otherwise append)

**Depends on:** None

**Context:** `httpx.ReadTimeout` is a subclass of `httpx.HTTPError`, so it is currently swallowed by the generic `except httpx.HTTPError` branch and mislabeled `connection_error` with an empty message (`str(ReadTimeout())` is `""`). We add an explicit `httpx.TimeoutException` branch BEFORE the generic one, with a distinct code and actionable text. We also raise the default per-request timeout so short blips do not trip it (the real cure for execute-plan is Task 2's async return, but this hardens the client generally).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mcp_client.py`:

```python
import httpx
import pytest

from mcp_server.client import PraxisClient, PraxisClientError


class _TimeoutTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)


async def test_read_timeout_maps_to_timeout_code_with_message():
    client = PraxisClient(
        base_url="http://test", token="t", transport=_TimeoutTransport()
    )
    with pytest.raises(PraxisClientError) as excinfo:
        await client.get("/api/status")
    assert excinfo.value.code == "timeout"
    assert excinfo.value.message  # non-empty, actionable
    assert "timed out" in excinfo.value.message.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_client.py::test_read_timeout_maps_to_timeout_code_with_message -v`
Expected: FAIL (currently raises code `connection_error` with an empty message).

- [ ] **Step 3: Add the explicit timeout branch and raise the default timeout**

In `src/mcp_server/client.py`, change the `timeout=30.0` on line 66 to `timeout=120.0`, and insert a `TimeoutException` branch BEFORE the existing `except httpx.HTTPError`:

```python
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=120.0
            ) as client:
                response = await client.request(
                    method, url, headers=self._headers(), json=json
                )
        except httpx.ConnectError as exc:
            msg = f"Cannot reach Praxis at {self.base_url}. Is the server running?"
            raise PraxisClientError("connection_error", msg) from exc  # noqa: EM101
        except httpx.TimeoutException as exc:
            msg = (
                f"Praxis at {self.base_url} did not respond within the client "
                f"timeout ({method} {path}). The request may still be running "
                f"server-side; poll for its result rather than retrying."
            )
            raise PraxisClientError("timeout", msg) from exc  # noqa: EM101
        except httpx.HTTPError as exc:
            msg = f"HTTP transport error: {exc}"
            raise PraxisClientError("connection_error", msg) from exc  # noqa: EM101
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_client.py -v`
Expected: PASS. Then `uv run ruff check src/mcp_server/client.py` and `uv run mypy src/orchestrator/ --ignore-missing-imports` (client is under mcp_server; also run `uv run mypy src/mcp_server/ --ignore-missing-imports`).

- [ ] **Step 5: Commit**

```bash
git add src/mcp_server/client.py tests/test_mcp_client.py
git commit -m "fix(mcp): distinct timeout error code with actionable message"
```

---

### Task 2: Async execute-plan (persist pending plan, decompose in loop)

**Files:**
- Create: `src/orchestrator/core/execute_plan_decompose.py`
- Modify: `src/orchestrator/api/execute_plan.py`
- Modify: `src/orchestrator/core/orchestrator.py:97-122` (`process_plan_once`)
- Modify: `src/orchestrator/database.py:148-150` (add Migration 2)
- Modify: `src/orchestrator/models/schemas.py:474-485` (`ExecutePlanResponse`)
- Modify: `src/mcp_server/server.py:201-231` (`execute_plan` tool docstring only)
- Test: `tests/test_api_execute_plan.py`, `tests/test_execute_plan_decompose.py` (new)

**Depends on:** None

**Context:** This is the headline fix. Today `execute_plan` does project-create → brain decompose (multi-minute) → `create_plan` + `activate_plan`, all in one request coroutine with persistence LAST. We split it: the endpoint creates the project, persists a PENDING plan row carrying the raw decomposition inputs in a new `plans.pending_input` JSON column, and returns `plan_id` immediately. The orchestration loop then runs the decomposition (the same pure helper) and activates the plan. `poll_task`/dashboard track it once tasks exist.

The decomposition logic (build prompt → `llm_router.run("plan_review", ...)` → `parse_review_response` → `_normalize_slugs` → scrub caller context) is extracted verbatim into `execute_plan_decompose.decompose_plan()` so the endpoint and loop share ONE implementation.

- [ ] **Step 1: Write the failing test for the pure decomposition helper**

Create `tests/test_execute_plan_decompose.py`:

```python
from typing import Any

from orchestrator.core.execute_plan_decompose import decompose_plan


class _FakeRouter:
    def __init__(self, raw: str) -> None:
        self._raw = raw
        self.calls: list[tuple[str, str]] = []

    async def run(self, call_site: str, prompt: str, project_id: Any = None) -> str:
        self.calls.append((call_site, prompt))
        return self._raw


class _FakeProfile:
    context_window = 8192


class _FakeEffective:
    async def capability_profile(self, project_id: Any, model: str) -> _FakeProfile:
        return _FakeProfile()


async def test_decompose_plan_returns_normalized_opus_plan():
    raw = (
        '{"plan_slug":"demo","tasks":[{"id":"t1","title":"A","description":"d",'
        '"depends_on":[]},{"id":"t2","title":"B","description":"d",'
        '"depends_on":["t1"]}]}'
    )
    router = _FakeRouter(raw)
    opus_plan = await decompose_plan(
        plan="build a thing",
        model="qwen3.6-27b",
        context="some ctx",
        router=router,
        effective_settings=_FakeEffective(),
        project_id="p1",
    )
    tasks = opus_plan["tasks"]
    assert all("slug" in t for t in tasks)
    # depends_on remapped from brain id "t1" to the first task's slug.
    assert tasks[1]["depends_on"] == [tasks[0]["slug"]]
    assert router.calls and router.calls[0][0] == "plan_review"
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/test_execute_plan_decompose.py -v`
Expected: FAIL with `ModuleNotFoundError: orchestrator.core.execute_plan_decompose`.

- [ ] **Step 3: Create the pure decomposition helper**

Create `src/orchestrator/core/execute_plan_decompose.py`:

```python
"""Shared brain-driven decomposition of an externally-authored plan.

Extracted from api/execute_plan.py so the endpoint (fast path) and the
orchestration loop (async path) run one identical implementation.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from orchestrator.core.capability_history import summarize_outcomes
from orchestrator.core.context_scrub import scrub_context
from orchestrator.core.plan_derive import slugify
from orchestrator.core.plan_review import (
    build_review_prompt,
    parse_review_response,
)


# Fraction of the model's context window reserved for a single leaf's context.
_LEAF_BUDGET_FRACTION = 0.4


def normalize_slugs(opus_plan: dict[str, Any]) -> None:
    """Add a unique ``slug`` to each task and remap ``depends_on`` ids -> slugs.

    The plan-review brain emits tasks keyed by ``id`` (e.g. "t1") with
    ``depends_on`` referencing those ids, but TaskQueue.activate_plan and
    get_dispatchable_tasks key on ``slug``. Without this bridge the dispatch
    loop raises ``KeyError: 'slug'``.
    """
    id_to_slug: dict[str, str] = {}
    seen: set[str] = set()
    for task in opus_plan["tasks"]:
        slug = slugify(str(task.get("title") or task.get("id") or "task"))
        while slug in seen:
            slug = f"{slug}-{uuid.uuid4().hex[:4]}"
        seen.add(slug)
        task["slug"] = slug
        if "id" in task:
            id_to_slug[str(task["id"])] = slug
    for task in opus_plan["tasks"]:
        deps = task.get("depends_on") or []
        task["depends_on"] = [id_to_slug.get(str(d), str(d)) for d in deps]


def branch_slug(text: str) -> str:
    """Build a short branch-safe slug from free text plus a uniqueness suffix."""
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "plan"
    return f"{base}-{uuid.uuid4().hex[:6]}"


async def decompose_plan(
    plan: str,
    model: str,
    context: str | None,
    router: Any,
    effective_settings: Any,
    project_id: str | None,
) -> dict[str, Any]:
    """Capability-review a plan into a normalized opus_plan task graph.

    Raises:
        PlanReviewError: If the brain output cannot be parsed.
    """
    profile = await effective_settings.capability_profile(
        project_id=None, model=model
    )
    per_leaf_budget = int(profile.context_window * _LEAF_BUDGET_FRACTION)
    history = summarize_outcomes([])
    prompt = build_review_prompt(plan, profile, history, per_leaf_budget)

    raw = await router.run("plan_review", prompt, project_id=project_id)
    opus_plan = parse_review_response(raw)
    normalize_slugs(opus_plan)

    scrubbed_context = scrub_context(context)
    if scrubbed_context is not None:
        for task in opus_plan["tasks"]:
            task.setdefault("context_text", scrubbed_context)
    return opus_plan
```

- [ ] **Step 4: Run the helper test to green**

Run: `uv run pytest tests/test_execute_plan_decompose.py -v`
Expected: PASS.

- [ ] **Step 5: Add Migration 2 for `plans.pending_input`**

In `src/orchestrator/database.py`, after `_migration_0001_baseline` (before the `MIGRATIONS` list at line 148), add:

```python
async def _migration_0002_pending_input(connection: aiosqlite.Connection) -> None:
    """Add plans.pending_input: raw decomposition inputs for async execute-plan."""
    cursor = await connection.execute("PRAGMA table_info(plans)")
    cols = {row[1] for row in await cursor.fetchall()}
    if "pending_input" not in cols:
        await connection.execute("ALTER TABLE plans ADD COLUMN pending_input TEXT")
```

Then extend the `MIGRATIONS` list:

```python
MIGRATIONS: list[Migration] = [
    Migration(1, "baseline: schema as of 2026-07-02", _migration_0001_baseline),
    Migration(2, "add plans.pending_input for async execute-plan", _migration_0002_pending_input),
]
```

- [ ] **Step 6: Write the failing endpoint test (async return shape)**

In `tests/test_api_execute_plan.py`, add a test asserting the endpoint returns immediately WITHOUT calling the brain, and persists a pending plan. Use the existing app fixtures in that file as a model (inspect the file first for the client/db fixture names). Skeleton:

```python
async def test_execute_plan_returns_pending_without_calling_brain(client, app_state):
    # app_state.llm_router must be a mock/spy that records calls.
    resp = client.post(
        "/api/execute-plan",
        json={
            "repo_url": "https://github.com/x/y",
            "plan": "do the thing",
            "model": "qwen3.6-27b",
        },
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["plan_id"]
    assert body["status"] == "decomposing"
    # The brain must NOT be called in the request path anymore.
    assert app_state.llm_router.run.await_count == 0
    plan = await app_state.db.fetch_one(
        "SELECT status, pending_input, opus_plan FROM plans WHERE id = ?",
        (body["plan_id"],),
    )
    assert plan["status"] == "pending"
    assert plan["pending_input"] is not None
    assert plan["opus_plan"] is None
```

Match the fixture names actually used in `tests/test_api_execute_plan.py` (they may already provide a mocked `llm_router` and `activate_plan`). If the existing tests mock `activate_plan`, keep those tests working by updating them to the new async flow (they should now assert the pending-plan persistence, not activation).

- [ ] **Step 7: Run it to confirm it fails**

Run: `uv run pytest tests/test_api_execute_plan.py -v`
Expected: FAIL (endpoint still decomposes synchronously and returns `leaves`/`blocked`).

- [ ] **Step 8: Rewrite the endpoint to async-return**

Replace the body of `execute_plan` in `src/orchestrator/api/execute_plan.py` so it persists a pending plan and returns immediately. Keep `_create_or_reuse_project`; remove the now-duplicated `_slugify`/`_normalize_slugs`/decompose logic (they live in `execute_plan_decompose`). New handler:

```python
import json

from orchestrator.core.execute_plan_decompose import branch_slug
from orchestrator.core.harnesses import default_harness_id
from orchestrator.models.schemas import ExecutePlanRequest, ExecutePlanResponse


@router.post(
    "/execute-plan",
    status_code=status.HTTP_201_CREATED,
    response_model=ExecutePlanResponse,
)
async def execute_plan(request: Request, body: ExecutePlanRequest) -> dict[str, Any]:
    """Persist an external plan for async, loop-driven capability decomposition.

    Returns immediately with a plan_id; the orchestration loop runs the
    (multi-minute) brain decomposition and activates the task graph. This
    avoids the old failure mode where an MCP client timeout cancelled the
    request coroutine and silently discarded a completed decomposition.
    """
    state = request.app.state
    db = state.db
    queue = state.task_queue
    settings = state.settings

    harness = body.harness or default_harness_id()
    project_id = await _create_or_reuse_project(
        db, body.repo_url, None, body.model, harness
    )
    branch_name = body.branch or f"plan/execute-{branch_slug(body.plan)}"
    pending_input = json.dumps(
        {
            "plan": body.plan,
            "model": body.model,
            "context": body.context,
            "branch": branch_name,
        }
    )
    plan_id = await queue.create_pending_execute_plan(project_id, pending_input)

    base_url = f"http://localhost:{getattr(settings, 'port', 8080)}/"
    return {
        "plan_id": plan_id,
        "project_id": project_id,
        "dashboard_url": base_url,
        "status": "decomposing",
    }
```

Add `create_pending_execute_plan` to `src/orchestrator/core/task_queue.py` (near `create_plan`):

```python
    async def create_pending_execute_plan(
        self, project_id: str, pending_input: str
    ) -> str:
        """Persist a PENDING execute-plan whose decomposition runs in the loop."""
        plan_id = str(uuid.uuid4())
        await self._db.execute(
            """INSERT INTO plans (id, project_id, source, status, pending_input)
               VALUES (?, ?, 'execute-plan', 'pending', ?)""",
            (plan_id, project_id, pending_input),
        )
        logger.info("Created pending execute-plan %s for project %s", plan_id, project_id)
        return plan_id
```

- [ ] **Step 9: Update `ExecutePlanResponse`**

In `src/orchestrator/models/schemas.py`, replace the `leaves`/`blocked` fields with a `status` field:

```python
class ExecutePlanResponse(BaseModel):
    """Response for an accepted execute-plan.

    Decomposition runs asynchronously in the orchestration loop; poll the
    plan's tasks (or watch the dashboard) once they appear. ``status`` is
    ``"decomposing"`` until the loop activates the task graph.
    """

    plan_id: str
    project_id: str
    dashboard_url: str
    status: str = "decomposing"
```

- [ ] **Step 10: Add the loop decomposition branch**

In `src/orchestrator/core/orchestrator.py`, add a method and wire it into `process_plan_once`. Insert this branch at the TOP of `process_plan_once` (right after the `plan is None` guard, before the existing autonomous/PENDING checks):

```python
        if (
            plan["status"] == PlanStatus.PENDING
            and plan["source"] == "execute-plan"
            and plan["opus_plan"] is None
        ):
            await self.decompose_pending_execute_plan(plan_id, project)
            return
```

Add the method to the `Orchestrator` class (needs `import json` at top — it is already imported? confirm; add if missing):

```python
    async def decompose_pending_execute_plan(
        self, plan_id: str, project: dict[str, Any]
    ) -> None:
        """Run the brain decomposition for a pending execute-plan, then activate."""
        from orchestrator.core.execute_plan_decompose import decompose_plan
        from orchestrator.core.plan_review import PlanReviewError

        plan = await self._tq.get_plan(plan_id)
        if plan is None or not plan.get("pending_input"):
            return
        if self._opus is not None and not await self._opus.is_available():
            await self._opus.queue_action(
                {"action": "execute_plan", "plan_id": plan_id, "project_id": project["id"]}
            )
            self._bus.publish({"type": "opus_queued", "action": "execute_plan"})
            return

        payload = json.loads(plan["pending_input"])
        router = getattr(self, "_llm_router", None) or self._opus._router  # see note
        try:
            opus_plan = await decompose_plan(
                plan=payload["plan"],
                model=payload["model"],
                context=payload.get("context"),
                router=router,
                effective_settings=self._effective_settings,
                project_id=project["id"],
            )
        except PlanReviewError as exc:
            await self._tq.update_plan_status(plan_id, PlanStatus.FAILED)
            self._bus.publish(
                {"type": "plan_failed", "plan_id": plan_id, "reason": str(exc)}
            )
            return

        await self._tq.activate_plan(plan_id, opus_plan, payload["branch"])
        self._bus.publish(
            {
                "type": "plan_activated",
                "plan_id": plan_id,
                "branch": payload["branch"],
                "task_count": len(opus_plan["tasks"]),
            }
        )
```

**Note on `router` (VERIFIED against the codebase):** `app.state.llm_router` exists (`main.py:104`) and `app.state.event_bus` exists (`main.py:114`), but `Orchestrator(...)` at `main.py:124` does NOT currently receive the router. Add a `llm_router: Any = None` parameter to `Orchestrator.__init__` (store `self._llm_router = llm_router`) and pass `llm_router=app.state.llm_router` at `main.py:124`. Then use `self._llm_router` directly (drop the `getattr` fallback shown above).

**Note on `PlanStatus.FAILED` (VERIFIED):** the enum in `schemas.py` currently has `PENDING`, `ACTIVE`, `COMPLETED`, `REJECTED` — there is NO `FAILED`. Add `FAILED = "failed"` to `class PlanStatus` (a decomposition failure is not a user rejection). Use `PlanStatus.FAILED` in `decompose_pending_execute_plan`.

- [ ] **Step 11: Update the MCP tool docstring**

In `src/mcp_server/server.py`, update the `execute_plan` tool docstring (lines ~210-221) to state the async contract:

```python
    """Execute a full, externally-authored implementation plan on a repo.

    Praxis accepts the plan and returns immediately with {plan_id, project_id,
    dashboard_url, status="decomposing"}. Decomposition (a multi-minute brain
    call) then runs asynchronously in the orchestration loop; the task graph
    and per-task PRs appear shortly after. Watch the dashboard_url, or poll the
    plan's tasks as they are created. Pass the FULL plan text. Use this (not
    dispatch_task) when you already have a multi-step plan.
    """
```

- [ ] **Step 12: Run the full suite for this task**

Run: `uv run pytest tests/test_api_execute_plan.py tests/test_execute_plan_decompose.py tests/test_orchestrator.py tests/test_database.py -v`
Expected: PASS. Fix any existing execute-plan tests that assumed synchronous decomposition (update them to the async contract). Then `uv run ruff check --fix src/ tests/` and `uv run mypy src/orchestrator/ --ignore-missing-imports`.

- [ ] **Step 13: Commit**

```bash
git add src/orchestrator/api/execute_plan.py src/orchestrator/core/execute_plan_decompose.py src/orchestrator/core/orchestrator.py src/orchestrator/core/task_queue.py src/orchestrator/database.py src/orchestrator/models/schemas.py src/mcp_server/server.py tests/test_api_execute_plan.py tests/test_execute_plan_decompose.py
git commit -m "fix(execute-plan): async-return and decompose in the loop to stop silent work loss"
```

---

### Task 3: Failure callbacks consume the retry budget

**Files:**
- Modify: `src/orchestrator/api/internal.py:89-100`
- Test: `tests/test_api_internal.py`

**Depends on:** None

**Context:** On a `failed` status callback, `agent_done` calls `update_task_status(FAILED)` and stops. We change the else-branch to mirror the review-fail retry logic: resolve the task's plan → project, and if `attempt < max_retries`, call `retry_task` (which sets PENDING and increments `attempt`) and publish `task_retry`; otherwise `fail_task` and publish `task_failed`. This unwedges the plan that a single lost/failed callback would otherwise freeze forever.

- [ ] **Step 1: Write the failing test**

In `tests/test_api_internal.py` (inspect it for the existing fixtures/mocks first), add:

```python
async def test_failed_callback_retries_when_budget_remains(client, app_state):
    # Arrange: a task on attempt 1, project max_retries=3.
    # (Use the file's existing helpers to seed a plan/task/run.)
    task_id, run_id = await _seed_in_progress_task(app_state, attempt=1, max_retries=3)

    resp = client.post(
        "/api/internal/agent-done",
        json={"task_id": task_id, "run_id": run_id, "status": "failed"},
        headers={"X-Praxis-Callback-Token": app_state.internal_callback_secret},
    )
    assert resp.status_code == 200
    task = await app_state.task_queue.get_task(task_id)
    assert task["status"] == "pending"      # re-queued, not terminal
    assert task["attempt"] == 2             # retry budget consumed


async def test_failed_callback_marks_failed_when_budget_exhausted(client, app_state):
    task_id, run_id = await _seed_in_progress_task(app_state, attempt=3, max_retries=3)
    resp = client.post(
        "/api/internal/agent-done",
        json={"task_id": task_id, "run_id": run_id, "status": "failed"},
        headers={"X-Praxis-Callback-Token": app_state.internal_callback_secret},
    )
    assert resp.status_code == 200
    task = await app_state.task_queue.get_task(task_id)
    assert task["status"] == "failed"
```

Write the `_seed_in_progress_task` helper (or reuse existing seeding helpers in the file). It must insert a project with `max_retries`, a plan, a task in `in_progress`, and an agent run.

- [ ] **Step 2: Run to confirm failure**

Run: `uv run pytest tests/test_api_internal.py -k failed_callback -v`
Expected: FAIL (task goes straight to `failed`, attempt stays 1).

- [ ] **Step 3: Implement the retry branch**

In `src/orchestrator/api/internal.py`, replace the terminal `else` branch (lines ~96-100) with retry-aware logic:

```python
    else:
        # A failure callback should consume the retry budget exactly like a
        # review/gate failure; otherwise one lost callback wedges the plan.
        plan = await queue.get_plan(task["plan_id"])
        project = await queue.get_project(plan["project_id"]) if plan else None
        max_retries = int(project["max_retries"]) if project else 0
        feedback = body.question or f"Agent finished with status {body.status}"
        if int(task["attempt"]) < max_retries:
            await queue.retry_task(body.task_id)
            request.app.state.event_bus.publish(
                {
                    "type": "task_retry",
                    "task_id": body.task_id,
                    "attempt": int(task["attempt"]) + 1,
                }
            )
            logger.info("Task %s failed callback; retrying", body.task_id)
        else:
            await queue.fail_task(body.task_id, feedback)
            request.app.state.event_bus.publish(
                {"type": "task_failed", "task_id": body.task_id, "feedback": feedback}
            )
            logger.warning(
                "Task %s agent finished with status %s; retries exhausted",
                body.task_id,
                body.status,
            )
```

Confirm `request.app.state.event_bus` is the correct handle (grep `event_bus` in `api/` for the existing access pattern; if the app stores it elsewhere, use that). If `retry_task`/`fail_task` are not already imported via `queue`, they are methods on `queue` (TaskQueue) so no import is needed.

- [ ] **Step 4: Run tests to green**

Run: `uv run pytest tests/test_api_internal.py -v`
Expected: PASS. Then ruff + mypy as above.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/api/internal.py tests/test_api_internal.py
git commit -m "fix(callback): failed callbacks consume the retry budget instead of wedging the plan"
```

---

### Task 4: Thread review_feedback into worker retries

**Files:**
- Modify: `src/orchestrator/core/worker_bible.py`
- Modify: `src/orchestrator/core/orchestrator_dispatch.py:143-190`
- Test: `tests/test_worker_bible.py`

**Depends on:** None

**Context:** `mark_passed`/`fail_task` store `review_feedback` on the task (incl. the verify-gate command output). The dispatcher already fetches the full `task` dict, so the feedback is in hand at `_build_worker_bible` time. We add a `review_feedback` source to `BibleSources` as a high-priority FLOOR section ("# PREVIOUS ATTEMPT FEEDBACK") so the worker sees exactly what the gate/reviewer objected to. This is the same always-resent slot that made the `progress_note` handover work.

- [ ] **Step 1: Write the failing test**

In `tests/test_worker_bible.py`, add:

```python
from orchestrator.core.worker_bible import BibleSources, build_bible


def test_bible_includes_review_feedback_as_floor_section():
    src = BibleSources(
        goal="do it",
        handover="# PROGRESS",
        context_window=8192,
        review_feedback="ruff F401: 'Awaitable' imported but unused",
    )
    out = build_bible(src)
    assert "PREVIOUS ATTEMPT FEEDBACK" in out
    assert "F401" in out


def test_bible_omits_feedback_section_when_absent():
    src = BibleSources(goal="do it", handover="# PROGRESS", context_window=8192)
    out = build_bible(src)
    assert "PREVIOUS ATTEMPT FEEDBACK" not in out
```

- [ ] **Step 2: Run to confirm failure**

Run: `uv run pytest tests/test_worker_bible.py -k review_feedback -v`
Expected: FAIL (`BibleSources` has no `review_feedback`).

- [ ] **Step 3: Add the source and section**

In `src/orchestrator/core/worker_bible.py`, add the field to `BibleSources` (after `caller_context`):

```python
    caller_context: str | None = None
    review_feedback: str | None = None
    repo_memory: str | None = None
```

And in `build_bible`, add a floor section right after the `caller_context` block (priority 3, so it sits with the other floor sections and is never trimmed):

```python
    if src.review_feedback:
        raw_sections.append(
            Section(
                "feedback",
                "# PREVIOUS ATTEMPT FEEDBACK (fix these before anything else)\n"
                f"{src.review_feedback}",
                3,
                floor=True,
            )
        )
```

(The existing `caller` section is also priority 3; `fit_sections` orders by priority then insertion, both are `floor=True`, so both survive. Keep the `plan` section at 4 and `repo` at 9.)

- [ ] **Step 4: Pass the task's feedback from the dispatcher**

In `src/orchestrator/core/orchestrator_dispatch.py`, `_build_worker_bible` returns `build_bible(BibleSources(...))`. Add `review_feedback=task.get("review_feedback")` to that `BibleSources(...)` call:

```python
        return build_bible(
            BibleSources(
                goal=goal,
                handover=handover,
                context_window=context_window,
                plan_slice=plan_task.get("plan_text"),
                caller_context=plan_task.get("context_text"),
                review_feedback=task.get("review_feedback"),
                repo_memory=None,  # repo files folded in by entrypoint --read
            )
        )
```

- [ ] **Step 5: Run tests to green**

Run: `uv run pytest tests/test_worker_bible.py tests/test_orchestrator_dispatch.py -v` (use the actual dispatch test file name; check `ls tests/ | grep dispatch`).
Expected: PASS. Then ruff + mypy.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/core/worker_bible.py src/orchestrator/core/orchestrator_dispatch.py tests/test_worker_bible.py
git commit -m "fix(worker): inject review_feedback into the Static Bible so retries are directed, not blind"
```

---

### Task 5: Plan-completion integration signal

**Files:**
- Modify: `src/orchestrator/core/orchestrator_review.py:501-521` (`on_plan_completed`)
- Modify: `src/orchestrator/core/git_ops.py` (add `open_integration_pr`)
- Test: `tests/test_orchestrator_review.py` (use the actual file name for review-mixin tests)

**Depends on:** None

**Context:** When every task in a plan merges, the feature sits on the plan branch with no signal. We emit a `plan_integration_ready` SSE event carrying the plan branch and a GitHub compare URL, and best-effort open a plan-branch → default-branch PR. The PR open is wrapped so a failure (no token, gh missing) never interrupts completion, matching the existing defensive style of `_sync_plan_checkbox`.

- [ ] **Step 1: Write the failing test**

In the review-mixin test file, add a test that `on_plan_completed` publishes `plan_integration_ready` with the branch. Model it on existing `on_plan_completed` tests (which stub `_context_sync`). Skeleton:

```python
async def test_on_plan_completed_emits_integration_ready(orchestrator, seeded_plan):
    events = []
    orchestrator._bus.publish = lambda e: events.append(e)
    orchestrator._context_sync = _StubContextSync()  # returns {"draft_id": "d1"}
    await orchestrator.on_plan_completed(seeded_plan["id"])
    types = [e["type"] for e in events]
    assert "plan_integration_ready" in types
    evt = next(e for e in events if e["type"] == "plan_integration_ready")
    assert evt["plan_branch"] == seeded_plan["plan_branch_name"]
```

- [ ] **Step 2: Run to confirm failure**

Run: `uv run pytest tests/test_orchestrator_review.py -k integration_ready -v`
Expected: FAIL (no such event).

- [ ] **Step 3: Add the git helper**

In `src/orchestrator/core/git_ops.py`, add a best-effort integration-PR opener (match the module's existing function style; most helpers here shell out to `gh`/`git` via a subprocess wrapper — reuse whatever the file already uses, e.g. an internal `_run`):

```python
def compare_url(repo_url: str, base: str, head: str) -> str:
    """Build a GitHub compare URL for base...head (no network)."""
    slug = repo_url.rstrip("/").removesuffix(".git").split("github.com/")[-1]
    return f"https://github.com/{slug}/compare/{base}...{head}?expand=1"
```

If `git_ops` already exposes a PR-create primitive (grep for `gh pr create` / `create_pr`), add an `open_integration_pr(repo, base, head, token)` that reuses it and returns the PR URL or `None` on failure. If not, the event with `compare_url` alone satisfies the fix; opening the PR is optional. Keep the PR-open best-effort and swallow errors.

- [ ] **Step 4: Emit the event in `on_plan_completed`**

In `src/orchestrator/core/orchestrator_review.py`, at the end of `on_plan_completed` (after the `context_draft_ready` publish), add:

```python
        from orchestrator.core.git_ops import compare_url

        plan_branch = plan.get("plan_branch_name")
        if plan_branch:
            base = project.get("default_branch") or "main"
            self._bus.publish(
                {
                    "type": "plan_integration_ready",
                    "project_id": project["id"],
                    "plan_id": plan_id,
                    "plan_branch": plan_branch,
                    "base_branch": base,
                    "compare_url": compare_url(project["repo_url"], base, plan_branch),
                }
            )
```

- [ ] **Step 5: Run tests to green**

Run: `uv run pytest tests/test_orchestrator_review.py -v`
Expected: PASS. Then ruff + mypy.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/core/orchestrator_review.py src/orchestrator/core/git_ops.py tests/test_orchestrator_review.py
git commit -m "feat(plans): emit plan_integration_ready with a compare URL on plan completion"
```

---

### Task 6: Document awaiting_merge in the orchestration guide

**Files:**
- Modify: `src/mcp_server/resources/orchestration_guide.md`
- Test: `tests/test_mcp_server.py` (or the guide/resource test file)

**Depends on:** None

**Context:** Section 5 of the guide describes `passed -> merged` and calls `passed` "usually transient", contradicting the merge-gate default (a task parks at `awaiting_merge` with the PR OPEN for human approval). A blind caller reading only the guide expects auto-merge. Add the `awaiting_merge` state.

- [ ] **Step 1: Write the failing test**

In the MCP server test file, add:

```python
from mcp_server.server import load_orchestration_guide


def test_guide_documents_awaiting_merge():
    guide = load_orchestration_guide()
    assert "awaiting_merge" in guide
    # It must describe the human-approval handoff, not just name the state.
    assert "approve" in guide.lower()
```

- [ ] **Step 2: Run to confirm failure**

Run: `uv run pytest tests/test_mcp_server.py -k awaiting_merge -v`
Expected: FAIL.

- [ ] **Step 3: Edit the guide**

In `src/mcp_server/resources/orchestration_guide.md`, find the task-status section (the `passed -> merged` text) and replace/augment it so it reads (adapt to the surrounding prose):

```markdown
Task status flow:
`in_progress -> reviewing -> awaiting_merge -> merged`

- `reviewing`: Praxis ran its mechanical verify gate and brain review.
- `awaiting_merge`: the PR PASSED review and is parked OPEN for a human to
  approve and merge. `poll_task` returns `status="awaiting_merge"` with
  `verdict="pass"`, the full `review`, and `pr_url`. Relay the `pr_url` to the
  user; Praxis does NOT auto-merge by default (a project may opt in via
  `auto_merge`, which still never applies to protected branches).
- `merged`: the PR was merged (via human approval or an opted-in auto-merge).
- `awaiting_clarification`: the worker was blocked and asked a question; see
  the clarification flow.
```

- [ ] **Step 4: Run tests to green**

Run: `uv run pytest tests/test_mcp_server.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mcp_server/resources/orchestration_guide.md tests/test_mcp_server.py
git commit -m "docs(mcp): document the awaiting_merge human-handoff state in the orchestration guide"
```

---

### Task 7: Commit entrypoint fixes, fix empty-AGENTS.md, add uv to image

**Files:**
- Modify: `docker/opencode-agent/entrypoint.sh` (already has uncommitted diff; plus one new fix)
- Modify: `docker/opencode-agent/Dockerfile`
- Test: shell + Docker build (no pytest)

**Depends on:** None

**Context:** The working tree already contains two live-proven entrypoint fixes (commits-ahead-of-base detection; force-push + `gh pr view` PR reuse) that must be committed. Separately, the Bible-strip block (lines ~195-201) leaves an empty `AGENTS.md` in the PR because `[ ! -s "${WORKSPACE}/AGENTS.md" ]` treats a whitespace-only file as non-empty (report §"Task 4 PASSED", PR #4). The Dockerfile ships no dev toolchain, so the worker cannot self-lint (report root cause 1). Add `uv`.

- [ ] **Step 1: Confirm the uncommitted entrypoint diff is present**

Run: `git diff --stat docker/opencode-agent/entrypoint.sh`
Expected: shows ~21 insertions / ~5 deletions (the commits-ahead + force-push + PR-reuse block). If empty, the fixes were lost; re-apply them from `data/e2e-fable-run-2026-07-03.md` §"Retry 2" and §"Attempt 4" before proceeding.

- [ ] **Step 2: Fix the empty-AGENTS.md guard**

In `docker/opencode-agent/entrypoint.sh`, the strip block (around lines 195-201) currently is:

```bash
if [ -f "${WORKSPACE}/AGENTS.md" ] && grep -q "praxis:bible:start" "${WORKSPACE}/AGENTS.md"; then
    sed -i '/<!-- praxis:bible:start -->/,/<!-- praxis:bible:end -->/d' "${WORKSPACE}/AGENTS.md"
    sed -i '/./,$!d' "${WORKSPACE}/AGENTS.md"  # drop leading blank lines
    if ! git ls-files --error-unmatch AGENTS.md >/dev/null 2>&1 && [ ! -s "${WORKSPACE}/AGENTS.md" ]; then
        rm -f "${WORKSPACE}/AGENTS.md"
    fi
fi
```

Replace the emptiness check so a whitespace-only file counts as empty:

```bash
if [ -f "${WORKSPACE}/AGENTS.md" ] && grep -q "praxis:bible:start" "${WORKSPACE}/AGENTS.md"; then
    sed -i '/<!-- praxis:bible:start -->/,/<!-- praxis:bible:end -->/d' "${WORKSPACE}/AGENTS.md"
    sed -i '/./,$!d' "${WORKSPACE}/AGENTS.md"  # drop leading blank lines
    # Treat a whitespace-only file as empty ([ -s ] is true for a lone newline).
    if ! grep -q '[^[:space:]]' "${WORKSPACE}/AGENTS.md" 2>/dev/null; then
        if ! git ls-files --error-unmatch AGENTS.md >/dev/null 2>&1; then
            rm -f "${WORKSPACE}/AGENTS.md"          # we created it; drop entirely
        else
            git checkout -- AGENTS.md 2>/dev/null || true  # restore repo's own copy
        fi
    fi
fi
```

- [ ] **Step 3: Add uv to the Dockerfile**

In `docker/opencode-agent/Dockerfile`, after the `RUN npm install -g opencode-ai` line (line 18) and BEFORE the `USER agent` switch, add uv (installed to a system path so the non-root agent can use it):

```dockerfile
# Dev toolchain so the worker can self-lint/test what the verify gate lints
# (ruff/mypy/pytest via uv). Installed to /usr/local/bin (root-writable here).
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
```

(Placed before `USER agent` so it writes to `/usr/local/bin`. `curl` and `ca-certificates` are already installed in the first `RUN`.)

- [ ] **Step 4: Shell-lint the entrypoint**

Run: `docker run --rm -i koalaman/shellcheck - < docker/opencode-agent/entrypoint.sh` (or `shellcheck docker/opencode-agent/entrypoint.sh` if installed locally).
Expected: no NEW errors versus baseline. Warnings that predate this change are acceptable.

- [ ] **Step 5: Build the image**

Run: `docker build -t opencode-agent:latest -f docker/opencode-agent/Dockerfile docker/opencode-agent/`
Expected: exit 0. Then verify uv is present:
`docker run --rm --entrypoint uv opencode-agent:latest --version`
Expected: prints a `uv 0.x` version.

- [ ] **Step 6: Commit**

```bash
git add docker/opencode-agent/entrypoint.sh docker/opencode-agent/Dockerfile
git commit -m "fix(opencode-agent): commit self-commit/force-push fixes, drop empty AGENTS.md, add uv toolchain"
```

---

### Task 8: Dashboard JS fixes (ANSI, Opus pill, stale phase, COMPLETED name)

**Files:**
- Modify: `web/app.js`
- Test: manual (Playwright) — no JS unit harness in this repo

**Depends on:** None

**Context:** Four independent JS defects, all in `web/app.js`. This is a no-build classic script; verify by loading the dashboard. Root causes confirmed against the live dashboard:
- **ANSI:** `renderTaskCard` (line 1053) and `renderCuratedLog` (line 1153 raw mode; line 1184/1210 clean mode) render log text through `esc()` only, never stripping ANSI SGR sequences (`[..m`).
- **Opus pill:** `renderHealthBar` (lines 1007-1013) infers `opusStatus` from the agent DOT's CSS class, mapping `disconnected -> "resuming"`. The real status is `status.opus_state.status`. Confirmed live: opus_state was `available` yet the pill read "resuming".
- **Stale phase:** `dashboardTaskLogs[taskId]` (set at line 1398) is never cleared when a task reaches a terminal status, so the last phase line ("pushing branch") lingers on merged cards until a manual refresh.
- **Anonymous COMPLETED:** `renderCompletedLane` (line 1071) and `renderSwimLane` (line 1025) build `specPreview` from `esc(plan.spec)`, but `plans.spec` was dropped in Spec 2 so `plan.spec` is `undefined` → blank name.

- [ ] **Step 1: Add an ANSI-strip helper and apply it to log text**

Near the top of `web/app.js` (by the other helpers, e.g. after the `esc`/`jsArg` helpers — grep `function esc` for the spot), add:

```javascript
    // Strip ANSI SGR / CSI escape sequences (opencode CLI emits colored output).
    // eslint-disable-next-line no-control-regex
    const ANSI_RE = /[][[()#;?]*(?:[0-9]{1,4}(?:;[0-9]{0,4})*)?[0-9A-ORZcf-nqry=><]/g;
    function stripAnsi(s) { return String(s == null ? "" : s).replace(ANSI_RE, ""); }
```

Apply it wherever raw log text enters the DOM:
- Line 1053 (card log line): `const logLine = dashboardTaskLogs[task.id] ? '<div class="card-log-line">' + esc(stripAnsi(dashboardTaskLogs[task.id])) + '</div>' : "";`
- Line 1398 (store): `if (lines.length) dashboardTaskLogs[taskId] = stripAnsi(lines[lines.length - 1]);`
- Line 1408 (live update): `logLineEl.textContent = dashboardTaskLogs[taskId] || "";` (already stripped at store time — no change needed).
- In `renderCuratedLog` (line 1150): strip once at the top so BOTH raw and clean modes are clean: after `if (!rawText) return ...;` add `rawText = stripAnsi(rawText);`.

- [ ] **Step 2: Fix the Opus pill to use the real state**

Store the real opus status when `/api/status` is applied. At line ~1531 (`setConnection("agent", status.agent_model, status.opus_state.status);`) capture it:

```javascript
        window.__opusStatus = status.opus_state.status;   // "available" | "rate_limited" | "resuming"
```

Then in `renderHealthBar` (lines 1007-1013), replace the dot-class inference with the stored value:

```javascript
      const opusStatus = window.__opusStatus || "unknown";
```

(Delete the three `if (agentDot ...) opusStatus = ...` lines. Keep the rest of `renderHealthBar` unchanged.)

- [ ] **Step 3: Clear the stale phase line on terminal status**

In the SSE handler block at lines 1413-1417 that reloads the dashboard on task events, add explicit clearing for terminal events. Replace that block with one that clears `dashboardTaskLogs` for the affected task on completion/failure:

```javascript
      ["task_completed", "task_failed"].forEach(type => {
        source.addEventListener(type, event => {
          try {
            const data = JSON.parse(event.data);
            if (data && data.task_id) delete dashboardTaskLogs[data.task_id];
          } catch (error) { /* ignore */ }
          if (currentView === "dashboard") void loadDashboard({ skipSseReconnect: true });
        });
      });
      ["plan_activated", "agent_dispatched", "review_completed", "task_retry", "improvement_proposed"].forEach(type => {
        source.addEventListener(type, () => {
          if (currentView === "dashboard") void loadDashboard({ skipSseReconnect: true });
        });
      });
```

(This splits the original single `forEach` so terminal events also purge the phase text. `task_completed` fires on merge; `task_failed` on terminal failure.)

- [ ] **Step 4: Give plans a display name (fallback for the dropped spec)**

Add a helper near `renderSwimLane`:

```javascript
    function planLabel(plan) {
      const raw = plan.spec || plan.plan_branch_name || plan.plan_path || plan.spec_path || "";
      const name = String(raw).replace(/^plan\//, "").replace(/\.md$/, "");
      return name || ("Plan " + String(plan.id || "").slice(0, 8));
    }
```

Use it in both lanes:
- `renderSwimLane` line 1025: `const specPreview = esc(planLabel(plan)).slice(0, 80);`
- `renderCompletedLane` line 1071: `const specPreview = esc(planLabel(plan)).slice(0, 80);`

- [ ] **Step 5: Manual verification with the dashboard**

Start the server against a DB that has completed + active plans:
`uv run uvicorn orchestrator.main:app --host 127.0.0.1 --port 12399` (background).
Load `http://127.0.0.1:12399`, set the token in localStorage (`localStorage.setItem('praxis_token','<AUTH_TOKEN>')`), reload. Verify:
- The health pill reads "Opus available" (matching `/api/status`), not "resuming".
- COMPLETED rows show a plan name (branch-derived), not a bare "COMPLETED".
- Any card log line shows no `[0m`/`ESC` artifacts.
Stop the server (`taskkill //PID <pid> //F`).

- [ ] **Step 6: Commit**

```bash
git add web/app.js
git commit -m "fix(dashboard): strip ANSI, real Opus status pill, clear stale phase, name plan rows"
```

---

### Task 9: Dashboard CSS fixes (lane wrap, log contrast)

**Files:**
- Modify: `web/styles.css`
- Test: manual (Playwright)

**Depends on:** None

**Context:** Two independent CSS defects, both in `web/styles.css`. Confirmed live at 2560px and by inspecting the theme variables:
- **Lane overflow:** `.lane-cards { overflow-x: auto }` (line 421) makes each lane a fragile horizontally-scrolling row. At wide viewports 11 cards nearly exactly fill the width; one lane overflowed by 1px producing a stray full-width scrollbar while the other clipped its last card. Switching to `flex-wrap: wrap` was tested live and eliminated both symptoms (all cards visible, no page overflow).
- **Log contrast:** `.log-line-prose` and `.log-line-file` use `color: var(--text)` inside `.side-panel-log { background: var(--log-bg) }`. In the LIGHT theme `--text` is `#171717` and `--log-bg` is `#111312` = black-on-black. The log surface is always dark, so its foreground must come from the log palette (`--log-text`), not the theme text.

- [ ] **Step 1: Make lane cards wrap instead of scroll horizontally**

In `web/styles.css` line 421, change:

```css
    .lane-cards { display: flex; gap: 10px; padding: 0 20px 14px; overflow-x: auto; }
```

to:

```css
    .lane-cards { display: flex; flex-wrap: wrap; gap: 10px; padding: 0 20px 14px; }
```

Then remove the now-redundant mobile override at line 584 (`.lane-cards { flex-wrap: wrap; }`) since wrap is the default; leave the `.task-card { min-width: 140px; }` rule in that media block.

- [ ] **Step 2: Fix log foreground colors to the log palette**

In `web/styles.css`, the curated-log line styles (lines 564-571) reference theme `--text`. Repoint the ones that sit on the dark log surface to `--log-text` (or a log-scoped accent). Change:

```css
    .log-line-file { color: var(--text); }
```
to
```css
    .log-line-file { color: var(--log-text); }
```

and:

```css
    .log-line-prose { color: var(--text); display: block; margin: 2px 0; }
```
to
```css
    .log-line-prose { color: var(--log-text); display: block; margin: 2px 0; }
```

Also repoint `.log-line-info` (line 569, `var(--text-muted)`), which is also on the dark surface and is faint in light theme, to a log-appropriate muted tone:

```css
    .log-line-info { color: color-mix(in srgb, var(--log-text) 70%, transparent); }
```

Leave `.log-line-phase` / `-commit` / `-test-pass` / `-test-fail` as-is (their green/red badge colors read fine on the dark surface).

- [ ] **Step 3: Manual verification**

Start the server (as in Task 8 Step 5), load the dashboard at 2560px wide:
- No stray full-width horizontal scrollbar under any lane; no clipped cards; all task cards wrap onto multiple rows.
- Open a task with logs (click a card) and confirm the Live Activity prose text is readable in BOTH light and dark theme (toggle with the theme button). Text must contrast against the dark log background.
Stop the server.

- [ ] **Step 4: Commit**

```bash
git add web/styles.css
git commit -m "fix(dashboard): wrap lane cards and use the log palette for readable Live Activity"
```

---

## Parallel Execution Map

All nine tasks are independent (disjoint file ownership, no shared state), so they can run in a single wave. Group by risk for review cadence:

- **Wave 1 (backend core):** Task 1, Task 2, Task 3, Task 4, Task 5 (no dependencies; distinct files)
- **Wave 2 (docs + docker + dashboard):** Task 6, Task 7, Task 8, Task 9 (no dependencies; distinct files)

Waves here are a review-batching convenience, not a dependency constraint: nothing in Wave 2 depends on Wave 1. If executing sequentially, do Task 1 → Task 9 in order. If dispatching subagents in parallel, all nine may run at once because no two tasks touch the same file. Do NOT run two agents in the same non-isolated working tree (use `isolation: worktree` or serialize) to avoid clobbering uncommitted work.

## Post-merge verification (run once, after all tasks land)

- [ ] Full suite: `uv run pytest --cov=orchestrator --cov-report=term-missing -q` — expect all green, coverage >= 80%.
- [ ] Lint/format/types: `uv run ruff format --check src/ tests/ && uv run ruff check src/ tests/ && uv run mypy src/orchestrator/ --ignore-missing-imports`.
- [ ] Rebuild the opencode image (Task 7 already did; confirm): `docker build -t opencode-agent:latest -f docker/opencode-agent/Dockerfile docker/opencode-agent/`.
- [ ] Sanity-load the dashboard once more to confirm Tasks 8+9 landed together without regression.

## Self-Review (performed while writing)

- **Spec coverage:** all 9 FIX-SESSION-HANDOFF items map to a task (1→T1, 2→T3, 3→T4, 4→T7, 5→T7+Dockerfile, 6→T7, plan-completion→T5, awaiting_merge→T6, dashboard→T8/T9). Async execute_plan (headline) → T2. Deferred items (orchestrator container, decomposition over-split, poll_plan) are explicitly out of scope.
- **File disjointness:** verified no two tasks modify the same file (see the File Structure table). `orchestrator.py` is touched only by T2; `orchestrator_review.py` only by T5; `orchestrator_dispatch.py` only by T4; `internal.py` only by T3; `app.js` only by T8; `styles.css` only by T9.
- **Type/name consistency:** `create_pending_execute_plan`, `decompose_plan`, `normalize_slugs`, `branch_slug`, `compare_url`, `stripAnsi`, `planLabel` are each defined once and referenced consistently. `PlanStatus.FAILED` must be added to the enum (verified absent; flagged in T2 Step 10). Router wiring (`self._llm_router`) is a verified `__init__`/`main.py:124` change in T2 Step 10 — do not skip it, or the loop decomposition has no router. `app.state.event_bus` (T3) and `app.state.llm_router` (T2) are both verified present in `main.py`.
- **Placeholder scan:** no TODO/"handle errors appropriately"/"similar to" placeholders; every code step shows real code. Test skeletons that depend on existing fixtures (T2/T3/T5) explicitly instruct the implementer to match the fixture names in the target test file (verify before writing) rather than inventing them.
