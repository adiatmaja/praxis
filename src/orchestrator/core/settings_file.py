"""Load global orchestrator settings from YAML, overlaid by environment vars."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


_ENV_PREFIX = "PRAXIS_"


def load_yaml_settings(path: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Return YAML settings with PRAXIS_* env vars overriding matching keys."""
    env = os.environ.copy() if env is None else env
    file = Path(path)
    data: dict[str, Any] = {}
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
        if key.startswith(_ENV_PREFIX):
            name = key[len(_ENV_PREFIX) :].lower()
            data[name] = _coerce(raw)
    return data


def _coerce(value: str) -> Any:
    if value.isdigit():
        return int(value)
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    return value
