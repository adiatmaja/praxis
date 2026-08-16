# Fresh-newcomer walkthrough: clone to a reviewed PR

Product plan Task 9. This is the artifact the earlier run discharged only in
substance. Run date **2026-08-15/16**, product commit **`eae90c2`**, verified
in sync with `origin/main` before cloning.

The spec attaches a number to simplicity: clone to first reviewed PR in 15
minutes or less. This run met it on the documented path and did not meet it on
the wall clock. Both numbers are below, unrounded.

## Machine

| | |
|---|---|
| OS | Windows 11 Pro 10.0.26200 |
| CPU | AMD Ryzen 9 4900H (8C/16T) |
| RAM | 15.4 GB |
| GPU | NVIDIA RTX 2060 |
| Docker | Engine 29.6.1 (Docker Desktop, WSL2) |
| Python | CPython 3.11.9 via `uv` |

**The single most important caveat: this machine's Docker layer cache and `uv`
cache were warm.** Every build number below is therefore a floor, not a cold
number. The 2026-08-14 run on the same machine measured **16 min 07 s** for a
genuinely cold `docker compose up --build` and **2 min 53 s** for a cold agent
image build. Nothing here contradicts that; this run simply did not pay it
again. A newcomer on a clean machine should expect roughly **+19 minutes** on
top of every total in this document.

## Method

Fresh `git clone` into `C:\working-space\praxis-newcomer`. No `.env` copied,
data volume removed between arms, and `opencode-agent:latest` / `agy-agent:latest`
deleted beforehand. Treated as available: `README.md`, docs the README links,
`.env.example`, and what the running product itself said. Everything else was
out of bounds.

Target repo: `adiatmaja/playground`, reset to a single `main` at `df04d51`.

**The task.** `main` is deliberately red: `src/playground/test_initials.py`
holds four frozen acceptance tests importing `playground.initials`, and
`src/playground/initials.py` does not exist. Both arms were given the identical
instruction to create that one file. This is the same task the 2026-08-14 run
used, so the numbers are comparable to that baseline.

**The matrix.** Two arms, changing only the preset chosen at `praxis init`:

- **Arm A** `gemini-agy` -> `agy` / Gemini 3.7 Flash (High)
- **Arm B** `local-lmstudio` -> `opencode` / qwen3.8-27b

`hosted-openweight` was skipped deliberately: there is no `api.z.ai` key for
this deployment.

**Prerequisites confirmed before the clock started** (the previous run lost
time to exactly this):

- `praxis-gemini-creds` volume seeded, verified with a real `agy -p` returning
  `PONG`, exit 0, on Gemini 3.7 Flash (High). Existence of the volume is not
  evidence it is seeded; the PONG is.
- LM Studio reachable with `qwen3.8-27b` in `state: loaded` (131k context).

## Commands in order, with elapsed time

### Shared

| Phase | Command | Time |
|---|---|---|
| Clone | `git clone https://github.com/adiatmaja/praxis.git praxis-newcomer` | **4 s** |
| Install | `uv venv && uv sync --extra dev` | **5 s** (warm uv cache) |

### Arm A: `gemini-agy`

| Phase | Command | Time |
|---|---|---|
| init, Enter-only | `uv run praxis init` (all defaults) | **1 s**, stops by design, exit 1 |
| init, full | `uv run praxis init` (preset 1, confirm `y`) | **66 s** incl. agent image builds + compose up |
| init, re-run on clean DB | same | **13 s** |
| Register | `uv run praxis add-project playground <url> "Gemini 3.7 Flash (High)"` | **1 s** |
| Set verifier | `PATCH /api/projects/{id}` `{"verify_cmd":"python -m pytest -q"}` | **1 s** |
| Dispatch -> parked at merge gate | `POST /api/dispatch` | **~80 s** |
| Approve -> merged | `POST /api/tasks/{id}/approve-merge` | **4 s** |

**Arm A documented-path total: 2 min 41 s.**
Wall clock including my own detours: **17 min 57 s** (see "Where I left the path").

### Arm B: `local-lmstudio`

| Phase | Command | Time |
|---|---|---|
| init | `uv run praxis init` (preset 2) | **12 s** |
| Point at the endpoint | edit `.env` `LM_STUDIO_URL`, then `docker compose up -d` | **~15 s** |
| Register + verifier | `add-project` + `PATCH` | **2 s** |
| Dispatch -> parked at merge gate | `POST /api/dispatch` | **109 s** |
| Approve -> merged | `POST /api/tasks/{id}/approve-merge` | **5 s** |

**Arm B documented-path total: 2 min 23 s** (plus the 9 s shared clone+install).

Arm B's raw wall clock was 199 minutes. That number is meaningless and is
recorded only so it is not mistaken for a measurement: the session paused for
roughly three hours waiting on a usage-limit refresh. Summed phase time is the
honest figure.

### Both arms produced correct work on the first try

| Arm | Worker time | Turns | Tokens | Diff | Retries | Result |
|---|---|---|---|---|---|---|
| A: agy / Gemini 3.7 Flash (High) | 47.3 s (`duration_seconds`) | 1 | 86,465 total (82,830 in / 3,635 out / 2,189 thinking / 284,280 cache-read) | 17 lines, 1 file | 0 | merged, verify + review passed |
| B: opencode / qwen3.8-27b | ~90 s (container) | n/a | **unreported** | 17 lines, 1 file | 0 | merged, verify + review passed |

Both converged on the same core expression:

```python
return "".join(f"{part[0].upper()}." for part in name.split())
```

Both added type annotations and a Google-style docstring, created exactly one
file, and touched nothing else. `main` stayed untouched in both arms; the work
merged into the plan branch and an integration PR was opened for a human.

**agy reports token usage; opencode reports none.** Confirmed by inspecting
both agent logs. Arm B's cost is unmeasurable from inside Praxis.

## The qwen3.8 reasoning_effort question (Arm B, the highest-value item)

The concern going in: the 2026-08-15 fix covers only payloads Praxis builds
itself. OpenCode constructs its own LM Studio requests, and Praxis does not
control their `reasoning_effort`. qwen3.8 thinks by default, so an omitted key
means maximum effort, not off.

**No pathology was observed.** All six `[PRAXIS PHASE]` markers completed, one
pass, no retries, no empty completions, no `finish_reason=length`, no
truncation, no latency creep. 109 s dispatch-to-passed is *faster* than the
2026-08-14 qwen3.6 baseline of 3 min 14 s on the same task.

**This is weak evidence and should not be read as an all-clear.** Three
reasons, stated plainly:

1. The task is a single easy leaf. Maximum-effort thinking hurts on long or
   ambiguous work; it is nearly invisible on a 17-line function.
2. **opencode reports no token usage at all**, so the one measurement that
   would actually settle the question, how many thinking tokens were burned,
   does not exist.
3. **I did not intercept opencode's outgoing payload.** The plan called for
   inspecting it if symptoms appeared; none did, so this was not done. Whether
   opencode sends `reasoning_effort` at all remains **unverified**.

The arm is "no pathology on an easy task", not "verified safe".

## Leak log

Knowledge used that a genuine newcomer would not have. Four entries.

| # | Leak | Severity | Source |
|---|---|---|---|
| 1 | **LM Studio endpoint host.** Arm B's preset assumes `localhost:1234`; this deployment serves the model from a remote host over VPN. I took the URL from the dev tree's `.env`. | Medium | Out-of-bounds `.env` |
| 2 | **`verify_cmd = python -m pytest -q`.** The natural choice for this repo, but this session's brief had already named it as settled, so it is not cleanly derived. Disclosed, same as last run. | Low, disclosed | Session brief |
| 3 | **`docker compose up -d` as the recovery for a changed `.env`.** The docs teach `docker compose restart`, which silently does not work (defect 4 below). I knew to reach for `up -d`. A newcomer would have been stuck here. | Medium | Prior-run memory |
| 4 | **The agy creds volume was pre-seeded.** Arm A only ran because a one-time interactive `agy login` had been completed in an earlier session. Nothing in the product would have walked a newcomer through it. | High for Arm A | Prior session |

**The low count is not by itself a good sign, and the caveat from last run
still applies.** It is low because the README and `docs/deployment.md` are
genuinely strong. The friction that remains is in product behavior, not
documentation. An empty leak log would have meant cheating.

Note that leak 4 is materially worse than anything in the previous run's log:
**Arm A is not reproducible by a newcomer at all**, because the product never
tells them how to satisfy the requirement it stops on.

## Where I left the documented path

Recorded because the method requires it. Three of these were my own errors.

1. **I reused the previous run's data volume.** `praxis-newcomer_praxis_data`
   survived from 2026-08-14; the first Arm A install came up with a project row
   dated `2026-08-14 04:55` already in it. Caught by noticing that a failed
   `add-project` still listed a project. Removed the volume and re-ran init
   clean; both doctor FAILs reproduced on the genuinely fresh install, so the
   findings stand.
2. **I polled the wrong shape for 10 minutes.** `GET /api/tasks/{id}` returns
   the task nested under a `"task"` key; I read `status` at the top level and
   saw `None` on every poll while the task had in fact finished in ~80 s. My
   bug, not the product's.
3. **I misreported defect 4 as fixed, then corrected it.** My redaction regex
   masked `host.docker.internal` and the VPN host identically, so a stale value
   looked like a fresh one. Re-checked without the over-broad mask and
   confirmed the defect is real.
4. **`verify_cmd` had to be set with a raw `curl` PATCH**, because no client
   exposes it (defect 3 below).
5. **The first dispatch was rejected** for using `main` as a plan base. This is
   the product working correctly, and the error text is good; noted as a
   departure only because it cost one round trip.

## What is confirmed FIXED since 2026-08-14

Verified first-hand, with output:

- **The README CLI gap, last run's single most valuable finding, is closed.**
  Quick Start now documents `uv venv && uv sync --extra dev` and
  `uv run praxis init`, carries an explicit paragraph explaining why a bare
  `praxis` reports "command not found", documents port **12323**, names
  `AUTH_TOKEN`, and makes `uv run praxis doctor` discoverable with an
  explanation of what it checks. All four sub-points confirmed.
- **The verify gate now leaves evidence** (last run's defect 5). Both arms
  logged `verify gate passed (`python -m pytest -q`)`.
- **The review verdict is now logged** (last run's defect 6):
  `review verdict: pass (pr=...)`.
- **An unreachable implementer is now caught before dispatch.** This was last
  run's top friction ("nothing verified it"). `praxis doctor` correctly went
  red on a genuinely unroutable endpoint and green (`qwen3.8-27b available`)
  once fixed. The check is accurate and it is the reason Arm B did not waste a
  dispatch.
- **The merge gate remains exact.** Parked at PASSED, never auto-merged, 4-5 s
  from approval to merge, `main` untouched, integration PR opened for a human.

## Defects found, ranked

1. **FIXED 2026-08-16** **`praxis init` tells you the deployment's default preset needs a login it
   cannot collect, and never tells you how to do it.** The Enter-only path
   stops on `gemini-agy` with a message that names the requirement
   (`interactive_login`) and the consequence ("the worker fails its first
   task"), but gives **no command, no doc link, and no next step**. The exact
   cross-platform recipe exists in `docs/deployment.md`, and the harness
   registry in-process even carries a `when_to_pick` string ending "see
   docs/deployment.md", init prints none of it. Its only recovery advice is
   "pick one that needs no credential", i.e. abandon the deployment's own
   default. **Verdict on the question this run was asked to judge: the stop
   reads as a dead end.** It is well-written about *what* is wrong and silent
   about *what to do*, which is the worse half to omit.

2. **FIXED 2026-08-16** **The Enter-only path discards everything it just collected.** It prompts
   for auth token, port, and git credentials, then on declining the preset
   writes **no `.env` at all** and exits 1. A newcomer holding Enter through
   the documented Quick Start ends with an empty directory and a non-zero exit.

3. **FIXED 2026-08-16** **`praxis doctor` reports freshly built agent images as stale, on every
   fresh clone.** The check compares image build time against the entrypoint's
   filesystem mtime. `git clone` stamps every file at clone time, so the
   entrypoint is always "newer" than any cached or pulled layer. Docker's own
   build output proves the content is unchanged (`COPY entrypoint.sh` -> `CACHED`,
   a content-hash match), and the image is still declared stale. Reproduced on
   both arms and on a genuinely clean install. Every newcomer will see this
   red on a correct install. Fix: compare content (label the image with a hash
   of the entrypoint), not mtimes.

4. **FIXED 2026-08-16** **The `gemini-agy` preset can never produce a green doctor.** The
   `worker_endpoint` probe requires an OpenAI-compatible `GET /v1/models`, but
   agy authenticates to Google over OAuth and has `supports_local_llm=False`.
   Notably this is a **half-finished fix**: the code deliberately suppresses
   the *model-name* comparison for non-local-LLM harnesses, with a comment
   naming the category error, but still runs the reachability probe ungated,
   and `if not reachable` fires first. So the deployment's own default preset
   shows 2 of 11 red on a correct install. Fix: gate the reachability half on
   `supports_local_llm` too, exactly as the model half already is.

5. **FIXED 2026-08-16** **Editing `.env` then `docker compose restart` silently keeps the old
   value.** Confirmed, not fixed since last run. The container kept serving the
   stale `LM_STUDIO_URL` and doctor stayed red; `docker compose up -d` fixed it
   immediately. The docs say `docker compose restart orchestrator` repeatedly,
   always correctly, about the *mounted YAML*, and Quick Start tells you to
   edit `.env`, so the repeated pattern teaches the wrong recovery for the
   wrong file. This cost real time in both runs.

6. **`verify_cmd` is unreachable from every client.** Absent from the dashboard
   (found last run) and absent from `praxis configure`, which offers only
   `--gate`, `--threshold`, `--retries`. The verifier is one of the four
   advertised seats and can only be set with a raw `PATCH /api/projects/{id}`.
   `docs/deployment.md:350` still claims it is set "via the API or dashboard
   project settings", which remains factually wrong.

7. **The dispatch response still advertises the wrong port.**
   `dashboard_url: http://localhost:8080/` while the product runs on 12323.
   Flagged last run, not fixed.

8. **`praxis add-project` requires a model argument**, described as "LM Studio
   model name", contradicting the README's claim that "a project that omits its
   own `model_name` falls back to this default, so you can register a repo and
   start delegating without picking a model". The CLI gives you no way to omit
   it.

9. **The README's reference config is stale.** It says the reference is `agy`
   driving **Gemini 3.6 Flash (High)**; `config/praxis.yaml` says **3.7**.
   Commit `cd99357` updated `CLAUDE.md`, `docs/deployment.md` and
   `docs/gotchas.md` and missed `README.md`. `docs/deployment.md`'s own verify
   snippet also still shows `Gemini 3.5 Flash (High)`.

10. **There is no CLI dispatch command.** The README says you can drive the
    engine "from an MCP client, the dashboard, or the CLI", but the CLI has no
    way to dispatch a single task; `submit` only starts the full
    Spec->Plan->Run path. Both arms here used raw `curl`.

11. **`/health` still reports `"commit":"dev"`** on a default install, so the
    doctor's commit check can only ever return NOTE.

## Score

**Thinking layer: 9/10.** Both workers produced correct, in-scope, well-typed,
well-documented code on the first attempt with zero retries, from the same
instruction, on two completely different providers. The verify gate ran and
logged, the reviewer passed a correct diff and logged its verdict, the merge
gate parked exactly as designed and left `main` untouched. The protected-branch
refusal and the bench-mode-style error texts are precise and actionable. The
capability thesis looks sound: the open-weight local model and the hosted Flash
model were indistinguishable in output quality on a properly scoped leaf.

**Execution and onboarding layer: 5/10.** Up from 3. The README gap that made
the CLI unusable is genuinely closed, the verify gate and review verdict now
leave evidence, and doctor now catches an unreachable implementer before it
wastes a dispatch, all four are real, verified improvements. But a correct
fresh install still shows two red checks that are both the product's fault, the
default preset cannot be completed by anyone who reads only the product's own
output, and the Enter-only path throws away your answers and exits 1.

**Overall: 7/10** (up from 5). The engine is good. The first ten minutes are
still where it loses people, and the specific way it loses them has moved from
"the CLI does not work" to "the default path stops and will not tell you how to
continue".

**Is the 15-minute claim met?** On the documented path, yes, comfortably:
**2 min 41 s** (Arm A) and **2 min 23 s** (Arm B) from a warm machine, or under
5 minutes including clone and install. **On a cold machine it is not met**,
add roughly 19 minutes of unavoidable image build, which puts a true first run
near 22 minutes. That is the honest number, and the slowest step is the Docker
build, not anything Praxis controls at runtime.

## What to fix, ranked

Items 1-5 below are closed by
`docs/superpowers/plans/2026-08-16-onboarding-blockers.md`.

1. **Print the login recipe at the point of refusal.** When init challenges a
   preset's `interactive_login`, print the actual commands (or at minimum the
   `docs/deployment.md` anchor). The registry string is already in memory. This
   single change is what makes Arm A reproducible by a newcomer, and it is the
   difference between a considered stop and a dead end.
2. **Persist what init collected before it exits.** Declining a preset should
   not discard the auth token, port, and git credentials.
3. **Make the image-staleness check content-based**, not mtime-based. It is
   red on every fresh clone today, which trains people to ignore doctor output.
4. **Gate the worker-endpoint reachability probe on `supports_local_llm`**,
   finishing the fix whose other half is already there with a comment
   explaining why.
5. **Make `.env` changes take effect, or say they do not.** Either have the
   docs name `docker compose up -d` for `.env` edits, or have doctor detect
   that the running container's env differs from the file on disk.
6. **Expose `verify_cmd`** in `praxis configure` and the dashboard, and fix
   `docs/deployment.md:350`.
7. **Fix `dashboard_url`** to use the configured port.
8. **Reconcile README with `config/praxis.yaml`** on the reference worker (3.6
   -> 3.7), and let `add-project` omit the model to take the global default.
9. **Add a CLI dispatch command**, or stop listing the CLI as a way to drive
   the engine.

## Follow-up this run did not close

- **opencode's outgoing LM Studio payload was never inspected**, so whether it
  sends `reasoning_effort` is still unverified. Worth settling deliberately
  rather than waiting for a symptom, given qwen3.8 defaults to thinking.
- **opencode reports no token usage**, so open-weight worker cost is invisible
  to Praxis. agy already puts it on the wire.
- **No cold-machine number was produced this run.** The 19-minute figure is
  carried over from 2026-08-14 on the same hardware.

## Live verification 2026-08-16

Ran against real Docker, real images, and a real fresh clone, on branch
`docs/walkthrough-and-onboarding-plan`. Unit tests were green for every fix
below before this ran; three of them were still inert, and only executing the
product exposed that. Recorded as it happened, including what failed.

**Prerequisite.** `agy -p` inside `agy-agent:latest` with the
`praxis-gemini-creds` volume returned `PONG`, so the credential really is
seeded rather than merely present as a volume.

### What passed first try

- **Label round-trip (defect 3).** `AGY_ENTRYPOINT_SHA256` exported from
  `hash_entrypoint`, then `docker compose --profile agents build agy-agent`:
  the image label read back as
  `60fbbc713340227b9f42f17ae39de3067b5910b23876922324a505241f39f3a2`, byte
  identical to the source hash. Same for opencode
  (`eda1bae895d181ff0594398f1af8b9c248bfd9d1dcad997d532c32b527c653a5`).
- **Staleness goes red for the right reason.** Appending a comment to
  `docker/agy-agent/entrypoint.sh` without rebuilding turned the row FAIL;
  restoring the file turned it OK. The check is live, not decorative.
- **Worker endpoint (defect 4).** With the `gemini-agy` default preset the row
  is OK with detail `not applicable: this harness does not use an OpenAI
  endpoint`, where it was permanently red before.
- **Answers survive a declined preset (defect 2).** On a fresh clone with no
  `.env`, `printf '\n\n\n\n\n\n' | praxis init` printed
  `Wrote ...\.env`, and the file held `AUTH_TOKEN` and `PORT`. No
  `GITHUB_TOKEN`, correctly, because holding Enter selects `skip` (local mode).

### What live verification caught that the unit tests did not

1. **`env_drift` could never fire (defect 5 shipped inert).** The row read
   `could not read the container or .env to compare`. Cause: `.env` is never
   mounted into the container. Compose reads it on the HOST to substitute
   variables and passes the resulting values as env vars; the file itself does
   not enter. `_env_drift_facts` was reading a path that does not exist there.
   Fixed by mounting `./.env:/app/.env:ro` in both compose files.
2. **`env_drift` then reported a false red on `PORT`.** `PORT` in `.env` is the
   HOST publish port (12323, via `${PORT}:8080`); inside the container uvicorn
   always listens on 8080. Comparing them is the same category error defect 4
   was about. Fixed with a documented `_HOST_ONLY_ENV_KEYS` exclusion.
   After both fixes, the trap itself is now caught end to end: editing
   `LM_STUDIO_URL` then `docker compose restart` gives
   `FAIL ... container env is stale for: LM_STUDIO_URL` with the hint
   `run docker compose up -d (not restart)`, and `up -d` returns it to OK.
3. **The setup recipe never reached the operator (defect 1 shipped inert).**
   The first live `praxis init` printed the refusal with no recipe at all.
   Cause: `_fetch_presets_or_defaults` builds its menu dict key by key and did
   not list `setup_doc` / `setup_hint`, so the YAML held the recipe, `init`
   looked for it, and nothing joined the two. The Task 8 unit test passed
   because it constructed the preset dict literally. Fixed by carrying both
   fields through `WorkerPreset` and pinning the seam with a test that fails
   when either key is dropped. Re-run on a fresh clone then printed the full
   `agy login` recipe and the `docs/deployment.md` pointer.
4. **A bare `docker compose --profile agents build` leaves the label empty**,
   so the freshness check reports AMBER rather than green. Only `praxis init`
   exports the hash env vars. The check's fix hint named exactly that
   non-working command; it now names `praxis init` first.

### Final state

`praxis doctor` against a correct `gemini-agy` install: **all twelve checks
green**, including the two rows that were structurally unable to go green
before this plan. The one FAIL seen mid-run was the build-stamp check
correctly noticing the container predated the newest commit; it cleared on
rebuild.

**Not verified.** The opencode arm was not exercised end to end this run
(LM Studio was not loaded), so the opencode label was proven to round-trip but
no opencode task was dispatched. The `reasoning_effort` question above remains
open.

---

# Run #3, 2026-08-16

Product commit **`c858247`** (`main` == `origin/main`, clean). Previous scores
**5/10** (2026-08-14) and **7/10** (2026-08-15/16). This is the first run where
the first-ten-minutes blockers were fixed *before* it started.

**Score: 6/10.** Down from 7/10, and the drop is not a regression in what the
onboarding plan fixed. Everything that plan set out to fix is confirmed fixed
and green. The score fell because running the documented CLI path end to end,
which no previous run did, found that `praxis submit` silently discards the
specification. The previous runs scored a product whose only working path was
one a newcomer cannot reach.

## Method

Identical to run #2 so the numbers compare. Fresh `git clone` into
`C:\working-space\praxis-newcomer`, no `.env` copied, data volume removed
between arms, `opencode-agent:latest` and `agy-agent:latest` deleted first.
Treated as available: `README.md`, the docs it links, `.env.example`, and the
running product's own output. Everything else out of bounds.

Target `adiatmaja/playground` at `df04d51`, same frozen-test task: create
`src/playground/initials.py` so four frozen acceptance tests pass.

**Caches were warm** (Docker layers and `uv`). Every build number below is a
floor. The 2026-08-14 cold-machine measurement was **+19 minutes**; nothing here
contradicts it.

### Prerequisites, confirmed with real calls before the clock

- `agy -p` inside `agy-agent:latest` with the `praxis-gemini-creds` volume
  returned `PONG`, exit 0.
- LM Studio: `qwen3.8-27b` in `state: loaded`, 131072 context.

Both passed first time. Unlike 2026-08-14, no time was lost here.

## Phase timings, unrounded

| Phase | Elapsed |
|---|---|
| `git clone` | 61.381 s |
| `uv venv && uv sync --extra dev` | 4.180 s |
| `docker build -t agy-agent:latest` per `docs/deployment.md` | 18.118 s |
| `praxis init`, Arm A, incl. both agent images + orchestrator + doctor | 115.671 s |
| `praxis init`, Arm B, images already built | 18.164 s |
| Arm B dispatch to reviewed PR | **45 m 38 s** |

`praxis init` to an all-green doctor from a fresh clone: **1 m 55.671 s.**

## The four questions this run had to answer

**1. Does a fresh clone reach a green `praxis doctor` unaided? YES.** Both
arms. Arm A printed `All checks passed.` and exited 0 with all twelve checks
green, on the first correct `init`. Arm B showed exactly one FAIL, the worker
endpoint, because `local-lmstudio` defaults to `host.docker.internal:1234` and
this deployment serves LM Studio remotely. That FAIL is correct behavior and its
remedy line was actionable. This is the first run where this succeeded, and it
is the plan's central claim.

**2. Does holding Enter through `init` leave a newcomer somewhere useful?
PARTLY.** It no longer leaves an empty directory and exit 1. It writes a real
`.env`, verbatim:

```
Wrote C:\working-space\praxis-newcomer\.env
```

containing `AUTH_TOKEN` and `PORT`. But it still exits 1 with no preset, no
images, and no orchestrator, because the configured default preset
`gemini-agy` requires `interactive_login` and the default answer to
`Choose it anyway? [y/n] (n)` is no. Holding Enter is a dead end that preserves
your answers rather than a dead end that discards them. Better, not solved.

**3. Does the opencode arm work end to end? YES, first try, zero retries.**
Not exercised at all last session. Dispatched at 14:13:10, review PASSED at
14:58:48, PR #36, attempt 1, exactly one file changed. I ran the four frozen
tests against the branch myself rather than trusting the reviewer:
`4 passed in 0.04s`. The reviewer's own note was substantive and correct.

**4. Does opencode send `reasoning_effort`? NO. Now VERIFIED, not inferred.**
This was UNVERIFIED after two runs. I put a logging reverse proxy between the
agent container and LM Studio and captured every outgoing request rather than
waiting for a symptom. Across **18 captured `POST /v1/chat/completions`, not one
carried `reasoning_effort`,** or any other thinking-control key. The complete
top-level key set opencode sends is:

```
["max_tokens", "messages", "model", "stream", "stream_options", "top_p"]
["max_tokens", "messages", "model", "stream", "stream_options", "tool_choice", "tools", "top_p"]
```

qwen3.8-27b thinks by DEFAULT, so an absent key means **maximum** reasoning
effort on every opencode worker call, not off. `core/thinking.py` is the SSoT
for payloads Praxis hand-builds; opencode builds its own requests inside the
container and Praxis has no say in them.

**The cost is measured, not theoretical.** 45 m 38 s for a task whose answer is
a 9-line function, across only 18 model calls, roughly 2.5 minutes per call. It
spent its first 31 minutes and 7 calls in the `understanding` phase reading six
files. Correct, and impractically slow.

## Leak log

| # | Leak | Severity | Source |
|---|---|---|---|
| 1 | LM Studio is served remotely (`https://pcllm.sigmasolusi.com`), not `localhost:1234`. A genuine newcomer picking `local-lmstudio` gets the doctor FAIL and no way to know the URL. | HIGH | Read from the existing dev `.env`. Same as run #2's leak #1, unfixed. |
| 2 | Reused the already-seeded `praxis-gemini-creds` volume. A real newcomer must complete browser OAuth. | MEDIUM | Pre-existing volume; the handoff mandates verifying it. |
| 3 | Used an existing `gh auth token` for `GITHUB_TOKEN` rather than minting a PAT. | LOW | `gh auth token`. Does not change any product behavior tested. |
| 4 | Dispatched Arm B over raw REST because the CLI has no dispatch command. Knowing the `/api/dispatch` schema is not newcomer knowledge. | MEDIUM | Prior sessions. Consequence of defect 9. |
| 5 | Knew to look at `spec_path` and `api/plans.py` to confirm the discarded spec. A newcomer would only see a wrong plan. | LOW | Source reading, after the symptom. |

## Defects, ranked

**1. CRITICAL, new. `praxis submit` accepts a specification and silently
discards it.** It is the only way the CLI can drive the engine.
`api/plans.py:32-50`:

```python
async def create_plan(request: Request, project_id: str, body: PlanCreate) -> dict[str, Any]:
    ...
    plan_id = await request.app.state.task_queue.create_plan(project_id)
```

`body` is validated (`spec` must be non-empty) and then **never referenced
again**. `plans.spec` was dropped in Spec 2 and nothing replaced it: `spec_path`
stayed `None`. The brain planned from the repository *name* alone. Submitted
spec: create one Python file to satisfy four frozen tests. What the brain
produced, verbatim from `opus_plan`:

```
"plan_summary": "Set up and scaffold the playground repository with basic project structure and tooling"
tasks: Initialize project structure / Add linting and formatting configuration
       / Create example JavaScript modules / Add testing setup
```

It invented a **Node.js/JavaScript** scaffold for a Python repo with a
`pyproject.toml`, including "Add ESLint and Prettier" and "Create src/utils.js".
Then it activated the plan and dispatched a real worker against the real
repository. This is worse than defect 9: the CLI path is not merely incomplete,
it is actively destructive, and it fails silently. I stopped it and restored the
target repo.

**2. HIGH. The setup recipe `init` prints tells you to run a command against an
image that does not exist yet.** The defect-1 fix from the last plan now prints
the `agy login` recipe, which is a real improvement, but `init` exits *before*
building images, so following it verbatim on a fresh clone gives:

```
Unable to find image 'agy-agent:latest' locally
docker: Error response from daemon: pull access denied for agy-agent, repository does not exist or may require 'docker login'
```

Chicken-and-egg: `init` will not proceed without the credential, and the
credential cannot be created without the image `init` would have built.
Recoverable only because the recipe also links `docs/deployment.md`, which has
the build command at line 26, correctly ordered above the login section. The
printed recipe is missing that one step.

**3. HIGH. The documented `docker build` command produces an image the doctor
can never judge.** `docs/deployment.md:23-26` omits
`--build-arg PRAXIS_ENTRYPOINT_SHA256=...`, so the image carries
`org.praxis.entrypoint-sha256` present but **empty**, which is the designed
"cannot judge" state. Verified: label empty after the documented build,
populated (`60fbbc71...`) after `praxis init` rebuilt it. Only `init` builds
correctly, so anyone following the docs gets a permanently unjudgeable image.
This was noted in the previous run against `--profile agents build`; the
per-image commands have the same hole.

**4. MEDIUM, new. `/api/dispatch` ignores the supplied `title`.** Passing
`"title": "Create src/playground/initials.py"` stored the title as the truncated
first sentence of `instructions`, and derived the branch from that:

```
title:  "The repository has four frozen acceptance tests in src/playground/test_initials."
branch: agent/the-repository-has-four-frozen-acceptanc-6558ef
```

**5. MEDIUM, new. The CLI prints IDs its own commands reject.** Tables show
8-character IDs; every command needs the full UUID. Feeding the CLI its own
output:

```
$ uv run praxis stop 8f929f8a
Error 404: {"detail":"Task not found"}
```

`praxis pending` likewise truncates the PR URL so it cannot be copied.

**6. MEDIUM. `dashboard_url` reports the wrong port** (known defect 7,
confirmed verbatim). Dispatch returned `"dashboard_url":"http://localhost:8080/"`
against an installation on 12323.

**7. LOW. `/health` reports `"commit":"dev"`** (known defect 10, confirmed),
which makes the build-stamp doctor check permanently a NOTE:
`running commit dev; no working tree available here to compare against`.

**8. LOW, new. The `plans` table renders a dead `Spec` column,** always empty,
left over from the dropped `plans.spec`.

**9. LOW, new. Table rendering corrupts the callback URL,** in both arms:
`http://host.docker.internal:1232?` for a port of `12323`.

**Known and re-confirmed, unchanged:** defect 6 (`verify_cmd` unreachable from
every client; the created project had `verify_cmd: null` and no CLI flag sets
it), defect 8 (`add-project` requires a model, and calls it "LM Studio model
name" even for the agy default), defect 9 (no CLI dispatch command).

Defect 8 is milder than recorded: the harness *is* correctly inherited from the
preset. `add-project` with no `--harness` flag produced `harness: "agy"` from
`.env`. Only `model` is wrongly mandatory.

## Confirmed fixed since run #2

All verified live on a fresh clone, not from unit tests.

1. **The `.env` directory trap recovers.** Deliberately ran `docker compose up`
   before `init`; Docker created an empty `.env/` directory as predicted, and
   `init` printed, verbatim:
   ```
   Removed the empty C:\working-space\praxis-newcomer\.env directory Docker left
   behind (compose ran before init).
   ```
2. **`init` prints the full setup recipe** on an unmet preset requirement, both
   `docker run` commands and the `docs/deployment.md` anchor. See defect 2 for
   the one step it still omits.
3. **Declining a preset preserves your answers** (`Wrote ...\.env` on the
   `typer.Exit` path).
4. **Agent-image freshness is judged by content**, green in both arms on a fresh
   clone, where the old mtime check was structurally red.
5. **`worker_endpoint` can reach green on `gemini-agy`**:
   `not applicable: this harness does not use an OpenAI endpoint`.
6. **`env_drift` works and stays quiet**: `container env matches .env` in both
   arms, including after an `.env` edit followed by `up -d`.

Also confirmed correct: the protected-branch guard refused `main` as a plan base
with an actionable message; auth failed closed (401 on absent, empty, and junk
bearer tokens) even on a container started with a blank `AUTH_TOKEN`; the merge
gate parked the passing task rather than merging it, and `praxis pending`
surfaced it.

## Score: 6/10, reasoning

**What earns it.** The plan's own target is met without qualification: a fresh
clone reaches an all-green doctor in under two minutes, unaided, on the first
correct `init`. That failed in both previous runs. Every one of the five shipped
fixes is confirmed live. Both arms produced correct work first try with zero
retries, and I verified Arm B's output by running the frozen tests rather than
trusting the review. The safety rails all held.

**What costs it.** The single documented CLI path for driving the engine,
`praxis submit`, accepts your specification, throws it away, invents unrelated
work from the repository name, and dispatches a worker to do it. A newcomer
following only the README hits this on their first real task, gets a JavaScript
scaffolding plan for their Python repo, and has no way to see why. Both
successful arms in this run reached the engine through paths a newcomer does not
have: raw REST for Arm B, and for Arm A the CLI worked only up to the point
where the spec vanished.

The 45-minute opencode task is the second cost. It is correct but no one would
adopt it, and now we know the cause with intercepted evidence rather than a
guess.

Run #2's 7/10 was measured on a product whose reachable surface was never fully
exercised. The onboarding plan genuinely moved *setup* from 5 to better than 7.
The first ten minutes are close to solved. The eleventh minute, the first real
task through the documented path, is where it now breaks.

## What to fix, ranked

1. **Make `POST /api/projects/{id}/plans` persist the spec** and feed it to the
   planner. Write it as a spec doc and set `spec_path`, which is the Spec 2
   design the code stopped short of. Gate it with a test that starts at the REAL
   `praxis submit` call and asserts the submitted text reaches the brain prompt,
   then mutate the carrier and watch it go red. This is exactly the
   `unit-green-seam-inert` shape: both ends correct, the link dead, invisible
   from either side.
2. **Add `praxis dispatch`** (defect 9). Without it the CLI cannot drive the
   engine at all, and item 1 is the only reason it looks like it can.
3. **Add the image build step to the recipe `init` prints**, or build images
   before the preset check.
4. **Fix the documented `docker build` commands** to pass the entrypoint hash,
   or have the doctor say plainly that a hand-built image cannot be judged.
5. **Accept short IDs** in CLI commands, or print full ones.
6. Honor `title` in `/api/dispatch`; fix the `dashboard_url` port; drop the dead
   `Spec` column; fix the truncated callback URL rendering.
7. **Decide what to do about opencode's thinking effort.** Praxis cannot inject
   `reasoning_effort` into requests opencode builds itself. Either configure it
   through opencode's own config, choose a model that does not think by default,
   or document that the opencode arm runs at maximum effort.

## Verdict on a second onboarding plan

Yes, and it is now clearly scoped. Items 1 and 2 above are one plan: make the
documented client actually drive the engine. Item 1 alone is worth shipping on
its own, ahead of everything else in the backlog, because it is a correctness
bug that silently produces wrong work against a real repository.

Defects 6-11 from the earlier list survive this run unchanged and belong in the
same plan, along with the non-interactive `praxis init` surface captured but not
built last session.
