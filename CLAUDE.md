# CLAUDE.md — Praxis

## What Is This

Praxis is a Docker-based AI agent orchestrator. Claude Opus (via `claude -p` CLI, subscription)
handles planning and code review. Local LLM (LM Studio + Aider in Docker containers) handles
implementation.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11, FastAPI, Uvicorn |
| Database | SQLite (aiosqlite, raw SQL, no ORM) |
| CLI | Typer + rich |
| Web UI | Single-file HTML/CSS/JS (`web/index.html`) |
| Containers | Docker SDK for Python |
| Agent | Aider (custom Docker image) |
| LLM (plan/review) | Claude Opus via `claude -p` (subscription) |
| LLM (implement) | Local model via LM Studio (OpenAI-compatible) |
| Git/GitHub | git CLI, gh CLI |
| Reverse Proxy | Caddy (auto-HTTPS, hosted profile) |

## Project Structure

```
praxis/
├── src/
│   ├── orchestrator/
│   │   ├── main.py                  # FastAPI app + lifespan (seeds default user)
│   │   ├── config.py                # Settings via pydantic-settings
│   │   ├── database.py              # SQLite connection + inline migrations
│   │   ├── api/
│   │   │   ├── projects.py          # /api/projects CRUD
│   │   │   ├── plans.py             # /api/plans + approve/reject + /api/plans/promote
│   │   │   ├── lifecycle.py         # /api/projects/{id}/lifecycle + /doc-raw (Spec→Plan→Run)
│   │   │   ├── tasks.py             # /api/tasks + logs streaming
│   │   │   ├── system.py            # /api/status, /api/opus/state
│   │   │   ├── events.py            # /api/events SSE stream
│   │   │   ├── internal.py          # /api/internal/agent-done callback
│   │   │   └── auth.py              # Bearer token validation
│   │   ├── core/
│   │   │   ├── orchestrator.py      # Main loop: plan -> dispatch -> review -> improve
│   │   │   ├── task_queue.py        # Task state machine + scheduling
│   │   │   ├── opus_bridge.py       # claude -p invocation + rate limit handling
│   │   │   ├── agent_manager.py     # Docker container lifecycle
│   │   │   ├── git_ops.py           # Branch, PR, merge, conflict ops
│   │   │   ├── plan_derive.py       # plan.md -> opus_plan (deterministic parse + LM Studio fallback)
│   │   │   └── event_bus.py         # In-memory async pub/sub for SSE
│   │   └── models/
│   │       └── schemas.py           # Pydantic request/response + Opus JSON payloads
│   └── cli/
│       └── main.py                  # Typer CLI client (entrypoint: orchestrator-cli)
├── web/
│   └── index.html                   # Single-file dashboard (dark/light theme)
├── docker/
│   ├── orchestrator/Dockerfile
│   ├── aider-agent/
│   │   ├── Dockerfile
│   │   └── entrypoint.sh
│   ├── opencode-agent/
│   │   ├── Dockerfile
│   │   └── entrypoint.sh
│   ├── openhands-agent/
│   │   ├── Dockerfile
│   │   └── entrypoint.sh
│   └── caddy/Caddyfile
├── tests/                           # 200 tests, 88% coverage
├── docker-compose.yml               # Production compose
├── docker-compose.local.yml         # Dev overrides (hot reload, mounted source)
├── pyproject.toml
├── .env.example
└── .python-version                  # 3.11
```

## Commands

```bash
# Setup
uv venv && uv sync --extra dev && cp .env.example .env

# Run locally
uv run uvicorn orchestrator.main:app --host 127.0.0.1 --port 8080

# Tests
uv run pytest --cov=orchestrator --cov-report=term-missing -v

# Lint & format
uv run ruff format src/ tests/
uv run ruff check --fix src/ tests/

# Type check
uv run mypy src/orchestrator/ --ignore-missing-imports

# Docker
docker compose up --build                              # local mode
docker compose --profile hosted up --build             # with Caddy
```

## Task State Machine

```
PENDING -> IN_PROGRESS -> REVIEWING -> PASSED -> MERGED
                                    -> FAILED -> (re-dispatch, max 3)
```

## Data Model (SQLite)

Tables: `users`, `projects`, `plans`, `tasks`, `agent_runs`, `opus_state`,
`doc_index`, `settings_overrides`

- A default `admin` user is auto-seeded on first startup (token_hash = AUTH_TOKEN)
- `opus_state` is a singleton row tracking `available` / `rate_limited` / `resuming`
- **`plans.spec` was dropped (Spec 2)** — markdown docs are the source of truth, so the
  redundant free-text `spec` content column is gone. The DB is a thin execution ledger:
  `plans` keeps `opus_plan` (the runtime task graph — `TaskQueue.get_dispatchable_tasks`
  reads it to resolve `depends_on` ordering, so it is NOT redundant), `spec_path`,
  `plan_path`, status, branch. `initialize()` drops legacy `spec` via the SQLite
  table-rebuild pattern (CREATE new, INSERT…SELECT, DROP, RENAME), guarded by a
  `PRAGMA table_info` check so it only runs on pre-Spec-2 DBs. A `before_drop` callback
  (wired in `main.py` lifespan to `core/backfill.backfill_legacy_specs`) runs first,
  writing any orphaned legacy spec text to a `*-legacy.md` spec doc and setting
  `spec_path`, so no content is lost. Backfill is skipped when no `GITHUB_TOKEN`.

## Key Design Decisions

- **No ORM** — raw SQL via aiosqlite, migrations as inline CREATE TABLE IF NOT EXISTS
- **Global settings layer (Spec 2)** — git-trackable defaults live in `config/praxis.yaml`
  (loaded by `core/settings_file.load_yaml_settings`); `Settings.__init__` overlays them
  beneath env vars, so precedence is **env > YAML > field default**. Keys map by uppercase
  field name (e.g. `loop_interval` ← `PRAXIS_LOOP_INTERVAL` in the YAML loader, `LOOP_INTERVAL`
  for the pydantic env layer). Project config stays DB-backed.
- **Single FastAPI monolith** — no microservices for v1
- **Docker SDK** for spawning Aider containers programmatically
- **`claude -p`** for all Opus interactions (planning, review, improvement analysis)
- **Rate limit handling** — detect 5h subscription limit, queue Opus calls, auto-resume
- **Two-tier git branching** — `plan/{date}-{slug}` groups tasks, `agent/{task-slug}` per task
- **Single static auth token** for v1 (data model supports multi-user for future)
- **Pluggable harnesses** — `core/harnesses.py` is the registry (image + About
  content) for Aider, OpenCode, OpenHands. Projects pick one via the `harness`
  column (default `aider`). `AgentManager.spawn_agent` selects the image and
  sets a harness-agnostic env contract (`HARNESS`, `MODEL`, `OPENAI_API_BASE`,
  + repo/branch/callback vars). Each `docker/<harness>-agent/` image honors the
  same entrypoint contract.

## Gotchas

- **CLI is at `src/cli/`**, not top-level `cli/`. The entrypoint in pyproject.toml is
  `orchestrator-cli = "cli.main:app"` which works because `[tool.setuptools.packages.find]`
  has `where = ["src"]`
- **ruff subcommand is `ruff format`**, not `ruff fmt` — the installed version uses the
  full command name
- **`.env` file is read by pydantic-settings** as fallback. Tests that assert missing env
  vars raise `ValidationError` must pass `_env_file=None` to `Settings()` to prevent
  fallback reads
- **Default user auto-seeded** — `main.py` lifespan seeds a `default` user on first
  startup. Without it, project creation returns 500 ("No user found")
- **Windows port cleanup** — `kill -9` from bash doesn't work for Windows processes.
  Use `taskkill //PID <pid> //F` to release ports
- **orchestrator.py contains the improvement loop** — there is no separate
  `core/improvement.py` file. All orchestration logic (planning, dispatch, review,
  autonomous improvement) lives in `core/orchestrator.py`
- **SSE endpoint** at `/api/events` stays open indefinitely (long-lived connection).
  The EventBus is in-memory only — events are lost if no subscribers are connected
- **SQLite DB file** is created at `data/orchestrator.db` relative to CWD. The `data/`
  directory is auto-created by lifespan. Delete the file to reset all state
- **Context Sync clones cross-platform** — `Settings.brainstorm_workspace` defaults to
  `tempfile.gettempdir()/praxis-brainstorm` (not hardcoded `/tmp/...`), so the Memory
  view works on Windows. `ContextSync.current()` clones the repo on every open and
  cleans up its `read-{uuid}` dir in a `finally`. Clone/git failures surface as a
  `502` from `GET /api/projects/{id}/context` (handled in `api/context.py`), not an
  opaque 500. The Memory view re-clones on each open — no caching yet
- **Agent runs are reconciled, not fire-and-forget** — `Orchestrator.reconcile_runs()`
  runs every loop pass and at startup. It fails/retries any `running` agent run whose
  container vanished or exited without an `agent-done` callback, and attaches a live-log
  monitor (`monitor_run`) to still-running containers. This is what stops a lost callback
  from wedging a task in `in_progress` forever. Reconciled runs auto-retry up to the
  project's `max_retries` when Docker is available (gated so a broken Docker can't thrash),
  else go terminal `failed`. `_reconcile_exited` waits `_callback_grace` (5s) and re-checks
  status so it never races a real in-flight callback.
- **`agent_log` events are produced by `monitor_run`** — the live-log SSE
  (`/api/tasks/{id}/logs`) streams these and falls back to the run's persisted `logs`
  column (checkpointed each poll), so the panel works even when Docker is down. There is
  no other producer of `agent_log`.
- **Agent callbacks retry with backoff** — each `docker/<harness>-agent/entrypoint.sh`
  `send_callback` retries the POST to `/api/internal/agent-done` up to
  `CALLBACK_MAX_ATTEMPTS` (default 5) until HTTP 200. The orchestrator's reconciliation
  is the backstop if all attempts fail.
- **Aider agent image is standalone** — `aider-agent:latest` is not in docker-compose.
  Build it directly: `docker build -t aider-agent:latest -f docker/aider-agent/Dockerfile docker/aider-agent/`
- **Agent container runs as non-root** — The `agent` user cannot write to `/`.
  Workspace is at `/home/agent/workspace`. Do not change WORKDIR to a root-owned path
- **Agent git auth uses `GH_TOKEN`** — configured via credential helper in entrypoint.
  Without it, HTTPS git operations (clone private repos, push branches) fail with
  "could not read Username"
- **`OPENAI_API_KEY` required by Aider** — even for local LLMs via LM Studio, Aider's
  litellm backend requires a non-empty API key. The entrypoint sets a dummy value
  (`not-needed`) if not provided
- **Aider URL scraping** — Aider auto-detects URLs in prompts and tries to scrape them
  (installing Playwright). Use `--no-browser --no-detect-urls` flags to prevent this
- **Plan branch race condition** — Multiple agents dispatched in parallel may try to
  create the same `plan/` base branch. The entrypoint handles this: if push fails
  (branch already exists), it fetches from remote instead
- **Approve sets plan to ACTIVE immediately** — If the orchestration loop hasn't called
  Opus to break the spec into tasks yet, an ACTIVE plan with no `opus_plan` will trigger
  `plan_and_activate` on the next loop iteration
- **Harness images are standalone** — build each directly, none are in
  docker-compose:
  `docker build -t opencode-agent:latest -f docker/opencode-agent/Dockerfile docker/opencode-agent/`
  (same for `openhands-agent`).
- **OpenCode/OpenHands don't auto-commit** — unlike Aider, their entrypoints run
  `git add -A && git commit` after the agent. A run that produces no changes is
  marked `failed`.
- **OpenHands needs a sandbox runtime** — the image uses `RUNTIME=local` to avoid
  Docker-in-Docker. If unsupported, mount `/var/run/docker.sock` when spawning.
- **Generic MODEL env var** — all harness entrypoints consume `MODEL` (raw model
  name); each adds its own provider prefix (Aider/OpenHands use `openai/`,
  OpenCode uses an `lmstudio/` config provider).
- **Unified Plans view = Spec → Plan → Run lifecycle** — the dashboard's single Plans
  view (no separate Specs/Plan Docs nav items) lists one row per spec doc, joined to its
  plan doc (via `spec_path:` front-matter) and any DB plan (via `plan_path`).
  `GET /api/projects/{id}/lifecycle` aggregates these. Repo markdown is the source of
  truth; lifecycle docs live in the **target repo**, not the orchestrator's local docs
  root — the frontend reads them via `GET /api/projects/{id}/doc-raw?path=` (calls
  `brainstorm.read_doc`), NOT `/api/docs/raw` (which reads the local root).
- **Promote a plan.md into a run** — `POST /api/plans/promote {project_id, plan_path}`
  reads the plan from the repo, derives tasks via `core/plan_derive.derive_opus_plan`
  (deterministic `### Task N` / checkbox parser first; local **LM Studio** JSON-schema
  fallback for unstructured plans — never Opus, extraction must stay free), then reuses
  `TaskQueue.activate_plan` so the Run view is unchanged. Stores `spec_path`/`plan_path`
  on the `plans` row. Idempotent per `plan_path`. Errors: 404 missing doc, 422 zero tasks,
  502 local LLM/clone failure.
- **DocIndexer only scans `specs/` and `plans/` dirs** — top-level `docs/*.md`
  (workflow.md, architecture.md, deployment.md) are excluded to stop reference docs being
  misclassified as plans. `PLAN_BOOTSTRAP` instructs generated plans to stamp
  `spec_path:` front-matter so the Spec↔Plan link is explicit, not filename convention.

## Documentation

- **Architecture & components:** `docs/architecture.md`
- **Workflow & orchestration cycle:** `docs/workflow.md`
- **Deployment, Docker & API reference:** `docs/deployment.md`
- **Design spec:** `docs/superpowers/specs/2026-06-01-ai-agent-orchestrator-design.md`
- **Implementation plans:** `docs/superpowers/plans/` (5 sequential plans)
- **Testing & debugging:** `CLAUDE.local.md`

## Coding Standards

- Python 3.11+, PEP 8, type annotations on all function signatures
- Line length: 88 (ruff default)
- Use `X | Y` union syntax, built-in generics (`list[str]`, not `List[str]`)
- `logging` module only — never `print()` in production
- Google-style docstrings
- Pydantic for API boundaries, dataclasses for internal DTOs
- pytest with 80%+ coverage, `pytest-asyncio` with `asyncio_mode = "auto"`
- Catch specific exceptions, use `raise ... from` for chaining

## GitHub

Repository: https://github.com/adiatmaja/praxis.git
