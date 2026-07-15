# Architecture

## System Overview

```
  ┌─────────────────────────────────────────────────────────────────┐
  │  CLIENTS  (all pure REST clients — no engine logic)            │
  │  ┌────────┐  ┌─────────┐  ┌──────────────┐  ┌───────────────┐  │
  │  │ Web UI │  │Typer CLI│  │  MCP server  │  │ REST API      │  │
  │  │        │  │         │  │ (praxis-mcp) │  │ (Bearer auth) │  │
  │  └───┬────┘  └────┬────┘  └──────┬───────┘  └───────┬───────┘  │
  └──────┼────────────┼──────────────┼──────────────────┼──────────┘
         └────────────┴──────┬───────┴──────────────────┘
                    REST API + SSE
                         │
  ┌──────────────────────▼──────────────────────────────────────────┐
  │  ORCHESTRATOR  (FastAPI + SQLite)                                │
  │                                                                  │
  │  ┌───────────┐  ┌───────────┐  ┌───────────┐  ┌────────────┐   │
  │  │ API Router│  │Task Queue │  │  Agent    │  │   Opus     │   │
  │  │ REST + SSE│  │ (SQLite)  │  │  Manager  │  │   Bridge   │   │
  │  └───────────┘  └───────────┘  └─────┬─────┘  └─────┬──────┘   │
  └──────────────────────────────────────┼───────────────┼──────────┘
                                         │               │
                          ┌──────────────┼──────┐        │
                          ▼              ▼      ▼        │
                  harness-agent  harness-agent  ...   Brain provider CLI
                  (Docker, e.g.  (Docker)             claude / codex / agy
                   opencode)                          (subscription) or local
                          │              │
                          └──────┬───────┘
                                 ▼
                   OpenAI-compatible endpoint
                   (LM Studio / Ollama / hosted)
```

> **Provider-agnostic by design.** The `claude -p` (plan/review) and LM Studio (implement)
> boxes above are the *reference* wiring, not a requirement. Every brain role — plan,
> review, verify, decompose — resolves through `core/llm_router.py` to `{provider, model,
> effort}` and can point at any supported provider (`claude`, `codex`, `agy`, or a `local`
> OpenAI-compatible endpoint). The implementer harness is likewise pluggable (OpenCode,
> Aider, OpenHands) and can drive any OpenAI-compatible endpoint (LM Studio, Ollama, or a
> hosted one). Nothing is hard-wired to a single vendor.

## Components

### API Layer (`src/orchestrator/api/`)

| Module | Endpoints | Auth |
|--------|-----------|------|
| `projects.py` | `POST/GET /api/projects`, `GET/PATCH /api/projects/{id}` | Bearer |
| `plans.py` | `POST /api/projects/{id}/plans`, `GET /api/plans/{id}`, approve/reject, `POST /api/plans/promote` | Bearer |
| `lifecycle.py` | `GET /api/projects/{id}/lifecycle` (Spec→Plan→Run aggregate), `GET /api/projects/{id}/doc-raw` | Bearer |
| `specs.py` | `POST /api/specs` Create-Spec chat + generate_plan | Bearer |
| `docs.py` | `GET /api/docs` index, `GET /api/docs/raw` (orchestrator-local) | Bearer |
| `context.py` | `GET /api/projects/{id}/context` (Memory view) | Bearer |
| `settings.py` | `GET/PUT /api/settings` global/project, `GET/PUT /api/settings/models`, `POST /api/settings/models/reset` | Bearer |
| `harnesses.py` | `GET /api/harnesses` catalog | Bearer |
| `tasks.py` | `GET /api/plans/{id}/tasks`, `GET/POST /api/tasks/{id}`, logs | Bearer |
| `dispatch.py` | `POST /api/dispatch` — single-task plan injection (used by the MCP server) | Bearer |
| `system.py` | `GET /api/status`, `GET /api/opus/state` | Bearer |
| `events.py` | `GET /api/events` (SSE stream) | Bearer |
| `internal.py` | `POST /api/internal/agent-done` | None (agent callback) |

### MCP Server (`src/mcp_server/`)

A standalone stdio MCP adapter (`praxis-mcp` console script) that lets an MCP client (e.g.
Claude Code) act as the brain and dispatch implementation work to a non-Anthropic model
running inside Praxis. It owns **no engine logic** — it is a third REST client alongside the
dashboard and CLI, forwarding every tool call over HTTP with a Bearer token.

| Module | Responsibility |
|--------|---------------|
| `client.py` | `PraxisClient` — async httpx wrapper, env config (`PRAXIS_BASE_URL`/`PRAXIS_AUTH_TOKEN`), Bearer auth, HTTP-status → `PraxisClientError` translation |
| `server.py` | FastMCP server; five tools (`dispatch_task`, `poll_task`, `list_providers`, `get_task_logs`, `cancel_task`) delegating to testable `*_impl` functions. Tool errors are returned as `{error, message}`, never raised |
| `__main__.py` | `praxis-mcp` stdio entry point (`mcp.run()`) |

The only engine-side addition is `POST /api/dispatch`, which injects a one-task `opus_plan`
via `TaskQueue.activate_plan` (Praxis has no direct single-task creation route). Dispatch is
async — `dispatch_task` returns a handle (`{task_id, dashboard_url, status}`); the caller
polls `poll_task` and follows the `dashboard_url` to watch wedged tasks the request/response
MCP transport cannot surface.

### Core Engine (`src/orchestrator/core/`)

| Module | Responsibility |
|--------|---------------|
| `orchestrator.py` | Loop core: `__init__`, `plan_and_activate`, `process_plan_once`, `run_once`, `run_loop`, `shutdown` |
| `orchestrator_dispatch.py` | `DispatchMixin`: `dispatch_pending_tasks`, `_build_worker_bible` |
| `orchestrator_review.py` | `ReviewMixin`: `review_task`, `approve_task_merge`, `reject_task_merge`, `on_plan_completed` |
| `orchestrator_reconcile.py` | `ReconcileMixin`: `reconcile_runs`, `monitor_run`, `_classify_pr_failure` (8 methods total) |
| `orchestrator_improve.py` | `ImprovementMixin`: `check_improvements`, `create_improvement_plan` |
| `task_queue.py` | Plan/task CRUD, state machine, dependency-aware dispatch |
| `opus_bridge.py` | Brain calls (via `llm_router`, or legacy `claude -p`), JSON parsing, rate limit tracking |
| `llm_router.py` | Per-call-site `{provider, model, effort}` routing — CLI (`claude`/`agy`/`codex`) or LM Studio `local`. Raises `ProviderAuthError` on a dead CLI session (stderr auth-scan catches codex's exit-0/401); resolves Windows `.CMD`/`.EXE` shims via `shutil.which` |
| `effective_settings.py` | override(project) → global → default resolution, incl. `call_site_config` |
| `settings_file.py` | Load `config/praxis.yaml`, overlay env (`PRAXIS_*`) |
| `plan_derive.py` | `plan.md` → `opus_plan` (deterministic parse + local LM Studio fallback) |
| `brainstorm.py` | Clone target repo, run `claude -p`, write/list/read lifecycle docs |
| `context_sync.py` | CLAUDE.md/MEMORY.md freshness for the Memory view |
| `doc_indexer.py` | Index `specs/` + `plans/` markdown into `doc_index` |
| `markdown_utils.py` | Pure markdown helpers (title, checklist progress, front-matter) |
| `backfill.py` | One-time legacy `plans.spec` → repo spec doc (Spec 2 migration) |
| `agent_manager.py` | Docker SDK: spawn/stop/cleanup harness containers |
| `harnesses.py` | Harness registry (Aider/OpenCode/OpenHands: image + About) |
| `git_ops.py` | git/gh CLI wrappers: branch, push, PR, merge, diff |
| `event_bus.py` | In-memory async pub/sub for SSE streaming |

### Data Layer

- **SQLite** via aiosqlite (no ORM, raw SQL) — a thin **execution ledger** (Spec 2);
  markdown docs in the target repo are the source of truth for spec/plan content.
- Tables: `users`, `projects`, `plans`, `tasks`, `agent_runs`, `opus_state`,
  `doc_index`, `settings_overrides`
- `plans` keeps `opus_plan` (runtime task graph), `spec_path`, `plan_path`, status,
  branch — the free-text `spec` column was **dropped** in Spec 2.
- Global orchestrator settings live in `config/praxis.yaml` (env-overridable); per-call-site
  model overrides persist in `settings_overrides` (`models.<call_site>`).
- Migrations: inline `CREATE TABLE IF NOT EXISTS` + additive `ALTER`/table-rebuild in `database.py`
- Default admin user auto-seeded on first startup

## Task State Machine

```
PENDING ──► IN_PROGRESS ──► REVIEWING ──► PASSED ──► MERGED
                                      └──► FAILED ──► (re-dispatch, max 3)
```

## Plan Lifecycle

DB plan status:

```
pending ──► active ──► completed
                   └──► rejected (autonomous plans only, via approval gate)
```

### Unified Spec → Plan → Run (Spec 1)

A lifecycle object is anchored on a spec markdown file in the **target project repo**.
The dashboard's single **Plans** view aggregates one row per spec, joined to its plan doc
and any DB run:

```
  docs/**/specs/<slug>.md   ──generate_plan──►   docs/**/plans/<slug>.md
        (Spec)                spec_path: link          (Plan)
                                                          │ Promote to Run
                                                          ▼
                                       derive_opus_plan (parser → local LLM fallback)
                                                          │
                                                          ▼
                                  DB plan (spec_path, plan_path, opus_plan) ──► orchestrator loop
```

- **Spec ↔ Plan** link: `generate_plan` stamps `spec_path:` YAML front-matter into the plan.
- **Plan ↔ Run** link: `POST /api/plans/promote` stores `plan_path`/`spec_path` on the DB plan.
- Promote is idempotent per `plan_path`; derivation errors surface as 404/422/502.

## Rate Limit Handling

Opus Bridge tracks Claude subscription limits:

```
available ──► rate_limited (5h cooldown detected)
                   │
                   ▼
              queued_actions (stored in opus_state.queued_actions JSON)
                   │
                   ▼ (after resume_at)
              resuming ──► available (drain queued actions)
```

## Git Branching Strategy

```
main
 └── plan/2026-06-01-auth-system          (plan branch)
      ├── agent/auth-login                 (task branch -> PR to plan)
      ├── agent/auth-register              (task branch -> PR to plan)
      └── agent/auth-middleware            (task branch -> PR to plan)
```

- Plan branches group related tasks
- Agent branches are isolated per task
- PRs target the plan branch, not main
- Integration PR from plan branch to main after all tasks pass

## Agent Isolation Model

A common worry with parallel work: "if an agent edits my repo while I'm mid-change, does it clobber
my uncommitted work?" It can't. Each coding agent runs in its **own throwaway Docker container** and
does a fresh `git clone` **from the GitHub remote** — it never mounts, opens, or writes your local
checkout. There is no bind mount back to your disk. The only durable output is a pushed
`agent/{task-slug}` branch and its PR; when the container exits, its filesystem is gone.

```
  YOUR MACHINE                                    GITHUB REMOTE
  ┌────────────────────────────┐                 ┌───────────────────────────┐
  │  your local checkout       │                 │  origin/main               │
  │  (edits, uncommitted work) │                 │                            │
  │        UNTOUCHED           │                 │  agent/task-a  ──► PR #1    │
  └────────────────────────────┘                 │  agent/task-b  ──► PR #2    │
              ▲                                   └───────────┬───────────────┘
              │ you pull only when                    ▲       │ clone (read)
              │ you merge a PR                        │       │ push branch (write)
              │                              push ────┘       ▼
  ┌───────────┼───────────────────────────────────────────────────────────────┐
  │  DOCKER (one container per task · no volume mount to your disk)            │
  │                                                                            │
  │   ┌──────────────────────────┐        ┌──────────────────────────┐        │
  │   │ container: task-a         │        │ container: task-b         │       │
  │   │  git clone ──► /home/     │        │  git clone ──► /home/     │       │
  │   │     agent/workspace       │        │     agent/workspace       │       │
  │   │  checkout -b agent/task-a │        │  checkout -b agent/task-b │       │
  │   │  local model edits + commit        │  local model edits + commit       │
  │   │  push branch ──► open PR  │        │  push branch ──► open PR  │       │
  │   └──────────────────────────┘        └──────────────────────────┘        │
  │        (filesystem discarded on exit)                                      │
  └────────────────────────────────────────────────────────────────────────────┘

  Writes land in TWO places, neither is your working tree:
    1. the container's own filesystem  (/home/agent/workspace — ephemeral)
    2. the GitHub remote               (new agent/<slug> branch + PR — durable)
```

This is stronger isolation than a git worktree would give: a worktree shares one `.git` object store
on your machine, so concurrent agents (and you) contend on the same repo. A per-container clone gives
each agent a physically separate `.git` and filesystem, and keeps the **remote** as the only shared
surface. The one deliberate trade-off: because the agent clones from the remote, it sees only
committed-and-pushed code, not your local uncommitted changes — pass reference context explicitly via
`dispatch_task`'s `context` field instead.

Note the primary isolation boundary is the **filesystem**: agent containers run on Docker's default
**bridge** network (not host networking) with `extra_hosts={"host.docker.internal": "host-gateway"}`,
reaching LM Studio and the orchestrator callback via `host.docker.internal` (the LM Studio URL is
rewritten from `localhost`/`127.0.0.1` by `_container_host_url`). This drops blanket host-network
access, but the worker can still reach host-gateway services, so network exposure is reduced, not
eliminated. See the Security Model section of the README before running on a machine with sensitive
unauthenticated local services.

## Deployment Modes

### Local (default)

```
docker compose up --build
```

- Orchestrator on port 8080
- LM Studio on localhost:1234 (host machine)
- Docker socket mounted for agent spawning

### Hosted (with Caddy)

```
DOMAIN=praxis.example.com docker compose --profile hosted up --build
```

- Caddy reverse proxy with auto-HTTPS
- Security headers (HSTS, XSS protection, etc.)

## Capability Engine: Outcome Recording (F5)

When the review phase reaches a terminal verdict for a task, `record_outcome` writes a single
`task_outcomes` row containing the review decision, attribution (via `counts_against_worker`),
and measured diff statistics. The write is fire-and-forget — DB or emit errors are swallowed so
the review pipeline never stalls. On the next decomposition, `decompose_plan` calls
`fetch_recent_outcomes` (scoped to the worker model, widening from the current project to all
projects) and feeds those historical results into the decompose prompt's history slot, allowing
the brain to calibrate leaf sizing against real evidence. Learned Wilson-bound limits and the
`GET /api/capability/{model}` endpoint remain planned for Plan 6.
