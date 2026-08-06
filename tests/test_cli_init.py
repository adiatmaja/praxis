"""init is idempotent, writes a valid .env, and never overwrites blindly.

The subject here is a merge, not a render.  `.env` is the operator's file;
`praxis init` is a guest in it.  Every assertion below is a clause of that
contract, and every clause fails silently when broken: nothing raises, init
still prints "Praxis is running", and the damage surfaces weeks later as a
reverted timezone or a worker model truncated at its first space.
"""

import json
import tomllib
from pathlib import Path

import pytest

from cli.init import (
    _render_value,
    build_env_file,
    generate_token,
    mcp_snippet,
    merge_env,
    parse_env,
)


REPO = Path(__file__).resolve().parents[1]

# Shaped like this repo's real .env: unmanaged keys, a comment, a blank line.
REAL_SHAPED_ENV = (
    "AUTH_TOKEN=old-token\n"
    "DATABASE_URL=sqlite+aiosqlite:///data/orchestrator.db\n"
    "HOST=0.0.0.0\n"
    "PORT=12323\n"
    "\n"
    "# Docker volume holding agy (Gemini) OAuth creds.\n"
    "GEMINI_CREDS_VOLUME=praxis-gemini-creds\n"
)


@pytest.mark.unit
def test_a_generated_token_is_long_enough_to_be_a_secret():
    token = generate_token()
    assert len(token) >= 32
    assert token != generate_token()


@pytest.mark.unit
def test_build_env_file_contains_the_two_required_values():
    text = build_env_file({"AUTH_TOKEN": "t", "GITHUB_TOKEN": "g"})
    assert "AUTH_TOKEN=t" in text
    assert "GITHUB_TOKEN=g" in text


@pytest.mark.unit
def test_build_env_file_quotes_a_value_with_spaces():
    """Unquoted, compose and pydantic-settings both truncate at the space.

    `DEFAULT_WORKER_MODEL=Gemini 3.6 Flash (High)` silently becomes a worker
    model of `Gemini`, which is not a model anyone has.
    """
    text = build_env_file({"DEFAULT_WORKER_MODEL": "Gemini 3.6 Flash (High)"})
    assert 'DEFAULT_WORKER_MODEL="Gemini 3.6 Flash (High)"' in text


@pytest.mark.unit
def test_a_value_containing_a_quote_is_escaped_not_just_wrapped():
    """Wrapping `a"b` in double quotes without escaping produces `KEY="a"b"`.

    That parses as the value `a`, so the tail is lost with no error anywhere.
    """
    rendered = _render_value('say "hi"')
    assert rendered == '"say \\"hi\\""'
    assert parse_env(f"K={rendered}\n")["K"] == 'say "hi"'


@pytest.mark.unit
def test_local_mode_writes_no_github_token():
    """The key is PRESENT and empty, which is what `init` actually passes.

    Omitting it from the dict instead makes this assertion vacuous: it then
    holds no matter what `build_env_file` does with an empty value, and
    `GITHUB_TOKEN=` (configured-looking but useless) ships unnoticed.
    """
    text = build_env_file({"AUTH_TOKEN": "t", "GITHUB_TOKEN": ""})
    assert "GITHUB_TOKEN" not in text


@pytest.mark.unit
def test_merge_env_preserves_unrelated_existing_keys():
    """Re-running init must not blow away an operator's other settings."""
    existing = "TZ=Asia/Jakarta\nAUTH_TOKEN=old\n"
    merged = merge_env(existing, {"AUTH_TOKEN": "new"})
    assert "TZ=Asia/Jakarta" in merged
    assert "AUTH_TOKEN=new" in merged
    assert "AUTH_TOKEN=old" not in merged


@pytest.mark.unit
def test_merge_env_preserves_comments():
    existing = "# my notes\nTZ=UTC\n"
    assert "# my notes" in merge_env(existing, {"AUTH_TOKEN": "t"})


@pytest.mark.unit
def test_merge_env_preserves_every_line_of_a_real_shaped_env():
    """The synthetic one-key fixtures above under-test the real failure.

    A real `.env` has unmanaged keys, a comment, and a blank line, and the way
    this breaks in practice is that one of those categories is dropped while
    the narrow fixtures stay green.
    """
    merged = merge_env(REAL_SHAPED_ENV, {"AUTH_TOKEN": "new-token", "PORT": "12323"})
    for line in REAL_SHAPED_ENV.splitlines():
        if line.startswith("AUTH_TOKEN="):
            continue
        assert line in merged.splitlines()
    assert "AUTH_TOKEN=new-token" in merged.splitlines()


@pytest.mark.unit
def test_merge_env_keeps_the_original_line_order():
    """Replacement is IN PLACE.  A reordering merge produces a noisy diff.

    An operator who cannot eyeball the diff of their own `.env` stops reading
    it, which is how the destructive change nobody noticed gets through.
    """
    merged = merge_env(REAL_SHAPED_ENV, {"AUTH_TOKEN": "new-token"})
    keys = [ln.split("=", 1)[0] for ln in merged.splitlines() if "=" in ln]
    assert keys == ["AUTH_TOKEN", "DATABASE_URL", "HOST", "PORT", "GEMINI_CREDS_VOLUME"]


@pytest.mark.unit
def test_merge_env_is_idempotent():
    once = merge_env("", {"AUTH_TOKEN": "t"})
    twice = merge_env(once, {"AUTH_TOKEN": "t"})
    assert once == twice


@pytest.mark.unit
def test_merge_env_never_writes_a_managed_key_twice():
    """Appending instead of replacing accumulates duplicates across re-runs.

    The last one wins, so the file the operator reads and the config the
    process loads disagree, with nothing in either to show why.
    """
    merged = merge_env(REAL_SHAPED_ENV, {"AUTH_TOKEN": "new-token"})
    assert [ln for ln in merged.splitlines() if ln.startswith("AUTH_TOKEN=")] == [
        "AUTH_TOKEN=new-token"
    ]


@pytest.mark.unit
def test_merge_env_appends_a_managed_key_that_is_absent():
    merged = merge_env("TZ=UTC\n", {"PORT": "9999"})
    assert merged.splitlines() == ["TZ=UTC", "PORT=9999"]


@pytest.mark.unit
def test_merge_env_leaves_an_existing_value_alone_when_the_new_one_is_empty():
    """Empty means "no opinion", never "delete".

    The GitHub prompt defaults to 'skip', so an operator re-running init and
    holding Enter would otherwise silently delete a working GITHUB_TOKEN.
    """
    merged = merge_env("GITHUB_TOKEN=ghp_real\n", {"GITHUB_TOKEN": ""})
    assert "GITHUB_TOKEN=ghp_real" in merged


@pytest.mark.unit
def test_merge_env_refuses_a_key_it_does_not_manage():
    """The invariant, enforced rather than merely documented.

    Nothing else stops a future caller from passing `TZ` in `values` and
    rewriting a key init has no business touching.
    """
    with pytest.raises(ValueError, match="TZ"):
        merge_env("TZ=UTC\n", {"TZ": "Asia/Jakarta"})


@pytest.mark.unit
def test_parse_env_round_trips_what_build_env_file_wrote():
    values = {
        "AUTH_TOKEN": "t",
        "PORT": "12323",
        "DEFAULT_WORKER_MODEL": "Gemini 3.6 Flash (High)",
    }
    assert parse_env(build_env_file(values)) == values


@pytest.mark.unit
def test_parse_env_ignores_comments_and_blank_lines():
    parsed = parse_env(REAL_SHAPED_ENV)
    assert parsed["AUTH_TOKEN"] == "old-token"
    assert parsed["GEMINI_CREDS_VOLUME"] == "praxis-gemini-creds"
    assert "#" not in "".join(parsed)


@pytest.mark.unit
def test_the_mcp_snippet_is_valid_json_naming_the_praxis_server():
    snippet = mcp_snippet(api_url="http://127.0.0.1:12323", token="tok")
    parsed = json.loads(snippet)
    assert "praxis" in parsed["mcpServers"]
    assert parsed["mcpServers"]["praxis"]["command"] == "praxis-mcp"


@pytest.mark.unit
def test_the_mcp_snippet_names_a_console_script_that_actually_exists():
    """A snippet naming a command that is not installed fails at paste time."""
    scripts = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    command = json.loads(mcp_snippet("http://x", "t"))["mcpServers"]["praxis"][
        "command"
    ]
    assert command in scripts["project"]["scripts"]


@pytest.mark.unit
def test_the_mcp_snippet_carries_the_api_url_and_token():
    snippet = mcp_snippet(api_url="http://127.0.0.1:12323", token="tok")
    assert "http://127.0.0.1:12323" in snippet
    assert "tok" in snippet


@pytest.mark.unit
def test_the_mcp_snippet_env_is_what_the_mcp_server_actually_reads(monkeypatch):
    """Fed to the real `PraxisClient.from_env`, not compared to a literal.

    A snippet with plausible-but-wrong names (`PRAXIS_API`, `PRAXIS_TOKEN`)
    is worse than no snippet: the operator pastes it and the MCP server
    silently falls back to localhost:12323 with no token.  Asserting against
    the consumer means a rename on either side breaks this test.
    """
    from mcp_server.client import PraxisClient

    env = json.loads(mcp_snippet("http://127.0.0.1:9999", "tok"))["mcpServers"][
        "praxis"
    ]["env"]
    for key in ("PRAXIS_BASE_URL", "PRAXIS_AUTH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    client = PraxisClient.from_env()
    assert client.base_url == "http://127.0.0.1:9999"
    assert client.token == "tok"
