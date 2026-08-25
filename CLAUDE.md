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
mode is the companion feature (still beta: run #13 live-verified the review-scope
fix through MCP `dispatch_task` AND found five defects on the mode's own path, so
the criterion is now a walkthrough that finds NO new defect in it), presented
after the flagship, never beside it.
Capability-aware task decomposition is the flagship MECHANISM (unique in the landscape);
role separation is the supporting architecture; cost is a consequence. **The exact public
copy - README headline ("Govern any coding harness from inside the one you already
use."), GitHub About, README opener - is frozen in `docs/positioning.md` ("Canonical
copy (2026-08-24)"): copy it verbatim, never re-derive it.** SSoT: `docs/positioning.md`
("The framing"). Flagship mechanism under development: the **Capability Calibration
Loop** - `docs/superpowers/specs/2026-07-11-capability-engine-roadmap.md` (F1-F15, S1-S11).

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
- **Execution:** `task_queue`, `agent_manager`, `agent_prompt`, `worker_bible`, `harnesses`,
  `preflight`, `session_resume`, `progress_handover`, `clarification_states`,
  `token_budget`, `context_window` (resolves a worker's context window, or says unknown),
  `micro_edit`
- **Git & platform:** `git_ops`, `git_backend` (GitHub / local), `github_credentials`,
  `repo_url_policy`, `merge_policy`, `branch_sweeper`, `diff_guard`, `diff_stats`,
  `blast_radius` (repo-wide reach of the identifiers a diff changes, for the review prompt)
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

# Run - containerized (RECOMMENDED: survives terminal exit, restart: unless-stopped)
docker compose -f docker-compose.yml -f docker-compose.local.yml up --build -d  # dev, hot-reload
docker compose up --build                                                         # production
docker compose --profile hosted up --build                                        # with Caddy

# Run - bare uvicorn (quick one-off only; process dies with the terminal and orphans in-flight tasks)
uv run uvicorn orchestrator.main:app --host 127.0.0.1 --port 8080

# Tests. Run the NARROWEST selection that covers the change; the full suite is
# ~4.5 minutes and 2880 tests, and running it after every edit is the single
# most expensive habit in this repo.
uv run pytest tests/test_<the_file_you_touched>.py -q
uv run pytest -q -k "<subject>"

# Full suite: only when it is actually needed. That means before a commit that
# lands, after a change to a shared seam (database.py, task_queue.py, schemas.py,
# a mixin), or when a narrow run comes back green on a change you do not fully
# trust. Not after every edit, and never twice for the same tree.
uv run pytest --cov=orchestrator --cov-report=term-missing -v

# Lint & format
uv run ruff format src/ tests/
uv run ruff check --fix src/ tests/

# Type check
uv run mypy src/orchestrator/ --ignore-missing-imports
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
  findings, so any NEW hit is a real signal; keep the skip list minimal.
- **Dependabot**: weekly pip/actions/docker, grouped by risk tier (runtime deps ungrouped
  so a breaking prod bump is isolated).

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

All eight `TaskStatus` values appear above; a surface that lists fewer teaches a caller to
poll for a state that will never arrive. `NO_CHANGES` and `SUPERSEDED` are terminal,
neither success nor failure; both are in `SATISFIED_STATUSES` (`core/status_vocab.py`),
which unblocks dependents and lets the plan complete. `NEEDS_CLARIFICATION` is the third
gate: nothing advances it but a human answering.

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
  request" (two-tier mode and every pre-migration row). Schema version is 10;
  `tests/test_migrations.py` pins it.
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

## Auto-Delegate Mode (daily-dev)

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
  `_env_file=None`, and clear ambient env or CI's value wins.
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
  `docker compose --profile agents build` - a bare `docker build` leaves the label EMPTY.
- **A local `repo_url` is validated in one namespace and mounted in another**: preflight
  checks it with `Path.exists()` INSIDE the orchestrator container, but the daemon resolves
  the same string as a bind-mount source on the HOST. `LOCAL_REPOS_PATH` (+ the
  `LOCAL_REPOS_HOST_PATH` escape hatch) bridges it; apply with `up -d`, never `restart`.
- **A doctor probe must not mutate what it diagnoses**: the agy credentials probe mounts
  the real volume read-only and layers a tmpfs over the writable path, so the kernel
  guarantees it cannot silently seed the "no credentials" state it is checking for.
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
- **A key in `.env` that the settings file also names WINS and says so** (one `INFO`
  line naming the winner, once per key per process).
- **An unrecognised key in `.env` is IGNORED, not rejected**: a typo in a real key is
  silent and `env_drift` cannot see keys that live only in `.env`. `AUTH_TOKEN` is the
  required exception.

**The loop**

- **Per-task review scoping DEPENDS on one worker per branch at a time, and the loop
  enforces it** (one task per wave in single-branch mode, held while any task is
  `in_progress`/`reviewing`). A task is reviewed on `review_base_sha..head`; two workers
  interleaving commits widen both ranges silently. A re-dispatch KEEPS its recorded sha;
  only a vanished branch gets a fresh one; NULL means "review the whole PR".
- **Merge is gated by default** (`core/merge_policy.py`); protected branches never
  auto-merge at all.
- **A blank `verify_cmd` is "not configured", never a pass** (blank shell exits 0).
  `core/verify_gate.normalize_verify_cmd` is the SSoT at all four read sites; the API
  rejects it 422; `run_verify` raises rather than shell it.
- **`loop_interval` reaches `run_loop`; shipped default is 5**; non-positive is floored,
  never honoured.
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
  (`core/repo_survey.py`): no readable repo means NO proposal.
- **A plan with no commits has nothing to integrate**: decided by
  `_nothing_to_integrate_reason` on a positive check, which settles TWO facts. Two known
  equal SHAs (every task a no-op), and an ABSENT plan branch (single-branch mode, where
  merging the task PRs IS the integration and deletes the branch). `remote_head_sha`
  returns None for an absent branch and RAISES when it cannot ask, so None is an ANSWER;
  only the exception falls through to the creation attempt.
- **A plan reaches the gate TWICE**: each task onto the plan branch, then the plan's own
  integration PR. The URL lives on `plans.integration_pr_url`; `integration_merged_at`
  takes it back out of `pending`.
- **An empty worker diff is a FACT, not a verdict**: entrypoints report `no_changes`; the
  orchestrator decides by verifying the base branch. Assert on the status carried to the
  callback, not "empty diff -> failed".
- **The CLI falls back to the nearest `./.env`** for `AUTH_TOKEN`/`PORT`, walking up from
  cwd; `praxis env` says which source won.
- **`praxis init` logic is `run_init(Answers(...))`, not `init()`** (typer wraps the
  command's defaults in `OptionInfo`).
- **GitHub's PR state outranks `gh`'s exit code**: `gh pr merge` can 504 AFTER a
  successful merge, so `merge_pr` re-reads `gh pr view --json state` before failing.
- **An id belongs on its own line, never in a table column** (rich shrinks and folds
  it; a truncated id 404s). `pending`/`plans`/`tasks` print a copyable
  `praxis <verb> <id>` line below the table; assert contiguity on ONE line at 80
  columns, and test EVERY branch of a conditional copyable line.
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
  APPEND to the graph; supersede, never delete.
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

**Contracts that break fixtures**

- **`LEAF_SCHEMA_VERSION` is 2**: any new `LeafTask` field changes `model_dump()` and
  breaks `tests/fixtures/decompose/expected_leaf_graph.json`; regenerate in the same commit.
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
