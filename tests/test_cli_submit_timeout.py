"""`praxis submit` must not print a traceback on an expected slow path.

`POST /api/projects/{id}/plans` clones the repo to commit the spec doc BEFORE
it answers, which is the one request in this CLI that regularly outruns
`_DEFAULT_TIMEOUT` (60s) on a large repo. A live run got a raw
`httpx.ReadTimeout` traceback and no plan id, even though the plan had in
fact been created server-side.

`httpx.RequestError` (`ConnectTimeout`, and `ConnectError` when the
orchestrator is simply not running) is the opposite fact: the request never
reached the server at all, and must not be reported the same way as a
`ReadTimeout`.
"""

from __future__ import annotations

import httpx
import pytest
from typer.testing import CliRunner

from cli.main import app
from tests.cli_text import flat, on_one_line


runner = CliRunner()

PROJECT_ID = "b5a1b6b0-1c2b-4e2a-9a3a-9c1f6b2a7e11"


def _patch_client_raising(monkeypatch, exc: Exception) -> None:
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "t")
    monkeypatch.setenv("COLUMNS", "80")

    def handler(_request: httpx.Request) -> httpx.Response:
        raise exc

    monkeypatch.setattr(
        "cli.main._client",
        lambda: httpx.Client(
            base_url="http://x",
            headers={"Authorization": "Bearer t"},
            transport=httpx.MockTransport(handler),
        ),
    )


@pytest.mark.unit
def test_submit_read_timeout_reports_recovery_not_a_traceback(monkeypatch) -> None:
    _patch_client_raising(
        monkeypatch, httpx.ReadTimeout("timed out", request=httpx.Request("POST", "/x"))
    )

    result = runner.invoke(app, ["submit", PROJECT_ID, "a spec"])

    assert result.exit_code != 0
    # The load-bearing check: CliRunner writes NOTHING to `result.output` for
    # an exception that escapes uncaught, it only records it on
    # `result.exception`. So an assertion against `result.output` cannot tell
    # "the handler ran" from "the handler doesn't exist"; this is the one
    # assertion that can.
    assert result.exception is None or isinstance(result.exception, SystemExit)
    output = flat(result)
    assert "may have been created" in output
    assert on_one_line(result, f"praxis plans {PROJECT_ID}")


@pytest.mark.unit
def test_submit_connect_timeout_says_the_request_never_arrived(monkeypatch) -> None:
    """The opposite fact from ReadTimeout, and must not read the same."""
    _patch_client_raising(
        monkeypatch,
        httpx.ConnectTimeout("timed out", request=httpx.Request("POST", "/x")),
    )

    result = runner.invoke(app, ["submit", PROJECT_ID, "a spec"])

    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    output = flat(result)
    assert "never reached" in output
    assert "may have been created" not in output


@pytest.mark.unit
def test_submit_connect_error_says_the_request_never_arrived(monkeypatch) -> None:
    """The orchestrator simply not running: `ConnectError`, not a timeout.

    `ConnectTimeout` only fires when the TCP connect itself hangs. When
    nothing is listening at all, httpx raises `ConnectError`, a
    `RequestError` sibling that is NOT a `TimeoutException`. Before this test
    existed, that path exited 1 with empty stdout and no diagnostic - the
    same defect class this file exists to close, in the same command.
    """
    _patch_client_raising(
        monkeypatch,
        httpx.ConnectError("connection refused", request=httpx.Request("POST", "/x")),
    )

    result = runner.invoke(app, ["submit", PROJECT_ID, "a spec"])

    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    output = flat(result)
    assert output.strip(), "submit exited silently on a down orchestrator"
    assert "never reached" in output
    assert "may have been created" not in output
