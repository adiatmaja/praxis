"""Typer CLI client for the Praxis orchestrator API."""

from __future__ import annotations

import math
import os
import sys
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, NoReturn

import httpx
import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from cli.doctor import doctor as _doctor
from cli.init import _fetch_presets_or_defaults, parse_env
from cli.init import init as _init
from orchestrator.core.approvals import plan_awaits_approval
from orchestrator.core.run_elapsed import format_duration
from orchestrator.core.settings_file import config_file_path
from orchestrator.core.verify_gate import (
    SCOPE_VERIFY_PASSED,
    SCOPE_VERIFY_UNATTRIBUTED,
)


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
    try:
        data = response.json()
    except ValueError:
        return None
    # `isinstance` before `.get`, because the docstring above promises that
    # None covers every way the orchestrator can fail to answer usefully, and
    # a bare `data.get("presets")` does not deliver that: a 200 carrying a
    # JSON list raises AttributeError straight out of the CLI as a traceback,
    # which is the one outcome this fallback exists to prevent.
    if not isinstance(data, dict):
        return None
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


#: Server text is DATA, and rich reads a bare "[" as the start of a style tag.
#: There are two tools here and they are not interchangeable:
#:
#: - ``rich.text.Text`` for a value printed as its own console argument or put
#:   in a table cell. It renders literally and cannot be re-parsed. This is what
#:   `cli/doctor.py` uses for every string that came off the wire.
#: - ``rich.markup.escape`` for a value INTERPOLATED into a line that carries
#:   markup of ours, and for anything handed to :func:`_copyable`, which prints
#:   with markup ON so that ``_status_cell``'s already-escaped text renders
#:   right.
#:
#: Both halves of the failure are silent in their own way: ``[main]`` is DELETED
#: with nothing to say it happened, and ``[/dim]`` raises ``MarkupError`` out of
#: whatever command was printing. `_truncate_error` learned this for `plans`;
#: the siblings below had not.
def _check(response: httpx.Response) -> dict[str, Any] | list[dict[str, Any]]:
    if response.status_code >= 400:
        # `response.text` is the server's own detail, which for every route
        # behind `guard_repo_access` is decoded `git`/`gh` stderr: measured,
        # `harness [agy] is unknown; allowed: [opencode]` printed as
        # `harness is unknown; allowed:`, an error with every identifier it
        # exists to name removed. This checker is SHARED, so a MarkupError
        # raised here can take down any verb in the CLI.
        console.print(f"[red]Error {response.status_code}:[/red]", Text(response.text))
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


def _copyable(line: str) -> None:
    """Print a command line the operator is meant to select and paste.

    ``soft_wrap=True`` is the whole point. rich's default wrapping inserts a
    REAL newline at the console width, breaking on whitespace, so a command
    wider than the terminal arrives as two lines and selecting either one
    yields half a command. That is how `praxis reject <plan-id>` shipped with
    its verb on one row and its argument on the next, and how
    `praxis init --non-interactive --preset local-lmstudio
    --accept-preset-requirements` lost its trailing flag: the uuid itself
    never folds, because rich breaks only on whitespace, which is exactly why
    a wide terminal and a test pinned to one both looked fine.

    Soft wrapping emits the line whole and lets the TERMINAL wrap it for
    display, which keeps it one logical line for selection and one line when
    the output is redirected to a file.

    ``highlight=False`` for the same reason: rich's default highlighter
    colorizes things that look like data, and a uuid looks exactly like data,
    so on a colour-capable terminal it wraps ANSI escapes AROUND and sometimes
    INSIDE the id. It is invisible on screen and survives an ordinary
    selection, but it is still markup injected into the one string whose whole
    job is to be copied verbatim. A command is an instruction, not a value to
    be syntax-highlighted. `_print_run_log` already prints with the same flag,
    on the same reasoning.

    Args:
        line: The full command, already formatted.
    """
    console.print(line, soft_wrap=True, highlight=False)


@app.command()
def projects() -> None:
    """List all projects."""

    with _client() as client:
        data = _check_list(client.get("/api/projects"))
    table = Table(title="Projects")
    # No ID column, for the reason `tasks` and `plans` already carry. This
    # surface asserted the opposite in a comment ("a uuid, whole") while
    # `max_width=36` delivered nineteen characters: it is a MAXIMUM, so with
    # five columns competing for an 80-column console rich shrank the id and
    # folded every uuid across two rows. `add-project` prints a full id once at
    # creation and nothing else ever does, while `configure`, `submit`, and
    # `plans` all look a project up by EXACT match, so a folded id means that
    # from a new terminal tomorrow the documented path is unreachable without
    # curl. The id goes below the table as a copyable line instead.
    table.add_column("Name")
    table.add_column("Repo")
    table.add_column("Model")
    # Two DIFFERENT gates, and the column used to show only one of them under
    # the bare name "Gate". `approval_gate` decides whether an autonomous
    # IMPROVEMENT PLAN starts running unapproved; `auto_merge` decides whether
    # Praxis merges without a human, and it appeared on no CLI surface at all.
    # Reading a single "Gate: OFF" as "merges are automatic here" gets the more
    # dangerous of the two backwards, so both are named.
    table.add_column("Improve gate")
    table.add_column("Auto-merge")
    for project in data:
        # `Text` for every value off the wire (see the note on `_check`). A
        # project name is operator-typed and a `repo_url` can be a
        # `praxis-local://` ref carrying query parameters, so neither is safe
        # to hand to the markup parser. Only the two gate cells, which this
        # function writes itself, stay plain strings.
        table.add_row(
            Text(project["name"] or ""),
            Text(project["repo_url"] or ""),
            # `or ""` is not defensive padding: the API has always allowed a
            # null `model_name` (the project falls back to the deployment's
            # DEFAULT_WORKER_MODEL), and `Text` takes a str where `add_row`
            # took None for an empty cell.
            Text(project["model_name"] or ""),
            "ON" if project["approval_gate"] else "OFF",
            "ON" if project.get("auto_merge") else "OFF",
        )
    console.print(table)
    if not data:
        # The first command a newcomer runs after `praxis init`, and it printed
        # a bordered table with a header row and no body and nothing else. That
        # is indistinguishable from a query that returned nothing because it is
        # broken, and it names no way forward. `tasks` and `plans` got this
        # treatment in the same pass and this one was missed, which is its own
        # small lesson: three list surfaces, one fix, and the miss landed on the
        # surface with the most first-time traffic.
        console.print(
            "\nNo projects yet. Register one with "
            "'praxis add-project <name> <repo-url>'."
        )
        return
    console.print()
    for project in data:
        # `escape`, not `Text`: `_copyable` prints with markup ON, because
        # `plans` feeds it `_status_cell` output that is already escaped.
        _copyable(f"praxis plans {project['id']}   # {escape(project['name'] or '')}")


@app.command()
def add_project(
    name: str = typer.Argument(..., help="Project display name"),
    repo: str = typer.Argument(..., help="GitHub repo URL"),
    # Optional because the API has always allowed `model_name` to be null and
    # fall back to the deployment's configured worker model. Requiring it here
    # asked the operator for a value that is undiscoverable under the shipped
    # default, whose model is named only in the settings file and nowhere the
    # CLI prints. Still positional, so existing invocations are unchanged.
    #
    # Both help strings below name DEFAULT_WORKER_*, because that is what the
    # API actually reads (`body.model_name or settings.default_worker_model`).
    # They used to say "the configured worker preset", which is a different
    # thing: `praxis presets` flags a preset with `default: true`, and that
    # flag changes what `praxis init` OFFERS, not what an omitted flag
    # resolves to. After `praxis init --preset local-lmstudio` the two
    # disagree, and the help was a claim about resolution order that was then
    # wrong. This is the second time this line has asserted the wrong default,
    # so say which variable is read and stop naming an intermediary.
    model: str | None = typer.Argument(
        None,
        help=(
            "Worker model for this project. Omit to use DEFAULT_WORKER_MODEL, "
            "or the settings file's default_worker_model. If neither is set "
            "the server refuses with a 422 rather than guessing."
        ),
    ),
    harness: str | None = typer.Option(
        None,
        "--harness",
        help=(
            "Coding harness this project's worker runs in, e.g. 'opencode' or "
            "'agy'. Omit to use DEFAULT_WORKER_HARNESS, or the settings file's "
            "default_worker_harness. The server rejects an unknown value and "
            "names the allowed set."
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
        response = client.post("/api/projects", json=body)
        # Checked on the FAILURE path only, so a correctly-typed repo path or
        # URL never sees this note: Git Bash's MSYS layer rewrites a leading
        # '/' argument before `praxis` ever runs, and `praxis add-project ...
        # /run/desktop/mnt/host/c/...` arrives here as a path rooted at the
        # Git install directory, which then 422s with a confusing "path does
        # not exist" that names nothing about the shell that caused it.
        #
        # Folded into one message rather than printed before `_check_dict`:
        # the server's own error is the CONCLUSION here and the hint is the
        # REMEDY, so the remedy has to be the last thing on screen, the same
        # standard `praxis doctor` holds every diagnostic to. Printed first,
        # it would scroll off above the "Error 422: ..." line the operator
        # actually reads last.
        if response.status_code >= 400 and _looks_msys_mangled(repo):
            # `Text`, for the reason on `_check`: this is the same server
            # detail, printed by a second copy of the same line.
            console.print(
                f"[red]Error {response.status_code}:[/red]", Text(response.text)
            )
            console.print(
                "[yellow]Your shell rewrote this path.[/yellow] Git Bash "
                "converts a leading '/' into an MSYS path. Re-run with "
                "MSYS_NO_PATHCONV=1 prefixed to the command."
            )
            raise typer.Exit(1)
        data = _check_dict(response)
    console.print(f"[green]Created project:[/green] {data['id']}")


#: Case-insensitive: Git Bash's MSYS layer intercepts an argv token that
#: starts with a leading '/' and rewrites it to an absolute path rooted at
#: the Git installation directory, in either slash style, before `praxis`
#: ever sees it. That is the giveaway a mangled value carries: the ORIGINAL
#: argument is gone by the time this process starts, so there is nothing to
#: recover, only to recognize. `MSYSTEM` being set is not enough on its own:
#: it is true for every Git Bash session, including ones passing a perfectly
#: normal path or a `https://` URL that must not trip this message.
#:
#: Only covers the admin-default install location. A non-admin install
#: (`%LOCALAPPDATA%\Programs\Git`), a `D:` (or other) drive, or a bare
#: `C:\Git` will not match, so a mangled path from one of those still 422s
#: with the raw, un-annotated server error rather than this hint. That is a
#: silent miss, never a false alarm: it is exactly today's behaviour for
#: those installs, not a regression.
_MSYS_PATH_PREFIXES = ("c:/program files/git/", "c:\\program files\\git\\")


def _looks_msys_mangled(value: str) -> bool:
    """True when `value` looks like a leading '/' Git Bash rewrote.

    Pure and testable without a subprocess: the tell is the rewritten VALUE
    itself (it starts inside the Git install directory), not the shell that
    produced it.

    Args:
        value: The raw `repo_url`/path argument as the CLI received it.

    Returns:
        True when `value` starts with the MSYS-rewritten prefix, in either
        slash style, case-insensitively.
    """
    return value.strip().lower().startswith(_MSYS_PATH_PREFIXES)


@app.command()
def configure(
    project_id: str = typer.Argument(..., help="Project ID"),
    gate: bool | None = typer.Option(
        None,
        help=(
            "Gate autonomous improvement plans before they run. This is NOT "
            "the merge gate: a reviewed PR always waits for a human unless "
            "auto_merge is on for the project."
        ),
    ),
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
    default_branch: str | None = typer.Option(
        None,
        "--default-branch",
        help=(
            "The base branch this project's plans are cut from and "
            "integrated into. Refused with 422 while any plan is pending or "
            "active."
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
    if default_branch is not None:
        body["default_branch"] = default_branch
    if not body:
        console.print("[yellow]No settings to update[/yellow]")
        return
    with _client() as client:
        data = _check_dict(client.patch(f"/api/projects/{project_id}", json=body))
    # A project name is operator-typed, so this is the same value `projects`
    # puts in a table cell and the same hazard: `Text` per the note on `_check`.
    console.print("[green]Updated project:[/green]", Text(data["name"] or ""))


def _not_utf8(what: str, exc: UnicodeDecodeError) -> NoReturn:
    """Refuse a specification that is not UTF-8, naming the encoding and the fix.

    The encoding has to be in the message. "Cannot read spec file X" alone
    sends the operator looking for a permissions or path problem, when the file
    is right there and readable; the only thing wrong with it is how it was
    saved, and that is something they can fix in thirty seconds once told.

    Args:
        what: How to refer to the input, e.g. ``spec file C:\\x.md``.
        exc: The decode failure, whose offset locates the offending byte.

    Raises:
        typer.Exit: Always, with code 1.
    """
    console.print(
        f"[red]Cannot read {escape(what)}: it is not valid UTF-8.[/red] "
        f"({exc.reason} at byte {exc.start})",
        soft_wrap=True,
    )
    console.print(
        "Specs are read as UTF-8. Re-save the file with UTF-8 encoding "
        "(Notepad: Save As -> Encoding: UTF-8) and run this again. A single "
        "smart quote pasted from Word is enough to trip this.",
        soft_wrap=True,
    )
    raise typer.Exit(1) from exc


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
            try:
                spec_text = sys.stdin.buffer.read().decode("utf-8")
            except UnicodeDecodeError as exc:
                _not_utf8("the piped specification", exc)
        else:
            try:
                spec_text = file.read_text(encoding="utf-8")
            except OSError as exc:
                # soft_wrap=True: a narrow console (this project's own test
                # suite runs at COLUMNS=80) would otherwise word-wrap the
                # path mid-token, the exact defect class `pending`/`tasks`
                # already had to fix for uuids.
                console.print(
                    f"[red]Cannot read spec file {escape(str(file))}:[/red] {exc}",
                    soft_wrap=True,
                )
                raise typer.Exit(1) from exc
            except UnicodeDecodeError as exc:
                # A `UnicodeDecodeError` is a `ValueError`, NOT an `OSError`,
                # so it walked straight past the handler above and out of the
                # CLI as a raw traceback -- on the most ordinary input this
                # platform produces. A spec saved by Notepad as ANSI, or one
                # carrying a single smart quote pasted out of Word, is cp1252,
                # and the traceback names a decoder frame rather than the file
                # or the remedy.
                _not_utf8(f"spec file {file}", exc)
    else:
        spec_text = spec

    with _client() as client:
        try:
            response = client.post(
                f"/api/projects/{project_id}/plans", json={"spec": spec_text}
            )
        except httpx.ReadTimeout as exc:
            # This endpoint clones the target repo to commit the spec doc
            # BEFORE it answers, which is the one request in this CLI that
            # regularly outruns `_DEFAULT_TIMEOUT` on a large repo. The
            # request DID reach the server, so the plan may well already
            # exist; only the response never arrived, which makes this an
            # unknown to resolve, not a failure to report. Caught FIRST and
            # separately from `httpx.RequestError` below for exactly that
            # reason: it is the one case in this whole family where something
            # may have survived server-side.
            console.print(
                "[yellow]Timed out waiting for the server, but the plan "
                "may have been created:[/yellow] the spec is committed to "
                "the repository before the response returns."
            )
            console.print("Check with:")
            _copyable(f"praxis plans {project_id}")
            raise typer.Exit(1) from exc
        except httpx.RequestError as exc:
            # The parent of every OTHER "never got an answer" shape httpx can
            # raise here: `ConnectTimeout` (the TCP connect itself hung) and
            # `ConnectError` (nothing was listening at all - the orchestrator
            # simply not running, which used to exit 1 with empty stdout and
            # no diagnostic, the same defect class this command exists to
            # close). None of them reached the server, so none of them get
            # the "may have been created" line above; that would send an
            # operator to go check for a plan that provably does not exist.
            console.print(
                "[red]Could not reach the server[/red] "
                f"({_api_url()}): {exc}. The request never reached it, so "
                "no plan was created."
            )
            raise typer.Exit(1) from exc
        data = _check_dict(response)
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
    if not data:
        console.print(
            f"\nNo plans for project {project_id}. "
            "Create one with 'praxis submit <project-id> --file spec.md'."
        )
        return
    console.print()
    for plan in data:
        _copyable(f"praxis tasks {plan['id']}   # {_status_cell(plan)}")
        # NOT an arm of the chain below, deliberately. Being wedged on a failed
        # dependency and having an integration PR open are independent facts
        # about a plan, and folding this into the `elif` ladder would let one
        # of them suppress the other -- which is the "a conditional copyable
        # line reads as a working one right up until you need the id it
        # withheld" defect this very block was rewritten to fix.
        #
        # The ids printed are the BLOCKERS, never the blocked leaves. Only a
        # `failed` task can be retried (the endpoint answers 409 for anything
        # else), so a copyable `praxis retry <blocked-leaf-id>` would be an
        # offer that cannot be taken. The blocked leaves are a COUNT in the
        # status cell above and are listed by `praxis tasks`; one copyable
        # line per blocker, because each is a separate command to run and an
        # id sharing a line with another id cannot be selected cleanly.
        blockers = plan.get("stalled_blocked_by_task_ids") or []
        if blockers:
            stuck = plan.get("stalled_task_ids") or []
            noun = "task" if len(stuck) == 1 else "tasks"
            # No plan id and no task id in this sentence: it wraps at 80
            # columns and rich would fold a uuid across the break, which 404s
            # when it is copied. The ids live on the copyable lines only.
            console.print(
                f"  Stalled: {len(stuck)} {noun} can never be dispatched, "
                "because a dependency failed terminally and the loop never "
                "revisits it. Nothing here is waiting on the orchestrator."
            )
            for blocker in blockers:
                _copyable(
                    f"praxis retry {escape(str(blocker))}"
                    "   # release the tasks waiting on it"
                )
        # One next-step line per STATE, not only for the one state that had
        # one. A plan whose only offered verb was `praxis tasks` left the
        # reader at an empty table with no way forward: a pending autonomous
        # proposal needs approve or reject, and a completed plan with no
        # integration PR needs to be told that, since its work is on the plan
        # branch and nothing points at it.
        if plan.get("integration_pr_url") and not plan.get("integration_merged_at"):
            _copyable(
                f"praxis merge-plan {plan['id']}   # integrate onto the base branch"
            )
            _copyable(f"  PR: {plan['integration_pr_url']}")
        elif plan.get("status") == "pending" and plan.get("source") == "autonomous":
            _copyable(f"praxis approve {plan['id']}   # dispatch it")
            _copyable(f"praxis reject {plan['id']}   # close it")
        elif plan.get("status") == "completed" and not plan.get(
            "integration_merged_at"
        ):
            # The FULL recorded reason, not the 60-char cell preview: it names
            # the branch the work is stranded on and the base it never reached,
            # and both are what an operator needs to go find it.
            reason = plan.get("error")
            if reason:
                # markup=False for the same reason `praxis logs` uses it: this
                # is server text, and rich would eat a bracketed branch name or
                # raise on a closing-shaped token. highlight=False because a
                # reason is prose, not a value to be syntax-coloured.
                console.print(
                    f"  No integration PR. The server recorded: {reason}",
                    markup=False,
                    highlight=False,
                )
            else:
                # This line used to assert the stranded reading for every plan
                # that reached it, and it is false for the commoner one: a
                # single-branch plan whose task PRs were merged HAS reached the
                # base branch and has no plan branch left, so the reader was
                # sent looking for a branch that had been deleted. Nothing on
                # the wire settles it, so both readings are named and the
                # reader is pointed at the verb that does.
                # No plan id in this sentence, deliberately. It wraps at 80
                # columns and rich would fold a uuid across the break, which
                # 404s when it is copied. The copyable `praxis tasks <id>` line
                # printed above every plan is where the id lives.
                console.print(
                    "  No integration PR, and no reason was recorded. Either "
                    "the work already reached the base branch (its task PRs "
                    "were merged, or every task was a no-op), or it is on the "
                    "plan branch and did not. The 'praxis tasks' line above "
                    "shows which."
                )


#: How much of a stored planner error to show inline in the status cell. A
#: raw response excerpt runs to hundreds of characters, and unlike a plan id
#: it carries no lookup contract, so truncating it is safe where truncating
#: an id never would be.
_ERROR_PREVIEW_LEN = 60


def _truncate_error(error: str) -> str:
    """Collapse, shorten and escape a stored plan error for one status-cell line.

    Whitespace is collapsed first: `plans.error` can carry a multi-line raw
    excerpt, and a literal newline in a table cell wraps unpredictably.

    Rich markup is escaped LAST, and it is not cosmetic. This text is written
    by the server from `gh` output and exception reprs, and a rich table cell
    parses `[...]` as a style tag: `gh: [main] not a branch` rendered as
    `gh:  not a branch`, with the branch name silently deleted from the one
    line that was supposed to explain the failure, and any error carrying a
    closing-shaped token (`[/dim]`) raised `MarkupError` and took the whole of
    `praxis plans` down with it. Escaping after truncation keeps the preview
    budget counting the characters the operator actually reads.
    """
    collapsed = " ".join(error.split())
    if len(collapsed) > _ERROR_PREVIEW_LEN:
        return escape(collapsed[:_ERROR_PREVIEW_LEN].rstrip() + " ...")
    return escape(collapsed)


def _status_cell(plan: dict[str, Any]) -> str:
    """Render a plan's status with what actually happened to its work.

    "completed" alone answers the wrong question. It means every task merged
    onto the PLAN branch, which a reader hears as "landed on main". The suffix
    says which it really is: integrated, waiting on an open PR, or completed
    with no PR at all.

    That last one is two states, not one, and they are opposites: there was
    nothing to integrate because the work already reached the base branch, or
    the integration PR could not be opened and the work is stranded. Only the
    stranded one records a reason on `plans.error`, so a reason present is
    printed and a reason absent is left unclaimed. `plans` names both readings
    under the table; see the comment on the `completed` arm below.

    Deliberately a suffix rather than a fifth column. The ID column has to
    stay wide enough to print a uuid contiguously, since every other verb
    looks a plan up by exact match, and a narrow console splits a folded id
    across border characters.

    An `active`/`pending` plan gets the same treatment for a different
    reason: a planner stuck retrying JSON extraction forever looks IDENTICAL
    from here to a plan decomposing normally, both print a bare `active`, and
    `praxis tasks` says "has no tasks yet" for both too. `error`,
    `plan_attempts` and `max_planning_attempts` are all read with `.get`,
    defaulting to absent/zero, so a server that predates any of them renders
    exactly as it did before that field existed.
    """
    status_text = str(plan.get("status") or "")
    if plan.get("integration_merged_at"):
        return f"{status_text} (integrated)"
    if plan.get("integration_pr_url"):
        return f"{status_text} (PR open)"
    if status_text == "completed":
        # `(no PR)` alone covered two outcomes that mean opposite things, and
        # `on_plan_completed` now tells them apart: the STRANDED one records
        # why on `plans.error`, and the nothing-to-integrate one deliberately
        # records nothing, because merging the task PRs already put the work on
        # the base branch. So a reason present is worth printing verbatim.
        #
        # One way only. An absent reason is not proof the work landed:
        # `reset_plan_attempts` clears a recovered plan's attempt COUNT and
        # leaves `plans.error` alone, and several early-return paths through
        # `on_plan_completed` record nothing at all. The bare cell therefore
        # stays exactly what it was, and `plans` names both readings under the
        # table rather than either surface asserting one.
        reason = plan.get("error")
        if reason:
            return f"{status_text} (no PR; {_truncate_error(str(reason))})"
        # Since migration 14 the stage RECORDS the no-op outcome, so the bare
        # cell is no longer the only honest rendering: a recorded
        # `nothing_to_integrate` means the work already reached base, and a
        # NULL on a completed plan means the stage has not recorded anything
        # yet (the PR is being opened). A server older than the column sends
        # neither and renders exactly as before.
        state = plan.get("integration_state")
        if state == "nothing_to_integrate":
            return f"{status_text} (no PR; already on base)"
        if state is None and "integration_state" in plan:
            return f"{status_text} (integrating)"
        return f"{status_text} (no PR)"
    if status_text in ("active", "pending"):
        # AHEAD of the planning arm below, and the order is the point. A plan
        # wedged on a FAILED dependency has finished planning; what it carries
        # on `plans.error` is whatever the last planning attempt recorded, and
        # `reset_plan_attempts` clears the COUNT without clearing the error, so
        # a recovered plan keeps a stale one indefinitely. Letting that arm win
        # would print "active (planning; last error: ...)" over a plan that is
        # not planning and cannot be fixed by waiting -- describing the wrong
        # obstacle at exactly the moment somebody needs the right one.
        #
        # `.get` with an empty default, like every other field read here: a
        # server that predates `stalled_task_ids` sends nothing and this cell
        # then renders exactly as it did before the field existed. The falsy
        # default is also the SAFE polarity, since claiming a live plan is
        # wedged is the more expensive mistake of the two.
        blocked = plan.get("stalled_task_ids") or []
        if blocked:
            noun = "task" if len(blocked) == 1 else "tasks"
            return (
                f"{status_text} (stalled; {len(blocked)} {noun} blocked by a failure)"
            )
        attempts = plan.get("plan_attempts") or 0
        error = plan.get("error")
        # The denominator comes off the WIRE (`PlanResponse.max_planning_attempts`),
        # never from a constant here. This file used to mirror the engine's cap
        # and admit in a comment that nothing kept the copy honest: raising the
        # engine's constant printed "attempt 4/3" at an operator -- a
        # denominator saying the plan is already dead -- with the suite green.
        # A server too old to send it gets no denominator rather than a guessed
        # one, which is exactly what this cell printed before the cap existed.
        cap = plan.get("max_planning_attempts")
        if attempts or error:
            detail = "planning"
            if attempts:
                detail += (
                    f", attempt {attempts}/{cap}" if cap else f", attempt {attempts}"
                )
            if error:
                detail += f"; last error: {_truncate_error(str(error))}"
            return f"{status_text} ({detail})"
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
        # A title is decomposer output and a branch name is derived from it,
        # so `refactor [core] parser` is an ordinary value here, not an exotic
        # one. `Text` per the note on `_check`.
        # The elapsed time rides INSIDE the status cell rather than in a fifth
        # column: this table already lost its id column to rich shrinking five
        # columns on an 80-column console, and a duration is only meaningful
        # for the one status that carries it.
        table.add_row(
            Text(task["title"] or ""),
            Text(task["branch_name"] or ""),
            _task_status_cell(task["status"], task.get("running_for_seconds")),
            str(task["attempt"]),
        )
    console.print(table)
    if not data:
        # A bordered table with a header row and no body is indistinguishable
        # from "the plan failed to decompose", and this is the first thing an
        # operator types after `praxis plans`.
        console.print(
            f"\nPlan {plan_id} has no tasks yet. A pending plan has none until "
            "it is decomposed; check its status with 'praxis plans "
            "<project-id>'."
        )
        return
    console.print()
    for task in data:
        # The title appears TWICE on this screen and each site escapes on its
        # own; fixing only the table left the copyable line still eating it.
        _copyable(f"praxis task {task['id']}   # {escape(task['title'] or '')}")


@app.command()
def task(task_id: str = typer.Argument(..., help="Task ID")) -> None:
    """Get task details with agent run history.

    Every line here is server text, and `review_feedback` is the one that made
    this urgent: the orchestrator writes its own bracketed safety markers into
    it (`[supply-chain] Blocked: ...`, `[diff-guard] Warning: ...`), so rendered
    as markup this surface DELETED the word saying a dependency had been
    blocked -- on the screen a human reads before approving a merge. Feedback
    carrying a closing-shaped token raised `MarkupError` and exited 1 with a
    traceback. See the note on `_check`.
    """

    with _client() as client:
        data = _check_dict(client.get(f"/api/tasks/{task_id}"))
    task_data = data["task"]
    console.print(Text(task_data["title"] or "", style="bold"))
    console.print(
        Text(
            "Status: "
            f"{task_data['status']} | Branch: {task_data['branch_name']} | "
            f"Attempt: {task_data['attempt']}"
        )
    )
    if task_data["pr_url"]:
        console.print(Text(f"PR: {task_data['pr_url']}"))
    # Unconditional whenever a run is open, and ABOVE the review feedback: this
    # is the one fact that was missing when a worker ran for about two hours on
    # somebody's own hardware and no surface said so. A wedged worker, a slow
    # worker and one burning a GPU are the same row without it.
    running_line = _running_line(data.get("running_for_seconds"))
    if running_line:
        console.print(Text(running_line))
    # BEFORE the feedback, and unconditionally when there is something to say:
    # this is the fact the feedback structurally cannot carry, because the
    # reviewer grades the diff against the LEAF's plan_text. A human inspecting
    # one task is exactly who needs it, and putting it under a long model
    # prose block is the same as not printing it.
    #
    # `praxis pending` renders this too. Both, on purpose: pending is the queue
    # and this is the detail view, and a fact that only appears in the queue is
    # invisible to anyone who went straight to the task.
    drift_line = _drift_line(task_data.get("contract_drift"))
    if drift_line:
        console.print(Text(drift_line))
    if task_data["review_feedback"]:
        console.print("[yellow]Feedback:[/yellow]", Text(task_data["review_feedback"]))
    for run in data["runs"]:
        console.print(
            Text(
                f"  {run['id'][:8]} | {run['status']} | {run['started_at']}"
                f" | {format_duration(run.get('elapsed_seconds'))}"
            )
        )


#: One server-side wait blocks at most this long (``core/waiting``'s cap); the
#: client budget for that request is the cap plus a round trip. `wait` loops
#: on the endpoint up to its own ``--timeout``, so the HTTP client never ends
#: a wait: the server does, and its answer carries the state.
_WAIT_SERVER_CAP = 90.0
_WAIT_CLIENT_TIMEOUT = _WAIT_SERVER_CAP + 30.0

#: Exit code when ``--timeout`` passes with the engine still moving. Distinct
#: from 1 (an error) so a script can tell "ask again later" from "broken".
_WAIT_TIMED_OUT_EXIT = 2


def _wait_kind(client: httpx.Client, target_id: str) -> str:
    """``"task"`` or ``"plan"`` for the id, or exit 1 when it is neither."""
    if client.get(f"/api/tasks/{target_id}").status_code == 200:
        return "task"
    if client.get(f"/api/plans/{target_id}").status_code == 200:
        return "plan"
    console.print(
        Text(f"{target_id} is neither a task id nor a plan id on this server.")
    )
    raise typer.Exit(1)


def _wait_rest_lines(kind: str, target_id: str, body: dict[str, Any]) -> None:
    """Say why the engine stopped and name the verb that moves it, copyably."""
    status_text = str(body.get("status") or "")
    # BEFORE the waiting_on split: an open integration PR is the plan's own
    # merge gate, so the server reports it as waiting on a HUMAN, and a landed
    # one as nothing. Under the "nothing" arm alone, the live run that
    # accepted this verb printed "plan completed" and no verb at all.
    if kind == "plan" and body.get("integration_pr_url"):
        if body.get("integration_merged_at"):
            console.print(Text(f"Plan {status_text}: the work landed on base."))
        else:
            console.print(
                Text(
                    f"Plan {status_text}: integration PR "
                    f"{body['integration_pr_url']} is open; land it with"
                )
            )
            _copyable(f"  praxis merge-plan {target_id}")
        return
    if body.get("waiting_on") == "nothing":
        if kind == "plan" and body.get("integration_state") == "nothing_to_integrate":
            console.print(
                Text(
                    "Plan completed: nothing to integrate, the work already "
                    "reached the base branch through the task PRs."
                )
            )
            return
        if status_text == "failed":
            reason = body.get("error") or (body.get("task") or {}).get(
                "review_feedback"
            )
            console.print(Text(f"{kind.capitalize()} failed: {reason or 'no reason'}"))
            if kind == "task":
                console.print(Text("Retry after changing something with"))
                _copyable(f"  praxis retry {target_id}")
            return
        console.print(Text(f"Nothing more will happen: {kind} is {status_text}."))
        return
    # waiting_on == "human"
    if kind == "task":
        blocked = body.get("blocked_by") or {}
        if status_text == "pending" and (blocked.get("gated") or blocked.get("failed")):
            for dep in blocked.get("gated") or []:
                console.print(
                    Text(
                        "Blocked behind a leaf at the merge gate: "
                        f"{dep.get('title') or dep.get('task_id')} "
                        f"{dep.get('pr_url') or ''}"
                    )
                )
                _copyable(f"  praxis merge {dep.get('task_id')}")
            for dep in blocked.get("failed") or []:
                console.print(
                    Text(
                        "Blocked behind a terminally failed leaf: "
                        f"{dep.get('title') or dep.get('task_id')}"
                    )
                )
                _copyable(f"  praxis retry {dep.get('task_id')}")
            return
        if status_text == "needs_clarification":
            question = (body.get("task") or {}).get("clarification_question") or ""
            console.print(Text(f"Worker asked: {question}"))
            console.print(Text("Answer with"))
            _copyable(f'  praxis clarify {target_id} "<answer>"')
        else:
            console.print(Text(f"Parked at the merge gate: {body.get('pr_url')}"))
            console.print(Text("Approve with"))
            _copyable(f"  praxis merge {target_id}")
        return
    if plan_awaits_approval(body):
        console.print(
            Text(
                "Parked at the proposal gate: an autonomous improvement proposal "
                "nobody has approved. Dispatch it or close it with"
            )
        )
        _copyable(f"  praxis approve {target_id}")
        _copyable(f"  praxis reject {target_id}")
        return
    blocking = [str(t) for t in (body.get("stalled_blocked_by_task_ids") or [])]
    if blocking:
        console.print(
            Text("Stalled: a pending leaf sits behind a terminally failed one.")
        )
        for task_id in blocking:
            _copyable(f"  praxis retry {task_id}")
        return
    for leaf in body.get("tasks") or []:
        if leaf.get("status") == "passed":
            console.print(
                Text(f"Leaf parked at the merge gate: {leaf.get('pr_url') or ''}")
            )
            _copyable(f"  praxis merge {leaf.get('task_id')}")
        elif leaf.get("status") == "needs_clarification":
            console.print(Text("Leaf asked a question; answer with"))
            _copyable(f'  praxis clarify {leaf.get("task_id")} "<answer>"')


def _wait_print_transitions(
    kind: str,
    body: dict[str, Any],
    seen: dict[str, str] | None,
) -> dict[str, str]:
    """Print what moved since the last answer; return the leaf map for next time.

    Every line is server text: titles carry brackets, so ``Text``.
    """
    stamp = time.strftime("%H:%M:%S")
    status_text = str(body.get("status") or "")
    if kind == "task":
        if seen is None:
            console.print(Text(str((body.get("task") or {}).get("title") or "")))
        if body.get("changed"):
            line = f"{stamp}  {body.get('previous')} -> {status_text}"
            attempt = body.get("attempt")
            if isinstance(attempt, int) and attempt > 1:
                line += f" (attempt {attempt})"
            if body.get("pr_url"):
                line += f"  {body['pr_url']}"
            console.print(Text(line))
        elif seen is None:
            console.print(Text(f"{stamp}  {status_text}"))
        return {}
    leaves = {
        str(t.get("task_id")): str(t.get("status")) for t in body.get("tasks") or []
    }
    titles = {
        str(t.get("task_id")): str(t.get("title") or t.get("task_id"))
        for t in body.get("tasks") or []
    }
    if seen is None:
        console.print(Text(f"{stamp}  plan {status_text}"))
        for task_id, leaf_status in leaves.items():
            console.print(Text(f"{stamp}    {titles[task_id]}: {leaf_status}"))
        return leaves
    if body.get("previous") != status_text:
        console.print(Text(f"{stamp}  plan {body.get('previous')} -> {status_text}"))
    for task_id, leaf_status in leaves.items():
        before = seen.get(task_id)
        if before is None:
            console.print(Text(f"{stamp}    {titles[task_id]}: {leaf_status}"))
        elif before != leaf_status:
            console.print(
                Text(f"{stamp}    {titles[task_id]}: {before} -> {leaf_status}")
            )
    return leaves


@app.command()
def wait(
    target_id: str = typer.Argument(..., help="Task ID or plan ID"),
    timeout: float = typer.Option(
        900.0,
        "--timeout",
        help="Seconds to wait at most before exiting 2 (default 15 minutes).",
    ),
) -> None:
    """Block until a task or plan comes to rest, printing each transition.

    Built on the server's wait endpoint, so there is no poll loop to write
    and no field to read wrong. Exits 0 when the engine has nothing more to
    do by itself (a terminal state, or parked on a person: the last lines
    say which and name the verb), exits 2 when --timeout passed with the
    engine still moving, and 1 on any error.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    with _client(_WAIT_CLIENT_TIMEOUT) as client:
        kind = _wait_kind(client, target_id)
        seen: dict[str, str] | None = None
        fingerprint: str | None = None
        body: dict[str, Any] = {}
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # The server is asked for the WHOLE remaining budget when that fits
            # under its cap, so a timed-out answer to such a request IS the
            # deadline: the budget is judged by what was asked, not by a wall
            # clock the server's own wait already consumed.
            asked = math.ceil(min(_WAIT_SERVER_CAP, remaining))
            params: dict[str, Any] = {"timeout": f"{asked:g}"}
            if fingerprint:
                params["fingerprint"] = fingerprint
            body = _check_dict(
                client.get(f"/api/{kind}s/{target_id}/wait", params=params)
            )
            seen = _wait_print_transitions(kind, body, seen)
            fingerprint = str(body.get("fingerprint") or "") or None
            if body.get("waiting_on") in {"human", "nothing"}:
                _wait_rest_lines(kind, target_id, body)
                return
            if body.get("timed_out") and asked >= remaining:
                break
    status_text = str(body.get("status") or "unknown")
    line = f"Timed out: still {status_text} after {format_duration(timeout)}"
    running = body.get("running_for_seconds")
    if isinstance(running, int | float) and not isinstance(running, bool):
        line += f", running for {format_duration(float(running))}"
    console.print(Text(line))
    console.print(Text("Keep waiting with"))
    _copyable(f"  praxis wait {target_id}")
    raise typer.Exit(_WAIT_TIMED_OUT_EXIT)


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
    """Stop a task: kill any running agent AND mark the task failed.

    The second half is not a side effect, it is most of what this does. The
    endpoint sets the task FAILED and drops its stored worker session
    unconditionally, outside the loop over running containers, so `stop` on a
    task with nothing running still ends the task and discards the
    conversation a resume would have replayed. Described only as "stop a
    running agent", and printing only "Stopped 0 agent(s)", it read as a
    no-op. `praxis task <id>` will show FAILED afterwards either way.
    """

    with _client() as client:
        data = _check_dict(client.post(f"/api/tasks/{task_id}/stop"))
    # `stopped` counts run ROWS closed, which is NOT the number of containers
    # killed. On a host with no Docker every row still closes and the old line
    # said "Stopped 1 agent(s)" having contacted nothing, while suppressing the
    # caveat below because `stopped` was truthy: the operator walked away
    # believing a container was dead that is still running.
    stopped = data["stopped"]
    containers = data.get("containers_stopped", stopped)
    docker_available = data.get("docker_available", True)
    console.print(
        f"[yellow]Stopped {containers} container(s)[/yellow]; task {task_id} is "
        "now failed and its worker session was cleared."
    )
    if not docker_available:
        console.print(
            "[red]Docker was not reachable, so no container was signalled.[/red] "
            f"{stopped} run row(s) were closed; anything still running has to be "
            "stopped by hand."
        )
    elif containers < stopped:
        console.print(
            f"[red]{stopped - containers} container(s) could not be stopped[/red] "
            "and may still be running; see the orchestrator log."
        )
    elif not stopped:
        console.print(
            "[dim]No container was running, so only the task status changed.[/dim]"
        )


def _read_back_status(client: httpx.Client, task_id: str) -> str | None:
    """Return a task's current status, or None when it cannot be established.

    Every failure mode collapses to None on purpose. This is called only to
    make a refusal explicable, so a guess here would put a fabricated fact on
    the screen an operator acts from, and the caller has a third phrasing for
    exactly that. Same polarity as the unknown context window and the blank
    verify command: the state that cannot be settled is reported, never folded
    into one of the two that can.
    """
    try:
        response = client.get(f"/api/tasks/{task_id}")
    except httpx.RequestError:
        return None
    if response.status_code >= 400:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    task_row = body.get("task")
    if not isinstance(task_row, dict):
        return None
    status_value = task_row.get("status")
    return status_value if isinstance(status_value, str) and status_value else None


def _plan_state_after_retry(client: httpx.Client, plan_id: Any) -> str | None:
    """Return the owning plan's status after a requeue, or None if unreadable.

    Every failure mode collapses to None for the same reason
    :func:`_read_back_status` does: this exists to make a claim on screen
    honest, so a guess would defeat its whole purpose, and the caller has a
    third phrasing for the unknown case.

    Args:
        client: The live HTTP client, inside the ``_client()`` context.
        plan_id: ``plan_id`` off the retry response, of unknown shape.

    Returns:
        The plan status string, or None when it could not be established.
    """
    if not isinstance(plan_id, str) or not plan_id:
        return None
    try:
        response = client.get(f"/api/plans/{plan_id}")
    except httpx.RequestError:
        return None
    if response.status_code >= 400:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    status_value = body.get("status")
    return status_value if isinstance(status_value, str) and status_value else None


def _report_plan_state_after_retry(plan_status: str | None) -> None:
    """Say whether anything will actually dispatch the leaf that was requeued.

    The line the defect of 2026-08-27 needed and did not have. `praxis retry`
    printed "watch it leave pending and pick up again" for a leaf on a plan
    the engine had written `failed`, which `get_runnable_plans` never returns,
    so the leaf sat pending forever and the only symptom was silence.

    Three states, kept distinct, because folding the third into either of the
    others is the same class of lie: the plan is back in the loop's reach, the
    plan is still stopped and nothing will run, or the plan could not be read
    back and this command declines to say.

    Args:
        plan_status: What :func:`_plan_state_after_retry` established.
    """
    if plan_status is None:
        # Deliberately shares no phrase with either answer above it. An
        # operator skims this line, and "the loop will pick this up" inside a
        # sentence that means the opposite is read as the sentence it quotes.
        console.print(
            "  Its plan's status could not be read back, so whether anything "
            "will dispatch this leaf is not stated here rather than guessed."
        )
        return
    if plan_status == "failed":
        console.print(
            "[yellow]  Its plan is still failed, so NOTHING will dispatch "
            "this leaf:[/yellow]",
            Text(
                "the loop only looks at pending and active plans. Requeuing "
                "was supposed to reactivate it and did not."
            ),
        )
        return
    if plan_status in ("rejected", "completed"):
        console.print(
            "[yellow]  Its plan is[/yellow]",
            Text(
                f"{plan_status}, so the loop will not dispatch this leaf. That "
                "is deliberate: reactivating would overturn a decision "
                "somebody already made."
            ),
        )
        return
    # ``Text``, not an f-string, for the same reason the two branches above use
    # it: ``plan_status`` came off the wire, and this client explicitly refuses
    # to decide what the server's vocabulary is. rich reads a bare ``[`` as a
    # style tag, so a status it does not recognise would be silently DELETED
    # from the line, or raise ``MarkupError`` out of the verb.
    console.print(
        "  Its plan is",
        Text(f"{plan_status}, so the loop will pick this up."),
    )


def _refused_retry(client: httpx.Client, task_id: str) -> NoReturn:
    """Explain a 409 from the retry endpoint by naming the task's real status.

    The endpoint answers 409 for every status but `failed`, and its detail
    names the RULE ("only failed tasks can be retried") rather than the FACT.
    Left to `_check`, the screen read `Error 409: {"detail": "Task is not
    failed - only failed tasks can be retried"}`: a raw JSON body for the most
    likely wrong-verb mistake on this surface, after which the operator still
    has to run a second command to learn anything.

    The status is READ BACK rather than inferred, because the 409 body cannot
    supply it and this client must not decide what the server's vocabulary is.
    """
    status_value = _read_back_status(client, task_id)
    if status_value is not None:
        console.print(
            "[yellow]Not retried:[/yellow]",
            Text(
                f"task {task_id} is {status_value}, and only a failed task can "
                "be retried."
            ),
        )
    else:
        console.print(
            "[yellow]Not retried:[/yellow] only a failed task can be retried, "
            "and this one is not. Its current status could not be read back, "
            "so it is not named here rather than guessed."
        )
    _copyable(f"praxis task {task_id}   # its real status, and the review feedback")
    raise typer.Exit(1)


@app.command()
def retry(
    task_id: str = typer.Argument(..., help="Full task ID from `praxis task`"),
) -> None:
    """Requeue a failed task: back to pending, one more attempt.

    The recovery verb for a wedged plan. A PENDING leaf whose dependency FAILED
    terminally can never be dispatched -- `failed` is not in
    `SATISFIED_STATUSES`, so no tick will ever return it -- and the plan sits
    ACTIVE, indistinguishable from healthy, forever. MCP `poll_plan` reports
    that as `stalled.action_required = "retry_failed_task"` and this is the
    verb that performs it. `POST /api/tasks/{id}/retry` had existed the whole
    time with no verb in front of it, so the hint named an action an operator
    could only take with curl.

    It also restarts a plan the ENGINE stopped. When every leaf is terminal and
    one of them failed, `process_plan_once` writes the PLAN `failed`, and
    `get_runnable_plans` selects only pending and active plans - so a retry
    there used to requeue a leaf that no tick would ever look at again, print
    "watch it pick up again", and mean nothing. Requeuing now takes the plan
    back to `active` with the task, and the line below reports the plan's real
    status rather than promising one.

    This adds no precondition of its own. The only rule is the server's (a task
    must be `failed`), and the retry CAP is deliberately enforced on the review
    path alone: a second copy of it here would be a rule that can drift from
    the one that actually governs dispatch. A human retrying past the automatic
    bound is making a decision, not tripping a bug.
    """

    with _client() as client:
        response = client.post(f"/api/tasks/{task_id}/retry")
        if response.status_code == httpx.codes.CONFLICT:
            _refused_retry(client, task_id)
        data = _check_dict(response)
        # READ BACK, never assumed: the retry answers a TASK row, and whether
        # anything will dispatch the leaf is a fact about its PLAN. Same
        # discipline as `_refused_retry`, and for the sharper reason here that
        # this is the exact fact the command used to get wrong.
        plan_state = _plan_state_after_retry(client, data.get("plan_id"))
    console.print(
        "[green]Requeued:[/green]",
        Text(
            f"{data.get('title') or task_id} "
            f"(now {data.get('status')}, attempt {data.get('attempt')})"
        ),
    )
    _report_plan_state_after_retry(plan_state)
    _copyable(f"praxis task {task_id}   # watch it leave pending and pick up again")
    plan_id = data.get("plan_id")
    if plan_id:
        # Only when the row names one. `praxis tasks` with an empty argument is
        # a usage error, and a copyable line that cannot be run reads as a
        # broken CLI rather than as a missing field.
        _copyable(
            f"praxis tasks {plan_id}   # the leaves this unblocks, once it passes"
        )


@app.command()
def status() -> None:
    """Show orchestrator status."""

    with _client() as client:
        data = _check_dict(client.get("/api/status"))
    opus_state = data["opus_state"]
    agent_model = data.get("agent_model") or {}
    # "Opus" is the internal legacy name of the brain (the `opus_state` table,
    # `OpusBridge`), not the model. Printing it told an operator their planner
    # was Opus on an install whose shipped role chain runs Sonnet, and the
    # doctor two commands away named the real one. Name what resolved.
    planner = str(agent_model.get("name") or "").strip()
    label = f"Planner ({planner})" if planner else "Planner"
    console.print(f"{label}: [bold]{opus_state['status']}[/bold]")
    if opus_state["resume_at"]:
        console.print(f"  Resume at: {opus_state['resume_at']}")
    console.print(f"  Queued actions: {opus_state['queued_count']}")
    # `connected_measured` is False for a `local` planner, an unrecognized
    # provider, or one `/api/status` failed to resolve at all: that endpoint
    # never probes those, since only a CLI-shaped provider has a binary to
    # check. Printing "connected"/"disconnected" in that case would assert a
    # measurement nobody took; `praxis doctor` is where it IS measured (a live
    # round trip for a CLI provider still not covered by this poll).
    if agent_model.get("connected_measured"):
        cli_state = "connected" if agent_model.get("connected") else "disconnected"
        console.print(f"  CLI: {cli_state}")
    else:
        detail = str(agent_model.get("detail") or "not probed by this endpoint")
        console.print(f"  CLI: [dim]not measured here[/dim] - {detail}")
    # `agents_reachable` is False when the agent manager is absent (no Docker
    # on this host) or the container listing raised (daemon down): both used
    # to leave `active_agents`/`total_agents` at 0/0, indistinguishable from a
    # genuinely idle system with zero agents running.
    if data.get("agents_reachable"):
        console.print(
            f"Active agents: {data['active_agents']} / {data['total_agents']} total"
        )
    else:
        console.print(
            "[yellow]Active agents: unknown[/yellow] (could not reach the "
            "agent manager; Docker may be unavailable)"
        )
    _print_open_runs(data)
    _print_brain_stages(data)


def _print_brain_stages(data: dict[str, Any]) -> None:
    """Print the brain's in-flight stages (decompositions, reviews) and the cap.

    Absent on an older server, so a missing key prints nothing rather than a
    measurement nobody took. Ids are server text: escaped, never bare.
    """
    stages = data.get("brain_stages")
    if not isinstance(stages, dict):
        return
    in_flight = stages.get("in_flight") or []
    cap = stages.get("cap")
    console.print()
    console.print(f"Brain stages: {len(in_flight)} in flight (cap {cap})")
    for stage in in_flight:
        what = f"{stage.get('stage')} plan {stage.get('plan_id')}"
        if stage.get("task_id"):
            what += f" task {stage.get('task_id')}"
        clock = format_duration(stage.get("running_for_seconds"))
        verb = (
            f"running for {clock}"
            if stage.get("state") == "running"
            else f"waiting for a slot, {clock}"
        )
        console.print(f"  {escape(what)}: {verb}", soft_wrap=True)


def _print_open_runs(data: dict[str, Any]) -> None:
    """Print the install-wide "what is running" table below the agent count.

    This is the LEDGER's view (open = ``finished_at IS NULL``), distinct from
    the "Active agents" line above (Docker's view); the two are allowed to
    disagree, e.g. a container Docker has lost is still an open run here until
    reconcile closes it. Server-provided text (task title, project name) goes
    into a `Text` cell, never a plain string: rich reads a bare `[` as a
    markup tag, and a decomposer-authored title like `refactor [core] parser`
    is an ordinary value here. Ids never go in a table column; each run gets
    its own copyable `praxis task <id>` line below the table, soft-wrapped so
    it survives selection whole.
    """
    running = data.get("running") or []
    running_known = data.get("running_known", True)
    console.print()
    if not running_known:
        console.print(
            "[yellow]Running work: unknown[/yellow] (could not read the run ledger)"
        )
        return
    if not running:
        console.print("[dim]No worker is currently running.[/dim]")
        return
    table = Table(title="Running")
    table.add_column("Task")
    table.add_column("Project")
    table.add_column("Attempt")
    table.add_column("Running for")
    for run in running:
        table.add_row(
            Text(str(run.get("task_title") or "")),
            Text(str(run.get("project_name") or "")),
            str(run.get("task_attempt") or ""),
            format_duration(run.get("running_for_seconds")),
        )
    console.print(table)
    console.print()
    for run in running:
        _copyable(f"praxis task {run['task_id']}")


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


def _scope_glance(review_scope: str | None) -> str:
    """A short glance at what a review covered, or "" when there is none.

    Parsed straight off the review's own stored sentence (never re-derived
    from other state), so the glance can never drift from the full statement
    printed beside it. The two axes that actually matter to a human approving
    a merge: did the review read a real checkout or only diff text, and did
    the verify gate run and pass. "checkout, verify passed" must read as
    plainly different from "diff only, no gate" -- that distinction is the
    whole point of carrying this to the merge gate at all.
    """
    if not review_scope:
        return ""
    checkout = "checkout" if "read a clean checkout" in review_scope else "diff only"
    # THREE outcomes since 2026-08-26, and the third is the one that matters
    # most here. A gate that failed and was CHARGED to the task still never
    # reaches this surface: it fails the task where it runs, so it never
    # reaches a review verdict of pass and never parks at the merge gate.
    #
    # But a gate that failed and was NOT charged to the task does park here,
    # and it is the review's own way of saying "this repository's verify
    # command is red, and it was red before this task". Rendering that as
    # "no gate" told the one person who could act on it that nothing ran.
    #
    # Both phrases are imported, never spelled out: this function parses the
    # producer's sentence on purpose, so the phrase is a cross-package
    # contract, and a copy here is how the two silently drift apart.
    if SCOPE_VERIFY_PASSED in review_scope:
        verify = "verify passed"
    elif SCOPE_VERIFY_UNATTRIBUTED in review_scope:
        verify = "verify RED (not this task)"
    else:
        verify = "no gate"
    return f"{checkout}, {verify}"


def _task_status_cell(status: object, running_for_seconds: object) -> str:
    """A status, with how long its live run has been going when there is one.

    Args:
        status: The task's status string.
        running_for_seconds: The server's measurement, or ``None``.

    Returns:
        ``"in_progress (2h 14m)"`` while a run is open, the bare status
        otherwise. Nothing is appended for ``None`` so a finished task's cell
        is unchanged: the suffix exists to make a long run stand out, and a
        parenthesis on every row is how it would stop doing that.
    """
    text = str(status)
    if not isinstance(running_for_seconds, int | float) or isinstance(
        running_for_seconds, bool
    ):
        return text
    return f"{text} ({format_duration(float(running_for_seconds))})"


def _running_line(running_for_seconds: object) -> str:
    """ "Running for ..." when a run is open, "" when none is.

    Prints NOTHING rather than "unknown" when the server reports ``None``,
    which is the normal case for every task that is not currently executing.
    An "unknown" on each of those would be noise on the line that exists to
    make ONE state visible: a run that has been going too long.

    The number is the server's own measurement, not a span computed here from
    a timestamp: the client that did its own date math on these stamps read
    naive UTC as local time and rendered a 20-minute-old row as "7h ago".
    """
    if not isinstance(running_for_seconds, int | float) or isinstance(
        running_for_seconds, bool
    ):
        return ""
    return f"Running for: {format_duration(float(running_for_seconds))}"


def _drift_line(drift: object) -> str:
    """The stored contract-drift summary, or "" when there is nothing to print.

    Deliberately prints NOTHING for two different-looking cases: a task whose
    check never ran (``None`` -- a row older than the column, or a review that
    failed before a diff existed) and one that ran and found the diff inside
    the plan's authorised paths. A clean result is the normal case at this
    gate, and a line saying so on every parked task is the fastest way to
    train a reader to skip the block that also carries the warnings.

    The ungradable REASON is printed, because that one is not the normal case
    and reads as a silence otherwise.

    The summary is the producer's own sentence (``contract_drift.summary_line``)
    rather than a second wording assembled here, for the same reason
    ``_scope_glance`` parses the review's sentence instead of re-deriving it.
    """
    if not isinstance(drift, dict):
        return ""
    if drift.get("gradable") and not (
        drift.get("named_not_authorised")
        or drift.get("unmentioned")
        or drift.get("created_described_as_existing")
    ):
        return ""
    summary = drift.get("summary")
    return summary if isinstance(summary, str) else ""


def _drift_glance(drift: object) -> str:
    """One table cell: the strong tier, the weak tier, or nothing.

    Kept to a few characters because it shares a row with the age, title,
    branch and scope on an 80-column console. The strong tier names its count
    rather than its paths -- the paths are on the copyable line below the
    table, where there is room for them.
    """
    if not isinstance(drift, dict):
        return ""
    named = drift.get("named_not_authorised") or []
    if named:
        return f"{len(named)} unauthorised"
    phantom = drift.get("created_described_as_existing") or []
    if phantom:
        return f"{len(phantom)} phantom"
    if drift.get("unmentioned"):
        return "new paths"
    return ""


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
    # Blocked questions are a THIRD gate and were reported by nothing. The
    # worker asked something, the brain declined to answer it or answered
    # below the project's confidence threshold, and the task has been sitting
    # at NEEDS_CLARIFICATION ever since. `GATED_STATUSES` covers the merge
    # gate only, so `count` excludes them exactly as it excludes proposals,
    # and each new kind of parked work has to be tested for separately here or
    # this verb goes quiet in precisely the state it exists to report.
    clarifications = data.get("clarifications") or []
    if not data["count"] and not proposals and not clarifications:
        console.print("[green]Nothing awaiting approval.[/green]")
        return

    if tasks:
        table = Table(title=f"{len(tasks)} task(s) awaiting approval")
        table.add_column("Age")
        table.add_column("Task", max_width=40)
        table.add_column("Branch", overflow="fold")
        # Short glance only: what the review actually covered (checkout vs
        # diff text, verify passed/failed/not run), so a human deciding
        # whether to click approve does not have to open `praxis task` first
        # to learn whether the green in front of them means anything.
        table.add_column("Scope")
        # Only when at least one parked task has something to say. An always-on
        # column of blanks costs width on every row of an 80-column table for
        # the common case where every diff stayed where the plan put it.
        show_drift = any(_drift_glance(t.get("contract_drift")) for t in tasks)
        if show_drift:
            table.add_column("Plan paths")
        for task in tasks:
            # `Text` per the note on `_check`. `_scope_glance` returns one of
            # this module's own fixed phrases, so it stays a plain string.
            # Annotated: a bare literal of mixed `str` and `Text` infers as
            # `list[object]`, which `add_row` rejects. Both member types are
            # deliberate - see the note above on which cells take `Text`.
            row: list[str | Text] = [
                f"{int(task['age_hours'])}h",
                Text(task["title"] or task["task_id"]),
                Text(task["branch"] or ""),
                _scope_glance(task.get("review_scope")),
            ]
            if show_drift:
                row.append(_drift_glance(task.get("contract_drift")))
            table.add_row(*row)
        console.print(table)

    if plans_awaiting:
        plan_table = Table(title=f"{len(plans_awaiting)} plan(s) awaiting integration")
        plan_table.add_column("Age")
        plan_table.add_column("Plan branch", overflow="fold")
        for plan in plans_awaiting:
            plan_table.add_row(
                f"{int(plan['age_hours'])}h",
                Text(plan["branch"] or plan["plan_id"]),
            )
        console.print(plan_table)

    if clarifications:
        question_table = Table(
            title=f"{len(clarifications)} task(s) blocked on a question"
        )
        question_table.add_column("Age")
        question_table.add_column("Task", max_width=30)
        question_table.add_column("Question", overflow="fold")
        for blocked in clarifications:
            # A worker's question is raw model output, the most bracket-prone
            # string on any of these surfaces: one containing `[/dim]` raised
            # `MarkupError` and took the whole of `praxis pending` down, which
            # hid the merge gate and the proposals too.
            question_table.add_row(
                f"{int(blocked['age_hours'])}h",
                Text(blocked["title"] or blocked["task_id"]),
                Text(blocked["question"] or "(no question recorded)"),
            )
        console.print(question_table)

    if proposals:
        proposal_table = Table(
            title=f"{len(proposals)} improvement proposal(s) awaiting approval"
        )
        proposal_table.add_column("Age")
        proposal_table.add_column("Project", overflow="fold")
        for proposal in proposals:
            proposal_table.add_row(
                f"{int(proposal['age_hours'])}h",
                Text(proposal["project_id"] or ""),
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
        _copyable(
            f"praxis merge {task['task_id']}   "
            f"# {escape(task['title'] or task['task_id'])}"
        )
        # The gate is two-way at the API (`approve-merge` and `reject-merge`)
        # and was one-way here, so the only thing `pending` let you say about
        # parked work was yes. On its own line, not appended to the merge
        # line: a verb and a 36-character uuid on one line already fills half
        # an 80-column console, and rich breaks on whitespace, so a second
        # command on the same line puts `praxis reject-merge` on one row and
        # its id on the next. That is not a copyable line, it is two halves.
        _copyable(f"praxis reject-merge {task['task_id']}   # send it back")
        if task["pr_url"]:
            _copyable(f"  PR: {task['pr_url']}")
        # The review's own full account, not just the table's short glance.
        # It is prose, not a command, so it is printed rather than made
        # copyable, and it is skipped entirely for a row with none (a
        # pre-feature task, or a PASS that recorded no scope statement) rather
        # than printing a fabricated "None".
        if task.get("review_scope"):
            console.print(Text(f"  {task['review_scope']}"))
        # What the diff did to the paths the PLAN authorised. Printed after the
        # scope statement and only when there is something to say, because this
        # is the fact the review's own verdict structurally cannot carry: the
        # reviewer grades against the leaf's plan_text, so a leaf told to
        # rewrite the plan's acceptance bar passes, correctly, in silence.
        drift_line = _drift_line(task.get("contract_drift"))
        if drift_line:
            console.print(Text(f"  {drift_line}"))
    for plan in plans_awaiting:
        # The plan line names the same verb the per-task line does, because
        # `merge-plan` is one command that covers both stages: it drains
        # whatever is still parked, then merges the integration PR. A plan
        # reaching this list means stage one is already done.
        _copyable(
            f"praxis merge-plan {plan['plan_id']}   # integrate onto the base branch"
        )
        if plan["pr_url"]:
            _copyable(f"  PR: {plan['pr_url']}")
    for proposal in proposals:
        # Both verbs, because unlike the merge gate this one is genuinely
        # two-way and rejecting is the common answer: `approve` dispatches the
        # proposal's tasks, `reject` closes it for good.
        #
        # One per LINE. Both on one line measured 110 characters, and rich
        # word-wraps at the console width breaking only on whitespace, so at
        # 80 columns the uuid survived intact while `praxis reject` and its
        # argument landed on different rows. Selecting either row gave you
        # half a command. The uuid never folds, which is what made this look
        # fine in a wide terminal and in a test that pinned one.
        _copyable(f"praxis approve {proposal['plan_id']}   # dispatch it")
        _copyable(f"praxis reject {proposal['plan_id']}   # close it")
    for blocked in clarifications:
        _copyable(f'praxis clarify {blocked["task_id"]} "your answer here"')


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


@app.command()
def clarify(
    task_id: str = typer.Argument(..., help="Full task ID from `praxis pending`"),
    answer: str = typer.Argument(..., help="Your answer to the worker's question"),
) -> None:
    """Answer a task's blocking question and put it back in the queue.

    `POST /api/tasks/{id}/clarify` has existed the whole time with no verb in
    front of it, and no surface listed the tasks waiting on it either. A
    worker asks a question, the brain declines to answer it or answers below
    the project's confidence threshold, and the task parks at
    NEEDS_CLARIFICATION. `praxis pending` reported only the merge gate, so the
    product stopped and waited for a person without telling any person: the
    only way to find out was to poll MCP or read the task row.

    The answer is re-queued for the worker, which resumes its own session
    where the harness supports one, so answer the question rather than
    restating the task.
    """

    with _client() as client:
        data = _check_dict(
            client.post(f"/api/tasks/{task_id}/clarify", json={"answer": answer})
        )
    # The endpoint answers `{"status": "requeued"}` and names no id, so the id
    # printed here is the one the operator passed in. Reading it back off the
    # response would print an empty string and read as a task that vanished.
    console.print(f"[green]Answered:[/green] {task_id} ({data.get('status')})")
    _copyable(f"praxis task {task_id}   # watch it pick up again")


@app.command("reject-merge")
def reject_merge(
    task_id: str = typer.Argument(..., help="Full task ID from `praxis pending`"),
    feedback: str | None = typer.Option(
        None,
        "--feedback",
        help=(
            "Why you are rejecting it. Posted as a comment on the task's PR "
            "and stored as the failure reason, so the retry has something to "
            "work from. Omit and the task records a bare rejection."
        ),
    ),
) -> None:
    """Reject one parked task: comment on its PR and send it back.

    The other half of the merge gate. `POST /api/tasks/{id}/reject-merge` has
    existed the whole time with no verb in front of it, so `praxis pending`
    printed a `merge` line for parked work and offered no way to say no:
    `praxis reject` takes a PLAN id (an autonomous improvement proposal) and
    404s on a task id, which reads as a broken command rather than the wrong
    one. A gate with one door is not a gate.

    The task is failed and re-dispatched if retry attempts remain, otherwise
    it stays failed. That is the server's decision, and the printed line says
    which happened rather than assuming.
    """

    body = {"feedback": feedback} if feedback else {}
    with _client() as client:
        data = _check_dict(client.post(f"/api/tasks/{task_id}/reject-merge", json=body))
    console.print(f"[red]Rejected:[/red] {data['task_id']} ({data['status']})")
    console.print(
        f"Run 'praxis task {task_id}' to see whether it was re-dispatched or "
        "has exhausted its attempts."
    )


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
            _copyable(f"  PR: {integration['pr_url']}")
    elif integration_status == "already_merged":
        console.print("[green]Already integrated:[/green] nothing left to merge")
        if integration.get("pr_url"):
            _copyable(f"  PR: {integration['pr_url']}")
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
    elif integration_status == "none" and not errors:
        # The branch that was missing, and the likeliest one: tasks merged
        # onto the PLAN branch and there is no integration PR, because the
        # best-effort `gh pr create` did not produce one. Without this the
        # whole output was `Merged: N task(s)` and exit 0, which reads as
        # landed on the base branch. It is not: it is on the plan branch with
        # nothing pointing at it. The reason the endpoint returned was
        # discarded on the floor.
        console.print(
            f"[yellow]Not integrated:[/yellow] {approved} task(s) merged onto "
            "the plan branch, but the plan has no integration PR "
            f"({integration.get('reason') or 'reason not reported'}). The work "
            "is NOT on the base branch."
        )
        console.print("Run 'praxis plans <project-id>' to see the plan's state.")
    elif not errors:
        # A status this CLI does not know about. Saying nothing would put an
        # unrecognised value back in the silent category this chain exists to
        # empty, so name it and stay out of the way.
        console.print(
            f"[yellow]Integration status[/yellow] {integration_status!r} for "
            f"plan {plan_id}: {integration.get('reason') or 'no reason given'}"
        )

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
    file directly when the orchestrator is unreachable or no token has been
    set up yet, which is exactly the moment a newcomer running `praxis init`
    for the first time needs this list the most.

    Those two sources read the same records, so that fallback is a different
    path to identical data. There is a THIRD source, and it is not: when the
    settings file is absent, `_fetch_presets_or_defaults` returns a single
    hardcoded preset that carries neither the shipped model string nor the
    `default` flag. The docstring here used to say the fallback "is never a
    degraded view", which was false in precisely the case it named, a
    newcomer who has not run init yet, so the source line below distinguishes
    all three.
    """
    data = _live_presets()
    source = "the running orchestrator"
    if data is None:
        data = _fetch_presets_or_defaults()
        # The resolved path, not a literal: PRAXIS_CONFIG_PATH can move this
        # file, and naming the wrong one sends the reader to edit a file the
        # process never reads.
        settings_path = config_file_path()
        if Path(settings_path).is_file():
            source = f"{settings_path} (orchestrator unreachable or not set up yet)"
        else:
            source = (
                f"a built-in fallback: no settings file at "
                f"{Path(settings_path).resolve()}, and the orchestrator is "
                "unreachable. Run this from your Praxis install directory to "
                "see the presets this deployment actually ships."
            )

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
            # A preset with unmet requirements needs the flag, or this exact
            # line exits 1. `praxis init` refuses a preset it cannot satisfy
            # non-interactively, and two of the three shipped presets need a
            # credential, so printing the bare command for all of them handed
            # the reader a copy-pasteable command guaranteed to fail. The
            # requirement is not a reason to hide the preset: it is a reason
            # to print the command that actually runs it.
            if preset.get("requires"):
                _copyable(
                    f"praxis init --non-interactive --preset {name} "
                    "--accept-preset-requirements"
                )
            else:
                _copyable(f"praxis init --non-interactive --preset {name}")
        if any(preset.get("requires") for preset in data):
            console.print(
                "\n[dim]--accept-preset-requirements says you have already "
                "supplied the credential in the Needs column. It does not "
                "supply it.[/dim]"
            )


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
    elif values.get("AUTH_TOKEN"):
        auth_from = f"AUTH_TOKEN in {path}"
    elif values.get("ORCHESTRATOR_TOKEN"):
        # Named separately, because the whole point of this verb is saying
        # which source won. The single branch this replaces reported
        # "AUTH_TOKEN in <path>" for a file containing only
        # ORCHESTRATOR_TOKEN, i.e. it named a key that is not in the file, on
        # the one surface whose job is to stop you debugging the wrong one.
        auth_from = f"ORCHESTRATOR_TOKEN in {path}"

    console.print(f"URL:   {_api_url()}")
    console.print(f"       from {url_from}")
    console.print(f"Token: {auth_from}")
    if path is None:
        console.print(
            "\n[yellow]No .env found[/yellow] walking up from the current "
            "directory. Run this from your Praxis install directory."
        )
    if auth_from == "not found":
        # `_auth_token()`'s own error names the two variables and points at
        # `praxis init`, but this path never calls it, so the verb that error
        # tells you to run was the one place that dead-ended with no remedy.
        console.print(
            "\n[yellow]No token resolved.[/yellow] Set AUTH_TOKEN or "
            "ORCHESTRATOR_TOKEN in this shell, or run [cyan]praxis init[/cyan] "
            "from your Praxis install directory to write one to .env."
        )
        raise typer.Exit(1)

    # The token is never printed above, only its source. It IS printed here,
    # because this block exists to be pasted into another shell and half an
    # export block is not usable. Anyone who can run this can already read
    # the same value out of `.env`.
    # Through `_copyable`, whose docstring is written about exactly this
    # situation and which these three lines were the only paste-block in the
    # CLI not using. They had `highlight=False` but not `soft_wrap=True`, so
    # rich inserted a REAL newline at the console width and selecting the row
    # gave half a command -- in the one block whose stated job is to be pasted
    # into another shell. Measured on a hosted install: the PowerShell line,
    # the longest of the three, folded.
    #
    # `escape` because `_copyable` prints with markup on and the token is an
    # arbitrary operator-chosen string.
    url = escape(_api_url())
    token = escape(_auth_token())
    console.print("\nTo make this explicit in another shell:\n")
    _copyable(f"  export ORCHESTRATOR_URL={url}")
    _copyable(f"  export ORCHESTRATOR_TOKEN={token}")
    _copyable(
        f'  (PowerShell: $env:ORCHESTRATOR_URL="{url}"; '
        f'$env:ORCHESTRATOR_TOKEN="{token}")'
    )


@app.command("mcp")
def mcp() -> None:
    """Re-print the MCP client configuration block for this install.

    ``praxis init`` prints this block ONCE, in the middle of an output that is
    mostly Docker build progress and that takes minutes to produce. Until this
    verb existed there was no second way to see it: ``mcp_snippet`` had exactly
    one caller, inside ``init``, so an operator whose scrollback had rolled had
    to re-run the whole install to recover it.

    That is not hypothetical. It was measured on 2026-08-28, when an assistant
    following the README's own agent setup brief lost the block to output
    truncation and reported that its only route was to run ``init`` again. The
    brief's step 5 depends on this block, so that step was effectively
    un-completable from a long transcript.

    The snippet itself comes from ``cli.init.mcp_snippet``, never a second copy
    here: the env var names in it are the ones ``mcp_server.client`` actually
    reads, and a block with plausible-but-wrong names is worse than no block,
    because it is pasted and then silently falls back to a default URL with no
    token.

    Works with the orchestrator DOWN, like ``praxis presets``: this reads
    ``.env``, not the API, and the commonest moment to want it is while setting
    the client up.
    """
    from cli.init import mcp_snippet

    path, _values = _env_file_values()
    if path is None:
        console.print(
            "[yellow]No .env found[/yellow] walking up from the current "
            "directory. Run this from your Praxis install directory, or run "
            "[cyan]praxis init[/cyan] there first."
        )
        raise typer.Exit(1)

    # The install root is the directory holding `.env`, which is what the
    # snippet's `--directory` has to point at. Deliberately NOT `Path.cwd()`
    # (`mcp_snippet`'s own default, correct for `init` because `init` has
    # already proven the cwd is the root): this verb is meant to be runnable
    # from a subdirectory, and a `--directory` pointing at one would give the
    # client a `uv run` that cannot find the project.
    root = path.parent
    console.print(f"MCP configuration for the install at {root}:\n")
    # soft_wrap=True, markup=False: same reasoning as `init`'s copy, and the
    # same defect. The longest line here is the absolute install path INSIDE a
    # JSON string, so a fold does not merely look untidy, it emits invalid JSON
    # carrying a broken path. `markup=False` because the JSON's own brackets
    # are data, not rich tags.
    console.print(
        mcp_snippet(_api_url(), _auth_token(), root), markup=False, soft_wrap=True
    )
    console.print(
        "\n[dim]The token is a credential: put this in a gitignored file "
        "(for Claude Code, `.mcp.json` at the target project's root) and "
        "never commit it.[/dim]"
    )


@app.command()
def onboard() -> None:
    """First-run helper: report what is configured and name the next verb.

    This used to print "No models configured yet" unconditionally, without
    making a single call, on an install that ships four registered models and
    three role chains. It also sent the operator to `praxis config`, which is
    a typer GROUP: running it prints help and registers nothing. Both halves
    were false on every install, which is the worst place for it, since this
    is the verb a newcomer runs before they know enough to doubt it.
    """
    console.print("[bold]Welcome to Praxis.[/bold]")

    registry: list[dict[str, Any]] | None = None
    roles: dict[str, Any] | None = None
    if _token_available():
        try:
            with _client() as client:
                registry_response = client.get("/api/settings/registry")
                roles_response = client.get("/api/settings/roles")
            # Shapes taken from `config show`, which is the only other reader:
            # the registry endpoint answers with a LIST of model records and
            # the roles endpoint with a MAPPING of role to chain. Guarding on
            # the type rather than assuming keeps a changed shape from being
            # rendered as "nothing configured".
            if registry_response.status_code < 400:
                body = registry_response.json()
                registry = body if isinstance(body, list) else None
            if roles_response.status_code < 400:
                body = roles_response.json()
                roles = body if isinstance(body, dict) else None
        except httpx.RequestError:
            registry = None
            roles = None

    if registry is None:
        # Silence would read as "nothing is configured", which is the very
        # claim this rewrite exists to stop making. Say that the question was
        # not answered, and how to answer it.
        console.print(
            "Could not read this install's model configuration "
            "(the orchestrator is unreachable or no token is set up yet).\n"
            "Run [cyan]praxis env[/cyan] to see where the CLI is pointing, "
            "then [cyan]praxis init[/cyan] if it is not running yet."
        )
    elif registry:
        chains = ", ".join(
            f"{role}: {' -> '.join(chain)}"
            for role, chain in sorted((roles or {}).items())
            if chain
        )
        console.print(
            f"{len(registry)} model(s) registered"
            + (f"; role chains {chains}" if chains else "; no role chains set")
        )
        console.print("Run [cyan]praxis config show[/cyan] to see them in full.")
    else:
        console.print("No models are registered yet.")

    console.print(
        "To change them: [cyan]praxis config add-model[/cyan] registers a "
        "model and [cyan]praxis config set-role[/cyan] sets a role's fallback "
        "chain. [cyan]praxis config[/cyan] on its own is a command group and "
        "only prints help."
    )


if __name__ == "__main__":
    app()
