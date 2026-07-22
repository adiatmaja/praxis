# Worker Context Pack + Token Accounting (F9 / F7 / S7) — Design

> Plan 4 of the capability-engine roadmap
> (`docs/superpowers/specs/2026-07-11-capability-engine-roadmap.md`).
> Bundles three features that all touch the dispatch/callback seam: **F9** repo
> context pack for the worker, **F7** token/cost accounting per plan, **S7** the
> callback payload contract that F7's worker-token counts ride on.

## Problem

Two gaps, one shared seam:

1. **Handoff-fidelity (F9).** The worker receives the plan text and the caller's
   `local_context`, but nothing about the *repo* it is editing. It must discover
   signatures by reading files itself, which burns worker turns and, on weaker
   local models, produces interface drift (the recurring "worker drops interface
   details" finding across dogfoods). The leaf already declares its `files`
   (F2); we are not using them at dispatch.

2. **Economics are unmeasured (F7).** The product's headline is capability-aware
   cost efficiency, yet nothing records how many brain calls or worker tokens a
   plan actually spends. There is no per-plan rollup and no honest
   "API cost avoided" number.

Both land on the dispatch/callback seam, and F7's worker-token counts require a
**versioned callback payload (S7)** so a stale agent image fails loudly instead
of silently dropping the new fields.

## Design

### F9 — Repo context pack (deterministic, zero-infra)

New pure module `core/context_pack.py`. At dispatch, for each leaf:

1. Take the leaf's declared `files` (F2 `LeafTask.files`).
2. From the already-cloned repo, find **one-hop importers**: files whose import
   statements reference a declared file's module (deterministic `grep`-style
   scan of the clone, no embeddings, no vector DB).
3. For the declared files **and** their one-hop importers, extract **skeletons**:
   module/class/function signatures + docstrings, **bodies elided**. Python via
   `ast`; other languages via a line-regex signature scan (best-effort, never
   raises). Truncate per-file and overall to a hard char cap.
4. Return the assembled skeleton markdown.

The pack is injected as a new prioritized `Section("context_pack", ...,
priority=6)` in `build_bible`, sitting **between** `plan` (priority 4) and
`repo_memory` (priority 9). `fit_sections` already trims the low-priority tail
under a tight budget, so the pack is droppable before repo memory but kept ahead
of it. It is NOT a floor section: the goal/handover/plan always win.

Rationale: for leaf-sized tasks *with a declared file list*, deterministic
one-hop retrieval beats semantic search and carries zero infrastructure. This is
the cheapest single change that raises local-worker pass rates.

### F7 — Token/cost accounting per plan

New `llm_calls` table (migration): `id, plan_id, task_id, call_site, provider,
model, prompt_chars, response_chars, duration_ms, source, created_at`. `source`
distinguishes `brain` (router-measured) from `worker` (harness-reported via the
callback).

- **Brain calls:** the `LLMRouter` is the single brain choke point. `run()` and
  `_run_local()` already own the prompt string and the subprocess timing; they
  record one `llm_calls` row per call. `prompt_chars`/`response_chars` are exact;
  token estimation is a cheap `chars/4` helper (`core/token_budget` already has
  budgeting utilities). Provider/model/call_site come from the resolved config.
- **Worker calls:** the harness reports its token usage in the S7 callback
  payload; `agent_done` writes one `source='worker'` row.
- **Rollup:** `poll_plan` gains a `token_usage` block: brain calls count, worker
  chars/tokens, and `est_api_cost_avoided_usd` computed against a static
  published per-token price table (`core/pricing.py`, honest counterfactual
  pricing of measured usage).

Char counts (not token counts) are the persisted primitive so we never depend on
a tokenizer matching the provider; the cost estimate applies a per-model
chars-to-tokens ratio at rollup time.

### S7 — Callback payload contract

`AgentDonePayload` gains `payload_version: int = 1` and optional token fields
(`worker_prompt_chars`, `worker_response_chars`, `worker_model`). A handler
guard rejects an *unknown future* version loudly (422 + logged) rather than
silently coercing, closing the stale-image class of bug. One compatibility test
per supported version asserts a v1 (no token fields) and a v2 (with token
fields) body both parse. Both entrypoints (`agy`, `opencode`) send
`payload_version` and, when the harness surfaces them, the token fields.

## Scope boundary

IN:
- `core/context_pack.py` (build + one-hop importer scan + skeleton extraction).
- Context-pack `Section` wired into `build_bible` + the dispatch call site.
- `llm_calls` table (migration) + `core/llm_calls.record_llm_call` writer.
- Router brain-call instrumentation (both `run` and `_run_local`).
- `core/pricing.py` static price table + `est_api_cost_avoided_usd` helper.
- Worker-token capture in `agent_done` from the S7 payload.
- `payload_version` + token fields + version guard on `AgentDonePayload`,
  compatibility tests, and both entrypoints emitting them.
- `poll_plan` `token_usage` rollup block.
- Docs: CLAUDE.md gotcha index lines, `docs/architecture.md` note, ROADMAP flip.

OUT (later plans / deferred):
- Dashboard token panel UI (the rollup ships on `poll_plan`; the web panel is a
  follow-up — agy cannot visually verify it).
- Proactive rate-limit-queue feeding from projected brain-call count (F7 stretch;
  needs the queue projection model, deferred).
- Embedding / vector retrieval for F9 (explicitly rejected: one-hop is enough).
- Multi-hop importer graphs (one hop only).
- Non-Python skeleton fidelity beyond a best-effort regex signature scan.

## Pinned inter-leaf contracts

```python
# core/context_pack.py
def build_context_pack(
    repo_dir: str,
    files: list[str],
    *,
    max_chars: int = 6000,
) -> str:
    """Return skeleton markdown for `files` + their one-hop importers.

    Bodies elided; signatures + docstrings only. Never raises: unreadable or
    binary files are skipped. Returns "" when nothing usable is found.
    """

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
# -> {"brain_calls": int, "brain_chars": int, "worker_chars": int,
#     "est_api_cost_avoided_usd": float}

# core/pricing.py
def est_cost_avoided_usd(rows: list[dict]) -> float:
    """Counterfactual USD cost of the measured char usage at published prices."""
```

`AgentDonePayload` v2 shape (additive, all new fields optional):

```json
{"task_id": "...", "run_id": "...", "status": "completed",
 "pr_url": null, "question": null, "payload_version": 2,
 "worker_prompt_chars": 12000, "worker_response_chars": 3400,
 "worker_model": "gemini-3.1-pro"}
```

## Verification

Each leaf ships pytest coverage. The project `verify_cmd` (set 2026-07-17:
`uv sync --extra dev && ruff format --check && ruff check && mypy && pytest -q`)
gates every leaf and each wave built on merged leaves. F9's skeleton extraction
has golden-fixture tests (a known Python file -> expected signature-only output).
S7 has one parse test per payload version.
