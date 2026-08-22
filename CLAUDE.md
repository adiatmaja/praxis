# CLAUDE.md — Praxis

## What Is This

Praxis is a provider-agnostic, Docker-based tool set up inside the coding harness you
already use, letting it drive other harnesses with every change governed by a gated
loop. It splits the software engineering loop into four roles — **plan**, **implement**,
**review**, and **verify** — each independently configurable to any provider, model, or harness,
and decomposes every plan to fit the capability of the model that implements it. Nothing is
hard-wired to a single vendor.

**Framing (canonical, 2026-08-22, supersedes 2026-08-21):** the identity is *a tool for
agentic AI to govern other coding harnesses*: set up inside the harness you already use, it
lets that harness manage, control, and steer the others from a single session, with every
change governed by the loop instead of one-shotted. Praxis is NOT itself a harness. Public
wording is "govern/governed", never "predictable" (a measurable claim with no published
benchmark yet). **Implement-a-plan is the flagship FEATURE** (a single dispatched task is
its smallest case, not a second feature); auto-delegate mode is the companion feature
(beta until the review-scope fix), the continuous shape of the same connection, presented
after the flagship, never beside it. Capability-aware task decomposition is the flagship
MECHANISM behind the promise (still unique in the landscape), not the headline; role
separation is the supporting architecture; cost is a consequence.
Reference: `docs/positioning.md` ("The framing"), which also lists the wording to avoid
("meta-harness" and "control plane" are claimed by larger projects).
Flagship mechanism under development: the **Capability Calibration Loop**. Roadmap + feature/contract
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
│   │   ├── config.py         # pydantic-settings; env > .env > config/praxis.yaml > default
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
`doctor`, `approvals`, `events` (SSE), `internal` (agent callback). `api/auth.py` is in the
same package but is NOT a router: it holds the auth dependency, declares no `APIRouter`, and
`main.py` includes nothing from it.

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
  `markdown_utils`, `backfill`, `spec_docs`
- **Operability:** `doctor`, `doctor_probes`, `verify_gate`, `build_info`, `entrypoint_hash`,
  `approvals`, `event_bus`, `log_context`

## Commands

```bash
# Setup (one command, idempotent, ends by verifying)
uv run praxis init
uv run praxis init --non-interactive --preset <name>   # scriptable, never prompts
uv run praxis presets           # the names --preset accepts; works with the server down

# Submit a spec (use --file: Git Bash truncates a bash -c argument at 8 KB)
uv run praxis submit <project-id> --file spec.md
cat spec.md | uv run praxis submit <project-id> --file -

# Diagnose (read-only against repo and DB, but spends one planner call per
# run, cached 60s; exits non-zero on any red, and a rate limit is amber)
uv run praxis doctor
uv run praxis env               # which URL and token the CLI resolved, and from where
uv run praxis logs <task-id>    # the agent container log, captured on the run row

# See what is parked at the merge gate, and open it
uv run praxis pending                # parked tasks AND plans awaiting integration
uv run praxis merge <task-id>        # one task
uv run praxis reject-merge <task-id> [--feedback "..."]   # the other half of the gate
uv run praxis merge-plan <plan-id>   # parked tasks, then the integration PR

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
| `docker.yml` | `docker/**` changes | shell-checks entrypoints, validates `--profile agents config`, and builds every image the repo actually builds: `opencode-agent` and `agy-agent` in a matrix from their own dir, plus a separate orchestrator job. **Orchestrator builds with repo-root context** (its Dockerfile copies `src/`, `web/`, `pyproject.toml` from root). `docker/caddy/` is a Caddyfile only, mounted into the stock `caddy:2-alpine` image, so there is nothing there to build |
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
         -> NO_CHANGES        (work already present; verified on the base branch)
         -> NEEDS_CLARIFICATION (worker asked; parks for `praxis clarify` and
                                 waits indefinitely for a person)
         -> SUPERSEDED        (split into children, which carry the work)

Then, once every task is done:
plan COMPLETED -> integration PR -> (human approve) -> integration_merged_at
```

All eight `TaskStatus` values appear above; a surface that lists fewer teaches
a caller to poll for a state that will never arrive. `NO_CHANGES` is terminal
and is neither a success nor a failure, like `SUPERSEDED`. Both are in
`SATISFIED_STATUSES` (`core/status_vocab.py`), which is what unblocks
dependents and lets the plan complete. `NEEDS_CLARIFICATION` is the third gate:
nothing advances it but a human answering.

## Data Model (SQLite)

Tables: `users`, `projects`, `plans`, `tasks`, `agent_runs`, `opus_state`,
`doc_index`, `settings_overrides`, `capability_events`, `task_outcomes`

- A default `admin` user is auto-seeded on first startup (token_hash = AUTH_TOKEN)
- `capability_events` and `task_outcomes` come from MIGRATIONS, not the baseline
  `CREATE_TABLE_STATEMENTS` block, which is why they are easy to miss when reading
  `database.py` top-down. They carry the capability engine's decision trail (S1 events)
  and the per-task outcome record `record_outcome` writes at a terminal verdict
- `opus_state` is a singleton row tracking `available` / `rate_limited` / `resuming`
- `plans.integration_pr_url` / `integration_merged_at` (migration 9) are where a
  completed plan's last step lives; without them the integration PR is invisible to
  every read-only surface
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
  beneath env vars, so precedence is **environment variable > `.env` file > settings file >
  field default**. The `.env` layer is explicit and load-bearing: the YAML is overlaid as init
  KWARGS, which outrank every pydantic-settings source, so a key in BOTH files used to take
  the YAML's value silently (`LOOP_INTERVAL=0` in `.env` ran at the YAML's 5). `_dotenv_keys`
  now counts the dotenv file as environment; a `PRAXIS_`-prefixed var is exempt because it IS
  environment. A key present in both logs ONE `INFO` line naming the winner, so the reverse
  silence (editing the settings file and getting no effect) is covered too. Keys map by
  uppercase field name (e.g. `loop_interval` ← `PRAXIS_LOOP_INTERVAL` in the YAML loader,
  `LOOP_INTERVAL` for the pydantic env layer). Project config stays DB-backed.
- **Provider-agnostic LLM router (Spec 3)** — `core/llm_router.py` resolves each brain
  call-site to `{provider, model, effort}` and executes it. `CALL_SITE_DEFAULTS` is the
  model-tiering policy (e.g. `plan_spec`→sonnet, `review_diff_rereview`→haiku,
  `derive_tasks`→local), but it is SHADOWED wherever the call-site has a role: see the
  role-chain gotcha below. CLI providers: `claude`, `agy` (Gemini), `codex` (GPT) via
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

**`docs/gotchas.md` is the full list, with the narrative for each. Read the relevant entry
there before touching a subsystem.** Below are only the ones that bite during ordinary edits,
because each fails SILENTLY. When you learn a new one, write it there and add a line here only
if it belongs in this shortlist. (No count is quoted on purpose: a number in prose goes stale
in both directions and then gets cited as authority.)

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
- **`pathlib.write_text` converts a file to CRLF on Windows, and `read_text` HIDES it**
  (universal newlines), so a `\n` anchor in a patch or mutation script silently misses
  and reports a working fix as an inert guard. Detect with `read_bytes()`, normalize
  before matching, write back in the original style.
- **Assert on CLI output through `tests/cli_text.py`**, never on `result.stdout`
  directly: rich colorizes on the Linux runner and not on the Windows one, so an escape
  lands inside the matched phrase. Run the suite once as
  `FORCE_COLOR=1 TERM=xterm-256color pytest` before believing a help-text guard.

**Config and deployment**

- **`config/praxis.yaml` is MOUNTED, not baked**: a YAML edit needs `docker compose restart`,
  never a rebuild. `core/settings_file.config_file_path()` is the ONLY place that path is
  decided; a hardcoded `"config/praxis.yaml"` literal in `src/` reintroduces a fixed bug and
  `tests/test_config_path.py` greps for exactly that.
- **Which command applies a `.env` edit depends on whether compose FORWARDS the key**,
  and the two answers are opposites. A key compose substitutes or passes through
  (`${AUTH_TOKEN}`, bare `- DEFAULT_WORKER_HARNESS`) is baked into the container's
  environment when the container is CREATED: `restart` reuses the old value and only
  `up -d` recreates it. A key compose does NOT forward (`LOOP_INTERVAL`,
  `CALLBACK_GRACE`) never enters the container environment at all; the process reads it
  from the MOUNTED `/app/.env` at startup, so `restart` applies it and `up -d` is a
  NO-OP when nothing in the compose config changed. Measured 2026-08-21 on a live
  install: `up -d` after `LOOP_INTERVAL=11` printed "Container orchestrator Running"
  and changed nothing; `restart` picked it up and logged the override line. `up -d`
  then `restart` is correct for both and is what to tell an operator. The `env_drift`
  doctor check covers the forwarded half only, which is all it can see.
- **A host `~/.claude` hook blocks every brain call inside the container**, tunnel up or
  down, because that directory is mounted. Set the hook's opt-out (e.g.
  `CLAUDE_VPN_KILLSWITCH_OFF=1`) in `.env.container`, then `up -d`. That file is a
  gitignored optional compose `env_file`, so its keys become real container environment
  variables the `claude` subprocess inherits. It replaced "add a literal to
  `docker-compose.yml`", which still works but dirties a TRACKED file. NEVER `.env`:
  compose reads that on the host for substitution and passes nothing from it into the
  container, so the fix appears not to work.
- **Agent images are compose services behind the `agents` profile** (build-only,
  `command: ["true"]`, so `up` never starts them): rebuild `opencode-agent:latest` /
  `agy-agent:latest` after ANY `entrypoint.sh` change or a stale image runs silently.
  Staleness is judged by CONTENT (the `org.praxis.entrypoint-sha256` label), never mtime.
  Rebuild via `praxis init` then `docker compose --profile agents build`: a bare `docker build`
  leaves that label EMPTY (it is a build ARG), which reads as "cannot judge", not as fresh.
- **Worker preset env vars reach the container as BARE compose pass-throughs**
  (`- DEFAULT_WORKER_HARNESS`), never `${VAR:-default}`: any expansion form sets the variable
  even when unset, which silently suppresses the mounted YAML.
- **`praxis doctor` is the front door to every problem**: twelve checks, read-only against
  your repo and database but spending one planner call per run; pure decision logic in
  `core/doctor_probes.py`, live fact gathering in `api/doctor.py`.
- **The planner check probes the CONFIGURED planner, not the CLI default**: the model
  `plan_spec` resolves to through `EffectiveSettings.call_site_chain` (the same bound
  method `main.py` hands the router, so the two cannot drift), executed through
  `llm_router.build_argv` so the probe runs the flags the loop runs. The row NAMES the
  provider and model it probed. A `local` planner is AMBER (no binary to probe), never
  a red; `codex` and `agy` stay "not probed" for the reasons in `probe_provider_roundtrip`.
  `GET /api/status` and `praxis status` resolve it the SAME way now: they used to report
  the legacy `agent_model` setting (`claude-opus-4-8`) and probe LM Studio regardless of
  harness, so the status surface and the doctor disagreed about one install.
- **A fix applied to the doctor is NOT applied to the product.** The doctor is where
  diagnosis lives, so corrections about what an install actually runs land there and feel
  complete, but every row describes a fact a status endpoint, a CLI verb or the dashboard
  also reports. When a doctor row is corrected, grep for what else answers the same
  question and fix it in the same commit; a subject sweep derived from the session's own
  diff cannot catch this, because the doctor-only fix changed nothing in that diff.
- **A YAML role chain SHADOWS `CALL_SITE_DEFAULTS` entirely**: once a call-site has a
  role (`core/roles.ROLE_OF_CALL_SITE`) and `models.roles` declares a chain for it,
  `EffectiveSettings.call_site_chain` returns that chain and never consults the defaults
  map or a per-call-site override. The shipped chains are `plan: [sonnet, opus]` and
  `review: [sonnet, haiku]`, so on a stock install EVERY routed call-site runs sonnet and
  the second entry is only the unavailability fallback. Reading `CALL_SITE_DEFAULTS` to
  answer "what does this call-site run" gives the wrong answer, and a **Settings, Models**
  override for such a call-site is stored and ignored: clear the role chain first.
  `docs/configurations.md` has the full account.
- **A key in `.env` that the settings file also names now WINS, and says so**: the dotenv
  file counts as environment (`env > .env > settings file > default`). It did not before,
  so `LOOP_INTERVAL=0` in `.env` ran at the YAML's 5 in silence. A key in both files logs
  one `INFO` line naming the winner, once per key per process.
- **Container-only variables go in `.env.container`**, a gitignored optional compose
  `env_file` on the orchestrator. That is where a Claude Code hook opt-out belongs now;
  the old remedy edited `docker-compose.yml`, which is TRACKED and leaves a permanent diff
  in a fresh clone. Not `.env`: compose reads that on the host for substitution and passes
  nothing from it into the container on its own.
- **An unrecognised key in `.env` is IGNORED, not rejected**: `./.env` is mounted
  into the container and parsed whole, so `extra="forbid"` used to abort startup.
  The cost is that a typo in a real key is now silent and NOTHING catches it;
  `env_drift` only compares keys the container actually received, so a key living
  only in `.env` is invisible to it. `AUTH_TOKEN` is the exception, being required.
- **`praxis doctor` spends one planner call per run** (cached 60s). It is
  read-only against your repo and DB, not free. A rate limit is AMBER, not RED.

**The loop**

- **Merge is gated by default**: a review PASS parks at `PASSED` and never auto-merges
  (`core/merge_policy.py`); protected branches never auto-merge at all.
- **A blank `verify_cmd` is "not configured", never a pass**: `"   "` is TRUTHY, so it
  slipped every falsy guard, reached the shell, and a blank shell command exits 0, so the
  gate reported `passed` having run nothing. `core/verify_gate.normalize_verify_cmd` is the
  SSoT, applied at the per-task gate, the wave gate, and inside `_verify_plan_branch` (the
  funnel, because `on_plan_completed` is a FOURTH raw read that normalizing the named three
  would have left lying). The API rejects it 422; `run_verify` raises rather than shell it.
- **`loop_interval` reaches `run_loop` now, and the shipped default is 5**: `main.py` used
  to call `run_loop(stop_event)` with no interval, so the key was inert and every install
  ran at a hardcoded 5s. The settings layer, the YAML and `run_loop` were reconciled ON 5
  rather than on the 30 the settings layer claimed, because 5 is the only value any install
  has ever run; adopting 30 would have been a silent sixfold latency increase. A
  non-positive value is floored, not honoured, so it cannot busy-spin the loop.
- **Brain prompts go via stdin, never argv** (argv overflows the OS limit; Windows `WinError 206`).
- **`gh pr` calls need `--repo <owner/name>`** or they target the orchestrator's own cwd.
- **A PR may be reused only on a POSITIVE open-state hit**: `gh pr view <branch>` resolves a
  branch to a PR regardless of state, and identical specs reproduce identical branch names,
  so it attached new work to an already-merged PR while every layer reported success. Both
  entrypoints use `gh pr list --head --base --state open`, as `_existing_integration_pr`
  does. All three filters are load-bearing and each has its own test in
  `tests/test_entrypoint_pr_reuse.py`; the emptiness of the OUTPUT is the signal, never
  the exit status.
- **Verify gates FAIL CLOSED and fetch the branch first**: treat `error` like `failed`, and
  only `skipped` passes through. An `error` is never memoized, so the next tick retries.
- **The merge gate has TWO CLI verbs and they are not the obvious pair**:
  `praxis merge <task-id>` and `praxis reject-merge <task-id> [--feedback ...]` are the
  two halves; `praxis merge-plan <plan-id>` does the plan's parked tasks and then its
  integration PR, exiting 1 if any task failed. `praxis approve` / `praxis reject` are for
  improvement PLANS and 404 on a task id, which reads as a broken command rather than the
  wrong one. A rejected task is re-dispatched if attempts remain; the printed line says
  which happened rather than assuming.
- **The improvement loop must be given the REPOSITORY, and fails closed without it**:
  `check_improvements` once built its whole prompt from three strings and cloned nothing,
  so it proposed Praxis-shaped work for an unrelated repo. `core/repo_survey.py` +
  `BrainstormManager.survey_repo` supply it; no readable repo means NO proposal, because
  falling back to the name-only summary reproduces the defect whenever a clone fails.
- **A plan with no commits has nothing to integrate**: all-no-op plans leave the branch
  identical to base, so `gh pr create` refuses and that is a FACT, not a failed PR.
  `_plan_branch_has_nothing_to_integrate` decides before attempting. Positive check only:
  two known, equal `str` SHAs. Anything else falls through to the attempt.
- **A plan reaches the gate TWICE**: each task onto the plan branch, then the plan's
  own integration PR onto the base branch. The PR url lives on
  `plans.integration_pr_url` and `integration_merged_at` is what takes it back out of
  `pending`. Reporting only the first stage is how "completed" came to mean "not on
  main, and nothing says where it went".
- **An empty worker diff is a FACT, not a verdict**: the entrypoints report
  `no_changes`, the orchestrator decides by verifying the branch the leaf was cut
  from. Empty transcript, untrustworthy `rev-list`, and a failing verify all stay
  `failed`. A test asserting "empty diff -> failed" passes before AND after a bad
  fix; assert on the status carried to the callback.
- **The CLI falls back to the nearest `./.env`** for `AUTH_TOKEN` and `PORT`, walking
  up from cwd; explicit env vars still win. `praxis env` says which source won.
- **`praxis init` logic is `run_init(Answers(...))`, not `init()`**: typer turns the
  command's defaults into `OptionInfo`, so only the plain function is callable.
- **GitHub's PR state outranks `gh`'s exit code**: `gh pr merge` can 504 AFTER the
  merge succeeded, so `merge_pr` re-reads `gh pr view --json state` before failing.
- **An id belongs on its own line, not in a table column**: rich's `max_width` is a
  MAXIMUM, so a 36-wide uuid column still shrank to 16 and folded across THREE rows
  once five columns competed for 80 chars; `min_width` only pushes the last columns
  off the right edge. `pending`, `plans`, and `tasks` all print a plain copyable
  `praxis <verb> <id>` line below the table instead. An id truncated to 8 chars
  404s, because every consumer looks it up by exact match. Assert contiguity on ONE
  line at 80 columns: the old guard pinned `COLUMNS=160` and flattened the output,
  so it passed for five walkthroughs while nothing was copyable. **A copyable line
  printed only SOMETIMES reads as a working one**: `plans` printed its line only for
  a plan with an open integration PR, so pending, active and integrated plans got
  none, and it looked fixed in exactly the state you check after fixing it. One test
  scenario per branch of the condition, or the working branch masks the rest.
- **Help text is a status line too, and rich's borders can make its guard inert**:
  `add-project --harness` claimed the "registry default" while an omitted flag
  actually takes `settings.default_worker_harness`. Typer wraps a long help string
  across panel rows and draws a border on each, so the rendered text is
  `use the registry | | default` and a plain `" ".join(output.split())` never
  matches it. Strip the box glyphs BEFORE collapsing whitespace (the safe direction:
  it can only make a bad string easier to find).
- **The CLI forces UTF-8 on stdout/stderr** (`cli.main._force_utf8_stdout`). Without
  it, Windows redirected output falls back to cp1252, rich's truncation ellipsis
  becomes the byte `0x85`, and `praxis tasks | grep` answers "Binary file (standard
  input) matches". Interactive output was always fine, which is what hid it.
- **A doctor hint must name a verb that can do the job**, not merely one that exists:
  `praxis config` is a registered GROUP, so an existence check passes while running
  it only prints help. `tests/test_doctor_hints_name_real_verbs.py` gates this;
  group names come off the sub-app's `info`, since `add_typer` with no explicit name
  leaves `group.name` a `DefaultPlaceholder`.
- **A new surface's QUERY is the seam that goes inert**: `praxis pending` hid every
  autonomous proposal because `GET /api/approvals/pending` never selected those rows,
  while the predicate and the renderer were both correct and 45 of 46 tests stayed
  green. Test the layer that FETCHES, not only the one that DECIDES.
- **The merge verbs need their own HTTP timeout**: `merge-plan` merges a plan's
  PASSED tasks sequentially (up to `max_leaves_per_plan`), and one `merge_pr`
  under repeated 504s is three attempts plus backoff. The CLI's read-only 60s
  budget times out mid-merge while the orchestrator finishes correctly, so both
  verbs use `_MERGE_TIMEOUT` and report "may still be running", never "failed".
- **`get_dispatchable_tasks` maps `opus_plan["tasks"]` to rows BY LIST INDEX**: anything
  touching the graph (e.g. `core/leaf_split.py`) must only APPEND; supersede, never delete.
- **Hand-built LM Studio payloads must state `reasoning_effort` explicitly**
  (`core/thinking.py` is the SSoT). Not because of any one default: the default is not a
  stable API and INVERTED on the same endpoint, meaning maximum on 2026-08-15 and zero on
  2026-08-21. The levels are also not monotonic (`medium` thinks more than `high`), and
  `json_schema` extraction returns EMPTY whenever the model thinks at all, which is why
  `plan_derive` pins `none`.
- **Worker effort is PER-HARNESS and must be stated too**: `core/harnesses.py` declares each
  harness's `effort_channel`, `core/worker_effort.py` resolves it. `None` means "this harness
  has no knob", NOT "off". The OpenCode config key is camelCase `reasoningEffort`; snake_case
  is the wire field and is silently ignored in the config file.
- **An omitted `harness` must never downgrade a project**: `execute_plan` and `dispatch` pass
  `None` through, so an existing project keeps its configured harness and only a NEW project
  falls back to the registry default.
- **The progress handover reads the REMOTE branch**
  (`GitOps.remote_branch_commit_log`, `gh api .../compare/`), never a local clone. It used
  to pass `"."`, which in the container is `/app`: no `.git`, no target repo, so it raised
  on every dispatch into a swallowed `except` and the mechanism never once worked. Three
  states must stay distinguishable: `[]` is "no commits yet", `None` is "history
  unavailable", non-empty is "resume here". Every test mocking it to `[]` is
  indistinguishable from the bug, which is why it survived so long.
- **`git_ops.commit_and_push` returns `bool`**: True committed, False the index was already
  clean. "Nothing to commit" is a FACT (`git commit` exits 1 on a clean tree), and raising
  it turned a no-op save into a 500. Callers must report `unchanged`, not a commit.
- **Every route that touches a target repo goes through `api/repo_errors.guard_repo_access`**:
  `FileNotFoundError` to 404, everything else to 502 carrying the decoded `.stderr`.
  `str(CalledProcessError)` is only the exit code, and six routes handled that while four
  answered a bare 500 for an install missing a credential.
- **A submitted spec travels as a repo doc, never in the DB**: `POST /plans` commits it under
  `docs/superpowers/specs/` first and stores only `spec_path`; `plan_and_activate` reads it
  back through `Orchestrator._spec_reader` and fails the plan closed if it cannot. Both ends
  can be correct with the link dead, so `tests/test_submit_spec_seam.py` starts at the real
  `praxis submit` and ends at the prompt; keep it that way or the seam goes invisible again.
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
- **Auto-delegate, written and unexecuted, ORDER MATTERS:** review-scope single-branch
  (`plans/2026-08-14-review-scope-single-branch.md`) must land BEFORE the micro-edit lane
  (`specs/2026-08-21-micro-edit-lane.md`), which commits to the same shared branch with no
  dispatch and so depends on the base-SHA column the former adds. The lane skips the
  WORKER, never the governance: verify gate and a cheap review still run, on the same PR
- **Implemented + merged (2026-06-29 epic, live e2e-verified 2026-07-01):** worker context
  continuity (`specs/2026-06-29-worker-context-continuity-design.md`), capability-aware plan
  execution (`specs/2026-06-29-capability-aware-execution-design.md`), and the MCP orchestration
  guide (`2026-06-29-mcp-orchestration-guide-*`), each with a matching plan in
  `docs/superpowers/plans/2026-06-29-*`. Delivers the Static Bible + git-spine progress
  handover (whose commit-log read was passing `"."` and had therefore never worked in
  production until 2026-08-22; see the gotcha above),
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
