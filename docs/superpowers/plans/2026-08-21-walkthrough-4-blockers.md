# Walkthrough #4 Blockers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Praxis loop closable from the CLI alone, and remove the three
failure modes newcomer walkthrough #4 found by running the product.

**Architecture:** Five independent seams. The CLI gains the merge-gate verbs it
never had and stops hiding the ids they need (`src/cli/main.py`). `Settings`
stops treating an unrecognised `.env` key as fatal (`config.py`). `merge_pr`
stops reporting failure for a merge GitHub actually performed
(`core/git_ops.py`). `praxis configure` exposes `verify_cmd`, which the API
already accepts. And the `planner_cli` doctor probe stops passing green when the
CLI is installed but every prompt is refused, by doing one real round-trip.

**Tech Stack:** Python 3.11, Typer + rich (CLI), pydantic-settings, FastAPI,
pytest with `asyncio_mode = "auto"`, `httpx.MockTransport` for CLI tests.

**Source:** `docs/walkthrough-15min.md`, section "Run #4, 2026-08-21", the
"What to fix, ranked" list. The twelve tasks of
`2026-08-20-onboarding-blockers-2.md` are a separate, still-unexecuted plan and
are NOT duplicated here.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `src/cli/main.py` | Typer client for the REST API | Add `merge`, `merge-plan`; make `pending` print the task id; add `--verify-cmd` to `configure` |
| `tests/test_cli_merge.py` | CLI merge-gate command tests | Create |
| `tests/test_cli_pending.py` | `praxis pending` rendering tests | Create |
| `tests/test_cli_configure.py` | `praxis configure` payload tests | Create |
| `src/orchestrator/config.py` | `Settings`, env + YAML precedence | `extra="ignore"` so a stray `.env` key cannot abort startup |
| `tests/test_config.py` | Settings tests | Add the stray-key regression test |
| `src/orchestrator/core/git_ops.py` | git and `gh` shell-outs | Re-read PR state before declaring a merge failed; widen transient signatures |
| `tests/test_git_ops.py` | git_ops tests | Add idempotency + timeout-signature tests |
| `src/orchestrator/api/system.py` | provider CLI probes | Add `probe_provider_roundtrip` |
| `src/orchestrator/core/doctor_probes.py` | pure check verdicts | `probe_planner_cli` gains `prompt_ok` |
| `src/orchestrator/api/doctor.py` | live fact gathering | Feed `prompt_ok` into the probe |
| `tests/test_api_doctor.py`, `tests/test_doctor_probes.py` | doctor tests | Extend |

Tasks 1 to 4 all edit `src/cli/main.py`, so they are deliberately chained rather
than parallel: two agents editing that file concurrently would clobber each
other.

---

### Task 1: `praxis merge` opens the merge gate for one task

**Files:**
- Modify: `src/cli/main.py` (add after the `pending` command, around line 295)
- Test: `tests/test_cli_merge.py` (create)

**Depends on:** None

This is the headline fix. `POST /api/tasks/{id}/approve-merge` exists and is
documented at `docs/deployment.md:533`, but no CLI command reaches it, so the
loop cannot be closed without `curl`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_merge.py`:

```python
"""Tests for the merge-gate CLI verbs."""

from __future__ import annotations

import httpx
from typer.testing import CliRunner

from cli.main import app


runner = CliRunner()


def _patch_client(monkeypatch, handler) -> None:
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "t")
    monkeypatch.setattr(
        "cli.main._client",
        lambda: httpx.Client(
            base_url="http://x",
            headers={"Authorization": "Bearer t"},
            transport=httpx.MockTransport(handler),
        ),
    )


def test_merge_posts_approve_merge_for_the_task(monkeypatch) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            seen["path"] = request.url.path
            return httpx.Response(
                200, json={"task_id": "abc-123", "status": "merged"}
            )
        return httpx.Response(404, json={"detail": "not found"})

    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["merge", "abc-123"])

    assert result.exit_code == 0
    assert seen["path"] == "/api/tasks/abc-123/approve-merge"
    assert "merged" in result.stdout


def test_merge_surfaces_a_gate_conflict(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "task is not parked"})

    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["merge", "abc-123"])

    assert result.exit_code == 1
    assert "409" in result.stdout
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_cli_merge.py -v`

Expected: FAIL. Typer exits 2 with "No such command 'merge'", so the
`exit_code == 0` assertion fails.

- [ ] **Step 3: Write the implementation**

In `src/cli/main.py`, immediately after the `pending` command (which ends at the
`console.print(table)` around line 294) and before the `config_app = typer.Typer(`
block, add:

```python
@app.command()
def merge(
    task_id: str = typer.Argument(..., help="Full task ID from `praxis pending`"),
) -> None:
    """Approve and merge one review-passed task parked at the merge gate."""

    with _client() as client:
        data = _check_dict(client.post(f"/api/tasks/{task_id}/approve-merge"))
    console.print(f"[green]Merged:[/green] {data['task_id']} ({data['status']})")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_cli_merge.py -v`

Expected: PASS, 2 passed.

- [ ] **Step 5: Prove the test can fail (mutation check)**

Change the path in the implementation from `/api/tasks/{task_id}/approve-merge`
to `/api/plans/{task_id}/approve`, which is exactly the wrong endpoint run #4
tried. Re-run the test.

Expected: FAIL on `seen["path"] == "/api/tasks/abc-123/approve-merge"`.

Restore the correct path and re-run. Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/cli/main.py tests/test_cli_merge.py
git commit -m "feat: add praxis merge to open the merge gate from the CLI"
```

---

### Task 2: `praxis merge-plan` batch-approves a whole plan

**Files:**
- Modify: `src/cli/main.py` (immediately after the `merge` command from Task 1)
- Test: `tests/test_cli_merge.py:end` (append)

**Depends on:** Task 1

`POST /api/plans/{id}/approve-merges` returns `{approved, errors}`. Because a
dependent task waits for its predecessor to be *merged*, a multi-task plan needs
one gate call per task; this is the verb that does them in one go.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli_merge.py`:

```python
def test_merge_plan_posts_batch_and_reports_counts(monkeypatch) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            seen["path"] = request.url.path
            return httpx.Response(
                200, json={"approved": ["t1", "t2"], "errors": {"t3": "boom"}}
            )
        return httpx.Response(404, json={"detail": "not found"})

    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["merge-plan", "plan-9"])

    assert result.exit_code == 0
    assert seen["path"] == "/api/plans/plan-9/approve-merges"
    assert "2" in result.stdout
    assert "t3" in result.stdout
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_cli_merge.py::test_merge_plan_posts_batch_and_reports_counts -v`

Expected: FAIL with "No such command 'merge-plan'".

- [ ] **Step 3: Write the implementation**

In `src/cli/main.py`, directly after the `merge` command added in Task 1:

```python
@app.command("merge-plan")
def merge_plan(
    plan_id: str = typer.Argument(..., help="Plan ID from `praxis plans`"),
) -> None:
    """Approve every review-passed task parked in one plan."""

    with _client() as client:
        data = _check_dict(client.post(f"/api/plans/{plan_id}/approve-merges"))
    approved = data.get("approved") or []
    errors = data.get("errors") or {}
    console.print(f"[green]Merged:[/green] {len(approved)} task(s)")
    for task_id, reason in errors.items():
        console.print(f"[red]Failed:[/red] {task_id}: {reason}")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cli_merge.py -v`

Expected: PASS, 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/cli/main.py tests/test_cli_merge.py
git commit -m "feat: add praxis merge-plan for batch merge-gate approval"
```

---

### Task 3: `praxis pending` prints the task id it wants you to act on

**Files:**
- Modify: `src/cli/main.py:276-294` (the `pending` command)
- Test: `tests/test_cli_pending.py` (create)

**Depends on:** Task 2

Run #4's exact failure: `pending` printed no id at all, and truncated the branch
and PR URL with a `?`, so it handed the operator nothing they could pass to
anything. `summarize_pending` already returns `task_id`
(`src/orchestrator/core/approvals.py:43`); the CLI just never rendered it.

`overflow="fold"` makes rich wrap rather than truncate, so the full uuid and the
full PR URL both survive an 80-column terminal.

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_pending.py`:

```python
"""`praxis pending` must print something the operator can act on."""

from __future__ import annotations

import httpx
from typer.testing import CliRunner

from cli.main import app


runner = CliRunner()

TASK_ID = "8b1bafa2-e401-4b17-81c2-56b56c91c906"
PR_URL = "https://github.com/adiatmaja/playground/pull/37"


def _patch_client(monkeypatch, handler) -> None:
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "t")
    monkeypatch.setattr(
        "cli.main._client",
        lambda: httpx.Client(
            base_url="http://x",
            headers={"Authorization": "Bearer t"},
            transport=httpx.MockTransport(handler),
        ),
    )


def test_pending_prints_the_full_task_id_and_pr_url(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "count": 1,
                "oldest_hours": 0.0,
                "tasks": [
                    {
                        "task_id": TASK_ID,
                        "title": "Implement initials() helper function",
                        "branch": "agent/implement-initials-function",
                        "pr_url": PR_URL,
                        "age_hours": 0.0,
                    }
                ],
            },
        )

    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["pending"])

    assert result.exit_code == 0
    # Rich may wrap, so compare with whitespace collapsed.
    flat = "".join(result.stdout.split())
    assert TASK_ID in flat
    assert PR_URL in flat


def test_pending_is_quiet_when_nothing_is_parked(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"count": 0, "oldest_hours": 0.0, "tasks": []})

    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["pending"])

    assert result.exit_code == 0
    assert "Nothing awaiting approval" in result.stdout
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_cli_pending.py -v`

Expected: `test_pending_prints_the_full_task_id_and_pr_url` FAILS (no id column
at all, and the PR URL is truncated). The second test PASSES already.

- [ ] **Step 3: Write the implementation**

Replace the body of `pending` in `src/cli/main.py` (currently lines 276-294) with:

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
    table.add_column("Age")
    table.add_column("Task")
    # Folded, not truncated: these are the values the operator must copy into
    # `praxis merge`, so a rich ellipsis makes the whole table useless.
    table.add_column("Task ID", overflow="fold")
    table.add_column("Branch", overflow="fold")
    table.add_column("PR", overflow="fold")
    for task in data["tasks"]:
        table.add_row(
            f"{int(task['age_hours'])}h",
            task["title"] or task["task_id"],
            task["task_id"] or "",
            task["branch"] or "",
            task["pr_url"] or "",
        )
    console.print(table)
    console.print("\nApprove one with: [cyan]praxis merge <Task ID>[/cyan]")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cli_pending.py -v`

Expected: PASS, 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/cli/main.py tests/test_cli_pending.py
git commit -m "fix: praxis pending prints the task id and full PR url"
```

---

### Task 4: `praxis configure --verify-cmd` turns the verify gate on

**Files:**
- Modify: `src/cli/main.py:112-133` (the `configure` command)
- Test: `tests/test_cli_configure.py` (create)

**Depends on:** Task 3

The verify gate ran zero times across walkthrough #4; every review logged
`verify gate skipped: no verify_cmd configured`. `ProjectUpdate` already accepts
`verify_cmd` (`src/orchestrator/models/schemas.py:296`), so this is a CLI-only
gap: the field was reachable solely by a raw `PATCH`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_configure.py`:

```python
"""`praxis configure` must reach every field the API already accepts."""

from __future__ import annotations

import json

import httpx
from typer.testing import CliRunner

from cli.main import app


runner = CliRunner()


def _patch_client(monkeypatch, handler) -> None:
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "t")
    monkeypatch.setattr(
        "cli.main._client",
        lambda: httpx.Client(
            base_url="http://x",
            headers={"Authorization": "Bearer t"},
            transport=httpx.MockTransport(handler),
        ),
    )


def test_configure_sends_verify_cmd(monkeypatch) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH":
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"name": "playground"})
        return httpx.Response(404, json={"detail": "not found"})

    _patch_client(monkeypatch, handler)
    result = runner.invoke(
        app, ["configure", "p1", "--verify-cmd", "python -m pytest -q"]
    )

    assert result.exit_code == 0
    assert captured == {"verify_cmd": "python -m pytest -q"}


def test_configure_with_no_options_sends_nothing(monkeypatch) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json={"name": "playground"})

    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["configure", "p1"])

    assert result.exit_code == 0
    assert calls == []
    assert "No settings to update" in result.stdout
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_cli_configure.py -v`

Expected: `test_configure_sends_verify_cmd` FAILS with exit code 2, "No such
option: --verify-cmd". The second test PASSES already.

- [ ] **Step 3: Write the implementation**

In `src/cli/main.py`, change the `configure` signature and body. Add the new
option after `retries`, and the new body clause after the `max_retries` one:

```python
@app.command()
def configure(
    project_id: str = typer.Argument(..., help="Project ID"),
    gate: bool | None = typer.Option(None, help="Approval gate on/off"),
    threshold: float | None = typer.Option(None, help="Confidence threshold"),
    retries: int | None = typer.Option(None, help="Max retries"),
    verify_cmd: str | None = typer.Option(
        None,
        "--verify-cmd",
        help="Shell command the verify gate runs before review, e.g. 'python -m pytest -q'",
    ),
) -> None:
    """Update project settings."""

    body: dict[str, Any] = {}
    if gate is not None:
        body["approval_gate"] = gate
    if threshold is not None:
        body["confidence_threshold"] = threshold
    if retries is not None:
        body["max_retries"] = retries
    if verify_cmd is not None:
        body["verify_cmd"] = verify_cmd
    if not body:
        console.print("[yellow]No settings to update[/yellow]")
        return
    with _client() as client:
        data = _check_dict(client.patch(f"/api/projects/{project_id}", json=body))
    console.print(f"[green]Updated project:[/green] {data['name']}")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cli_configure.py -v`

Expected: PASS, 2 passed.

- [ ] **Step 5: Run the whole CLI suite for regressions**

Run: `uv run pytest tests/test_cli_config.py tests/test_cli_merge.py tests/test_cli_pending.py tests/test_cli_configure.py tests/test_cli_stub.py -v`

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/cli/main.py tests/test_cli_configure.py
git commit -m "feat: expose verify_cmd on praxis configure"
```

---

### Task 5: an unrecognised `.env` key must not kill the orchestrator

**Files:**
- Modify: `src/orchestrator/config.py:115`
- Test: `tests/test_config.py` (append)

**Depends on:** None

`docker-compose.yml` mounts `./.env:/app/.env:ro` so the `env_drift` doctor check
can compare them. pydantic-settings therefore parses that dotenv file whole
inside the container, and `BaseSettings` defaults to `extra="forbid"`, so one
operator-added key aborts startup and the container restart-loops:

```
pydantic_core._pydantic_core.ValidationError: 1 validation error for Settings
claude_vpn_killswitch_off
  Extra inputs are not permitted [type=extra_forbidden, input_value='1', ...]
ERROR:    Application startup failed. Exiting.
```

Nothing pins the forbidding behaviour: the only `ValidationError` test in
`tests/test_config.py:93` covers a *missing required* field, which `extra="ignore"`
does not affect.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
@pytest.mark.unit
def test_unknown_env_key_does_not_abort_startup(monkeypatch, tmp_path) -> None:
    """A stray .env key must be ignored, not fatal.

    ./.env is mounted into the container for the env_drift check, so operators
    put container-only variables there. Before this, one unrecognised key
    crashed Settings() and the orchestrator restart-looped.
    """
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AUTH_TOKEN=tok\n"
        "GITHUB_TOKEN=ghp_x\n"
        "CLAUDE_VPN_KILLSWITCH_OFF=1\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("CLAUDE_VPN_KILLSWITCH_OFF", raising=False)

    settings = Settings(_env_file=str(env_file))

    assert settings.auth_token == "tok"
    assert not hasattr(settings, "claude_vpn_killswitch_off")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_config.py::test_unknown_env_key_does_not_abort_startup -v`

Expected: FAIL with `ValidationError ... claude_vpn_killswitch_off ... extra_forbidden`.

- [ ] **Step 3: Write the implementation**

In `src/orchestrator/config.py`, replace line 115:

```python
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")
```

with:

```python
    # extra="ignore", deliberately: docker-compose.yml mounts ./.env at
    # /app/.env for the env_drift doctor check, so pydantic-settings parses the
    # operator's whole dotenv file. With the pydantic-settings default of
    # "forbid", a single container-only variable an operator adds there aborts
    # startup and the container restart-loops, with a traceback that names the
    # key but never says .env is the source.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_config.py -v`

Expected: PASS, including the pre-existing
`Settings(_env_file=None)` missing-field test, which must still raise.

- [ ] **Step 5: Prove the guard still catches a genuinely missing field**

Run: `uv run pytest tests/test_config.py -v -k "missing or required or validation"`

Expected: the pre-existing test that asserts `ValidationError` for absent
`AUTH_TOKEN` still PASSES. If it does not, `extra="ignore"` was applied to the
wrong config object.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/config.py tests/test_config.py
git commit -m "fix: a stray .env key no longer aborts orchestrator startup"
```

---

### Task 6: a merge GitHub actually performed must not report failure

**Files:**
- Modify: `src/orchestrator/core/git_ops.py:22-30` (transient signatures) and
  `src/orchestrator/core/git_ops.py:286-334` (`merge_pr`)
- Test: `tests/test_git_ops.py` (append to the "merge_pr transient-retry tests"
  section that starts at line 606)

**Depends on:** None

Observed twice in three merges during walkthrough #4. `gh pr merge` returned
`504 Gateway Timeout` *after* GitHub had already merged the PR, so Praxis raised
`merge failed`, the task stayed at `passed`, kept appearing in `praxis pending`,
and the plan could not advance. A manual retry recovered it both times.

Two defects, both fixed here:

1. The 504 body is "We couldn't respond to your request in time... Please try
   resubmitting your request." None of `_TRANSIENT_MERGE_PATTERNS` matches it
   ("try again" is there, "resubmitting" is not), so it raised on attempt one
   with no retry at all.
2. Retrying is not sufficient on its own. The authoritative question is whether
   the PR is merged, so on any failure, ask GitHub before declaring failure.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_git_ops.py`:

```python
@pytest.mark.unit
async def test_merge_pr_succeeds_when_the_pr_is_already_merged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 504 AFTER a successful merge must not be reported as a failure.

    Observed twice in three merges during newcomer walkthrough #4: gh timed out
    while GitHub had already merged, leaving the task stuck at the gate.
    """

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(git_ops_mod, "_merge_sleep", fake_sleep)
    git = GitOps("ghp_test")

    async def fake_token_for_workspace(workspace: str) -> str:
        return "ghp_test"

    monkeypatch.setattr(git, "_token_for_workspace", fake_token_for_workspace)

    timeout_stderr = (
        "non-200 OK status code: 504 Gateway Timeout body: "
        '"{\\"message\\": \\"We couldn\'t respond to your request in time. '
        'Sorry about that. Please try resubmitting your request.\\"}"'
    )

    async def fake_run_command(
        cmd: list[str],
        cwd: str | None = None,
        token: str | None = None,
    ) -> tuple[int, str, str]:
        if "merge" in cmd:
            return (1, "", timeout_stderr)
        if "view" in cmd:
            return (0, '{"state":"MERGED"}', "")
        return (0, "", "")

    monkeypatch.setattr(git, "_run_command", fake_run_command)

    # Must NOT raise: GitHub says the PR is merged.
    await git.merge_pr("/tmp/workspace", 39)


@pytest.mark.unit
async def test_merge_pr_still_raises_when_the_pr_is_not_merged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The idempotency check must not swallow a genuine failure."""

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(git_ops_mod, "_merge_sleep", fake_sleep)
    git = GitOps("ghp_test")

    async def fake_token_for_workspace(workspace: str) -> str:
        return "ghp_test"

    monkeypatch.setattr(git, "_token_for_workspace", fake_token_for_workspace)

    async def fake_run_command(
        cmd: list[str],
        cwd: str | None = None,
        token: str | None = None,
    ) -> tuple[int, str, str]:
        if "merge" in cmd:
            return (1, "", "Not found: repository or object does not exist")
        if "view" in cmd:
            return (0, '{"state":"OPEN"}', "")
        return (0, "", "")

    monkeypatch.setattr(git, "_run_command", fake_run_command)

    with pytest.raises(RuntimeError, match="Git command failed"):
        await git.merge_pr("/tmp/workspace", 7)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_git_ops.py -v -k "already_merged or still_raises"`

Expected: `test_merge_pr_succeeds_when_the_pr_is_already_merged` FAILS with
`RuntimeError: Git command failed (exit 1)`. The second test PASSES already
(it documents the behaviour that must survive).

- [ ] **Step 3: Write the implementation**

First, in `src/orchestrator/core/git_ops.py`, extend the signature tuple at
lines 22-30 with the gateway-timeout wording:

```python
# Transient GitHub merge-race signatures (case-insensitive).
_TRANSIENT_MERGE_PATTERNS: tuple[str, ...] = (
    "base branch was modified",
    "not mergeable",
    "pull request is not mergeable",
    "try again",
    "please try again",
    "merge already in progress",
    "unexpected error",
    # GitHub's own 504 wording. It says "resubmitting", not "try again", so it
    # matched nothing and raised on the first attempt. Seen on two of three
    # merges during newcomer walkthrough #4.
    "504",
    "gateway timeout",
    "resubmitting your request",
)
```

Then add a state reader just above `merge_pr` (before line 286):

```python
    async def _pr_is_merged(
        self, workspace: str, pr_number: int, repo: str | None, token: str | None
    ) -> bool:
        """Ask GitHub whether a PR is already merged.

        `gh pr merge` can time out AFTER GitHub has performed the merge, so a
        non-zero exit is not evidence the merge did not happen. Any failure to
        answer returns False, which keeps the caller failing closed.
        """
        cmd = [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--json",
            "state",
            *(["--repo", repo] if repo else []),
        ]
        code, stdout, _ = await self._run_command(cmd, cwd=workspace, token=token)
        if code != 0:
            return False
        try:
            return str(json.loads(stdout).get("state", "")).upper() == "MERGED"
        except (ValueError, AttributeError):
            return False
```

`git_ops.py` does **not** currently import `json`. Add it to the stdlib import
block at the top so it reads:

```python
import asyncio
import json
import logging
import os
import re
import subprocess
```

Finally, in `merge_pr`, treat an already-merged PR as success. Replace the
failure branch (current lines 311-316):

```python
            message = f"Git command failed (exit {code}): {' '.join(cmd)}\n{stderr}"
            exc = RuntimeError(message)
            stderr_lower = stderr.lower()
            if not any(pat in stderr_lower for pat in _TRANSIENT_MERGE_PATTERNS):
                raise exc
            last_exc = exc
```

with:

```python
            message = f"Git command failed (exit {code}): {' '.join(cmd)}\n{stderr}"
            exc = RuntimeError(message)
            # gh can fail AFTER GitHub merged (a 504 on the response, not the
            # merge). GitHub's own answer outranks gh's exit code.
            if await self._pr_is_merged(workspace, pr_number, repo, token):
                logger.info(
                    "PR #%d reported a merge error but GitHub says it is merged; "
                    "treating as success: %s",
                    pr_number,
                    stderr.strip(),
                )
                return
            stderr_lower = stderr.lower()
            if not any(pat in stderr_lower for pat in _TRANSIENT_MERGE_PATTERNS):
                raise exc
            last_exc = exc
```

Also apply the same check before the final raise, so an exhausted retry loop
does not report failure for a merge that landed on the last attempt. Replace
lines 331-332:

```python
        if last_exc is not None:
            raise last_exc
```

with:

```python
        if last_exc is not None:
            if await self._pr_is_merged(workspace, pr_number, repo, token):
                logger.info(
                    "PR #%d exhausted merge retries but GitHub says it is merged",
                    pr_number,
                )
                return
            raise last_exc
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_git_ops.py -v -k "merge_pr"`

Expected: all PASS, including the pre-existing
`test_merge_pr_retries_on_transient_error_then_succeeds` and
`test_merge_pr_non_transient_error_raises_immediately`.

Note the pre-existing non-transient test returns `(0, "", "")` for any non-merge
command, so `_pr_is_merged` parses `""` as JSON, fails, and returns False. It
must still raise. If it does not, the fail-closed path is wrong.

- [ ] **Step 5: Run the whole git_ops suite**

Run: `uv run pytest tests/test_git_ops.py -v`

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/core/git_ops.py tests/test_git_ops.py
git commit -m "fix: a timed-out gh merge that GitHub performed is no longer a failure"
```

---

### Task 7: `praxis doctor` must catch a planner CLI that cannot actually answer

**Files:**
- Modify: `src/orchestrator/api/system.py` (add `probe_provider_roundtrip`)
- Modify: `src/orchestrator/core/doctor_probes.py:221-239` (`probe_planner_cli`)
- Modify: `src/orchestrator/api/doctor.py:503-506, 582-590`
- Test: `tests/test_doctor_probes.py` (append), `tests/test_api_doctor.py` (append)

**Depends on:** None

During walkthrough #4 the `planner_cli` check printed
`OK planner CLI installed and authenticated` while *every* brain call inside the
container was being refused, because `_PROVIDER_CMDS["claude"]` has
`auth_cmd = None`, which means "authenticated iff the binary exists". The
operator only learned the truth several minutes later, as
`ValueError: Could not extract JSON from response` in the middle of a plan.

One real round-trip at diagnosis time turns a confusing mid-plan failure into a
red line in `praxis doctor`. `prompt_ok` is tri-state so the existing callers and
tests keep their meaning: `None` is "not probed".

- [ ] **Step 1: Write the failing probe test**

Append to `tests/test_doctor_probes.py`:

First add `probe_planner_cli` to the existing
`from orchestrator.core.doctor_probes import (...)` list at the top of the file
(it imports names directly; there is no `probes` alias). `CheckStatus` is
already imported from `orchestrator.core.doctor`. Then append:

```python
@pytest.mark.unit
def test_planner_cli_red_when_installed_but_prompt_refused():
    """Installed + authenticated is not enough if prompts do not complete.

    A host hook mounted into the container (walkthrough #4) refused every
    prompt while this check stayed green.
    """
    result = probe_planner_cli(
        cli_available=True, authenticated=True, prompt_ok=False
    )

    assert result.status is CheckStatus.RED
    assert "prompt" in result.detail.lower()
    # The registry hint says "run its login command", which is actively wrong
    # here: the CLI IS logged in. The probe must override it.
    assert "login" not in result.hint.lower()


@pytest.mark.unit
def test_planner_cli_green_when_the_round_trip_answers():
    result = probe_planner_cli(
        cli_available=True, authenticated=True, prompt_ok=True
    )

    assert result.status is CheckStatus.GREEN


@pytest.mark.unit
def test_planner_cli_unprobed_keeps_the_old_verdict():
    result = probe_planner_cli(cli_available=True, authenticated=True)

    assert result.status is CheckStatus.GREEN
```

Note the tests in this file are sync and use `@pytest.mark.unit` with no return
annotation; match that.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_doctor_probes.py -v -k planner`

Expected: the first two FAIL with
`TypeError: probe_planner_cli() got an unexpected keyword argument 'prompt_ok'`.
The third PASSES.

- [ ] **Step 3: Write the probe implementation**

Replace `probe_planner_cli` in `src/orchestrator/core/doctor_probes.py`
(lines 221-239):

```python
def probe_planner_cli(
    cli_available: bool,
    authenticated: bool,
    prompt_ok: bool | None = None,
) -> CheckResult:
    """Green only when the planner CLI is installed, authenticated, and answering.

    Args:
        cli_available: The CLI binary resolved on PATH.
        authenticated: The CLI reports a usable session.
        prompt_ok: Result of one real round-trip, or None when not probed.
            None preserves the pre-round-trip verdict; False is a hard red,
            because an installed-and-authenticated CLI whose prompts are refused
            fails every plan while looking healthy here.
    """
    if not cli_available:
        return CheckResult(
            check_id="planner_cli",
            status=CheckStatus.RED,
            detail="planner CLI not found on PATH",
        )
    if not authenticated:
        return CheckResult(
            check_id="planner_cli",
            status=CheckStatus.RED,
            detail="planner CLI installed but not authenticated",
        )
    if prompt_ok is False:
        return CheckResult(
            check_id="planner_cli",
            status=CheckStatus.RED,
            detail=(
                "planner CLI is installed and authenticated but a test prompt "
                "did not complete; a hook or policy may be blocking it"
            ),
            # Explicit, because the registry hint for this check says "run its
            # login command" and that is the one thing that will NOT help: the
            # CLI is already authenticated. CheckResult only auto-fills a hint
            # when none is passed.
            hint=(
                "the CLI is authenticated but something refused the prompt. "
                "Check for a Claude Code hook in the mounted ~/.claude whose "
                "detector assumes the host OS; see docs/gotchas.md"
            ),
        )
    if prompt_ok is True:
        return CheckResult(
            check_id="planner_cli",
            status=CheckStatus.GREEN,
            detail="planner CLI installed, authenticated, and answering prompts",
        )
    return CheckResult(
        check_id="planner_cli",
        status=CheckStatus.GREEN,
        detail="planner CLI installed and authenticated",
    )
```

- [ ] **Step 4: Run the probe tests to verify they pass**

Run: `uv run pytest tests/test_doctor_probes.py -v`

Expected: all PASS.

- [ ] **Step 5: Add the round-trip prober**

In `src/orchestrator/api/system.py`, after the `_provider_probe_cache`
declaration (line 44), add the cache and constants:

```python
# Round-trip probe cache: name -> (monotonic_ts, ok). Separate from
# _provider_probe_cache because this one costs a real model call, so it is
# called ONLY from /api/doctor, never from the 5s-polled /api/status.
_roundtrip_probe_cache: dict[str, tuple[float, bool]] = {}
_ROUNDTRIP_PROMPT = "reply with exactly: PONG"
_ROUNDTRIP_SENTINEL = "PONG"  # nosec B105 - a sentinel string, not a credential
_ROUNDTRIP_TIMEOUT = 25.0
```

Then add the function after `_probe_provider`:

```python
async def probe_provider_roundtrip(name: str) -> bool | None:
    """Run one real, minimal prompt through a provider CLI.

    Checking the OUTPUT rather than only the exit code is deliberate: a hook
    that refuses a prompt can still exit 0, which is exactly how a blocked
    planner passed as healthy during newcomer walkthrough #4.

    Returns:
        True when the CLI answered, False when it did not, and None when this
        provider has no round-trip defined (so the caller can tell "not probed"
        apart from "probed and refused").
    """
    if name != "claude":
        return None
    now = time.monotonic()
    cached = _roundtrip_probe_cache.get(name)
    if cached is not None and now - cached[0] < _CLAUDE_PROBE_TTL:
        return cached[1]

    resolved = shutil.which("claude")
    if resolved is None:
        _roundtrip_probe_cache[name] = (now, False)
        return False

    ok = False
    try:
        proc = await asyncio.create_subprocess_exec(
            resolved,
            "-p",
            _ROUNDTRIP_PROMPT,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=_ROUNDTRIP_TIMEOUT
        )
        ok = proc.returncode == 0 and _ROUNDTRIP_SENTINEL in stdout.decode(
            errors="replace"
        )
    except (TimeoutError, OSError) as exc:
        logger.debug("claude round-trip probe failed: %s", exc)
        ok = False

    _roundtrip_probe_cache[name] = (now, ok)
    return ok
```

- [ ] **Step 6: Wire it into the doctor endpoint**

In `src/orchestrator/api/doctor.py`, extend the import on line 36:

```python
from orchestrator.api.system import _probe_provider, probe_provider_roundtrip
```

After the existing provider probe (lines 503-506), gather the round-trip:

```python
    no_provider: dict[str, Any] = {"cli_available": False, "authenticated": False}
    provider, provider_error = await _safe(
        "planner_cli", lambda: _probe_provider("claude"), no_provider
    )
    no_roundtrip: bool | None = None
    roundtrip, roundtrip_error = await _safe(
        "planner_cli_roundtrip",
        lambda: probe_provider_roundtrip("claude"),
        no_roundtrip,
    )
```

Then feed it in, replacing the `probe_planner_cli` call at lines 587-590:

```python
        result_map["planner_cli"] = probes.probe_planner_cli(
            cli_available=bool(provider.get("cli_available")),
            authenticated=bool(provider.get("authenticated")),
            # An errored probe is "not probed", never a red: a probe that could
            # not run must not invent a verdict about the CLI.
            prompt_ok=None if roundtrip_error else roundtrip,
        )
```

- [ ] **Step 7: Write the endpoint test**

Append to `tests/test_api_doctor.py`, following the monkeypatching style already
used there for `_probe_provider`:

Tests in this file are `async def`, use `await client.get(...)`, take the
`client` and `auth_headers` fixtures, and are marked `@pytest.mark.integration`.
Status values in the response are lowercase strings. Append:

```python
@pytest.mark.integration
async def test_doctor_reds_planner_cli_when_the_round_trip_is_refused(
    client, auth_headers, monkeypatch
):
    """The check must go red when prompts are blocked, not stay green.

    Walkthrough #4: this row printed OK while every brain call in the container
    was refused, and the operator found out mid-plan instead.
    """
    from orchestrator.api import doctor as doctor_api

    async def fake_provider(name: str) -> dict:
        return {"cli_available": True, "authenticated": True}

    async def fake_roundtrip(name: str) -> bool:
        return False

    monkeypatch.setattr(doctor_api, "_probe_provider", fake_provider)
    monkeypatch.setattr(doctor_api, "probe_provider_roundtrip", fake_roundtrip)

    response = await client.get("/api/doctor", headers=auth_headers)

    assert response.status_code == 200
    check = next(
        c for c in response.json()["checks"] if c["check_id"] == "planner_cli"
    )
    assert check["status"] == "red"
    assert check["hint"]
```

The `from orchestrator.api import doctor as doctor_api` then `monkeypatch.setattr(doctor_api, ...)`
form is what the existing `_install_fake_docker` helper in this file uses; it
patches the name the endpoint actually calls rather than a string path.

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/test_doctor_probes.py tests/test_api_doctor.py -v`

Expected: all PASS.

- [ ] **Step 9: Prove the new check can fail (mutation check)**

In `probe_planner_cli`, change `if prompt_ok is False:` to
`if prompt_ok is None:`. Re-run
`uv run pytest tests/test_doctor_probes.py -v -k planner`.

Expected: `test_planner_cli_unprobed_keeps_the_old_verdict` goes RED, proving
the tri-state is actually load-bearing rather than decorative. Restore and
re-run; expected PASS.

- [ ] **Step 10: Commit**

```bash
git add src/orchestrator/api/system.py src/orchestrator/core/doctor_probes.py \
        src/orchestrator/api/doctor.py tests/test_doctor_probes.py \
        tests/test_api_doctor.py
git commit -m "feat: doctor probes a real planner round-trip, not just install"
```

---

### Task 8: documentation and full-suite gate

**Files:**
- Modify: `CLAUDE.md` (the "Gotchas" shortlist)
- Modify: `docs/gotchas.md` (append a section)
- Modify: `docs/deployment.md:533` area (the API reference table)

**Depends on:** Task 4, Task 5, Task 6, Task 7

- [ ] **Step 1: Record the two silent-failure traps in `docs/gotchas.md`**

Append:

```markdown
## The merge gate and the operator's `.env`

- **An unrecognised key in `.env` used to abort orchestrator startup**:
  `docker-compose.yml` mounts `./.env` at `/app/.env` so the `env_drift` doctor
  check can compare them, which means pydantic-settings parses the operator's
  whole dotenv file. `BaseSettings` defaults to `extra="forbid"`, so one
  container-only variable produced `extra_forbidden` and a restart loop, with a
  traceback naming the key but never naming `.env` as the source. `Settings` now
  sets `extra="ignore"`. Note the trade-off this locks in: a typo in a real
  setting is now silently ignored rather than rejected, so `praxis doctor`'s
  `env_drift` check is the thing that catches it, not startup.

- **`gh pr merge` can fail AFTER GitHub has merged**: a `504 Gateway Timeout` on
  the response is not evidence the merge did not happen. Observed on two of
  three merges in one session. The old code raised, the task stayed at `passed`,
  it kept appearing in `praxis pending`, and every dependent task stalled because
  dependents wait for a MERGE and not a pass. `merge_pr` now asks
  `gh pr view --json state` before declaring failure, and GitHub's answer
  outranks gh's exit code. Note the 504 wording is "resubmitting your request",
  which is why it matched none of the old `_TRANSIENT_MERGE_PATTERNS` entries and
  never even retried.

- **An installed, authenticated planner CLI can still refuse every prompt**:
  `_PROVIDER_CMDS["claude"]` has no auth command, so `authenticated` meant
  "the binary exists". A hook mounted in with `~/.claude` refused every prompt
  while `praxis doctor` printed `OK planner CLI installed and authenticated`, and
  the operator found out minutes later as
  `ValueError: Could not extract JSON from response` mid-plan. The check now runs
  one real round-trip and asserts on the OUTPUT, not the exit code, because a
  blocking hook can still exit 0.
```

- [ ] **Step 2: Add the one-line index entries to `CLAUDE.md`**

Under "**Config and deployment**" in the Gotchas shortlist:

```markdown
- **An unrecognised key in `.env` is IGNORED, not rejected**: `./.env` is mounted
  into the container and parsed whole, so `extra="forbid"` used to abort startup.
  The cost is that a typo in a real key is silent; `doctor`'s `env_drift` catches it.
```

Under "**The loop**":

```markdown
- **GitHub's PR state outranks `gh`'s exit code**: `gh pr merge` can 504 after the
  merge succeeded, so `merge_pr` re-reads `gh pr view --json state` before failing.
- **`praxis merge <task-id>` / `praxis merge-plan <plan-id>` open the merge gate**;
  `praxis approve` is for improvement PLANS and 404s on a task id.
```

- [ ] **Step 3: Point the API reference at the CLI**

In `docs/deployment.md`, on the `POST /api/tasks/{id}/approve-merge` row (line
533) and the `approve-merges` row, append to each description:
`CLI: praxis merge <task-id>.` and `CLI: praxis merge-plan <plan-id>.`

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest --cov=orchestrator --cov-report=term-missing -q`

Expected: all pass, coverage at or above 80 percent. The baseline on `1f30673`
was 2257 passed.

- [ ] **Step 5: Lint, format, type check**

```bash
uv run ruff format src/ tests/
uv run ruff check --fix src/ tests/
uv run mypy src/orchestrator/ --ignore-missing-imports
```

Expected: clean. Note it is `ruff format`, never `ruff fmt`.

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md docs/gotchas.md docs/deployment.md
git commit -m "docs: record the merge-gate CLI verbs and two silent-failure traps"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1 (CLI `merge`), Task 5 (`.env` extras), Task 6 (merge idempotency), Task 7 (doctor round-trip): no dependencies, four different files, run in parallel
- **Wave 2:** Task 2 (CLI `merge-plan`, depends on Task 1)
- **Wave 3:** Task 3 (CLI `pending`, depends on Task 2)
- **Wave 4:** Task 4 (CLI `configure --verify-cmd`, depends on Task 3)
- **Wave 5:** Task 8 (docs + full-suite gate, depends on Tasks 4, 5, 6, 7)

Tasks 1 to 4 are chained only because they all edit `src/cli/main.py`. Two agents
editing that file in one worktree destroy each other's uncommitted work, so do
not flatten these waves without giving each task its own worktree.

---

## Verification: the walkthrough question this plan closes

After Task 4, re-run the last leg of newcomer walkthrough #4 and confirm the loop
closes with no `curl`:

```bash
uv run praxis submit <project-id> "<a small spec>"
uv run praxis tasks <plan-id>          # wait for a task to reach `passed`
uv run praxis pending                  # must now print a full Task ID
uv run praxis merge <task-id>          # must print "Merged: <id> (merged)"
```

Run #4's answer to "can a newcomer reach a merged PR using only the CLI" was
**no**. This sequence completing is what turns it to yes. The final merge of the
integration PR into `main` stays a human action on GitHub, by design.
