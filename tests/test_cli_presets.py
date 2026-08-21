"""`praxis presets` must name the worker preset values `--preset <name>` takes.

Nothing in the CLI listed them before: not `praxis config show`, not any
other verb. The values come from the real surface (`GET /api/settings/presets`,
falling back to the local `config/praxis.yaml` a newcomer already has before
the orchestrator is even running), never a hardcoded copy that can drift from
it.

Every test in this file runs at COLUMNS=80: a table column is a MAXIMUM in
rich, not a minimum, and folding a preset name across rows makes it
uncopyable. The copyable command line must appear on one contiguous line
below the table, exactly like `praxis pending` and `praxis tasks`.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from cli import main as cli_main
from cli.main import app


runner = CliRunner()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Start every test from an empty environment, a cold cache, and a
    working directory with no ambient `.env` or `config/praxis.yaml` to leak
    real repo state into a test asserting on canned data."""
    for name in ("AUTH_TOKEN", "ORCHESTRATOR_TOKEN", "ORCHESTRATOR_URL", "COLUMNS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.chdir(tmp_path)
    cli_main._env_file_values.cache_clear()
    yield
    cli_main._env_file_values.cache_clear()


def _patch_client(monkeypatch, handler) -> None:
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "t")
    monkeypatch.setattr(
        "cli.main._client",
        lambda _timeout=None: httpx.Client(
            base_url="http://x",
            headers={"Authorization": "Bearer t"},
            transport=httpx.MockTransport(handler),
        ),
    )


def test_presets_lists_live_data_with_a_contiguous_copyable_line(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/settings/presets"
        return httpx.Response(
            200,
            json={
                "presets": [
                    {
                        "name": "gemini-agy",
                        "label": "Gemini via the agy harness",
                        "harness": "agy",
                        "model": "Gemini 3.7 Flash (High)",
                        "endpoint": "",
                        "requires": ["interactive_login"],
                        "default": True,
                    },
                    {
                        "name": "local-lmstudio",
                        "label": "Local GPU via LM Studio",
                        "harness": "opencode",
                        "model": "qwen3.8-27b",
                        "endpoint": "http://host.docker.internal:1234",
                        "requires": [],
                        "default": False,
                    },
                ]
            },
        )

    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["presets"])

    assert result.exit_code == 0, result.stdout
    assert "gemini-agy" in result.stdout
    assert "local-lmstudio" in result.stdout
    # The copyable command must be a single, contiguous line -- not folded
    # across rich's column wrapping.
    #
    # `gemini-agy` needs `--accept-preset-requirements` and `local-lmstudio`
    # does not, and that is the whole point: `praxis init --non-interactive`
    # REFUSES a preset with unmet requirements, so the bare command this test
    # used to pin was guaranteed to exit 1 for two of the three shipped
    # presets, after writing a partial `.env` on the way out. A copyable line
    # that cannot run is worse than no line, because it looks like the
    # supported path.
    lines = result.stdout.splitlines()
    assert any(
        line.strip()
        == (
            "praxis init --non-interactive --preset gemini-agy "
            "--accept-preset-requirements"
        )
        for line in lines
    ), result.stdout
    assert any(
        line.strip() == "praxis init --non-interactive --preset local-lmstudio"
        for line in lines
    ), result.stdout


def test_presets_falls_back_to_local_config_when_orchestrator_unreachable(
    monkeypatch, tmp_path
) -> None:
    """A newcomer running this before `docker compose up` has no server yet,
    which is exactly when they need the names -- so a connection failure
    must read the local config instead of printing a connection error."""
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "t")

    # Simulate "orchestrator down" via a transport that refuses the
    # connection, so `_live_presets()` must catch it and return None rather
    # than letting it propagate.
    def handler(request: httpx.Request) -> httpx.Response:
        message = "connection refused"
        raise httpx.ConnectError(message, request=request)

    monkeypatch.setattr(
        "cli.main._client",
        lambda _timeout=None: httpx.Client(
            base_url="http://x",
            headers={"Authorization": "Bearer t"},
            transport=httpx.MockTransport(handler),
        ),
    )

    config_path = tmp_path / "praxis.yaml"
    config_path.write_text(
        "worker_presets:\n"
        "  - name: only-local-preset\n"
        "    label: Only Local\n"
        "    harness: opencode\n"
        "    model: some-model\n"
        "    endpoint: ''\n"
        "    requires: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PRAXIS_CONFIG_PATH", str(config_path))

    result = runner.invoke(app, ["presets"])

    assert result.exit_code == 0, result.stdout
    assert "only-local-preset" in result.stdout
    assert any(
        line.strip() == "praxis init --non-interactive --preset only-local-preset"
        for line in result.stdout.splitlines()
    ), result.stdout


def test_presets_falls_back_to_local_config_when_no_token_yet(
    monkeypatch, tmp_path
) -> None:
    """A newcomer who has not run `praxis init` yet has no token at all;
    that must not surface `_auth_token()`'s own error message, since the
    whole point of this command is to be usable before setup."""
    config_path = tmp_path / "praxis.yaml"
    config_path.write_text(
        "worker_presets:\n"
        "  - name: pre-init-preset\n"
        "    label: Pre Init\n"
        "    harness: opencode\n"
        "    model: some-model\n"
        "    endpoint: ''\n"
        "    requires: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PRAXIS_CONFIG_PATH", str(config_path))

    result = runner.invoke(app, ["presets"])

    assert result.exit_code == 0, result.stdout
    assert "No auth token found" not in result.stdout
    assert "pre-init-preset" in result.stdout
