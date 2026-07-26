"""Tests for the `praxis config` CLI sub-app."""

from __future__ import annotations

import httpx
from typer.testing import CliRunner

from cli.main import app


runner = CliRunner()


def _mock_transport(handler):
    return httpx.MockTransport(handler)


def test_config_show_renders_registry(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/settings/registry":
            return httpx.Response(
                200,
                json=[
                    {
                        "name": "sonnet",
                        "provider": "claude",
                        "model": "claude-sonnet-4-6",
                        "effort": None,
                    }
                ],
            )
        if request.url.path == "/api/settings/roles":
            return httpx.Response(200, json={"plan": ["sonnet"]})
        if request.url.path == "/api/settings/capabilities":
            return httpx.Response(200, json={"as_of": "2026-07-17", "models": {}})
        return httpx.Response(404)

    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "t")
    monkeypatch.setattr(
        "cli.main._client",
        lambda: httpx.Client(
            base_url="http://x",
            headers={"Authorization": "Bearer t"},
            transport=_mock_transport(handler),
        ),
    )
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0
    assert "sonnet" in result.stdout


def test_set_role_parses_csv(monkeypatch) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/settings/roles":
            if request.method == "GET":
                return httpx.Response(200, json={"plan": ["sonnet"]})
            if request.method == "PUT":
                import json as _j

                captured.update(_j.loads(request.content))
                return httpx.Response(200, json=captured["chains"])
        return httpx.Response(404)

    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "t")
    monkeypatch.setattr(
        "cli.main._client",
        lambda: httpx.Client(
            base_url="http://x",
            headers={"Authorization": "Bearer t"},
            transport=_mock_transport(handler),
        ),
    )
    result = runner.invoke(app, ["config", "set-role", "plan", "sonnet,opus"])
    assert result.exit_code == 0
    assert captured["chains"] == {"plan": ["sonnet", "opus"]}
