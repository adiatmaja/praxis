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

## Stratification

Pre-stratified on published per-instance metadata: gold-patch size
(1 file under 5 lines / 2 files or 5 to 100 lines / 3+ files or over 100 lines)
crossed with repo size (under 100 / 100 to 500 / 500+ tracked files). Fixed
sample per cell, published seed (`bench/config.SAMPLE_SEED`), and the drawn
instance list committed under `bench/samples/`.

**Measured against SWE-bench Lite, only 4 of those 9 cells are populated.**
Verified over all 300 Lite instances on 2026-08-07: every gold patch touches
exactly 1 file (so the `large` bucket, which needs 3+ files or over 100 lines,
is empty), patch size runs 1 to 76 changed lines with a median of 6, and the
smallest repo is `psf/requests` at 121 tracked files (so the `tiny` bucket,
under 100, is empty). The boundaries above come from arXiv 2505.23419, which
describes full SWE-bench; Lite is filtered to single-file patches and does not
span them. The pilot therefore draws 16 instances across `small`/`medium`
crossed with `mid`/`big`, not 30 across 9 cells. Re-cutting the boundaries for
Lite's real distribution, or moving to a corpus that spans the published ones,
is an open design decision recorded in the plan's execution record.

## Grading

The OFFICIAL SWE-bench evaluation harness, run against the patch extracted from
the final branch (`git diff base...result`). Praxis never grades itself.

## Running it

```bash
# One-time: prepare instances as local bare repos at the buggy base commit
uv run python -m bench.prepare --sample bench/samples/lite-pilot-16.json

# Pilot: 16 Lite tasks, conditions A and B, one worker
uv run python -m bench.runner --sample bench/samples/lite-pilot-16.json \
    --conditions A,B --worker local-openweight

# Grade and report
uv run python -m bench.grade --run bench/.work/runs/<run-id>
uv run python -m bench.report --run bench/.work/runs/<run-id>
```

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
