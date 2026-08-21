"""Load global orchestrator settings from YAML, overlaid by environment vars."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml


logger = logging.getLogger(__name__)

_ENV_PREFIX = "PRAXIS_"

# Default location of the global settings YAML, relative to the process CWD.
# In the container this is a read-only bind mount, so an operator edit takes
# effect on restart rather than needing an image rebuild.
_DEFAULT_CONFIG_PATH = "config/praxis.yaml"

_CONFIG_PATH_ENV = "PRAXIS_CONFIG_PATH"

# Default location of the bundled model capability snapshot, relative to the
# process CWD.  Same reasoning as _DEFAULT_CONFIG_PATH above: the container's
# WORKDIR is /app and the Dockerfile both COPYs and bind-mounts config/ there,
# so this resolves to the same file whether run bare from the repo root or
# inside the container.
_DEFAULT_CAPABILITIES_PATH = "config/model_capabilities.json"

_CAPABILITIES_PATH_ENV = "PRAXIS_CAPABILITIES_PATH"

#: Absolute paths already reported as missing.  Module state because the
#: warning has to survive across calls: `load_yaml_settings` runs on every
#: `EffectiveSettings._get_yaml()`, so a per-call warning would bury the log
#: under thousands of identical lines.  Bounded in practice by how many
#: distinct settings paths one process resolves, which is one.
_WARNED_MISSING_PATHS: set[str] = set()


def config_file_path() -> str:
    """Return the path to the global settings YAML.

    ``PRAXIS_CONFIG_PATH`` overrides the default so the container can point at
    its mount and a test can point at a temporary file.  This is the ONLY place
    the path is decided; a hardcoded literal anywhere else reintroduces the
    2026-07-27 bug where a YAML edit required an image rebuild.

    Returns:
        Filesystem path to the YAML settings file, which need not exist.
    """
    return os.environ.get(_CONFIG_PATH_ENV) or _DEFAULT_CONFIG_PATH


def capabilities_file_path() -> str:
    """Return the path to the bundled model capability snapshot.

    Mirrors ``config_file_path()``: ``PRAXIS_CAPABILITIES_PATH`` overrides the
    default so a test or an alternate deployment can point elsewhere. This is
    the ONLY place ``core.capabilities.CapabilityCatalog`` may decide where
    ``model_capabilities.json`` lives; a hardcoded literal anywhere else
    reintroduces the class of bug ``config_file_path()`` exists to prevent
    for ``praxis.yaml`` (``tests/test_config_path.py`` greps for both).

    Returns:
        Filesystem path to the capability snapshot JSON, which need not
        exist (``CapabilityCatalog`` degrades to an empty catalog).
    """
    return os.environ.get(_CAPABILITIES_PATH_ENV) or _DEFAULT_CAPABILITIES_PATH


def _is_overlay_var(key: str) -> bool:
    """True when an environment variable name is a ``PRAXIS_*`` setting override.

    ``PRAXIS_CONFIG_PATH`` is deliberately excluded: it is a POINTER TO the
    settings file, not a setting inside it.  Both compose files set it
    permanently, so folding it in would give every deployment a phantom
    ``config_path`` key that no consumer wants.

    Shared by :func:`load_yaml_settings` and :func:`env_overlay_keys` so the
    two cannot drift: the second exists to tell a caller which keys the first
    sourced from the environment rather than from the file, and a divergence
    would make that answer wrong in exactly the cases it is consulted for.

    Args:
        key: Environment variable name.

    Returns:
        True when the variable overrides a settings key.
    """
    return key.startswith(_ENV_PREFIX) and key != _CONFIG_PATH_ENV


def env_overlay_keys(env: dict[str, str] | None = None) -> set[str]:
    """Return the settings names ``PRAXIS_*`` env vars contribute to the overlay.

    Names are the lowercase form :func:`load_yaml_settings` folds them in
    under, so a caller can tell an overlaid key apart from one that genuinely
    came out of the file.

    Args:
        env: Environment mapping to read, or None for ``os.environ``.

    Returns:
        Lowercase settings names supplied by the environment.
    """
    env = os.environ.copy() if env is None else env
    return {key[len(_ENV_PREFIX) :].lower() for key in env if _is_overlay_var(key)}


def load_yaml_settings(path: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Return YAML settings with PRAXIS_* env vars overriding matching keys."""
    env = os.environ.copy() if env is None else env
    file = Path(path)
    data: dict[str, Any] = {}
    if path and not file.is_file():
        _warn_missing(path)
    if file.is_file():
        try:
            loaded = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            message = f"Invalid YAML in {path}: {exc}"
            raise ValueError(message) from exc
        if not isinstance(loaded, dict):
            message = f"{path} must contain a mapping"
            raise ValueError(message)
        data = loaded
    for key, raw in env.items():
        if _is_overlay_var(key):
            name = key[len(_ENV_PREFIX) :].lower()
            data[name] = _coerce(raw)
    return data


def _warn_missing(path: str) -> None:
    """Report a settings file that is not there, once per distinct path.

    Silence here is indistinguishable from a healthy load: the loader returns
    ``{}`` either way, every YAML default reverts to its built-in value, and
    the orchestrator boots and serves settings the operator never chose.
    Precisely: this fires only when the configured path does not exist at
    all, for example a typo'd ``PRAXIS_CONFIG_PATH`` or a mount landing on an
    empty host directory.  It does NOT cover a dropped ``./config:/app/config``
    mount: ``docker/orchestrator/Dockerfile`` also ``COPY config/ config/``,
    so the path still exists as a stale baked-in copy rather than an absent
    one, and that case is caught instead by
    ``core.doctor_probes.probe_config_mount`` (via ``os.path.ismount``).

    Warning once rather than on every call is not politeness: this runs on
    every ``EffectiveSettings._get_yaml()``, and a log drowned in one repeated
    line is as unread as a log with nothing in it.

    Args:
        path: The path that was looked for, as configured.
    """
    # Lexical, not `Path.resolve()`: this is a hot path and abspath never
    # touches the filesystem.  The absolute form is the actionable half of the
    # message, because a CWD-relative "config/praxis.yaml" says nothing about
    # which working directory failed to contain it.
    absolute = os.path.abspath(path)
    if absolute in _WARNED_MISSING_PATHS:
        return
    _WARNED_MISSING_PATHS.add(absolute)
    logger.warning(
        "Settings file not found at %s; built-in defaults are in effect. "
        "Set PRAXIS_CONFIG_PATH, or create the file, if this is not intended.",
        absolute,
    )


def _coerce(value: str) -> Any:
    if value.isdigit():
        return int(value)
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    return value
