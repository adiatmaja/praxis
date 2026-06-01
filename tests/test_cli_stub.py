"""CLI entrypoint resolution tests."""
# ruff: noqa: S101

from __future__ import annotations

import tomllib
from pathlib import Path

from typer import Typer

from cli.main import app


def test_cli_entrypoint_resolves_app() -> None:
    assert isinstance(app, Typer)


def test_cli_package_is_discovered_from_src() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["tool"]["setuptools"]["packages"]["find"]["where"] == ["src"]
    assert Path("src/cli/__init__.py").is_file()
    assert Path("src/cli/main.py").is_file()
