# praxis-bench

A reproducible, stratified SWE-bench evaluation of Praxis's capability-aware
decomposition, with an ablation that isolates decomposition from verification.

This package is **development-only**. It is excluded from the orchestrator
Docker image and from the 80 percent coverage gate. It runs on an operator's
machine because it needs a GPU or a subscription CLI, neither of which exists on
a CI runner.

## What it answers

Does decomposing a task to fit the implementing model's capability actually raise
the resolve rate, and if so, is it the decomposition or the per-leaf verification
doing the work?

## Design

Within-subject: the same instances, the same worker, and the same brain across
four conditions.

| Condition | What runs | Isolates |
|-----------|-----------|----------|
| A | monolithic: the whole issue as one task via `dispatch_task` | baseline |
| B | Praxis decomposition via `execute_plan` | decomposition |
| C | condition B with the verify gate disabled | is it decomposition or verification |
| D | condition B plus adaptive split-on-failure | the adaptive policy delta |

A and C are a **matched pair**: both run without a verify gate. The runner
asserts this before it starts; without it the A-versus-B comparison is
confounded by verification.

## How each condition is realized

The table above says what each arm *isolates*. This says how each arm is
*built*, so the mechanism is inspectable rather than taken on trust.

| Condition | Project `verify_cmd` | Orchestrator bench mode | `max_retries` | Decomposition |
|-----------|----------------------|-------------------------|---------------|---------------|
| A | registered, same as B | **required**: started with both flags | 1 | no, `dispatch_task` |
| B | registered | must be OFF: started with neither flag | 1 | yes, `execute_plan` |
| C | registered, same as B | **required**: started with both flags | 1 | yes, `execute_plan` |
| D | registered | must be OFF: started with neither flag | 3 | yes, `execute_plan` |

Three things in that table are easy to get wrong, and each is wrong *silently*.

**Every condition registers a `verify_cmd`, including the ungated arms.** The
project's `verify_cmd` is read in four places and only three of them are the
mechanical gate. The other two are worker-facing: `orchestrator_dispatch` uses
it as the leaf's acceptance floor when the leaf declares no check of its own,
and threads it into the worker's Bible. Neither of those consults bench mode.
So registering `verify_cmd=None` for condition C would change **what the worker
is told to do**, and B versus C would then differ in the gate *and* in the task
attempted. That confound produces a number with no interpretation and nothing
in the results shows it. The runner therefore refuses any condition, gated or
not, that resolves no `verify_cmd`.

**The gate difference comes entirely from the orchestrator's bench mode.**
`core/bench_mode.py` disables the three mechanical gate sites when both
`PRAXIS_BENCH=1` and `PRAXIS_BENCH_DISABLE_VERIFY=1` are set. Either flag alone
is refused, which means a half-set environment yields a silently **gated**
condition C wearing C's label.

**Adaptive split is disabled per project, not per process.** The engine's triage
path is always on once merged, so A, B, and C cap the project at
`max_retries=1`: a leaf never reaches the second worker-attributable failure
that triggers triage. Only D gets `max_retries=3`. `BenchClient.
register_project` derives this from the condition's `adaptive_split` flag.

### The restart requirement, which nothing verifies for you

`core/bench_mode.py` reads its two flags with `os.environ` **inside the
orchestrator process**. The runner is a separate process talking to it over
REST, so nothing the runner sets reaches the orchestrator. A gateless condition
needs an orchestrator that was **started** with both flags; a gated condition
needs one started without them.

One invocation therefore cannot mix them, and the runner refuses a condition set
that disagrees on the gate before any container spawns. `A,B,C` is a valid
*design* and an impossible *invocation*: run it as `B` then `A,C`, or the
reverse, restarting the orchestrator in between and passing the same `--run-id`
so both halves append to one attempts file.

**There is no API exposing the orchestrator's bench mode, so the runner cannot
check that the restart actually happened.** An operator who forgets it gets rows
carrying the right condition label and the wrong arm, and no number in the
report will say so. This is the one manual step in the protocol, and it is
unverified by design rather than by oversight; adding an endpoint for it is out
of scope here.

## Stratification

Pre-stratified on published per-instance metadata: gold-patch size crossed with
repo size, a fixed sample per cell, a published seed (`bench/config.SAMPLE_SEED`),
and the drawn instance list committed under `bench/samples/`.

**The upper boundaries are re-cut for SWE-bench Lite, deliberately and before
any outcome was observed.** The published boundaries (arXiv 2505.23419: 1 file
under 5 lines / 2 files or 5 to 100 lines / 3+ files or over 100 lines, crossed
with under 100 / 100 to 500 / 500+ tracked files) describe FULL SWE-bench. Lite
is filtered to single-file patches and does not span them. Measured over all 300
Lite instances on 2026-08-07: every gold patch touches exactly 1 file, patch size
runs 1 to 76 changed lines with a median of 6, and the smallest repo is
`psf/requests` at 121 tracked files. Under the published cuts the `large` patch
bucket and the `tiny` repo bucket are **structurally empty**, only 4 of 9 cells
populate, and 5 of every table's rows carry no evidence at all.

The cuts actually used are therefore:

| dimension | small / tiny | medium / mid | large / big |
|---|---|---|---|
| gold-patch size | 1 file, under 5 lines | 1 to 2 files, 5 to 15 lines | 3+ files or over 15 lines |
| repo size (tracked files) | under 500 | 500 to 1999 | 2000 and over |

Both LOWER boundaries are the published ones and are unchanged; 500 is also a
published boundary that Lite does straddle, with 27 instances below it. Only the
two outer edges (100 lines becomes 15, and 500 files is promoted from the upper
cut to the lower one with 2000 as the new upper) are fitted to the corpus. The
`files >= 3` clause is kept so a corpus that does span multiple files still
strata the published way.

All 9 cells now populate, the thinnest holding 6 instances, so the pilot draws
**36** and a full run would draw 123 (three cells hold fewer than
`FULL_PER_STRATUM`). The repo-size names are ordinal: `tiny` means the smaller
third of this corpus, not an absolute size.

This re-cut is legitimate ONLY because no outcome had been observed when it was
made. It is recorded here, in `bench/config.stratum_for`, and in the plan's
execution record so it can never be mistaken for a post-hoc adjustment, and the
report must repeat it.

### Re-drawing

```bash
uv run python -m bench.sample --pool bench/.work/pool-lite.json \
    --out bench/samples/lite-pilot-36.json
uv run python -m bench.enrich --sample bench/samples/lite-pilot-36.json
```

The second command is not optional. The pool carries patch metadata and a
tracked-file count, not the upstream issue text, and the issue text IS the
worker-facing prompt. A drawn-but-unenriched sample parses, validates, and has
the right entry count; `bench.runner` refuses it, but only once a run is already
spawning containers.

## Grading

The OFFICIAL SWE-bench evaluation harness, run against the patch extracted from
the final branch (`git diff base...result`). Praxis never grades itself.

## Running it

**Before anything else: turn the local git backend on.** The bench registers
every instance as a project whose `repo_url` is a filesystem path to a prepared
bare repo, and the REST API refuses a local path unless the deployment opts in.
Set `allow_local_repo_paths: true` in `config/praxis.yaml` and restart the
orchestrator (that file is mounted, so this is a restart, never an image
rebuild). Without it `/api/projects`, `/api/dispatch` and `/api/execute-plan`
all answer 422 with `allow_local_repo_paths` named in the detail.

```bash
# One-time: prepare instances as local bare repos at the buggy base commit
uv run python -m bench.prepare --sample bench/samples/lite-pilot-36.json

# Pilot half 1: the GATED conditions. Start the orchestrator with NEITHER
# PRAXIS_BENCH nor PRAXIS_BENCH_DISABLE_VERIFY set.
uv run python -m bench.runner --sample bench/samples/lite-pilot-36.json \
    --conditions B --worker local-openweight \
    --run-id pilot-1 --verify-cmd "uv run pytest -q"

# Now RESTART the orchestrator with PRAXIS_BENCH=1 and
# PRAXIS_BENCH_DISABLE_VERIFY=1 in its environment.

# Pilot half 2: the UNGATED conditions. Same --run-id, so both halves append to
# bench/.work/runs/pilot-1/attempts.jsonl.
uv run python -m bench.runner --sample bench/samples/lite-pilot-36.json \
    --conditions A --worker local-openweight \
    --run-id pilot-1 --verify-cmd "uv run pytest -q"

# Grade and report
uv run python -m bench.grade --run bench/.work/runs/pilot-1
uv run python -m bench.report --run bench/.work/runs/pilot-1
```

`--conditions` defaults to `A,B`, which spans both groups and is **refused on
purpose**: running the pilot has to be a deliberate choice of half, not a quiet
gating of condition A. See "The restart requirement" above.

## Cost

The pilot measures cost per task so the full run can be budgeted before it is
started. Expect the pilot to take several hours of wall clock on one machine.

## Honesty

Every report carries, by template and not by choice:

- a **contamination note** naming the worker model's training cutoff and linking
  SWE-rebench as the decontaminated alternative;
- the **correlational-anchor caveat** on the stratum boundaries;
- a hand-inspected sample of 10 failures classified plan-shaped versus
  execution-shaped (arXiv 2603.14248: decomposition only fixes the former), and
  a statement of which class dominates.

A null or negative result is published unchanged. The engineering plus the rigor
is the artifact; the number is whatever it is.
