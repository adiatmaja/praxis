# CLAUDE.md — Praxis

## What Is This

Praxis is a Docker-based AI agent orchestrator. Claude Opus (via `claude -p` CLI, subscription)
handles planning and code review. A local LLM (LM Studio) drives a pluggable coding harness in
Docker containers to handle implementation (OpenCode is the default; Aider and OpenHands are
optional alternatives).

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11, FastAPI, Uvicorn |
| Database | SQLite (aiosqlite, raw SQL, no ORM) |
| CLI | Typer + rich |
| Web UI | No-build HTML/CSS/JS (`web/index.html` + `styles.css` + `app.js`) |
| Containers | Docker SDK for Python |
| Agent | Pluggable harness — OpenCode (default), Aider, OpenHands (custom Docker images) |
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
│   │   │   ├── specs.py             # /api/specs Create-Spec chat + generate_plan
│   │   │   ├── docs.py              # /api/docs doc index + raw read
│   │   │   ├── context.py           # /api/projects/{id}/context (Memory view)
│   │   │   ├── settings.py          # /api/settings global/project + /settings/models
│   │   │   ├── harnesses.py         # /api/harnesses catalog
│   │   │   ├── tasks.py             # /api/tasks + logs streaming
│   │   │   ├── system.py            # /api/status (CLI-probed), /api/lm-models, /api/opus/state
│   │   │   ├── events.py            # /api/events SSE stream
│   │   │   ├── internal.py          # /api/internal/agent-done callback
│   │   │   └── auth.py              # Bearer token validation
│   │   ├── core/
│   │   │   ├── orchestrator.py          # Loop core: __init__, plan_and_activate, run_once, run_loop, shutdown
│   │   │   ├── orchestrator_dispatch.py # DispatchMixin: dispatch_pending_tasks, _build_worker_bible
│   │   │   ├── orchestrator_review.py   # ReviewMixin: review_task, approve/reject merge, on_plan_completed
│   │   │   ├── orchestrator_reconcile.py# ReconcileMixin: reconcile_runs, monitor_run, _classify_pr_failure
│   │   │   ├── orchestrator_improve.py  # ImprovementMixin: check_improvements, create_improvement_plan
│   │   │   ├── task_queue.py        # Task state machine + scheduling
│   │   │   ├── opus_bridge.py       # claude -p invocation + rate limit handling
│   │   │   ├── llm_router.py        # Per-call-site {provider,model,effort} routing (Spec 3)
│   │   │   ├── effective_settings.py# override(project)→global→default resolution
│   │   │   ├── settings_file.py     # config/praxis.yaml loader + env overrides (Spec 2)
│   │   │   ├── agent_manager.py     # Docker container lifecycle
│   │   │   ├── harnesses.py         # Harness registry (Aider/OpenCode/OpenHands)
│   │   │   ├── git_ops.py           # Branch, PR, merge, conflict ops
│   │   │   ├── brainstorm.py        # Clone repo, run claude -p, write/list/read docs
│   │   │   ├── context_sync.py      # CLAUDE.md/MEMORY.md freshness (Memory view)
│   │   │   ├── doc_indexer.py       # Index specs/ + plans/ markdown into doc_index
│   │   │   ├── markdown_utils.py    # Pure markdown helpers (title, checklist, frontmatter)
│   │   │   ├── plan_derive.py       # plan.md -> opus_plan (deterministic parse + LM Studio fallback)
│   │   │   ├── backfill.py          # One-time legacy plans.spec -> repo doc (Spec 2)
│   │   │   └── event_bus.py         # In-memory async pub/sub for SSE
│   │   └── models/
│   │       └── schemas.py           # Pydantic request/response + Opus JSON payloads
│   ├── mcp_server/
│   │   ├── client.py                # PraxisClient — thin httpx wrapper over REST API
│   │   ├── server.py                # MCP tool definitions (dispatch_task, poll_task, …)
│   │   └── __main__.py              # stdio entry point (praxis-mcp)
│   └── cli/
│       └── main.py                  # Typer CLI client (entrypoint: orchestrator-cli)
├── web/
│   ├── index.html                   # Dashboard HTML (no-build, dark/light theme)
│   ├── styles.css                   # Dashboard CSS (extracted from index.html)
│   └── app.js                       # Dashboard JS (extracted from index.html, classic script)
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
├── config/
│   └── praxis.yaml                  # Global orchestrator settings (env-overridable)
├── tests/                           # 568 tests, 89% coverage
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

# Run — containerized (RECOMMENDED: survives terminal exit, restart: unless-stopped)
docker compose -f docker-compose.yml -f docker-compose.local.yml up --build -d  # dev, hot-reload
docker compose up --build                                                         # production
docker compose --profile hosted up --build                                        # with Caddy

# Run — bare uvicorn (quick one-off only; process dies with the terminal and orphans in-flight tasks)
uv run uvicorn orchestrator.main:app --host 127.0.0.1 --port 8080

# Tests
uv run pytest --cov=orchestrator --cov-report=term-missing -v

# Lint & format
uv run ruff format src/ tests/
uv run ruff check --fix src/ tests/

# Type check
uv run mypy src/orchestrator/ --ignore-missing-imports
```

## CI/CD (GitHub Actions)

Workflows live in `.github/workflows/` (added 2026-07-02, all verified green on runners):

| Workflow | Trigger | Does |
|----------|---------|------|
| `ci.yml` | push main / all PRs | `lint` job (ruff format+check, mypy) on Ubuntu; `test` matrix on **ubuntu-latest + windows-latest** (`pytest --cov-fail-under=80 --timeout=120`) |
| `docker.yml` | `docker/**` changes | shell-checks entrypoints + builds all 4 images. **Orchestrator builds with repo-root context** (its Dockerfile copies `src/`, `web/`, `pyproject.toml` from root); the 3 agent images build from their own dir in a matrix |
| `security.yml` | push / PR / weekly | pip-audit (on `uv export`ed lockfile) + bandit + gitleaks |
| `codeql.yml` | push / PR / weekly | CodeQL security-and-quality |
| `dependency-review.yml` | PRs touching deps | blocks high-severity vulnerable additions (needs repo **Dependency graph** setting on) |
| `actionlint.yml` | `.github/workflows/**` | lints the workflows themselves |

- **bandit config is in `pyproject.toml`** (`[tool.bandit]`): global `skips = ["B404","B603","B607"]` for the product's legitimate subprocess/CLI shell-outs, plus a handful of targeted inline `# nosec` at specific call sites (e.g. `verify_gate.py` B602). Result is 0 findings, so any NEW bandit hit is a real signal, keep the skip list minimal.
- **Dependabot** (`.github/dependabot.yml`): weekly pip/github-actions/docker updates, grouped by risk tier (Actions batched into one PR; dev/linter deps grouped; runtime deps left ungrouped so a breaking prod bump is isolated).

## Task State Machine

```
PENDING -> IN_PROGRESS -> REVIEWING -> PASSED -> (human approve) -> MERGED
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

- **No ORM** — raw SQL via aiosqlite, baseline tables as inline CREATE TABLE IF NOT EXISTS
  (the `CREATE_TABLE_STATEMENTS` tuple, run every startup).
  Schema changes now go through the versioned migration list in `database.py`
  (`MIGRATIONS` + `PRAGMA user_version`, applied at the end of `initialize()`).
  Add a new `Migration(n, desc, fn)` instead of another ad-hoc conditional
  rebuild; steps must be idempotent (re-run safe).
- **Global settings layer (Spec 2)** — git-trackable defaults live in `config/praxis.yaml`
  (loaded by `core/settings_file.load_yaml_settings`); `Settings.__init__` overlays them
  beneath env vars, so precedence is **env > YAML > field default**. Keys map by uppercase
  field name (e.g. `loop_interval` ← `PRAXIS_LOOP_INTERVAL` in the YAML loader, `LOOP_INTERVAL`
  for the pydantic env layer). Project config stays DB-backed.
- **Provider-agnostic LLM router (Spec 3)** — `core/llm_router.py` resolves each brain
  call-site to `{provider, model, effort}` and executes it. `CALL_SITE_DEFAULTS` is the
  model-tiering policy (e.g. `plan_spec`→opus/high, `review_diff_rereview`→haiku,
  `derive_tasks`→local). CLI providers: `claude`, `agy` (Gemini), `codex` (GPT) via
  `build_argv`; `local` routes through an LM Studio OpenAI call. `OpusBridge` takes an
  optional `router` and routes `plan_spec`/`review_diff`(tier first|rereview)/
  `analyze_improvements`/`classify_doc` through it (falls back to the legacy `_run_claude`
  when no router, preserving rate-limit handling). **Brainstorm is NOT routed yet** — its
  `stream-json` output is incompatible with the text-mode `build_argv`; deferred follow-up.
  Per-call-site overrides live in `settings_overrides` (key `models.<call_site>`), resolved
  by `EffectiveSettings.call_site_config` and managed via `GET/PUT /api/settings/models` +
  `POST /api/settings/models/reset`, surfaced in the dashboard **Settings → Models** tab
  (defaults + per-row/all Reset). **Provider status (verified 2026-06-23):** `claude`
  and `local` work; `codex` is wired correctly but needs `codex login` (revoked sessions
  surface as `ProviderAuthError`); `agy` is **unusable** as a brain — its `--print` only
  renders to an interactive TTY and yields no capturable stdout non-interactively (see the
  provider-auth gotcha below).
- **Single FastAPI monolith** — no microservices for v1
- **Docker SDK** for spawning harness agent containers programmatically
- **`claude -p`** for all Opus interactions (planning, review, improvement analysis)
- **Rate limit handling** — detect 5h subscription limit, queue Opus calls, auto-resume
- **Two-tier git branching** — `plan/{date}-{slug}` groups tasks, `agent/{task-slug}` per task
- **Single static auth token** for v1 (data model supports multi-user for future)
- **Pluggable harnesses** — `core/harnesses.py` is the registry (image + About
  content) for Aider, OpenCode, OpenHands. Projects pick one via the `harness`
  column (default `opencode`). `AgentManager.spawn_agent` selects the image and
  sets a harness-agnostic env contract (`HARNESS`, `MODEL`, `OPENAI_API_BASE`,
  + repo/branch/callback vars). Each `docker/<harness>-agent/` image honors the
  same entrypoint contract.

## Gotchas

Full detail for every trap below lives in **`docs/gotchas.md`** — read it before touching
the relevant subsystem. Condensed index:

- **Merge is gated by default** — review PASS parks at `PASSED`, never auto-merges; needs
  `approve-merge` or `auto_merge=True` (protected branches never auto-merge). `core/merge_policy.py`.
- **CLI is at `src/cli/`** (not top-level), works via `where = ["src"]` in pyproject.
- **`ruff format`, not `ruff fmt`**; **claude effort flag is `--effort`, not `--reasoning-effort`**.
- **`.env` read by pydantic-settings** — tests asserting missing env must pass `_env_file=None`.
- **Default user auto-seeded** in lifespan; without it project creation 500s.
- **Windows port cleanup** uses `taskkill //PID <pid> //F`, not `kill -9`.
- **Orchestrator is split across mixins** (`orchestrator_{dispatch,review,reconcile,improve}.py`);
  patch module-level helpers on the MIXIN module, not `core.orchestrator`.
- **SSE `/api/events`** is long-lived; EventBus is in-memory (events lost with no subscriber).
- **SQLite DB** at `data/orchestrator.db` (CWD-relative); delete to reset state.
- **Context Sync / Memory view** re-clones per open (cross-platform temp dir), surfaces 502 not 500.
- **Agent runs are reconciled** (`reconcile_runs`), not fire-and-forget — the backstop for lost callbacks.
- **`agent_log` events come only from `monitor_run`**, attached at spawn AND in reconcile.
- **Agent container names are reused** — spawn force-removes stale `praxis-agent-*` (409 otherwise).
- **Callback URL is port-derived** (`Settings.callback_url()`); wrong port → every callback 404s.
- **Brain prompts go via stdin, never argv** (argv overflows OS limit, Windows `WinError 206`).
- **`gh pr` calls need `--repo <owner/name>`** or they target the orchestrator's own cwd.
- **Agent callbacks retry with backoff**; `/api/internal/agent-done` fails closed (503) w/o secret.
- **Agent containers use bridge net + `host.docker.internal`**, not `network_mode=host`.
- **Harness images are standalone (NOT in compose)** — rebuild `aider-agent:latest` etc. after
  ANY `entrypoint.sh` change or a stale image runs silently. Read baked files via `docker cp`.
- **Agent runs non-root** — workspace `/home/agent/workspace`; **git auth via `GH_TOKEN`**;
  **Aider needs a dummy `OPENAI_API_KEY`** + `--no-browser --no-detect-urls`.
- **GitHub creds via provider seam** (`core/github_credentials.py`) — App installation tokens
  (short-lived, repo-scoped) or `GITHUB_TOKEN` PAT fallback; install tokens cap at 1h.
- **Plan branch race** handled by fetch-fallback on push failure.
- **OpenCode/OpenHands don't auto-commit** (entrypoint does); **OpenCode needs `limit.output`**;
  **Static Bible must NOT land in the PR** (entrypoint strips it from `AGENTS.md`).
- **PR body uses `TASK_SUMMARY`**, not a `TASK_PROMPT` slice. **`MODEL` env is provider-prefixed per harness.**
- **Unified Plans view = Spec→Plan→Run**; lifecycle docs live in the TARGET repo (read via `/doc-raw`).
- **Promote plan.md** via `POST /api/plans/promote` (deterministic parse → LM Studio fallback, never Opus).
- **DocIndexer scans only `specs/`+`plans/`**; plans need `spec_path:` front-matter + 4-backtick outer fences.
- **`/api/status` Planner availability is CLI-probed** (`claude --version`), not DB-only.
- **Dashboard LM Studio URL is the effective (global) one**; New-Project model is a `/api/lm-models` dropdown.
- **Create-Spec chat needs the SSE stream open** (`openSpecChat` re-opens it); errors via `brainstorm_error`.
- **Provider auth is detected, never automated** — `ProviderAuthError` on dead session; codex exits 0 on
  401 (stderr-scanned); Windows shims resolved via `shutil.which`; agy unusable as a brain.
- **MCP server is a separate package** (`src/mcp_server/`); read-back tools
  `get_project`/`list_projects` wrap `GET /api/projects` client-side (no new REST
  endpoint). Orchestrators resolve a repo's configured worker `model` via
  `get_project` before `execute_plan`/`dispatch_task` (see the orchestration guide).
- **`dispatch` `branch` is always a base**, never a target (re-dispatch = new PR).
- **`execute_plan` bridges brain ids → slugs** (`_normalize_slugs`) or the dispatch loop `KeyError`s.
- **Login banner is SSE-driven** (`provider_auth_required`), not just poll (`codex login status` lies).
- **Mechanical verify gate runs before the brain** (`core/verify_gate.py`) — non-zero exit fails free.
- **Build stamp on /health + /api/status** (`core/build_info.py`) exposes running commit; restart after deploy.
- **Decomposition emits per-leaf `plan_text`** (verbatim contract) so review checks against the real spec.
- **`opus_bridge.py` + `users.token_hash` are legacy names on purpose** (renames deferred as churn).
- **Blocked workers ask, they don't guess** — `Status: BLOCKED`/`NEEDS_CONTEXT` → `NEEDS_CLARIFICATION`
  (no retry burned) → brain `answer_clarification` → re-dispatch or human gate. All three harnesses parse it.
- **Remote preflight is shared** (`core/preflight.py`) — every dispatch path runs
  cheap, read-only remote checks before spawning a container. Non-GitHub URL, auth
  failure, missing branch or file return 422. Unreachable remote returns 502. Base-SHA
  mismatch returns 409. Without a configured credential, checks are skipped with a
  warning so local-only experimentation still works.

## Documentation

- **Architecture & components:** `docs/architecture.md`
- **Workflow & orchestration cycle:** `docs/workflow.md`
- **Deployment, Docker & API reference:** `docs/deployment.md`
- **Gotchas (full narrative):** `docs/gotchas.md`
- **Design spec:** `docs/superpowers/specs/2026-06-01-ai-agent-orchestrator-design.md`
- **Implementation plans:** `docs/superpowers/plans/` (sequential plans)
- **Implemented + merged (2026-06-29 epic, live e2e-verified 2026-07-01):** worker context
  continuity (`specs/2026-06-29-worker-context-continuity-design.md`), capability-aware plan
  execution (`specs/2026-06-29-capability-aware-execution-design.md`), and the MCP orchestration
  guide (`2026-06-29-mcp-orchestration-guide-*`), each with a matching plan in
  `docs/superpowers/plans/2026-06-29-*`. Delivers the Static Bible + git-spine progress handover,
  pre-flight token budgeting, the `execute_plan` entry point (REST + MCP) that capability-gates an
  externally-authored plan against the local model before dispatch, and the static MCP **resource**
  `praxis://guide/orchestration`. A live run on `openclaw-telegram` with `qwen3.6-27b` drove
  `execute_plan` end-to-end (brain decompose → local worker implement → review → squash-merge);
  the run also surfaced + fixed the `--effort`/`limit.output`/`_normalize_slugs` bugs above.
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
