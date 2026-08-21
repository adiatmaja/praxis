"""A value in `.env` must not lose silently to the settings file.

The documented order is environment, then the settings file, then the
built-in default. `Settings.__init__` injects the settings file's values as
INIT KWARGS, and init kwargs outrank every pydantic-settings source including
the dotenv one, so a key present in BOTH files silently took the settings
file's value with nothing logged anywhere.

Measured live on 2026-08-21: `LOOP_INTERVAL=0` in `.env` left the container
running at the settings file's 5 and said nothing. The same shadowing split
the two supported ways of running the product apart, which is the part that
makes it more than a curiosity: compose forwards DEFAULT_WORKER_HARNESS and
DEFAULT_WORKER_MODEL as real environment variables, so the dotenv file that
`praxis init` writes WON inside a container and LOST under a bare uvicorn,
with no way to tell which you were looking at.

`_env_file=None` throughout except where the dotenv layer is the subject:
without it pydantic-settings reads the repo's real `.env` and the ambient
environment beats both, so an unpinned test asserts about the developer's
machine rather than about the code.
"""
# ruff: noqa: S101

from __future__ import annotations

import logging

import pytest

from orchestrator.config import Settings
from orchestrator.core.settings_file import env_overlay_keys


@pytest.fixture
def clean_env(monkeypatch):
    """Remove every variable these tests reason about from the environment."""
    for name in (
        "LOOP_INTERVAL",
        "DEFAULT_WORKER_HARNESS",
        "DEFAULT_WORKER_MODEL",
        "PRAXIS_LOOP_INTERVAL",
        "PRAXIS_DEFAULT_WORKER_HARNESS",
        "AUTH_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _yaml(tmp_path, text: str) -> str:
    target = tmp_path / "settings.yaml"
    target.write_text(text, encoding="utf-8")
    return str(target)


def _dotenv(tmp_path, text: str) -> str:
    target = tmp_path / "dotenv"
    target.write_text(text, encoding="utf-8")
    return str(target)


@pytest.mark.unit
def test_a_dotenv_value_beats_the_settings_file(clean_env, tmp_path) -> None:
    """The defect itself. `.env` says 11, the settings file says 5."""
    settings = Settings(
        yaml_path=_yaml(tmp_path, "loop_interval: 5\n"),
        _env_file=_dotenv(tmp_path, "AUTH_TOKEN=t\nLOOP_INTERVAL=11\n"),
    )

    assert settings.loop_interval == 11


@pytest.mark.unit
def test_the_settings_file_still_wins_over_the_built_in_default(
    clean_env, tmp_path
) -> None:
    """The fix must not invert the layer below it.

    A dotenv file that does NOT name the key leaves the settings file in
    charge, which is the case every shipped install is in for `loop_interval`.
    """
    settings = Settings(
        yaml_path=_yaml(tmp_path, "loop_interval: 30\n"),
        _env_file=_dotenv(tmp_path, "AUTH_TOKEN=t\n"),
    )

    assert settings.loop_interval == 30


@pytest.mark.unit
def test_a_real_environment_variable_still_beats_the_dotenv_file(
    clean_env, tmp_path
) -> None:
    """Precedence is environment, then dotenv, then settings file."""
    clean_env.setenv("LOOP_INTERVAL", "7")
    settings = Settings(
        yaml_path=_yaml(tmp_path, "loop_interval: 5\n"),
        _env_file=_dotenv(tmp_path, "AUTH_TOKEN=t\nLOOP_INTERVAL=11\n"),
    )

    assert settings.loop_interval == 7


@pytest.mark.unit
def test_a_praxis_prefixed_variable_is_not_treated_as_shadowed(
    clean_env, tmp_path
) -> None:
    """The exemption that keeps the fix from dropping a real env var.

    A `PRAXIS_*` variable is not in the overlay because the FILE holds it, it
    is there because the environment put it there. Treating it as "shadowed
    by the dotenv file" would discard a genuine environment variable in
    favour of a lower-precedence source, which is the original bug with the
    layers swapped.
    """
    clean_env.setenv("PRAXIS_LOOP_INTERVAL", "23")
    settings = Settings(
        yaml_path=_yaml(tmp_path, "loop_interval: 5\n"),
        _env_file=_dotenv(tmp_path, "AUTH_TOKEN=t\nLOOP_INTERVAL=11\n"),
    )

    assert settings.loop_interval == 23


@pytest.mark.unit
def test_the_worker_preset_agrees_bare_and_containerized(clean_env, tmp_path) -> None:
    """The half of this that a newcomer actually meets.

    `praxis init` writes DEFAULT_WORKER_HARNESS to `.env`, and both compose
    files forward it BARE, so in a container it arrives as a real environment
    variable and wins. Run bare, the same file lost to the settings file and
    the operator's chosen preset was silently not the one in effect.
    """
    settings = Settings(
        yaml_path=_yaml(tmp_path, "default_worker_harness: agy\n"),
        _env_file=_dotenv(tmp_path, "AUTH_TOKEN=t\nDEFAULT_WORKER_HARNESS=opencode\n"),
    )

    assert settings.default_worker_harness == "opencode"


@pytest.mark.unit
def test_no_dotenv_file_leaves_the_settings_file_in_charge(clean_env, tmp_path) -> None:
    """`_env_file=None` is how tests isolate themselves; it must read none."""
    settings = Settings(
        auth_token="t",
        yaml_path=_yaml(tmp_path, "loop_interval: 30\n"),
        _env_file=None,
    )

    assert settings.loop_interval == 30


@pytest.mark.unit
def test_an_explicit_none_env_file_resolves_to_no_dotenv_path() -> None:
    """Asserted directly, because the behavioural version above is inert here.

    Reverting the `_env_file` handling makes the resolver fall back to the
    repo's OWN `.env`, which happens not to name `loop_interval`, so the
    settings-level test above passes either way. That is the shape of every
    guard this session has had to throw away: it fails for the mutation you
    imagined and not for the one you wrote. This one names the seam.
    """
    from orchestrator.config import _dotenv_paths

    assert _dotenv_paths({"_env_file": None}, ".env") == []
    assert _dotenv_paths({}, None) == []


@pytest.mark.unit
def test_an_override_is_reported_once(clean_env, tmp_path, caplog) -> None:
    """Closing one silence must not open another in the other direction.

    After the fix, an operator who edits the settings file for a key their
    dotenv file also names gets their edit ignored. That is the right
    precedence and the wrong amount of silence, so it is logged: once per
    key, because `Settings` is constructed more than once per process.
    """
    from orchestrator import config as config_module

    config_module._LOGGED_DOTENV_OVERRIDES.clear()
    yaml_path = _yaml(tmp_path, "loop_interval: 5\n")
    dotenv_path = _dotenv(tmp_path, "AUTH_TOKEN=t\nLOOP_INTERVAL=11\n")

    with caplog.at_level(logging.INFO, logger="orchestrator.config"):
        Settings(yaml_path=yaml_path, _env_file=dotenv_path)
        Settings(yaml_path=yaml_path, _env_file=dotenv_path)

    lines = [
        r.getMessage() for r in caplog.records if "LOOP_INTERVAL" in r.getMessage()
    ]
    assert len(lines) == 1


@pytest.mark.unit
def test_a_real_environment_variable_is_not_reported_as_a_dotenv_override(
    clean_env, tmp_path, caplog
) -> None:
    """Only the dotenv case is a surprise; the environment winning is not."""
    from orchestrator import config as config_module

    config_module._LOGGED_DOTENV_OVERRIDES.clear()
    clean_env.setenv("LOOP_INTERVAL", "7")

    with caplog.at_level(logging.INFO, logger="orchestrator.config"):
        Settings(
            yaml_path=_yaml(tmp_path, "loop_interval: 5\n"),
            _env_file=_dotenv(tmp_path, "AUTH_TOKEN=t\n"),
        )

    assert not [r for r in caplog.records if "LOOP_INTERVAL" in r.getMessage()]


@pytest.mark.unit
def test_env_overlay_keys_names_the_settings_the_environment_supplied() -> None:
    """The helper the exemption above is built on, and its exclusion.

    `PRAXIS_CONFIG_PATH` is a POINTER TO the settings file, not a setting
    inside it, and both compose files set it permanently, so folding it in
    would give every deployment a phantom `config_path` key.
    """
    keys = env_overlay_keys(
        {
            "PRAXIS_LOOP_INTERVAL": "9",
            "PRAXIS_CONFIG_PATH": "/app/somewhere/settings.yaml",
            "LOOP_INTERVAL": "9",
        }
    )

    assert keys == {"loop_interval"}
