"""init is idempotent, writes a valid .env, and never overwrites blindly.

The subject here is a merge, not a render.  `.env` is the operator's file;
`praxis init` is a guest in it.  Every assertion below is a clause of that
contract, and every clause fails silently when broken: nothing raises, init
still prints "Praxis is running", and the damage surfaces weeks later as a
reverted timezone or a worker model truncated at its first space.
"""

import io
import json
import tomllib
from pathlib import Path

import pytest
import typer
from dotenv import dotenv_values
from rich.console import Console

from cli import init as init_mod
from cli.init import (
    _FALLBACK_PRESET,
    MANAGED_KEYS,
    _managed_values,
    _render_value,
    build_env_file,
    cli_env_exports,
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
def test_merge_env_leaves_an_existing_value_alone_when_there_is_no_opinion():
    """None means "the operator declined to say", never "delete".

    The GitHub prompt defaults to 'skip', so an operator re-running init and
    holding Enter would otherwise silently delete a working GITHUB_TOKEN.
    """
    merged = merge_env("GITHUB_TOKEN=ghp_real\n", {"GITHUB_TOKEN": None})
    assert "GITHUB_TOKEN=ghp_real" in merged


@pytest.mark.unit
def test_merge_env_removes_a_key_whose_new_value_is_authored_empty():
    """An empty string is a value the PRESET wrote, and it has to be able to win.

    Sharing "no opinion" with "empty" leaves no way to clear a preset-derived
    key at all, which is what strands a stale endpoint behind a switch.
    """
    merged = merge_env("LM_STUDIO_URL=https://api.z.ai/v1\n", {"LM_STUDIO_URL": ""})
    assert "LM_STUDIO_URL" not in merged


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
    """Secondary to the python-dotenv round trip below, which is the guarantee.

    On its own this proves only that `_render_value` and `parse_env` agree
    with each other, which is worth nothing if they agree on something no real
    consumer does.  It is kept because it is free, not because it is the test.
    """
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


# --------------------------------------------------------------------------
# Graded against python-dotenv, not against this module's own parser.
#
# python-dotenv is what pydantic-settings reads `.env` with, and `docker
# compose` agrees with it on every shape below.  A round trip validated
# through `parse_env` proves internal consistency and nothing else: the two
# halves can agree perfectly on a string neither real consumer would ever
# produce.
# --------------------------------------------------------------------------


def _dotenv(text: str) -> dict[str, str]:
    """Parse ``text`` with the real python-dotenv, dropping valueless keys.

    A bare ``FOO`` line binds to None there and is absent from `parse_env`'s
    ``dict[str, str]`` entirely; that difference is a return type, not a
    parse, so it is normalized away rather than asserted on.
    """
    return {
        key: value
        for key, value in dotenv_values(stream=io.StringIO(text)).items()
        if value is not None
    }


#: One entry per value class the operator can realistically type.  Empty is
#: covered separately: `build_env_file` omits it, so it has no round trip.
ROUND_TRIP_VALUES = [
    pytest.param("Gemini 3.6 Flash (High)", id="space"),
    pytest.param("tok#nothashcomment", id="hash"),
    pytest.param("s3cret   # looks like a comment", id="hash-after-space"),
    pytest.param('say "hi"', id="double-quote"),
    pytest.param("it's fine", id="single-quote"),
    pytest.param("back\\slash", id="backslash"),
    pytest.param("C:\\Users\\new\\tmp", id="windows-path"),
    pytest.param("line\\nbreak", id="literal-backslash-n"),
    pytest.param("   ", id="whitespace-only"),
]


@pytest.mark.unit
@pytest.mark.parametrize("value", ROUND_TRIP_VALUES)
def test_build_env_file_round_trips_through_python_dotenv(value):
    """What `init` writes must read back identically for its real consumers.

    `C:\\Users\\new\\tmp` is the case that shows why: written into double
    quotes without doubling the backslashes, python-dotenv decodes `\\n` and
    `\\t` as escapes and hands the container a path containing a newline and
    a tab, with nothing anywhere reporting a problem.
    """
    text = build_env_file({"DEFAULT_WORKER_MODEL": value})
    assert _dotenv(text) == {"DEFAULT_WORKER_MODEL": value}


@pytest.mark.unit
def test_an_empty_value_produces_no_binding_at_all_for_python_dotenv():
    """`GITHUB_TOKEN=` would look configured to a reader and be useless."""
    text = build_env_file({"AUTH_TOKEN": "t", "GITHUB_TOKEN": ""})
    assert _dotenv(text) == {"AUTH_TOKEN": "t"}


@pytest.mark.unit
def test_a_backslash_forces_the_quoted_escaped_form():
    """Quoting is what makes the escaping reachable at all.

    A value whose only special character is a backslash is otherwise rendered
    raw, and then `_render_value`'s doubling never runs.  Pinned as a
    rendering contract because the semantic damage only appears once some
    other character (a space, a `#`) drags the value into quotes.
    """
    assert _render_value("C:\\Users\\new\\tmp") == '"C:\\\\Users\\\\new\\\\tmp"'


@pytest.mark.unit
def test_parse_env_strips_an_inline_comment_the_way_every_real_parser_does():
    """The reported corruption, in one line.

    Kept in the value, this token is written straight back by `merge_env`, so
    the effective AUTH_TOKEN changes and every configured MCP client 401s.
    Nothing surfaces it: init runs the doctor with the same corrupted string
    and the container reads the same corrupted string, so the row is green.
    """
    parsed = parse_env("AUTH_TOKEN=s3cret   # the one my MCP clients use\n")
    assert parsed["AUTH_TOKEN"] == "s3cret"


@pytest.mark.unit
def test_parse_env_keeps_a_hash_that_is_not_a_comment():
    """Over-stripping is the same bug facing the other way.

    python-dotenv starts a comment only at a `#` preceded by whitespace, so a
    token containing a `#` survives; truncating there would corrupt a
    perfectly valid credential just as silently.
    """
    assert parse_env("AUTH_TOKEN=s3c#ret\n")["AUTH_TOKEN"] == "s3c#ret"
    assert parse_env('AUTH_TOKEN="s3c#ret"  # mine\n')["AUTH_TOKEN"] == "s3c#ret"


@pytest.mark.unit
def test_parse_env_drops_the_export_prefix_from_the_key():
    """`export AUTH_TOKEN=tok` binds AUTH_TOKEN for dotenv and for compose."""
    assert parse_env("export AUTH_TOKEN=tok\n") == {"AUTH_TOKEN": "tok"}


#: Line shapes an operator's real `.env` can contain, including the ones
#: python-dotenv rejects outright.  Divergence on any of them is a value the
#: running product reads differently than `init` does.
DOTENV_LINES = [
    "AUTH_TOKEN=s3cret   # the one my MCP clients use",
    "AUTH_TOKEN=s3cret#nothashcomment",
    'AUTH_TOKEN="s3c#ret"  # trailing comment',
    "AUTH_TOKEN='s3c#ret'",
    'AUTH_TOKEN="v"# glued comment',
    "export AUTH_TOKEN=tok",
    "export AUTH_TOKEN='tok tok'",
    "  export PORT=12323",
    'K="a\\"b"',
    "K='a\\'b'",
    'K="C:\\\\Users\\\\new\\\\tmp"',
    'K="line\\nbreak"',
    'K="  "',
    "K=",
    'K=""',
    "K=   ",
    "K=v # c",
    "K=v\t# c",
    "K=#hash",
    "K=v  ",
    "K = spaced out",
    "# just a comment",
    "",
    'K="unterminated',
    'K="v" junk',
]


@pytest.mark.unit
@pytest.mark.parametrize("line", DOTENV_LINES)
def test_parse_env_agrees_with_python_dotenv(line):
    """Differential, not illustrative: the oracle is the real parser."""
    text = line + "\n"
    assert parse_env(text) == _dotenv(text)


@pytest.mark.unit
def test_merge_env_replaces_an_exported_key_in_place_keeping_the_prefix():
    """`export AUTH_TOKEN=` is valid for dotenv and compose alike.

    Parsed as the key `export AUTH_TOKEN`, it matches nothing, so a second
    `AUTH_TOKEN=` line is appended.  Last-wins saves the behavior, but the
    file permanently lies to whoever reads it.
    """
    merged = merge_env("export AUTH_TOKEN=tok\nPORT=7777\n", {"AUTH_TOKEN": "NEW"})
    assert merged.splitlines() == ["export AUTH_TOKEN=NEW", "PORT=7777"]


@pytest.mark.unit
def test_merge_env_keeps_an_inline_comment_on_a_line_it_replaces():
    """The header `build_env_file` writes promises every comment survives.

    An inline comment on a managed key is a comment; silently deleting it
    while claiming otherwise is how an operator learns not to trust the file.
    """
    merged = merge_env(
        "AUTH_TOKEN=old   # the one my MCP clients use\n", {"AUTH_TOKEN": "new"}
    )
    assert merged.splitlines() == ["AUTH_TOKEN=new   # the one my MCP clients use"]
    assert _dotenv(merged) == {"AUTH_TOKEN": "new"}


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


@pytest.mark.unit
def test_the_printed_cli_exports_are_what_the_cli_actually_reads(monkeypatch):
    """Fed to `cli.main`'s own readers, not compared to a literal.

    The MCP half of this block got a consumer-driven test and the CLI half
    got two hardcoded strings.  It is the same defect either way: one side
    renames, the printed instructions silently become wrong, and the operator
    concludes the install is broken because `praxis doctor` reports a running
    orchestrator unreachable.
    """
    from cli import main as cli_main

    exports = cli_env_exports("http://127.0.0.1:9999", "tok")
    for key in ("ORCHESTRATOR_URL", "ORCHESTRATOR_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    for key, value in exports.items():
        monkeypatch.setenv(key, value)

    assert cli_main._api_url() == "http://127.0.0.1:9999"
    assert cli_main._auth_token() == "tok"


@pytest.mark.unit
def test_the_next_steps_block_prints_the_exports_it_documents(monkeypatch):
    """The instructions and the tested mapping must be one source, not two."""
    buffer = io.StringIO()
    monkeypatch.setattr(
        init_mod, "console", Console(file=buffer, width=200, no_color=True)
    )

    init_mod._print_next_steps("http://127.0.0.1:9999", "tok", _FALLBACK_PRESET)

    printed = buffer.getvalue()
    for name, value in cli_env_exports("http://127.0.0.1:9999", "tok").items():
        assert f"{name}={value}" in printed


# --------------------------------------------------------------------------
# The prompt paths: what holding Enter produces, and what `init` writes.
# --------------------------------------------------------------------------


def _hold_enter(monkeypatch):
    """Answer every prompt the way holding Enter does: take the default."""

    class _Default:
        @staticmethod
        def ask(*_args, **kwargs):
            return kwargs.get("default")

    for name in ("Confirm", "IntPrompt", "Prompt"):
        monkeypatch.setattr(init_mod, name, _Default)


def _stub_the_world(monkeypatch):
    """Replace everything `init` does outside the working directory.

    Without this an `init()` under test shells out to a real `docker compose
    build`, so the stubs are what make the entry point testable at all rather
    than a convenience.

    Returns:
        The recorded `docker compose` argument lists, in call order.
    """
    compose_calls: list[list[str]] = []

    def _fake_compose(args, _what):
        compose_calls.append(args)

    monkeypatch.setattr(init_mod, "_compose", _fake_compose)
    monkeypatch.setattr(init_mod, "_wait_for_health", lambda _url, _timeout_s=180: True)
    monkeypatch.setattr(init_mod, "_run_doctor", lambda _url, _token: 0)
    return compose_calls


@pytest.fixture
def fake_root(tmp_path, monkeypatch):
    """A directory that satisfies init's repo-root guard, chdir'd into.

    Real marker files rather than a monkeypatched guard: stubbing the guard
    out is how a test stays green after the guard stops working, and this
    fixture exists precisely so the guard runs for real in every `init()` test
    below.  `monkeypatch.chdir` is also what keeps those tests off the
    repository's own `.env`, which they must never touch.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "praxis"\n', encoding="utf-8"
    )
    (tmp_path / ".env.example").write_text("AUTH_TOKEN=\n", encoding="utf-8")
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.mark.unit
def test_holding_enter_chooses_a_preset_init_can_actually_satisfy(monkeypatch):
    """Run against the real config/praxis.yaml, because that is what ships.

    The shipped default was `hosted-openweight`, whose `requires: [api_key]`
    init never collects and Settings has no field for.  LM_STUDIO_URL IS
    forwarded into the container, so holding Enter repointed every `local`
    router call-site at an endpoint that rejects it.  A yellow note does not
    help: the whole premise of the default path is that nobody reads.
    """
    _hold_enter(monkeypatch)
    chosen = init_mod._choose_preset(init_mod._fetch_presets_or_defaults())
    assert chosen["requires"] == []


@pytest.mark.unit
def test_holding_enter_refuses_a_preset_whose_requirement_init_cannot_collect(
    monkeypatch,
):
    """When nothing on the menu is satisfiable, the default path must stop.

    Falling through would hand back a configuration that cannot work, which
    is the failure this whole fix is about.
    """
    _hold_enter(monkeypatch)
    unsatisfiable = [
        {
            "name": "hosted-openweight",
            "label": "Hosted open-weight model",
            "harness": "opencode",
            "model": "glm-4.7",
            "endpoint": "https://api.z.ai/v1",
            "requires": ["api_key"],
        }
    ]
    with pytest.raises(typer.Exit) as exit_info:
        init_mod._choose_preset(unsatisfiable)
    assert exit_info.value.exit_code == 1


@pytest.mark.unit
def test_the_managed_key_set_is_exactly_the_one_init_writes():
    """Shrinking MANAGED_KEYS broke `init` at runtime with 20/20 green.

    `test_merge_env_refuses_a_key_it_does_not_manage` pins only the WIDENING
    direction.  Narrowing is the one that breaks the product: `init` still
    builds all six values, `merge_env` rejects the set, and it raises after
    every prompt has already been answered.  Derived from the builder rather
    than restated, so the two cannot drift.
    """
    built = _managed_values(
        token="t", gh_token=None, port="12323", preset=_FALLBACK_PRESET
    )
    assert set(built) == set(MANAGED_KEYS)


@pytest.mark.unit
def test_switching_preset_replaces_every_preset_derived_key(monkeypatch):
    """A half-applied switch is a config the operator believes they replaced.

    `gemini-agy` has `endpoint: ""`, so a shared "empty means keep" rule
    leaves `LM_STUDIO_URL=https://api.z.ai/v1` sitting next to `agy` forever,
    with no answer to any prompt that can clear it.
    """
    existing = (
        "AUTH_TOKEN=tok\n"
        "LM_STUDIO_URL=https://api.z.ai/v1\n"
        "DEFAULT_WORKER_HARNESS=opencode\n"
        "DEFAULT_WORKER_MODEL=glm-4.7\n"
    )
    gemini = {
        "name": "gemini-agy",
        "label": "Gemini via the agy harness",
        "harness": "agy",
        "model": "Gemini 3.6 Flash (High)",
        "endpoint": "",
        "requires": ["interactive_login"],
    }
    merged = merge_env(
        existing,
        _managed_values(token="tok", gh_token=None, port="12323", preset=gemini),
    )

    assert "LM_STUDIO_URL" not in merged
    assert "api.z.ai" not in merged
    assert "DEFAULT_WORKER_HARNESS=agy" in merged
    assert _dotenv(merged)["DEFAULT_WORKER_MODEL"] == "Gemini 3.6 Flash (High)"


@pytest.mark.unit
def test_the_real_repo_root_satisfies_the_guard():
    """The guard's markers must describe THIS repo, not an idea of one.

    Asserted against the real checkout so a marker that drifts (a renamed
    `.env.example`, a moved compose file) fails here rather than by locking
    every operator out of the one command that sets Praxis up.
    """
    assert init_mod.repo_root_problem(REPO) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("missing", "expected"),
    [
        pytest.param("pyproject.toml", "pyproject.toml", id="no-pyproject"),
        pytest.param(".env.example", ".env.example", id="no-env-example"),
        pytest.param("docker-compose.yml", "docker-compose.yml", id="no-compose"),
    ],
)
def test_a_directory_missing_any_marker_is_not_the_root(fake_root, missing, expected):
    """Every marker is load-bearing; any single one alone is not enough.

    `pyproject.toml` on its own would accept any Python project, and a lone
    `.env.example` would accept a sibling checkout, which is exactly the kind
    of near-miss directory an operator actually runs this from.
    """
    (fake_root / missing).unlink()
    problem = init_mod.repo_root_problem(fake_root)
    assert problem is not None
    assert expected in problem


@pytest.mark.unit
def test_another_projects_checkout_is_not_the_root(fake_root):
    """The markers are generic; the project NAME is what makes them specific.

    A sibling repo of this shape would otherwise pass, and init would write a
    live AUTH_TOKEN into it and then configure nothing.
    """
    (fake_root / "pyproject.toml").write_text(
        '[project]\nname = "not-praxis"\n', encoding="utf-8"
    )
    problem = init_mod.repo_root_problem(fake_root)
    assert problem is not None
    assert "not-praxis" in problem


@pytest.mark.unit
def test_a_malformed_pyproject_is_not_the_root(fake_root):
    """Unreadable is not a pass. Failing open here defeats the whole guard."""
    (fake_root / "pyproject.toml").write_text("[project\nname =", encoding="utf-8")
    assert init_mod.repo_root_problem(fake_root) is not None


@pytest.mark.unit
def test_init_refuses_to_run_outside_the_repo_root(tmp_path, monkeypatch):
    """The reported defect: a live AUTH_TOKEN written wherever you happened to be.

    `init` is CWD-relative throughout (`.env`, `docker compose`,
    `config/praxis.yaml`), so run elsewhere it writes a secret into an
    unrelated directory before anything can fail.  The refusal has to come
    before the first write, which is why this asserts on the absent file and
    not only on the exit code.
    """
    monkeypatch.chdir(tmp_path)
    _hold_enter(monkeypatch)
    _stub_the_world(monkeypatch)

    with pytest.raises(typer.Exit) as exit_info:
        init_mod.init()

    assert exit_info.value.exit_code == 1
    assert not (tmp_path / ".env").exists()


@pytest.mark.unit
def test_the_refusal_names_what_it_looked_for_and_where_it_looked(
    tmp_path, monkeypatch
):
    """A bare "wrong directory" leaves the operator guessing which one is right."""
    buffer = io.StringIO()
    monkeypatch.setattr(
        init_mod, "console", Console(file=buffer, width=200, no_color=True)
    )
    monkeypatch.chdir(tmp_path)
    _hold_enter(monkeypatch)
    _stub_the_world(monkeypatch)

    with pytest.raises(typer.Exit):
        init_mod.init()

    printed = buffer.getvalue()
    assert str(tmp_path) in printed
    for marker in init_mod._ROOT_MARKERS:
        assert marker in printed


@pytest.mark.unit
def test_a_blank_github_answer_still_keeps_the_existing_credential(monkeypatch):
    """The other half of the same distinction, wired end to end.

    The prompt tells the operator that blank keeps the current token, so this
    goes through `_resolve_github_token` rather than asserting on merge_env
    directly: separating "" from None must not quietly break the promise the
    prompt makes.
    """
    _hold_enter(monkeypatch)
    answer = init_mod._resolve_github_token({"GITHUB_TOKEN": "ghp_real"})
    merged = merge_env(
        "GITHUB_TOKEN=ghp_real\n",
        _managed_values(
            token="t", gh_token=answer, port="12323", preset=_FALLBACK_PRESET
        ),
    )
    assert _dotenv(merged)["GITHUB_TOKEN"] == "ghp_real"
