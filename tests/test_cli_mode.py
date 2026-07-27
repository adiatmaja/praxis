"""Tests for `praxis mode` CLI command."""

# ruff: noqa: S101

from __future__ import annotations

from typer.testing import CliRunner

from cli.main import app


runner = CliRunner()


def test_mode_status(monkeypatch) -> None:
    captured: dict = {}

    class FakeResp:
        status_code = 200
        text = ""

        def json(self):
            return {
                "enabled": True,
                "worker": {
                    "harness": "agy",
                    "model": "Gemini 3.6 Flash (High)",
                },
            }

        def raise_for_status(self):
            pass

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url: str, **kwargs):
            captured["get"] = url
            return FakeResp()

        def put(self, url: str, **kwargs):
            captured["put"] = (url, kwargs.get("json"))
            return FakeResp()

    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "test-token")
    monkeypatch.setattr("cli.main.httpx.Client", FakeClient)

    result = runner.invoke(app, ["mode", "status"])
    assert result.exit_code == 0
    assert "auto-delegate: ON" in result.stdout
    assert "agy" in result.stdout
    assert captured.get("get", "").endswith("/api/settings/auto-delegate")


def test_mode_on(monkeypatch) -> None:
    captured: dict = {}

    class FakeResp:
        status_code = 200
        text = ""

        def json(self):
            return {
                "enabled": True,
                "worker": {
                    "harness": "agy",
                    "model": "Gemini 3.6 Flash (High)",
                },
            }

        def raise_for_status(self):
            pass

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url: str, **kwargs):
            captured["get"] = url
            return FakeResp()

        def put(self, url: str, **kwargs):
            captured["put"] = (url, kwargs.get("json"))
            return FakeResp()

    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "test-token")
    monkeypatch.setattr("cli.main.httpx.Client", FakeClient)

    result = runner.invoke(app, ["mode", "on"])
    assert result.exit_code == 0
    assert "auto-delegate: ON" in result.stdout
    assert captured["put"][0].endswith("/api/settings/auto-delegate")
    assert captured["put"][1] == {"enabled": True}


def test_mode_off(monkeypatch) -> None:
    captured: dict = {}

    class FakeResp:
        status_code = 200
        text = ""

        def json(self):
            return {
                "enabled": False,
                "worker": {
                    "harness": "claude",
                    "model": None,
                },
            }

        def raise_for_status(self):
            pass

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url: str, **kwargs):
            captured["get"] = url
            return FakeResp()

        def put(self, url: str, **kwargs):
            captured["put"] = (url, kwargs.get("json"))
            return FakeResp()

    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "test-token")
    monkeypatch.setattr("cli.main.httpx.Client", FakeClient)

    result = runner.invoke(app, ["mode", "off"])
    assert result.exit_code == 0
    assert "auto-delegate: OFF" in result.stdout
    assert "unset" in result.stdout
    assert captured["put"][0].endswith("/api/settings/auto-delegate")
    assert captured["put"][1] == {"enabled": False}


def test_mode_invalid_action() -> None:
    result = runner.invoke(app, ["mode", "invalid"])
    assert result.exit_code != 0
    assert "action must be one of: on, off, status" in result.stdout
