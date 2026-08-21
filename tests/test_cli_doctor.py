import httpx
import pytest
from typer.testing import CliRunner

from cli.doctor import render
from cli.main import app


runner = CliRunner()


@pytest.mark.unit
def test_all_green_exits_zero():
    payload = {
        "status": "green",
        "checks": [
            {
                "check_id": "docker_daemon",
                "label": "Docker",
                "status": "green",
                "detail": "ok",
                "hint": "",
            }
        ],
    }
    assert render(payload) == 0


@pytest.mark.unit
def test_any_red_exits_non_zero():
    payload = {
        "status": "red",
        "checks": [
            {
                "check_id": "docker_daemon",
                "label": "Docker",
                "status": "red",
                "detail": "not reachable",
                "hint": "start Docker Desktop",
            }
        ],
    }
    assert render(payload) == 1


def _amber_payload(hint: str = "") -> dict:
    """One amber row and nothing else."""
    return {
        "status": "amber",
        "checks": [
            {
                "check_id": "git_credential",
                "label": "Git credential",
                "status": "amber",
                "detail": "local mode",
                "hint": hint,
            }
        ],
    }


@pytest.mark.unit
def test_amber_alone_exits_zero():
    """Local mode has no GitHub credential; that is not a failure.

    The exit code is deliberately unchanged by the summary fix below: amber
    means "nothing here is known to be broken", and `praxis init` ends by
    running this, so failing on it would make every unprobeable planner a hard
    install error.
    """
    assert render(_amber_payload()) == 0


@pytest.mark.unit
def test_amber_is_not_summarized_as_all_checks_passed(capsys):
    """The claim, not the exit code, was the false one.

    `render` partitioned on RED alone, so a table whose rows read "not
    checked: the Docker daemon did not answer" and "no test prompt was made"
    ended with "All checks passed." This is the last line `praxis init`
    prints, which is where a newcomer has least reason to doubt it.
    """
    render(_amber_payload())
    out = " ".join(capsys.readouterr().out.split())

    assert "All checks passed" not in out
    assert "could not be verified" in out


@pytest.mark.unit
def test_an_amber_hint_is_printed(capsys):
    """An amber's hint is written to be read, and only reds got printed.

    `probe_planner_cli` composes hints like "nothing to fix if that is
    deliberate" precisely for these rows, so the rows that most needed
    explaining were the silent ones.
    """
    render(_amber_payload(hint="nothing to fix if that is deliberate"))
    out = " ".join(capsys.readouterr().out.split())

    assert "nothing to fix if that is deliberate" in out


@pytest.mark.unit
def test_a_wholly_green_table_still_says_so(capsys):
    """The other branch, so the fix cannot swallow the honest all-clear."""
    payload = {
        "status": "green",
        "checks": [
            {
                "check_id": "docker_daemon",
                "label": "Docker",
                "status": "green",
                "detail": "reachable",
                "hint": "",
            }
        ],
    }

    assert render(payload) == 0
    assert "All checks passed" in capsys.readouterr().out


@pytest.mark.unit
def test_the_hint_is_printed_for_each_red(capsys):
    payload = {
        "status": "red",
        "checks": [
            {
                "check_id": "docker_daemon",
                "label": "Docker",
                "status": "red",
                "detail": "not reachable",
                "hint": "start Docker Desktop",
            }
        ],
    }
    render(payload)
    assert "start Docker Desktop" in capsys.readouterr().out


@pytest.mark.unit
def test_doctor_renders_a_table_when_the_api_is_unreachable(monkeypatch):
    """`praxis doctor` must diagnose a DOWN server, not crash on it.

    docker_daemon and orchestrator_health are exactly the checks an operator
    needs when the server does not answer at all, so a connection failure
    must still produce a table (with orchestrator_health red, carrying its
    registry hint) and a non-zero exit, never a raw traceback.
    """

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url: str, **kwargs):
            message = "connection refused"
            raise httpx.ConnectError(message)

    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "test-token")
    monkeypatch.setattr("cli.main.httpx.Client", FakeClient)

    result = runner.invoke(app, ["doctor"])

    # A normal `typer.Exit(code=1)` surfaces as SystemExit(1) here; only an
    # UNCAUGHT ConnectError (the mutation this test exists to catch) would
    # surface as anything else, and it would also skip printing the table
    # the assertions below check for.
    assert isinstance(result.exception, SystemExit)
    assert result.exit_code == 1
    assert "orchestrator" in result.stdout.lower()
    # The orchestrator_health check's own registry hint, not a fabricated one.
    assert "docker compose up" in result.stdout


def _client_returning(status: int, body: str):
    """A `cli.main.httpx.Client` stand-in whose GET answers a fixed status."""

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url: str, **kwargs):
            return httpx.Response(status, text=body)

    return FakeClient


@pytest.mark.unit
def test_doctor_renders_a_table_on_a_401(monkeypatch):
    """The bypass that mattered most: doctor's own auth check.

    `/api/doctor` is auth-gated, so a wrong AUTH_TOKEN, one of doctor's twelve
    checks, is the likeliest way to hit an error status. Routed through
    `cli.main._check_dict` it printed `Error 401: Unauthorized` and exited
    before `render()` ever ran, so the operator got no table and no hint for
    the very check that explains the failure.
    """
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "wrong-token")
    monkeypatch.setattr("cli.main.httpx.Client", _client_returning(401, "Unauthorized"))

    result = runner.invoke(app, ["doctor"])

    assert isinstance(result.exception, SystemExit)
    assert result.exit_code == 1
    assert "praxis doctor" in result.stdout  # the table itself, not a bare error
    assert "401" in result.stdout
    # The auth_token check's own registry hint.
    assert "check AUTH_TOKEN" in result.stdout


@pytest.mark.unit
def test_doctor_renders_a_table_on_a_500(monkeypatch):
    """A server-side fault points at orchestrator_health, not at the token."""
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "test-token")
    monkeypatch.setattr(
        "cli.main.httpx.Client", _client_returning(500, "Internal Server Error")
    )

    result = runner.invoke(app, ["doctor"])

    assert isinstance(result.exception, SystemExit)
    assert result.exit_code == 1
    assert "praxis doctor" in result.stdout
    assert "500" in result.stdout
    # The orchestrator_health check's own registry hint.
    assert "docker compose up" in result.stdout
