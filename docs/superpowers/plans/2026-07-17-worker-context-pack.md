---
title: Worker Context Pack + Token Accounting (F9 / F7 / S7)
spec_path: docs/superpowers/specs/2026-07-17-worker-context-pack-design.md
---

# Worker Context Pack + Token Accounting (F9 / F7 / S7) Implementation Plan

> **For agentic workers:** Implement this plan task-by-task. Each task is
> independently testable. Steps use checkbox (`- [ ]`) syntax for tracking.
> Write the test FIRST, watch it fail, then implement to green. Run the project
> verify command before finishing each task.

**Goal:** Give the worker a deterministic repo context pack (declared files +
one-hop importers, skeletons only) in its Bible, and measure token/cost usage
per plan across both brain and worker calls, riding a versioned callback payload.

**Architecture:** Plan 4 of the capability-engine roadmap (spec:
`docs/superpowers/specs/2026-07-17-worker-context-pack-design.md`; features
F9 + F7 + S7). Three concerns share the dispatch/callback seam. F9 adds a pure
`core/context_pack.py` and a new droppable `Section` in `core/worker_bible.py`.
F7 adds an `llm_calls` table (migration), a `core/llm_calls.py` writer, router
brain-call instrumentation, a `core/pricing.py` cost helper, and a `poll_plan`
rollup. S7 versions `AgentDonePayload` and both harness entrypoints.

**Tech Stack:** Python 3.11, aiosqlite (raw SQL, versioned `Migration` framework
in `database.py` — add `Migration(n, ...)`, never an ad-hoc rebuild), Pydantic
API boundaries, `ast` for Python skeletons, pytest with `asyncio_mode = "auto"`.

**Decomposition note (kept shallow on purpose):** the two feature spines are
independent. F9 spine: context_pack module -> Bible wiring (depth 2). F7 spine:
migration -> writer -> {router instrumentation, worker capture, rollup} (depth 3).
S7 payload change is a leaf the worker-capture and entrypoint leaves depend on
(depth 2). Longest chain is depth 3, inside `max_dep_depth=4` with headroom. Do
NOT serialize independent leaves into one chain.

---

## Scope boundary (read before starting)

IN scope:
- `core/context_pack.py`: `build_context_pack` (one-hop importer scan + skeleton
  extraction, never raises).
- `core/worker_bible.py`: new `context_pack` field on `BibleSources` + droppable
  `Section("context_pack", ..., priority=6)` between `plan` and `repo_memory`.
- Dispatch call site (`core/orchestrator_dispatch.py`): build the pack from the
  leaf's declared `files` against the cloned repo and thread it into the Bible.
- `llm_calls` table (next migration number) + `core/llm_calls.py`
  (`record_llm_call`, `plan_token_usage`).
- Router instrumentation in `core/llm_router.py` (`run` + `_run_local`).
- `core/pricing.py` static price table + `est_cost_avoided_usd`.
- `AgentDonePayload` `payload_version` + optional worker-token fields + a version
  guard; worker-token row written in `agent_done`.
- Both entrypoints (`docker/agy-agent`, `docker/opencode-agent`) send
  `payload_version` (and token fields when available).
- `poll_plan` `token_usage` rollup block.
- Docs: CLAUDE.md gotcha index lines, `docs/architecture.md` note, ROADMAP flip.

OUT of scope (do NOT implement):
- Dashboard token panel UI (web/) — follow-up; the rollup ships on `poll_plan`.
- Rate-limit-queue projection feeding.
- Embeddings / vector retrieval / multi-hop importer graphs.
- Changing dispatch, review, or merge *behavior* — this is additive
  instrumentation + context enrichment only.

## Pinned inter-leaf contracts (honor these signatures verbatim)

```python
# core/context_pack.py
def build_context_pack(
    repo_dir: str,
    files: list[str],
    *,
    max_chars: int = 6000,
) -> str:
    """Skeleton markdown for `files` + one-hop importers. Never raises;
    returns "" when nothing usable is found."""

# core/llm_calls.py
async def record_llm_call(
    db: Any,
    *,
    plan_id: str | None,
    task_id: str | None,
    call_site: str,
    provider: str,
    model: str,
    prompt_chars: int,
    response_chars: int,
    duration_ms: int,
    source: str = "brain",   # "brain" | "worker"
) -> None: ...

async def plan_token_usage(db: Any, plan_id: str) -> dict: ...
# returns {"brain_calls": int, "brain_chars": int, "worker_chars": int,
#          "est_api_cost_avoided_usd": float}

# core/pricing.py
def est_cost_avoided_usd(rows: list[dict]) -> float: ...

# core/worker_bible.py — BibleSources gains:
#   context_pack: str | None = None
# build_bible inserts Section("context_pack", f"# REPO CONTEXT (signatures)\n{...}",
#   6) when present, AFTER the plan section, BEFORE repo_memory. Not a floor.
```

`AgentDonePayload` additive fields (all optional, default preserves v1 behavior):
`payload_version: int = 1`, `worker_prompt_chars: int | None = None`,
`worker_response_chars: int | None = None`, `worker_model: str | None = None`.
The handler rejects `payload_version` greater than the max supported version with
HTTP 422 and a logged reason.

---

## Task 1: `core/context_pack.py` skeleton extraction (F9 core)

**Files:** `src/orchestrator/core/context_pack.py`, `tests/test_context_pack.py`

- [ ] Write `tests/test_context_pack.py` FIRST with golden fixtures: create a
      temp repo dir with a small Python module (a class + two functions with
      docstrings and bodies). Assert `build_context_pack(dir, ["mod.py"])`
      returns the signatures and docstrings but NOT the function bodies.
- [ ] Test one-hop importers: a second file `caller.py` that does
      `from mod import ...`; assert its signature appears in the pack even though
      only `mod.py` was declared.
- [ ] Test robustness: a binary/unreadable file and a non-existent path are
      skipped silently (never raises); a repo with no matches returns `""`.
- [ ] Test the `max_chars` cap truncates deterministically.
- [ ] Implement `build_context_pack`: parse declared Python files with `ast`
      (module docstring + class/def signatures + their docstrings, bodies
      elided). For non-Python declared files, best-effort regex signature scan.
      Find one-hop importers by scanning the repo for `import`/`from` lines
      referencing a declared file's module stem. Assemble markdown, cap length.

Verify: `uv run pytest tests/test_context_pack.py -q` green; project verify_cmd green.

## Task 2: Wire context pack into the Bible (F9 wiring) — depends on Task 1

**Files:** `src/orchestrator/core/worker_bible.py`, `tests/test_worker_bible.py`

- [ ] Test FIRST: a `BibleSources` with `context_pack="..."` produces a Bible
      containing the `# REPO CONTEXT (signatures)` section, positioned AFTER the
      plan section and BEFORE repo memory.
- [ ] Test it is droppable: under a tiny `context_window`, floor sections
      (goal/handover/agreement) survive and the context pack is dropped before
      them; and the context pack drops before... assert repo_memory relative
      order via priority (pack priority 6 < repo priority 9, so under pressure
      repo_memory drops first — assert pack kept when repo dropped).
- [ ] Add `context_pack: str | None = None` to `BibleSources` and the
      `Section("context_pack", ..., 6)` insert in `build_bible`.

Verify: `uv run pytest tests/test_worker_bible.py -q` green; project verify_cmd green.

## Task 3: Build the pack at dispatch (F9 call site) — depends on Task 2

**Files:** `src/orchestrator/core/orchestrator_dispatch.py`,
`tests/test_orchestrator_dispatch.py` (extend existing)

- [ ] Test FIRST: `_build_worker_bible` (or the dispatch path that assembles it)
      calls `build_context_pack` with the leaf's declared `files` and the cloned
      repo dir, and threads the result into `BibleSources.context_pack`. Mock
      `build_context_pack` and assert it is invoked with the declared files.
- [ ] Test the no-files case: a leaf with empty/absent `files` yields
      `context_pack=None` (no crash, no call with empty list side effects).
- [ ] Implement: read the leaf's `files` from the plan task, resolve the clone
      dir already available in the dispatch path, call `build_context_pack`,
      pass into `BibleSources`.

Verify: `uv run pytest tests/test_orchestrator_dispatch.py -q` green; project verify_cmd green.

## Task 4: `llm_calls` table migration (F7 storage)

**Files:** `src/orchestrator/core/database.py`, `tests/test_database.py` (extend)

- [ ] Test FIRST: after `initialize()`, `PRAGMA table_info(llm_calls)` returns
      the expected columns; `PRAGMA user_version` advanced by exactly one; re-run
      of `initialize()` is idempotent (no error).
- [ ] Add `Migration(n, "add llm_calls table", fn)` to the `MIGRATIONS` list
      (use the next free number — read the current max, do not hardcode a stale
      one). Columns: `id TEXT PK, plan_id TEXT, task_id TEXT, call_site TEXT,
      provider TEXT, model TEXT, prompt_chars INTEGER, response_chars INTEGER,
      duration_ms INTEGER, source TEXT, created_at TEXT`. Idempotent
      (`CREATE TABLE IF NOT EXISTS`).

Verify: `uv run pytest tests/test_database.py -q` green; project verify_cmd green.

## Task 5: `core/pricing.py` cost helper (F7 pricing)

**Files:** `src/orchestrator/core/pricing.py`, `tests/test_pricing.py`

- [ ] Test FIRST: `est_cost_avoided_usd([...])` over a few rows with known
      provider/model/char counts returns a positive float; unknown model falls
      back to a default rate (documented), never raises; empty list returns 0.0.
- [ ] Implement a static `_PRICES` table (published per-1M-token USD, brain
      providers only — worker/local counts as avoided cost) and a `chars/4`
      token estimate. `est_cost_avoided_usd` sums `worker`-source char cost at
      the counterfactual brain price.

Verify: `uv run pytest tests/test_pricing.py -q` green; project verify_cmd green.

## Task 6: `core/llm_calls.py` writer + rollup (F7 writer) — depends on Task 4, Task 5

**Files:** `src/orchestrator/core/llm_calls.py`, `tests/test_llm_calls.py`

- [ ] Test FIRST: `record_llm_call(db, ...)` inserts one row; `plan_token_usage`
      aggregates brain_calls / brain_chars / worker_chars and calls
      `pricing.est_cost_avoided_usd`. Assert the dict shape from the pinned
      contract. A plan with no rows returns zeros.
- [ ] Implement both functions using raw SQL over the `llm_calls` table. Swallow
      nothing — the writer may raise on programmer error, but callers wrap it
      (see Task 7/8) so instrumentation never breaks the request path.

Verify: `uv run pytest tests/test_llm_calls.py -q` green; project verify_cmd green.

## Task 7: Router brain-call instrumentation (F7 brain) — depends on Task 6

**Files:** `src/orchestrator/core/llm_router.py`, `tests/test_llm_router.py` (extend)

- [ ] Test FIRST: a routed brain call records one `source='brain'` `llm_calls`
      row with the resolved provider/model/call_site and non-zero
      prompt_chars/response_chars/duration_ms. Mock the subprocess/local call and
      the `record_llm_call` writer; assert the writer is called once with correct
      args. Assert a writer exception does NOT propagate (instrumentation is
      best-effort, wrapped in try/except with a logged warning).
- [ ] Implement: thread an optional `db` + `plan_id`/`task_id` into the router's
      `run`/`_run_local` (or the smallest seam that owns them), measure
      `duration_ms` around the call, and record. Preserve existing behavior when
      no `db` is provided (unit tests that construct the router bare must still
      pass).

Verify: `uv run pytest tests/test_llm_router.py -q` green; project verify_cmd green.

## Task 8: S7 payload version + worker-token capture — depends on Task 6

**Files:** `src/orchestrator/api/internal.py`,
`src/orchestrator/models/schemas.py` (if payload moves) `tests/test_api_internal.py`

- [ ] Test FIRST (S7 compat): a v1 body (no `payload_version`, no token fields)
      parses and behaves exactly as today; a v2 body with `payload_version=2` +
      token fields parses. A body with `payload_version` greater than the max
      supported returns HTTP 422 with a logged reason.
- [ ] Test worker capture: a completed v2 callback with token fields writes one
      `source='worker'` `llm_calls` row for the task/plan (mock or in-memory db).
- [ ] Implement: add the fields + a `_MAX_PAYLOAD_VERSION` guard to
      `AgentDonePayload`/`agent_done`; on a completed callback carrying token
      fields, call `record_llm_call(..., source="worker")` wrapped so a writer
      failure never fails the callback.

Verify: `uv run pytest tests/test_api_internal.py -q` green; project verify_cmd green.

## Task 9: Entrypoints emit payload_version (S7 harness) — depends on Task 8

**Files:** `docker/agy-agent/entrypoint.sh`, `docker/opencode-agent/entrypoint.sh`

- [ ] Add `"payload_version":2` to the JSON payload both entrypoints POST to
      `/api/internal/agent-done`. Where the harness surfaces token usage in its
      logs, parse and include `worker_prompt_chars`/`worker_response_chars`/
      `worker_model`; when unavailable, omit them (handler treats them as None).
- [ ] Keep the existing retry/backoff and token-header logic untouched.
- [ ] Note in the task summary that BOTH images require a rebuild
      (`docker build -t agy-agent:latest ...` / `opencode-agent:latest`) — the
      orchestrator operator does this after merge; a stale image sending v1 must
      still work (backward compatibility is the whole point of S7).

Verify: entrypoints are shell — `bash -n` both scripts; no pytest for this leaf
(the project verify_cmd's trailing `pytest` will report no-tests and pass via the
exit-5 rule).

## Task 10: `poll_plan` rollup + docs — depends on Task 6, Task 9

**Files:** `src/orchestrator/mcp_server/server.py` (or the `poll_plan` REST
handler it wraps), `tests/` (extend the poll_plan test), `CLAUDE.md`,
`docs/architecture.md`, `docs/superpowers/specs/2026-07-11-capability-engine-roadmap.md`

- [ ] Test FIRST: `poll_plan` output includes a `token_usage` block matching
      `plan_token_usage`'s shape for a plan with recorded rows.
- [ ] Wire `plan_token_usage(db, plan_id)` into the `poll_plan` response.
- [ ] Docs: add CLAUDE.md gotcha index one-liners for (a) the F9 context pack
      Section priority/position and (b) the S7 versioned payload + stale-image
      contract; add a short `docs/architecture.md` note; flip Plan 4 status in
      the roadmap to DONE.

Verify: full project verify_cmd green (`uv run pytest -q` all tests).

---

## Definition of done

- [ ] All 10 tasks merged to the plan branch, each green under the project
      verify_cmd.
- [ ] Whole-plan verify passes: `uv sync --extra dev && uv run ruff format
      --check src/ tests/ && uv run ruff check src/ tests/ && uv run mypy
      src/orchestrator/ --ignore-missing-imports && uv run pytest -q`.
- [ ] F9: a dispatched leaf's Bible carries a `# REPO CONTEXT (signatures)`
      section built from its declared files.
- [ ] F7: `poll_plan` reports a `token_usage` rollup with brain + worker chars
      and an est cost-avoided figure.
- [ ] S7: callbacks are version-tagged; a v1 body still works; an unknown future
      version is rejected loudly.
