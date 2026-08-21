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
