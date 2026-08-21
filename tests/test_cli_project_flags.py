"""`add-project` and `configure` must be able to say which harness to use.

The API has accepted `harness` on both create and update since the harness
registry landed, and validates it against the registry. The CLI simply never
offered a flag, so the one setting that decides which harness does the typing
was unreachable from the command line and could only be changed by curl.

`model` is the mirror image: the API has always allowed it to be null and fall
back to the worker preset, while the CLI demanded it positionally. Under the
shipped default preset (`gemini-agy`) the correct value lives in
config/praxis.yaml and is printed by no command, so the newcomer was asked for
a value they had no way to look up.
"""
# ruff: noqa: S101

from __future__ import annotations

from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from cli.main import app


runner = CliRunner()


def _capture(monkeypatch, response: dict[str, Any]) -> list[dict[str, Any]]:
    """Record the JSON body the CLI actually sends."""
    sent: list[dict[str, Any]] = []
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "t")
    monkeypatch.setenv("COLUMNS", "160")

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        sent.append(json.loads(request.content or b"{}"))
        return httpx.Response(200, json=response)

    def _fake_client(timeout: float = 60.0) -> httpx.Client:
        return httpx.Client(
            base_url="http://x",
            headers={"Authorization": "Bearer t"},
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("cli.main._client", _fake_client)
    return sent


@pytest.mark.unit
def test_add_project_forwards_the_harness(monkeypatch) -> None:
    sent = _capture(monkeypatch, {"id": "p1"})
    result = runner.invoke(
        app, ["add-project", "pg", "https://github.com/o/r", "--harness", "agy"]
    )
    assert result.exit_code == 0
    assert sent[0]["harness"] == "agy"


@pytest.mark.unit
def test_add_project_omits_an_unspecified_model_rather_than_nulling_it(
    monkeypatch,
) -> None:
    """An absent key and an explicit null are not the same thing.

    Sending `model_name: null` writes an explicit null onto the project row and
    stops it tracking the preset; omitting the key leaves the server's own
    default in charge. Deleting the `is not None` guard makes only this red.
    """
    sent = _capture(monkeypatch, {"id": "p1"})
    result = runner.invoke(app, ["add-project", "pg", "https://github.com/o/r"])
    assert result.exit_code == 0
    assert "model_name" not in sent[0]
    assert "harness" not in sent[0]


@pytest.mark.unit
def test_add_project_still_accepts_a_positional_model(monkeypatch) -> None:
    """Making the argument optional must not break the documented invocation."""
    sent = _capture(monkeypatch, {"id": "p1"})
    result = runner.invoke(
        app, ["add-project", "pg", "https://github.com/o/r", "qwen3.8-27b"]
    )
    assert result.exit_code == 0
    assert sent[0]["model_name"] == "qwen3.8-27b"


@pytest.mark.unit
def test_configure_forwards_the_harness(monkeypatch) -> None:
    sent = _capture(monkeypatch, {"name": "pg"})
    result = runner.invoke(app, ["configure", "p1", "--harness", "opencode"])
    assert result.exit_code == 0
    assert sent[0]["harness"] == "opencode"


@pytest.mark.unit
def test_configure_with_only_a_harness_is_not_an_empty_update(monkeypatch) -> None:
    """`configure` short-circuits on an empty body; harness must populate it.

    Forgetting to add `harness` to the body dict would leave the flag parsed,
    accepted, and silently discarded with "No settings to update", which is the
    quietest possible way for a fix to be inert.
    """
    sent = _capture(monkeypatch, {"name": "pg"})
    result = runner.invoke(app, ["configure", "p1", "--harness", "agy"])
    assert result.exit_code == 0
    assert sent, "no request was sent; the harness flag was discarded"
    assert "No settings to update" not in result.stdout
