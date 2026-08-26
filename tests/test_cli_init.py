"""init is idempotent, writes a valid .env, and never overwrites blindly.

The subject here is a merge, not a render.  `.env` is the operator's file;
`praxis init` is a guest in it.  Every assertion below is a clause of that
contract, and every clause fails silently when broken: nothing raises, init
still prints "Praxis is running", and the damage surfaces weeks later as a
reverted timezone or a worker model truncated at its first space.
"""

import contextlib
import inspect
import io
import json
import re
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
    Answers,
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


@pytest.fixture(autouse=True)
def _never_touch_the_real_env(monkeypatch):
    """A session-level net: forgetting `fake_root` must not reach the real .env.

    `init()` is CWD-relative throughout, and the repository's own root
    satisfies its own guard, so a test that forgot to chdir into `fake_root`
    would sail past `_require_repo_root` and could go on to answer "yes" to
    `Update .env?`, rewriting the operator's live file.  Every current test
    is correctly scoped (verified), but the hazard is structural, so this
    fixture stands guard for whatever test is added next.

    Cheap and inert: it never reads or writes `.env` itself, only compares
    `Path.cwd()` against the checkout root immediately before `run_init()`
    would otherwise run.

    Guards `run_init`, not `init`: `init` is a typer shim whose parameter
    defaults are `OptionInfo` objects, so it cannot be called directly at all.
    All the behaviour, and therefore all the hazard, is in `run_init`.
    """
    real_run_init = init_mod.run_init

    def _guarded_run_init(answers):
        cwd = Path.cwd().resolve()
        assert cwd != REPO.resolve(), (
            "a test is about to call run_init() from the real repo root; "
            "did it forget the `fake_root` fixture?"
        )
        return real_run_init(answers)

    monkeypatch.setattr(init_mod, "run_init", _guarded_run_init)


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
    # AUTH_TOKEN must go too: `_auth_token` prefers it over ORCHESTRATOR_TOKEN,
    # so an ambient one (CI sets AUTH_TOKEN for the whole test step) shadows the
    # export under test and the assertion reads the environment, not the mapping.
    for key in ("ORCHESTRATOR_URL", "ORCHESTRATOR_TOKEN", "AUTH_TOKEN"):
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


def _stub_the_world(monkeypatch, healthy: bool = True, doctor_code: int = 0):
    """Replace everything `init` does outside the working directory.

    Without this an `init()` under test shells out to a real `docker compose
    build`, so the stubs are what make the entry point testable at all rather
    than a convenience.

    Args:
        monkeypatch: The pytest fixture doing the patching.
        healthy: What `/health` polling reports.
        doctor_code: The doctor's verdict, which is init's own exit code.

    Returns:
        A record of what init did: "compose" argument lists in call order,
        and "doctor" (url, token) pairs.
    """
    calls: dict[str, list] = {"compose": [], "doctor": []}

    def _fake_compose(args, _what, env=None):
        calls["compose"].append(args)

    def _fake_doctor(url, token):
        calls["doctor"].append((url, token))
        return doctor_code

    monkeypatch.setattr(init_mod, "_compose", _fake_compose)
    monkeypatch.setattr(
        init_mod, "_wait_for_health", lambda _url, _timeout_s=180: healthy
    )
    monkeypatch.setattr(init_mod, "_run_doctor", _fake_doctor)
    return calls


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
def test_the_shipped_menu_offers_the_flagged_default_first(monkeypatch):
    """Run against the real config/praxis.yaml, because that is what ships.

    The shipped menu default must agree with `default_worker_*`. It did not:
    the rule picked the first preset needing no credential, which selected
    local-lmstudio (opencode/qwen3.8) while the configured default worker was
    agy/Gemini. An explicit `default: true` resolves it, since the rule cannot
    see that a one-time interactive login has already been done.
    """
    presets = init_mod._fetch_presets_or_defaults()
    # An unresolvable config falls back to `_FALLBACK_PRESET`, a single entry:
    # without this, the assertions below hold no matter what the real menu did,
    # which is exactly the vacuous pass this test is for.
    assert len(presets) > 1, "config/praxis.yaml did not resolve to a real menu"
    assert any(p.get("default") for p in presets), (
        "config/praxis.yaml flags no preset `default: true`; the menu default "
        "would silently fall back to the first credential-free preset"
    )
    index = init_mod._default_preset_index(presets)
    assert presets[index - 1]["harness"] == "agy"


@pytest.mark.unit
def test_a_flagged_default_still_challenges_an_unmet_requirement(monkeypatch):
    """The flag changes what is OFFERED, never what is CHECKED.

    Holding Enter must not silently accept a preset whose one-time setup may
    not have happened; the confirmation still defaults to no, so the operator
    who is not reading cannot answer it by holding Enter.
    """
    _hold_enter(monkeypatch)
    presets = init_mod._fetch_presets_or_defaults()
    with pytest.raises(typer.Exit):
        init_mod._choose_preset(presets, Answers())


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
        init_mod._choose_preset(unsatisfiable, Answers())
    assert exit_info.value.exit_code == 1


@pytest.mark.unit
def test_unmet_requirement_prints_the_setup_hint(capsys, monkeypatch):
    """The stop must name the remedy, not only the requirement."""
    from rich.prompt import Confirm

    from cli.init import _confirm_unmet_requirements

    monkeypatch.setattr(Confirm, "ask", lambda *_a, **_k: False)
    preset = {
        "label": "Gemini via the agy harness",
        "requires": ["interactive_login"],
        "default": True,
        "setup_doc": "docs/deployment.md#agy",
        "setup_hint": "docker run --rm -it ... 'agy login'",
    }

    with contextlib.suppress(typer.Exit):
        _confirm_unmet_requirements(preset, Answers())

    out = capsys.readouterr().out
    assert "agy login" in out
    assert "docs/deployment.md#agy" in out


@pytest.mark.unit
def test_unmet_requirement_without_hint_still_stops(capsys, monkeypatch):
    """A preset with no hint must not crash on the missing key."""
    from rich.prompt import Confirm

    from cli.init import _confirm_unmet_requirements

    monkeypatch.setattr(Confirm, "ask", lambda *_a, **_k: False)
    preset = {"label": "Some preset", "requires": ["api_key"]}

    with pytest.raises(typer.Exit):
        _confirm_unmet_requirements(preset, Answers())


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
def test_a_renamed_fork_that_keeps_the_console_script_is_the_root(fake_root):
    """A downstream fork renaming the project must not be locked out.

    Praxis is positioned as open source and provider-agnostic, so a renamed
    fork is a real user. The entry point (`cli.main:app`) is the same code
    regardless of what `[project] name` calls itself; the console script is
    the marker that actually makes this checkout runnable as `praxis init`.
    """
    (fake_root / "pyproject.toml").write_text(
        '[project]\nname = "acme-praxis-fork"\n\n'
        '[project.scripts]\npraxis = "cli.main:app"\n',
        encoding="utf-8",
    )
    assert init_mod.repo_root_problem(fake_root) is None


@pytest.mark.unit
def test_a_differently_cased_name_is_still_the_root(fake_root):
    """PEP 503 says `Praxis` and `praxis` name the same project."""
    (fake_root / "pyproject.toml").write_text(
        '[project]\nname = "Praxis"\n', encoding="utf-8"
    )
    assert init_mod.repo_root_problem(fake_root) is None


@pytest.mark.unit
def test_three_markers_present_but_genuinely_not_praxis_is_still_refused(fake_root):
    """Neither marker being satisfiable is what actually earns the refusal.

    A directory that has all three generic marker FILES but whose
    `pyproject.toml` neither names the project `praxis` (even normalized) nor
    declares a `praxis` console script must still be refused.
    """
    (fake_root / "pyproject.toml").write_text(
        '[project]\nname = "not-praxis-at-all"\n\n'
        '[project.scripts]\nsomething-else = "x:y"\n',
        encoding="utf-8",
    )
    assert init_mod.repo_root_problem(fake_root) is not None


@pytest.mark.unit
def test_a_malformed_pyproject_is_not_the_root(fake_root):
    """Unreadable is not a pass. Failing open here defeats the whole guard."""
    (fake_root / "pyproject.toml").write_text("[project\nname =", encoding="utf-8")
    assert init_mod.repo_root_problem(fake_root) is not None


@pytest.mark.unit
def test_a_pyproject_where_project_is_a_string_is_not_the_root(fake_root):
    """`[project]` parsing to a string, not a table, must not raise.

    `data.get("project", {}).get("name", "")` assumes `project` parsed to a
    table; when it did not, `.get` does not exist on a `str`, and the
    operator gets a raw traceback from the first command they ever run.
    """
    (fake_root / "pyproject.toml").write_text('project = "oops"\n', encoding="utf-8")
    assert init_mod.repo_root_problem(fake_root) is not None


@pytest.mark.unit
def test_a_pyproject_where_project_is_an_array_is_not_the_root(fake_root):
    """Same failure mode as the string case, with a list instead."""
    (fake_root / "pyproject.toml").write_text(
        'project = ["a", "b"]\n', encoding="utf-8"
    )
    assert init_mod.repo_root_problem(fake_root) is not None


@pytest.mark.unit
def test_a_pyproject_where_name_is_not_a_string_is_not_the_root(fake_root):
    """A non-string `name` must read as "no name", not crash or match."""
    (fake_root / "pyproject.toml").write_text(
        "[project]\nname = 42\n", encoding="utf-8"
    )
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
        init_mod.run_init(Answers())

    assert exit_info.value.exit_code == 1
    assert not (tmp_path / ".env").exists()


@pytest.mark.unit
def test_the_guard_runs_before_any_prompt_is_issued(tmp_path, monkeypatch):
    """The guard's whole point is a secret written before a prompt can stall it.

    Moving `_require_repo_root()` to after all four prompts still leaves the
    write refused (the test above catches that half), but an operator who
    has already answered a prompt by the time the refusal fires has been
    asked something pointless in a directory `init` was never going to use.
    """
    monkeypatch.chdir(tmp_path)
    calls: list[str] = []

    class _RecordingPrompt:
        @staticmethod
        def ask(prompt="", **kwargs):
            calls.append(str(prompt))
            return kwargs.get("default")

    for name in ("Confirm", "IntPrompt", "Prompt"):
        monkeypatch.setattr(init_mod, name, _RecordingPrompt)
    _stub_the_world(monkeypatch)

    with pytest.raises(typer.Exit) as exit_info:
        init_mod.run_init(Answers())

    assert exit_info.value.exit_code == 1
    assert calls == []


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
        init_mod.run_init(Answers())

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
    answer = init_mod._resolve_github_token({"GITHUB_TOKEN": "ghp_real"}, Answers())
    merged = merge_env(
        "GITHUB_TOKEN=ghp_real\n",
        _managed_values(
            token="t", gh_token=answer, port="12323", preset=_FALLBACK_PRESET
        ),
    )
    assert _dotenv(merged)["GITHUB_TOKEN"] == "ghp_real"


# --------------------------------------------------------------------------
# `init()` itself.
#
# Everything above this line tests a pure helper.  The entry point that
# decides what to write and reports what it did had no coverage at all, which
# is how a compose file that never forwarded the worker preset and a
# guard-less write into an unrelated directory both survived a whole phase
# with the suite green.
#
# Every test below runs inside `fake_root`, which chdirs into `tmp_path`.  The
# repository's own `.env` is never read, written or deleted.
# --------------------------------------------------------------------------

#: A two-entry menu so a test can prove the CHOICE landed, not just a default.
#: The second entry is `gemini-agy`'s real shape: a model containing spaces
#: (which must survive quoting through a real file) and no endpoint (which
#: must clear a previous preset's `LM_STUDIO_URL`).
TWO_PRESETS = [
    {
        "name": "local-lmstudio",
        "label": "Local GPU via LM Studio",
        "harness": "opencode",
        "model": "qwen3-32b",
        "endpoint": "http://host.docker.internal:1234",
        "requires": [],
    },
    {
        "name": "gemini-agy",
        "label": "Gemini via the agy harness",
        "harness": "agy",
        "model": "Gemini 3.6 Flash (High)",
        "endpoint": "",
        "requires": [],
    },
]


def _scripted(monkeypatch, answers: dict):
    """Answer init's prompts by substring match, else the default (Enter).

    Keyed on the prompt text rather than on call order, because the order
    changes with the state of `.env`: the "Reuse the AUTH_TOKEN" and
    "Update .env?" prompts only appear when a file is already there.
    """

    def _prompt_class():
        class _Scripted:
            @staticmethod
            def ask(prompt="", **kwargs):
                for needle, value in answers.items():
                    if needle in str(prompt):
                        return value
                return kwargs.get("default")

        return _Scripted

    for name in ("Confirm", "IntPrompt", "Prompt"):
        monkeypatch.setattr(init_mod, name, _prompt_class())


def _run_init(
    monkeypatch,
    answers: dict | None = None,
    presets: list | None = None,
    healthy: bool = True,
    doctor_code: int = 0,
    flags: Answers | None = None,
) -> dict:
    """Drive a full `run_init()` inside the current directory.

    Args:
        answers: Prompt text fragment -> scripted reply, for interactive runs.
        presets: The preset menu to offer.
        healthy: Whether the stubbed health check succeeds.
        doctor_code: The stubbed doctor's exit code.
        flags: Command-line answers. Defaults to none at all, which is the
            interactive path every pre-existing test in this file exercises.

    Returns:
        The `_stub_the_world` record, plus "exit_code".
    """
    _scripted(monkeypatch, answers or {})
    calls = _stub_the_world(monkeypatch, healthy=healthy, doctor_code=doctor_code)
    monkeypatch.setattr(
        init_mod,
        "_fetch_presets_or_defaults",
        lambda: [dict(p) for p in (presets or TWO_PRESETS)],
    )
    with pytest.raises(typer.Exit) as exit_info:
        init_mod.run_init(flags or Answers())
    calls["exit_code"] = exit_info.value.exit_code
    return calls


@pytest.mark.unit
def test_a_fresh_run_writes_exactly_the_managed_keys(fake_root, monkeypatch):
    """Exact equality, because both directions of drift are silent.

    A missing key leaves the container falling back to a default nobody chose;
    an extra one is `init` writing outside the contract `merge_env` enforces.
    Neither raises, and the operator is told the file was written either way.
    """
    _run_init(monkeypatch, {"Auth token": "tok", "GitHub token": "ghp_x"})

    written = _dotenv((fake_root / ".env").read_text(encoding="utf-8"))
    assert written == {
        "AUTH_TOKEN": "tok",
        "GITHUB_TOKEN": "ghp_x",
        "PORT": "12323",
        "LM_STUDIO_URL": "http://host.docker.internal:1234",
        "DEFAULT_WORKER_HARNESS": "opencode",
        "DEFAULT_WORKER_MODEL": "qwen3-32b",
    }


@pytest.mark.unit
def test_a_fresh_local_mode_run_writes_no_github_token_line(fake_root, monkeypatch):
    """'skip' has to reach the file as an absent key, not an empty one.

    `GITHUB_TOKEN=` reads as configured to anyone looking at the file and is
    useless to everything that consumes it.
    """
    _run_init(monkeypatch, {"Auth token": "tok", "GitHub token": "skip"})
    assert "GITHUB_TOKEN" not in (fake_root / ".env").read_text(encoding="utf-8")


@pytest.mark.unit
def test_a_re_run_does_not_rotate_an_existing_auth_token(fake_root, monkeypatch):
    """Holding Enter through a re-run must not invalidate every MCP client.

    Rotating it is silent from init's side: it writes the new token, runs the
    doctor with the new token, and reports green.  The breakage surfaces in
    every already-configured client instead, as a 401 with no explanation.
    """
    (fake_root / ".env").write_text("AUTH_TOKEN=do-not-rotate\n", encoding="utf-8")

    _run_init(monkeypatch)

    written = _dotenv((fake_root / ".env").read_text(encoding="utf-8"))
    assert written["AUTH_TOKEN"] == "do-not-rotate"


@pytest.mark.unit
def test_a_re_run_preserves_an_unmanaged_key_its_position_and_its_comment(
    fake_root, monkeypatch
):
    """The whole `.env` merge contract, asserted through the real entry point.

    `merge_env` is tested directly above; this proves `init` actually routes
    an existing file through it rather than through `build_env_file`, which
    would silently discard every unmanaged key on the first re-run.
    """
    (fake_root / ".env").write_text(
        "TZ=Asia/Jakarta   # my zone\n"
        "AUTH_TOKEN=existing-token\n"
        "\n"
        "# volume notes\n"
        "GEMINI_CREDS_VOLUME=praxis-gemini-creds\n",
        encoding="utf-8",
    )

    _run_init(monkeypatch)

    lines = (fake_root / ".env").read_text(encoding="utf-8").splitlines()
    assert lines[:5] == [
        "TZ=Asia/Jakarta   # my zone",
        "AUTH_TOKEN=existing-token",
        "",
        "# volume notes",
        "GEMINI_CREDS_VOLUME=praxis-gemini-creds",
    ]


@pytest.mark.unit
def test_the_chosen_preset_is_what_lands_in_the_file(fake_root, monkeypatch):
    """Answer 2, not the default, so the CHOICE is what is pinned.

    Taking the default here would pass even if `_choose_preset`'s return value
    were dropped on the floor between the menu and the file.
    """
    _run_init(monkeypatch, {"Auth token": "tok", "Preset": 2})

    written = _dotenv((fake_root / ".env").read_text(encoding="utf-8"))
    assert written["DEFAULT_WORKER_HARNESS"] == "agy"
    assert written["DEFAULT_WORKER_MODEL"] == "Gemini 3.6 Flash (High)"


@pytest.mark.unit
def test_declining_preset_still_writes_collected_answers(fake_root, monkeypatch):
    """Declining a preset must not discard the token, port, and credentials.

    `_choose_preset` raises `typer.Exit(1)` out of
    `_confirm_unmet_requirements` when the operator declines a preset that
    needs a credential `init` cannot collect.  That used to propagate
    straight out of `init()` before anything was written: a newcomer holding
    Enter through Quick Start answers every prompt, then declines the one
    preset needing a login `init` cannot do for them, and ends with an empty
    directory and exit 1 -- every answer just typed, discarded.
    """
    monkeypatch.setattr(
        init_mod, "_resolve_auth_token", lambda _current, _answers: "TESTTOKEN"
    )
    monkeypatch.setattr(
        init_mod, "_resolve_github_token", lambda _current, _answers: "GHTOKEN"
    )
    monkeypatch.setattr(init_mod.IntPrompt, "ask", lambda *_a, **_k: 12323)
    monkeypatch.setattr(
        init_mod,
        "_fetch_presets_or_defaults",
        lambda: [
            {
                "name": "p",
                "label": "P",
                "harness": "agy",
                "model": "M",
                "endpoint": "",
                "requires": ["interactive_login"],
                "default": True,
            }
        ],
    )
    monkeypatch.setattr(init_mod.Confirm, "ask", lambda *_a, **_k: False)

    with pytest.raises(typer.Exit):
        init_mod.run_init(Answers())

    env = (fake_root / ".env").read_text(encoding="utf-8")
    assert "AUTH_TOKEN=TESTTOKEN" in env
    assert "PORT=12323" in env
    assert "GITHUB_TOKEN=GHTOKEN" in env


@pytest.mark.unit
def test_declining_preset_on_a_re_run_leaves_the_existing_worker_config_intact(
    fake_root, monkeypatch
):
    """None means "no opinion", never "clear" -- pinned through the partial write.

    A re-run that declines the preset writes a partial `.env` with
    `preset=None`.  If that ever resolved to `""` instead of `None`,
    `merge_env` would read it as the PRESET authoring an empty value and
    blank the operator's existing, working `DEFAULT_WORKER_HARNESS` and
    `DEFAULT_WORKER_MODEL` -- the opposite of "nothing new was confirmed".
    """
    (fake_root / ".env").write_text(
        "AUTH_TOKEN=old-token\n"
        "DEFAULT_WORKER_HARNESS=opencode\n"
        "DEFAULT_WORKER_MODEL=qwen3-32b\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        init_mod, "_resolve_auth_token", lambda _current, _answers: "old-token"
    )
    monkeypatch.setattr(
        init_mod, "_resolve_github_token", lambda _current, _answers: None
    )
    monkeypatch.setattr(init_mod.IntPrompt, "ask", lambda *_a, **_k: 12323)
    monkeypatch.setattr(
        init_mod,
        "_fetch_presets_or_defaults",
        lambda: [
            {
                "name": "p",
                "label": "P",
                "harness": "agy",
                "model": "M",
                "endpoint": "",
                "requires": ["interactive_login"],
                "default": True,
            }
        ],
    )
    monkeypatch.setattr(init_mod.Confirm, "ask", lambda *_a, **_k: False)

    with pytest.raises(typer.Exit):
        init_mod.run_init(Answers())

    env = (fake_root / ".env").read_text(encoding="utf-8")
    assert "DEFAULT_WORKER_HARNESS=opencode" in env
    assert "DEFAULT_WORKER_MODEL=qwen3-32b" in env


@pytest.mark.unit
def test_switching_preset_on_a_re_run_clears_the_previous_endpoint(
    fake_root, monkeypatch
):
    """A half-applied switch is a config the operator believes they replaced.

    `gemini-agy` authors an empty endpoint, so the previous preset's
    `LM_STUDIO_URL` has to go.  Left behind it points `agy` at an LM Studio
    box, and nothing in the file or the logs says the two disagree.
    """
    (fake_root / ".env").write_text(
        "AUTH_TOKEN=tok\n"
        "LM_STUDIO_URL=http://host.docker.internal:1234\n"
        "DEFAULT_WORKER_HARNESS=opencode\n"
        "DEFAULT_WORKER_MODEL=qwen3-32b\n",
        encoding="utf-8",
    )

    _run_init(monkeypatch, {"Preset": 2})

    text = (fake_root / ".env").read_text(encoding="utf-8")
    assert "LM_STUDIO_URL" not in text
    assert _dotenv(text)["DEFAULT_WORKER_HARNESS"] == "agy"


@pytest.mark.unit
def test_declining_the_update_leaves_the_file_byte_identical(fake_root, monkeypatch):
    """No has to mean no, including for the answers already collected."""
    original = "AUTH_TOKEN=keepme\nPORT=9999\n"
    (fake_root / ".env").write_text(original, encoding="utf-8")

    _run_init(monkeypatch, {"Update": False, "Auth token": "discarded", "Preset": 2})

    assert (fake_root / ".env").read_text(encoding="utf-8") == original


@pytest.mark.unit
def test_declining_the_update_makes_the_rest_of_the_run_follow_the_file(
    fake_root, monkeypatch
):
    """Compose reads `.env`, so the discarded answers cannot drive the verify.

    Carrying them on would poll and authenticate against a port and token the
    running container never received: the doctor then reports an unreachable
    or 401 orchestrator that is in fact healthy, and the operator debugs a
    problem that does not exist.
    """
    (fake_root / ".env").write_text("AUTH_TOKEN=keepme\nPORT=9999\n", encoding="utf-8")

    calls = _run_init(
        monkeypatch,
        {"Update": False, "Auth token": "discarded", "Dashboard port": 1234},
    )

    assert calls["doctor"] == [("http://127.0.0.1:9999", "keepme")]


@pytest.mark.unit
def test_init_actually_calls_print_next_steps(fake_root, monkeypatch):
    """Deleting the call from `init()` left the whole suite at 88 passed.

    The existing next-steps coverage invokes `_print_next_steps` directly, so
    nothing pinned that the entry point calls it at all.
    """
    calls = []
    monkeypatch.setattr(
        init_mod,
        "_print_next_steps",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    _run_init(monkeypatch)

    assert len(calls) == 1


@pytest.mark.unit
def test_declining_the_update_reports_the_worker_config_actually_on_disk(
    fake_root, monkeypatch
):
    """The decline path must not claim a preset that was never written.

    Answer "no" to `Update .env?` after picking `gemini-agy`: the file still
    says `DEFAULT_WORKER_HARNESS=opencode` / `DEFAULT_WORKER_MODEL=qwen3-32b`,
    so the printed note has to describe THAT, not the preset the operator was
    about to pick before declining.
    """
    (fake_root / ".env").write_text(
        "AUTH_TOKEN=keepme\n"
        "PORT=9999\n"
        "DEFAULT_WORKER_HARNESS=opencode\n"
        "DEFAULT_WORKER_MODEL=qwen3-32b\n",
        encoding="utf-8",
    )
    buffer = io.StringIO()
    monkeypatch.setattr(
        init_mod, "console", Console(file=buffer, width=200, no_color=True)
    )

    _run_init(monkeypatch, {"Update": False, "Preset": 2})

    printed = buffer.getvalue()
    assert "qwen3-32b" in printed
    assert "Gemini 3.6 Flash" not in printed


@pytest.mark.unit
def test_init_builds_the_agent_images_before_starting_the_orchestrator(
    fake_root, monkeypatch
):
    """Skipping the agent build leaves an install that only fails at dispatch.

    The orchestrator comes up, the doctor goes green, and the first dispatched
    task dies on a missing `opencode-agent:latest` minutes or days later.
    """
    calls = _run_init(monkeypatch)

    assert calls["compose"] == [
        ["--profile", "agents", "build"],
        ["up", "-d", "--build"],
    ]


@pytest.mark.unit
def test_the_exit_code_is_the_doctors_verdict(fake_root, monkeypatch):
    """A scripted install has to be able to gate on "it actually works".

    Exiting 0 regardless turns every red row into something only a human
    reading the table would ever notice.
    """
    calls = _run_init(monkeypatch, doctor_code=2)
    assert calls["exit_code"] == 2


@pytest.mark.unit
def test_an_orchestrator_that_never_becomes_healthy_fails_without_a_verdict(
    fake_root, monkeypatch
):
    """Running the doctor against a dead orchestrator reports the wrong problem."""
    calls = _run_init(monkeypatch, healthy=False)

    assert calls["exit_code"] == 1
    assert calls["doctor"] == []


@pytest.mark.unit
def test_cli_doctor_exposes_every_name_run_doctor_imports():
    """`_run_doctor` is un-stubbed by nothing in this suite; its import has to hold.

    `_compose` and `_wait_for_health` stay stubbed on purpose (running a real
    `docker compose` in a unit test would be worse), which is exactly what
    makes `_run_doctor`'s `from cli.doctor import ...` line the one piece of
    real wiring nothing else here exercises.  Renaming a private helper it
    imports (`_payload_for`, `_unreachable_payload`) is an `ImportError` on
    the LAST line of `init()`, after the install otherwise succeeded, and
    that measured 95 passed here beforehand.

    Names are extracted from `_run_doctor`'s own source rather than
    hardcoded, so this cannot itself drift from what it actually imports.
    """
    source = inspect.getsource(init_mod._run_doctor)
    match = re.search(r"from cli\.doctor import (.+)", source)
    assert match, "expected _run_doctor to import names from cli.doctor"
    names = [n.strip() for n in match.group(1).split(",")]
    assert names, "expected at least one imported name"

    import cli.doctor as doctor_mod

    missing = [name for name in names if not hasattr(doctor_mod, name)]
    assert missing == [], f"cli.doctor no longer exposes: {missing}"


# --------------------------------------------------------------------------
# Entrypoint content hashes, passed to the agent image build so the
# `org.praxis.entrypoint-sha256` label is actually populated.
# --------------------------------------------------------------------------


def _write_entrypoints(root: Path) -> None:
    """Create a minimal ``docker/<harness>-agent/entrypoint.sh`` per harness."""
    for harness in ("agy", "opencode"):
        d = root / "docker" / f"{harness}-agent"
        d.mkdir(parents=True)
        (d / "entrypoint.sh").write_text(
            f"#!/bin/bash\necho {harness}\n", encoding="utf-8"
        )


@pytest.mark.unit
def test_build_env_includes_entrypoint_hashes(tmp_path, monkeypatch):
    """init must compute a hash per harness before invoking the build."""
    from orchestrator.core.entrypoint_hash import hash_entrypoint

    root = tmp_path
    _write_entrypoints(root)

    env = init_mod._entrypoint_build_env(root)

    assert env["AGY_ENTRYPOINT_SHA256"] == hash_entrypoint(
        root / "docker" / "agy-agent" / "entrypoint.sh"
    )
    assert env["OPENCODE_ENTRYPOINT_SHA256"] == hash_entrypoint(
        root / "docker" / "opencode-agent" / "entrypoint.sh"
    )


@pytest.mark.unit
def test_build_env_omits_unreadable_entrypoint(tmp_path):
    """A missing entrypoint yields no key, so the label stays empty."""
    env = init_mod._entrypoint_build_env(tmp_path)
    assert "AGY_ENTRYPOINT_SHA256" not in env


@pytest.mark.unit
def test_agent_image_build_receives_entrypoint_hash_env(fake_root, monkeypatch):
    """The real subprocess.run for the agent image build carries the hashes.

    `_stub_the_world` (used by every other full-flow test here) fakes
    `_compose` itself, which would hide whether the `env=` merge that lives
    in `init()` -- not in `_compose` -- actually reaches the subprocess call.
    This test leaves `_compose` real and fakes `subprocess.run` instead, so
    it is the one place that actually observes the wiring rather than just
    the helper that feeds it.
    """
    _write_entrypoints(fake_root)

    calls: list[dict] = []

    def _fake_run(cmd, check=True, env=None):
        calls.append({"cmd": cmd, "env": env})

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr(init_mod.subprocess, "run", _fake_run)
    monkeypatch.setattr(init_mod, "_wait_for_health", lambda _url, _timeout_s=180: True)
    monkeypatch.setattr(init_mod, "_run_doctor", lambda _url, _token: 0)
    monkeypatch.setattr(
        init_mod,
        "_fetch_presets_or_defaults",
        lambda: [dict(p) for p in TWO_PRESETS],
    )
    _hold_enter(monkeypatch)

    with pytest.raises(typer.Exit):
        init_mod.run_init(Answers())

    build_calls = [c for c in calls if "agents" in c["cmd"]]
    assert len(build_calls) == 1, f"expected exactly one agent build call, got {calls}"
    env = build_calls[0]["env"]
    assert env is not None, "agent image build must receive an explicit env"
    assert "AGY_ENTRYPOINT_SHA256" in env
    assert "OPENCODE_ENTRYPOINT_SHA256" in env

    other_calls = [c for c in calls if "agents" not in c["cmd"]]
    assert other_calls, "expected the orchestrator `up -d --build` call too"
    for call in other_calls:
        assert call["env"] is None, "only the agent build should get a merged env"


def test_the_menu_carries_the_setup_recipe_from_the_yaml(monkeypatch):
    """`_fetch_presets_or_defaults` must not drop setup_doc/setup_hint.

    The dict it builds is enumerated key by key, so a field added to
    WorkerPreset and not listed there reaches `_confirm_unmet_requirements`
    as absent and the recipe silently never prints. Only a live init run
    caught it the first time; this pins it.
    """
    from cli.init import _fetch_presets_or_defaults

    presets = _fetch_presets_or_defaults()
    agy = [p for p in presets if p.get("requires")]
    assert agy, "expected at least one preset with an unmet-able requirement"
    assert any(p.get("setup_hint") for p in agy), (
        "no preset requiring a credential carries a setup_hint; the recipe "
        "cannot reach the operator"
    )


def test_an_empty_env_directory_from_docker_is_removed(tmp_path):
    """Compose before init leaves a `.env/` directory; init must recover.

    Both compose files bind-mount ./.env, and Docker creates a missing bind
    source as a DIRECTORY, so every later write fails with IsADirectoryError
    and nothing on screen says why.
    """
    from cli.init import _clear_env_placeholder_dir

    env_path = tmp_path / ".env"
    env_path.mkdir()
    _clear_env_placeholder_dir(env_path)

    assert not env_path.exists()
    env_path.write_text("AUTH_TOKEN=x\n", encoding="utf-8")


def test_a_non_empty_env_directory_is_refused_not_deleted(tmp_path):
    """Never guess at deletion: a non-empty directory could hold real files."""
    import typer

    from cli.init import _clear_env_placeholder_dir

    env_path = tmp_path / ".env"
    env_path.mkdir()
    (env_path / "something.txt").write_text("keep me", encoding="utf-8")

    with pytest.raises(typer.Exit):
        _clear_env_placeholder_dir(env_path)

    assert (env_path / "something.txt").read_text(encoding="utf-8") == "keep me"


def test_a_real_env_file_is_left_alone(tmp_path):
    from cli.init import _clear_env_placeholder_dir

    env_path = tmp_path / ".env"
    env_path.write_text("AUTH_TOKEN=x\n", encoding="utf-8")
    _clear_env_placeholder_dir(env_path)

    assert env_path.read_text(encoding="utf-8") == "AUTH_TOKEN=x\n"


# ---------------------------------------------------------------------------
# Non-interactive mode
#
# The wizard is TTY-only, so the only way to drive it from an agent or a setup
# script was to pipe newlines at it. That does not work, and it fails in the
# worst possible way: the QUESTION SET changes with state. Five questions on a
# fresh clone, seven once `.env` exists ("Reuse the AUTH_TOKEN already in
# .env?" first, "Update .env?" last). An answer sequence tuned on the first
# run silently misaligns on the second and answers the wrong questions.
#
# Every test below therefore scripts NO prompt answers at all. A prompt that
# reaches rich in this mode returns its default rather than raising, so
# asserting on the resulting `.env` is what actually proves nothing was asked.
# ---------------------------------------------------------------------------


def _no_prompts(monkeypatch):
    """Make any prompt an outright failure, so silence is provable.

    Without this a stray prompt quietly takes its default and the run looks
    non-interactive when it merely got lucky.
    """

    def _boom(*_args, **_kwargs):
        message = "init prompted in non-interactive mode"
        raise AssertionError(message)

    for name in ("Confirm", "IntPrompt", "Prompt"):
        stub = type(name, (), {"ask": staticmethod(_boom)})
        monkeypatch.setattr(init_mod, name, stub)


@pytest.mark.unit
def test_non_interactive_writes_a_full_env_without_asking_anything(
    fake_root, monkeypatch
):
    """The primitive PR #109 Task 10 asked for, end to end on a fresh clone."""
    _no_prompts(monkeypatch)
    calls = _run_init(
        monkeypatch,
        flags=Answers(non_interactive=True, auth_token="tok-abc", preset="gemini-agy"),
    )

    values = dotenv_values(fake_root / ".env")
    assert values["AUTH_TOKEN"] == "tok-abc"
    assert values["DEFAULT_WORKER_HARNESS"] == "agy"
    assert values["PORT"] == "12323"
    assert calls["exit_code"] == 0


@pytest.mark.unit
def test_non_interactive_selects_a_preset_by_name_not_by_position(
    fake_root, monkeypatch
):
    """A menu index shifts whenever the settings YAML gains or reorders a row.

    Pinning position 1 in a setup script would silently start selecting a
    different worker; pinning the name cannot.
    """
    _no_prompts(monkeypatch)
    _run_init(
        monkeypatch,
        flags=Answers(non_interactive=True, auth_token="t", preset="local-lmstudio"),
        presets=list(reversed(TWO_PRESETS)),
    )

    values = dotenv_values(fake_root / ".env")
    assert values["DEFAULT_WORKER_HARNESS"] == "opencode"
    assert values["DEFAULT_WORKER_MODEL"] == "qwen3-32b"


@pytest.mark.unit
def test_an_unknown_preset_name_fails_before_anything_is_written(
    fake_root, monkeypatch
):
    """Naming a preset that does not exist must not silently fall back."""
    _no_prompts(monkeypatch)
    calls = _run_init(
        monkeypatch,
        flags=Answers(non_interactive=True, auth_token="t", preset="no-such-preset"),
    )

    assert calls["exit_code"] == 1
    assert calls["compose"] == [], (
        "nothing may be built or started after an unknown preset"
    )


@pytest.mark.unit
def test_non_interactive_reuses_the_existing_token_rather_than_rotating_it(
    fake_root, monkeypatch
):
    """A re-run must not rotate the token out from under configured clients.

    The interactive path defaults to reuse; unattended has to do the same, or
    every scheduled re-run breaks every MCP client pointed at this install.
    """
    (fake_root / ".env").write_text(REAL_SHAPED_ENV, encoding="utf-8")
    _no_prompts(monkeypatch)
    _run_init(monkeypatch, flags=Answers(non_interactive=True, preset="gemini-agy"))

    assert dotenv_values(fake_root / ".env")["AUTH_TOKEN"] == "old-token"


@pytest.mark.unit
def test_non_interactive_generates_and_prints_a_token_when_there_is_none(
    fake_root, monkeypatch, capsys
):
    """The operator never saw a prompt, so the generated secret must be printed.

    Without this the install works and is unusable: the token exists only
    inside a file the caller was not told to read.
    """
    _no_prompts(monkeypatch)
    _run_init(monkeypatch, flags=Answers(non_interactive=True, preset="gemini-agy"))

    written = dotenv_values(fake_root / ".env")["AUTH_TOKEN"]
    assert written
    assert written in capsys.readouterr().out


@pytest.mark.unit
def test_non_interactive_never_clears_an_existing_github_credential(
    fake_root, monkeypatch
):
    """Omitting --github-token means "leave it alone", never "delete it".

    An unattended re-run that wiped the credential would put the install into
    local mode with nothing raising anywhere.
    """
    (fake_root / ".env").write_text(
        "AUTH_TOKEN=t\nGITHUB_TOKEN=ghp_real\n", encoding="utf-8"
    )
    _no_prompts(monkeypatch)
    _run_init(monkeypatch, flags=Answers(non_interactive=True, preset="gemini-agy"))

    assert dotenv_values(fake_root / ".env")["GITHUB_TOKEN"] == "ghp_real"


@pytest.mark.unit
def test_non_interactive_refuses_a_preset_with_unmet_requirements(
    fake_root, monkeypatch
):
    """The guard survives the flag. Skipping it is the whole hazard.

    A run that skipped this starts, reports healthy, and fails its first real
    task, which is exactly what the confirmation exists to prevent.
    """
    _no_prompts(monkeypatch)
    unsatisfiable = [{**TWO_PRESETS[0], "requires": ["api_key"]}]
    calls = _run_init(
        monkeypatch,
        flags=Answers(non_interactive=True, auth_token="t"),
        presets=unsatisfiable,
    )

    assert calls["exit_code"] == 1
    assert calls["compose"] == [], "an unmet requirement must stop before docker runs"


@pytest.mark.unit
def test_accepting_the_requirements_explicitly_lets_the_run_proceed(
    fake_root, monkeypatch
):
    """The escape hatch, which must be an explicit assertion and not a default."""
    _no_prompts(monkeypatch)
    unsatisfiable = [{**TWO_PRESETS[0], "requires": ["api_key"]}]
    calls = _run_init(
        monkeypatch,
        flags=Answers(
            non_interactive=True, auth_token="t", accept_preset_requirements=True
        ),
        presets=unsatisfiable,
    )

    assert calls["exit_code"] == 0
    assert dotenv_values(fake_root / ".env")["DEFAULT_WORKER_HARNESS"] == "opencode"


@pytest.mark.unit
def test_a_flag_pre_answers_the_prompt_in_interactive_mode_too(fake_root, monkeypatch):
    """One decision path, two modes. A second path is a second thing to drift.

    The port flag is asserted rather than the token because the port has a
    prompt that would otherwise take its own default and mask the flag.
    """
    _run_init(
        monkeypatch,
        {"Auth token": "tok", "GitHub token": "skip"},
        flags=Answers(port=9911),
    )

    assert dotenv_values(fake_root / ".env")["PORT"] == "9911"


# --------------------------------------------------------------------------
# A refused run must not end on a green "Wrote ..." line.
#
# Measured: `praxis init --non-interactive --preset nope` prints the red
# "No worker preset named 'nope'", then `Wrote C:\...\.env`, then exits 1. The
# last line an operator or a CI log reads says success.
#
# The partial write itself is deliberate and well argued, but the argument is
# specifically about "the token, port, and GitHub credential just typed": a
# newcomer holding Enter through Quick Start who then declines a preset needing
# a login `init` cannot perform. A NON-INTERACTIVE run typed nothing. There is
# nothing to rescue, so writing anyway only leaves a half-configured `.env`
# behind and reports it as an accomplishment.
# --------------------------------------------------------------------------


def _refused_non_interactive(monkeypatch, capsys, flags: Answers, presets: list):
    """Drive a non-interactive `run_init` that is expected to refuse."""
    _no_prompts(monkeypatch)
    monkeypatch.setattr(
        init_mod, "_fetch_presets_or_defaults", lambda: [dict(p) for p in presets]
    )
    with pytest.raises(typer.Exit) as exit_info:
        init_mod.run_init(flags)
    return exit_info.value.exit_code, capsys.readouterr().out


@pytest.mark.unit
def test_an_unknown_preset_name_does_not_report_a_successful_write(
    fake_root, monkeypatch, capsys
):
    """The measured defect: a red refusal followed by a green success line."""
    code, out = _refused_non_interactive(
        monkeypatch,
        capsys,
        Answers(non_interactive=True, auth_token="t", preset="nope"),
        TWO_PRESETS,
    )

    assert code == 1
    assert "No worker preset named" in out
    assert "Wrote" not in out


@pytest.mark.unit
def test_an_unknown_preset_name_leaves_no_env_behind(fake_root, monkeypatch, capsys):
    """And the line was not merely silenced: nothing was written either.

    A `.env` on disk after a refused run is what makes the false success line
    dangerous rather than cosmetic -- the next command finds a file, reads a
    token out of it, and reports a half-configured install as a working one.
    """
    _refused_non_interactive(
        monkeypatch,
        capsys,
        Answers(non_interactive=True, auth_token="t", preset="nope"),
        TWO_PRESETS,
    )

    assert not (fake_root / ".env").exists()


@pytest.mark.unit
def test_a_refused_requirement_in_non_interactive_mode_writes_nothing_either(
    fake_root, monkeypatch, capsys
):
    """The other way `_choose_preset` exits, on the same collected-nothing run."""
    unsatisfiable = [{**TWO_PRESETS[0], "requires": ["api_key"]}]

    code, out = _refused_non_interactive(
        monkeypatch,
        capsys,
        Answers(non_interactive=True, auth_token="t"),
        unsatisfiable,
    )

    assert code == 1
    assert "Wrote" not in out
    assert not (fake_root / ".env").exists()


@pytest.mark.unit
def test_a_refused_non_interactive_run_leaves_an_existing_env_byte_identical(
    fake_root, monkeypatch, capsys
):
    """A re-run that refuses must not rewrite the operator's working file.

    This is the half a "suppress the message" fix would miss entirely: the
    partial write merges MANAGED_KEYS into the existing file, so a typo'd
    `--preset` on a re-run rewrote a live install's `.env` and then exited 1.
    """
    before = REAL_SHAPED_ENV
    (fake_root / ".env").write_text(before, encoding="utf-8")

    _refused_non_interactive(
        monkeypatch,
        capsys,
        Answers(non_interactive=True, auth_token="new-token", preset="nope"),
        TWO_PRESETS,
    )

    assert (fake_root / ".env").read_text(encoding="utf-8") == before
