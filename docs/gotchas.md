# Gotchas — Praxis

Full narrative for the non-obvious traps in Praxis. `CLAUDE.md` carries a condensed
one-line index of these; this file is the detailed reference. Add new gotchas here and
keep the CLAUDE.md index in sync.

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
  `list_agent_containers` queries the `praxis-agent-` prefix used by all harness containers.
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
- **Harness agent images are standalone — rebuild after ANY `entrypoint.sh` change** —
  the agent images (`opencode-agent:latest` default, `agy-agent:latest` experimental)
  are not in docker-compose, so a stale image silently runs old
  entrypoint logic while the source looks current. This bit us live: a pre-callback-token
  image sent an **empty** `X-Praxis-Callback-Token`, so every callback 401'd and tasks
  never advanced past implement (only reconcile → failed). Rebuild fixes it, e.g.:
  `docker build -t opencode-agent:latest -f docker/opencode-agent/Dockerfile docker/opencode-agent/`.
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
- **Plan branch race condition** — Multiple agents dispatched in parallel may try to
  create the same `plan/` base branch. The entrypoint handles this: if push fails
  (branch already exists), it fetches from remote instead
- **Approve sets plan to ACTIVE immediately** — If the orchestration loop hasn't called
  Opus to break the spec into tasks yet, an ACTIVE plan with no `opus_plan` will trigger
  `plan_and_activate` on the next loop iteration
- **Harness images are standalone** — build each directly, none are in
  docker-compose:
  `docker build -t opencode-agent:latest -f docker/opencode-agent/Dockerfile docker/opencode-agent/`
  (or the equivalent for `agy-agent`).
- **OpenCode and agy don't auto-commit** — their entrypoints run `git add -A && git commit`
  after the agent. A run that produces no changes is marked `failed`.
- **OpenCode config needs `limit.output`, not just `limit.context`** — when the
  orchestrator detects a model's context window (`MODEL_CONTEXT_LIMIT`), the OpenCode
  entrypoint writes a `limit` block; OpenCode's schema requires BOTH `context` and
  `output`, so omitting `output` fails validation ("Missing key ...limit.output") and
  the container exits before implementing. The detect-context-limit feature exposed this.
- **The Static Bible must NOT land in the PR** — OpenCode injects the Bible into
  `AGENTS.md` (compaction-proof slot), so the entrypoint strips the
  `<!-- praxis:bible:start -->...:end -->` block before `git add -A` (and deletes
  `AGENTS.md` if it was untracked and now empty). Without this the Bible leaks into every
  OpenCode PR. agy prepends the Bible into the effective `-p` prompt (never written to a
  committed file).
- **PR body uses `TASK_SUMMARY`, not a slice of `TASK_PROMPT`** — `TASK_PROMPT` is the
  fully-wrapped prompt that starts with a generic autonomous-loop preamble, so
  `${TASK_PROMPT:0:500}` showed only boilerplate. Both entrypoints now render the
  body from `TASK_SUMMARY` (task title + description), set by `spawn_agent(task_summary=)`.
- **Generic MODEL env var** — both harness entrypoints consume `MODEL` (raw model
  name); OpenCode uses an `lmstudio/` config provider, while agy passes the Gemini
  model string verbatim to `--model`.
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
- **Blocked workers ask, they don't guess** - every harness entrypoint parses the
  FINAL REPORT; a `Status: BLOCKED`/`NEEDS_CONTEXT` sends the `Concerns:` text as
  a `question` in the agent-done callback and opens NO PR. The task parks at
  `NEEDS_CLARIFICATION` (does NOT burn a retry). The loop asks the brain
  (`answer_clarification`, Sonnet/medium) to answer from task+plan_text; a
  confident answer (>= project `confidence_threshold`) re-dispatches with the Q&A
  injected via `progress_note` (-> Static Bible), otherwise the task parks
  `awaiting_human` (SSE `task_needs_clarification`, `POST /api/tasks/{id}/clarify`,
  MCP `poll_task` -> `awaiting_clarification`). Both harnesses (opencode, agy)
  parse the FINAL REPORT and send `needs_clarification` when the report ends
  with `Status: BLOCKED` or `Status: NEEDS_CONTEXT`.
- **Remote preflight is shared** — `core/preflight.py` runs cheap, read-only remote
  checks before every container spawn. Non-GitHub URL, auth failure, missing branch
  or file return 422. Unreachable remote returns 502. Base-SHA mismatch returns 409.
  Without a configured credential, checks are skipped with a warning so local-only
   experimentation still works.
- **F2 decomposition constraints are hard, not advisory** — the capability profile's
  numeric limits (`max_files_touched`, `max_loc_delta`, `max_dep_depth`, etc.) are
  injected into the decompose prompt as a `HARD CONSTRAINTS` block, one line per limit,
  stating that violating leaves will be rejected automatically. The brain is expected
  to comply; prose guidance alone is not enforcement — F3 enforces it. Budget
  consistency uses the same `reserve_fraction = 0.6` as `worker_bible`/`fit_sections`,
  replacing the independent `_LEAF_BUDGET_FRACTION = 0.4` that existed before.
- **F3 leaf validator is deterministic and fail-closed** — `core/leaf_validator.py`
  runs after `_normalize_slugs` in `decompose_plan`. It checks: DAG + depth limits,
  no dangling `depends_on` slugs, file/LOC limits, verbatim `plan_text` (≥70% fuzzy
  match to source plan), non-trivial `verification` (>40 chars with runnable signal),
  cross-cutting file overlap, and escalate-type mismatch. On hard rejection the brain
  is re-invoked with specific violations (≤2 informed rounds), then the plan is
  rejected entirely — never dispatching an invalid graph. Warnings (vague phrases,
  oversized checklist, bare-gerund titles) trigger ≤1 re-decompose round.
- **F15 supply-chain gates block auto-merge** — `core/diff_guard.py` checks for new
  dependencies in `pyproject.toml`/`package.json`/lockfiles and runs a gitleaks-style
  secret regex over the PR diff. Any hit forces the human gate regardless of review
  verdict. A local model prompted with repo context is a supply-chain surface;
  "worker added a dependency" must never auto-merge.
- **Plan-branch verify gates FETCH the branch and FAIL CLOSED** —
  `_verify_plan_branch` (used by both `DispatchMixin._wave_verify_gate` and
  `ReviewMixin.on_plan_completed`) clones the repo then calls
  `git_ops.checkout_branch`, which `git fetch origin <branch>` and then
  `git checkout -B <branch> FETCH_HEAD`. This matters: a plain
  `git fetch origin <branch>` only advances `FETCH_HEAD` — it creates no local or
  `refs/remotes/origin/<branch>` ref — so the old `git checkout <branch>` failed with
  exit 1 (`pathspec ... did not match`). That error was caught and the gate returned
  status `error`, which both callers used to treat as pass-through, so the whole-plan
  verify backstop was SILENTLY SKIPPED on every plan (GitHub CI was the only real
  aggregate gate). Now an `error` status is treated like `failed` (the wave is parked /
  `plan_verify_failed` is published), but an `error` is NOT memoized in
  `_wave_verify_state`, so a transient clone/network fault is retried on the next loop
  tick; only `skipped` (no `verify_cmd`, no plan branch, or no credential) passes through.
  Set the project `verify_cmd` mypy scope to match CI (`mypy src/`, not
  `mypy src/orchestrator/`) so the loop gates cover the same surface CI does — a narrower
  scope lets `src/mcp_server` / `src/cli` type errors slip past the worker and whole-plan
  gates and fail only on CI.
- **Auto-delegate mode: global toggle, global default worker, single branch, sweeper** —
  `auto_delegate.enabled` (source of truth in `settings_overrides`) is read via
  `EffectiveSettings.auto_delegate_enabled()` and toggled with `praxis mode on|off` /
  `PUT /api/settings/auto-delegate` / MCP `get_mode`. When ON, the brain plans and reviews
  only and delegates every implementation task to the global default worker,
  `default_worker_harness` / `default_worker_model` in `config/praxis.yaml` (reference:
  `agy` / `Gemini 3.6 Flash (High)`; the product default outside the mode stays
  `opencode`). A project registered without a `model_name` falls back to that worker.
  `dispatch_pending_tasks` then reuses one caller-named work branch and threads
  `single_branch=True` → `SINGLE_BRANCH=1` into the container, so both harness entrypoints
  REUSE the existing remote `BRANCH` with a non-force push instead of cutting a fresh
  `agent/{slug}` (changing that behavior needs an agent IMAGE REBUILD). Dead work branches
  (no open PR, no live run, never protected) are reclaimed by
  `core/branch_sweeper.dead_branches` via `ReconcileMixin.sweep_dead_branches` on the
  reconcile loop, which swallows its own errors so a sweep failure never wedges the loop.
  Mode is sequential in v1 (one delegate in flight at a time).
- **`config/praxis.yaml` is MOUNTED, not baked** — both compose files bind-mount
  `./config` read-only at `/app/config` and set `PRAXIS_CONFIG_PATH` to point at
  it, so a YAML edit takes effect on `docker compose restart orchestrator` and
  never needs an image rebuild. This replaced the reverse behavior, which bit us
  live on 2026-07-27 when a `default_worker_*` change silently kept serving the
  baked-in value. `core/settings_file.config_file_path()` is the ONLY place the
  path is decided; a hardcoded `"config/praxis.yaml"` literal anywhere else
  reintroduces the bug, and `tests/test_config_path.py` greps for exactly that.
  The dev file needs only the mount: `PRAXIS_CONFIG_PATH` reaches it through the
  compose environment merge from the base file. `PRAXIS_CONFIG_PATH` is also the
  one `PRAXIS_*` var `load_yaml_settings` does NOT fold into the settings dict,
  since it is a pointer to the file rather than a setting inside it.
  Agent-image entrypoint changes still require a rebuild.
- **Session resume replays only across an answered clarification** — `core/session_resume.
  resolve_resume_session` returns a session id to replay only when three conditions all hold:
  a `worker_session_id` is stored on the task, `worker_session_harness` matches the harness
  about to be spawned (a project's harness can change between dispatches, and an agy
  conversation id means nothing to OpenCode), and `clarification_state` is `answered_by_brain`
  or `resolved` (the frozen `RESUMABLE_CLARIFICATION_STATES` set). A plain failure retry
  (`TaskQueue.retry_task`, invoked from `api/internal.py` when a task fails and retries
  remain) never sets `clarification_state` to either value, so the third condition alone
  excludes it. More fundamentally, a failure retry's branch is force-pushed and rebuilt from
  base by the entrypoint: a deliberate clean re-implementation. Resuming a worker's memory
  there would have it confidently reference edits that no longer exist on the tree it is
  handed, which is worse than a cold start.
- **`WORKER_SESSION_ID` means BOTH "resume the conversation" and "reuse the remote branch"** —
  the two concerns are one gate, not two independent flags. `AgentManager.build_spawn_env`
  sets the var only when replay is eligible, and both `opencode-agent/entrypoint.sh` and
  `agy-agent/entrypoint.sh` reuse `origin/${BRANCH}` (instead of cutting a fresh branch from
  `BASE_BRANCH`) whenever EITHER `SINGLE_BRANCH=1` or `WORKER_SESSION_ID` is set, pushing
  non-force for the same reason single-branch mode does. Restoring a worker's memory without
  restoring the tree it refers to is the exact failure mode this design exists to prevent, so
  the two behaviors are wired to the same variable on purpose.
- **`Status: BLOCKED` / `NEEDS_CONTEXT` is now a checkpoint, not a discard** — before sending
  the `needs_clarification` callback, both entrypoints stage, commit
  (`wip: checkpoint before clarification (${BRANCH})`), and push the working tree to `BRANCH`,
  still opening no PR (the clarification contract toward the orchestrator is unchanged). A
  clean tree at BLOCKED is normal, not an error: the checkpoint steps are skipped and the
  callback proceeds as before. The invariant that makes replay safe: a session id is only ever
  reported once its checkpoint is confirmed on the remote. Every git step in the checkpoint
  block is gated behind an `if` (never a bare statement), so a failure there trips a local
  `checkpoint_ok` flag instead of `set -e`, which would otherwise skip `send_callback`
  entirely; when `checkpoint_ok` is not 1, `CAPTURED_SESSION_ID` is blanked before the callback
  is sent. A failed push therefore silently forces the next turn to start cold and rebuild
  from base, which is intended degradation, not a bug: resume is an optimization and must
  never be allowed to fail a task.
- **Capture is asymmetric between the two harnesses on purpose** — OpenCode's `opencode run`
  invocation and its existing `Status:` grep are left byte-for-byte untouched; after the run
  returns, the entrypoint separately calls `opencode session list --format json` and pipes it
  through `extract_session.py`, which picks the newest entry by `time.created`. agy has no
  session-list equivalent, so its invocation itself switches to
  `--output-format json`, and its `extract_session.py` splits the envelope: the conversation id
  is printed on the first line, the response body on the rest, which the entrypoint feeds back
  into the SAME `Status:` grep used before this feature existed.
- **The OpenCode session volume is scoped PER TASK, and must stay that way:**
  `OPENCODE_SESSIONS_VOLUME` is only the base name; the mounted volume is
  `<base>-<sanitized-task-id>`, built by `_opencode_session_volume_name` in
  `core/agent_manager.py`. Deterministic per task so a re-dispatch finds its own session,
  unique per task so nothing else can see it. Do not "simplify" this back to one shared
  volume. A dispatch wave runs up to `AgentManager._max_agent_concurrency` OpenCode containers
  at once, all mounting the same path; since capture picks the NEWEST session in the store,
  a container could read a concurrently-running sibling's session and report another task's
  conversation id against its own task id. That is cross-task memory bleed on the next resume,
  not a degradation to a cold start, and no test catches it because the race needs two live
  containers. The per-task name removes it by construction rather than by heuristic.
- **The agy JSON envelope shape is UNVERIFIED.** No `agy-agent:latest` image and no
  `praxis-gemini-creds` volume were available while this was built, so `--output-format json`
  and `--conversation <id>` are unconfirmed against a real agy v1.1.2 build. `conversation_id`
  is a single guessed key with no fallback; the response-body key is a genuine multi-candidate
  guess (`response`, `text`, `output`, `content`, `message`, tried in that order in
  `docker/agy-agent/extract_session.py`). Malformed or unrecognized JSON makes the extractor
  exit 1, and the entrypoint falls back to treating the raw agy output as the transcript
  exactly as it did before this feature, so a wrong guess degrades to today's behavior rather
  than breaking the task. But the agy resume happy path (an id captured, stored, and
  successfully replayed) has never been exercised against real output; it needs a live
  dogfood run before anyone should rely on it. Both extractors are baked into their images at
  `/usr/local/bin/extract_session.py`, so a change here needs an agent IMAGE REBUILD like any
  other entrypoint edit (see the standalone-images gotcha above).
- **Neither session store is pruned in v1:** `TaskQueue.mark_merged`, `TaskQueue.fail_task`
  and `TaskQueue.retry_task` clear `worker_session_id`/`worker_session_harness` in the same
  UPDATE as the status change, and `api/tasks.py::stop_task` clears it explicitly because it
  sets FAILED directly rather than through `fail_task`. So a stale id is never replayed. But
  nothing reclaims disk: OpenCode allocates **one volume per task** (`<base>-<task-id>`, see
  the per-task scoping note below) and the agy `.gemini` conversation store grows in place.
  Per-task volumes make this growth worse than a single shared volume would, which was the
  accepted price of removing a cross-task session-bleed race. This is deliberate:
  the orchestrator does not mount either volume, so it cannot reach them from the reconcile
  loop without spawning a throwaway container, which the design spec judged to be more
  machinery than the problem currently justifies.
- **Leaf templates are enforced, not suggested**: `core/leaf_templates.py` is the
  single source of the per-`LeafType` `plan_text` section requirements, read by
  BOTH the decompose prompt (`core/plan_review.build_review_prompt`) and the F3
  validator (`core/leaf_validator._check_leaf_template`, a HARD rule). Adding a
  `LeafType` value without adding its entry to `REQUIRED_SECTIONS` raises a
  `KeyError` in `missing_sections`; the golden test
  `test_every_leaf_type_has_a_section_tuple` catches that at test time. Section
  matching is line-anchored on purpose: without `^`, the word "goal" appearing
  inside prose satisfies the Goal requirement and the rule becomes vacuous.
- **The context pack fits greedily by priority, not strictly left-to-right**:
  `core/worker_bible.build_bible` orders sections by the fixed ranks in
  `docs/decomposition-standard.md` section 4: leaf contract, edit locations,
  acceptance check, previous-attempt feedback, and progress handover are all
  `floor=True`; neighbor contracts, the working agreement, caller narrative, and
  repo memory are fitted greedily in that priority order when the budget is
  tight. Priority sets the preference for what to keep, not a strict drop order:
  a section that does not fit is skipped and a smaller lower-priority one may
  still survive. Do not "simplify" a floor back to a plain priority: a worker
  that loses its edit locations or its acceptance check has been handed a
  scoping judgment, which is exactly what the standard forbids.
- **`LEAF_SCHEMA_VERSION` is 2 and the golden fixture asserts `model_dump()`
  byte-for-byte**: any new `LeafTask` field, even one with a default, changes
  `parse_review_response` output and breaks
  `tests/fixtures/decompose/expected_leaf_graph.json`. Regenerate the fixture in
  the same commit rather than loosening the golden test.
- **`_normalize_edit_locations` must never raise**: the module-level
  `_normalize_edit_locations` in `core/orchestrator_dispatch.py` (a plain function,
  not a `DispatchMixin` method, so patch it on the mixin module) normalizes the
  plan task's raw `files` value
  before it becomes the Bible's EDIT LOCATIONS floor section, a section that can
  never be dropped. On the plan_spec and improvement paths, `plan_task` is raw
  brain JSON (only the decomposition path validates it through `LeafTask`), so
  `files` can be any shape: a string, a list, a dict, or garbage. The normalizer
  must extract paths and return newline-joined strings or None without raising,
  because a `TypeError` here aborts the whole orchestration loop. Before it
  existed, a bare string `files` value iterated character-by-character into the
  floor section, and a non-iterable value raised an unhandled exception.
