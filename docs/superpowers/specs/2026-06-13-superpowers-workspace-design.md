# Praxis Superpowers Workspace

**Date**: 2026-06-13
**Status**: Approved
**Scope**: `web/index.html`, `src/orchestrator/` (api, core, config, database), `docker/`, `docs/`

## Overview

Turn Praxis into a Superpowers-driven workspace where the full spec → plan → execute → learn
cycle is first-class in the dashboard. Today the dashboard is DB-driven, spec creation is a
plain textarea, the Superpowers skills are not integrated, and `CLAUDE.md`/`MEMORY.md` go
stale because nothing writes back to them.

This epic is one cohesive goal delivered in five units, built in order. Each unit gets its
own implementation plan (mirroring the existing `design + plan-1..N` pattern in this repo).
UX clarity (Unit E) is a lens applied across A–D, not a separate build.

```
  ┌─────────┐   ┌─────────┐   ┌──────────┐   ┌──────────────┐
  │ 1 Spec  │──▶│ 2 Plan  │──▶│ 3 Execute│──▶│ 4 Context    │
  │ brainst.│   │ writing-│   │ agents   │   │   Sync       │
  └─────────┘   │ plans   │   └──────────┘   └──────┬───────┘
       ▲        └─────────┘                          │
       └──────── fresh CLAUDE.md/MEMORY.md feeds next plan ◀┘
```

## Cross-Cutting Principles

- **LLM access policy** — subscription `claude -p` + local open-source models (LM Studio)
  only. **Never** an Anthropic API key. Haiku for lightweight tasks via
  `claude -p --model claude-haiku-4-5`; planner/reviewer model configurable (separate spec).
- **Docs are the source of truth** for spec + plan *content*. SQLite keeps runtime state
  (tasks, agent_runs, opus_state, projects config) and a thin index row per markdown file.
- **Full-context plans** — every task executes in a fresh, memoryless container, so each
  generated task must be self-contained.
- **Human review gates** — brainstorming stops at the committed spec; planning is explicitly
  triggered; Context Sync edits are approved before commit.

---

## Unit A — Dashboard Layout Fix

**Problem.** `renderDashboard()` sets `#view-container.innerHTML = renderHealthBar() +
<div class="dashboard-body">…`, but `#view-container` carries `.master-detail`
(`display:flex`, default row direction). The health bar and the body become side-by-side
flex columns, so the health bar is sized to a ~430px column and vertically centered — leaving
a large empty white area on the left while the swim lanes are crammed to the right.

**Fix.** Introduce a dashboard-specific column wrapper so the health bar is a full-width top
bar and `dashboard-body` fills the remaining height. Do not rely on `.master-detail` for the
dashboard view. Verify in light and dark themes and at the 768px breakpoint.

**Acceptance.** Health bar spans the full content width on top; swim lanes start at the left
edge of the content area; no empty gutter; side panel still opens to the right.

---

## Unit B — Docs-Aware Specs & Plans Views

**Source of truth.** `docs/**/*.md` owns spec + plan content. The `plans.spec` /
`plans.opus_plan` prose migrates out of the DB; a thin index row points at each file.

**Classification pipeline** (on startup and on a Refresh button):

1. Walk `docs/**/*.md` recursively (not just `docs/superpowers/`).
2. Cache by `path + content_hash`; only (re)classify new or changed files.
3. Step 1 — deterministic markers: frontmatter `type:`, location under `specs/` or `plans/`,
   or a `## Tasks` section with `- [ ]` checklist ⇒ plan.
4. Step 2 — ambiguous files classified by Haiku (`claude -p --model claude-haiku-4-5`),
   returning `spec | plan | other`. Fallback to a local LM Studio model if `claude -p` is
   unavailable.
5. Parse `- [x]` / `- [ ]` in plan files into a completion percentage.

**Data model.** New `doc_index` table: `path`, `category`, `content_hash`, `title`,
`branch`, `done_count`, `total_count`, `classified_by` (`marker`|`haiku`|`local`),
`updated_at`.

**API.** `GET /api/docs?category=spec|plan`, `GET /api/docs/{path}` (raw markdown),
`POST /api/docs/refresh` (re-run pipeline).

**UI.** Specs view and Plans view list cards with title, category, branch, and (for plans)
a completion progress bar. Refresh button in each view header. No live FS watch.

**Acceptance.** All `.md` under `docs/` appear correctly categorized; plan progress bars
match checklist state; Refresh re-reads from disk; unchanged files are not re-sent to Haiku.

---

## Unit C — Superpowers Lifecycle

### Stage 1 — Create Spec (interactive)

- "Create Spec" opens a **text chat panel** in the dashboard.
- Claude runs `superpowers:brainstorming` inside an **isolated container** launched with
  `--dangerously-skip-permissions` (the safe place for that flag; mirrors the existing
  Docker-agent pattern; the container has the superpowers plugin installed and the target
  repo cloned).
- **Transport — headless session relay.** The backend holds a Claude Code session id per
  conversation. Each user turn is `claude -p --resume <session_id>
  --output-format stream-json`; the stream is parsed and pushed to the chat over the existing
  SSE bus. v1 chat is **text-only** — brainstorming's own visual-companion browser is not
  embedded.
- The invocation is scoped so brainstorming **stops at writing + committing the spec**
  (`docs/superpowers/specs/<date>-<slug>-design.md`) and does **not** auto-advance to
  planning.

### Stage 2 — Create Plan from a spec

- From the Specs view: **modify spec first** (edit markdown directly, or refine via chat),
  changes commit back to the `.md`.
- A **notes / guidance** field on the Create Plan action is injected into the
  `superpowers:writing-plans` prompt alongside the spec.
- **Full-context plans (hard rule).** The `writing-plans` invocation is fed the full spec +
  notes + repo context (CLAUDE.md, relevant paths, patterns). Each generated task embeds its
  own background, exact files, and acceptance criteria — no cross-task assumptions — because
  each runs in a fresh container.
- One-shot `claude -p` (no chat). Output: `docs/superpowers/plans/<date>-<slug>.md` with a
  `- [ ]` task checklist, committed.

### Stage 3 — Execute plan

- Existing Docker-agent dispatch machinery. As tasks merge, the corresponding plan-file
  checkboxes flip to `- [x]` (feeding Unit B progress bars), alongside DB runtime state.

**Acceptance.** A user can create a spec end-to-end via chat; the spec is committed and
indexed; plan generation is human-triggered and produces self-contained tasks; checklist
state updates as tasks complete.

---

## Unit D — Context Sync

Closes the loop. After a plan finishes executing:

- **Trigger model A — auto-draft + human approve.** Praxis drafts proposed updates to the
  target repo's `CLAUDE.md` and `MEMORY.md` via `claude -p` (using
  `claude-md-management:revise-claude-md` / learnings), summarizing merged changes + plan.
- A new **Memory page** renders `CLAUDE.md` and `MEMORY.md`, shows last-synced time, presents
  the proposed-changes diff, and offers Approve & commit / Edit / Sync now. No silent commits.
- **`MEMORY.md` lives inside the target repo** (git-tracked, e.g. `docs/MEMORY.md`) so each
  isolated agent that clones the repo gets it as context.

**API.** `POST /api/projects/{id}/context-sync` (draft), `GET /api/projects/{id}/context`
(current files + last-synced), approve/commit endpoint.

**Acceptance.** On plan completion a draft is produced; the Memory page shows a reviewable
diff; approving commits to the repo; `MEMORY.md` is written in the target repo.

---

## Unit E — UX Clarity (woven)

Applied across A–D, not deferred: clear navigation between Dashboard / Specs / Plans / Memory;
progress visible at a glance; guided creation flows; consistent use of the existing
theme/master-detail vocabulary.

---

## Build Order & Plans

1. **Plan 1** — Unit A (dashboard layout fix) — quick win, unblocks rendering.
2. **Plan 2** — Unit B (docs source of truth + classification + views).
3. **Plan 3** — Unit C (Superpowers lifecycle: spec chat, plan gen, execution wiring).
4. **Plan 4** — Unit D (Context Sync + Memory page).

UX (E) folds into each. Each plan is generated via `superpowers:writing-plans` when its unit
is ready to build.

## Out of Scope

- Embedding brainstorming's visual-companion browser inside the in-product chat.
- Configurable planner/reviewer model (separate spec — `2026-06-13-configurable-agent-model`).
- Anthropic API-key based access (explicitly forbidden).
