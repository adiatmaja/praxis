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
- **Every hand-built LM Studio payload must state `reasoning_effort` explicitly** —
  `core/thinking.py` is the SSoT (`effort_param`), and `tests/test_thinking_explicit.py`
  gates the invariant over all payloads. qwen3.8-27b **thinks by default**, so an ABSENT
  key requests MAXIMUM effort rather than none: measured on the configured endpoint
  2026-08-15, omission produced 354 reasoning tokens, byte-identical to `high`, while
  `none` produced 0 (`low` is 317, so `low` is NOT an off switch). This is not latent —
  `plan_derive._derive_via_lm_studio`'s `json_schema` payload returned EMPTY, unparseable
  content with the key omitted, which raises `JSONDecodeError` out of `derive_opus_plan`
  and breaks the promote-plan.md path; at `none` the same call returns a clean task list.
  `LLMRouter._run_local` also used to DISCARD the registry `effort` that every CLI
  provider honors via `build_argv`; it now threads it through. The gate strips comment
  lines before matching, because each of these call sites carries a comment mentioning
  `reasoning_effort` and prose would otherwise satisfy it while the payload stayed silent
  (verified: without the strip, deleting the real parameter still passed).
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
  `agy` / `Gemini 3.7 Flash (High)`; the product default outside the mode stays
  `opencode`). A project registered without a `model_name` falls back to that worker.
  `dispatch_pending_tasks` then reuses one caller-named work branch and threads
  `single_branch=True` → `SINGLE_BRANCH=1` into the container, so both harness entrypoints
  REUSE the existing remote `BRANCH` with a non-force push instead of cutting a fresh
  `agent/{slug}` (changing that behavior needs an agent IMAGE REBUILD). Dead work branches
  (no open PR, no live run, never protected) are reclaimed by
  `core/branch_sweeper.dead_branches` via `ReconcileMixin.sweep_dead_branches` on the
  reconcile loop, which swallows its own errors so a sweep failure never wedges the loop.
  Mode is sequential in v1 (one delegate in flight at a time).
- **`config/praxis.yaml` is MOUNTED, not baked**: both compose files bind-mount
  `./config` read-only at `/app/config`, and the base file sets
  `PRAXIS_CONFIG_PATH` to point at it, so a YAML edit takes effect on
  `docker compose restart orchestrator` and never needs an image rebuild.
  This replaced the reverse behavior, which bit us
  live on 2026-07-27 when a `default_worker_*` change silently kept serving the
  baked-in value. `core/settings_file.config_file_path()` is the ONLY place the
  `praxis.yaml` path is decided (`config/model_capabilities.json` has its own
  module-relative resolver in `core/capabilities.py`); a hardcoded
  `"config/praxis.yaml"` literal anywhere else reintroduces the bug, and
  `tests/test_config_path.py` greps for exactly that.
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
- **MCP payloads lead with a `summary` key, and `get_task_logs` tails**:
  every state-returning MCP tool puts a one-line human summary first in its
  returned dict, because dict insertion order is what the client renders and the
  first line is what a reader actually sees. `get_task_logs` clips to the LAST
  `LOG_TAIL_CHARS` (40 KB) and says so both in the payload (`truncated`,
  `total_chars`) and inline in the text: Claude Code warns above 10,000 tokens of
  MCP output and caps at 25,000 by default, so an unbounded dump is truncated by
  the client exactly when a task has run long enough to wedge. Tail, never head:
  the top of a failing run's log is startup banner. Note that MCP tool
  DESCRIPTIONS also truncate, at 2 KB each, but Praxis was measured on
  2026-08-06 and its largest is 942 B, so there is nothing to fix there.
- **`praxis doctor` is the front door to every problem**: `core/doctor.py`
  registers eleven read-only checks, each with a fix hint;
  `core/doctor_probes.py` holds the pure decision logic (facts in, verdict out)
  so every check is testable with no Docker, network, or filesystem. Two checks
  exist specifically to convert this project's oldest silent failures into red
  lights: `agent_image_freshness` (an image older than its `entrypoint.sh` runs
  stale logic while the source looks current) and `callback_url` (a port
  mismatch 404s every agent callback, so tasks only ever finish via reconcile
  and get marked failed even on success). A raising probe becomes a RED result
  rather than an exception, and any RED makes the CLI exit non-zero; AMBER (for
  example "local mode, no GitHub credential") does not. Add a check to `CHECKS`
  and its probe together, or `run_checks` returns a RED "no probe registered".
  Three things that are easy to get wrong and are now pinned by tests: probes
  are pre-bound ZERO-ARGUMENT callables (a shared `**context` handed every probe
  every fact and turned each check red for the wrong reason), a hintless RED
  resolves its SPECIFIC hint from the registry in `CheckResult.__post_init__`
  (the generic docs pointer is a last resort, not the default), and fact
  gathering in `api/doctor.py` is guarded per UNIT via `_safe`, never per
  exception type, because the endpoint whose job is diagnosing a broken machine
  must answer 200 however broken that machine is. `agent_image_freshness`
  reports AMBER, not GREEN, when it had no entrypoint source to compare against:
  a green that checked nothing is the exact failure this check exists to remove,
  which is why both compose files mount `./docker` read-only.
- **`praxis init` is re-runnable and never eats your `.env`**: `cli/init.py`
  merges only the keys in `MANAGED_KEYS` into an existing `.env`, preserving
  every other key, its position, and every comment. It ends by running doctor and
  exits with doctor's code, so "init succeeded" and "the installation works" are
  the same claim. Its `.env` parser mirrors `python-dotenv`'s single-line grammar
  deliberately, and a differential test grades it against the real parser: an
  earlier version kept an inline comment that both python-dotenv and Docker
  Compose strip, so reusing an existing `AUTH_TOKEN` wrote a corrupted value back
  and every configured MCP client would 401 against a green doctor row. An empty
  managed value means "clear this key" and `None` means "no opinion", which is
  what lets switching worker preset fully replace the preset-derived keys instead
  of leaving a stale endpoint behind.
- **The approvals digest is rate-limited but the surfaces are not**:
  `core/approvals.should_publish_digest` gates only the `approvals_digest` SSE
  event (default every 6h, `approvals_digest_interval_h`). The MCP
  `pending_approvals` tool, the digest line on `poll_task`/`poll_plan`, `praxis
  pending`, and the dashboard badge all read live. Nothing parked means no event
  at all: a badge that appears when there is nothing to do trains people to
  ignore it, which defeats the purpose. Both the poll digest and the loop
  publisher swallow their own failures, because an add-on that can wedge
  `poll_task` or stop dispatch for every project is worse than no digest.
- **Local git mode is a backend, not a special case**:
  `core/git_backend.resolve_backend` picks `LocalGitBackend` when a project's
  `repo_url` is a filesystem path or a `file://` URL, and `GitHubBackend`
  otherwise. Everything above the seam is shared and unchanged: the merge gate
  still parks a PASS at `PASSED` unless `auto_merge` is set, the verify gates
  still run, outcomes are still recorded. Only the plumbing differs. A local
  "PR" is a `praxis-local://pr?branch=...&base=...` string stored in the
  existing `tasks.pr_url` column, so there is no schema change; parse it with
  `PullRequestRef.from_url`, never with string slicing. `GitHubBackend` itself
  raises `ValueError` for ANY ref carrying `repo=None`, not merely a
  `backend="github"` one, because a `None` repo makes `gh` resolve against the
  orchestrator's own cwd and act on the wrong repository; that is the
  fail-closed guard behind the repo's existing `--repo` gotcha. Keying it on
  the ref's own `backend` tag was a hole: the backend is resolved from
  `project["repo_url"]` while the ref is parsed from `task["pr_url"]`, two
  independent sources, so a `local` ref reaching `GitHubBackend` sailed
  straight past a backend-keyed check. `to_url()` guards the same way, so a
  repo-less github ref cannot poison a `tasks.pr_url` row with
  `https://github.com/None/pull/42`. Do NOT reintroduce a "backfill the repo
  from the project" fallback: it is unreachable given `from_url`'s regex, and
  reachable it would substitute the project's slug for the PR's, silently
  targeting the wrong repository for a fork PR.
- **A `pr_url` that no longer parses must FAIL the task, never return quietly**:
  `review_task` routes it through the same fail-and-retry path a failed review
  uses. A bare `return` (which is what the plan originally specified) wedges the
  plan forever, and silently: the task stays `REVIEWING`, the orchestration loop
  re-enters `review_task` on every tick, `REVIEWING` counts toward `active` so
  the plan never reaches `COMPLETED`, and `plan_stalled` requires `not active`
  so it never fires either. The only symptom is one log line per loop interval.
  `approve_task_merge` and `reject_task_merge` raise `ValueError` instead,
  because they are API-driven and an operator's click must not no-op.
- **One `repo_url` policy, shared by all three request schemas**: before
  `core/repo_url_policy.py` existed, `ProjectCreate` allowed https and
  scp-style SSH only, `DispatchRequest` also allowed `http://` and `ssh://`
  and checked the git option-injection fragments only as a PREFIX (so
  `https://github.com/u/r --upload-pack=/bin/sh` was accepted), and
  `ExecutePlanRequest` had no validator at all and accepted `ext::sh -c ...`
  and `git://` too. None of that was a live RCE (git refuses `ext::` itself,
  and the URL reaches git as a single argv element so an embedded option is
  never re-split), but three disagreeing copies of one security control means
  the weakest copy is the real one. There is now one implementation and every
  schema calls it; the fragment check is a CONTAINMENT check and runs BEFORE
  the local-path branch, or an opted-in deployment reintroduces the hole.
- **A local `repo_url` is admitted by the schema and judged by the settings
  gate**: a pydantic validator cannot see runtime settings, so
  `classify_repo_url` returns `RepoUrlKind.LOCAL` rather than refusing, and
  `core/preflight.assert_repo_url_allowed` decides. It reads
  `Settings.allow_local_repo_paths` (default OFF) with a `getattr` default of
  `False`, so an older or partial settings object fails CLOSED, and it is
  called at `/api/projects`, `/api/dispatch` and `/api/execute-plan` before
  any DB write. `execute_plan` calls it UNCONDITIONALLY, unlike its
  `expected_base_sha` preflight, which only runs when that field is set.
  With the default off the HTTP-visible behavior is unchanged: a local path
  still gets a 422, just with a detail naming the setting. Turning it on is
  what makes the local git backend, and therefore `bench/`, reachable through
  the REST surface at all. The knob lives in the MOUNTED settings YAML, so it
  is a `docker compose restart orchestrator`, never a rebuild. Do NOT write
  the literal `config/` + `praxis.yaml` path anywhere under `src/`:
  `tests/test_config_path.py` greps for it and a comment or an error message
  is enough to break the full suite.
- **A local `repo_url` is preflighted on ALL THREE endpoints, and only
  `_preflight_local` checks the SHAPE**: the `allow_local_repo_paths` gate
  answers "may a local path be used at all"; it says nothing about whether the
  path is a bare git repo. `/api/execute-plan` used to call `preflight_remote`
  only inside `if body.expected_base_sha is not None:`, so a plain
  `{"repo_url": "/"}` returned 201, wrote a project row, and
  `agent_manager.local_repo_volume` would have bind-mounted the entire host
  filesystem read-write into an LLM-driven agent container. Its two siblings
  returned 422 for the identical payload. The endpoint now preflights whenever
  `is_local_repo_url(body.repo_url)` is true, credential or not. Note also that
  the opt-in is ADMISSION CONTROL, not a kill switch: turning it back off does
  not stop an already-registered local project from being dispatched and
  bind-mounted, because nothing re-checks at spawn time.
- **A local `repo_url` is judged on its DECODED form, not the raw string**:
  `git_backend.local_repo_path` percent-decodes a `file://` URL and expands `~`
  before handing the result to git, so validating only the raw candidate leaves
  the validated string and the consumed string different. `file://%2D%2Dupload-
  pack=/bin/sh` decodes to `--upload-pack=/bin/sh`. The "it reaches git as a
  single argv element so an embedded option is never re-split" argument, which
  is correct for the remote forms, does NOT hold here: an argv element that
  BEGINS with a dash is consumed as an option. Verified against real git:
  `git clone --no-single-branch --upload-pack=/bin/sh dest` answers
  `fatal: repository 'dest' does not exist`, i.e. the path was eaten as a flag.
  So the option-injection fragments are checked against the decoded path too,
  and a decoded path starting with `-` is refused outright. A dash ELSEWHERE in
  the path is legitimate and stays allowed.
- **`sanitize_branch_ref` is shared by both request schemas**: `branch` had the
  same three-disagreeing-copies problem as `repo_url`, on a field that reaches
  git's argv more directly. `DispatchRequest` sanitized it; `ExecutePlanRequest`
  had no validator at all, so `branch="--upload-pack=/bin/sh"` was accepted and
  became `plans.plan_branch_name`, then `BASE_BRANCH`, then `PullRequestRef.base`
  (which round-trips intact through `quote`/`unquote`, since `-` is unreserved),
  then a git argument. No end-to-end exploit was demonstrated, because
  `git checkout <base>` dies on the unknown option first; it is fixed on the
  principle, not on a proof of exploitability.
- **`doctor._is_local_mode` asks `is_local_repo_url`, never a SQL `LIKE`**: it
  used to run `SELECT COUNT(*) ... WHERE repo_url NOT LIKE 'file://%'`, which
  recognizes one of the five forms that function accepts. `bench/runner.py`
  registers plain filesystem paths, so a full benchmark deployment (every
  project local, no GitHub credential by design) had `_is_local_mode` return
  False and `praxis doctor` reported a false problem about the missing
  credential. Exactly the disagreeing-copies pattern the shared policy exists
  to kill.
- **The local repo MUST be bare**: workers push to it, and git refuses a push
  to a checked-out branch. `core/preflight._preflight_local` enforces this with
  a `rev-parse --is-bare-repository` check and returns 422 (`NOT_A_REPO`) so
  the failure surfaces before a container spawns rather than deep inside the
  worker. Local mode also needs NO GitHub credential: preflight skips every
  remote call and `AgentManager` never consults the credential provider.
- **Local mode bind-mounts the bare repo read-write at a fixed container path**:
  `agent_manager.LOCAL_REPO_MOUNT` (`/srv/praxis-repo.git`). `REPO_URL` inside
  the container is rewritten to that path, and `GIT_BACKEND=local` tells both
  entrypoints to skip credential-helper setup and `gh pr create`. `GH_TOKEN`
  still gets a placeholder because the entrypoints hard-require it
  (`: "${GH_TOKEN:?...}"`). An unset `GIT_BACKEND` defaults to `github`, so an
  older orchestrator against a new image behaves exactly as before. Changing
  either entrypoint needs an agent IMAGE REBUILD.
- **Both entrypoints collapse `GIT_BACKEND` into one `IS_LOCAL_BACKEND` boolean
  before any guard reads it**, mirroring the `REUSING_BRANCH` precedent that
  already exists in the same files for exactly this drift reason. Before that,
  the credential helper tested `= "github"` and the PR block tested `= "local"`,
  opposite sides of two different values, so any third value (a typo, a future
  backend, a stale image) got NO credential helper AND the full `gh` path, the
  worst combination available. Equally important: **test these guards by
  EXECUTING them, not by grepping near them.** The original tests asserted the
  string `GIT_BACKEND` appeared within a line window of each `gh` call, and
  inverting either guard, closing the guard early so every `gh` call ran
  unconditionally, and moving the `GIT_BACKEND` default below its first use all
  left the entire suite green. `tests/test_entrypoint_local_backend.py` now
  slices the real regions out of the committed file and runs them under bash
  with `gh` and `git` replaced by argv-logging spies. Note the failure this
  catches is invisible in production: a stray `gh pr view` in local mode is
  swallowed by its own `2>/dev/null`, then clobbers `PR_URL` to empty.
- **`url_encode` must escape `%` before `/`, space, and `&`, in that order**:
  the entrypoint builds `praxis-local://pr?branch=...&base=...` with a
  four-`sed` pipeline, and `PullRequestRef.from_url` parses it back with a
  `([^&]+)` capture per field. A branch name containing `&` (or a literal `%`)
  encoded in the wrong order produces a URL the regex cannot parse correctly,
  and the failure is silent: the worker still reports `status=completed`, the
  orchestrator stores an unparseable (or subtly wrong) `pr_url`, and the
  reviewable change vanishes with no error anywhere. Escaping `&` before `%`
  double-escapes the very character it was meant to protect, decoding back to
  the wrong branch name instead of raising. Verified live: `agent/fix&more`
  round-tripped through the wrong order comes back as `agent/fix%26more`, not
  `agent/fix&more`.

- **The worker preset reaches the container as a BARE compose pass-through**:
  both compose files list `- DEFAULT_WORKER_HARNESS` and `- DEFAULT_WORKER_MODEL`
  with no `=` and no default. This is deliberate and the two obvious
  alternatives are both wrong. `Settings.__init__` drops a YAML key whenever
  that name is present in `os.environ`, and precedence is env > YAML > field
  default. `- VAR=${VAR:-something}` therefore wins on every run and permanently
  suppresses the mounted `config/praxis.yaml`; `- VAR=${VAR}` is worse in a
  subtler way, because when the variable is unset compose sets it to an EMPTY
  string, which is still "in `os.environ`" and so suppresses the YAML just the
  same while looking like nothing was configured. Only the bare form resolves to
  `null`, i.e. genuinely absent, leaving the YAML authoritative. It still reads
  the project `.env`, which is where `praxis init` writes. Before this,
  `LM_STUDIO_URL` was forwarded and the two preset keys were not, so half of one
  preset applied and nothing reported the disagreement.
  `tests/test_config_path.py` pins every `init.MANAGED_KEYS` entry, so a newly
  managed key that nobody forwards fails the suite. Caveat, still open:
  `LM_STUDIO_URL` itself remains on the `${VAR:-default}` form, because that
  default is the only source of `host.docker.internal` for a containerized
  deployment; six other `Settings` fields share that shape.
- **`praxis init` refuses to run outside the repo root**: it previously assumed
  its CWD was the checkout and, run anywhere else, wrote a `.env` containing a
  live `AUTH_TOKEN` into that unrelated directory while naming neither the
  directory nor the secret. `repo_root_problem()` returns a REASON rather than a
  bool and requires `pyproject.toml`, `.env.example`, and `docker-compose.yml`
  together with either a PEP 503 normalized `[project] name` of `praxis` or a
  `praxis` entry under `[project.scripts]`, so a renamed downstream fork that
  still ships the CLI is accepted rather than locked out by a message telling it
  to `cd` where it already is. The guard runs before any prompt, not merely
  before the write, and it fails closed on a malformed or structurally odd
  `pyproject.toml` (a `[project]` that parses to a string or an array must yield
  `""`, never an `AttributeError` traceback out of the first command a new
  operator ever runs).
- **`load_yaml_settings` warns ONCE per distinct missing path**, not once per
  call: `EffectiveSettings._get_yaml` has no memoization at all and re-reads the
  file on every capability, escalation, registry, and preset lookup, so an
  unconditional warning would flood the log on a hot path. Before this a typo in
  `PRAXIS_CONFIG_PATH` silently reverted every YAML default with no log line
  anywhere. Note precisely what it does NOT cover: `docker/orchestrator/Dockerfile`
  does `COPY config/ config/`, so if the `./config:/app/config:ro` mount is
  dropped the baked copy is still PRESENT, just stale, and an absence check
  cannot see it. That case is the doctor's `config_mount` probe, which detects it
  via `os.path.ismount`. The two are complementary, not redundant.
- **Triage fires once per leaf, on the SECOND worker-attributable failure**:
  failure 1 keeps the cheap `retry_task` path; from failure 2 on,
  `ReviewMixin._run_leaf_triage` asks `leaf_failure_triage` for a
  `TriageDecision`. The bound is durable, not in-memory: `tasks.triage_decision`
  is stamped before the decision is acted on, and its presence blocks any later
  triage. `tasks.parent_task_id` is the one-split-generation guard, so a split
  child can never split again (the code also zeroes its remaining leaf budget so
  the brain is not even asked). A router exception or two unparseable answers
  both fall back to `human`: triage is an optimization over the existing retry
  path and must never be able to wedge a task. Provider errors never reach
  triage at all; they take the respawn-cap path and burn no retry.
- **Split children APPEND to both the graph and the task table, and the parent
  is never deleted**: `TaskQueue.get_dispatchable_tasks` maps
  `opus_plan["tasks"]` to `get_tasks_for_plan` rows BY LIST INDEX, so inserting
  a child anywhere but the end, or removing the superseded parent, silently
  re-associates every task after it with the wrong row. `core/leaf_split.py` is
  written around this invariant and `tests/test_leaf_split.py` mutation-checks
  it. A split parent goes to `SUPERSEDED`, which both `all_tasks_done` and the
  dependency predicate in `get_dispatchable_tasks` count as satisfied; without
  that a split plan can never complete and its children never dispatch. Children
  start at `attempt = 2`, so they inherit the remaining retry budget rather than
  resetting it.
- **Escalation is a dispatch-time substitution, never a router fallback**: the
  implement seat is spawn-baked, so `LLMRouter` cannot fall back for it.
  `core/escalation.next_escalation` walks the ordered `implement_escalation`
  list in `config/praxis.yaml` using `tasks.escalation_index`, and
  `DispatchMixin` reads `tasks.implement_harness`/`implement_model` at spawn.
  `record_outcome` reads the SAME two columns: crediting the original worker
  with an escalated success teaches the calibration loop a lie. `config/praxis.yaml`
  is mounted, not baked (see the gotcha above), so an escalation-ladder edit
  takes effect on `docker compose restart orchestrator`, never an orchestrator
  image rebuild.
- **The merge gate judges `ref.base` when it is knowable, `plans.plan_branch_name`
  only as a fallback**: `backend.merge(ref)` writes to `ref.base`, but
  `auto_merge_eligible` used to be called with the plan branch instead. In
  auto-delegate single-branch mode those two differ, since dispatch reuses one
  caller-named work branch while basing it on the project default, so the
  protected-branch carve-out never saw `main` and a reviewed pass auto-merged
  straight into it (fixed in `106f6a7`, `base_branch = ref.base or
  plan.get("plan_branch_name")`). The fix is PARTIAL: a GitHub PR URL encodes no
  base, so `PullRequestRef.from_url` yields `base=""` there, and gating on that
  would treat every base as protected and disable auto-merge for every GitHub
  project. So only local refs are fixed; GitHub keeps the old plan-branch
  behavior. Closing the GitHub half needs the PR's real base, either a
  `base_branch(ref)` method on `GitBackend` backed by `gh pr view --json
  baseRefName`, or a base column on `tasks` populated at dispatch.
- **Difficulty scoring runs AFTER F3 and shares its round budget**
  `execute_plan_decompose` scores every leaf that survives validation; a leaf
  under `difficulty.reject_below` sends the whole decomposition back to the
  brain with its failing feature names, using the SAME `_DECOMPOSE_ATTEMPTS`
  budget as the F3 informed re-ask. A second failure raises `PlanReviewError`
  and no invalid graph is ever dispatched. The v1 weights in
  `config/praxis.yaml` are hand-set and explicitly provisional: their SIGNS are
  literature-grounded, their magnitudes are not, and the Capability Calibration
  Loop replaces them behind the `DifficultyScorer` protocol. A leaf with no
  `estimated_loc` is scored as if it used the whole LOC budget, on purpose: an
  unstated size is the leaf the planner did not think about, not a free pass.
- **The verify-gate kill switch is double-gated and literal**
  `core/bench_mode.verify_gate_disabled()` returns True only when BOTH
  `PRAXIS_BENCH` and `PRAXIS_BENCH_DISABLE_VERIFY` equal the literal string
  `"1"`. Either alone is refused, and a truthiness check is deliberately NOT
  used: a loose check is how a kill switch ends up live. It exists solely for
  benchmark condition C (decomposition without verification), disables the
  per-task gate, the per-wave gate, and the whole-plan gate together, and logs a
  warning every time it fires so a gateless run is never mistaken for a normal
  one in the logs.
- **Agent image staleness is judged by CONTENT, never mtime**: `core/entrypoint_hash.py`
  hashes `entrypoint.sh` with LF-normalized line endings, the build bakes it into
  each image as the `org.praxis.entrypoint-sha256` LABEL, and the doctor compares
  the two. The predecessor compared image build time against the file's mtime, and
  since `git clone` stamps every file at clone time, a correct fresh install always
  reported a stale image. `image_content_differs` is deliberately TRI-STATE: an
  image built before the label existed carries none, and calling those stale would
  reproduce the same false red from the other direction, so unknown is AMBER.
- **The worker-endpoint check is gated on `supports_local_llm` on BOTH halves**: the
  model-name comparison was already gated (agy names a provider model, not an LM
  Studio one) but the reachability probe was not, and `if not reachable` fires
  first, so the flagged default preset `gemini-agy` could never go green. Gate both
  or neither.
- **`docker compose restart` does NOT re-read `.env`; only `up -d` does**: the docs
  say `restart` correctly and repeatedly about the MOUNTED `config/praxis.yaml`, and
  Quick Start says to edit `.env`, so the pattern teaches the wrong recovery for the
  wrong file. The `env_drift` doctor check now detects it instead of relying on the
  operator knowing.
- **An unmet preset requirement must print the remedy, not just the requirement**:
  `praxis init` names what is missing AND how to supply it, from the preset's
  `setup_hint` / `setup_doc` in `config/praxis.yaml`. It also writes the collected
  token, port, and credentials BEFORE the preset challenge can exit, because
  `_choose_preset` raises `typer.Exit(1)` and used to discard them all.
- **Built without the build arg, the `org.praxis.entrypoint-sha256` label key is
  PRESENT with an EMPTY STRING value, not absent and not `<no value>`**: this is
  why `image_content_differs`'s "cannot judge" test is `not image_label` (catches
  both `None` and `""`), never `image_label is None` alone; the latter would treat
  a pre-label image as a real hash mismatch and go red instead of amber. The
  partial-`.env` write added for the preset-challenge exit is scoped to that ONE
  path (`_choose_preset` raising `typer.Exit(1)`): the separate "Update `.env`?"
  decline is a different code path and keeps its own pre-existing byte-identical
  guarantee, unchanged by this fix.
