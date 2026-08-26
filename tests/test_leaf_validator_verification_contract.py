"""The verification contract: one string, four consumers, one extraction rule.

Measured live on a real ``execute_plan`` decomposition (2026-08-26). The
decomposer emitted, for BOTH leaves, a runnable command wrapped in a sentence.
``is_runnable_verification`` accepted it, ``shell_command_for_verification``
refused it, and the review path logged "the task declares no runnable
verification of its own" about a leaf whose own check was ``pytest -q``. The
positive signal never fired on the path it was built for.

The decomposer was not misbehaving: it was obeying the prompt. The worked
example in ``plan_review._PROMPT`` is that exact shape, so the product taught
the shape its own guard refused. That is why the first test reads the example
OUT of the prompt instead of quoting it -- a hand-copied fixture drifts from the
prompt, and this contract has now drifted twice.

The extraction rule this pins is narrow and checkable: EXACTLY ONE backticked
span in the whole string IS the command. Two spans is a guess and stays
refused; the span still has to name a program, so unwrapping never widens WHAT
may be shelled, only where the command may be found.
"""

from __future__ import annotations

import re

import pytest

from orchestrator.core.leaf_validator import (
    is_runnable_verification,
    shell_command_for_verification,
    validate_leaves,
    validate_split_children,
)
from orchestrator.core.plan_review import _PROMPT
from orchestrator.models.schemas import CapabilityProfile, LeafTask


# The two verifications the live decomposition actually produced, verbatim.
MEASURED_LEAF_1 = (
    "Run `python -m pytest src/playground/test_hm_core.py -q` and confirm all "
    "tests are collected and pass with 0 failures."
)
MEASURED_LEAF_2 = (
    "Run `python -m pytest src/playground -q` and confirm all tests in both "
    "test_hm_core.py and test_hm.py pass with 0 failures."
)


def _profile() -> CapabilityProfile:
    return CapabilityProfile(
        model_name="test-model", parameter_count_b=7, context_window=8192
    )


def _leaf(task_id: str, verification: str) -> LeafTask:
    """A leaf that satisfies every other rule, so only verification is measured."""
    return LeafTask(
        id=task_id,
        title=f"Leaf {task_id}",
        description=f"Edit src/{task_id}.py.",
        plan_text=(
            f"Goal: src/{task_id}.py gains a helper.\n"
            f"Files: src/{task_id}.py\n"
            "Steps:\n"
            "Add the helper.\n"
            "Acceptance: the suite passes."
        ),
        files=[f"src/{task_id}.py"],
        estimated_loc=20,
        verification=verification,
    )


# ---------------------------------------------------------------------------
# 1. The prompt and the guard, tied by construction.
# ---------------------------------------------------------------------------


def _example_verification_from_prompt() -> str:
    """Pull the worked example's ``verification`` out of the decompose prompt."""
    match = re.search(r'"verification":\s*"([^"]+)"', _PROMPT)
    assert match is not None, "the decompose prompt no longer shows a verification"
    return match.group(1)


@pytest.mark.unit
def test_the_decompose_prompts_own_worked_example_is_shellable():
    """The shape Praxis TEACHES must be a shape Praxis can RUN.

    Read out of ``_PROMPT`` rather than quoted, so editing the prompt's example
    into a shape the guard refuses fails here instead of going live. This is the
    guard against the drift that produced the defect: prompt and guard are one
    contract, and nothing previously connected them.
    """
    example = _example_verification_from_prompt()
    assert is_runnable_verification(example) is True
    assert shell_command_for_verification(example) is not None


# ---------------------------------------------------------------------------
# 2. The measured strings.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(
            MEASURED_LEAF_1,
            "python -m pytest src/playground/test_hm_core.py -q",
            id="measured-leaf-1",
        ),
        pytest.param(
            MEASURED_LEAF_2,
            "python -m pytest src/playground -q",
            id="measured-leaf-2",
        ),
        pytest.param(
            "Run `pytest -q` and confirm it passes",
            "pytest -q",
            id="the-shape-the-prompt-teaches",
        ),
        pytest.param(
            "`uv run pytest -q`", "uv run pytest -q", id="span-is-whole-value"
        ),
    ],
)
def test_one_backticked_span_in_prose_is_the_command(value: str, expected: str):
    """The signal the review path was built for, on the path it was built for."""
    assert shell_command_for_verification(value) == expected


# ---------------------------------------------------------------------------
# 3. Exactly as narrow as the claim.
# ---------------------------------------------------------------------------


# Values that must never reach a shell. Named at module level so the vacuity
# test below can measure the SET rather than trusting each case in isolation.
REFUSED: dict[str, str] = {
    # THE hazard the widening creates. Leaves quote file paths in backticks
    # constantly -- the prompt's own plan_text example does it twice -- and
    # shelling a .py path yields "Permission denied" or a shell parsing Python,
    # i.e. a task FAILED on evidence Praxis fabricated.
    "backticked-file-path": "Confirm `src/playground/hm.py` defines TypeVar",
    "backticked-doc-path": "Check that `docs/api.md` was updated",
    # Two spans: picking one IS a guess, which is what the guard refuses.
    "two-backticked-commands": "Run `pytest -q` and then `ruff check src/`",
    "span-is-not-a-command": "Run `pytest -q`; it must print `0 failures`",
    # An unbalanced backtick is not a span at all.
    "unbalanced-backtick": "Run `pytest -q and confirm",
    # The whole point of the guard, unchanged: prose the HARD rule accepts.
    "prose-no-command": "the module imports cleanly",
    "names-symbols": "TypeVar, Con and Fun are importable",
    "unrecognised-leading-token": "cd subdir && pytest -q",
    "backticked-prose": "Confirm `the module imports cleanly`",
    "newline-inside-the-span": "Run `python -m\npytest`",
}


@pytest.mark.unit
@pytest.mark.parametrize("value", REFUSED.values(), ids=REFUSED.keys())
def test_a_value_that_does_not_unambiguously_name_a_program_is_refused(value: str):
    """A refusal costs a SIGNAL; a wrong acceptance costs a FALSE ACCUSATION."""
    assert shell_command_for_verification(value) is None


@pytest.mark.unit
def test_the_refusal_fixtures_are_not_vacuous():
    """Most of the refused set must be prose the HARD rule ACCEPTS.

    A guard that only refuses what F3 already rejects protects nothing, and the
    test above would keep passing while measuring that. This asserts the gap the
    guard exists to cover is actually present in the fixtures, so a future
    tightening of ``is_runnable_verification`` fails HERE rather than silently
    hollowing out every case above.
    """
    also_accepted_by_f3 = [v for v in REFUSED.values() if is_runnable_verification(v)]
    assert len(also_accepted_by_f3) >= 7, (
        "the refused fixtures no longer exercise the F3/shell-guard gap"
    )


@pytest.mark.unit
def test_a_bare_file_path_is_not_a_command_however_it_is_wrapped():
    """One rule for wrapped and unwrapped, or the two drift again.

    ``./scripts/check.sh`` is an invocation; ``src/client.py`` is a reference.
    The discriminator is ``./`` or an argument, not the backticks around it.
    """
    assert shell_command_for_verification("src/client.py") is None
    assert shell_command_for_verification("`src/client.py`") is None
    assert shell_command_for_verification("./scripts/check.sh") == "./scripts/check.sh"
    assert (
        shell_command_for_verification("`./scripts/check.sh`") == "./scripts/check.sh"
    )


# ---------------------------------------------------------------------------
# 4. F3 and the shell guard, related by construction rather than by comment.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_check_f3_accepts_but_praxis_cannot_shell_is_warned_never_blocked():
    """The drift is now VISIBLE at the seat that authored it.

    The HARD rule deliberately stays looser: its worst outcome is a wasted brain
    re-ask, and a worker can act on prose. But a leaf whose declared check Praxis
    cannot run silently loses the review path's positive signal, and nothing said
    so. This SOFT rule says so, and cannot drift from the guard because it IS the
    guard -- the rule calls ``shell_command_for_verification``.

    The difference is created inside this call, not inherited from a fixture:
    both leaves go through one ``validate_leaves``, and only the prose one is
    named.
    """
    shellable = _leaf("t1", MEASURED_LEAF_1)
    prose_only = _leaf("t2", "the module imports cleanly")
    leaves = [shellable, prose_only]

    result = validate_leaves({}, _profile(), "", leaves)

    assert result.dispatchable is True, "a soft rule must never block a plan"
    warned = {v.task_id for v in result.soft if v.rule == "verification_not_shellable"}
    assert warned == {"t2"}


@pytest.mark.unit
def test_split_children_are_graded_by_the_same_rule():
    """``validate_leaves`` had ONE call site once, and the split path bypassed
    every rule. The triage prompt asks a child for "a 'verification' naming a
    runnable command" in the decompose prompt's own words, so a child loses the
    review path's signal exactly the same way and must be told the same thing.
    """
    result = validate_split_children(
        [_leaf("c1", MEASURED_LEAF_2), _leaf("c2", "the module imports cleanly")],
        _profile(),
    )

    assert result.dispatchable is True
    warned = {v.task_id for v in result.soft if v.rule == "verification_not_shellable"}
    assert warned == {"c2"}


@pytest.mark.unit
def test_the_soft_rule_is_the_shell_guard_not_a_copy_of_it():
    """One derivation, proved by a CROSS-MODULE consequence.

    Every value the guard accepts must go unwarned and every value it refuses
    must be warned, over one shared fixture set. A second implementation of the
    rules would pass the tests above and fail here the moment the two answers
    parted, which is the drift this whole change exists to end.
    """
    values = [
        MEASURED_LEAF_1,
        MEASURED_LEAF_2,
        "pytest -q",
        "the module imports cleanly",
        "Confirm `src/client.py` defines it",
        "Run `pytest -q` and then `ruff check src/`",
    ]
    leaves = [_leaf(f"t{i}", v) for i, v in enumerate(values)]

    result = validate_leaves({}, _profile(), "", leaves)
    warned = {v.task_id for v in result.soft if v.rule == "verification_not_shellable"}
    expected = {
        leaf.id
        for leaf in leaves
        if shell_command_for_verification(leaf.verification) is None
    }

    assert warned == expected
    # The fixture set must exercise BOTH answers, or the equality is vacuous.
    assert 0 < len(expected) < len(leaves)
