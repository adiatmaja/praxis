"""Tests for the build-info stamp."""

from __future__ import annotations

import pytest

from orchestrator.core import build_info


def test_build_stamp_has_expected_keys() -> None:
    stamp = build_info.build_stamp()
    assert set(stamp) == {"commit", "started_at"}
    assert isinstance(stamp["commit"], str) and stamp["commit"]  # noqa: PT018
    assert isinstance(stamp["started_at"], str) and stamp["started_at"]  # noqa: PT018


def test_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRAXIS_BUILD_SHA", "deadbeef")
    assert build_info._resolve_commit() == "deadbeef"


def test_fallback_to_unknown_when_no_env_and_no_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When PRAXIS_BUILD_SHA is unset and git is unavailable, return 'unknown'."""
    monkeypatch.delenv("PRAXIS_BUILD_SHA", raising=False)
    # Simulate git being absent / failing by patching subprocess.run to raise OSError.
    monkeypatch.setattr(
        build_info.subprocess,
        "run",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("git not found")),
    )
    assert build_info._resolve_commit() == "unknown"
