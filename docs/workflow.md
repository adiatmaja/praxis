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

## Two entry paths

1. **Direct spec → run (legacy):** submit a spec and let Opus plan it (steps below).
2. **Doc-driven Spec → Plan → Run (preferred, Spec 1):** Create a spec via the chat
   (committed to `docs/**/specs/`), Generate Plan (committed to `docs/**/plans/` with a
   `spec_path:` link), then **Promote to Run** — `POST /api/plans/promote` reads the
   `plan.md`, derives tasks (`plan_derive`: deterministic parser → local LM Studio
   fallback), and creates a DB plan that feeds the same orchestration cycle from step 3.
   Markdown docs in the target repo are the source of truth; the DB is an execution ledger.

## Step-by-Step

1. **Spec submission** — User submits a specification via Web UI, CLI, or REST API,
   targeting a registered GitHub repository (or promotes an existing `plan.md`).

2. **Planning** — Orchestrator routes the planning call through the `LLMRouter`
   (default `claude-opus-4-8`; configurable per call-site in Settings → Models). It
   returns a JSON plan with a summary, slug, and task list with dependency graph. (For
   the Promote path, tasks are derived locally instead — no Opus call.)

3. **Plan activation** — Orchestrator creates a `plan/{date}-{slug}` branch from main,
   stores the parsed plan in SQLite, and marks the plan as `active`.

4. **Task dispatch** — For each task with no unmet dependencies, orchestrator spawns
   a Docker container running the project's selected harness (Aider / OpenCode /
   OpenHands). The container:
   - Clones the repo
   - Creates an `agent/{task-slug}` branch from the plan branch
   - Runs the selected harness with the task description as the prompt
   - Commits, pushes, and creates a PR targeting the plan branch
   - Calls back to `/api/internal/agent-done` when finished

5. **Code review** — Orchestrator fetches the PR diff via `gh pr diff` and routes it
   to the review call-site (`review_diff_first` → Sonnet by default; re-reviews →
   `review_diff_rereview` → Haiku). Returns a JSON verdict: `pass` or `fail` with feedback.

6. **Pass** — PR is squash-merged into the plan branch. Agent branch is deleted.

7. **Fail** — Feedback is posted as a PR comment. Task is re-dispatched with the
   feedback included in the prompt (max 3 retries).

8. **Integration** — When all tasks are merged into the plan branch, an integration
   PR is created targeting main.

9. **Improvement loop** (optional) — If autonomous improvement is enabled, Opus
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

All three harnesses (aider, opencode, openhands) parse the FINAL REPORT for
block signals and route them through the same harness-agnostic callback.

## Per-Project Settings

Each registered repository can be configured with:

| Setting | Default | Description |
|---------|---------|-------------|
| `approval_gate` | `true` | Require user approval for autonomous improvements |
| `confidence_threshold` | `0.7` | Minimum confidence for Opus to propose improvements |
| `max_retries` | `3` | Max re-dispatches per failed code review |
| `max_improvement_cycles` | `5` | Hard cap on autonomous improvement loops |
| `model_name` | *(required)* | LLM model identifier for the implementer agent |
| `harness` | `opencode` | Implementer harness: `aider` / `opencode` / `openhands` |
| `agent_model` / `agent_model_effort` | *(inherited)* | Per-project override of the implementer model/effort |

Global brain call-site models (planning, review, classify, derive) are configured in
**Settings → Models** (per-call-site provider/model/effort with defaults + Reset), and
global orchestrator settings come from `config/praxis.yaml` (env-overridable).
