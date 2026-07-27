"""Unit tests for auto-delegate toggle resolvers in EffectiveSettings."""

# ruff: noqa: S101

from __future__ import annotations

import pytest

from orchestrator.config import Settings
from orchestrator.core.effective_settings import EffectiveSettings
from orchestrator.database import Database


@pytest.mark.unit
async def test_disabled_by_default(db: Database, test_settings: Settings) -> None:
    es = EffectiveSettings(test_settings, db)
    assert await es.auto_delegate_enabled() is False


@pytest.mark.unit
async def test_enable_and_read(db: Database, test_settings: Settings) -> None:
    es = EffectiveSettings(test_settings, db)
    await es.set_override("auto_delegate.enabled", "true")
    assert await es.auto_delegate_enabled() is True

    await es.set_override("auto_delegate.enabled", None)
    assert await es.auto_delegate_enabled() is False


@pytest.mark.unit
def test_worker_reflects_default(db: Database, test_settings: Settings) -> None:
    es = EffectiveSettings(test_settings, db)
    assert es.auto_delegate_worker() == {
        "harness": test_settings.default_worker_harness,
        "model": test_settings.default_worker_model,
    }
