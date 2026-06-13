# Dashboard Layout Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the dashboard from rendering the health bar as a side column, so the health bar is a full-width top bar and the swim lanes fill the content area with no empty gutter.

**Architecture:** The dashboard view injects its HTML into `#view-container`, which carries the `.master-detail` class (`display:flex`, default row direction). That makes the health bar and `.dashboard-body` side-by-side flex items. Fix: wrap the dashboard's own markup in a dedicated column flex shell (`.dashboard-shell`) so it stacks vertically regardless of the container's flex direction, and verify with a Playwright layout assertion.

**Tech Stack:** Single-file HTML/CSS/JS dashboard (`web/index.html`); Playwright (already installed) for the layout verification; FastAPI server for serving the page.

---

### Task 1: Add a Playwright layout check that fails on the current bug

**Files:**
- Create: `tests/visual/check_dashboard_layout.mjs`
- Create: `tests/visual/package.json`

**Depends on:** None

This is a runnable assertion, not a pytest test — the bug is a CSS/DOM layout issue. It drives a headless browser against a running server, sets the dev token, and asserts the health bar spans the full content width and sits above the lanes with no left gutter.

- [ ] **Step 1: Create the verification script**

```javascript
// tests/visual/check_dashboard_layout.mjs
// Usage: node check_dashboard_layout.mjs
// Requires the server running at BASE (default http://127.0.0.1:8080)
// and AUTH token in TOKEN (default local-dev-token-praxis).
import { chromium } from 'playwright';

const BASE = process.env.BASE || 'http://127.0.0.1:8080';
const TOKEN = process.env.TOKEN || 'local-dev-token-praxis';

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await ctx.newPage();
await page.goto(BASE, { waitUntil: 'domcontentloaded' });
await page.evaluate(t => localStorage.setItem('praxis_token', t), TOKEN);
await page.reload({ waitUntil: 'domcontentloaded' });
await page.waitForSelector('.health-bar', { timeout: 10000 });
await page.waitForTimeout(2000);

const m = await page.evaluate(() => {
  const hb = document.querySelector('.health-bar').getBoundingClientRect();
  const body = document.querySelector('.dashboard-body').getBoundingClientRect();
  const container = document.getElementById('view-container').getBoundingClientRect();
  const lanes = document.querySelector('.dashboard-lanes')?.getBoundingClientRect() || null;
  return { hb, body, container, lanes };
});

const errors = [];
// Health bar must span (almost) the full content width, not a ~430px column.
if (m.hb.width < m.container.width * 0.9) {
  errors.push(`health bar width ${Math.round(m.hb.width)} < 90% of container ${Math.round(m.container.width)} (rendered as a column)`);
}
// Health bar must sit ABOVE the body, not beside it.
if (m.hb.bottom > m.body.top + 2) {
  errors.push(`health bar bottom ${Math.round(m.hb.bottom)} overlaps body top ${Math.round(m.body.top)} (side-by-side, not stacked)`);
}
// Lanes must start near the left edge of the content area (no big gutter).
if (m.lanes && m.lanes.left - m.container.left > 24) {
  errors.push(`lanes left gutter ${Math.round(m.lanes.left - m.container.left)}px too large`);
}

await browser.close();
if (errors.length) {
  console.error('LAYOUT CHECK FAILED:\n- ' + errors.join('\n- '));
  process.exit(1);
}
console.log('LAYOUT CHECK PASSED');
```

- [ ] **Step 2: Create a minimal package.json so the script can resolve Playwright**

```json
{
  "name": "praxis-visual-checks",
  "private": true,
  "type": "module",
  "dependencies": { "playwright": "1.60.0" }
}
```

- [ ] **Step 3: Install Playwright for the check**

Run: `cd tests/visual && npm install && npx playwright install chromium`
Expected: installs without error; `node_modules/playwright` exists.

- [ ] **Step 4: Start the server in another terminal**

Run: `uv run uvicorn orchestrator.main:app --host 127.0.0.1 --port 8080`
Expected: `Application startup complete` and `{"status":"ok"}` from `curl http://127.0.0.1:8080/health`.

- [ ] **Step 5: Run the check and confirm it FAILS on the current layout**

Run: `cd tests/visual && node check_dashboard_layout.mjs`
Expected: FAIL — `LAYOUT CHECK FAILED` with "health bar width ... (rendered as a column)" (requires at least one active/pending plan in the DB so the dashboard renders lanes; if the DB is empty, seed one via the API first).

- [ ] **Step 6: Commit the failing check**

```bash
git add tests/visual/check_dashboard_layout.mjs tests/visual/package.json
git commit -m "test: add failing dashboard layout check"
```

---

### Task 2: Wrap the dashboard in a column shell so it stacks

**Files:**
- Modify: `web/index.html:374` (add `.dashboard-shell` CSS near `.dashboard-body`)
- Modify: `web/index.html:1106-1111` (`renderDashboard()` innerHTML assembly)

**Depends on:** Task 1

- [ ] **Step 1: Add the `.dashboard-shell` style**

In `web/index.html`, immediately before the existing `.dashboard-body` rule (currently at line 374), add:

```css
    .dashboard-shell { flex: 1; min-height: 0; display: flex; flex-direction: column; }
```

This gives the dashboard its own vertical stacking context, independent of `.master-detail`'s row direction.

- [ ] **Step 2: Wrap the rendered markup in the shell**

In `renderDashboard()` (currently lines 1106-1111), replace:

```javascript
      const container = document.getElementById("view-container");
      container.innerHTML =
        renderHealthBar(attentionCount) +
        '<div class="dashboard-body">' +
          '<div class="dashboard-lanes">' + bodyHtml + '</div>' +
          '<div class="side-panel" id="dashboard-side-panel"></div>' +
        '</div>';
```

with:

```javascript
      const container = document.getElementById("view-container");
      container.innerHTML =
        '<div class="dashboard-shell">' +
          renderHealthBar(attentionCount) +
          '<div class="dashboard-body">' +
            '<div class="dashboard-lanes">' + bodyHtml + '</div>' +
            '<div class="side-panel" id="dashboard-side-panel"></div>' +
          '</div>' +
        '</div>';
```

- [ ] **Step 3: Re-run the layout check and confirm it PASSES**

Run: `cd tests/visual && node check_dashboard_layout.mjs`
Expected: `LAYOUT CHECK PASSED`.

- [ ] **Step 4: Visually confirm dark theme and the 768px breakpoint**

Run the server, open `http://127.0.0.1:8080`, toggle the theme (the theme button), and resize the window below 768px. Expected: health bar is a full-width top bar in both themes; lanes start at the left edge; side panel still opens to the right (and overlays full-screen below 768px per the existing `@media` rule at `web/index.html:496`).

- [ ] **Step 5: Commit the fix**

```bash
git add web/index.html
git commit -m "fix(ui): stack dashboard health bar above lanes (kill empty gutter)"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1 (no dependencies)
- **Wave 2:** Task 2 (depends on Task 1)

All tasks are sequential — the check must exist and fail before the fix makes it pass.

## Notes

- The `.master-detail` class is shared by other views (Projects, Plans, Tasks); do **not**
  change its `display`/`flex-direction` — that would break those views. The fix is scoped to
  the dashboard via the new `.dashboard-shell` wrapper.
- If `data/orchestrator.db` is empty, the dashboard shows the idle state and the check's lane
  assertion is skipped (it guards on `.dashboard-lanes` existing). Seed at least one
  active/pending plan to exercise the full layout (see `CLAUDE.local.md` curl examples).
