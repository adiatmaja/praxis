"""What `praxis init` PRINTS has to be true, not merely reassuring.

Every assertion here is about a claim, not a computation: a duration, a help
string, a parenthetical naming why a preset was chosen, a note about what a
`.env` forwards.  All of them failed the same way before this file existed --
silently, and in the direction that costs the operator the most.  A run
predicted as "a few minutes" that takes ten reads as a hang and gets killed;
a note asserting an override that does not happen sends the operator looking
for the wrong config; a help string describing only the non-interactive rule
teaches the interactive operator something false about their own session.

Nothing here mocks the claim it checks.  The duration lines are driven by a
fake clock so the number printed is provably a MEASUREMENT of the step and
not a constant, and the help strings are read out of typer's real rendered
output, box-drawing glyphs and all.
"""

import inspect
import io
import re
from pathlib import Path

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

from cli import init as init_mod
from cli.init import Answers, build_env_file, cli_env_exports, merge_env


REPO = Path(__file__).resolve().parents[1]

#: Unicode box drawing.  typer renders --help inside a rich panel and wraps a
#: long option string across rows, so a border glyph lands in the MIDDLE of
#: the sentence: "use the registry | | default".  Collapsing whitespace first
#: and searching after leaves a guard that can never match, and passes whether
#: the help is right or wrong.  Strip the glyphs BEFORE collapsing.
_BOX_GLYPHS = re.compile("[" + chr(0x2500) + "-" + chr(0x257F) + "]")

#: ANSI SGR sequences.  rich colorizes help when it believes the stream can
#: take it, and that belief is PLATFORM DEPENDENT: on the Windows runner these
#: assertions saw plain text and passed, while on the Linux runner the same
#: help arrived as `Without it: \x1b[1;36m--non\x1b[0m\x1b[1;36m-interactive\x1b[0m
#: refuses such a preset`, with the escapes falling INSIDE the phrase being
#: matched. Every guard in this file went red on CI while green locally, which
#: is the same defect as the box glyphs one layer out: the rendered text is not
#: the text you wrote, and what it turns into depends on where it runs.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _flatten(text: str) -> str:
    """Return ``text`` with ANSI and box glyphs removed, whitespace collapsed.

    Order matters. ANSI first, because an escape can sit mid-word and would
    otherwise survive into the collapsed string; box glyphs second, because
    they are what a wrapped panel row leaves behind; whitespace last.
    """
    return " ".join(_BOX_GLYPHS.sub(" ", _ANSI.sub("", text)).split())


@pytest.fixture(autouse=True)
def _never_touch_the_real_env(monkeypatch):
    """Refuse to run `run_init` from the real checkout, as in test_cli_init.

    `run_init` is CWD-relative and this repository satisfies its own root
    guard, so a test that forgot `fake_root` would answer "yes" to
    `Update .env?` against the operator's live file.
    """
    real_run_init = init_mod.run_init

    def _guarded_run_init(answers):
        assert Path.cwd().resolve() != REPO.resolve(), (
            "a test is about to call run_init() from the real repo root; "
            "did it forget the `fake_root` fixture?"
        )
        return real_run_init(answers)

    monkeypatch.setattr(init_mod, "run_init", _guarded_run_init)


@pytest.fixture
def fake_root(tmp_path, monkeypatch):
    """A directory satisfying init's repo-root guard, chdir'd into."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "praxis"\n', encoding="utf-8"
    )
    (tmp_path / ".env.example").write_text("AUTH_TOKEN=\n", encoding="utf-8")
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


ONE_PRESET = [
    {
        "name": "local-lmstudio",
        "label": "Local GPU via LM Studio",
        "harness": "opencode",
        "model": "qwen3-32b",
        "endpoint": "http://host.docker.internal:1234",
        "requires": [],
    }
]


class _Clock:
    """A monotonic clock only the test advances.

    Real wall time makes every elapsed line read ``0s``, which is equally
    consistent with a working measurement and with a hardcoded string.  Under
    this clock the printed number can only be right if the code actually
    subtracted two readings taken around the step.
    """

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, _seconds: float) -> None:  # pragma: no cover - unused
        pass


def _drive_init(
    monkeypatch,
    *,
    buffer: io.StringIO,
    flags: Answers,
    presets: list | None = None,
    answers: dict | None = None,
    on_compose=None,
) -> int:
    """Run `run_init` with docker, health and the doctor stubbed out.

    Args:
        monkeypatch: The pytest fixture doing the patching.
        buffer: Where init's console output is collected.
        flags: Command-line answers.
        presets: The preset menu to offer.
        answers: Prompt-fragment -> reply, for the interactive path.
        on_compose: Called with each compose argument list, so a test can
            advance the fake clock across a specific step.

    Returns:
        init's exit code.
    """
    replies = answers or {}

    def _prompt_class():
        class _Scripted:
            @staticmethod
            def ask(prompt="", **kwargs):
                for needle, value in replies.items():
                    if needle in str(prompt):
                        return value
                return kwargs.get("default")

        return _Scripted

    for name in ("Confirm", "IntPrompt", "Prompt"):
        monkeypatch.setattr(init_mod, name, _prompt_class())

    def _fake_compose(args, _what, env=None):
        if on_compose is not None:
            on_compose(args)

    monkeypatch.setattr(init_mod, "console", Console(file=buffer, width=200))
    monkeypatch.setattr(init_mod, "_compose", _fake_compose)
    monkeypatch.setattr(init_mod, "_wait_for_health", lambda _url, _timeout_s=180: True)
    monkeypatch.setattr(init_mod, "_run_doctor", lambda _url, _token: 0)
    monkeypatch.setattr(
        init_mod,
        "_fetch_presets_or_defaults",
        lambda: [dict(p) for p in (presets or ONE_PRESET)],
    )
    with pytest.raises(typer.Exit) as exit_info:
        init_mod.run_init(flags)
    return exit_info.value.exit_code


# --------------------------------------------------------------------------
# 1. How long this takes: said up front, then measured.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_the_realistic_duration_is_stated_before_anything_is_written(
    fake_root, monkeypatch
):
    """ "A few minutes" understated a cold-cache run by a factor of three.

    It was also the ONLY thing said about duration, and it was said at the
    build itself rather than up front, so the operator learned the cost after
    committing to it.  On a cold cache the reasonable conclusion at minute six
    is that it has hung, and the fix is a range plus what drives it, said
    before the first prompt.
    """
    buffer = io.StringIO()
    _drive_init(monkeypatch, buffer=buffer, flags=Answers(non_interactive=True))
    printed = _flatten(buffer.getvalue())

    assert "about 2 minutes" in printed
    assert "about 10 minutes" in printed
    assert "agent image builds" in printed, (
        "the estimate must name what dominates it, or the operator cannot "
        "tell a slow build from a hung one"
    )
    assert "a few minutes" not in printed, "the understated claim is still here"
    assert printed.index("about 10 minutes") < printed.index("Wrote"), (
        "the expectation has to be set before anything is written, not at "
        "the step it describes"
    )


@pytest.mark.unit
def test_each_long_step_reports_the_time_it_actually_took(fake_root, monkeypatch):
    """A prediction becomes a measurement, or it stays a guess.

    The clock advances only inside the stubbed compose calls, so ``2m03s`` can
    only appear if the code read the clock before the agent build and again
    after it.  A hardcoded string, or a reading taken at the wrong end, prints
    something else.
    """
    clock = _Clock()
    monkeypatch.setattr(init_mod, "time", clock)

    def _advance(args):
        clock.now += 123.0 if "agents" in args else 7.0

    buffer = io.StringIO()
    _drive_init(
        monkeypatch,
        buffer=buffer,
        flags=Answers(non_interactive=True),
        on_compose=_advance,
    )
    printed = _flatten(buffer.getvalue())

    assert "Agent images built in 2m03s" in printed
    assert "Healthy after 2m10s total" in printed


@pytest.mark.unit
@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        pytest.param(0, "0s", id="instant"),
        pytest.param(59, "59s", id="under-a-minute"),
        pytest.param(60, "1m00s", id="exactly-a-minute"),
        pytest.param(123, "2m03s", id="zero-padded-seconds"),
        pytest.param(600, "10m00s", id="cold-cache"),
    ],
)
def test_an_elapsed_interval_renders_as_minutes_and_seconds(seconds, expected):
    """``2m3s`` and ``2m03s`` differ only in whether it scans as a duration."""
    assert init_mod._format_elapsed(1000.0, 1000.0 + seconds) == expected


# --------------------------------------------------------------------------
# 2 and 3. Help text, read out of typer's real rendered output.
# --------------------------------------------------------------------------


def _init_help(monkeypatch) -> str:
    """Render `praxis init --help` at 80 columns, flattened.

    80 columns on purpose: it is the width that forces typer to wrap a long
    option string across panel rows, which is the case a naive guard silently
    stops matching.
    """
    monkeypatch.setenv("COLUMNS", "80")
    app = typer.Typer()
    app.command()(init_mod.init)
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    return _flatten(result.output)


@pytest.mark.unit
def test_the_help_text_is_actually_reachable_through_the_flattener(monkeypatch):
    """Guard the guard: a dead matcher passes whatever the help says.

    A long option string wraps across panel rows with a border glyph between
    the halves, so a flattener that collapses whitespace before stripping
    those glyphs matches nothing at all.  This pins a phrase that is KNOWN to
    span a wrap, so the tests below are proven to be able to fail.
    """
    help_text = _init_help(monkeypatch)
    assert "one-time setup (API key, interactive login) is already done" in help_text


@pytest.mark.unit
def test_the_accept_requirements_help_describes_both_modes(monkeypatch):
    """ "Without it, a preset with unmet requirements is refused" is half true.

    Only `--non-interactive` refuses.  Interactively the confirmation defaults
    to no but the operator may answer yes and proceed with no flag at all, so
    the old wording described a restriction their session does not have.
    """
    help_text = _init_help(monkeypatch)

    assert "Without it, a preset with unmet requirements is refused" not in help_text
    assert "--non-interactive refuses such a preset" in help_text
    assert "interactively you are asked to confirm and may still proceed" in help_text


@pytest.mark.unit
def test_the_preset_help_does_not_claim_a_configured_deployment_default(monkeypatch):
    """Two of the three selection rules fire only when NOTHING is configured.

    "the deployment's configured default" describes the first rule and
    silently mis-describes the other two, which are the ones a deployment that
    flags nothing actually gets.
    """
    help_text = _init_help(monkeypatch)

    assert "the deployment's configured default" not in help_text
    assert "the preset the settings file flags default" in help_text
    assert "else the first needing no credential" in help_text


# --------------------------------------------------------------------------
# 3. The parenthetical next to the chosen preset names the rule that chose it.
# --------------------------------------------------------------------------

_FLAGGED_MENU = [
    {**ONE_PRESET[0], "name": "first", "requires": []},
    {**ONE_PRESET[0], "name": "flagged", "requires": [], "default": True},
]
_NOTHING_FLAGGED_MENU = [
    {**ONE_PRESET[0], "name": "needs-a-key", "requires": ["api_key"]},
    {**ONE_PRESET[0], "name": "free", "requires": []},
]
_ALL_NEED_CREDENTIALS_MENU = [
    {**ONE_PRESET[0], "name": "first-needy", "requires": ["api_key"]},
    {**ONE_PRESET[0], "name": "second-needy", "requires": ["interactive_login"]},
]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("menu", "name", "rule"),
    [
        pytest.param(_FLAGGED_MENU, "flagged", init_mod._RULE_FLAGGED, id="flagged"),
        pytest.param(
            _NOTHING_FLAGGED_MENU,
            "free",
            init_mod._RULE_NO_CREDENTIAL,
            id="no-credential-fallback",
        ),
        pytest.param(
            _ALL_NEED_CREDENTIALS_MENU,
            "first-needy",
            init_mod._RULE_FIRST_ON_MENU,
            id="first-on-menu-fallback",
        ),
    ],
)
def test_the_chosen_preset_names_the_rule_that_selected_it(
    monkeypatch, menu, name, rule
):
    """One scenario per BRANCH: the flagged branch masks the other two.

    `_default_preset_index` returns a genuinely flagged preset in its FIRST
    loop only.  Under a settings file flagging nothing -- including the
    hardcoded fallback preset, which carries no `default` key at all -- the
    chosen preset is not a deployment default, and "(deployment default)"
    asserted it anyway.  Checked only against a flagged menu, that line looks
    correct in exactly the state you would check it in.
    """
    buffer = io.StringIO()
    monkeypatch.setattr(init_mod, "console", Console(file=buffer, width=200))

    chosen = init_mod._choose_preset(
        [dict(p) for p in menu],
        Answers(non_interactive=True, accept_preset_requirements=True),
    )
    printed = _flatten(buffer.getvalue())

    assert chosen["name"] == name
    assert f"Preset: {name} ({rule})" in printed
    assert "(deployment default)" not in printed


@pytest.mark.unit
@pytest.mark.parametrize(
    "menu",
    [
        pytest.param(_FLAGGED_MENU, id="flagged"),
        pytest.param(_NOTHING_FLAGGED_MENU, id="no-credential-fallback"),
        pytest.param(_ALL_NEED_CREDENTIALS_MENU, id="first-on-menu-fallback"),
    ],
)
def test_the_printed_rule_cannot_drift_from_the_index_it_explains(menu):
    """The reason has to come from the same decision as the choice.

    Re-deriving it beside `_default_preset_index` is how the explanation stays
    green while describing a different preset than the one selected.
    """
    assert init_mod._default_preset_choice(menu)[0] == init_mod._default_preset_index(
        menu
    )


@pytest.mark.unit
def test_the_hardcoded_fallback_preset_is_not_reported_as_a_deployment_default():
    """The fallback carries no `default` key, so no deployment flagged it.

    It is reached exactly when the settings file could not be read at all,
    which is the state in which "deployment default" is least true.
    """
    assert not init_mod._FALLBACK_PRESET.get("default")
    _, rule = init_mod._default_preset_choice([dict(init_mod._FALLBACK_PRESET)])
    assert rule == init_mod._RULE_NO_CREDENTIAL


# --------------------------------------------------------------------------
# 4. What an untouched `.env` actually forwards.
# --------------------------------------------------------------------------


def _decline_the_update(fake_root, monkeypatch, env_text: str) -> str:
    """Run init against ``env_text``, answering no to `Update .env?`."""
    (fake_root / ".env").write_text(env_text, encoding="utf-8")
    buffer = io.StringIO()
    _drive_init(
        monkeypatch,
        buffer=buffer,
        flags=Answers(),
        answers={"Update": False},
    )
    return _flatten(buffer.getvalue())


@pytest.mark.unit
def test_an_env_with_no_worker_keys_is_not_reported_as_overriding_anything(
    fake_root, monkeypatch
):
    """An absent key forwards NOTHING, which is the opposite of an override.

    Both compose files list DEFAULT_WORKER_* as bare pass-throughs, so a key
    `.env` does not set leaves the settings file applying unchanged.  The
    decline branch read `current.get(..., "")` and printed
    ``DEFAULT_WORKER_HARNESS=''`` while asserting it "still overrides the
    worker defaults", which states the reverse of what is in effect and looks
    exactly as configured as the true case.
    """
    printed = _decline_the_update(
        fake_root, monkeypatch, "AUTH_TOKEN=keepme\nPORT=9999\n"
    )

    assert "DEFAULT_WORKER_HARNESS=''" not in printed
    assert "DEFAULT_WORKER_MODEL=''" not in printed
    assert "sets no DEFAULT_WORKER_HARNESS or DEFAULT_WORKER_MODEL" in printed
    assert "applies unchanged" in printed


@pytest.mark.unit
def test_a_half_set_env_reports_each_worker_key_separately(fake_root, monkeypatch):
    """One verdict for the pair is wrong for whichever half disagrees.

    `.env` can set the harness and not the model; the harness then really is
    forwarded and really does override, while the model really does not.
    """
    printed = _decline_the_update(
        fake_root,
        monkeypatch,
        "AUTH_TOKEN=keepme\nPORT=9999\nDEFAULT_WORKER_HARNESS=opencode\n",
    )

    assert "DEFAULT_WORKER_HARNESS='opencode'" in printed
    assert "overrides that worker default" in printed
    assert "sets no DEFAULT_WORKER_MODEL" in printed
    assert "DEFAULT_WORKER_MODEL=''" not in printed


@pytest.mark.unit
def test_a_fully_set_env_still_reports_the_override_it_really_has(
    fake_root, monkeypatch
):
    """The true case must survive the fix, or the note just moved its lie.

    With both keys set the file genuinely is forwarded and genuinely does
    override, and the note has to keep saying so -- naming the values on disk,
    never the preset the operator declined to write.
    """
    printed = _decline_the_update(
        fake_root,
        monkeypatch,
        "AUTH_TOKEN=keepme\nPORT=9999\n"
        "DEFAULT_WORKER_HARNESS=opencode\nDEFAULT_WORKER_MODEL=qwen3-32b\n",
    )

    assert "DEFAULT_WORKER_HARNESS='opencode'" in printed
    assert "DEFAULT_WORKER_MODEL='qwen3-32b'" in printed
    assert "overrides that worker default" in printed
    assert "sets no DEFAULT_WORKER" not in printed


# --------------------------------------------------------------------------
# 5 and 6. Two smaller claims, both checked against the thing they describe.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_the_env_header_promises_only_the_comment_preservation_it_delivers():
    """`merge_env` drops a trailing comment with the line a preset clears.

    That removal is worth keeping: `gemini-agy` authors an empty
    `LM_STUDIO_URL`, and a comment left hanging over a key that no longer
    exists documents a setting the file does not have.  So the header is
    narrowed to the truth rather than the behaviour bent to the header.  Both
    halves are asserted here, because a header qualified while the behaviour
    quietly changed would leave this passing and the two out of step again.
    """
    merged = merge_env(
        "LM_STUDIO_URL=http://box:1234  # my GPU\n", {"LM_STUDIO_URL": ""}
    )
    assert "my GPU" not in merged, "the behaviour this header describes changed"

    # The `# ` prefixes come off BEFORE the lines are joined: left on, a
    # comment marker lands mid-sentence and no phrase spanning two header
    # lines can ever match, which is the same dead guard as the box glyphs.
    header = " ".join(
        line.lstrip("#").strip()
        for line in build_env_file({"AUTH_TOKEN": "t"}).splitlines()
        if line.startswith("#")
    )
    assert "every key it does not manage, and every comment." not in header
    assert "except a trailing comment on a managed key" in header


@pytest.mark.unit
def test_the_printed_cli_exports_docstring_names_the_port_the_cli_defaults_to():
    """The rationale named 8080, the bare-uvicorn port, long after the move.

    Read off `cli.main` rather than restated, so the number cannot drift
    again: it is the CLI's own default that makes the exports necessary or
    not, and a comment explaining a default that no longer exists teaches the
    next reader something false about where a normal install answers.
    """
    from cli import main as cli_main

    doc = inspect.getdoc(cli_env_exports) or ""
    ports = set(re.findall(r"\b\d{4,5}\b", doc))
    assert ports == {str(cli_main._DEFAULT_PORT)}, (
        f"docstring names ports {sorted(ports)}; cli.main defaults to "
        f"{cli_main._DEFAULT_PORT}"
    )
