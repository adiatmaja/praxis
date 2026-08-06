"""Loading global settings, including what happens when the file is not there.

A missing settings file is the quietest failure in this codebase: every YAML
default reverts to its built-in value, the orchestrator boots, and nothing
anywhere says the file the operator edited was never read.  A typo in
``PRAXIS_CONFIG_PATH`` and a broken container mount both look exactly like a
correctly configured install.
"""

import logging

import pytest

from orchestrator.core import settings_file
from orchestrator.core.settings_file import load_yaml_settings


@pytest.fixture(autouse=True)
def _forget_warned_paths():
    """Start and end each test with a clean warn-once ledger.

    The ledger is module state by necessity (the warning has to survive across
    calls), so without this the assertions here would depend on test order.
    """
    settings_file._WARNED_MISSING_PATHS.clear()
    yield
    settings_file._WARNED_MISSING_PATHS.clear()


def test_load_defaults(tmp_path):
    p = tmp_path / "praxis.yaml"
    p.write_text("loop_interval: 5\ncallback_grace: 5\n", encoding="utf-8")
    cfg = load_yaml_settings(str(p), env={})
    assert cfg["loop_interval"] == 5


def test_env_overrides_yaml(tmp_path):
    p = tmp_path / "praxis.yaml"
    p.write_text("loop_interval: 5\n", encoding="utf-8")
    cfg = load_yaml_settings(str(p), env={"PRAXIS_LOOP_INTERVAL": "9"})
    assert cfg["loop_interval"] == 9


def test_missing_file_returns_empty(tmp_path):
    assert load_yaml_settings(str(tmp_path / "nope.yaml"), env={}) == {}


def test_malformed_yaml_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("loop_interval: : :\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid YAML"):
        load_yaml_settings(str(p), env={})


@pytest.mark.unit
def test_a_missing_settings_file_is_warned_about(tmp_path, caplog):
    """The reported defect: a typo'd path reverted every default in silence.

    Returning ``{}`` is the right behavior (a fresh clone must still boot), so
    the only thing that can distinguish "there is no file" from "the file I
    edited is being read" is a log line.
    """
    missing = tmp_path / "typo.yaml"
    with caplog.at_level(logging.WARNING):
        load_yaml_settings(str(missing), env={})

    messages = [record.message for record in caplog.records]
    assert any(str(missing) in message for message in messages)
    assert any("defaults" in message for message in messages)


@pytest.mark.unit
def test_a_missing_settings_file_is_warned_about_only_once(tmp_path, caplog):
    """It runs on every `_get_yaml()`, which is a hot path.

    `EffectiveSettings` reloads the file for each capability, escalation and
    registry lookup, so a warning per call buries the log under thousands of
    identical lines.  That is the same silence as no warning at all, reached
    from the other direction: an operator who cannot read the log does not.
    """
    missing = str(tmp_path / "typo.yaml")
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            load_yaml_settings(missing, env={})

    matching = [r for r in caplog.records if missing in r.message]
    assert len(matching) == 1


@pytest.mark.unit
def test_each_distinct_missing_path_gets_its_own_warning(tmp_path, caplog):
    """Deduping on "have I ever warned" would hide the second misconfiguration.

    A process legitimately reads more than one path over its life (a test
    harness, a re-resolved `PRAXIS_CONFIG_PATH`), and the interesting case is
    the one that changed.
    """
    first = str(tmp_path / "one.yaml")
    second = str(tmp_path / "two.yaml")
    with caplog.at_level(logging.WARNING):
        load_yaml_settings(first, env={})
        load_yaml_settings(second, env={})

    assert [r for r in caplog.records if first in r.message]
    assert [r for r in caplog.records if second in r.message]


@pytest.mark.unit
def test_a_file_that_exists_is_not_warned_about(tmp_path, caplog):
    """The warning has to mean something, so the healthy path must be quiet."""
    present = tmp_path / "praxis.yaml"
    present.write_text("loop_interval: 5\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        load_yaml_settings(str(present), env={})

    assert caplog.records == []


@pytest.mark.unit
def test_no_configured_path_at_all_is_not_warned_about(caplog):
    """An empty path is a caller saying "no file", not a misconfiguration.

    `config_file_path()` never returns "", so this state is only reachable
    through an explicit `Settings(yaml_path="")`.  Warning that "" does not
    exist would be noise that says nothing actionable.
    """
    with caplog.at_level(logging.WARNING):
        assert load_yaml_settings("", env={}) == {}

    assert caplog.records == []
