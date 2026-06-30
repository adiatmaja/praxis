# Capability-Aware Plan Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user hand Praxis an externally-authored plan and have Praxis right-size it for the local model (capability-aware decomposition), flag tasks too hard for it, and escalate failures to the brain or a user-owned paid fallback instead of shipping hallucinated work.

**Architecture:** A new `execute_plan` entry point runs a brain-driven plan-review pass (`core/plan_review.py`) that gates each task against a Capability Profile (declared seed + learned history) plus Spec 1's token budget, emitting the existing `opus_plan` task graph with per-leaf checklists. Tasks judged unsplittable-and-too-hard are flagged `needs_stronger_model`; runtime failures trigger policy-driven escalation. Builds directly on Spec 1 (`token_budget`, leaf `checklist`).

**Tech Stack:** Python 3.11, FastAPI, pytest (`asyncio_mode=auto`), aiosqlite (raw SQL), MCP (stdio), `core/llm_router` (claude / local / paid fallback).

**Spec:** `docs/superpowers/specs/2026-06-29-capability-aware-execution-design.md`
**Depends on plan:** `docs/superpowers/plans/2026-06-29-worker-context-continuity.md` (must ship first).

---

## Background (read this first — assumes zero prior context)

Praxis orchestrates a **brain** (Claude `claude -p`, routed via `core/llm_router.py`) for planning/review and a **local-LLM worker** (OpenCode/Aider in Docker) for implementation. The loop is in `core/orchestrator.py`; tasks are created only via `TaskQueue.activate_plan(plan_id, opus_plan, branch)`, which consumes an `opus_plan` dict shaped roughly:

```json
{"tasks": [{"id": "t1", "title": "...", "description": "...",
            "depends_on": [], "checklist": [{"text": "..."}]}]}
```

**Existing entry points:** `POST /api/dispatch` (single task, no review), `POST /api/plans/promote` (turn a repo `plan.md` into tasks via `core/plan_derive.derive_opus_plan` — deterministic parser + **local LM** fallback, **never the brain**, "extraction must stay free"). MCP tools live in `src/mcp_server/server.py` forwarding through `PraxisClient` (`src/mcp_server/client.py`) to the REST API.

**The problem:** plans are usually authored *outside* Praxis (Claude Code in another project), sized for a strong model. Handing them verbatim to a weak local model causes hallucination/quality loss. Praxis must judge each task against the *local model's real capability* at ingestion, decompose to do-able leaves, refuse the impossible, and escalate failures — without account rotation (the rejected "9router").

**This plan adds:**
1. **Capability Profile** — per-project/model: declared seed (incl. `parameter_count_b`) + learned summary from `agent_runs`.
2. **Plan-review brain pass** — `core/plan_review.py` decomposes + gates → `opus_plan` with checklists; flags `needs_stronger_model`.
3. **`execute_plan`** — REST endpoint + MCP tool: ingest + review + activate.
4. **Escalation** — on N failures / zero-commit / `ContextBudgetExceeded` / flag → `block` (default) | `brain` | `paid_fallback`.

**Key conventions (from CLAUDE.md):** `uv run` everything; `ruff format` (not `ruff fmt`); mypy `--ignore-missing-imports`; `pytest-asyncio` auto mode; Python 3.11 unions/generics; raw SQL + guarded inline migrations; brain prompts via **stdin**; route brain calls through `core/llm_router` (per-call-site `{provider, model, effort}`); settings via `EffectiveSettings` override→global→default + `settings_overrides` rows. Mirror the existing `plans/promote` plumbing for `execute_plan`.

**Verify before starting:** open each File Structure file and confirm signatures/line numbers (anchors from 2026-06-29). Confirm the exact `opus_plan` keys `TaskQueue.activate_plan` requires by reading `core/task_queue.py`.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `src/orchestrator/core/capability_history.py` (new) | Summarize `agent_runs` outcomes by task shape for the review prompt |
| `src/orchestrator/core/plan_review.py` (new) | Brain-driven capability-aware decomposition → validated `opus_plan` |
| `src/orchestrator/core/effective_settings.py` | `capability_profile(project)` + `escalation_policy(project)` resolvers |
| `src/orchestrator/api/execute_plan.py` (new) | `POST /api/execute-plan`: ingest + review + activate |
| `src/orchestrator/main.py` | Register the new router |
| `src/mcp_server/server.py`, `src/mcp_server/client.py` | `execute_plan` MCP tool + client method |
| `src/orchestrator/core/orchestrator.py` | Escalation triggers/actions; honor `needs_stronger_model` |
| `src/orchestrator/database.py` | `needs_stronger_model` / `escalation_state` / `escalated_to` columns |
| `src/orchestrator/models/schemas.py` | `ExecutePlanRequest/Response`, `CapabilityProfile`, task flags |
| `config/praxis.yaml` | Default declared capability profile + escalation policy (`block`) |

---

## Task 1: Capability profile resolver + defaults

**Files:**
- Modify: `src/orchestrator/core/effective_settings.py`
- Modify: `config/praxis.yaml`
- Modify: `src/orchestrator/models/schemas.py`
- Test: `tests/test_effective_settings.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_effective_settings.py  (append; reuses existing EffectiveSettings fixture)
import pytest


@pytest.mark.unit
async def test_capability_profile_falls_back_to_yaml_default(effective_settings):
    prof = await effective_settings.capability_profile(project_id=None)
    assert prof.parameter_count_b > 0
    assert prof.context_window > 0


@pytest.mark.unit
async def test_capability_profile_project_override_wins(effective_settings, seed_override):
    await seed_override("capability.qwen3", {"parameter_count_b": 70,
                                             "context_window": 32000,
                                             "strengths": "big", "weaknesses": "",
                                             "max_task_complexity": "high"},
                        project_id="p1")
    prof = await effective_settings.capability_profile(project_id="p1", model="qwen3")
    assert prof.parameter_count_b == 70


@pytest.mark.unit
async def test_escalation_policy_defaults_block(effective_settings):
    assert await effective_settings.escalation_policy(project_id=None) == "block"
```

> Match the fixtures (`effective_settings`, and a helper to seed a
> `settings_overrides` row) to the existing `tests/test_effective_settings.py`.
> If no seed helper exists, insert the override row directly via the db fixture.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_effective_settings.py -k capability -v`
Expected: FAIL with `AttributeError: ... 'capability_profile'`

- [ ] **Step 3: Add the DTO**

In `src/orchestrator/models/schemas.py`:

```python
from pydantic import BaseModel


class CapabilityProfile(BaseModel):
    model_name: str
    parameter_count_b: float
    context_window: int
    strengths: str = ""
    weaknesses: str = ""
    max_task_complexity: str = "medium"
```

- [ ] **Step 4: Add YAML defaults**

In `config/praxis.yaml`, add:

```yaml
capability:
  default:
    parameter_count_b: 30
    context_window: 8192
    strengths: "single-file edits, adding tests, small bug fixes"
    weaknesses: "multi-file refactors, novel architecture, large context"
    max_task_complexity: "medium"
escalation:
  policy: block   # block | brain | paid_fallback
```

(Confirm the YAML loader `core/settings_file.load_yaml_settings` passes unknown nested keys through; if it whitelists keys, add `capability`/`escalation` to the allowed set.)

- [ ] **Step 5: Add the resolvers**

In `src/orchestrator/core/effective_settings.py`:

```python
    async def capability_profile(
        self, project_id: str | None, model: str | None = None
    ) -> "CapabilityProfile":
        """Resolve the capability profile: project override -> YAML default."""
        from orchestrator.models.schemas import CapabilityProfile

        model_name = model or "default"
        override = await self._override(project_id, f"capability.{model_name}")
        defaults = self._yaml.get("capability", {}).get("default", {})
        data = {**defaults, **(override or {})}
        return CapabilityProfile(model_name=model_name, **data)

    async def escalation_policy(self, project_id: str | None) -> str:
        override = await self._override(project_id, "escalation.policy")
        if override:
            return str(override)
        return str(self._yaml.get("escalation", {}).get("policy", "block"))
```

(Match the actual override-lookup method name and YAML accessor already in this file — read it first; `self._override` / `self._yaml` are placeholders for whatever it uses.)

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/test_effective_settings.py -k "capability or escalation" -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator/core/effective_settings.py config/praxis.yaml src/orchestrator/models/schemas.py tests/test_effective_settings.py
git commit -m "feat: add capability profile and escalation policy resolvers"
```

---

## Task 2: Capability history summarizer

**Files:**
- Create: `src/orchestrator/core/capability_history.py`
- Test: `tests/test_capability_history.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_capability_history.py
import pytest

from orchestrator.core.capability_history import summarize_outcomes


@pytest.mark.unit
def test_empty_history_returns_no_history_sentinel():
    assert summarize_outcomes([]) == "(no prior run history for this model)"


@pytest.mark.unit
def test_summary_reports_pass_fail_counts_by_type():
    runs = [
        {"task_type": "test", "files_touched": 1, "loc_delta": 20, "outcome": "pass"},
        {"task_type": "test", "files_touched": 1, "loc_delta": 30, "outcome": "pass"},
        {"task_type": "refactor", "files_touched": 6, "loc_delta": 400, "outcome": "fail"},
    ]
    out = summarize_outcomes(runs)
    assert "test" in out and "refactor" in out
    assert "2 passed" in out or "pass: 2" in out.lower()
    assert "refactor" in out and "fail" in out.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_capability_history.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# src/orchestrator/core/capability_history.py
"""Summarize past worker outcomes by task shape, for the review brain.

We feed a compact summary (not raw rows) into the plan-review prompt so the
capability gate calibrates to what THIS model actually achieved on THIS repo.
With no history, returns a sentinel so the brain relies on the declared profile.
"""

from __future__ import annotations

from collections import defaultdict


def summarize_outcomes(runs: list[dict]) -> str:
    """Return a short per-task-type pass/fail summary.

    Args:
        runs: Rows with ``task_type``, ``files_touched``, ``loc_delta``,
            ``outcome`` ("pass"/"fail").
    """
    if not runs:
        return "(no prior run history for this model)"
    by_type: dict[str, dict[str, int]] = defaultdict(
        lambda: {"pass": 0, "fail": 0, "max_files": 0, "max_loc": 0}
    )
    for r in runs:
        t = by_type[r.get("task_type", "unknown")]
        outcome = "pass" if r.get("outcome") == "pass" else "fail"
        t[outcome] += 1
        t["max_files"] = max(t["max_files"], int(r.get("files_touched", 0)))
        t["max_loc"] = max(t["max_loc"], int(r.get("loc_delta", 0)))
    lines = ["Observed local-model outcomes by task type:"]
    for ttype, s in sorted(by_type.items()):
        lines.append(
            f"- {ttype}: pass: {s['pass']}, fail: {s['fail']} "
            f"(largest seen: {s['max_files']} files / {s['max_loc']} LOC)"
        )
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_capability_history.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/capability_history.py tests/test_capability_history.py
git commit -m "feat: summarize agent_run outcomes for capability calibration"
```

---

## Task 3: Plan-review decomposition pass

**Files:**
- Create: `src/orchestrator/core/plan_review.py`
- Test: `tests/test_plan_review.py`

**Depends on:** Task 1, Task 2 (and Spec 1's `token_budget`)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_plan_review.py
import json

import pytest

from orchestrator.core.plan_review import (
    PlanReviewError,
    build_review_prompt,
    parse_review_response,
)
from orchestrator.models.schemas import CapabilityProfile


PROFILE = CapabilityProfile(
    model_name="qwen3", parameter_count_b=30, context_window=8192,
    strengths="single-file", weaknesses="refactors", max_task_complexity="medium",
)


@pytest.mark.unit
def test_prompt_includes_param_count_and_history_and_budget():
    prompt = build_review_prompt(
        plan_text="Build a thing", profile=PROFILE,
        history_summary="(no prior run history for this model)",
        per_leaf_token_budget=3200,
    )
    assert "30" in prompt              # parameter count
    assert "3200" in prompt            # token budget
    assert "no prior run history" in prompt


@pytest.mark.unit
def test_parse_valid_graph_normalizes_to_opus_plan():
    raw = json.dumps({"tasks": [
        {"id": "t1", "title": "Add model", "description": "...",
         "depends_on": [], "checklist": [{"text": "write test"}],
         "needs_stronger_model": False},
    ]})
    plan = parse_review_response(raw)
    assert plan["tasks"][0]["id"] == "t1"
    assert plan["tasks"][0]["checklist"][0]["text"] == "write test"


@pytest.mark.unit
def test_parse_flags_needs_stronger_model():
    raw = json.dumps({"tasks": [
        {"id": "t1", "title": "Rewrite engine", "description": "...",
         "depends_on": [], "checklist": [{"text": "do it"}],
         "needs_stronger_model": True},
    ]})
    plan = parse_review_response(raw)
    assert plan["tasks"][0]["needs_stronger_model"] is True


@pytest.mark.unit
def test_parse_rejects_malformed_json_no_silent_pass():
    with pytest.raises(PlanReviewError):
        parse_review_response("not json at all")


@pytest.mark.unit
def test_parse_rejects_empty_task_list():
    with pytest.raises(PlanReviewError):
        parse_review_response(json.dumps({"tasks": []}))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_plan_review.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# src/orchestrator/core/plan_review.py
"""Brain-driven capability-aware decomposition of an external plan.

Unlike core/plan_derive (deterministic extraction, never the brain), this is a
reasoning pass: it judges each task against the local model's capability profile
and token budget, splits oversized tasks into do-able leaves with checklists,
and flags genuinely-too-hard leaves as needs_stronger_model. The brain call
itself is made by the caller via core/llm_router; this module owns the prompt
and the strict response parsing (a malformed response must never pass silently).
"""

from __future__ import annotations

import json

from orchestrator.models.schemas import CapabilityProfile


class PlanReviewError(Exception):
    """Raised when the review brain returns an unusable response."""


_PROMPT = """You decompose an implementation plan so a LOCAL model can execute it.

Local model capability:
- name: {model_name}
- parameters (billions): {parameter_count_b}
- context window (tokens): {context_window}
- strengths: {strengths}
- weaknesses: {weaknesses}
- max task complexity: {max_task_complexity}

{history_summary}

Hard limit: each leaf task's full context must fit ~{per_leaf_token_budget} tokens.

Plan to decompose:
{plan_text}

Split the plan into the SMALLEST leaf tasks this local model can each complete
on its own. For every leaf provide an ordered checklist of concrete steps. If a
leaf cannot be split small enough for this model (too complex for its parameter
count, or irreducibly large), set "needs_stronger_model": true for that leaf.

Respond with ONLY valid JSON:
{{
  "tasks": [
    {{"id": "t1", "title": "...", "description": "...", "depends_on": [],
      "checklist": [{{"text": "..."}}], "needs_stronger_model": false}}
  ]
}}
"""


def build_review_prompt(
    plan_text: str,
    profile: CapabilityProfile,
    history_summary: str,
    per_leaf_token_budget: int,
) -> str:
    """Render the decomposition prompt for the review brain."""
    return _PROMPT.format(
        model_name=profile.model_name,
        parameter_count_b=profile.parameter_count_b,
        context_window=profile.context_window,
        strengths=profile.strengths,
        weaknesses=profile.weaknesses,
        max_task_complexity=profile.max_task_complexity,
        history_summary=history_summary,
        per_leaf_token_budget=per_leaf_token_budget,
        plan_text=plan_text,
    )


def parse_review_response(raw: str) -> dict:
    """Parse + validate the brain's JSON into an opus_plan dict.

    Raises:
        PlanReviewError: on invalid JSON, missing/empty tasks, or bad shape.
    """
    raw = raw.strip()
    # Tolerate a ```json fence if the brain added one.
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        raw = raw[len("json"):] if raw.lstrip().startswith("json") else raw
        raw = raw.strip().rstrip("`").strip()
    try:
        data = json.loads(raw)
    except (ValueError, IndexError) as exc:
        raise PlanReviewError(f"review response not valid JSON: {exc}") from exc
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise PlanReviewError("review response had no tasks")
    for t in tasks:
        if "id" not in t or "title" not in t:
            raise PlanReviewError(f"task missing id/title: {t}")
        t.setdefault("description", t["title"])
        t.setdefault("depends_on", [])
        t.setdefault("checklist", [{"text": t["title"]}])
        t.setdefault("needs_stronger_model", False)
    return {"tasks": tasks}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_plan_review.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/plan_review.py tests/test_plan_review.py
git commit -m "feat: capability-aware plan-review decomposition prompt + parser"
```

---

## Task 4: Escalation columns

**Files:**
- Modify: `src/orchestrator/database.py`
- Modify: `src/orchestrator/models/schemas.py`
- Test: `tests/test_database.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_database.py  (append)
import pytest


@pytest.mark.integration
async def test_tasks_table_has_escalation_columns(db):
    cols = {row[1] for row in await (
        await db.execute("PRAGMA table_info(tasks)")).fetchall()}
    assert {"needs_stronger_model", "escalation_state", "escalated_to"} <= cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_database.py -k escalation_columns -v`
Expected: FAIL

- [ ] **Step 3: Add guarded migration**

In `src/orchestrator/database.py` `initialize()`, alongside the Spec 1 task migrations:

```python
    if "needs_stronger_model" not in cols:
        await db.execute(
            "ALTER TABLE tasks ADD COLUMN needs_stronger_model INTEGER DEFAULT 0"
        )
    if "escalation_state" not in cols:
        await db.execute("ALTER TABLE tasks ADD COLUMN escalation_state TEXT")
    if "escalated_to" not in cols:
        await db.execute("ALTER TABLE tasks ADD COLUMN escalated_to TEXT")
```

(Reuse the same `cols` set computed for the Spec 1 migration; recompute via `PRAGMA table_info(tasks)` if this runs in a separate block.)

- [ ] **Step 4: Add to task DTO**

In `src/orchestrator/models/schemas.py` on the task response model:

```python
    needs_stronger_model: bool = False
    escalation_state: str | None = None
    escalated_to: str | None = None
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_database.py -k escalation_columns -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/database.py src/orchestrator/models/schemas.py tests/test_database.py
git commit -m "feat: add escalation tracking columns to tasks"
```

---

## Task 5: `execute_plan` endpoint

**Files:**
- Create: `src/orchestrator/api/execute_plan.py`
- Modify: `src/orchestrator/main.py`
- Modify: `src/orchestrator/models/schemas.py`
- Test: `tests/test_api_execute_plan.py`

**Depends on:** Task 1, Task 2, Task 3, Task 4

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_execute_plan.py
import json

import pytest


@pytest.mark.integration
async def test_execute_plan_reviews_and_activates(client, auth_headers, monkeypatch):
    captured = {}

    async def fake_router_run(call_site, prompt, project_id, cwd=None):
        return json.dumps({"tasks": [
            {"id": "t1", "title": "Add model", "description": "d",
             "depends_on": [], "checklist": [{"text": "write test"}],
             "needs_stronger_model": False}]})

    async def fake_activate(plan_id, opus_plan, branch_name):
        captured["opus_plan"] = opus_plan

    monkeypatch.setattr(client.app.state.router, "run", fake_router_run)
    monkeypatch.setattr(client.app.state.task_queue, "activate_plan", fake_activate)

    resp = client.post("/api/execute-plan", headers=auth_headers, json={
        "repo_url": "https://github.com/o/r",
        "plan": "Build a thing with a model and a test",
        "model": "qwen3",
    })
    assert resp.status_code == 201
    assert captured["opus_plan"]["tasks"][0]["id"] == "t1"
    body = resp.json()
    assert body["leaves"] and body["blocked"] == []


@pytest.mark.integration
async def test_execute_plan_missing_plan_returns_422(client, auth_headers):
    resp = client.post("/api/execute-plan", headers=auth_headers, json={
        "repo_url": "https://github.com/o/r", "model": "qwen3"})
    assert resp.status_code == 422
```

> Match `client.app.state.router` / `task_queue` to how the app actually exposes
> these (read `main.py` lifespan). If the router is reached via `OpusBridge`,
> patch the bridge's `review_diff`-style entry instead, but the contract holds:
> the brain output drives `activate_plan`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_execute_plan.py -v`
Expected: FAIL (404 — route not registered)

- [ ] **Step 3: Add the request/response schemas**

In `src/orchestrator/models/schemas.py`:

```python
class ExecutePlanRequest(BaseModel):
    repo_url: str
    plan: str
    model: str
    harness: str | None = None
    branch: str | None = None
    context: str | None = None


class ExecutePlanResponse(BaseModel):
    plan_id: str
    dashboard_url: str
    leaves: list[str]
    blocked: list[str]
```

- [ ] **Step 4: Write the endpoint**

```python
# src/orchestrator/api/execute_plan.py
"""Ingest an externally-authored plan, capability-review it, and execute it."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from orchestrator.api.auth import require_auth
from orchestrator.core.capability_history import summarize_outcomes
from orchestrator.core.plan_review import (
    PlanReviewError,
    build_review_prompt,
    parse_review_response,
)
from orchestrator.core.token_budget import estimate_tokens  # noqa: F401 (budget calc)
from orchestrator.models.schemas import ExecutePlanRequest, ExecutePlanResponse


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["execute-plan"])


@router.post("/execute-plan", status_code=201, response_model=ExecutePlanResponse)
async def execute_plan(
    body: ExecutePlanRequest,
    request: Request,
    _: None = Depends(require_auth),
) -> ExecutePlanResponse:
    state = request.app.state
    profile = await state.effective_settings.capability_profile(
        project_id=None, model=body.model
    )
    per_leaf_budget = int(profile.context_window * 0.4)  # mirror Spec1 reserve
    history = summarize_outcomes(await state.db_outcomes_for_model(body.model))
    prompt = build_review_prompt(body.plan, profile, history, per_leaf_budget)

    try:
        raw = await state.router.run("plan_review", prompt, project_id=None)
        opus_plan = parse_review_response(raw)
    except PlanReviewError as exc:
        raise HTTPException(status_code=502, detail=f"plan review failed: {exc}")

    # Create plan row + activate via the existing TaskQueue path (mirror promote).
    plan_id, branch = await state.task_queue.create_plan_for_repo(
        repo_url=body.repo_url, model=body.model, harness=body.harness,
        base_branch=body.branch,
    )
    await state.task_queue.activate_plan(plan_id, opus_plan, branch)

    leaves = [t["id"] for t in opus_plan["tasks"] if not t["needs_stronger_model"]]
    blocked = [t["id"] for t in opus_plan["tasks"] if t["needs_stronger_model"]]
    return ExecutePlanResponse(
        plan_id=plan_id,
        dashboard_url=f"{state.settings.public_url}/#/plans/{plan_id}",
        leaves=leaves,
        blocked=blocked,
    )
```

> `create_plan_for_repo`, `db_outcomes_for_model`, `state.router`,
> `state.effective_settings`, `state.settings.public_url` are the integration
> seams — wire each to the real method. If `create_plan_for_repo` does not exist,
> follow exactly what `api/dispatch.py` does to create + activate a one-task plan,
> generalized to N tasks (it already builds a plan row + branch). The
> `plan_review` call site must be added to `CALL_SITE_DEFAULTS` in
> `core/llm_router.py` (Task 6).

- [ ] **Step 5: Register the router**

In `src/orchestrator/main.py`, where other routers are included:

```python
from orchestrator.api import execute_plan as execute_plan_api
app.include_router(execute_plan_api.router)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/test_api_execute_plan.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator/api/execute_plan.py src/orchestrator/main.py src/orchestrator/models/schemas.py tests/test_api_execute_plan.py
git commit -m "feat: add execute-plan endpoint with capability review"
```

---

## Task 6: Register `plan_review` call-site in the router

**Files:**
- Modify: `src/orchestrator/core/llm_router.py` (`CALL_SITE_DEFAULTS`)
- Test: `tests/test_llm_router.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_llm_router.py  (append)
import pytest

from orchestrator.core.llm_router import CALL_SITE_DEFAULTS


@pytest.mark.unit
def test_plan_review_call_site_registered():
    cfg = CALL_SITE_DEFAULTS["plan_review"]
    assert cfg["provider"] == "claude"   # capability judgment needs the brain
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_llm_router.py -k plan_review_call_site -v`
Expected: FAIL with `KeyError: 'plan_review'`

- [ ] **Step 3: Add the entry**

In `src/orchestrator/core/llm_router.py` `CALL_SITE_DEFAULTS`, add (match the existing dict's value shape, e.g. effort field):

```python
    "plan_review": {"provider": "claude", "model": "opus", "effort": "high"},
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_llm_router.py -k plan_review_call_site -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/llm_router.py tests/test_llm_router.py
git commit -m "feat: route plan_review through the brain at high effort"
```

---

## Task 7: Escalation on repeated failure

**Files:**
- Modify: `src/orchestrator/core/orchestrator.py` (review/fail handling)
- Test: `tests/test_orchestrator.py`

**Depends on:** Task 1, Task 4

- [ ] **Step 1: Write the failing test**

```python
# tests/test_orchestrator.py  (append)
import pytest


@pytest.mark.unit
async def test_decide_escalation_blocks_by_default(orchestrator, monkeypatch):
    async def policy(project_id):
        return "block"

    monkeypatch.setattr(
        orchestrator._effective_settings, "escalation_policy", policy
    )
    action = await orchestrator._decide_escalation(
        project={"id": "p1"}, retries_exhausted=True
    )
    assert action == "block"


@pytest.mark.unit
async def test_decide_escalation_returns_brain_when_configured(orchestrator, monkeypatch):
    async def policy(project_id):
        return "brain"

    monkeypatch.setattr(
        orchestrator._effective_settings, "escalation_policy", policy
    )
    action = await orchestrator._decide_escalation(
        project={"id": "p1"}, retries_exhausted=True
    )
    assert action == "brain"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_orchestrator.py -k decide_escalation -v`
Expected: FAIL with `AttributeError: ... '_decide_escalation'`

- [ ] **Step 3: Add the decision method + wire it**

In `src/orchestrator/core/orchestrator.py`:

```python
    async def _decide_escalation(
        self, project: dict, retries_exhausted: bool
    ) -> str:
        """Return the escalation action for a failing leaf: block|brain|paid_fallback."""
        if not retries_exhausted:
            return "retry"
        return await self._effective_settings.escalation_policy(project["id"])
```

In the review/fail path, when a task has exhausted `max_retries` (or hit zero-commit / `ContextBudgetExceeded` / `needs_stronger_model`), call `_decide_escalation`. For `block`: set `escalation_state="blocked"` and leave the task terminal (do not re-dispatch). For `brain`/`paid_fallback`: set `escalation_state="escalated"`, `escalated_to=<action>`, and route the implement through the brain / the user-owned fallback provider via `llm_router` (a follow-up wiring step may stub the actual fallback implement call — the decision + state transition is what this task delivers and tests).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_orchestrator.py -k decide_escalation -v`
Expected: PASS

- [ ] **Step 5: Run the full orchestrator suite**

Run: `uv run pytest tests/test_orchestrator.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/core/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: policy-driven escalation for failing/too-hard leaves"
```

---

## Task 8: `execute_plan` MCP tool

**Files:**
- Modify: `src/mcp_server/server.py`
- Modify: `src/mcp_server/client.py`
- Test: `tests/test_mcp_server.py`

**Depends on:** Task 5

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mcp_server.py  (append; reuses existing PraxisClient test patterns)
import pytest


@pytest.mark.unit
async def test_client_execute_plan_posts_expected_body(praxis_client, httpx_mock):
    httpx_mock.add_response(
        method="POST", url="http://test/api/execute-plan",
        json={"plan_id": "p1", "dashboard_url": "http://test/#/plans/p1",
              "leaves": ["t1"], "blocked": []}, status_code=201)
    out = await praxis_client.execute_plan(
        repo_url="https://github.com/o/r", plan="Build it", model="qwen3")
    assert out["plan_id"] == "p1"
    req = httpx_mock.get_request()
    assert b'"plan"' in req.content
```

> Match the existing MCP/client test harness (httpx mock vs respx vs a fake
> transport) used in `tests/test_mcp_server.py`. The contract: `execute_plan`
> POSTs to `/api/execute-plan` with `repo_url`/`plan`/`model`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_server.py -k execute_plan -v`
Expected: FAIL with `AttributeError: ... 'execute_plan'`

- [ ] **Step 3: Add the client method**

In `src/mcp_server/client.py`, mirroring `dispatch`:

```python
    async def execute_plan(
        self, repo_url: str, plan: str, model: str,
        harness: str | None = None, branch: str | None = None,
        context: str | None = None,
    ) -> dict:
        """POST an externally-authored plan for capability-aware execution."""
        body = {"repo_url": repo_url, "plan": plan, "model": model}
        if harness is not None:
            body["harness"] = harness
        if branch is not None:
            body["branch"] = branch
        if context is not None:
            body["context"] = context
        resp = await self._client.post("/api/execute-plan", json=body)
        resp.raise_for_status()
        return resp.json()
```

- [ ] **Step 4: Add the MCP tool**

In `src/mcp_server/server.py`, mirror the `dispatch_task` tool registration with an `execute_plan` tool whose docstring reads:

```
Execute a full, externally-authored implementation plan on a repo. Praxis runs
a capability-aware review that decomposes the plan into tasks the LOCAL model can
each complete, flags any tasks too hard for it (returned in "blocked"), and runs
its own review/merge loop. Pass the FULL plan text. Use this (not dispatch_task)
when you already have a multi-step plan; dispatch_task is for a single small task.
```

Forward to `PraxisClient.execute_plan(...)`.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_server.py -k execute_plan -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/mcp_server/server.py src/mcp_server/client.py tests/test_mcp_server.py
git commit -m "feat: add execute_plan MCP tool and client method"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1, Task 2, Task 4, Task 6 (no dependencies)
- **Wave 2:** Task 3 (Task 1, Task 2), Task 7 (Task 1, Task 4)
- **Wave 3:** Task 5 (Task 1, Task 2, Task 3, Task 4)
- **Wave 4:** Task 8 (Task 5)

---

## Final Verification

After all tasks:

```bash
uv run ruff format src/ tests/ && uv run ruff check --fix src/ tests/
uv run mypy src/orchestrator/ --ignore-missing-imports
uv run pytest --cov=orchestrator --cov-report=term-missing -v
```

Expected: format clean, mypy clean, all tests pass (coverage ≥ 80%).

Manual end-to-end (optional, needs claude CLI + LM Studio + Docker):
- `POST /api/execute-plan` with a small multi-step plan → confirm tasks created with checklists, any too-hard task returned in `blocked`, the loop dispatches the rest.

## Notes / Out of Scope

- **Continue-on-PR / amend-existing-PR** — still a tracked follow-up; re-dispatch = new PR.
- **`paid_fallback` actual implement call** — the decision + state transition is delivered here; the
  concrete fallback-provider implement wiring (and surfacing the user-owned credential) can be a thin
  follow-up once a fallback provider is configured. Default policy `block` ships fully working.
- **Worker Bible/Handover/budget** — delivered by Spec 1's plan; this plan reuses `token_budget`
  and the leaf `checklist`.
