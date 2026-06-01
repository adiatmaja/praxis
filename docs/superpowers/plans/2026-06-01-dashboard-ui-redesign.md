# Dashboard UI/UX Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redesign the Praxis dashboard from tab-based dark-only to sidebar + master-detail with light/dark toggle, plus backend additions for dynamic connection status.

**Architecture:** Two independent streams — backend (config + API changes for model/connection info) and frontend (full rewrite of `web/index.html`). Backend adds `agent_model_name` config field and subagent model probing via LM Studio `/v1/models`. Frontend is a complete replacement using CSS custom properties for theming, sidebar nav, and master-detail layout.

**Tech Stack:** Python 3.11, FastAPI, pydantic-settings, httpx (for LM Studio probe), vanilla HTML/CSS/JS, Inter font (Google Fonts CDN)

---

### Task 1: Add `agent_model_name` to Settings

**Files:**
- Modify: `src/orchestrator/config.py:8-18`
- Modify: `tests/test_config.py`
- Modify: `.env.example`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
@pytest.mark.unit
def test_settings_agent_model_name_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_TOKEN", "auth-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-secret")

    settings = Settings()

    assert settings.agent_model_name == "claude-opus-4-6"


@pytest.mark.unit
def test_settings_agent_model_name_custom(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_TOKEN", "auth-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-secret")
    monkeypatch.setenv("AGENT_MODEL_NAME", "gpt-5.5-medium")

    settings = Settings()

    assert settings.agent_model_name == "gpt-5.5-medium"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py::test_settings_agent_model_name_default tests/test_config.py::test_settings_agent_model_name_custom -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'agent_model_name'`

- [ ] **Step 3: Add `agent_model_name` to Settings**

In `src/orchestrator/config.py`, add the field to the `Settings` class after `lm_studio_url`:

```python
class Settings(BaseSettings):
    """Runtime settings sourced from environment variables."""

    auth_token: str
    github_token: str
    database_url: str = "sqlite+aiosqlite:///data/orchestrator.db"
    lm_studio_url: str = "http://host.docker.internal:1234"
    agent_model_name: str = "claude-opus-4-6"
    host: str = "0.0.0.0"  # noqa: S104
    port: int = 8080

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")
```

- [ ] **Step 4: Update `.env.example`**

Add after `LM_STUDIO_URL`:

```
AGENT_MODEL_NAME=claude-opus-4-6
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/config.py tests/test_config.py .env.example
git commit -m "feat: add agent_model_name to Settings config"
```

---

### Task 2: Add model connection info to `/api/status`

**Files:**
- Modify: `src/orchestrator/api/system.py:1-45`
- Modify: `tests/test_api_system.py`
- Modify: `tests/conftest.py:58-76` (client fixture — add settings to app.state)

**Depends on:** Task 1

- [ ] **Step 1: Write the failing test for agent_model in status**

Add to `tests/test_api_system.py`:

```python
@pytest.mark.integration
async def test_status_includes_agent_model(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    await seed_user(db)

    response = await client.get("/api/status", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert "agent_model" in data
    assert data["agent_model"]["name"] == "claude-opus-4-6"
    assert isinstance(data["agent_model"]["connected"], bool)


@pytest.mark.integration
async def test_status_includes_subagent_model(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    await seed_user(db)

    response = await client.get("/api/status", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert "subagent_model" in data
    assert "name" in data["subagent_model"]
    assert isinstance(data["subagent_model"]["connected"], bool)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_api_system.py::test_status_includes_agent_model tests/test_api_system.py::test_status_includes_subagent_model -v`
Expected: FAIL — `agent_model` key missing from response

- [ ] **Step 3: Implement model probing in system.py**

Replace the full contents of `src/orchestrator/api/system.py`:

```python
"""System status endpoints."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request

from orchestrator.api.auth import verify_token
from orchestrator.models.schemas import OpusStateResponse


logger = logging.getLogger(__name__)

router = APIRouter(tags=["system"], dependencies=[Depends(verify_token)])


def _opus_state_response(state: dict[str, Any]) -> dict[str, Any]:
    queued_actions = json.loads(state["queued_actions"])
    return {
        "status": state["status"],
        "rate_limited_at": state.get("rate_limited_at"),
        "resume_at": state.get("resume_at"),
        "queued_count": len(queued_actions),
    }


async def _probe_subagent(lm_studio_url: str) -> dict[str, Any]:
    """Probe LM Studio /v1/models to get loaded model name."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{lm_studio_url}/v1/models")
            response.raise_for_status()
            data = response.json()
            models = data.get("data", [])
            if models:
                return {"name": models[0]["id"], "connected": True}
            return {"name": "unknown", "connected": True}
    except Exception as exc:
        logger.debug("Subagent probe failed: %s", exc)
        return {"name": "unknown", "connected": False}


@router.get("/status")
async def system_status(request: Request) -> dict[str, Any]:
    """Return aggregate orchestrator status."""

    opus_state = await request.app.state.opus_bridge.get_opus_state()
    agent_manager = getattr(request.app.state, "agent_manager", None)
    settings = request.app.state.settings
    containers: list[dict[str, Any]] = []
    if agent_manager is not None:
        try:
            containers = agent_manager.list_agent_containers()
        except Exception:
            containers = []

    opus_status = opus_state["status"]
    agent_connected = opus_status in ("available", "rate_limited", "resuming")

    subagent_info = await _probe_subagent(settings.lm_studio_url)

    return {
        "opus_state": _opus_state_response(opus_state),
        "active_agents": len(
            [c for c in containers if c["status"] == "running"]
        ),
        "total_agents": len(containers),
        "agent_model": {
            "name": settings.agent_model_name,
            "connected": agent_connected,
        },
        "subagent_model": subagent_info,
    }


@router.get("/opus/state", response_model=OpusStateResponse)
async def opus_state(request: Request) -> dict[str, Any]:
    """Return Opus rate-limit state."""

    return _opus_state_response(await request.app.state.opus_bridge.get_opus_state())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_api_system.py -v`
Expected: All tests PASS (subagent_model will show `connected: false` in test since no LM Studio is running)

- [ ] **Step 5: Run full test suite to check for regressions**

Run: `uv run pytest --cov=orchestrator --cov-report=term-missing -v`
Expected: All existing tests PASS, coverage >= 80%

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/api/system.py tests/test_api_system.py
git commit -m "feat: add agent and subagent model info to /api/status"
```

---

### Task 3: Rewrite dashboard — CSS foundation and theme toggle

**Files:**
- Modify: `web/index.html` (full rewrite — this task creates the CSS and HTML shell)

**Depends on:** None

- [ ] **Step 1: Write the CSS custom properties and base layout HTML**

Replace the entire contents of `web/index.html` with the foundation. This includes:
- `:root` light theme variables
- `[data-theme="dark"]` matte black overrides
- All badge color variants for both themes
- Base layout: sidebar + main (topbar + master-detail)
- Button variants: primary (solid light, ghost dark), secondary, danger
- Responsive breakpoints (1024px tablet, 768px mobile)
- Theme init/toggle JS

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Praxis</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    /* ===== THEME: LIGHT (default) ===== */
    :root {
      --bg: #f8f8f8;
      --panel: #ffffff;
      --border: #ebebeb;
      --border-subtle: #f0f0f0;
      --text: #1a1a1a;
      --text-muted: #888888;
      --text-faint: #bbbbbb;
      --hover-bg: #f5f5f5;
      --active-bg: #f0f0f0;
      --selected-bg: #f5f7ff;
      --selected-border: #1a1a1a;
      --log-bg: #f0f0f0;
      /* Buttons (light) */
      --btn-primary-bg: #1a1a1a;
      --btn-primary-text: #ffffff;
      --btn-primary-border: #1a1a1a;
      --btn-secondary-bg: transparent;
      --btn-secondary-text: #555555;
      --btn-secondary-border: #e0e0e0;
      --btn-danger-text: #dc2626;
      /* Badges */
      --badge-active-bg: #dbeafe; --badge-active-text: #1d4ed8;
      --badge-pending-bg: #f3f4f6; --badge-pending-text: #6b7280;
      --badge-passed-bg: #dcfce7; --badge-passed-text: #15803d;
      --badge-merged-bg: #dcfce7; --badge-merged-text: #15803d;
      --badge-completed-bg: #dcfce7; --badge-completed-text: #15803d;
      --badge-reviewing-bg: #fef3c7; --badge-reviewing-text: #b45309;
      --badge-failed-bg: #fee2e2; --badge-failed-text: #dc2626;
      --badge-rejected-bg: #fee2e2; --badge-rejected-text: #dc2626;
    }

    /* ===== THEME: DARK (matte black) ===== */
    [data-theme="dark"] {
      --bg: #1c1c1e;
      --panel: #222224;
      --border: #2e2e30;
      --border-subtle: #2a2a2c;
      --text: #d4d4d4;
      --text-muted: #666666;
      --text-faint: #555555;
      --hover-bg: #2a2a2c;
      --active-bg: #2e2e30;
      --selected-bg: #28282e;
      --selected-border: #d4d4d4;
      --log-bg: #1a1a1c;
      /* Buttons (dark — ghost primary) */
      --btn-primary-bg: transparent;
      --btn-primary-text: #d4d4d4;
      --btn-primary-border: #555555;
      --btn-secondary-bg: transparent;
      --btn-secondary-text: #999999;
      --btn-secondary-border: #3a3a3c;
      --btn-danger-text: #fca5a5;
      /* Badges */
      --badge-active-bg: #1e3a5f; --badge-active-text: #60a5fa;
      --badge-pending-bg: #2e2e30; --badge-pending-text: #888888;
      --badge-passed-bg: #14532d; --badge-passed-text: #86efac;
      --badge-merged-bg: #14532d; --badge-merged-text: #86efac;
      --badge-completed-bg: #14532d; --badge-completed-text: #86efac;
      --badge-reviewing-bg: #451a03; --badge-reviewing-text: #fbbf24;
      --badge-failed-bg: #450a0a; --badge-failed-text: #fca5a5;
      --badge-rejected-bg: #450a0a; --badge-rejected-text: #fca5a5;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: 'Inter', ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: var(--bg);
      color: var(--text);
      font-size: 14px;
      line-height: 1.5;
      height: 100vh;
      display: flex;
      overflow: hidden;
    }

    /* ===== SIDEBAR ===== */
    .sidebar {
      width: 240px;
      background: var(--panel);
      border-right: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      height: 100vh;
      flex-shrink: 0;
    }
    .sidebar-header {
      padding: 20px 20px 16px;
      border-bottom: 1px solid var(--border);
    }
    .sidebar-logo {
      font-size: 18px; font-weight: 700; color: var(--text); letter-spacing: -0.3px;
    }
    .sidebar-logo span { color: var(--text-faint); font-weight: 400; font-size: 12px; margin-left: 8px; }
    .sidebar-nav { padding: 12px 10px; flex: 1; }
    .nav-section {
      font-size: 10px; font-weight: 600; color: var(--text-faint);
      text-transform: uppercase; letter-spacing: 0.6px; padding: 16px 10px 6px;
    }
    .nav-section:first-child { padding-top: 4px; }
    .nav-item {
      display: flex; align-items: center; gap: 10px;
      padding: 7px 10px; border-radius: 6px; color: var(--text-muted);
      cursor: pointer; font-size: 13px; font-weight: 500;
      transition: all 0.15s ease;
    }
    .nav-item:hover { background: var(--hover-bg); color: var(--text); }
    .nav-item.active { background: var(--active-bg); color: var(--text); font-weight: 600; }
    .nav-icon { font-size: 14px; opacity: 0.5; width: 18px; text-align: center; }
    .nav-item.active .nav-icon { opacity: 0.8; }

    /* Sidebar stats */
    .sidebar-stats { padding: 14px 20px; border-top: 1px solid var(--border); }
    .sidebar-connections { padding: 14px 20px; border-top: 1px solid var(--border); }
    .section-label {
      font-size: 10px; font-weight: 600; color: var(--text-faint);
      text-transform: uppercase; letter-spacing: 0.6px; margin-bottom: 8px;
    }
    .stat-row {
      display: flex; justify-content: space-between; padding: 4px 0;
      font-size: 12px; color: var(--text-muted);
    }
    .stat-row strong { color: var(--text); font-weight: 600; }
    .connection-row { display: flex; align-items: center; gap: 8px; margin-bottom: 7px; }
    .connection-row:last-child { margin-bottom: 0; }
    .conn-dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
    .conn-dot.connected { background: #22c55e; }
    .conn-dot.disconnected { background: #ef4444; }
    .conn-dot.rate_limited { background: #eab308; }
    .conn-info { font-size: 12px; color: var(--text-muted); }
    .conn-role { font-weight: 600; color: var(--text); }
    .conn-model { color: var(--text-faint); font-size: 11px; }

    /* ===== MAIN ===== */
    .main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
    .topbar {
      height: 52px; background: var(--panel); border-bottom: 1px solid var(--border);
      display: flex; align-items: center; justify-content: space-between;
      padding: 0 24px; flex-shrink: 0;
    }
    .topbar-title { font-size: 15px; font-weight: 600; }
    .topbar-actions { display: flex; align-items: center; gap: 10px; }

    /* Buttons */
    .btn {
      height: 32px; padding: 0 12px; border-radius: 6px;
      border: 1px solid var(--btn-secondary-border);
      background: var(--btn-secondary-bg); color: var(--btn-secondary-text);
      font-size: 12px; font-weight: 500; cursor: pointer;
      display: inline-flex; align-items: center; gap: 5px;
      font-family: inherit; transition: all 0.15s ease;
    }
    .btn:hover { border-color: var(--text-muted); color: var(--text); }
    .btn-primary {
      background: var(--btn-primary-bg); color: var(--btn-primary-text);
      border-color: var(--btn-primary-border); font-weight: 600;
    }
    .btn-primary:hover { opacity: 0.85; }
    .btn-danger { color: var(--btn-danger-text); }
    .btn-compact { height: 28px; font-size: 11px; padding: 0 9px; }
    .theme-btn {
      width: 32px; height: 32px; padding: 0;
      display: flex; align-items: center; justify-content: center;
      font-size: 16px; border-radius: 6px;
      border: 1px solid var(--btn-secondary-border);
      background: var(--btn-secondary-bg); cursor: pointer; color: var(--text-muted);
      font-family: inherit; transition: all 0.15s ease;
    }
    .theme-btn:hover { color: var(--text); border-color: var(--text-muted); }

    /* Inputs */
    input, select, textarea {
      width: 100%; border: 1px solid var(--border); border-radius: 6px;
      background: var(--bg); color: var(--text); padding: 8px 10px;
      font-family: inherit; font-size: 13px;
    }
    textarea { min-height: 120px; resize: vertical; line-height: 1.45; }
    label { display: block; color: var(--text-muted); font-size: 12px; margin-bottom: 5px; }
    .formrow { margin-bottom: 12px; }
    a { color: var(--text-muted); text-decoration: none; }
    a:hover { color: var(--text); text-decoration: underline; }

    /* ===== MASTER-DETAIL ===== */
    .master-detail { flex: 1; display: flex; overflow: hidden; }
    .master-panel {
      width: 50%; border-right: 1px solid var(--border);
      background: var(--panel); display: flex; flex-direction: column; overflow: hidden;
    }
    .master-header {
      padding: 14px 20px; border-bottom: 1px solid var(--border-subtle);
      display: flex; align-items: center; justify-content: space-between; flex-shrink: 0;
    }
    .master-title { font-size: 13px; font-weight: 600; color: var(--text-muted); }
    .master-list { flex: 1; overflow-y: auto; }
    .master-row {
      display: flex; align-items: center; padding: 12px 20px;
      border-bottom: 1px solid var(--border-subtle); cursor: pointer;
      transition: background 0.1s ease; gap: 12px;
    }
    .master-row:hover { background: var(--hover-bg); }
    .master-row.selected { background: var(--selected-bg); border-left: 2px solid var(--selected-border); }
    .row-name { font-weight: 600; font-size: 13px; flex: 1; }
    .row-meta {
      font-size: 11px; color: var(--text-faint);
      font-family: 'SF Mono', 'Cascadia Mono', Consolas, monospace;
    }

    /* Badges */
    .badge {
      display: inline-flex; align-items: center; height: 20px;
      padding: 0 7px; border-radius: 4px; font-size: 10px;
      font-weight: 600; text-transform: uppercase; letter-spacing: 0.3px;
      white-space: nowrap;
    }
    .badge.active, .badge.in_progress { background: var(--badge-active-bg); color: var(--badge-active-text); }
    .badge.pending { background: var(--badge-pending-bg); color: var(--badge-pending-text); }
    .badge.passed { background: var(--badge-passed-bg); color: var(--badge-passed-text); }
    .badge.merged { background: var(--badge-merged-bg); color: var(--badge-merged-text); }
    .badge.completed { background: var(--badge-completed-bg); color: var(--badge-completed-text); }
    .badge.reviewing { background: var(--badge-reviewing-bg); color: var(--badge-reviewing-text); }
    .badge.failed, .badge.rejected { background: var(--badge-failed-bg); color: var(--badge-failed-text); }

    /* Detail panel */
    .detail-panel {
      flex: 1; background: var(--bg); display: flex;
      flex-direction: column; overflow: hidden;
    }
    .detail-header {
      padding: 16px 24px; background: var(--panel);
      border-bottom: 1px solid var(--border-subtle); flex-shrink: 0;
    }
    .detail-title { font-size: 16px; font-weight: 700; margin-bottom: 4px; }
    .detail-subtitle { font-size: 12px; color: var(--text-faint); }
    .detail-content { flex: 1; overflow-y: auto; padding: 20px 24px; }
    .detail-section { margin-bottom: 20px; }
    .detail-section-title {
      font-size: 11px; font-weight: 600; color: var(--text-faint);
      text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 10px;
    }
    .detail-card {
      background: var(--panel); border: 1px solid var(--border);
      border-radius: 8px; padding: 14px 16px;
    }
    .detail-field {
      display: flex; justify-content: space-between; padding: 5px 0;
      font-size: 13px; border-bottom: 1px solid var(--border-subtle);
    }
    .detail-field:last-child { border-bottom: none; }
    .field-label { color: var(--text-muted); }
    .field-value { color: var(--text); font-weight: 500; }
    .detail-actions {
      display: flex; gap: 8px; padding: 16px 24px;
      background: var(--panel); border-top: 1px solid var(--border-subtle); flex-shrink: 0;
    }
    .detail-empty {
      flex: 1; display: flex; align-items: center; justify-content: center;
      color: var(--text-faint); font-size: 13px;
    }

    /* Log viewer */
    .log {
      min-height: 200px; max-height: 100%; flex: 1; overflow: auto;
      white-space: pre-wrap; word-break: break-word; border-radius: 6px;
      border: 1px solid var(--border); background: var(--log-bg);
      color: var(--text); padding: 12px; line-height: 1.45;
      font-family: 'SF Mono', 'Cascadia Mono', Consolas, monospace; font-size: 12px;
    }

    .hidden { display: none; }

    /* ===== RESPONSIVE ===== */
    @media (max-width: 1024px) {
      .sidebar { width: 56px; }
      .sidebar-header { padding: 16px 10px; }
      .sidebar-logo span, .nav-section, .section-label,
      .stat-row span, .conn-info, .stat-row strong { display: none; }
      .sidebar-logo { font-size: 14px; text-align: center; }
      .nav-item { justify-content: center; padding: 10px; }
      .nav-item gap { gap: 0; }
      .sidebar-stats, .sidebar-connections { padding: 10px; }
      .stat-row { justify-content: center; }
      .connection-row { justify-content: center; }
    }

    @media (max-width: 768px) {
      .sidebar { display: none; }
      .master-detail { flex-direction: column; }
      .master-panel { width: 100%; border-right: none; border-bottom: 1px solid var(--border); max-height: 40vh; }
      .detail-panel { min-height: 0; }
    }

    /* Scrollbar */
    ::-webkit-scrollbar { width: 5px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
  </style>
</head>
<body>

  <!-- SIDEBAR -->
  <nav class="sidebar">
    <div class="sidebar-header">
      <div class="sidebar-logo">Praxis <span>v1.0</span></div>
    </div>
    <div class="sidebar-nav">
      <div class="nav-section">Workspace</div>
      <div class="nav-item active" data-view="projects" onclick="switchView('projects')">
        <div class="nav-icon">&#9723;</div> Projects
      </div>
      <div class="nav-item" data-view="plans" onclick="switchView('plans')">
        <div class="nav-icon">&#9634;</div> Plans
      </div>
      <div class="nav-item" data-view="tasks" onclick="switchView('tasks')">
        <div class="nav-icon">&#9654;</div> Tasks
      </div>
      <div class="nav-section">Monitor</div>
      <div class="nav-item" data-view="logs" onclick="switchView('logs')">
        <div class="nav-icon">&#9776;</div> Live Logs
      </div>
    </div>
    <div class="sidebar-stats">
      <div class="section-label">Status</div>
      <div class="stat-row"><span>Active agents</span> <strong id="stat-agents">0</strong></div>
      <div class="stat-row"><span>Queued</span> <strong id="stat-queued">0</strong></div>
      <div class="stat-row"><span>Tasks running</span> <strong id="stat-tasks">0</strong></div>
    </div>
    <div class="sidebar-connections">
      <div class="section-label">Connections</div>
      <div class="connection-row">
        <div id="agent-dot" class="conn-dot disconnected"></div>
        <div class="conn-info">
          <span class="conn-role">Agent</span>
          <span id="agent-model" class="conn-model">(unknown)</span>
        </div>
      </div>
      <div class="connection-row">
        <div id="subagent-dot" class="conn-dot disconnected"></div>
        <div class="conn-info">
          <span class="conn-role">Subagent</span>
          <span id="subagent-model" class="conn-model">(unknown)</span>
        </div>
      </div>
    </div>
  </nav>

  <!-- MAIN -->
  <div class="main">
    <div class="topbar">
      <div id="topbar-title" class="topbar-title">Projects</div>
      <div class="topbar-actions">
        <button class="theme-btn" onclick="toggleTheme()" id="theme-btn" title="Toggle theme">&#9790;</button>
        <button class="btn" onclick="resetToken()">Token</button>
        <button class="btn btn-primary" id="topbar-action" onclick="topbarAction()">+ New Project</button>
      </div>
    </div>

    <!-- Views rendered dynamically by JS into #view-container -->
    <div id="view-container" class="master-detail"></div>
  </div>

  <script>
    /* ===== THEME ===== */
    function initTheme() {
      const saved = localStorage.getItem('praxis_theme');
      if (saved) return applyTheme(saved);
      if (matchMedia('(prefers-color-scheme: dark)').matches) return applyTheme('dark');
      applyTheme('light');
    }
    function applyTheme(theme) {
      document.documentElement.dataset.theme = theme;
      document.getElementById('theme-btn').innerHTML = theme === 'dark' ? '&#9788;' : '&#9790;';
    }
    function toggleTheme() {
      const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
      applyTheme(next);
      localStorage.setItem('praxis_theme', next);
    }

    /* ===== AUTH ===== */
    const API = window.location.origin;
    let token = localStorage.getItem('praxis_token') || '';

    function ensureToken() {
      if (!token) resetToken();
      return token;
    }
    function resetToken() {
      token = window.prompt('API token', token) || '';
      localStorage.setItem('praxis_token', token);
      pollStatus();
    }
    function headers() {
      return { 'Authorization': 'Bearer ' + ensureToken(), 'Content-Type': 'application/json' };
    }
    async function api(method, path, body) {
      const opts = { method, headers: headers() };
      if (body !== undefined) opts.body = JSON.stringify(body);
      const res = await fetch(API + path, opts);
      if (!res.ok) throw new Error(await res.text());
      return res.json();
    }

    /* ===== ESCAPING ===== */
    function esc(v) {
      return String(v ?? '').replace(/[&<>"']/g, c =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }
    function badge(status) {
      return '<span class="badge ' + esc(status) + '">' + esc(status) + '</span>';
    }

    /* ===== STATE ===== */
    let currentView = 'projects';
    let projects = [];
    let plans = [];
    let selectedProjectId = null;
    let selectedPlanId = null;
    let selectedTaskId = null;
    let showingForm = false;
    let eventSource = null;

    /* ===== NAV ===== */
    const viewTitles = { projects: 'Projects', plans: 'Plans', tasks: 'Tasks', logs: 'Live Logs' };
    const actionLabels = { projects: '+ New Project', plans: '+ Submit Spec', tasks: '', logs: '' };

    function switchView(view) {
      currentView = view;
      showingForm = false;
      document.querySelectorAll('.nav-item').forEach(el => el.classList.toggle('active', el.dataset.view === view));
      document.getElementById('topbar-title').textContent = viewTitles[view];
      const actionBtn = document.getElementById('topbar-action');
      actionBtn.textContent = actionLabels[view];
      actionBtn.classList.toggle('hidden', !actionLabels[view]);
      renderView();
    }

    function topbarAction() {
      showingForm = true;
      renderView();
    }

    /* ===== RENDER DISPATCH ===== */
    function renderView() {
      if (currentView === 'projects') renderProjects();
      else if (currentView === 'plans') renderPlans();
      else if (currentView === 'tasks') renderTasks();
      else if (currentView === 'logs') renderLogs();
    }

    /* Placeholder renderers — implemented in Task 4 */
    function renderProjects() {
      document.getElementById('view-container').innerHTML =
        '<div class="detail-empty">Projects view — loading...</div>';
      loadProjects();
    }
    function renderPlans() {
      document.getElementById('view-container').innerHTML =
        '<div class="detail-empty">Plans view — loading...</div>';
      loadPlans();
    }
    function renderTasks() {
      document.getElementById('view-container').innerHTML =
        '<div class="detail-empty">Tasks view — loading...</div>';
    }
    function renderLogs() {
      document.getElementById('view-container').innerHTML =
        '<div class="detail-empty">Logs view — loading...</div>';
    }

    /* ===== DATA LOADING (stubs — filled in Task 4) ===== */
    async function loadProjects() { projects = await api('GET', '/api/projects'); renderProjectsView(); }
    async function loadPlans() {
      if (!projects.length) await loadProjects();
      plans = [];
      for (const p of projects) {
        const pp = await api('GET', '/api/projects/' + p.id + '/plans');
        plans.push(...pp.map(pl => ({ ...pl, projectName: p.name })));
      }
      renderPlansView();
    }
    function renderProjectsView() { /* Task 4 */ }
    function renderPlansView() { /* Task 4 */ }

    /* ===== STATUS POLLING ===== */
    async function pollStatus() {
      if (!token) return;
      try {
        const s = await api('GET', '/api/status');
        const opus = s.opus_state.status;
        document.getElementById('stat-agents').textContent = s.active_agents;
        document.getElementById('stat-queued').textContent = s.opus_state.queued_count;
        document.getElementById('stat-tasks').textContent = s.total_agents;

        /* Agent connection */
        const agentDot = document.getElementById('agent-dot');
        const am = s.agent_model || {};
        agentDot.className = 'conn-dot ' + (am.connected ? (opus === 'rate_limited' ? 'rate_limited' : 'connected') : 'disconnected');
        document.getElementById('agent-model').textContent = '(' + (am.name || 'unknown') + ')';

        /* Subagent connection */
        const subDot = document.getElementById('subagent-dot');
        const sm = s.subagent_model || {};
        subDot.className = 'conn-dot ' + (sm.connected ? 'connected' : 'disconnected');
        document.getElementById('subagent-model').textContent = '(' + (sm.name || 'unknown') + ')';
      } catch (e) {
        document.getElementById('agent-dot').className = 'conn-dot disconnected';
        document.getElementById('subagent-dot').className = 'conn-dot disconnected';
      }
    }

    /* ===== INIT ===== */
    initTheme();
    ensureToken();
    pollStatus();
    setInterval(pollStatus, 5000);
    switchView('projects');
  </script>
</body>
</html>
```

- [ ] **Step 2: Manually verify in browser**

Run: `uv run uvicorn orchestrator.main:app --host 127.0.0.1 --port 8080`
Open: `http://127.0.0.1:8080`
Verify:
- Light theme loads by default (or dark if OS preference)
- Theme toggle switches between light and dark
- Sidebar shows nav items, stats (zeroed), connections (disconnected)
- Master-detail container renders placeholder text
- Token prompt appears if no token saved
- Status polling updates sidebar after token is entered

- [ ] **Step 3: Commit**

```bash
git add web/index.html
git commit -m "feat(ui): rewrite dashboard with sidebar layout and theme toggle"
```

---

### Task 4: Implement Projects view (master-detail + inline form)

**Files:**
- Modify: `web/index.html` (replace placeholder render functions)

**Depends on:** Task 3

- [ ] **Step 1: Replace the projects render functions**

In `web/index.html`, replace the `renderProjectsView` stub and add the project detail/form rendering. Find the comment `function renderProjectsView() { /* Task 4 */ }` and replace with:

```javascript
function renderProjectsView() {
  const container = document.getElementById('view-container');
  const masterRows = projects.map(p =>
    '<div class="master-row' + (selectedProjectId === p.id ? ' selected' : '') +
    '" onclick="selectProject(\'' + esc(p.id) + '\')">' +
    '<div class="row-name">' + esc(p.name) + '</div>' +
    '<div class="row-meta">' + esc(p.model_name) + '</div>' +
    '</div>'
  ).join('') || '<div style="padding:20px;color:var(--text-faint)">No projects</div>';

  const detail = showingForm ? renderProjectForm() :
    selectedProjectId ? renderProjectDetail(projects.find(p => p.id === selectedProjectId)) :
    '<div class="detail-empty">Select a project</div>';

  container.innerHTML =
    '<div class="master-panel">' +
      '<div class="master-header">' +
        '<div class="master-title">' + projects.length + ' Projects</div>' +
        '<button class="btn btn-compact" onclick="loadProjects()">Refresh</button>' +
      '</div>' +
      '<div class="master-list">' + masterRows + '</div>' +
    '</div>' +
    '<div class="detail-panel">' + detail + '</div>';
}

function selectProject(id) {
  selectedProjectId = id;
  showingForm = false;
  renderProjectsView();
}

function renderProjectDetail(project) {
  if (!project) return '<div class="detail-empty">Project not found</div>';
  return '<div class="detail-header">' +
    '<div class="detail-title">' + esc(project.name) + '</div>' +
    '<div class="detail-subtitle">' + esc(project.repo_url) + '</div>' +
  '</div>' +
  '<div class="detail-content">' +
    '<div class="detail-section">' +
      '<div class="detail-section-title">Configuration</div>' +
      '<div class="detail-card">' +
        '<div class="detail-field"><span class="field-label">Model</span><span class="field-value" style="font-family:monospace;font-size:12px;">' + esc(project.model_name) + '</span></div>' +
        '<div class="detail-field"><span class="field-label">Default Branch</span><span class="field-value">' + esc(project.default_branch) + '</span></div>' +
        '<div class="detail-field"><span class="field-label">Approval Gate</span><span class="field-value">' + (project.approval_gate ? 'On' : 'Off') + '</span></div>' +
        '<div class="detail-field"><span class="field-label">Max Retries</span><span class="field-value">' + esc(project.max_retries) + '</span></div>' +
        '<div class="detail-field"><span class="field-label">Confidence Threshold</span><span class="field-value">' + Math.round((project.confidence_threshold || 0) * 100) + '%</span></div>' +
      '</div>' +
    '</div>' +
  '</div>' +
  '<div class="detail-actions">' +
    '<button class="btn btn-primary" onclick="switchView(\'plans\')">View Plans</button>' +
    '<button class="btn btn-danger" onclick="deleteProject(\'' + esc(project.id) + '\')">Delete</button>' +
  '</div>';
}

function renderProjectForm() {
  return '<div class="detail-header">' +
    '<div class="detail-title">New Project</div>' +
    '<div class="detail-subtitle">Add a repository for orchestration</div>' +
  '</div>' +
  '<div class="detail-content">' +
    '<form id="project-form" onsubmit="submitProject(event)">' +
      '<div class="formrow"><label>Name</label><input id="pf-name" required autocomplete="off"></div>' +
      '<div class="formrow"><label>Repository URL</label><input id="pf-repo" required autocomplete="off" placeholder="https://github.com/user/repo"></div>' +
      '<div class="formrow"><label>Model</label><input id="pf-model" required autocomplete="off" value="deepseek-coder-v2"></div>' +
      '<div style="display:flex;gap:8px;margin-top:16px">' +
        '<button type="submit" class="btn btn-primary">Create</button>' +
        '<button type="button" class="btn" onclick="showingForm=false;renderProjectsView()">Cancel</button>' +
      '</div>' +
    '</form>' +
  '</div>';
}

async function submitProject(event) {
  event.preventDefault();
  await api('POST', '/api/projects', {
    name: document.getElementById('pf-name').value,
    repo_url: document.getElementById('pf-repo').value,
    model_name: document.getElementById('pf-model').value,
  });
  showingForm = false;
  await loadProjects();
}

async function deleteProject(id) {
  if (!confirm('Delete this project?')) return;
  await api('DELETE', '/api/projects/' + id);
  selectedProjectId = null;
  await loadProjects();
}
```

- [ ] **Step 2: Manually verify in browser**

Open `http://127.0.0.1:8080`, enter token, verify:
- Project list shows in master panel
- Click project shows detail
- "+ New Project" shows inline form
- Create/cancel/delete work

- [ ] **Step 3: Commit**

```bash
git add web/index.html
git commit -m "feat(ui): implement projects master-detail view"
```

---

### Task 5: Implement Plans view (master-detail + inline form)

**Files:**
- Modify: `web/index.html` (replace plans placeholder)

**Depends on:** Task 4

- [ ] **Step 1: Replace the plans render functions**

In `web/index.html`, replace `function renderPlansView() { /* Task 4 */ }` with:

```javascript
function renderPlansView() {
  const container = document.getElementById('view-container');
  const masterRows = plans.map(p =>
    '<div class="master-row' + (selectedPlanId === p.id ? ' selected' : '') +
    '" onclick="selectPlan(\'' + esc(p.id) + '\')">' +
    '<div class="row-name" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + esc(p.spec).slice(0, 80) + '</div>' +
    '<div class="row-meta">' + esc(p.projectName) + '</div>' +
    badge(p.status) +
    '</div>'
  ).join('') || '<div style="padding:20px;color:var(--text-faint)">No plans</div>';

  const detail = showingForm ? renderPlanForm() :
    selectedPlanId ? renderPlanDetail(plans.find(p => p.id === selectedPlanId)) :
    '<div class="detail-empty">Select a plan</div>';

  container.innerHTML =
    '<div class="master-panel">' +
      '<div class="master-header">' +
        '<div class="master-title">' + plans.length + ' Plans</div>' +
        '<button class="btn btn-compact" onclick="loadPlans()">Refresh</button>' +
      '</div>' +
      '<div class="master-list">' + masterRows + '</div>' +
    '</div>' +
    '<div class="detail-panel">' + detail + '</div>';
}

function selectPlan(id) {
  selectedPlanId = id;
  showingForm = false;
  renderPlansView();
}

function renderPlanDetail(plan) {
  if (!plan) return '<div class="detail-empty">Plan not found</div>';
  const approveReject = (plan.status === 'pending' && plan.source === 'autonomous') ?
    '<button class="btn btn-primary" onclick="approvePlan(\'' + esc(plan.id) + '\')">Approve</button>' +
    '<button class="btn btn-danger" onclick="rejectPlan(\'' + esc(plan.id) + '\')">Reject</button>' : '';

  return '<div class="detail-header">' +
    '<div class="detail-title">' + esc(plan.projectName) + '</div>' +
    '<div class="detail-subtitle">Source: ' + esc(plan.source) + ' &middot; Confidence: ' +
      (plan.confidence != null ? Math.round(plan.confidence * 100) + '%' : '-') + '</div>' +
  '</div>' +
  '<div class="detail-content">' +
    '<div class="detail-section">' +
      '<div class="detail-section-title">Specification</div>' +
      '<div class="detail-card" style="white-space:pre-wrap;font-size:13px;line-height:1.6;">' + esc(plan.spec) + '</div>' +
    '</div>' +
    (plan.opus_plan ? '<div class="detail-section">' +
      '<div class="detail-section-title">Opus Plan</div>' +
      '<div class="detail-card" style="white-space:pre-wrap;font-size:12px;font-family:monospace;line-height:1.5;">' + esc(plan.opus_plan) + '</div>' +
    '</div>' : '') +
    '<div class="detail-section">' +
      '<div class="detail-section-title">Status</div>' +
      '<div class="detail-card">' +
        '<div class="detail-field"><span class="field-label">Status</span><span class="field-value">' + badge(plan.status) + '</span></div>' +
        '<div class="detail-field"><span class="field-label">Branch</span><span class="field-value" style="font-family:monospace;font-size:12px;">' + esc(plan.plan_branch_name || '-') + '</span></div>' +
      '</div>' +
    '</div>' +
  '</div>' +
  (approveReject ? '<div class="detail-actions">' + approveReject + '</div>' : '');
}

function renderPlanForm() {
  const opts = projects.map(p =>
    '<option value="' + esc(p.id) + '">' + esc(p.name) + '</option>'
  ).join('');
  return '<div class="detail-header">' +
    '<div class="detail-title">Submit Spec</div>' +
    '<div class="detail-subtitle">Create a new plan from a specification</div>' +
  '</div>' +
  '<div class="detail-content">' +
    '<form id="plan-form" onsubmit="submitPlan(event)">' +
      '<div class="formrow"><label>Project</label><select id="plf-project" required>' + opts + '</select></div>' +
      '<div class="formrow"><label>Specification</label><textarea id="plf-spec" required></textarea></div>' +
      '<div style="display:flex;gap:8px;margin-top:16px">' +
        '<button type="submit" class="btn btn-primary">Submit</button>' +
        '<button type="button" class="btn" onclick="showingForm=false;renderPlansView()">Cancel</button>' +
      '</div>' +
    '</form>' +
  '</div>';
}

async function submitPlan(event) {
  event.preventDefault();
  const projectId = document.getElementById('plf-project').value;
  await api('POST', '/api/projects/' + projectId + '/plans', {
    spec: document.getElementById('plf-spec').value,
  });
  showingForm = false;
  await loadPlans();
}

async function approvePlan(id) { await api('POST', '/api/plans/' + id + '/approve'); await loadPlans(); }
async function rejectPlan(id) { await api('POST', '/api/plans/' + id + '/reject'); await loadPlans(); }
```

- [ ] **Step 2: Manually verify in browser**

Open `http://127.0.0.1:8080`, navigate to Plans, verify:
- Plan list in master panel with spec preview, project name, status badge
- Click plan shows detail with full spec, opus plan, status, approve/reject
- "+ Submit Spec" shows inline form with project dropdown and textarea

- [ ] **Step 3: Commit**

```bash
git add web/index.html
git commit -m "feat(ui): implement plans master-detail view"
```

---

### Task 6: Implement Tasks view (master-detail with plan selector)

**Files:**
- Modify: `web/index.html` (replace tasks placeholder)

**Depends on:** Task 5

- [ ] **Step 1: Replace the tasks render function**

In `web/index.html`, replace the `renderTasks` function:

```javascript
async function renderTasks() {
  if (!plans.length) await loadPlans();
  const container = document.getElementById('view-container');

  const planOpts = plans.map(p =>
    '<option value="' + esc(p.id) + '"' + (selectedPlanId === p.id ? ' selected' : '') + '>' +
    esc(p.projectName) + ': ' + esc(p.spec).slice(0, 50) + '</option>'
  ).join('');

  container.innerHTML =
    '<div class="master-panel">' +
      '<div class="master-header">' +
        '<div class="master-title">Tasks</div>' +
        '<button class="btn btn-compact" onclick="renderTasks()">Refresh</button>' +
      '</div>' +
      '<div style="padding:12px 20px;border-bottom:1px solid var(--border-subtle);">' +
        '<label>Plan</label>' +
        '<select id="task-plan-select" onchange="onTaskPlanChange()">' + planOpts + '</select>' +
      '</div>' +
      '<div class="master-list" id="tasks-master-list"></div>' +
    '</div>' +
    '<div class="detail-panel" id="task-detail-panel"><div class="detail-empty">Select a task</div></div>';

  if (selectedPlanId || plans.length) {
    const planId = selectedPlanId || (plans[0] && plans[0].id);
    if (planId) { selectedPlanId = planId; await loadTasksForPlan(planId); }
  }
}

function onTaskPlanChange() {
  selectedPlanId = document.getElementById('task-plan-select').value;
  selectedTaskId = null;
  loadTasksForPlan(selectedPlanId);
}

let currentTasks = [];

async function loadTasksForPlan(planId) {
  currentTasks = await api('GET', '/api/plans/' + planId + '/tasks');
  const list = document.getElementById('tasks-master-list');
  list.innerHTML = currentTasks.map(t =>
    '<div class="master-row' + (selectedTaskId === t.id ? ' selected' : '') +
    '" onclick="selectTask(\'' + esc(t.id) + '\')">' +
    '<div class="row-name">' + esc(t.title) + '</div>' +
    '<div class="row-meta">' + esc(t.branch_name) + '</div>' +
    badge(t.status) +
    '</div>'
  ).join('') || '<div style="padding:20px;color:var(--text-faint)">No tasks for this plan</div>';
}

function selectTask(id) {
  selectedTaskId = id;
  const task = currentTasks.find(t => t.id === id);
  /* Re-render master rows for selection highlight */
  const list = document.getElementById('tasks-master-list');
  list.querySelectorAll('.master-row').forEach(row => {
    row.classList.toggle('selected', row.querySelector('.row-name') &&
      row.querySelector('.row-name').textContent === (task ? task.title : ''));
  });
  renderTaskDetail(task);
}

function renderTaskDetail(task) {
  const panel = document.getElementById('task-detail-panel');
  if (!task) { panel.innerHTML = '<div class="detail-empty">Task not found</div>'; return; }

  const stopBtn = task.status === 'in_progress' ?
    '<button class="btn btn-danger" onclick="stopTask(\'' + esc(task.id) + '\')">Stop</button>' : '';
  const logsBtn = '<button class="btn" onclick="viewTaskLogs(\'' + esc(task.id) + '\')">View Logs</button>';

  panel.innerHTML =
    '<div class="detail-header">' +
      '<div class="detail-title">' + esc(task.title) + '</div>' +
      '<div class="detail-subtitle">' + esc(task.branch_name) + '</div>' +
    '</div>' +
    '<div class="detail-content">' +
      '<div class="detail-section">' +
        '<div class="detail-section-title">Configuration</div>' +
        '<div class="detail-card">' +
          '<div class="detail-field"><span class="field-label">Status</span><span class="field-value">' + badge(task.status) + '</span></div>' +
          '<div class="detail-field"><span class="field-label">Attempt</span><span class="field-value">' + esc(task.attempt) + '</span></div>' +
          '<div class="detail-field"><span class="field-label">PR</span><span class="field-value">' +
            (task.pr_url ? '<a href="' + esc(task.pr_url) + '" target="_blank" rel="noreferrer">View PR</a>' : '-') + '</span></div>' +
          '<div class="detail-field"><span class="field-label">Updated</span><span class="field-value">' + esc(task.updated_at) + '</span></div>' +
        '</div>' +
      '</div>' +
      (task.description ? '<div class="detail-section">' +
        '<div class="detail-section-title">Description</div>' +
        '<div class="detail-card" style="white-space:pre-wrap;font-size:13px;line-height:1.6;">' + esc(task.description) + '</div>' +
      '</div>' : '') +
      (task.review_feedback ? '<div class="detail-section">' +
        '<div class="detail-section-title">Review Feedback</div>' +
        '<div class="detail-card" style="white-space:pre-wrap;font-size:12px;font-family:monospace;line-height:1.5;">' + esc(task.review_feedback) + '</div>' +
      '</div>' : '') +
    '</div>' +
    '<div class="detail-actions">' + stopBtn + logsBtn + '</div>';
}

async function stopTask(id) {
  await api('POST', '/api/tasks/' + id + '/stop');
  await loadTasksForPlan(selectedPlanId);
}

function viewTaskLogs(taskId) {
  switchView('logs');
  setTimeout(() => connectTaskLogs(taskId), 100);
}
```

- [ ] **Step 2: Manually verify in browser**

Navigate to Tasks, verify:
- Plan selector dropdown at top
- Task list in master panel
- Click task shows detail with config, description, review feedback
- Stop and View Logs buttons work

- [ ] **Step 3: Commit**

```bash
git add web/index.html
git commit -m "feat(ui): implement tasks master-detail view"
```

---

### Task 7: Implement Live Logs view (SSE with source selector)

**Files:**
- Modify: `web/index.html` (replace logs placeholder)

**Depends on:** Task 6

- [ ] **Step 1: Replace the logs render function and add SSE helpers**

In `web/index.html`, replace the `renderLogs` function:

```javascript
function renderLogs() {
  const container = document.getElementById('view-container');
  container.innerHTML =
    '<div class="master-panel">' +
      '<div class="master-header">' +
        '<div class="master-title">Log Sources</div>' +
        '<button class="btn btn-compact" onclick="renderLogs()">Refresh</button>' +
      '</div>' +
      '<div class="master-list">' +
        '<div class="master-row selected" onclick="connectEvents()">' +
          '<div class="row-name">System Events</div>' +
          '<div class="row-meta">SSE</div>' +
        '</div>' +
        buildTaskLogSources() +
      '</div>' +
    '</div>' +
    '<div class="detail-panel" style="display:flex;flex-direction:column;">' +
      '<div class="detail-header" style="display:flex;align-items:center;justify-content:space-between;">' +
        '<div class="detail-title" id="log-title">System Events</div>' +
        '<button class="btn btn-compact" onclick="connectEvents()">Reconnect</button>' +
      '</div>' +
      '<div style="flex:1;overflow:hidden;padding:12px 24px 24px;">' +
        '<div id="log-output" class="log"></div>' +
      '</div>' +
    '</div>';
  connectEvents();
}

function buildTaskLogSources() {
  return currentTasks
    .filter(t => t.status === 'in_progress' || t.status === 'reviewing')
    .map(t =>
      '<div class="master-row" onclick="connectTaskLogs(\'' + esc(t.id) + '\')">' +
        '<div class="row-name">' + esc(t.title) + '</div>' +
        badge(t.status) +
      '</div>'
    ).join('');
}

function appendLog(line) {
  const log = document.getElementById('log-output');
  if (!log) return;
  log.textContent += line + '\n';
  log.scrollTop = log.scrollHeight;
}

function connectEvents() {
  if (document.getElementById('log-title')) {
    document.getElementById('log-title').textContent = 'System Events';
  }
  openSse('/api/events?token=' + encodeURIComponent(ensureToken()));
}

function connectTaskLogs(taskId) {
  const task = currentTasks.find(t => t.id === taskId);
  if (document.getElementById('log-title')) {
    document.getElementById('log-title').textContent = task ? task.title : 'Task Logs';
  }
  openSse('/api/tasks/' + taskId + '/logs?token=' + encodeURIComponent(ensureToken()));
}

function openSse(path) {
  if (eventSource) eventSource.close();
  const logEl = document.getElementById('log-output');
  if (logEl) logEl.textContent = '';
  eventSource = new EventSource(API + path);
  eventSource.onopen = () => appendLog('connected');
  eventSource.onerror = () => appendLog('connection error');
  eventSource.onmessage = e => appendLog(e.data);
  ['plan_activated', 'agent_dispatched', 'review_completed', 'improvement_proposed',
   'task_completed', 'task_failed', 'task_retry', 'opus_queued', 'log', 'agent_log'].forEach(type => {
    eventSource.addEventListener(type, e => appendLog('[' + type + '] ' + e.data));
  });
}
```

- [ ] **Step 2: Manually verify in browser**

Navigate to Live Logs, verify:
- System Events source is listed and auto-connected
- Active tasks appear as log sources
- Log stream shows in monospace viewer
- Reconnect button works
- Switching sources clears and reconnects

- [ ] **Step 3: Commit**

```bash
git add web/index.html
git commit -m "feat(ui): implement live logs view with SSE source selector"
```

---

### Task 8: Final polish — responsive, edge cases, and full test run

**Files:**
- Modify: `web/index.html` (minor fixes)

**Depends on:** Task 7, Task 2

- [ ] **Step 1: Add `.superpowers/` to `.gitignore`**

Check if `.superpowers/` is already in `.gitignore`. If not, add it:

```
.superpowers/
```

- [ ] **Step 2: Run the full backend test suite**

Run: `uv run pytest --cov=orchestrator --cov-report=term-missing -v`
Expected: All tests PASS, coverage >= 80%

- [ ] **Step 3: Run linting and type checking**

Run: `uv run ruff format src/ tests/ && uv run ruff check --fix src/ tests/ && uv run mypy src/orchestrator/ --ignore-missing-imports`
Expected: No errors

- [ ] **Step 4: Manual end-to-end browser test**

Start server: `uv run uvicorn orchestrator.main:app --host 127.0.0.1 --port 8080`
Open: `http://127.0.0.1:8080`

Test checklist:
- [ ] Light theme renders correctly
- [ ] Dark theme renders correctly
- [ ] Theme persists across page reload
- [ ] Sidebar nav switches between all 4 views
- [ ] Projects: list, select, create form, delete
- [ ] Plans: list, select, create form, approve/reject (if autonomous plan exists)
- [ ] Tasks: plan selector, task list, task detail, stop button
- [ ] Logs: system events connect, task log sources appear, reconnect works
- [ ] Connections section shows agent/subagent model names and status
- [ ] Stats section updates every 5 seconds
- [ ] Responsive: narrow browser window collapses sidebar, stacks master-detail on mobile width

- [ ] **Step 5: Commit final changes**

```bash
git add .gitignore web/index.html
git commit -m "chore: final polish and responsive fixes for dashboard redesign"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1, Task 3 (no dependencies — run in parallel)
- **Wave 2:** Task 2 (depends on Task 1), Task 4 (depends on Task 3)
- **Wave 3:** Task 5 (depends on Task 4)
- **Wave 4:** Task 6 (depends on Task 5)
- **Wave 5:** Task 7 (depends on Task 6)
- **Wave 6:** Task 8 (depends on Task 7, Task 2)
