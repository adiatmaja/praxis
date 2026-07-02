"""Tests for app lifespan startup — DocIndexer wiring."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from orchestrator.core.doc_indexer import DocIndexer
from orchestrator.main import app


@pytest.mark.unit
def test_app_has_doc_indexer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_TOKEN", "test-auth")
    monkeypatch.setenv("GITHUB_TOKEN", "test-gh")

    with TestClient(app) as client:
        assert hasattr(client.app.state, "doc_indexer")
        assert isinstance(client.app.state.doc_indexer, DocIndexer)


@pytest.mark.unit
def test_lifespan_builds_app_credential_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_credential_provider is called at startup and selects App backend."""
    import orchestrator.main as main_mod
    from orchestrator.core.github_credentials import GitHubAppCredentialProvider

    built: dict[str, str] = {}
    real = main_mod.build_credential_provider

    def spy(settings: object) -> object:
        provider = real(settings)
        built["type"] = type(provider).__name__
        return provider

    monkeypatch.setattr(main_mod, "build_credential_provider", spy)
    monkeypatch.setenv("AUTH_TOKEN", "t")
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    monkeypatch.setenv(
        "GITHUB_APP_PRIVATE_KEY",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----",
    )
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    with TestClient(app):
        pass

    assert built.get("type") == GitHubAppCredentialProvider.__name__
