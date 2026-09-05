# CLAUDE.md - Praxis

## What Is This

Praxis is a provider-agnostic, Docker-based tool set up inside the coding harness you
already use, letting it drive other harnesses with every change governed by a gated
loop. It splits the software engineering loop into four roles - **plan**, **implement**,
**review**, and **verify** - each independently configurable to any provider, model, or harness,
and decomposes every plan to fit the capability of the model that implements it. Nothing is
hard-wired to a single vendor.

**Framing (canonical, 2026-08-22; presentation updated 2026-08-24):** the identity is *a
tool for agentic AI to govern other coding harnesses* - set up inside the harness you
already use, every change governed by the loop instead of one-shotted. Praxis is NOT
itself a harness. The public presentation names its SLOT: the **execution phase of
spec-driven development** - brainstorm, spec, and plan happen wherever the user likes,
Praxis takes the plan from there (same identity, stated by workflow position). Public
wording is "govern/governed", never "predictable" (unbenchmarked claim); avoid
"meta-harness" and "control plane" (claimed by larger projects). **Implement-a-plan is
the flagship FEATURE** (a single dispatched task is its smallest case); auto-delegate
mode is the companion feature (still beta), presented after the flagship, never
beside it.
Capability-aware task decomposition is the flagship MECHANISM (unique in the landscape);
role separation is the supporting architecture; cost is a consequence. **The exact public
copy - README headline ("Govern any coding harness from inside the one you already
use."), GitHub About (revised 2026-08-28: the "govern other coding agents" clause), and
the README opener - is frozen in `docs/positioning.md` ("Canonical copy (2026-08-24)",
plus its dated amendments): copy it verbatim, never re-derive it.** Since 2026-08-28
(owner direction) the README opener is PROBLEM-FIRST - the no-safety-net handoff hook
leads, the definition lives in the About and `pyproject.toml` only, and the old "Why
Praxis exists" section is gone, absorbed by the hook. The README also carries a
paste-able agent setup brief (Quick Start, "Set it up with your agent") with STOP points
at the interactive logins; keep it in sync with `praxis init --non-interactive` and the
init-printed MCP snippet. **No em dashes anywhere in the README or docs prose** (owner
rule; use comma/colon/semicolon). SSoT: `docs/positioning.md` ("The framing"). Flagship mechanism under development: the **Capability Calibration
Loop** - `docs/superpowers/specs/2026-07-11-capability-engine-roadmap.md` (F1-F15, S1-S11).

**WHERE ENGINEERING EFFORT GOES (2026-08-26, owner's direction). Read this before
planning a session.** The work is **maturing the EXECUTION PHASE of spec-driven
development**: `execute_plan`, the path from a plan document the user authored
elsewhere, through capability-aware decomposition, dependency ordering, per-leaf
review and the merge gate, to the integration PR. That is the flagship feature and
the slot the product claims, so that is the surface that must be excellent.
**Dropping auto-delegate's beta label is NOT a goal, and a session must not organise
itself around it.** Auto-delegate is MAINTAINED, not advanced: fix what a walk
happens to find in it, record it, move on. Its label drops incidentally when the
criterion is met, never as the target of a run. A walkthrough should therefore drive
`execute_plan` end to end on a real repository; `dispatch_task` is its smallest case,
not a separate exercise. Rationale and the same directive in the SSoT:
`docs/positioning.md` (consequence 1, "Where engineering effort goes").

The reference configuration pairs a subscription reasoning model (e.g. Claude via `claude -p`)
for planning/review with an open-weight model (e.g. via LM Studio) driving a pluggable coding
harness in Docker for implementation, but any role can point at any supported provider
(`claude`, `codex`, `agy`, or a `local` OpenAI-compatible endpoint) via the LLM router.
Worker harnesses are pluggable too: OpenCode (default) and Antigravity (agy/Gemini) ship
and are both tested; the harness contract supports others, but none are tested.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11, FastAPI, Uvicorn |
| Database | SQLite (aiosqlite, raw SQL, no ORM) |
| CLI | Typer + rich |
| Web UI | No-build HTML/CSS/JS (`web/index.html` + `styles.css` + `app.js`) |
| Containers | Docker SDK for Python |
| Agent | Pluggable harness - OpenCode (default), agy/Antigravity (custom Docker images) |
| LLM (any role) | Provider-agnostic via the LLM router - `claude`, `codex`, `agy`, or `local` (OpenAI-compatible); per-call-site configurable |
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
`doctor`, `approvals`, `events` (SSE), `internal` (agent callback). `api/auth.py` is NOT a
router: it holds the auth dependency and `main.py` includes nothing from it.

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
- **Execution:** `task_queue`, `plan_reachability` (pure derivation over
  `(plans.opus_plan, task rows)`, no DB: which pending leaves can never be dispatched,
  reproducing `get_dispatchable_tasks`'s pairing rule rather than a second copy),
  `agent_manager`, `agent_prompt`, `worker_bible`, `harnesses`,
  `preflight`, `session_resume`, `progress_handover`, `clarification_states`,
  `token_budget`, `context_window` (resolves a worker's context window, or says unknown),
  `micro_edit`, `run_elapsed` (pure derivation over `agent_runs` rows, no DB: how long a
  run has been going or took, and the one answer every surface and the wall-clock bound
  share)
- **Git & platform:** `git_ops`, `git_backend` (GitHub / local), `github_credentials`,
  `repo_url_policy`, `merge_policy`, `branch_sweeper`, `diff_guard`, `diff_stats`,
  `blast_radius` (repo-wide reach of the identifiers a diff changes, for the review prompt),
  `contract_drift` (did this diff edit a path the PLAN DOCUMENT names but never
  authorises - computed at review, rendered at the merge gate, never blocks)
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
uv run praxis mcp               # re-print the MCP client config block (server may be DOWN)
uv run praxis logs <task-id>    # the agent container log, captured on the run row

# See what is parked at the merge gate, and open it
uv run praxis pending                # parked tasks AND plans awaiting integration
uv run praxis merge <task-id>        # one task, plus every gated task on the SAME PR
uv run praxis reject-merge <task-id> [--feedback "..."]   # the other half of the gate
uv run praxis merge-plan <plan-id>   # parked tasks, then the integration PR

# Wait on a task or a plan (prints each transition; exit 0 at rest, 2 on timeout).
# Built on GET /api/{tasks,plans}/{id}/wait, the same endpoint MCP wait_task /
# wait_plan use. NEVER hand-roll a poll loop: the detail payload nests the row
# under `task` and a loop reading the top level once waited on finished work.
uv run praxis wait <task-id|plan-id> [--timeout 900]

# Unwedge a plan that is stalled but still ACTIVE (`praxis plans` names the id).
# A FAILED plan is REACTIVATED by the requeue too, or nothing would ever dispatch it.
uv run praxis retry <task-id>        # a FAILED task back to pending; 409 on anything else

# Setup, manual equivalent of `praxis init`
uv venv && uv sync --extra dev && cp .env.example .env

# Build every image AgentManager can spawn (agent images are behind a profile)
docker compose --profile agents build

# Run - containerized (RECOMMENDED: survives terminal exit, restart: unless-stopped)
# NOTE: the dev overlay BIND-MOUNTS ./src and runs --reload, so this container serves the
# WORKING TREE. A live observation taken while anything is editing src/ is evidence about
# the tree, not about HEAD, and rebuilding does not help (the mount shadows the image).
docker compose -f docker-compose.yml -f docker-compose.local.yml up --build -d  # dev, hot-reload
docker compose up --build                                                         # production
docker compose --profile hosted up --build                                        # with Caddy

# Run - bare uvicorn (quick one-off only; process dies with the terminal and orphans in-flight tasks)
uv run uvicorn orchestrator.main:app --host 127.0.0.1 --port 8080

# Tests. Run the NARROWEST selection that covers the change; the full suite runs
# in minutes, and running it after every edit is the single most expensive habit
# in this repo. Run it from GIT BASH: under PowerShell `bash` resolves to the WSL
# relay, whose /bin/bash does not exist, and every entrypoint shell test fails
# with execvpe - which reads as a broken suite rather than a wrong shell.
uv run pytest tests/test_<the_file_you_touched>.py -q
uv run pytest -q -k "<subject>"

# Full suite: only when it is actually needed. That means before a commit that
# lands, after a change to a shared seam (database.py, task_queue.py, schemas.py,
# a mixin), or when a narrow run comes back green on a change you do not fully
# trust. Not after every edit, and never twice for the same tree.
uv run pytest --cov=orchestrator --cov-report=term-missing -v

# Lint & format. CI's SCOPE IS WIDER THAN THIS - it adds bench/, and its mypy
# takes src/ (which is src/orchestrator + src/cli + src/mcp_server), not just
# the orchestrator. The narrow forms below are fine mid-edit; before a push run
# the CI forms underneath them, or a red lands on main from a directory you
# never checked. That is exactly how 1f155c7 went red: a mypy error in
# src/cli/main.py, on a run where the orchestrator and mcp_server were clean.
uv run ruff format src/ tests/
uv run ruff check --fix src/ tests/
uv run mypy src/orchestrator/ --ignore-missing-imports

# What CI actually runs (.github/workflows/ci.yml, `lint`). Mypy reports the
# file COUNT it checked - 117 at the time of writing - so a narrower scope is
# visible in the output if you look.
uv run ruff format --check src/ tests/ bench/
uv run ruff check src/ tests/ bench/
uv run mypy src/ bench/ --ignore-missing-imports
```

## CI/CD (GitHub Actions)

Workflows in `.github/workflows/` (added 2026-07-02, verified green on runners):

| Workflow | Trigger | Does |
|----------|---------|------|
| `ci.yml` | push main / all PRs | `lint` (ruff format+check, mypy) on Ubuntu; `test` matrix on **ubuntu-latest + windows-latest** (`pytest --cov-fail-under=80 --timeout=120`) |
| `docker.yml` | `docker/**` changes | shell-checks entrypoints, validates `--profile agents config`, builds `opencode-agent` + `agy-agent` (matrix, own dir) and the orchestrator (**repo-root context** - its Dockerfile copies `src/`, `web/`, `pyproject.toml`). `docker/caddy/` is a Caddyfile mounted into stock `caddy:2-alpine`, nothing to build |
| `security.yml` | push / PR / weekly | pip-audit (on `uv export`ed lockfile) + bandit + gitleaks |
| `codeql.yml` | push / PR / weekly | CodeQL security-and-quality |
| `dependency-review.yml` | PRs touching deps | blocks high-severity vulnerable additions (needs repo **Dependency graph** on) |
| `actionlint.yml` | `.github/workflows/**` | lints the workflows themselves |

- **bandit config in `pyproject.toml`** (`[tool.bandit]`): `skips = ["B404","B603","B607"]`
  for legitimate subprocess/CLI shell-outs + targeted inline `# nosec`. Baseline is 0
  findings, so any NEW hit is a real signal; keep the skip list minimal. **A `# nosec`
  only suppresses the line bandit REPORTS**, which for a multi-line f-string is the first
  INTERPOLATED line, not the assignment that opens it: a directive on `x = (` or on a
  comment line of its own is silently DEAD and the scan still fails. Prove a placement by
  deleting it and re-running; bandit never warns about a nosec that guards nothing.
  **`# noqa: S608` is RUFF's code and does NOTHING here**: bandit never reads `noqa`, and
  this repo's `[tool.ruff.lint] select` has no `"S"`, so that directive silences neither
  tool while looking like a suppression.
- **RUN THE CI COMMANDS, NOT YOUR OWN.** Three CI reds now. The 2026-08-27 one was the
  SCOPE, not the command: CI's mypy is `src/ bench/` and this file's own type-check line
  was `src/orchestrator/`, so a `src/cli/` error was invisible to a local run that reported
  "Success: no issues found" - the most convincing possible green. **A narrower scope is
  not a weaker check, it is a DIFFERENT check.** Mypy prints the file count it checked;
  compare it (117) before believing a clean run. Both reds of 2026-08-26 were local checks
  that passed for a reason CI does not share. `uv run bandit` exits `program not found` (bandit
  is NOT a dev dependency; CI uses `uvx`), and grepping that error for findings looks
  exactly like a clean scan - run `uvx bandit -r src/ -c pyproject.toml` and read the
  `Total issues (by severity)` block. And CI has NO `.env`, so anything resolved from the
  worker-preset default differs from a developer box: to test a suspected environment
  dependence, FORCE the CI value (e.g. `DEFAULT_WORKER_HARNESS=agy`) and confirm the old
  form fails, then sweep the whole suite under it rather than fixing one test per CI round
  trip.
- **Dependabot**: weekly pip/actions/docker, grouped by risk tier (runtime deps ungrouped
  so a breaking prod bump is isolated).

## Task State Machine

```
PENDING -> IN_PROGRESS -> REVIEWING -> PASSED -> (human approve) -> MERGED
                                    -> FAILED -> (re-dispatch, max 3)
                                              -> (attempts spent) terminally FAILED,
                                                 and every PENDING leaf behind it is
                                                 UNREACHABLE -- `praxis retry <task-id>`
         -> NO_CHANGES        (work already present; verified on the base branch)
         -> NEEDS_CLARIFICATION (worker asked; parks for `praxis clarify` and
                                 waits indefinitely for a person)
         -> SUPERSEDED        (split into children, which carry the work)

Then, once every task is done:
plan COMPLETED -> integration PR -> (human approve) -> integration_merged_at
```

Every `TaskStatus` value appears above; a surface that lists fewer teaches a caller to
poll for a state that will never arrive. (No count is quoted on purpose: the last one
said "eight" while the enum carried nine.) `NO_CHANGES` and `SUPERSEDED` are terminal,
neither success nor failure; both are in `SATISFIED_STATUSES` (`core/status_vocab.py`),
which unblocks dependents and lets the plan complete. `NEEDS_CLARIFICATION` is the third
gate: nothing advances it but a human answering. `FAILED` is NOT in that set, so once a
leaf is terminally failed its dependents can never be dispatched and the plan is stalled
while still reading ACTIVE with a null `error` -- deliberately, and the detection is
`core/plan_reachability.py` (see `docs/gotchas.md`).

## Data Model (SQLite)

Tables: `users`, `projects`, `plans`, `tasks`, `agent_runs`, `opus_state`,
`doc_index`, `settings_overrides`, `capability_events`, `task_outcomes`

- Default `admin` user auto-seeded on first startup (token_hash = AUTH_TOKEN).
- `capability_events` and `task_outcomes` come from MIGRATIONS, not the baseline
  `CREATE_TABLE_STATEMENTS` block (easy to miss reading `database.py` top-down). They
  carry the capability engine's decision trail and per-task outcome records.
- `opus_state` is a singleton row: `available` / `rate_limited` / `resuming`.
- `plans.integration_pr_url` / `integration_merged_at` (migration 9) hold a completed
  plan's last step; without them the integration PR is invisible to read-only surfaces.
- `tasks.review_base_sha` (migration 10) is where a task's own work STARTS on a shared
  branch. NULLABLE and the NULL is load-bearing: it means "review the whole pull
  request" (two-tier mode and every pre-migration row).
- `plans.plan_attempts` (migration 11) bounds the planning retry; `plans.error` carries
  the reason. Both are on `PlanResponse` and on MCP `poll_plan`, with the cap served
  alongside the count so no client mirrors it. **BOTH planning seats charge it** since
  2026-08-26: `decompose_pending_execute_plan` used to bound nothing at all, so every
  exception class escaped and re-ran each tick while the row read exactly like a healthy
  plan mid-decomposition. `plans.error` is a ONE-WAY signal: `reset_plan_attempts` clears
  the count but NOT the error, so present means a real reason and absent proves nothing.
- `projects.context_window` (migration 12) is the per-project override that outranks a
  declared window and the LM Studio probe alike.
- `tasks.contract_drift` (migration 13) is what a task's diff did to the paths its PLAN
  DOCUMENT authorised, computed at review time from the diff already in hand and carried
  to whoever opens the merge gate. JSON text, decoded by the ONE decoder
  (`core/contract_drift.decode_payload`). NULLABLE, and the NULL means "never computed"
  (a pre-feature row, or a review that failed before a diff existed); a checked-and-clean
  task carries `{"gradable": true, "named_not_authorised": [], ...}` instead. **The
  distinction lives in the DATA and in the REST payload, NOT on any rendering surface**
  (corrected 2026-08-28; this entry used to claim "every surface renders it as 'not
  checked', never as clean", which was never true of any of them). `_drift_line`
  (`src/cli/main.py`), `contractDriftBlock` (`web/app.js`) and MCP's summary all render
  NOTHING for NULL and for checked-clean alike, deliberately and for a stated reason: a
  clean line on every parked task trains the reader to skip the block that also carries
  the warnings. So a human at the gate cannot tell "not checked" from "clean", and that
  is the accepted trade, not an oversight. Only the UNGRADABLE reason is printed, because
  that one is not the normal case.
- **`plans.integration_state` (migration 14) is the POSITIVE record of the integration
  stage**: `opened` / `reused` / `nothing_to_integrate` / `failed`, or `skipped` for an
  early return, written on EVERY exit of `on_plan_completed` (a `finally` in the wrapper;
  a decided outcome unconditionally, `skipped` only where nothing is recorded yet, so a
  re-entry after the PR was recorded keeps `opened`). NULL means exactly "the stage has
  not recorded an outcome yet". Found by item 0's own acceptance walk: `process_plan_once`
  writes COMPLETED and THEN the stage opens the PR, so a wait resting on the status alone
  said "nothing more will happen" 30 s before PR #13 existed, and the row carried nothing
  (no `updated_at` on plans, and the no-op outcome deliberately writes no URL and no
  error) that could tell "integrating now" from "nothing to integrate". The backfill
  maps a pre-column completed row from what it shows (URL: `opened`; error: `failed`;
  neither: `nothing_to_integrate`) and never overwrites a recorded state.
  `core/waiting.plan_waiting_on` rests on it: completed + merged: `nothing`; + open PR:
  `human`; + recorded state or error: `nothing`; + NULL: `review` (integrating).
  `praxis plans` renders `(no PR; already on base)` and `(integrating)` from it.
- **Schema version is 14; `tests/test_migrations.py` pins it.** Idempotency is proved by
  invoking a step DIRECTLY TWICE, never by rewinding `user_version`: a rewind that no
  longer reaches far enough silently stops re-running the step, and a `count(...) == 1`
  assertion passes whether the step re-ran or never ran at all.
- **`plans.spec` was dropped (Spec 2)** - markdown docs are the source of truth; the DB
  is a thin execution ledger. `plans` keeps `opus_plan` (the runtime task graph -
  `get_dispatchable_tasks` reads it for `depends_on` ordering, NOT redundant),
  `spec_path`, `plan_path`, status, branch. `initialize()` drops legacy `spec` via the
  SQLite table-rebuild pattern, guarded by `PRAGMA table_info`; a `before_drop` callback
  (`core/backfill.backfill_legacy_specs`, wired in lifespan) first writes orphaned spec
  text to a `*-legacy.md` spec doc. Backfill skipped when no `GITHUB_TOKEN`.

## Key Design Decisions

- **No ORM** - raw SQL via aiosqlite; baseline tables as `CREATE TABLE IF NOT EXISTS`
  run every startup. Schema changes go through the versioned `MIGRATIONS` list +
  `PRAGMA user_version` in `database.py`; steps must be idempotent.
- **Global settings layer (Spec 2)** - git-trackable defaults in `config/praxis.yaml`;
  precedence is **env var > `.env` file > settings file > field default**. The `.env`
  layer is explicit and load-bearing (`_dotenv_keys` counts the dotenv file as
  environment; `PRAXIS_`-prefixed vars are exempt). A key present in both files logs ONE
  `INFO` line naming the winner. Keys map by uppercase field name. Project config stays
  DB-backed.
- **Provider-agnostic LLM router (Spec 3)** - `core/llm_router.py` resolves each brain
  call-site to `{provider, model, effort}` and executes it. `CALL_SITE_DEFAULTS` is the
  tiering policy but is SHADOWED wherever the call-site has a role (see the role-chain
  gotcha). CLI providers `claude` / `agy` / `codex` via `build_argv`; `local` is an LM
  Studio OpenAI call. `OpusBridge` routes `plan_spec` / `review_diff` /
  `analyze_improvements` / `classify_doc` through the router (legacy `_run_claude`
  fallback keeps rate-limit handling). **Brainstorm is NOT routed yet** (stream-json
  incompatible with text-mode `build_argv`). Per-call-site overrides live in
  `settings_overrides` (`models.<call_site>`), managed via `GET/PUT
  /api/settings/models`, surfaced in dashboard **Settings → Models**. Provider status
  (2026-06-23): `claude` + `local` work; `codex` needs `codex login`; `agy` is
  **unusable as a brain** (its `--print` yields no non-interactive stdout).
- **Single FastAPI monolith**; **Docker SDK** spawns harness agent containers;
  **`claude -p`** for Opus interactions; **rate-limit handling** detects the 5h
  subscription limit, queues Opus calls, auto-resumes.
- **Two-tier git branching** - `plan/{date}-{slug}` groups tasks, `agent/{task-slug}` per task.
- **Single static auth token** for v1 (data model supports multi-user later).
- **Pluggable harnesses** - `core/harnesses.py` is the registry; projects pick via the
  `harness` column (default `opencode`). `AgentManager.spawn_agent` selects the image and
  sets a harness-agnostic env contract (`HARNESS`, `MODEL`, `OPENAI_API_BASE`, +
  repo/branch/callback vars); each `docker/<harness>-agent/` honors the same entrypoint
  contract.
- **Worker-facing prompts are written for the FLOOR model** - `core/agent_prompt.py` and
  `core/worker_bible.py` target the least capable worker that might receive them: short
  imperative lines, explicit format with a worked example, critical rules repeated at the
  end. A frontier model loses nothing from this register; a floor model loses the task
  without it. The brain-side rules for writing task descriptions live in the MCP resource
  `praxis://guide/orchestration` ("Designing the worker prompt") and in the decompose
  prompt (`core/plan_review.py`), so `execute_plan` leaves follow them automatically.

## Auto-Delegate Mode (daily-dev) - COMPANION, maintained not advanced

> Reference for the companion feature. Per the engineering-focus note above, do not
> organise a session around this section: the flagship is `execute_plan`, documented
> in `docs/workflow.md` and exercised through MCP `execute_plan`.

When auto-delegate is ON (`GET /api/settings/auto-delegate` → `enabled:true`), the brain
does NOT edit code directly: per task it designs the worker prompt (see the guide named
above), calls MCP `dispatch_task` (global default worker), then reviews the resulting PR.
Planning, prompt design, and review stay with the brain. Mode is sequential (one delegate
in flight) on a single caller-named work branch; dead branches are swept by reconcile.
The sequential rule is load-bearing (per-task review scoping depends on it) and
`dispatch_pending_tasks` ENFORCES it at the BRANCH: one task per wave, held while any task
on that branch is `in_progress` or `reviewing`, **across every plan in the project**. The
cross-plan scope is what makes it work here at all: each MCP `dispatch_task` becomes its
own one-task plan, so a plan-scoped hold never had a second task to hold against and could
not fire on this path (fixed 2026-08-25). What is STILL unenforceable: a commit pushed to
that branch from outside Praxis. Toggle: `praxis mode on|off|status`.

**N tasks share ONE pull request here, and the merge gate accounts for that** (fixed
2026-08-26, found live in walkthrough #14). Merging any one of them lands all of their
work, so `approve_task_merge` sweeps every OTHER gated task in the SAME project on the
same `pr_url` out of the gate with it. All three scope conditions are load-bearing; the
project one especially, because a local ref is `praxis-local://pr?branch=...&base=...`
and encodes NO repository, so two local projects sharing a branch name share the exact
URL string. `pending_approvals`'s `count` counts DISTINCT PRs for the same reason: nine
parked tasks over four PRs read as "9 PRs awaiting your approval".

**A PR resolved in the GitHub UI is reconciled** (2026-08-26): Praxis hands a human a
`pr_url` and the obvious way to act on it is GitHub, which used to leave the row parked
forever. `reconcile_merge_gate` asks the backend and acts, and the four outcomes are
deliberately NOT symmetric. MERGED leaves the gate through `_sweep_merged_siblings`.
A CLOSED task PR did NOT land, so it FAILS with a reason and never retries: a closed PR
carries no feedback a worker could act on, and retrying would loop autonomously off a
human's "no". OPEN, and anything nobody could establish, are left alone. **A CLOSED
INTEGRATION PR deliberately does LESS**: it records the reason on `plans.error` and stays
parked, because the only status that unparks it is REJECTED, and that puts the plan
branch into the sweeper's terminal-failed set where a real `git push --delete` destroys
the ENTIRE plan's work (`orchestrator_reconcile.py`, the plan branch bucket).
`praxis-local://` refs are skipped outright, never probed: a bare repo has no UI, and the
tempting ancestor check is wrong because `LocalGitBackend.merge` SQUASH merges. Throttled
by a per-PR cooldown, a minimum parked age, and a within-pass memo keyed on `pr_url`.

**Micro-edit lane (2026-08-25):** pass `micro_edit={path, content, commit_message}` to
`dispatch_task` and NO container is spawned - `core/micro_edit.py` clones server-side,
commits that one file to the work branch, and the task goes straight to `reviewing`. It
skips the WORKER, never the governance: the verify gate, the review, the merge gate and
the outcome row all run. The lane records its OWN `review_base_sha` at the commit (read
after checkout, before the write) and sets `implement_harness`/`implement_model` to
`"brain"` so calibration is never taught the worker did it. Requires `branch` and
auto-delegate mode (422 otherwise). A failed micro edit is TERMINAL, never retried.
Rubric lives in the mode contract (`praxis://guide/orchestration`), not in the engine.

## Gotchas

**`docs/gotchas.md` is the full list, with the narrative for each. Read the relevant entry
there before touching a subsystem.** Below is the shortlist that bites during ordinary
edits, because each fails SILENTLY; the one-liners here are reminders, not the whole
story. New gotchas go in `docs/gotchas.md` first. (No count is quoted on purpose.)

**Editing and running**

- **`ruff format`, not `ruff fmt`**; the claude effort flag is **`--effort`**, not `--reasoning-effort`.
- **CLI lives at `src/cli/`** (works via `where = ["src"]` in pyproject).
- **Patch orchestrator helpers on the MIXIN module** that calls them
  (`orchestrator_{dispatch,review,reconcile,improve}.py`), never `core.orchestrator`.
- **`.env` is read by pydantic-settings**: tests asserting a missing env var must pass
  `_env_file=None`, and clear ambient env or CI's value wins. **A test that asserts two
  things DIFFER must CREATE the difference**: hardcoding one side of a `!=` while the
  other resolves from the worker-preset default passed locally (`.env` set `opencode`)
  and failed on CI (no `.env`, both sides `agy`) with `assert 'agy' != 'agy'`.
- **SQLite lives at `data/orchestrator.db`** (CWD-relative); delete it to reset state.
- **Windows port cleanup is `taskkill //PID <pid> //F`**, never `kill -9`.
- **`.gitattributes` pins the tree to LF**; the Windows CI runner checks out CRLF, which
  silently breaks any `entrypoint.sh` a test executes.
- **`pathlib.write_text` converts to CRLF on Windows and `read_text` HIDES it**: a `\n`
  anchor silently misses. Detect with `read_bytes()`, write back in the original style.
- **Assert CLI output through `tests/cli_text.py`**, never raw `result.stdout` (rich
  colorizes per-platform). Run once as `FORCE_COLOR=1 TERM=xterm-256color pytest` before
  believing a help-text guard.
- **Worker prompt surfaces have a wire contract AND a register**: both entrypoints grep
  `^Status:` and the `^Concerns`..`====` block from worker output, so those labels stay
  at line start; and `agent_prompt.py`/`worker_bible.py` are written for the least
  capable worker model - keep edits in that register (see Key Design Decisions).

**Config and deployment**

- **`config/praxis.yaml` is MOUNTED, not baked**: a YAML edit needs `docker compose
  restart`, never a rebuild. `core/settings_file.config_file_path()` is the ONLY place
  the path is decided; `tests/test_config_path.py` greps for hardcoded literals.
- **Applying a `.env` edit: tell an operator `up -d` then `restart`.** Keys compose
  forwards (`${AUTH_TOKEN}`, bare pass-throughs) are baked at container CREATE and need
  `up -d`; keys it does not forward (`LOOP_INTERVAL`) are read from the mounted
  `/app/.env` and need `restart`. The `env_drift` doctor check sees only the forwarded half.
- **A host `~/.claude` hook blocks every brain call inside the container** (the dir is
  mounted). Put the opt-out (e.g. `CLAUDE_VPN_KILLSWITCH_OFF=1`) in `.env.container`
  (gitignored compose `env_file`), then `up -d`. NEVER `.env`: compose passes nothing
  from it into the container.
- **Agent images sit behind the `agents` compose profile** (build-only): rebuild after
  ANY `entrypoint.sh` change or a stale image runs silently. Staleness is judged by the
  `org.praxis.entrypoint-sha256` label (content, not mtime); rebuild via `praxis init` +
  `docker compose --profile agents build` - a bare `docker build` leaves the label EMPTY,
  and so does a compose build WITHOUT `praxis init` first: the arg comes from
  `OPENCODE_ENTRYPOINT_SHA256` / `AGY_ENTRYPOINT_SHA256` in the environment, which only
  `init` exports (`docs/gotchas.md` has the one-liner to export them by hand).
- **A local `repo_url` is validated in one namespace and mounted in another**: preflight
  checks it with `Path.exists()` INSIDE the orchestrator container, but the daemon resolves
  the same string as a bind-mount source on the HOST. `LOCAL_REPOS_PATH` (+ the
  `LOCAL_REPOS_HOST_PATH` escape hatch) bridges it; apply with `up -d`, never `restart`.
  `agent_manager.host_bind_source` TRANSLATES the prefix for the agent's own mount and
  refuses naming both namespaces when it cannot; compose substitution vars never reach the
  container env, so `compose_variable` reads env then the mounted `/app/.env`.
- **A doctor probe must not mutate what it diagnoses**: the agy credentials probe mounts
  the real volume read-only and layers a tmpfs over the writable path, so the kernel
  guarantees it cannot silently seed the "no credentials" state it is checking for.
- **`max_agent_concurrency` (YAML, env; default 3) bounds agent containers install-wide**
  (2026-09-05). It lived only in `AgentManager`'s constructor default until probe 9b fanned
  an 18-leaf wave out to exactly three workers and refused the fourth ("Concurrent agent
  cap reached (3 of 3 running)", re-dispatched when a slot opens, nothing charged).
  `main.py` passes it; `tests/test_max_agent_concurrency_is_a_setting.py` pins the kwarg.
  **`max_brain_concurrency` (YAML, env; default 3) is its twin for BRAIN stages**
  (2026-09-05): how many decompositions and reviews may run at once; a stage that finds
  no slot waits (visible as `waiting` on `/api/status` and `praxis status`), and `1`
  restores the old ordering without the pass ever blocking on a stage again. See the
  stage-jobs entry under "The loop".
- **Worker preset env vars are BARE compose pass-throughs** (`- DEFAULT_WORKER_HARNESS`),
  never `${VAR:-default}`: any expansion form sets the var even when unset and silently
  suppresses the mounted YAML.
- **`praxis doctor` is the front door**: read-only against repo and DB, but it does SPEND
  (one planner call per run, and one `agy models` container when an agy harness is in
  play), each cached 60s; a rate limit is AMBER. Decision logic in
  `core/doctor_probes.py`, fact gathering in `api/doctor.py`. (No check count is quoted
  on purpose: the last one was stale.)
- **The planner check probes the CONFIGURED planner** (via `call_site_chain` +
  `build_argv`, same objects the loop uses) and names what it probed. A `local` planner
  is AMBER; `codex`/`agy` stay "not probed". `GET /api/status` and `praxis status`
  resolve the same way.
- **A fix applied to the doctor is NOT applied to the product**: every doctor row has a
  status endpoint / CLI verb / dashboard twin answering the same question - grep for
  them and fix in the same commit.
- **A YAML role chain SHADOWS `CALL_SITE_DEFAULTS` entirely**: shipped chains are
  `plan: [sonnet, opus]`, `review: [sonnet, haiku]`, so on a stock install every routed
  call-site runs sonnet. A **Settings → Models** override for a role'd call-site is
  stored and IGNORED: clear the role chain first. Full account: `docs/configurations.md`.
- **A role chain is stored PER ROLE, and only when it differs from the settings file.**
  Every writer of `PUT /api/settings/roles` (`praxis config set-role`, the dashboard, a
  bare curl) GETs the EFFECTIVE map, changes one key and PUTs the whole map back, so
  storing that body wholesale pinned EVERY role after a single `set-role` and the mounted
  YAML stopped reaching any of them. Per-role keys alone do NOT fix it, because the body
  names every role either way: the comparison against the file is what makes "the caller
  touched this one" recoverable. A legacy wholesale row is CONSUMED on the next write, not
  stranded. `models.registry` keeps wholesale storage (a list has no per-entry key for
  "absent means use the file", and a merge could no longer express REMOVING a model the
  file declares) but adopts the same equality rule.
- **A key in `.env` that the settings file also names WINS and says so** (one `INFO`
  line naming the winner, once per key per process).
- **An unrecognised key in `.env` is IGNORED, not rejected**: a typo in a real key is
  silent and `env_drift` cannot see keys that live only in `.env`. `AUTH_TOKEN` is the
  required exception.
- **AN OPENAI-COMPATIBLE ENDPOINT MAY IGNORE `model` ENTIRELY, so a model string Praxis
  never verified is a LIE IT RECORDS** (2026-08-27). Measured against the reference LM
  Studio endpoint: `glm-4.7` AND `totally-made-up-model-xyz` both returned HTTP 200 with
  `"model": "qwen3.8-27b"` - whatever was loaded. No 404, no error field. So a worker
  preset or an escalation rung naming a model nobody serves is not a failure you find out
  about: it is a SILENT no-op that still stamps `tasks.implement_model` and still writes
  `task_outcomes` rows under that name, teaching the capability engine a stronger model's
  success rate from a weaker model's output. `task_outcomes` is the one table the whole
  calibration loop reads. **`implement_escalation` therefore ships EMPTY** (a supported
  state: `escalate` falls through to `human`), and a rung Praxis cannot verify is worse
  than no rung; `test_shipped_ladder_never_names_the_default_worker_as_a_rung` enforces the
  file's own rule as far as equality can. **Verify a rung with the `model` field of the
  RESPONSE, never the request** - the curl is in `config/praxis.yaml`. A stripped namespace
  prefix (`vendor/x` -> `x`) or an added instance suffix (`x` -> `x:2`) is benign; a
  DIFFERENT model is the fault. The model-LIST probe was authoritative all along.
  **The pre-dispatch probe EXISTS now** (2026-09-05, `core/worker_model_probe.py`): before
  a worker is spawned on a harness with an OpenAI-compatible endpoint, a one-token
  completion is sent and the RESPONSE's `model` field is compared with the same two
  tolerances. `substituted` FAILS the task before any container exists (no run row, no
  outcome row, `worker_model_substituted` event, operator-facing reason naming both
  models); `served` and `unverified` both proceed, because the probe catches
  SUBSTITUTION, not reachability, and an outage is the provider-error path's job. Patch
  it on the MIXIN (`orchestrator_dispatch.probe_served_model`); conftest stubs it autouse.
  The ladder still ships empty: the probe makes a rung REFUSABLE, not verified.
  **`model_matches` is the ONE comparison** (same day): the doctor's worker row and
  `detect_context_limit` used exact strings, so a `qwen/qwen3.8-27b` listing read the
  configured `qwen3.8-27b` as "not loaded" and the window as unknown on an install every
  dispatch ran fine on. All three seats share it now.

**The loop**

- **Per-task review scoping DEPENDS on one worker per branch at a time, and the loop
  enforces it** (one task per wave in single-branch mode, held while any task is
  `in_progress`/`reviewing`). A task is reviewed on `review_base_sha..head`; two workers
  interleaving commits widen both ranges silently. A re-dispatch KEEPS its recorded sha;
  only a vanished branch gets a fresh one; NULL means "review the whole PR".
- **Merge is gated by default** (`core/merge_policy.py`); protected branches never
  auto-merge at all.
- **A review verdict CANNOT say the diff rewrote the plan's contract, so the gate says it**
  (`core/contract_drift.py`, 2026-08-27). The reviewer grades against the LEAF's
  `plan_text`, so a leaf told to rewrite the plan's acceptance bar passes CORRECTLY and in
  silence - that is round 7's fabrication exactly. Computed at review from the diff already
  in hand, stored on `tasks.contract_drift`, rendered by `praxis pending` / `praxis task`,
  `GET /api/approvals/pending`, MCP `poll_task` (strong tier in the SUMMARY, so a relaying
  assistant repeats it) and the dashboard. TWO tiers: a path the plan NAMES but never
  authorises (strong - caught PR #103) versus one the plan never mentions (weak - a new
  sibling file, the normal output of the sizing rule). **A THIRD tier since 2026-09-05
  (probe 9e): a path the diff CREATED that the plan DESCRIBES as already existing**
  (`created_described_as_existing`; the cue is "already has/exists", "existing", "next to
  the" on the same plan line as the path). A plan asserted a `tokenizer.py` with `words`
  and `_normalize`; neither existed; the worker invented both and the reviewer praised it
  for "reusing `_normalize`". Strong tier: in the MCP summary, the CLI glance ("1
  phantom") and the dashboard block. ADVISORY everywhere and wrapped so
  it cannot wedge a review. **The same signal was REFUSED as an F3 validator rule and that
  is not a contradiction**: a false positive costs a brain call and can fail a plan
  upstream, and one glance here. Full account, with the mutation-proven properties, in
  `docs/gotchas.md`.
- **A blank `verify_cmd` is "not configured", never a pass** (blank shell exits 0).
  `core/verify_gate.normalize_verify_cmd` is the SSoT at all four read sites; the API
  rejects it 422; `run_verify` raises rather than shell it.
- **`loop_interval` reaches `run_loop`; shipped default is 5**; non-positive is floored,
  never honoured. **`callback_grace` and `worker_timeout_minutes` reach the Orchestrator
  the same way, from `main.py`** - the first did NOT until 2026-08-29 and was decoration.
  A new timing key is not wired until something passes it: check with
  `rg -n "<key>" src/` and expect a hit in `main.py`, not just in `config.py`.
- **Brain prompts go via stdin, never argv** (argv overflows; Windows `WinError 206`).
- **`gh pr` calls need `--repo <owner/name>`** or they target the orchestrator's own cwd.
- **A PR may be reused only on a POSITIVE open-state hit**: `gh pr list --head --base
  --state open` (all three filters load-bearing, `tests/test_entrypoint_pr_reuse.py`);
  the emptiness of the OUTPUT is the signal, never the exit status.
- **Verify gates FAIL CLOSED and fetch the branch first**: `error` counts as `failed`,
  only `skipped` passes through, and an `error` is never memoized (next tick retries).
- **The merge gate verbs are `praxis merge` / `reject-merge` / `merge-plan`.**
  `praxis approve` / `reject` are for improvement PLANS and 404 on a task id (reads as a
  broken command rather than the wrong one).
- **The improvement loop must be given the REPOSITORY and fails closed without it**
  (`core/repo_survey.py`): no readable repo means NO proposal. The positive
  `EMPTY_REPO_SURVEY` "no files found" line is refused by EXACT equality, not by a
  blankness check it sails through.
- **An improvement proposal's SHAPE is validated, and a malformed one FAILS the plan
  terminally**: `activate_plan` commits the plan row BEFORE the task loop that subscripts
  `title`/`slug`/`description`, with no rollback and a commit per statement, so a missing
  field left an ACTIVE plan with a graph and ZERO task rows - `all_tasks_done` is
  `bool(tasks) and ...`, so it was runnable forever. Same `_validate_plan_shape` the
  `plan_spec` seat uses, but terminal here rather than retried: this seat owns no input a
  later pass could re-plan. The improvement branch carries the plan id too
  (`plan/{today}-improve-{plan_id[:8]}`), or two same-day plans share one branch.
- **A plan with no commits has nothing to integrate**: decided by
  `_nothing_to_integrate_reason` on a positive check, which settles THREE facts. Two known
  equal SHAs (every task a no-op); an ABSENT plan branch (single-branch mode, where
  merging the task PRs IS the integration and deletes the branch); and a branch that
  merely TRAILS base, via `GitBackend.base_contains`, the only arm that needs the BACKEND.
  `remote_head_sha` returns None for an absent branch and RAISES when it cannot ask, so
  None is an ANSWER; only the exception falls through to the creation attempt. Likewise
  `base_contains` returns `None` for "could not ask" and only `True` changes the flow.
- **A COMPLETED plan's follow-ups run in the BACKGROUND, never inline in the pass**
  (2026-09-05, probe 7). The context-sync draft (a bare `claude -p`, formerly with no
  timeout) and the improvement analysis are brain calls of minutes, and `run_once` is one
  sequential pass: measured, three freshly submitted plans sat undecomposed for 5m41s
  behind one completion. `Orchestrator._spawn_background` runs both (tracked; `shutdown`
  cancels; tests `await orch.drain_background()` before asserting on a proposal or a
  `context_draft_ready` event). The integration PR stays INLINE: it is the plan's own
  last step and the wait rests on it. `ContextSync._run_revise` is bounded by
  `DEFAULT_REVISE_TIMEOUT` (600 s) and kills the CLI.
- **DECOMPOSITION AND REVIEW ARE STAGE JOBS, NOT AWAITED IN THE PASS** (2026-09-05,
  round 13). Measured before: three plans submitted together decomposed back to back at
  73/51/41 s and plan A's first leaf dispatched 1m44s after A activated; on the 19-leaf
  plan the three reviews of wave 1 ran serially and wave 2 spawned only after the last
  of them, 4m07s after wave 1. `Orchestrator._start_stage` now starts each planning seat
  and each review as a tracked `asyncio.Task` keyed `plan:<id>` / `review:<id>`, under
  one semaphore sized by **`max_brain_concurrency`** (YAML/env, default 3, passed from
  `main.py`, floored at 1; mirrors `max_agent_concurrency`). Three rules carry the old
  invariants: the KEY dedupes (the plan reads PENDING with no graph and the task reads
  REVIEWING for the whole call, exactly what the next pass sees, so without it a plan is
  activated twice and a task reviewed twice, and `plan_attempts` would no longer be the
  only bound); the single-branch hold and the terminal decision already read REVIEWING as
  active, so a review in flight holds the shared branch and never completes its plan
  early; and EVERY merge into a repository goes through
  `ReviewMixin._merge_under_repo_lock`, one `asyncio.Lock` per `repo_url`, because two
  passing reviews can now reach `backend.merge` together and the local backend's
  clone-merge-push would lose one (`_land_merged_pr` is the one landing seat for the
  auto-merge arm and `approve_task_merge`; `tests/test_pass_serves_stages_concurrently.py`
  pins exactly ONE bare `await backend.merge(` in the mixin). A stage failure is that
  stage's problem (logged with the key; `ProviderAuthError` republished as
  `provider_auth_required`, which `run_loop` no longer sees); `shutdown` cancels stages
  and the router KILLS the cancelled CLI. **Tests that drive a pass and then assert on a
  decomposition or a review must `await orch.drain_background()` first**: 27 call sites
  gained one; a test that passes without it is passing on scheduling luck.
  `GET /api/status` serves `brain_stages` (`cap`, `in_flight` with `waiting`/`running`
  and elapsed) and `praxis status` prints a "Brain stages" line. `handle_clarification`
  is still inline (rare; recorded). Thirteen behaviour mutations red, two of them only
  after the guards were strengthened (an equivalent floor mutation, and a shutdown guard
  that checked the count after `clear()` instead of the cancellation itself).
- **A plan reaches the gate TWICE**: each task onto the plan branch, then the plan's own
  integration PR. The URL lives on `plans.integration_pr_url`; `integration_merged_at`
  takes it back out of `pending`.
- **A fact about the REPOSITORY, the BASE BRANCH or a SIBLING leaf is not a fact about THIS
  leaf**, and that one class recurred at seat after seat: the review head gate, the wave
  verify gate, the empty-diff seat (all fixed 2026-08-26), and `on_plan_completed`'s
  backstop (fixed 2026-08-27). Enumerate the seats with
  `grep -rn "run_verify(\|_verify_plan_branch(\|verify_gate_disabled(" src/`, and sort the
  hits into DECISION seats versus the shared funnel and status reporting; no count is
  quoted on purpose. **Only TWO of the three parts apply at PLAN scope**: a completed
  plan branch carries several leaves, so no single leaf's declared `verification` speaks
  for the tree, and the positive-signal step must not be invented for it. The backstop
  stays ADVISORY - the integration PR opens on every arm - and an un-attributable result
  gets its own `verify_status` value, never `failed` (which lied) and never `passed`
  (a larger lie).
- **The project `verify_cmd` settles REGRESSION; the BASE BRANCH settles ATTRIBUTION; the
  leaf's own declared `verification` is the positive signal** (`review_task`, 2026-08-26).
  Every non-final leaf of a DEPENDENT chain used to be failed by a bar only the complete
  feature could clear. A base `error` or skip FAILS CLOSED and names the missing comparison.
  Not attributing is NOT passing: `_GATE_UNATTRIBUTED` reaches the human, and every surface
  that renders `review_scope` must say a gate RAN and is RED (the CLI's `_scope_glance`
  called it "no gate" within the hour; the phrases now live in `core/verify_gate.py` and
  writer and reader both import them).
- **"Fails identically" means the SAME FAILURE, not "both failed"** (fixed 2026-08-27).
  The comparison was `base.status != "failed"` and nothing more, so a base that could not
  even COLLECT and a head that failed three assertions counted as one failure and a real
  regression was excused. `verify_gate.compare_failures` asks the runner's own EXIT CODE:
  zero noise sources, no parser, no language knowledge. Output equality and set
  containment were both measured and REFUSED - an honest worker adding passing tests
  lengthens the progress line, so containment charges it for repository health. A TIMEOUT
  reports `returncode=None`, load-bearing because a killed process reports 1 on Windows,
  exactly like "tests failed". Three outcomes, not two: `FAILED_ALIKE` and `INCOMPARABLE`
  license the same ACTION and different CLAIMS. Attribution on `FAILED_DIFFERENTLY` is
  BOUNDED to a leaf that declared the project command as its own acceptance, because
  leaf 1 of a dependent chain can UNMASK failures.
  **This is what makes `split` reachable at all**: the final leaf's acceptance IS the
  project command (correct plan authorship), so its check is non-discriminating, so every
  failure was non-attributable, so no calibration row and no triage - and triage is where
  `split` is decided. Four correct rules composing. `split` is STILL UNOBSERVED; reachable
  is not observed. **And the blocker MOVED rather than cleared** (2026-08-27): that arm is
  sound and confirmed reachable, but the final leaf of a dependent chain never reaches it -
  see the triage-route entry below. Do NOT re-fix the verify gate for `split`.
- **The budget gate sized the BIBLE, not the prompt** (fixed 2026-08-27), scoring
  everything else zero: measured, it saw 445 tokens against a 1638 budget and PASSED while
  Praxis put 2152 in the window. `_build_worker_bible` now passes the EXACT prompt
  (`companion_prompt`), beating the derived `_TEMPLATE` floor, which omits the unbounded
  task description. **No reserve was added for what cannot be counted** (harness system
  prompt, tool schemas, the repo's `AGENTS.md`): `WORKER_RESERVE_FRACTION` already holds
  back 60% for that envelope, so a reserve double-charges - the shortfall is NAMED in
  `token_budget.UNCOUNTED_CONTEXT` instead. Counting the prompt is a MEASUREMENT of a
  string Praxis builds, so the never-guesses principle is untouched. **`difficulty.py` had
  the same shape and is FIXED too** (2026-08-27): `context_ratio` charged
  `estimate_tokens(plan_text)` alone, while the text reaches the worker INSIDE the
  implementer prompt. It now adds `agent_prompt.fixed_scaffolding_tokens()` - a FLOOR,
  derived from the template on each call so a template edit moves it. Measured: 1344
  tokens, **41% of the whole per-leaf budget on an 8192-token window**, the shipped
  capability default, so the scorer was over-optimistic exactly for the small-window
  workers the flag protects. Score impact 0.943 -> 0.903 at 8192 and 0.952 -> 0.951 at
  131072, both clear of `flag_below` 0.55 and `reject_below` 0.35, so no re-calibration.
- **A harness callback is RETRIED and the handler must be idempotent** (fixed 2026-08-27).
  Both entrypoints re-POST up to `CALLBACK_MAX_ATTEMPTS` on any non-200 including the
  `HTTP 000` curl reports when its `--max-time 10` elapses - and this handler takes
  MINUTES, so a redelivery of a callback the server SUCCEEDED on is the normal case.
  Measured: 5 `task_outcomes` rows for 4 runs, attempts 1,2,2,4,4, so the retry budget was
  silently halved. `claim_agent_run_completion` is an atomic conditional UPDATE keyed on
  `finished_at IS NULL` (never on `status`, a bare `str` the harness chooses) and is the
  FIRST write; a redelivery answers 200 `duplicate_callback`. **`RUN_ID` was dead in
  production** (`grep -rn "RUN_ID" src/` returned zero) so every callback resolved
  `runs[-1]`; it is plumbed now, and without it a redelivery arriving after a retry
  disposes a worker mid-execution.
- **A permanent misconfiguration is not a transient provider error**, and the callback
  path's re-queue was UNCAPPED (fixed 2026-08-27; the reconcile path had
  `PROVIDER_ERROR_RESPAWN_CAP`, this one had nothing - 12 respawns, attempt stays 1). The
  cap is SHARED now. `permanent_worker_config_error` exists but is **deliberately UNWIRED**,
  and the round-6 reason for that ("its premise was REFUTED - the model that refused by
  name at 02:19 served real work at 06:03") **was itself WRONG, corrected 2026-08-27**: the
  endpoint was never serving that model at 06:03 either. See the model-substitution entry
  under "Config and deployment". Its DETECTOR still cannot fire here - it greps the
  container log for a refusal BY NAME, and a substituting endpoint never refuses - so
  wiring it would need the pre-dispatch probe described there, not the log matcher.
  **The classification NAMES ITS EVIDENCE** (2026-09-05): `find_provider_signal` returns
  the signal AND the log line it matched, and both re-queue seats put them in the log
  line, the `worker_provider_error` event (`signal`, `evidence`) and the stored feedback
  (`provider_error_feedback`, action first, original reason last). Round 10's "a bare
  `failed` is read as a provider error" was unverifiable precisely because the old line
  printed the worker's REASON and not the match. The entrypoint's own `WARNING: callback
  attempt N/M failed (HTTP ...)` lines are EXCLUDED from the scan: they report the
  ORCHESTRATOR's answer to the callback, not the model endpoint's, and `HTTP 503` on one
  re-queued a real failure for free. Exact prefix at line start, nothing wider.
  **A QUOTA is a provider error too** (2026-09-05, round 13): agy answered every worker
  in three seconds with "Individual quota reached ... Resets in 1h15m27s" and, because
  the harness reports plain `failed`, eleven calibration rows and two brain-authored
  `split`s were charged to a model that never ran. `quota reached` / `Quota exceeded` /
  `RESOURCE_EXHAUSTED` are signals now. Honouring the reset time is NOT built (the
  respawn cap still ends the leaf in about two minutes, honestly). When a run fails in
  seconds, read `praxis logs <task-id>` before believing the attempt count.
- **The wave verify gate makes the same comparison, or it parks a plan forever, invisibly**
  (2026-08-26): a plan branch red for a SIBLING's contract was called a regression, and
  since `merged_count` cannot advance while the wave is parked the verdict was permanent
  while every leaf stayed a healthy PENDING and the plan read ACTIVE with a null `error`.
  Base RED identically is memoized and NOT parked; a base `error` fails closed and is NOT
  memoized. Its base is `projects.default_branch` and nothing else, since the plan branch IS
  the head here. Surfaced on `plans.error`, deliberately NOT in `plan_reachability` (a parked
  wave is not in its `(opus_plan, task rows)` inputs).
- **The sweeper must never delete a branch carrying merged work, and once it did**
  (2026-08-26): a human approved leaf 1 and `praxis merge` landed it on the plan branch, leaf
  2 spent its attempts, the plan went FAILED, and `sweep_dead_branches` ran a real
  `git push --delete` over it. Recoverable only via `refs/pull/<n>/head`, IRRECOVERABLE on a
  `praxis-local://` backend. `dead_branches` now takes a further veto, `carrying_merged_work`,
  REQUIRED rather than defaulted on both it and the ledger, and a spared branch is reported
  at WARNING. `terminal_with_failures` is the arm that fired, and it is defined by removing
  the integration-PR veto that would have saved the branch.
- **An empty worker diff is a FACT, not a verdict**: entrypoints report `no_changes`; the
  orchestrator decides. Assert on the status carried to the callback, not "empty diff ->
  failed". **The base-branch verify command alone cannot decide it** (fixed 2026-08-26):
  it answers "is this repo healthy", not "was this task's work done", so a healthy repo
  made every empty diff read as already-done and closed a task that built NOTHING as a
  SUCCESS that unblocks dependents. A leaf's DECLARED EDIT LOCATIONS are the
  discriminator; they live only in `plans.opus_plan` (there is no `tasks.files` column,
  and `agent_runs.files_touched` is an integer COUNT of what a worker changed). An absent
  declared path now outranks every verdict the gate can give. A leaf declaring NOTHING
  keeps its previous answer and SAYS the check could not run; undecidable path shapes
  decide nothing. The path check rides the SAME checkout the command runs in, because two
  fetches could observe two states of the branch. **That decision also TRIAGES and RECORDS**
  (2026-08-26): `no_change_outcome` returns `NoChangeDecision(closed, why,
  worker_attributable)` (still unpacks as a 2-tuple for its two out-of-module callers), an
  attributable decline writes a `FailureClass.NO_OUTPUT` outcome row and then triages, and a
  non-attributable one records NOTHING and just fails/retries.
  **And a RED project `verify_cmd` is NOT attributable here** (fixed 2026-08-26, the same
  day, the opposite direction): the worker changed nothing, so the branch verified IS the
  tree it was handed and the redness pre-dates the attempt BY CONSTRUCTION. Charging it
  wrote repository health into the column the capability loop reads as worker capability.
  The leaf's OWN declared `verification` decides instead, on the SAME checkout: passes ->
  the no-op is established and the leaf closes; fails -> attributable, on the DECLARED
  command's output; absent -> fail closed but do NOT charge. **That leaf command must
  DIFFER from the project command or it is no evidence at all**
  (`leaf_validator.discriminating_leaf_command`, the SSoT both seats call): widening what
  counts as a runnable check let the two become byte-identical and silently reopened this.
  `_verify_failure_stands` writes its row with a NULL `failure_class` via `_GATE_UNCOMPARED`
  rather than `VERIFY_FAIL`, because every arm reaching it means the base could not be ASKED.
  **AND A LEAF CHECK CAN BE NON-DISCRIMINATING WITHOUT RESTATING THE PROJECT COMMAND**
  (found live 2026-08-27, plan `8a2f4349` / playground PR #107 - a FALSE SUCCESS, every
  gate green). Attempt 1's work was review-REJECTED and left on the agent branch; the retry
  saw it already there and changed nothing; the base branch carried both declared paths
  (pre-existing files) and passed the leaf's own check `pytest test_guard.py -q` - 22 tests,
  none of them the ones the leaf was told to ADD to that file. The leaf closed `no_changes`,
  the plan reported **COMPLETED with 0 commits**, and the implementation stayed in an open
  rejected PR nothing would ever surface again. `discriminating_leaf_command` cannot see it:
  the command genuinely DIFFERS from the project command and is still non-discriminating,
  because the suite it runs is one the leaf itself was told to extend. Fixed with a fact
  that needs no string analysis - **work on a branch base does not contain is not in the
  repository** (`_work_sits_unmerged_on_the_task_branch`, tri-state, only an explicit
  `base_contains is False` refutes, NOT attributable, and it may only ever refuse to close).
  **A DECLINE REASON IS WORKER-FACING GUIDANCE, NOT A DIAGNOSIS** - the same fix got this
  wrong first: the callback writes the reason to `tasks.review_feedback` and the Bible
  injects that column into the NEXT attempt's prompt, REPLACING the review that rejected
  the work. A sentence about branch topology names no action and displaces the reviewer's
  concrete objections, so the worker has nothing to do and burns the attempt.
  `_unmerged_work_reason` states the ACTION first, names the PR for the human reading the
  same column, and quotes the prior review last, skipping the quote when the stored
  feedback is already one of its own messages (or attempt 3 quotes attempt 2 quoting
  attempt 1). **Grep for what writes `review_feedback` before adding a message to it.**
- **Adaptive triage is reached by WORKER-ATTRIBUTABLE failures only, and the RULE is the
  contract, never a caller list**: a failure may reach `_triage_then_fail` when the worker
  was handed the leaf and its own output is what fell short. Everything else calls
  `_fail_and_maybe_retry` DIRECTLY, because a fault that says nothing about the leaf would
  spend a brain call on noise and triage's worst answer (`human`) is terminal; the
  reviewer-error and unparseable-`pr_url` paths, and provider errors peeled off upstream,
  are the standing exclusions on exactly that ground. Attribution is computed where the
  evidence is in hand (`NoChangeDecision.worker_attributable`, or the route's own
  identity), never by matching the reason text.
  **Derive the routes with the QUERY "what else can fail a task"**
  (`rg '_fail_and_maybe_retry\(|_triage_then_fail\(' src/`, plus the callback router's own
  status arms), never by reading. **This entry has been WRONG TWICE in two days for
  exactly that reason**: it said "exactly TWO callers", missing a self-reported
  `no_changes` failing through the callback router; the correction then enumerated the
  no-change decline at each of its seats and still missed a worker that RAN and reported
  `failed`, which is what both entrypoints report for every non-zero exit and so the
  commonest ending of all. Measured both times: `triage_decision` NULL across every
  attempt, `task_outcomes` empty. A structural rule is a claim about the paths you
  ENUMERATED, not the paths that EXIST. The router supplies facts and the mixin decides, so
  the `attempt >= 2 and not already_triaged` bound exists ONCE; a mutation of it must turn
  EVERY route's tests red, which is the only proof the gate is shared rather than copied.
  Each route names its own `FailureClass` and none may be overloaded: `NO_OUTPUT` means the
  run SUCCEEDED and produced nothing (and is written only once that emptiness is REFUTED),
  `RUN_FAILED` means the run itself ended in failure and nothing reached a review. The
  no-change route passes triage the measured `(0, 0, "")`; the run-failure route passes
  `(None, None, "")`, because nothing counted anything there.
- **A requeue REACTIVATES a `failed` plan, or the recovery is inert** (fixed 2026-08-27):
  dispatch reaches a task through two gates that both key on the PLAN
  (`get_runnable_plans` selects `pending`/`active`; `process_plan_once` returns unless
  ACTIVE), so writing only `tasks.status = pending` answered 200, spent an attempt and
  wedged silently while `terminal_incomplete`'s hint recommended exactly that action. The
  rule lives in `TaskQueue.retry_task`, shared with `force-status`, and acts on `failed`
  ONLY. The sweeper effect is entirely SPARING (`failed -> active` removes the
  `terminal_failed` signal and adds the `live_branches` veto). `plans.error` is NOT
  cleared: it is a one-way column.
- **A plan can be STALLED while it reads ACTIVE with a null `error`**: a PENDING leaf
  behind a terminally FAILED one is unreachable forever, transitively.
  `core/plan_reachability.py` derives it for every surface (`poll_plan`'s `stalled`,
  `PlanResponse.stalled_task_ids` / `.stalled_blocked_by_task_ids`, `praxis plans`, the
  dashboard); recovery is `praxis retry <task-id>` / MCP `retry_task` /
  `POST /api/tasks/{id}/retry`, and only the BLOCKING id is a legal argument (409 on any
  status but `failed`). The ACTIVE status and the null `error` are both deliberate.
- **WAIT, DO NOT POLL, and the wait never blocks on a state only a person can move**
  (2026-09-05, `core/waiting.py`). A poll loop read `GET /api/tasks/{id}` at the top
  level, got `None` for `status` every cycle (the row is nested under `task`), and would
  have waited ten minutes on a worker that finished in 40 s. Now the detail payload
  MIRRORS `status`/`attempt`/`pr_url`/`plan_id` at the top level with `terminal` and
  `waiting_on` (`worker`/`review`/`human`/`nothing`; plans add `planner`) beside them,
  the plan payload carries the same two as REQUIRED fields (a default would have hidden
  the three routes that returned a bare row), and `GET /api/{tasks,plans}/{id}/wait`
  blocks on the event bus. Three rules: the bus is a WAKE-UP, never the truth (not every
  transition publishes, so the row is re-read on every wake and on a 2 s tick, and the
  subscription is taken BEFORE the first read); a HUMAN gate (merge gate, clarification,
  stall, the PROPOSAL gate via `approvals.plan_awaits_approval`) returns at once with
  `changed: false`; the timeout is CAPPED at 90 s, under the MCP client's 120 s, so the
  server always ends a wait. `fingerprint` (status+attempt+pr_url, every leaf for a
  plan) passed back between calls sees a re-dispatch (`pending -> pending`) and a
  transition that landed between two calls. MCP `wait_task`/`wait_plan` name ONE
  `next_action` (`wait_again`, `relay_pr`, `answer_clarification`, `retry`,
  `approve_proposal`, `none`);
  `praxis wait` exits 0 at rest and 2 on timeout. **A PENDING leaf behind a gated or
  terminally failed dependency waits on that person too** (`waiting.task_blockers`,
  served as `blocked_by: {gated, failed}` on the task payload; found on probe 1, where
  `wait_task` on the second leaf said "wait again" while the plan named the gate).
- **The CLI falls back to the nearest `./.env`** for `AUTH_TOKEN`/`PORT`, walking up from
  cwd; `praxis env` says which source won.
- **`praxis init` logic is `run_init(Answers(...))`, not `init()`** (typer wraps the
  command's defaults in `OptionInfo`).
- **GitHub's PR state outranks `gh`'s exit code**: `gh pr merge` can 504 AFTER a
  successful merge, so `merge_pr` re-reads `gh pr view --json state` before failing.
- **A human's REJECT outranks a planning failure already in flight, on BOTH arms**
  (2026-08-28). `_still_activatable` guarded only the SUCCESS arm, so a decomposition that
  came back UNUSABLE wrote FAILED plus an engine-authored reason over the operator's
  REJECTED, and `plans.error` is one-way. Guarding the terminal arm alone is NOT enough:
  a transient failure never reaches `_fail_plan`, it reaches `_charge_planning_attempt`,
  which writes the error and bumps the counter without touching the status.
  `_planning_outcome_still_applies` is shared by both and is checked BEFORE the counter
  moves. `_UNFAILABLE_PLAN_STATUSES` is REJECTED + COMPLETED and is deliberately NOT the
  complement of `_ACTIVATABLE_PLAN_STATUSES`: re-failing a FAILED plan is idempotent.
- **A repository used to be pinned to the base branch its FIRST project row got**
  (2026-08-28). Both `execute_plan` and `dispatch` resolve a repo with
  `ORDER BY rowid LIMIT 1`, so a second project row for the same URL is unreachable while
  the call still overwrites the first row's `model_name` and `harness`. **The half with no
  trade-off is FIXED (2026-08-29): `POST /api/projects` answers 409** naming the existing
  project, its base branch and the remedy, rather than 201 for a row nothing will ever use.
  Match is EXACT string equality, the same comparison the resolvers make; a looser one
  would refuse a URL they treat as a different repo. **`default_branch` is now mutable
  (2026-09-05, owner's call resolved):** `ProjectUpdate` accepts it, and
  `praxis configure --default-branch <branch>` / `PATCH /api/projects/{id}` send it.
  Refused with 422 while the project has any non-terminal plan (`pending` or `active`):
  branches are cut at dispatch and the integration PR's base is read at completion, so a
  mid-flight change would silently retarget running work. The same value is a no-op (no
  plan check, no preflight); a genuinely new branch is preflighted on the remote exactly
  like `create_project` (shared helper `_preflight_branch`), so a branch missing on the
  remote is refused before the row is written.
- **Nothing reported how long a worker had been running, and nothing bounded it**
  (2026-08-29; one ran ~2h against the owner's own hardware unnoticed).
  `core/run_elapsed.py` is the ONE derivation. Its three rules: a naive stamp is UTC
  (SQLite's `CURRENT_TIMESTAMP`) while a zone-bearing one is left alone - BOTH shapes
  appear on one `agent_runs` row; unmeasurable is `None`, NEVER `0.0` (unlike
  `approvals._age_hours`, which feeds a sort key and is right to answer 0.0); open is
  `finished_at IS NULL`, never `status`. Computed on the SERVER at every seat. The
  dashboard seat is `GET /api/plans/{id}/tasks` (a computed `running_for_seconds` over a
  joined `running_since`), NOT `/api/tasks/{id}` - both dashboard surfaces read the plan's
  task list, so enriching only the detail endpoint looks complete and renders nothing. The
  INSTALL-WIDE seat (2026-09-05, no plan/task id needed) is `TaskQueue.get_open_runs` ->
  `GET /api/status` `running`/`running_count`/`running_known` -> `praxis status`, the
  LEDGER's view (open = `finished_at IS NULL`) which can disagree with the Docker-derived
  `active_agents`/`total_agents` on the same response; the CLI prints copyable
  `praxis task <id>` lines below the table.
- **`worker_timeout_minutes` (60, `0` disables) bounds ONE run, and the expiry is NOT
  worker-attributable** - that is the load-bearing decision, not the bound. It means WE
  STOPPED IT, and nothing about an expiry tells a hung harness from a stalled endpoint
  from a large leaf, so it writes NO `task_outcomes` row and reaches NO triage; it goes
  through `_resolve_failed_run` and retries normally. The check sits BEFORE reconcile's
  live-monitor short circuit (a run streaming logs at hour two is exactly one WITH a live
  monitor). An unreadable start stamp never expires a run. The reason is worker-facing
  GUIDANCE (it lands in `review_feedback`, which the Bible injects into the next attempt),
  so it names the ACTION first and disclaims attribution second.
- **A container that exited CLEAN inside the harness's callback-retry window is left
  OPEN** (2026-09-05, `CALLBACK_RETRY_WINDOW_SECONDS` = 90 in `orchestrator_reconcile`).
  The entrypoint retries its callback 5 times at `--max-time 10` with 4+6+8+10 s of
  backoff (78 s worst case) while reconcile's grace was 5 s: when the host stalled for a
  minute, three workers exited 0, reconcile closed all three "without a completion
  callback", their redeliveries were refused as duplicates, and finished work was thrown
  away and re-run as attempt 2. The window is judged from Docker's own `FinishedAt`
  (now on `get_container_status`), never by sleeping in the pass; a non-zero exit, an
  unreadable stamp, or a double that does not say WHEN keep the old disposal.
- **The expiry WAITS `_callback_grace`, re-reads the run, and disposes of nothing once
  `finished_at` is set** (fixed 2026-08-29, found on the bound's FIRST live firing: the
  worker reported `completed` 404ms AFTER the kill). `complete_agent_run` closes a run
  UNCONDITIONALLY, so without the re-read a successful run becomes a fabricated timeout
  with a real PR open and unmentioned. **The ORDER is the whole fix** - stopping first
  kills the worker either way and only changes when Praxis finds out. The race is NOT
  uniformly distributed: a bound SELECTS for workers finishing near it. Both the sleep
  and the `finished_at` check appear VERBATIM in `_reconcile_exited`, so a mutation
  anchor here needs a disambiguating neighbour line.
- **`AgentManager.stop_agent` is SYNCHRONOUS and was being awaited** (fixed 2026-08-29):
  `await None` raised into the surrounding `except`, so every superseded container WAS
  stopped and was then reported as one Docker had refused, with its run row written
  `failed` / "may still be running". The guard could not fail because the test double
  declared `async def stop_agent`. **A double more capable than the real object is where
  the bug lives.**
- **`callback_grace` was a documented key nothing read** (fixed 2026-08-29):
  `Orchestrator.__init__` hardcoded `5.0` - `loop_interval`'s old shape surviving next
  door. Both are passed from `main.py` now. Its guard SURVIVED commenting the kwarg out
  (the searched substring lives on inside the comment), and a companion guard comparing
  the YAML to a constructed `Settings` was tautological, since `Settings` OVERLAYS that
  YAML. Strip comments; compare against the FIELD default.
- **The dashboard's own date math must treat an API timestamp as UTC**: the API serves
  SQLite's naive `created_at` (`"2026-08-27 21:13:33"`), which V8 parses as LOCAL time, so
  every relative age was off by the viewer's UTC offset (measured: "7h ago" for a plan 20
  minutes old, from UTC+7). `toInstant` in `web/app.js` is the normaliser; a zone-bearing
  stamp must be left alone or it shifts twice. Enumerate the sites with
  `new Date\(|Date\.parse\(|timeAgo\(|age_hours|toLocale` over `web/app.js` - the ones
  using the SERVER's `age_hours` were always correct.
- **A stalled plan reads ACTIVE, so every surface needs its own marker.** The dashboard
  swim lane had none and showed it as an ordinary active lane; the stall was only in the
  plan detail. A guard that greps a function body for the marker CANNOT fail if the
  marker's declaration stays behind: pin the EMISSION.
- **`praxis init` prints the MCP block ONCE, so `praxis mcp` exists to re-print it**
  (2026-08-28). `mcp_snippet` had a single caller inside `init`, in the middle of a
  multi-minute mostly-Docker-build output, so an operator whose scrollback rolled had to
  re-run the whole install; an assistant following the README brief hit exactly that.
  `praxis mcp` reuses `cli.init.mcp_snippet` (never a second copy: the env var names are
  the ones `mcp_server.client` reads), works with the orchestrator DOWN, and reports the
  INSTALL ROOT rather than the cwd, since `mcp_snippet` defaults to `Path.cwd()` and that
  default is right for `init` and wrong for a verb runnable from a subdirectory.
- **A sidebar stat, or any figure written only AFTER its fetch, must not ship a numeric
  default** (2026-08-28). All four `#stat-*` elements shipped `0` in `index.html` and are
  assigned only on success, so a rejected token rendered four measurements nobody took
  while the panel below correctly said "not authorized". Same class as `Agents: NaN`. The
  non-numeric placeholder is only safe because nothing parses them back out of the DOM,
  and `tests/test_sidebar_stats_are_not_measured_zeros.py` pins both halves together.
- **`soft_wrap=True` on EVERY pasteable line, and `cli/init.py` had none** (2026-08-28).
  `grep -n soft_wrap src/cli/init.py` returned 0 while `init` is the one command whose
  whole output is meant to be copied. Its MCP block folded mid-path INSIDE a JSON string
  (invalid JSON, corrupted path, and it is the block the README's brief says to paste),
  and its PowerShell export folded after the URL so pasting it set `ORCHESTRATOR_URL` and
  silently dropped the token. **Assert LINE STRUCTURE with `cli_text.strip_ansi`, never
  `plain`** (`plain` collapses whitespace and rejoins the fold), and **assert the fixture
  is long enough to fold as an explicit precondition**: every existing test of this output
  uses `Console(width=200)`, where nothing folds, which is why it shipped.
- **An id belongs on its own line, never in a table column** (rich shrinks and folds
  it; a truncated id 404s). `pending`/`plans`/`tasks` print a copyable
  `praxis <verb> <id>` line below the table; assert contiguity on ONE line at 80
  columns, and test EVERY branch of a conditional copyable line. **The needle must be
  LONGER than the terminal or the guard cannot fail**: a 48-char `praxis task <uuid>`
  prefix stayed green with `_copyable` replaced by a bare `console.print`, because the
  fold lands after it. Assert the WHOLE line under an explicit width precondition.
- **Server-provided text is DATA and rich reads a bare `[` as a style tag**: a cell takes
  `rich.text.Text`, an interpolated or `_copyable`d value takes `rich.markup.escape`
  (`_copyable` must keep markup ON). Both halves are silent in their own way -- `[main]`
  is DELETED, `[/dim]` raises `MarkupError` out of whatever verb was printing. A rich
  markup FIXTURE must start with a lowercase letter (`a-z # / @`); `[High]` renders
  verbatim whatever the code does, so it proves nothing.
- **Typer help text guards**: strip rich's box glyphs BEFORE collapsing whitespace, or a
  wrapped panel border makes the guard inert.
- **The CLI forces UTF-8 on stdout/stderr** (`_force_utf8_stdout`): without it,
  redirected Windows output is cp1252 and `praxis tasks | grep` says "Binary file matches".
- **A doctor hint must name a verb that can DO the job**: `praxis config` is a GROUP, so
  an existence check passes while running it prints help
  (`tests/test_doctor_hints_name_real_verbs.py`).
- **A new surface's QUERY is the seam that goes inert**: test the layer that FETCHES,
  not only the one that decides (`pending` once hid every proposal on a correct
  predicate + renderer).
- **The merge verbs use their own `_MERGE_TIMEOUT`** and report "may still be running",
  never "failed" (one `merge_pr` under 504s outlives the read-only 60s budget).
- **`get_dispatchable_tasks` maps `opus_plan["tasks"]` to rows BY LIST INDEX**: only
  APPEND to the graph; supersede, never delete. The join is positional the whole way
  through since 2026-08-26; it used to re-key the positional pairs into a SLUG dict,
  which made the map non-injective the moment two entries shared a slug: the earlier row
  was orphaned forever, the later one dispatched TWICE in one wave, a dependent leaf ran
  against unbuilt work, and the plan sat ACTIVE indistinguishable from healthy.
  `plan_derive` uniques its slugs now (the decomposer and `leaf_split` already did), and
  a dependency naming a repeated slug waits for EVERY row carrying it.
- **Hand-built LM Studio payloads must state `reasoning_effort` explicitly**
  (`core/thinking.py` is the SSoT): the endpoint default INVERTED between 2026-08-15 and
  2026-08-21, levels are not monotonic, and `json_schema` extraction returns EMPTY when
  the model thinks - `plan_derive` pins `none`.
- **Worker effort is PER-HARNESS and must be stated too** (`core/worker_effort.py`);
  `None` means "no knob", NOT "off". OpenCode's config key is camelCase
  `reasoningEffort`; snake_case is silently ignored.
- **An omitted `harness` must never downgrade a project**: `execute_plan`/`dispatch`
  pass `None` through; only a NEW project takes the registry default.
- **The progress handover reads the REMOTE branch** (never a local clone; `"."` in the
  container is `/app`). Three states stay distinguishable: `[]` "no commits yet",
  `None` "history unavailable", non-empty "resume here" - a test mocking it to `[]` is
  indistinguishable from the bug.
- **`git_ops.commit_and_push` returns `bool`**: False = index already clean, a FACT;
  callers report `unchanged`, never raise.
- **Every route touching a target repo goes through `api/repo_errors.guard_repo_access`**
  (`FileNotFoundError` → 404, else 502 carrying decoded `.stderr`).
- **A submitted spec travels as a repo doc, never in the DB**: `POST /plans` commits it
  under `docs/superpowers/specs/` and stores `spec_path`; `plan_and_activate` fails
  closed if it cannot read it back. `tests/test_submit_spec_seam.py` covers the seam
  end-to-end; keep it that way.
- **Agent runs non-root** in `/home/agent/workspace`, git auth via `GH_TOKEN`.
- **Both entrypoints trap TERM and stop the agent promptly** (2026-09-05, round 13):
  `praxis stop` used to take 33 s because bash as PID 1 ignores an unhandled TERM and
  defers traps while a foreground command runs. The agent now runs in the background under
  job control (`run_agent`), `on_term` kills its process group and exits 143 (a `failed`
  callback). Rebuild the agent images after any entrypoint edit.
- **A planner that answers in prose is PERMANENTLY failed, never retried**: no JSON
  anywhere in the reply means a refusal/question/permission request, structural not
  keyword-matched; malformed JSON is the transient bucket and retries to a bound.
- **An unknown context window SKIPS the budget gate and says so**: `core/context_window`
  never falls back to a guessed number; harness identity is not the correctness
  mechanism, only whether a probe answered.
- **`opus_state` has ONE writer per transition and the queue is a ledger nobody drains**:
  `OpusStatus.RESUMING` is written by nothing; work resumes because the loop re-enters.
  `claude`'s throttle notice is on STDOUT, not stderr - check both streams.
- **An unreachable repo is quarantined with backoff, not retried every tick**: consecutive
  `git ls-remote` failures back off to a ceiling and log ONCE; a success resets the count.
- **`scrub_context` does two jobs and only redaction belongs at intake**: length capping
  needs the resolved worker window, so it belongs solely to `worker_bible.build_bible`;
  an intake cap taken without the probe truncates permanently before the real budget runs.

- **The DECOMPOSE PROMPT and the F3 VALIDATOR are one contract and drifted apart**: the
  prompt ordered a VERBATIM copy of the plan while the template block injected forty lines
  earlier HARD-required section labels the user never wrote, so a brain that obeyed failed
  every leaf and every plan paid a wasted brain call. Exactly ONE shape satisfies both
  (the labels, with the source lines under `Steps`). Edit either side and re-read the
  other. `_section_for_task` returns `""` when it cannot find a leaf's section, never the
  whole document: a guess there is a guaranteed violation.
- **`plan_text_verbatim` almost never RUNS, and the decomposer rewrote an acceptance bar
  under cover of it** (2026-08-27). `_section_for_task` resolves a leaf's section by
  looking for the DECOMPOSER-AUTHORED `title` inside a plan heading, so the check that
  grades drift is disabled BY drifting, and titles do not match headings even on a
  faithful decomposition. Measured on production artefacts: 0 of 3 sections resolved on a
  plan whose leaf deleted the contract test file and specified sixteen replacement tests
  of its own invention, 1 of 3 on a faithful one; `validation_warnings` carried only
  `file_overlap` on both. Where the rule DID run it scored 0.99/0.93 against a 0.70
  threshold, so the METRIC is sound and SECTION RESOLUTION is the defect. **Two fixes are
  measured and REFUTED in `docs/gotchas.md` - a `files`-subset rule (inert for the same
  reason) and content-based section resolution (false-fires on a faithful
  one-section-to-N-leaves split, 0.31 against 0.25).** Read them before proposing either.
  The plan the leaves are graded against is in `plans.pending_input`, as a JSON envelope
  under `"plan"`. **FIXED** by replacing the silent `continue` with a DOCUMENT-backed
  fallback (`_plan_backed`): are the leaf's OWN lines in the plan, whitespace-collapsed
  because a decomposer UNWRAPS the plan's hard-wrapped bullets (a line-exact compare
  scores 0.0 on faithful leaves - it was tried). Gated on the plan HAVING headings, or it
  fires on every three-word brief, which an existing fixture caught. Section resolution is
  unchanged and still preferred. Validated on the real artefacts, kept as
  `tests/fixtures/decompose/plan_text_backing_cases.json`: 3/3 fabricated, 0/3 faithful.
  **THAT SEPARATION WAS AN ARTEFACT OF THOSE TWO PLANS, and the rule MUST NOT become
  HARD** (measured the same day over 16 real decompositions / 34 leaves, the corpus kept
  as `plan_text_backing_corpus.json`): it fires on **19 of the 31 FAITHFUL leaves**, and
  the fabricated scores 0.04/0.20/0.12 sit INSIDE the faithful lower third - ten faithful
  leaves score 0.00 - so no threshold separates. The cause is MARKUP, not judgment: a
  decomposer strips a plan bullet's backticks for a plain-text worker prompt and a
  substring test then misses a line-for-line copy (probe `f91dc84e`: three leaves that ARE
  the plan's bullets, all three warned). Elaborating a requirement into floor-model steps
  scores near zero too, and that is the flagship mechanism working. **Section resolution
  succeeded on 1 of 32 leaves**, so the fallback IS the rule. Stripping markup was measured
  (17/29 -> 9/29 false, 3/3 -> 1/3 fabricated, the survivor being the real defect leaf) and
  deliberately NOT shipped: better, still not separation, and tuning on one fabricating
  plan repeats the error. The rule stays SOFT, its message now SAYS it is a weak hint, and
  `test_verbatim_rule_does_not_separate_on_the_wider_corpus` pins the count so a tuner must
  re-measure on the corpus. The unshipped candidate (leaf `files` graded against the plan's
  `Files:` lines, DOCUMENT-scoped) looked perfect at 1 true / 0 false in 11 leaves and then
  false-fired on the very next sample; `docs/gotchas.md` carries that and the fact that the
  round-7 PROMPT fix HELD when the fabricating plan was replayed verbatim.
- **The route that reaches triage for a BIG leaf argues against `split`** (2026-08-27).
  `07583d2`'s verify-gate arm is sound and reachable, but the final leaf of a dependent
  chain reaches the NO-CHANGE route instead: its declared path exists precisely because an
  earlier leaf created it, so the one discriminator independent of the project command is
  unavailable. Reaching triage at all needs a leaf whose declared path is ABSENT from its
  base, and then the evidence pack is `files_touched=0` with an empty diff, which argues
  for `escalate` - observed live, and CORRECT, since `split` is inferred from PARTIAL
  progress. **Do not tune the triage prompt to prefer `split` on zero output.**
- **The ADAPTIVE SPLIT is governed too** (2026-08-26): `validate_leaves` had ONE call
  site, so every child a split produced bypassed every F3 rule while the standard makes
  adaptive splitting policy #1. `validate_split_children` shares the same rule
  implementations; three whole-graph rules are deliberately skipped and `dep_cycle` is
  applied over the SIBLING set, because two children pointing at each other survive
  rewiring and neither ever becomes dispatchable. Children are SCORED but never gated: a
  rejection there could only re-run the parent that already failed twice.
- **`duplicate_id` is a HARD rule that runs FIRST and ALONE** in both `validate_leaves`
  and `validate_split_children`, returning immediately when it fires: every rule below it
  is id-keyed, and on a repeated id `_detect_cycles` turns a sibling edge into a self edge
  and reports a cycle nobody wrote. Nothing upstream enforced it - `LeafTask.id` is a bare
  `str` and neither prompt asks for uniqueness - and a repeat collapsed FOUR id-keyed maps
  at once (sibling rewiring, cycle detection, the capability-event slug, the per-child
  score), silently, onto whichever leaf came LAST. The decompose path does NOT collapse the
  same way: `normalize_slugs` re-keys every id to its own uniquified slug, so the fix there
  is inside `normalize_slugs` (an id carried by two tasks is DELETED from the map, so the
  dep stays raw and `dangling_dep` rejects it into the informed re-ask).
- **A difficulty YAML typo must degrade the score, never wedge decomposition**, and that
  promise was false at THREE seats that each re-derived the numbers with a bare `float()`.
  `difficulty.resolve_weights` / `resolve_bias` are the SSoT; an unusable value keeps its
  grounded default (never 0.0, which silently deletes a sign) and non-finite is rejected,
  because a NaN weight makes every comparison False and the gate stops flagging while
  reading as though it ran.

**Contracts that break fixtures**

- **`LEAF_SCHEMA_VERSION` is 2**: any new `LeafTask` field changes `model_dump()` and
  breaks `tests/fixtures/decompose/expected_leaf_graph.json`; regenerate in the same commit.
- **`tests/fixtures/decompose/plan_text_backing_cases.json` holds TWO REAL decompositions**
  - one that fabricated an acceptance suite, one faithful, same decomposer and same day -
  extracted programmatically from `plans.pending_input` and `plans.opus_plan`, never
  retyped. **Two plans is not an evidence base and this pair MISLED once already** - it
  separates perfectly and the rule does not generalise. `plan_text_backing_corpus.json`
  beside it is the wider one (15 real decompositions, 32 leaves, six of them plans authored
  to vary the plan SHAPE and run live through `execute_plan`), each leaf labelled by
  reading its `Files`/`Acceptance`/`Steps` against its plan. **Tune or re-severity any
  decomposition-grading rule against the CORPUS, never the pair.** Both are extracted
  programmatically; do not hand-edit either, re-extract.
- **The status vocabulary is frozen in `core/status_vocab.py`**: add a value to the enum
  AND its exhaustive `test_schemas` assertion together.
- **`core/leaf_templates.py` is the single source of per-`LeafType` section
  requirements**; the F3 validator enforces them at test time.

## Documentation

- **Architecture & components:** `docs/architecture.md`
- **Workflow & orchestration cycle:** `docs/workflow.md`
- **Deployment, Docker & API reference:** `docs/deployment.md`
- **Decomposition standard (cited contract):** `docs/decomposition-standard.md`
- **Configuration surface (seats, presets, arrangements):** `docs/configurations.md`
- **Gotchas (full narrative):** `docs/gotchas.md`
- **MCP setup + worker-prompt design rules:** `docs/mcp.md`; the canonical brain-facing
  guide is the MCP resource `src/mcp_server/resources/orchestration_guide.md`
- **Design spec:** `docs/superpowers/specs/2026-06-01-ai-agent-orchestrator-design.md`
- **Capability-engine roadmap (canonical, 2026-07-11):**
  `docs/superpowers/specs/2026-07-11-capability-engine-roadmap.md` (F1-F15, S1-S11);
  next up = Plan 3 `outcome-recording`
- **Implementation plans:** `docs/superpowers/plans/` (sequential)
- **Auto-delegate: review-scope DONE (2026-08-24), micro-edit lane DONE (2026-08-25).**
  `specs/2026-08-21-micro-edit-lane.md` carries four corrections against the code at its
  top, written before any of it was built; they override the body where they conflict.
  Not built, and deliberately: the engine-side difficulty hint that would give the brain
  a second opinion on the rubric, and widening the lane beyond auto-delegate mode.
- **Implemented + merged (2026-06-29 epic, e2e-verified 2026-07-01):** worker context
  continuity, capability-aware plan execution, MCP orchestration guide (specs + plans in
  `docs/superpowers/`). Delivers the Static Bible, git-spine progress handover,
  pre-flight token budgeting, and the `execute_plan` entry point (REST + MCP).
- **Testing & debugging:** `CLAUDE.local.md`

## Coding Standards

- Python 3.11+, PEP 8, type annotations on all function signatures
- Line length: 88 (ruff default)
- Use `X | Y` union syntax, built-in generics (`list[str]`, not `List[str]`)
- `logging` module only - never `print()` in production
- Google-style docstrings
- Pydantic for API boundaries, dataclasses for internal DTOs
- pytest with 80%+ coverage, `pytest-asyncio` with `asyncio_mode = "auto"`
- Catch specific exceptions, use `raise ... from` for chaining

## GitHub

Repository: https://github.com/adiatmaja/praxis.git
