"""`praxis doctor`: one table, green and red, one fix hint per red."""

from __future__ import annotations

from typing import Any

import httpx
import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text


console = Console()

_SYMBOL = {
    "green": "[green]OK[/green]",
    "amber": "[yellow]NOTE[/yellow]",
    "red": "[red]FAIL[/red]",
}


def render(payload: dict) -> int:
    """Print the doctor table and return the intended process exit code.

    AMBER is a real and common verdict here, not a rounding error: a planner
    nothing round-tripped, an image nothing compared, a `.env` that could not
    be read. This function partitioned on RED alone and then printed "All
    checks passed", so a table full of "not checked" rows was summarized as a
    clean bill of health. It is the last line `praxis init` prints, which puts
    the claim exactly where a newcomer has least reason to doubt it.

    The exit code is unchanged, and deliberately: amber means "nothing here is
    known to be broken", so failing a scripted install on it would make every
    unprobeable planner a hard error. What changes is the sentence.

    Every string that came from the SERVER is wrapped in ``rich.text.Text``,
    which renders literally. Only the symbols this function writes itself keep
    console markup. A doctor detail can carry raw third-party output verbatim
    (the agy row prints what `agy models` said), and rich reads a bare "[" as
    the start of a tag: a detail containing "[/red]" raises MarkupError out of
    this function, and `doctor()` catches only httpx.RequestError, so the
    operator got a traceback and NO TABLE AT ALL -- from the one command whose
    stated contract is that it always answers. A detail containing "[bold]"
    was silently deleted instead, which is worse than the crash. `cli/init.py`
    already passes markup=False for config-authored text for exactly this
    reason. This is also what keeps the "[truncated at N characters]" marker
    visible; rich was eating it, making the truncation silent.
    """
    table = Table(title="praxis doctor")
    for column in ("", "Check", "Detail"):
        table.add_column(column)
    for check in payload["checks"]:
        table.add_row(
            _SYMBOL.get(check["status"], check["status"]),
            Text(check.get("label") or check["check_id"]),
            Text(check["detail"]),
        )
    console.print(table)

    reds = [c for c in payload["checks"] if c["status"] == "red"]
    ambers = [c for c in payload["checks"] if c["status"] == "amber"]
    for check in reds:
        console.print(
            "[red]FAIL[/red]",
            Text(f"{check.get('label') or check['check_id']}: {check['hint']}"),
        )
    # An amber's hint is written to be read ("nothing to fix if that is
    # deliberate", "re-run doctor once the daemon is up"), and printing it only
    # for reds meant the rows that most needed explaining were the silent ones.
    for check in ambers:
        if check.get("hint"):
            console.print(
                "[yellow]NOTE[/yellow]",
                Text(f"{check.get('label') or check['check_id']}: {check['hint']}"),
            )
    if reds:
        console.print(f"\n[red]{len(reds)} check(s) failed.[/red]")
        return 1
    if ambers:
        console.print(
            f"\n[yellow]No failures, but {len(ambers)} check(s) could not be "
            "verified.[/yellow] Nothing above is known to be broken, and "
            "nothing above confirms it works either."
        )
        return 0
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
