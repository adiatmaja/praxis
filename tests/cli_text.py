"""Canonical helpers for asserting on rendered CLI output.

Every assertion about what the CLI PRINTED has to survive two transformations
the test author did not write and cannot see in the source: rich colorizes when
it believes the stream can take it, and it draws box glyphs around anything it
wraps into a panel or a table. Both are ENVIRONMENT DEPENDENT. Five guards in
``test_init_claims.py`` were green on Windows and red on CI's Linux for exactly
this, and running the suite under ``FORCE_COLOR=1`` turns four more red that
pass without it.

This module exists because there were already TWO private copies of these
helpers and they had drifted: one stripped a hand-listed string of box glyphs,
the other the whole ``U+2500-U+257F`` block, so the same phrase could be
matchable in one file and not the other. A helper duplicated per file is a
helper that will be corrected in one file.

Order is load-bearing and is the same in every function here: ANSI first
(an escape can sit MID-WORD, so anything else done first leaves it embedded),
box glyphs second (what a wrapped panel row leaves behind), whitespace last.
"""

from __future__ import annotations

import re
from typing import Any


#: ANSI SGR sequences, e.g. the ``\x1b[1;36m`` rich wraps a highlighted number
#: in, which lands INSIDE an id and breaks ``"abc-123" in stdout``.
ANSI = re.compile(r"\x1b\[[0-9;]*m")

#: The whole Box Drawing block. A hand-listed subset is how the two previous
#: copies drifted, so this deliberately takes the range rather than a list.
BOX_GLYPHS = re.compile(r"[─-╿]")


def strip_ansi(text: str) -> str:
    """Return ``text`` with colour removed and nothing else changed.

    Use this when LINE STRUCTURE matters, e.g. checking that a copyable
    command survived on one line. Collapsing whitespace would destroy the
    very thing under test.
    """
    return ANSI.sub("", text)


def plain(text: str) -> str:
    """Return ``text`` with ANSI and box glyphs removed, whitespace collapsed.

    Use this for PROSE assertions, where the phrase matters and the line
    breaks do not.
    """
    return " ".join(BOX_GLYPHS.sub(" ", ANSI.sub("", text)).split())


def on_one_line(result: Any, needle: str) -> bool:
    """True when ``needle`` appears CONTIGUOUS on a single output line.

    Colour is stripped per line, so a highlighted id still matches, but a
    needle broken across two lines by rich's wrap correctly does not: that
    break is the defect several of these guards exist to catch.
    """
    return any(needle in strip_ansi(line) for line in result.stdout.splitlines())


def flat(result: Any) -> str:
    """The whole of ``result.stdout`` as one colour-free, collapsed line."""
    return plain(result.stdout)
