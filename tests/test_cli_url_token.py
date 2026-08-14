"""Tests for the CLI's default orchestrator URL and auth-token resolution.

`_api_url()` must default to the port the product actually documents
(12323, see README.md), not the container's internal bind port (8080).
`_auth_token()` must accept the name every other surface uses (`AUTH_TOKEN`,
set in `.env` / `.env.example` / read by the dashboard) while still honoring
`ORCHESTRATOR_TOKEN` for anyone who already configured that name (including
`praxis init`'s own printed `cli_env_exports`, see
`tests/test_cli_init.py::test_the_printed_cli_exports_are_what_the_cli_actually_reads`).
"""

from __future__ import annotations

import pytest

from cli import main as cli_main


@pytest.mark.unit
def test_api_url_defaults_to_the_documented_port(monkeypatch) -> None:
    """With no ORCHESTRATOR_URL set, the CLI must point at 12323, the port
    README.md documents (http://localhost:12323), not the container's
    internal bind port 8080."""
    monkeypatch.delenv("ORCHESTRATOR_URL", raising=False)
    assert cli_main._api_url() == "http://localhost:12323"


@pytest.mark.unit
def test_api_url_still_honors_explicit_orchestrator_url(monkeypatch) -> None:
    """An explicit ORCHESTRATOR_URL always wins over the default."""
    monkeypatch.setenv("ORCHESTRATOR_URL", "http://127.0.0.1:9999")
    assert cli_main._api_url() == "http://127.0.0.1:9999"


@pytest.mark.unit
def test_auth_token_reads_the_documented_auth_token_var(monkeypatch) -> None:
    """AUTH_TOKEN is the name .env / .env.example / the dashboard use; the
    CLI must accept it directly, not only its own ORCHESTRATOR_TOKEN name."""
    monkeypatch.delenv("ORCHESTRATOR_TOKEN", raising=False)
    monkeypatch.setenv("AUTH_TOKEN", "doc-tok")
    assert cli_main._auth_token() == "doc-tok"


@pytest.mark.unit
def test_auth_token_prefers_auth_token_over_orchestrator_token(monkeypatch) -> None:
    """When both are set, the documented AUTH_TOKEN name wins."""
    monkeypatch.setenv("AUTH_TOKEN", "preferred-tok")
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "legacy-tok")
    assert cli_main._auth_token() == "preferred-tok"


@pytest.mark.unit
def test_auth_token_falls_back_to_orchestrator_token_for_existing_users(
    monkeypatch,
) -> None:
    """Backward compatibility: a user with only ORCHESTRATOR_TOKEN set (the
    CLI's original env var name, still what `praxis init` prints via
    `cli_env_exports`) must not be broken by preferring AUTH_TOKEN."""
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "legacy-tok")
    assert cli_main._auth_token() == "legacy-tok"
