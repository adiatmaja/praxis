"""The concurrent-agent cap is an operator setting, not a constructor default.

Probe 9b/10 (2026-09-05): a 19-leaf plan with 18 independent leaves fanned out
to exactly three workers, and the fourth was refused with "Concurrent agent cap
reached (3 of 3 running)". Correct, and unreachable by an operator: the 3 lived
only in ``AgentManager``'s constructor default, so a two-worker laptop could
not lower it and a large box could not raise it. A cap nobody can set is a
cap only its author knows about.
"""
# ruff: noqa: S101

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator import main as main_mod
from orchestrator.config import Settings
from orchestrator.main import app


def test_the_cap_defaults_to_three_and_is_an_int(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MAX_AGENT_CONCURRENCY", raising=False)
    monkeypatch.setenv("AUTH_TOKEN", "t")
    assert Settings(_env_file=None).max_agent_concurrency == 3


def test_the_cap_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_AGENT_CONCURRENCY", "7")
    monkeypatch.setenv("AUTH_TOKEN", "t")
    assert Settings(_env_file=None).max_agent_concurrency == 7


def test_the_lifespan_passes_the_cap_to_the_agent_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pinned on the EMISSION: the kwarg the lifespan hands the manager. A
    setting nothing passes is decoration (``callback_grace`` was, for months)."""
    seen: dict[str, Any] = {}
    real = main_mod.AgentManager

    class Spy(real):  # type: ignore[misc,valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            seen.update(kwargs)
            raise RuntimeError("spy: not building a real manager")

    monkeypatch.setattr(main_mod, "AgentManager", Spy)
    monkeypatch.setenv("AUTH_TOKEN", "t")
    monkeypatch.setenv("GITHUB_TOKEN", "placeholder")
    monkeypatch.setenv("MAX_AGENT_CONCURRENCY", "5")
    with TestClient(app):
        pass
    assert seen.get("max_agent_concurrency") == 5
