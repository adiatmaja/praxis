# Plan: field-report remediation (2026-08-25)

Source: `C:\working-space\.mtel-ui-audit\PRAXIS-HANDOFF-2026-08-25.md`, a field report
from a first-time praxis user who drove one real dispatch end to end (a screenshot
UI/UX audit of `mtel-mockup`, `agy` harness, local bare-repo git backend, Docker
Desktop for Windows). The work came back good; the tooling around it cost an
afternoon.

Decisions taken against the report, with reasons, are in "Rejected / deferred" at
the bottom. Read that before assuming an unimplemented proposal was forgotten.

## Parallel Execution Map

| Wave | Tasks | Why they are safe together |
|------|-------|----------------------------|
| 1 | T1, T2, T3 | Disjoint file sets. T2 reads a JSON field T1 creates; the field names are fixed in this plan so both can be built against the contract. |
| 2 | T4, T5 | T4 needs T3's env-var names; T5 needs T1's `opus_bridge.py` edits. Disjoint from each other. |
| 3 | T6 | Docs and repo hygiene only. |

---

## Task 1: a permanent planner failure must not look like progress (report §2)

### The defect

`core/orchestrator.py:244 plan_and_activate` wraps `_load_spec_text` in a
try/except that records the reason on `plans.error` and moves the plan to
`FAILED`. It does **not** wrap `self._opus.plan_spec(...)` (line 270). When the
planner returns prose instead of JSON, `_extract_json`
(`core/opus_bridge.py:284`) raises `ValueError`, that escapes to `run_once`'s
per-plan guard, and the plan stays `PENDING` and is retried on every tick,
forever. From outside, `praxis plans` says `active` and `praxis tasks` says "has
no tasks yet", which is exactly what both say during a healthy decomposition.

The field case: the planner ran `claude -p` with cwd `/app` while the repo was at
`/run/desktop/mnt/host/c/...`, so the model replied with a permission request in
prose. That will never become JSON on retry.

### Requirements

**1a. Classify the extraction failure structurally, not by keyword.**

In `core/opus_bridge.py`, replace the bare `ValueError` from `_extract_json` with
two exception types in a small hierarchy (put them in `opus_bridge.py` next to
the method; do not create a new module for two classes):

- `BrainResponseError(ValueError)` - base, so existing `except ValueError`
  callers keep working. Carries `.raw` (the full raw response) and a `.excerpt`
  property capped at 500 characters.
- `BrainProseResponseError(BrainResponseError)` - raised when the response
  contains **no JSON at all** (neither a fenced block nor a `{...}` span). The
  prompt says "Respond with ONLY valid JSON", so a response with no JSON in it is
  a refusal, a question, or a permission request. None of those become JSON on
  retry. **Permanent.**
- `BrainMalformedJsonError(BrainResponseError)` - raised when a JSON span was
  found but `json.loads` rejected it. **Transient**, worth a retry.

Do NOT pattern-match English prose for "permission". The structural rule covers
the observed case and cannot rot.

`_extract_json` currently lets `json.JSONDecodeError` propagate from the two
`json.loads` calls; wrap those so a malformed span raises
`BrainMalformedJsonError` with `raise ... from`.

**1b. Bound the retry and record the reason.**

Add migration 11: `ALTER TABLE plans ADD COLUMN plan_attempts INTEGER NOT NULL
DEFAULT 0`. Follow the existing idempotent `PRAGMA table_info` guard pattern of
`_migration_0003_plan_error` in `src/orchestrator/database.py`. Schema version
goes 10 -> 11; update the pin in `tests/test_migrations.py` in the same commit.

Add `TaskQueue.bump_plan_attempts(plan_id) -> int` returning the new count, and
`TaskQueue.reset_plan_attempts(plan_id)`. Reset on a successful activation so a
plan that recovers does not carry a stale count.

In `plan_and_activate`, wrap the `plan_spec` call:

- `BrainProseResponseError` -> terminal on the **first** occurrence. Set
  `plans.error` to a message that names what happened and quotes the excerpt,
  move the plan to `FAILED`, publish `plan_failed`, log at ERROR.
- any other `Exception` -> bump `plan_attempts`; if the new count reaches
  `_MAX_PLANNING_ATTEMPTS` (module constant, value 3), set `plans.error` and go
  `FAILED` the same way; otherwise record the error on `plans.error` and leave
  the plan `PENDING` so the next tick retries.

The message written to `plans.error` is the whole point of this task. Hold it to
the standard of `praxis doctor`: name what happened, name the likely cause, and
name the remedy. For the prose case, that means something along the lines of "the
planner answered in prose instead of JSON, which means it refused or asked a
question rather than failing; a repository path outside the orchestrator's
working directory is the known cause", plus the excerpt.

**1c. Fix the root cause: give the planner a repo it can read.**

`plan_spec` today receives `repo_url` as a **string interpolated into the
prompt** and runs with the orchestrator's default cwd (`/app`). The model is
asked to reason about a path it cannot open.

Fix it the provider-agnostic way: clone the repo into a temp dir **under
`/app`** and pass that as `cwd`, mirroring what the brainstorm path already does
at `/tmp/praxis-brainstorm/<uuid>`. Reuse the existing clone helpers
(`core/git_ops.clone_with_token` for remote, the local backend's `checkout` for a
local path) rather than writing new ones; `core/orchestrator_review.py:436` and
`:1645` are both worked examples of the pattern.

Do **not** use `claude --add-dir`: that is a `claude`-only flag and the router is
provider-agnostic across `claude`/`codex`/`agy`/`local`.

Degrade, never wedge: if the clone fails, log at WARNING and plan without a
`cwd`, exactly as review already degrades to diff-only.

**1d. Surface it on the API.**

Expose `error` and `plan_attempts` on `PlanResponse` in
`src/orchestrator/models/schemas.py` so the CLI (Task 2) and `poll_plan` can read
them. Field names are fixed by this plan: **`error`** and **`plan_attempts`**.

### Files

`src/orchestrator/core/opus_bridge.py`, `src/orchestrator/core/orchestrator.py`,
`src/orchestrator/database.py`, `src/orchestrator/core/task_queue.py`,
`src/orchestrator/models/schemas.py`, `tests/test_migrations.py`, plus tests.

### Tests that must exist

- A prose response fails the plan on the FIRST tick, with the excerpt on
  `plans.error`.
- A malformed-JSON response retries and only fails on the third attempt.
- A plan that succeeds on attempt 2 has `plan_attempts` reset to 0.
- Migration 11 is idempotent and the schema version pin is 11.
- The clone-failure path still plans (degrades, does not raise).

---

## Task 2: CLI honesty on slow and failed paths (report §2.4, §5, §7, §11)

### 2a. `praxis plans` must distinguish decomposing from wedged (§2.4)

`praxis plans` prints a bare status cell. A plan that is `active`/`pending` with
`plan_attempts > 0` or a non-null `error` must say so. Target rendering, on the
status cell or the copyable line beneath the table:

```
active (planning, attempt 2/3; last error: could not extract JSON ...)
```

Read `error` and `plan_attempts` from the plans API response (Task 1 adds them;
treat both as optional so the CLI works against an older server).

`_status_cell` is the existing helper. Keep ids off table columns and on their
own copyable line, per this project's rule.

### 2b. `praxis submit` must not print a traceback on an expected slow path (§5)

`submit` posts to `/api/projects/{id}/plans` inside `_client()` at the 60s
default timeout. On a large repo that endpoint clones to commit the spec doc and
exceeds it. The user got a raw `httpx.ReadTimeout` traceback **and the plan had
in fact been created**.

Catch `httpx.ReadTimeout` (and `httpx.ConnectTimeout` separately, which means the
opposite and must not be conflated) around the POST and print a message that says
the plan may well exist and how to check:

```
Timed out waiting for the server, but the plan may have been created:
the spec is committed to the repository before the response returns.
Check with:
  praxis plans <project-id>
```

Use `_copyable` for the command line. Exit non-zero, since the CLI genuinely does
not know the plan id.

### 2c. Detect Git Bash path mangling (§7)

On Windows, Git Bash rewrites a leading `/` argument into an MSYS path:
`praxis add-project ... /run/desktop/mnt/host/c/...` arrives as
`C:/Program Files/Git/run/desktop/mnt/host/c/...` and 422s with a confusing
"path does not exist".

Add a check in the CLI where a `repo_url`/path argument is submitted: if the
value starts with the MSYS install prefix (detect `C:/Program Files/Git/` and
`C:\Program Files\Git\`, case-insensitively, and also honour `MSYSTEM` being set
if that is cheaper to test), print, before or alongside the server error:

```
Your shell rewrote this path. Git Bash converts a leading '/' into an
MSYS path. Re-run with MSYS_NO_PATHCONV=1 prefixed to the command.
```

Put the detection in one small helper so it is testable without a subprocess, and
call it from the failure path so a correct path never sees the message.

### 2d. README: `praxis` is not on PATH (§7)

One line early in the README's setup section stating that after `praxis init` the
command is invoked as `uv run praxis ...` from the praxis directory.

### 2e. MOVED to Task 6d. The location in this plan was wrong.

This sub-task originally said the string lived in `src/cli/` and was reachable by
grepping `agent/` there. It is not, and `add-project` has no `branch` argument at
all. Both the implementer and the reviewer independently established that the
string is the docstring on `DispatchRequest.branch` at
`src/orchestrator/models/schemas.py:650-653`, outside this task's declared file
list, and the implementer correctly declined to edit a file its task did not
name. Retargeted as Task 6d rather than silently dropped.

### Files

`src/cli/main.py`, `README.md`, plus tests.

### Tests that must exist

- The plans table shows the attempt/error suffix when the fields are present and
  does not when they are absent.
- `submit` on a `ReadTimeout` prints the recovery line and exits non-zero, with
  no traceback.
- The MSYS-prefix helper matches both slash styles and does not match a normal
  path.
- Assert all CLI output through `tests/cli_text.py`, never raw `result.stdout`.

---

## Task 3: the local-repo path namespace split (report §3, §6-compose)

### The defect

`core/preflight._preflight_local` (`preflight.py:171`) validates `repo_url` with
`Path.exists()` **inside the orchestrator container**.
`core/agent_manager.local_repo_volume` (`agent_manager.py:163`) then uses that
same string as a Docker bind-mount **source**, which the daemon resolves in the
host / Linux VM namespace. On Linux the two coincide. On Docker Desktop for
Windows nothing satisfies both, and `docs/configurations.md` says only "any path
the container can reach", which is true of the check and false of the mount.
`core/orchestrator_review.py:1564` already carries a comment naming the split.

### 3a. Make the 422 name the real constraint

`_preflight_local`'s `MISSING_REPO` message currently reads "local repository
path does not exist: {path}". Extend it to name the constraint and the Docker
Desktop remedy, at doctor-grade specificity: the path must resolve **identically**
inside the orchestrator container and on the Docker host, because one is checked
here and the other is used as a bind source when the worker spawns; on Docker
Desktop that means `/run/desktop/mnt/host/<drive>/...` mounted at the same path
into the orchestrator.

### 3b. Ship the mount

**The subtlety, and it is the reason 3.2 as written in the report does not
work:** an identity mount (`$X:$X`) cannot work on Docker Desktop, because the
daemon's bind **source** must be the Windows path (`C:/...`) while the
orchestrator must **see** the VM path (`/run/desktop/mnt/host/c/...`). Those are
different strings. So this needs two settings, not one:

- `LOCAL_REPOS_HOST_PATH` - the bind **source**, in the host namespace. On Linux,
  the same absolute path. On Docker Desktop for Windows, `C:/Users/.../repos`.
- `LOCAL_REPOS_PATH` - the mount **target** inside the orchestrator, and also the
  prefix a project's `repo_url` must use. On Linux, equal to the above. On Docker
  Desktop, `/run/desktop/mnt/host/c/Users/.../repos`.

Add to `docker-compose.yml` under `orchestrator.volumes`, gated so an unset value
mounts nothing. Compose has no conditional volumes, so use the documented
degenerate form: default both to a harmless existing path, or use an
`x-`anchored optional include. Pick whichever keeps `docker compose config` valid
with the vars unset and **verify it by running `docker compose config`**, which
is the acceptance test.

Both keys go in `.env.example` with the comment explaining the two namespaces.
Follow the existing bare-pass-through convention for worker preset vars: never
`${VAR:-default}` for a key the mounted YAML also names, per this repo's gotcha.

### 3c. Container name escape hatch (§6, compose half only)

Change `container_name: orchestrator` to
`container_name: ${PRAXIS_CONTAINER_NAME:-orchestrator}`. The default is
unchanged on purpose: every doc and debugging recipe in this repo says
`docker logs orchestrator`. Add the key to `.env.example`. The diagnosis half of
§6 is Task 4.

### 3d. Docs

- `docs/configurations.md`, "no-GitHub-credential" / local-repo section: state
  the two-namespace constraint, the two env vars, worked values for Linux and for
  Docker Desktop, and that this configuration is exercised on Linux and is
  therefore less travelled on Docker Desktop.
- `docs/deployment.md`: the same mount, where operators look for volumes.

### Files

`src/orchestrator/core/preflight.py`, `docker-compose.yml`, `.env.example`,
`docs/configurations.md`, `docs/deployment.md`, plus tests.

### Tests that must exist

- The `MISSING_REPO` message names the two-namespace constraint (assert on the
  substance, not the whole string).
- `docker compose config` is valid with the new vars unset and with them set.
  There is an existing pattern for compose validation in the docker workflow;
  reuse it rather than inventing one.

---

## Task 4: doctor rows for the three things that cost the afternoon (report §3.3, §4.1, §6)

`praxis doctor` is, per report §8, the best thing in the project: its failure text
names causes, remedies, and why the obvious fix is wrong. **Hold these three rows
to that standard.** Decision logic goes in `core/doctor_probes.py`, fact gathering
in `api/doctor.py`, which is the existing split.

Every doctor row in this project has a status endpoint / CLI verb / dashboard twin
answering the same question. Grep for them and fix in the same commit; a doctor
fix that never reaches the product is a known repeat defect here.

### 4a. Local-repo path round-trip (§3.3)

For each project whose `repo_url` is local (`git_backend.is_local_repo_url`),
check that it resolves inside the container **and** that it sits under
`LOCAL_REPOS_PATH` when that is configured. Red when the path is missing; amber
when it resolves but is outside the configured mount, because that is the
configuration that passes preflight and then fails at spawn. Name the two
namespaces in the remedy.

### 4b. Probe agy auth instead of documenting it (§4.1)

The wrong `agy login` command was already corrected in `fe1f745` across
`harnesses.py`, `agent_manager.py`, `docs/deployment.md` and the README. Replace
the remaining reliance on documentation with a probe.

Run `agy models` in a throwaway container mounting the creds volume and read the
answer:

- "Please sign in to view available models" (or any sign-in prompt) -> **amber**,
  with the current instruction: `docker run --rm -it -v
  praxis-gemini-creds:/home/agent/.gemini --entrypoint bash agy-agent:latest -c
  'agy'`. There is no `login` subcommand; launching the CLI with no arguments is
  what starts the OAuth flow.
- a model list -> **ok**, and report the count.
- anything else -> surface it **verbatim**. Do not summarize an unrecognized
  answer into a verdict.

Cost control, because doctor is documented as read-only and fast: run this row
**only** when an agy harness is actually in play (a project configured with the
`agy` harness, or the default worker preset resolving to agy) and only when the
`agy-agent` image exists locally. Otherwise report "not probed" with the reason,
the same shape `codex`/`agy` planners already use in the planner row. Cache it
alongside the existing doctor cache.

Do **not** validate a project's `model_name` at `add-project` time. That would
make project creation spawn a container and fail on a quota or network hiccup.
If the probe returns a model list, it may note that the configured model is not
in it - as a doctor row, not a gate.

### 4c. Account switch is undocumented and bit a real user (§4.1)

Re-authenticating with a **different** Google account requires wiping the creds
volume, not just re-running sign-in. Document in `docs/deployment.md` next to the
agy setup anchor:

```
docker run --rm --user root -v praxis-gemini-creds:/home/agent/.gemini \
  --entrypoint bash agy-agent:latest \
  -c 'rm -rf /home/agent/.gemini/* /home/agent/.gemini/.[!.]*; chown -R agent:agent /home/agent/.gemini'
```

Say why: a weekly quota running out mid-session is the real reason this comes up.

### 4d. Container ownership (§6, diagnosis half)

Read the running container's `com.docker.compose.project` label and compare it to
this directory. When a container named by `PRAXIS_CONTAINER_NAME` (default
`orchestrator`) exists but its label is not this checkout, say so by name. This
extends the build-stamp row added in run #13, which already reports the host dir
the server was started from; put the new fact wherever it reads best next to
that, rather than duplicating it.

The consequence to name in the remedy: two checkouts silently swap databases,
because `container_name` is global to the daemon.

### Files

`src/orchestrator/core/doctor_probes.py`, `src/orchestrator/api/doctor.py`,
`docs/deployment.md`, and the status/CLI twins you find by grepping, plus tests.

### Tests that must exist

- Each new row's decision function, at every outcome including "not probed".
- The agy probe is not run when no agy harness is in play.
- An unrecognized `agy models` answer is surfaced verbatim, not bucketed.
- `tests/test_doctor_hints_name_real_verbs.py` still passes: a hint must name a
  verb that can do the job, never a Typer group.

---

## Task 5: give the review gate a signal it can actually see (report §1.1)

### The defect

The worker changed `.s-alert` from `display: flex` to `display: block`. That was
correct. Three hundred lines down the same stylesheet, `.mtel-demo-banner`'s
`justify-content: center` went silently inert, and a compliance disclaimer moved
from centred to left-aligned on three pages. The review passed and **every
statement it made was true**. The defect was not in the diff.

### 5a. Blast radius in the review prompt

`review_task` (`core/orchestrator_review.py:436-552`) already holds both the diff
and a real clean checkout of the PR head, and already passes `cwd=checkout` to
the reviewer. Everything needed is in hand.

New module `src/orchestrator/core/blast_radius.py`:

- Extract identifiers from **changed lines** of the diff, deliberately scoped:
  CSS selectors (`.foo`, `#foo`) from changed rule heads, and definition-site
  identifiers from changed lines matching `def X`, `class X`, `function X`,
  `export ... X`, `const X =`. Do not attempt a general cross-language symbol
  extractor; that is a rabbit hole and this scope covers the observed class.
- Count repo-wide occurrences of each in the checkout.
- Return a small ordered result: the top N (N = 10) identifiers by count,
  excluding counts of 1 (an identifier used only where it was defined carries no
  signal).

**Hard constraints, because this runs on every review:**

- **Fail open.** Any exception, and the review proceeds with no blast-radius
  section. A review must never wedge on a repo walk. Log at WARNING.
- **Bounded.** Skip binary and non-text files by extension and by a null-byte
  sniff, cap total files scanned and total bytes read, and cap wall-clock. The
  field repo was 70 MB, mostly PNGs.
- Pure functions where possible so the extraction and the counting are testable
  without a filesystem.

Wire it into `review_task` and into `OpusBridge.review_diff` as a new **optional**
keyword argument, rendered into `REVIEW_PROMPT_TEMPLATE` as its own section. When
absent, the section must degrade to a neutral line rather than an empty heading.
The prompt line the report suggests is the right register:

> This change modifies `.s-alert`, which occurs 373 times in this repository.
> Consider what else depends on the old behaviour, including code the diff does
> not show.

Keep the worker-prompt register rules in mind: this is a **brain**-facing prompt,
not a worker one, so the floor-model register does not apply here.

### 5b. Make the review state its own scope

The report's strongest general point: "a check that cannot fire is worse than no
check", and a green that reads as verification when it is only a diff summary is
actively misleading.

The review already knows exactly what it did and did not observe: whether a
checkout was available or it degraded to diff-only, whether `verify_cmd` ran,
passed, was skipped, or is not configured, and now the blast-radius counts. Emit
that as a short scope statement carried with the verdict to the merge gate, so a
human approving sees what the green covers. There is existing vocabulary for the
skipped cases (`_SKIP_CHECKOUT_UNAVAILABLE`, `_SKIP_NO_VERIFY_CMD`,
`_SKIP_BENCH_MODE_DISABLED`); reuse it rather than inventing a second set of
words for the same facts.

Do not overbuild this. It is a sentence assembled from facts already in local
variables, attached where the parked-PR event and the merge-gate surface can read
it.

### Files

New `src/orchestrator/core/blast_radius.py`,
`src/orchestrator/core/opus_bridge.py`,
`src/orchestrator/core/orchestrator_review.py`, plus tests.

### Tests that must exist

- Extraction finds a CSS selector and a `def` from a realistic diff and ignores
  unchanged context lines.
- Counting is correct over a small temp tree, and skips a binary file.
- An exception inside blast-radius computation leaves the review unaffected: the
  reviewer is still called, with no blast-radius section.
- The prompt renders a neutral line when there is nothing to report.
- The scope statement names the degraded checkout case and the no-verify-cmd
  case distinctly.

---

## Task 6: record what was decided, and repo hygiene (report §1.3, appendix)

### 6a. Spec for rendered artifacts in review (§1.3), not an implementation

Write `docs/superpowers/specs/2026-08-25-rendered-artifacts-in-review.md`.

Why it is a spec and not this session's code: it adds an `artifacts` field to the
dispatch/leaf contract, which bumps `LEAF_SCHEMA_VERSION` 2 -> 3 and invalidates
`tests/fixtures/decompose/expected_leaf_graph.json`; and it needs the LLM router
to grow a multimodal path, which it does not have. `build_argv` is text-mode, and
brainstorm is already unrouted for a related reason. Attaching images to a review
is a router capability model, not a prompt edit.

The spec must record the one hard datum the report established: **`agy`
demonstrably reads PNGs.** The reporter probed it directly and got back an
accurate description naming real Indonesian UI strings from a screenshot. That is
the capability the feature would exploit and praxis does not currently use.

Also record the honesty defect worth designing against: the worker's findings
report confabulated a viewport list in its **preamble** while every per-finding
citation was accurate. A reviewer reading only the report absorbs a false coverage
claim. Rendered evidence is the fix for that too.

### 6b. `docker-compose.override.yml` must never be tracked

It is compose's standard local-override filename and it is currently **not**
gitignored, so an accidental `git add -A` commits a machine-specific bind mount.
Add it to `.gitignore`. Do not delete the file itself in this task; see the note
below.

### 6c. Documentation of record

- `docs/gotchas.md`: new entries, in that file's narrative style, for the
  two-namespace local-repo constraint and for "a planner that answers in prose is
  permanently failed, not retried". `CLAUDE.md` gets the one-line index entries
  only, per this repo's split. Quote no counts.
- `docs/walkthrough-15min.md` is append-only and is for walkthrough runs; this was
  a field report from another user, so it does not belong there. Do not add a
  section to it.

### 6d. The dispatch branch docstring disagrees with the code (report §11)

Retargeted from Task 2e, whose file location this plan got wrong.

`DispatchRequest.branch` in `src/orchestrator/models/schemas.py:650-653` says the
worker "always cuts a NEW `agent/<slug>` branch from this and opens a NEW PR".
That is false for the path MCP `dispatch_task` actually takes. In single-branch
(auto-delegate) mode, `core/orchestrator_dispatch.py:285-316` sets
`branch = plan.get("plan_branch_name") or project["default_branch"]` and the
worker commits ONTO the named branch; only the non-single-branch else-arm cuts
`agent/{task_slug}`.

The field report hit exactly this: a second dispatch onto the first task's plan
branch stacked correctly, both commits present, nothing lost, and the doc said it
would not. Correct the docstring to describe both arms and which mode selects
which. Verify against the code, not against this description.

### Files

`docs/superpowers/specs/2026-08-25-rendered-artifacts-in-review.md`,
`.gitignore`, `docs/gotchas.md`, `CLAUDE.md`,
`src/orchestrator/models/schemas.py`.

---

## Rejected and deferred, with reasons

| Report item | Decision | Reason |
|---|---|---|
| §1.2 CSS "property went inert" lint | **Rejected** | A language-specific mechanical rule inside a language-agnostic orchestrator, covering one property family in one language, with no closed form for the general class. Redundant here by the reporter's own account of 1.1 ("This alone would have caught it"). The seam for repo-specific mechanical checks already exists and is `verify_cmd`, which runs on the checkout before the reviewer sees the diff. |
| §1.3 artifacts in review | **Deferred to spec** (Task 6a) | Contract change plus a router capability praxis does not have. |
| §1.4 change-volume signal | **Rejected** | Subsumed by 1.1, and only exists if the project emits it. |
| §2.3 `claude --add-dir` | **Replaced** (Task 1c) | `--add-dir` is a `claude`-only flag; the router spans `claude`/`codex`/`agy`/`local`. Cloning under `/app` fixes it for every provider. |
| §2.2 keyword detection of a permission request | **Replaced** (Task 1a) | Keyword-matching English prose rots. "No JSON anywhere in a JSON-only response" is structural and covers the same case. |
| §4.1 validate `model_name` at `add-project` | **Rejected** | Would make project creation spawn a container and fail on a quota or network hiccup. Same information belongs on a doctor row. |
| §6 drop `container_name` | **Replaced** (Tasks 3c + 4d) | Every doc and debugging recipe in this repo says `docker logs orchestrator`. Env-var default-unchanged gives the escape hatch; the doctor label check gives the diagnosis the reporter actually wanted. |
| §7 `praxis projects` raced startup seeding | **Deferred** | More likely slow startup than a race; diagnosis costs more than it returns without a reproduction. |
| §7 `praxis tasks` with no args listing everything in flight | **Deferred** | Real gap, moderate work, not in this pass. |

## Not done in this plan, and why (needs the operator)

`HANDOFF.md` records that another live session shares this orchestrator with a
worker running on a local repo path. That is why `config/praxis.yaml` has
`allow_local_repo_paths` flipped to `true` in the working tree and why
`docker-compose.override.yml` exists. Both should be reverted, and four tests
(`test_the_shipped_yaml_also_ships_the_opt_in_off` and friends) are red until the
config is. Reverting them mid-audit breaks that session's next dispatch, so they
are left to the operator. Task 6b gitignores the override file, which is safe
either way.
