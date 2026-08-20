# Onboarding Blockers, Round 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the documented CLI path usable end to end by a newcomer, closing every
defect walkthrough runs #2 and #3 left open after the spec-carrier fix.

**Architecture:** No new subsystems. This is a set of independent repairs to three
existing surfaces: the Typer CLI (`src/cli/`), the REST boundary
(`src/orchestrator/api/`), and the operator docs. Each repair is small; what they have
in common is that each one is a link between a surface a person touches and a consumer
somewhere behind it, which is exactly the class that fails silently.

**Tech Stack:** Python 3.11, Typer + rich, FastAPI, pytest, Docker Compose.

---

## Read this first: the gate every task in this plan inherits

The defect that preceded this plan (`praxis submit` discarding the specification) was
not a missing feature. Both ends existed, both were correct, both were unit tested, and
the link between them was dead. Every unit test passed on a product that silently threw
the user's input away.

Several tasks below have the same shape: a flag or config value at one end, a consumer
at the other. For those tasks the plan states the carrier explicitly and requires a test
anchored **outside both ends**, plus a mutation proof. "Mutation proof" means: break the
carrier on purpose, run the test, watch it fail with a message that names the real
problem, then restore. A test you did not watch fail is not evidence.

The tasks carrying this requirement are marked **CARRIER**. Do not downgrade one of them
to a pair of unit tests.

---

## File structure

| File | Responsibility | Tasks |
|---|---|---|
| `src/cli/main.py` | Every Typer command and its rendering | 1, 4, 5, 8, 9 |
| `src/cli/init.py` | Install flow: prompts, `.env`, image build, doctor | 2, 3, 10 |
| `src/orchestrator/api/dispatch.py` | Single-task dispatch endpoint | 5 |
| `src/orchestrator/models/schemas.py` | Request/response boundary types | 5 |
| `src/orchestrator/main.py` | Lifespan wiring | 11 |
| `config/praxis.yaml` | Mounted global settings and worker presets | 2 |
| `docker-compose.yml` | Container env contract | 6 |
| `docs/deployment.md`, `README.md` | Operator-facing docs | 3, 7, 9 |
| `tests/test_cli_dispatch.py` | New: dispatch command | 1 |
| `tests/test_cli_ids.py` | New: IDs the CLI prints are IDs it accepts | 4 |
| `tests/test_dispatch_title.py` | New: title carrier | 5 |
| `tests/test_cli_init_non_interactive.py` | New: headless install surface | 10 |

---

### Task 1: `praxis dispatch`, so the CLI can drive a single task

Run #2 defect 10, re-confirmed by run #3. The README says you can drive the engine "from
an MCP client, the dashboard, or the CLI", and the CLI has no dispatch. Both walkthrough
arms fell back to raw `curl`.

`POST /api/dispatch` needs `repo_url`, `instructions`, and `model`; the CLI works in
project IDs, so the command resolves the project first and reuses its configured values.
`harness` is deliberately NOT sent unless the operator passes it: `None` means "no
preference" and preserves the project's configured harness (see the harness-parity fix).

**Do not add a `--title` flag in this task.** The endpoint ignores `title` today. Task 5
makes it real, and adding the flag first would ship exactly the dead-carrier defect this
plan exists to clean up.

**Files:**
- Modify: `src/cli/main.py` (add a command after `submit`, around line 148)
- Test: `tests/test_cli_dispatch.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
"""Tests for `praxis dispatch`."""
# ruff: noqa: S101

from __future__ import annotations

from typer.testing import CliRunner

from cli.main import app


runner = CliRunner()


def _fake_client(captured: dict, project: dict, response: dict) -> type:
    class FakeResp:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        status_code = 200
        text = ""

        def json(self) -> dict:
            return self._payload

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> bool:
            return False

        def get(self, url: str, **kwargs):
            captured["get"] = url
            return FakeResp(project)

        def post(self, url: str, **kwargs):
            captured["post"] = (url, kwargs.get("json"))
            return FakeResp(response)

    return FakeClient


def test_dispatch_reuses_the_project_repo_and_model(monkeypatch) -> None:
    captured: dict = {}
    project = {
        "id": "proj-1",
        "repo_url": "https://github.com/u/a",
        "model_name": "Gemini 3.7 Flash (High)",
        "harness": "agy",
    }
    response = {
        "task_id": "task-1",
        "plan_id": "plan-1",
        "status": "queued",
        "dashboard_url": "http://localhost:12323/",
        "warnings": [],
    }
    monkeypatch.setenv("AUTH_TOKEN", "test-token")
    monkeypatch.setattr(
        "cli.main.httpx.Client", _fake_client(captured, project, response)
    )

    result = runner.invoke(app, ["dispatch", "proj-1", "Add a health endpoint"])

    assert result.exit_code == 0, result.output
    assert captured["get"].endswith("/api/projects/proj-1")
    url, body = captured["post"]
    assert url.endswith("/api/dispatch")
    assert body["repo_url"] == "https://github.com/u/a"
    assert body["instructions"] == "Add a health endpoint"
    assert body["model"] == "Gemini 3.7 Flash (High)"
    assert "harness" not in body
    assert "task-1" in result.stdout


def test_dispatch_omits_harness_unless_asked(monkeypatch) -> None:
    """An omitted harness must stay omitted: None means 'keep the project's'."""
    captured: dict = {}
    project = {
        "id": "proj-1",
        "repo_url": "https://github.com/u/a",
        "model_name": "m",
        "harness": "agy",
    }
    response = {
        "task_id": "t",
        "plan_id": "p",
        "status": "queued",
        "dashboard_url": "http://localhost:12323/",
        "warnings": [],
    }
    monkeypatch.setenv("AUTH_TOKEN", "test-token")
    monkeypatch.setattr(
        "cli.main.httpx.Client", _fake_client(captured, project, response)
    )

    runner.invoke(app, ["dispatch", "proj-1", "do it", "--harness", "opencode"])

    assert captured["post"][1]["harness"] == "opencode"


def test_dispatch_surfaces_warnings(monkeypatch) -> None:
    captured: dict = {}
    project = {"id": "p", "repo_url": "r", "model_name": "m", "harness": "agy"}
    response = {
        "task_id": "t",
        "plan_id": "p",
        "status": "queued",
        "dashboard_url": "http://localhost:12323/",
        "warnings": ["base branch was created from main"],
    }
    monkeypatch.setenv("AUTH_TOKEN", "test-token")
    monkeypatch.setattr(
        "cli.main.httpx.Client", _fake_client(captured, project, response)
    )

    result = runner.invoke(app, ["dispatch", "p", "do it"])

    assert "base branch was created from main" in result.stdout
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `uv run pytest tests/test_cli_dispatch.py -v`
Expected: FAIL. Typer exits 2 with "No such command 'dispatch'".

- [ ] **Step 3: Add the command**

In `src/cli/main.py`, directly after the `submit` command:

```python
@app.command()
def dispatch(
    project_id: str = typer.Argument(..., help="Project ID"),
    instructions: str = typer.Argument(..., help="What the worker must do"),
    branch: str | None = typer.Option(
        None, help="Base branch; omit to cut a fresh plan/mcp-<slug>"
    ),
    model: str | None = typer.Option(None, help="Override the project's worker model"),
    harness: str | None = typer.Option(
        None, help="Override the project's harness (opencode | agy)"
    ),
) -> None:
    """Delegate one task to a worker and print where it landed."""

    with _client() as client:
        project = _check_dict(client.get(f"/api/projects/{project_id}"))
        body: dict[str, Any] = {
            "repo_url": project["repo_url"],
            "instructions": instructions,
            "model": model or project["model_name"],
        }
        # Omitted means "no preference": sending the project's own harness back
        # would be harmless today but re-points the project on any future
        # endpoint change, which is the bug the harness-parity fix closed.
        if harness is not None:
            body["harness"] = harness
        if branch is not None:
            body["branch"] = branch
        data = _check_dict(client.post("/api/dispatch", json=body))

    console.print(f"[green]Dispatched:[/green] {data['task_id']}")
    console.print(f"Plan: {data['plan_id']} | Status: {data['status']}")
    for warning in data.get("warnings") or []:
        console.print(f"[yellow]warning:[/yellow] {warning}")
    console.print(f"Dashboard: {data['dashboard_url']}")
```

- [ ] **Step 4: Run the test and watch it pass**

Run: `uv run pytest tests/test_cli_dispatch.py -v`
Expected: 3 passed.

- [ ] **Step 5: Document it**

In `README.md`, wherever the CLI commands are listed, add the line:

```
praxis dispatch <project-id> "<instructions>"   # delegate one task to a worker
```

- [ ] **Step 6: Commit**

```bash
git add src/cli/main.py tests/test_cli_dispatch.py README.md
git commit -m "feat: add praxis dispatch so the CLI can delegate a single task"
```

---

### Task 2: `praxis init` builds agent images before it sends you to use one

Run #3 defect 2. `init` prompts for the worker preset at `src/cli/init.py:937` and builds
the agent images at line 986. When the operator declines an unmet requirement, the preset's
`setup_hint` prints a `docker run ... agy-agent:latest` recipe and `init` exits, all before
any image exists:

```
Unable to find image 'agy-agent:latest' locally
docker: Error response from daemon: pull access denied for agy-agent
```

Fixing the hint text alone does not work: the correct build needs the
`*_ENTRYPOINT_SHA256` build args that `init` itself computes (Task 3), so telling the
newcomer to run a bare `docker build` trades one defect for another. Move the build above
the prompt instead. It is idempotent and cached, so the cost lands once.

**Files:**
- Modify: `src/cli/init.py:936-986`
- Test: `tests/test_cli_init.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli_init.py`:

```python
def test_images_are_built_before_the_preset_prompt(monkeypatch, tmp_path) -> None:
    """The setup_hint names an agent image, so the image must already exist.

    Declining an unmet preset requirement exits init immediately. If the build
    runs after the prompt, the recipe init just printed cannot work on a fresh
    clone.
    """
    order: list[str] = []

    def fake_compose(args, description, env=None):
        order.append("build" if "build" in args else "up")

    def fake_choose_preset(presets):
        order.append("preset")
        raise typer.Exit(code=1)

    monkeypatch.setattr("cli.init._compose", fake_compose)
    monkeypatch.setattr("cli.init._choose_preset", fake_choose_preset)
    monkeypatch.setattr("cli.init._require_repo_root", lambda: tmp_path)
    monkeypatch.setattr("cli.init._resolve_auth_token", lambda current: "tok")
    monkeypatch.setattr("cli.init._resolve_github_token", lambda current: "gh")
    monkeypatch.setattr("cli.init.IntPrompt.ask", lambda *a, **k: 12323)

    with pytest.raises(typer.Exit):
        init()

    assert order, "init ran neither a build nor a prompt"
    assert order[0] == "build", f"the preset prompt ran before the build: {order}"
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `uv run pytest tests/test_cli_init.py::test_images_are_built_before_the_preset_prompt -v`
Expected: FAIL with `the preset prompt ran before the build: ['preset']` (the build never
runs at all, because `_choose_preset` exits first).

- [ ] **Step 3: Move the build above the prompt**

In `src/cli/init.py`, cut this block from its current position (around line 984):

```python
    console.print("\nBuilding agent images (this takes a few minutes the first time)")
    build_env = {**os.environ, **_entrypoint_build_env(root)}
    _compose(["--profile", "agents", "build"], "the agent image build", env=build_env)
```

and paste it immediately BEFORE the `try:` that wraps `_choose_preset`, adding the reason:

```python
    # Built BEFORE the preset prompt on purpose. A preset whose requirement the
    # operator declines prints a setup_hint that runs a command against
    # `agy-agent:latest`, and init exits right after printing it. Building
    # afterwards makes that recipe fail verbatim on every fresh clone. The build
    # is cached and idempotent, so the cost is paid once.
    console.print("\nBuilding agent images (this takes a few minutes the first time)")
    build_env = {**os.environ, **_entrypoint_build_env(root)}
    _compose(["--profile", "agents", "build"], "the agent image build", env=build_env)

    try:
        preset = _choose_preset(_fetch_presets_or_defaults())
    except typer.Exit:
        ...
```

- [ ] **Step 4: Run the whole init suite**

Run: `uv run pytest tests/test_cli_init.py -v`
Expected: all pass, including the new test.

- [ ] **Step 5: Commit**

```bash
git add src/cli/init.py tests/test_cli_init.py
git commit -m "fix: build agent images before init prints a recipe that uses one"
```

---

### Task 3: the documented `docker build` commands produce a judgeable image

Run #3 defect 3. `docs/deployment.md:21-27` documents:

```bash
docker build -t opencode-agent:latest -f docker/opencode-agent/Dockerfile docker/opencode-agent/
```

which omits `--build-arg PRAXIS_ENTRYPOINT_SHA256=...`. The Dockerfile defaults that ARG
to the empty string, so the image carries `org.praxis.entrypoint-sha256` present but
empty. That is the designed "cannot judge" state: `praxis doctor`'s freshness check goes
amber, and a genuinely stale image looks merely unjudgeable rather than wrong. Verified in
run #3: the label was empty after the documented build and populated (`60fbbc71...`) after
`praxis init` rebuilt it.

The fix is documentation, not code: the only correct build path is the one that computes
the hash, so the docs must stop offering a second one.

**Files:**
- Modify: `docs/deployment.md:18-30`
- Test: `tests/test_docs_convention.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

Append to `tests/test_docs_convention.py`:

```python
def test_deployment_never_documents_a_bare_agent_docker_build() -> None:
    """A bare `docker build` of an agent image leaves the freshness label empty.

    The label is a build ARG that defaults to "", and only `praxis init`
    computes it. Documenting a build that omits it hands every reader an image
    the doctor can never judge.
    """
    text = Path("docs/deployment.md").read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("docker build"):
            continue
        if "agent" not in stripped:
            continue
        assert "PRAXIS_ENTRYPOINT_SHA256" in stripped, (
            f"documented agent build omits the entrypoint hash: {stripped}"
        )
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `uv run pytest tests/test_docs_convention.py -v`
Expected: FAIL, naming the `docker build -t opencode-agent:latest ...` line.

- [ ] **Step 3: Replace the documented commands**

In `docs/deployment.md`, replace the whole fenced block at lines 21-27 with:

````markdown
```bash
# Build every image the orchestrator can spawn. `praxis init` runs this for you
# and is the recommended path; run it directly only if you know the entrypoint
# hashes in .env are current.
praxis init            # computes *_ENTRYPOINT_SHA256 into .env, then builds

# Equivalent, once .env carries current hashes:
docker compose --profile agents build
```

> Do NOT build an agent image with a bare `docker build`. The freshness label
> `org.praxis.entrypoint-sha256` comes from a build ARG that defaults to empty,
> and only `praxis init` computes it. A bare build produces an image whose label
> is present-but-empty, which `praxis doctor` reads as "cannot judge" and reports
> amber, so a stale image looks merely unverifiable rather than wrong.
````

- [ ] **Step 4: Run the test and watch it pass**

Run: `uv run pytest tests/test_docs_convention.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add docs/deployment.md tests/test_docs_convention.py
git commit -m "docs: remove the bare agent docker build that leaves the label empty"
```

---

### Task 4: the CLI prints IDs its own commands accept

Run #3 defect 5. Every table truncates to 8 characters (`task["id"][:8]` plus
`max_width=8`), and every command needs the full UUID:

```
$ uv run praxis stop 8f929f8a
Error 404: {"detail":"Task not found"}
```

Feeding the CLI its own output is the single most common thing a newcomer does.

**Chosen fix: print full IDs.** Rejected alternative: server-side prefix resolution. It
is the nicer UX but needs a global task index (there is no `GET /api/tasks`, only
`GET /api/plans/{id}/tasks`), and it would put a `LIKE` prefix match on the identity of
every mutating task endpoint. Printing the real ID fixes the reported failure completely
at a fraction of the surface.

The same task fixes the URL truncation reported as two separate defects (run #3 defects 5
and 9). Both are one root cause: a rich column with no overflow policy shrinks to fit the
terminal and appends an ellipsis, which a CP1252 console renders as `?`. That is where
`http://host.docker.internal:1232?` came from, for a port of 12323. Nothing is corrupt;
it is display truncation of a value the reader needs to copy.

**Files:**
- Modify: `src/cli/main.py:78`, `:157-163`, `:192-199`, `:226`, `:284-293`
- Modify: `src/cli/doctor.py:26`
- Test: `tests/test_cli_ids.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
"""Every ID the CLI prints must be an ID the CLI accepts."""
# ruff: noqa: S101

from __future__ import annotations

from typer.testing import CliRunner

from cli.main import app


runner = CliRunner()

FULL_ID = "8f929f8a-4c21-4a55-9f0e-2b6d1e7c3a90"
PR_URL = "https://github.com/adiatmaja/playground/pull/12345"


def _client_returning(payload: list | dict) -> type:
    class FakeResp:
        status_code = 200
        text = ""

        def json(self):
            return payload

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> bool:
            return False

        def get(self, url: str, **kwargs):
            return FakeResp()

    return FakeClient


def _unwrapped(stdout: str) -> str:
    """Rich wraps wide cells; rejoin so an ID split across lines still matches."""
    return "".join(line.strip() for line in stdout.splitlines())


def test_projects_prints_the_full_id(monkeypatch) -> None:
    payload = [
        {
            "id": FULL_ID,
            "name": "playground",
            "repo_url": "https://github.com/u/a",
            "model_name": "m",
            "approval_gate": True,
        }
    ]
    monkeypatch.setenv("AUTH_TOKEN", "t")
    monkeypatch.setattr("cli.main.httpx.Client", _client_returning(payload))

    result = runner.invoke(app, ["projects"], terminal_width=200)

    assert FULL_ID in _unwrapped(result.stdout)


def test_tasks_prints_the_full_id(monkeypatch) -> None:
    payload = [
        {
            "id": FULL_ID,
            "title": "Add a thing",
            "branch_name": "agent/add-a-thing",
            "status": "passed",
            "attempt": 1,
        }
    ]
    monkeypatch.setenv("AUTH_TOKEN", "t")
    monkeypatch.setattr("cli.main.httpx.Client", _client_returning(payload))

    result = runner.invoke(app, ["tasks", "plan-1"], terminal_width=200)

    assert FULL_ID in _unwrapped(result.stdout)


def test_pending_prints_a_copyable_pr_url(monkeypatch) -> None:
    payload = {
        "count": 1,
        "tasks": [
            {
                "task_id": FULL_ID,
                "title": "Add a thing",
                "branch": "agent/add-a-thing",
                "pr_url": PR_URL,
                "age_hours": 3.2,
            }
        ],
    }
    monkeypatch.setenv("AUTH_TOKEN", "t")
    monkeypatch.setattr("cli.main.httpx.Client", _client_returning(payload))

    result = runner.invoke(app, ["pending"], terminal_width=60)

    output = _unwrapped(result.stdout)
    assert PR_URL in output
    assert "…" not in result.stdout
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `uv run pytest tests/test_cli_ids.py -v`
Expected: 3 FAIL. The first two because only `8f929f8a` is printed; the third because the
narrow terminal makes rich shrink the PR column and insert an ellipsis.

- [ ] **Step 3: Stop truncating**

In `src/cli/main.py`, four changes.

`projects` (line 78 and its row):

```python
    table.add_column("ID", style="dim", overflow="fold")
    ...
        table.add_row(
            project["id"],
            project["name"],
            project["repo_url"],
            project["model_name"],
            "ON" if project["approval_gate"] else "OFF",
        )
```

`plans` (lines 157-163):

```python
    table.add_column("ID", style="dim", overflow="fold")
    table.add_column("Spec", overflow="fold")
    table.add_column("Source")
    table.add_column("Status")
    for plan in data:
        table.add_row(
            plan["id"], plan.get("spec_path") or "", plan["source"], plan["status"]
        )
```

`tasks` (lines 192-199):

```python
    table.add_column("ID", style="dim", overflow="fold")
    ...
        table.add_row(
            task["id"],
            task["title"],
            task["branch_name"],
            task["status"],
            str(task["attempt"]),
        )
```

`task` detail (line 226) and `pending` (lines 284-293):

```python
    for run in data["runs"]:
        console.print(f"  {run['id']} | {run['status']} | {run['started_at']}")
```

```python
    table = Table(title=f"{data['count']} awaiting approval")
    for column in ("Age", "Task", "Branch", "PR"):
        # fold, never shrink: a truncated PR URL cannot be copied, and the
        # ellipsis rich inserts renders as "?" on a CP1252 console, which reads
        # as corruption rather than as truncation.
        table.add_column(column, overflow="fold")
```

In `src/cli/doctor.py` line 26, same reason:

```python
        table.add_column(column, overflow="fold")
```

- [ ] **Step 4: Run the tests and watch them pass**

Run: `uv run pytest tests/test_cli_ids.py tests/test_cli_doctor.py -v`
Expected: all pass.

- [ ] **Step 5: Prove the ellipsis assertion is live**

Temporarily change the `pending` column back to `table.add_column(column)`, run
`uv run pytest tests/test_cli_ids.py::test_pending_prints_a_copyable_pr_url -v`, confirm
it fails on the ellipsis, then restore. A rendering assertion that never fails is not an
assertion.

- [ ] **Step 6: Commit**

```bash
git add src/cli/main.py src/cli/doctor.py tests/test_cli_ids.py
git commit -m "fix: print full IDs and unwrapped URLs so CLI output can be fed back in"
```

---

### Task 5 (**CARRIER**): `/api/dispatch` honors a supplied `title`

Run #3 defect 4. `DispatchRequest` has no `title` field at all, and
`src/orchestrator/api/dispatch.py:195` derives one:

```python
"title": body.instructions[:80],
```

so a caller that passes `"title": "Create src/playground/initials.py"` gets

```
title:  "The repository has four frozen acceptance tests in src/playground/test_initials."
branch: agent/the-repository-has-four-frozen-acceptanc-6558ef
```

The carrier here is: MCP/CLI caller -> `DispatchRequest.title` -> task row title -> branch
slug. Test it end to end through the real endpoint, not by unit-testing the schema.

**Files:**
- Modify: `src/orchestrator/models/schemas.py:515-540`
- Modify: `src/orchestrator/api/dispatch.py:193-219`
- Modify: `src/cli/main.py` (the `dispatch` command from Task 1)
- Test: `tests/test_dispatch_title.py`

**Depends on:** Task 1

- [ ] **Step 1: Write the failing test**

```python
"""The title a dispatch caller supplies must reach the task and its branch."""
# ruff: noqa: S101

from __future__ import annotations

import pytest
from httpx import AsyncClient

from orchestrator.database import Database
from tests.conftest import seed_user


LONG_INSTRUCTIONS = (
    "The repository has four frozen acceptance tests in "
    "src/playground/test_initials.py. Make them pass without editing them."
)


@pytest.mark.integration
async def test_supplied_title_becomes_the_task_title_and_branch(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    await seed_user(db)

    resp = await client.post(
        "/api/dispatch",
        headers=auth_headers,
        json={
            "repo_url": "https://github.com/u/a",
            "instructions": LONG_INSTRUCTIONS,
            "model": "m",
            "title": "Create src/playground/initials.py",
        },
    )

    assert resp.status_code == 200, resp.text
    task = await db.fetch_one(
        "SELECT * FROM tasks WHERE id = ?", (resp.json()["task_id"],)
    )
    assert task is not None
    assert task["title"] == "Create src/playground/initials.py"
    assert "initials" in task["branch_name"]
    assert "the-repository-has-four" not in task["branch_name"]


@pytest.mark.integration
async def test_title_still_falls_back_to_the_instructions(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    await seed_user(db)

    resp = await client.post(
        "/api/dispatch",
        headers=auth_headers,
        json={
            "repo_url": "https://github.com/u/a",
            "instructions": LONG_INSTRUCTIONS,
            "model": "m",
        },
    )

    task = await db.fetch_one(
        "SELECT * FROM tasks WHERE id = ?", (resp.json()["task_id"],)
    )
    assert task is not None
    assert task["title"] == LONG_INSTRUCTIONS[:80]
```

- [ ] **Step 2: Run the tests and watch the first fail**

Run: `uv run pytest tests/test_dispatch_title.py -v`
Expected: the first FAILS (the title is the truncated instructions), the second PASSES.
A passing fallback test on a broken product is exactly why the first one exists.

- [ ] **Step 3: Add the field**

In `src/orchestrator/models/schemas.py`, inside `DispatchRequest` after `instructions`:

```python
    title: str | None = None
    """Short human label for the task, used verbatim as the task title and as
    the seed for its branch slug. Omitted falls back to the first 80 characters
    of ``instructions``, which reads as a sentence fragment in every listing."""
```

- [ ] **Step 4: Use it in the endpoint**

In `src/orchestrator/api/dispatch.py`, replace lines 194-198:

```python
    slug_source = body.title or body.instructions
    slug = _slugify(slug_source)
    task_dict: dict[str, Any] = {
        "title": body.title or body.instructions[:80],
        "description": body.instructions,
        "slug": slug,
        "depends_on": [],
    }
```

`branch_name` at line 218 already derives from `slug`, so it follows automatically.

- [ ] **Step 5: Run the tests and watch both pass**

Run: `uv run pytest tests/test_dispatch_title.py tests/test_api_dispatch.py -v`
Expected: all pass.

- [ ] **Step 6: Mutation proof**

Change the endpoint back to `"title": body.instructions[:80]` while leaving the schema
field in place, run the test, and confirm it fails naming the wrong title. This is the
exact state the product was in: the field accepted and ignored. Restore afterwards.

- [ ] **Step 7: Expose it on the CLI**

In `src/cli/main.py`, add to the `dispatch` command from Task 1:

```python
    title: str | None = typer.Option(
        None, help="Short task label; defaults to the first line of instructions"
    ),
```

and inside the body assembly:

```python
        if title is not None:
            body["title"] = title
```

Add to `tests/test_cli_dispatch.py`:

```python
def test_dispatch_forwards_the_title(monkeypatch) -> None:
    captured: dict = {}
    project = {"id": "p", "repo_url": "r", "model_name": "m", "harness": "agy"}
    response = {
        "task_id": "t",
        "plan_id": "p",
        "status": "queued",
        "dashboard_url": "http://localhost:12323/",
        "warnings": [],
    }
    monkeypatch.setenv("AUTH_TOKEN", "test-token")
    monkeypatch.setattr(
        "cli.main.httpx.Client", _fake_client(captured, project, response)
    )

    runner.invoke(app, ["dispatch", "p", "long instructions", "--title", "Short label"])

    assert captured["post"][1]["title"] == "Short label"
```

- [ ] **Step 8: Commit**

```bash
git add src/orchestrator/models/schemas.py src/orchestrator/api/dispatch.py \
        src/cli/main.py tests/test_dispatch_title.py tests/test_cli_dispatch.py
git commit -m "fix: honor a supplied dispatch title in the task and its branch"
```

---

### Task 6: `dashboard_url` reports the port the user can actually reach

Run #2 defect 7, re-confirmed by run #3: dispatch returns
`"dashboard_url": "http://localhost:8080/"` on an installation running on 12323.

`Settings.dashboard_url()` (`src/orchestrator/config.py:99-107`) returns
`http://localhost:{self.port}/`, and `self.port` is the IN-CONTAINER port, which compose
pins to 8080 on purpose. The container genuinely cannot know its published port, so this
is not fixable in Python: it has to be told. `AGENT_CALLBACK_URL` already solves the
identical problem one line away in `docker-compose.yml`, by deriving from `${PORT}`.
`dashboard_url()` already prefers `public_url` when set, so no code change is needed at
all, only the missing env line.

**Files:**
- Modify: `docker-compose.yml` (environment block, next to `AGENT_CALLBACK_URL`)
- Test: `tests/test_compose_agents_profile.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

Append to `tests/test_compose_agents_profile.py`:

```python
def test_compose_tells_the_container_its_host_facing_url() -> None:
    """dashboard_url() cannot infer the published port; compose must supply it.

    The container listens on 8080 and is published on ${PORT}. Without this,
    every dispatch response advertises a URL the user cannot open.
    """
    text = Path("docker-compose.yml").read_text(encoding="utf-8")
    assert "PUBLIC_URL=${PUBLIC_URL:-http://localhost:${PORT:-12323}}" in text
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `uv run pytest tests/test_compose_agents_profile.py -v`
Expected: FAIL.

- [ ] **Step 3: Add the line**

In `docker-compose.yml`, immediately above the `AGENT_CALLBACK_URL` entry:

```yaml
      # The human-facing dashboard URL returned in API responses. The container
      # listens on 8080 and is PUBLISHED on ${PORT}, so it cannot derive this
      # itself: Settings.dashboard_url() would answer http://localhost:8080/,
      # which nobody can open. Same reasoning as AGENT_CALLBACK_URL below.
      - PUBLIC_URL=${PUBLIC_URL:-http://localhost:${PORT:-12323}}
```

- [ ] **Step 4: Run the test and watch it pass**

Run: `uv run pytest tests/test_compose_agents_profile.py -v`
Expected: PASS.

- [ ] **Step 5: Verify live, because a compose env line is exactly the kind of change that passes tests and does nothing**

```bash
docker compose up -d
curl -s -H "Authorization: Bearer $AUTH_TOKEN" http://localhost:12323/api/status >/dev/null
docker exec orchestrator printenv PUBLIC_URL
```

Expected: `http://localhost:12323`. Then dispatch anything and confirm the response's
`dashboard_url` matches. A green test here only proves the YAML contains a string.

- [ ] **Step 6: Commit**

```bash
git add docker-compose.yml tests/test_compose_agents_profile.py
git commit -m "fix: advertise the published dashboard port, not the container port"
```

---

### Task 7: `/health` reports a real commit on a default install

Run #2 defect 11. `docker-compose.yml` passes `PRAXIS_BUILD_SHA: ${PRAXIS_BUILD_SHA:-dev}`,
and nothing sets it, so `/health` answers `"commit": "dev"` and the doctor's build-stamp
check can only ever return NOTE:

```
running commit dev; no working tree available here to compare against
```

`praxis init` already writes computed values into `.env` (the entrypoint hashes), so this
belongs in the same place.

**Files:**
- Modify: `src/cli/init.py` (`_managed_values`, around line 356)
- Test: `tests/test_cli_init.py`

**Depends on:** None

**Read this before writing any code.** `_managed_values` has a paired constant,
`MANAGED_KEYS` (`src/cli/init.py:38-45`), and `merge_env` raises `ValueError` for any key
not in it. Adding a key to one and not the other leaves the whole suite green and makes
`init` crash on a real run, *after* every prompt has been answered, whenever `.env`
already exists. That failure mode is called out in `_managed_values`'s own docstring
because it has happened before. Both edits belong in this task, and the second test below
exists to enforce the pairing.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli_init.py` (adding `_managed_values`, `merge_env` and
`MANAGED_KEYS` to the existing `from cli.init import (...)` block):

```python
def test_init_records_the_build_sha(monkeypatch) -> None:
    """Without PRAXIS_BUILD_SHA the build stamp is the literal string 'dev'."""
    monkeypatch.setattr("cli.init._git_short_sha", lambda: "abc1234")

    values = _managed_values(token="t", gh_token="g", port="12323", preset=None)

    assert values["PRAXIS_BUILD_SHA"] == "abc1234"


def test_every_managed_value_is_a_managed_key() -> None:
    """merge_env refuses unmanaged keys, so the two lists must not drift.

    Drift here is silent in tests and fatal in production: init crashes out of
    merge_env after the operator has answered every prompt, and only when a
    .env already exists.
    """
    values = _managed_values(token="t", gh_token="g", port="12323", preset=None)

    assert set(values) <= set(MANAGED_KEYS)
    # And prove the guard is real rather than trivially true.
    merge_env("AUTH_TOKEN=old\n", values)


def test_a_missing_git_sha_does_not_break_the_install(monkeypatch) -> None:
    monkeypatch.setattr("cli.init._git_short_sha", lambda: "")

    values = _managed_values(token="t", gh_token="g", port="12323", preset=None)

    assert "PRAXIS_BUILD_SHA" not in values
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `uv run pytest tests/test_cli_init.py -k build_sha -v`
Expected: the first FAILS with `KeyError: 'PRAXIS_BUILD_SHA'`.

- [ ] **Step 3: Implement, both halves**

First add the key to `MANAGED_KEYS` (`src/cli/init.py:38`):

```python
MANAGED_KEYS: tuple[str, ...] = (
    "AUTH_TOKEN",
    "GITHUB_TOKEN",
    "PORT",
    "LM_STUDIO_URL",
    "DEFAULT_WORKER_HARNESS",
    "DEFAULT_WORKER_MODEL",
    "PRAXIS_BUILD_SHA",
)
```

Then:

In `src/cli/init.py`, add near the other helpers:

```python
def _git_short_sha() -> str:
    """Return the working tree's short commit, or "" when it cannot be read.

    A missing SHA must not fail the install: it only costs the build stamp,
    which degrades to the pre-existing "dev".
    """
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""
```

and inside `_managed_values`, alongside the other keys:

```python
    sha = _git_short_sha()
    if sha:
        values["PRAXIS_BUILD_SHA"] = sha
```

- [ ] **Step 4: Run the tests and watch them pass**

Run: `uv run pytest tests/test_cli_init.py -v`
Expected: all pass, including every pre-existing init test.

- [ ] **Step 5: Mutation proof on the pairing**

Remove `"PRAXIS_BUILD_SHA"` from `MANAGED_KEYS` while leaving it in `_managed_values`,
run `uv run pytest tests/test_cli_init.py -k managed_key -v`, and confirm it fails with
`refusing to rewrite unmanaged .env keys: PRAXIS_BUILD_SHA`. Restore. Without watching
that, the pairing test is decoration.

- [ ] **Step 6: Verify live**

```bash
uv run praxis init            # or `docker compose up -d` if .env is already current
curl -s http://localhost:12323/health
```

Expected: `"commit"` is a real short SHA, not `dev`, and `praxis doctor`'s build-stamp
check stops reporting NOTE.

- [ ] **Step 7: Commit**

```bash
git add src/cli/init.py tests/test_cli_init.py
git commit -m "fix: stamp the real build commit into .env during init"
```

---

### Task 8: `verify_cmd` is reachable from a client

Run #2 defect 6. The verifier is one of the four advertised seats, and the only way to
set it is a raw `PATCH /api/projects/{id}`: it is absent from the dashboard and absent
from `praxis configure`, which offers only `--gate`, `--threshold`, `--retries`.
(`docs/deployment.md:353-357` was corrected in a previous round and now honestly says
"via the REST API"; this task makes the honest statement unnecessary.)

**Files:**
- Modify: `src/cli/main.py:112-133`
- Test: `tests/test_cli_config.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli_config.py`:

```python
def test_configure_sets_verify_cmd(monkeypatch) -> None:
    captured: dict = {}

    class FakeResp:
        status_code = 200
        text = ""

        def json(self):
            return {"id": "p", "name": "App"}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> bool:
            return False

        def patch(self, url: str, **kwargs):
            captured["patch"] = (url, kwargs.get("json"))
            return FakeResp()

    monkeypatch.setenv("AUTH_TOKEN", "t")
    monkeypatch.setattr("cli.main.httpx.Client", FakeClient)

    result = runner.invoke(
        app, ["configure", "p", "--verify-cmd", "uv run pytest -q"]
    )

    assert result.exit_code == 0, result.output
    assert captured["patch"][1] == {"verify_cmd": "uv run pytest -q"}
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `uv run pytest tests/test_cli_config.py -v`
Expected: FAIL, exit code 2, "No such option: --verify-cmd".

- [ ] **Step 3: Add the option**

In `src/cli/main.py`, in the `configure` command signature:

```python
    verify_cmd: str | None = typer.Option(
        None,
        "--verify-cmd",
        help="Command run against a PR head before review, e.g. 'uv run pytest -q'",
    ),
```

and in the body, before the `if not body:` guard:

```python
    if verify_cmd is not None:
        body["verify_cmd"] = verify_cmd
```

- [ ] **Step 4: Run the test and watch it pass**

Run: `uv run pytest tests/test_cli_config.py -v`
Expected: all pass.

- [ ] **Step 5: Correct the doc**

In `docs/deployment.md`, in the `Per-project verify_cmd` section, add the CLI to the list
of supported paths:

```markdown
- `praxis configure <project-id> --verify-cmd "uv run pytest -q"`
```

- [ ] **Step 6: Commit**

```bash
git add src/cli/main.py tests/test_cli_config.py docs/deployment.md
git commit -m "feat: set verify_cmd from praxis configure"
```

---

### Task 9: `praxis add-project` stops demanding a model, and the docs stop naming the wrong one

Two run #2 defects that a newcomer meets in the same minute.

Defect 8: `add-project` takes `model` as a required Argument described as "LM Studio model
name", contradicting the README's claim that "a project that omits its own `model_name`
falls back to this default, so you can register a repo and start delegating without
picking a model". Run #3 narrowed this: the harness IS correctly inherited from the preset
(`add-project` with no flag produced `harness: "agy"` from `.env`); only `model` is
wrongly mandatory.

Defect 9: `README.md:283` says the reference config is `agy` driving **Gemini 3.6 Flash
(High)**; `config/praxis.yaml:85` says **3.7**. `docs/deployment.md:61`'s verify snippet
still shows **3.5**.

**Files:**
- Modify: `src/cli/main.py:94-109`
- Modify: `README.md:283`, `docs/deployment.md:61`
- Test: `tests/test_cli_config.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli_config.py`:

```python
def test_add_project_without_a_model_sends_none(monkeypatch) -> None:
    """The deployment default exists precisely so a newcomer need not pick one."""
    captured: dict = {}

    class FakeResp:
        status_code = 201
        text = ""

        def json(self):
            return {"id": "new-project-id"}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> bool:
            return False

        def post(self, url: str, **kwargs):
            captured["post"] = (url, kwargs.get("json"))
            return FakeResp()

    monkeypatch.setenv("AUTH_TOKEN", "t")
    monkeypatch.setattr("cli.main.httpx.Client", FakeClient)

    result = runner.invoke(
        app, ["add-project", "playground", "https://github.com/u/a"]
    )

    assert result.exit_code == 0, result.output
    assert captured["post"][1]["model_name"] is None
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `uv run pytest tests/test_cli_config.py -v`
Expected: FAIL, exit code 2, "Missing argument 'MODEL'".

- [ ] **Step 3: Make the model optional**

In `src/cli/main.py`, replace the `add_project` signature and body:

```python
@app.command()
def add_project(
    name: str = typer.Argument(..., help="Project display name"),
    repo: str = typer.Argument(..., help="Repository URL"),
    model: str | None = typer.Argument(
        None,
        help=(
            "Worker model for this project. Omit to use the deployment's "
            "default worker (DEFAULT_WORKER_MODEL)."
        ),
    ),
) -> None:
    """Register a new repository."""

    with _client() as client:
        data = _check_dict(
            client.post(
                "/api/projects",
                json={"name": name, "repo_url": repo, "model_name": model},
            )
        )
    console.print(f"[green]Created project:[/green] {data['id']}")
```

Note the help text no longer says "LM Studio model name": the deployment's default worker
is agy/Gemini, and LM Studio is one option among several.

- [ ] **Step 4: Confirm the server accepts a null model**

Run: `uv run pytest tests/test_api_projects.py tests/test_api_projects_default_worker.py -v`
Expected: all pass. If `ProjectCreate.model_name` is not already `str | None`, make it
optional in `src/orchestrator/models/schemas.py` and re-run; the default-worker fallback
already exists server-side, so no other change is needed.

- [ ] **Step 5: Fix the two stale model names**

In `README.md:283`, change `Gemini 3.6 Flash (High)` to `Gemini 3.7 Flash (High)`.
In `docs/deployment.md:61`, change `--model "Gemini 3.5 Flash (High)"` to
`--model "Gemini 3.7 Flash (High)"`.

- [ ] **Step 6: Add a guard so this cannot drift again**

Append to `tests/test_docs_convention.py`:

```python
def test_docs_name_the_configured_default_worker_model() -> None:
    """Four files have named this model and three have been wrong at once."""
    import yaml

    config = yaml.safe_load(Path("config/praxis.yaml").read_text(encoding="utf-8"))
    configured = config["default_worker_model"]
    for doc in ("README.md", "docs/deployment.md"):
        text = Path(doc).read_text(encoding="utf-8")
        stale = [
            line
            for line in text.splitlines()
            if "Flash (High)" in line and configured not in line
        ]
        assert not stale, f"{doc} names a stale worker model: {stale}"
```

- [ ] **Step 7: Run everything and commit**

Run: `uv run pytest tests/test_cli_config.py tests/test_docs_convention.py -v`

```bash
git add src/cli/main.py README.md docs/deployment.md \
        tests/test_cli_config.py tests/test_docs_convention.py
git commit -m "fix: make add-project's model optional and correct the named model"
```

---

### Task 10: `praxis init --non-interactive`, so an agent can install Praxis

Captured across two sessions, never built. `praxis init` is a TTY wizard (rich `Prompt` /
`Confirm`); an agent driving it can only pipe newlines at it and hope. The missing
primitive is a surface where the AGENT runs the conversation in its own UI and `init` just
applies the decisions, with `praxis doctor` as the verifier.

Contract:

| Flag | Meaning |
|---|---|
| `--non-interactive` | Never prompt. Any missing required value is an error, not a default. |
| `--auth-token TEXT` | Required under `--non-interactive`. |
| `--port INTEGER` | Defaults to 12323. |
| `--github-token TEXT` | Optional; omitted leaves any existing value alone. |
| `--preset TEXT` | Preset name. Omitted uses the YAML default. |
| `--accept-unmet` | Proceed even when the chosen preset has an unmet requirement. |

Exit codes, so a caller can branch without parsing prose:

| Code | Meaning |
|---|---|
| 0 | Installed and `doctor` is green |
| 1 | Installed but `doctor` is not green, or an infrastructure step failed |
| 2 | Bad invocation (missing `--auth-token`, unknown `--preset`) |
| 3 | Chosen preset has an unmet requirement and `--accept-unmet` was not passed |

Under `--non-interactive`, `init` prints one JSON object as its last line, so the caller
reads a value instead of scraping the rich output. On exit 3 the object carries the
preset's `setup_hint` verbatim: that is the text the agent relays to its human.

**Files:**
- Modify: `src/cli/init.py`
- Test: `tests/test_cli_init_non_interactive.py`

**Depends on:** Task 2 (the build must already precede the preset decision, or a headless
run hits the same chicken-and-egg), Task 7 (`_managed_values` gains a key in the same
function this task calls)

- [ ] **Step 1: Write the failing tests**

```python
"""`praxis init --non-interactive`: an agent-drivable install surface."""
# ruff: noqa: S101

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from cli.main import app


runner = CliRunner()


def _last_json(stdout: str) -> dict:
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"no JSON object in output:\n{stdout}")


def test_missing_auth_token_is_a_usage_error(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("cli.init._require_repo_root", lambda: tmp_path)

    result = runner.invoke(app, ["init", "--non-interactive"])

    assert result.exit_code == 2
    assert _last_json(result.stdout)["reason"] == "missing_auth_token"


def test_never_prompts(monkeypatch, tmp_path) -> None:
    """A prompt under --non-interactive hangs an agent forever."""

    def explode(*args, **kwargs):
        raise AssertionError("init prompted under --non-interactive")

    monkeypatch.setattr("cli.init._require_repo_root", lambda: tmp_path)
    monkeypatch.setattr("cli.init.Prompt.ask", explode)
    monkeypatch.setattr("cli.init.IntPrompt.ask", explode)
    monkeypatch.setattr("cli.init.Confirm.ask", explode)
    monkeypatch.setattr("cli.init._compose", lambda *a, **k: None)
    monkeypatch.setattr("cli.init._wait_for_health", lambda url: True)
    monkeypatch.setattr("cli.init._run_doctor", lambda url, token: 0)

    result = runner.invoke(
        app,
        [
            "init",
            "--non-interactive",
            "--auth-token",
            "tok",
            "--preset",
            "local-lmstudio",
        ],
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / ".env").is_file()
    assert "AUTH_TOKEN=tok" in (tmp_path / ".env").read_text(encoding="utf-8")


def test_unmet_requirement_exits_3_with_the_recipe(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("cli.init._require_repo_root", lambda: tmp_path)
    monkeypatch.setattr("cli.init._compose", lambda *a, **k: None)

    result = runner.invoke(
        app,
        [
            "init",
            "--non-interactive",
            "--auth-token",
            "tok",
            "--preset",
            "gemini-agy",
        ],
    )

    assert result.exit_code == 3
    payload = _last_json(result.stdout)
    assert payload["reason"] == "preset_requires_credential"
    assert payload["preset"] == "gemini-agy"
    assert "agy login" in payload["setup_hint"]


def test_unknown_preset_is_a_usage_error(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("cli.init._require_repo_root", lambda: tmp_path)
    monkeypatch.setattr("cli.init._compose", lambda *a, **k: None)

    result = runner.invoke(
        app,
        ["init", "--non-interactive", "--auth-token", "tok", "--preset", "nope"],
    )

    assert result.exit_code == 2
    assert _last_json(result.stdout)["reason"] == "unknown_preset"
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `uv run pytest tests/test_cli_init_non_interactive.py -v`
Expected: 4 FAIL, exit code 2 from Typer, "No such option: --non-interactive".

- [ ] **Step 3: Add the flags and the headless branch**

In `src/cli/init.py`, change the `init` signature:

```python
def init(
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help="Never prompt; take every decision from flags and report JSON.",
    ),
    auth_token: str | None = typer.Option(None, "--auth-token"),
    port: int | None = typer.Option(None, "--port"),
    github_token: str | None = typer.Option(None, "--github-token"),
    preset_name: str | None = typer.Option(None, "--preset"),
    accept_unmet: bool = typer.Option(
        False,
        "--accept-unmet",
        help="Proceed even when the chosen preset has an unmet requirement.",
    ),
) -> None:
```

Add the reporter and the headless resolvers near the other helpers:

```python
def _report(payload: dict[str, Any]) -> None:
    """Print one machine-readable line. Agents read this, humans read the table."""
    console.print(json.dumps(payload), markup=False, highlight=False)


def _resolve_preset_headless(
    presets: list[dict[str, Any]], name: str | None, accept_unmet: bool
) -> dict[str, Any]:
    """Pick a preset without prompting, or exit with a machine-readable reason.

    Raises:
        typer.Exit: 2 when the name is unknown, 3 when a requirement is unmet
            and the caller did not accept it.
    """
    if name is None:
        chosen = next((p for p in presets if p.get("default")), None) or (
            presets[0] if presets else None
        )
    else:
        chosen = next((p for p in presets if p["name"] == name), None)
    if chosen is None:
        _report(
            {
                "ok": False,
                "reason": "unknown_preset",
                "requested": name,
                "available": [p["name"] for p in presets],
            }
        )
        raise typer.Exit(code=2)
    unmet = _unmet_requirements(chosen)
    if unmet and not accept_unmet:
        _report(
            {
                "ok": False,
                "reason": "preset_requires_credential",
                "preset": chosen["name"],
                "unmet": unmet,
                "setup_hint": chosen.get("setup_hint") or "",
                "next": "run the setup_hint, then re-run with --accept-unmet",
            }
        )
        raise typer.Exit(code=3)
    return chosen
```

Then, at the top of `init()`, replace the three interactive resolutions when
`non_interactive` is set:

```python
    if non_interactive and not auth_token:
        _report(
            {
                "ok": False,
                "reason": "missing_auth_token",
                "next": "pass --auth-token",
            }
        )
        raise typer.Exit(code=2)

    if non_interactive:
        token = auth_token or ""
        resolved_port = str(port or _DEFAULT_PORT)
        gh_token = github_token or current.get("GITHUB_TOKEN", "")
    else:
        token = _resolve_auth_token(current)
        resolved_port = str(
            IntPrompt.ask(
                "Dashboard port",
                default=_int_or(current.get("PORT", ""), _DEFAULT_PORT),
            )
        )
        gh_token = _resolve_github_token(current)
```

Use `resolved_port` in place of `port` for the rest of the function, and select the preset
with:

```python
    presets = _fetch_presets_or_defaults()
    if non_interactive:
        preset = _resolve_preset_headless(presets, preset_name, accept_unmet)
    else:
        try:
            preset = _choose_preset(presets)
        except typer.Exit:
            ...  # unchanged
```

Skip the `Confirm.ask(f"Update {env_path}?")` when `non_interactive` (always write: the
caller passed the values on purpose), and finish with:

```python
    code = _run_doctor(api_url, token)
    if non_interactive:
        _report(
            {
                "ok": code == 0,
                "reason": "installed" if code == 0 else "doctor_not_green",
                "dashboard_url": api_url,
                "preset": preset["name"],
            }
        )
    raise typer.Exit(code=code)
```

Extract `_unmet_requirements(preset)` from whatever `_confirm_unmet_requirements`
currently computes, so the interactive and headless paths judge requirements with the
same function. Two copies of that rule is how one of them ends up lying.

- [ ] **Step 4: Run the tests and watch them pass**

Run: `uv run pytest tests/test_cli_init_non_interactive.py tests/test_cli_init.py -v`
Expected: all pass, including every existing interactive test unchanged.

- [ ] **Step 5: Verify headlessly, end to end, on a fresh clone**

```bash
git clone https://github.com/adiatmaja/praxis /tmp/praxis-headless
cd /tmp/praxis-headless && uv sync --extra dev
uv run praxis init --non-interactive --auth-token "$(openssl rand -hex 16)" \
  --preset local-lmstudio; echo "exit=$?"
```

Expected: exit 0 with `{"ok": true, ...}` as the last line, or exit 1 with
`doctor_not_green` when the endpoint is unreachable. Confirm no prompt ever appears; a
hang here is the whole defect.

- [ ] **Step 6: Document it**

Add to `README.md`, under the Quick Start, a short "Installing from an agent" block
showing the command, the four exit codes, and the fact that the last stdout line is JSON.

- [ ] **Step 7: Commit**

```bash
git add src/cli/init.py tests/test_cli_init_non_interactive.py README.md
git commit -m "feat: add a non-interactive praxis init an agent can drive"
```

---

### Task 11 (**CARRIER**): `loop_interval` actually sets the loop interval

Found while verifying the spec-carrier fix, 2026-08-20. `config/praxis.yaml:2` documents
`loop_interval: 30` and `Settings.loop_interval` defaults to 30, but
`src/orchestrator/main.py` starts the loop as:

```python
app.state.orchestration_task = asyncio.create_task(
    app.state.orchestrator.run_loop(app.state.orchestration_stop_event)
)
```

`run_loop`'s own default is `interval_seconds: float = 5.0`, so every install runs at 5
seconds and the documented setting has never had any effect. Same shape as the spec
carrier: a configured value at one end, a consumer at the other, nothing in between.

**Files:**
- Modify: `src/orchestrator/main.py` (the `create_task` call)
- Test: `tests/test_main_wiring.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

Append to `tests/test_main_wiring.py`:

```python
async def test_loop_interval_setting_reaches_run_loop(
    test_settings: Settings, monkeypatch
) -> None:
    """A documented setting that never reaches its consumer is not a setting."""
    seen: dict = {}

    async def fake_run_loop(stop_event, interval_seconds=5.0):
        seen["interval"] = interval_seconds

    monkeypatch.setenv("LOOP_INTERVAL", "17")
    monkeypatch.setattr(Orchestrator, "run_loop", fake_run_loop)

    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)

    assert seen.get("interval") == 17
```

with `import asyncio` and `from orchestrator.core.orchestrator import Orchestrator` added
to the file's imports.

- [ ] **Step 2: Run the test and watch it fail**

Run: `uv run pytest tests/test_main_wiring.py::test_loop_interval_setting_reaches_run_loop -v`
Expected: FAIL with `assert 5.0 == 17`.

- [ ] **Step 3: Pass the setting**

In `src/orchestrator/main.py`:

```python
    app.state.orchestration_task = asyncio.create_task(
        app.state.orchestrator.run_loop(
            app.state.orchestration_stop_event,
            # Documented in config/praxis.yaml and settable via LOOP_INTERVAL.
            # Omitted, run_loop falls back to its own 5s default and the
            # configured value silently does nothing.
            interval_seconds=settings.loop_interval,
        )
    )
```

- [ ] **Step 4: Run the test and watch it pass**

Run: `uv run pytest tests/test_main_wiring.py -v`
Expected: all pass.

- [ ] **Step 5: Mutation proof**

Remove the `interval_seconds=` argument, confirm the test fails with `assert 5.0 == 17`,
restore.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/main.py tests/test_main_wiring.py
git commit -m "fix: honor the configured loop_interval instead of run_loop's default"
```

---

### Task 12: confirm on the wire that OpenCode now states its thinking effort

**This task measures nothing new and re-opens no question.** Run #3 settled the behaviour
by interception: a logging reverse proxy between the agent container and LM Studio captured
18 `POST /v1/chat/completions` and not one carried `reasoning_effort` or any other
thinking-control key. The complete top-level key set OpenCode sent was:

```
["max_tokens", "messages", "model", "stream", "stream_options", "top_p"]
["max_tokens", "messages", "model", "stream", "stream_options", "tool_choice", "tools", "top_p"]
```

qwen3.8-27b thinks by default, so an absent key means MAXIMUM effort, not off. Measured
cost: 45m38s for a task whose answer is a nine-line function.

**The decision has already been taken and implemented**, on the `feat/harness-parity-contract`
branch: `docker/opencode-agent/entrypoint.sh` now writes
`"options": { "reasoningEffort": "${WORKER_REASONING_EFFORT}" }` into the generated
provider config, and `core/worker_effort.py` resolves the value from each harness's
declared `effort_channel`. That work is unmerged and has never been observed on the wire.

So the only thing left is to look. If the key still does not appear, the camelCase/
snake_case trap or the dotted-provider-name trap (both recorded in `docs/gotchas.md`) is
the first place to look, not the model.

**Files:**
- Create: `bench/tools/lmstudio_logging_proxy.py`
- Modify: `docs/gotchas.md` (record the observed result either way)

**Depends on:** `feat/harness-parity-contract` merged to main

- [ ] **Step 1: Write the proxy**

About 90 lines: accept a connection, log the request body, forward it verbatim upstream,
stream the reply back unchanged. It must not alter the payload in any way, or it proves
nothing about what OpenCode sends.

```python
"""Logging pass-through proxy for LM Studio, for answering "what is actually sent".

Run:
    uv run python bench/tools/lmstudio_logging_proxy.py \
        --listen 0.0.0.0:1235 --upstream https://pcllm.sigmasolusi.com

Point the worker at the proxy instead of LM Studio, run one task, then read
the captured bodies. Two prior runs tried to infer this from symptoms and got
nowhere; interception answered it in minutes.
"""

from __future__ import annotations

import argparse
import json
import logging
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse


logger = logging.getLogger("lmstudio-proxy")
app = FastAPI()
UPSTREAM = ""


@app.post("/{path:path}")
async def forward(path: str, request: Request) -> StreamingResponse:
    body = await request.body()
    try:
        parsed: Any = json.loads(body)
        logger.info("POST /%s top-level keys: %s", path, sorted(parsed))
        logger.info("POST /%s body: %s", path, json.dumps(parsed)[:4000])
    except json.JSONDecodeError:
        logger.info("POST /%s non-JSON body (%d bytes)", path, len(body))

    client = httpx.AsyncClient(base_url=UPSTREAM, timeout=None)
    upstream = client.build_request(
        "POST",
        f"/{path}",
        content=body,
        headers={
            k: v
            for k, v in request.headers.items()
            if k.lower() not in {"host", "content-length"}
        },
    )
    response = await client.send(upstream, stream=True)

    async def body_stream():
        async for chunk in response.aiter_raw():
            yield chunk
        await response.aclose()
        await client.aclose()

    return StreamingResponse(
        body_stream(),
        status_code=response.status_code,
        headers={
            k: v
            for k, v in response.headers.items()
            if k.lower() not in {"content-length", "content-encoding"}
        },
    )


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", default="0.0.0.0:1235")
    parser.add_argument("--upstream", required=True)
    args = parser.parse_args()
    UPSTREAM = args.upstream
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    host, _, port = args.listen.partition(":")
    uvicorn.run(app, host=host, port=int(port))
```

- [ ] **Step 2: Run one opencode task through it**

```bash
uv run python bench/tools/lmstudio_logging_proxy.py \
  --listen 0.0.0.0:1235 --upstream <the real LM Studio base URL> &
# Point the project's endpoint at http://host.docker.internal:1235, then:
uv run praxis dispatch <project-id> "Add a docstring to src/playground/greet.py"
```

- [ ] **Step 3: Read the captured key sets**

Expected: every `POST /v1/chat/completions` now carries `reasoning_effort` with the
configured value. Record the observed key set verbatim.

- [ ] **Step 4: Record the result**

Append the observed key set to the harness-parity section of `docs/gotchas.md`, replacing
"unverified on the wire" with what was seen and the date. If the key is still absent,
STOP and open a defect rather than tuning the model: the config path is the suspect.

- [ ] **Step 5: Commit**

```bash
git add bench/tools/lmstudio_logging_proxy.py docs/gotchas.md
git commit -m "test: confirm on the wire what the opencode worker sends for effort"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1, Task 2, Task 3, Task 4, Task 6, Task 7, Task 8, Task 9, Task 11
  (independent; different files or different sections of the same doc)
- **Wave 2:** Task 5 (depends on Task 1), Task 10 (depends on Task 2, Task 7)
- **Wave 3:** Task 12 (depends on `feat/harness-parity-contract` reaching main, and reads
  best after Task 1 gives it a CLI dispatch to drive)

Tasks 3, 8 and 9 all touch `docs/deployment.md` in different sections; if they run
concurrently in separate worktrees, land them in the order 3, 8, 9 to keep the merges
trivial.

---

## Not in this plan, and why

- **The dead `Spec` column** (run #3 defect 8) is no longer dead. The spec-carrier fix
  populates `plans.spec_path` on every submit, so the column now renders the spec doc's
  path. Task 4 stops truncating it. Nothing to remove.
- **`docs/deployment.md:353-357`'s `verify_cmd` claim** was corrected in a previous round
  and is now accurate; Task 8 adds the CLI path it can then advertise.
- **Prefix-matching IDs server-side.** Rejected in Task 4, with the reason recorded there.
- **Re-measuring OpenCode's thinking effort.** Settled by interception in run #3. Task 12
  observes the already-implemented fix; it does not re-open the question.

---

## Before calling this plan done

Run the walkthrough again from a fresh clone, as walkthrough #4, using the method recorded
in `docs/walkthrough-15min.md`: no `.env` copied, data volume removed between arms, both
agent images deleted first, and only `README.md`, the docs it links, `.env.example`, and
the product's own output treated as available. Keep the leak log.

The specific question run #4 must answer, which no earlier run could: can a newcomer take
a spec from `praxis submit` all the way to a merged PR using only the CLI, without ever
reaching for `curl`?
