"""`praxis init`: clone to a running, verified orchestrator in one command.

Idempotent and re-runnable.  It never overwrites an existing ``.env``
wholesale: unrelated keys, comments, blank lines and line order all survive,
and only the keys it manages are replaced, in place.  That merge is the whole
risk surface of this command, because every way it can go wrong is silent:
nothing raises, init still reports success, and the operator finds out weeks
later that their timezone reverted or their callback URL vanished.

It ends by running the doctor, because "it started" and "it works" are
different claims and only the second one is useful.
"""

from __future__ import annotations

import json
import re
import secrets
import subprocess  # nosec B404 - docker compose is the interface
import time
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import typer
from rich.console import Console
from rich.prompt import Confirm, IntPrompt, Prompt


console = Console()

#: Keys ``init`` writes.  Everything else in an existing ``.env`` is off
#: limits, and :func:`merge_env` enforces that rather than trusting callers.
MANAGED_KEYS: tuple[str, ...] = (
    "AUTH_TOKEN",
    "GITHUB_TOKEN",
    "PORT",
    "LM_STUDIO_URL",
    "DEFAULT_WORKER_HARNESS",
    "DEFAULT_WORKER_MODEL",
)

# Backslash is in here so a Windows path is quoted and escaped rather than
# written raw; the quoted form is the only one `_render_value` escapes.
_NEEDS_QUOTING = re.compile(r"[\s#\"'\\]")

# Every pattern below mirrors `python-dotenv`'s own parser (dotenv/parser.py)
# rather than approximating it.  python-dotenv is what pydantic-settings reads
# `.env` with, and `docker compose` agrees with it on all of these shapes, so
# a divergence here is a value the running product reads differently than
# `init` does -- which is unobservable, because both sides are consistent with
# themselves.
_EXPORT_PREFIX = re.compile(r"^export[^\S\r\n]+")
_KEY = re.compile(r"[^=\#\s]+")
_SINGLE_QUOTED = re.compile(r"^'((?:\\'|[^'])*)'")
_DOUBLE_QUOTED = re.compile(r'^"((?:\\"|[^"])*)"')
_INLINE_COMMENT = re.compile(r"\s+#")
_TRAILING_COMMENT = re.compile(r"^[^\S\r\n]*(?:#.*)?$")
_SINGLE_ESCAPE = re.compile(r"\\([\\'])")
_DOUBLE_ESCAPE = re.compile(r"\\([\\'\"abfnrtv])")
_ESCAPED: dict[str, str] = {
    "\\": "\\",
    "'": "'",
    '"': '"',
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
}

_DEFAULT_PORT = 12323

#: Files that must all be present for a directory to be the Praxis repo root.
#: Three of them, not one, because ``init`` is CWD-relative everywhere (``.env``,
#: ``docker compose``, the settings YAML) and any single generic marker
#: would accept a sibling checkout, which is the near-miss directory an
#: operator actually runs this from.
_ROOT_MARKERS: tuple[str, ...] = (
    "pyproject.toml",
    ".env.example",
    "docker-compose.yml",
)

#: The ``[project] name`` those markers have to belong to.  The three files
#: above are shapes any Python service can have; the name is what makes them
#: THIS repository.  Already PEP 503 normal form, so comparing against it
#: after normalizing a candidate name is a same-shape comparison.
_PROJECT_NAME = "praxis"

#: The console script a fork must keep to be accepted under a different
#: ``[project] name``.  See :func:`_has_praxis_console_script`.
_CONSOLE_SCRIPT = "praxis"

_NAME_SEPARATORS = re.compile(r"[-_.]+")

_FALLBACK_PRESET: dict[str, Any] = {
    "name": "local-lmstudio",
    "label": "Local GPU via LM Studio",
    "harness": "opencode",
    "model": "",
    "endpoint": "http://host.docker.internal:1234",
    "requires": [],
}


def generate_token() -> str:
    """Return a fresh URL-safe auth token."""
    return secrets.token_urlsafe(32)


def _read_pyproject(path: Path) -> dict[str, Any]:
    """Parse a ``pyproject.toml``, or return {} for anything unreadable.

    Shared by :func:`_pyproject_name` and :func:`_has_praxis_console_script`
    so a candidate directory is read and parsed once rather than twice.

    Args:
        path: Path to the file to read.

    Returns:
        The parsed TOML document, or {} when the file cannot be read or
        parsed.
    """
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {}


def _project_table(path: Path) -> dict[str, Any]:
    """Return a ``pyproject.toml``'s ``[project]`` table, or {} if it is not one.

    ``[project]`` parsing to anything other than a table (a bare string, an
    array) is exactly as "not a project table" as the file being unreadable:
    calling ``.get`` on it would raise, and a guard that raises on malformed
    input instead of refusing is not a guard.
    """
    project = _read_pyproject(path).get("project")
    return project if isinstance(project, dict) else {}


def _pyproject_name(path: Path) -> str:
    """Return a ``pyproject.toml``'s ``[project] name``, or "" if unreadable.

    Unreadable AND structurally odd both yield "", never the expected name,
    and never a raised exception: ``[project]`` not being a table, or `name`
    not being a string, are exactly as "not the project name" as a missing
    file, and a guard that fails open on any of them is not a guard.

    Args:
        path: Path to the file to read.

    Returns:
        The declared project name, or "" when the file cannot be read or
        parsed, ``[project]`` is not a table, or it declares no string name.
    """
    name = _project_table(path).get("name", "")
    return name if isinstance(name, str) else ""


def _has_praxis_console_script(path: Path) -> bool:
    """Whether ``path``'s ``[project.scripts]`` declares a ``praxis`` command.

    A downstream fork that renames ``[project] name`` is still a Praxis
    checkout if it kept this: the entry point (``cli.main:app``) is the same
    code regardless of what the project calls itself.  Fails closed like
    :func:`_pyproject_name`, on the same parsed TOML.

    Args:
        path: Path to the file to read.

    Returns:
        True when the file parses and its ``[project.scripts]`` table
        declares a ``praxis`` command.
    """
    scripts = _project_table(path).get("scripts")
    return isinstance(scripts, dict) and _CONSOLE_SCRIPT in scripts


def _normalize_project_name(name: str) -> str:
    """PEP 503 normalize a project name: lowercase, runs of ``-_.`` collapsed.

    ``Praxis`` and ``praxis`` name the same project under PEP 503; a bare
    ``!=`` string comparison refuses the second for nothing but casing.
    """
    return _NAME_SEPARATORS.sub("-", name).lower()


def repo_root_problem(directory: Path) -> str | None:
    """Say why ``directory`` is not the Praxis repo root, or None if it is.

    A reason rather than a bool, because the whole value of this check is the
    error message: "wrong directory" leaves the operator guessing which one is
    right.

    Args:
        directory: Candidate root, normally the process CWD.

    Returns:
        A short human-readable reason, or None when ``directory`` IS the root.
    """
    for marker in _ROOT_MARKERS:
        if not (directory / marker).is_file():
            return f"no {marker} here"
    pyproject = directory / "pyproject.toml"
    name = _pyproject_name(pyproject)
    if _normalize_project_name(name) == _PROJECT_NAME:
        return None
    if _has_praxis_console_script(pyproject):
        return None
    found = repr(name) if name else "a pyproject.toml that declares no name"
    return (
        f"pyproject.toml here is {found}, not {_PROJECT_NAME!r} (even "
        "normalized), and [project.scripts] does not declare a `praxis` "
        "command either"
    )


def _require_repo_root() -> Path:
    """Return the CWD, refusing to continue anywhere but the Praxis repo root.

    ``init`` resolves ``.env``, ``docker compose`` and the settings YAML
    relative to the CWD, so run from anywhere else it writes a ``.env``
    holding a live ``AUTH_TOKEN`` into an unrelated directory and configures
    nothing.  Refusing loudly is the fix rather than auto-locating the root:
    silently relocating an operator's write is its own surprise, and a secret
    is the wrong thing to be surprised by.

    Returns:
        The current working directory, once it is known to be the root.

    Raises:
        typer.Exit: With code 1, before anything has been written.
    """
    cwd = Path.cwd()
    problem = repo_root_problem(cwd)
    if problem is None:
        return cwd
    console.print(
        "[red]`praxis init` must run from the Praxis repository root.[/red]",
        highlight=False,
    )
    console.print(f"  Looked in: {cwd}", highlight=False)
    console.print(
        f"  Expected:  {', '.join(_ROOT_MARKERS)}, with pyproject.toml naming "
        f"the project {_PROJECT_NAME!r} (or declaring a `praxis` console "
        "script, for a renamed fork)",
        highlight=False,
    )
    console.print(f"  Found:     {problem}", highlight=False)
    console.print(
        "\nNothing was written. `praxis init` only runs from the Praxis "
        "repository root, or a fork whose pyproject.toml still declares a "
        "`praxis` console script; it writes a .env holding a live "
        "AUTH_TOKEN and will not leave one in an unrelated directory.",
        highlight=False,
    )
    raise typer.Exit(code=1)


def _render_value(value: str) -> str:
    """Render one ``.env`` value, quoting and escaping when it needs it.

    Quoting alone is not enough: wrapping ``a"b`` in double quotes yields
    ``KEY="a"b"``, which parses as ``a`` and drops the tail with no error
    anywhere in the stack.
    """
    if not _NEEDS_QUOTING.search(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


@dataclass(frozen=True)
class _EnvLine:
    """One ``KEY=VALUE`` line, split the way ``python-dotenv`` splits it.

    Attributes:
        prefix: The ``export `` prefix the line carried, or "".
        key: The bare key, without that prefix.
        value: The value with quotes removed and escapes resolved.
        comment: Any trailing comment, including the whitespace in front of
            it, or "".  Carried so a merge can put it back.
    """

    prefix: str
    key: str
    value: str
    comment: str


def _parse_line(line: str) -> _EnvLine | None:
    """Split one ``.env`` line, or return None when it holds no binding.

    None covers blank lines, whole-line comments, and lines ``python-dotenv``
    rejects outright (an unterminated quote, junk after a closing quote).
    Rejecting those rather than guessing is the point: a line the real parser
    drops must not be one ``init`` silently rewrites into something the real
    parser would then accept.

    Args:
        line: One physical line, without its terminator.

    Returns:
        The split line, or None when there is nothing on it to bind.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    export = _EXPORT_PREFIX.match(stripped)
    key_match = _KEY.match(stripped, export.end() if export else 0)
    if key_match is None:
        return None
    rest = stripped[key_match.end() :].lstrip(" \t")
    if not rest.startswith("="):
        return None
    raw = rest[1:].lstrip(" \t")

    if raw[:1] in ("'", '"'):
        single = raw[0] == "'"
        quoted = (_SINGLE_QUOTED if single else _DOUBLE_QUOTED).match(raw)
        if quoted is None or not _TRAILING_COMMENT.match(raw[quoted.end() :]):
            return None
        escapes = _SINGLE_ESCAPE if single else _DOUBLE_ESCAPE
        value = escapes.sub(lambda m: _ESCAPED[m.group(1)], quoted.group(1))
        comment = raw[quoted.end() :]
    else:
        hash_at = _INLINE_COMMENT.search(raw)
        cut = hash_at.start() if hash_at else len(raw)
        value, comment = raw[:cut].rstrip(), raw[cut:]

    if comment and not comment[:1].isspace():
        # A comment glued to a closing quote (`K="v"# c`) has to regain its
        # separator: re-rendered onto an unquoted value it would otherwise
        # become part of that value.
        comment = f" {comment}"
    return _EnvLine(
        prefix=export.group(0) if export else "",
        key=key_match.group(0),
        value=value,
        comment=comment,
    )


def parse_env(text: str) -> dict[str, str]:
    """Parse ``.env`` text the way its real consumers parse it.

    ``python-dotenv`` (what pydantic-settings reads ``.env`` with) and
    ``docker compose`` agree on all of this, so this agrees with them: an
    ``export `` prefix is not part of the key, an unquoted value ends at the
    first ``#`` preceded by whitespace, a ``#`` inside quotes is not a
    comment, and escape sequences resolve inside quotes only.  Reading a value
    differently than the running product reads it is how a reused
    ``AUTH_TOKEN`` acquires an inline comment: nothing surfaces it, because
    ``init``, the doctor and the container then all agree on the same
    corrupted string.

    Two divergences remain on purpose: a value spanning multiple physical
    lines is not reassembled, and ``$VAR`` interpolation is not performed.

    Args:
        text: Raw contents of a ``.env`` file.

    Returns:
        Mapping of key to unquoted, unescaped value.
    """
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        entry = _parse_line(line)
        if entry is not None:
            parsed[entry.key] = entry.value
    return parsed


def build_env_file(values: Mapping[str, str | None]) -> str:
    """Render a fresh ``.env`` from the managed values only.

    Empty values are omitted rather than written blank, which is what lets
    local mode (no GitHub credential at all) produce a file with no
    ``GITHUB_TOKEN`` line instead of one that looks configured but is not.
    There is no file to preserve here, so "the operator said nothing"
    (``None``) and "the preset says empty" (``""``) are the same instruction:
    write no line.  In :func:`merge_env` they are not.

    Args:
        values: Managed key to value.  Falsy values are skipped.

    Returns:
        The full text of a new ``.env`` file, newline-terminated.
    """
    lines = [
        "# Written by `praxis init`. Safe to edit; re-running init preserves",
        "# every key it does not manage, and every comment.",
    ]
    lines.extend(
        f"{key}={_render_value(value)}" for key, value in values.items() if value
    )
    return "\n".join(lines) + "\n"


def merge_env(existing: str, values: Mapping[str, str | None]) -> str:
    """Merge managed values into an existing ``.env`` text.

    Unrelated keys, comments and blank lines are preserved verbatim and keep
    their position; a managed key already present is replaced IN PLACE (never
    appended, or re-runs accumulate duplicates whose last entry silently
    wins), keeping the ``export `` prefix and the trailing comment that line
    carried; a managed key absent is appended.

    ``None`` and ``""`` are different answers, and folding them together is
    how a preset switch half-applies.  ``None`` means the OPERATOR declined to
    say, so an existing line survives untouched: the GitHub prompt promises
    that Enter keeps the current credential, and that has to be true.  ``""``
    means the PRESET authored an empty value, so the line is removed:
    ``gemini-agy`` has no endpoint, and leaving the previous preset's
    ``LM_STUDIO_URL`` sitting next to ``agy`` is a mixed configuration the
    operator believes they replaced, with no way to clear it.

    Args:
        existing: Current ``.env`` contents.  May be empty.
        values: Managed key to value, where ``None`` means "leave any
            existing line alone" and ``""`` means "remove it".  Every key must
            be in :data:`MANAGED_KEYS`.

    Returns:
        The merged text, newline-terminated.

    Raises:
        ValueError: If ``values`` names a key ``init`` does not manage.  The
            whole contract is that only those keys change, and a caller
            passing ``TZ`` here would violate it silently.
    """
    unmanaged = sorted(set(values) - set(MANAGED_KEYS))
    if unmanaged:
        message = f"refusing to rewrite unmanaged .env keys: {', '.join(unmanaged)}"
        raise ValueError(message)

    seen: set[str] = set()
    out: list[str] = []
    for line in existing.splitlines():
        entry = _parse_line(line)
        if entry is None or entry.key not in values:
            out.append(line)
            continue
        seen.add(entry.key)
        replacement = values[entry.key]
        if replacement is None:
            out.append(line)
        elif replacement:
            rendered = _render_value(replacement)
            out.append(f"{entry.prefix}{entry.key}={rendered}{entry.comment}")
        # An authored "" drops the line: `build_env_file` writes no line for
        # it either, and a blank `KEY=` is the "configured but useless" shape
        # both of them exist to avoid.
    out.extend(
        f"{key}={_render_value(value)}"
        for key, value in values.items()
        if key not in seen and value
    )
    return "\n".join(out).rstrip("\n") + "\n"


def mcp_snippet(api_url: str, token: str) -> str:
    """Return the MCP client configuration for this installation.

    The env var names here are the ones ``mcp_server.client.PraxisClient``
    actually reads.  A snippet with plausible-but-wrong names is worse than no
    snippet: the operator pastes it and the server silently falls back to its
    built-in default URL with no token.

    Args:
        api_url: Base URL the MCP server should call.
        token: Value of ``AUTH_TOKEN`` for this installation.

    Returns:
        Pretty-printed JSON, ready to paste into an MCP client config.
    """
    return json.dumps(
        {
            "mcpServers": {
                "praxis": {
                    "command": "praxis-mcp",
                    "env": {
                        "PRAXIS_BASE_URL": api_url,
                        "PRAXIS_AUTH_TOKEN": token,
                    },
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


def _compose(args: list[str], what: str) -> None:
    """Run one ``docker compose`` invocation, failing with a next step.

    Raises:
        typer.Exit: With code 1 when docker is missing or the command fails.
            A raw ``CalledProcessError`` traceback is the wrong output for the
            first command a new operator ever runs.
    """
    try:
        subprocess.run(  # nosec B603 B607 - fixed argv, operator-invoked
            ["docker", "compose", *args], check=True
        )
    except FileNotFoundError as exc:
        console.print(
            "[red]docker was not found on PATH.[/red] Install Docker Desktop "
            "(or the docker CLI), then re-run `praxis init`."
        )
        raise typer.Exit(code=1) from exc
    except subprocess.CalledProcessError as exc:
        console.print(
            f"[red]{what} failed (exit {exc.returncode}).[/red] Fix the error "
            "above, then re-run `praxis init`; it is safe to re-run."
        )
        raise typer.Exit(code=1) from exc


def _int_or(value: str, fallback: int) -> int:
    """Return ``value`` as an int, or ``fallback`` when it is not one."""
    try:
        return int(value)
    except ValueError:
        return fallback


def _resolve_auth_token(current: dict[str, str]) -> str:
    """Return the auth token to use, reusing the existing one by default.

    Generating a fresh token on every run would rotate the value out from
    under every MCP client already configured against this installation, and
    "hold Enter through the prompts" is exactly how a re-run is performed.
    """
    existing = current.get("AUTH_TOKEN", "")
    if existing:
        # Split out of an `and` deliberately: mypy cannot solve rich's
        # `DefaultType` overload inside a short-circuit and reports the bool
        # default as a str. The annotation pins it.
        reuse: bool = Confirm.ask("Reuse the AUTH_TOKEN already in .env?", default=True)
        if reuse:
            return existing
    return Prompt.ask("Auth token", default=generate_token())


def _resolve_github_token(current: dict[str, str]) -> str | None:
    """Prompt for a GitHub credential, returning None for "leave it alone".

    None rather than "" because :func:`merge_env` reads them differently: ""
    is an authored value that clears the key, and clearing is the opposite of
    what this prompt promises a blank answer does.

    Args:
        current: The ``.env`` as it stands, used to phrase the prompt.

    Returns:
        The credential to write, or None to leave any existing line alone.
    """
    existing = current.get("GITHUB_TOKEN", "")
    if existing:
        # 'skip' is NOT offered here: an empty answer preserves the existing
        # line, so it could not do what its name promises. Say the true way.
        console.print(
            "\nGit access. A GITHUB_TOKEN is already set; blank keeps it. "
            "To remove it, delete the line from .env."
        )
        answer = Prompt.ask("GitHub token (blank keeps the current one)", default="")
    else:
        console.print(
            "\nGit access. Choose 'skip' to evaluate Praxis against a local "
            "bare repo with no GitHub credential at all."
        )
        answer = Prompt.ask("GitHub token (or 'skip' for local mode)", default="skip")
    answer = answer.strip()
    return None if answer.lower() in ("", "skip") else answer


def _default_preset_index(presets: list[dict[str, Any]]) -> int:
    """Return the 1-based menu default.

    An explicit ``default: true`` in the settings YAML wins outright. That
    is the deployment saying "I have already satisfied this preset's one-time
    setup", which the fallback rule below cannot express: a one-time
    interactive login leaves no evidence in the YAML.

    Absent that flag, the default is the first preset ``init`` can satisfy
    unaided.  Holding Enter through every prompt is the documented re-run, so
    the default must never be a preset that cannot work.  ``requires:
    [api_key]`` is exactly that: ``init`` collects no key, :class:`Settings`
    has no field for one, and ``LM_STUDIO_URL`` IS forwarded into the
    container, so the default answer silently repointed every ``local`` router
    call-site at an endpoint that rejects it.

    Flagging a preset default does NOT bypass the guard, it only changes what
    is offered: :func:`_confirm_unmet_requirements` still challenges an unmet
    requirement before the choice is accepted.

    Args:
        presets: The menu, in display order.

    Returns:
        The 1-based index to offer as the default.  Falls back to 1 when
        nothing on the menu is satisfiable; :func:`_confirm_unmet_requirements`
        is what stops that case.
    """
    for index, preset in enumerate(presets, start=1):
        if preset.get("default"):
            return index
    for index, preset in enumerate(presets, start=1):
        if not preset["requires"]:
            return index
    return 1


def _confirm_unmet_requirements(preset: dict[str, Any]) -> None:
    """Make an unsatisfiable preset an explicit choice, never a default one.

    ``init`` can collect none of these: there is no ``Settings`` field for an
    API key and no way to drive an interactive login from here.  The
    confirmation defaults to no, so the operator who is not reading cannot
    answer it by holding Enter.

    Args:
        preset: The chosen preset.

    Raises:
        typer.Exit: With code 1 when the operator declines, before anything
            has been written or started.
    """
    requires = list(preset["requires"])
    if not requires:
        return
    console.print(
        f"\n[yellow]{preset['label']} needs {', '.join(requires)}, which "
        "`praxis init` cannot collect.[/yellow] You would have to configure "
        "it yourself afterwards, or the worker fails its first task."
    )
    if preset.get("default"):
        # A flagged default is the deployment asserting the one-time setup is
        # already done, so "you picked the wrong thing" is the wrong framing
        # here: say what to check instead of implying a mistake.
        console.print(
            "This preset is the configured default for this deployment, which "
            "normally means the one-time setup is already done. Answer yes if "
            "you have completed it."
        )
    proceed: bool = Confirm.ask("Choose it anyway?", default=False)
    if not proceed:
        console.print(
            "[red]No worker preset chosen.[/red] Re-run `praxis init` and pick "
            "one that needs no credential."
        )
        raise typer.Exit(code=1)


def _choose_preset(presets: list[dict[str, Any]]) -> dict[str, Any]:
    """Print the preset menu and return the operator's choice.

    Args:
        presets: The menu, in display order.

    Returns:
        The chosen preset.

    Raises:
        typer.Exit: With code 1 when the choice needs a credential ``init``
            cannot collect and the operator declines it.
    """
    console.print("\nWorker presets:")
    for index, preset in enumerate(presets, start=1):
        requires = ", ".join(preset["requires"])
        extra = f"  (requires: {requires})" if requires else ""
        console.print(f"  {index}. {preset['label']}{extra}")
    default = _default_preset_index(presets)
    choice = IntPrompt.ask("Preset", default=default) - 1
    chosen = presets[max(0, min(choice, len(presets) - 1))]
    _confirm_unmet_requirements(chosen)
    return chosen


def _managed_values(
    token: str, gh_token: str | None, port: str, preset: Mapping[str, Any]
) -> dict[str, str | None]:
    """Return every key ``init`` writes and what each one gets.

    Split out of :func:`init` so the managed key set is testable.  Deleting a
    key from :data:`MANAGED_KEYS` used to leave the whole suite green while
    making ``init`` raise ``ValueError`` out of :func:`merge_env`, after every
    prompt had already been answered.

    Args:
        token: Auth token to write.
        gh_token: GitHub credential, or None to leave any existing one alone.
        port: Dashboard port, as a string.
        preset: The chosen worker preset.  Its values are authored, so an
            empty endpoint clears ``LM_STUDIO_URL`` rather than keeping it.

    Returns:
        Managed key to value, in the order ``init`` writes them.
    """
    return {
        "AUTH_TOKEN": token,
        "GITHUB_TOKEN": gh_token,
        "PORT": port,
        "LM_STUDIO_URL": preset["endpoint"],
        "DEFAULT_WORKER_HARNESS": preset["harness"],
        "DEFAULT_WORKER_MODEL": preset["model"],
    }


def cli_env_exports(api_url: str, token: str) -> dict[str, str]:
    """Return the environment `praxis doctor` and `praxis pending` need.

    These are the names ``cli.main`` actually reads, and the reason to print
    them at all is that its default URL is port 8080, not the compose-mapped
    one: without them the very next `praxis doctor` reports the orchestrator
    unreachable even though it is running, which reads as a broken install.
    A printed name that no longer matches what the CLI reads is the same
    silent failure the MCP snippet had, so both shells are rendered from this
    one mapping.

    Args:
        api_url: Base URL the running orchestrator answers on.
        token: Value of ``AUTH_TOKEN`` for this installation.

    Returns:
        Environment variable name to value.
    """
    return {"ORCHESTRATOR_URL": api_url, "ORCHESTRATOR_TOKEN": token}


def _print_next_steps(
    api_url: str,
    token: str,
    preset: Mapping[str, Any],
    *,
    preset_written: bool = True,
) -> None:
    """Print the MCP snippet, the CLI env vars, and the worker-defaults caveat.

    Args:
        api_url: Base URL the running orchestrator answers on.
        token: Value of ``AUTH_TOKEN`` for this installation.
        preset: The worker preset to describe in the caveat.  When
            ``preset_written`` is False this must already reflect what is
            actually on disk (see ``init``'s decline branch), not the preset
            the operator picked and then declined to write.
        preset_written: Whether ``preset`` is what `.env` was just written
            with.  False on the decline path, where nothing was written and
            the caveat has to describe the file's existing contents instead
            of claiming a preset it did not put there.
    """
    from orchestrator.core.settings_file import config_file_path

    console.print("\n[green]Praxis is running.[/green]")
    console.print(f"  Dashboard: {api_url}")

    console.print("\nAdd it to your MCP client with this configuration:\n")
    console.print(mcp_snippet(api_url, token))

    # The CLI reads a DIFFERENT pair of env vars than the MCP server. Both
    # forms below come from `cli_env_exports` so they cannot drift from each
    # other, or from what `cli.main` reads.
    exports = cli_env_exports(api_url, token)
    console.print("\nExport these so `praxis doctor` and `praxis pending` reach it:\n")
    for name, value in exports.items():
        console.print(f"  export {name}={value}", highlight=False)
    powershell = "; ".join(f'$env:{n}="{v}"' for n, v in exports.items())
    console.print(f"  (PowerShell: {powershell})", highlight=False)

    # Both compose files forward DEFAULT_WORKER_* as bare pass-through entries,
    # so the preset written above is what the container resolves. The mounted
    # settings YAML is still the fallback and still applies to a key `.env`
    # does not set, which is why the doctor's own reading is the one to trust.
    if preset_written:
        console.print(
            f"\n[yellow]Note:[/yellow] preset {preset['name']!r} was written to .env "
            f"and is forwarded into the container, where it overrides the worker "
            f"defaults in {config_file_path()}. Check the worker row in the table "
            "below for what the orchestrator actually resolved."
        )
    else:
        console.print(
            f"\n[yellow]Note:[/yellow] .env was left unchanged, so the worker config "
            f"it already has (DEFAULT_WORKER_HARNESS={preset['harness']!r}, "
            f"DEFAULT_WORKER_MODEL={preset['model']!r}) is what is forwarded into the "
            f"container, not the preset just picked above. It still overrides the "
            f"worker defaults in {config_file_path()}. Check the worker row in the "
            "table below for what the orchestrator actually resolved."
        )


def init() -> None:
    """Set up and start Praxis, then verify it.

    Raises:
        typer.Exit: Always. The exit code is the doctor's verdict, so a
            scripted install can gate on whether the result actually works,
            or 1 when this is not the Praxis repo root.
    """
    console.print("[bold]praxis init[/bold]\n")

    # First, before any prompt and long before any write: everything below is
    # CWD-relative, so the wrong CWD means a secret in the wrong directory.
    root = _require_repo_root()

    env_path = root / ".env"
    existing = env_path.read_text(encoding="utf-8") if env_path.is_file() else ""
    current = parse_env(existing)

    token = _resolve_auth_token(current)
    port = str(
        IntPrompt.ask(
            "Dashboard port", default=_int_or(current.get("PORT", ""), _DEFAULT_PORT)
        )
    )
    gh_token = _resolve_github_token(current)
    preset = _choose_preset(_fetch_presets_or_defaults())

    values = _managed_values(token=token, gh_token=gh_token, port=port, preset=preset)
    env_text = merge_env(existing, values) if existing else build_env_file(values)
    preset_written = True
    if existing and not Confirm.ask(f"Update {env_path}?", default=True):
        # Compose reads .env, not these variables, so the rest of the run has
        # to follow the file rather than the answers it just discarded.
        token = current.get("AUTH_TOKEN", token)
        port = current.get("PORT", port)
        # The chosen preset was never written either: what actually landed
        # in the container is whatever the file already had, so the
        # next-steps caveat has to describe THAT, not the declined choice.
        preset = {
            **preset,
            "harness": current.get("DEFAULT_WORKER_HARNESS", ""),
            "model": current.get("DEFAULT_WORKER_MODEL", ""),
        }
        preset_written = False
        console.print("[yellow]Left .env unchanged; using its current values.[/yellow]")
    else:
        env_path.write_text(env_text, encoding="utf-8")
        console.print(f"[green]Wrote {env_path}[/green]")

    console.print("\nBuilding agent images (this takes a few minutes the first time)")
    _compose(["--profile", "agents", "build"], "the agent image build")

    console.print("Starting the orchestrator")
    _compose(["up", "-d", "--build"], "starting the orchestrator")

    api_url = f"http://127.0.0.1:{port}"
    console.print(f"Waiting for {api_url}/health")
    if not _wait_for_health(api_url):
        console.print(
            "[red]The orchestrator did not become healthy.[/red] "
            "Check `docker logs --tail 50 orchestrator`, then re-run "
            "`praxis init`."
        )
        raise typer.Exit(code=1)

    _print_next_steps(api_url, token, preset, preset_written=preset_written)

    console.print("\nVerifying the installation:\n")
    raise typer.Exit(code=_run_doctor(api_url, token))


def _run_doctor(api_url: str, token: str) -> int:
    """Render the doctor table for this installation and return its exit code.

    Reuses ``cli.doctor``'s own payload handling so a 401 (an ``AUTH_TOKEN``
    the running container disagrees with) still renders a table with the
    matching fix hint, instead of a ``KeyError`` on a JSON error body.
    """
    from cli.doctor import _payload_for, _unreachable_payload, render

    try:
        with httpx.Client(
            base_url=api_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
        ) as client:
            payload = _payload_for(client.get("/api/doctor"))
    except httpx.RequestError as exc:
        payload = _unreachable_payload(exc)
    return render(payload)


def _fetch_presets_or_defaults() -> list[dict[str, Any]]:
    """Read presets from the local YAML so init works before the server is up."""
    from orchestrator.core.settings_file import config_file_path, load_yaml_settings
    from orchestrator.core.worker_presets import parse_presets

    raw = load_yaml_settings(config_file_path()).get("worker_presets", [])
    presets = [
        {
            "name": p.name,
            "label": p.label,
            "harness": p.harness,
            "model": p.model,
            "endpoint": p.endpoint,
            "requires": list(p.requires),
            "default": p.default,
        }
        for p in parse_presets(raw)
    ]
    return presets or [_FALLBACK_PRESET]
