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

_UNESCAPE = re.compile(r"\\(.)")

_DEFAULT_PORT = 12323

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


def parse_env(text: str) -> dict[str, str]:
    """Parse ``.env`` text into a plain mapping, undoing :func:`_render_value`.

    Comments, blank lines and anything without an ``=`` are skipped.  This is
    read-only and lossy on purpose: it exists so ``init`` can default its
    prompts to what the operator already has, never to rewrite the file from
    the parsed result.

    Args:
        text: Raw contents of a ``.env`` file.

    Returns:
        Mapping of key to unquoted, unescaped value.
    """
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, raw = stripped.partition("=")
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            inner = value[1:-1]
            value = _UNESCAPE.sub(r"\1", inner) if value[0] == '"' else inner
        parsed[key.strip()] = value
    return parsed


def build_env_file(values: dict[str, str]) -> str:
    """Render a fresh ``.env`` from the managed values only.

    Empty values are omitted rather than written blank, which is what lets
    local mode (no GitHub credential at all) produce a file with no
    ``GITHUB_TOKEN`` line instead of one that looks configured but is not.

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


def merge_env(existing: str, values: dict[str, str]) -> str:
    """Merge managed values into an existing ``.env`` text.

    Unrelated keys, comments and blank lines are preserved verbatim and keep
    their position; a managed key already present is replaced IN PLACE (never
    appended, or re-runs accumulate duplicates whose last entry silently
    wins); a managed key absent is appended.  An empty value means "no
    opinion" and leaves any existing line alone, so an operator holding Enter
    through the prompts cannot delete a working credential.

    Args:
        existing: Current ``.env`` contents.  May be empty.
        values: Managed key to value.  Every key must be in
            :data:`MANAGED_KEYS`.

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
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in values:
            seen.add(key)
            out.append(f"{key}={_render_value(values[key])}" if values[key] else line)
        else:
            out.append(line)
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


def _resolve_github_token(current: dict[str, str]) -> str:
    """Prompt for a GitHub credential, returning "" for "leave it alone"."""
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
    return "" if answer.lower() in ("", "skip") else answer


def _choose_preset(presets: list[dict[str, Any]]) -> dict[str, Any]:
    """Print the preset menu and return the operator's choice."""
    console.print("\nWorker presets:")
    for index, preset in enumerate(presets, start=1):
        requires = ", ".join(preset["requires"])
        extra = f"  (requires: {requires})" if requires else ""
        console.print(f"  {index}. {preset['label']}{extra}")
    choice = IntPrompt.ask("Preset", default=1) - 1
    return presets[max(0, min(choice, len(presets) - 1))]


def _print_next_steps(api_url: str, token: str, preset: dict[str, Any]) -> None:
    """Print the MCP snippet, the CLI env vars, and the worker-defaults caveat."""
    from orchestrator.core.settings_file import config_file_path

    console.print("\n[green]Praxis is running.[/green]")
    console.print(f"  Dashboard: {api_url}")

    console.print("\nAdd it to your MCP client with this configuration:\n")
    console.print(mcp_snippet(api_url, token))

    # The CLI reads a DIFFERENT pair of env vars than the MCP server, and its
    # default URL is port 8080, not the compose-mapped one. Without these two
    # exports the very next `praxis doctor` reports the orchestrator
    # unreachable even though it is running, which reads as a broken install.
    console.print("\nExport these so `praxis doctor` and `praxis pending` reach it:\n")
    console.print(f"  export ORCHESTRATOR_URL={api_url}")
    console.print("  export ORCHESTRATOR_TOKEN=<the AUTH_TOKEN above>")
    console.print(
        f'  (PowerShell: $env:ORCHESTRATOR_URL="{api_url}"; '
        '$env:ORCHESTRATOR_TOKEN="...")'
    )

    # DEFAULT_WORKER_* are read from .env only by a bare `uvicorn` run: the
    # compose files do not forward them into the container, which reads its
    # worker defaults from the mounted settings YAML instead. Saying so beats
    # letting the preset choice look like it took effect when it did not.
    console.print(
        f"\n[yellow]Note:[/yellow] the containerized orchestrator reads its worker "
        f"defaults from {config_file_path()}, not from .env. Preset "
        f"{preset['name']!r} was recorded in .env for bare-uvicorn runs; check the "
        "worker row in the table below for what the container actually resolved."
    )


def init() -> None:
    """Set up and start Praxis, then verify it.

    Raises:
        typer.Exit: Always. The exit code is the doctor's verdict, so a
            scripted install can gate on whether the result actually works.
    """
    console.print("[bold]praxis init[/bold]\n")

    env_path = Path(".env")
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
        # Compose reads .env, not these variables, so the rest of the run has
        # to follow the file rather than the answers it just discarded.
        token = current.get("AUTH_TOKEN", token)
        port = current.get("PORT", port)
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

    _print_next_steps(api_url, token, preset)

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
        }
        for p in parse_presets(raw)
    ]
    return presets or [_FALLBACK_PRESET]
