---
type: plan
spec_path: docs/superpowers/specs/2026-08-06-usable-praxis-spec.md
---

# Usable Praxis Product Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take a fresh machine from `git clone` to a first delegated, reviewed pull request in 15 minutes or less, and make the docs, framing, and launch match what the product actually is.

**Architecture:** Phase A removes every setup step that is currently manual or undocumented. Compose learns to build the agent images (they are standalone today, which is the single most common stale-image failure), `config/praxis.yaml` becomes a read-only bind mount instead of a baked image layer (retiring the 2026-07-27 gotcha), and two new Typer commands do the rest: `praxis init` writes `.env`, builds, starts, waits for health, and prints the `claude mcp add` snippet; `praxis doctor` is one read-only table of green and red checks with a fix hint per red, and becomes the first line of every troubleshooting doc entry. Worker presets turn a three-field configuration into one name. A merge-gate digest surfaces parked work on the surfaces users already poll, because the documented death of this product category is a review queue nobody sees. Phase B halves the user-facing docs corpus and executes the launch checklist.

**Tech Stack:** Typer + rich (existing CLI), Docker Compose, FastAPI, the existing MCP stdio server, no-build HTML/CSS/JS dashboard.

**Spec:** `docs/superpowers/specs/2026-08-06-usable-praxis-spec.md` (workstreams C and D: sections 4.1 through 5.2; plan rows P7, P8).

---

## Execution order across the three Usable-Praxis plans

Full order is documented in `2026-08-06-decomposition-standard-v2.md`. This plan's
place in it:

- **Phase A** (P7) runs **SECOND**, immediately after the engine plan's Phase A
  and before everything else. It has no dependency on any other phase; it is
  deliberately early because every subsequent working session and every dogfood
  run benefits from `praxis doctor` and a config mount that does not need an
  image rebuild.
- **Phase B** (P8) runs **LAST**, after the benchmark's full report exists,
  because the launch checklist is gated on that report.

---

## Dispatch guide: agent type per task

Read this before dispatching anything.

Model AND reasoning effort both come from the agent DEFINITION, not from the
Agent tool call. The tool call accepts `subagent_type`, `model`, `isolation`,
`run_in_background`, `description`, and `prompt`; it has no `effort` parameter.
The `effort` frontmatter field in `.claude/agents/*.md` **overrides the session
effort level** for the duration of that subagent (options `low`, `medium`,
`high`, `xhigh`, `max`; the available set depends on the model). So the clean
way to run this plan is to define the agent types below once, then dispatch each
task by `subagent_type` and change nothing else.

Set the ORCHESTRATING session to `high` (or `xhigh` for the phases marked
below). That governs your own planning, the between-task review, and the diff
audit; each subagent overrides it with its own effort.

Higher effort usually costs LESS in total here, not more: every task is a test,
run, implement, run, mutate, run, commit loop, so turn count dominates and
under-setting effort adds turns.

### Agent types to define once

Create these in `.claude/agents/` before starting. They are reused by all three
Usable Praxis plans.

```yaml
# .claude/agents/praxis-impl-light.md
---
name: praxis-impl-light
description: Mechanical Praxis plan tasks - doc index edits, single registry entries, small additive models.
model: haiku
effort: medium
---
Execute exactly one task from a Praxis implementation plan, following its steps
verbatim. Write the failing test first and confirm it fails for the stated
reason before implementing. Run every mutation check the task specifies. Do not
touch a file the task does not name.
```

```yaml
# .claude/agents/praxis-impl-standard.md
---
name: praxis-impl-standard
description: Standard Praxis plan implementation tasks - well-specified modules, validators, endpoints, and their tests.
model: sonnet
effort: high
---
Execute exactly one task from a Praxis implementation plan, following its steps
verbatim. Write the failing test first and confirm it fails for the stated
reason before implementing. Run every mutation check the task specifies; if a
mutation check does not fail, the test is vacuous - fix the test and redo the
check. Do not touch a file the task does not name. Report every file you
changed.
```

```yaml
# .claude/agents/praxis-impl-critical.md
---
name: praxis-impl-critical
description: High-risk Praxis plan tasks - load-bearing invariants, the review path, architecture seams, shell entrypoints.
model: opus
effort: xhigh
---
Execute exactly one task from a Praxis implementation plan. This task was marked
critical because a subtle error in it fails silently rather than loudly.

Before implementing, state in one paragraph what the load-bearing invariant is
and how the task's tests pin it. Write the failing test first and confirm it
fails for the stated reason. Run every mutation check the task specifies and
report its output; a mutation check that does not fail means the test is
vacuous, so fix the test and redo the check. Do not touch a file the task does
not name. Report every file you changed and every assumption you made.
```

```yaml
# .claude/agents/praxis-impl-max.md
---
name: praxis-impl-max
description: The single highest-stakes Praxis plan task, where an undetected error invalidates downstream work.
model: opus
effort: max
---
Execute exactly one task from a Praxis implementation plan. Correctness here
matters more than cost: an undetected error invalidates every result that
depends on this task, and the failure is not visible until much later.

Before implementing, state what would have to be true for this task's output to
be silently wrong, and how the task's tests would catch it. Write the failing
test first. Run every mutation check and report its full output. Verify the
result independently of the tests where the task tells you how. Do not touch a
file the task does not name.
```

```yaml
# .claude/agents/praxis-review-first.md
---
name: praxis-review-first
description: First-pass review of a completed Praxis plan task against the plan text.
model: sonnet
effort: high
tools: Read, Grep, Glob, Bash
---
Review one completed task against its plan text. Check: every step was done,
the tests match what the plan specified, the mutation checks actually ran, no
file outside the task's declared list was touched, and no em dash was
introduced. Report findings only; do not fix anything.
```

```yaml
# .claude/agents/praxis-review-adversarial.md
---
name: praxis-review-adversarial
description: Adversarial second-pass review of a critical Praxis plan task.
model: opus
effort: xhigh
tools: Read, Grep, Glob, Bash
---
Try to break this task's implementation. Assume the tests are wrong until you
have checked them. For each test, ask what mutation would leave it passing, and
say so if one exists. Check the load-bearing invariant the task names is
actually enforced, not merely asserted. Report findings only; do not fix
anything.
```

```yaml
# .claude/agents/praxis-review-recheck.md
---
name: praxis-review-recheck
description: Cheap re-review of a Praxis plan task after review fixes were applied.
model: haiku
effort: medium
tools: Read, Grep, Glob, Bash
---
Confirm that each previously reported finding was addressed and that nothing
else changed. Report findings only; do not fix anything.
```

If a model rejects an effort level (the available set depends on the model),
drop the `effort` line from that definition and let it inherit the session.

**Review pairing:** `praxis-review-first` after every task; then
`praxis-review-adversarial` for any task dispatched to `praxis-impl-critical` or
`praxis-impl-max`, and `praxis-review-recheck` for everything else.
`praxis-review-recheck` also handles every re-review after fixes.

**Never take a subagent's report at face value.** Diff every file it touched
before accepting the task. Subagents on this repo have historically done
unrequested work and under-reported it.

### Session baseline

| Phase | Tasks | Orchestrating-session effort |
|-------|-------|------------------------------|
| A | 1 to 9 | `high` |
| B | 10 to 13 | `high` |

### Per task

| Task | `subagent_type` | Effective model / effort | Why this tier |
|------|-----------------|--------------------------|---------------|
| 1 Compose agents profile | `praxis-impl-standard` | sonnet / high | Compose entries plus a registry-pinned test |
| 2 Mount the config | `praxis-impl-critical` | opus / xhigh | Touches startup settings resolution and retires a gotcha found live in production. The grep guard and the live restart proof are the load-bearing parts |
| 3 Worker presets | `praxis-impl-standard` | sonnet / high | Parsing plus an endpoint plus a dropdown |
| 4 Doctor check registry | `praxis-impl-standard` | sonnet / high | Pure registry with clear contracts |
| 5 Doctor probes and surfaces | `praxis-impl-standard` | sonnet / high | Eleven small pure functions plus two thin surfaces |
| 6 `praxis init` | `praxis-impl-critical` | opus / xhigh | Writes `.env` and runs Docker. The non-destructive merge is what stops a re-run eating an operator's other settings |
| 7 Approvals digest | `praxis-impl-standard` | sonnet / high | Query, rate limiter, and four surfaces |
| 8 MCP surface | `praxis-impl-standard` | sonnet / high | Bounded log tail plus a leading summary key. The tail direction and the truncation notice are the parts with a real failure mode |
| 9 Timed walkthrough | **You, not an agent** | n/a | The number only means something if a human runs the clock on a clean machine |
| 10 Docs restructure | `praxis-impl-critical` | opus / xhigh | A large what-do-I-cut judgment against hard budgets. Low blast radius if wrong, but expensive to redo |
| 11 Framing | `praxis-impl-standard` | sonnet / high | Text edits pinned by a ratchet test |
| 12 Demo artifact | **You, not an agent** | n/a | Recording a real session |
| 13 Launch checklist | **You, not an agent** | n/a | Four gates, each a judgment call |

---

## Standing constraints (read before Task 1)

- **Agent images are standalone and NOT in compose today.** Task 1 adds them
  under a `agents` profile as build-only services. That does not change the
  rebuild requirement for entrypoint edits; it only means one documented command
  builds them.
- **`config/praxis.yaml` is baked into the orchestrator image today.** The dev
  compose file mounts `src/`, `web/`, `.git/`, and `data/` but NOT `config/`, so
  a YAML edit currently needs an image rebuild. Task 2 fixes this. Until Task 2
  lands, any YAML change in this repo needs `up --build`.
- **The CLI entry points are `praxis` and `orchestrator-cli`**, both bound to
  `cli.main:app` in `pyproject.toml`. New commands go in `src/cli/main.py`.
- **Auth is a single static token** (`AUTH_TOKEN`), stored raw in
  `users.token_hash` (a legacy name, deliberately not renamed).
- **`Settings.callback_url()` derives the agent callback from `PORT`.** A wrong
  port makes every agent callback 404, so `praxis doctor` checks it explicitly.
- **The dashboard is no-build**: plain HTML, CSS, and a classic script. Do not
  introduce a bundler.
- **No em dashes** in any prose, doc, code comment, or commit message.

### Test-harness facts (verified against the repo on 2026-08-06)

Do not re-derive these; they were checked while this plan was written and every
test in it assumes them.

| Fact | Detail |
|------|--------|
| Database fixture | **`db`** (not `test_db`), an async fixture yielding an initialized `Database` |
| API client fixture | **`client` is an httpx `AsyncClient`**, not a sync `TestClient`. Every API test is `async def` and every call is awaited |
| Auth fixture | `auth_headers` returns `{"Authorization": "Bearer test-auth"}` |
| Event bus | **`EventBus` has no callback API.** It is `subscribe() -> asyncio.Queue`, `unsubscribe(queue)`, `publish(event)`. Anything collecting events drains a queue |
| Foreign keys | `PRAGMA foreign_keys=ON`. A `projects` row needs its `users` row inserted first |
| Plan creation | `TaskQueue.create_plan(project_id, summary=None, source="user", ...) -> plan_id` |
| Agent run completion | `TaskQueue.complete_agent_run(run_id, status, logs)`. There is no `finish_agent_run` |
| Dead branches | `branch_sweeper.dead_branches(branches, *, open_pr_branches, terminal_failed, merged_plan)`, all keyword-only |
| Dispatch contract | `DispatchRequest` is keyed on **`repo_url`** plus `instructions`, `model`, `harness`, `branch`. There is no `project_id` field; the endpoint reuses an existing project matching `repo_url` |
| Execute-plan contract | `ExecutePlanRequest` is keyed on **`repo_url`** plus `plan`, `model`, `harness`, `branch` |
| Async mode | `asyncio_mode = "auto"`, so async tests need no decorator |
| Markers | `unit`, `integration`, `slow` are registered in `pyproject.toml` |


---

## File Structure

### Phase A (P7)

| File | Responsibility |
|------|----------------|
| Modify `docker-compose.yml` | `agents` profile with build-only entries; `config/` read-only mount |
| Modify `docker-compose.local.yml` | Same config mount for dev |
| Modify `src/orchestrator/core/settings_file.py` | Resolve the config path from an env var, defaulting to the mount |
| Modify `src/orchestrator/config.py` | `config_path` setting |
| Modify `config/praxis.yaml` | `worker_presets`, `approvals_digest` |
| Create `src/orchestrator/core/worker_presets.py` | Preset resolution |
| Create `src/orchestrator/api/presets.py` | `GET /api/settings/presets` |
| Create `src/orchestrator/core/doctor.py` | The check registry and its runner (engine-side, CLI-agnostic) |
| Create `src/orchestrator/api/doctor.py` | `GET /api/doctor` |
| Create `src/cli/doctor.py` | `praxis doctor` rendering |
| Create `src/cli/init.py` | `praxis init` flow |
| Modify `src/cli/main.py` | Register both commands |
| Create `src/orchestrator/core/approvals.py` | Parked-work digest query |
| Create `src/orchestrator/api/approvals.py` | `GET /api/approvals/pending` |
| Modify `src/mcp_server/server.py` | `pending_approvals` tool; digest line on `poll_task` and `poll_plan`; then (Task 8) the `get_task_logs` tail, the leading `summary` key on every state-returning tool, and the server `instructions` |
| Modify `src/orchestrator/core/orchestrator.py` | Publish `approvals_digest` on the loop |
| Modify `web/app.js`, `web/styles.css` | Persistent approvals badge |

### Phase B (P8)

| File | Responsibility |
|------|----------------|
| Rewrite `README.md` | 120 lines: one-liner, one diagram, 15-minute quickstart, one real transcript, links |
| Create `docs/getting-started.md` | The 15-minute path plus the optional roads |
| Create `docs/reference.md` | Config keys, API, MCP tools, deployment modes, troubleshooting |
| Create `docs/internal/` | `positioning.md`, `social-launch-drafts.md`, `workflow-diagram.md` move here |
| Create stub files at old paths | One-release pointers for deep links |
| Create `docs/demo.md` | The 90-second demo transcript |
| Modify `CLAUDE.md` | Documentation index reflects the new shape |

---

## Phase A: setup, doctor, presets, the approvals digest, and the MCP surface

### Task 1: Compose builds the agent images

**Files:**
- Modify: `docker-compose.yml`
- Test: `tests/test_compose_agents_profile.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

Create `tests/test_compose_agents_profile.py`:

```python
"""Compose must know how to build every image AgentManager can spawn.

The agent images being standalone is the single most common stale-image
failure in this project. One documented build command fixes that.
"""

from pathlib import Path

import pytest
import yaml


REPO = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))


def _service(name: str) -> dict:
    return COMPOSE["services"][name]


@pytest.mark.unit
@pytest.mark.parametrize("name", ["opencode-agent", "agy-agent"])
def test_the_agent_image_has_a_compose_service(name):
    assert name in COMPOSE["services"]


@pytest.mark.unit
@pytest.mark.parametrize("name", ["opencode-agent", "agy-agent"])
def test_the_agent_service_is_behind_the_agents_profile(name):
    assert _service(name)["profiles"] == ["agents"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "name,tag",
    [("opencode-agent", "opencode-agent:latest"), ("agy-agent", "agy-agent:latest")],
)
def test_the_image_tag_matches_what_the_registry_spawns(name, tag):
    """A tag mismatch means compose builds an image nothing ever runs."""
    from orchestrator.core.harnesses import REGISTRY

    assert _service(name)["image"] == tag
    assert any(spec.image == tag for spec in REGISTRY.values())


@pytest.mark.unit
@pytest.mark.parametrize("name", ["opencode-agent", "agy-agent"])
def test_the_agent_service_builds_from_its_own_directory(name):
    build = _service(name)["build"]
    assert build["context"] == f"docker/{name}"
    assert build["dockerfile"] == "Dockerfile"


@pytest.mark.unit
@pytest.mark.parametrize("name", ["opencode-agent", "agy-agent"])
def test_the_agent_service_never_starts_on_a_plain_up(name):
    """These are build targets, not long-running services."""
    service = _service(name)
    assert service.get("command") in (["true"], "true")
    assert service.get("restart", "no") == "no"


@pytest.mark.unit
def test_the_orchestrator_is_not_in_the_agents_profile():
    """`docker compose up -d` must still start the orchestrator."""
    assert "profiles" not in _service("orchestrator")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_compose_agents_profile.py -v`
Expected: FAIL with `KeyError: 'opencode-agent'`.

- [ ] **Step 3: Read the harness registry**

Run: `uv run python -c "from orchestrator.core.harnesses import REGISTRY; print({k: v.image for k, v in REGISTRY.items()})"`

Use the printed tags verbatim in the compose entries. If they differ from
`opencode-agent:latest` and `agy-agent:latest`, use the real ones and update the
test's parametrize list to match the registry rather than the other way round.

- [ ] **Step 4: Add the services**

In `docker-compose.yml`, add before the `caddy` service:

```yaml
  # Build-only entries so ONE documented command builds every image the
  # orchestrator can spawn: `docker compose --profile agents build`. These are
  # not long-running services; the `agents` profile keeps them out of a plain
  # `docker compose up`. Note that an entrypoint.sh change still requires a
  # rebuild; `praxis doctor` detects a stale image and says so.
  opencode-agent:
    build:
      context: docker/opencode-agent
      dockerfile: Dockerfile
    image: opencode-agent:latest
    command: ["true"]
    profiles:
      - agents

  agy-agent:
    build:
      context: docker/agy-agent
      dockerfile: Dockerfile
    image: agy-agent:latest
    command: ["true"]
    profiles:
      - agents
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_compose_agents_profile.py -v`
Expected: PASS (13 parametrized tests).

- [ ] **Step 6: Verify the build path actually works**

```bash
docker compose --profile agents build
docker image inspect opencode-agent:latest --format '{{.Id}}'
docker image inspect agy-agent:latest --format '{{.Id}}'
```

Expected: both print an image id. Then confirm a plain `up` does not try to
start them:

```bash
docker compose config --services
docker compose --profile agents config --services
```

Expected: the first listing has no `opencode-agent`; the second does.

- [ ] **Step 7: Update the CI docker workflow**

`.github/workflows/docker.yml` builds the agent images from their own dirs in a
matrix. That still works and is still the right shape (it builds without
compose). Add a step that validates the compose file parses with the new
profile, so a typo cannot ship:

```yaml
      - name: Validate compose with the agents profile
        run: docker compose --profile agents config --quiet
```

- [ ] **Step 8: Commit**

```bash
git add docker-compose.yml .github/workflows/docker.yml tests/test_compose_agents_profile.py
git commit -m "feat(compose): build the agent images from an agents profile

One documented command builds every image AgentManager can spawn. Build-only
entries behind a profile, so a plain up is unchanged. A test pins the image
tags to the harness registry so compose can never build an unused tag."
```

---

### Task 2: Mount `config/praxis.yaml` instead of baking it

**Files:**
- Modify: `src/orchestrator/config.py`
- Modify: `src/orchestrator/core/settings_file.py`
- Modify: `src/orchestrator/core/effective_settings.py`
- Modify: `docker-compose.yml`
- Modify: `docker-compose.local.yml`
- Test: `tests/test_config_path.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

Create `tests/test_config_path.py`:

```python
"""The YAML path is resolvable, so a mount replaces an image rebuild.

Found live 2026-07-27: editing config/praxis.yaml had no effect until the
orchestrator image was rebuilt, because dev compose mounts src/, web/, .git/,
and data/ but not config/, and the YAML is baked in at build time.
"""

import pytest

from orchestrator.core.settings_file import config_file_path, load_yaml_settings


@pytest.mark.unit
def test_the_default_path_is_the_repo_relative_config(monkeypatch):
    monkeypatch.delenv("PRAXIS_CONFIG_PATH", raising=False)
    assert config_file_path().replace("\\", "/").endswith("config/praxis.yaml")


@pytest.mark.unit
def test_an_env_override_wins(monkeypatch, tmp_path):
    target = tmp_path / "elsewhere.yaml"
    monkeypatch.setenv("PRAXIS_CONFIG_PATH", str(target))
    assert config_file_path() == str(target)


@pytest.mark.unit
def test_settings_load_from_the_overridden_path(monkeypatch, tmp_path):
    target = tmp_path / "elsewhere.yaml"
    target.write_text("default_worker_model: from-the-mount\n", encoding="utf-8")
    monkeypatch.setenv("PRAXIS_CONFIG_PATH", str(target))
    assert load_yaml_settings(config_file_path())["default_worker_model"] == (
        "from-the-mount"
    )


@pytest.mark.unit
def test_a_missing_file_yields_empty_settings_not_a_crash(monkeypatch, tmp_path):
    """A fresh clone with no config file must still boot."""
    monkeypatch.setenv("PRAXIS_CONFIG_PATH", str(tmp_path / "absent.yaml"))
    assert load_yaml_settings(config_file_path()) == {}


@pytest.mark.unit
async def test_effective_settings_reads_the_overridden_path(db, monkeypatch, tmp_path):
    """No caller may hardcode 'config/praxis.yaml' any more."""
    target = tmp_path / "elsewhere.yaml"
    target.write_text("max_leaves_per_plan: 7\n", encoding="utf-8")
    monkeypatch.setenv("PRAXIS_CONFIG_PATH", str(target))

    from orchestrator.config import Settings
    from orchestrator.core.effective_settings import EffectiveSettings

    settings = EffectiveSettings(Settings(auth_token="t", _env_file=None), db)
    assert await settings.max_leaves_per_plan() == 7


@pytest.mark.unit
def test_no_module_hardcodes_the_config_path():
    """Grep guard: one resolver, no scattered literals."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src"
    offenders = [
        path
        for path in src.rglob("*.py")
        if path.name != "settings_file.py"
        and "config/praxis.yaml" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"hardcoded config path in: {offenders}"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_config_path.py -v`
Expected: FAIL with `ImportError: cannot import name 'config_file_path'`, and
`test_no_module_hardcodes_the_config_path` failing on
`core/effective_settings.py`.

- [ ] **Step 3: Add the resolver**

In `src/orchestrator/core/settings_file.py`, add above `load_yaml_settings`:

```python
# Default location of the global settings YAML, relative to the process CWD.
# In the container this is a read-only bind mount, so an operator edit takes
# effect on restart rather than needing an image rebuild.
_DEFAULT_CONFIG_PATH = "config/praxis.yaml"

_CONFIG_PATH_ENV = "PRAXIS_CONFIG_PATH"


def config_file_path() -> str:
    """Return the path to the global settings YAML.

    ``PRAXIS_CONFIG_PATH`` overrides the default so the container can point at
    its mount and a test can point at a temporary file.  This is the ONLY place
    the path is decided; a hardcoded literal anywhere else reintroduces the
    2026-07-27 bug where a YAML edit required an image rebuild.
    """
    return os.environ.get(_CONFIG_PATH_ENV) or _DEFAULT_CONFIG_PATH
```

- [ ] **Step 4: Route every caller through it**

In `src/orchestrator/core/effective_settings.py`, change `_get_yaml`:

```python
    async def _get_yaml(self) -> dict:
        """Return the raw YAML settings dict for capability/escalation lookups."""
        from orchestrator.core.settings_file import (
            config_file_path,
            load_yaml_settings,
        )

        return load_yaml_settings(config_file_path())
```

In `src/orchestrator/config.py`, the `Settings.__init__` YAML overlay likewise
calls `load_yaml_settings(config_file_path())`. Read the file and make the
substitution wherever the literal appears.

Run the grep guard to find any remaining literal:

```bash
grep -rn "config/praxis.yaml" src/
```

Expected after the edit: only `src/orchestrator/core/settings_file.py`.

- [ ] **Step 5: Mount the file in both compose files**

In `docker-compose.yml`, add to the orchestrator's `volumes`:

```yaml
      # Global settings, MOUNTED not baked: an operator edit takes effect on
      # `docker compose restart orchestrator`, never an image rebuild. This
      # retires the 2026-07-27 gotcha where a default_worker_* change silently
      # kept serving the baked-in value.
      - ./config:/app/config:ro
```

and to its `environment`:

```yaml
      - PRAXIS_CONFIG_PATH=/app/config/praxis.yaml
```

In `docker-compose.local.yml`, add the same volume line (the dev file's
`environment` block inherits `PRAXIS_CONFIG_PATH` from the base file through
compose merge, so it needs only the mount).

- [ ] **Step 6: Run the test to verify it passes**

Run: `uv run pytest tests/test_config_path.py -q`
Expected: PASS (6 tests).

- [ ] **Step 7: Prove the gotcha is actually retired**

This is the whole point of the task, so verify it live rather than trusting the
unit tests:

```bash
PRAXIS_BUILD_SHA=$(git rev-parse --short HEAD) docker compose -f docker-compose.yml -f docker-compose.local.yml up --build -d
curl -s -H "Authorization: Bearer $AUTH_TOKEN" http://localhost:12323/api/settings/registry | head -c 200

# Now edit the YAML WITHOUT rebuilding.
python - <<'PY'
from pathlib import Path
p = Path("config/praxis.yaml")
p.write_text(p.read_text(encoding="utf-8").replace(
    'default_worker_model: "Gemini 3.6 Flash (High)"',
    'default_worker_model: "MOUNT-PROOF"'), encoding="utf-8")
PY

docker compose restart orchestrator
sleep 5
curl -s -H "Authorization: Bearer $AUTH_TOKEN" http://localhost:12323/api/settings/auto-delegate
```

Expected: the response names `MOUNT-PROOF` after a RESTART, with no rebuild.
Then revert the YAML edit and restart again. If the old value persists, the
mount or the env var is not wired; fix it before continuing, because every
later task assumes config edits are cheap.

- [ ] **Step 8: Update the gotchas**

In `docs/gotchas.md`, REPLACE the "Dev compose does NOT mount `config/`" gotcha
with:

```markdown
- **`config/praxis.yaml` is MOUNTED, not baked**: both compose files bind-mount
  `./config` read-only at `/app/config` and set `PRAXIS_CONFIG_PATH` to point at
  it, so a YAML edit takes effect on `docker compose restart orchestrator` and
  never needs an image rebuild. This replaced the reverse behavior, which bit us
  live on 2026-07-27 when a `default_worker_*` change silently kept serving the
  baked-in value. `core/settings_file.config_file_path()` is the ONLY place the
  path is decided; a hardcoded `"config/praxis.yaml"` literal anywhere else
  reintroduces the bug, and `tests/test_config_path.py` greps for exactly that.
  Agent-image entrypoint changes still require a rebuild.
```

Update the matching CLAUDE.md index line in the same commit; the old line says
the opposite of the new truth, so leaving it is worse than having no line.

- [ ] **Step 9: Commit**

```bash
git add src/orchestrator/config.py src/orchestrator/core/settings_file.py src/orchestrator/core/effective_settings.py docker-compose.yml docker-compose.local.yml docs/gotchas.md CLAUDE.md tests/test_config_path.py
git commit -m "feat(config): mount praxis.yaml instead of baking it into the image

A YAML edit now takes effect on restart, not on rebuild. One resolver owns
the path and a grep test forbids a hardcoded literal anywhere else."
```

---

### Task 3: Worker presets

**Files:**
- Create: `src/orchestrator/core/worker_presets.py`
- Create: `src/orchestrator/api/presets.py`
- Modify: `config/praxis.yaml`
- Modify: `src/orchestrator/core/effective_settings.py`
- Modify: `src/orchestrator/main.py`
- Modify: `web/app.js`
- Test: `tests/test_worker_presets.py`, `tests/test_api_presets.py`

**Depends on:** Task 2

- [ ] **Step 1: Write the failing test**

Create `tests/test_worker_presets.py`:

```python
"""Presets are convenience wiring over existing settings, not new resolution."""

import pytest

from orchestrator.core.worker_presets import (
    WorkerPreset,
    parse_presets,
    resolve_preset,
)


RAW = [
    {
        "name": "local-lmstudio",
        "label": "Local GPU via LM Studio",
        "harness": "opencode",
        "model": "qwen3.6-27b",
        "endpoint": "http://host.docker.internal:1234",
        "requires": [],
    },
    {
        "name": "hosted-openweight",
        "label": "Hosted open-weight (OpenAI-compatible)",
        "harness": "opencode",
        "model": "glm-4.7",
        "endpoint": "https://api.z.ai/v1",
        "requires": ["api_key"],
    },
    {
        "name": "gemini-agy",
        "label": "Gemini via agy",
        "harness": "agy",
        "model": "Gemini 3.6 Flash (High)",
        "endpoint": "",
        "requires": ["interactive_login"],
    },
]


@pytest.mark.unit
def test_parse_returns_one_preset_per_entry():
    assert len(parse_presets(RAW)) == 3


@pytest.mark.unit
def test_a_preset_carries_its_harness_model_and_endpoint():
    preset = resolve_preset(parse_presets(RAW), "local-lmstudio")
    assert preset == WorkerPreset(
        name="local-lmstudio",
        label="Local GPU via LM Studio",
        harness="opencode",
        model="qwen3.6-27b",
        endpoint="http://host.docker.internal:1234",
        requires=(),
    )


@pytest.mark.unit
def test_an_unknown_preset_name_raises_with_the_known_names():
    with pytest.raises(KeyError, match="local-lmstudio"):
        resolve_preset(parse_presets(RAW), "does-not-exist")


@pytest.mark.unit
def test_a_preset_declaring_a_requirement_exposes_it():
    preset = resolve_preset(parse_presets(RAW), "gemini-agy")
    assert "interactive_login" in preset.requires


@pytest.mark.unit
def test_a_malformed_entry_is_skipped_not_fatal():
    """A typo in operator YAML must not stop the orchestrator booting."""
    presets = parse_presets([*RAW, {"label": "no name"}, "not a dict"])
    assert len(presets) == 3


@pytest.mark.unit
def test_parse_preserves_declaration_order():
    assert [p.name for p in parse_presets(RAW)] == [
        "local-lmstudio",
        "hosted-openweight",
        "gemini-agy",
    ]


@pytest.mark.unit
def test_the_shipped_yaml_declares_the_three_reference_presets():
    from orchestrator.core.settings_file import config_file_path, load_yaml_settings

    presets = parse_presets(load_yaml_settings(config_file_path()).get("worker_presets", []))
    assert {p.name for p in presets} == {
        "local-lmstudio",
        "hosted-openweight",
        "gemini-agy",
    }


@pytest.mark.unit
def test_every_shipped_preset_names_a_registered_harness():
    from orchestrator.core.harnesses import REGISTRY
    from orchestrator.core.settings_file import config_file_path, load_yaml_settings

    presets = parse_presets(load_yaml_settings(config_file_path()).get("worker_presets", []))
    for preset in presets:
        assert preset.harness in REGISTRY, preset.name
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_worker_presets.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.core.worker_presets'`.

- [ ] **Step 3: Write the module**

Create `src/orchestrator/core/worker_presets.py`:

```python
"""Named (harness, model, endpoint) triples for one-choice worker setup.

Presets are convenience wiring over the existing settings layers: choosing one
sets three fields that already existed.  There is deliberately NO new resolution
logic here, so a preset can never disagree with what the orchestrator actually
does.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerPreset:
    """One named worker configuration."""

    name: str
    label: str
    harness: str
    model: str
    endpoint: str
    requires: tuple[str, ...] = ()


def parse_presets(raw: list[Any]) -> list[WorkerPreset]:
    """Parse the ``worker_presets`` YAML block, skipping malformed entries.

    A typo in operator YAML must degrade to "that preset is missing", never
    stop the orchestrator from booting.
    """
    presets: list[WorkerPreset] = []
    for entry in raw or []:
        if not isinstance(entry, dict) or not entry.get("name"):
            logger.warning("skipping malformed worker preset entry: %r", entry)
            continue
        if not entry.get("harness") or not entry.get("model"):
            logger.warning(
                "skipping worker preset %r: harness and model are both required",
                entry.get("name"),
            )
            continue
        presets.append(
            WorkerPreset(
                name=str(entry["name"]),
                label=str(entry.get("label") or entry["name"]),
                harness=str(entry["harness"]),
                model=str(entry["model"]),
                endpoint=str(entry.get("endpoint") or ""),
                requires=tuple(str(r) for r in (entry.get("requires") or [])),
            )
        )
    return presets


def resolve_preset(presets: list[WorkerPreset], name: str) -> WorkerPreset:
    """Return the named preset.

    Raises:
        KeyError: With the known names in the message, so a typo is self-fixing.
    """
    for preset in presets:
        if preset.name == name:
            return preset
    known = ", ".join(p.name for p in presets) or "(none configured)"
    message = f"unknown worker preset {name!r}; known presets: {known}"
    raise KeyError(message)
```

- [ ] **Step 4: Add the YAML block**

In `config/praxis.yaml`, add:

```yaml
# Named worker configurations. Choosing one sets harness, model, and endpoint
# together; there is no new resolution logic, only fewer fields to get right.
# `praxis init` offers hosted-openweight first because it needs no local GPU.
worker_presets:
  - name: hosted-openweight
    label: "Hosted open-weight model (OpenAI-compatible endpoint)"
    harness: opencode
    model: "glm-4.7"
    endpoint: "https://api.z.ai/v1"
    requires: [api_key]
  - name: local-lmstudio
    label: "Local GPU via LM Studio"
    harness: opencode
    model: "qwen3.6-27b"
    endpoint: "http://host.docker.internal:1234"
    requires: []
  - name: gemini-agy
    label: "Gemini via the agy harness"
    harness: agy
    model: "Gemini 3.6 Flash (High)"
    endpoint: ""
    requires: [interactive_login]
```

- [ ] **Step 5: Add the settings accessor and the endpoint**

In `src/orchestrator/core/effective_settings.py`:

```python
    async def worker_presets(self) -> list[Any]:
        """Return the configured worker presets, parsed and validated."""
        from orchestrator.core.worker_presets import parse_presets

        yaml_data = await self._get_yaml()
        return parse_presets(yaml_data.get("worker_presets") or [])
```

Create `src/orchestrator/api/presets.py`:

```python
"""Worker preset catalog for the dashboard and the CLI."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends

from orchestrator.api.auth import require_token


router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("/presets")
async def list_presets(
    _: None = Depends(require_token),
) -> dict[str, list[dict[str, Any]]]:
    """Return the named worker presets, in declaration order."""
    from orchestrator.main import effective_settings

    presets = await effective_settings.worker_presets()
    return {"presets": [asdict(p) for p in presets]}
```

Read `src/orchestrator/api/settings.py` first and match its actual dependency
and app-state access pattern; substitute the real ones rather than the
placeholder import above.

Register the router in `src/orchestrator/main.py` next to the other
`app.include_router(...)` calls.

- [ ] **Step 6: Write the API test**

Create `tests/test_api_presets.py`:

```python
import pytest


@pytest.mark.integration
async def test_presets_endpoint_requires_auth(client):
    response = await client.get("/api/settings/presets")
    assert response.status_code == 401


@pytest.mark.integration
async def test_presets_endpoint_returns_the_shipped_presets(client, auth_headers):
    response = await client.get("/api/settings/presets", headers=auth_headers)
    assert response.status_code == 200
    names = {p["name"] for p in response.json()["presets"]}
    assert {"local-lmstudio", "hosted-openweight", "gemini-agy"} <= names


@pytest.mark.integration
async def test_every_preset_exposes_the_three_fields(client, auth_headers):
    response = await client.get("/api/settings/presets", headers=auth_headers)
    for preset in response.json()["presets"]:
        assert preset["harness"]
        assert preset["model"]
        assert "endpoint" in preset
        assert isinstance(preset["requires"], list)
```

`client` in `tests/conftest.py` is an httpx **AsyncClient**, not a sync
TestClient, so every API test is `async def` and every call is awaited.
`auth_headers` is `{"Authorization": "Bearer test-auth"}`.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_worker_presets.py tests/test_api_presets.py -v`
Expected: PASS.

- [ ] **Step 8: Group the dashboard New Project dropdown by preset**

In `web/app.js`, fetch `/api/settings/presets` when the New Project form opens
and render the model field as an `<optgroup>` per preset label, with the
preset's model as the option. Keep the existing `/api/lm-models` list as a
final "Other (from LM Studio)" group so nothing is lost. Selecting a preset
option must also set the harness field.

- [ ] **Step 9: Mutation-check the malformed-entry tolerance**

Temporarily remove the `if not isinstance(entry, dict) or not entry.get("name"):`
guard.
Run: `uv run pytest tests/test_worker_presets.py::test_a_malformed_entry_is_skipped_not_fatal -v`
Expected: FAIL with `TypeError` or `KeyError`. Restore and re-run to confirm PASS.

- [ ] **Step 10: Commit**

```bash
git add src/orchestrator/core/worker_presets.py src/orchestrator/api/presets.py src/orchestrator/core/effective_settings.py src/orchestrator/main.py config/praxis.yaml web/app.js tests/test_worker_presets.py tests/test_api_presets.py
git commit -m "feat(presets): add named worker presets

Three reference presets in YAML, an endpoint, and a grouped dashboard
dropdown. Convenience wiring over existing settings, no new resolution."
```

---

### Task 4: `praxis doctor`

**Files:**
- Create: `src/orchestrator/core/doctor.py`
- Create: `src/orchestrator/api/doctor.py`
- Create: `src/cli/doctor.py`
- Modify: `src/cli/main.py`
- Modify: `src/orchestrator/main.py`
- Test: `tests/test_doctor.py`, `tests/test_api_doctor.py`

**Depends on:** Task 1, Task 2, Task 3

- [ ] **Step 1: Write the failing test**

Create `tests/test_doctor.py`:

```python
"""Every check is a named, independently failing unit with a fix hint.

Doctor's contract: read-only, never raises, one hint per red, and a non-zero
exit whenever anything is red. Every troubleshooting doc entry starts here.
"""

import pytest

from orchestrator.core.doctor import (
    CHECK_IDS,
    CheckResult,
    CheckStatus,
    run_checks,
)


@pytest.mark.unit
def test_the_expected_checks_are_registered():
    assert set(CHECK_IDS) == {
        "docker_daemon",
        "orchestrator_health",
        "build_stamp",
        "agent_images",
        "agent_image_freshness",
        "auth_token",
        "git_credential",
        "planner_cli",
        "worker_endpoint",
        "callback_url",
        "config_mount",
    }


@pytest.mark.unit
def test_a_red_check_always_carries_a_fix_hint():
    result = CheckResult(
        check_id="docker_daemon", status=CheckStatus.RED, detail="not reachable"
    )
    assert result.hint, "every red result must tell the user what to do"


@pytest.mark.unit
def test_a_green_check_needs_no_hint():
    result = CheckResult(
        check_id="docker_daemon", status=CheckStatus.GREEN, detail="reachable"
    )
    assert result.hint == ""


@pytest.mark.unit
async def test_run_checks_returns_one_result_per_registered_check():
    results = await run_checks(probes=_all_failing())
    assert {r.check_id for r in results} == set(CHECK_IDS)


@pytest.mark.unit
async def test_a_raising_probe_becomes_a_red_result_not_an_exception():
    """Doctor diagnoses a broken machine; it must not break on one."""

    def _boom(*args, **kwargs):
        message = "everything is on fire"
        raise RuntimeError(message)

    probes = {check_id: _boom for check_id in CHECK_IDS}
    results = await run_checks(probes=probes)
    assert all(r.status is CheckStatus.RED for r in results)
    assert all("on fire" in r.detail for r in results)


@pytest.mark.unit
async def test_all_green_reports_ok():
    from orchestrator.core.doctor import overall_status

    results = await run_checks(probes=_all_passing())
    assert overall_status(results) is CheckStatus.GREEN


@pytest.mark.unit
async def test_one_red_makes_the_overall_status_red():
    from orchestrator.core.doctor import overall_status

    probes = _all_passing()
    probes["worker_endpoint"] = lambda **_: CheckResult(
        check_id="worker_endpoint",
        status=CheckStatus.RED,
        detail="no model loaded",
        hint="load the configured model in LM Studio",
    )
    assert overall_status(await run_checks(probes=probes)) is CheckStatus.RED


@pytest.mark.unit
async def test_an_amber_check_does_not_make_the_overall_status_red():
    """Local mode has no GitHub credential; that is a note, not a failure."""
    from orchestrator.core.doctor import overall_status

    probes = _all_passing()
    probes["git_credential"] = lambda **_: CheckResult(
        check_id="git_credential",
        status=CheckStatus.AMBER,
        detail="local mode: no GitHub credential configured",
    )
    assert overall_status(await run_checks(probes=probes)) is CheckStatus.AMBER


@pytest.mark.unit
async def test_stale_agent_image_is_detected_from_the_entrypoint_mtime():
    from orchestrator.core.doctor import image_is_stale

    assert image_is_stale(image_built_at=100.0, entrypoint_mtime=200.0) is True
    assert image_is_stale(image_built_at=300.0, entrypoint_mtime=200.0) is False


@pytest.mark.unit
async def test_a_missing_image_build_time_is_treated_as_stale():
    """Unknown build time means we cannot prove freshness; say so."""
    from orchestrator.core.doctor import image_is_stale

    assert image_is_stale(image_built_at=None, entrypoint_mtime=200.0) is True


def _all_passing() -> dict:
    return {
        check_id: (
            lambda check_id=check_id, **_: CheckResult(
                check_id=check_id, status=CheckStatus.GREEN, detail="ok"
            )
        )
        for check_id in CHECK_IDS
    }


def _all_failing() -> dict:
    return {
        check_id: (
            lambda check_id=check_id, **_: CheckResult(
                check_id=check_id,
                status=CheckStatus.RED,
                detail="nope",
                hint="fix it",
            )
        )
        for check_id in CHECK_IDS
    }
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_doctor.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.core.doctor'`.

- [ ] **Step 3: Write the check engine**

Create `src/orchestrator/core/doctor.py`:

```python
"""Read-only diagnosis of a Praxis installation.

Contract, in order of importance:

1. Read-only.  Doctor never changes state, so it is always safe to run.
2. Never raises.  It diagnoses a broken machine; breaking on one is useless.
   Every probe is wrapped, and a raising probe becomes a RED result carrying
   the exception text.
3. Every RED result carries a fix hint.  A red light with no next step is
   worse than no light.

Every troubleshooting entry in ``docs/reference.md`` starts with "run
``praxis doctor``" plus the matching row's hint, so this registry and that doc
are one thing in two places.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable


logger = logging.getLogger(__name__)


class CheckStatus(StrEnum):
    """Traffic-light outcome of one check."""

    GREEN = "green"
    AMBER = "amber"
    RED = "red"


@dataclass(frozen=True)
class CheckResult:
    """One check's verdict."""

    check_id: str
    status: CheckStatus
    detail: str
    hint: str = ""
    label: str = ""

    def __post_init__(self) -> None:
        if self.status is CheckStatus.RED and not self.hint:
            object.__setattr__(
                self,
                "hint",
                "see docs/reference.md troubleshooting for this check",
            )


@dataclass(frozen=True)
class Check:
    """Static metadata for one registered check."""

    check_id: str
    label: str
    hint: str


CHECKS: tuple[Check, ...] = (
    Check(
        "docker_daemon",
        "Docker daemon reachable",
        "start Docker Desktop (or `sudo systemctl start docker`) and re-run",
    ),
    Check(
        "orchestrator_health",
        "Orchestrator responding on /health",
        "run `docker compose up -d` and check `docker logs --tail 50 orchestrator`",
    ),
    Check(
        "build_stamp",
        "Running commit matches the working tree",
        "rebuild and restart: "
        "`PRAXIS_BUILD_SHA=$(git rev-parse --short HEAD) docker compose up --build -d`",
    ),
    Check(
        "agent_images",
        "Agent images present",
        "run `docker compose --profile agents build`",
    ),
    Check(
        "agent_image_freshness",
        "Agent images newer than their entrypoints",
        "an entrypoint changed since the image was built; run "
        "`docker compose --profile agents build` or a stale image runs silently",
    ),
    Check(
        "auth_token",
        "AUTH_TOKEN accepted by the API",
        "check AUTH_TOKEN in .env matches the value the CLI is sending",
    ),
    Check(
        "git_credential",
        "Git credential usable",
        "set GITHUB_TOKEN (or the GitHub App vars) in .env, or use a local "
        "`file://` repo to evaluate without any credential",
    ),
    Check(
        "planner_cli",
        "Planner CLI installed and authenticated",
        "install the planner CLI and run its login command; see "
        "docs/getting-started.md",
    ),
    Check(
        "worker_endpoint",
        "Worker endpoint reachable with the configured model loaded",
        "start the endpoint and load the configured model, or switch preset "
        "with `praxis config` ",
    ),
    Check(
        "callback_url",
        "Agent callback URL port matches the orchestrator port",
        "set AGENT_CALLBACK_URL to match PORT in .env, or unset it so it is "
        "derived; a mismatch 404s every agent callback",
    ),
    Check(
        "config_mount",
        "config/praxis.yaml is mounted, not baked",
        "add the `./config:/app/config:ro` volume and PRAXIS_CONFIG_PATH to "
        "your compose file",
    ),
)

CHECK_IDS: tuple[str, ...] = tuple(c.check_id for c in CHECKS)

_BY_ID: dict[str, Check] = {c.check_id: c for c in CHECKS}


def image_is_stale(image_built_at: float | None, entrypoint_mtime: float) -> bool:
    """True when an agent image predates its entrypoint source.

    An unknown build time counts as stale: freshness cannot be proven, and a
    silently stale agent image is the failure mode this check exists to catch.
    """
    if image_built_at is None:
        return True
    return image_built_at < entrypoint_mtime


async def _invoke(probe: Callable[..., Any], check_id: str, **kwargs: Any) -> CheckResult:
    """Run one probe, converting any exception into a RED result."""
    try:
        outcome = probe(**kwargs)
        if inspect.isawaitable(outcome):
            outcome = await outcome
    except Exception as exc:  # noqa: BLE001 - a broken probe is a red light
        logger.debug("doctor check %s raised", check_id, exc_info=True)
        return CheckResult(
            check_id=check_id,
            status=CheckStatus.RED,
            detail=f"{type(exc).__name__}: {exc}",
            hint=_BY_ID[check_id].hint,
            label=_BY_ID[check_id].label,
        )
    if not isinstance(outcome, CheckResult):
        return CheckResult(
            check_id=check_id,
            status=CheckStatus.RED,
            detail=f"probe returned {type(outcome).__name__}, expected CheckResult",
            hint=_BY_ID[check_id].hint,
            label=_BY_ID[check_id].label,
        )
    hint = outcome.hint or (
        _BY_ID[check_id].hint if outcome.status is CheckStatus.RED else ""
    )
    return CheckResult(
        check_id=outcome.check_id,
        status=outcome.status,
        detail=outcome.detail,
        hint=hint,
        label=outcome.label or _BY_ID[check_id].label,
    )


async def run_checks(
    probes: dict[str, Callable[..., Any]], **context: Any
) -> list[CheckResult]:
    """Run every registered check, in registry order.

    Args:
        probes: ``check_id`` to callable.  A missing probe yields a RED
            "not implemented" result rather than being skipped, so a check can
            never silently disappear.
        **context: Passed through to every probe.

    Returns:
        One result per registered check, in ``CHECKS`` order.
    """
    results: list[CheckResult] = []
    for check in CHECKS:
        probe = probes.get(check.check_id)
        if probe is None:
            results.append(
                CheckResult(
                    check_id=check.check_id,
                    status=CheckStatus.RED,
                    detail="no probe registered for this check",
                    hint=check.hint,
                    label=check.label,
                )
            )
            continue
        results.append(await _invoke(probe, check.check_id, **context))
    return results


def overall_status(results: list[CheckResult]) -> CheckStatus:
    """Worst status across all results. Any RED is RED; any AMBER is AMBER."""
    if any(r.status is CheckStatus.RED for r in results):
        return CheckStatus.RED
    if any(r.status is CheckStatus.AMBER for r in results):
        return CheckStatus.AMBER
    return CheckStatus.GREEN
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_doctor.py -v`
Expected: PASS (10 tests).

- [ ] **Step 5: Mutation-check the never-raises guarantee**

Temporarily remove the `try`/`except` from `_invoke`.
Run: `uv run pytest tests/test_doctor.py::test_a_raising_probe_becomes_a_red_result_not_an_exception -v`
Expected: FAIL with `RuntimeError: everything is on fire`. Restore and re-run to
confirm PASS.

- [ ] **Step 6: Mutation-check the always-a-hint guarantee**

Temporarily remove the `__post_init__` on `CheckResult`.
Run: `uv run pytest tests/test_doctor.py::test_a_red_check_always_carries_a_fix_hint -v`
Expected: FAIL. Restore and re-run to confirm PASS.

- [ ] **Step 7: Commit the engine**

```bash
git add src/orchestrator/core/doctor.py tests/test_doctor.py
git commit -m "feat(doctor): add the read-only check registry

Eleven named checks, each with a fix hint. A raising probe becomes a red
result rather than an exception: doctor diagnoses broken machines and must
not break on one."
```

---

### Task 5: The doctor probes and its two surfaces

**Files:**
- Modify: `src/orchestrator/core/doctor.py` (add the real probes)
- Create: `src/orchestrator/api/doctor.py`
- Create: `src/cli/doctor.py`
- Modify: `src/cli/main.py`
- Modify: `src/orchestrator/main.py`
- Test: `tests/test_doctor_probes.py`, `tests/test_api_doctor.py`, `tests/test_cli_doctor.py`

**Depends on:** Task 4

- [ ] **Step 1: Write the failing probe test**

Create `tests/test_doctor_probes.py`:

```python
"""Each probe's decision logic, with the environment stubbed out."""

import pytest

from orchestrator.core.doctor import CheckStatus
from orchestrator.core.doctor_probes import (
    probe_agent_image_freshness,
    probe_callback_url,
    probe_config_mount,
    probe_git_credential,
    probe_worker_endpoint,
)


@pytest.mark.unit
def test_callback_url_green_when_the_port_matches():
    result = probe_callback_url(
        port=12323,
        callback_url="http://host.docker.internal:12323/api/internal/agent-done",
    )
    assert result.status is CheckStatus.GREEN


@pytest.mark.unit
def test_callback_url_red_when_the_port_differs():
    """The classic silent failure: every agent callback 404s."""
    result = probe_callback_url(
        port=12323,
        callback_url="http://host.docker.internal:8080/api/internal/agent-done",
    )
    assert result.status is CheckStatus.RED
    assert "12323" in result.detail
    assert result.hint


@pytest.mark.unit
def test_callback_url_green_when_unset_because_it_is_derived():
    assert probe_callback_url(port=12323, callback_url=None).status is CheckStatus.GREEN


@pytest.mark.unit
def test_git_credential_amber_in_local_mode():
    result = probe_git_credential(configured=False, local_mode=True)
    assert result.status is CheckStatus.AMBER
    assert "local" in result.detail.lower()


@pytest.mark.unit
def test_git_credential_red_when_absent_in_github_mode():
    result = probe_git_credential(configured=False, local_mode=False)
    assert result.status is CheckStatus.RED
    assert result.hint


@pytest.mark.unit
def test_git_credential_green_when_configured():
    assert probe_git_credential(configured=True, local_mode=False).status is (
        CheckStatus.GREEN
    )


@pytest.mark.unit
def test_worker_endpoint_red_when_unreachable():
    result = probe_worker_endpoint(reachable=False, models=[], configured_model="m")
    assert result.status is CheckStatus.RED


@pytest.mark.unit
def test_worker_endpoint_red_when_the_configured_model_is_not_loaded():
    """Reachable but wrong model is the failure that looks like success."""
    result = probe_worker_endpoint(
        reachable=True, models=["other-model"], configured_model="qwen3.6-27b"
    )
    assert result.status is CheckStatus.RED
    assert "qwen3.6-27b" in result.detail


@pytest.mark.unit
def test_worker_endpoint_green_when_the_model_is_loaded():
    result = probe_worker_endpoint(
        reachable=True, models=["qwen3.6-27b"], configured_model="qwen3.6-27b"
    )
    assert result.status is CheckStatus.GREEN


@pytest.mark.unit
def test_agent_image_freshness_red_when_the_entrypoint_is_newer():
    result = probe_agent_image_freshness(
        images={"opencode-agent:latest": 100.0},
        entrypoint_mtimes={"opencode-agent:latest": 200.0},
    )
    assert result.status is CheckStatus.RED
    assert "opencode-agent" in result.detail


@pytest.mark.unit
def test_agent_image_freshness_green_when_images_are_newer():
    result = probe_agent_image_freshness(
        images={"opencode-agent:latest": 300.0},
        entrypoint_mtimes={"opencode-agent:latest": 200.0},
    )
    assert result.status is CheckStatus.GREEN


@pytest.mark.unit
def test_config_mount_red_when_the_path_is_inside_the_image():
    result = probe_config_mount(config_path="/app/config/praxis.yaml", mounted=False)
    assert result.status is CheckStatus.RED


@pytest.mark.unit
def test_config_mount_green_when_mounted():
    result = probe_config_mount(config_path="/app/config/praxis.yaml", mounted=True)
    assert result.status is CheckStatus.GREEN
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_doctor_probes.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.core.doctor_probes'`.

- [ ] **Step 3: Write the probes**

Create `src/orchestrator/core/doctor_probes.py` with one pure function per
check. Each takes already-gathered facts as arguments and returns a
`CheckResult`, so the decision logic is testable without Docker, a network, or a
filesystem. Implement all eleven; the five with non-trivial logic are:

```python
"""Pure decision logic for each doctor check.

Each probe receives already-gathered facts and returns a verdict.  Gathering
(Docker calls, HTTP requests, filesystem stats) happens in the API layer; the
decisions live here so they are testable with no environment at all.
"""

from __future__ import annotations

from orchestrator.core.doctor import CheckResult, CheckStatus, image_is_stale


def probe_callback_url(port: int, callback_url: str | None) -> CheckResult:
    """Green when the callback port matches the orchestrator port.

    A mismatch makes every agent callback 404, so tasks only ever finish via
    reconcile and are marked failed even on success.  An unset value is green
    because it is then derived from ``PORT``.
    """
    if not callback_url:
        return CheckResult(
            check_id="callback_url",
            status=CheckStatus.GREEN,
            detail=f"derived from PORT={port}",
        )
    if f":{port}/" in callback_url:
        return CheckResult(
            check_id="callback_url",
            status=CheckStatus.GREEN,
            detail=callback_url,
        )
    return CheckResult(
        check_id="callback_url",
        status=CheckStatus.RED,
        detail=(
            f"AGENT_CALLBACK_URL is {callback_url} but PORT is {port}; "
            "every agent callback will 404"
        ),
    )


def probe_git_credential(configured: bool, local_mode: bool) -> CheckResult:
    """Green when configured, amber in local mode, red otherwise."""
    if configured:
        return CheckResult(
            check_id="git_credential",
            status=CheckStatus.GREEN,
            detail="credential configured",
        )
    if local_mode:
        return CheckResult(
            check_id="git_credential",
            status=CheckStatus.AMBER,
            detail=(
                "local mode: no GitHub credential configured, which is correct "
                "for evaluating with a file:// repo"
            ),
        )
    return CheckResult(
        check_id="git_credential",
        status=CheckStatus.RED,
        detail="no GitHub credential configured and no local repo in use",
    )


def probe_worker_endpoint(
    reachable: bool, models: list[str], configured_model: str
) -> CheckResult:
    """Green only when the endpoint answers AND the configured model is loaded.

    Reachable-but-wrong-model is the failure that looks like success: the
    dashboard shows a connected endpoint and every dispatch fails on a model
    the server does not have.
    """
    if not reachable:
        return CheckResult(
            check_id="worker_endpoint",
            status=CheckStatus.RED,
            detail="worker endpoint did not answer GET /v1/models",
        )
    if configured_model and configured_model not in models:
        loaded = ", ".join(models) or "(none)"
        return CheckResult(
            check_id="worker_endpoint",
            status=CheckStatus.RED,
            detail=(
                f"endpoint is up but the configured model {configured_model!r} "
                f"is not loaded; loaded: {loaded}"
            ),
        )
    return CheckResult(
        check_id="worker_endpoint",
        status=CheckStatus.GREEN,
        detail=f"{configured_model or 'endpoint'} available",
    )


def probe_agent_image_freshness(
    images: dict[str, float | None], entrypoint_mtimes: dict[str, float]
) -> CheckResult:
    """Red when any agent image predates its entrypoint source.

    This converts the project's oldest silent failure into a red light: a stale
    agent image runs old entrypoint logic while the source looks current.
    """
    stale = [
        tag
        for tag, built_at in images.items()
        if tag in entrypoint_mtimes
        and image_is_stale(built_at, entrypoint_mtimes[tag])
    ]
    if stale:
        return CheckResult(
            check_id="agent_image_freshness",
            status=CheckStatus.RED,
            detail=f"stale image(s): {', '.join(sorted(stale))}",
        )
    return CheckResult(
        check_id="agent_image_freshness",
        status=CheckStatus.GREEN,
        detail="all agent images newer than their entrypoints",
    )


def probe_config_mount(config_path: str, mounted: bool) -> CheckResult:
    """Red when the settings YAML is baked into the image rather than mounted."""
    if mounted:
        return CheckResult(
            check_id="config_mount",
            status=CheckStatus.GREEN,
            detail=f"{config_path} is a bind mount",
        )
    return CheckResult(
        check_id="config_mount",
        status=CheckStatus.RED,
        detail=(
            f"{config_path} is baked into the image; YAML edits will need a "
            "rebuild instead of a restart"
        ),
    )
```

Add the remaining six probes (`docker_daemon`, `orchestrator_health`,
`build_stamp`, `agent_images`, `auth_token`, `planner_cli`) in the same shape:
facts in, `CheckResult` out. Reuse `api/system.py::_probe_claude_cli` logic for
`planner_cli` and `core/build_info.py` for `build_stamp` rather than duplicating
either.

- [ ] **Step 4: Add the API endpoint**

Create `src/orchestrator/api/doctor.py` that gathers the facts (Docker SDK image
list and their `Created` timestamps, an HTTP `GET /v1/models` against the
effective worker endpoint, `os.path.ismount` or a `/proc/mounts` scan for the
config mount, `Settings` values, `build_info`) and calls `run_checks`, returning:

```json
{
  "status": "green|amber|red",
  "checks": [
    {"check_id": "...", "label": "...", "status": "...", "detail": "...", "hint": "..."}
  ]
}
```

Register it in `src/orchestrator/main.py`.

- [ ] **Step 5: Write the API test**

Create `tests/test_api_doctor.py`:

```python
import pytest


@pytest.mark.integration
async def test_doctor_requires_auth(client):
    response = await client.get("/api/doctor")
    assert response.status_code == 401


@pytest.mark.integration
async def test_doctor_returns_every_check(client, auth_headers):
    from orchestrator.core.doctor import CHECK_IDS

    response = await client.get("/api/doctor", headers=auth_headers)
    assert {c["check_id"] for c in response.json()["checks"]} == set(CHECK_IDS)


@pytest.mark.integration
async def test_doctor_is_http_200_even_when_checks_are_red(client, auth_headers):
    """A diagnosis is a successful response, whatever it diagnoses."""
    response = await client.get("/api/doctor", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["status"] in {"green", "amber", "red"}


@pytest.mark.integration
async def test_every_red_check_in_the_response_carries_a_hint(client, auth_headers):
    body = (await client.get("/api/doctor", headers=auth_headers)).json()
    for check in body["checks"]:
        if check["status"] == "red":
            assert check["hint"]
```

- [ ] **Step 6: Write the CLI command**

Create `src/cli/doctor.py`:

```python
"""`praxis doctor`: one table, green and red, one fix hint per red."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table


console = Console()

_SYMBOL = {"green": "[green]OK[/green]", "amber": "[yellow]NOTE[/yellow]", "red": "[red]FAIL[/red]"}


def render(payload: dict) -> int:
    """Print the doctor table and return the intended process exit code."""
    table = Table(title="praxis doctor")
    for column in ("", "Check", "Detail"):
        table.add_column(column)
    for check in payload["checks"]:
        table.add_row(
            _SYMBOL.get(check["status"], check["status"]),
            check.get("label") or check["check_id"],
            check["detail"],
        )
    console.print(table)

    reds = [c for c in payload["checks"] if c["status"] == "red"]
    for check in reds:
        console.print(
            f"[red]FAIL[/red] {check.get('label') or check['check_id']}: "
            f"{check['hint']}"
        )
    if reds:
        console.print(f"\n[red]{len(reds)} check(s) failed.[/red]")
        return 1
    console.print("\n[green]All checks passed.[/green]")
    return 0


def doctor() -> None:
    """Diagnose this Praxis installation. Exits non-zero on any failure."""
    from cli.main import _check_dict, _client

    with _client() as client:
        payload = _check_dict(client.get("/api/doctor"))
    raise typer.Exit(code=render(payload))
```

Register it in `src/cli/main.py`:

```python
from cli.doctor import doctor as _doctor

app.command("doctor")(_doctor)
```

- [ ] **Step 7: Write the CLI test**

Create `tests/test_cli_doctor.py`:

```python
import pytest

from cli.doctor import render


@pytest.mark.unit
def test_all_green_exits_zero():
    payload = {
        "status": "green",
        "checks": [
            {"check_id": "docker_daemon", "label": "Docker", "status": "green",
             "detail": "ok", "hint": ""}
        ],
    }
    assert render(payload) == 0


@pytest.mark.unit
def test_any_red_exits_non_zero():
    payload = {
        "status": "red",
        "checks": [
            {"check_id": "docker_daemon", "label": "Docker", "status": "red",
             "detail": "not reachable", "hint": "start Docker Desktop"}
        ],
    }
    assert render(payload) == 1


@pytest.mark.unit
def test_amber_alone_exits_zero():
    """Local mode has no GitHub credential; that is not a failure."""
    payload = {
        "status": "amber",
        "checks": [
            {"check_id": "git_credential", "label": "Git credential",
             "status": "amber", "detail": "local mode", "hint": ""}
        ],
    }
    assert render(payload) == 0


@pytest.mark.unit
def test_the_hint_is_printed_for_each_red(capsys):
    payload = {
        "status": "red",
        "checks": [
            {"check_id": "docker_daemon", "label": "Docker", "status": "red",
             "detail": "not reachable", "hint": "start Docker Desktop"}
        ],
    }
    render(payload)
    assert "start Docker Desktop" in capsys.readouterr().out
```

- [ ] **Step 8: Run every test to verify they pass**

Run: `uv run pytest tests/test_doctor.py tests/test_doctor_probes.py tests/test_api_doctor.py tests/test_cli_doctor.py -v`
Expected: PASS.

- [ ] **Step 9: Run doctor against the real installation**

```bash
uv run praxis doctor; echo "exit: $?"
```

Expected: a table with eleven rows. Fix every red it reports before continuing;
that is the point of the command. Record any check whose hint turned out to be
wrong or unhelpful and improve the hint, then re-run.

- [ ] **Step 10: Mutation-check the exit code**

Temporarily change `return 1` to `return 0` in `render`.
Run: `uv run pytest tests/test_cli_doctor.py::test_any_red_exits_non_zero -v`
Expected: FAIL. Restore and re-run to confirm PASS.

- [ ] **Step 11: Commit**

```bash
git add src/orchestrator/core/doctor_probes.py src/orchestrator/api/doctor.py src/cli/doctor.py src/cli/main.py src/orchestrator/main.py tests/test_doctor_probes.py tests/test_api_doctor.py tests/test_cli_doctor.py
git commit -m "feat(doctor): add the probes, the REST endpoint, and the CLI table

Pure decision logic per check, facts gathered at the API layer. Stale agent
images and a mismatched callback port, this project's two oldest silent
failures, are now red lights with fix hints."
```

---

### Task 6: `praxis init`

**Files:**
- Create: `src/cli/init.py`
- Modify: `src/cli/main.py`
- Test: `tests/test_cli_init.py`

**Depends on:** Task 3, Task 5

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_init.py`:

```python
"""init is idempotent, writes a valid .env, and never overwrites blindly."""

import pytest

from cli.init import (
    build_env_file,
    generate_token,
    mcp_snippet,
    merge_env,
)


@pytest.mark.unit
def test_a_generated_token_is_long_enough_to_be_a_secret():
    token = generate_token()
    assert len(token) >= 32
    assert token != generate_token()


@pytest.mark.unit
def test_build_env_file_contains_the_two_required_values():
    text = build_env_file({"AUTH_TOKEN": "t", "GITHUB_TOKEN": "g"})
    assert "AUTH_TOKEN=t" in text
    assert "GITHUB_TOKEN=g" in text


@pytest.mark.unit
def test_build_env_file_quotes_a_value_with_spaces():
    text = build_env_file({"DEFAULT_WORKER_MODEL": "Gemini 3.6 Flash (High)"})
    assert 'DEFAULT_WORKER_MODEL="Gemini 3.6 Flash (High)"' in text


@pytest.mark.unit
def test_local_mode_writes_no_github_token():
    text = build_env_file({"AUTH_TOKEN": "t"})
    assert "GITHUB_TOKEN" not in text


@pytest.mark.unit
def test_merge_env_preserves_unrelated_existing_keys():
    """Re-running init must not blow away an operator's other settings."""
    existing = "TZ=Asia/Jakarta\nAUTH_TOKEN=old\n"
    merged = merge_env(existing, {"AUTH_TOKEN": "new"})
    assert "TZ=Asia/Jakarta" in merged
    assert "AUTH_TOKEN=new" in merged
    assert "AUTH_TOKEN=old" not in merged


@pytest.mark.unit
def test_merge_env_preserves_comments():
    existing = "# my notes\nTZ=UTC\n"
    assert "# my notes" in merge_env(existing, {"AUTH_TOKEN": "t"})


@pytest.mark.unit
def test_merge_env_is_idempotent():
    once = merge_env("", {"AUTH_TOKEN": "t"})
    twice = merge_env(once, {"AUTH_TOKEN": "t"})
    assert once == twice


@pytest.mark.unit
def test_the_mcp_snippet_is_valid_json_naming_the_praxis_server():
    import json

    snippet = mcp_snippet(api_url="http://127.0.0.1:12323", token="tok")
    parsed = json.loads(snippet)
    assert "praxis" in parsed["mcpServers"]
    assert parsed["mcpServers"]["praxis"]["command"] == "praxis-mcp"


@pytest.mark.unit
def test_the_mcp_snippet_carries_the_api_url_and_token():
    snippet = mcp_snippet(api_url="http://127.0.0.1:12323", token="tok")
    assert "http://127.0.0.1:12323" in snippet
    assert "tok" in snippet
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_cli_init.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cli.init'`.

- [ ] **Step 3: Write the module**

Create `src/cli/init.py`:

```python
"""`praxis init`: clone to a running, verified orchestrator in one command.

Idempotent and re-runnable.  It never overwrites an existing ``.env``
wholesale: unrelated keys and comments survive, only the keys it manages are
replaced.  It ends by running the doctor, because "it started" and "it works"
are different claims and only the second one is useful.
"""

from __future__ import annotations

import json
import re
import secrets
import subprocess  # nosec B404 - docker compose is the interface
import time
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.prompt import Confirm, Prompt


console = Console()

# Keys `init` manages. Everything else in an existing .env is left alone.
_MANAGED_KEYS = (
    "AUTH_TOKEN",
    "GITHUB_TOKEN",
    "PORT",
    "LM_STUDIO_URL",
    "DEFAULT_WORKER_HARNESS",
    "DEFAULT_WORKER_MODEL",
)

_NEEDS_QUOTING = re.compile(r"[\s#\"']")


def generate_token() -> str:
    """Return a fresh URL-safe auth token."""
    return secrets.token_urlsafe(32)


def _render_value(value: str) -> str:
    return f'"{value}"' if _NEEDS_QUOTING.search(value) else value


def build_env_file(values: dict[str, str]) -> str:
    """Render a fresh ``.env`` from the managed values only."""
    lines = [
        "# Written by `praxis init`. Safe to edit; re-running init preserves",
        "# every key it does not manage, and every comment.",
    ]
    for key, value in values.items():
        if value:
            lines.append(f"{key}={_render_value(value)}")
    return "\n".join(lines) + "\n"


def merge_env(existing: str, values: dict[str, str]) -> str:
    """Merge managed values into an existing ``.env`` text.

    Unrelated keys and comments are preserved verbatim and keep their position;
    a managed key already present is replaced in place; a managed key absent is
    appended.  This is what makes re-running ``init`` safe.
    """
    replaced: set[str] = set()
    out: list[str] = []
    for line in existing.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in values:
            if values[key]:
                out.append(f"{key}={_render_value(values[key])}")
            replaced.add(key)
        else:
            out.append(line)
    for key, value in values.items():
        if key not in replaced and value:
            out.append(f"{key}={_render_value(value)}")
    return "\n".join(out).rstrip("\n") + "\n"


def mcp_snippet(api_url: str, token: str) -> str:
    """Return the MCP client configuration for this installation."""
    return json.dumps(
        {
            "mcpServers": {
                "praxis": {
                    "command": "praxis-mcp",
                    "env": {"PRAXIS_API": api_url, "PRAXIS_TOKEN": token},
                }
            }
        },
        indent=2,
    )


def _wait_for_health(url: str, timeout_s: int = 180) -> bool:
    """Poll ``/health`` until it answers or the timeout expires."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{url}/health", timeout=5).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(3)
    return False


def init() -> None:  # noqa: PLR0915 - a linear setup script, deliberately flat
    """Set up and start Praxis, then verify it."""
    console.print("[bold]praxis init[/bold]\n")

    env_path = Path(".env")
    existing = env_path.read_text(encoding="utf-8") if env_path.is_file() else ""

    token = Prompt.ask("Auth token", default=generate_token())
    port = Prompt.ask("Dashboard port", default="12323")

    console.print(
        "\nGit access. Choose 'skip' to evaluate Praxis against a local "
        "bare repo with no GitHub credential at all."
    )
    gh_token = Prompt.ask(
        "GitHub token (or 'skip' for local mode)", default="skip"
    )
    if gh_token.strip().lower() == "skip":
        gh_token = ""

    presets = _fetch_presets_or_defaults()
    console.print("\nWorker presets:")
    for index, preset in enumerate(presets, start=1):
        extra = f"  (requires: {', '.join(preset['requires'])})" if preset["requires"] else ""
        console.print(f"  {index}. {preset['label']}{extra}")
    choice = int(Prompt.ask("Preset", default="1")) - 1
    preset = presets[max(0, min(choice, len(presets) - 1))]

    values = {
        "AUTH_TOKEN": token,
        "GITHUB_TOKEN": gh_token,
        "PORT": port,
        "LM_STUDIO_URL": preset["endpoint"],
        "DEFAULT_WORKER_HARNESS": preset["harness"],
        "DEFAULT_WORKER_MODEL": preset["model"],
    }
    env_text = merge_env(existing, values) if existing else build_env_file(values)
    if existing and not Confirm.ask(f"Update {env_path}?", default=True):
        console.print("[yellow]Left .env unchanged.[/yellow]")
    else:
        env_path.write_text(env_text, encoding="utf-8")
        console.print(f"[green]Wrote {env_path}[/green]")

    console.print("\nBuilding agent images (this takes a few minutes the first time)")
    subprocess.run(  # nosec B603 B607 - fixed argv, operator-invoked
        ["docker", "compose", "--profile", "agents", "build"], check=True
    )

    console.print("Starting the orchestrator")
    subprocess.run(  # nosec B603 B607 - fixed argv, operator-invoked
        ["docker", "compose", "up", "-d", "--build"], check=True
    )

    api_url = f"http://127.0.0.1:{port}"
    console.print(f"Waiting for {api_url}/health")
    if not _wait_for_health(api_url):
        console.print(
            "[red]The orchestrator did not become healthy.[/red] "
            "Check `docker logs --tail 50 orchestrator`, then re-run "
            "`praxis init`."
        )
        raise typer.Exit(code=1)

    console.print(f"\n[green]Praxis is running.[/green]")
    console.print(f"  Dashboard: {api_url}")
    console.print("\nAdd it to your MCP client with this configuration:\n")
    console.print(mcp_snippet(api_url, token))

    console.print("\nVerifying the installation:\n")
    from cli.doctor import render

    with httpx.Client(
        base_url=api_url, headers={"Authorization": f"Bearer {token}"}, timeout=60
    ) as client:
        payload = client.get("/api/doctor").json()
    raise typer.Exit(code=render(payload))


def _fetch_presets_or_defaults() -> list[dict]:
    """Read presets from the local YAML so init works before the server is up."""
    from orchestrator.core.settings_file import config_file_path, load_yaml_settings
    from orchestrator.core.worker_presets import parse_presets

    presets = parse_presets(load_yaml_settings(config_file_path()).get("worker_presets", []))
    return [
        {
            "name": p.name,
            "label": p.label,
            "harness": p.harness,
            "model": p.model,
            "endpoint": p.endpoint,
            "requires": list(p.requires),
        }
        for p in presets
    ] or [
        {
            "name": "local-lmstudio",
            "label": "Local GPU via LM Studio",
            "harness": "opencode",
            "model": "",
            "endpoint": "http://host.docker.internal:1234",
            "requires": [],
        }
    ]
```

Register it in `src/cli/main.py`:

```python
from cli.init import init as _init

app.command("init")(_init)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_cli_init.py -v`
Expected: PASS (9 tests).

- [ ] **Step 5: Mutation-check the non-destructive merge**

Temporarily change `merge_env` to `return build_env_file(values)`.
Run: `uv run pytest tests/test_cli_init.py -v -k "preserves"`
Expected: both preservation tests FAIL. Restore and re-run to confirm PASS. This
is the difference between a re-runnable command and one that eats the operator's
timezone setting.

- [ ] **Step 6: Commit**

```bash
git add src/cli/init.py src/cli/main.py tests/test_cli_init.py
git commit -m "feat(init): add praxis init

Prompts for token, git access (with a skip-for-local-mode path), and a
worker preset; writes .env non-destructively; builds, starts, waits for
health, prints the MCP snippet, and ends by running doctor."
```

---

### Task 7: The merge-gate approvals digest

**Files:**
- Create: `src/orchestrator/core/approvals.py`
- Create: `src/orchestrator/api/approvals.py`
- Modify: `src/mcp_server/server.py`
- Modify: `src/orchestrator/core/orchestrator.py`
- Modify: `src/cli/main.py`
- Modify: `web/app.js`, `web/styles.css`
- Modify: `config/praxis.yaml`
- Test: `tests/test_approvals.py`, `tests/test_mcp_pending_approvals.py`

**Depends on:** Task 5

The documented abandonment trigger for this product category is parked work
nobody sees ("the card sits In Review for three days"). Praxis parks at PASSED
by design, so surfacing it is not a nicety.

- [ ] **Step 1: Write the failing test**

Create `tests/test_approvals.py`:

```python
"""Parked work must be visible on every surface a user already polls."""

from datetime import UTC, datetime, timedelta

import pytest

from orchestrator.core.approvals import (
    digest_line,
    should_publish_digest,
    summarize_pending,
)


def _task(hours_old: float, **overrides) -> dict:
    base = {
        "id": "t1",
        "title": "Add the widget",
        "status": "passed",
        "branch_name": "agent/add-widget",
        "pr_url": "https://github.com/o/r/pull/7",
        "updated_at": (
            datetime.now(UTC) - timedelta(hours=hours_old)
        ).isoformat(),
    }
    base.update(overrides)
    return base


@pytest.mark.unit
def test_summarize_counts_only_parked_tasks():
    rows = [_task(1), _task(2), _task(3, status="merged"), _task(4, status="pending")]
    summary = summarize_pending(rows)
    assert summary["count"] == 2


@pytest.mark.unit
def test_summarize_reports_the_oldest_age_in_hours():
    summary = summarize_pending([_task(2), _task(26)])
    assert 25.5 < summary["oldest_hours"] < 26.5


@pytest.mark.unit
def test_summarize_lists_each_parked_task_with_its_pr():
    summary = summarize_pending([_task(1)])
    assert summary["tasks"][0]["pr_url"] == "https://github.com/o/r/pull/7"
    assert summary["tasks"][0]["branch"] == "agent/add-widget"


@pytest.mark.unit
def test_an_empty_queue_summarizes_to_zero_not_to_an_error():
    summary = summarize_pending([])
    assert summary["count"] == 0
    assert summary["oldest_hours"] == 0.0
    assert summary["tasks"] == []


@pytest.mark.unit
def test_the_digest_line_names_the_count_and_the_oldest_age():
    line = digest_line({"count": 2, "oldest_hours": 26.4, "tasks": []})
    assert "2" in line
    assert "26" in line
    assert "approval" in line.lower()


@pytest.mark.unit
def test_the_digest_line_is_empty_when_nothing_is_parked():
    assert digest_line({"count": 0, "oldest_hours": 0.0, "tasks": []}) == ""


@pytest.mark.unit
def test_the_digest_line_is_singular_for_one_task():
    line = digest_line({"count": 1, "oldest_hours": 3.0, "tasks": []})
    assert "1 PR" in line
    assert "PRs" not in line


@pytest.mark.unit
def test_no_digest_is_published_when_nothing_is_parked():
    assert should_publish_digest(count=0, last_published_at=None, interval_h=6) is False


@pytest.mark.unit
def test_the_first_digest_publishes_immediately():
    assert should_publish_digest(count=2, last_published_at=None, interval_h=6) is True


@pytest.mark.unit
def test_a_second_digest_inside_the_interval_is_suppressed():
    recent = datetime.now(UTC) - timedelta(hours=1)
    assert should_publish_digest(count=2, last_published_at=recent, interval_h=6) is False


@pytest.mark.unit
def test_a_digest_after_the_interval_publishes_again():
    old = datetime.now(UTC) - timedelta(hours=7)
    assert should_publish_digest(count=2, last_published_at=old, interval_h=6) is True
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_approvals.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.core.approvals'`.

- [ ] **Step 3: Write the module**

Create `src/orchestrator/core/approvals.py`:

```python
"""Surfacing work parked at the human merge gate.

Praxis parks a reviewed-clean task at PASSED by design, so it never merges
without a human.  The documented way this product category dies is exactly
there: a review queue nobody looks at.  This module turns parked work into a
line on every surface a user already polls, plus a rate-limited SSE event.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from orchestrator.core.status_vocab import GATED_STATUSES


def _age_hours(updated_at: str | None) -> float:
    """Hours since a task last changed, or 0.0 if unparseable."""
    if not updated_at:
        return 0.0
    try:
        stamp = datetime.fromisoformat(str(updated_at))
    except ValueError:
        return 0.0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return max((datetime.now(UTC) - stamp).total_seconds() / 3600.0, 0.0)


def summarize_pending(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize tasks parked at the merge gate.

    Args:
        rows: Task rows; only those in ``GATED_STATUSES`` are counted.

    Returns:
        ``{"count", "oldest_hours", "tasks": [{"task_id", "title", "branch",
        "pr_url", "age_hours"}]}``, newest-parked last.
    """
    parked = [r for r in rows if str(r.get("status")) in GATED_STATUSES]
    tasks = sorted(
        (
            {
                "task_id": r.get("id"),
                "title": r.get("title"),
                "branch": r.get("branch_name"),
                "pr_url": r.get("pr_url"),
                "age_hours": _age_hours(r.get("updated_at")),
            }
            for r in parked
        ),
        key=lambda t: t["age_hours"],
        reverse=True,
    )
    return {
        "count": len(tasks),
        "oldest_hours": tasks[0]["age_hours"] if tasks else 0.0,
        "tasks": tasks,
    }


def digest_line(summary: dict[str, Any]) -> str:
    """Render a one-line summary, or an empty string when nothing is parked."""
    count = int(summary.get("count") or 0)
    if count == 0:
        return ""
    noun = "PR" if count == 1 else "PRs"
    oldest = int(summary.get("oldest_hours") or 0)
    return f"{count} {noun} awaiting your approval, oldest {oldest}h."


def should_publish_digest(
    count: int, last_published_at: datetime | None, interval_h: float
) -> bool:
    """True when a digest event is due.

    Nothing parked means no digest at all; a badge that appears when there is
    nothing to do trains people to ignore it.
    """
    if count <= 0:
        return False
    if last_published_at is None:
        return True
    elapsed = (datetime.now(UTC) - last_published_at).total_seconds() / 3600.0
    return elapsed >= interval_h
```

- [ ] **Step 4: Add the endpoint, the loop publisher, and the CLI**

Create `src/orchestrator/api/approvals.py` exposing
`GET /api/approvals/pending`, which selects every task in `GATED_STATUSES`
across all projects and returns `summarize_pending(rows)`. Register it in
`main.py`.

In `src/orchestrator/core/orchestrator.py`, add to `run_once` (after
`reconcile_runs`):

```python
        await self._publish_approvals_digest()
```

and the method:

```python
    async def _publish_approvals_digest(self) -> None:
        """Publish a rate-limited digest of work parked at the merge gate.

        Fire-and-forget: a digest failure must never wedge the loop.
        """
        from orchestrator.core.approvals import (
            should_publish_digest,
            summarize_pending,
        )

        try:
            rows = await self._tq._db.fetch_all(
                "SELECT * FROM tasks WHERE status IN ('passed')"
            )
            summary = summarize_pending([dict(r) for r in rows])
            interval_h = 6.0
            if self._effective_settings is not None:
                interval_h = await self._effective_settings.approvals_digest_interval_h()
            if not should_publish_digest(
                summary["count"], self._last_approvals_digest_at, interval_h
            ):
                return
            self._last_approvals_digest_at = datetime.now(UTC)
            self._bus.publish({"type": "approvals_digest", **summary})
        except Exception:  # noqa: BLE001 - a digest must never wedge the loop
            logger.exception("approvals digest failed")
```

Initialize `self._last_approvals_digest_at: datetime | None = None` in
`__init__`.

Add to `config/praxis.yaml`:

```yaml
# How often the approvals digest event may fire, in hours. Parked work is
# surfaced on poll_task, poll_plan, `praxis pending`, and the dashboard badge
# continuously; this only rate-limits the SSE event.
approvals_digest_interval_h: 6
```

with the matching `EffectiveSettings.approvals_digest_interval_h()` accessor
(same shape as `max_leaves_per_plan`).

Add `praxis pending` to `src/cli/main.py`:

```python
@app.command()
def pending() -> None:
    """List tasks parked at the human merge gate."""
    with _client() as client:
        data = _check_dict(client.get("/api/approvals/pending"))
    if not data["count"]:
        console.print("[green]Nothing awaiting approval.[/green]")
        return
    table = Table(title=f"{data['count']} awaiting approval")
    for column in ("Age", "Task", "Branch", "PR"):
        table.add_column(column)
    for task in data["tasks"]:
        table.add_row(
            f"{int(task['age_hours'])}h",
            task["title"] or task["task_id"],
            task["branch"] or "",
            task["pr_url"] or "",
        )
    console.print(table)
```

- [ ] **Step 5: Add the MCP tool and the digest line**

Create `tests/test_mcp_pending_approvals.py`:

```python
"""MCP is the primary surface; parked work must be visible there first."""

from unittest.mock import AsyncMock

import pytest

from mcp_server.server import pending_approvals_impl, poll_plan_impl, poll_task_impl


def _client(pending: dict) -> AsyncMock:
    client = AsyncMock()

    async def _get(path: str):
        if path == "/api/approvals/pending":
            return pending
        if path.startswith("/api/tasks/"):
            return {"task": {"id": "t1", "status": "passed", "pr_url": "u"}, "runs": []}
        if path.startswith("/api/plans/") and path.endswith("/tasks"):
            return []
        return {"status": "active", "opus_plan": None}

    client.get.side_effect = _get
    return client


@pytest.mark.unit
async def test_pending_approvals_returns_the_summary():
    client = _client({"count": 2, "oldest_hours": 26.0, "tasks": []})
    result = await pending_approvals_impl(client)
    assert result["count"] == 2


@pytest.mark.unit
async def test_poll_task_carries_the_digest_line_when_work_is_parked():
    client = _client({"count": 2, "oldest_hours": 26.0, "tasks": []})
    result = await poll_task_impl(client, "t1")
    assert "2 PRs awaiting your approval" in result["approvals"]


@pytest.mark.unit
async def test_poll_task_omits_the_digest_when_nothing_is_parked():
    client = _client({"count": 0, "oldest_hours": 0.0, "tasks": []})
    result = await poll_task_impl(client, "t1")
    assert result.get("approvals", "") == ""


@pytest.mark.unit
async def test_poll_plan_carries_the_digest_line():
    client = _client({"count": 1, "oldest_hours": 3.0, "tasks": []})
    result = await poll_plan_impl(client, "p1")
    assert "1 PR awaiting your approval" in result["approvals"]


@pytest.mark.unit
async def test_a_failing_digest_lookup_never_breaks_the_poll():
    """The digest is an add-on; poll must still answer if it fails."""
    client = AsyncMock()

    async def _get(path: str):
        if path == "/api/approvals/pending":
            message = "boom"
            raise RuntimeError(message)
        return {"task": {"id": "t1", "status": "passed", "pr_url": "u"}, "runs": []}

    client.get.side_effect = _get
    result = await poll_task_impl(client, "t1")
    assert result["task_id"] == "t1"
```

Implement `pending_approvals_impl` in `src/mcp_server/server.py`, register it as
an `@mcp.tool()`, and append the digest line to `poll_task_impl` and
`poll_plan_impl` in a `try`/`except` that swallows its own failure.

- [ ] **Step 6: Add the dashboard badge**

In `web/app.js`, handle the `approvals_digest` SSE event by setting a persistent
header badge with the count, and poll `/api/approvals/pending` once on page load
so the badge is correct before the first event. Clicking it filters the task
list to parked tasks. Add a `.approvals-badge` rule in `web/styles.css`.

- [ ] **Step 7: Run every test to verify they pass**

Run: `uv run pytest tests/test_approvals.py tests/test_mcp_pending_approvals.py -v`
Expected: PASS (16 tests).

- [ ] **Step 8: Mutation-check the rate limiter**

Temporarily change `should_publish_digest` to `return count > 0`.
Run: `uv run pytest tests/test_approvals.py::test_a_second_digest_inside_the_interval_is_suppressed -v`
Expected: FAIL. Restore and re-run to confirm PASS.

- [ ] **Step 9: Mutation-check the empty-queue rule**

Temporarily change `if count <= 0: return False` to `pass`.
Run: `uv run pytest tests/test_approvals.py::test_no_digest_is_published_when_nothing_is_parked -v`
Expected: FAIL. Restore and re-run to confirm PASS.

- [ ] **Step 10: Commit**

```bash
git add src/orchestrator/core/approvals.py src/orchestrator/api/approvals.py src/orchestrator/core/orchestrator.py src/orchestrator/core/effective_settings.py src/mcp_server/server.py src/cli/main.py web/ config/praxis.yaml tests/test_approvals.py tests/test_mcp_pending_approvals.py
git commit -m "feat(approvals): surface work parked at the merge gate everywhere

pending_approvals MCP tool, a digest line appended to poll_task and
poll_plan, praxis pending, a rate-limited approvals_digest SSE event, and a
persistent dashboard badge. Parking work nobody sees is the documented way
this product category dies."
```

---

### Task 8: Make the MCP surface readable and bounded

**Files:**
- Modify: `src/mcp_server/server.py`
- Test: `tests/test_mcp_surface.py`

**Depends on:** Task 7

MCP is the primary surface by design: the spec's decision 4 is that every doc,
demo, and default assumes the user drives Praxis from the assistant session they
already have open. Two things currently undercut that. `get_task_logs` returns
unbounded concatenated container output, so the tool you reach for to diagnose a
wedged task is the one most likely to be truncated by the client. And every
state-returning tool answers with a raw dict, so the transcript shows JSON where
one line of prose would do.

Scope note, so nobody re-litigates it: a 2 KB truncation limit applies to MCP
tool descriptions, and Praxis was measured against it on 2026-08-06. The largest
description is `execute_plan` at 942 B and all eleven are well clear. **There is
nothing to fix there; do not "audit" or shorten the descriptions.**

- [ ] **Step 1: Write the failing test**

Create `tests/test_mcp_surface.py`:

```python
"""The MCP surface must be readable in a transcript and bounded in size.

Claude Code warns above 10,000 tokens of MCP output and hard-caps at 25,000 by
default, so an unbounded log dump is truncated exactly when it is needed most.
"""

from unittest.mock import AsyncMock

import pytest

from mcp_server.server import (
    LOG_TAIL_CHARS,
    get_task_logs_impl,
    poll_plan_impl,
    poll_task_impl,
)


def _client(**routes) -> AsyncMock:
    client = AsyncMock()

    async def _get(path: str):
        for prefix, value in routes.items():
            if path.startswith(prefix.replace("_", "/")):
                return value
        return {}

    client.get.side_effect = _get
    return client


@pytest.mark.unit
async def test_short_logs_are_returned_whole():
    client = AsyncMock()
    client.get.return_value = {"runs": [{"logs": "boom\n"}]}
    result = await get_task_logs_impl(client, "t1")
    assert result["logs"] == "boom\n"
    assert result["truncated"] is False


@pytest.mark.unit
async def test_long_logs_are_tailed_not_headed():
    """Triage needs the END of the log; the failure is at the bottom."""
    client = AsyncMock()
    body = "".join(f"line {i}\n" for i in range(200_000))
    client.get.return_value = {"runs": [{"logs": body + "FINAL FAILURE\n"}]}
    result = await get_task_logs_impl(client, "t1")
    assert "FINAL FAILURE" in result["logs"]
    assert "line 0\n" not in result["logs"]


@pytest.mark.unit
async def test_tailed_logs_respect_the_cap():
    client = AsyncMock()
    client.get.return_value = {"runs": [{"logs": "x" * 5_000_000}]}
    result = await get_task_logs_impl(client, "t1")
    assert len(result["logs"]) <= LOG_TAIL_CHARS + 200


@pytest.mark.unit
async def test_truncation_is_announced_not_silent():
    """A silently clipped log makes the reader trust an incomplete picture."""
    client = AsyncMock()
    client.get.return_value = {"runs": [{"logs": "x" * 5_000_000}]}
    result = await get_task_logs_impl(client, "t1")
    assert result["truncated"] is True
    assert result["total_chars"] == 5_000_000
    assert "truncated" in result["logs"].lower()


@pytest.mark.unit
async def test_logs_from_every_run_are_still_concatenated_before_tailing():
    client = AsyncMock()
    client.get.return_value = {
        "runs": [{"logs": "first\n"}, {"logs": "second\n"}]
    }
    result = await get_task_logs_impl(client, "t1")
    assert result["logs"] == "first\nsecond\n"


@pytest.mark.unit
async def test_poll_task_leads_with_a_one_line_summary():
    client = AsyncMock()
    client.get.return_value = {
        "task": {
            "id": "t1",
            "title": "Add the widget",
            "status": "passed",
            "pr_url": "https://github.com/o/r/pull/7",
            "attempt": 2,
        },
        "runs": [],
    }
    result = await poll_task_impl(client, "t1")
    assert list(result)[0] == "summary"
    assert "Add the widget" in result["summary"]
    assert "awaiting_merge" in result["summary"]


@pytest.mark.unit
async def test_poll_plan_summary_counts_leaves_by_state():
    client = AsyncMock()

    async def _get(path: str):
        if path.endswith("/tasks"):
            return [
                {"id": "1", "title": "a", "status": "merged"},
                {"id": "2", "title": "b", "status": "merged"},
                {"id": "3", "title": "c", "status": "passed"},
                {"id": "4", "title": "d", "status": "pending"},
            ]
        return {"status": "active", "opus_plan": None}

    client.get.side_effect = _get
    result = await poll_plan_impl(client, "p1")
    assert list(result)[0] == "summary"
    assert "2 of 4" in result["summary"]
    assert "1 awaiting approval" in result["summary"]


@pytest.mark.unit
async def test_an_errored_response_still_leads_with_a_summary():
    from mcp_server.client import PraxisClientError

    client = AsyncMock()
    client.get.side_effect = PraxisClientError("connection refused")
    result = await poll_task_impl(client, "t1")
    assert "summary" in result
    assert "connection refused" in result["summary"]
```

Read `src/mcp_server/server.py` and `src/mcp_server/client.py` first and match
`_error`'s real shape and `PraxisClientError`'s real constructor before running.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_mcp_surface.py -v`
Expected: FAIL with `ImportError: cannot import name 'LOG_TAIL_CHARS'`.

- [ ] **Step 3: Bound `get_task_logs`**

In `src/mcp_server/server.py`, add near the other module constants:

```python
# Claude Code warns above 10,000 tokens of MCP output and caps at 25,000 by
# default (roughly 100 KB of text). Container logs for a retried task exceed
# that easily, so the tool tails rather than dumps: for failure triage the
# useful end of a log is the bottom, not the top. Raising the client-side limit
# would not help; the head of a 5 MB log is noise either way.
LOG_TAIL_CHARS = 40_000
```

Replace `get_task_logs_impl` with:

```python
async def get_task_logs_impl(client: Any, task_id: str) -> dict[str, Any]:
    """Return the TAIL of a task's agent-run logs (inline failure triage).

    Logs are concatenated across every run for the task, then clipped to the
    last ``LOG_TAIL_CHARS`` characters.  Truncation is announced in the payload
    and inline in the text: a silently clipped log invites the reader to trust
    an incomplete picture, which is worse than a short one.
    """
    try:
        data = await client.get(f"/api/tasks/{task_id}")
    except PraxisClientError as exc:
        return _error(exc)
    runs = data.get("runs", [])
    logs = "".join(str(run.get("logs") or "") for run in runs)
    total = len(logs)
    truncated = total > LOG_TAIL_CHARS
    if truncated:
        logs = (
            f"[truncated: showing the last {LOG_TAIL_CHARS} of {total} "
            "characters; the tail is what matters for triage]\n"
            + logs[-LOG_TAIL_CHARS:]
        )
    return {
        "task_id": task_id,
        "logs": logs,
        "truncated": truncated,
        "total_chars": total,
    }
```

- [ ] **Step 4: Lead every state-returning tool with a summary**

Add a summary builder next to the other helpers:

```python
def _task_summary(task: dict[str, Any]) -> str:
    """One human-readable line for a task, as the first key of the payload."""
    title = task.get("title") or task.get("id") or "task"
    status = _TASK_STATUS_MAP.get(task.get("status", ""), task.get("status"))
    parts = [f"{title}: {status}"]
    if task.get("pr_url"):
        parts.append(str(task["pr_url"]))
    attempt = task.get("attempt")
    if isinstance(attempt, int) and attempt > 1:
        parts.append(f"attempt {attempt}")
    return ", ".join(parts)


def _plan_summary(tasks: list[dict[str, Any]]) -> str:
    """One human-readable line for a plan's leaf states."""
    total = len(tasks)
    merged = sum(1 for t in tasks if t.get("status") == "merged")
    gated = sum(1 for t in tasks if str(t.get("status")) in _GATED_STATUSES)
    failed = sum(1 for t in tasks if t.get("status") == "failed")
    parts = [f"{merged} of {total} leaves merged"]
    if gated:
        parts.append(f"{gated} awaiting approval")
    if failed:
        parts.append(f"{failed} failed")
    return ", ".join(parts)
```

Then make `summary` the FIRST key of the returned dict in `poll_task_impl`,
`poll_plan_impl`, `dispatch_task_impl`, `execute_plan_impl`,
`pending_approvals_impl` (Task 7), and `list_projects_impl`. Python dicts
preserve insertion order and the MCP client renders them in that order, so
placing it first is what puts the line at the top of the transcript.

Give `_error` a `summary` too, so a failed call is readable as well:

```python
def _error(exc: PraxisClientError) -> dict[str, Any]:
    return {"summary": f"Praxis error: {exc}", "error": str(exc)}
```

Match the existing `_error` return shape; add the key, do not change the rest.

- [ ] **Step 5: Give the server instructions**

`FastMCP("praxis")` currently passes no `instructions`, so a fresh client is
told nothing about how to drive Praxis beyond the
`praxis://guide/orchestration` resource, which only helps if the client fetches
it. Server instructions truncate at 2 KB, so keep this short and put the
pointer first:

```python
mcp = FastMCP(
    "praxis",
    instructions=(
        "Praxis delegates implementation to another provider or harness and "
        "hands back a reviewed pull request. Read the praxis://guide/orchestration "
        "resource before your first dispatch. Typical loop: get_project to read a "
        "repo's configured worker, dispatch_task or execute_plan to delegate, "
        "poll_task or poll_plan until a task reports awaiting_merge, then relay "
        "the PR URL to the human for approval. Praxis never merges without them."
    ),
)
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `uv run pytest tests/test_mcp_surface.py tests/test_mcp_server.py -v`
Expected: PASS. Existing MCP tests assert on individual keys rather than whole
dicts, so adding a leading key should not break them; where one compares a whole
payload, add `summary` to the expectation rather than dropping the key.

- [ ] **Step 7: Mutation-check the tail direction**

Temporarily change `logs[-LOG_TAIL_CHARS:]` to `logs[:LOG_TAIL_CHARS]`.
Run: `uv run pytest tests/test_mcp_surface.py::test_long_logs_are_tailed_not_headed -v`
Expected: FAIL. Restore and re-run to confirm PASS. The direction is the whole
point: the head of a failing run's log is startup banner.

- [ ] **Step 8: Mutation-check the truncation announcement**

Temporarily drop the `[truncated: ...]` prefix, keeping the slice.
Run: `uv run pytest tests/test_mcp_surface.py::test_truncation_is_announced_not_silent -v`
Expected: FAIL. Restore and re-run to confirm PASS.

- [ ] **Step 9: Verify against a live client**

Restart the MCP server in your client and call `poll_plan` on any real plan.
Expected: the transcript's first rendered line is the summary sentence, not
`{"plan_id": ...`. If it still leads with JSON, the `summary` key is not first
in the dict; check insertion order rather than adding formatting.

- [ ] **Step 10: Add the gotcha**

Append to `docs/gotchas.md`:

```markdown
- **MCP payloads lead with a `summary` key, and `get_task_logs` tails**:
  every state-returning MCP tool puts a one-line human summary first in its
  returned dict, because dict insertion order is what the client renders and the
  first line is what a reader actually sees. `get_task_logs` clips to the LAST
  `LOG_TAIL_CHARS` (40 KB) and says so both in the payload (`truncated`,
  `total_chars`) and inline in the text: Claude Code warns above 10,000 tokens of
  MCP output and caps at 25,000 by default, so an unbounded dump is truncated by
  the client exactly when a task has run long enough to wedge. Tail, never head:
  the top of a failing run's log is startup banner. Note that MCP tool
  DESCRIPTIONS also truncate, at 2 KB each, but Praxis was measured on
  2026-08-06 and its largest is 942 B, so there is nothing to fix there.
```

Add a matching one-line CLAUDE.md index entry.

- [ ] **Step 11: Commit**

```bash
git add src/mcp_server/server.py tests/test_mcp_surface.py docs/gotchas.md CLAUDE.md
git commit -m "feat(mcp): bound get_task_logs and lead every payload with a summary

get_task_logs tailed to 40 KB with the truncation announced, because the
client caps MCP output at 25,000 tokens and an unbounded dump fails exactly
when a task has run long enough to need diagnosing. Every state-returning
tool now leads with a one-line summary so the transcript reads as prose
rather than JSON, and the server carries instructions pointing at the
orchestration guide."
```

---

### Task 9: Close out Phase A with a timed fresh-machine walkthrough

**Files:**
- Create: `docs/walkthrough-15min.md`
- Modify: `docs/gotchas.md`
- Modify: `CLAUDE.md`

**Depends on:** Task 1, Task 2, Task 3, Task 5, Task 6, Task 7, Task 8

The spec attaches a number to simplicity: clone to first reviewed PR in 15
minutes or less, measured by walkthrough. This task measures it.

- [ ] **Step 1: Prepare a genuinely clean environment**

Either a fresh VM or, at minimum:

```bash
docker compose down -v
docker image rm opencode-agent:latest agy-agent:latest 2>/dev/null || true
git clone https://github.com/adiatmaja/praxis.git /tmp/praxis-fresh
cd /tmp/praxis-fresh
```

Do not carry over `.env`, the data volume, or the agent images. The number is
meaningless if the slow steps are already done.

- [ ] **Step 2: Start the clock and run the documented path**

Record the screen. Run exactly what a new user would run, and nothing else:

```bash
uv venv && uv sync
uv run praxis init
```

Follow the prompts. When init finishes, note the elapsed time.

- [ ] **Step 3: Drive a first task through to a reviewed PR**

Register a small target repo and dispatch one task, either from the dashboard or
from an MCP client using the printed snippet. Wait for the task to reach
`awaiting_merge`. Stop the clock when the reviewed PR link is in your hand.

- [ ] **Step 4: Write the walkthrough with the real number**

Create `docs/walkthrough-15min.md` recording: the machine and OS, every command
run in order, the elapsed time per phase (`uv sync`, image build, compose up,
first dispatch, first review), the total, and every place you had to leave the
documented path.

If the total exceeds 15 minutes, do not round it down. Write the real number,
identify the slowest step, and either fix it or state plainly in the doc that
the 15-minute claim is not currently met and what would have to change. An
honest 22 minutes is a usable artifact; a fabricated 14 is not.

- [ ] **Step 5: Fix what the walkthrough exposed**

Every deviation from the documented path is a bug in this phase's work. Fix it,
then re-run steps 1 through 4. Iterate until either the number is met or the doc
explains why it is not.

- [ ] **Step 6: Add the gotchas**

Append to `docs/gotchas.md`:

```markdown
- **`praxis doctor` is the front door to every problem** 
  `core/doctor.py` registers eleven read-only checks, each with a fix hint;
  `core/doctor_probes.py` holds the pure decision logic (facts in, verdict out)
  so every check is testable with no Docker, network, or filesystem. Two checks
  exist specifically to convert this project's oldest silent failures into red
  lights: `agent_image_freshness` (an image older than its `entrypoint.sh` runs
  stale logic while the source looks current) and `callback_url` (a port
  mismatch 404s every agent callback, so tasks only ever finish via reconcile
  and get marked failed even on success). A raising probe becomes a RED result
  rather than an exception, and any RED makes the CLI exit non-zero; AMBER (for
  example "local mode, no GitHub credential") does not. Add a check to `CHECKS`
  and its probe together, or `run_checks` returns a RED "no probe registered".
- **`praxis init` is re-runnable and never eats your `.env`**: `cli/init.py`
  merges only the keys it manages (`_MANAGED_KEYS`) into an existing `.env`,
  preserving every other key, its position, and every comment. It ends by
  running doctor and exits with doctor's code, so "init succeeded" and "the
  installation works" are the same claim.
- **The approvals digest is rate-limited but the surfaces are not** 
  `core/approvals.should_publish_digest` gates only the `approvals_digest` SSE
  event (default every 6h, `approvals_digest_interval_h`). The MCP
  `pending_approvals` tool, the digest line on `poll_task`/`poll_plan`, `praxis
  pending`, and the dashboard badge all read live. Nothing parked means no
  event at all: a badge that appears when there is nothing to do trains people
  to ignore it, which defeats the purpose.
```

- [ ] **Step 7: Add three CLAUDE.md index lines**

Add one-line entries mirroring the three gotchas, and add the new commands to
the Commands section of `CLAUDE.md`:

```bash
# Setup (one command, idempotent, ends by verifying)
uv run praxis init

# Diagnose (read-only, exits non-zero on any red)
uv run praxis doctor

# See what is parked at the merge gate
uv run praxis pending
```

- [ ] **Step 8: Verify the whole gate**

```bash
uv run ruff format src/ tests/
uv run ruff check --fix src/ tests/
uv run mypy src/ --ignore-missing-imports
uv run pytest --cov=orchestrator --cov-fail-under=80 -q
```

Expected: all clean.

- [ ] **Step 9: Commit**

```bash
git add docs/walkthrough-15min.md docs/gotchas.md CLAUDE.md
git commit -m "docs: record the timed fresh-machine walkthrough

Real measured number, per-phase timings, and every deviation from the
documented path. Plus the doctor, init, and approvals-digest gotchas."
```

### Phase A execution record (2026-08-06)

Executed 2026-08-06, tasks 1 to 8 complete; task 9's timed walkthrough is NOT
done and is the phase's one open deliverable (see below). Gate at the end: ruff
format and check clean, mypy clean on 88 files, 1332 tests passing at 90.55
percent coverage.

Task commits in order: `2fca5e9`, `2a43698`, `b482740`, `7244044`, `3cc3de9`,
`5d0c1a0`, `496a7ac`, `831508d`. Six further commits came out of review
findings: `fb624d3`, `89893e5`, `1e2cb49`, `918e466`, `217bff5`, plus `fa62e3f`,
which cleared the first item deferred out of the engine plan's Phase A.

Tasks 3 and 4 were dispatched CONCURRENTLY (zero file overlap; task 4's declared
dependency on 1, 2, and 3 is conceptual, not a code import). That worked, but it
produced a defect neither agent could see: task 4's display label contained the
literal `config/praxis.yaml`, which task 2's grep guard forbids in any module
under `src/`. Both agents' scoped test runs were green and only the
orchestrator's full-suite run at the join point caught it (`89893e5`). Parallel
dispatch here requires a full-suite run at every join.

**Defects in this plan's own verbatim code, corrected during execution.** Fix
them here before anyone re-runs these tasks.

1. Task 1's test used `@pytest.mark.parametrize("name,tag", ...)`. This repo's
   ruff enforces PT006, which requires a tuple, so Step 5 could not have gone
   green as written. Shipped as `("name", "tag")`.
2. Task 1 Step 5 says "PASS (13 parametrized tests)". The Step 1 code defines 11.
3. Task 2's `test_a_missing_file_yields_empty_settings_not_a_crash` asserts
   `== {}` but cannot pass: `load_yaml_settings` folds every `PRAXIS_*` env var
   into its result and the test sets `PRAXIS_CONFIG_PATH` itself. Fixed in the
   code, by excluding exactly that variable from the overlay, since it is a
   pointer to the file rather than a setting inside it.
4. Task 2's test calls `EffectiveSettings.max_leaves_per_plan()`, which does not
   exist. Substituted `escalation_policy(None)`.
5. Task 3's `api/presets.py` snippet used `require_token` and
   `from orchestrator.main import effective_settings`. The real pattern is
   `verify_token` at router level plus `request.app.state.effective_settings`;
   the plan's import would have been circular.
6. Task 4's registry shipped three bad imports: `asyncio` and `dataclasses.field`
   unused, and `Callable` from `typing` where ruff UP035 wants `collections.abc`.
7. Task 6's MCP snippet used `PRAXIS_API` and `PRAXIS_TOKEN`. The client reads
   `PRAXIS_BASE_URL` and `PRAXIS_AUTH_TOKEN`, so an operator pasting the snippet
   got a silent failure. This was the single worst defect in the plan text.
8. Task 8's test calls `PraxisClientError("connection refused")` with one
   argument; the real signature takes two, `(code, message)`.
9. Task 8's proposed `_error` replacement dropped the `message` key and changed
   `error` from a machine-readable code to a stringified exception, which would
   have broken existing callers. Kept the original shape and added `summary`.

**Vacuous tests the plan specified, exposed only by mutation.** Task 6's
`test_local_mode_writes_no_github_token` passed with its own mutation applied,
because `build_env_file({"AUTH_TOKEN": "t"})` omits the key entirely while
`init()` always passes it. Task 4's `test_the_expected_checks_are_registered`
pinned only the set, not the registry order its own docstring promises. The two
`image_is_stale` tests never touched the equality boundary, so `<` and `<=` were
interchangeable.

**Design defects found by review, not present in the plan text.** Doctor's
per-check fix hints were unreachable: `CheckResult.__post_init__` stamped a
generic pointer at construction, so all eleven of task 5's probes would have
shipped the same docs link with every test green. `run_checks`'s `**context`
would have handed every probe every fact, turning each check red for the wrong
reason. `GET /api/doctor` could 500, because fact gathering ran outside the
per-probe exception shield. `agent_image_freshness` returned a GREEN identical
to a verified pass when it had compared nothing, which it always had in a
container because no compose file mounted the entrypoint sources. `parse_env`
kept an inline comment that both python-dotenv and Docker Compose strip, so
reusing an existing `AUTH_TOKEN` wrote a corrupted value back and every
configured MCP client would 401 against a green doctor row.

**Open, and the reason Phase A is not closed:**

Task 9's timed fresh-machine walkthrough was not run. It needs a human with a
clock, a screen recording, a genuinely cold machine, and a real dispatch through
to a reviewed PR. It also cannot be run as written today, because Step 1 clones
`https://github.com/adiatmaja/praxis.git` and none of this work is pushed, so a
fresh clone would measure a Praxis without any of it. Either push first or clone
the local repo. `docs/walkthrough-15min.md` is deliberately NOT written, because
a fabricated number is worse than none.

**Deferred, in priority order:**

1. `DEFAULT_WORKER_HARNESS` and `DEFAULT_WORKER_MODEL` are written to `.env` by
   `praxis init` but forwarded into the orchestrator container by NEITHER compose
   file, so the container resolves them from the mounted YAML and the operator's
   preset choice is inert for the deployment `init` just started. `LM_STUDIO_URL`
   IS forwarded, so the two halves of one preset disagree. This is the largest
   remaining silent failure in the phase's own surface.
2. `init()` itself has 0 percent test coverage; only its pure helpers are tested.
3. `load_yaml_settings` is silent when the configured path does not exist, so a
   typo reverts every default with no log line. It runs on every `_get_yaml()`,
   so the log level and frequency need thought.
4. `.env.example` does not document `PRAXIS_CONFIG_PATH`.
5. `core/capabilities.py` has its own module-relative resolver for
   `config/model_capabilities.json`, invisible to the grep guard.
6. `praxis init` assumes its CWD is the repo root; run elsewhere it writes a
   secret-bearing `.env` into that directory.
7. The dashboard model dropdown can list the same model twice, once as a preset
   option and once under "Other (from LM Studio)".

**Phase A is complete.** Per the cross-plan execution order, the next work is
the benchmark plan's Phase A, then the engine plan's Phases B and C, then the
benchmark's Phases B and C, before returning here for Phase B.

---

## Phase B: documentation restructure, framing, and launch

Phase B runs LAST. Task 13 (the launch checklist) is gated on the benchmark
plan's published report.

**Target shape.** The user-facing corpus roughly halves, from about 2,400 lines
to about 1,200. Current sizes for reference: `README.md` 266,
`docs/architecture.md` 284, `docs/deployment.md` 577, `docs/workflow.md` 183,
`docs/workflow-diagram.md` 121, `docs/mcp.md` 159, `docs/positioning.md` 207,
`docs/social-launch-drafts.md` 209, `docs/decompose.md` 141,
`docs/open-weight-models-*.md` 463 combined.

**Kill criterion, applied to every page:** a sentence that does not change what
the reader does next is cut.

### Task 10: Restructure the user-facing documentation

**Files:**
- Rewrite: `README.md`
- Create: `docs/getting-started.md`
- Create: `docs/reference.md`
- Create: `docs/internal/positioning.md`, `docs/internal/social-launch-drafts.md`, `docs/internal/workflow-diagram.md`
- Rewrite as stubs: `docs/architecture.md`, `docs/workflow.md`, `docs/deployment.md`, `docs/mcp.md`, `docs/decompose.md`, `docs/positioning.md`, `docs/social-launch-drafts.md`, `docs/workflow-diagram.md`, `docs/open-weight-models-complete.md`, `docs/open-weight-models-lmstudio.md`
- Test: `tests/test_docs_shape.py`

**Depends on:** Task 9, and the benchmark plan's Task 17 (the report must exist to be linked)

- [ ] **Step 1: Write the failing test**

Create `tests/test_docs_shape.py`:

```python
"""The docs corpus has a budget, and every link in it must resolve.

Budgets are the enforcement mechanism for the kill criterion: a sentence that
does not change what the reader does next is cut.
"""

import re
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]

# Pages a user reads. Contributor-facing docs (gotchas, superpowers, bench,
# internal) are deliberately excluded from the budget.
USER_FACING = (
    "README.md",
    "docs/getting-started.md",
    "docs/reference.md",
    "docs/decomposition-standard.md",
)

STUBBED = (
    "docs/architecture.md",
    "docs/workflow.md",
    "docs/deployment.md",
    "docs/mcp.md",
    "docs/decompose.md",
    "docs/positioning.md",
    "docs/social-launch-drafts.md",
    "docs/workflow-diagram.md",
    "docs/open-weight-models-complete.md",
    "docs/open-weight-models-lmstudio.md",
)


def _lines(rel: str) -> int:
    return len((REPO / rel).read_text(encoding="utf-8").splitlines())


@pytest.mark.unit
def test_the_readme_is_within_budget():
    assert _lines("README.md") <= 120


@pytest.mark.unit
def test_getting_started_is_within_budget():
    assert _lines("docs/getting-started.md") <= 220


@pytest.mark.unit
def test_the_user_facing_corpus_is_within_budget():
    total = sum(_lines(p) for p in USER_FACING)
    assert total <= 1200, f"user-facing docs are {total} lines, budget is 1200"


@pytest.mark.unit
def test_the_readme_carries_exactly_one_diagram():
    """Two large diagrams plus a five-row tier table was the old README."""
    text = (REPO / "README.md").read_text(encoding="utf-8")
    fences = re.findall(r"^```", text, re.MULTILINE)
    diagram_blocks = text.count("```\n┌") + text.count("```text")
    assert diagram_blocks <= 1, "the README carries at most one diagram"
    assert len(fences) % 2 == 0, "unbalanced code fence in the README"


@pytest.mark.unit
def test_the_readme_states_the_fifteen_minute_claim():
    assert "15 minute" in (REPO / "README.md").read_text(encoding="utf-8").lower()


@pytest.mark.unit
def test_the_readme_links_the_benchmark_report():
    assert "docs/bench/" in (REPO / "README.md").read_text(encoding="utf-8")


@pytest.mark.unit
def test_the_readme_links_the_decomposition_standard():
    text = (REPO / "README.md").read_text(encoding="utf-8")
    assert "docs/decomposition-standard.md" in text


@pytest.mark.unit
@pytest.mark.parametrize("path", STUBBED)
def test_every_moved_page_leaves_a_pointer_stub(path):
    """Deep links from code comments and the dashboard must not 404."""
    text = (REPO / path).read_text(encoding="utf-8")
    assert len(text.splitlines()) <= 15, f"{path} should be a stub"
    assert "moved" in text.lower()
    assert "docs/" in text


@pytest.mark.unit
@pytest.mark.parametrize("path", ["positioning.md", "social-launch-drafts.md", "workflow-diagram.md"])
def test_the_internal_docs_moved(path):
    assert (REPO / "docs" / "internal" / path).is_file()


@pytest.mark.unit
def test_every_relative_markdown_link_in_the_corpus_resolves():
    """A restructure that breaks its own links is worse than no restructure."""
    broken: list[str] = []
    link_re = re.compile(r"\]\((?!https?://|#|mailto:)([^)#]+)")
    for rel in (*USER_FACING, *STUBBED):
        source = REPO / rel
        for match in link_re.finditer(source.read_text(encoding="utf-8")):
            target = (source.parent / match.group(1)).resolve()
            if not target.exists():
                broken.append(f"{rel} -> {match.group(1)}")
    assert broken == [], f"broken links: {broken}"


@pytest.mark.unit
def test_every_troubleshooting_entry_starts_from_doctor():
    """One front door for problems, per the spec."""
    text = (REPO / "docs" / "reference.md").read_text(encoding="utf-8")
    start = text.lower().find("## troubleshooting")
    assert start != -1, "reference.md needs a Troubleshooting section"
    section = text[start:]
    entries = re.findall(r"^###\s+(.+)$", section, re.MULTILINE)
    assert entries, "the troubleshooting section needs entries"
    for entry in entries:
        block_start = section.find(f"### {entry}")
        block_end = section.find("###", block_start + 1)
        block = section[block_start : block_end if block_end != -1 else len(section)]
        assert "praxis doctor" in block, (
            f"troubleshooting entry {entry!r} must start from `praxis doctor`"
        )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_docs_shape.py -v`
Expected: FAIL. The README is 266 lines, `docs/getting-started.md` and
`docs/reference.md` do not exist, and nothing is stubbed.

- [ ] **Step 3: Write `docs/reference.md` first**

Write the destination before emptying the sources, so nothing is lost in
transit. `docs/reference.md` absorbs, in this order:

1. **Configuration**: every key in `config/praxis.yaml` and every environment
   variable, each with its default and one sentence on when to change it. Source:
   `config/praxis.yaml`, `src/orchestrator/config.py`, `.env.example`.
2. **Model tiers**: the five-row tier table currently in the README, moved here
   verbatim.
3. **API**: the endpoint list from `docs/deployment.md`.
4. **MCP tools**: the tool list from `docs/mcp.md`, one line each.
5. **Deployment modes**: local dev, production compose, the hosted Caddy profile,
   and local git evaluation mode. Condensed from `docs/deployment.md`.
6. **Security**: the trust model, plus the paragraph on local git mode required
   by the spec (bare repos are operator-provided paths, containers still run
   non-root, `PRAXIS_BENCH_*` flags are refused outside bench mode).
7. **Troubleshooting**: one `###` entry per symptom. **Every entry begins with
   "Run `praxis doctor`."** followed by which check goes red and its hint, then
   any detail doctor cannot give. Absorb the user-facing entries from
   `docs/deployment.md` and `docs/gotchas.md`; leave contributor-facing gotchas
   in `docs/gotchas.md`.

The test enforces point 7 mechanically. Write the section so it passes for the
right reason, not by inserting the phrase decoratively.

- [ ] **Step 4: Write `docs/getting-started.md`**

At most 220 lines. Structure:

1. **The 15-minute path**: prerequisites (Docker, `uv`, a planner CLI), then
   `uv venv && uv sync`, `praxis init`, register a repo, dispatch one task,
   approve the PR. Every command copy-pasteable. Link `docs/walkthrough-15min.md`
   for the recorded run.
2. **Optional roads**, one short section each, each ending in "run `praxis
   doctor` to confirm": a local GPU worker via LM Studio; the agy harness and its
   one-time interactive login; GitHub App credentials instead of a PAT; the
   hosted Caddy profile; evaluating with a local `file://` repo and no GitHub
   credential at all.

- [ ] **Step 5: Rewrite the README**

At most 120 lines:

- The one-liner from spec 5.1, verbatim: "Praxis lets the AI assistant you
  already use delegate implementation to any other provider or harness, and hands
  you back a reviewed pull request. Tasks are sized to what the implementing
  model can actually do."
- Three sentences of what it is.
- ONE compact ASCII diagram. Build it with a column-precise script rather than by
  hand; hand-aligned box art drifts.
- The 15-minute quickstart: the four commands, and the claim stated as a number.
- ONE real example session transcript: an MCP client dispatching a task, the
  worker PR appearing, review passing, the human approving. Use a real
  transcript, not an invented one.
- Links: getting started, reference, the decomposition standard, the benchmark
  report, gotchas.

Everything else moves or is cut. The two large diagrams and the five-row model
tier table currently in the README go to `docs/reference.md`.

- [ ] **Step 6: Move the internal docs**

```bash
mkdir -p docs/internal
git mv docs/positioning.md docs/internal/positioning.md
git mv docs/social-launch-drafts.md docs/internal/social-launch-drafts.md
git mv docs/workflow-diagram.md docs/internal/workflow-diagram.md
```

`docs/gotchas.md` stays at `docs/` (it is contributor-facing and the CLAUDE.md
index points at it).

- [ ] **Step 7: Leave a stub at every old path**

For each path in the test's `STUBBED` list, write a file of at most 15 lines:

```markdown
# Moved

This page has moved. It is kept for one release so existing links keep working.

- Setup and the 15-minute path: [`docs/getting-started.md`](getting-started.md)
- Configuration, API, MCP tools, deployment, troubleshooting: [`docs/reference.md`](reference.md)
- How tasks are sized: [`docs/decomposition-standard.md`](decomposition-standard.md)

Remove this stub after the next release.
```

Adjust the pointer list per page so it names the actual destination of that
page's content. For `docs/positioning.md` and the other two moved internals,
point at `internal/<name>.md`.

- [ ] **Step 8: Run the test to verify it passes**

Run: `uv run pytest tests/test_docs_shape.py -v`
Expected: PASS. If the corpus budget test fails, cut content; do not raise the
budget. The budget is the mechanism.

- [ ] **Step 9: Check the deep links from code and dashboard**

```bash
grep -rn "docs/deployment.md\|docs/architecture.md\|docs/workflow.md\|docs/mcp.md" src/ web/ README.md CLAUDE.md
```

Every hit either resolves to a stub (fine for one release) or should be updated
to the new destination now. Update the ones in `src/` and `web/`, since those
are the ones that will still be there after the stubs are removed.

- [ ] **Step 10: Update the CLAUDE.md documentation index**

Replace the Documentation section of `CLAUDE.md` with the new shape: README,
getting-started, reference, decomposition-standard, bench report, gotchas,
`docs/internal/`, and the superpowers specs and plans. Keep the specs and plans
entries as they are.

- [ ] **Step 11: Add a link checker to CI, if it is cheap**

The spec permits this only if it costs nothing meaningful.
`tests/test_docs_shape.py::test_every_relative_markdown_link_in_the_corpus_resolves`
already runs in the existing pytest job, which satisfies the intent at zero
additional CI cost. Do NOT add a separate workflow.

- [ ] **Step 12: Commit**

```bash
git add README.md docs/ CLAUDE.md tests/test_docs_shape.py
git commit -m "docs: restructure the user-facing corpus

README down to 120 lines with one diagram, getting-started for the 15-minute
path and the optional roads, reference for config, API, MCP, deployment,
security, and troubleshooting. Internal docs move to docs/internal, every
old path leaves a one-release pointer stub, and a test enforces the budgets,
the link integrity, and that every troubleshooting entry starts from
praxis doctor."
```

---

### Task 11: Framing

**Files:**
- Modify: `README.md`
- Modify: `docs/internal/positioning.md`
- Modify: `src/mcp_server/server.py` (server description)
- Test: `tests/test_framing.py`

**Depends on:** Task 10

- [ ] **Step 1: Write the failing test**

Create `tests/test_framing.py`:

```python
"""The framing decisions from spec 5.1 are checkable, so check them.

Framing drifts one careless sentence at a time. This is a ratchet.
"""

from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]

# Category words the spec says to avoid, because they promise something Praxis
# is not and attract the wrong comparison.
BANNED = ("orchestrator platform", "agent swarm", "autonomous dev team")

# Words the spec says to use.
PREFERRED = (
    "delegation engine",
    "cross-provider",
    "capability-aware",
    "reviewed pull request",
)

SURFACES = ("README.md", "docs/internal/positioning.md")


@pytest.mark.unit
@pytest.mark.parametrize("path", SURFACES)
@pytest.mark.parametrize("phrase", BANNED)
def test_no_surface_uses_a_banned_category_word(path, phrase):
    text = (REPO / path).read_text(encoding="utf-8").lower()
    assert phrase not in text


@pytest.mark.unit
def test_the_readme_uses_the_preferred_vocabulary():
    text = (REPO / "README.md").read_text(encoding="utf-8").lower()
    hits = [p for p in PREFERRED if p in text]
    assert len(hits) >= 3, f"README uses only {hits} of the preferred words"


@pytest.mark.unit
def test_the_readme_opens_with_the_canonical_one_liner():
    lines = [
        line.strip()
        for line in (REPO / "README.md").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    opening = " ".join(lines[:6]).lower()
    assert "the ai assistant you already use" in opening
    assert "reviewed pull request" in opening
    assert "sized to what the implementing model can actually do" in opening


@pytest.mark.unit
def test_reliability_leads_and_cost_does_not():
    """Cost is a consequence, never the anchor. Existing project canon."""
    text = (REPO / "README.md").read_text(encoding="utf-8").lower()
    first_third = text[: len(text) // 3]
    assert "cost" not in first_third or "consequence" in first_third


@pytest.mark.unit
def test_the_mcp_server_description_matches_the_one_liner():
    text = (REPO / "src" / "mcp_server" / "server.py").read_text(encoding="utf-8")
    assert "reviewed pull request" in text.lower()


@pytest.mark.unit
def test_no_em_dashes_in_any_user_facing_doc():
    """Project-wide style rule; a restructure is exactly when it slips."""
    offenders = []
    for path in (
        "README.md",
        "docs/getting-started.md",
        "docs/reference.md",
        "docs/decomposition-standard.md",
        "docs/internal/positioning.md",
    ):
        if "" in (REPO / path).read_text(encoding="utf-8"):
            offenders.append(path)
    assert offenders == [], f"em dash found in: {offenders}"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_framing.py -v`
Expected: FAIL on the one-liner, the vocabulary, and very likely the em-dash
check (the current README and positioning doc predate that rule).

- [ ] **Step 3: Apply the framing**

- README: open with the canonical one-liner verbatim from spec 5.1.
- `docs/internal/positioning.md`: keep the identity sentence per the roadmap, but
  shift the lead emotion from cost to reliability. The three things that lead:
  sized tasks that succeed, no silent failures, and implementation that a
  mid-session rate limit cannot destroy. Cost stays as a consequence, stated
  once, late.
- Replace every banned category word with a preferred one. "Orchestrator
  platform" becomes "delegation engine"; "agent swarm" and "autonomous dev team"
  have no replacement, they are simply cut.
- `src/mcp_server/server.py`: set the FastMCP server description to the
  one-liner so an MCP client's tool listing says the same thing the README does.
- Remove every em dash from the listed files. Replace with a comma, a colon, or
  a semicolon, whichever the sentence actually wants.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_framing.py -v`
Expected: PASS.

- [ ] **Step 5: Mutation-check the banned-word ratchet**

Temporarily insert the phrase "orchestrator platform" into `README.md`.
Run: `uv run pytest tests/test_framing.py -v -k banned`
Expected: FAIL. Remove the phrase and re-run to confirm PASS.

- [ ] **Step 6: Commit**

```bash
git add README.md docs/internal/positioning.md src/mcp_server/server.py tests/test_framing.py
git commit -m "docs: apply the canonical framing

Reliability leads, cost is a consequence stated once and late. The one-liner
is identical in the README and the MCP server description. A test ratchets
the banned category words, the preferred vocabulary, and the no-em-dash rule."
```

---

### Task 12: The demo artifact

**Files:**
- Create: `docs/demo.md`
- Create: `docs/assets/demo.gif` or `docs/assets/demo.cast`
- Modify: `README.md`

**Depends on:** Task 11

- [ ] **Step 1: Rehearse the demo path**

The demo is 90 seconds and shows one thing: a task delegated from an assistant
session comes back as a reviewed PR. Rehearse it until it runs clean:

1. An MCP client session, already connected to Praxis.
2. Ask it to implement one small, real change in a real repo.
3. It calls `dispatch_task`.
4. `poll_task` shows `in_progress`, then `awaiting_merge` with a PR URL.
5. Open the PR. It has a real diff and the review verdict.
6. Approve. The task goes to `merged`.

Pick a change small enough to finish inside the recording, and real enough that
the diff is worth looking at. Do not stage a fake diff.

- [ ] **Step 2: Record it**

Either an asciinema cast (preferred: small, text-selectable, no video hosting)
or a GIF. Keep it under 90 seconds and under 2 MB. Do not add narration text
overlays; the transcript in `docs/demo.md` carries the explanation.

- [ ] **Step 3: Write `docs/demo.md`**

The annotated transcript: every message and tool call in order, with one line of
commentary per step explaining what Praxis did. This is what a reader who cannot
or will not play a recording gets, and it must be complete on its own.

State the configuration used at the top: which brain, which worker, which
harness, which repo. A demo whose setup is unstated is not reproducible.

- [ ] **Step 4: Link it from the README**

One line in the README, above the quickstart:

```markdown
[See it work](docs/demo.md), 90 seconds: one request in an assistant session, one reviewed pull request back.
```

Note the punctuation: a comma and a colon, never an em dash. Task 11's
`test_no_em_dashes_in_any_user_facing_doc` covers the README, so a slip here
fails the suite rather than shipping.

- [ ] **Step 5: Verify the asset actually renders**

```bash
ls -la docs/assets/
```

Then view the README on GitHub (push to a branch and look at it) and confirm the
asset loads and the link resolves. A broken demo link on the README is worse
than no demo.

- [ ] **Step 6: Commit**

```bash
git add docs/demo.md docs/assets/ README.md
git commit -m "docs: add the 90-second demo and its annotated transcript

One request in an assistant session, one reviewed pull request back. The
transcript is complete on its own for readers who will not play a recording."
```

---

### Task 13: Execute the launch checklist

**Files:**
- Create: `docs/internal/launch-log.md`
- Modify: `docs/internal/social-launch-drafts.md`

**Depends on:** Task 12, and the benchmark plan's Task 17

The spec's checklist is gated and ordered. Work it in order and stop at the
first gate that does not pass.

- [ ] **Step 1: Gate 1, the benchmark report is published in-repo**

```bash
ls docs/bench/*.md docs/bench/raw/*.jsonl
```

Expected: at least one report and its raw data. If the benchmark plan's Phase C
has not completed, STOP. Record in `docs/internal/launch-log.md` that gate 1 is
not met and end this task. Launching without the proof artifact is exactly the
recurring unanswered demand ("show me a benchmark") the research identified.

- [ ] **Step 2: Gate 2, the fresh-machine walkthrough hits 15 minutes**

Read `docs/walkthrough-15min.md`. If the recorded total exceeds 15 minutes,
either fix the slowest step and re-run the walkthrough, or record in the launch
log that the claim is being softened and change the README's number to the real
one. Do not launch with a number the walkthrough contradicts.

- [ ] **Step 3: Gate 3, the docs restructure is merged**

```bash
uv run pytest tests/test_docs_shape.py tests/test_framing.py -q
git log --oneline -5
```

Expected: green, and the restructure commits on the main branch.

- [ ] **Step 4: Gate 4, the demo artifact exists and plays**

Confirm `docs/demo.md` and its asset render on GitHub, from a logged-out browser
if possible. A demo that only works for you is not a demo.

- [ ] **Step 5: Write the launch posts**

Only now, spend `docs/internal/social-launch-drafts.md`. Update each draft so it
leads with **one benchmark number** and **the 15-minute claim**, both taken from
the actual artifacts, not from the aspiration:

- **Show HN**: the number, the claim, one paragraph on what is actually novel
  (capability-aware sizing plus the full loop), and an honest limitations line.
  Link the report and the repo.
- **r/LocalLLaMA**: lead with the local-worker angle and the measured
  local-versus-hosted comparison from the benchmark's two-worker matrix. This
  audience will read the report, so do not overstate it.
- **X thread**: the number, the demo, the repo. Short.

If the benchmark result is null or negative, say so in the first line of each
post and lead with the rigor instead of the number. A rigorous null published
plainly earns more credibility than a quiet non-launch, and the research is
explicit that unverified value is a documented abandonment trigger.

- [ ] **Step 6: Post, or record the decision not to**

Post in the order above. Then write `docs/internal/launch-log.md`: the date, the
four gates and their evidence, the exact posts and their links, and the numbers
used.

If you decide not to launch yet, write that decision and its reason in the same
file with the same specificity. The spec's definition of done accepts "a
documented decision not to launch yet" as a completed outcome; an undocumented
non-launch is not.

- [ ] **Step 7: Commit**

```bash
git add docs/internal/launch-log.md docs/internal/social-launch-drafts.md
git commit -m "docs: record the launch checklist outcome

Four gates with their evidence, the posts and their numbers, or the
documented decision not to launch yet."
```

---

## Parallel Execution Map

- **Wave 1:** Task 1, Task 2 (both `Depends on: None`)
- **Wave 2:** Task 3 (Task 2)
- **Wave 3:** Task 4 (Tasks 1, 2, 3)
- **Wave 4:** Task 5 (Task 4)
- **Wave 5:** Task 6 (Tasks 3, 5), Task 7 (Task 5)
- **Wave 6:** Task 8 (Task 7)
- **Wave 7:** Task 9 (Tasks 1, 2, 3, 5, 6, 7, 8), Phase A gate and the timed walkthrough
- **Wave 8:** Task 10 (Task 9, plus the benchmark plan's Task 17)
- **Wave 9:** Task 11 (Task 10)
- **Wave 10:** Task 12 (Task 11)
- **Wave 11:** Task 13 (Task 12, plus the benchmark plan's Task 17)

Tasks 1 and 2 are genuinely independent: one edits compose services, the other
edits the config path and the compose volumes. If they run concurrently, resolve
the `docker-compose.yml` conflict by taking both changes; they touch different
keys. Tasks 6 and 7 are independent of each other (init versus the approvals
digest) and both depend only on doctor.

## Definition of done for this plan

Mapped from the umbrella spec's section 10:

5. **Fresh-machine walkthrough: clone to first reviewed PR in 15 minutes or
   less, recorded.** Task 9 measures it and records the real number. If the
   number is not met, the doc says so and names the blocking step.
6. **README at 120 lines or fewer; user-facing docs corpus at about 1,200 lines
   or fewer; every troubleshooting entry starts from `praxis doctor`.** Task 10,
   enforced by `tests/test_docs_shape.py` rather than by discipline.
7. **Launch executed per spec 5.2, or a documented decision not to launch yet.**
   Task 13, with the four gates and their evidence in
   `docs/internal/launch-log.md`.

Plus the spec's section 4 items that carry no numbered definition-of-done entry:
one-command setup (Tasks 1, 2, 6), `praxis init` and `praxis doctor` (Tasks 4,
5, 6), worker presets (Task 3), and the merge-gate digest (Task 7).

Task 8 (the MCP surface) serves the spec's decision 4 rather than a numbered
done-item: MCP is the distribution wedge, so the surface a user actually reads
has to be readable, and the tool they reach for when a task wedges has to survive
the client's output cap. It is also a prerequisite for the Task 12 demo being
representative, since that demo is an MCP transcript.
