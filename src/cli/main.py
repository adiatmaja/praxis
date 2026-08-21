"""Typer CLI client for the Praxis orchestrator API."""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, NoReturn

import httpx
import typer
from rich.console import Console
from rich.table import Table

from cli.doctor import doctor as _doctor
from cli.init import _fetch_presets_or_defaults, parse_env
from cli.init import init as _init
from orchestrator.core.settings_file import config_file_path


def _force_utf8_stdout() -> None:
    """Emit UTF-8 even when this CLI's output is redirected.

    Attached to a Windows console, Python writes through WriteConsoleW and the
    declared encoding is irrelevant. REDIRECTED, it falls back to the locale
    encoding, which on a Windows install is cp1252. Rich truncates a too-wide
    cell with U+2026, and cp1252 encodes that as the single byte 0x85, which is
    not valid UTF-8. The result was that `praxis tasks | grep ...` reported
    "Binary file (standard input) matches" and matched nothing, so every table
    this CLI prints became unpipeable the moment a value was long enough to
    truncate. Reconfiguring is a no-op for the interactive case and fixes the
    redirected one.
    """
    for stream in (sys.stdout, sys.stderr):
        encoding = getattr(stream, "encoding", None) or ""
        if encoding.lower().replace("-", "") == "utf8":
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            # A stream that cannot be reconfigured (already detached, or
            # replaced by a test harness) is left alone rather than crashing
            # the CLI over its own output encoding.
            continue


_force_utf8_stdout()

app = typer.Typer(name="orchestrator-cli", help="Praxis CLI")
console = Console()
app.command("doctor")(_doctor)
app.command("init")(_init)


#: How far up from the working directory to look for a `.env`. Bounded so a
#: CLI run from a deep path cannot walk to the filesystem root and adopt some
#: unrelated project's token.
_ENV_SEARCH_DEPTH = 6

#: The compose-mapped port `praxis init` writes. The old default here was
#: 8080, the bare-uvicorn port, which is not where a normal install answers.
_DEFAULT_PORT = 12323


def _find_env_file(start: Path | None = None) -> Path | None:
    """Locate the `.env` of the Praxis install the operator is standing in.

    Walks up from the working directory, nearest first, so running from
    ``praxis/src`` finds the same file as running from ``praxis/``.

    Returns:
        The first ``.env`` found, or None.
    """
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents)[:_ENV_SEARCH_DEPTH]:
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
    return None


@lru_cache(maxsize=1)
def _env_file_values() -> tuple[Path | None, dict[str, str]]:
    """Read the nearest `.env`, parsed exactly as the product parses it.

    Cached for the process: every verb resolves the URL and the token, and
    re-reading the file per call would let two halves of one command disagree
    if the file changed underneath them.

    An unreadable file yields no values rather than an error. The point of
    this fallback is to rescue an operator whose environment is empty; making
    it a new way to fail would defeat it.
    """
    path = _find_env_file()
    if path is None:
        return None, {}
    try:
        return path, parse_env(path.read_text(encoding="utf-8"))
    except OSError:
        return path, {}


def _api_url() -> str:
    """Resolve the orchestrator's base URL: env var, then `.env`, then default."""
    from_env = os.environ.get("ORCHESTRATOR_URL")
    if from_env:
        return from_env
    _path, values = _env_file_values()
    port = (values.get("PORT") or "").strip()
    if port.isdigit():
        return f"http://localhost:{port}"
    return f"http://localhost:{_DEFAULT_PORT}"


def _auth_token() -> str:
    """Resolve the auth token: env vars first, then the install's own `.env`.

    ``AUTH_TOKEN`` is the name `.env`, `.env.example` and the dashboard all
    document; ``ORCHESTRATOR_TOKEN`` is kept as a fallback so an existing user
    who only set the CLI's original var name (including `praxis init`'s own
    printed ``cli_env_exports``) is not broken.

    The `.env` fallback exists because neither of those names appears anywhere
    in ``README.md`` or ``docs/deployment.md``. A returning operator standing
    in the repo root, with a working install and ``AUTH_TOKEN`` right there in
    `.env`, was told to set an environment variable named nowhere in the docs,
    and the only documented recovery was re-running the whole wizard.
    """
    token = os.environ.get("AUTH_TOKEN") or os.environ.get("ORCHESTRATOR_TOKEN", "")
    if not token:
        _path, values = _env_file_values()
        token = values.get("AUTH_TOKEN", "") or values.get("ORCHESTRATOR_TOKEN", "")
    if not token:
        console.print(
            "[red]No auth token found.[/red] Looked at: the AUTH_TOKEN and "
            "ORCHESTRATOR_TOKEN environment variables, then AUTH_TOKEN in the "
            "nearest .env walking up from the current directory."
        )
        console.print(
            "Run [cyan]praxis env[/cyan] from your Praxis install to see what "
            "the CLI resolved, or [cyan]praxis init[/cyan] to set one up."
        )
        raise typer.Exit(1)
    return token


#: Every read-only verb answers out of SQLite in milliseconds, so 60 s is
#: already generous and a longer wait would only make a down orchestrator feel
#: hung.
_DEFAULT_TIMEOUT = 60.0

#: The merge verbs are different in kind: they wait on real work at GitHub.
#: `merge-plan` merges a plan's PASSED tasks SEQUENTIALLY, bounded only by
#: max_leaves_per_plan (24), and one `merge_pr` under repeated 504s is three
#: `gh pr merge` attempts plus backoff plus up to four `gh pr view` calls. At
#: the default budget a 12-task plan times out deterministically, in exactly
#: the 504 case Task 6 taught the server to survive. 15 minutes covers the
#: ceiling with room to spare, and the operator still gets an actionable
#: message rather than a traceback if it is ever reached.
_MERGE_TIMEOUT = 900.0


def _client(timeout: float = _DEFAULT_TIMEOUT) -> httpx.Client:
    return httpx.Client(
        base_url=_api_url(),
        headers={"Authorization": f"Bearer {_auth_token()}"},
        timeout=timeout,
    )


def _token_available() -> bool:
    """Report whether a token can be resolved, without `_auth_token()`'s
    own error message.

    `praxis presets` needs to try a live call and fall back quietly; a
    newcomer who has not run `praxis init` yet has no token at all, and that
    is not a fault worth printing, only a reason to read the local config
    instead.
    """
    if os.environ.get("AUTH_TOKEN") or os.environ.get("ORCHESTRATOR_TOKEN"):
        return True
    _path, values = _env_file_values()
    return bool(values.get("AUTH_TOKEN") or values.get("ORCHESTRATOR_TOKEN"))


#: Short: this is a best-effort probe on the way to a graceful fallback, not
#: a command an operator is waiting on. A down orchestrator should read as
#: "unreachable, falling back" almost instantly, not hang for 60s first.
_PRESETS_PROBE_TIMEOUT = 5.0


def _live_presets() -> list[dict[str, Any]] | None:
    """Fetch worker presets from a running orchestrator, or None.

    None covers both "no token resolved yet" and "orchestrator unreachable
    or answered with an error"; the caller has exactly one fallback branch
    instead of two, and neither case prints anything on its way here.
    """
    if not _token_available():
        return None
    try:
        with _client(_PRESETS_PROBE_TIMEOUT) as client:
            response = client.get("/api/settings/presets")
    except httpx.RequestError:
        return None
    if response.status_code != 200:
        return None
    data = response.json()
    presets = data.get("presets")
    return presets if isinstance(presets, list) else None


def _abandoned_merge(exc: httpx.RequestError) -> NoReturn:
    """Report a merge request the CLI gave up on, without claiming it failed.

    The orchestrator does not stop merging when the client stops listening, so
    "no answer" does NOT mean "not merged"; announcing a failure here would be
    a lie that invites a re-run. A re-run is also the wrong instruction: the
    API answers 409 "is not awaiting merge" for work that already landed,
    which reads as an error rather than as "already done". So point at
    ``praxis pending``, whose absence of the task is the real proof.
    """
    what = (
        "request timed out"
        if isinstance(exc, httpx.TimeoutException)
        else f"request failed: {exc}"
    )
    console.print(f"[yellow]{what}; the merge may still be running.[/yellow]")
    console.print(
        "Re-run 'praxis pending' to check: a task that no longer appears there "
        "has been merged."
    )
    raise typer.Exit(1)


def _check(response: httpx.Response) -> dict[str, Any] | list[dict[str, Any]]:
    if response.status_code >= 400:
        console.print(f"[red]Error {response.status_code}:[/red] {response.text}")
        raise typer.Exit(1)
    data = response.json()
    if not isinstance(data, (dict, list)):
        raise typer.Exit(1)
    return data


def _check_dict(response: httpx.Response) -> dict[str, Any]:
    data = _check(response)
    if not isinstance(data, dict):
        raise typer.Exit(1)
    return data


def _check_list(response: httpx.Response) -> list[dict[str, Any]]:
    data = _check(response)
    if not isinstance(data, list):
        raise typer.Exit(1)
    return data


@app.command()
def projects() -> None:
    """List all projects."""

    with _client() as client:
        data = _check_list(client.get("/api/projects"))
    table = Table(title="Projects")
    # A uuid, whole. `add-project` prints a full id once at creation time and
    # nothing else ever does, while `configure`, `submit`, and `plans` all look
    # a project up by EXACT match: a truncated id here means that from a new
    # terminal tomorrow the documented path is unreachable without curl. `fold`
    # wraps the value instead of cutting it when the console is narrow.
    table.add_column("ID", style="dim", max_width=36, overflow="fold")
    table.add_column("Name")
    table.add_column("Repo")
    table.add_column("Model")
    table.add_column("Gate")
    for project in data:
        table.add_row(
            project["id"],
            project["name"],
            project["repo_url"],
            project["model_name"],
            "ON" if project["approval_gate"] else "OFF",
        )
    console.print(table)


@app.command()
def add_project(
    name: str = typer.Argument(..., help="Project display name"),
    repo: str = typer.Argument(..., help="GitHub repo URL"),
    # Optional because the API has always allowed `model_name` to be null and
    # fall back to the configured worker preset. Requiring it here asked the
    # operator for a value that is undiscoverable under the shipped default
    # (`gemini-agy`), whose model is named only in the settings YAML and nowhere
    # the CLI prints. Still positional, so existing invocations are unchanged.
    model: str | None = typer.Argument(
        None,
        help=(
            "Worker model name for this project. Omit to use the model from "
            "the configured worker preset."
        ),
    ),
    harness: str | None = typer.Option(
        None,
        "--harness",
        help=(
            "Coding harness this project's worker runs in, e.g. 'opencode' or "
            "'agy'. Omit to use the harness from the configured worker preset, "
            "which is what `praxis presets` shows as the default. The server "
            "rejects an unknown value and names the allowed set."
        ),
    ),
) -> None:
    """Register a new GitHub repository."""

    body: dict[str, Any] = {"name": name, "repo_url": repo}
    # Send only what was supplied. A null `model_name` or `harness` in the
    # payload is not the same as an absent one: the project row would record an
    # explicit null and stop tracking the preset.
    if model is not None:
        body["model_name"] = model
    if harness is not None:
        body["harness"] = harness
    with _client() as client:
        data = _check_dict(client.post("/api/projects", json=body))
    console.print(f"[green]Created project:[/green] {data['id']}")


@app.command()
def configure(
    project_id: str = typer.Argument(..., help="Project ID"),
    gate: bool | None = typer.Option(None, help="Approval gate on/off"),
    threshold: float | None = typer.Option(None, help="Confidence threshold"),
    retries: int | None = typer.Option(None, help="Max retries"),
    verify_cmd: str | None = typer.Option(
        None,
        "--verify-cmd",
        help=(
            "Shell command the verify gate runs before review, "
            "e.g. 'python -m pytest -q'"
        ),
    ),
    harness: str | None = typer.Option(
        None,
        "--harness",
        help=(
            "Coding harness this project's worker runs in, e.g. 'opencode' or "
            "'agy'. The server rejects an unknown value and names the allowed "
            "set."
        ),
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
    if harness is not None:
        body["harness"] = harness
    if not body:
        console.print("[yellow]No settings to update[/yellow]")
        return
    with _client() as client:
        data = _check_dict(client.patch(f"/api/projects/{project_id}", json=body))
    console.print(f"[green]Updated project:[/green] {data['name']}")


@app.command()
def submit(
    project_id: str = typer.Argument(..., help="Project ID"),
    spec: str | None = typer.Argument(
        None,
        help=(
            "Specification text. Omit this and use --file for anything long: "
            "Git Bash truncates a bash -c argument at 8192 bytes and reports "
            "it as a syntax error in your OWN script, not as 'too long'."
        ),
    ),
    file: Path | None = typer.Option(
        None,
        "--file",
        "-f",
        help=(
            "Read the specification from this file (UTF-8) instead of the "
            "spec argument. Pass '-' to read the specification from stdin."
        ),
    ),
) -> None:
    """Submit a specification for planning."""

    if spec is not None and file is not None:
        console.print(
            "[red]Pass the spec as an argument or with --file, not both.[/red]"
        )
        raise typer.Exit(2)
    if spec is None and file is None:
        console.print(
            "[red]Missing specification.[/red] Pass it as an argument, or "
            "--file <path> to read it from a file (a real spec runs long "
            "enough that the inline form breaks in Git Bash)."
        )
        raise typer.Exit(2)

    if file is not None:
        if str(file) == "-":
            # UTF-8 explicit, via the raw byte stream rather than the text
            # stdin: on Windows the console default is cp1252, and this
            # command exists specifically to stop the encoding from being
            # left to guesswork.
            spec_text = sys.stdin.buffer.read().decode("utf-8")
        else:
            try:
                spec_text = file.read_text(encoding="utf-8")
            except OSError as exc:
                # soft_wrap=True: a narrow console (this project's own test
                # suite runs at COLUMNS=80) would otherwise word-wrap the
                # path mid-token, the exact defect class `pending`/`tasks`
                # already had to fix for uuids.
                console.print(
                    f"[red]Cannot read spec file {file}:[/red] {exc}",
                    soft_wrap=True,
                )
                raise typer.Exit(1) from exc
    else:
        spec_text = spec

    with _client() as client:
        data = _check_dict(
            client.post(f"/api/projects/{project_id}/plans", json={"spec": spec_text})
        )
    console.print(f"[green]Plan created:[/green] {data['id']} ({data['status']})")


@app.command()
def plans(project_id: str = typer.Argument(..., help="Project ID")) -> None:
    """List plans for a project."""

    with _client() as client:
        data = _check_list(client.get(f"/api/projects/{project_id}/plans"))
    table = Table(title="Plans")
    # No ID column, for the reason spelled out in `tasks`: `max_width=36` is a
    # MAXIMUM, so with four columns on an 80-column console rich shrank the id
    # and folded each uuid across two rows, and every consumer looks a plan up
    # by exact match. This surface kept the defect two runs longer than `tasks`
    # did because it DID print a copyable line, but only for a plan with an
    # open integration PR: a pending, active, or already-integrated plan got
    # none at all, which is the majority of them. A conditional copyable line
    # reads as a working one right up until you need the id it withheld.
    table.add_column("Spec", max_width=40)
    table.add_column("Source")
    table.add_column("Status")
    for plan in data:
        spec_display = (plan.get("spec_path") or "")[:40]
        table.add_row(
            spec_display,
            plan["source"],
            _status_cell(plan),
        )
    console.print(table)
    if data:
        console.print()
        for plan in data:
            console.print(f"praxis tasks {plan['id']}   # {_status_cell(plan)}")
            if plan.get("integration_pr_url") and not plan.get("integration_merged_at"):
                console.print(
                    f"praxis merge-plan {plan['id']}   # integrate onto the base branch"
                )
                console.print(f"  PR: {plan['integration_pr_url']}")


def _status_cell(plan: dict[str, Any]) -> str:
    """Render a plan's status with what actually happened to its work.

    "completed" alone answers the wrong question. It means every task merged
    onto the PLAN branch, which a reader hears as "landed on main". The suffix
    says which of the three it really is: integrated, waiting on an open PR,
    or completed with no PR at all (the best-effort `gh pr create` failed,
    which is a reportable state, not a dash).

    Deliberately a suffix rather than a fifth column. The ID column has to
    stay wide enough to print a uuid contiguously, since every other verb
    looks a plan up by exact match, and a narrow console splits a folded id
    across border characters.
    """
    status_text = str(plan.get("status") or "")
    if plan.get("integration_merged_at"):
        return f"{status_text} (integrated)"
    if plan.get("integration_pr_url"):
        return f"{status_text} (PR open)"
    if status_text == "completed":
        return f"{status_text} (no PR)"
    return status_text


@app.command()
def approve(plan_id: str = typer.Argument(..., help="Plan ID")) -> None:
    """Approve an autonomous improvement plan."""

    with _client() as client:
        data = _check_dict(client.post(f"/api/plans/{plan_id}/approve"))
    console.print(f"[green]Plan approved:[/green] {data['id']}")


@app.command()
def reject(plan_id: str = typer.Argument(..., help="Plan ID")) -> None:
    """Reject an autonomous improvement plan."""

    with _client() as client:
        data = _check_dict(client.post(f"/api/plans/{plan_id}/reject"))
    console.print(f"[red]Plan rejected:[/red] {data['id']}")


@app.command()
def tasks(plan_id: str = typer.Argument(..., help="Plan ID")) -> None:
    """List tasks in a plan."""

    with _client() as client:
        data = _check_list(client.get(f"/api/plans/{plan_id}/tasks"))
    table = Table(title="Tasks")
    # No ID column. `task`, `logs`, `stop`, and `merge` all take a full task id
    # by exact match, and `max_width=36` did NOT deliver one: it is a maximum,
    # not a minimum, so with five columns competing for an 80-column console
    # rich shrank the id to 16 characters and folded each uuid across three
    # rows. Raising it to `min_width` only moved the damage, pushing Status and
    # Attempt off the right edge. So the id goes below the table as a copyable
    # line, exactly as `pending` and `plans` already do, and the table keeps the
    # columns you actually scan.
    table.add_column("Title")
    table.add_column("Branch")
    table.add_column("Status")
    table.add_column("Attempt")
    for task in data:
        table.add_row(
            task["title"],
            task["branch_name"],
            task["status"],
            str(task["attempt"]),
        )
    console.print(table)
    if data:
        console.print()
        for task in data:
            console.print(f"praxis task {task['id']}   # {task['title']}")


@app.command()
def task(task_id: str = typer.Argument(..., help="Task ID")) -> None:
    """Get task details with agent run history."""

    with _client() as client:
        data = _check_dict(client.get(f"/api/tasks/{task_id}"))
    task_data = data["task"]
    console.print(f"[bold]{task_data['title']}[/bold]")
    console.print(
        "Status: "
        f"{task_data['status']} | Branch: {task_data['branch_name']} | "
        f"Attempt: {task_data['attempt']}"
    )
    if task_data["pr_url"]:
        console.print(f"PR: {task_data['pr_url']}")
    if task_data["review_feedback"]:
        console.print(f"[yellow]Feedback:[/yellow] {task_data['review_feedback']}")
    for run in data["runs"]:
        console.print(f"  {run['id'][:8]} | {run['status']} | {run['started_at']}")


#: Lines of container log printed by default. A worker transcript runs to
#: hundreds of kilobytes and the answer is almost always at the end, so the
#: default is a tail. The count of suppressed lines is always printed, because
#: a silently truncated log is how you conclude the worker said nothing.
_LOG_TAIL_DEFAULT = 200


def _print_run_log(run: dict[str, Any], tail: int) -> None:
    """Print one agent run's captured container log.

    Args:
        run: An ``agent_runs`` row as returned by ``GET /api/tasks/{id}``.
        tail: Lines to print from the end; 0 means all of them.
    """
    header = (
        f"run {run['id']}  |  {run.get('status') or '?'}  |  "
        f"started {run.get('started_at') or '?'}"
    )
    console.print(f"[bold]{header}[/bold]")

    logs = run.get("logs") or ""
    if not logs.strip():
        # An empty log is a REPORTABLE state, not an absence. It means the
        # orchestrator had no agent manager when the callback arrived, or the
        # container was already gone. Printing nothing here is how an operator
        # concludes "the worker said nothing", which is a different and much
        # more alarming fact than "we did not capture it".
        console.print(
            "  [yellow]No log captured for this run.[/yellow] The orchestrator "
            "stores container output when the agent reports; an empty value "
            "means it could not read the container, not that the worker was "
            "silent."
        )
        return

    lines = logs.splitlines()
    shown = lines if tail <= 0 else lines[-tail:]
    suppressed = len(lines) - len(shown)
    if suppressed > 0:
        console.print(
            f"  [dim]... {suppressed} earlier line(s) suppressed; "
            f"--tail 0 prints all {len(lines)}[/dim]"
        )
    for line in shown:
        # markup=False and highlight=False are both load-bearing. Worker
        # transcripts contain bracketed text (`[PRAXIS PHASE] understanding`,
        # `[main] INFO`), and rich reads a leading `[` as the start of a markup
        # tag: it would swallow those tokens or raise on an unclosed one. This
        # is a log viewer, so the bytes must survive verbatim.
        console.print(line, markup=False, highlight=False)


@app.command()
def logs(
    task_id: str = typer.Argument(..., help="Full task ID from `praxis tasks`"),
    tail: int = typer.Option(
        _LOG_TAIL_DEFAULT,
        "--tail",
        help="Lines to print from the end of each log. 0 prints everything.",
    ),
    all_runs: bool = typer.Option(
        False,
        "--all",
        help="Print every attempt's log, oldest first, not just the latest.",
    ),
) -> None:
    """Print the agent container log for a task's run.

    The orchestrator removes an agent container seconds after it reports, so
    `docker logs` is already too late by the time you know you want it. The
    output is captured onto the run row at that moment and was reachable only
    by curling `GET /api/tasks/{id}` and reading `runs[].logs` out of the JSON.

    Defaults to the most recent attempt, which is the one you want after a
    failure. Use `--all` to see how earlier attempts differed, which is how you
    tell a worker that failed the same way three times from one that failed
    three different ways.
    """

    with _client() as client:
        data = _check_dict(client.get(f"/api/tasks/{task_id}"))
    runs = data.get("runs") or []
    if not runs:
        task_data = data.get("task") or {}
        console.print(
            f"[yellow]No agent runs for this task[/yellow] "
            f"(status: {task_data.get('status') or 'unknown'})."
        )
        console.print(
            "A task that never dispatched has no container output. "
            "Run 'praxis task <id>' for its status and review feedback."
        )
        return

    selected = runs if all_runs else runs[-1:]
    if not all_runs and len(runs) > 1:
        console.print(
            f"[dim]Attempt {len(runs)} of {len(runs)}; --all shows the "
            f"earlier {len(runs) - 1}.[/dim]"
        )
    for index, run in enumerate(selected):
        if index:
            console.print()
        _print_run_log(run, tail)


@app.command()
def stop(task_id: str = typer.Argument(..., help="Task ID")) -> None:
    """Stop a running agent."""

    with _client() as client:
        data = _check_dict(client.post(f"/api/tasks/{task_id}/stop"))
    console.print(f"[yellow]Stopped {data['stopped']} agent(s)[/yellow]")


@app.command()
def status() -> None:
    """Show orchestrator status."""

    with _client() as client:
        data = _check_dict(client.get("/api/status"))
    opus_state = data["opus_state"]
    console.print(f"Opus: [bold]{opus_state['status']}[/bold]")
    if opus_state["resume_at"]:
        console.print(f"  Resume at: {opus_state['resume_at']}")
    console.print(f"  Queued actions: {opus_state['queued_count']}")
    console.print(
        f"Active agents: {data['active_agents']} / {data['total_agents']} total"
    )


@app.command()
def mode(action: str = typer.Argument(..., help="on | off | status")) -> None:
    """Turn auto-delegate mode on/off or show its status."""
    action_lower = action.lower()
    if action_lower not in {"on", "off", "status"}:
        console.print("action must be one of: on, off, status")
        raise typer.Exit(code=2)
    with _client() as client:
        if action_lower == "status":
            resp = client.get("/api/settings/auto-delegate")
        else:
            resp = client.put(
                "/api/settings/auto-delegate", json={"enabled": action_lower == "on"}
            )
        data = _check_dict(resp)
    enabled_str = "ON" if data.get("enabled") else "OFF"
    worker = data.get("worker") or {}
    harness = worker.get("harness", "")
    model = worker.get("model") or "unset"
    console.print(f"auto-delegate: {enabled_str} (worker: {harness} / {model})")


@app.command()
def pending() -> None:
    """List tasks and completed plans parked at the human merge gate."""
    with _client() as client:
        data = _check_dict(client.get("/api/approvals/pending"))
    tasks = data.get("tasks") or []
    plans_awaiting = data.get("plans") or []
    # Autonomous proposals are gated separately from `count`, so they must be
    # tested separately here too. Reading `count` alone printed "Nothing
    # awaiting approval" while an improvement-loop proposal sat PENDING and
    # only `praxis plans <project-id>` would reveal it, which meant knowing
    # both that it existed and which project it belonged to.
    proposals = data.get("proposals") or []
    if not data["count"] and not proposals:
        console.print("[green]Nothing awaiting approval.[/green]")
        return

    if tasks:
        table = Table(title=f"{len(tasks)} task(s) awaiting approval")
        table.add_column("Age")
        table.add_column("Task", max_width=40)
        table.add_column("Branch", overflow="fold")
        for task in tasks:
            table.add_row(
                f"{int(task['age_hours'])}h",
                task["title"] or task["task_id"],
                task["branch"] or "",
            )
        console.print(table)

    if plans_awaiting:
        plan_table = Table(title=f"{len(plans_awaiting)} plan(s) awaiting integration")
        plan_table.add_column("Age")
        plan_table.add_column("Plan branch", overflow="fold")
        for plan in plans_awaiting:
            plan_table.add_row(
                f"{int(plan['age_hours'])}h",
                plan["branch"] or plan["plan_id"],
            )
        console.print(plan_table)

    if proposals:
        proposal_table = Table(
            title=f"{len(proposals)} improvement proposal(s) awaiting approval"
        )
        proposal_table.add_column("Age")
        proposal_table.add_column("Project", overflow="fold")
        for proposal in proposals:
            proposal_table.add_row(
                f"{int(proposal['age_hours'])}h",
                proposal["project_id"] or "",
            )
        console.print(proposal_table)

    console.print()
    # A bordered table wraps unpredictably once it leaves this terminal
    # (docker logs, CI log viewers, less, older SSH clients all hard-wrap at
    # a fixed column), which can split a uuid or a url mid-value across
    # cells. Print one plain, copy-pasteable line per item instead: rich's
    # default word-wrap only breaks on whitespace, so a token with none (a
    # uuid, a url) survives contiguous at any width.
    for task in tasks:
        console.print(
            f"praxis merge {task['task_id']}   # {task['title'] or task['task_id']}"
        )
        if task["pr_url"]:
            console.print(f"  PR: {task['pr_url']}")
    for plan in plans_awaiting:
        # The plan line names the same verb the per-task line does, because
        # `merge-plan` is one command that covers both stages: it drains
        # whatever is still parked, then merges the integration PR. A plan
        # reaching this list means stage one is already done.
        console.print(
            f"praxis merge-plan {plan['plan_id']}   # integrate onto the base branch"
        )
        if plan["pr_url"]:
            console.print(f"  PR: {plan['pr_url']}")
    for proposal in proposals:
        # Both verbs, because unlike the merge gate this one is genuinely
        # two-way and rejecting is the common answer: `approve` dispatches the
        # proposal's tasks, `reject` closes it for good.
        console.print(
            f"praxis approve {proposal['plan_id']}   # or: praxis reject "
            f"{proposal['plan_id']}"
        )


@app.command()
def merge(
    task_id: str = typer.Argument(..., help="Full task ID from `praxis pending`"),
) -> None:
    """Approve and merge one review-passed task parked at the merge gate."""

    with _client(_MERGE_TIMEOUT) as client:
        try:
            response = client.post(f"/api/tasks/{task_id}/approve-merge")
        except httpx.RequestError as exc:
            _abandoned_merge(exc)
        data = _check_dict(response)
    console.print(f"[green]Merged:[/green] {data['task_id']} ({data['status']})")


@app.command("merge-plan")
def merge_plan(
    plan_id: str = typer.Argument(..., help="Plan ID from `praxis plans`"),
) -> None:
    """Merge a plan's parked tasks, then integrate the plan onto the base branch.

    Both stages, because a plan is only landed when both have run: the tasks
    merge onto the plan branch, and the plan's integration PR merges onto the
    project's base branch.
    """

    with _client(_MERGE_TIMEOUT) as client:
        try:
            response = client.post(f"/api/plans/{plan_id}/approve-merges")
        except httpx.RequestError as exc:
            _abandoned_merge(exc)
        data = _check_dict(response)
    approved = int(data.get("approved") or 0)
    errors = data.get("errors") or []
    integration = data.get("integration") or {}
    integration_status = str(integration.get("status") or "none")

    if approved:
        console.print(f"[green]Merged:[/green] {approved} task(s)")

    # Every branch below names what state the plan is actually in. The version
    # this replaces printed `Merged: 0 task(s)` and exited 0 against a finished
    # plan whose integration PR was open and unmentioned, which reads as "all
    # done" when the work is not on the base branch at all.
    if integration_status == "merged":
        console.print("[green]Integrated:[/green] plan merged to its base branch")
        if integration.get("pr_url"):
            console.print(f"  PR: {integration['pr_url']}")
    elif integration_status == "already_merged":
        console.print("[green]Already integrated:[/green] nothing left to merge")
        if integration.get("pr_url"):
            console.print(f"  PR: {integration['pr_url']}")
    elif integration_status == "not_ready" and not errors:
        console.print(
            f"[yellow]Not integrated:[/yellow] {integration.get('reason') or 'plan is not finished'}"
        )
    elif integration_status == "none" and not approved and not errors:
        console.print(
            f"[yellow]Nothing to merge[/yellow] for plan {plan_id}: "
            f"{integration.get('reason') or 'no parked tasks and no integration PR'}"
        )
        console.print("Run 'praxis tasks <plan-id>' to see where the plan stopped.")

    for failure in errors:
        console.print(
            f"[red]Failed:[/red] {failure.get('task_id', '?')}: "
            f"{failure.get('error', 'unknown error')}"
        )
    if errors:
        raise typer.Exit(1)


config_app = typer.Typer(
    name="config", help="Configure the model registry and role chains"
)
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
            m["name"],
            m["provider"],
            m.get("model") or "-",
            m.get("effort") or "-",
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
    chain: str = typer.Argument(
        ..., help="Comma-separated model names, priority first"
    ),
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
        registry.append(
            {
                "name": name,
                "provider": provider,
                "model": model,
                "effort": effort or None,
            }
        )
        _check_list(client.put("/api/settings/registry", json=registry))
    console.print(f"[green]Registered:[/green] {name} ({provider}/{model or '-'})")


@config_app.command("refresh-capabilities")
def config_refresh_capabilities() -> None:
    """Attempt to refresh the capability snapshot (bundled-only in v1)."""
    with _client() as client:
        data = _check_dict(client.post("/api/settings/capabilities/refresh"))
    console.print(f"[yellow]{data.get('status')}[/yellow]: {data.get('detail')}")


@app.command()
def presets() -> None:
    """List the worker presets `praxis init --preset <name>` accepts.

    Nothing else in the CLI names these values: `praxis config show` lists
    the model registry and role chains, not presets, and no other verb
    covers them either. Tries the running orchestrator first, since that is
    the configuration actually in effect; falls back to reading the settings
    YAML directly when the orchestrator is unreachable or no token has been
    set up yet, which is exactly the moment a newcomer running `praxis init`
    for the first time needs this list the most. Both sources read the same
    underlying preset records, so the fallback is never a degraded view, only
    a different path to identical data.
    """
    data = _live_presets()
    source = "the running orchestrator"
    if data is None:
        data = _fetch_presets_or_defaults()
        # The resolved path, not a literal: PRAXIS_CONFIG_PATH can move this
        # file, and naming the wrong one sends the reader to edit a file the
        # process never reads.
        source = f"{config_file_path()} (orchestrator unreachable or not set up yet)"

    table = Table(title="Worker Presets")
    table.add_column("Name")
    table.add_column("Harness")
    table.add_column("Model")
    table.add_column("Needs")
    table.add_column("Default")
    for preset in data:
        requires = preset.get("requires") or []
        table.add_row(
            str(preset.get("name") or ""),
            str(preset.get("harness") or ""),
            str(preset.get("model") or "") or "-",
            ", ".join(str(r) for r in requires) if requires else "-",
            "yes" if preset.get("default") else "",
        )
    console.print(table)
    console.print(f"[dim]Source: {source}[/dim]")
    if data:
        console.print()
        for preset in data:
            name = preset.get("name") or ""
            console.print(f"praxis init --non-interactive --preset {name}")


@app.command("env")
def env() -> None:
    """Show where the CLI is pointing, and print the exports to make it explicit.

    The fallback that reads `.env` is deliberately quiet on every other verb,
    so this is the one place that says out loud which file it read and which
    token source won. A CLI that silently picked up the wrong install is worse
    than one that could not find any.
    """
    path, values = _env_file_values()

    # Named `*_from` rather than `*_source`: these hold a human-readable
    # description of WHERE a value came from, never the value itself, and
    # bandit's B105 flags any string literal assigned to a name containing
    # "token" as a hardcoded password. Renaming removes the false positive at
    # the source, which is better than adding B105 to the global skip list and
    # blinding the scan to the real thing everywhere else.
    url_from = f"built-in default (port {_DEFAULT_PORT})"
    if os.environ.get("ORCHESTRATOR_URL"):
        url_from = "ORCHESTRATOR_URL environment variable"
    elif (values.get("PORT") or "").strip().isdigit():
        url_from = f"PORT in {path}"

    auth_from = "not found"
    if os.environ.get("AUTH_TOKEN"):
        auth_from = "AUTH_TOKEN environment variable"
    elif os.environ.get("ORCHESTRATOR_TOKEN"):
        auth_from = "ORCHESTRATOR_TOKEN environment variable"
    elif values.get("AUTH_TOKEN") or values.get("ORCHESTRATOR_TOKEN"):
        auth_from = f"AUTH_TOKEN in {path}"

    console.print(f"URL:   {_api_url()}")
    console.print(f"       from {url_from}")
    console.print(f"Token: {auth_from}")
    if path is None:
        console.print(
            "\n[yellow]No .env found[/yellow] walking up from the current "
            "directory. Run this from your Praxis install directory."
        )
    if auth_from == "not found":
        raise typer.Exit(1)

    # The token is never printed above, only its source. It IS printed here,
    # because this block exists to be pasted into another shell and half an
    # export block is not usable. Anyone who can run this can already read
    # the same value out of `.env`.
    console.print("\nTo make this explicit in another shell:\n")
    console.print(f"  export ORCHESTRATOR_URL={_api_url()}", highlight=False)
    console.print(f"  export ORCHESTRATOR_TOKEN={_auth_token()}", highlight=False)
    console.print(
        f'  (PowerShell: $env:ORCHESTRATOR_URL="{_api_url()}"; '
        f'$env:ORCHESTRATOR_TOKEN="{_auth_token()}")',
        highlight=False,
    )


@app.command()
def onboard() -> None:
    """First-run helper: point the operator at model configuration."""
    console.print(
        "[bold]Welcome to Praxis.[/bold] No models configured yet.\n"
        "Run [cyan]praxis config[/cyan] to register models and set role fallback chains,\n"
        "or [cyan]praxis config show[/cyan] to view the current defaults."
    )


if __name__ == "__main__":
    app()
