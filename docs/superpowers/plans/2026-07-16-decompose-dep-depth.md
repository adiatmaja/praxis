---
title: Relax decompose dependency-depth limit and discourage over-serialization
spec_path: docs/superpowers/plans/2026-07-16-decompose-dep-depth.md
---

# Fix: decompose deepens the dependency graph past max_dep_depth and rejects the whole plan

## Problem

The leaf-task capability constraint `max_dep_depth` defaults to **3**. A naturally
serial pipeline (a combined test+implementation leaf, then an integration leaf, then a
cross-cutting leaf, then a docs leaf) is already depth 3, and any single extra serial
stage overflows the limit. When decomposition linearizes independent leaves into a
chain, the depth climbs to 4 and the **whole plan is rejected** by the deterministic
leaf validator (`_check_dep_depth` raises a HARD violation).

Two independent contributors:
1. The default limit of 3 is too tight for legitimate docs-terminated TDD pipelines.
2. The decompose prompt does not discourage chaining otherwise-independent leaves,
   which inflates dependency depth unnecessarily.

## Fix

Raise the default `max_dep_depth` from **3** to **4**, and add explicit decompose-prompt
guidance to only add a `depends_on` edge when a leaf genuinely consumes another leaf's
output (never to impose ordering on independent work).

### Task 1: Raise the default max_dep_depth and add anti-linearization prompt guidance

Files:
- `src/orchestrator/models/schemas.py`
- `src/orchestrator/core/leaf_validator.py`
- `src/orchestrator/core/plan_review.py`
- `tests/test_schemas.py`
- `tests/test_leaf_validator.py`

Changes:

1. In `src/orchestrator/models/schemas.py`, the `CapabilityProfile` model has the field
   `max_dep_depth: int = 3`. Change the default to `4`. Do NOT change any other field.

2. In `src/orchestrator/core/leaf_validator.py`, the module constant
   `_DEFAULT_MAX_DEP_DEPTH = 3` is the fallback used when a profile lacks the attribute.
   Change it to `_DEFAULT_MAX_DEP_DEPTH = 4` so the fallback matches the schema default.
   Do NOT change `_check_dep_depth`'s logic; it already reads
   `getattr(profile, "max_dep_depth", _DEFAULT_MAX_DEP_DEPTH)`.

3. In `src/orchestrator/core/plan_review.py`, the decompose prompt currently instructs:

   ```
   Set "depends_on" to the ids of any leaves whose output this leaf builds on (e.g.
   a leaf that edits a file another leaf creates). Only truly independent leaves get
   an empty list.
   ```

   Extend that instruction to explicitly discourage over-serialization. Add a sentence
   immediately after it, worded to this effect (keep it inside the same prompt string,
   preserve surrounding formatting and the `{...}` template placeholders elsewhere):

   ```
   Do NOT add a dependency edge merely to impose an order on independent work:
   over-serializing the graph inflates dependency depth and can cause the whole plan
   to be rejected. A leaf depends on another ONLY when it genuinely consumes that
   leaf's output (edits a file the other creates, imports a symbol the other defines).
   ```

4. In `tests/test_schemas.py`, there is an assertion that the default profile has
   `max_dep_depth == 3` (around line 451). Update it to assert `== 4`. Grep the whole
   test file for `max_dep_depth` first: only the DEFAULT assertion changes; tests that
   construct a profile with an explicit `max_dep_depth=<n>` must NOT be touched.

5. In `tests/test_leaf_validator.py`, add a regression test in the `HARD: dep_depth`
   section proving the new default admits a depth-4 chain and still rejects depth-5.
   Follow the existing `test_validate_leaves_dep_depth_within_limit` pattern, but rely
   on the default profile (do not pass an explicit `max_dep_depth`) so the test pins the
   new default. Add both a passing depth-4 case and a rejected depth-5 case. Use the
   existing `_profile()` / `_source_plan()` helpers and `LeafTask(...)` construction
   already used in that file.

Verification:

```
uv run ruff check src/ tests/ && uv run mypy src/orchestrator/ --ignore-missing-imports && uv run pytest tests/test_schemas.py tests/test_leaf_validator.py tests/test_plan_review.py -q
```

All of the above must pass. The default-depth change must not break any existing test
in `test_leaf_validator.py` or `test_plan_review.py` (those set `max_dep_depth`
explicitly, so they are unaffected). The only default-sensitive assertion is the one in
`test_schemas.py`.
