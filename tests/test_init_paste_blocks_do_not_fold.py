"""`praxis init`'s output is meant to be PASTED, and it folded.

``cli.main._copyable`` already documents this defect class: rich's default
wrapping inserts a REAL newline at the console width, so a line wider than the
terminal arrives as two and pasting either one yields half of it. That fix was
applied across ``cli/main.py`` and never to ``cli/init.py``, which had NO
``soft_wrap`` anywhere, and which is the one command whose entire output exists
to be copied somewhere else.

Found on 2026-08-28 by running an actual cold install rather than reading the
code. Two lines folded, and the second is the expensive one:

* the MCP configuration block, whose longest line is the absolute install path
  INSIDE a JSON string. It folded mid-path::

      "C:\\\\Users\\\\...\\\\C--working-space-pra
      xis\\\\...\\\\coldinstall",

  That is not untidy output, it is invalid JSON carrying a corrupted path, and
  it is the exact block the README's agent setup brief tells an assistant to
  paste into ``.mcp.json``;

* the PowerShell export line, which carries BOTH assignments joined by ``; ``.
  It folded after the URL, so pasting it set ``ORCHESTRATOR_URL`` and silently
  dropped ``ORCHESTRATOR_TOKEN``. It does not fail at paste time. It fails
  later, as an auth error against a URL the operator can see is correct, on the
  platform this project is primarily developed on.

**Why it shipped, and why these tests pin a WIDTH.** Every existing test of
this output builds ``Console(file=buffer, width=200, no_color=True)``. Nothing
folds at 200. A wide terminal and a test pinned to one look identical to a
correct implementation, which is the same trap ``_copyable``'s docstring
records for `praxis reject` and for init's own
``--accept-preset-requirements`` flag. These render at 80, the width rich
falls back to when output is redirected, which is what a CI log and a piped
install transcript both get.

The assertions are behavioural rather than textual: the JSON block has to
PARSE and its path has to round-trip, and the PowerShell line has to still
contain both assignments. A guard that matched substrings would pass on
folded output, since both halves are still present, just on different rows.
"""

# ruff: noqa: S101

from __future__ import annotations

import io
import json
from typing import Any

import pytest
from rich.console import Console

from cli import init as init_mod
from tests.cli_text import strip_ansi


#: Width rich falls back to for a redirected stream, and narrow enough that a
#: real install path folds. The defect is invisible above roughly 120.
_NARROW = 80

#: A long but entirely ordinary Windows install path: a OneDrive-synced
#: Documents tree, which is the DEFAULT location on a great many Windows
#: machines. The path that exposed this live was a scratchpad temp directory,
#: but nothing about the defect needs one that exotic, only one that pushes
#: the JSON line past the console width.
#:
#: The LENGTH is load-bearing, and is asserted as a precondition below. A
#: shorter path makes the block fit at 80, and then every assertion here passes
#: on folded output. Measured: the first version of this fixture,
#: ``...\AppData\Local\Programs\praxis-orchestrator``, renders its longest JSON
#: line at 73 columns and SURVIVED the mutation that deletes the fix.
_LONG_ROOT = r"C:\Users\somebody.name\OneDrive - Some Organisation\Documents\dev\praxis"

_PRESET: dict[str, Any] = {
    "name": "local-lmstudio",
    "harness": "opencode",
    "model": "q",
}


def _render_next_steps(monkeypatch: pytest.MonkeyPatch, token: str) -> str:
    """Render the post-install block at a narrow width and return the text.

    ``strip_ansi``, never ``plain``: ``plain`` collapses whitespace, which
    REJOINS exactly the rows rich folded apart and makes every assertion below
    pass on broken output. Measured: with ``plain`` all three tests survived
    all three mutations. ``cli_text`` says so in ``strip_ansi``'s own
    docstring, "use this when LINE STRUCTURE matters".
    """
    buffer = io.StringIO()
    monkeypatch.setattr(
        init_mod, "console", Console(file=buffer, width=_NARROW, no_color=True)
    )
    monkeypatch.setattr(
        init_mod, "mcp_snippet", lambda url, tok, **_: _snippet(url, tok)
    )
    init_mod._print_next_steps("http://127.0.0.1:12323", token, _PRESET)
    return strip_ansi(buffer.getvalue())


def _snippet(api_url: str, token: str) -> str:
    """The real snippet shape, pinned to a long root so the path line is long."""
    return json.dumps(
        {
            "mcpServers": {
                "praxis": {
                    "command": "uv",
                    "args": ["run", "--directory", _LONG_ROOT, "praxis-mcp"],
                    "env": {
                        "PRAXIS_BASE_URL": api_url,
                        "PRAXIS_AUTH_TOKEN": token,
                    },
                }
            }
        },
        indent=2,
    )


def test_the_mcp_block_is_still_valid_json_at_a_narrow_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The block a brief tells an assistant to paste has to parse."""
    # Precondition, not decoration. If the snippet fits inside the console
    # there is nothing to fold and this test CANNOT fail. It silently could
    # not, until this assertion was added: see the note on _LONG_ROOT.
    longest = max(len(line) for line in _snippet("http://x", "t").splitlines())
    assert longest > _NARROW, (
        f"the fixture path is too short to fold: longest JSON line is {longest} "
        f"columns against a {_NARROW}-column console, so this guard is inert"
    )

    text = _render_next_steps(monkeypatch, "tNkw0Uwg_VwoGbssIsY2VnOIl8JYESv_2vTXjB1")

    start = text.index("{")
    end = text.rindex("}", 0, text.index("Export these so"))
    block = text[start : end + 1]

    parsed = json.loads(block)  # folds here with a real newline inside a string
    args = parsed["mcpServers"]["praxis"]["args"]
    assert args[2] == _LONG_ROOT, (
        "the install path did not round-trip through the printed block; rich "
        f"folded it into {args[2]!r}"
    )


def test_the_powershell_export_keeps_both_assignments_on_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Half of this line is worse than none: it half-succeeds."""
    token = "tNkw0Uwg_VwoGbssIsY2VnOIl8JYESv_2vTXjB1KdlY"
    text = _render_next_steps(monkeypatch, token)

    line = next(ln for ln in text.splitlines() if "PowerShell:" in ln)
    assert "ORCHESTRATOR_URL" in line, (
        f"the PowerShell line lost the URL assignment. Got: {line!r}"
    )
    assert "ORCHESTRATOR_TOKEN" in line, (
        "the PowerShell line folded, so pasting it sets the URL and silently "
        f"drops the token. Got: {line!r}"
    )
    assert token in line, "the token itself was split across rows"


def test_the_posix_exports_each_stay_on_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sh form is shorter but folds too once a token is long enough."""
    token = "x" * 90
    text = _render_next_steps(monkeypatch, token)

    line = next(ln for ln in text.splitlines() if "export ORCHESTRATOR_TOKEN" in ln)
    assert token in line, f"the export line folded mid-token: {line!r}"
