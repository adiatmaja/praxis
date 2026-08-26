"""`praxis submit --file` must survive a spec too big for a shell argument.

Git Bash truncates a `bash -c` argument at 8192 bytes and reports it as a
syntax error in the CALLER's own script, not as "argument too long". A real
spec runs to thousands of bytes, so the positional-only `submit` breaks on
exactly the input it exists for. `--file` reads the spec from disk (UTF-8,
explicit: this is Windows and the console default is cp1252) instead.
"""

from __future__ import annotations

import httpx
from typer.testing import CliRunner

from cli.main import app
from tests.cli_text import flat


runner = CliRunner()

PROJECT_ID = "b5a1b6b0-1c2b-4e2a-9a3a-9c1f6b2a7e11"
PLAN_ID = "c1a2b3c4-d5e6-4f70-8a91-1234567890ab"


def _patch_client(monkeypatch, handler) -> None:
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "t")
    monkeypatch.setattr(
        "cli.main._client",
        lambda: httpx.Client(
            base_url="http://x",
            headers={"Authorization": "Bearer t"},
            transport=httpx.MockTransport(handler),
        ),
    )


def test_submit_positional_spec_still_works(monkeypatch) -> None:
    """Unchanged behaviour: the old one-argument invocation keeps working."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _j

        captured.update(_j.loads(request.content))
        return httpx.Response(200, json={"id": PLAN_ID, "status": "pending"})

    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["submit", PROJECT_ID, "Add input validation"])

    assert result.exit_code == 0
    assert captured["spec"] == "Add input validation"
    assert PLAN_ID in result.stdout


def test_submit_file_reads_utf8_content(monkeypatch, tmp_path) -> None:
    """`--file` reads the spec text from disk, UTF-8, and posts it verbatim."""
    captured: dict = {}
    spec_path = tmp_path / "spec.md"
    # Includes non-ASCII to prove the read is explicitly UTF-8, not the
    # Windows console default (cp1252), which would mangle or raise on this.
    spec_text = "Tambahkan validasi input pengguna.\nCakupan: café, naïve.\n"
    spec_path.write_bytes(spec_text.encode("utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _j

        captured.update(_j.loads(request.content))
        return httpx.Response(200, json={"id": PLAN_ID, "status": "pending"})

    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["submit", PROJECT_ID, "--file", str(spec_path)])

    assert result.exit_code == 0, result.stdout
    assert captured["spec"] == spec_text


def test_submit_file_short_flag_works(monkeypatch, tmp_path) -> None:
    """`-f` is the documented short form and must behave identically."""
    captured: dict = {}
    spec_path = tmp_path / "spec.md"
    spec_path.write_bytes(b"short flag spec")

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _j

        captured.update(_j.loads(request.content))
        return httpx.Response(200, json={"id": PLAN_ID, "status": "pending"})

    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["submit", PROJECT_ID, "-f", str(spec_path)])

    assert result.exit_code == 0, result.stdout
    assert captured["spec"] == "short flag spec"


def test_submit_file_dash_reads_stdin(monkeypatch) -> None:
    """`--file -` reads the spec from stdin, UTF-8 explicit."""
    captured: dict = {}
    spec_text = "Spec piped in from stdin: café\n"

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _j

        captured.update(_j.loads(request.content))
        return httpx.Response(200, json={"id": PLAN_ID, "status": "pending"})

    _patch_client(monkeypatch, handler)
    result = runner.invoke(
        app,
        ["submit", PROJECT_ID, "--file", "-"],
        input=spec_text,
    )

    assert result.exit_code == 0, result.stdout
    assert captured["spec"] == spec_text


def test_submit_rejects_both_positional_and_file(monkeypatch, tmp_path) -> None:
    """Passing both is a clear usage error, never a silent pick of one."""
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"id": PLAN_ID, "status": "pending"})

    _patch_client(monkeypatch, handler)
    spec_path = tmp_path / "spec.md"
    spec_path.write_bytes(b"file spec")

    result = runner.invoke(
        app, ["submit", PROJECT_ID, "inline spec", "--file", str(spec_path)]
    )

    assert result.exit_code != 0
    assert "both" in result.stdout.lower() or "either" in result.stdout.lower()
    assert called["n"] == 0


def test_submit_rejects_neither_spec_nor_file(monkeypatch) -> None:
    """Passing neither is a clear usage error, never a silent no-op post."""
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"id": PLAN_ID, "status": "pending"})

    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["submit", PROJECT_ID])

    assert result.exit_code != 0
    assert called["n"] == 0


def test_submit_missing_file_errors_without_creating_a_plan(
    monkeypatch, tmp_path
) -> None:
    """A missing/unreadable file names the path, exits non-zero, posts nothing."""
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"id": PLAN_ID, "status": "pending"})

    _patch_client(monkeypatch, handler)
    missing_path = tmp_path / "does-not-exist.md"

    result = runner.invoke(app, ["submit", PROJECT_ID, "--file", str(missing_path)])

    assert result.exit_code != 0
    # The path carries digits and separators rich highlights, so an escape can
    # land mid-path; match against colour-free output.
    assert str(missing_path) in flat(result)
    assert called["n"] == 0


# --------------------------------------------------------------------------
# A spec that is not UTF-8 is an ORDINARY input on this platform.
#
# `UnicodeDecodeError` is a `ValueError`, not an `OSError`, so it walked
# straight past the handler two lines above and out of the CLI as a raw
# traceback. A spec saved by Notepad as ANSI, or one carrying a single Word
# smart quote pasted from a document, is the normal case here, and a traceback
# names neither the cause nor the remedy: the operator sees a decoder frame,
# not "this file is not UTF-8".
# --------------------------------------------------------------------------

#: cp1252 bytes for `Rencana: "café" — naïve`. Byte 0x97 (an em dash in cp1252)
#: is not a valid UTF-8 continuation, so this decodes cleanly in the encoding
#: Notepad and Word produce and raises in the one `submit` requires.
CP1252_SPEC = "Rencana: “café” — naïve\n".encode("cp1252")


def _refusing_handler(called: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"id": PLAN_ID, "status": "pending"})

    return handler


def test_a_non_utf8_spec_file_is_reported_not_raised(monkeypatch, tmp_path) -> None:
    """The message must name UTF-8, because that is the whole remedy."""
    called = {"n": 0}
    _patch_client(monkeypatch, _refusing_handler(called))
    spec_path = tmp_path / "spec.md"
    spec_path.write_bytes(CP1252_SPEC)

    result = runner.invoke(app, ["submit", PROJECT_ID, "--file", str(spec_path)])

    # A SystemExit is the CLI reporting; anything else is the traceback.
    assert isinstance(result.exception, SystemExit)
    assert result.exit_code != 0
    out = flat(result)
    assert "UTF-8" in out
    assert str(spec_path) in out
    assert called["n"] == 0


def test_a_non_utf8_stdin_spec_is_reported_not_raised(monkeypatch) -> None:
    """`--file -` decodes stdin explicitly too, and had the same hole.

    A piped spec is the documented form for anything long
    (`cat spec.md | praxis submit <id> --file -`), so the file that cannot be
    passed as an argument is exactly the file most likely to reach this path.
    """
    called = {"n": 0}
    _patch_client(monkeypatch, _refusing_handler(called))

    result = runner.invoke(
        app, ["submit", PROJECT_ID, "--file", "-"], input=CP1252_SPEC
    )

    assert isinstance(result.exception, SystemExit)
    assert result.exit_code != 0
    assert "UTF-8" in flat(result)
    assert called["n"] == 0
