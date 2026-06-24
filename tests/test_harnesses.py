"""Harness registry unit tests."""
# ruff: noqa: S101

from __future__ import annotations

import pytest

from orchestrator.core.harnesses import (
    REGISTRY,
    HarnessSpec,
    default_harness_id,
    get_harness,
    list_harnesses,
)


@pytest.mark.unit
def test_registry_contains_expected_harnesses() -> None:
    assert set(REGISTRY) == {"aider", "opencode", "openhands"}


@pytest.mark.unit
def test_default_is_opencode() -> None:
    assert default_harness_id() == "opencode"
    assert REGISTRY["opencode"].image == "opencode-agent:latest"


@pytest.mark.unit
def test_exactly_one_recommended() -> None:
    recommended = [h for h in REGISTRY.values() if h.recommended]
    assert len(recommended) == 1


@pytest.mark.unit
def test_get_harness_returns_spec() -> None:
    spec = get_harness("opencode")
    assert isinstance(spec, HarnessSpec)
    assert spec.id == "opencode"
    assert spec.image == "opencode-agent:latest"


@pytest.mark.unit
def test_get_unknown_harness_raises() -> None:
    with pytest.raises(KeyError):
        get_harness("nope")


@pytest.mark.unit
def test_every_spec_has_about_content() -> None:
    for spec in REGISTRY.values():
        assert spec.description
        assert spec.uniqueness
        assert spec.pros
        assert spec.cons
        assert spec.when_to_pick


@pytest.mark.unit
def test_list_harnesses_is_serializable() -> None:
    items = list_harnesses()
    assert all(isinstance(item, dict) for item in items)
    assert {"id", "display_name", "pros", "cons"} <= set(items[0])
