"""Bench-only switches, double-gated so they cannot leak into normal operation.

Condition C of the benchmark is condition B with the per-leaf verify gate
disabled, which isolates whether the measured effect comes from decomposition
or from verification.  That is a genuinely dangerous switch: a Praxis running
without its verify gate merges unverified worker output.

So it takes TWO independent environment variables, both set to the literal
string "1".  A loose truthiness check (``bool(os.environ.get(...))``) is exactly
how a kill switch ends up live in production, so this module deliberately does
not use one.
"""

from __future__ import annotations

import logging
import os


logger = logging.getLogger(__name__)

_BENCH_MODE_ENV = "PRAXIS_BENCH"
_DISABLE_VERIFY_ENV = "PRAXIS_BENCH_DISABLE_VERIFY"
_ENABLED = "1"


def bench_mode() -> bool:
    """True only when ``PRAXIS_BENCH`` is exactly ``"1"``."""
    return os.environ.get(_BENCH_MODE_ENV) == _ENABLED


def verify_gate_disabled() -> bool:
    """True only when bench mode AND the explicit disable flag are both set.

    Returns False for every other combination, including the disable flag
    alone.  Logs loudly when it does return True: a run with no verify gate
    must never be mistaken for a normal one in the logs.
    """
    if not bench_mode():
        return False
    if os.environ.get(_DISABLE_VERIFY_ENV) != _ENABLED:
        return False
    logger.warning(
        "BENCH MODE: the mechanical verify gate is DISABLED. This is only "
        "valid for benchmark condition C and must never run in production."
    )
    return True
