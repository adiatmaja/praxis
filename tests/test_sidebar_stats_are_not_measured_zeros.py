"""The sidebar stats stated four measurements nobody took.

Every one of `#stat-projects`, `#stat-plans`, `#stat-agents` and `#stat-queue`
is written only AFTER its fetch succeeds. A rejected token, an unreachable API,
or simply the moment before the first poll therefore leaves the STATIC default
from ``index.html`` on screen.

That default was ``0``. Observed on the live dashboard on 2026-08-28 with a bad
token: the panel two rows down correctly read "not authorized" for both
provider seats, and the "Not authorized" banner filled the main area, while the
Stats block above them read Projects 0, Plans 0, Agents 0, Queue 0. The page as
a whole was honest and that block was not.

Same defect class as the ``"N/M merged"`` and ``"Agents: NaN"`` entries in
``tests/test_dashboard_truthful_rendering.py``, whose docstring names it: "the
absent fact degraded into a value that looks measured". That file also records
the closely related bug where ``renderHealthBar`` read ``#stat-agents`` back
out of the DOM and asserted "a zero nobody measured" from this very default.

A non-numeric placeholder is safe here precisely because that read-back was
removed: nothing parses these elements, which this file also pins, because a
future reader who reintroduces a ``Number(...textContent)`` would turn the
placeholder into ``NaN`` and rediscover the older bug.
"""

# ruff: noqa: S101

from __future__ import annotations

import re
from pathlib import Path


INDEX_HTML = Path("web/index.html").read_text(encoding="utf-8")
APP_JS = Path("web/app.js").read_text(encoding="utf-8")

#: The four sidebar counters, all written only on a successful fetch.
_STAT_IDS = ("stat-projects", "stat-plans", "stat-agents", "stat-queue")


def _static_default(stat_id: str) -> str:
    """The literal text `index.html` ships inside the element."""
    match = re.search(
        r'id="' + re.escape(stat_id) + r'"[^>]*>(.*?)</strong>', INDEX_HTML
    )
    assert match is not None, f"{stat_id} is no longer a <strong> in index.html"
    return match.group(1).strip()


def test_no_stat_ships_a_numeric_default() -> None:
    """A number here is a claim; these are written only after a fetch lands."""
    for stat_id in _STAT_IDS:
        default = _static_default(stat_id)
        assert not default.isdigit(), (
            f"{stat_id} ships the numeric default {default!r}. It is only "
            "overwritten once its fetch succeeds, so an unauthorized or "
            "unreachable dashboard renders that number as a measurement"
        )
        assert default, f"{stat_id} ships an empty default, which reads as a gap"


def test_every_stat_is_actually_present() -> None:
    """Guard the guard: a renamed id must not silently empty the loop above."""
    for stat_id in _STAT_IDS:
        assert f'id="{stat_id}"' in INDEX_HTML, (
            f"{stat_id} is gone from index.html; this file's assertions would "
            "otherwise pass vacuously"
        )


def test_nothing_parses_a_stat_back_out_of_the_dom() -> None:
    """The placeholder is only safe while nothing reads these as numbers.

    ``renderHealthBar`` used to do exactly that and printed "Agents: NaN".
    Reintroducing it would now also turn the honest placeholder into NaN, so
    the two facts are pinned together.
    """
    for stat_id in _STAT_IDS:
        pattern = re.compile(
            r"Number\(\s*document\.getElementById\(\s*[\"']" + re.escape(stat_id)
        )
        assert not pattern.search(APP_JS), (
            f"something parses #{stat_id} back out of the DOM; that is the "
            '"Agents: NaN" defect, and it breaks the non-numeric default'
        )
