# Worker Context Continuity Design (Spec 1 of 2)

**Date:** 2026-06-29
**Status:** Approved (brainstorming)
**Depends on:** the existing worker-context plan (`2026-06-24-worker-context-and-review-fixes.md`,
merged as `c9acfe9`) which added `context_text` / `CONTEXT_TEXT` injection and `context_scrub`.
**Followed by:** `2026-06-29-capability-aware-execution-design.md` (Spec 2), which depends on the
token-budgeting primitive defined here.

---

## Problem

A Praxis worker runs as a **one-shot Docker container per task**: clone -> implement -> commit ->
PR -> callback. Two failure modes degrade quality, both observed in real runs:

1. **Within-run context loss.** A single implement run can grow past the local model's context
   window. OpenCode (the default harness) auto-compacts; on compaction the model can lose the task
   goal, conventions, and what it was mid-way through, then drift or finish half the work. Aider
   does not compact at all: per the `1482513` commit and the `harness-context-compaction` finding,
   on overflow it sends the full payload raw, LM Studio silently truncates, and Aider reports
   SUCCESS on a partial view — surfacing in Praxis as a task marked PASSED on incomplete work.

2. **Cross-run context loss.** Each one-shot container starts cold. The orchestrating Claude Code
   session's knowledge (architecture intent, conventions, decisions) does not survive the handoff;
   today the worker sees only the task title + description + plan file + the curated `CONTEXT_TEXT`.
   On a retry or escalation, the *next* worker also starts cold — it does not know what the prior
   attempt already did or where it stalled. This is the `plan-implement-memory-handoff`
   "handoff-fidelity gap."

The fix is **continuity**: a stable reference the worker always has, plus a faithful record of
progress that survives both compaction (within-run) and re-dispatch (cross-run) — and a guarantee
that the worker is never handed more than its context window can hold.

## Non-goals / Out of scope

- **Capability-aware decomposition, escalation, and the `execute_plan` entry point** — that is
  Spec 2. This spec only makes the worker's context *durable and budgeted*; it does not decide
  whether a task is too hard.
- **Mounting local or gitignored files into the worker.** The worker stays strictly GitHub-only
  (the standing security decision). All continuity context is prose, secret-scrubbed.
- **Account/credential rotation ("9router").** Explicitly rejected: rotating accounts to dodge
  usage limits is limit-circumvention and against provider terms. Not built.

---

## Design

Two artifacts injected into the worker container, plus one pre-flight check.

```
  Praxis dispatch (per leaf task)
  ┌──────────────────────────────────────────────────────────────┐
  │  1. Build STATIC BIBLE        (immutable for the run)          │
  │  2. Build PROGRESS HANDOVER   (regenerated from git + checklist)│
  │  3. PRE-FLIGHT BUDGET CHECK   (bible+handover+prompt <= window) │
  └───────────────┬────────────────────────────────────────────────┘
                  ▼  written into the harness "always-resent slot"
  ┌──────────────────────────────────────────────────────────────┐
  │  WORKER CONTAINER                                              │
  │   AGENTS.md (OpenCode) / --read files (Aider)                  │
  │     = Bible + Handover  -> re-included after every compaction  │
  │   worker commits per checklist item (instructed by the Bible)  │
  └───────────────┬────────────────────────────────────────────────┘
                  ▼ branch commits = ground truth
  on retry/escalation: regenerate Handover from git log + checklist
```

### Component A — The Static Bible

A single curated, secret-scrubbed, size-capped markdown document, immutable for the duration of a
run. Assembled by a new `core/worker_bible.py` from these sources, in priority order (highest first,
so truncation drops the least important tail):

1. **Pinned task goal** — the leaf's title + description + acceptance criteria, stated once at the
   very top under a `# GOAL (do not lose this)` heading.
2. **Plan / spec slice** — the relevant section of the plan this leaf came from (from
   `plan_text` / `plan_path`, already plumbed).
3. **Caller-curated context** — the existing `CONTEXT_TEXT` (already scrubbed by
   `core/context_scrub.scrub_context`).
4. **Repo-local committed memory** — `CLAUDE.md`, `MEMORY.md`, `AGENTS.md`, `docs/*.md` from the
   clone (GitHub-only; best-effort). For Aider this is the existing `--read` auto-discovery; for the
   Bible we fold their content in so a single artifact carries everything.
5. **Working agreement** — a short fixed preamble instructing the worker: *commit after each
   completed checklist item with a message naming the item; keep the goal in view; do not delete
   existing functionality the task did not ask you to remove.* The per-item commit instruction is
   what makes the git spine (Component B) usable within a run.

The Bible is scrubbed again on assembly (defense in depth — repo files could contain secrets) and
size-capped. It is written into the **harness always-resent slot** so it structurally survives
compaction with no event detection:

- **OpenCode:** write to `${WORKSPACE}/AGENTS.md` (OpenCode reads it as persistent project
  instructions, re-included across compaction). If the repo already has an `AGENTS.md`, the Bible is
  prepended under a clearly fenced `<!-- praxis:bible -->` block, preserving the repo's own.
- **Aider:** pass via `--read .praxis-bible.md` (Aider re-sends read-only files each message).

### Component B — The Progress Handover

A structured record of *done / in-progress / to-do*, **not** a model-written summary. The spine is
ground truth (git + the leaf's checklist); the model may add only a single, clearly-marked,
untrusted "current intent" line.

**Leaf checklist.** Each leaf task carries an explicit ordered checklist of steps (produced by
Spec 2's decomposition; until Spec 2 lands, a single-item checklist derived from the task
description). Stored on the task row.

**Cross-run reconstruction (deterministic, the primary case).** On every (re-)dispatch — first
attempt, retry, or escalation — Praxis regenerates the Handover before building the Bible:

- Read the branch's `git log` (commit subjects name completed checklist items, per the working
  agreement) and the diff stat.
- Mark checklist items `[x]` when a commit names them, `[ ]` otherwise; the first unchecked item is
  `-> in progress`.
- Render a `# PROGRESS (resume here)` section: completed items (with commit shas), the current item,
  remaining items, and a one-line "last action" derived from the latest commit.

This means a retried/escalated worker resumes from real state instead of redoing finished work.
Reconstruction is in a new `core/progress_handover.py` (pure, given a `git log`/checklist input) so
it is unit-testable without a container.

**Within-run survival.** The Handover is part of the always-resent slot alongside the Bible, so
after a compaction the worker re-reads the checklist and the goal. The working-agreement
per-item-commit instruction lets the *worker's own* post-compaction self-orientation align with what
the next reconstruction will see. (Honest limitation: within a single `opencode run`, Praxis cannot
observe mid-run git state from outside; the within-run anchor is therefore the always-resent
checklist + goal, and the per-item commits are what make the *next* run's reconstruction faithful.
We do not attempt mid-run external git inspection in this spec.)

**Trust boundary.** The model-supplied "current intent" line is rendered under a
`> (worker note, unverified)` blockquote and is never used to mark an item complete — only commits
do that.

### Component C — Pre-flight Token Budgeting

Before dispatch, ensure the worker is never handed more than it can hold. Builds on the existing
`detect_context_limit(lm_studio_url, model_name)` in `agent_manager.py`.

- A new `core/token_budget.py` estimates tokens for `Bible + Handover + task prompt` and reserves
  headroom for the model's working/output context (a configurable fraction of the window, default
  ~60% reserved for the agent's own reasoning + expected edits).
- Estimation is a cheap char/token heuristic (≈4 chars/token) — no tokenizer dependency; the goal is
  a safety margin, not exactness.
- If the assembled context exceeds budget: first **trim the Bible** by dropping its lowest-priority
  sources (repo docs, then plan slice) down to a floor that always keeps the goal + checklist +
  caller context. If it still overflows, raise `ContextBudgetExceeded`.
- In this spec, `ContextBudgetExceeded` surfaces as a clear task failure ("context for this task
  exceeds the local model's window; split the task"). **In Spec 2 the same signal feeds
  decomposition/escalation** instead of failing — that is the seam between the two specs.
- When `detect_context_limit` returns `None` (LM Studio unreachable), budgeting is skipped
  (best-effort, same posture as the existing detection) and only scrubbing + the existing cap apply.

---

## Data model changes

- `tasks` table: add `checklist` (JSON array of `{text, done, commit_sha}`) and
  `progress_note` (TEXT, the last untrusted worker intent line; nullable). Inline
  `ALTER TABLE ... ADD COLUMN` migration guarded by a `PRAGMA table_info` check, per the
  no-ORM/inline-migration convention.
- No new tables.

## Code changes (overview)

| File | Change |
|------|--------|
| `core/worker_bible.py` (new) | Assemble + scrub + size-cap the Static Bible from its sources |
| `core/progress_handover.py` (new) | Pure reconstruction of done/in-progress/to-do from git log + checklist |
| `core/token_budget.py` (new) | Estimate tokens, trim Bible to budget, raise `ContextBudgetExceeded` |
| `core/agent_manager.py` | Accept `bible_text` + write to AGENTS.md (OpenCode) / `--read` (Aider); reuse `detect_context_limit` for budget |
| `core/orchestrator.py` | On dispatch/retry: reconstruct handover, build bible, run budget check, pass bible into `spawn_agent` |
| `core/git_ops.py` | `branch_commit_log(branch)` helper feeding handover reconstruction |
| `docker/opencode-agent/entrypoint.sh` | Write `BIBLE_TEXT` to `AGENTS.md` (prepend, preserve repo's own); keep `CONTEXT_TEXT`/`PLAN_*` for back-compat |
| `docker/aider-agent/entrypoint.sh` | `--read .praxis-bible.md` |
| `models/schemas.py` | `checklist` / `progress_note` on the task DTOs |

## Testing

- `worker_bible`: source priority ordering, secret re-scrub, repo-AGENTS.md preservation, cap.
- `progress_handover`: commit-subject -> checklist `[x]` mapping; first-unchecked = in-progress;
  worker note rendered as untrusted and never marks done; empty-log (fresh) case.
- `token_budget`: under/over budget, trim order (docs dropped before goal/checklist), floor that
  never drops the goal, `ContextBudgetExceeded`, `None` limit -> skip.
- `agent_manager`: AGENTS.md write for OpenCode vs `--read` for Aider; budget invoked.
- Entrypoints: manual container rebuild + smoke (no unit harness for bash); **rebuild both
  standalone images** per the CLAUDE.md gotcha.
- ≥80% coverage; `pytest-asyncio` `asyncio_mode=auto`; mark unit/integration.

## Risks / trade-offs

- **Within-run reconstruction is not externally observable** for a single `opencode run` — mitigated
  by the always-resent checklist + per-item-commit working agreement, not by mid-run git polling.
- **Per-item commits depend on the worker following instructions** — a weak model may commit once at
  the end. That degrades gracefully: the handover then shows one completed lump, which is still
  better than a blank cold start, and Spec 2's capability gate is what keeps genuinely-too-weak
  models off such tasks.
- **Token estimate is heuristic** — intentionally conservative (reserve headroom) to avoid the
  silent-truncation failure; risk is over-conservative splitting, which Spec 2 can tune.
