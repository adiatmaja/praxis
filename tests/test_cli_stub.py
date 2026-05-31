"""CLI entrypoint resolution tests."""
# ruff: noqa: S101

from __future__ import annotations

from typer import Typer

from cli.main import app


def test_cli_entrypoint_resolves_app() -> None:
    assert isinstance(app, Typer)
