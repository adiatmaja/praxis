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
| Web UI | No-build HTML/CSS/JS (`web/index.html` + `styles.css` + `app.js`) |
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
├── tests/                           # 517 tests, 89% coverage
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

- **Merge is gated by default**: review PASS parks a task at `PASSED` (PR left
  open, `task_awaiting_merge` event emitted), it does NOT auto-merge. Merge
  happens only via `POST /api/tasks/{id}/approve-merge` (or the dashboard /
  plan-level `approve-merges`), or when a project sets `auto_merge=True`. Even
  with `auto_merge=True`, Praxis never auto-merges into a protected branch
  (project default / `main` / `master` / `release*`): `core/merge_policy.py`.
  MCP `poll_task` reports this as `status: awaiting_merge` so a main brain can
  relay the PR for approval.
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
- **Orchestrator is split across mixins** — `core/orchestrator.py` holds only the
  loop core (`__init__`, `plan_and_activate`, `process_plan_once`, `run_once`,
  `run_loop`, `shutdown`). Dispatch, review/merge, reconcile, and improvement live
  in `core/orchestrator_{dispatch,review,reconcile,improve}.py` as mixins on the
  single `Orchestrator` class. Tests patch module-level helpers (e.g. `run_verify`,
  `clone_with_token`) on the MIXIN module that calls them, not on
  `core.orchestrator`.
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
  no other producer of `agent_log`. **`dispatch_pending_tasks` attaches the monitor at
  spawn** (`_start_monitor`), not just `reconcile_runs` — otherwise a container that
  finished within one loop interval streamed zero `agent_log` events and the dashboard
  Live Log stayed empty.
- **Agent container names are reused, so spawn force-removes stale ones** —
  `spawn_agent` names containers `praxis-agent-{task_id[:8]}` (harness-neutral prefix)
  with `auto_remove=False`. Retrying/re-dispatching a task collides with the exited
  container from its prior run (Docker 409 Conflict → agent never starts → empty Live
  Log). `_remove_existing_container` deletes any same-named container first.
  `list_agent_containers` queries both `praxis-agent-` and the legacy `aider-agent-`
  prefix so reconcile can still clean up containers spawned before the 2026-07 rename.
- **Agent callback URL is port-derived, not hardcoded** — `Settings.callback_url()`
  builds `http://host.docker.internal:{PORT}/api/internal/agent-done` (override with
  `AGENT_CALLBACK_URL`) and is passed to `Orchestrator(callback_url=...)`. Running the
  orchestrator on a non-8080 port without this makes every agent callback 404, so tasks
  only ever finish via reconcile (and get marked failed even on success).
- **Brain prompts go to the CLI via stdin, never argv** — `OpusBridge._run_claude_raw`
  and `LLMRouter.run` pipe the prompt through stdin (`communicate(input=...)`); a full
  PR diff as an argv element overflows the OS command-line limit (Windows `WinError 206`
  above ~32K chars). `build_argv` deliberately omits the prompt.
- **The claude CLI effort flag is `--effort`, not `--reasoning-effort`** — the installed
  CLI (v2.1.170) renamed it; passing the old flag fails every effort-tiered brain call
  with `unknown option '--reasoning-effort'` (this 500'd `/api/execute-plan`, whose
  `plan_review` call-site is opus/high). Both `core/llm_router.build_argv` and
  `OpusBridge` use `--effort`. `review_task` runs `gh pr diff/merge/comment`
  with `--repo <owner/name>` (from `GitOps.repo_slug(pr_url)`); without it `gh` resolves
  the PR against the orchestrator's own cwd (the praxis repo) and reviews the wrong diff.
- **Agent callbacks retry with backoff** — each `docker/<harness>-agent/entrypoint.sh`
  `send_callback` retries the POST to `/api/internal/agent-done` up to
  `CALLBACK_MAX_ATTEMPTS` (default 5) until HTTP 200. The orchestrator's reconciliation
  is the backstop if all attempts fail. The `/api/internal/agent-done` endpoint now
  **fails closed (503)** when `internal_callback_secret` is unconfigured; in practice this
  never triggers because the secret is derived deterministically from the required
  `AUTH_TOKEN`, so any running orchestrator always has it set.
- **Agent containers run on Docker's default bridge network** — `spawn_agent` sets
  `extra_hosts={"host.docker.internal": "host-gateway"}` instead of
  `network_mode="host"`. The LM Studio URL is rewritten via `_container_host_url` so a
  `localhost` orchestrator setting stays reachable from inside the container. The worker
  can still reach `host.docker.internal` (needed for LM Studio and the callback
  endpoint), so this reduces but does not eliminate host network exposure.
- **Aider agent image is standalone — rebuild it after ANY `entrypoint.sh` change** —
  `aider-agent:latest` is not in docker-compose, so a stale image silently runs old
  entrypoint logic while the source looks current. This bit us live: a pre-callback-token
  image sent an **empty** `X-Praxis-Callback-Token`, so every callback 401'd and tasks
  never advanced past implement (only reconcile → failed). Rebuild fixes it:
  `docker build -t aider-agent:latest -f docker/aider-agent/Dockerfile docker/aider-agent/`.
  To read an image's baked-in files reliably use `docker cp <container>:/path` — NOT
  `docker run --entrypoint cat <img> /path`, which on buildkit multi-manifest images
  resolves the attestation manifest (no rootfs) and returns nothing (false negative).
- **Agent container runs as non-root** — The `agent` user cannot write to `/`.
  Workspace is at `/home/agent/workspace`. Do not change WORKDIR to a root-owned path
- **Agent git auth uses `GH_TOKEN`** — configured via credential helper in entrypoint.
  Without it, HTTPS git operations (clone private repos, push branches) fail with
  "could not read Username"
- **GitHub credentials go through a provider seam:** `core/github_credentials.py`
  resolves a token per repo. With `GITHUB_APP_ID` + `GITHUB_APP_PRIVATE_KEY` set,
  Praxis mints short-lived, repo-scoped installation tokens (App private key never
  enters a container); otherwise it falls back to the static `GITHUB_TOKEN` PAT.
  `GitOps`, `AgentManager`, `BrainstormManager`, and `ContextSync` all take a
  `credentials` provider (a bare token string is still accepted and wrapped in a
  `PatCredentialProvider`). Installation tokens cap at 1h, so a >1h agent run can
  fail its final push (refresh endpoint is a planned follow-up).
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
- **OpenCode config needs `limit.output`, not just `limit.context`** — when the
  orchestrator detects a model's context window (`MODEL_CONTEXT_LIMIT`), the OpenCode
  entrypoint writes a `limit` block; OpenCode's schema requires BOTH `context` and
  `output`, so omitting `output` fails validation ("Missing key ...limit.output") and
  the container exits before implementing. The detect-context-limit feature exposed this.
- **The Static Bible must NOT land in the PR** — OpenCode injects the Bible into
  `AGENTS.md` (compaction-proof slot), so the entrypoint strips the
  `<!-- praxis:bible:start -->...:end -->` block before `git add -A` (and deletes
  `AGENTS.md` if it was untracked and now empty). Without this the Bible leaks into every
  OpenCode PR. Aider's Bible is a read-only `--read .praxis-bible.md` (never committed);
  OpenHands does not inject a Bible.
- **PR body uses `TASK_SUMMARY`, not a slice of `TASK_PROMPT`** — `TASK_PROMPT` is the
  fully-wrapped prompt that starts with a generic autonomous-loop preamble, so
  `${TASK_PROMPT:0:500}` showed only boilerplate. All three entrypoints now render the
  body from `TASK_SUMMARY` (task title + description), set by `spawn_agent(task_summary=)`.
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
  It also requires a **4-backtick outer fence** around any embedded file content that
  itself contains a ` ```lang ` block — a 3-backtick outer fence renders malformed (even
  on GitHub). The renderer only repairs single fences; this is the source-side fix.
- **`/api/status` Planner availability is CLI-probed, not DB-only** — `api/system.py`
  `_probe_claude_cli` runs `claude --version` (cached 60s) and gates `agent_model.connected`
  on it, so a missing `claude` binary no longer reports "available" off the `opus_state` row
  alone. The response also carries `agent_model.cli_available` and the effective `lm_studio_url`.
- **LM Studio URL shown in the dashboard is the effective (global) one** — the agent uses
  `EffectiveSettings.lm_studio_url()` (global override → env default), NOT the per-project
  `projects.lm_studio_url` column. The project config panel reads `lm_studio_url` from
  `/api/status` so it doesn't mislead. The New-Project model field is a dropdown fed by
  `GET /api/lm-models` (proxies LM Studio `/v1/models`), falling back to free-text when
  LM Studio is unreachable — avoids the old hardcoded, unreachable `deepseek-coder-v2` default.
- **Create-Spec chat needs the SSE stream open** — brainstorm replies/errors arrive over
  `/api/events`; `switchView` closes that stream on non-dashboard views, so `openSpecChat`
  re-opens it (`connectDashboardSse`) when `!eventSource`, else the chat hangs silently.
  Failed turns surface via the `brainstorm_error` event (published by `api/specs.py`
  `_run_turn_safely`); the frontend renders them as red error bubbles.
- **Provider auth is detected, never automated** — login (`codex login`, etc.) is an
  interactive flow the orchestrator can't run; it only detects and surfaces. `llm_router`
  raises `ProviderAuthError` when a CLI is installed but its session is dead. Key traps it
  closes: (1) **codex exits 0 while printing a 401** to stderr on a revoked-but-cached
  token — `LLMRouter.run` scans stderr for auth signatures BEFORE the returncode check, so
  a failed-auth call never passes through as success; (2) **Windows shims** — `codex`/`agy`
  are `.CMD`/`.EXE` npm/installer shims that bare `create_subprocess_exec` can't launch
  (`WinError 2`), so both `llm_router.run` and `api/system._probe_provider` resolve argv[0]
  via `shutil.which`; (3) **agy `--print` needs the prompt as a positional argv value**, not
  stdin, and yields zero capturable stdout when not on a TTY → `ProviderOutputError` (agy is
  effectively unusable as a brain). `run_loop` catches `ProviderAuthError` and publishes a
  `provider_auth_required` SSE event instead of retrying-to-fail (the plan stays put, not
  marked failed).
- **MCP server is a separate package** — `src/mcp_server/` (stdio adapter, `praxis-mcp`
  entry point) forwards to the REST API via `PraxisClient`; it owns no engine logic. The
  only engine-side addition is `POST /api/dispatch` (`api/dispatch.py`), which injects a
  single-task plan because Praxis has no direct single-task creation route — tasks are
  created only via `TaskQueue.activate_plan`. MCP dispatch sets `approval_gate=False` on the
  auto-created project so the loop dispatches without a gate. v1 always runs review (no
  `review=false` opt-out yet).
- **`dispatch` `branch` is always a base, never a target** - Praxis cuts a new
  `agent/<slug>` branch off it and opens a new PR. There is no amend-existing-PR
  mode yet; re-dispatch = new PR. Follow-up: add `target_branch`/`pr_number`.
- **`execute_plan` must bridge brain ids → slugs** — `plan_review` emits tasks keyed by
  `id` (`t1`) with `depends_on` referencing ids, but `TaskQueue.activate_plan` /
  `get_dispatchable_tasks` key on `slug` and expect `depends_on` to hold slugs. Without the
  bridge the dispatch loop raises `KeyError: 'slug'`. `api/execute_plan._normalize_slugs`
  adds a unique `slug` per task and remaps `depends_on` id→slug before activation. (The
  existing execute-plan API tests mock `activate_plan`, so they did NOT catch this — a live
  run did; there's now a dedicated `_normalize_slugs` unit test.)
- **Dashboard login banner is SSE-driven, not just poll-driven** — `/api/status` adds a
  `providers` block (`_probe_provider`: cli_available + best-effort authenticated +
  login_hint), but **`codex login status` lies** ("Logged in" even on a revoked token), so
  the poll alone can't catch a dead codex session. The banner in `web/index.html` therefore
  ALSO reacts to the `provider_auth_required` SSE event; a runtime 401 outranks the optimistic
  poll and stays flagged until the user dismisses it (the poll must NOT clear a runtime
  failure, or the banner flickers). Net: the codex banner only appears once a real
  codex-routed brain call hits the 401 — not on page load.
- **Mechanical verify gate runs before the brain**: if a project sets
  `verify_cmd`, `review_task` runs it against the cloned PR head first
  (`core/verify_gate.py`); a non-zero exit fails the task with the command
  output as feedback and never spends brain tokens. Trusted operator config,
  not taken from the PR. Harness-agnostic (runs orchestrator-side, not in the
  agent container).
- **Build stamp on /health + /api/status**: `core/build_info.py` exposes the
  running commit + start time so a stale server (started before a feature merged)
  is visible. Restart after deploy; `PRAXIS_BUILD_SHA` overrides the git-derived sha.
- **Decomposition emits per-leaf `plan_text`**: the capability-review brain now
  copies the verbatim contract (signatures/API) into each leaf's `plan_text`, which
  `review_task` feeds to `review_diff`; without it the reviewer checked diffs against
  the task blurb and missed spec drift (e.g. a dropped `AbortSignal` param).
- **Two names are legacy on purpose** — `core/opus_bridge.py` is the
  provider-agnostic brain bridge (see its docstring), and `users.token_hash`
  stores the RAW v1 auth token, not a hash (see `api/auth.py`). Renames were
  evaluated (2026-07-02 refactor) and deliberately skipped as churn; a future
  `token_hash` rename should ride the migration framework in `database.py`.

## Documentation

- **Architecture & components:** `docs/architecture.md`
- **Workflow & orchestration cycle:** `docs/workflow.md`
- **Deployment, Docker & API reference:** `docs/deployment.md`
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
