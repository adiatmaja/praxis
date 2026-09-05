"""`praxis logs <task-id>` must print what the CLI already had and refused to.

Run #5's ranked defect 5. The orchestrator removes an agent container seconds
after it reports, so `docker logs` is already too late by the time an operator
knows they want it. The output IS captured, onto the run row, and was reachable
only by curling `GET /api/tasks/{id}` and reading `runs[].logs` out of the JSON
by hand. Three walkthroughs in a row lost a diagnosis to this.
"""

from __future__ import annotations

import httpx
import pytest
from typer.testing import CliRunner

from cli import main as cli_main
from cli.main import app
from tests.cli_text import flat


runner = CliRunner()

FULL_TASK_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the .env fallback out of these tests, and its cache cold."""
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "t")
    cli_main._env_file_values.cache_clear()


def _patch_client(monkeypatch, payload: dict, status_code: int = 200) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    def _fake_client(timeout: float = cli_main._DEFAULT_TIMEOUT) -> httpx.Client:
        return httpx.Client(
            base_url="http://x",
            headers={"Authorization": "Bearer t"},
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("cli.main._client", _fake_client)


def _run(run_id: str, logs: str, status: str = "failed") -> dict:
    return {
        "id": run_id,
        "task_id": FULL_TASK_ID,
        "container_id": "c1",
        "status": status,
        "logs": logs,
        "started_at": "2026-08-21T10:00:00+00:00",
        "finished_at": "2026-08-21T10:05:00+00:00",
    }


def test_the_latest_run_log_is_printed(monkeypatch) -> None:
    """The blocker, in one assertion."""
    _patch_client(
        monkeypatch,
        {
            "task": {"status": "failed"},
            "runs": [_run("r1", "first attempt"), _run("r2", "second attempt")],
        },
    )

    result = runner.invoke(app, ["logs", FULL_TASK_ID])

    assert result.exit_code == 0
    assert "second attempt" in result.stdout
    assert "first attempt" not in result.stdout


def test_all_prints_every_attempt_oldest_first(monkeypatch) -> None:
    """How you tell three identical failures from three different ones."""
    _patch_client(
        monkeypatch,
        {
            "task": {"status": "failed"},
            "runs": [_run("r1", "first attempt"), _run("r2", "second attempt")],
        },
    )

    result = runner.invoke(app, ["logs", FULL_TASK_ID, "--all"])

    assert result.exit_code == 0
    assert result.stdout.index("first attempt") < result.stdout.index("second attempt")


def test_bracketed_log_text_survives_verbatim(monkeypatch) -> None:
    """Worker transcripts are full of `[...]`, which rich reads as markup.

    Delete `markup=False` and rich swallows these tokens, or raises on an
    unclosed one. A log viewer that eats part of the log is worse than none,
    because the operator cannot tell it happened.
    """
    noisy = "[PRAXIS PHASE] understanding\n[main] INFO ready\n[unclosed tag"
    _patch_client(
        monkeypatch, {"task": {"status": "failed"}, "runs": [_run("r1", noisy)]}
    )

    result = runner.invoke(app, ["logs", FULL_TASK_ID])

    assert result.exit_code == 0
    assert "[PRAXIS PHASE] understanding" in result.stdout
    assert "[main] INFO ready" in result.stdout
    assert "[unclosed tag" in result.stdout


def test_a_long_log_is_tailed_and_says_how_much_it_hid(monkeypatch) -> None:
    """A silently truncated log is how you conclude the worker said nothing."""
    body = "\n".join(f"line-{i:04d}" for i in range(1, 501))
    _patch_client(
        monkeypatch, {"task": {"status": "failed"}, "runs": [_run("r1", body)]}
    )

    result = runner.invoke(app, ["logs", FULL_TASK_ID])

    assert result.exit_code == 0
    # Through `flat`: rich highlights the digits inside "line-0500" and inside
    # the suppression count when the stream takes colour, so a raw `in
    # result.stdout` passes uncoloured and fails under FORCE_COLOR.
    plain_out = flat(result)
    assert "line-0500" in plain_out
    assert "line-0001" not in plain_out
    assert "300 earlier line(s) suppressed" in plain_out


def test_tail_zero_prints_everything(monkeypatch) -> None:
    body = "\n".join(f"line-{i:04d}" for i in range(1, 501))
    _patch_client(
        monkeypatch, {"task": {"status": "failed"}, "runs": [_run("r1", body)]}
    )

    result = runner.invoke(app, ["logs", FULL_TASK_ID, "--tail", "0"])

    assert result.exit_code == 0
    assert "line-0001" in result.stdout
    assert "suppressed" not in result.stdout


def test_an_empty_log_is_reported_not_printed_as_silence(monkeypatch) -> None:
    """ "We did not capture it" and "the worker said nothing" are different facts.

    The second is alarming and the first is not, and printing nothing at all
    makes an operator conclude the second. Same lesson as `report_status=none`
    in the entrypoint diagnostic.
    """
    _patch_client(monkeypatch, {"task": {"status": "failed"}, "runs": [_run("r1", "")]})

    result = runner.invoke(app, ["logs", FULL_TASK_ID])

    assert result.exit_code == 0
    assert "No log captured" in result.stdout


def test_a_task_that_never_dispatched_says_so(monkeypatch) -> None:
    """No runs is not an error, and must not print an empty screen."""
    _patch_client(monkeypatch, {"task": {"status": "pending"}, "runs": []})

    result = runner.invoke(app, ["logs", FULL_TASK_ID])

    assert result.exit_code == 0
    assert "No agent runs" in result.stdout
    assert "pending" in result.stdout


def test_an_unknown_task_surfaces_the_api_error(monkeypatch) -> None:
    _patch_client(monkeypatch, {"detail": "Task not found"}, status_code=404)

    result = runner.invoke(app, ["logs", FULL_TASK_ID])

    assert result.exit_code == 1
    assert "404" in result.stdout


def test_the_run_header_says_the_status_is_the_harness_report(monkeypatch) -> None:
    """``agent_runs.status`` is the word the HARNESS sent, not a verdict.

    A worker killed from outside once reported ``completed`` (round 12, probe
    2) while the orchestrator failed the task from the shape of its callback,
    and the bare header read as if Praxis had judged the run complete. The
    verdict lives on the task; the header must say whose word this is.
    """
    _patch_client(
        monkeypatch,
        {"task": {"status": "failed"}, "runs": [_run("r1", "x", status="completed")]},
    )

    result = runner.invoke(app, ["logs", FULL_TASK_ID])

    assert result.exit_code == 0
    assert "harness reported completed" in result.stdout
