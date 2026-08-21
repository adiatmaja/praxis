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

# Run #4, 2026-08-21

Product commit **`1f30673`** (`main` == `origin/main`, clean), which is the
spec-carrier fix (#108) merged on top of the harness-parity contract. Previous
scores **5/10**, **7/10**, **6/10**.

**Decision, stated because the score is not comparable across it: the twelve
tasks in `docs/superpowers/plans/2026-08-20-onboarding-blockers-2.md` were NOT
executed first.** Run #4 measures the spec-carrier fix in isolation, and it
re-found the known defects as expected. What settled the choice was checking the
CLI surface before starting: the merge gate has no CLI verb at all, and that gap
is not one of the twelve, so executing the plan first would not have changed this
run's headline answer, only made the number incomparable to run #3's 6/10.

## The question this run existed to answer

**Can a newcomer take a spec from `praxis submit` all the way to a merged PR
using only the CLI, without ever reaching for `curl`?**

**No.** Everything up to the merge gate is reachable from the CLI and worked.
The final step is not. `praxis pending` shows the parked task; nothing in the CLI
opens it. `POST /api/tasks/{id}/approve-merge` and
`POST /api/plans/{id}/approve-merges` exist in REST and are referenced by the MCP
server, but no CLI command invokes either. The one command whose name a newcomer
would reach for, `praxis approve`, is for autonomous improvement plans and returns
`404 Plan not found` against a task id. I completed the loop with `curl`, and that
`curl` is forced, not a convenience.

## Method

Identical to runs #2 and #3 so the numbers compare. Fresh `git clone` into
`C:\working-space\praxis-newcomer`, no `.env` copied, data volume removed between
arms, `opencode-agent:latest` and `agy-agent:latest` deleted first. Treated as
available: `README.md`, the docs it links, `.env.example`, and the running
product's own output. Everything else out of bounds and logged as a leak.

Target `adiatmaja/playground`. Two different specs, one per arm, both small and
independently verifiable by running the repo's tests.

**Caches were warm** (Docker layers and `uv`), as in run #3, so every build
number is a floor. The 2026-08-14 cold-machine measurement was **+19 minutes**.

### Prerequisites, confirmed with real calls before the clock

- `claude -p` returned `PONG` on the host, exit 0.
- LM Studio: `qwen3.8-27b` in `state: loaded`.
- Docker Desktop 29.6.1 up, no containers running.
- `adiatmaja/playground` clean: one branch, no open PRs.
- **`praxis-gemini-creds` was MISSING.** The documented PONG check auto-created
  an empty root-owned volume and died on `permission denied`, which reads as a
  broken image rather than absent credentials. Re-seeded by interactive
  `agy login` before the clock, as prior runs did, so timings stay comparable.

### Pre-clock merges

PR #109 merged clean. PR #108 came back `CONFLICTING`: since HANDOFF was written,
`feat/harness-parity-contract` had landed, so `origin/main` was `150df33` and not
`f3d9c22`. Rebased #108 onto the new main (two additive doc conflicts, both
kept), full suite **2257 passed**, CI 8/8, then merged.

## Phase timings, unrounded

Clock started at **00:06:03** with both agent images deleted.

| Phase | Elapsed |
|---|---|
| `uv venv && uv sync --extra dev` | 4 s |
| `praxis init`, Arm A, all three images + orchestrator + doctor | 41 s |
| `praxis init`, Arm B, images already built | 14 s |
| `praxis add-project` | 2 s (both arms) |
| `praxis submit` | 6 s (A), 5 s (B) |
| **Blocked by the VPN killswitch** | **8 m 34 s** (00:10:21 to 00:18:55) |
| Arm A: plan activated after unblock | 16 s |
| Arm A: dispatch to ready-for-review | 86 s |
| Arm A: review verdict | 32 s |
| Arm A: merge approve to integration PR | 8 s |
| Arm B: submit to plan activated (2 tasks) | 24 s |
| Arm B: task 1 dispatch to ready-for-review | 73 s |
| Arm B: task 1 review verdict | 52 s |

Clock start to an all-green `praxis doctor` on a fresh clone: **about 3 m 30 s**
(the `git clone` leg itself was not captured; `bc` is absent on this machine and
my timing expression silently produced an empty value, which is my instrumentation
slip, not the product's).

**Arm A total, clock start to integration PR: 16 m 10 s, of which 8 m 34 s was
the killswitch.** Productive path: about **7 m 36 s**.

## Leak log

| Severity | Leak | Source |
|---|---|---|
| HIGH | The LM Studio endpoint `https://pcllm.sigmasolusi.com`. `local-lmstudio` defaults to `host.docker.internal:1234`, doctor FAILs, and the remedy line says "start the endpoint and load the configured model", which assumes the default is right. Nothing in `README.md`, the docs it links, `.env.example`, or product output can tell a newcomer where their endpoint is or that the value is `LM_STUDIO_URL`. | Standing leak, reproduced from prior runs |
| HIGH | That the VPN killswitch has a `CLAUDE_VPN_KILLSWITCH_OFF=1` escape hatch, and that it must be set as a compose literal rather than in `.env`. Without this the run is dead at planning with `Could not extract JSON from response`. | Read the hook source and the pydantic traceback |
| MEDIUM | Pre-seeding `praxis-gemini-creds` before the clock rather than counting interactive OAuth as a measured phase. | Method choice, matches runs #1 to #3 |

**Not leaks, checked deliberately:** `POST /api/tasks/{id}/approve-merge` is
documented at `docs/deployment.md:533`, which the README links, so the REST route
is discoverable. The worker model string `Gemini 3.7 Flash (High)` came from the
install's own `.env`, which is product output.

## Where the documented path forced me off it

`add-project` demands a `MODEL` argument described as "LM Studio model name"
while I was on the agy/Gemini preset, where LM Studio is not involved. There is
no `--harness` flag. I supplied the value the preset had written into `.env`,
which worked, but the CLI gave no hint that this was the right thing to type.

The merge gate has no CLI verb, so every task boundary needs `curl`. See the
headline finding.

## What both arms actually produced

Both arms produced **correct work on the first try, zero retries**, verified by
cloning each branch and running the repo's own suite rather than by reading the
diff.

**Arm A, `gemini-agy`, Gemini 3.7 Flash (High).** One task. Spec: implement
`initials()` against four frozen acceptance tests.

```python
def initials(name: str) -> str:
    return "".join(f"{word[0].upper()}." for word in name.split())
```

Minimal and correct, including the two cases a naive `split(" ")` gets wrong:
collapsing repeated whitespace, and the empty string. **9 passed.**

**Arm B, `local-lmstudio`, qwen3.8-27b via OpenCode.** Two tasks. Spec: create
`to_roman()` for 1..3999 plus its tests. Produced a table-driven converter with a
Google-style docstring, a `ValueError` guard, and full annotations. **21 passed.**

**Capability-aware decomposition was visibly doing its job:** the same shape of
spec became **one** task for the stronger model and **two** for the weaker one.

Two behavioural notes. Arm B's task 1 also wrote `test_roman.py`, which was task
2's job, so the worker crossed the task boundary. Task 2 then found its work
already present and refactored the tests into a parametrized form rather than
duplicating them, which is a reasonable recovery but means the two leaves
overlapped. Second, with the merge gate on, task 2 stayed `pending` until task 1
was **merged**, not merely passed.

## The reasoning_effort question: closed, not re-measured

`feat/harness-parity-contract` had already landed on `main`, so per the brief
this was a single confirmation.

Config side, read from a live worker container at
`/home/agent/.config/opencode/opencode.json`:

```json
"models": { "qwen3.8-27b": { "options": { "reasoningEffort": "none" }, ... } }
```

camelCase, provider id `lmstudio` (dot-free), both as the gotchas require.

Wire side, via a logging reverse proxy in front of LM Studio: **28 of 28**
captured `POST /v1/chat/completions` carried `"reasoning_effort": "none"`, zero
absent. OpenCode's transform layer does convert the camelCase config key into the
snake_case wire field.

Run #3 intercepted 18 completions and found not one carrying an effort key. The
performance difference is the headline consequence: run #3's Arm B took
**45 m 38 s** from dispatch to reviewed PR; here each Arm B leaf took **73 s and
72 s**. Same model, same harness, same machine. Stating the effort is worth
roughly an order of magnitude on this workload.

## Confirmed fixed since run #3

- **`praxis submit` carries the specification.** This was run #3's headline
  defect and the reason its score dropped. Verified live from a fresh install
  through the documented CLI path: the submitted text was committed to the target
  repo as `docs/superpowers/specs/2026-08-20-implement-the-initials-helper-...md`,
  `praxis plans` showed `spec_path` populated rather than empty, the doc body
  matched the submitted sentence verbatim, and the resulting plan implemented the
  thing the spec asked for. Both arms.
- **Worker effort is stated per harness.** See above. Also visible on the agy
  side, where the spawned container carried `MODEL=Gemini 3.7 Flash (High)`, the
  `model_name` effort channel.
- **A fresh clone reaches a green doctor unaided**, second run running. Arm A
  printed `All checks passed.` with all twelve green on the first correct `init`.

## Defects, ranked

**1. HIGH. The CLI cannot open the merge gate, so the loop cannot be closed from
the CLI.** This is the run's headline. `praxis pending` lists the parked task but
prints **no task id at all**, and truncates both the branch and the PR URL, so it
hands you nothing you can act on. `praxis approve` targets plans and returns
`404 Plan not found` against a task id, tried with both the 8-char form the tables
print and the full uuid. The working route is `POST /api/tasks/{id}/approve-merge`,
documented at `docs/deployment.md:533`, so it is discoverable but is REST-only.
Because a dependent task stays `pending` until its predecessor is **merged**, this
is one forced `curl` per task boundary, not one at the end.

**2. HIGH, new. Any key `Settings` does not declare in `.env` hard-crashes the
orchestrator at startup.** `docker-compose.yml` mounts `./.env:/app/.env:ro` for
the `env_drift` doctor check, so pydantic-settings parses that file whole inside
the container and `extra_forbidden` aborts startup:

```
pydantic_core._pydantic_core.ValidationError: 1 validation error for Settings
claude_vpn_killswitch_off
  Extra inputs are not permitted [type=extra_forbidden, input_value='1', ...]
ERROR:    Application startup failed. Exiting.
```

`.env` is the documented place to put configuration. The error names the key but
never says the source is `.env` or that extras are forbidden, and the container
then restart-loops, so the symptom is "the dashboard stopped answering" some time
after an unrelated edit.

**3. HIGH, environment-specific but total. Mounting `~/.claude` imports the
operator's Claude Code hooks into the container, where their assumptions do not
hold.** The mount at `docker-compose.yml:33` is deliberate and necessary, but it
also brings hooks. This host's `UserPromptSubmit` hook runs a detector built on
`ipconfig`, a Windows command absent in the Linux container, so it fails closed on
**every** brain call. Proven both ways in the same minute: host `claude -p`
returned `PONG` while `docker exec orchestrator claude -p` returned
`BLOCKED by VPN killswitch - OpenVPN tunnel is down`. Inside the loop this
surfaces as `ValueError: Could not extract JSON from response: ...`, which reads
as a product bug. **This corrects the standing handoff advice:** the tunnel being
up does not help and reconnecting cannot fix it. Cost 8 m 34 s here and cost the
previous session its final end-to-end confirmation.

**4. MEDIUM, new. A GitHub 504 after a successful merge leaves the task stuck
while the PR is merged.** `gh pr merge --squash --delete-branch` returned
`504 Gateway Timeout`, Praxis surfaced `merge failed`, and the PR was in fact
already `MERGED` on GitHub seconds earlier. The task stayed `passed` and kept
appearing in `praxis pending`, so the plan could not advance. **This happened on
two of three merges**, so it is reproducible here rather than a rare blip. A
manual retry recovered cleanly both times, so there is no data loss, but nothing
retries automatically and the operator sees a scary error contradicted by GitHub.

**5. MEDIUM. The verify gate never ran in either arm.** Every review logged
`verify gate skipped: no verify_cmd configured`. The mechanical-gate-before-model-review
property the README sells was inert for this entire run, because `verify_cmd` is
still settable only by raw `PATCH /api/projects/{id}`. On a repo whose tests are
the whole point of the task, this is the gate most worth having.

**6. MEDIUM. `add-project` demands a `MODEL` described as "LM Studio model
name"**, with no `--harness` flag, even when the chosen preset is agy/Gemini and
LM Studio is not involved.

**7. LOW, confirmed as known.** `praxis init`'s agy recipe names
`agy-agent:latest` before that image exists; tables print 8-char ids that the API
then rejects; `/health` reports commit `dev` so the build-stamp check is a
permanent NOTE; the callback URL renders truncated as
`http://host.docker.internal:1232?`; README says Gemini 3.6, `config/praxis.yaml`
and `.env` say 3.7, `docs/deployment.md` says 3.5.

**8. LOW, new.** The documented `praxis-gemini-creds` PONG check auto-creates an
empty root-owned volume when the volume is absent, so missing credentials present
as `mkdir ... permission denied` with a Go stack trace rather than "not logged
in".

## Score: 7/10, reasoning

Up from 6/10, and the rise is narrower than it looks: run #3's drop was caused by
one correctness failure, `praxis submit` discarding the spec, and that is fixed
and verified live. What remains is a last-mile problem rather than a correctness
one.

What earns the 7. The engine does the hard part well. Two arms, two different
providers, three tasks, **all correct on the first try with zero retries**,
verified by running the tests rather than reading diffs. The spec now genuinely
crosses the handoff. Capability-aware decomposition visibly sized the same spec
differently for a stronger and a weaker model. Effort is stated on the wire, and
that alone took an Arm B leaf from 45 minutes to 73 seconds. A fresh clone
reaches an all-green doctor in about three and a half minutes, twice running.

What holds it back. The question this run existed to answer is answered **no**:
a newcomer cannot get from `praxis submit` to a merged PR on the CLI alone, and
because dependent tasks wait for a *merge* rather than a *pass*, the gap bites
once per task, not once per plan. `praxis pending` compounds it by printing no
id. Two new real defects surfaced that only running the product could find: an
`.env` key crashing startup, and a GitHub 504 leaving a merged PR looking failed
on two of three attempts. And the verify gate, the cheap deterministic guard the
architecture is proudest of, did not run once all night.

It is not an 8 because the primary loop still cannot be driven to completion by
the interface the README points a newcomer at. It is not a 6 because nothing this
run found produces *wrong work*, which is exactly what separates it from run #3.

## What to fix, ranked

1. **Add the merge-gate verbs to the CLI**: `praxis merge <task-id>` and a
   plan-level batch equivalent, and make `praxis pending` print the task id.
   This single item is what turns the run's answer from no to yes.
2. **Stop `.env` extras from killing startup.** Either ignore undeclared keys
   when parsing the mounted file, or fail with a message that names `.env` as the
   source and tells the operator to move the key into compose.
3. **Make the merge idempotent under a timeout.** Before reporting `merge
   failed`, re-read the PR state; if it is already `MERGED`, record success.
4. **Make `verify_cmd` reachable** from `praxis configure`, so the mechanical
   gate is on by default rather than skipped by default.
5. **Neutralise imported hooks in the container**, or document the trap loudly.
   Mounting `~/.claude` for credentials should not import host-OS-specific hooks;
   at minimum `praxis doctor` should probe a real `claude -p` from inside the
   container so this fails at diagnosis time rather than mid-plan.
6. Defects 6 to 8 above, plus the surviving items from the earlier list.

## Follow-up this run did not close

The twelve tasks of `2026-08-20-onboarding-blockers-2.md` remain unexecuted by
choice, and this run adds four items to that backlog: the merge-gate CLI verbs,
the `.env` extras crash, merge idempotency under a 504, and the imported-hook
trap. The non-interactive `praxis init` surface is still captured but not built,
though note this run drove the wizard end to end with piped stdin, so the
defaults path is more usable than "TTY only" suggested.

# Run #5, 2026-08-21

Product commit **`c0545fe`** (`main` == `origin/main`, clean), the merged
walkthrough-4 blockers plan. Previous scores **5/10**, **7/10**, **6/10**,
**7/10**.

Run #4 answered its question NO: the merge gate had no CLI verb, so the loop
could not be closed without `curl`. Run #5 measures whether that repair holds.

## The question this run existed to answer, in two halves

**First: can a newcomer take a spec from `praxis submit` all the way to a merged
PR using only the CLI, never reaching for `curl`?**

**Almost, and the gap is one specific step.** Everything through the per-task
merge gate now works on the CLI and works well: `submit`, `plans`, `tasks`,
`pending`, `merge`. No `curl` was needed for any of it, in either arm, across
four plans. But when the last task merges, Praxis opens an **integration PR to
`main`** and the CLI never mentions it. `praxis pending` says
`Nothing awaiting approval.` while two integration PRs sit open on GitHub.
`praxis plans` shows `completed` with no URL. `praxis merge-plan` against the
finished plan returns `Merged: 0 task(s)` and exits **0**. The PR URL exists in
exactly one place, the orchestrator's own log:

```
2026-08-21 03:40:01,881 INFO [orchestrator.core.git_ops] Opened integration PR:
  https://github.com/adiatmaja/playground/pull/48
```

So the newcomer's spec reaches a plan branch and stops there. The work is not on
`main`, and nothing the product prints says where it went. Run #4's fix moved the
wall one step later rather than removing it.

**Second: can they close the terminal, come back the next day, and do it again?**

**No, and this is the sharper failure of the two.** In a shell without the
exported variables, every CLI command dies on one line:

```
$ uv run praxis projects
Set AUTH_TOKEN (or ORCHESTRATOR_TOKEN) env var
```

That is standing in the repo root, with `AUTH_TOKEN` sitting in `.env` two feet
away and the port declared in `docker-compose.yml`. The CLI reads neither.
Worse, **`README.md` and `docs/deployment.md` contain zero occurrences of
`ORCHESTRATOR_TOKEN` or `ORCHESTRATOR_URL`** (checked by grep). Those names were
printed exactly once, by `praxis init`, into the terminal that is now closed.

The only recovery the product offers is to re-run the entire wizard, which does
reprint them and took **27 s** but also rebuilds the container, re-prompts for a
GitHub token, and re-asks the agy credential confirmation. It works. It is the
wrong instrument, and a newcomer has no way to know it is the one that works.

## Method

Identical to runs #2, #3 and #4. Fresh `git clone` into
`C:\working-space\praxis-newcomer`, no `.env` copied, data volume removed between
arms, `opencode-agent:latest` and `agy-agent:latest` deleted first. Treated as
available: `README.md`, the docs it links, `.env.example`, and the running
product's own output. Everything else out of bounds and logged as a leak.

Target `adiatmaja/playground`, reset to a single `main` before the clock. Two
specs per arm's worth of work, all small and verifiable by running the repo's
own suite.

**Caches were warm** (Docker layers and `uv`), so every build number is a floor.
The 2026-08-14 cold-machine measurement was **+19 minutes**.

### Prerequisites, confirmed with real calls before the clock

Every one by a real call, never an existence check:

- Docker Desktop **29.6.1** up.
- LM Studio at `https://pcllm.sigmasolusi.com` answered a real
  `POST /v1/chat/completions` on `qwen3.8-27b`, generating tokens.
- `praxis-gemini-creds` seeded: the documented `agy -p` returned **`PONG`**.
- Host `claude -p` reachable.

### Pre-clock teardown, and one finding it handed over free

Run #4's leftovers were still present and had to be removed: the compose project,
its data volume, both agent images, the clone, and **four branches on the target
repo** (`agent/implement-roman-converter`, `agent/write-roman-tests`,
`plan/2026-08-20-implement-initials-helper`,
`plan/2026-08-20-roman-numeral-converter`). All four had been merged a day
earlier and none had been swept. That is the known "merged branch is swept by
nothing" item, confirmed by observation rather than by reading code, and this run
later found *why* (see defect 4).

## Phase timings, unrounded

Clock started **10:10:02** with both agent images deleted.

| Phase | Elapsed |
|---|---|
| `git clone` | 3 s |
| `uv venv && uv sync --extra dev` | 4 s |
| `praxis init`, Enter-only | 1 s, stops by design, exit 1 |
| `praxis init`, Arm A, both images + orchestrator + doctor | 51 s |
| VPN killswitch: diagnose from doctor output, patch compose, verify | **17 s** |
| `praxis doctor`, all green | 6 s |
| Arm A: `add-project` / `configure --verify-cmd` / `submit` | 3 s / 1 s / 5 s |
| Arm A: plan activated | 13 s |
| Arm A: task 1 dispatch to parked at gate | 91 s |
| Arm A: `praxis merge` task 1 | 5 s |
| Arm A: task 2, three attempts, **plan failed** | 184 s |
| Arm A run 2: submit to parked at gate | 124 s |
| Arm A run 2: merge, task 2, merge, plan completed | 4 s / 110 s / 5 s / 3 s |
| **Returning operator: clean shell** | **blocked outright** |
| Returning operator: recover by re-running `praxis init` | 27 s |
| Arm B: `praxis init` preset 2 (worker endpoint FAIL) | 17 s |
| Arm B: set `LM_STUDIO_URL`, `up -d`, doctor green | 16 s |
| Arm B: `add-project` / `configure` + `submit` | 2 s / 4 s |
| Arm B: task 1 dispatch to parked at gate | 137 s |
| Arm B: merge, task 2, merge, plan completed | 6 s / 293 s / 5 s / 7 s |

**Clock start to an all-green `praxis doctor` on a fresh clone: 3 m 40 s.**
Of that, 83 s was command time and the rest was reading the doctor's own output.

**Arm A, clean plan, submit to integration PR: 5 m 00 s.**
**Arm B, submit to integration PR: 8 m 04 s.**

The 15-minute target is met comfortably on the documented path in both arms. It
is the two questions above, not the clock, that this run fails.

## The killswitch cost 17 seconds instead of 8 m 34 s

This is the clearest single improvement since run #4, and it is worth stating
precisely because it is the payoff of a fix that looked cosmetic.

Run #4 lost **8 m 34 s** to the VPN killswitch, because the old `planner_cli`
check went green whenever the binary existed and the failure surfaced minutes
later, mid-plan, as `ValueError: Could not extract JSON from response`. The
rewritten check now runs a real round trip and reports the blocking hook's own
words, at setup time, inside `praxis init`:

```
FAIL Planner CLI answers a test prompt: the CLI is authenticated but something
refused the prompt. Check for a Claude Code hook in the mounted ~/.claude whose
detector assumes the host OS; see docs/gotchas.md
  [Operation stopped by hook: BLOCKED by VPN killswitch - OpenVPN tunnel is
   down. Prompt not sent.]
```

That names the cause, the mechanism, and the file to read. Diagnosis to verified
fix took 17 s.

**The remedy half is still missing, though.** `docs/gotchas.md:981` confirms the
diagnosis in detail but offers no action: it never mentions
`CLAUDE_VPN_KILLSWITCH_OFF=1`, nor that it must be a compose literal rather than
an `.env` key. The doctor points at a door that opens onto an explanation, not a
fix.

## Leak log

| Severity | Leak | Source |
|---|---|---|
| HIGH | The LM Studio endpoint `https://pcllm.sigmasolusi.com`. Reproduced exactly: `local-lmstudio` defaults to `host.docker.internal:1234`, doctor FAILs with `worker endpoint did not answer a usable GET /v1/models`, and the remedy says "start the endpoint and load the configured model", which assumes the default is right. The detail line **does not print the URL it probed**, so the newcomer cannot even see what to correct, and nothing names `LM_STUDIO_URL`. | Standing leak, reproduced from runs #2 to #4 |
| HIGH | That the killswitch has a `CLAUDE_VPN_KILLSWITCH_OFF=1` escape hatch and that it must be a compose literal. Downgraded in one half: the *diagnosis* is now fully discoverable from product output plus the doc it cites. The *remedy* is not written anywhere in the product. | Prior-run knowledge |
| MEDIUM | Pre-seeding `praxis-gemini-creds` before the clock rather than counting interactive OAuth as a measured phase. | Method choice, matches runs #1 to #4 |
| MEDIUM | Reading `docker/agy-agent/entrypoint.sh`, `api/internal.py` and `core/agent_manager.py` to explain the Arm A plan failure. Logged because it is out of persona; it happened only **after** the newcomer path had already dead-ended at "Agent finished with status failed", which is itself the defect. | Deliberate diagnosis |

**Not leaks, checked deliberately:** the GitHub token came from `gh auth token`,
which any newcomer has an equivalent of. The worker model strings
`Gemini 3.7 Flash (High)` and `qwen3.8-27b` both came from the install's own
`.env`, which is product output.

## What both arms actually produced

**Correct work, first try, zero retries, on every task that had work to do.**
Verified by cloning each plan branch and running the repo's own suite, not by
reading the diff.

| Arm | Harness / model | Tasks | Result |
|---|---|---|---|
| A | `agy` / Gemini 3.7 Flash (High) | 2 | `truncate()`, **29 passed** |
| B | `opencode` / qwen3.8-27b | 2 | `word_count()`, **30 passed** |

Both produced full type annotations and Google-style docstrings, and both got the
boundary conditions right unprompted (`limit < 1` raising `ValueError`;
whitespace-only input returning an empty dict).

Token telemetry reconfirmed the harness-parity split without re-measuring it: agy
reported `total_tokens` 148,232 and 158,296 on its two runs; both opencode runs
recorded `tokens_used=None, tokens_source=unavailable`.

## Defects, ranked

### 1. HIGH: the integration PR to `main` is invisible to the CLI

The loop's final artifact has no CLI surface at all. `pending` reports
`Nothing awaiting approval.` with two integration PRs open; `plans` prints
`completed` and no URL; `merge-plan` on the completed plan returns
`Merged: 0 task(s)` and **exits 0**, which reads as success. Reproduced in both
arms (PR #45 and PR #48). This is the direct successor to run #4's headline and
the reason half one of this run's question is still not a clean yes.

### 2. HIGH: a returning operator cannot reach their own install

Covered above. The fix is small and obvious: have the CLI fall back to the
`AUTH_TOKEN` in `./.env` and the port in the compose file when the environment is
empty, or add a `praxis env` that reprints the export block without rebuilding
anything. Either would have turned a hard stop into nothing at all.

### 3. HIGH: an empty diff is treated as failure, and it killed a whole plan

**This is the most valuable new finding, and it needed interception to see.**

Arm A's first plan decomposed into "Create slugify module" and "Create slugify
tests". Task 1 wrote **both** files. Task 2 therefore had nothing to change. The
worker did the right thing and said so:

```
No changes produced by agy
  harness_rc=0
  report_status=DONE
  envelope_status=SUCCESS
  envelope_num_turns=1
...
- Full acceptance test suite (`python -m pytest -q`): 26 passed.
```

The entrypoint's "no changes" branch turned that into `failed`. It retried three
times, got the identical correct answer each time, exhausted retries, and
**failed the plan** even though the repository was already in exactly the state
the spec asked for.

The boundary crossing itself is known from run #4 and reproduced in **every plan
of both arms this run** (4/4: task 1 always wrote task 2's file too). What is new
is the consequence: when the later leaf then declines to touch the file, the
plan dies. Arm A run 2 and Arm B survived only because their workers chose to
expand the existing tests, producing a diff by luck rather than by design. Arm
B's task 2 added exactly 8 lines to an already-complete test file, confirmed from
its captured container log. So this is a live coin flip on every multi-leaf plan,
not an edge case.

**It is not agy-specific.** `docker/opencode-agent/entrypoint.sh:464-477` has the
same branch ending in `STATUS="failed"; exit 1`, so the fix has to land in **both**
entrypoints. Arm B never tripped it only because its worker happened to produce a
diff.

### 4. HIGH: the stale-branch sweeper targets live plan branches, and its delete is broken

Seconds after the Arm A plan failed, the reconcile loop tried to delete
`plan/2026-08-21-add-slugify-helper`, the branch holding task 1's merged work:

```
WARNING [orchestrator.core.orchestrator_reconcile] Failed to delete dead branch
plan/2026-08-21-add-slugify-helper ... git ... push <url> --delete <branch>
fatal: not a git repository (or any of the parent directories): .git
```

Two defects in one line. The sweeper classified a plan branch carrying merged
work as dead. And the delete **cannot ever succeed**: it runs `git push` with an
explicit URL but no repository, which git refuses unconditionally. That second
bug is why run #4's four branches survived overnight: the sweeper is inert by
accident. Fixing the `not a git repository` bug **without** first fixing the
classification would arm a branch-deleter that starts by destroying merged work,
and per `docs/gotchas.md` deleting a branch also closes every PR based on it.
These must be fixed in that order.

### 5. MEDIUM: an `ERROR` from agy is recorded and ignored

Captured live from the Arm A task-1 container, which Praxis then merged:

```json
{"status":"ERROR",
 "error":"declaring permissions: cortex tool write_to_file: ...
   /home/agent/workspace/src/playground/test_truncate.py is not a valid
   artifact path; artifacts must be in /home/agent/.gemini/.../brain/<id>/"}
```

The entrypoint committed, pushed, opened PR #43 and reported success. Reading
`entrypoint.sh:536-550` confirms why: `envelope_status` is parsed **only** inside
the "no changes produced" diagnostic branch. When a diff exists, an `ERROR`
envelope is indistinguishable from `SUCCESS`. Here the work happened to be
complete so it passed review, which is precisely what makes it dangerous.

### 6. MEDIUM: the CLI cannot show why a task failed, though Praxis stored the reason

`praxis task <id>` gives the newcomer this and nothing else:

```
Status: failed | Branch: agent/create-slugify-tests | Attempt: 3
Feedback: Agent finished with status failed
  7aa64bc7 | failed | 2026-08-21 03:17:11
```

The real diagnosis was captured: `api/internal.py:91` calls
`get_container_logs` and stores it on the run before the container is removed.
MCP exposes it as `get_task_logs`. The CLI has no equivalent. Everything in
defect 3 came from that stored text, reached over HTTP because no command
prints it. A `praxis logs <task-id>` would have made the whole investigation a
one-liner.

### 7. LOW: `add-project` still asks for an "LM Studio model name" on the agy preset

Unchanged from run #4, reproduced verbatim: the `MODEL` argument is described as
"LM Studio model name" while on the Gemini/agy preset where LM Studio is not
involved, and there is still no `--harness` flag.

### 8. LOW: two cosmetic-but-misleading outputs

On the agy preset the orchestrator warns on every single spawn:
`Could not detect context limit from http://host.docker.internal:1234/api/v0/models`,
probing LM Studio for a harness that does not use it. And `praxis doctor`
truncates its own callback URL mid-value
(`http://host.docker.internal:1232?`), hiding the digit that check exists to
verify.

### 9. LOW: an unrequested `autonomous` plan appears with a blank spec

After the returning-operator recovery, `praxis plans` listed a third plan the
operator never submitted, `source=autonomous`, `status=pending`, with an **empty
Spec column**. It is the improvement loop doing its job, but to a newcomer it is
an unexplained row with no description.

## Confirmed fixed since run #4

Every one verified by doing, not by reading:

| Item | Evidence |
|---|---|
| Merge gate has a CLI verb | `praxis merge <task-id>` opened the gate **four times** across both arms, 4-6 s each, zero `curl` |
| `praxis pending` prints a copyable line | `praxis merge c3e200b3-...  # Create slugify module` plus the full PR URL, at every default width |
| Full ids everywhere | `projects`, `plans`, `tasks` all printed 36-char ids that the API accepted verbatim |
| `--verify-cmd` on `praxis configure` | the gate ran on **every** task and every plan branch: `verify gate passed`. In run #4 it ran zero times |
| `planner_cli` doctor check is real | caught the blocked hook at setup; 8 m 34 s down to 17 s |
| README documents the merge gate | Quick Start lines 227-229 and step 6 name both verbs |
| `praxis submit` carries the spec | not re-litigated; the Spec column showed the committed `docs/superpowers/specs/...` path on all four plans |

## Score: 7/10, reasoning

Flat against run #4, and the flatness is the point: the blocker moved rather
than cleared.

**What earned it.** Setup is now genuinely good. Clone to an all-green doctor in
**3 m 40 s**, `praxis init` building both agent images and starting the
orchestrator in **51 s**, and a doctor that catches a blocked planner at setup
instead of letting it detonate mid-plan eight minutes later. The per-task loop
closes cleanly on the CLI, exactly as promised, four times out of four. The verify
gate went from never running to running on every task and every plan branch. Both
arms produced correct, well-annotated, first-try work on models an order of
magnitude apart in cost, and capability-aware decomposition visibly did its job.

**What held it down.** Both halves of this run's question still answer "no". The
spec does not reach `main` on the CLI, because the integration PR is invisible to
every command. The returning operator is stopped dead by a missing environment
variable whose name appears nowhere in the documentation. And a plan can now be
killed outright by a leaf that correctly reports it has nothing to do: a defect
that fires on a coin flip, that no unit test would catch, and that only showed up
because the container's own output was intercepted before Docker removed it.

The gap between 7 and 9 is three small changes: a `praxis logs`, an `.env`
fallback in the CLI, and making `pending` list integration PRs.

## What to fix, ranked

1. **Make `pending` list integration PRs** and give `merge-plan` something to do
   when a plan is complete. Non-zero exit and a plain URL line, same shape as the
   per-task line that already works well.
2. **Let the CLI find its own install.** Fall back to `./.env`'s `AUTH_TOKEN` and
   the compose port. Failing that, `praxis env` to reprint the exports without
   rebuilding.
3. **Stop treating an empty diff as failure.** A worker reporting `DONE` with
   `envelope_status=SUCCESS` and a passing suite has succeeded; the leaf is a
   no-op and should close as such. Cheapest correct version: if the verify command
   passes on the base branch, pass the leaf.
4. **Fix the sweeper's classification before fixing its delete.** In that order,
   for the reason in defect 4.
5. **Add `praxis logs <task-id>`.** The data is already stored and already
   exposed over MCP.
6. **Gate on agy's `envelope_status`** outside the no-changes branch.
7. **Print the URL the worker-endpoint check probed**, and name `LM_STUDIO_URL`
   in its remedy. This single line would close a leak that has now stood for four
   consecutive runs.

## Follow-up this run did not close

The twelve tasks of `2026-08-20-onboarding-blockers-2.md` remain unexecuted, and
the non-interactive `praxis init` gap is now measurably worse than "TTY only"
suggested: the wizard's **question set changes with state**. On a fresh clone it
asks five questions; once `.env` exists it asks seven, inserting "Reuse the
AUTH_TOKEN already in .env?" first and "Update .env?" last. A piped answer set
that works on the first run silently misaligns on the second and aborts. That is
the one place this run had to guess twice.

Also untouched: no `praxis reject-merge`, so the gate remains one-way from the
CLI; the all-whitespace `verify_cmd` hole; and the doctor probing the CLI default
model rather than the configured planner.

# Run #6, 2026-08-21

Product commit **`92bc3e3`**, the walkthrough-#5 blocker fixes plus
`praxis logs`. Previous scores **5/10**, **7/10**, **6/10**, **7/10**, **7/10**.

Run #5 answered its question ALMOST: everything worked up to the last link, and
the integration PR was invisible to the CLI. Run #6 measures whether the repair
holds, and whether an empty diff has stopped meaning failure.

## The question this run existed to answer

**Can a newcomer take a spec from `praxis submit` all the way onto `main`,
using only the CLI, never reaching for `curl`?**

**Yes.** For the first time in six runs, the loop closed completely. A spec went
in, two tasks were planned, dispatched, reviewed, merged to the plan branch, and
the plan's integration PR merged onto `main`, with `duration.py` and
`test_duration.py` landing there. Zero `curl`. Every step was reachable from a
printed, copyable command.

**Score: 8/10.** The point deducted is not for friction. It is for a new HIGH
defect this run found, described below, in which Praxis reports a change as
reviewed and merged while the worker's actual commit is left behind.

## Setup: 70 seconds, the best of the six runs

| Step | Time |
|---|---|
| `git clone` | 5 s |
| `uv venv && uv sync --extra dev` | 5 s |
| `praxis init --non-interactive --accept-preset-requirements` | 60 s |
| **To a running orchestrator** | **70 s** |
| Killswitch workaround, recreate, re-doctor | ~35 s |
| Re-init with a GitHub credential | 21 s |

`praxis init --non-interactive` worked first try and is the single largest
usability change since run #1. No wizard, no piped newlines, no answer set that
misaligns on the second run.

**The unmet-requirements guard fired, and that is worth recording.** Run with no
`--preset`, init picked the deployment default (`gemini-agy`), refused in **2 s**
because that preset needs an interactive login, printed the two-command setup
recipe, and named both escape hatches (`--accept-preset-requirements`,
`--preset <name>`). It also wrote the partial `.env` rather than discarding the
token it had already resolved. A newcomer-agent gets a correct, actionable stop
instead of an install that starts, reports healthy, and fails its first task.

## The three run-#5 blockers, measured

**1. Integration PR visible and mergeable: FIXED, verified live.**

```
$ praxis plans <project-id>
| d8275b9c-... | docs/superpowers/sp... | user | completed (PR open) |

$ praxis pending
           1 plan(s) awaiting integration
| 0h | plan/2026-08-21-add-format-duration-helper |

praxis merge-plan d8275b9c-d335-429f-bebc-a5f94f6c58be   # integrate onto the base branch
  PR: https://github.com/adiatmaja/playground/pull/51

$ praxis merge-plan d8275b9c-d335-429f-bebc-a5f94f6c58be
Integrated: plan merged to its base branch
  PR: https://github.com/adiatmaja/playground/pull/51
```

Afterwards `praxis pending` says `Nothing awaiting approval.` and means it, and
`praxis plans` reads `completed (integrated)`. In run #5 that same sentence was
printed with two integration PRs open. The status suffix is what closes the
interpretive gap: `completed` alone reads as "landed" and never was.

**2. CLI finds its own install: FIXED, verified live.** In a shell with
`AUTH_TOKEN`, `ORCHESTRATOR_TOKEN` and `ORCHESTRATOR_URL` all explicitly unset,
standing in the install directory:

```
URL:   http://localhost:12323
       from PORT in C:\working-space\praxis-newcomer\.env
Token: AUTH_TOKEN in C:\working-space\praxis-newcomer\.env
```

Run #5's returning-operator case is closed.

**3. Empty diff is a no-op: NOT EXERCISED. Unverified.**

This is the honest result and it must not be read as a pass. The plan decomposed
into exactly the shape that triggers it, "Implement format_duration helper" then
"Write unit tests for format_duration", and **task 1 wrote the tests too**
(`test_duration.py`, +33 lines). That is 5 of 5 plans across two runs in which
task 1 writes task 2's file.

Task 2 still produced a diff: it wrote a second, overlapping set of tests. The
reviewer noticed and passed it anyway, calling it "minor overlap with the
pre-existing parametrized `test_format_duration` suite, but this is harmless
redundancy rather than a defect". So the worker took the coin flip run #5
described, and the no-op path was never reached.

A deliberate attempt to force it, by re-submitting the identical spec against a
repository that already satisfied it, did not reach it either: the worker again
found something to write. **The `no_changes` path remains unverified live after
two attempts to provoke it.** It is unit-tested at both the entrypoint and the
orchestrator, but the entrypoint half has never been observed firing in
production.

## NEW, HIGH: a re-submitted spec makes Praxis merge the wrong PR

Found by the second attempt above, and it is the most serious defect in six runs
because it silently discards reviewed work while reporting success at every
layer.

Two plans from the same spec text produce the same slugs, so the second plan
reuses the first plan's branch names, both the `plan/<date>-<slug>` branch and
the `agent/<task-slug>` branch. The worker for plan 2 pushed a genuine new commit
to `agent/implement-format-duration`, and then:

```
--- Creating PR ---
Reusing existing PR: https://github.com/adiatmaja/playground/pull/49
PR created: https://github.com/adiatmaja/playground/pull/49
```

PR #49 is plan 1's task PR. It was **already merged**, and its base was plan 1's
branch. The cause is one missing filter in `docker/*/entrypoint.sh`:

```bash
if PR_URL=$(gh pr view "${BRANCH}" --json url --jq .url 2>/dev/null) && [ -n "${PR_URL}" ]; then
    echo "Reusing existing PR: ${PR_URL}"
```

`gh pr view <branch>` resolves a branch to a PR **regardless of state**, so a
merged or closed PR is happily "reused". The orchestrator's own equivalent,
`_existing_integration_pr` in `core/orchestrator_review.py`, deliberately does
the opposite, and its docstring explains why a POSITIVE open-state check matters.
The two halves of the product disagree with each other.

What was measured:

- `agent/implement-format-duration` is **1 commit ahead** of the plan branch
  (`gh api compare` returned `status=ahead ahead=1 behind=0`). That commit is
  real, new, and unmerged.
- The task is parked at `passed`, pointing at PR #49.
- PR #49's files are the three from plan 1. So the review that passed judged
  **already-merged code**, not the worker's commit. The stored feedback describes
  plan 1's implementation.

What was NOT executed, and why: merging that task would discard the commit, so
the last step is confirmed by reading `GitOps.merge_pr` rather than by running
it. On a non-zero `gh pr merge` it calls `_pr_is_merged`, which returns true for
#49, logs "reported a merge error but GitHub says it is merged; treating as
success", and returns cleanly. The task would be marked MERGED and the commit
would never reach the plan branch.

Severity is high because every layer reports success: the worker says DONE, the
review passes, the merge reports merged, and the change is not there. That is
precisely the failure mode the gated loop exists to prevent.

The trigger is ordinary, not exotic: re-submitting a spec after a failure, or two
plans that summarise to the same slug on the same day.

## `praxis logs` earned its place immediately

Run #5's ranked item 5, shipped in `92bc3e3`, used four times in this run. It is
what produced the finding above: the "Reusing existing PR" line exists only in
the container transcript, and the container was removed minutes before anyone
thought to look. It printed the agy envelope, the token count (275,614 on that
run), the commit, the push and the PR resolution.

Before this verb that diagnosis required curling `GET /api/tasks/{id}` and
extracting `runs[].logs` from JSON by hand.

## Leaks that persist

| Sev | Leak | Age |
|---|---|---|
| HIGH | The VPN killswitch remedy, `CLAUDE_VPN_KILLSWITCH_OFF=1` as a compose LITERAL, is written **nowhere in the product**. The doctor now diagnoses it precisely and points at `docs/gotchas.md`, and that document explains the diagnosis without ever stating the fix. Verified this run by grep: the string appears only in this walkthrough log and in a superseded plan file. | 5 runs |
| MED | `praxis config show` is documented in the README as the way to list preset names, but it needs a running orchestrator, which is what you are trying to set up. On a fresh clone the only source of preset names is reading `config/praxis.yaml`. | new |
| MED | The `tasks` table folds a uuid across three lines at an 80-column console, so ids are still not copyable there. `pending` and `plans` both print a clean copyable line; `tasks` does not. | 3 runs |
| LOW | agy reported a real internal tool error ("invalid artifact path") inside its envelope, still reported DONE, and still committed. The error reaches `agent_runs.logs` and nothing reads it. | new |

## What run #7 should measure

The no-op path is the only run-#5 fix still unverified, and two natural attempts
failed to provoke it. Forcing it needs a task whose file the previous task
provably completed AND whose worker cannot find anything to add: a docs-only or
config-only leaf is the likeliest shape.

Otherwise the merged-PR defect above is the top fix, and it should land before
run #7 so that run measures a product with no known silent-work-loss path.
