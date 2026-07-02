"""Tests for the build-info stamp."""

from __future__ import annotations

from orchestrator.core import build_info


def test_build_stamp_has_expected_keys() -> None:
    stamp = build_info.build_stamp()
    assert set(stamp) == {"commit", "started_at"}
    assert isinstance(stamp["commit"], str) and stamp["commit"]  # noqa: PT018
    assert isinstance(stamp["started_at"], str) and stamp["started_at"]  # noqa: PT018


def test_env_override_wins(monkeypatch) -> None:
    monkeypatch.setenv("PRAXIS_BUILD_SHA", "deadbeef")
    assert build_info._resolve_commit() == "deadbeef"
