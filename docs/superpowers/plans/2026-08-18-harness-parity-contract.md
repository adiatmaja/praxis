# Harness Parity Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make delegation to any harness manageable and predictable by giving every harness one declared contract: the same selection rule, the same thinking-effort signal, and the same token telemetry shape.

**Architecture:** `core/harnesses.py` already decides which image runs a task; it becomes the single source of truth for *how* that harness is driven too. Two new declarative fields on `HarnessSpec` (`effort_channel`, `reports_tokens`) let a new pure module `core/worker_effort.py` resolve one explicit effort value per spawn, following the same never-silent rule that `core/thinking.py` encodes for brain payloads. The spawn env contract carries that value to the entrypoints, the callback carries token counts back, and `execute_plan` stops overwriting a project's configured harness with the global default.

**Tech Stack:** Python 3.11, FastAPI, aiosqlite (raw SQL + versioned `MIGRATIONS`), pytest with `asyncio_mode = "auto"`, bash entrypoints in `docker/*-agent/`.

---

## Background: the three defects this closes

Verified by reading the code on 2026-08-18:

1. **Harness selection is silently overwritten.** `src/orchestrator/api/execute_plan.py:174` computes `harness = body.harness or default_harness_id()` and then `_create_or_reuse_project` (`execute_plan.py:65-68`) runs `UPDATE projects SET model_name = ?, harness = ?` unconditionally. A caller who omits `harness` re-points an existing project at `opencode` even when it was configured for `agy`.

2. **The thinking signal is not equivalent across harnesses.** `build_spawn_env` (`src/orchestrator/core/agent_manager.py:190-230`) sets `MODEL`, `HARNESS`, `OPENAI_API_BASE`, `MODEL_CONTEXT_LIMIT` and never mentions reasoning effort. `docker/opencode-agent/entrypoint.sh:252-271` therefore writes an LM Studio provider config with no `reasoning_effort`, and per `core/thinking.py` an absent key means **maximum** effort on qwen3.8, not off. agy expresses effort inside the model string instead (`docker/agy-agent/entrypoint.sh`, e.g. `"Gemini 3.5 Flash (High)"`). Same task, two different and undeclared thinking regimes.

3. **There is no token telemetry at all.** `agent_runs` (`src/orchestrator/database.py:82-91`) has no token column, and `AgentDonePayload` (`src/orchestrator/api/internal.py:22-30`) has no token field. Runs cannot be compared across harnesses.

**Scope boundary:** this plan does not try to make OpenCode *report* tokens (it does not expose them). It makes the absence explicit and queryable rather than invisible. Recording `NULL` because the harness declares `reports_tokens=False` is a predictable answer; recording nothing is not.

## File Structure

| File | Responsibility |
|------|----------------|
| `src/orchestrator/core/harnesses.py` (modify) | Declares each harness's effort channel and token-reporting capability alongside its image |
| `src/orchestrator/core/worker_effort.py` (create) | Pure resolver: harness id + configured effort -> the explicit env value, or `None` when the channel is not env |
| `src/orchestrator/core/agent_manager.py` (modify) | Threads `WORKER_REASONING_EFFORT` into the spawn env contract |
| `src/orchestrator/config.py` (modify) | Adds the `worker_reasoning_effort` setting (env > YAML > default) |
| `docker/opencode-agent/entrypoint.sh` (modify) | Emits `reasoning_effort` into the LM Studio provider model options |
| `src/orchestrator/database.py` (modify) | Migration 8: `agent_runs.tokens_used`, `agent_runs.tokens_source` |
| `src/orchestrator/api/internal.py` (modify) | Accepts and persists `tokens_used` on the completion callback |
| `docker/agy-agent/entrypoint.sh` (modify) | Reports token usage on the callback |
| `src/orchestrator/api/execute_plan.py` (modify) | Resolves harness from body > existing project > default; never downgrades silently |
| `tests/test_worker_effort.py` (create) | Unit tests for the resolver |
| `tests/test_harness_parity.py` (create) | Contract tests binding registry declarations to real consumer behavior |

---

### Task 1: Declare the per-harness driving contract

**Files:**
- Modify: `src/orchestrator/core/harnesses.py:13-29` (dataclass), `:32-117` (registry entries)
- Test: `tests/test_harness_parity.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

Create `tests/test_harness_parity.py`:

```python
"""Contract tests: every harness must DECLARE how it is driven.

The point of these tests is that a new harness cannot be added without
answering the two questions that make delegation predictable: how does it
receive a thinking-effort signal, and does it report token usage.
"""

from __future__ import annotations

import pytest

from orchestrator.core.harnesses import EFFORT_CHANNELS, REGISTRY


@pytest.mark.unit
def test_every_harness_declares_a_known_effort_channel() -> None:
    for harness_id, spec in REGISTRY.items():
        assert spec.effort_channel in EFFORT_CHANNELS, (
            f"{harness_id} declares unknown effort_channel {spec.effort_channel!r}"
        )


@pytest.mark.unit
def test_every_harness_declares_token_reporting() -> None:
    for harness_id, spec in REGISTRY.items():
        assert isinstance(spec.reports_tokens, bool), harness_id


@pytest.mark.unit
def test_declared_channels_match_the_verified_reality() -> None:
    # opencode is driven through an OpenAI-compatible provider config, so the
    # effort is a request parameter we control. agy takes its effort inside the
    # Gemini model string ("Gemini 3.5 Flash (High)") and exposes no separate knob.
    assert REGISTRY["opencode"].effort_channel == "request_option"
    assert REGISTRY["opencode"].reports_tokens is False
    assert REGISTRY["agy"].effort_channel == "model_name"
    assert REGISTRY["agy"].reports_tokens is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_harness_parity.py -v`
Expected: FAIL with `ImportError: cannot import name 'EFFORT_CHANNELS' from 'orchestrator.core.harnesses'`

- [ ] **Step 3: Write minimal implementation**

In `src/orchestrator/core/harnesses.py`, add above the dataclass (after the imports):

```python
#: How a harness receives a thinking-effort signal.
#:
#: ``request_option`` - praxis controls it directly in the request/provider
#:   config, so it MUST be stated explicitly (see :mod:`orchestrator.core.thinking`:
#:   an absent key means MAXIMUM effort on qwen3.8, not off).
#: ``model_name``     - the effort is encoded in the model string itself
#:   (e.g. "Gemini 3.5 Flash (High)"); there is no separate knob to set.
#: ``none``           - the harness exposes no effort control at all.
EFFORT_CHANNELS: frozenset[str] = frozenset({"request_option", "model_name", "none"})
```

Add these two fields to `HarnessSpec`, after `does_own_git: bool = True` and before `notes`:

```python
    effort_channel: str = "none"
    reports_tokens: bool = False
```

In the `opencode` registry entry, add after `does_own_git=False,`:

```python
        effort_channel="request_option",
        reports_tokens=False,
```

In the `agy` registry entry, add after `supports_local_llm=False,`:

```python
        effort_channel="model_name",
        reports_tokens=True,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_harness_parity.py -v`
Expected: PASS, 3 passed

- [ ] **Step 5: Mutation check — prove the contract test bites**

Temporarily change the `opencode` entry to `effort_channel="none"`, then run:

Run: `uv run pytest tests/test_harness_parity.py -v -s`
Expected: FAIL on `test_declared_channels_match_the_verified_reality`

Restore `effort_channel="request_option"` and re-run to confirm PASS. Do not `git checkout --` the file (it may hold other uncommitted work); edit it back by hand.

- [ ] **Step 6: Verify the harness list API still serializes**

`list_harnesses()` uses `dataclasses.asdict`, so the new fields flow into `GET /api/harnesses` automatically.

Run: `uv run pytest tests/ -k harness -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator/core/harnesses.py tests/test_harness_parity.py
git commit -m "feat: declare per-harness effort channel and token reporting"
```

---

### Task 2: The effort resolver

**Files:**
- Create: `src/orchestrator/core/worker_effort.py`
- Test: `tests/test_worker_effort.py`

**Depends on:** Task 1

- [ ] **Step 1: Write the failing test**

Create `tests/test_worker_effort.py`:

```python
"""Unit tests for the worker thinking-effort resolver."""

from __future__ import annotations

import pytest

from orchestrator.core.worker_effort import (
    DEFAULT_WORKER_EFFORT,
    VALID_EFFORTS,
    resolve_worker_effort,
)


@pytest.mark.unit
def test_request_option_harness_always_gets_an_explicit_value() -> None:
    assert resolve_worker_effort("opencode", "medium") == "medium"


@pytest.mark.unit
def test_none_configured_still_yields_an_explicit_value_not_none() -> None:
    # An absent reasoning_effort means MAXIMUM effort downstream, so "no
    # opinion" must resolve to a stated level, never to silence.
    assert resolve_worker_effort("opencode", None) == DEFAULT_WORKER_EFFORT
    assert DEFAULT_WORKER_EFFORT in VALID_EFFORTS


@pytest.mark.unit
def test_model_name_harness_gets_nothing_to_set() -> None:
    # agy carries effort inside the model string; setting an env var would be
    # a lie that reads as configured-but-ignored.
    assert resolve_worker_effort("agy", "high") is None


@pytest.mark.unit
def test_unknown_harness_falls_back_to_no_signal() -> None:
    assert resolve_worker_effort("does-not-exist", "high") is None


@pytest.mark.unit
def test_invalid_effort_is_rejected_loudly() -> None:
    with pytest.raises(ValueError, match="unsupported reasoning effort"):
        resolve_worker_effort("opencode", "turbo")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_worker_effort.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.core.worker_effort'`

- [ ] **Step 3: Write minimal implementation**

Create `src/orchestrator/core/worker_effort.py`:

```python
"""Resolve the thinking-effort signal sent to an implementation worker.

Why this file exists
--------------------
:mod:`orchestrator.core.thinking` encodes the rule for BRAIN payloads: never
express "no thinking" as an absent key, because an omitted ``reasoning_effort``
means MAXIMUM effort on qwen3.8, not off. Workers had no equivalent. OpenCode's
provider config carried no effort at all while agy took its effort baked into
the Gemini model string, so the same task ran under two different and
undeclared thinking regimes depending on which harness picked it up.

This module is the worker-side half of that rule. It reads the harness's
declared ``effort_channel`` (see :mod:`orchestrator.core.harnesses`) and answers
one question: what, if anything, should the spawn environment state?

Returning ``None`` is meaningful and is NOT the same as "off". It means the
harness has no knob to turn, so praxis must not pretend to have set one.
"""

from __future__ import annotations

import logging

from orchestrator.core.harnesses import REGISTRY


logger = logging.getLogger(__name__)

#: Effort levels accepted by the OpenAI-compatible providers praxis drives.
VALID_EFFORTS: frozenset[str] = frozenset({"none", "low", "medium", "high"})

#: Used when the operator expressed no preference. "none" is the only level
#: measured to yield zero reasoning tokens on the configured endpoint; see the
#: measurement table in :mod:`orchestrator.core.thinking`.
DEFAULT_WORKER_EFFORT = "none"


def resolve_worker_effort(harness_id: str, configured: str | None) -> str | None:
    """Return the effort value to place in the spawn environment.

    Args:
        harness_id: The harness that will run the task.
        configured: The operator's requested level, or None for no preference.

    Returns:
        An explicit level for harnesses praxis drives through a request option,
        or None when the harness has no separate effort knob.

    Raises:
        ValueError: If ``configured`` is not a supported level.
    """
    if configured is not None and configured not in VALID_EFFORTS:
        raise ValueError(
            f"unsupported reasoning effort {configured!r}; "
            f"expected one of {sorted(VALID_EFFORTS)}"
        )

    spec = REGISTRY.get(harness_id)
    if spec is None:
        logger.warning(
            "Unknown harness %s; sending no effort signal to the worker",
            harness_id,
        )
        return None

    if spec.effort_channel != "request_option":
        return None

    return configured or DEFAULT_WORKER_EFFORT
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_worker_effort.py -v`
Expected: PASS, 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/worker_effort.py tests/test_worker_effort.py
git commit -m "feat: add worker thinking-effort resolver"
```

---

### Task 3: The `worker_reasoning_effort` setting

**Files:**
- Modify: `src/orchestrator/config.py`
- Modify: `config/praxis.yaml`
- Test: `tests/test_worker_effort.py` (append)

**Depends on:** Task 2

- [ ] **Step 1: Read the surrounding settings block**

Run: `grep -n "loop_interval" src/orchestrator/config.py`

Add the new field beside the other worker-facing settings, matching the file's existing `Field(...)` style. Read the two fields above and below your insertion point before writing so the annotation style matches.

- [ ] **Step 2: Write the failing test**

Append to `tests/test_worker_effort.py`:

```python
@pytest.mark.unit
def test_settings_expose_worker_reasoning_effort_default() -> None:
    from orchestrator.config import Settings

    settings = Settings(_env_file=None, AUTH_TOKEN="t", GITHUB_TOKEN="t")
    assert settings.worker_reasoning_effort == DEFAULT_WORKER_EFFORT


@pytest.mark.unit
def test_env_overrides_worker_reasoning_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator.config import Settings

    monkeypatch.setenv("WORKER_REASONING_EFFORT", "medium")
    settings = Settings(_env_file=None, AUTH_TOKEN="t", GITHUB_TOKEN="t")
    assert settings.worker_reasoning_effort == "medium"
```

Note: `_env_file=None` is required — `.env` is read by pydantic-settings and would otherwise supply values.

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_worker_effort.py -k settings -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'worker_reasoning_effort'`

- [ ] **Step 4: Write minimal implementation**

Add to the `Settings` class in `src/orchestrator/config.py`:

```python
    worker_reasoning_effort: str = Field(
        default="none",
        description=(
            "Thinking effort sent to harnesses praxis drives through a request "
            "option (currently OpenCode). Harnesses that encode effort in the "
            "model string (agy) ignore this. One of: none, low, medium, high."
        ),
    )
```

Add to `config/praxis.yaml`, under the existing top-level keys:

```yaml
# Thinking effort for implementation workers driven through a request option
# (OpenCode). An ABSENT reasoning_effort means MAXIMUM effort on qwen3.8, so
# praxis always states this explicitly. Harnesses that bake effort into the
# model string (agy) ignore it.
praxis_worker_reasoning_effort: none
```

Confirm the YAML key prefix against `core/settings_file.load_yaml_settings` before writing — the loader maps `praxis_<field>` to `<FIELD>`.

Run: `grep -n "praxis_" src/orchestrator/core/settings_file.py | head -5`

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_worker_effort.py -v`
Expected: PASS, 7 passed

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/config.py config/praxis.yaml tests/test_worker_effort.py
git commit -m "feat: add worker_reasoning_effort setting"
```

---

### Task 4: Carry the effort into the spawn environment

**Files:**
- Modify: `src/orchestrator/core/agent_manager.py:165-230` (`build_spawn_env`)
- Test: `tests/test_harness_parity.py` (append)

**Depends on:** Task 2

- [ ] **Step 1: Write the failing test**

Append to `tests/test_harness_parity.py`:

```python
from orchestrator.core.agent_manager import build_spawn_env


def _spawn_env(harness_id: str, **kwargs: object) -> dict[str, str]:
    return build_spawn_env(
        repo_url="https://github.com/o/r",
        branch="agent/x",
        base_branch="main",
        task_prompt="do the thing",
        container_lm_url="http://host.docker.internal:1234",
        model_name="qwen3.8-27b",
        harness_id=harness_id,
        gh_token="tok",
        callback_url="http://orchestrator:8080/internal/agent-done",
        task_id="task-1",
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.unit
def test_opencode_spawn_env_states_reasoning_effort_explicitly() -> None:
    env = _spawn_env("opencode", reasoning_effort="medium")
    assert env["WORKER_REASONING_EFFORT"] == "medium"


@pytest.mark.unit
def test_opencode_spawn_env_never_omits_the_effort_key() -> None:
    # Silence is the bug this guards: an absent key means MAXIMUM effort.
    env = _spawn_env("opencode")
    assert "WORKER_REASONING_EFFORT" in env
    assert env["WORKER_REASONING_EFFORT"] == "none"


@pytest.mark.unit
def test_agy_spawn_env_omits_the_key_it_cannot_honor() -> None:
    env = _spawn_env("agy", reasoning_effort="high")
    assert "WORKER_REASONING_EFFORT" not in env
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_harness_parity.py -v`
Expected: FAIL with `TypeError: build_spawn_env() got an unexpected keyword argument 'reasoning_effort'`

- [ ] **Step 3: Write minimal implementation**

In `src/orchestrator/core/agent_manager.py`, add the import near the other `orchestrator.core` imports:

```python
from orchestrator.core.worker_effort import resolve_worker_effort
```

Add the parameter to `build_spawn_env`, after `worker_session_id: str | None = None,`:

```python
    reasoning_effort: str | None = None,
```

Add this block just before `return environment`:

```python
    # Effort is resolved from the harness's DECLARED channel, so a harness that
    # bakes effort into its model string gets no env var rather than a variable
    # it would silently ignore. See core/worker_effort.py.
    effective_effort = resolve_worker_effort(harness_id, reasoning_effort)
    if effective_effort is not None:
        environment["WORKER_REASONING_EFFORT"] = effective_effort
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_harness_parity.py -v`
Expected: PASS, 6 passed

- [ ] **Step 5: Pass the setting through at the call site**

`build_spawn_env` is called once, at `src/orchestrator/core/agent_manager.py:340`. Read that call and the surrounding `spawn_agent` signature:

Run: `sed -n 300,350p src/orchestrator/core/agent_manager.py`

Thread the value through from settings the same way the other spawn inputs arrive. If `spawn_agent` already holds a settings object, pass `reasoning_effort=settings.worker_reasoning_effort`; if it does not, add a `reasoning_effort: str | None = None` parameter to `spawn_agent` and supply it from the dispatch mixin (`src/orchestrator/core/orchestrator_dispatch.py`) where the other per-task spawn arguments are assembled.

- [ ] **Step 6: Mutation check — prove the seam is live, not just unit-green**

Three of five fixes in a past session passed every unit test and did nothing in production, because the config never reached the consumer. Prove the wiring:

Delete the `reasoning_effort=` argument at the `spawn_agent` call site, then run:

Run: `uv run pytest tests/test_harness_parity.py -v -s`

If this still passes, your tests only cover `build_spawn_env` in isolation and the wiring is unverified. Add a test that calls the real dispatch path with a mocked Docker client and asserts `WORKER_REASONING_EFFORT` reached the container `environment` dict. Restore the argument and confirm PASS.

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator/core/agent_manager.py src/orchestrator/core/orchestrator_dispatch.py tests/test_harness_parity.py
git commit -m "feat: carry explicit reasoning effort into the spawn env contract"
```

---

### Task 5: OpenCode entrypoint honors the effort

**Files:**
- Modify: `docker/opencode-agent/entrypoint.sh:246-271`
- Test: executed shell assertion (below)

**Depends on:** Task 4

- [ ] **Step 1: Write the failing check**

Create `tests/shell/test_opencode_effort.sh`:

```bash
#!/usr/bin/env bash
# Asserts the generated OpenCode config states reasoning_effort explicitly.
# This EXECUTES the config-writing block; a syntax check alone has shipped a
# real bug here before (printf leading-dash), so never settle for `bash -n`.
set -euo pipefail

WORK="$(mktemp -d)"
export HOME="${WORK}"
export MODEL="qwen3.8-27b"
export OPENAI_API_BASE="http://host.docker.internal:1234/v1"
export MODEL_CONTEXT_LIMIT="32768"
export WORKER_REASONING_EFFORT="medium"
BIBLE_INSTRUCTIONS=''

# Extract and run only the config-writing block from the real entrypoint.
sed -n '/^echo "--- Writing OpenCode config/,/^EOF$/p' \
    "$(dirname "$0")/../../docker/opencode-agent/entrypoint.sh" > "${WORK}/block.sh"
# shellcheck disable=SC1090
. "${WORK}/block.sh"

CFG="${HOME}/.config/opencode/opencode.json"
python3 - "$CFG" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
model = cfg["provider"]["lmstudio"]["models"]["qwen3.8-27b"]
opts = model.get("options", {})
assert opts.get("reasoning_effort") == "medium", (
    f"expected reasoning_effort=medium, got {opts!r}"
)
print("OK: reasoning_effort stated explicitly")
PY
rm -rf "${WORK}"
```

Make it executable and run it:

Run: `chmod +x tests/shell/test_opencode_effort.sh && bash tests/shell/test_opencode_effort.sh`
Expected: FAIL with `AssertionError: expected reasoning_effort=medium, got {}`

- [ ] **Step 2: Write minimal implementation**

In `docker/opencode-agent/entrypoint.sh`, replace the `model_cfg` construction (currently lines 252-258) with:

```bash
# WORKER_REASONING_EFFORT is resolved by the orchestrator from the harness's
# declared effort channel (src/orchestrator/core/worker_effort.py). It is ALWAYS
# set for this harness: an absent reasoning_effort means MAXIMUM effort on
# qwen3.8, not off, so silence here is a real behaviour change with no error.
effort="${WORKER_REASONING_EFFORT:-none}"
model_opts='"options": { "reasoning_effort": "'"${effort}"'" }'
echo "Using reasoning effort: ${effort}"

# MODEL_CONTEXT_LIMIT is detected per-model from LM Studio by the orchestrator
# (never hardcoded). When present, advertise it as the model's context limit so
# OpenCode's auto-compaction triggers at the real window instead of overflowing
# into silent server-side truncation. Omitted -> OpenCode uses its own default.
model_cfg='{ "name": "'"${MODEL}"'", '"${model_opts}"' }'
if [ -n "${MODEL_CONTEXT_LIMIT:-}" ]; then
    # OpenCode's schema requires BOTH context and output in a limit block;
    # omitting output fails validation ("Missing key ...limit.output").
    model_cfg='{ "name": "'"${MODEL}"'", '"${model_opts}"', "limit": { "context": '"${MODEL_CONTEXT_LIMIT}"', "output": 8192 } }'
    echo "Using detected context limit: ${MODEL_CONTEXT_LIMIT}"
fi
```

- [ ] **Step 3: Run the check to verify it passes**

Run: `bash tests/shell/test_opencode_effort.sh`
Expected: `OK: reasoning_effort stated explicitly`

- [ ] **Step 4: Verify the no-limit branch also emits valid JSON**

Run: `MODEL_CONTEXT_LIMIT= bash tests/shell/test_opencode_effort.sh`
Expected: `OK: reasoning_effort stated explicitly` (the assertion is on options, which both branches carry)

- [ ] **Step 5: Shellcheck the modified entrypoint**

Run: `docker run --rm -v "$(pwd):/mnt" koalaman/shellcheck:stable /mnt/docker/opencode-agent/entrypoint.sh`
Expected: no new warnings versus `git stash`-ed baseline. `docker.yml` shell-checks entrypoints in CI, so a new warning fails the build.

- [ ] **Step 6: Rebuild the agent image**

The agent images are standalone and NOT in compose. A stale image runs the old entrypoint silently.

```bash
docker build -t opencode-agent:latest docker/opencode-agent/
```

- [ ] **Step 7: Confirm the rebuilt image carries the new entrypoint**

Staleness is judged by CONTENT via the `org.praxis.entrypoint-sha256` label, never mtime.

```bash
docker inspect opencode-agent:latest --format '{{ index .Config.Labels "org.praxis.entrypoint-sha256" }}'
sha256sum docker/opencode-agent/entrypoint.sh
```

Expected: the two hashes match. If the Dockerfile does not compute this label, check how `core/entrypoint_hash.py` derives it and match that exactly.

- [ ] **Step 8: Commit**

```bash
git add docker/opencode-agent/entrypoint.sh tests/shell/test_opencode_effort.sh
git commit -m "feat: state reasoning_effort explicitly in the OpenCode provider config"
```

---

### Task 6: Token telemetry column

**Files:**
- Modify: `src/orchestrator/database.py:266-291` (`MIGRATIONS`), plus a new migration function beside `_migration_0007_leaf_triage`
- Test: `tests/test_harness_parity.py` (append)

**Depends on:** None

- [ ] **Step 1: Write the failing test**

Append to `tests/test_harness_parity.py`:

```python
@pytest.mark.integration
async def test_agent_runs_has_token_telemetry_columns(db) -> None:
    row = await db.fetch_one("SELECT * FROM pragma_table_info('agent_runs')")
    assert row is not None
    cols = await db.fetch_all("SELECT name FROM pragma_table_info('agent_runs')")
    names = {c["name"] for c in cols}
    assert "tokens_used" in names
    assert "tokens_source" in names
```

Use the existing in-memory database fixture from `tests/conftest.py`. Check its exact name first:

Run: `grep -n "def db\|def database" tests/conftest.py`

Adjust the fixture argument to match what conftest actually provides.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_harness_parity.py -k token_telemetry -v`
Expected: FAIL — `tokens_used` not in the column set

- [ ] **Step 3: Write minimal implementation**

Add this function in `src/orchestrator/database.py`, next to the other `_migration_*` functions:

```python
async def _migration_0008_run_tokens(connection: aiosqlite.Connection) -> None:
    """Add token telemetry to agent_runs so harnesses are comparable.

    ``tokens_source`` distinguishes "the harness reported 0" from "this harness
    cannot report", which is the whole point: an unexplained NULL is the same
    invisible gap the column exists to close.
    """
    await connection.execute("ALTER TABLE agent_runs ADD COLUMN tokens_used INTEGER")
    await connection.execute("ALTER TABLE agent_runs ADD COLUMN tokens_source TEXT")
```

Add the entry to the end of the `MIGRATIONS` list:

```python
    Migration(
        8,
        "add agent_runs token telemetry (tokens_used, tokens_source)",
        _migration_0008_run_tokens,
    ),
```

Note: `ALTER TABLE ... ADD COLUMN` is not re-run safe if the column exists. Match the idempotency style used by the existing migrations — read `_migration_0002_pending_input` and copy its guard:

Run: `grep -n "_migration_0002_pending_input" -A 15 src/orchestrator/database.py`

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_harness_parity.py -v`
Expected: PASS

- [ ] **Step 5: Verify the migration is re-run safe**

Run: `uv run pytest tests/ -k "migration or database" -v`
Expected: PASS. Migrations must be idempotent; a second `initialize()` on the same DB must not raise `duplicate column name`.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/database.py tests/test_harness_parity.py
git commit -m "feat: add agent_runs token telemetry columns"
```

---

### Task 7: Callback accepts and records tokens

**Files:**
- Modify: `src/orchestrator/api/internal.py:22-30` (`AgentDonePayload`), `:61-120` (handler)
- Modify: `docker/agy-agent/entrypoint.sh:86-120` (callback payload)
- Test: `tests/test_harness_parity.py` (append)

**Depends on:** Task 6

- [ ] **Step 1: Write the failing test**

Append to `tests/test_harness_parity.py`:

```python
@pytest.mark.integration
async def test_callback_records_reported_tokens(client, auth_headers, seeded_task) -> None:
    resp = client.post(
        "/internal/agent-done",
        json={
            "task_id": seeded_task["id"],
            "run_id": seeded_task["run_id"],
            "status": "completed",
            "pr_url": "https://github.com/o/r/pull/1",
            "tokens_used": 12345,
        },
        headers={"X-Praxis-Callback-Token": "test-secret"},
    )
    assert resp.status_code == 200


@pytest.mark.integration
async def test_callback_without_tokens_is_still_accepted(client, seeded_task) -> None:
    # OpenCode cannot report tokens. That must not fail the run.
    resp = client.post(
        "/internal/agent-done",
        json={
            "task_id": seeded_task["id"],
            "run_id": seeded_task["run_id"],
            "status": "completed",
            "pr_url": "https://github.com/o/r/pull/1",
        },
        headers={"X-Praxis-Callback-Token": "test-secret"},
    )
    assert resp.status_code == 200
```

Match the fixture names and the callback secret to what `tests/conftest.py` provides. Check first:

Run: `grep -n "internal_callback_secret\|def client\|def seeded_task" tests/conftest.py`

If no `seeded_task` fixture exists, look at how an existing internal-callback test seeds its task and reuse that pattern:

Run: `grep -rn "agent-done" tests/ | head -5`

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_harness_parity.py -k callback -v`
Expected: FAIL — a 422, because `tokens_used` is not a declared field and the model rejects it, or a silent pass with the value discarded. Either way the value is not persisted.

- [ ] **Step 3: Write minimal implementation**

In `src/orchestrator/api/internal.py`, add to `AgentDonePayload`:

```python
    tokens_used: int | None = None
    tokens_source: str | None = None
```

In the `agent_done` handler, after the run is resolved (near the existing `runs = await queue.get_runs_for_task(...)` logic), persist the counts:

```python
    # Token telemetry is optional by design: harnesses declare whether they can
    # report it (core/harnesses.py reports_tokens). Recording the source makes
    # "not reported" distinguishable from "reported zero".
    if body.run_id is not None:
        source = body.tokens_source or (
            "harness" if body.tokens_used is not None else "unavailable"
        )
        await queue.record_run_tokens(body.run_id, body.tokens_used, source)
```

Add `record_run_tokens` to the task queue. Read the neighbouring methods first so the connection handling matches:

Run: `grep -n "async def record_worker_session" -A 15 src/orchestrator/core/task_queue.py`

Then add:

```python
    async def record_run_tokens(
        self, run_id: str, tokens_used: int | None, source: str
    ) -> None:
        """Persist token telemetry for an agent run.

        Args:
            run_id: The agent_runs row to update.
            tokens_used: Total tokens reported by the harness, or None when the
                harness cannot report them.
            source: "harness" or "unavailable".
        """
        await self.db.execute(
            "UPDATE agent_runs SET tokens_used = ?, tokens_source = ? WHERE id = ?",
            (tokens_used, source, run_id),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_harness_parity.py -v`
Expected: PASS

- [ ] **Step 5: Emit tokens from the agy entrypoint**

In `docker/agy-agent/entrypoint.sh`, the callback payload is assembled around line 106. Add a token field alongside the existing `pr_json` / `run_json` / `session_json` variables, using the same `json_escape` discipline:

```bash
    local tokens_json="null"
    if [ -n "${TOKENS_USED:-}" ]; then
        # Numeric, so emit unquoted; guard against a non-numeric capture.
        case "${TOKENS_USED}" in
            ''|*[!0-9]*) tokens_json="null" ;;
            *) tokens_json="${TOKENS_USED}" ;;
        esac
    fi
```

Then add `,\"tokens_used\":${tokens_json}` to the `payload` string on the existing line, keeping the surrounding escaping exactly as it is.

Set `TOKENS_USED` where agy's output is captured, parsing its JSON envelope. The envelope shape was verified live on 2026-08-14; confirm the current field path before writing the parse, and if agy's output does not carry it, leave `TOKENS_USED` unset — `null` is the correct, honest answer and the harness declaration in Task 1 must then be corrected to `reports_tokens=False`.

- [ ] **Step 6: Execute the entrypoint's payload builder**

Do not settle for `bash -n`. Extract the callback function into a scratch script, source it with representative values, and assert the emitted payload parses:

```bash
bash -c 'source <(sed -n "/^json_escape/,/^}/p" docker/agy-agent/entrypoint.sh); \
  printf "%s" "hello" | json_escape' | python3 -c 'import json,sys; json.loads(sys.stdin.read()); print("OK")'
```

Expected: `OK`

- [ ] **Step 7: Rebuild the agy image and verify the label**

```bash
docker build -t agy-agent:latest docker/agy-agent/
docker inspect agy-agent:latest --format '{{ index .Config.Labels "org.praxis.entrypoint-sha256" }}'
sha256sum docker/agy-agent/entrypoint.sh
```

Expected: hashes match.

- [ ] **Step 8: Commit**

```bash
git add src/orchestrator/api/internal.py src/orchestrator/core/task_queue.py docker/agy-agent/entrypoint.sh tests/test_harness_parity.py
git commit -m "feat: record per-run token telemetry from the agent callback"
```

---

### Task 8: Stop `execute_plan` overwriting a project's harness

**Files:**
- Modify: `src/orchestrator/api/execute_plan.py:49-88`, `:174-177`
- Test: `tests/test_harness_parity.py` (append)

**Depends on:** Task 1

- [ ] **Step 1: Write the failing test**

Append to `tests/test_harness_parity.py`:

```python
@pytest.mark.integration
async def test_execute_plan_keeps_the_projects_configured_harness(db) -> None:
    from orchestrator.api.execute_plan import _create_or_reuse_project

    await db.execute(
        """INSERT INTO projects
           (id, user_id, name, repo_url, default_branch, approval_gate,
            model_name, harness)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("p1", "u1", "r", "https://github.com/o/r", "main", False, "m", "agy"),
    )

    await _create_or_reuse_project(
        db, "https://github.com/o/r", None, "m", harness=None
    )

    row = await db.fetch_one("SELECT harness FROM projects WHERE id = 'p1'")
    assert row["harness"] == "agy", "an omitted harness must not downgrade the project"


@pytest.mark.integration
async def test_execute_plan_explicit_harness_still_wins(db) -> None:
    from orchestrator.api.execute_plan import _create_or_reuse_project

    await db.execute(
        """INSERT INTO projects
           (id, user_id, name, repo_url, default_branch, approval_gate,
            model_name, harness)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("p2", "u1", "r", "https://github.com/o/r2", "main", False, "m", "agy"),
    )

    await _create_or_reuse_project(
        db, "https://github.com/o/r2", None, "m", harness="opencode"
    )

    row = await db.fetch_one("SELECT harness FROM projects WHERE id = 'p2'")
    assert row["harness"] == "opencode"
```

Both tests need a seeded user row, because `_create_or_reuse_project` raises 500 when `SELECT id FROM users LIMIT 1` is empty. Seed one first, matching how other tests in the suite do it:

Run: `grep -rn "INSERT INTO users" tests/ | head -3`

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_harness_parity.py -k configured_harness -v`
Expected: FAIL — `assert 'opencode' == 'agy'`, the exact silent downgrade

- [ ] **Step 3: Write minimal implementation**

Change the signature at `src/orchestrator/api/execute_plan.py:49-51`:

```python
async def _create_or_reuse_project(
    db: Any, repo_url: str, name: str | None, model: str, harness: str | None
) -> str:
    """Return an existing project id for the repo, or create one. Mirrors dispatch.

    A None ``harness`` means "the caller expressed no preference". For an
    existing project that preserves whatever it was configured with; only a new
    project falls back to the registry default. Defaulting eagerly used to
    re-point an agy project at opencode on every plan submitted without the
    field, which made "which harness ran this" unanswerable.
    """
```

Replace the reuse branch (lines 63-69):

```python
    if project is not None:
        project_id = project["id"]
        effective_harness = harness or project["harness"] or default_harness_id()
        await db.execute(
            "UPDATE projects SET model_name = ?, harness = ? WHERE id = ?",
            (model, effective_harness, project_id),
        )
        return str(project_id)
```

Replace the insert's harness value (line 85) with:

```python
            harness or default_harness_id(),
```

Then at line 174, stop pre-defaulting:

```python
    harness = body.harness
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_harness_parity.py -v`
Expected: PASS

- [ ] **Step 5: Check no other caller relied on the eager default**

Run: `grep -rn "_create_or_reuse_project" src/ tests/`
Expected: only `execute_plan.py` and the new tests. If `api/dispatch.py` has a parallel copy of this function, apply the identical fix there — the docstring claims it "mirrors dispatch", so verify:

Run: `grep -n "harness" src/orchestrator/api/dispatch.py | head -20`

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/api/execute_plan.py tests/test_harness_parity.py
git commit -m "fix: execute_plan no longer overwrites a project's configured harness"
```

---

### Task 9: Document the contract

**Files:**
- Modify: `docs/gotchas.md`
- Modify: `CLAUDE.md` (the Gotchas shortlist)
- Modify: `docs/architecture.md`

**Depends on:** Task 5, Task 7, Task 8

- [ ] **Step 1: Add the gotcha narratives**

Append two entries to `docs/gotchas.md`, matching the file's existing entry format (read the last entry first to copy the heading style):

```markdown
### Worker thinking effort must be stated, per harness, from the declared channel

`core/thinking.py` covers brain payloads: an absent `reasoning_effort` means
MAXIMUM effort on qwen3.8, not off. Workers had the same hole and it was worse,
because it differed by harness. OpenCode's generated provider config carried no
effort at all, while agy takes its effort baked into the Gemini model string
(`"Gemini 3.5 Flash (High)"`). The same task therefore ran under two different
and undeclared thinking regimes depending on which harness picked it up, with
no error and no failing test.

`core/harnesses.py` now declares an `effort_channel` per harness and
`core/worker_effort.py` resolves exactly one value from it. `None` is a real
answer meaning "this harness has no knob", and is not the same as "off" - do
not collapse the two.

### An omitted `harness` used to re-point an existing project at OpenCode

`POST /api/execute-plan` computed `body.harness or default_harness_id()` and
then `UPDATE projects SET harness = ?` unconditionally. Submitting a plan
without the field silently downgraded an agy project to opencode, which made
"which harness ran this task" unanswerable after the fact. The parameter is now
`str | None` all the way down: `None` preserves the project's configured
harness and only a NEW project falls back to the registry default.
```

- [ ] **Step 2: Add the one-line index entries to CLAUDE.md**

Under **The loop** in the Gotchas shortlist, add:

```markdown
- **Worker effort is per-harness and must be stated**: `core/harnesses.py` declares each
  harness's `effort_channel`; `core/worker_effort.py` resolves it. An absent
  `reasoning_effort` means MAXIMUM, and `None` means "no knob", not "off".
- **An omitted `harness` never downgrades a project**: `execute_plan` passes `None`
  through so an existing project keeps its configured harness.
```

- [ ] **Step 3: Document the contract in architecture.md**

Find the harness section and add a table of the declared contract:

Run: `grep -n "harness" docs/architecture.md | head -10`

Add beneath it:

```markdown
Every harness declares how praxis drives it, so delegation is predictable
regardless of which one runs a task:

| Harness | Effort channel | Reports tokens |
|---------|----------------|----------------|
| OpenCode | `request_option` (LM Studio provider `options.reasoning_effort`) | No |
| agy | `model_name` (effort is inside the Gemini model string) | Yes |

Adding a harness means answering both columns. `core/worker_effort.py` reads the
first; the `/internal/agent-done` callback records the second as
`agent_runs.tokens_source`, so "not reported" is distinguishable from "zero".
```

- [ ] **Step 4: Verify docs stay in sync with the code**

Run: `uv run pytest tests/ -k "doc or config_path" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add docs/gotchas.md docs/architecture.md CLAUDE.md
git commit -m "docs: document the per-harness driving contract"
```

---

### Task 10: Full verification

**Files:** none (verification only)

**Depends on:** Task 9

- [ ] **Step 1: Run the full suite with coverage**

Run: `uv run pytest --cov=orchestrator --cov-report=term-missing`
Expected: PASS, coverage at or above 80 (CI enforces `--cov-fail-under=80`)

- [ ] **Step 2: Lint and format**

```bash
uv run ruff format src/ tests/
uv run ruff check --fix src/ tests/
```

Expected: no remaining errors. Note it is `ruff format`, never `ruff fmt`.

- [ ] **Step 3: Type check**

Run: `uv run mypy src/orchestrator/ --ignore-missing-imports`
Expected: no new errors

- [ ] **Step 4: Confirm the doctor is still green**

Run: `uv run praxis doctor`
Expected: exit 0, all twelve checks green. If the agent-image checks are red, the rebuilds from Tasks 5 and 7 did not take.

- [ ] **Step 5: Live end-to-end on one task per harness**

This is the step that catches an inert seam. Unit-green wiring has shipped dead before.

Dispatch one trivial task to an OpenCode worker and one to an agy worker, then confirm both halves of the contract landed:

```bash
docker logs <opencode-container> 2>&1 | grep "Using reasoning effort"
```

Expected: `Using reasoning effort: none` (or whatever `worker_reasoning_effort` is set to)

```bash
sqlite3 data/orchestrator.db \
  "SELECT id, tokens_used, tokens_source FROM agent_runs ORDER BY started_at DESC LIMIT 2;"
```

Expected: the agy run has a numeric `tokens_used` with `tokens_source='harness'`; the OpenCode run has `NULL` with `tokens_source='unavailable'`. A row with `tokens_source` NULL means the callback branch never ran.

- [ ] **Step 6: Commit any fixes found by the live run**

```bash
git add -A
git commit -m "fix: live-run corrections to the harness parity contract"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1 (no dependencies), Task 6 (no dependencies) — run in parallel
- **Wave 2:** Task 2 (depends on Task 1), Task 7 (depends on Task 6), Task 8 (depends on Task 1)
- **Wave 3:** Task 3 (depends on Task 2), Task 4 (depends on Task 2)
- **Wave 4:** Task 5 (depends on Task 4)
- **Wave 5:** Task 9 (depends on Task 5, Task 7, Task 8)
- **Wave 6:** Task 10 (depends on Task 9)

Note on Wave 3: Tasks 3 and 4 both touch different files (`config.py` vs `agent_manager.py`) but Task 4 Step 5 consumes the setting Task 3 adds. If run truly concurrently in one working tree, run Task 3 first — concurrent agents in a non-isolated tree clobber each other's uncommitted work. Use a worktree per agent or serialize this wave.

---

## Notes

**What this plan deliberately does not do:**

- It does not make OpenCode report tokens. OpenCode does not expose them; the plan makes that absence explicit and queryable via `tokens_source` instead of inventing a number.
- It does not add a third harness. The declarative fields exist so that adding one forces the two questions to be answered, but adding one is separate work.
- It does not touch the brain-side routing in `core/llm_router.py`. `CALL_SITE_DEFAULTS` already governs brain effort; this plan is strictly the worker half.

**Rebuild discipline:** Tasks 5 and 7 change entrypoints. The agent images are standalone and NOT in compose, so a missed `docker build` means a stale image runs the old entrypoint with no error. Staleness is judged by the `org.praxis.entrypoint-sha256` label, never mtime.
