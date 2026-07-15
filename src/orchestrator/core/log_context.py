"""Correlation-ID logging helper."""

from __future__ import annotations

import logging
from collections.abc import MutableMapping
from typing import Any


class _IdAdapter(logging.LoggerAdapter):
    """Prefix log messages with plan / task / run correlation IDs."""

    def process(
        self, msg: str, kwargs: MutableMapping[str, Any]
    ) -> tuple[str, MutableMapping[str, Any]]:
        parts = [f"{k}={v}" for k, v in (self.extra or {}).items() if v is not None]
        prefix = f"[{' '.join(parts)}] " if parts else ""
        return (f"{prefix}{msg}", kwargs)


def task_logger(
    base: logging.Logger,
    *,
    plan_id: str | None = None,
    task_id: str | None = None,
    run_id: str | None = None,
) -> logging.LoggerAdapter:
    """Return a LoggerAdapter that prefixes correlation IDs."""
    return _IdAdapter(
        base,
        {"plan": plan_id, "task": task_id, "run": run_id},
    )
