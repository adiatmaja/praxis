# Provider-Agnostic LLM Router + Models Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) tracking. **Execute after Spec 1 is merged to `main` and this branch is rebased on it** — the Models tab must include the `derive_tasks` call-site Spec 1 introduces.

**Goal:** Make every LLM call-site configurable (provider + model + effort) with defaults shown and Reset-to-defaults, so Praxis is a general orchestrator (Claude / Gemini / GPT / local) rather than Claude-only.

**Architecture:** A `LLMRouter` resolves each call-site to `{provider, model, effort}` via the existing global→project `effective_settings` pattern, then dispatches to a provider command builder. CLI providers: `claude`, `agy` (Gemini), `codex` (GPT); local routes through an LM Studio OpenAI-compatible call. `OpusBridge` and `brainstorm` call the router instead of hard-coded `claude -p`.

**Tech Stack:** Python 3.11, FastAPI, asyncio subprocess, httpx, pytest. Single-file HTML settings UI.

---

## Context for a Zero-Context Engineer

- `src/orchestrator/core/opus_bridge.py` — `_run_claude_raw` builds `["claude", "-p", prompt, "--output-format", "text", "--model", model, "--reasoning-effort", effort]`; `_run_claude` handles rate-limit detection (`_check_and_handle_rate_limit`) + raises on non-zero. Public methods: `plan_spec`, `review_diff(diff, task_description)`, `analyze_improvements`, `classify_doc` (hardcodes `claude-haiku-4-5`). `_extract_json` parses model output.
- `src/orchestrator/core/brainstorm.py` — `BrainstormSession._build_args` builds `["claude","-p",message,"--output-format","stream-json","--dangerously-skip-permissions"]` with **no `--model`**. `generate_plan` likewise.
- `src/orchestrator/core/effective_settings.py` — `EffectiveSettings(settings, db)` resolves values as override(project)→global→default; e.g. `agent_model()`. Mirror this for call-site model config.
- `src/orchestrator/api/settings.py` + `settings_overrides` table (`key, value`) persist global settings; `tests/test_api_settings.py`, `tests/test_effective_settings.py`, `tests/test_live_settings_resolution.py` show the patterns.
- Web settings popup: `web/index.html` has `.settings-tabs` (Global / Project) and `switchSettingsTab`.
- Provider CLIs (per project owner): `claude`, `agy` (Gemini), `codex` (GPT), `aider` (local). **Verify each CLI's one-shot prompt flag during implementation**; `aider` is a repo coding agent, so for one-shot brain calls the local provider routes through an LM Studio OpenAI-compatible call instead (reuse the pattern in `core/plan_derive.py`). Flag this in the PR.
- Tests: `tests/conftest.py` (`db`, `client`, `auth_headers`). Commit per task, `<type>: <desc>`, no Co-Authored-By trailer.

---

## File Structure

- **Create** `src/orchestrator/core/llm_router.py` — call-site defaults, provider builders, `run()`.
- **Create** `tests/test_llm_router.py`.
- **Modify** `src/orchestrator/core/opus_bridge.py` — call the router; add `review_diff(tier=...)`.
- **Modify** `src/orchestrator/core/brainstorm.py` — route through configured provider/model.
- **Modify** `src/orchestrator/api/settings.py` — `GET/PUT /api/settings/models`, `POST .../reset`.
- **Modify** `tests/test_api_settings.py`.
- **Modify** `web/index.html` — Settings → Models tab.

---

## Task 1: Router defaults + provider argv builders

**Files:**
- Create: `src/orchestrator/core/llm_router.py`
- Test: `tests/test_llm_router.py`

**Depends on:** None

- [ ] **Step 1: Failing test** — create `tests/test_llm_router.py`:

```python
import pytest

from orchestrator.core.llm_router import (
    CALL_SITE_DEFAULTS, build_argv, UnknownProviderError,
)


def test_defaults_cover_all_call_sites():
    expected = {
        "plan_spec", "review_diff_first", "review_diff_rereview",
        "analyze_improvements", "classify_doc", "brainstorm_run_turn",
        "brainstorm_generate_plan", "context_sync", "derive_tasks",
    }
    assert expected <= set(CALL_SITE_DEFAULTS)


def test_build_argv_claude():
    argv = build_argv("claude", model="claude-opus-4-8", effort="high", prompt="hi")
    assert argv[:2] == ["claude", "-p"]
    assert "--model" in argv and "claude-opus-4-8" in argv


def test_build_argv_unknown_provider():
    with pytest.raises(UnknownProviderError):
        build_argv("frobnicator", model="x", effort=None, prompt="hi")
```

- [ ] **Step 2: Run → fail** — `uv run pytest tests/test_llm_router.py -v`.

- [ ] **Step 3: Implement** — `src/orchestrator/core/llm_router.py`:

```python
"""Provider-agnostic LLM call routing for orchestrator brain call-sites."""

from __future__ import annotations


class UnknownProviderError(Exception):
    """Raised when a call-site config names a provider with no builder."""


# Default {provider, model, effort} per call-site (the model-tiering policy).
CALL_SITE_DEFAULTS: dict[str, dict[str, str | None]] = {
    "plan_spec": {"provider": "claude", "model": "claude-opus-4-8", "effort": "high"},
    "review_diff_first": {"provider": "claude", "model": "claude-sonnet-4-6", "effort": None},
    "review_diff_rereview": {"provider": "claude", "model": "claude-haiku-4-5", "effort": None},
    "analyze_improvements": {"provider": "claude", "model": "claude-opus-4-8", "effort": "high"},
    "classify_doc": {"provider": "claude", "model": "claude-haiku-4-5", "effort": None},
    "brainstorm_run_turn": {"provider": "claude", "model": "claude-sonnet-4-6", "effort": None},
    "brainstorm_generate_plan": {"provider": "claude", "model": "claude-sonnet-4-6", "effort": None},
    "context_sync": {"provider": "claude", "model": "claude-haiku-4-5", "effort": None},
    "derive_tasks": {"provider": "local", "model": "", "effort": None},
}


def build_argv(provider: str, model: str, effort: str | None, prompt: str) -> list[str]:
    """Build the subprocess argv for a CLI provider. 'local' is not a CLI."""
    if provider == "claude":
        argv = ["claude", "-p", prompt, "--output-format", "text"]
        if model:
            argv += ["--model", model]
        if effort:
            argv += ["--reasoning-effort", effort]
        return argv
    if provider == "agy":  # Gemini CLI — verify one-shot flag during impl
        argv = ["agy", "-p", prompt]
        if model:
            argv += ["--model", model]
        return argv
    if provider == "codex":  # GPT CLI — verify one-shot flag during impl
        argv = ["codex", "exec", prompt]
        if model:
            argv += ["--model", model]
        return argv
    message = f"Unknown or non-CLI provider: {provider}"
    raise UnknownProviderError(message)
```

- [ ] **Step 4: Run → pass** — `uv run pytest tests/test_llm_router.py -v` → 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/llm_router.py tests/test_llm_router.py
git commit -m "feat: LLM router defaults and provider argv builders"
```

---

## Task 2: Router `run()` with resolution + local provider

**Files:**
- Modify: `src/orchestrator/core/llm_router.py`
- Test: `tests/test_llm_router.py`

**Depends on:** Task 1

- [ ] **Step 1: Failing test** — append:

```python
from orchestrator.core.llm_router import LLMRouter


async def test_run_claude_provider(mocker):
    resolver = mocker.AsyncMock(return_value={"provider": "claude",
                                              "model": "claude-opus-4-8", "effort": "high"})
    proc = mocker.AsyncMock()
    proc.communicate = mocker.AsyncMock(return_value=(b"OUT", b""))
    proc.returncode = 0
    mocker.patch("asyncio.create_subprocess_exec",
                 new=mocker.AsyncMock(return_value=proc))
    router = LLMRouter(resolve=resolver)
    out = await router.run("plan_spec", "prompt", project_id=None)
    assert out == "OUT"
    resolver.assert_awaited_once()


async def test_run_local_provider(mocker):
    resolver = mocker.AsyncMock(return_value={"provider": "local",
                                              "model": "", "effort": None})
    mocker.patch("orchestrator.core.llm_router.LLMRouter._run_local",
                 new=mocker.AsyncMock(return_value="LOCAL"))
    router = LLMRouter(resolve=resolver, lm_studio_url="http://lm:1234")
    out = await router.run("derive_tasks", "p", project_id=None)
    assert out == "LOCAL"
```

- [ ] **Step 2: Run → fail** — `uv run pytest tests/test_llm_router.py -k run_ -v`.

- [ ] **Step 3: Implement** — append to `llm_router.py`:

```python
import asyncio
from collections.abc import Awaitable, Callable

Resolver = Callable[[str, str | None], Awaitable[dict]]


class LLMRouter:
    """Resolve a call-site to {provider, model, effort} and execute it."""

    def __init__(self, resolve: Resolver, lm_studio_url: str = "") -> None:
        self._resolve = resolve
        self._lm_studio_url = lm_studio_url

    async def run(self, call_site: str, prompt: str, project_id: str | None) -> str:
        cfg = await self._resolve(call_site, project_id)
        provider = cfg["provider"]
        if provider == "local":
            return await self._run_local(prompt, cfg.get("model") or "")
        argv = build_argv(provider, cfg.get("model") or "", cfg.get("effort"), prompt)
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode:
            message = f"{provider} failed (exit {proc.returncode}): {stderr.decode().strip()}"
            raise RuntimeError(message)
        return stdout.decode().strip()

    async def _run_local(self, prompt: str, model: str) -> str:
        import httpx

        url = self._lm_studio_url.rstrip("/") + "/v1/chat/completions"
        body = {"messages": [{"role": "user", "content": prompt}], "temperature": 0}
        if model:
            body["model"] = model
        async with httpx.AsyncClient(timeout=120) as http:
            resp = await http.post(url, json=body)
            resp.raise_for_status()
            return str(resp.json()["choices"][0]["message"]["content"]).strip()
```

- [ ] **Step 4: Run → pass** — `uv run pytest tests/test_llm_router.py -v`.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/llm_router.py tests/test_llm_router.py
git commit -m "feat: LLMRouter run() with provider dispatch + local backend"
```

---

## Task 3: Call-site model resolution in EffectiveSettings

**Files:**
- Modify: `src/orchestrator/core/effective_settings.py`
- Test: `tests/test_effective_settings.py`

**Depends on:** Task 1

- [ ] **Step 1: Failing test** — append to `tests/test_effective_settings.py`:

```python
async def test_resolve_call_site_falls_back_to_default(db, test_settings):
    from orchestrator.core.effective_settings import EffectiveSettings
    es = EffectiveSettings(test_settings, db)
    cfg = await es.call_site_config("plan_spec", None)
    assert cfg == {"provider": "claude", "model": "claude-opus-4-8", "effort": "high"}


async def test_resolve_call_site_global_override(db, test_settings):
    from orchestrator.core.effective_settings import EffectiveSettings
    await db.execute(
        "INSERT INTO settings_overrides (key, value) VALUES (?, ?)",
        ("models.plan_spec", '{"provider":"codex","model":"gpt-5","effort":null}'),
    )
    es = EffectiveSettings(test_settings, db)
    cfg = await es.call_site_config("plan_spec", None)
    assert cfg["provider"] == "codex"
```

- [ ] **Step 2: Run → fail** — `uv run pytest tests/test_effective_settings.py -k call_site -v`.

- [ ] **Step 3: Implement** — add to `EffectiveSettings`:

```python
    async def call_site_config(self, call_site: str, project_id: str | None) -> dict:
        import json
        from orchestrator.core.llm_router import CALL_SITE_DEFAULTS

        default = dict(CALL_SITE_DEFAULTS[call_site])
        # project override (if a per-project models column/JSON exists) → global → default
        row = await self._db.fetch_one(
            "SELECT value FROM settings_overrides WHERE key = ?",
            (f"models.{call_site}",),
        )
        if row and row["value"]:
            default.update(json.loads(row["value"]))
        return default
```

(If per-project overrides are wired via a project column, check it before the global `settings_overrides` row. For Spec 3 v1, global override is sufficient; per-project can extend this method later.)

- [ ] **Step 4: Run → pass** — `uv run pytest tests/test_effective_settings.py -v`.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/effective_settings.py tests/test_effective_settings.py
git commit -m "feat: per-call-site model resolution in effective settings"
```

---

## Task 4: Route OpusBridge + brainstorm through the router

**Files:**
- Modify: `src/orchestrator/core/opus_bridge.py`, `src/orchestrator/core/brainstorm.py`, `src/orchestrator/main.py` (construct router)
- Test: `tests/test_opus_bridge.py`

**Depends on:** Task 2, Task 3

- [ ] **Step 1: Failing test** — append to `tests/test_opus_bridge.py`:

```python
async def test_plan_spec_uses_router(db, mocker):
    from orchestrator.core.opus_bridge import OpusBridge
    router = mocker.Mock()
    router.run = mocker.AsyncMock(return_value='{"plan_summary":"s","plan_slug":"s","tasks":[]}')
    bridge = OpusBridge(db, router=router)
    out = await bridge.plan_spec("spec", "https://r", )
    router.run.assert_awaited_once()
    assert out["plan_slug"] == "s"
```

- [ ] **Step 2: Run → fail** — `uv run pytest tests/test_opus_bridge.py -k uses_router -v`.

- [ ] **Step 3: Implement** — give `OpusBridge.__init__` an optional `router` param; replace each `await self._run_claude(prompt, ...)` with `await self._router.run("<call_site>", prompt, project_id)`; `review_diff` gains `tier: str = "first"` selecting `review_diff_first`/`review_diff_rereview`; keep rate-limit handling for the claude provider inside the router or retain a claude-specific guard. For `brainstorm`, replace the bare `["claude","-p",...]` argv with `build_argv("claude"/configured, ...)` using the `brainstorm_run_turn` / `brainstorm_generate_plan` call-site config. In `main.py`, construct `LLMRouter(resolve=effective_settings.call_site_config, lm_studio_url=<project or global>)` and pass it to `OpusBridge` and `BrainstormManager`.

- [ ] **Step 4: Run → pass** — `uv run pytest tests/test_opus_bridge.py tests/test_brainstorm.py -v`.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/opus_bridge.py src/orchestrator/core/brainstorm.py src/orchestrator/main.py tests/test_opus_bridge.py
git commit -m "refactor: route brain call-sites through LLMRouter"
```

---

## Task 5: Models settings API (get/put/reset)

**Files:**
- Modify: `src/orchestrator/api/settings.py`
- Test: `tests/test_api_settings.py`

**Depends on:** Task 3

- [ ] **Step 1: Failing test** — append to `tests/test_api_settings.py`:

```python
async def test_models_get_returns_defaults(client, auth_headers):
    resp = await client.get("/api/settings/models", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["plan_spec"]["provider"] == "claude"


async def test_models_put_and_reset(client, auth_headers):
    put = await client.put("/api/settings/models", headers=auth_headers,
                           json={"call_site": "plan_spec",
                                 "config": {"provider": "codex", "model": "gpt-5", "effort": None}})
    assert put.status_code == 200
    got = await client.get("/api/settings/models", headers=auth_headers)
    assert got.json()["plan_spec"]["provider"] == "codex"
    reset = await client.post("/api/settings/models/reset", headers=auth_headers,
                              json={"call_site": "plan_spec"})
    assert reset.status_code == 200
    after = await client.get("/api/settings/models", headers=auth_headers)
    assert after.json()["plan_spec"]["provider"] == "claude"
```

- [ ] **Step 2: Run → fail** — `uv run pytest tests/test_api_settings.py -k models -v`.

- [ ] **Step 3: Implement** — add to `src/orchestrator/api/settings.py`:

```python
import json
from pydantic import BaseModel
from orchestrator.core.llm_router import CALL_SITE_DEFAULTS


class ModelPut(BaseModel):
    call_site: str
    config: dict


class ModelReset(BaseModel):
    call_site: str | None = None


@router.get("/settings/models")
async def get_models(request: Request, _: None = Depends(verify_token)) -> dict:
    db = request.app.state.db
    resolved = {}
    for site, default in CALL_SITE_DEFAULTS.items():
        row = await db.fetch_one(
            "SELECT value FROM settings_overrides WHERE key = ?", (f"models.{site}",)
        )
        cfg = dict(default)
        if row and row["value"]:
            cfg.update(json.loads(row["value"]))
        resolved[site] = {**cfg, "default": default}
    return resolved


@router.put("/settings/models")
async def put_models(request: Request, body: ModelPut,
                     _: None = Depends(verify_token)) -> dict:
    if body.call_site not in CALL_SITE_DEFAULTS:
        raise HTTPException(status_code=400, detail="unknown call_site")
    await request.app.state.db.execute(
        "INSERT INTO settings_overrides (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (f"models.{body.call_site}", json.dumps(body.config)),
    )
    return {"status": "ok"}


@router.post("/settings/models/reset")
async def reset_models(request: Request, body: ModelReset,
                       _: None = Depends(verify_token)) -> dict:
    db = request.app.state.db
    if body.call_site:
        await db.execute("DELETE FROM settings_overrides WHERE key = ?",
                         (f"models.{body.call_site}",))
    else:
        await db.execute("DELETE FROM settings_overrides WHERE key LIKE 'models.%'")
    return {"status": "ok"}
```

(Match the existing `settings.py` router object name / imports — it already imports `Request`, `Depends`, `verify_token`, `HTTPException`.)

- [ ] **Step 4: Run → pass** — `uv run pytest tests/test_api_settings.py -v`.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/api/settings.py tests/test_api_settings.py
git commit -m "feat: per-call-site Models settings API (get/put/reset)"
```

---

## Task 6: Settings → Models tab (frontend)

**Files:**
- Modify: `web/index.html`

**Depends on:** Task 5

No JS test harness; verify visually. Add a third settings tab "Models" listing one row per call-site with provider/model/effort inputs, a default badge, per-row Reset, and Reset all.

- [ ] **Step 1: Add the tab button** — beside the Global/Project tab buttons:

```html
<button class="settings-tab" type="button" id="settings-tab-models" onclick="switchSettingsTab('models')">Models</button>
```

and a panel container:

```html
<div class="settings-tab-panel" id="settings-panel-models"></div>
```

- [ ] **Step 2: Render the panel** — add JS:

```javascript
    async function loadModelsPanel() {
      const data = await api("GET", "/api/settings/models");
      const providers = ["claude", "agy", "codex", "local"];
      const rows = Object.entries(data).map(([site, cfg]) => {
        const opts = providers.map(p => '<option value="' + p + '"' +
          (cfg.provider === p ? ' selected' : '') + '>' + p + '</option>').join("");
        return '<div class="formrow"><label>' + esc(site) +
          ' <span class="doc-tag">def: ' + esc(cfg.default.provider) + '/' + esc(cfg.default.model || '-') + '</span></label>' +
          '<select id="m-prov-' + site + '">' + opts + '</select>' +
          '<input id="m-model-' + site + '" value="' + esc(cfg.model || '') + '" placeholder="model">' +
          '<input id="m-effort-' + site + '" value="' + esc(cfg.effort || '') + '" placeholder="effort">' +
          '<button class="btn btn-compact" type="button" onclick="saveModel(\'' + site + '\')">Save</button>' +
          '<button class="btn btn-compact" type="button" onclick="resetModel(\'' + site + '\')">Reset</button></div>';
      }).join("");
      document.getElementById("settings-panel-models").innerHTML = rows +
        '<button class="btn" type="button" onclick="resetModel(null)">Reset all</button>';
    }

    async function saveModel(site) {
      const config = {
        provider: document.getElementById("m-prov-" + site).value,
        model: document.getElementById("m-model-" + site).value || null,
        effort: document.getElementById("m-effort-" + site).value || null,
      };
      await api("PUT", "/api/settings/models", { call_site: site, config });
      await loadModelsPanel();
    }

    async function resetModel(site) {
      await api("POST", "/api/settings/models/reset", { call_site: site });
      await loadModelsPanel();
    }
```

- [ ] **Step 3: Hook tab switch** — in `switchSettingsTab`, when `tab === "models"` call `loadModelsPanel()` and toggle the `.active` class on `#settings-panel-models` / `#settings-tab-models` like the other tabs.

- [ ] **Step 4: Verify** — start server, open Settings → Models; confirm rows render with default badges, Save/Reset work, no console errors. Optionally extend `scripts/verify_lifecycle.js` to open the Models tab and screenshot.

- [ ] **Step 5: Commit**

```bash
git add web/index.html
git commit -m "feat: Settings Models tab for per-call-site model config"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1
- **Wave 2:** Task 2 (Task 1), Task 3 (Task 1)
- **Wave 3:** Task 4 (Task 2, Task 3), Task 5 (Task 3)
- **Wave 4:** Task 6 (Task 5)

## Post-Implementation

- [ ] Full suite green, coverage ≥ 80%, ruff + mypy clean.
- [ ] Verify the actual one-shot flags for `agy` / `codex` against their installed CLIs; adjust `build_argv`. Confirm `local` (LM Studio) path for brain calls; note that `aider` proper is the in-container implementer, not a one-shot brain CLI.
- [ ] Update `CLAUDE.md`: LLM router, per-call-site Models settings, provider CLIs.
- [ ] PR `feat/llm-router-settings` → `main`; squash-merge.
