"""`praxis env`'s export block exists to be pasted, so it must not fold.

``_copyable``'s docstring already spends thirty lines on this: rich's default
wrapping inserts a REAL newline at the console width, breaking on whitespace,
so a command wider than the terminal arrives as two lines and selecting either
one yields half a command. Every other copyable line in the CLI goes through
that helper. The three lines under "To make this explicit in another shell"
did not: they passed ``highlight=False`` and nothing else, in the one block
whose entire stated job is to be copied into a different shell.

Soft wrapping is what makes the difference visible here: it emits the line
whole and lets the TERMINAL wrap it for display, so the captured stdout holds
one logical line. That is exactly what ``on_one_line`` measures.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli import main as cli_main
from cli.main import app
from tests.cli_text import on_one_line


runner = CliRunner()

#: Long enough that EACH of the three lines exceeds an 80-column console, which
#: is the width this project's own suite runs at. Sized deliberately: with a
#: default `http://localhost:12323` and a 43-character `praxis init` token the
#: two `export` lines happen to fit, so a guard built on those values would go
#: green on two of the three lines whatever the code did. A hosted install
#: behind a real hostname is the case that folds them.
TOKEN = "Zx7q" + "k" * 60
URL = "https://praxis.orchestrator.internal.example.com:12323"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Empty environment, cold cache, and off the repository's own `.env`."""
    for name in ("AUTH_TOKEN", "ORCHESTRATOR_TOKEN", "ORCHESTRATOR_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    cli_main._env_file_values.cache_clear()
    yield
    cli_main._env_file_values.cache_clear()


def _run(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ORCHESTRATOR_URL", URL)
    monkeypatch.setenv("AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("COLUMNS", "80")
    return runner.invoke(app, ["env"])


@pytest.mark.unit
def test_the_bash_url_export_survives_on_one_line(monkeypatch) -> None:
    result = _run(monkeypatch)

    assert result.exit_code == 0
    assert on_one_line(result, f"export ORCHESTRATOR_URL={URL}")


@pytest.mark.unit
def test_the_bash_token_export_survives_on_one_line(monkeypatch) -> None:
    result = _run(monkeypatch)

    assert result.exit_code == 0
    assert on_one_line(result, f"export ORCHESTRATOR_TOKEN={TOKEN}")


@pytest.mark.unit
def test_the_powershell_line_survives_on_one_line(monkeypatch) -> None:
    """Measured live: this one folded, so the row gave half a command.

    It is the longest of the three (two assignments joined by `; `), so it is
    the one an operator on this project's own platform actually copies.
    """
    result = _run(monkeypatch)

    assert result.exit_code == 0
    assert on_one_line(
        result,
        f'$env:ORCHESTRATOR_URL="{URL}"; $env:ORCHESTRATOR_TOKEN="{TOKEN}"',
    )
