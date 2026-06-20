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
