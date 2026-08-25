"""Detect Git Bash's MSYS path mangling on a `repo_url`/path argument.

On Windows, Git Bash rewrites a leading `/` argument into an MSYS path before
`praxis` ever runs: `praxis add-project ... /run/desktop/mnt/host/c/...`
arrives here as `C:/Program Files/Git/run/desktop/mnt/host/c/...`, which then
422s with a confusing "path does not exist" that names nothing about the
shell that caused it.
"""

from __future__ import annotations

import httpx
import pytest
from typer.testing import CliRunner

from cli.main import _looks_msys_mangled, app
from tests.cli_text import flat


runner = CliRunner()


# --------------------------------------------------------------------------
# The pure helper: testable without a subprocess.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "C:/Program Files/Git/run/desktop/mnt/host/c/repo",
        "C:\\Program Files\\Git\\run\\desktop\\mnt\\host\\c\\repo",
        "c:/program files/git/run/desktop/mnt/host/c/repo",  # case-insensitive
    ],
)
def test_matches_both_slash_styles_case_insensitively(value: str) -> None:
    assert _looks_msys_mangled(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "https://github.com/adiatmaja/praxis",
        "/run/desktop/mnt/host/c/repo",  # the un-mangled value, if it arrived
        "C:/Users/me/repo",
        "C:/Program Files/Other/repo",
        "",
    ],
)
def test_does_not_match_a_normal_path(value: str) -> None:
    assert _looks_msys_mangled(value) is False


# --------------------------------------------------------------------------
# Wired into `add-project`'s failure path only.
# --------------------------------------------------------------------------


def _patch_client(monkeypatch, status: int, body: dict) -> None:
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "t")
    monkeypatch.setenv("COLUMNS", "80")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    monkeypatch.setattr(
        "cli.main._client",
        lambda: httpx.Client(
            base_url="http://x",
            headers={"Authorization": "Bearer t"},
            transport=httpx.MockTransport(handler),
        ),
    )


@pytest.mark.unit
def test_add_project_names_the_shell_rewrite_on_a_mangled_path(monkeypatch) -> None:
    _patch_client(monkeypatch, 422, {"detail": "path does not exist"})

    result = runner.invoke(
        app,
        [
            "add-project",
            "demo",
            "C:/Program Files/Git/run/desktop/mnt/host/c/repo",
        ],
    )

    assert result.exit_code != 0
    output = flat(result)
    assert "shell rewrote this path" in output
    assert "MSYS_NO_PATHCONV=1" in output
    assert "path does not exist" in output
    # Remedy LAST, the same standard `praxis doctor` holds every diagnostic
    # to: the server's own error is the conclusion, the hint is the fix, and
    # printing the hint first would scroll it off above the error an
    # operator actually reads last.
    assert output.index("path does not exist") < output.index("shell rewrote this path")


@pytest.mark.unit
def test_add_project_stays_quiet_on_a_correct_path_that_still_fails(
    monkeypatch,
) -> None:
    """A real 422 for an unrelated reason must not print an invented cause."""
    _patch_client(monkeypatch, 422, {"detail": "repo already registered"})

    result = runner.invoke(
        app, ["add-project", "demo", "https://github.com/adiatmaja/praxis"]
    )

    assert result.exit_code != 0
    output = flat(result)
    assert "shell rewrote this path" not in output
    assert "repo already registered" in output


@pytest.mark.unit
def test_add_project_stays_quiet_on_a_mangled_looking_path_that_succeeds(
    monkeypatch,
) -> None:
    """Called from the FAILURE path only: success never sees the message."""
    _patch_client(monkeypatch, 200, {"id": "p1"})

    result = runner.invoke(
        app,
        [
            "add-project",
            "demo",
            "C:/Program Files/Git/run/desktop/mnt/host/c/repo",
        ],
    )

    assert result.exit_code == 0
    assert "shell rewrote this path" not in flat(result)
