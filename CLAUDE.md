# CLAUDE.md — Praxis

## What Is This

Praxis is a capability-aware, provider-agnostic, Docker-based AI software engineering
orchestrator. It splits the software engineering loop into four roles — **plan**, **implement**,
**review**, and **verify** — each independently configurable to any provider, model, or harness,
and decomposes every plan to fit the capability of the model that implements it. Nothing is
hard-wired to a single vendor.

**Framing (canonical, 2026-07-11):** capability-aware task decomposition is THE main feature and
leads in all public docs; role separation is the supporting architecture; cost is a consequence.
Flagship under development: the **Capability Calibration Loop**. Roadmap + feature/contract
designs: `docs/superpowers/specs/2026-07-11-capability-engine-roadmap.md` (F1-F15, S1-S11,
10-plan breakdown).

The reference configuration pairs a subscription reasoning model (e.g. Claude via `claude -p`) for
planning/review with an open-weight model (e.g. via LM Studio) driving a pluggable coding harness
in Docker for implementation, but any role can point at any supported provider (`claude`, `codex`,
`agy`, or a `local` OpenAI-compatible endpoint) via the LLM router. Harnesses are pluggable too:
OpenCode is the default; Antigravity (agy/Gemini) is the experimental alternative. Cost efficiency is a
consequence of this flexibility, not the constraint.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11, FastAPI, Uvicorn |
| Database | SQLite (aiosqlite, raw SQL, no ORM) |
| CLI | Typer + rich |
| Web UI | No-build HTML/CSS/JS (`web/index.html` + `styles.css` + `app.js`) |
| Containers | Docker SDK for Python |
| Agent | Pluggable harness — OpenCode (default), agy/Antigravity (custom Docker images) |
| LLM (any role) | Provider-agnostic via the LLM router — `claude`, `codex`, `agy`, or `local` (OpenAI-compatible); per-call-site configurable |
| LLM (reference: plan/review) | Subscription reasoning model, e.g. Claude via `claude -p` |
| LLM (reference: implement) | Open-weight model, e.g. via LM Studio (OpenAI-compatible) |
| Git/GitHub | git CLI, gh CLI |
| Reverse Proxy | Caddy (auto-HTTPS, hosted profile) |

## Project Structure

```
praxis/
├── src/
│   ├── orchestrator/
│   │   ├── main.py           # FastAPI app + lifespan (seeds the default user)
│   │   ├── config.py         # pydantic-settings; env > config/praxis.yaml > default
│   │   ├── database.py       # SQLite, raw SQL, versioned MIGRATIONS list
│   │   ├── api/              # REST routers, one file per surface
│   │   ├── core/             # the engine (table below)
│   │   └── models/schemas.py # Pydantic boundary types + the LeafTask contract
│   ├── mcp_server/           # stdio MCP adapter over the REST API (praxis-mcp)
│   └── cli/main.py           # Typer CLI (entrypoints: praxis, orchestrator-cli)
├── web/                      # no-build dashboard: index.html + styles.css + app.js
├── docker/                   # orchestrator + opencode-agent + agy-agent + caddy
├── config/praxis.yaml        # global settings, MOUNTED read-only, never baked
├── bench/                    # SWE-bench-style evaluation harness
├── tests/                    # pytest suite
└── docker-compose.yml + docker-compose.local.yml
```

`api/` routers: `projects`, `plans`, `lifecycle`, `specs`, `docs`, `context`, `settings`,
`presets`, `harnesses`, `tasks`, `dispatch`, `execute_plan`, `git_state`, `system`,
`doctor`, `approvals`, `events` (SSE), `internal` (agent callback), `auth`.

`core/` is the engine. The orchestrator is ONE class split across mixins, so patch
module-level helpers on the mixin that calls them, never on `core.orchestrator`:

| Module | Role |
|--------|------|
| `orchestrator.py` | loop core: `plan_and_activate`, `run_once`, `run_loop`, `shutdown` |
| `orchestrator_dispatch.py` | `dispatch_pending_tasks`, `_build_worker_bible`, wave verify gate |
| `orchestrator_review.py` | `review_task`, merge approval, `on_plan_completed` |
| `orchestrator_reconcile.py` | `reconcile_runs`, `monitor_run`, stale-branch sweep |
| `orchestrator_improve.py` | autonomous improvement loop |

The rest of `core/`, grouped by concern:

- **Routing & settings:** `llm_router`, `roles`, `effective_settings`, `settings_file`,
  `opus_bridge` (legacy name, all brain calls), `thinking`, `provider_errors`,
  `escalation`, `worker_presets`, `bench_mode`
- **Execution:** `task_queue`, `agent_manager`, `agent_prompt`, `worker_bible`, `harnesses`,
  `preflight`, `session_resume`, `progress_handover`, `clarification_states`, `token_budget`
- **Git & platform:** `git_ops`, `git_backend` (GitHub / local), `github_credentials`,
  `repo_url_policy`, `merge_policy`, `branch_sweeper`, `diff_guard`, `diff_stats`
- **Capability engine:** `execute_plan_decompose`, `plan_derive`, `plan_graph`, `plan_review`,
  `leaf_validator`, `leaf_templates`, `leaf_split`, `leaf_triage`, `difficulty`,
  `capabilities`, `capability_events`, `capability_history`, `outcome_recorder`,
  `failure_taxonomy`, `status_vocab`
- **Docs & context:** `brainstorm`, `context_sync`, `context_scrub`, `doc_indexer`,
  `markdown_utils`, `backfill`
- **Operability:** `doctor`, `doctor_probes`, `verify_gate`, `build_info`, `entrypoint_hash`,
  `approvals`, `event_bus`, `log_context`

## Commands

```bash
# Setup (one command, idempotent, ends by verifying)
uv run praxis init

# Diagnose (read-only, exits non-zero on any red)
uv run praxis doctor

# See what is parked at the merge gate
uv run praxis pending

# Setup, manual equivalent of `praxis init`
uv venv && uv sync --extra dev && cp .env.example .env

# Build every image AgentManager can spawn (agent images are behind a profile)
docker compose --profile agents build

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

- **bandit config is in `pyproject.toml`** (`[tool.bandit]`): global `skips = ["B404","B603","B607"]` for the product's legitimate subprocess/CLI shell-outs, plus a handful of targeted inline `# nosec` at specific call sites (e.g. `verify_gate.py` B602). Result was 0 findings (verified 2026-07-02), so any NEW bandit hit is a real signal, keep the skip list minimal.
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
  content) for OpenCode and agy. Projects pick one via the `harness`
  column (default `opencode`). `AgentManager.spawn_agent` selects the image and
  sets a harness-agnostic env contract (`HARNESS`, `MODEL`, `OPENAI_API_BASE`,
  + repo/branch/callback vars). Each `docker/<harness>-agent/` image honors the
  same entrypoint contract.

## Auto-Delegate Mode (daily-dev)

When Praxis auto-delegate mode is ON (`GET /api/settings/auto-delegate` → `enabled:true`),
the brain does NOT edit code directly. For each implementation task it designs the worker
prompt, calls the MCP `dispatch_task` (which uses the global default worker — reference:
Gemini 3.7 Flash High via agy), then reviews the resulting PR. Planning, prompt design, and
review stay with the brain. Mode is sequential (one delegate in flight at a time) and uses a
single caller-named work branch; dead branches are swept by the reconcile loop. Toggle:
`praxis mode on|off|status`.

## Gotchas

**`docs/gotchas.md` is the full list (100 traps, with the narrative for each). Read the
relevant entry there before touching a subsystem.** Below are only the ones that bite during
ordinary edits, because each fails SILENTLY. When you learn a new one, write it there and add
a line here only if it belongs in this shortlist.

**Editing and running**

- **`ruff format`, not `ruff fmt`**; the claude effort flag is **`--effort`**, not `--reasoning-effort`.
- **CLI lives at `src/cli/`** (not top-level), which works via `where = ["src"]` in pyproject.
- **The orchestrator is one class across mixins**: patch module-level helpers on the MIXIN
  module that calls them (`orchestrator_{dispatch,review,reconcile,improve}.py`), not on
  `core.orchestrator`.
- **`.env` is read by pydantic-settings**, so tests asserting a missing env var must pass
  `_env_file=None`. Ambient env beats it too: clear the var in the test, or CI's value wins.
- **SQLite lives at `data/orchestrator.db`** (CWD-relative); delete it to reset all state.
- **Windows port cleanup is `taskkill //PID <pid> //F`**, never `kill -9`.
- **`.gitattributes` pins the working tree to LF.** The Windows CI runner checks out with
  `core.autocrlf=true`; CRLF silently breaks any `entrypoint.sh` executed by a test.

**Config and deployment**

- **`config/praxis.yaml` is MOUNTED, not baked**: a YAML edit needs `docker compose restart`,
  never a rebuild. `core/settings_file.config_file_path()` is the ONLY place that path is
  decided; a hardcoded `"config/praxis.yaml"` literal in `src/` reintroduces a fixed bug and
  `tests/test_config_path.py` greps for exactly that.
- **`docker compose restart` does NOT re-read `.env`; only `up -d` does.** The `env_drift`
  doctor check exists for this.
- **Agent images are standalone, NOT in compose**: rebuild `opencode-agent:latest` /
  `agy-agent:latest` after ANY `entrypoint.sh` change or a stale image runs silently.
  Staleness is judged by CONTENT (the `org.praxis.entrypoint-sha256` label), never mtime.
  Rebuild via `praxis init` then `docker compose --profile agents build`: a bare `docker build`
  leaves that label EMPTY (it is a build ARG), which reads as "cannot judge", not as fresh.
- **Worker preset env vars reach the container as BARE compose pass-throughs**
  (`- DEFAULT_WORKER_HARNESS`), never `${VAR:-default}`: any expansion form sets the variable
  even when unset, which silently suppresses the mounted YAML.
- **`praxis doctor` is the front door to every problem**: twelve read-only checks; pure
  decision logic in `core/doctor_probes.py`, live fact gathering in `api/doctor.py`.

**The loop**

- **Merge is gated by default**: a review PASS parks at `PASSED` and never auto-merges
  (`core/merge_policy.py`); protected branches never auto-merge at all.
- **Brain prompts go via stdin, never argv** (argv overflows the OS limit; Windows `WinError 206`).
- **`gh pr` calls need `--repo <owner/name>`** or they target the orchestrator's own cwd.
- **Verify gates FAIL CLOSED and fetch the branch first**: treat `error` like `failed`, and
  only `skipped` passes through. An `error` is never memoized, so the next tick retries.
- **`get_dispatchable_tasks` maps `opus_plan["tasks"]` to rows BY LIST INDEX**: anything
  touching the graph (e.g. `core/leaf_split.py`) must only APPEND; supersede, never delete.
- **Hand-built LM Studio payloads must state `reasoning_effort` explicitly**
  (`core/thinking.py` is the SSoT): an absent key means MAXIMUM effort, not off.
- **Worker effort is PER-HARNESS and must be stated too**: `core/harnesses.py` declares each
  harness's `effort_channel`, `core/worker_effort.py` resolves it. `None` means "this harness
  has no knob", NOT "off". The OpenCode config key is camelCase `reasoningEffort`; snake_case
  is the wire field and is silently ignored in the config file.
- **An omitted `harness` must never downgrade a project**: `execute_plan` and `dispatch` pass
  `None` through, so an existing project keeps its configured harness and only a NEW project
  falls back to the registry default.
- **Agent runs non-root** in `/home/agent/workspace`, with git auth via `GH_TOKEN`.

**Contracts that break fixtures**

- **`LEAF_SCHEMA_VERSION` is 2**: a new `LeafTask` field, even with a default, changes
  `model_dump()` and breaks `tests/fixtures/decompose/expected_leaf_graph.json`; regenerate
  it in the same commit.
- **The status vocabulary is frozen in `core/status_vocab.py`**: add a value to the enum AND
  its exhaustive `test_schemas` assertion together, never one alone.
- **`core/leaf_templates.py` is the single source of per-`LeafType` section requirements**;
  the F3 validator enforces them and a missing section raises at test time.

## Documentation

- **Architecture & components:** `docs/architecture.md`
- **Workflow & orchestration cycle:** `docs/workflow.md`
- **Deployment, Docker & API reference:** `docs/deployment.md`
- **Decomposition standard (cited contract):** `docs/decomposition-standard.md`
- **Configuration surface (seats, presets, arrangements):** `docs/configurations.md`
- **Gotchas (full narrative):** `docs/gotchas.md`
- **Design spec:** `docs/superpowers/specs/2026-06-01-ai-agent-orchestrator-design.md`
- **Capability-engine roadmap (canonical, 2026-07-11):** `docs/superpowers/specs/2026-07-11-capability-engine-roadmap.md`: features F1-F15, standardization contracts S1-S11, 10-plan breakdown; next up = Plan 3 `outcome-recording`
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
