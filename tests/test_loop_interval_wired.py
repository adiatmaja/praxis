"""Wiring test: the configured loop_interval must reach run_loop.

Defect: ``config/praxis.yaml`` documents ``loop_interval: 30`` and
``Settings.loop_interval`` defaults to 30, but ``main.py``'s lifespan started
the orchestration loop with no ``interval_seconds`` argument, so every
install silently ran at ``run_loop``'s own hardcoded default instead of the
documented, settable value. Both ends existed and were individually correct;
the wiring between them was dead, which is exactly the class of bug a test on
either end alone would miss. These tests go through the real FastAPI
``lifespan`` so they fail if main.py stops passing the value through, not
merely if ``run_loop`` itself stops accepting it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from orchestrator.config import Settings
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.main import app


@pytest.mark.integration
async def test_env_loop_interval_reaches_run_loop(
    test_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LOOP_INTERVAL env var must be the value run_loop actually receives."""
    seen: dict[str, float] = {}

    async def fake_run_loop(
        self: Orchestrator, stop_event: asyncio.Event, interval_seconds: float = 5.0
    ) -> None:
        seen["interval"] = interval_seconds

    monkeypatch.setenv("LOOP_INTERVAL", "17")
    monkeypatch.setattr(Orchestrator, "run_loop", fake_run_loop)

    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)

    assert seen.get("interval") == 17


@pytest.mark.integration
async def test_yaml_loop_interval_reaches_run_loop(
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """PRAXIS_LOOP_INTERVAL from the mounted YAML must also reach run_loop.

    Precedence in this project is env > config/praxis.yaml > field default.
    This pins the YAML leg specifically: a fix that only threads an env var
    through main.py, while ignoring what Settings() actually resolved, would
    still pass an env-only test but fail this one.
    """
    seen: dict[str, float] = {}

    async def fake_run_loop(
        self: Orchestrator, stop_event: asyncio.Event, interval_seconds: float = 5.0
    ) -> None:
        seen["interval"] = interval_seconds

    yaml_path = tmp_path / "praxis.yaml"
    yaml_path.write_text("loop_interval: 42\n", encoding="utf-8")
    monkeypatch.setenv("PRAXIS_CONFIG_PATH", str(yaml_path))
    monkeypatch.delenv("LOOP_INTERVAL", raising=False)
    monkeypatch.setattr(Orchestrator, "run_loop", fake_run_loop)

    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)

    assert seen.get("interval") == 42


def test_nonpositive_interval_is_refused_not_busy_spun() -> None:
    """A configured 0 or negative interval must not turn into a busy-spin.

    Isolated from asyncio/Orchestrator construction on purpose: the clamp is
    a pure decision (what value does the loop actually wait on), so it is
    tested as one directly rather than through a live run_loop iteration.
    """
    from orchestrator.core.orchestrator import _clamp_loop_interval

    assert _clamp_loop_interval(30.0) == 30.0
    assert _clamp_loop_interval(0) > 0
    assert _clamp_loop_interval(-5) > 0
