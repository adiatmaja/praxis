"""The CLI must be able to find the install the operator is standing in.

Run #5's second half: a clean shell in the repo root, with a running
orchestrator and ``AUTH_TOKEN`` right there in ``.env``, died on
``Set AUTH_TOKEN (or ORCHESTRATOR_TOKEN) env var``. Neither name appears
anywhere in ``README.md`` or ``docs/deployment.md``, so the only recovery was
re-running the whole setup wizard.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli import main as cli_main
from cli.main import app


runner = CliRunner()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from an empty environment and a cold cache.

    The `.env` read is cached for the process, so a leaked entry from a
    previous test would make the next one pass for the wrong reason.
    """
    for name in ("AUTH_TOKEN", "ORCHESTRATOR_TOKEN", "ORCHESTRATOR_URL"):
        monkeypatch.delenv(name, raising=False)
    cli_main._env_file_values.cache_clear()
    yield
    cli_main._env_file_values.cache_clear()


def _install(tmp_path: Path, body: str) -> Path:
    (tmp_path / ".env").write_text(body, encoding="utf-8")
    return tmp_path


def test_the_token_is_read_from_the_env_file_in_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The blocker, in one assertion."""
    monkeypatch.chdir(_install(tmp_path, "AUTH_TOKEN=local-dev-token-praxis\n"))

    assert cli_main._auth_token() == "local-dev-token-praxis"


def test_the_token_is_found_from_a_subdirectory_of_the_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running from `praxis/src` must resolve the same install as `praxis/`."""
    _install(tmp_path, "AUTH_TOKEN=t-from-parent\n")
    nested = tmp_path / "src" / "orchestrator"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    assert cli_main._auth_token() == "t-from-parent"


def test_an_explicit_environment_variable_still_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback is a rescue, never an override.

    Delete this precedence and an operator pointing the CLI at a remote
    deployment gets silently redirected at whatever `.env` they happen to be
    standing next to.
    """
    monkeypatch.chdir(_install(tmp_path, "AUTH_TOKEN=from-file\n"))
    monkeypatch.setenv("AUTH_TOKEN", "from-environment")

    assert cli_main._auth_token() == "from-environment"


def test_the_url_uses_the_port_from_the_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_install(tmp_path, "AUTH_TOKEN=t\nPORT=9999\n"))

    assert cli_main._api_url() == "http://localhost:9999"


def test_the_url_falls_back_to_the_compose_mapped_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not 8080. That is the bare-uvicorn port, not where an install answers."""
    monkeypatch.chdir(_install(tmp_path, "AUTH_TOKEN=t\n"))

    assert cli_main._api_url() == "http://localhost:12323"


def test_a_non_numeric_port_is_ignored_rather_than_pasted_into_a_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed PORT must degrade to the default, not build a broken URL."""
    monkeypatch.chdir(_install(tmp_path, "AUTH_TOKEN=t\nPORT=not-a-port\n"))

    assert cli_main._api_url() == "http://localhost:12323"


def test_an_env_file_with_no_token_still_reports_the_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding a file is not finding a token; the message must name both."""
    monkeypatch.chdir(_install(tmp_path, "PORT=12323\n"))

    result = runner.invoke(app, ["env"])

    assert result.exit_code == 1
    assert "not found" in result.stdout


def test_env_names_the_file_it_resolved_the_token_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A silently-adopted wrong install is worse than no install found."""
    monkeypatch.chdir(_install(tmp_path, "AUTH_TOKEN=t-abc\nPORT=12323\n"))

    result = runner.invoke(app, ["env"])

    assert result.exit_code == 0
    assert ".env" in result.stdout
    assert "http://localhost:12323" in result.stdout
    assert "t-abc" in result.stdout


def test_env_reports_no_env_file_without_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["env"])

    assert result.exit_code == 1
    assert "No .env found" in result.stdout


def test_an_unreadable_env_file_is_not_a_new_way_to_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fallback rescues an empty environment; it must never become a fault."""
    monkeypatch.chdir(_install(tmp_path, "AUTH_TOKEN=t\n"))
    monkeypatch.setattr(
        Path, "read_text", lambda *_a, **_k: (_ for _ in ()).throw(OSError("nope"))
    )

    result = runner.invoke(app, ["env"])

    assert result.exit_code == 1
    assert "not found" in result.stdout
