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
import os
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

#: What the whole run costs, said before the first prompt.  The numbers are
#: measured on this repository, not estimated: ~2 minutes end to end with a
#: warm Docker layer cache, ~10 with a cold one, and the agent image builds
#: are nearly all of both.  It names a RANGE and what drives it, because the
#: failure mode is not impatience, it is an operator on a cold cache deciding
#: at minute six that a run promised as "a few minutes" has hung.
RUNTIME_EXPECTATION = (
    "[dim]This takes about 2 minutes if Docker has these layers cached, and "
    "about 10 minutes on a first run that has to build them. The agent image "
    "builds are nearly all of it; everything else is seconds. Elapsed times "
    "are reported as each step finishes.[/dim]\n"
)


def _format_elapsed(started: float, now: float) -> str:
    """Render the interval between two ``time.monotonic()`` readings.

    Args:
        started: The reading taken when the step began.
        now: The reading taken when it finished.

    Returns:
        ``"1m23s"`` at or above a minute, ``"23s"`` below one.  Whole seconds
        only: this is an operator's sanity check against a hang, and a
        fractional part reads as precision that means nothing here.
    """
    minutes, seconds = divmod(max(int(now - started), 0), 60)
    return f"{minutes}m{seconds:02d}s" if minutes else f"{seconds}s"


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
    # The claim is qualified on purpose. `merge_env` drops the WHOLE line,
    # trailing comment included, when a preset authors "" for a managed key
    # (`gemini-agy` does exactly that to `LM_STUDIO_URL`). Removing the line is
    # the behaviour worth keeping: a comment left hanging over a key that no
    # longer exists documents a setting the file does not have. So the promise
    # is narrowed to what is true rather than the behaviour bent to match it.
    lines = [
        "# Written by `praxis init`. Safe to edit; re-running init preserves",
        "# every key it does not manage. Comments survive too, except a",
        "# trailing comment on a managed key a preset switch removes.",
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


def mcp_snippet(api_url: str, token: str, praxis_root: Path | None = None) -> str:
    """Return the MCP client configuration for this installation.

    The env var names here are the ones ``mcp_server.client.PraxisClient``
    actually reads.  A snippet with plausible-but-wrong names is worse than no
    snippet: the operator pastes it and the server silently falls back to its
    built-in default URL with no token.

    The command is ``uv run --directory <praxis_root> praxis-mcp``, never the
    bare console script: ``praxis`` is not put on the PATH, so an MCP client
    spawning ``praxis-mcp`` from any other project - the normal place to use
    it - fails with command-not-found.  ``--directory`` makes the invocation
    cwd-independent.

    Args:
        api_url: Base URL the MCP server should call.
        token: Value of ``AUTH_TOKEN`` for this installation.
        praxis_root: Absolute path of the Praxis checkout.  Defaults to the
            CWD, which ``_require_repo_root`` has already proven is the root
            on every path that prints this.

    Returns:
        Pretty-printed JSON, ready to paste into an MCP client config.
    """
    root = praxis_root if praxis_root is not None else Path.cwd()
    return json.dumps(
        {
            "mcpServers": {
                "praxis": {
                    "command": "uv",
                    "args": ["run", "--directory", str(root.resolve()), "praxis-mcp"],
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


def _compose(args: list[str], what: str, env: dict[str, str] | None = None) -> None:
    """Run one ``docker compose`` invocation, failing with a next step.

    Args:
        args: Compose subcommand and flags, e.g. ``["up", "-d"]``.
        what: Human description used in the failure message.
        env: Process environment for the subprocess, or ``None`` to inherit
            the parent's (``subprocess.run``'s own default).  The agent image
            build passes a merged environment carrying the per-harness
            entrypoint content hash as a compose build arg; every other
            invocation needs nothing extra.

    Raises:
        typer.Exit: With code 1 when docker is missing or the command fails.
            A raw ``CalledProcessError`` traceback is the wrong output for the
            first command a new operator ever runs.
    """
    try:
        subprocess.run(  # nosec B603 B607 - fixed argv, operator-invoked
            ["docker", "compose", *args], check=True, env=env
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


def _clear_env_placeholder_dir(env_path: Path) -> None:
    """Remove an EMPTY directory Docker created where ``.env`` belongs.

    Both compose files bind-mount ``./.env`` so the doctor's ``env_drift``
    check can read it.  Docker creates a missing host bind-mount source as a
    DIRECTORY, so running ``docker compose up`` in a fresh clone BEFORE
    ``praxis init`` leaves a ``.env/`` directory, and every later write to
    ``.env`` then fails with ``IsADirectoryError`` for a reason nothing on
    screen explains.

    Only an EMPTY directory is removed: that shape is unambiguously Docker's
    artifact and never operator data.  A non-empty one is left alone and
    reported, because guessing at deletion there could destroy real files.

    Args:
        env_path: The ``.env`` path about to be read and written.

    Raises:
        typer.Exit: If the directory exists and is not empty.
    """
    if not env_path.is_dir():
        return
    try:
        env_path.rmdir()
    except OSError:
        console.print(
            f"[red]{env_path} is a non-empty directory, not a file.[/red] "
            "Docker creates this when `docker compose up` runs before "
            "`praxis init`. Inspect it, move it aside, then re-run."
        )
        raise typer.Exit(code=1) from None
    console.print(
        f"[dim]Removed the empty {env_path} directory Docker left behind "
        "(compose ran before init).[/dim]"
    )


def _entrypoint_build_env(root: Path) -> dict[str, str]:
    """Return build-arg env vars carrying each harness entrypoint's hash.

    The compose build args are read from the process environment, so these
    are merged into the env passed to the build subprocess.  A harness whose
    entrypoint cannot be read contributes no key: an absent build arg leaves
    the label empty, which the doctor reports as "cannot judge" rather than
    as a mismatch.

    Args:
        root: The repository root.

    Returns:
        ``{"<HARNESS>_ENTRYPOINT_SHA256": "<hex>"}`` for each readable file.
    """
    from orchestrator.core.entrypoint_hash import hash_entrypoint
    from orchestrator.core.harnesses import REGISTRY

    build_env: dict[str, str] = {}
    for harness in REGISTRY.values():
        path = root / "docker" / f"{harness.id}-agent" / "entrypoint.sh"
        digest = hash_entrypoint(path)
        if digest is not None:
            build_env[f"{harness.id.upper()}_ENTRYPOINT_SHA256"] = digest
    return build_env


def _int_or(value: str, fallback: int) -> int:
    """Return ``value`` as an int, or ``fallback`` when it is not one."""
    try:
        return int(value)
    except ValueError:
        return fallback


@dataclass(frozen=True)
class Answers:
    """Answers supplied on the command line instead of at a prompt.

    Every field pre-answers exactly one question the wizard would ask, so the
    two modes share one decision path rather than growing a second one that
    can drift.

    ``non_interactive`` is the only field that changes behaviour on its own:
    it forbids prompting outright, so a question with no flag and no safe
    default becomes an error naming the flag rather than a process that hangs
    forever on a closed stdin.

    Non-interactive matters more than it looks. The wizard's QUESTION SET
    changes with state: five questions on a fresh clone, seven once `.env`
    exists ("Reuse the AUTH_TOKEN already in .env?" first, "Update .env?"
    last). Piping a fixed answer set is therefore not merely awkward, it is
    wrong: an answer sequence that works on the first run silently misaligns
    on the second and answers the wrong questions.
    """

    non_interactive: bool = False
    auth_token: str | None = None
    github_token: str | None = None
    port: int | None = None
    preset: str | None = None
    accept_preset_requirements: bool = False


def _resolve_auth_token(current: dict[str, str], answers: Answers) -> str:
    """Return the auth token to use, reusing the existing one by default.

    Generating a fresh token on every run would rotate the value out from
    under every MCP client already configured against this installation, and
    "hold Enter through the prompts" is exactly how a re-run is performed.
    Non-interactive follows the same rule for the same reason.
    """
    if answers.auth_token:
        return answers.auth_token
    existing = current.get("AUTH_TOKEN", "")
    if answers.non_interactive:
        if existing:
            return existing
        token = generate_token()
        # Printed, and printed prominently: this is the one generated value
        # the operator cannot recover from the console scrollback of a prompt
        # they never saw, and every MCP client and CLI shell needs it.
        console.print(f"[green]Generated AUTH_TOKEN:[/green] {token}", highlight=False)
        return token
    if existing:
        # Split out of an `and` deliberately: mypy cannot solve rich's
        # `DefaultType` overload inside a short-circuit and reports the bool
        # default as a str. The annotation pins it.
        reuse: bool = Confirm.ask("Reuse the AUTH_TOKEN already in .env?", default=True)
        if reuse:
            return existing
    return Prompt.ask("Auth token", default=generate_token())


def _resolve_port(current: dict[str, str], answers: Answers) -> str:
    """Return the dashboard port: flag, then the existing `.env`, then default."""
    if answers.port is not None:
        return str(answers.port)
    existing = _int_or(current.get("PORT", ""), _DEFAULT_PORT)
    if answers.non_interactive:
        return str(existing)
    return str(IntPrompt.ask("Dashboard port", default=existing))


def _resolve_github_token(current: dict[str, str], answers: Answers) -> str | None:
    """Prompt for a GitHub credential, returning None for "leave it alone".

    None rather than "" because :func:`merge_env` reads them differently: ""
    is an authored value that clears the key, and clearing is the opposite of
    what this prompt promises a blank answer does.

    Args:
        current: The ``.env`` as it stands, used to phrase the prompt.
        answers: Command-line answers; ``--github-token`` pre-answers this.

    Returns:
        The credential to write, or None to leave any existing line alone.
    """
    if answers.github_token:
        return answers.github_token
    existing = current.get("GITHUB_TOKEN", "")
    if answers.non_interactive:
        # None is the same answer a blank / "skip" gives interactively: keep
        # an existing line, write none on a fresh install (local mode). It is
        # never "" here, which would CLEAR a working credential, and an
        # unattended run must not do that by omission.
        return None
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


#: Why a preset ended up as the menu default, one string per rule in
#: :func:`_default_preset_choice`.  Printed verbatim, because "deployment
#: default" said unconditionally is an assertion about the settings file that
#: two of the three rules cannot support: the fallbacks fire precisely when
#: nothing is flagged, and the hardcoded :data:`_FALLBACK_PRESET` carries no
#: ``default`` key at all.
_RULE_FLAGGED = "flagged the default for this deployment"
_RULE_NO_CREDENTIAL = "first preset needing no credential; none is flagged default"
_RULE_FIRST_ON_MENU = (
    "first on the menu; none is flagged default, all need a credential"
)


def _default_preset_choice(presets: list[dict[str, Any]]) -> tuple[int, str]:
    """Return the 1-based menu default and the rule that selected it.

    The rule travels with the index rather than being re-derived by the
    caller: the parenthetical printed next to the chosen preset is a claim
    about WHY it was chosen, and a claim derived separately from the choice is
    one that can be wrong while the choice is right.

    Args:
        presets: The menu, in display order.

    Returns:
        ``(index, rule)``, where ``rule`` is one of :data:`_RULE_FLAGGED`,
        :data:`_RULE_NO_CREDENTIAL` or :data:`_RULE_FIRST_ON_MENU`.
    """
    for index, preset in enumerate(presets, start=1):
        if preset.get("default"):
            return index, _RULE_FLAGGED
    for index, preset in enumerate(presets, start=1):
        if not preset["requires"]:
            return index, _RULE_NO_CREDENTIAL
    return 1, _RULE_FIRST_ON_MENU


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
    return _default_preset_choice(presets)[0]


def _confirm_unmet_requirements(preset: dict[str, Any], answers: Answers) -> None:
    """Make an unsatisfiable preset an explicit choice, never a default one.

    ``init`` can collect none of these: there is no ``Settings`` field for an
    API key and no way to drive an interactive login from here.  The
    confirmation defaults to no, so the operator who is not reading cannot
    answer it by holding Enter.

    Non-interactive does NOT bypass this. A flag that skipped every prompt
    would skip this one too, and the guard exists precisely because the
    resulting install starts, reports healthy, and fails its first real task.
    So an unmet requirement is refused unless
    ``--accept-preset-requirements`` says the one-time setup is done, which is
    the same assertion the interactive "Choose it anyway?" makes.

    Args:
        preset: The chosen preset.
        answers: Command-line answers.

    Raises:
        typer.Exit: With code 1 when the operator declines, before anything
            has been written or started.
    """
    requires = list(preset["requires"])
    if not requires:
        return
    if answers.accept_preset_requirements:
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
    setup_hint = preset.get("setup_hint") or ""
    setup_doc = preset.get("setup_doc") or ""
    if setup_hint:
        # Naming the requirement without naming the remedy is what made this
        # stop read as a dead end: the recipe exists in the docs and the
        # preset now carries it, so print it at the point of refusal.
        # markup=False: the hint is config-authored shell text (quotes, `$`,
        # `<...>` placeholders), not console markup we control, and rich
        # treats a literal "[" as the start of a markup tag.
        console.print("\n[bold]To satisfy it:[/bold]")
        console.print(setup_hint.rstrip(), markup=False)
    if setup_doc:
        console.print(f"[dim]Full instructions: {setup_doc}[/dim]")
    proceed = (
        False
        if answers.non_interactive
        else Confirm.ask("Choose it anyway?", default=False)
    )
    if not proceed:
        console.print(
            "[red]No worker preset chosen.[/red] Complete the setup above and "
            "re-run `praxis init`, or re-run it now and pick a preset that "
            "needs no credential."
        )
        if answers.non_interactive:
            console.print(
                "Non-interactive: pass --accept-preset-requirements once the "
                "setup above is done, or --preset <name> to pick another."
            )
        raise typer.Exit(code=1)


def _choose_preset(presets: list[dict[str, Any]], answers: Answers) -> dict[str, Any]:
    """Print the preset menu and return the operator's choice.

    ``--preset`` takes a preset NAME, never the menu index. The index is
    positional and shifts whenever the settings YAML gains or reorders an
    entry, so a pinned index in someone's setup script would silently start
    selecting a different worker.

    Args:
        presets: The menu, in display order.
        answers: Command-line answers; ``--preset`` pre-answers this.

    Returns:
        The chosen preset.

    Raises:
        typer.Exit: With code 1 when the named preset does not exist, or when
            the choice needs a credential ``init`` cannot collect and the
            operator declines it.
    """
    if answers.preset:
        wanted = answers.preset.strip()
        chosen = next((p for p in presets if p["name"] == wanted), None)
        if chosen is None:
            available = ", ".join(p["name"] for p in presets)
            console.print(
                f"[red]No worker preset named {wanted!r}.[/red] Available: {available}"
            )
            raise typer.Exit(code=1)
        _confirm_unmet_requirements(chosen, answers)
        return chosen

    console.print("\nWorker presets:")
    for index, preset in enumerate(presets, start=1):
        requires = ", ".join(preset["requires"])
        extra = f"  (requires: {requires})" if requires else ""
        console.print(f"  {index}. {preset['label']}{extra}")
    default, rule = _default_preset_choice(presets)
    if answers.non_interactive:
        chosen = presets[default - 1]
        # The rule, not a blanket "deployment default": two of the three rules
        # fire only when the settings file flags nothing, so naming the
        # deployment there asserts a configuration that does not exist.
        console.print(f"Preset: {chosen['name']} ({rule})")
    else:
        choice = IntPrompt.ask("Preset", default=default) - 1
        chosen = presets[max(0, min(choice, len(presets) - 1))]
    _confirm_unmet_requirements(chosen, answers)
    return chosen


def _managed_values(
    token: str, gh_token: str | None, port: str, preset: Mapping[str, Any] | None
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
        preset: The chosen worker preset, or None when no preset has been
            chosen yet -- ``init`` writes a partial ``.env`` before the
            preset confirmation can exit.  With a preset, its values are
            authored, so an empty endpoint clears ``LM_STUDIO_URL`` rather
            than keeping it.  With None, every preset-derived key resolves to
            None ("no opinion") rather than "" ("clear"), so
            :func:`merge_env` leaves any existing line alone instead of
            blanking it.

    Returns:
        Managed key to value, in the order ``init`` writes them.
    """
    endpoint = preset.get("endpoint") if preset else None
    harness = preset.get("harness") if preset else None
    model = preset.get("model") if preset else None
    return {
        "AUTH_TOKEN": token,
        "GITHUB_TOKEN": gh_token,
        "PORT": port,
        "LM_STUDIO_URL": endpoint,
        "DEFAULT_WORKER_HARNESS": harness,
        "DEFAULT_WORKER_MODEL": model,
    }


def cli_env_exports(api_url: str, token: str) -> dict[str, str]:
    """Return the environment `praxis doctor` and `praxis pending` need.

    These are the names ``cli.main`` actually reads, and the reason to print
    them at all is that its default URL assumes port 12323 and the nearest
    ``.env``: an install answering on any other port, or a CLI run from
    outside this checkout, matches neither.  Without them the very next
    `praxis doctor` reports the orchestrator unreachable even though it is
    running, which reads as a broken install.
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
    # markup=False: the snippet's JSON list brackets are data, not rich tags.
    console.print(mcp_snippet(api_url, token), markup=False)

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
        console.print(_unchanged_env_note(preset, config_file_path()))


def _unchanged_env_note(preset: Mapping[str, Any], settings_path: str) -> str:
    """Describe the worker config an untouched ``.env`` actually forwards.

    A key `.env` does not set forwards NOTHING: both compose files list
    ``DEFAULT_WORKER_HARNESS`` / ``DEFAULT_WORKER_MODEL`` as bare
    pass-throughs, so an absent key leaves the settings file applying
    unchanged.  Printing ``DEFAULT_WORKER_HARNESS=''`` and calling it an
    override therefore states the opposite of what is in effect, and the
    operator has no way to see the difference: both readings look configured.
    The two keys are reported separately because `.env` can set one and not
    the other, and a single verdict for the pair would be wrong for one half.

    Args:
        preset: The worker config actually on disk, as ``init``'s decline
            branch assembles it: ``harness`` and ``model`` are the existing
            ``.env`` values, or "" when the file sets no such key.
        settings_path: Path of the settings file the container falls back to.

    Returns:
        The note to print, as rich markup.
    """
    keys = (
        ("DEFAULT_WORKER_HARNESS", preset.get("harness") or ""),
        ("DEFAULT_WORKER_MODEL", preset.get("model") or ""),
    )
    present = [(name, value) for name, value in keys if value]
    absent = [name for name, value in keys if not value]
    parts = [
        "\n[yellow]Note:[/yellow] .env was left unchanged, so the preset just "
        "picked above was not written."
    ]
    if present:
        listed = ", ".join(f"{name}={value!r}" for name, value in present)
        parts.append(
            f"It already sets {listed}, which is forwarded into the container "
            f"and overrides that worker default in {settings_path}."
        )
    if absent:
        subject = " or ".join(absent)
        parts.append(
            f"It sets no {subject}, so nothing is forwarded for "
            f"{'those' if len(absent) > 1 else 'that'} and {settings_path} "
            "applies unchanged."
        )
    parts.append(
        "Check the worker row in the table below for what the orchestrator "
        "actually resolved."
    )
    return " ".join(parts)


def init(
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        "-y",
        help=(
            "Never prompt. Take every answer from a flag, the existing .env, "
            "or a documented default. Required when stdin is not a terminal."
        ),
    ),
    auth_token: str | None = typer.Option(
        None,
        "--auth-token",
        help="Auth token to write. Default: reuse .env's, else generate and print one.",
    ),
    preset: str | None = typer.Option(
        None,
        "--preset",
        help=(
            "Worker preset by NAME (not menu position). Default: the preset "
            "the settings file flags default, else the first needing no "
            "credential, else the first on the menu."
        ),
    ),
    port: int | None = typer.Option(
        None, "--port", help=f"Dashboard port. Default: .env's, else {_DEFAULT_PORT}."
    ),
    github_token: str | None = typer.Option(
        None,
        "--github-token",
        help="GitHub credential. Omit to keep any existing one, or for local mode.",
    ),
    accept_preset_requirements: bool = typer.Option(
        False,
        "--accept-preset-requirements",
        help=(
            "Assert a preset's one-time setup (API key, interactive login) is "
            "already done. Without it: --non-interactive refuses such a "
            "preset, while interactively you are asked to confirm and may "
            "still proceed."
        ),
    ),
) -> None:
    """Set up and start Praxis, then verify it.

    Every flag pre-answers exactly one prompt. Add --non-interactive to forbid
    prompting entirely, which is what makes this drivable by an agent or a
    setup script. Exits with the doctor's verdict, so a scripted install can
    gate on whether the result actually works.
    """
    # This docstring is the --help text, so the reasoning lives here instead.
    #
    # Why a flag rather than piping answers at the wizard: the QUESTION SET
    # changes with state. Five questions on a fresh clone, seven once `.env`
    # exists ("Reuse the AUTH_TOKEN already in .env?" first, "Update .env?"
    # last). A fixed answer sequence works on the first run and silently
    # misaligns on the second, answering the wrong questions.
    #
    # Why a shim over `run_init`: typer rewrites these parameters' defaults
    # into `OptionInfo` objects, so a function carrying them cannot also be
    # called directly. Keeping the logic in a plain function that takes one
    # `Answers` is what lets it be driven from a test or from another entry
    # point without going through the command line.
    run_init(
        Answers(
            non_interactive=non_interactive,
            auth_token=auth_token,
            github_token=github_token,
            port=port,
            preset=preset,
            accept_preset_requirements=accept_preset_requirements,
        )
    )


def run_init(answers: Answers) -> None:
    """Set up and start Praxis, then verify it, from resolved answers.

    Args:
        answers: Every command-line answer, already parsed.

    Raises:
        typer.Exit: Always. See :func:`init`.
    """
    console.print("[bold]praxis init[/bold]\n")

    # First, before any prompt and long before any write: everything below is
    # CWD-relative, so the wrong CWD means a secret in the wrong directory.
    root = _require_repo_root()

    # Said up front, before the first prompt and before anything is written,
    # because the only duration claim used to arrive at the build itself and
    # said "a few minutes". Measured, it is ~2 minutes with a warm Docker
    # layer cache and ~10 with a cold one, and the agent image builds are
    # nearly all of it. An operator on a cold cache with "a few minutes" in
    # mind reasonably concludes at minute six that it has hung, and kills it.
    console.print(RUNTIME_EXPECTATION)

    env_path = root / ".env"
    _clear_env_placeholder_dir(env_path)
    existing = env_path.read_text(encoding="utf-8") if env_path.is_file() else ""
    current = parse_env(existing)

    token = _resolve_auth_token(current, answers)
    resolved_port = _resolve_port(current, answers)
    gh_token = _resolve_github_token(current, answers)

    try:
        chosen_preset = _choose_preset(_fetch_presets_or_defaults(), answers)
    except typer.Exit:
        # `_choose_preset` raises out of `_confirm_unmet_requirements` when
        # the operator declines a preset that needs a credential `init`
        # cannot collect.  That used to propagate straight out of `init()`
        # before anything was written, discarding the token, port, and
        # GitHub credential just typed -- the single most likely outcome of
        # holding Enter through Quick Start, leaving a fresh clone with no
        # `.env` at all and exit 1.  Write what was already collected before
        # re-raising.  `preset=None` makes every preset-derived key resolve
        # to "no opinion" rather than "clear", so on a re-run this leaves an
        # existing DEFAULT_WORKER_HARNESS/DEFAULT_WORKER_MODEL/LM_STUDIO_URL
        # line untouched.  Scoped to this except so the unrelated "Update
        # .env?" decline path below (reached only once a preset IS chosen)
        # keeps its own guarantee that declining leaves the file untouched.
        #
        # INTERACTIVE ONLY, and the qualifier is the whole argument above:
        # what is being rescued is "the token, port, and GitHub credential
        # JUST TYPED".  A non-interactive run typed nothing -- every value
        # came off the command line or out of the file already on disk -- so
        # there is nothing to rescue and two things to lose.  Measured:
        # `praxis init --non-interactive --preset nope` printed the red "No
        # worker preset named 'nope'", then `Wrote ...\.env`, then exited 1,
        # so the last line a CI log shows for a refused run claims success;
        # and on a RE-RUN that write merged MANAGED_KEYS into a live install's
        # `.env`, rewriting the operator's working file over a typo.
        # `_choose_preset` exits the same way for an unknown `--preset` name
        # and for an unmet requirement without
        # `--accept-preset-requirements`; neither collected anything.
        if answers.non_interactive:
            raise
        partial = _managed_values(
            token=token, gh_token=gh_token, port=resolved_port, preset=None
        )
        partial_text = (
            merge_env(existing, partial) if existing else build_env_file(partial)
        )
        env_path.write_text(partial_text, encoding="utf-8")
        console.print(f"[green]Wrote {env_path}[/green]")
        raise

    values = _managed_values(
        token=token, gh_token=gh_token, port=resolved_port, preset=chosen_preset
    )
    env_text = merge_env(existing, values) if existing else build_env_file(values)
    preset_written = True
    # Non-interactive never asks: writing `.env` is the whole point of the
    # run, and there is nobody to answer. The merge only touches MANAGED_KEYS
    # and reuses the existing token by default, so this is not a clobber.
    update = (
        True
        if answers.non_interactive
        else Confirm.ask(f"Update {env_path}?", default=True)
    )
    if existing and not update:
        # Compose reads .env, not these variables, so the rest of the run has
        # to follow the file rather than the answers it just discarded.
        token = current.get("AUTH_TOKEN", token)
        resolved_port = current.get("PORT", resolved_port)
        # The chosen preset was never written either: what actually landed
        # in the container is whatever the file already had, so the
        # next-steps caveat has to describe THAT, not the declined choice.
        chosen_preset = {
            **chosen_preset,
            "harness": current.get("DEFAULT_WORKER_HARNESS", ""),
            "model": current.get("DEFAULT_WORKER_MODEL", ""),
        }
        preset_written = False
        console.print("[yellow]Left .env unchanged; using its current values.[/yellow]")
    else:
        env_path.write_text(env_text, encoding="utf-8")
        console.print(f"[green]Wrote {env_path}[/green]")

    # Every duration printed from here on is MEASURED, not predicted: a
    # prediction that turns out wrong is indistinguishable from a hang, and a
    # running elapsed count is the one thing that tells the operator the
    # difference without them having to go and read docker's own output.
    started = time.monotonic()
    console.print("\nBuilding agent images (the long step; see the estimate above)")
    build_env = {**os.environ, **_entrypoint_build_env(root)}
    _compose(["--profile", "agents", "build"], "the agent image build", env=build_env)
    console.print(
        f"  Agent images built in {_format_elapsed(started, time.monotonic())}"
    )

    console.print("Starting the orchestrator")
    _compose(["up", "-d", "--build"], "starting the orchestrator")

    api_url = f"http://127.0.0.1:{resolved_port}"
    console.print(f"Waiting for {api_url}/health")
    if not _wait_for_health(api_url):
        console.print(
            "[red]The orchestrator did not become healthy.[/red] "
            "Check `docker logs --tail 50 orchestrator`, then re-run "
            "`praxis init`."
        )
        raise typer.Exit(code=1)
    console.print(f"  Healthy after {_format_elapsed(started, time.monotonic())} total")

    _print_next_steps(api_url, token, chosen_preset, preset_written=preset_written)

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
            # Carried through explicitly: this dict is built key by key, so a
            # field added to WorkerPreset and never listed here reaches the
            # menu as absent. That is how the setup recipe shipped inert, the
            # YAML held it and the printer looked for it and nothing joined
            # the two.
            "setup_doc": p.setup_doc,
            "setup_hint": p.setup_hint,
        }
        for p in parse_presets(raw)
    ]
    return presets or [_FALLBACK_PRESET]
