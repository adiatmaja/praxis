"""`praxis doctor`: one table, green and red, one fix hint per red."""

from __future__ import annotations

from typing import Any

import httpx
import typer
from rich.console import Console
from rich.table import Table


console = Console()

_SYMBOL = {
    "green": "[green]OK[/green]",
    "amber": "[yellow]NOTE[/yellow]",
    "red": "[red]FAIL[/red]",
}


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


def _synthetic_payload(
    red_check_id: str, detail: str, not_checked: str
) -> dict[str, Any]:
    """One red row carrying its registry hint; every other row honest amber.

    The shared shape behind every locally-synthesized table.  Only the row this
    process actually knows something about is asserted red; a CLI running
    against a remote deployment cannot inspect that server's Docker daemon,
    build, or credentials, so fabricating verdicts for them would be worse
    than saying nothing.  The red row's own registry hint comes along, which
    is the whole reason a table beats a one-line error.
    """
    from orchestrator.core.doctor import CHECKS

    checks: list[dict[str, Any]] = []
    for check in CHECKS:
        red = check.check_id == red_check_id
        checks.append(
            {
                "check_id": check.check_id,
                "label": check.label,
                "status": "red" if red else "amber",
                "detail": detail if red else not_checked,
                "hint": check.hint if red else "",
            }
        )
    return {"status": "red", "checks": checks}


def _unreachable_payload(exc: Exception) -> dict[str, Any]:
    """Synthesize a doctor payload when the API cannot be reached at all.

    ``docker_daemon`` and ``orchestrator_health`` are exactly the checks an
    operator needs when the server does not answer, so this renders a table
    instead of letting the connection error surface as a raw traceback (the
    module's own contract: "never raises, it diagnoses a broken machine").

    ``orchestrator_health`` is the red row: the request never got an answer,
    and its registry hint already points at the likely root cause (start the
    orchestrator, check its logs).
    """
    return _synthetic_payload(
        "orchestrator_health",
        f"could not reach the orchestrator API: {exc}",
        "not checked: the orchestrator API is unreachable",
    )


def _error_status_payload(response: httpx.Response) -> dict[str, Any]:
    """Synthesize a doctor payload from an error RESPONSE, not an exception.

    ``cli.main._check_dict`` prints a bare ``Error 401: Unauthorized`` and
    exits(1) on any status >= 400, before ``render()`` can run.  That is the
    one path where an operator gets no table at all, and ``/api/doctor`` is
    itself auth-gated, so a wrong ``AUTH_TOKEN`` (one of doctor's own eleven
    checks) is the likeliest way to land here and the case where the table
    helps most.  401/403 point at ``auth_token``; anything else is the
    server's own fault and points at ``orchestrator_health``.
    """
    status = response.status_code
    body = " ".join(response.text.split())[:200]
    red = "auth_token" if status in (401, 403) else "orchestrator_health"
    detail = f"the orchestrator API answered HTTP {status}"
    return _synthetic_payload(
        red,
        f"{detail}: {body}" if body else detail,
        f"not checked: {detail}",
    )


def _payload_for(response: httpx.Response) -> dict[str, Any]:
    """The API's own diagnosis, or a synthesized one when it did not give one."""
    if response.status_code >= 400:
        return _error_status_payload(response)
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict) or not isinstance(payload.get("checks"), list):
        return _synthetic_payload(
            "orchestrator_health",
            "the orchestrator API answered 200 with a body that is not a diagnosis",
            "not checked: the orchestrator API's response was unreadable",
        )
    return payload


def doctor() -> None:
    """Diagnose this Praxis installation. Exits non-zero on any failure.

    Deliberately does NOT route through ``cli.main._check_dict``: its
    exit-on-error path is correct for every other command and wrong for this
    one, which owes the operator a table whatever the server said.
    """
    from cli.main import _client

    try:
        with _client() as client:
            payload = _payload_for(client.get("/api/doctor"))
    except httpx.RequestError as exc:
        payload = _unreachable_payload(exc)
    raise typer.Exit(code=render(payload))
