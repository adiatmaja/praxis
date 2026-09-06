# Workflow

> The `Opus` / `claude -p` and LM Studio references throughout this doc are the reference
> configuration, not a constraint. Praxis is provider-agnostic: each role (plan, implement,
> review, verify) is independently configurable to any supported provider, model, or harness
> via `core/llm_router.py`. Read them as "the planning brain" and "the implementer", not as
> a fixed vendor.

## Full Orchestration Cycle

```
  ┌──────────┐     ┌──────────────┐     ┌──────────────┐
  │  User    │────►│  Brain plans │────►│  Create plan │
  │  submits │     │  via provider│     │  branch      │
  │  spec    │     │  CLI (router)│     │              │
  └──────────┘     └──────────────┘     └──────┬───────┘
                                               │
                          ┌────────────────────┼────────────────────┐
                          ▼                    ▼                    ▼
                   ┌────────────┐       ┌────────────┐      ┌────────────┐
                   │ Harness    │       │ Harness    │      │ Harness    │
                   │ agent/     │       │ agent/     │      │ agent/     │
                   │ task-1     │       │ task-2     │      │ task-3     │
                   └─────┬──────┘       └─────┬──────┘      └─────┬──────┘
                         │                    │                    │
                         └────────────────────┼────────────────────┘
                                              │
                                              ▼
                                     ┌────────────────┐
                                     │  Brain reviews │
                                     │  PR diffs      │
                                     └───────┬────────┘
                                             │
                                 ┌───────────┴───────────┐
                                 ▼                       ▼
                           ┌──────────┐           ┌───────────┐
                           │  Pass:   │           │  Fail:    │
                           │  squash  │           │  feedback │
                           │  merge   │           │  + retry  │
                           └────┬─────┘           └───────────┘
                                │
                                ▼
                       ┌─────────────────┐
                       │  Integration PR │
                       │  to main        │
                       └─────────────────┘
                                │
                                ▼
                       ┌─────────────────┐
                       │  Improvement    │
                       │  loop (optional)│
                       └─────────────────┘
```

## What runs at the same time

The loop makes one pass every `loop_interval` seconds, and the pass never blocks on a
brain call. A plan's decomposition and a task's review are each started as a tracked
job, keyed so the same plan is never decomposed twice and the same task never reviewed
twice while one is in flight, and bounded by `max_brain_concurrency` (default 3). Worker
containers are bounded separately by `max_agent_concurrency` (default 3). So several
plans decompose together, several leaves are reviewed together, and dispatch keeps
running while they do; a stage that finds no free slot waits for one, which
`praxis status` shows as a brain stage in the `waiting` state.

Three things stay serialized on purpose. In auto-delegate (single-branch) mode only one
task at a time may hold the shared work branch, because per-task review scoping is bounded
to the commits a task added after the branch head recorded at its dispatch. Merges into
one repository are serialized, because two passing reviews can now reach the merge
together. And a worker whose provider named a quota reset time is held until then, costing
nothing while it waits.

## Two entry paths

1. **Direct spec → run (legacy):** submit a spec and let Opus plan it (steps below).
2. **Doc-driven Spec → Plan → Run (preferred, Spec 1):** Create a spec via the chat
   (committed to `docs/**/specs/`), Generate Plan (committed to `docs/**/plans/` with a
   `spec_path:` link), then **Promote to Run**: `POST /api/plans/promote` reads the
   `plan.md`, derives tasks (`plan_derive`: deterministic parser → local LM Studio
   fallback), and creates a DB plan that feeds the same orchestration cycle from step 3.
   Markdown docs in the target repo are the source of truth; the DB is an execution ledger.

## Step-by-Step

1. **Spec submission**: User submits a specification via Web UI, CLI, or REST API,
   targeting a registered GitHub repository (or promotes an existing `plan.md`).

2. **Planning**: Orchestrator routes the planning call through the `LLMRouter` at the
   `plan_spec` call-site. What that resolves to is the **plan role chain** in
   `config/praxis.yaml` (`plan: [sonnet, opus]`), not `CALL_SITE_DEFAULTS`: once a
   call-site has a role, `EffectiveSettings.call_site_chain` returns the role chain and
   never consults the per-call-site default. So planning runs on Sonnet, and Opus is the
   rung it falls back to when Sonnet is unavailable. Per-call-site overrides are still
   settable in Settings → Models. It returns a JSON plan with a summary, slug, and task
   list with dependency graph. (For the Promote path, tasks are derived locally instead,
   with no brain call.)

3. **Plan activation**: Orchestrator creates a `plan/{date}-{slug}` branch from main,
   stores the parsed plan in SQLite, and marks the plan as `active`.

4. **Task dispatch**: For each task with no unmet dependencies, orchestrator spawns
   a Docker container running the project's selected harness (OpenCode by default,
   or agy). The container:
   - Clones the repo
   - Creates an `agent/{task-slug}` branch from the plan branch, in the DEFAULT
     mode only. In auto-delegate (single-branch) mode it commits directly onto
     the caller-named work branch instead, cutting no branch and opening no new
     PR per task, so a second dispatch onto the same branch stacks on the first
   - Runs the selected harness with the task description as the prompt
   - Commits, pushes, and creates a PR targeting the plan branch
   - Calls back to `/api/internal/agent-done` when finished

5. **Code review**: Orchestrator fetches the diff and routes it to
   the review call-site: `review_diff_first` for the first read, `review_diff_rereview`
   for a re-read after fixes. Both carry the `review` role, so both resolve through the
   role chain in `config/praxis.yaml` (`review: [sonnet, haiku]`) and both run on Sonnet;
   Haiku is the fallback rung, not the re-review tier. WHICH diff it fetches depends on
   `tasks.review_base_sha`: with one recorded (every dispatch since 2026-08-24) the task is
   reviewed on its OWN commits, `review_base_sha..head`, through `backend.get_diff_since`;
   with a NULL sha it is the whole pull request via `gh pr diff`, exactly as before.
   `CALL_SITE_DEFAULTS` still names
   Haiku for `review_diff_rereview`, and that default is shadowed by the role chain
   exactly as `docs/configurations.md` describes. Returns a JSON verdict: `pass` or
   `fail` with feedback.

   The prompt also carries a BLAST RADIUS section (`core/blast_radius.py`): how
   many times each identifier the diff defines or redefines occurs across the
   checkout. It exists because a review can be entirely correct and still miss
   the defect, when the defect is not in the diff at all. The case that prompted
   it: a CSS rule correctly stopped being a flex container, and a
   `justify-content` three hundred lines away in a different selector went
   silently inert. Every statement the review made was true. Measurement fails
   OPEN, so a review never wedges on a repo walk, and a walk that hit a cap says
   "at least N" rather than stating a lower bound as exact.

   On a PASS the verdict carries a SCOPE STATEMENT to the merge gate, naming
   what the review did and did not observe: whether it read a checkout or only
   the diff text, whether `verify_cmd` ran, passed, was skipped or is not
   configured, and whether blast radius was measured. A green that reads as
   verification when it is only a diff summary is worse than no check, so the
   review states its own reach rather than leaving the approver to assume it.
   It is PASS-only on purpose: `review_feedback` on a FAIL is injected verbatim
   into the next worker's prompt by `core/worker_bible.py`, which is written for
   the floor model.

6. **Pass**: PR is squash-merged into the plan branch. Agent branch is deleted.

7. **Fail**: Feedback is posted as a PR comment. Task is re-dispatched with the
   feedback included in the prompt (max 3 retries).

8. **Integration**: When all tasks are merged into the plan branch, an integration
   PR is created targeting main.

9. **Improvement loop** (optional): If autonomous improvement is enabled, Opus
   analyzes the completed plan and proposes further improvements. If confidence
   exceeds the project's threshold:
   - With approval gate ON: plan is created with `pending` status, awaiting user approval
   - With approval gate OFF: plan is automatically activated and dispatched

## Worker Clarification Channel

When a worker agent cannot proceed without more information, it reports
`Status: BLOCKED` or `NEEDS_CONTEXT` in its FINAL REPORT instead of guessing.
The orchestrator treats this as a clarification request: the task parks at
`NEEDS_CLARIFICATION` without burning a retry, and the brain (`answer_clarification`,
Sonnet/medium) attempts to answer from the task description and `plan_text`. A
confident answer (confidence >= project `confidence_threshold`) re-dispatches the
task with the Q&A injected via `progress_note` into the Static Bible; a low-confidence
answer parks the task at `awaiting_human` and emits `task_needs_clarification` over
SSE so a human can supply the answer via `POST /api/tasks/{id}/clarify` or MCP
`poll_task` (which reports `status: awaiting_clarification`).

State transitions:

```
IN_PROGRESS -> NEEDS_CLARIFICATION -> (brain confident) -> PENDING (re-dispatch)
                                   -> (low confidence)  -> awaiting_human -> PENDING
```

Both harnesses (opencode, agy) parse the FINAL REPORT for block signals and
route them through the same harness-agnostic callback.

## Auto-Delegate Mode (daily-dev)

Auto-delegate mode is a global toggle that reframes the same loop as a daily driver:
with it ON, the brain never edits code directly. For every implementation task the brain
designs the worker prompt, dispatches it to a single global default worker, and reviews the
returned PR. Planning, prompt design, and review stay with the brain; the coding is always
delegated. Mode is sequential in v1 (one delegate in flight at a time), and that is
load-bearing rather than a simplification: per-task review scoping depends on it. Two
workers committing to the shared branch at once interleave their commits, both review
ranges silently widen to include the other's files, and nothing errors. `dispatch_pending_tasks`
enforces it at the BRANCH: one task per wave, held while any task on that branch is in
progress or under review, across every plan in the project. The cross-plan scope is what
makes it cover a caller too, since each `dispatch_task` becomes its own one-task plan that
this same loop then picks up. What is still unenforceable is a commit pushed to that branch
from outside Praxis. See the gotcha in `docs/gotchas.md`.

Toggle it from any client:

```bash
praxis mode on | off | status          # CLI
```
```
GET  /api/settings/auto-delegate        # {enabled, worker:{harness,model}}
PUT  /api/settings/auto-delegate        # {enabled: true|false}
```

The MCP `get_mode` tool returns the same `{enabled, worker}` shape, so an MCP-driven brain can
check whether it should delegate before touching code.

Four mechanisms make the mode work:

1. **Global default worker (fallback).** The delegated worker is resolved from
   `default_worker_harness` / `default_worker_model` in `config/praxis.yaml` (reference config:
   the `agy` harness driving `Gemini 3.7 Flash (High)`). A project registered without its own
   `model_name` falls back to this default, so you can `praxis add-project` and start delegating
   immediately. The product default outside this mode stays OpenCode.
2. **Single-branch discipline.** `dispatch_pending_tasks` reads
   `EffectiveSettings.auto_delegate_enabled()` and, when ON, reuses one caller-named work branch
   (base = the project default branch) instead of a fresh `agent/{slug}` per task. It threads
   `single_branch=True` into `AgentManager.spawn_agent`, which sets `SINGLE_BRANCH=1` in the
   container. Both harness entrypoints honor the flag: reuse the existing remote branch and
   non-force push onto it rather than cutting a new one.
3. **Stale-branch sweeper.** `core/branch_sweeper.dead_branches` selects reclaimable work
   branches (no open PR, no live run, never a protected branch) and the reconcile loop
   deletes them via `orchestrator_reconcile.sweep_dead_branches`, a MODULE-LEVEL function
   that `reconcile_runs` calls bare rather than a method on `ReconcileMixin`; patch it on
   the module or the patch does nothing. It is fail-safe: a sweep error is logged and never
   wedges the loop. The ledger it reads is PER REPOSITORY: the queries used to be global
   while the sweep runs per remote, so a failed task in one repository nominated an
   identically named branch in another for deletion.
4. **Per-task review scoping.** Every task on the shared branch is reviewed on the commits
   it added after the branch head recorded at its dispatch (`tasks.review_base_sha`).
   Without it the reviewer saw the whole accumulated branch and failed every task after the
   first for touching files outside its scope, and the outcome recorder wrote that FAIL
   against a worker that had done its own task correctly.

## Per-Project Settings

Each registered repository can be configured with:

| Setting | Default | Description |
|---------|---------|-------------|
| `approval_gate` | `true` | Require user approval for autonomous improvements |
| `confidence_threshold` | `0.7` | Minimum confidence for Opus to propose improvements |
| `max_retries` | `3` | Max re-dispatches per failed code review |
| `max_improvement_cycles` | `5` | Hard cap on autonomous improvement loops |
| `model_name` | *(required)* | LLM model identifier for the implementer agent |
| `harness` | `opencode` | Implementer harness: `opencode` (default) / `agy` |
| `agent_model` / `agent_model_effort` | *(inherited)* | Per-project override of the implementer model/effort |

Global brain call-site models (planning, review, classify, derive) are configured in
**Settings → Models** (per-call-site provider/model/effort with defaults + Reset), and
global orchestrator settings come from `config/praxis.yaml` (env-overridable).
