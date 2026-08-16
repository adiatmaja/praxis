# Role-Model Fallback Config Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Add a named model registry, per-role ordered fallback chains (plan/implement/review), and a bundled coding-capability snapshot, exposed via a REST API, a `praxis config` CLI with first-run onboarding, and a dashboard "Models & Roles" tab; the router automatically falls back to the next model in a role's chain when the primary is unavailable.

**Architecture:** Config follows Praxis's existing layering — git-tracked YAML defaults in `config/praxis.yaml` overlaid by runtime JSON overrides in the `settings_overrides` table, resolved by `EffectiveSettings`. The 11 fine-grained brain call-sites each map to one of three roles via a frozen `ROLE_OF_CALL_SITE` table; `LLMRouter.run` resolves a call-site to an ordered list of `{provider, model, effort}` and tries each until one is available. "Unavailable" = auth failure, rate limit, or provider/gateway error (5xx/403/429) — a genuine bad-output/task error never triggers fallback. Nothing changes for existing installs until a chain is configured (empty chain → today's single `CALL_SITE_DEFAULTS`).

**Tech Stack:** Python 3.11, FastAPI, aiosqlite (raw SQL, no ORM), Typer + rich CLK, pytest (`asyncio_mode = "auto"`), no-build HTML/CSS/JS dashboard.

**Spec:** `docs/superpowers/specs/2026-07-17-role-model-fallback-config-design.md`

---

## Context for workers (read before starting)

You have zero prior conversation context. Key facts about this codebase:

- **Run tests:** `uv run pytest <path> -v`. **Format:** `uv run ruff format src/ tests/`. **Lint:** `uv run ruff check --fix src/ tests/`. **Types:** `uv run mypy src/orchestrator/ --ignore-missing-imports`. Line length 88. Use `X | Y` unions, built-in generics, Google docstrings, `logging` not `print`.
- **`ruff format`, NOT `ruff fmt`.**
- Tests use `asyncio_mode = "auto"` — `async def test_*` functions run directly, no decorator needed.
- Settings overrides live in the `settings_overrides` table (`key TEXT PRIMARY KEY, value TEXT, updated_at`). `EffectiveSettings.set_override(key, value)` upserts (value=None deletes); `EffectiveSettings._get_override(key)` returns the value or None (empty string treated as unset).
- YAML defaults are read via `orchestrator.core.settings_file.load_yaml_settings("config/praxis.yaml")` which returns a `dict[str, Any]`.
- The router today: `LLMRouter(resolve, lm_studio_url)` where `resolve` is `EffectiveSettings.call_site_config(call_site, project_id) -> dict`. `run(call_site, prompt, project_id, cwd=None)` resolves ONE config and executes it. It raises `ProviderAuthError`, `ProviderOutputError`, `UnknownProviderError`, or plain `RuntimeError`.
- `EventBus.publish(event: dict)` broadcasts to SSE subscribers; it auto-adds a `timestamp`. It is a plain (non-async) method.
- Providers: `claude`, `codex`, `agy` (all CLI), and `local` (LM Studio OpenAI call). See `core/llm_router.py`.
- The CLI is a Typer app at `src/cli/main.py` (`app = typer.Typer(...)`), entrypoint `orchestrator-cli`. It talks to the REST API via httpx using `ORCHESTRATOR_URL` (default `http://localhost:8080`) and `ORCHESTRATOR_TOKEN`.
- Never override or expose `auth_token` / `github_token`.

---

## File Structure

| File | Responsibility |
|------|---------------|
| `config/model_capabilities.json` (create) | Bundled capability snapshot, keyed by model id. |
| `src/orchestrator/core/capabilities.py` (create) | Load the snapshot; join onto model ids; soft refresh stub. |
| `src/orchestrator/core/provider_errors.py` (create) | Shared `is_provider_error(text)` predicate + `is_unavailability(exc)`. |
| `src/orchestrator/core/roles.py` (create) | Frozen `ROLE_OF_CALL_SITE` mapping + `MODEL_ROLES` + golden exhaustiveness. |
| `src/orchestrator/core/llm_router.py` (modify) | Chain-based `run`; fallback loop; `model_fallback` event. |
| `src/orchestrator/core/effective_settings.py` (modify) | `registered_models()`, `role_chains()`, `call_site_chain()`. |
| `src/orchestrator/core/orchestrator_reconcile.py` (modify) | Delegate `is_provider_error` to the shared helper (no behavior change). |
| `config/praxis.yaml` (modify) | Add `models.registry` + `models.roles` defaults. |
| `src/orchestrator/main.py` (modify) | Wire router to `call_site_chain` + event bus. |
| `src/orchestrator/api/settings.py` (modify) | `/settings/registry`, `/settings/roles`, `/settings/capabilities` endpoints. |
| `src/orchestrator/models/schemas.py` (modify) | `RegisteredModel`, `RoleChains` pydantic models. |
| `src/cli/main.py` (modify) | `config` Typer sub-app + onboarding hint. |
| `pyproject.toml` (modify) | Add `praxis = "cli.main:app"` console script. |
| `web/app.js`, `web/index.html`, `web/styles.css` (modify) | "Models & Roles" settings tab + onboarding banner. |
| Test files under `tests/` | One per module below. |

---

### Task 1: Capability snapshot data + loader

**Files:**
- Create: `config/model_capabilities.json`
- Create: `src/orchestrator/core/capabilities.py`
- Test: `tests/test_capabilities.py`

**Depends on:** None

- [x] **Step 1: Create the bundled snapshot**

Create `config/model_capabilities.json` with coding-relevant metrics (values are curated estimates sourced from artificialanalysis.ai; `swe_bench_verified` is a 0-1 fraction):

```json
{
  "_meta": { "source": "artificialanalysis.ai", "as_of": "2026-07-17" },
  "models": {
    "claude-opus-4-8": { "swe_bench_verified": 0.79, "agentic_coding": "high", "speed_tps": 62, "price_per_mtok_blended": 15.0 },
    "claude-sonnet-4-6": { "swe_bench_verified": 0.72, "agentic_coding": "high", "speed_tps": 91, "price_per_mtok_blended": 4.5 },
    "claude-haiku-4-5": { "swe_bench_verified": 0.55, "agentic_coding": "medium", "speed_tps": 140, "price_per_mtok_blended": 1.2 },
    "gemini-3-pro": { "swe_bench_verified": 0.71, "agentic_coding": "high", "speed_tps": 110, "price_per_mtok_blended": 3.5 }
  }
}
```

- [x] **Step 2: Write the failing test**

Create `tests/test_capabilities.py`:

```python
"""Tests for the bundled model-capability snapshot loader."""

from __future__ import annotations

import json

from orchestrator.core.capabilities import CapabilityCatalog


def test_loads_bundled_snapshot_and_returns_entry() -> None:
    catalog = CapabilityCatalog()
    entry = catalog.for_model("claude-opus-4-8")
    assert entry is not None
    assert entry["swe_bench_verified"] == 0.79
    assert catalog.as_of == "2026-07-17"


def test_missing_model_returns_none() -> None:
    catalog = CapabilityCatalog()
    assert catalog.for_model("no-such-model") is None


def test_missing_file_yields_empty_catalog(tmp_path) -> None:
    catalog = CapabilityCatalog(path=str(tmp_path / "absent.json"))
    assert catalog.for_model("claude-opus-4-8") is None
    assert catalog.all() == {}


def test_all_returns_full_model_map(tmp_path) -> None:
    p = tmp_path / "caps.json"
    p.write_text(json.dumps({"_meta": {"as_of": "2026-01-01"}, "models": {"m": {"speed_tps": 5}}}))
    catalog = CapabilityCatalog(path=str(p))
    assert catalog.all() == {"m": {"speed_tps": 5}}
    assert catalog.as_of == "2026-01-01"
```

- [x] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_capabilities.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.core.capabilities'`.

- [x] **Step 4: Implement the loader**

Create `src/orchestrator/core/capabilities.py`:

```python
"""Bundled, offline-first model-capability snapshot (coding-relevant metrics)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

DEFAULT_PATH = "config/model_capabilities.json"


class CapabilityCatalog:
    """Read-only view over the bundled capability snapshot.

    The snapshot is advisory: a missing file or missing model yields no data,
    never an error, so the orchestrator runs fully offline.
    """

    def __init__(self, path: str = DEFAULT_PATH) -> None:
        self._path = path
        self._models: dict[str, dict[str, Any]] = {}
        self.as_of: str | None = None
        self._load()

    def _load(self) -> None:
        file = Path(self._path)
        if not file.is_file():
            logger.info("Capability snapshot not found at %s; running without it", self._path)
            return
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read capability snapshot: %s", exc)
            return
        self._models = data.get("models", {})
        self.as_of = data.get("_meta", {}).get("as_of")

    def for_model(self, model_id: str) -> dict[str, Any] | None:
        """Return the capability entry for a model id, or None if absent."""
        return self._models.get(model_id)

    def all(self) -> dict[str, dict[str, Any]]:
        """Return the full model->metrics map."""
        return dict(self._models)
```

- [x] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_capabilities.py -v`
Expected: PASS (4 passed).

- [x] **Step 6: Commit**

```bash
git add config/model_capabilities.json src/orchestrator/core/capabilities.py tests/test_capabilities.py
git commit -m "feat(capabilities): add bundled coding-capability snapshot + loader"
```

---

### Task 2: Shared provider-error / unavailability predicate

**Files:**
- Create: `src/orchestrator/core/provider_errors.py`
- Modify: `src/orchestrator/core/orchestrator_reconcile.py` (delegate existing `is_provider_error`)
- Test: `tests/test_provider_errors.py`

**Depends on:** None

- [x] **Step 1: Write the failing test**

Create `tests/test_provider_errors.py`:

```python
"""Tests for the shared provider-error / unavailability predicates."""

from __future__ import annotations

from orchestrator.core.llm_router import ProviderAuthError, ProviderOutputError
from orchestrator.core.provider_errors import is_provider_error, is_unavailability


def test_provider_error_detects_gateway_signals() -> None:
    assert is_provider_error("... HTTP 429 Too Many Requests ...") is True
    assert is_provider_error("Error: Forbidden") is True
    assert is_provider_error("ECONNREFUSED") is True


def test_provider_error_ignores_normal_output() -> None:
    assert is_provider_error("tests passed, all good") is False


def test_auth_error_is_unavailability() -> None:
    assert is_unavailability(ProviderAuthError("claude", "claude login")) is True


def test_rate_limit_runtime_error_is_unavailability() -> None:
    assert is_unavailability(RuntimeError("claude failed (exit 1): usage limit reached")) is True
    assert is_unavailability(RuntimeError("HTTP 503 Service Unavailable")) is True


def test_bad_output_is_not_unavailability() -> None:
    assert is_unavailability(ProviderOutputError("empty output")) is False
    assert is_unavailability(RuntimeError("claude failed (exit 2): SyntaxError")) is False
```

- [x] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_provider_errors.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.core.provider_errors'`.

- [x] **Step 3: Implement the shared predicates**

Create `src/orchestrator/core/provider_errors.py`:

```python
"""Shared predicates for classifying provider/gateway unavailability.

Used by both the reconcile loop (worker container logs) and the LLM router
(brain-call fallback). "Unavailability" means the model endpoint is unusable
right now (auth dead, rate limited, gateway error) as opposed to the model
running fine but producing a bad answer.
"""

from __future__ import annotations

from orchestrator.core.llm_router import ProviderAuthError, ProviderOutputError


_PROVIDER_SIGNALS: tuple[str, ...] = (
    "Forbidden: request was blocked by a gateway or proxy",
    "Error: Forbidden",
    "HTTP 403",
    "HTTP 429",
    "HTTP 502",
    "HTTP 503",
    "HTTP 504",
    "rate_limit_exceeded",
    "Too Many Requests",
    "Service Unavailable",
    "Bad Gateway",
    "Gateway Timeout",
    "Connection refused",
    "ECONNREFUSED",
    "ECONNRESET",
    "connect ENOENT",
)

# Lowercase substrings that mark a rate-limit / usage-limit failure in a
# provider CLI's stderr (mirrors opus_bridge rate-limit detection).
_RATE_LIMIT_SIGNALS: tuple[str, ...] = (
    "rate limit",
    "usage limit",
    "too many requests",
)


def is_provider_error(text: str) -> bool:
    """Return True when text contains a provider/gateway error signal."""
    return any(signal in text for signal in _PROVIDER_SIGNALS)


def is_unavailability(exc: BaseException) -> bool:
    """Return True when an exception means "this model is unusable right now".

    Triggers fallback to the next model in a role's chain. A ProviderAuthError
    always qualifies. A RuntimeError qualifies only if its message carries a
    rate-limit or provider/gateway signal. A ProviderOutputError (empty/bad
    output) or any other error does NOT qualify — that is the model's answer,
    not its availability, and must not be masked by falling back.
    """
    if isinstance(exc, ProviderAuthError):
        return True
    if isinstance(exc, ProviderOutputError):
        return False
    if isinstance(exc, RuntimeError):
        msg = str(exc).lower()
        if any(sig in msg for sig in _RATE_LIMIT_SIGNALS):
            return True
        return is_provider_error(str(exc))
    return False
```

- [x] **Step 4: Delegate the reconcile predicate to the shared helper**

In `src/orchestrator/core/orchestrator_reconcile.py`, replace the body of the existing `is_provider_error` staticmethod (starts at line 302) so it delegates, keeping the signature and docstring:

```python
    @staticmethod
    def is_provider_error(logs: str) -> bool:
        """Return True when logs indicate a transient worker-side provider/gateway error.

        Delegates to ``core.provider_errors.is_provider_error`` (shared with the
        LLM router) so the signal list has a single source of truth.
        """
        from orchestrator.core.provider_errors import is_provider_error as _shared

        return _shared(logs)
```

- [x] **Step 5: Run tests to verify everything passes**

Run: `uv run pytest tests/test_provider_errors.py tests/test_orchestrator_reconcile.py -v`
Expected: PASS (new tests pass; existing reconcile provider-error tests still pass).

- [x] **Step 6: Commit**

```bash
git add src/orchestrator/core/provider_errors.py src/orchestrator/core/orchestrator_reconcile.py tests/test_provider_errors.py
git commit -m "feat(provider-errors): shared unavailability predicate for router + reconcile"
```

---

### Task 3: Role mapping + registry/chain resolution

**Files:**
- Create: `src/orchestrator/core/roles.py`
- Modify: `src/orchestrator/core/effective_settings.py`
- Modify: `config/praxis.yaml`
- Test: `tests/test_roles.py`, `tests/test_effective_settings_chains.py`

**Depends on:** None

- [x] **Step 1: Write the failing role-mapping test**

Create `tests/test_roles.py`:

```python
"""Golden tests freezing the call-site -> role mapping (exhaustive)."""

from __future__ import annotations

from orchestrator.core.llm_router import CALL_SITE_DEFAULTS
from orchestrator.core.roles import MODEL_ROLES, ROLE_OF_CALL_SITE


def test_every_call_site_has_a_role() -> None:
    missing = set(CALL_SITE_DEFAULTS) - set(ROLE_OF_CALL_SITE)
    assert not missing, f"call-sites with no role assignment: {sorted(missing)}"


def test_every_role_is_a_known_model_role() -> None:
    assert set(ROLE_OF_CALL_SITE.values()) <= set(MODEL_ROLES)


def test_model_roles_are_the_three_brain_bearing_roles() -> None:
    assert MODEL_ROLES == ("plan", "review", "implement")
```

- [x] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_roles.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.core.roles'`.

- [x] **Step 3: Implement the role mapping**

Create `src/orchestrator/core/roles.py`:

```python
"""Frozen mapping of fine-grained brain call-sites to coarse model roles.

Roles are what the operator configures (each gets an ordered fallback chain).
Call-sites are how the orchestrator routes internally. Adding a call-site to
CALL_SITE_DEFAULTS without adding it here fails the exhaustiveness test in
tests/test_roles.py — that is intentional (like core/status_vocab.py).
"""

from __future__ import annotations


# The model-bearing roles. "verify" is a deterministic shell gate (no model)
# and is intentionally absent.
MODEL_ROLES: tuple[str, ...] = ("plan", "review", "implement")

ROLE_OF_CALL_SITE: dict[str, str] = {
    "plan_spec": "plan",
    "plan_review": "plan",
    "derive_tasks": "plan",
    "analyze_improvements": "plan",
    "answer_clarification": "plan",
    "classify_doc": "plan",
    "context_sync": "plan",
    "brainstorm_run_turn": "plan",
    "brainstorm_generate_plan": "plan",
    "review_diff_first": "review",
    "review_diff_rereview": "review",
}
```

- [x] **Step 4: Run role tests to verify they pass**

Run: `uv run pytest tests/test_roles.py -v`
Expected: PASS (3 passed).

- [x] **Step 5: Add YAML defaults**

Append to `config/praxis.yaml` (after the `escalation:` block):

```yaml

models:
  # Named model registry. Role chains reference these by `name`.
  registry:
    - { name: opus,   provider: claude, model: claude-opus-4-8,    effort: high }
    - { name: sonnet, provider: claude, model: claude-sonnet-4-6,  effort: null }
    - { name: haiku,  provider: claude, model: claude-haiku-4-5,   effort: null }
    - { name: local,  provider: local,  model: "",                 effort: null }
  # Ordered fallback chains per role (first = priority).
  roles:
    plan:      [sonnet, opus]
    review:    [sonnet, haiku]
    implement: [local]
```

- [x] **Step 6: Write the failing resolution test**

Create `tests/test_effective_settings_chains.py`:

```python
"""Tests for registry + role-chain resolution in EffectiveSettings."""

from __future__ import annotations

import json

import pytest

from orchestrator.config import Settings
from orchestrator.core.effective_settings import EffectiveSettings
from orchestrator.database import Database


@pytest.fixture
async def es(tmp_path) -> EffectiveSettings:
    db = Database(f"sqlite:///{tmp_path / 'db.sqlite'}")
    await db.initialize()
    return EffectiveSettings(Settings(_env_file=None), db)


async def test_registered_models_defaults_from_yaml(es: EffectiveSettings) -> None:
    models = await es.registered_models()
    names = {m["name"] for m in models}
    assert {"opus", "sonnet", "haiku", "local"} <= names


async def test_call_site_chain_resolves_role_chain(es: EffectiveSettings) -> None:
    chain = await es.call_site_chain("plan_spec", None)
    # plan role default chain is [sonnet, opus]
    assert [c["model"] for c in chain] == ["claude-sonnet-4-6", "claude-opus-4-8"]
    assert chain[0]["provider"] == "claude"


async def test_registry_override_wins_over_yaml(es: EffectiveSettings) -> None:
    await es.set_override(
        "registry",
        json.dumps([{"name": "opus", "provider": "claude", "model": "x", "effort": None}]),
    )
    models = await es.registered_models()
    assert models == [{"name": "opus", "provider": "claude", "model": "x", "effort": None}]


async def test_empty_chain_falls_back_to_call_site_default(es: EffectiveSettings) -> None:
    await es.set_override("roles", json.dumps({"review": []}))
    chain = await es.call_site_chain("review_diff_first", None)
    # Empty role chain -> single legacy CALL_SITE_DEFAULTS entry
    assert len(chain) == 1
    assert chain[0]["model"] == "claude-sonnet-4-6"


async def test_call_site_override_used_when_no_role_chain(es: EffectiveSettings) -> None:
    # No role chain configured for a call-site whose role chain we blank out,
    # but a per-call-site override exists -> that override is a single-entry chain.
    await es.set_override("roles", json.dumps({"plan": []}))
    await es.set_override(
        "models.classify_doc",
        json.dumps({"provider": "claude", "model": "claude-haiku-4-5", "effort": None}),
    )
    chain = await es.call_site_chain("classify_doc", None)
    assert len(chain) == 1
    assert chain[0]["model"] == "claude-haiku-4-5"
```

- [x] **Step 7: Run test to verify it fails**

Run: `uv run pytest tests/test_effective_settings_chains.py -v`
Expected: FAIL with `AttributeError: 'EffectiveSettings' object has no attribute 'registered_models'`.

- [x] **Step 8: Implement resolution methods**

Add these methods to `EffectiveSettings` in `src/orchestrator/core/effective_settings.py` (place after `call_site_config`). Note `import json` and the roles import are needed:

```python
    async def registered_models(self) -> list[dict[str, Any]]:
        """Return the model registry: DB override > YAML default.

        Each entry is {name, provider, model, effort}.
        """
        import json

        raw = await self._get_override("registry")
        if raw:
            return list(json.loads(raw))
        yaml_data = await self._get_yaml()
        return list(yaml_data.get("models", {}).get("registry", []))

    async def role_chains(self) -> dict[str, list[str]]:
        """Return {role: [model_name, ...]} chains: DB override > YAML default."""
        import json

        raw = await self._get_override("roles")
        if raw:
            return dict(json.loads(raw))
        yaml_data = await self._get_yaml()
        return dict(yaml_data.get("models", {}).get("roles", {}))

    async def call_site_chain(
        self, call_site: str, project_id: str | None
    ) -> list[dict[str, Any]]:
        """Resolve a call-site to an ordered list of {provider, model, effort}.

        Precedence: configured role chain -> per-call-site override -> the
        legacy single CALL_SITE_DEFAULTS entry. An empty role chain is treated
        as "not configured" and falls through to the next tier.
        """
        from orchestrator.core.roles import ROLE_OF_CALL_SITE

        role = ROLE_OF_CALL_SITE.get(call_site)
        chains = await self.role_chains()
        chain_names = chains.get(role, []) if role else []
        if chain_names:
            registry = {m["name"]: m for m in await self.registered_models()}
            resolved: list[dict[str, Any]] = []
            for name in chain_names:
                entry = registry.get(name)
                if entry is None:
                    continue
                resolved.append(
                    {
                        "provider": entry["provider"],
                        "model": entry.get("model") or "",
                        "effort": entry.get("effort"),
                    }
                )
            if resolved:
                return resolved
        # No usable role chain -> single-entry chain from the legacy resolver.
        single = await self.call_site_config(call_site, project_id)
        return [single]
```

- [x] **Step 9: Run tests to verify they pass**

Run: `uv run pytest tests/test_effective_settings_chains.py tests/test_roles.py -v`
Expected: PASS (all green).

- [x] **Step 10: Commit**

```bash
git add src/orchestrator/core/roles.py src/orchestrator/core/effective_settings.py config/praxis.yaml tests/test_roles.py tests/test_effective_settings_chains.py
git commit -m "feat(roles): model registry + per-role fallback chain resolution"
```

---

### Task 4: Router fallback loop + event

**Files:**
- Modify: `src/orchestrator/core/llm_router.py`
- Test: `tests/test_llm_router_fallback.py`

**Depends on:** Task 2, Task 3

- [x] **Step 1: Write the failing test**

Create `tests/test_llm_router_fallback.py`:

```python
"""Tests for LLMRouter chain-based fallback."""

from __future__ import annotations

import pytest

from orchestrator.core.llm_router import LLMRouter, ProviderAuthError, ProviderOutputError


class _Bus:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def publish(self, event: dict) -> None:
        self.events.append(event)


def _router(chain, execute, bus=None) -> LLMRouter:
    async def resolve_chain(call_site, project_id):
        return chain

    r = LLMRouter(resolve_chain=resolve_chain, lm_studio_url="http://x", event_bus=bus)
    r._execute_one = execute  # type: ignore[assignment]
    return r


async def test_first_entry_used_when_available() -> None:
    async def execute(cfg, prompt, cwd):
        return f"ok:{cfg['model']}"

    r = _router([{"provider": "claude", "model": "a", "effort": None}], execute)
    assert await r.run("plan_spec", "p", None) == "ok:a"


async def test_falls_back_on_unavailability() -> None:
    calls: list[str] = []

    async def execute(cfg, prompt, cwd):
        calls.append(cfg["model"])
        if cfg["model"] == "a":
            raise ProviderAuthError("claude", "claude login")
        return "ok:b"

    bus = _Bus()
    r = _router(
        [
            {"provider": "claude", "model": "a", "effort": None},
            {"provider": "claude", "model": "b", "effort": None},
        ],
        execute,
        bus,
    )
    assert await r.run("plan_spec", "p", None) == "ok:b"
    assert calls == ["a", "b"]
    assert any(e["type"] == "model_fallback" for e in bus.events)


async def test_bad_output_does_not_fall_back() -> None:
    calls: list[str] = []

    async def execute(cfg, prompt, cwd):
        calls.append(cfg["model"])
        raise ProviderOutputError("empty")

    r = _router(
        [
            {"provider": "claude", "model": "a", "effort": None},
            {"provider": "claude", "model": "b", "effort": None},
        ],
        execute,
    )
    with pytest.raises(ProviderOutputError):
        await r.run("plan_spec", "p", None)
    assert calls == ["a"]  # never tried b


async def test_exhausted_chain_raises_last_error() -> None:
    async def execute(cfg, prompt, cwd):
        raise ProviderAuthError("claude", "claude login")

    r = _router(
        [
            {"provider": "claude", "model": "a", "effort": None},
            {"provider": "claude", "model": "b", "effort": None},
        ],
        execute,
    )
    with pytest.raises(ProviderAuthError):
        await r.run("plan_spec", "p", None)
```

- [x] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_llm_router_fallback.py -v`
Expected: FAIL — `LLMRouter.__init__` has no `resolve_chain`/`event_bus` params.

- [x] **Step 3: Refactor the router to a chain loop**

In `src/orchestrator/core/llm_router.py`:

(a) Change the `Resolver` alias and `LLMRouter.__init__`:

```python
Resolver = Callable[[str, str | None], Awaitable[list[dict]]]


class LLMRouter:
    """Resolve a call-site to an ordered chain and execute with fallback."""

    def __init__(
        self,
        resolve_chain: Resolver,
        lm_studio_url: str = "",
        event_bus: object | None = None,
    ) -> None:
        self._resolve_chain = resolve_chain
        self._lm_studio_url = lm_studio_url
        self._event_bus = event_bus
```

(b) Replace the `run` method with a chain loop that delegates single-config execution to a new `_execute_one`:

```python
    async def run(
        self,
        call_site: str,
        prompt: str,
        project_id: str | None,
        cwd: str | None = None,
    ) -> str:
        from orchestrator.core.provider_errors import is_unavailability

        chain = await self._resolve_chain(call_site, project_id)
        last_exc: BaseException | None = None
        for index, cfg in enumerate(chain):
            try:
                return await self._execute_one(cfg, prompt, cwd)
            except Exception as exc:  # noqa: BLE001 - re-raised below
                if not is_unavailability(exc) or index == len(chain) - 1:
                    raise
                last_exc = exc
                nxt = chain[index + 1]
                if self._event_bus is not None:
                    self._event_bus.publish(
                        {
                            "type": "model_fallback",
                            "call_site": call_site,
                            "from_model": cfg.get("model") or cfg.get("provider"),
                            "to_model": nxt.get("model") or nxt.get("provider"),
                            "reason": type(exc).__name__,
                        }
                    )
        # Chain empty (should not happen) — surface the last error if any.
        if last_exc is not None:
            raise last_exc
        message = f"no models configured for call-site {call_site}"
        raise ProviderOutputError(message)

    async def _execute_one(
        self, cfg: dict, prompt: str, cwd: str | None
    ) -> str:
        provider = cfg["provider"]
        if provider == "local":
            return await self._run_local(prompt, cfg.get("model") or "")
        prompt_in_argv = provider == "agy"
        if prompt_in_argv and len(prompt) > _AGY_ARGV_LIMIT:
            message = (
                f"prompt too large for agy argv ({len(prompt)} chars); agy cannot "
                "accept large prompts on this provider"
            )
            raise ProviderOutputError(message)
        argv = build_argv(provider, cfg.get("model") or "", cfg.get("effort"), prompt)
        resolved = shutil.which(argv[0])
        if resolved is None:
            raise ProviderAuthError(
                provider, LOGIN_HINTS.get(provider, f"install {provider}")
            )
        argv[0] = resolved
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdin_input = None if prompt_in_argv else prompt.encode()
        stdout, stderr = await proc.communicate(input=stdin_input)
        err = stderr.decode()
        if _looks_like_auth_failure(err):
            raise ProviderAuthError(
                provider, LOGIN_HINTS.get(provider, f"re-authenticate {provider}")
            )
        if proc.returncode:
            message = f"{provider} failed (exit {proc.returncode}): {err.strip()}"
            raise RuntimeError(message)
        out = stdout.decode().strip()
        if not out:
            message = f"{provider} returned empty output (exit 0)." + (
                " agy --print writes only to an interactive terminal and "
                "cannot be captured non-interactively."
                if provider == "agy"
                else ""
            )
            raise ProviderOutputError(message)
        return out
```

Note: this moves the old `run` body verbatim into `_execute_one`, keyed off a single `cfg` dict instead of `await self._resolve(...)`. Keep `_run_local` unchanged.

- [x] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_llm_router_fallback.py -v`
Expected: PASS (4 passed).

- [x] **Step 5: Run the existing router tests (regression)**

Run: `uv run pytest tests/test_llm_router.py -v`
Expected: Existing tests referencing the old `resolve=`/`run` single-config path will fail. Update them: construct `LLMRouter(resolve_chain=<async returns [cfg]>)` and, where they asserted single-config execution, wrap the single cfg in a one-element list. Do NOT change assertions about provider argv/output — those now live on `_execute_one` and are still exercised through `run` with a 1-element chain.

- [x] **Step 6: Commit**

```bash
git add src/orchestrator/core/llm_router.py tests/test_llm_router_fallback.py tests/test_llm_router.py
git commit -m "feat(router): ordered-chain fallback on model unavailability"
```

---

### Task 5: Wire the router in main.py

**Files:**
- Modify: `src/orchestrator/main.py:100-104`
- Test: `tests/test_main_wiring.py`

**Depends on:** Task 3, Task 4

- [x] **Step 1: Update the router construction**

In `src/orchestrator/main.py`, change the `LLMRouter(...)` block (currently lines 100-104) to pass the chain resolver and the event bus. Because the router now needs the event bus, move `app.state.event_bus = EventBus()` (currently line 114) to BEFORE the router construction:

```python
    app.state.event_bus = EventBus()
    router = LLMRouter(
        resolve_chain=effective_settings.call_site_chain,
        lm_studio_url=settings.lm_studio_url,
        event_bus=app.state.event_bus,
    )
    app.state.llm_router = router
```

Then delete the now-duplicate `app.state.event_bus = EventBus()` line that was at line 114.

- [x] **Step 2: Write a smoke test**

Create `tests/test_main_wiring.py`:

```python
"""Smoke test that the app wires the chain-based router + event bus."""

from __future__ import annotations

from orchestrator.main import app


async def test_router_uses_chain_resolver_and_bus() -> None:
    async with app.router.lifespan_context(app):
        router = app.state.llm_router
        assert router._event_bus is app.state.event_bus
        # resolve_chain returns a list for a known call-site
        chain = await router._resolve_chain("plan_spec", None)
        assert isinstance(chain, list) and chain
```

- [x] **Step 3: Run the test**

Run: `uv run pytest tests/test_main_wiring.py -v`
Expected: PASS. (If lifespan needs env, the repo's `.env` / conftest already provide `AUTH_TOKEN`; if it errors on missing Docker, that is caught and non-fatal per existing startup handling.)

- [x] **Step 4: Commit**

```bash
git add src/orchestrator/main.py tests/test_main_wiring.py
git commit -m "feat(main): wire router to role chains + event bus"
```

---

### Task 6: REST API — registry, roles, capabilities

**Files:**
- Modify: `src/orchestrator/models/schemas.py`
- Modify: `src/orchestrator/api/settings.py`
- Test: `tests/test_api_settings_models.py` (extend existing) or `tests/test_api_settings_registry.py` (create)

**Depends on:** Task 1, Task 3

- [x] **Step 1: Add pydantic models**

Append to `src/orchestrator/models/schemas.py`:

```python
class RegisteredModel(BaseModel):
    """One entry in the model registry."""

    name: str
    provider: str
    model: str = ""
    effort: str | None = None


class RoleChains(BaseModel):
    """Ordered fallback chains keyed by role name."""

    chains: dict[str, list[str]]
```

(Confirm `from pydantic import BaseModel` is already imported at the top of the file; it is used by existing schemas.)

- [x] **Step 2: Write the failing API test**

Create `tests/test_api_settings_registry.py`:

```python
"""Tests for registry / roles / capabilities settings endpoints."""

from __future__ import annotations


def test_get_registry_returns_defaults(client, auth_headers) -> None:
    resp = client.get("/api/settings/registry", headers=auth_headers)
    assert resp.status_code == 200
    names = {m["name"] for m in resp.json()}
    assert "sonnet" in names


def test_put_registry_roundtrips(client, auth_headers) -> None:
    body = [{"name": "opus", "provider": "claude", "model": "claude-opus-4-8", "effort": "high"}]
    resp = client.put("/api/settings/registry", json=body, headers=auth_headers)
    assert resp.status_code == 200
    got = client.get("/api/settings/registry", headers=auth_headers).json()
    assert got == body


def test_put_roles_rejects_unknown_model(client, auth_headers) -> None:
    resp = client.put(
        "/api/settings/roles",
        json={"chains": {"plan": ["ghost"]}},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_put_roles_rejects_empty_chain(client, auth_headers) -> None:
    resp = client.put(
        "/api/settings/roles",
        json={"chains": {"plan": []}},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_put_roles_accepts_valid_chain(client, auth_headers) -> None:
    resp = client.put(
        "/api/settings/roles",
        json={"chains": {"plan": ["sonnet", "opus"]}},
        headers=auth_headers,
    )
    assert resp.status_code == 200


def test_capabilities_joins_registry(client, auth_headers) -> None:
    resp = client.get("/api/settings/capabilities", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "models" in data and "as_of" in data
```

Note: `client` and `auth_headers` fixtures come from `tests/conftest.py` (already used by other `test_api_*.py` files).

- [x] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_api_settings_registry.py -v`
Expected: FAIL with 404s (routes not defined).

- [x] **Step 4: Implement the endpoints**

Add to `src/orchestrator/api/settings.py` (after the existing model endpoints). Add imports at the top: `from orchestrator.core.capabilities import CapabilityCatalog`, `from orchestrator.core.roles import MODEL_ROLES`, `from orchestrator.models.schemas import RegisteredModel, RoleChains`.

```python
@router.get("/settings/registry")
async def get_registry(request: Request) -> list[dict[str, Any]]:
    """Return the model registry (DB override or YAML default)."""
    es = cast(EffectiveSettings, request.app.state.effective_settings)
    return await es.registered_models()


@router.put("/settings/registry")
async def put_registry(
    request: Request, body: list[RegisteredModel]
) -> list[dict[str, Any]]:
    """Replace the model registry."""
    es = cast(EffectiveSettings, request.app.state.effective_settings)
    payload = [m.model_dump() for m in body]
    await es.set_override("registry", json.dumps(payload))
    return await es.registered_models()


@router.get("/settings/roles")
async def get_roles(request: Request) -> dict[str, list[str]]:
    """Return the per-role fallback chains."""
    es = cast(EffectiveSettings, request.app.state.effective_settings)
    return await es.role_chains()


@router.put("/settings/roles")
async def put_roles(request: Request, body: RoleChains) -> dict[str, list[str]]:
    """Replace the per-role fallback chains (validated against the registry)."""
    es = cast(EffectiveSettings, request.app.state.effective_settings)
    known = {m["name"] for m in await es.registered_models()}
    for role, chain in body.chains.items():
        if role not in MODEL_ROLES:
            raise HTTPException(422, detail=f"unknown role: {role}")
        if not chain:
            raise HTTPException(422, detail=f"role {role} chain must be non-empty")
        unknown = [n for n in chain if n not in known]
        if unknown:
            raise HTTPException(422, detail=f"unknown models in {role}: {unknown}")
    await es.set_override("roles", json.dumps(body.chains))
    return await es.role_chains()


@router.get("/settings/capabilities")
async def get_capabilities(request: Request) -> dict[str, Any]:
    """Return the bundled capability snapshot keyed by model id."""
    catalog = CapabilityCatalog()
    return {"as_of": catalog.as_of, "models": catalog.all()}


@router.post("/settings/capabilities/refresh")
async def refresh_capabilities() -> dict[str, str]:
    """Soft refresh stub — bundled snapshot only for v1 (offline-first)."""
    return {
        "status": "skipped",
        "detail": "Capability data is a bundled snapshot; live refresh not configured.",
    }
```

- [x] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_api_settings_registry.py -v`
Expected: PASS (6 passed).

- [x] **Step 6: Commit**

```bash
git add src/orchestrator/models/schemas.py src/orchestrator/api/settings.py tests/test_api_settings_registry.py
git commit -m "feat(api): registry/roles/capabilities settings endpoints"
```

---

### Task 7: CLI `praxis config` + onboarding

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/cli/main.py`
- Test: `tests/test_cli_config.py`

**Depends on:** Task 6

- [x] **Step 1: Add the `praxis` console script**

In `pyproject.toml`, under `[project.scripts]` (currently lines 39-41), add:

```toml
praxis = "cli.main:app"
```

- [x] **Step 2: Write the failing CLI test**

Create `tests/test_cli_config.py`:

```python
"""Tests for the `praxis config` CLI sub-app."""

from __future__ import annotations

import httpx
from typer.testing import CliRunner

from cli.main import app


runner = CliRunner()


def _mock_transport(handler):
    return httpx.MockTransport(handler)


def test_config_show_renders_registry(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/settings/registry":
            return httpx.Response(200, json=[{"name": "sonnet", "provider": "claude", "model": "claude-sonnet-4-6", "effort": None}])
        if request.url.path == "/api/settings/roles":
            return httpx.Response(200, json={"plan": ["sonnet"]})
        if request.url.path == "/api/settings/capabilities":
            return httpx.Response(200, json={"as_of": "2026-07-17", "models": {}})
        return httpx.Response(404)

    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "t")
    monkeypatch.setattr(
        "cli.main._client",
        lambda: httpx.Client(base_url="http://x", headers={"Authorization": "Bearer t"}, transport=_mock_transport(handler)),
    )
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0
    assert "sonnet" in result.stdout


def test_set_role_parses_csv(monkeypatch) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and request.url.path == "/api/settings/roles":
            import json as _j
            captured.update(_j.loads(request.content))
            return httpx.Response(200, json=captured["chains"])
        return httpx.Response(404)

    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "t")
    monkeypatch.setattr(
        "cli.main._client",
        lambda: httpx.Client(base_url="http://x", headers={"Authorization": "Bearer t"}, transport=_mock_transport(handler)),
    )
    result = runner.invoke(app, ["config", "set-role", "plan", "sonnet,opus"])
    assert result.exit_code == 0
    assert captured["chains"] == {"plan": ["sonnet", "opus"]}
```

- [x] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_cli_config.py -v`
Expected: FAIL — `config` command group does not exist.

- [x] **Step 4: Implement the `config` sub-app**

In `src/cli/main.py`, after the existing `app = typer.Typer(...)` line, add a config sub-app and register it. Add near the top-level commands:

```python
config_app = typer.Typer(name="config", help="Configure the model registry and role chains")
app.add_typer(config_app)


@config_app.command("show")
def config_show() -> None:
    """Show registered models, role fallback chains, and capabilities."""
    with _client() as client:
        registry = _check_list(client.get("/api/settings/registry"))
        roles = _check_dict(client.get("/api/settings/roles"))
        caps = _check_dict(client.get("/api/settings/capabilities"))
    cap_models = caps.get("models", {})

    reg_table = Table(title="Registered Models")
    for col in ("Name", "Provider", "Model", "Effort", "SWE-bench", "Speed", "$/Mtok"):
        reg_table.add_column(col)
    for m in registry:
        cap = cap_models.get(m.get("model", ""), {})
        swe = cap.get("swe_bench_verified")
        reg_table.add_row(
            m["name"], m["provider"], m.get("model") or "-", m.get("effort") or "-",
            f"{swe:.0%}" if isinstance(swe, (int, float)) else "-",
            str(cap.get("speed_tps", "-")),
            str(cap.get("price_per_mtok_blended", "-")),
        )
    console.print(reg_table)

    role_table = Table(title="Role Fallback Chains (first = priority)")
    role_table.add_column("Role")
    role_table.add_column("Chain")
    for role, chain in roles.items():
        role_table.add_row(role, " -> ".join(chain) if chain else "(default)")
    console.print(role_table)
    console.print(f"[dim]Capabilities as of {caps.get('as_of')}[/dim]")


@config_app.command("set-role")
def config_set_role(
    role: str = typer.Argument(..., help="plan | review | implement"),
    chain: str = typer.Argument(..., help="Comma-separated model names, priority first"),
) -> None:
    """Set a role's ordered fallback chain."""
    names = [n.strip() for n in chain.split(",") if n.strip()]
    with _client() as client:
        current = _check_dict(client.get("/api/settings/roles"))
        current[role] = names
        _check_dict(client.put("/api/settings/roles", json={"chains": current}))
    console.print(f"[green]Set {role}:[/green] {' -> '.join(names)}")


@config_app.command("add-model")
def config_add_model(
    name: str = typer.Argument(..., help="Registry name"),
    provider: str = typer.Argument(..., help="claude | codex | agy | local"),
    model: str = typer.Argument("", help="Provider model id"),
    effort: str = typer.Option("", help="Optional effort (e.g. high)"),
) -> None:
    """Register (or replace) a model in the registry."""
    with _client() as client:
        registry = _check_list(client.get("/api/settings/registry"))
        registry = [m for m in registry if m["name"] != name]
        registry.append({"name": name, "provider": provider, "model": model, "effort": effort or None})
        _check_list(client.put("/api/settings/registry", json=registry))
    console.print(f"[green]Registered:[/green] {name} ({provider}/{model or '-'})")


@config_app.command("refresh-capabilities")
def config_refresh_capabilities() -> None:
    """Attempt to refresh the capability snapshot (bundled-only in v1)."""
    with _client() as client:
        data = _check_dict(client.post("/api/settings/capabilities/refresh"))
    console.print(f"[yellow]{data.get('status')}[/yellow]: {data.get('detail')}")
```

- [x] **Step 5: Add the onboarding hint**

Add a top-level command that the onboarding path can call, and print a hint from `config show` when the registry override is unset. Simplest self-contained approach: add an `onboard` command:

```python
@app.command()
def onboard() -> None:
    """First-run helper: point the operator at model configuration."""
    console.print(
        "[bold]Welcome to Praxis.[/bold] No models configured yet.\n"
        "Run [cyan]praxis config[/cyan] to register models and set role fallback chains,\n"
        "or [cyan]praxis config show[/cyan] to view the current defaults."
    )
```

- [x] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli_config.py -v`
Expected: PASS (2 passed).

- [x] **Step 7: Commit**

```bash
git add pyproject.toml src/cli/main.py tests/test_cli_config.py
git commit -m "feat(cli): praxis config sub-app (registry/roles/capabilities) + onboarding"
```

---

### Task 8: Dashboard "Models & Roles" tab + onboarding banner

**Files:**
- Modify: `web/app.js`
- Modify: `web/index.html`
- Modify: `web/styles.css`

**Depends on:** Task 6

- [x] **Step 1: Extend `loadModelsPanel` to render registry + roles + capabilities**

In `web/app.js`, replace the `loadModelsPanel` function (around line 2052) so it fetches the three new endpoints and renders three blocks above the existing per-call-site rows. Keep the existing call-site rows below under a collapsible "Advanced: per-call-site overrides" heading. Full replacement:

```javascript
    async function loadModelsPanel() {
      const [registry, roles, caps] = await Promise.all([
        api("GET", "/api/settings/registry"),
        api("GET", "/api/settings/roles"),
        api("GET", "/api/settings/capabilities"),
      ]);
      const capModels = (caps && caps.models) || {};

      const regRows = registry.map(m => {
        const c = capModels[m.model || ""] || {};
        const swe = typeof c.swe_bench_verified === "number" ? Math.round(c.swe_bench_verified * 100) + "%" : "-";
        return '<tr>' +
          '<td>' + esc(m.name) + '</td>' +
          '<td>' + esc(m.provider) + '</td>' +
          '<td>' + esc(m.model || "-") + '</td>' +
          '<td>' + esc(m.effort || "-") + '</td>' +
          '<td>' + swe + '</td>' +
          '<td>' + esc(String(c.speed_tps || "-")) + '</td>' +
          '<td>' + esc(String(c.price_per_mtok_blended || "-")) + '</td>' +
          '</tr>';
      }).join("");
      const regTable =
        '<h3>Registered Models</h3>' +
        '<table class="reg-table"><thead><tr>' +
        '<th>Name</th><th>Provider</th><th>Model</th><th>Effort</th><th>SWE-bench</th><th>tok/s</th><th>$/Mtok</th>' +
        '</tr></thead><tbody>' + regRows + '</tbody></table>' +
        '<div class="doc-tag" style="margin-top:4px;">capabilities as of ' + esc(String(caps.as_of || "?")) + '</div>';

      const roleRows = ["plan", "review", "implement"].map(role => {
        const chain = (roles[role] || []).join(", ");
        return '<div class="formrow">' +
          '<label>' + role + '</label>' +
          '<input id="role-' + role + '" value="' + esc(chain) + '" placeholder="model names, priority first (comma-separated)">' +
          '<button class="btn btn-compact" type="button" onclick="saveRole(\'' + role + '\')">Save</button>' +
          '</div>';
      }).join("");
      const roleBlock = '<h3>Role Fallback Chains</h3>' +
        '<div class="doc-tag">First name = priority; later names are tried on rate-limit / auth / gateway errors.</div>' +
        roleRows;

      const providers = ["claude", "agy", "codex", "local"];
      const data = await api("GET", "/api/settings/models");
      const advRows = Object.entries(data).map(([site, cfg]) => {
        const opts = providers.map(p =>
          '<option value="' + p + '"' + (cfg.provider === p ? ' selected' : '') + '>' + p + '</option>'
        ).join("");
        return '<div class="formrow">' +
          '<label>' + esc(site) + ' <span class="doc-tag">def: ' + esc(cfg.default.provider) + '/' + esc(cfg.default.model || '-') + '</span></label>' +
          '<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;">' +
            '<select id="m-prov-' + site + '">' + opts + '</select>' +
            '<input id="m-model-' + site + '" value="' + esc(cfg.model || '') + '" placeholder="model">' +
            '<input id="m-effort-' + site + '" value="' + esc(cfg.effort || '') + '" placeholder="effort" style="flex:0 0 80px;">' +
            '<button class="btn btn-compact" type="button" onclick="saveModel(\'' + site + '\')">Save</button>' +
            '<button class="btn btn-compact" type="button" onclick="resetModel(\'' + site + '\')">Reset</button>' +
          '</div></div>';
      }).join("");
      const advBlock = '<details style="margin-top:12px;"><summary>Advanced: per-call-site overrides</summary>' +
        advRows + '<div style="margin-top:8px;"><button class="btn" type="button" onclick="resetModel(null)">Reset all</button></div></details>';

      document.getElementById("settings-panel-models").innerHTML =
        regTable + roleBlock + advBlock;
    }

    async function saveRole(role) {
      const raw = document.getElementById("role-" + role).value;
      const names = raw.split(",").map(s => s.trim()).filter(Boolean);
      const roles = await api("GET", "/api/settings/roles");
      roles[role] = names;
      try {
        await api("PUT", "/api/settings/roles", { chains: roles });
        await loadModelsPanel();
      } catch (e) {
        alert("Could not save role chain: " + (e && e.message ? e.message : e));
      }
    }
```

Keep the existing `saveModel` and `resetModel` functions as-is.

- [x] **Step 2: Add minimal styling**

Append to `web/styles.css`:

```css
.reg-table { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 6px; }
.reg-table th, .reg-table td { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--border); }
.reg-table th { color: var(--text-muted); font-weight: 500; }
```

- [x] **Step 3: Add the onboarding banner**

In `web/index.html`, find where the main dashboard content begins (search for the first top-level view container, e.g. an element with `id="app"` or the projects view). Add a hidden banner element right inside it:

```html
<div id="onboarding-banner" class="onboarding-banner" style="display:none;">
  No models configured yet. Open <strong>Settings → Models &amp; Roles</strong> to register models and set fallback chains.
  <button class="btn btn-compact" type="button" onclick="dismissOnboarding()">Dismiss</button>
</div>
```

Append to `web/styles.css`:

```css
.onboarding-banner { background: var(--accent-muted, #2a2a3a); color: var(--text); padding: 10px 14px; border-radius: 6px; margin: 8px 0; font-size: 13px; }
```

In `web/app.js`, add a check that runs on initial load (call it from the existing app-init path — search for where projects first load, e.g. `loadProjects()` at startup, and add `checkOnboarding();` after it):

```javascript
    async function checkOnboarding() {
      try {
        const roles = await api("GET", "/api/settings/roles");
        const configured = roles && Object.values(roles).some(c => Array.isArray(c) && c.length);
        const dismissed = localStorage.getItem("praxis_onboarding_dismissed") === "1";
        const banner = document.getElementById("onboarding-banner");
        if (banner) banner.style.display = (!configured && !dismissed) ? "block" : "none";
      } catch (e) { /* non-fatal */ }
    }

    function dismissOnboarding() {
      localStorage.setItem("praxis_onboarding_dismissed", "1");
      const banner = document.getElementById("onboarding-banner");
      if (banner) banner.style.display = "none";
    }
```

- [x] **Step 4: Manual verification**

Run the server (`docker compose -f docker-compose.yml -f docker-compose.local.yml up --build -d`), open the dashboard, go to Settings → Models. Confirm: Registered Models table shows rows with SWE-bench/speed/price; Role Fallback Chains show editable comma-separated inputs; saving `plan` = `opus,ghost` shows an error (unknown model); the Advanced details block still lists all call-sites. There is no automated test for the static dashboard (consistent with the repo — the `web/` assets have no JS test harness).

- [x] **Step 5: Commit**

```bash
git add web/app.js web/index.html web/styles.css
git commit -m "feat(dashboard): Models & Roles tab (registry + chains + capabilities) + onboarding banner"
```

---

### Task 9: Docs — CLAUDE.md gotchas + deployment reference

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/deployment.md`

**Depends on:** Task 1-8

- [x] **Step 1: Add a gotcha line to CLAUDE.md**

In `CLAUDE.md`, under the `## Gotchas` condensed index, add:

```markdown
- **Role fallback chains resolve before per-call-site overrides** — `EffectiveSettings.call_site_chain` maps a call-site to a role (`core/roles.ROLE_OF_CALL_SITE`, frozen + golden-tested) then to an ordered registry chain; an EMPTY chain falls through to the `models.<call_site>` override, then `CALL_SITE_DEFAULTS`. The router (`LLMRouter.run`) tries each entry and falls back ONLY on unavailability (`core/provider_errors.is_unavailability`: auth/rate-limit/gateway) — a bad-output error never falls back. `implement` role is NOT router-driven in v1 (worker model is spawn-baked).
```

- [x] **Step 2: Document the config surfaces in deployment.md**

Add a "Model registry & role fallback" section to `docs/deployment.md` covering: the `config/praxis.yaml` `models.registry`/`models.roles` defaults, the `/api/settings/registry|roles|capabilities` endpoints, `praxis config` CLI commands (`show`, `set-role`, `add-model`, `refresh-capabilities`), and that `config/model_capabilities.json` is a bundled offline snapshot. Match the existing prose style of that file.

- [x] **Step 3: Run the full suite + lint + types**

Run:
```bash
uv run pytest --cov=orchestrator -q
uv run ruff format src/ tests/ && uv run ruff check --fix src/ tests/
uv run mypy src/orchestrator/ --ignore-missing-imports
```
Expected: all tests pass, coverage ≥ 80%, ruff clean, mypy clean.

- [x] **Step 4: Commit**

```bash
git add CLAUDE.md docs/deployment.md
git commit -m "docs: role-model fallback config (gotcha + deployment reference)"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1 (capabilities), Task 2 (provider-errors), Task 3 (roles + resolution) — no dependencies, run in parallel.
- **Wave 2:** Task 4 (router fallback — needs Task 2, Task 3), Task 6 (API — needs Task 1, Task 3).
- **Wave 3:** Task 5 (main wiring — needs Task 3, Task 4), Task 7 (CLI — needs Task 6), Task 8 (dashboard — needs Task 6).
- **Wave 4:** Task 9 (docs + full-suite gate — needs Task 1-8).

---

## Notes

- **Inter-task contracts (pin these; do not rename):** `CapabilityCatalog.for_model` / `.all()` / `.as_of` (Task 1); `provider_errors.is_provider_error(text)` / `is_unavailability(exc)` (Task 2); `roles.MODEL_ROLES` (`("plan","review","implement")`) / `roles.ROLE_OF_CALL_SITE` (Task 3); `EffectiveSettings.registered_models()` / `.role_chains()` / `.call_site_chain(call_site, project_id)` returning `list[dict]` (Task 3); `LLMRouter(resolve_chain, lm_studio_url, event_bus)` + `_execute_one(cfg, prompt, cwd)` (Task 4). The `model_fallback` SSE event shape: `{type, call_site, from_model, to_model, reason}`.
- **Back-compat:** existing installs with no `registry`/`roles` overrides get the YAML defaults, which reproduce today's routing (sonnet for plan/review-first, etc.), so behavior is unchanged until an operator edits a chain.
- **Out of scope (deferred, per spec):** implement-role fallback through the spawn path, per-project role chains, automated capability refresh.
```