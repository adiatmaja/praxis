"""`praxis mcp` re-prints the MCP block, which nothing else could.

``praxis init`` prints the MCP client configuration ONCE, in the middle of an
output that is mostly Docker build progress and takes minutes to produce. Until
this verb existed, ``mcp_snippet`` had exactly one caller:

    grep -rn "mcp_snippet" src/ --include=*.py
      src/cli/init.py:  def mcp_snippet(...)          <- definition
      src/cli/init.py:  console.print(mcp_snippet(...))  <- the only call

So there was no second way to see it, and an operator whose scrollback had
rolled had to re-run the whole install.

Measured on 2026-08-28: an assistant following the README's own agent setup
brief, in a fresh clone, lost the block to output truncation and reported that
its only remaining route was to run ``init`` again. Step 5 of that brief
consists of pasting this block, so that step was effectively un-completable
from a long transcript.

Four properties, each of which was a way to get this wrong:

* the emitted block must PARSE. Its longest line is the absolute install path
  inside a JSON string, so a rich fold does not merely look untidy, it emits
  invalid JSON with a broken path. That is the defect fixed in ``init`` the
  same day;
* ``--directory`` must be the INSTALL ROOT, not the current directory.
  ``mcp_snippet`` defaults to ``Path.cwd()``, which is right for ``init``
  (which has already proven the cwd is the root) and wrong here, because this
  verb is meant to be runnable from a subdirectory. A ``--directory`` pointing
  at ``src/orchestrator`` gives the client a ``uv run`` that cannot find the
  project;
* it must work with the orchestrator DOWN, like ``praxis presets``. Setting up
  an MCP client is exactly when the server may not be running yet;
* the snippet must come from ``cli.init.mcp_snippet``, not a second copy. The
  env var names in it are the ones ``mcp_server.client`` actually reads, and a
  block with plausible-but-wrong names is worse than no block: it gets pasted,
  and the server then silently falls back to a default URL with no token.
"""

# ruff: noqa: S101

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli.main import app
from tests.cli_text import strip_ansi


runner = CliRunner()


@pytest.fixture
def install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A directory that looks like a Praxis install, and a cwd inside it."""
    (tmp_path / ".env").write_text(
        "AUTH_TOKEN=tok-abc123\nPORT=12323\n", encoding="utf-8"
    )
    for var in ("ORCHESTRATOR_URL", "ORCHESTRATOR_TOKEN", "AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    # `_env_file_values` is cached per process, so a test that changed the cwd
    # would otherwise serve the previous test's file.
    from cli import main as main_mod

    main_mod._env_file_values.cache_clear()
    return tmp_path


def _block(output: str) -> dict:
    text = strip_ansi(output)
    return json.loads(text[text.index("{") : text.rindex("}") + 1])


def test_the_block_parses_and_names_the_install_root(install: Path) -> None:
    """The whole point: a valid block, recoverable without re-running init."""
    result = runner.invoke(app, ["mcp"])

    assert result.exit_code == 0, result.output
    parsed = _block(result.output)
    server = parsed["mcpServers"]["praxis"]
    assert server["args"][2] == str(install.resolve())
    assert server["env"]["PRAXIS_AUTH_TOKEN"] == "tok-abc123"


def test_it_reports_the_install_root_not_the_current_directory(
    install: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run from a SUBDIRECTORY, the root must not follow the cwd.

    ``mcp_snippet``'s own default is ``Path.cwd()``. Taking that default here
    would emit a ``--directory`` pointing at the subdirectory, and the MCP
    client's ``uv run`` would then fail to find the project.
    """
    nested = install / "src" / "orchestrator"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    result = runner.invoke(app, ["mcp"])

    assert result.exit_code == 0, result.output
    args = _block(result.output)["mcpServers"]["praxis"]["args"]
    assert args[2] == str(install.resolve()), (
        f"--directory followed the cwd instead of the install root: {args[2]!r}"
    )


def test_it_needs_no_running_orchestrator(
    install: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting an MCP client up is exactly when the server may be down.

    Any HTTP the verb attempted would go through ``httpx``; making the client
    explode proves the happy path never builds one.
    """
    import httpx

    def _explode(*_a: object, **_k: object) -> None:
        msg = "praxis mcp must not contact the orchestrator"
        raise AssertionError(msg)

    monkeypatch.setattr(httpx, "Client", _explode)
    monkeypatch.setattr(httpx, "get", _explode)

    result = runner.invoke(app, ["mcp"])

    assert result.exit_code == 0, result.output
    assert "mcpServers" in strip_ansi(result.output)


def test_it_says_where_to_look_when_there_is_no_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `.env` anywhere up the tree is an operator state, not a traceback."""
    for var in ("ORCHESTRATOR_URL", "ORCHESTRATOR_TOKEN", "AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    empty = tmp_path / "nowhere"
    empty.mkdir()
    monkeypatch.chdir(empty)
    from cli import main as main_mod

    main_mod._env_file_values.cache_clear()
    monkeypatch.setattr(main_mod, "_find_env_file", lambda: None)

    result = runner.invoke(app, ["mcp"])

    assert result.exit_code == 1
    out = strip_ansi(result.output)
    assert "No .env found" in out
    assert "praxis init" in out, "the remedy has to name the verb that fixes it"


def test_the_snippet_is_not_a_second_copy() -> None:
    """One producer, or the env var names drift and a pasted block goes silent.

    ``mcp_server.client`` reads ``PRAXIS_BASE_URL`` and ``PRAXIS_AUTH_TOKEN``.
    A second hand-written copy here could name something plausible and wrong,
    which is worse than printing nothing: it gets pasted and the server falls
    back to a default URL with no token.
    """
    source = Path("src/cli/main.py").read_text(encoding="utf-8")
    assert "from cli.init import mcp_snippet" in source, (
        "praxis mcp must reuse cli.init.mcp_snippet rather than assemble its own block"
    )
    assert '"mcpServers"' not in source, (
        "main.py assembles its own MCP block; that is the second copy this "
        "test exists to prevent"
    )
