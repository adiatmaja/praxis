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
if (m.hb.width < m.container.width * 0.9) {
  errors.push(`health bar width ${Math.round(m.hb.width)} < 90% of container ${Math.round(m.container.width)} (rendered as a column)`);
}
if (m.hb.bottom > m.body.top + 2) {
  errors.push(`health bar bottom ${Math.round(m.hb.bottom)} overlaps body top ${Math.round(m.body.top)} (side-by-side, not stacked)`);
}
if (m.lanes && m.lanes.left - m.container.left > 24) {
  errors.push(`lanes left gutter ${Math.round(m.lanes.left - m.container.left)}px too large`);
}

await browser.close();
if (errors.length) {
  console.error('LAYOUT CHECK FAILED:\n- ' + errors.join('\n- '));
  process.exit(1);
}
console.log('LAYOUT CHECK PASSED');
