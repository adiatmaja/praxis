# Gotchas — Praxis

Full narrative for the non-obvious traps in Praxis. **This file is the complete list and the
canonical reference.** `CLAUDE.md` carries only a shortlist of the ones that bite during
ordinary edits, so a new gotcha goes HERE first; add it to the CLAUDE.md shortlist only if it
belongs among the everyday traps.

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
  startup. Without it, project creation returns 503 naming the real remedy (stop the
  orchestrator, delete `data/orchestrator.db`, restart)
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
  `AGENT_CALLBACK_URL`) and is passed to `Orchestrator(callback_url=...)`. Note the port
  in that URL must be the HOST-side one, because the agent container reaches back through
  `host.docker.internal`, while `PORT` inside the container is the in-container bind 8080.
  Compose therefore derives `AGENT_CALLBACK_URL` from `${PORT:-12323}` explicitly rather
  than letting `callback_url()` read the container's own `PORT`. Get that wrong and every
  agent callback 404s, so tasks only ever finish via reconcile (and get marked failed even
  on success).
- **Brain prompts go to the CLI via stdin, never argv** — `OpusBridge._run_claude_raw`
  and `LLMRouter.run` pipe the prompt through stdin (`communicate(input=...)`); a full
  PR diff as an argv element overflows the OS command-line limit (Windows `WinError 206`
  above ~32K chars). `build_argv` deliberately omits the prompt.
- **The claude CLI effort flag is `--effort`, not `--reasoning-effort`** — the installed
  CLI (v2.1.170) renamed it; passing the old flag fails every effort-tiered brain call
  with `unknown option '--reasoning-effort'` (this 500'd `/api/execute-plan`, whose
  `plan_review` call-site resolves to Sonnet through the `plan` role chain, and carried
  an effort flag at the time). Both `core/llm_router.build_argv` and
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
- **Rebuild the harness agent images after ANY `entrypoint.sh` change, with compose** —
  the agent images (`opencode-agent:latest` default, `agy-agent:latest` experimental)
  are never started by a plain `docker compose up`, so a stale image silently runs old
  entrypoint logic while the source looks current. This bit us live: a pre-callback-token
  image sent an **empty** `X-Praxis-Callback-Token`, so every callback 401'd and tasks
  never advanced past implement (only reconcile → failed). The rebuild is
  `praxis init` then `docker compose --profile agents build`: both images ARE compose
  services under `profiles: [agents]` (build-only entries with `command: ["true"]`, which
  is what keeps them out of `up`). Do **not** reach for a bare `docker build`. It leaves
  the `org.praxis.entrypoint-sha256` label EMPTY, because that label is a build ARG only
  compose supplies, and the freshness check reads an empty label as "cannot judge" rather
  than as fresh, so the rebuild looks done while the stale image may still be what runs.
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
- **Harness images build behind a compose profile** — one command builds every image
  `AgentManager` can spawn: `docker compose --profile agents build`. Never a bare
  `docker build`; see the entrypoint-rebuild entry above for why the label it omits
  matters.
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
  gates the invariant over all payloads. The reason is NOT any particular default, it is
  that **the default is not a stable API and has inverted twice** on the configured
  endpoint. Measured, same payload shape both times: on **2026-08-15** an absent key meant
  MAXIMUM effort (354 reasoning tokens, byte-identical to `high`, while `none` produced 0);
  on **2026-08-21** an absent key means ZERO, byte-identical to `none`. Nothing in Praxis
  changed between those dates. A silent payload is one whose behaviour is chosen by
  whichever LM Studio build is running, and it will flip again with no error and no failing
  test. Two more traps in the same table: `low` has never been an off switch (317, then
  188), and the levels are **not monotonic** — on 2026-08-21 `medium` thinks MORE than
  `high`, and `low` and `high` are indistinguishable, so do not treat the labels as a scale.
  This is not latent, and the blast radius moved with the default:
  `plan_derive._derive_via_lm_studio`'s `json_schema` payload returned EMPTY, unparseable
  content **with the key omitted** in 2026-08-15, and in 2026-08-21 returns EMPTY at `low`,
  `medium` AND `high` while omitted and `none` both parse. Either way it raises
  `JSONDecodeError` out of `derive_opus_plan` and breaks the promote-plan.md path. The
  durable fact under both measurements is that **structured `json_schema` extraction breaks
  whenever the model thinks at all**, which is why that call site pins `effort_param(None)`
  and why raising its effort to "improve" it is the one change guaranteed to break it.
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
  consistency uses the same `WORKER_RESERVE_FRACTION = 0.6` (`core/token_budget.py`) as
  `worker_bible`/`fit_sections`, replacing the independent `_LEAF_BUDGET_FRACTION = 0.4`
  that existed before and no longer exists anywhere under `src/`.
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
  tick; only `skipped` (no `verify_cmd`, bench mode disabling the gate deliberately, no plan branch, or no credential) passes through.
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
  `core/branch_sweeper.dead_branches` via `orchestrator_reconcile.sweep_dead_branches` on
  the reconcile loop, which swallows its own errors so a sweep failure never wedges the
  loop. That sweeper is a MODULE-LEVEL function, not a `ReconcileMixin` method:
  `reconcile_runs` calls it bare, so a test that patches it on the mixin (or on
  `core.orchestrator`) patches nothing and the real sweep still runs.
  Mode is sequential in v1 (one delegate in flight at a time).
- **Per-task review scoping DEPENDS on auto-delegate staying sequential, and nothing
  enforces it.** Every task in single-branch mode pushes to one shared work branch, so the
  pull request that branch carries accumulates every task's commits. Before 2026-08-24 the
  per-task reviewer was shown all of them and failed every task after the first for
  touching files outside its scope, and `core/outcome_recorder` wrote that FAIL against a
  worker that had done its own task correctly, so the mode was quietly poisoning the
  calibration signal as well as being unusable past task one. The fix records
  `tasks.review_base_sha` at dispatch (the branch head the task starts from, resolved
  through `backend.head_sha` BEFORE the container spawns, because the worker's first push
  moves it) and bounds the review to `review_base_sha..head` via `backend.get_diff_since`.
  A re-dispatch KEEPS the recorded sha: a retried worker pushes to the same branch and its
  first attempt's commits are still there, so re-recording would scope the review to the
  fixup commit alone. Only a branch that has VANISHED from the remote gets a fresh sha.
  An ORPHANED sha (force push, recreated branch) never yields an empty diff, because an
  empty diff reviews as a trivially passing change: both backends fall back to the whole
  pull request and log it. A NULL sha means "review the whole pull request", which is the
  pre-2026-08-24 behavior and what every two-tier row uses.
  **The landmine, and it fired the same day:** that boundary is correct only while ONE
  worker is on the branch at a time. The mode was called sequential on the ground that the
  brain dispatches one task at a time, which is true of the MCP path and FALSE of
  `execute_plan`, where the LOOP dispatches a whole wave with no brain in it. Measured live
  2026-08-24: a two-leaf plan dispatched both leaves at once, both recorded the same base
  sha because neither branch existed yet, and the second was failed by its reviewer for
  creating the first's file, three attempts running. `dispatch_pending_tasks` now starts ONE
  task per wave in single-branch mode and holds while any task on the branch is
  `in_progress` or `reviewing`; `passed` does not hold, its review having already happened.
  REVIEWING has to block for a different reason from IN_PROGRESS: a review resolves its
  range when it RUNS, so a worker committing during someone else's review widens THAT
  task's range. Whoever makes the mode concurrent for throughput has to solve the scoping
  first. The micro-edit lane
  (`docs/superpowers/specs/2026-08-21-micro-edit-lane.md`) inherits the same constraint
  from the other side: a brain commit landing on that branch while a worker runs breaks the
  range for both.
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
- **The agy JSON envelope shape was VERIFIED on 2026-08-14; the resume path still is not.**
  This entry originally said the whole envelope was unverified, written 2026-08-05 when no
  `agy-agent:latest` image and no `praxis-gemini-creds` volume were available. A live dogfood
  run on 2026-08-14 confirmed `--output-format json` against a real agy build: the envelope
  carries `{conversation_id, status, response, duration_seconds, num_turns, usage}`, and the
  sibling `usage` object carries `input_tokens`, `output_tokens`, `thinking_tokens`,
  `cache_read_tokens` and `total_tokens` with concrete non-zero counts. `usage.total_tokens`
  is what the token-telemetry callback reads (see the harness parity entries at the end of
  this file). What remains unverified is narrower than it looks: `--conversation <id>` and the
  resume happy path (an id captured, stored, and successfully replayed) have still never been
  exercised end to end. `conversation_id`
  is a single guessed key with no fallback; the response-body key is a genuine multi-candidate
  guess (`response`, `text`, `output`, `content`, `message`, tried in that order in
  `docker/agy-agent/extract_session.py`). Malformed or unrecognized JSON makes the extractor
  exit 1, and the entrypoint falls back to treating the raw agy output as the transcript
  exactly as it did before this feature, so a wrong guess degrades to today's behavior rather
  than breaking the task. **That sentence was FALSE until 2026-08-22, and this doc is part of
  why it stayed hidden**: a well-formed envelope carrying no recognized body key returned 0
  having printed only the conversation id, so the fallback never ran and the transcript was
  discarded whole. The extractor now returns 1 when no candidate key yields a body, and a
  second guard in the entrypoint copies RAW_LOG whenever the split leaves OUTPUT_LOG empty.
  But the agy resume happy path (an id captured, stored, and
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
  registers twelve read-only checks, each with a fix hint;
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
  that name is present in the environment, and precedence is environment
  variable > `.env` file > settings file > field default. `- VAR=${VAR:-something}`
  therefore wins on every run and permanently
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
- **Which command applies a `.env` edit depends on whether compose FORWARDS the key,
  and the two answers are opposites**: this was written here as the flat rule
  "`restart` does NOT re-read `.env`; only `up -d` does", which is half right and
  inverted for the other half.

  A key compose substitutes or passes through (`${AUTH_TOKEN}`, `${PORT}`, the bare
  `- DEFAULT_WORKER_HARNESS` entries) is resolved when the container is CREATED and
  baked into its environment. `restart` reuses that container, so it keeps the old
  value; only `up -d` recreates it. That is the case the flat rule describes and the
  case `env_drift` can see, because it compares the container's environment against
  the file.

  A key compose does NOT forward (`LOOP_INTERVAL`, `CALLBACK_GRACE`, anything else
  with a `Settings` field and no compose line) never enters the container environment
  at all. The process reads it from the MOUNTED `/app/.env` when it starts, so a
  `restart` applies it, and `up -d` is a NO-OP whenever nothing in the compose config
  itself changed. `env_drift` is blind to these by construction: it only compares keys
  the container actually received.

  Measured on a live install, 2026-08-21: with `LOOP_INTERVAL=11` appended to `.env`,
  `docker compose up -d` reported "Container orchestrator Running" and the running
  process kept its old value; `docker compose restart orchestrator` picked it up and
  logged `LOOP_INTERVAL is set in the dotenv file and also in ...; the dotenv value
  wins`. This only became reachable when the dotenv layer started beating the settings
  file (see the precedence entry above); before that a non-forwarded `.env` key did
  nothing whichever command you ran, so the flat rule was never wrong in practice.

  **Tell an operator `up -d` and then `restart`.** It is correct for both halves and
  costs one extra second.
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
- **Line endings are a contract, and `.gitattributes` is what enforces it**: GitHub's
  `windows-latest` runner checks out with `core.autocrlf=true`, so without the repo-root
  `.gitattributes` (`* text=auto eol=lf`) every `docker/*/entrypoint.sh` lands in the
  working tree with CRLF. That matters because the entrypoint tests EXECUTE the sliced
  guard regions under `bash`, which then reads `fi\r` and never runs them: the git spy
  records no calls and the assertions fail on Windows ONLY, with an error message
  (`credential.helper` never configured, `git rev-list` rc=128) that points at the
  product rather than at the checkout. The bench sample's explicit LF assertion failed
  the same way. Note the index was always clean: these blobs are LF-in-index, so the
  corruption existed only in the runner's working tree, which is exactly why it was
  invisible locally. Adding a new executable-under-test or a byte-asserted fixture
  needs no action, the glob already covers it; adding a genuinely binary file type does.

## Harness parity: making delegation predictable across harnesses

- **Worker thinking effort must be STATED, per harness, from the harness's declared channel**:
  `core/thinking.py` encodes the rule for BRAIN payloads, that a thinking level is never
  expressed as an absent key, because what the server does with a silent payload is not
  stable and has inverted twice. Workers had the same hole and it was worse,
  because it differed by harness with nothing declaring so. OpenCode's generated provider
  config carried no effort at all, so every OpenCode worker silently ran at maximum, while agy
  takes its effort baked into the Gemini model string (`"Gemini 3.7 Flash (High)"`). The same
  task therefore ran under two different and undeclared thinking regimes depending on which
  harness picked it up, with no error, no warning and no failing test. `core/harnesses.py` now
  declares an `effort_channel` per harness (`request_option`, `model_name`, or `none`) and
  `core/worker_effort.py` resolves exactly one value from it. Read the return type carefully:
  `None` is a real answer meaning "this harness has no knob to turn", and it is NOT the same as
  "off". Collapsing the two reintroduces the bug, because setting an env var a harness ignores
  reads as configured-but-working when it is neither.

- **The OpenCode config key is camelCase `reasoningEffort`, NOT snake_case `reasoning_effort`**:
  two different naming conventions live at two different layers, and picking the wrong one
  fails silently. OpenCode's own config schema reads a per-model request option as
  `"options": { "reasoningEffort": "high" }` (verified against <https://opencode.ai/docs/models/>).
  OpenCode's transform layer then converts that camelCase config key into the wire-level
  snake_case `reasoning_effort` field of the actual HTTP request to LM Studio. The snake_case
  form belongs to the wire payload, never to `docker/opencode-agent/entrypoint.sh`. Writing
  snake_case in the config produces syntactically valid JSON that OpenCode's option parser
  never finds, so the setting is dropped and the model quietly runs at whatever the server
  defaults to, which is not stable and has inverted twice. This one was caught only because the fix was checked against
  the vendor docs rather than written from memory.

- **`@ai-sdk/openai-compatible` SILENTLY DROPS model-level `options` when the provider name
  contains a dot**: filed upstream as <https://github.com/anomalyco/opencode/issues/23622>
  (note OpenCode's repo moved from `sst/opencode` to `anomalyco/opencode`, so a remembered
  `sst` URL is stale). Praxis names its provider `lmstudio`, with no dot, so it is safe today.
  The trap is renaming: pointing the provider id at something like `pcllm.sigmasolusi.com`
  would disable `reasoningEffort` with no error and no log line, and the symptom would be a
  worker that reasons more than it was told to, which looks like a model problem rather than a
  config problem. Keep the provider id dot-free.

- **An omitted `harness` used to re-point an existing project at OpenCode**: both
  `POST /api/execute-plan` and `POST /api/dispatch` computed `body.harness or
  default_harness_id()` up front and then ran `UPDATE projects SET model_name = ?, harness = ?`
  unconditionally for an existing project. Submitting a plan without the field therefore
  downgraded an agy project to opencode, and because the write succeeded quietly, "which
  harness actually ran this task" became unanswerable after the fact. The parameter is now
  `str | None` all the way down, and `None` means "the caller expressed no preference": an
  existing project keeps its configured harness, and only a NEW project falls back to the
  registry default. Both endpoints had the identical bug, so fixing one and not the other
  would have left the hole open through the MCP dispatch path.

- **A bare `docker build` gives an agent image an EMPTY entrypoint-sha256 label**: the
  Dockerfile takes the hash as a build ARG that defaults to the empty string, and
  `docker-compose.yml` supplies it as `PRAXIS_ENTRYPOINT_SHA256: ${OPENCODE_ENTRYPOINT_SHA256:-}`
  from a variable computed and exported by `src/cli/init.py` via
  `core/entrypoint_hash.hash_entrypoint` (which normalizes CRLF to LF before hashing, so the
  hash is checkout-independent). So the correct rebuild after an entrypoint edit is
  `praxis init` followed by `docker compose --profile agents build`, never a bare
  `docker build -t opencode-agent:latest docker/opencode-agent/`. A bare build produces an
  image whose label is present-but-empty, which the staleness check reads as "cannot judge"
  and reports amber rather than red, so the rebuild looks done while the image may not be.
  Note also that a plain `sha256sum` of the entrypoint only matches the label when the file
  happens to have no CRLF; that agreement is incidental, not the real comparison path.

## Submitted specs: the carrier between `praxis submit` and the planner

- **A validated field that is never read again is not a feature, it is a leak**:
  `POST /api/projects/{id}/plans` took a `PlanCreate` body, validated that `spec` was
  non-empty, and then never referenced `body` again. Spec 2 dropped the `plans.spec`
  column on the correct principle that markdown docs are the source of truth, but nothing
  was ever written in the column's place, so `spec_path` stayed NULL and the brain planned
  from the repository name alone. Observed live on a Python repo: a spec asking for one
  Python file produced a Node.js/ESLint/Jest scaffold, which was then activated and
  dispatched at the real repository. `praxis submit` is the only route the CLI has to the
  engine, so the failure was silently destructive rather than merely incomplete. The
  submitted text is now committed as a spec doc under `docs/superpowers/specs/` BEFORE the
  plan row exists: if that write fails the endpoint returns 502 and no plan is created,
  because a plan with no spec is worse than no plan.

- **`plans.spec_path` is a PATH, and the planner prompt wants TEXT**: even after the write
  end was fixed, `plan_and_activate` passed `plan["spec_path"]` straight into
  `plan_spec(spec=...)`, which interpolates its argument into the prompt. The brain would
  then have received the literal string `docs/superpowers/specs/2026-08-20-....md` as its
  specification. Resolving the path back to text needs a reader, so `Orchestrator` takes a
  `spec_reader` (wired to `BrainstormManager` in `main.py`); when it cannot resolve, the
  plan goes terminal FAILED with the reason recorded, never to the planner with an empty
  spec.

- **Both ends of this carrier were independently correct and independently tested**: the
  CLI genuinely sent the spec, the prompt template genuinely interpolated whatever it was
  given, and every unit test at either end passed while the product was broken. This is the
  `unit-green-seam-inert` shape and no end-anchored test can see it. `tests/test_submit_spec_seam.py`
  is deliberately anchored OUTSIDE both ends: it invokes the real Typer `submit` command
  over a real `httpx.Client` into the real ASGI app, then asserts the submitted sentence
  appears in the prompt string handed to the LLM router. It was proven by mutation, three
  times: drop `spec_path` on the insert, pass the path instead of the text, and drop the
  body from the rendered doc; each goes red with a distinct message.

## The merge gate, the operator's `.env`, and a doctor that spends money

- **The merge gate had no CLI verb at all, and that is why the loop could not be
  closed without `curl`**: `POST /api/tasks/{id}/approve-merge` and
  `POST /api/plans/{id}/approve-merges` existed, were documented, and were used
  by the MCP server, but nothing in the CLI invoked either. The command a
  newcomer reaches for, `praxis approve`, targets autonomous improvement PLANS
  and returns `404 Plan not found` against a task id. `praxis merge <task-id>`
  and `praxis merge-plan <plan-id>` now exist. `merge-plan` exits 1 when any
  task failed, because the endpoint skips non-eligible tasks BEFORE its try
  block, so every entry it returns in `errors` is a review-passed task that
  genuinely did not merge.

- **A table that prints an id an API will reject is worse than printing
  nothing**: `pending` printed no id at all, and `plans` printed `id[:8]` while
  the lookup is an exact match, so following the help text returned 404. Note
  `overflow="fold"` alone does not fix this: on a narrow column rich still wraps
  the uuid across physical lines separated by border characters, which scrambles
  the value on copy. Worse, a bordered table cannot hold a 36-char id AND a PR
  url at 80 columns no matter which columns you drop, so `pending` now prints a
  plain `praxis merge <id>` line per task underneath the table. Rich's default
  word-wrap only breaks on whitespace, so a token with none survives contiguous
  at any width. `plans`, `projects`, and `tasks` now print the id whole in a
  36-wide folding column, which removes the 404. Be precise about what that does
  and does not buy: the id is no longer truncated, but on a terminal narrower
  than roughly 100 columns the folding column still wraps it across physical
  lines with border characters between the fragments, so it reads correctly but
  does not copy cleanly. `tests/test_cli_ids.py` pins `COLUMNS=160` and therefore
  does not cover that case. Only `pending`, which prints a plain line rather than
  a table cell, is genuinely copy-pasteable at any width. Agent-run ids inside
  `praxis task` are still truncated on purpose: no command takes one.

- **An unrecognised key in `.env` used to abort orchestrator startup**:
  `docker-compose.yml` mounts `./.env` at `/app/.env` so `praxis doctor`'s
  `env_drift` check can compare them, which means pydantic-settings parses the
  operator's whole dotenv file. `BaseSettings` defaults to `extra="forbid"`, so
  one container-only variable produced `extra_forbidden` and a restart loop, with
  a traceback naming the key but never naming `.env` as the source. `Settings`
  now sets `extra="ignore"`. **The cost, stated accurately: a typo in a real key
  is now silent and NOTHING catches it.** `env_drift` structurally cannot:
  compose passes an explicit key allowlist and names `.env` in no `env_file:`
  directive (the one `env_file:` entry is the separate `.env.container`), so a
  typo'd key never enters the container env; `_env_drift_facts` then filters the
  on-disk map to `if k in os.environ`, discarding it; and `probe_env_drift` only
  compares keys present on both sides. Verified by executing the probe: a `.env`
  carrying `AUTH_TOKN` returns GREEN. `AUTH_TOKEN` itself is the exception,
  being a required field with no default.

- **Settings precedence is environment variable, then `.env`, then the settings
  file, then the field default, and a key in both files now says which won**:
  the dotenv file counts as ENVIRONMENT, and it did not use to. `Settings.__init__`
  overlays `config/praxis.yaml` as init KWARGS, and a kwarg outranks every
  pydantic-settings source including the dotenv one, so a key present in BOTH
  files silently took the settings file's value. Measured 2026-08-21:
  `LOOP_INTERVAL=0` in `.env` left the container running at the YAML's 5 and
  said nothing anywhere. Worse, it split the two ways of running the product
  apart, because compose forwards `DEFAULT_WORKER_HARNESS` and
  `DEFAULT_WORKER_MODEL` as real environment variables: the same dotenv file
  that `praxis init` writes won inside a container and lost under a bare
  `uvicorn`, with no way to tell which you were looking at. `_dotenv_keys` now
  reads the dotenv file pydantic-settings is about to read and treats those
  names as set, so the YAML value is not injected for them. Two things follow
  and both matter. A `PRAXIS_`-prefixed variable is EXEMPT, and has to be: those
  keys are in the overlay because an environment variable put them there, not
  because the file holds them, so shadowing one with the dotenv file would drop
  a real environment variable in favour of a lower-precedence source. And the
  fix would otherwise open the opposite silence, an operator editing the
  settings file for a key their dotenv file also names and getting no
  edit, so `_log_dotenv_overrides` emits ONE `INFO` line per key naming the
  winner and the settings-file path. Once per key per process, because
  `Settings` is constructed more than once and a per-construction line would
  repeat forever. Only the dotenv case is reported: a real environment variable
  winning is the documented order and surprises nobody.

- **`gh pr merge` can fail AFTER GitHub has performed the merge**: a
  `504 Gateway Timeout` on the response is not evidence the merge did not
  happen. Observed on two of three merges in one session. The old code raised,
  the task stayed at `passed`, it kept appearing in `praxis pending`, and every
  dependent task stalled, because dependents wait for a MERGE and not a review
  pass. `merge_pr` now asks `gh pr view --json state` before declaring failure,
  and GitHub's answer outranks gh's exit code. Note the 504 body says
  "resubmitting your request", which is why it matched none of the old
  `_TRANSIENT_MERGE_PATTERNS` entries and never even retried; gh has a second
  rendering (`HTTP 504 (https://api.github.com/...)`) containing neither that
  phrase nor "gateway timeout", so both anchored forms are needed and both are
  pinned by parametrized tests.

- **A merged task's `agent/*` branch is swept by nothing**: `branch_sweeper`
  nominates a branch only from `terminal_failed` or `merged_plan`, and
  `merged_plan` reads `plans.plan_branch_name`, never `tasks.branch_name`. A
  merged task is also excluded from `live_branches` and `open_pr_branches`.
  Normally `--delete-branch` on the merge cleans it up, which is why this has not
  bitten; it bites exactly when gh 504s after the merge, because the branch
  delete never lands. Still open: the structural fix is to add
  `SELECT branch_name FROM tasks WHERE status = 'merged'` to the sweeper's
  dead set. GitHub's repo-level "automatically delete head branches" setting
  collects these server-side meanwhile.

- **An installed, authenticated planner CLI can still refuse every prompt**:
  `_PROVIDER_CMDS["claude"]` has no auth command, so `authenticated` only ever
  meant "the binary exists". A hook mounted in with `~/.claude` refused every
  prompt while `praxis doctor` printed `OK planner CLI installed and
  authenticated`, and the operator found out minutes later as
  `ValueError: Could not extract JSON from response` mid-plan. The check now runs
  one real round-trip and asserts on the OUTPUT, not the exit code, because a
  blocking hook can still exit 0. Two consequences to know: the sentinel is
  stripped from the reply before matching, because the prompt contains the word
  `PONG` and an echo of the input would otherwise read as success; and a
  rate-limited subscription reports AMBER with its own hint rather than RED,
  because Praxis treats the 5h limit as normal and self-healing and
  `praxis init` exits with the doctor's status code.

- **`praxis doctor` is no longer free**: the `planner_cli` check spends one real
  planner call per run, cached 60s. It is read-only against your repo and
  database, but it is not free. The timeout is 20s deliberately: a healthy cold
  `claude -p` was measured at 15.68s on a Windows host, so a 15s timeout would
  false-RED a working planner and accuse the operator of a blocked hook.

- **A completed plan is NOT a landed plan, and nothing used to say so**: when
  the last task merges, the work sits on the plan branch and Praxis opens an
  integration PR to the base branch. That URL used to exist in exactly two
  places a user cannot reach: an SSE event that has already fired by the time
  anyone looks, and one INFO line in the orchestrator's log. `praxis pending`
  printed `Nothing awaiting approval.` with two such PRs open, `praxis plans`
  showed `completed` with no URL, and `praxis merge-plan` returned
  `Merged: 0 task(s)` and exited 0. It is now persisted on
  `plans.integration_pr_url` (migration 9) and `merge-plan` merges it once every
  task is merged. Two rules follow: `integration_merged_at` is what takes the
  plan back OUT of `pending`, so a merge path that forgets to stamp it turns
  one lie into its mirror image; and integration is refused while any task is
  unmerged or any task merge errored, because a partial plan must never reach
  the base branch.

- **The sweeper counted every `completed` plan as reclaimable**: so a plan
  branch carrying the whole plan's merged work was classified dead while its
  integration PR was open, and deleting it would also have closed that PR. This
  used to add that the delete was "separately broken (it shells `git push` with
  no repository, which git refuses unconditionally)", and **that was already
  false when it was written**: `GitOps.delete_remote_branch` passes `repo_url`
  as the push target and logs `Deleted remote branch %s on %s`. The deleter is
  ARMED, so a classification bug here destroys work rather than being caught by
  an inert delete; the assurance was the more dangerous half of the entry.
  Corrected 2026-08-24, together with the other half of the same defect: every
  ledger query was GLOBAL while the sweep runs per remote, so a failed task in
  repository A nominated an identically named branch in repository B for
  deletion. The sets are per repository now, and a row whose repository cannot
  be resolved can only ever SPARE a branch. Note the two guards that now
  protect it are co-extensive by construction, both reading
  `integration_pr_url`/`integration_merged_at` off the same row, so a
  whole-sweep test stays green when either is reverted alone; the ledger
  contents have to be asserted directly to pin them individually
  (`tests/test_reconcile_sweeper.py`).

- **A worker's empty diff was a failure, and that failed whole plans**: with a
  plan decomposed into "write the module" and "write its tests", task 1
  routinely writes both files. Task 2 then has nothing to change, reports it
  correctly, and used to be called `failed`, retried three times to the
  identical correct answer, and take the plan down with the repository already
  in the state the spec asked for. Measured in 4 of 4 plans across BOTH
  harnesses; the survivors only got lucky. Both entrypoints now report
  `no_changes` and the ORCHESTRATOR decides, by running the project's verify
  command against the branch the leaf was cut from. Three conditions are
  load-bearing and each has its own test: an empty transcript stays `failed`
  (nothing ran, so a dead worker cannot silently close every leaf), an
  untrustworthy `rev-list` count stays `failed` (the worker may have committed
  work this run cannot see), and a failing or erroring verify stays `failed`.
  A unit test asserting "empty diff -> failed" passes before the fix AND after
  a bad one, so assert on the STATUS carried to the callback instead.

- **`TaskStatus.NO_CHANGES` must be in `SATISFIED_STATUSES`, not merely in
  `TERMINAL_STATUSES`**: it is terminal and it is neither a success nor a
  failure, like `SUPERSEDED`. Leave it out of the satisfied set and every
  dependent of a no-op leaf waits forever for a `MERGED` that cannot come, and
  the plan never completes. Nothing raises.

- **The CLI could not find the install it was standing in**: a clean shell in
  the repo root, orchestrator running, `AUTH_TOKEN` right there in `.env`, died
  on `Set AUTH_TOKEN (or ORCHESTRATOR_TOKEN) env var`, and neither name appears
  anywhere in `README.md` or `docs/deployment.md`. The token and the port now
  fall back to the nearest `.env` walking up from the working directory, parsed
  with `cli.init.parse_env` (the product's own parser, not a second one that
  could disagree). Explicit env vars still win, which is what keeps a CLI
  pointed at a remote deployment from being silently redirected at whatever
  `.env` happens to be nearby. `praxis env` prints which file and which source
  won; every other verb stays quiet.

- **`praxis init` is TTY-only unless you pass `--non-interactive`**: piping
  newlines at the wizard does not work, and it fails in the worst way. The
  QUESTION SET changes with state: five questions on a fresh clone, seven once
  `.env` exists (`Reuse the AUTH_TOKEN already in .env?` first, `Update .env?`
  last). An answer sequence tuned on the first run silently misaligns on the
  second and answers the wrong questions. `--preset` takes a NAME, never the
  menu index, because the index shifts whenever the settings YAML gains or
  reorders a row. `--non-interactive` deliberately does NOT relax the unmet
  requirements guard: that install starts, reports healthy, and fails its first
  real task, which is exactly what the guard exists to prevent.

- **`init()` cannot be called directly, `run_init(Answers(...))` can**: typer
  rewrites a command's parameter defaults into `OptionInfo` objects, so a
  function carrying them is only callable through the command line. The logic
  lives in `run_init`, which takes one `Answers`; `init` is the typer shim. The
  autouse repo-root guard in `tests/test_cli_init.py` patches `run_init` for the
  same reason.

- **Git Bash silently TRUNCATES a `bash -c` argument at 8192 bytes**: the
  no-change entrypoint region crossed that ceiling as it grew and every agy case
  started failing with `syntax error: unexpected end of file`, which reads as a
  broken entrypoint. It was not: the same script parsed clean as a file, and
  echoing argv back showed 9507 bytes sent and 8186 received. Pass a generated
  script on STDIN (`bash -s`, `input=script`), never as an argument. Same class
  as the brain's own argv limit, at a quarter of the size.

- **Running a sliced entrypoint region against the REAL git commits to your
  repo**: `tests/test_entrypoint_no_change_diagnostic.py` puts a spy `git` first
  on `PATH` for exactly this reason. Debugging the slice by hand with
  `bash -c "$(cat slice.sh)"` from the repo root skips that spy, reaches
  `git add -A && git commit -m "agent: ${BRANCH}"`, and commits your entire
  work in progress under the message `agent:`. Recoverable with
  `git reset --soft`, but only if you notice.

- **Writing a source file from a Python one-liner on Windows converts it to
  CRLF**: text-mode `open(p, 'w').write(s)` expands `\n` to `\r\n`, and
  `.gitattributes` pins this working tree to LF. Read and write in BINARY mode
  when patching a file from a script (`open(p,'rb').read().decode()` /
  `open(p,'wb').write(s.encode())`), and check `git diff --stat` for the
  "CRLF will be replaced by LF" warning afterwards.

- **`praxis logs <task-id>` reads a captured log, not a live container**: the
  orchestrator removes each agent container seconds after it reports, so
  `docker logs` is already too late by the time you know you want it. The
  output is captured onto `agent_runs.logs` at that moment and surfaced at
  `GET /api/tasks/{id}` under `runs[].logs`; for three walkthroughs the only
  way to read it was curling that endpoint by hand. Two things the command has
  to get right, and both fail silently: **`markup=False`** on every printed
  line, because worker transcripts are full of bracketed tokens
  (`[PRAXIS PHASE] understanding`, `[main] INFO`) and rich reads a leading `[`
  as a markup tag, swallowing them or raising on an unclosed one; and an EMPTY
  `logs` value must be reported as "not captured" rather than printed as
  nothing, because "we could not read the container" and "the worker was
  silent" are different facts and only the second is alarming.

## Prerequisites the product diagnoses but does not cure

- **A host hook mounted into the container blocks every brain call, tunnel up or
  down.** `docker-compose.yml` mounts `~/.claude` into the orchestrator so
  `claude -p` can reach the host's subscription credentials, and that mount
  brings the host's Claude Code **hooks** with it. A hook whose detector assumes
  the host OS then fires inside the container on every call. The case that keeps
  happening is a VPN killswitch: on the host it checks whether the tunnel is up,
  in the container that check cannot succeed, so it refuses every prompt whether
  or not the VPN is actually running. `praxis doctor` reports `planner_cli` RED
  and quotes the hook's own message, so the diagnosis is precise.

  **The remedy is one line, and it goes in `.env.container`.** That file is a
  gitignored optional `env_file` declared on the orchestrator service, so
  everything in it is passed into the container as a real environment variable
  and the `claude` subprocess inherits it:

  ```bash
  echo CLAUDE_VPN_KILLSWITCH_OFF=1 >> .env.container
  docker compose up -d
  ```

  `required: false` means an install with no such file starts normally, and
  `Settings` uses `extra="ignore"`, so a variable in there that is not a
  settings field is carried to the process and simply not validated, which is
  exactly what a hook opt-out needs.

  It exists because the previous remedy, a literal under the orchestrator's
  `environment:` block in `docker-compose.yml`, dirties a TRACKED file. In a
  fresh clone that leaves a permanent local diff on the one file every operator
  is told not to hand-edit, which is a remedy nobody can follow twice. Editing
  that block still works and is still read, it is just no longer the
  instruction.

  Three ways to get this wrong, each of which fails silently:

  - **Putting it in `.env`.** This is where an operator reaches first and it
    does nothing. Compose reads `.env` on the HOST to substitute `${VAR}` in the
    compose file and passes nothing from it into the container on its own, so an
    opt-out there never becomes an environment variable of the orchestrator
    process and is never inherited by the `claude` subprocess. (The file is
    bind-mounted at `/app/.env` for the `env_drift` check, and pydantic-settings
    reads it, but that only fills `Settings` FIELDS; an unrecognised key is
    dropped by `extra="ignore"`.) The hook keeps firing and the symptom is
    unchanged. Nothing reports the mistake.
  - **`docker compose restart` instead of `up -d`.** `restart` does not re-read
    `env_file` or the compose file's substitutions. Same reason the `env_drift`
    check exists.
  - **Assuming it is only needed while the VPN is down.** It is not. The hook
    blocks the container in both states, which is what makes this look like a
    Praxis bug rather than a local hook.

  This cost time in five consecutive walkthroughs, because the diagnosis
  shipped and the cure did not: the doctor's hint named the cause and pointed
  at this page, and this page did not say what to do. The hint now states the
  remedy inline, `docker-compose.yml` carries it as a commented block beside
  `IS_SANDBOX` pointing at `.env.container`, and `tests/test_doctor_probes.py`
  asserts the hint names both where the fix goes and where it does not. Writing
  a correct diagnosis is not the same as shipping a fix, and a pointer to a page
  is only worth what the page says.

- **`gh pr view <branch>` resolves a branch to a PR regardless of state, and
  both agent entrypoints used it to decide whether to reuse a PR.** Plan
  branches are `plan/{date}-{plan_slug}` and agent branches are
  `agent/{task_slug}`, so re-submitting the same spec on the same day
  reproduces every branch name exactly. The lookup then returned the previous
  plan's already-MERGED PR, and the worker's real new commit was attached to a
  diff that had already landed. Every layer downstream then reported success on
  work that was not there: the review fetched the old merged diff and passed,
  `merge_pr` saw an already-merged PR and treated that as success (correctly,
  for its own purpose: a `gh pr merge` 504 can follow a successful merge), the
  task went to MERGED, and the commit never reached the plan branch. Measured in
  walkthrough #6: the agent branch was `ahead=1 behind=0` against the plan
  branch while the task pointed at a merged PR whose files belonged to the
  previous plan.

  The fix is the positive open-state lookup `_existing_integration_pr` already
  used for the integration PR, now in both entrypoints:
  `gh pr list --head "${BRANCH}" --base "${BASE_BRANCH}" --state open`. Only a
  POSITIVE open hit may skip creation; anything else, including a failure of the
  lookup itself, falls through to `gh pr create`, because treating a failure as
  "already open" would hide a real `gh` error forever. Note the emptiness of the
  OUTPUT is the signal and the exit status is not: `gh pr list` prints nothing
  and still exits 0 when nothing matches.

  **All three filters are load-bearing and each fails differently**, which is
  why `tests/test_entrypoint_pr_reuse.py` gives each its own scenario against a
  spy that models `gh pr list` rather than stubbing it: without `--state open` a
  merged or closed PR is reused; without `--base` the same agent branch's open
  PR against a PREVIOUS plan branch is reused, pointing the review at the wrong
  base; without `--head` a SIBLING leaf's open PR is reused, since every leaf of
  a plan targets the same plan branch. Proven by mutation, four mutants, each
  killed by a different test.

  **Deduplicating the slugs would not have fixed this**, and that is the reason
  the branch names were left alone. It removes one trigger and leaves the defect:
  a CLOSED PR on a branch this run rebuilds reaches the same state-blind lookup
  without any collision at all. With the lookup state-aware, a colliding branch
  name is harmless in the ordinary two-tier case, because `REUSING_BRANCH` is 0
  there and the branch is rebuilt from base and force-pushed.

- **The autonomous improvement loop used to propose work for a repository it
  never read.** `check_improvements` built its entire prompt from three strings,
  the project name, the repo URL and a plan path, and cloned nothing. Asked what
  to build next for `playground` (seven files of helper functions) it proposed
  hashing auth tokens with bcrypt, adding a transaction context manager to the
  Database class, adding a Content-Security-Policy header to the Caddyfile and
  rate-limiting the auth endpoints. None of those exist in that repo; every one
  describes Praxis itself. With no information about the target, the only
  codebase in the planner's context is the one it can see. Measured in
  walkthrough #7 (2026-08-21).

  This was never a prompt-tuning problem. The prompt was reasonable and had
  nothing to reason about. `core/repo_survey.py` supplies the missing input: a
  bounded, factual survey of the cloned tree, real paths plus a short excerpt of
  the files that say what the project is, reached through
  `BrainstormManager.survey_repo` (the same clone-read-delete shape as the other
  readers on that class).

  **It fails CLOSED, and that is the load-bearing half.** No readable repository
  means no proposal at all. Falling back to the name-only summary would
  reproduce the defect exactly on the days a clone fails, which is the worst
  possible time for it to return silently. Three conditions are equivalent and
  each has its own test: no reader configured, the read raised, or the survey
  came back blank. `build_repo_survey` never returns `""` (an empty repository
  yields a positive "no files" line), so a blank string means something failed
  upstream without saying so, and silence must not buy a proposal.

  Two things about the survey worth keeping. Its bounds ANNOUNCE themselves,
  because a silently truncated survey reads to the planner as a complete picture
  of a smaller project, which is a subtler version of the same failure. And
  `.git`, `node_modules`, `__pycache__` and friends are pruned, or the cap is
  spent on objects before a single source file is reached.

  **The approval gate was not the problem and must not be "fixed".** It held:
  `approval_gate` defaults true, so the plan parked at `pending` and nothing
  dispatched. Note while reading logs here that `create_improvement_plan` calls
  `activate_plan` unconditionally and only then flips the status back to
  `PENDING`, so the log says `Activated plan <id> with 5 tasks` even when the
  gate parks it. The status is correct everywhere a human looks.

- **A plan whose every task is a no-op has nothing to integrate, and that is a
  fact rather than an error.** Such a plan leaves its branch identical to base,
  so `gh pr create` refuses with `No commits between main and plan/...`, and
  attempting it anyway logged `Integration PR open failed` over a completely
  correct outcome. Same fact-versus-verdict split as `no_changes` one layer
  down: the absence of a diff is a fact and the orchestrator decides what it
  means. `_plan_branch_has_nothing_to_integrate` now makes that call BEFORE
  attempting creation.

  The check is POSITIVE and deliberately sufficient rather than necessary,
  exactly like `_existing_integration_pr`: only two known, equal SHAs skip
  creation. A branch that merely TRAILS its base also has nothing to integrate,
  is not detected, and falls through to the normal attempt. That is the safe
  direction. A `None` from either lookup means "could not ask", never "no
  commits".

  Both values must be actual `str`. That is not defensive clutter: "equal" is
  only meaningful for two answers, and any other object being equal to itself
  makes the check skip integration for EVERY plan while looking correct. It was
  measured doing precisely that against an `AsyncMock`, which returns the same
  sentinel for every call; seven existing tests went red and caught it.

## Surfaces that report the wrong thing while every layer says it worked

These are the traps where the code is correct, the tests are green, and the
operator is still told something false. Each was found by walking the product
as a newcomer, not by reading it.

- **A guard can be perfect and the fix still inert, because something upstream
  never hands it the rows.** `praxis pending` hid every autonomous improvement
  proposal. `plan_awaits_approval` classified them correctly and the CLI
  rendered them correctly, but `GET /api/approvals/pending` selected only plans
  with an open integration PR, so neither ever saw one. Reverting **just that
  `WHERE` clause** left 45 of 46 tests green. The lesson generalises: when a
  feature spans a query, a predicate and a renderer, the query is the seam that
  unit tests do not cover, and it is where a fix goes quietly inert. Test the
  layer that FETCHES, not only the layer that DECIDES.

  There is a second half to this one. Proposals are deliberately **not** added
  to `summarize_pending`'s `count`. That field feeds `digest_line`, which calls
  its items "PRs", and a proposal has no branch and no PR; folding it in would
  fix the invisibility by making the digest announce pull requests that do not
  exist. Two gates that both mean "a human must answer this" are still two
  gates.

  And a third half, which is the trap the second half set. Keeping a gate out
  of `count` is right; letting that keep it out of the SENTENCE was not.
  `digest_line` rendered `count` alone, so a queue holding nothing but a
  proposal, or nothing but a task blocked on an unanswered question, rendered
  `""`. That silence reached three surfaces at once: the MCP `pending_approvals`
  tool fell back to "No work parked at the merge gate" over a queue that was
  not empty, `poll_task` and `poll_plan` attached nothing, and the loop
  published an `approvals_digest` event whose own sentence mentioned none of
  what triggered it, because `should_publish_digest` fires on
  `outstanding_count` (all three gates) while the renderer read `count` (one).
  `digest_line` now emits one clause per gate, joined with `"; "`, and
  `oldest_hours` stays attached to the PR clause because it spans parked tasks
  and plans only. The general shape: when a decision function and a renderer
  read DIFFERENT fields of the same payload, the renderer is where the
  disagreement becomes a lie.

- **rich's `max_width` is a MAXIMUM, not a minimum, and a table will shrink a
  column below it.** `praxis tasks` set `max_width=36, overflow="fold"` on a
  uuid column and still folded every id across THREE rows: with five columns
  competing for an 80-column console, rich allocated the id 16 characters.
  Raising it to `min_width=36` does not help either, it only moves the damage,
  pushing Status and Attempt off the right edge entirely. The working pattern,
  which `pending` and `plans` already used, is to keep the table narrow and
  print the id below it on its own plain line: rich's word-wrap only breaks on
  whitespace, so a token containing none survives contiguous at any width.

  **The test that was supposed to catch this passed for five walkthroughs.** It
  pinned `COLUMNS=160` and then joined every line before asserting, so a
  three-way fold read as success. An id is copied a LINE at a time; assert
  contiguity on a single line, at the width a real terminal has.

- **On Windows, redirected CLI output is not UTF-8, and rich's ellipsis makes it
  invalid.** Attached to a console, Python writes through `WriteConsoleW` and
  the declared encoding is irrelevant, so everything looks fine interactively.
  Redirected, it falls back to the locale encoding, which is cp1252. rich
  truncates a too-wide cell with U+2026, cp1252 encodes that as the single byte
  `0x85`, and `0x85` is not valid UTF-8. The symptom is that
  `praxis tasks | grep ...` answers **"Binary file (standard input) matches"**
  and matches nothing, so every table the CLI prints becomes unpipeable the
  moment one value is long enough to truncate. `cli/main.py` reconfigures
  stdout and stderr to UTF-8 at import; it is a no-op for the interactive case.

- **An existence check is not a capability check.** The `worker_endpoint` red
  told the operator to "switch preset with `praxis config`". `praxis config` is
  a registered command GROUP, so any test asking "does this verb exist" passes.
  Run it and it prints its own help and changes nothing; no subcommand of it can
  change a worker preset. The rule that actually catches this is structural:
  naming a command GROUP with no subcommand is a dead end by construction,
  because a bare group is not runnable. `tests/test_doctor_hints_name_real_verbs.py`
  enforces both that rule and plain verb existence, and carries two
  guard-the-guard cases, since a regex matching nothing and an empty verb set
  would each make the whole file pass vacuously.

  Group names must be read off the sub-app's own `info`. `add_typer(config_app)`
  with no explicit name leaves `group.name` a `DefaultPlaceholder`, not a
  string, so reading `group.name` alone silently yields a verb set containing no
  groups at all.

- **A CLI can be stricter than the API it wraps, and the extra strictness is
  invisible.** `praxis add-project` demanded a model as a required positional
  argument. The API had always allowed `model_name` to be null and fall back to
  the worker preset. Under the shipped default preset the correct value lives
  only in the settings YAML and is printed by no command, so the newcomer was
  ordered to supply a value they had no way to look up. The same shape applied
  to `harness`: the API accepted and validated it on both create and update
  since the registry landed, and the CLI simply never offered a flag, leaving
  the setting that decides which harness does the typing reachable only by curl.

  When adding an optional flag here, send an absent key as **absent**. A null in
  the payload is not the same as an omitted one: it writes an explicit null onto
  the project row and stops it tracking the preset.

- **`test_config_path` trips on a COMMENT, and that is the correct behaviour.**
  It greps every module under `src/` for the literal settings-YAML path, because
  one resolver owning that path is what keeps a fixed bug fixed, and a grep
  cannot tell a real read from prose. A comment mentioning the path fails the
  suite. Reword the comment; never weaken the gate. The direction matters: strip
  comments in a gate only when prose could SATISFY it (a false negative, which
  is dangerous), never when prose merely TRIPS it (a false positive, which is
  safe).

- **A whitespace-only `verify_cmd` reported `passed` having executed nothing.**
  The worst member of this family, because the thing it lied about was the
  evidence itself. Every read site guarded with a falsy check, which correctly
  collapses `""` and `None` onto "not configured". But `"   "` is TRUTHY: it
  slipped all of them, reached `asyncio.create_subprocess_shell`, and a blank
  shell command exits 0. Measured directly before the fix,
  `run_verify(d, "   ")` returned `(True, "")`. The gate then logged
  `verify gate passed`, and on the no-changes path it wrote the permanent,
  specific, false sentence *"the repository already satisfied this task (verify
  passed on plan/x)"* onto the task.

  `core/verify_gate.normalize_verify_cmd` is now the single source of truth,
  and the API refuses the value outright with a 422 so it cannot enter a new
  database. The placement of the runtime half is the part worth remembering:
  there were FOUR raw reads, not the three that are easy to find by grepping
  for `project.get("verify_cmd")`. The fourth is `on_plan_completed`. Both it
  and the named whole-plan read funnel through `_verify_plan_branch`, so
  normalizing inside that funnel is what makes it impossible to leave one
  caller behind. Normalizing the three obvious sites would have left the
  integration gate still lying.

  A blank value had a second victim in the same family: `acceptance =
  leaf_check or project_check` in the dispatch mixin treated `"   "` as a real
  check, so a blank column could win the acceptance slot and be handed to the
  worker as the leaf's entire definition of done.

  `run_verify` now raises `ValueError` rather than shell a blank command. That
  is deliberate and it is not defensive clutter: all callers normalize first,
  so it can never fire in production, and it exists so a FUTURE call site that
  forgets to normalize fails loudly instead of quietly reporting a pass. It was
  not downgraded to `(False, ...)`, which would report a Praxis bug as a failing
  verification and burn the task's retry budget.

- **`loop_interval` was a documented, settable key that had never done
  anything.** `main.py` started the loop with `run_loop(stop_event)` and no
  interval, so the configured value never arrived and every install ran at
  `run_loop`'s own hardcoded 5s. A knob that silently does nothing is the same
  lie as a false status line, just in configuration form. The tell was visible
  in the source the whole time: the settings layer said 30 and `run_loop` said
  5, two different answers to one question, and neither was reachable by
  configuring it.

  The three were reconciled on **5**, not on the 30 the settings layer claimed.
  This is the judgement worth recording. The purpose of the fix was to stop a
  knob lying, and 5s is the only value any install has ever actually run, across
  eight newcomer walkthroughs. Adopting 30 would have shipped a silent sixfold
  increase in dispatch, review and merge-gate latency as a side effect of a
  transparency fix, which is a performance regression nobody asked for and
  nobody had measured. A non-positive configured value is floored with a warning
  rather than honoured, because passing 0 to `asyncio.wait_for` busy-spins the
  loop.

- **The doctor spent a real model call proving something other than what the
  loop runs.** The planner check hardcoded the provider name `"claude"` and ran
  `claude -p` with no `--model`, so the subscription CLI answered on its own
  default. The row then went green about a model the loop would never call. It
  is a particularly expensive member of the family: the check costs money, and
  the operator reads it as the authoritative answer to "is my planner working".

  It now resolves `plan_spec` through `EffectiveSettings.call_site_chain`, the
  exact bound method `main.py` hands `LLMRouter`, so doctor and loop cannot hold
  different opinions about what the planner is. It executes through
  `llm_router.build_argv`, so the flags the probe runs are the flags the loop
  runs, including `--effort` (which is the flag's real name). Resolution must go
  through `call_site_chain` and not `call_site_config`: only the chain honours
  the YAML role chain shadowing `CALL_SITE_DEFAULTS`, and a probe built on the
  defaults map reports on `claude-sonnet-4-6` no matter what the operator
  configured.

  Three consequences to keep. The row NAMES the provider and model it probed,
  because a green that does not say what it checked is how this survived. The
  cache is keyed by the whole resolved target rather than the provider name, or
  a reconfigured planner inherits the previous model's verdict. And a `local`
  planner is AMBER, not red: it is a working, supported planner with no binary
  anywhere, so probing PATH for it invents a red about a correct install.

- **A copyable line printed only SOMETIMES reads as a working one.** Run #8
  fixed the folded uuid in `praxis tasks` and `praxis pending` and left
  `praxis plans` behind, and the reason it went unnoticed for two more runs is
  the interesting part: `plans` DID print a copyable line, but only for a plan
  with an open integration PR. A pending, active or already-integrated plan
  got none, and its uuid folded across two rows at 80 columns. The surface
  looked fixed in exactly the state you check it in after fixing it, and was
  useless in the three states you actually meet first. `tasks` even carried a
  comment asserting that `plans` "already does" this. When a fix is conditional,
  the test needs one scenario per branch of the condition, or the passing branch
  masks the rest.

- **Help text is a status line too.** `praxis add-project --harness` said "Omit
  to use the registry default". `POST /api/projects` resolves
  `body.harness or settings.default_worker_harness`, so with the shipped
  `gemini-agy` preset an omitted flag yields `agy` while `default_harness_id()`
  is `opencode`: the help named the wrong one of the two available answers. It
  is the same family as a false status line, and it costs more than it looks
  like, because the operator only discovers the disagreement after a worker has
  run in a harness they did not choose.

  The guard for this one was itself inert on the first attempt, which is worth
  recording. `runner.invoke(app, ["add-project", "--help"])` renders through
  rich, which WRAPS a long option help string across panel rows and draws a
  border on each row, so the rendered text of "use the registry default" is
  literally `use the registry | | default`. A plain
  `" ".join(output.split())` leaves the borders in, the phrase never matches,
  and the assertion passes whether the help is right or wrong. Strip the
  box-drawing glyphs before collapsing whitespace. This is the SAFE direction
  of the strip-vs-keep rule: removing borders can only make a bad string easier
  to find, and cannot let prose satisfy a check that should have failed.


## A fix applied to the doctor is not applied to the product

Walkthrough #11's only new defect. `praxis status` printed `Opus: available` and
`GET /api/status` reported `agent_model.name = claude-opus-4-8`, while `praxis doctor`, two
commands away on the same install, correctly said `claude-sonnet-4-6`. The same endpoint
reported a working agy worker as `{"name": "unknown", "connected": false}`.

Both halves had ALREADY been fixed on the doctor. Run #9 taught its planner row to resolve
through `EffectiveSettings.call_site_chain` precisely because a YAML role chain SHADOWS the
per-call-site config, and its worker row already answers "not applicable: this harness does
not use an OpenAI endpoint". The fix landed on the doctor and on nothing else, and
`/api/status` went on reading the legacy `agent_model` setting (still defaulting to
`claude-opus-4-8`) and probing LM Studio regardless of the configured harness.

The doctor is where diagnosis lives, so a correction about what an install actually runs
lands there naturally and feels complete. But every doctor row describes a fact that some
other surface also reports: a status endpoint, a CLI verb, the dashboard. Fixing the
diagnostic while leaving the product's own answer wrong is the worst version of the split,
because the two disagree and the one a user reads first is the wrong one.

**When a doctor row is corrected, grep for what else answers the same question and fix those
in the same commit.** A subject re-sweep derived from the session's own diff cannot catch
this, because the doctor-only fix changed nothing in that diff.

## The progress handover read a repository that was not there

`_build_worker_bible` called `branch_commit_log(".", base, branch)`. Inside the orchestrator
container `.` is `/app`, which has no `.git` and no clone of the target repo anywhere on the
filesystem, so the call raised on every dispatch and was swallowed by a bare
`except Exception: commits = []`. Under a bare `uvicorn` from the repo root it was worse than
empty: `.` is the Praxis repo, so the refspec resolved against Praxis's own branches.

The consequence was silent and expensive. Every re-dispatched worker was handed a PROGRESS
section with every checklist item unticked, i.e. told that nothing had been done, while the
Static Bible told it that committing per checklist item is "how progress is tracked across
restarts". So the mechanism the worker was instructed to rely on had never once worked, and
the failure mode was a worker redoing completed work.

Nothing caught it because every test mocked the reader to `[]`, which is exactly what the
broken production path also produced. That is the shape to watch for: **a mock whose return
value is indistinguishable from the bug**.

Now `GitOps.remote_branch_commit_log` reads the branch from the REMOTE via
`gh api repos/<slug>/compare/<base>...<branch>`, needing no clone, and `render_handover`
distinguishes three states that used to render identically:

- `commits == []` -> `# PLAN (no commits on this branch yet)`
- `commits is None` -> `# PLAN (commit history unavailable; verify before redoing work)`
- non-empty -> `# PROGRESS (resume here)`

"Nothing was done" and "I could not find out what was done" are different facts, and
rendering the second as the first is what tells a resumed worker to start over. On attempt 1
the branch does not exist on the remote yet, so an unreadable history there IS "no commits";
on a re-dispatch it is genuinely unknown. The dispatcher decides which by the attempt number.

## "Nothing to commit" is a fact, and `commit_and_push` now returns it

`git commit` exits 1 on a clean tree. Under `check=True` that raised `CalledProcessError`,
and the two callers that write operator-authored text propagated it as a bare 500: saving a
spec in the dashboard editor without editing it, and approving a context draft the planner
had produced empty (which is the ordinary outcome when the planner is rate limited or
hook-blocked, since `ContextSync.draft` ignores the `claude -p` return code entirely).

`git_ops.commit_and_push` now returns `bool`: True when it committed and pushed, False when
the index was already clean. Emptiness is decided by `git diff --cached --quiet`, which
answers in exit codes rather than in prose and so cannot be defeated by a locale that
translates "nothing to commit"; any other exit code falls through to attempting the commit,
so a real failure still raises. `write_and_commit` and `ContextSync.approve` report
`status: "unchanged"` rather than claiming a commit that is not in the repo.

## The repo-access failure family is decided in ONE place

Every route that reaches a target repository ends at `clone_with_token` or
`commit_and_push`, both `subprocess.run(..., check=True)`. So they all fail identically: a
`CalledProcessError` whose `str()` is only `Command '[...]' returned non-zero exit status
128.` while the reason a human can act on sits unread on `.stderr`.

Six routes handled that and four did not, so the four answered a bare 500 for an install
that was merely missing a credential. `src/orchestrator/api/repo_errors.py` now holds the
single handler: `guard_repo_access(awaitable, what=...)` maps `FileNotFoundError` to 404
(the document is not there, which is the caller's mistake, not the remote's) and everything
else to 502 carrying the decoded stderr. **Use it from any new route that touches a repo.** A
remedy that lives in six copies is a remedy that will be corrected in five of them.

Related, and the same rule one layer up: `POST /api/projects/{id}/plans` answered 502 for a
missing GitHub credential where `POST /api/projects` answered 422 with the remedy. 502 says
"upstream is broken, retry"; a missing credential is a permanent configuration fact that no
retry heals. Both now answer 422.

## A CRLF working copy makes a mutation harness lie

A harness reported EIGHT working fixes as "STILL GREEN, inert guard". Every one of them was
present and correct. The anchors were written with `\n`, the file had CRLF, and every
multi-line anchor matched zero times.

Two traps compound:

1. `pathlib.write_text` translates `\n` to `\r\n` on Windows, so every file-based patch
   script silently converts its target. Eight source files were converted in one session
   before anyone noticed.
2. **`pathlib.read_text` universal-newlines the file back**, so `"\r\n" in path.read_text()`
   reports a CRLF file as clean. A hand-written CRLF guard is defeated by the very API it
   uses to check.

`.gitattributes` pins this tree to LF and normalizes on commit, so git hides the damage
entirely and only the local working copy is affected, which is exactly where mutation
harnesses run.

Detect with `read_bytes()`, normalize with `.replace("\r\n", "\n")` before matching, and
write back in the file's ORIGINAL style so the harness does not convert the file it is
testing. "The guard did not go red" is the one conclusion a mutation harness exists to
produce; reaching it by accident destroys the evidence the whole method rests on.

## The suite under FORCE_COLOR is not the suite

rich colorizes when it believes the stream can take it, and that belief is platform
dependent: the Linux CI runner colorizes typer help where the Windows runner does not.
Running the suite as `FORCE_COLOR=1 TERM=xterm-256color pytest` turned four guards red that
pass uncoloured, because an escape lands INSIDE the phrase being matched (`abc-\x1b[1;36m123`
never contains `abc-123`).

`tests/cli_text.py` is now the single helper: `strip_ansi` when line structure matters
(a copyable command must survive on ONE line), `plain` for prose assertions, `on_one_line`
and `flat` for the common cases. Order is load-bearing and identical in all of them: ANSI
first (an escape can sit mid-word), box glyphs second, whitespace last.

It exists because there were already TWO private copies and they had DRIFTED: one stripped a
hand-listed string of box glyphs, the other the whole `U+2500-U+257F` block, so the same
phrase was matchable in one file and not the other.
