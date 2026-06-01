# Praxis Dashboard UI/UX Redesign

**Date**: 2026-06-01
**Status**: Approved
**Scope**: `web/index.html` (single-file dashboard) + backend additions to `/api/status`

## Overview

Redesign the Praxis dashboard from a tab-based dark-only layout to a modern sidebar + master-detail layout with light/dark theme toggle. Design language is Minimal Engineering with Medium-inspired cleanliness — generous whitespace, subtle borders, typography-driven hierarchy.

## Design Language

- **Font**: Inter (sans-serif) for UI, monospace (`SF Mono`, `Cascadia Mono`, `Consolas`) for model names, branch names, and log output
- **Aesthetic**: Clean, data-dense, Linear/Vercel structure with Medium's whitespace and restraint
- **Transitions**: 0.15s ease on hover states, theme toggle, panel transitions
- **Border radius**: 6px buttons, 8px cards, 6px inputs

## Color System

All colors are CSS custom properties on `:root` (light) and `[data-theme="dark"]` (dark).

### Light Theme

| Token | Value | Usage |
|---|---|---|
| `--bg` | `#f8f8f8` | Page background |
| `--panel` | `#ffffff` | Sidebar, topbar, master panel, cards |
| `--border` | `#ebebeb` | Primary borders |
| `--border-subtle` | `#f0f0f0` | Row dividers, inner card borders |
| `--text` | `#1a1a1a` | Primary text |
| `--text-muted` | `#888888` | Secondary labels |
| `--text-faint` | `#bbbbbb` | Tertiary, section headers |
| `--hover-bg` | `#f5f5f5` | Row/nav hover |
| `--active-bg` | `#f0f0f0` | Active nav item |
| `--selected-bg` | `#f5f7ff` | Selected master row |
| `--selected-border` | `#1a1a1a` | Left border on selected row |

### Dark Theme (Matte Black)

| Token | Value | Usage |
|---|---|---|
| `--bg` | `#1c1c1e` | Page background |
| `--panel` | `#222224` | Sidebar, topbar, master panel, cards |
| `--border` | `#2e2e30` | Primary borders |
| `--border-subtle` | `#2a2a2c` | Row dividers, inner card borders |
| `--text` | `#d4d4d4` | Primary text |
| `--text-muted` | `#666666` | Secondary labels |
| `--text-faint` | `#555555` | Tertiary, section headers |
| `--hover-bg` | `#2a2a2c` | Row/nav hover |
| `--active-bg` | `#2e2e30` | Active nav item |
| `--selected-bg` | `#28282e` | Selected master row |
| `--selected-border` | `#d4d4d4` | Left border on selected row |

### Status Badges (both themes)

| Status | Light (bg / text) | Dark (bg / text) |
|---|---|---|
| `active`, `in_progress` | `#dbeafe` / `#1d4ed8` | `#1e3a5f` / `#60a5fa` |
| `passed`, `merged`, `completed` | `#dcfce7` / `#15803d` | `#14532d` / `#86efac` |
| `reviewing` | `#fef3c7` / `#b45309` | `#451a03` / `#fbbf24` |
| `failed`, `rejected` | `#fee2e2` / `#dc2626` | `#450a0a` / `#fca5a5` |
| `pending` | `#f3f4f6` / `#6b7280` | `#2e2e30` / `#888888` |

### Connection Dots

| State | Color |
|---|---|
| Connected | `#22c55e` |
| Disconnected | `#ef4444` |
| Rate limited | `#eab308` |

## Buttons

| Variant | Light | Dark |
|---|---|---|
| **Primary** | Solid: `#1a1a1a` bg, `#fff` text | Ghost: transparent bg, `#d4d4d4` text, `#555` border |
| **Secondary** | Ghost: transparent bg, `#555` text, `#e0e0e0` border | Ghost: transparent bg, `#999` text, `#3a3a3c` border |
| **Danger** | Ghost: transparent bg, `#dc2626` text | Ghost: transparent bg, `#fca5a5` text |

Hover states: border brightens, slight bg fill for ghost buttons.

## Layout Structure

```
+--[ Sidebar 240px ]--+--[ Main ]------------------------------------+
|  Logo: Praxis v1.0  |  Topbar (52px): page title | theme | token | +New |
|                      +------------------+--------------------------+
|  NAV (Workspace)     | Master Panel     | Detail Panel             |
|  - Projects          | (50% width)      | (50% width)              |
|  - Plans             |                  |                          |
|  - Tasks             | List of items    | Selected item details    |
|                      | for current view | OR inline form for       |
|  NAV (Monitor)       |                  | creating new items       |
|  - Live Logs         |                  |                          |
|                      |                  |                          |
|  STATUS              |                  |                          |
|  Active agents: N    |                  |                          |
|  Queued: N           |                  |                          |
|  Tasks running: N    |                  |                          |
|                      |                  |                          |
|  CONNECTIONS         |                  |                          |
|  * Agent (model)     |                  |                          |
|  * Subagent (model)  |                  |                          |
+----------------------+------------------+--------------------------+
```

### Sidebar (240px, fixed)

**Header**: Logo "Praxis" + version badge.

**Navigation**: Two sections with uppercase labels:
- **Workspace**: Projects, Plans, Tasks
- **Monitor**: Live Logs

Active item has `--active-bg` background and bold text. Hover has `--hover-bg`.

**Status section** (above Connections, separated by border):
- Active agents count
- Queued count
- Tasks running count
- Data from `GET /api/status`

**Connections section** (bottom of sidebar, separated by border):
- `Agent (claude-opus-4-6)` with green/red/yellow dot
- `Subagent (qwen3-32b)` with green/red dot
- Model names are dynamic (see Backend Additions)

### Topbar (52px)

- Left: current page title (e.g., "Projects")
- Right: theme toggle button (moon/sun icon), Token button, primary action button (e.g., "+ New Project")

### Master Panel (left 50%)

- Header: item count + refresh button
- Scrollable list of rows
- Each row: name, metadata (monospace), status badge
- Selected row: `--selected-bg` + left border accent
- Click row to populate detail panel

### Detail Panel (right 50%)

- Header: selected item title + subtitle
- Scrollable content with sections (cards)
- Footer: action buttons (primary, edit, delete)
- When "+ New" is clicked: detail panel becomes the creation form
- Empty state: centered "Select an item" message

## View Specifications

### Projects View

**Master**: Project list
- Row: project name (bold), model name (monospace, muted), plan status badge
- Click to select

**Detail (viewing)**: Selected project
- Configuration card: model, default branch, approval gate toggle
- Recent Tasks card: last 3-5 tasks with branch name and status
- Actions: View Plans, Edit, Delete

**Detail (creating)**: "+ New Project" form
- Fields: Name (text), Repository URL (text), Model (text, default: `deepseek-coder-v2`)
- Actions: Create, Cancel

### Plans View

**Master**: All plans across projects
- Row: spec preview (truncated), source badge (user/autonomous), status badge, confidence %
- Click to select

**Detail (viewing)**: Selected plan
- Full spec text
- Task breakdown list (if tasks exist)
- Approve / Reject buttons for autonomous plans with `pending` status
- Project name + link

**Detail (creating)**: "Submit Spec" form
- Fields: Project (select dropdown), Specification (textarea)
- Actions: Submit, Cancel

### Tasks View

**Master**: Tasks for selected plan
- Plan selector dropdown at top of master panel
- Row: task title, branch name (monospace), status badge, attempt count, PR link
- Click to select

**Detail (viewing)**: Selected task
- Configuration card: branch, status, attempt count, PR URL
- Agent run history (if available)
- Inline log viewer (monospace, auto-scroll, terminal-style background)
- Actions: Stop (if `in_progress`), View Logs (switches to full Live Logs view)

### Live Logs View

**Master**: Log sources
- System events (SSE `/api/events`)
- Active task entries (per-task log streams)
- Click to connect/switch stream

**Detail**: Live log stream
- Terminal-style viewer: monospace font, `12px`, auto-scroll
- Background: slightly darker than `--bg` in both themes
- Event type labels in brackets: `[plan_activated]`, `[agent_dispatched]`, etc.
- Reconnect button in detail header

## Theme Toggle

### Implementation
- CSS custom properties on `:root` define light theme (default)
- `[data-theme="dark"]` selector overrides all tokens for dark theme
- Toggle button in topbar switches `data-theme` attribute on `<html>`
- Preference persisted to `localStorage` key `praxis_theme`
- On first visit: respect `prefers-color-scheme` media query
- Icon: moon (`&#9790;`) in light mode, sun (`&#9788;`) in dark mode

### Behavior
```javascript
// Pseudocode
function initTheme() {
  const saved = localStorage.getItem('praxis_theme');
  if (saved) return applyTheme(saved);
  if (matchMedia('(prefers-color-scheme: dark)').matches) return applyTheme('dark');
  applyTheme('light');
}

function toggleTheme() {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  applyTheme(next);
  localStorage.setItem('praxis_theme', next);
}
```

## Backend Additions

### `/api/status` Response Changes

Add to existing response:

```json
{
  "opus_state": { ... },
  "active_agents": 3,
  "total_agents": 5,
  "agent_model": {
    "name": "claude-opus-4-6",
    "connected": true
  },
  "subagent_model": {
    "name": "qwen3-32b",
    "connected": true
  }
}
```

### Agent Model Resolution

The planning/review agent model name is resolved from:
1. Config field `agent_model_name` in `Settings` (pydantic-settings) — if explicitly set
2. Fallback: detect from `claude -p` CLI output or default to `"claude"` with `opus_state.status` for connection

### Subagent Model Resolution

The implementation LLM model name is resolved by querying:
```
GET {lm_studio_url}/v1/models
```
- If endpoint responds: extract first model name from response, mark `connected: true`
- If endpoint fails: `name: "unknown"`, `connected: false`

### Config Addition

Add to `Settings` in `config.py`:
```python
agent_model_name: str = "claude-opus-4-6"
```

## Responsive Behavior

### Tablet (< 1024px)
- Sidebar collapses to icon-only mode (56px wide)
- Tooltip on hover for nav items
- Master-detail proportions adjust (40%/60%)

### Mobile (< 768px)
- Sidebar hidden, hamburger menu in topbar
- Master-detail stacks vertically: master panel full-width
- Click row pushes detail panel full-width with back button
- Topbar simplified: logo + hamburger + theme toggle

## Constraints

- **Single-file**: all HTML, CSS, JS remain in `web/index.html` — no build step, no external dependencies (except Google Fonts CDN for Inter)
- **No framework**: vanilla JS, no React/Vue/Svelte
- **SSE**: existing EventSource pattern preserved for live logs
- **Auth**: existing token-based auth via `localStorage` preserved
- **Polling**: existing 5-second status poll interval preserved

## Migration from Current Dashboard

The redesign is a full rewrite of `web/index.html`. No incremental migration — the file is replaced entirely. All existing API calls and SSE connections are preserved; only the UI layer changes.

Backend changes (model name resolution in `/api/status`) are additive — no breaking changes to existing API contracts.
