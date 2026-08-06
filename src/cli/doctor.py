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


def _unreachable_payload(exc: Exception) -> dict[str, Any]:
    """Synthesize a doctor payload when the API cannot be reached at all.

    ``docker_daemon`` and ``orchestrator_health`` are exactly the checks an
    operator needs when the server does not answer, so this renders a table
    instead of letting the connection error surface as a raw traceback (the
    module's own contract: "never raises, it diagnoses a broken machine").

    Only ``orchestrator_health`` is asserted red here: that is the one fact
    this process actually knows (the request never got an answer). Every
    other check_id becomes an honest "not checked" amber row rather than a
    fabricated verdict, since a CLI running against a remote deployment has
    no way to inspect that server's Docker daemon, build, or credentials
    from here. ``orchestrator_health``'s own registry hint already points at
    the likely root cause (start the orchestrator, check its logs).
    """
    from orchestrator.core.doctor import CHECKS

    checks: list[dict[str, Any]] = []
    for check in CHECKS:
        if check.check_id == "orchestrator_health":
            checks.append(
                {
                    "check_id": check.check_id,
                    "label": check.label,
                    "status": "red",
                    "detail": f"could not reach the orchestrator API: {exc}",
                    "hint": check.hint,
                }
            )
        else:
            checks.append(
                {
                    "check_id": check.check_id,
                    "label": check.label,
                    "status": "amber",
                    "detail": "not checked: the orchestrator API is unreachable",
                    "hint": "",
                }
            )
    return {"status": "red", "checks": checks}


def doctor() -> None:
    """Diagnose this Praxis installation. Exits non-zero on any failure."""
    from cli.main import _check_dict, _client

    try:
        with _client() as client:
            payload = _check_dict(client.get("/api/doctor"))
    except httpx.RequestError as exc:
        payload = _unreachable_payload(exc)
    raise typer.Exit(code=render(payload))
