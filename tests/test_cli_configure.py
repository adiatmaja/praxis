"""`praxis configure` must reach every field the API already accepts."""

from __future__ import annotations

import json

import httpx
from typer.testing import CliRunner

from cli.main import app


runner = CliRunner()


def _patch_client(monkeypatch, handler) -> None:
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "t")
    monkeypatch.setattr(
        "cli.main._client",
        lambda: httpx.Client(
            base_url="http://x",
            headers={"Authorization": "Bearer t"},
            transport=httpx.MockTransport(handler),
        ),
    )


def test_configure_sends_verify_cmd(monkeypatch) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH":
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"name": "playground"})
        return httpx.Response(404, json={"detail": "not found"})

    _patch_client(monkeypatch, handler)
    result = runner.invoke(
        app, ["configure", "p1", "--verify-cmd", "python -m pytest -q"]
    )

    assert result.exit_code == 0
    assert captured == {"verify_cmd": "python -m pytest -q"}


def test_configure_sends_default_branch(monkeypatch) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH":
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"name": "playground"})
        return httpx.Response(404, json={"detail": "not found"})

    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["configure", "p1", "--default-branch", "develop"])

    assert result.exit_code == 0
    assert captured == {"default_branch": "develop"}


def test_configure_with_no_options_sends_nothing(monkeypatch) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json={"name": "playground"})

    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["configure", "p1"])

    assert result.exit_code == 0
    assert calls == []
    assert "No settings to update" in result.stdout
