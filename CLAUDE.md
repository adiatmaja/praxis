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
│   │   │   ├── harnesses.py         # Harness registry (OpenCode/agy)
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
│   ├── opencode-agent/
│   │   ├── Dockerfile
│   │   └── entrypoint.sh
│   ├── agy-agent/
│   │   ├── Dockerfile
│   │   └── entrypoint.sh
│   └── caddy/Caddyfile
├── config/
│   └── praxis.yaml                  # Global orchestrator settings (env-overridable)
├── tests/                           # pytest suite (current count/coverage: see CI)
├── docker-compose.yml               # Production compose
├── docker-compose.local.yml         # Dev overrides (hot reload, mounted source)
├── pyproject.toml
├── .env.example
└── .python-version                  # 3.11
```

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
Gemini 3.6 Flash High via agy), then reviews the resulting PR. Planning, prompt design, and
review stay with the brain. Mode is sequential (one delegate in flight at a time) and uses a
single caller-named work branch; dead branches are swept by the reconcile loop. Toggle:
`praxis mode on|off|status`.

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
- **Harness images are standalone (NOT in compose)** — rebuild `opencode-agent:latest` or `agy-agent:latest`
  after ANY `entrypoint.sh` change or a stale image runs silently. Read baked files via `docker cp`.
- **agy harness auth is a login-seeded Docker VOLUME, never host-path creds** — agy ignores
  `GEMINI_API_KEY`/ADC (upstream issue #78) and cross-OS host `~/.gemini` files are the wrong
  format. The working model (live-verified 2026-07-16): chown the `praxis-gemini-creds` volume
  to the agent user, run a one-time interactive `agy login` into it, then the orchestrator mounts
  it **read-write** at `/home/agent/.gemini` (`GEMINI_CREDS_VOLUME`). A fresh `agy -p` worker reads
  those Linux-native creds back and authenticates (issue #479's "write-only" claim does NOT bite
  v1.1.2 with a persisted volume). RW is required so ~1h token refreshes persist. Setup is
  identical on every OS — see `docs/deployment.md`. **`agy -p` still needs valid creds present or
  it hangs with no stdout and ignores `timeout` (forks a detached child); it is TTY-oriented.**
- **Agent runs non-root** — workspace `/home/agent/workspace`; **git auth via `GH_TOKEN`**;
  **agy needs valid creds in the `praxis-gemini-creds` volume** (see agy harness gotcha above).
- **GitHub creds via provider seam** (`core/github_credentials.py`) — App installation tokens
  (short-lived, repo-scoped) or `GITHUB_TOKEN` PAT fallback; install tokens cap at 1h.
- **Plan branch race** handled by fetch-fallback on push failure.
- **OpenCode and agy don't auto-commit** (entrypoint does); **OpenCode needs `limit.output`**;
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
- **`LeafTask` is the S2 decomposition contract** (`models/schemas.py`) with golden fixtures; a decomposed leaf must round-trip through it, so extend the fixtures when you add a field.
- **Status vocabulary is frozen in `core/status_vocab.py`** (S9) drawn from the `TaskStatus`/`PlanStatus` enums; add a value to the enum AND its exhaustive `test_schemas` assertion together, never one alone (a lone `SUPERSEDED` add broke that test at integration).
- **Capability decision records live in `core/capability_events.py`** (S1: versioned Pydantic events + `capability_events` table + bus wiring), but the emitter is STILL a stub; no production caller constructs `CapabilityEventEmitter(` yet (Plans 2/3/5 wire it).
- **F2 decomposition constraints are hard, not advisory** — the profile's numeric limits (`max_files_touched`, `max_loc_delta`, `max_dep_depth`, etc.) are injected as a `HARD CONSTRAINTS` block in the decompose prompt; leaves violating them are rejected by F3, not merely warned about.
- **F3 leaf validator is deterministic and fail-closed** — `core/leaf_validator.py` runs after `_normalize_slugs`; on hard rejection the brain is re-invoked with specific violations (≤2 informed rounds), then the plan is rejected entirely — no dispatch of an invalid graph.
- **F15 supply-chain gates block auto-merge** — `core/diff_guard.py` checks new deps in `pyproject.toml`/`package.json`/lockfiles and runs a secret regex over the diff; any hit forces the human gate regardless of review verdict. `added_dependencies` matches PEP621 array items too (`"pkg>=1.0"` under `[project] dependencies`), not just `requirements.txt`-style bare lines (Plan 2 Phase B fix; the array-item form was a false-negative — the shape THIS repo uses).
- **Persistent worker-endpoint block is capped, not respawned forever** — a provider/gateway error (Cloudflare/WAF 403, 429, 5xx via `ReconcileMixin.is_provider_error`) re-queues WITHOUT burning a retry, but `_provider_error_streak` counts trailing consecutive provider-error runs and after `Orchestrator.PROVIDER_ERROR_RESPAWN_CAP` (5) it stops, marks the task FAILED, and publishes `worker_endpoint_unreachable` (Plan 2 Phase B HIGH-1: was a silent infinite respawn loop with `status=in_progress`, `failed_count=0`). Bounded backoff (`_provider_error_backoff * streak`, capped 30s) throttles respawns before the cap.
- **Per-wave cross-leaf verify gate** — `DispatchMixin._wave_verify_gate` runs the project `verify_cmd` against the accumulated plan branch before dispatching each wave built on already-MERGED leaves (memoized per `merged_count` in `_wave_verify_state`); a fail publishes `plan_wave_verify_failed` and PARKS the wave. Catches cross-leaf contract breaks early (per-task tests are task-scoped and miss them, e.g. a leaf shipping `leaf.slug`); `on_plan_completed` whole-plan verify remains the final backstop. No `verify_cmd` = no-op (Plan 2 Phase B HIGH-2).
- **Plan-branch verify gates now FETCH the plan branch and FAIL CLOSED** — `_verify_plan_branch` clones then `git_ops.checkout_branch`, which fetches `origin <branch>` and checks out `FETCH_HEAD` via `checkout -B` (a bare `git fetch origin <branch>` only moves FETCH_HEAD, so a plain `git checkout <branch>` failed with exit 1 and the gate was SILENTLY SKIPPED on every plan). Both `_wave_verify_gate` and `on_plan_completed` now treat an `error` status the same as `failed` (park / publish `plan_verify_failed`), but an `error` is NOT memoized so the next tick retries (transient clone/network faults self-heal); only `skipped` (no `verify_cmd`/branch/credential) passes through.
- **`opus_bridge.py` + `users.token_hash` are legacy names on purpose** (renames deferred as churn).
- **Blocked workers ask, they don't guess** — `Status: BLOCKED`/`NEEDS_CONTEXT` → `NEEDS_CLARIFICATION`
  (no retry burned) → brain `answer_clarification` → re-dispatch or human gate. Both harnesses parse it.
- **Remote preflight is shared** (`core/preflight.py`) — every dispatch path runs
  cheap, read-only remote checks before spawning a container. Non-GitHub URL, auth
  failure, missing branch or file return 422. Unreachable remote returns 502. Base-SHA
  mismatch returns 409. Without a configured credential, checks are skipped with a
  warning so local-only experimentation still works.
- **`local_context` fills the worker's `repo_memory` Bible slot** with client-gathered
  non-committed context (gitignored config shapes, user-scope conventions); minimum-blocking,
  names/shapes over values, never a "read file X" pointer. Threaded like `context_text`.
- **Outcome recording is fire-and-forget** — `core/outcome_recorder.record_outcome` writes one
  `task_outcomes` row per terminal `review_task` verdict, swallows its own DB/emit errors;
  attribution decided ONLY by `core/failure_taxonomy.counts_against_worker` (S6);
  `provider_error` and human merge-gate rejections never count against the worker.
- **Decomposition history is real now** — `decompose_plan(db=...)` feeds
  `fetch_recent_outcomes` (scoped `(model, project)` → `(model, *)`, worker-attributable rows
  only) into the prompt history slot; Wilson-bound learned limits and `GET /api/capability`
  remain Plan 6.
- **Role fallback chains resolve before per-call-site overrides** — `EffectiveSettings.call_site_chain` maps a call-site to a role (`core/roles.ROLE_OF_CALL_SITE`, frozen + golden-tested) then to an ordered registry chain; an EMPTY chain falls through to the `models.<call_site>` override, then `CALL_SITE_DEFAULTS`. The router (`LLMRouter.run`) tries each entry and falls back ONLY on unavailability (`core/provider_errors.is_unavailability`: auth/rate-limit/gateway) — a bad-output error never falls back. `implement` role is NOT router-driven in v1 (worker model is spawn-baked).
- **Auto-delegate mode is a global toggle, not per-project** — `auto_delegate.enabled` persists in `settings_overrides` (source of truth), read via `EffectiveSettings.auto_delegate_enabled()`; toggle with `praxis mode on|off` or `PUT /api/settings/auto-delegate`, mirrored by MCP `get_mode`. The delegated/global-default worker (`default_worker_harness` / `default_worker_model`) lives in `config/praxis.yaml` (reference: `agy` / `Gemini 3.6 Flash (High)`); the product default outside the mode stays `opencode`. A project with no `model_name` falls back to the global default worker.
- **Single-branch discipline is entrypoint-driven** — in auto-delegate mode `dispatch_pending_tasks` reuses one caller-named work branch and threads `single_branch=True` → `SINGLE_BRANCH=1` into the container; both harness entrypoints then REUSE the existing remote `BRANCH` with a non-force push instead of cutting a fresh `agent/{slug}`. Changing this behavior needs an agent IMAGE REBUILD (entrypoint change), not just a src edit.
- **Stale-branch sweeper is fail-safe and reconcile-driven** — `core/branch_sweeper.dead_branches` picks reclaimable branches (no open PR, no live run, never protected); `ReconcileMixin.sweep_dead_branches` deletes them each reconcile pass and swallows its own errors (never wedges the loop).
- **`config/praxis.yaml` is MOUNTED, not baked**: both compose files bind-mount `./config` read-only at `/app/config` and the BASE file sets `PRAXIS_CONFIG_PATH` (the dev overlay inherits it through the compose environment merge), so a YAML edit (e.g. `default_worker_*`) takes effect on `docker compose restart orchestrator`, never an image rebuild. This reverses the behavior that bit us live 2026-07-27. `core/settings_file.config_file_path()` is the ONLY place the `praxis.yaml` path is decided; a hardcoded `"config/praxis.yaml"` literal anywhere else reintroduces the bug and `tests/test_config_path.py` greps for exactly that. Agent-image entrypoint changes still need a rebuild.
- **Session resume is gated to answered clarifications**: `core/session_resume.resolve_resume_session`
  returns an id only when a stored `worker_session_id`, a matching `worker_session_harness`, and a
  `clarification_state` of `answered_by_brain`/`resolved` all line up; a plain failure retry never
  satisfies that last condition, and its branch is rebuilt from base anyway, so restored memory can
  never describe a tree that no longer exists. `WORKER_SESSION_ID` means BOTH "resume the
  conversation" and "reuse the remote branch": they move together, or restored memory contradicts
  the tree. A worker reports its session id ONLY after its BLOCKED checkpoint push succeeds, so a
  failed push silently forces the next turn to start cold. Entrypoint change: needs an agent IMAGE
  REBUILD. **The agy JSON envelope shape is UNVERIFIED** (no real agy build was available while this
  was built); the happy path needs a live dogfood run before anyone relies on it.
- **Leaf templates are enforced**: `core/leaf_templates.py` is the single source of
  per-`LeafType` `plan_text` section requirements; the F3 validator enforces
  `REQUIRED_SECTIONS` and missing sections raise `KeyError` at test time.
- **Context pack fits greedily by priority**: floor sections (plan, edits,
  acceptance, feedback, handover) never drop; remaining sections fit in priority
  order so a section that doesn't fit is skipped but lower-priority ones may survive.
- **`LEAF_SCHEMA_VERSION` is 2**: a new `LeafTask` field, even with a default,
  changes `model_dump()` output and breaks `tests/fixtures/decompose/expected_leaf_graph.json`;
  regenerate the fixture in the same commit.
- **`_normalize_edit_locations` must never raise**: it normalizes raw brain JSON
  `files` into the edit locations floor section; a `TypeError` aborts the loop, so
  it returns None on garbage input rather than raising.
- **MCP payloads lead with a `summary` key, `get_task_logs` tails at 40 KB**: dict
  insertion order is what the client renders, so every state-returning tool puts
  a one-line summary first; a clipped log always says so, tailing the last
  `LOG_TAIL_CHARS`, never the head.
- **`praxis doctor` is the front door to every problem**: eleven read-only checks
  in `core/doctor.py`, pure decision logic in `core/doctor_probes.py`, live fact
  gathering in `api/doctor.py`. Probes are pre-bound ZERO-ARGUMENT callables; a
  hintless RED resolves its specific hint from the registry; gathering is guarded
  per unit so the endpoint always answers 200; `agent_image_freshness` is AMBER,
  never GREEN, when it had nothing to compare.
- **`praxis init` is re-runnable and never eats your `.env`**: it merges only
  `MANAGED_KEYS`, preserving every other key, position, and comment, and its
  `.env` parser is graded against real `python-dotenv` by a differential test.
  Empty means clear, `None` means no opinion.
- **The approvals digest is rate-limited but the surfaces are not**: only the
  `approvals_digest` SSE event is throttled (`approvals_digest_interval_h`,
  default 6h); `pending_approvals`, the `poll_task`/`poll_plan` digest line,
  `praxis pending`, and the dashboard badge all read live. Nothing parked means
  no event at all.

## Documentation

- **Architecture & components:** `docs/architecture.md`
- **Workflow & orchestration cycle:** `docs/workflow.md`
- **Deployment, Docker & API reference:** `docs/deployment.md`
- **Decomposition standard (cited contract):** `docs/decomposition-standard.md`
- **Gotchas (full narrative):** `docs/gotchas.md`
- **Design spec:** `docs/superpowers/specs/2026-06-01-ai-agent-orchestrator-design.md`
- **Capability-engine roadmap (canonical, 2026-07-11):** `docs/superpowers/specs/2026-07-11-capability-engine-roadmap.md` — features F1-F15, standardization contracts S1-S11, 10-plan breakdown; next up = Plan 3 `outcome-recording`
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
