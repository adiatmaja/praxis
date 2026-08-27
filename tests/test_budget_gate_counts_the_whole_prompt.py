"""The budget gate must size the whole prompt Praxis sends, not the Bible alone.

The defect these pin: ``fit_sections`` measured only the ``Section`` objects
``build_bible`` assembles, while every dispatch also carries ``TASK_PROMPT``
(``core/agent_prompt``) into the same context window. Both entrypoints
hard-require ``TASK_PROMPT`` (``: "${TASK_PROMPT:?...}"``) and both put the
Bible and the prompt in front of the same model - OpenCode via its
``instructions`` config plus the run prompt, agy by prepending the Bible to the
prompt - so the prompt is not an optional extra the gate may treat as zero.

Measured before the fix, with ``docs/gotchas.md`` ("The budget gate sizes the
Bible, not the prompt") as the write-up: against a 4096-token window
``worker_budget`` hands out 1638 tokens, and the FIXED scaffolding of
``agent_prompt._TEMPLATE`` alone, before any task text, is over 1300 of them.

What these tests deliberately do NOT do is assert that a budget function was
called. Every assertion below is a verdict: a prompt assembly that overflows is
refused, an identical one that fits is not, and the thing that flips the verdict
is the presence of the prompt rather than the presence of a call.
"""

from __future__ import annotations

import pytest

from orchestrator.core import agent_prompt
from orchestrator.core.token_budget import (
    ContextBudgetExceeded,
    Section,
    estimate_tokens,
    fit_sections,
    worker_budget,
)
from orchestrator.core.worker_bible import BibleSources, build_bible


# A window small enough that the prompt matters and large enough that the
# Bible alone clears it with room to spare. The preconditions below prove both
# rather than trusting the arithmetic in this comment.
_TIGHT_WINDOW = 4096
_ROOMY_WINDOW = 8192

# Padding that puts the floor sections well clear of the noise floor without
# coming near the budget on its own.
_GOAL_PADDING = "g" * 1000


def _sources(**overrides: object) -> BibleSources:
    """A minimal but realistic dispatch: a padded goal and a handover."""
    kwargs: dict[str, object] = {
        "goal": f"Ship the widget.\n{_GOAL_PADDING}",
        "handover": "# PROGRESS\n(no commits yet)",
        "context_window": _TIGHT_WINDOW,
    }
    kwargs.update(overrides)
    return BibleSources(**kwargs)  # type: ignore[arg-type]


def _bible_only_tokens(src: BibleSources) -> int:
    """Tokens of the Bible with nothing else charged against the window."""
    src.companion_prompt = ""
    return estimate_tokens(build_bible(src))


@pytest.mark.unit
def test_the_bible_alone_fits_this_window_so_the_prompt_is_what_decides():
    """Precondition, not a result. Without it every test below is vacuous.

    If the Bible alone already overflowed, the refusal in the next test would
    prove nothing about whether the prompt was counted. Sized here so a later
    edit that changes the scope briefing or the padding fails loudly instead of
    quietly making the suite inert.
    """
    bible_tokens = _bible_only_tokens(_sources())
    budget = worker_budget(_TIGHT_WINDOW)

    assert bible_tokens < budget, (
        f"the Bible alone ({bible_tokens} tok) must fit the {budget} tok budget "
        "or the refusal test below is measuring the wrong thing"
    )
    assert bible_tokens + agent_prompt.fixed_scaffolding_tokens() > budget, (
        "the prompt must be what tips this window over, or the refusal test "
        "below would pass with the prompt still uncounted"
    )


@pytest.mark.unit
def test_a_dispatch_that_only_overflows_once_the_prompt_is_counted_is_refused():
    """The defect, stated as a verdict.

    Same window, same sections, and the ONLY difference is whether the prompt
    Praxis also sends is charged against the budget. Before the fix this
    assembled silently and the endpoint refused the dispatch instead, which the
    engine then charged to the worker.
    """
    with pytest.raises(ContextBudgetExceeded):
        build_bible(_sources())


@pytest.mark.unit
def test_the_same_assembly_is_not_refused_when_nothing_else_is_sent():
    """The negative control: the refusal above is the prompt, not the pack.

    ``companion_prompt=""`` is the caller stating that nothing accompanies the
    Bible. Identical sections, identical window, and it assembles - so the
    refusal above cannot be blamed on the sections having grown.
    """
    out = build_bible(_sources(companion_prompt=""))

    assert "Ship the widget." in out


@pytest.mark.unit
def test_a_real_dispatch_still_fits_a_window_that_can_hold_it():
    """The positive control, and the whole risk of this change.

    A gate that refuses everything is worse than one that counts nothing: it
    fails real work with "split the task", the false signal this repo has
    already paid for twice. The same sections and the same charged prompt on a
    window that genuinely holds them must assemble.
    """
    out = build_bible(_sources(context_window=_ROOMY_WINDOW))

    assert "Ship the widget." in out
    assert "# SCOPE DISCIPLINE" in out


@pytest.mark.unit
def test_the_prompt_charge_is_derived_from_the_template_not_a_literal():
    """A hardcoded token count would rot the first time the template changed.

    Shrinking the template must shrink the charge, and the assembly that was
    refused above must now succeed. A literal constant survives this
    monkeypatch untouched and the refusal stays, which is exactly the drift
    this asserts against.
    """
    before = agent_prompt.fixed_scaffolding_tokens()
    with pytest.raises(ContextBudgetExceeded):
        build_bible(_sources())

    tiny = "Do the task:\n%%TASK_TITLE%%\n%%TASK_DESCRIPTION%%\n"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(agent_prompt, "_TEMPLATE", tiny)

        assert agent_prompt.fixed_scaffolding_tokens() < before
        out = build_bible(_sources())

    assert "Ship the widget." in out


@pytest.mark.unit
def test_a_caller_supplied_prompt_replaces_the_estimate_rather_than_adding_to_it():
    """The caller knows the real prompt; the derived floor is only a stand-in.

    Charging both would double-count the scaffolding, which the caller's
    string already contains, and would refuse work that fits. Here the supplied
    prompt is small enough to fit where the derived floor is not, so an
    implementation that adds them keeps raising.
    """
    out = build_bible(_sources(companion_prompt="Implement the widget. " * 10))

    assert "Ship the widget." in out


@pytest.mark.unit
def test_a_caller_supplied_prompt_larger_than_the_estimate_is_charged_in_full():
    """The other direction: the real prompt is usually BIGGER than the floor.

    The derived scaffolding is a lower bound because the task title and
    description are substituted into it. A caller that hands over the real
    prompt must be charged for the whole thing, or the exact accounting is
    less accurate than the estimate it replaced.
    """
    roomy = _sources(context_window=_ROOMY_WINDOW)
    assert build_bible(_sources(context_window=_ROOMY_WINDOW))  # fits unaided

    roomy.companion_prompt = "x" * 40_000  # 10 000 tok, over any 8192 budget
    with pytest.raises(ContextBudgetExceeded):
        build_bible(roomy)


@pytest.mark.unit
def test_an_unknown_window_still_skips_the_gate_and_still_says_so():
    """The third state survives. Unknown is not a small number.

    A window nobody could establish must keep every section and raise nothing,
    however large the accompanying prompt is. Substituting any number here -
    including a conservative one - is the ``or 8192`` defect returning by the
    back door.

    The pair is what makes this discriminating. The SAME sources and the SAME
    absurd companion prompt are refused at a known window and waved through at
    an unknown one, so the skip cannot be mistaken for the inputs happening to
    fit.
    """
    companion = "x" * 4_000_000  # 1 000 000 tok, over any real budget

    with pytest.raises(ContextBudgetExceeded):
        build_bible(_sources(context_window=200_000, companion_prompt=companion))

    out = build_bible(_sources(context_window=None, companion_prompt=companion))

    assert "# SCOPE DISCIPLINE" in out
    assert "Ship the widget." in out

    # Pinned at the gate itself as well as through the assembly, so a caller
    # that stops threading the skip is not the only thing this depends on.
    sections = [
        Section("goal", "g" * 200_000, priority=0, floor=True),
        Section("docs", "d" * 200_000, priority=9),
    ]
    kept = fit_sections(sections, context_window=None, out_of_band_tokens=10**9)
    assert [s.name for s in kept] == ["goal", "docs"]


@pytest.mark.unit
def test_a_window_too_small_for_praxis_own_scaffolding_says_so_not_split_the_task():
    """ "Split the task" is unactionable when the fixed scaffolding is the cause.

    No decomposition makes a leaf smaller than the prompt template wrapped
    around every leaf, so a refusal at this window is a deployment fact. The
    message has to name it, because the dispatcher's own wording tells a human
    to split.

    The window is chosen so the SCAFFOLDING is unambiguously the cause: the
    floor sections fit it comfortably on their own, asserted below, so a
    message blaming the floors would be naming the wrong thing.
    """
    window = 3000
    assert _bible_only_tokens(_sources(context_window=window)) < worker_budget(window)
    assert agent_prompt.fixed_scaffolding_tokens() > worker_budget(window)

    with pytest.raises(ContextBudgetExceeded) as exc:
        build_bible(_sources(context_window=window))

    scaffolding_message = str(exc.value)
    assert "raise the worker's context window" in scaffolding_message

    # The discriminator, not a second look at the same string: the OTHER
    # refusal branch is a genuine task-size problem and must NOT carry that
    # advice, or the message says the same thing whatever the cause and reading
    # it tells a human nothing. Here the floors are what overflow, with the
    # caller stating that nothing accompanies the Bible.
    with pytest.raises(ContextBudgetExceeded) as exc:
        build_bible(_sources(context_window=400, companion_prompt=""))

    floor_message = str(exc.value)
    assert "raise the worker's context window" not in floor_message
    assert "floor context" in floor_message


@pytest.mark.unit
def test_what_the_gate_cannot_count_is_named_rather_than_treated_as_zero():
    """The honest-shortfall half. A pass is not a promise that the prompt fits.

    Praxis does not control the harness's system prompt or tool schemas, and
    does not read the target repo's own AGENTS.md at dispatch time. Those are
    not reserved for by guesswork; they are named, so a reader of a passing
    verdict knows the number is a floor.
    """
    from orchestrator.core.token_budget import (
        UNCOUNTED_CONTEXT,
        describe_uncounted_context,
    )

    assert UNCOUNTED_CONTEXT, "the shortfall must be enumerated somewhere"
    described = describe_uncounted_context()
    for item in UNCOUNTED_CONTEXT:
        assert item in described
