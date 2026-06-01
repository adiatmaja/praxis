# Dashboard Flow View — Design Spec

**Date:** 2026-06-01
**Status:** Approved
**Author:** Johannes (brainstormed with Claude Opus)

---

## Overview

Add a new "Dashboard" view to the Praxis web UI that visualizes the full orchestration
pipeline as plan-centric horizontal swim lanes. The dashboard shows real-time agent
activity, pipeline status, and actionable items (approvals, retries) in a single unified
view. It becomes the landing page, while all existing views (Projects, Plans, Tasks,
Live Logs) remain unchanged.

## Goals

- Show "what are agents doing," "where is this plan," and "what needs attention" simultaneously
- Live updates via SSE — no manual refresh needed
- Support 1-2 active plans with rich detail per plan
- Keep all critical actions accessible without navigating away

## Non-Goals

- Replacing existing CRUD views (Projects, Plans, Tasks, Logs)
- Mobile-first design (responsive is enough; primary use is desktop)

---

## Layout Structure

```
┌─────────┬───────────────────────────────────────────────────────────┐
│ Sidebar │ Topbar: "Dashboard"                       [+ Submit Spec] │
│         ├───────────────────────────────────────────────────────────┤
│ >Dash   │ ┌─ System Health Bar ──────────────────────────────────┐  │
│  Projects│ │ ● Opus: Available   Agents: 2   ⚠ 1 attention   Q:0 │  │
│  Plans   │ └─────────────────────────────────────────────────────┘  │
│  Tasks   │                                                          │
│  Logs    │ ┌─ Plan Swim Lane ─────────────────────┬─ Detail ─────┐ │
│          │ │ ACTIVE: Add input validation          │ Task name    │ │
│          │ │ ┌────────┐┌────────┐┌────────┐┌─────┐│ Status       │ │
│          │ │ │MERGED  ││REVIEW ▪││IN_PROG ◉││PEND ││ Attempt      │ │
│          │ │ │email   ││password││username ││rate ││ PR link      │ │
│          │ │ └────────┘└────────┘└────────┘└─────┘│ Feedback     │ │
│          │ ├──────────────────────────────────────│ Live log     │ │
│          │ │ ▸ COMPLETED: Fix CSS dark mode (3/3) │ [Stop]       │ │
│          │ └──────────────────────────────────────┴──────────────┘ │
└─────────┴───────────────────────────────────────────────────────────┘
```

---

## Components

### 1. System Health Bar

A horizontal strip at the top of the dashboard content area. Always visible.

| Element | Source | Display |
|---------|--------|---------|
| Opus status | `GET /api/status` → `opus_state.status` | Colored dot: green=available, yellow=rate_limited, red=resuming. Label text. |
| Rate limit countdown | `opus_state.resume_at` | "Resumes in 4h 32m" when rate_limited. Hidden otherwise. |
| Active agents | `GET /api/status` → `active_agents` | "Agents: N" |
| Attention count | Computed client-side | Count of: failed tasks + plans pending approval. Orange badge: "N attention". Hidden when 0. |
| Queue count | `opus_state.queued_count` | "Queue: N" |

Updated live via polling (`GET /api/status` every 5s, same as existing).

### 2. Plan Swim Lanes

One horizontal lane per active plan (`status = "active"`). Vertically stacked, scrollable.

**Lane header:**
- Plan status badge (ACTIVE)
- Spec text truncated to ~100 chars
- Project name + plan branch name (right-aligned, muted)
- "View Spec" toggle button — expands full spec text below the header
- "Approve" / "Reject" buttons — visible only for autonomous plans with `status = "pending"`

**Task cards** within the lane, arranged left-to-right ordered by status progression:

| Status | Card Style | Extra Elements |
|--------|-----------|----------------|
| MERGED | Green left border, muted opacity | PR link |
| PASSED | Green left border | PR link |
| REVIEWING | Amber left border, pulsing amber dot | "Opus reviewing..." text |
| IN_PROGRESS | Blue left border, pulsing green dot | Last log line from SSE, progress bar (if available) |
| FAILED | Red left border, red background tint | "Retry" button |
| PENDING | Gray left border, subtle styling | "depends on: slug" if has dependencies |

**Every task card shows:**
- Status badge (small, uppercase)
- Task title
- Attempt count
- PR link (if exists, clickable → GitHub in new tab)

**Clicking a task card** opens the side panel for that task. The clicked card gets a
highlighted border to indicate selection.

### 3. Task Detail Side Panel

A panel that slides in from the right when a task card is clicked. Takes ~35% width.
Closes via a close button or clicking outside.

**Contents:**
- **Header:** Task title, branch name
- **Metadata card:** Status, attempt count, PR link, created_at, updated_at
- **Description:** Full task description (pre-wrapped)
- **Review feedback:** Shown when task has `review_feedback` (reviewing/passed/failed states). Pre-wrapped monospace.
- **Live log tail:** Last ~20 lines, auto-scrolling. Connected to SSE `agent_log` events for this task. Dark background, monospace font (same style as existing log view).
- **Actions:**
  - "Stop" button — visible when status = in_progress. Calls `POST /api/tasks/{id}/stop`.
  - "Retry" button — visible when status = failed. Calls retry endpoint.
  - "View Full Logs" link — navigates to Logs view for this task.

### 4. Completed Plans Section

Below active swim lanes. Shows the last 5 completed plans.

**Collapsed state (default):** Single row per plan.
- Disclosure triangle (▸)
- COMPLETED badge
- Spec text truncated
- Task summary: "N/N merged"
- Relative timestamp: "2h ago"
- Reduced opacity (0.6) to visually de-emphasize

**Expanded state (click to toggle):** Disclosure triangle rotates (▾). Below the
header row, task cards appear in a horizontal row (same card style as active lanes,
but all read-only — no action buttons). Click again to collapse.

### 5. Idle State

When no plans have `status = "active"`:

- System health bar still renders (Opus/agent status visible)
- Main area shows a centered message: "No active plans" in muted text
- Below: completed plans section with recent history
- Topbar primary action "+ Submit Spec" remains prominent

No auto-redirect. The dashboard is always the home page.

---

## Live Updates (SSE)

The dashboard connects to `GET /api/events?token=...` on mount (same endpoint as
existing Logs view). The connection stays open while the dashboard is the active view.
Disconnects when navigating to other views (same pattern as existing Logs view).

| SSE Event | Dashboard Action |
|-----------|-----------------|
| `plan_activated` | Fetch plans, add new swim lane |
| `agent_dispatched` | Update task card to IN_PROGRESS, start pulsing dot |
| `agent_log` | Update last log line on matching task card. If side panel is open for that task, append to log tail. |
| `review_completed` | Update task card to PASSED or FAILED. Show review feedback in side panel if open. |
| `task_completed` | Update task card to PASSED/MERGED |
| `task_failed` | Red styling + show Retry button on card |
| `task_retry` | Reset card to IN_PROGRESS, increment attempt |
| `improvement_proposed` | Increment attention count, add pending approval lane |
| `opus_queued` | Update queue count in health bar |

**Fallback:** If SSE disconnects (network error), show a subtle "reconnecting..." indicator
in the health bar and auto-reconnect with exponential backoff (same as existing behavior).

---

## Navigation Integration

- New "Dashboard" nav item in the sidebar, **first item** under the "Workspace" section
  (above Projects)
- Dashboard is the **default view** on page load (replaces Projects as the initial view)
- Sidebar nav icon: "D" (matching existing P, S, T, L pattern)
- Primary action button in topbar shows "+ Submit Spec" when on Dashboard view
- Clicking "+ Submit Spec" navigates to Plans view with the form pre-opened
  (`switchView('plans'); showingForm = true;`)

## User Actions on Dashboard

| Action | Trigger | API Call |
|--------|---------|----------|
| Submit spec | Topbar button → navigates to Plans view | (handled by existing Plans form) |
| Approve plan | "Approve" button in lane header | `POST /api/plans/{id}/approve` |
| Reject plan | "Reject" button in lane header | `POST /api/plans/{id}/reject` |
| Stop agent | "Stop" button in side panel | `POST /api/tasks/{id}/stop` |
| Retry failed task | "Retry" button on card or side panel | `POST /api/tasks/{id}/retry` |
| Expand completed plan | Click collapsed plan row | Client-side toggle (no API call) |
| Open task detail | Click task card | Client-side panel open (no API call, data already loaded) |
| View full logs | "View Full Logs" in side panel | `switchView('logs')` + connect to task log stream |
| View spec | "View Spec" toggle in lane header | Client-side toggle (spec already in plan data) |

---

## Data Loading

On dashboard mount:
1. `GET /api/projects` — load all projects
2. For each project: `GET /api/projects/{id}/plans` — load all plans
3. For each active plan: `GET /api/plans/{id}/tasks` — load tasks
4. `GET /api/status` — system health
5. Connect SSE to `/api/events`

This matches the existing data loading pattern (Plans view already loads all projects
then all plans). Task loading is scoped to active plans only to minimize requests.

Polling: `GET /api/status` every 5 seconds (same as existing).

---

## Styling

- Uses existing CSS variables (dark/light theme support automatic)
- Card borders use existing badge color variables (`--badge-active-bg`, etc.)
- Health bar uses a distinct background color (subtle, not jarring)
- Pulsing dot: CSS `@keyframes` animation, green for in-progress, amber for reviewing
- Side panel: same `--panel` background as existing detail panels
- Log tail in side panel: same `--log-bg` / `--log-text` as existing log view
- Completed plans: `opacity: 0.6` when collapsed, full opacity when expanded
- Transition: side panel slides in with a quick CSS transition (~200ms)

## Responsive (Narrow Screens)

- Below 768px: swim lane task cards wrap vertically (stacked instead of horizontal)
- Side panel becomes a full-width overlay (slides up from bottom or fills the view)
- Health bar items wrap to 2 rows if needed
- Completed plans section remains unchanged (already compact)

---

## Backend Change: Task Retry Endpoint

The orchestrator has `task_queue.retry_task()` internally but no API endpoint exposes it.
The dashboard needs a new endpoint:

```
POST /api/tasks/{task_id}/retry
```

- Auth: Bearer token (same as all other endpoints)
- Validates task exists and has `status = "failed"`
- Calls `task_queue.retry_task(task_id)` — resets status to PENDING, increments attempt
- Returns updated task JSON
- Publishes `task_retry` event to EventBus

Add this to `src/orchestrator/api/tasks.py` alongside the existing `stop` endpoint.

---

## Implementation Notes

- **Single file** — all changes go in `web/index.html` (maintaining the single-file pattern)
- **One backend addition** — `POST /api/tasks/{id}/retry` (see above)
- **SSE reuse** — same `openSse()` / `EventSource` pattern already in the codebase
- **State variables** — add: `dashboardTasks` (map of planId → tasks[]),
  `selectedDashboardTaskId`, `expandedCompletedPlans` (set of planIds)
- **Rendering** — new `renderDashboard()` function called by `switchView('dashboard')`
- **Default view** — change initial `switchView('projects')` call to `switchView('dashboard')`
