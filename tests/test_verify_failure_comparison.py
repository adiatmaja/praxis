"""Two runs that both went red did not necessarily go red the same way.

Measured live on 2026-08-27, plan ``4eb8ed70`` leaf ``2312ade8``, driving
``execute_plan`` end to end on ``adiatmaja/playground``. The review's head run
and base run of the SAME project command, ``python -m pytest src/playground -q``:

* head: the suite RAN and three assertions failed.
* base: the suite never ran at all, interrupted by a collection ``ImportError``.

The engine concluded "fails identically" and declined to attribute, because the
whole comparison was ``if base.status != "failed"`` -- status equality, nothing
else. A base that cannot IMPORT the module and a head that fails three
assertions are not the same failure, and a genuine leaf regression was excused
as pre-existing.

The second consequence is larger and is why adaptive ``split`` had never once
been observed. Leaf 3's declared ``verification`` was the project command,
copied faithfully from the source plan's own ``Acceptance:`` line -- which is
CORRECT plan authorship, because for the final leaf of a dependent chain the
whole suite really is the acceptance. ``discriminating_leaf_command`` therefore
refuses it (rightly: it cannot discriminate), so there was no positive signal,
so every failure was non-attributable, so no ``task_outcomes`` row was written
and triage was never called -- and triage is where ``split`` is decided. The
largest leaf in every plan was structurally un-splittable, un-triageable and
invisible to calibration.

WHY THE COMPARISON IS ON EXIT CODES AND NOT ON OUTPUT TEXT
----------------------------------------------------------
Naive output equality is not merely imperfect here, it fails in the DANGEROUS
direction: it would report two runs of one unchanged tree as different and
charge a worker for repository health. Six noise sources were MEASURED on one
runner on one platform before this was written, and each alone defeats a text
comparison:

1. durations (``3 failed, 58 passed in 0.22s`` vs ``... in 0.20s``),
2. environment lines that appear on a first run only,
3. progress lines that lengthen when a worker adds PASSING tests,
4. count lines that move for the same reason,
5. absolute checkout paths -- the base run happens in a fresh
   ``tempfile.TemporaryDirectory()`` and the head run in the PR checkout, so
   the two paths differ even for an identical tree,
6. relative traceback traversals whose ``..\\..\\`` depth follows the temp
   directory's depth.

The fixtures below are the real bytes, temp paths and all. A fixture that looks
clean would prove nothing, because it is precisely the mess that a text
comparator has to survive and does not.

The exit code has none of those noise sources by construction. It is the
RUNNER'S OWN classification of its failure, needs no parsing, and is language
agnostic in the only way that matters: where a runner does not distinguish
(``go test`` and ``cargo test`` return one code for build errors and test
failures alike) the answer degrades to INCOMPARABLE, which licenses exactly
what it licensed before this existed -- nothing.
"""
# ruff: noqa: S101

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from orchestrator.core import orchestrator_review as review_mod
from orchestrator.core.leaf_validator import (
    restates_project_command,
    shell_command_for_verification,
)
from orchestrator.core.orchestrator_dispatch import attribute_wave_verify_failure
from orchestrator.core.orchestrator_review import (
    _PlanVerifyResult,
    attribute_plan_verify_failure,
)
from orchestrator.core.verify_gate import (
    LEAF_CHECK_NONDISCRIMINATING,
    LEAF_CHECK_NONE,
    FailureComparison,
    VerifyRun,
    base_failure_clause,
    compare_failures,
    run_exit_code,
    run_verify,
)
from orchestrator.models.schemas import TaskStatus


# ---------------------------------------------------------------------------
# The measured strings. Product output, captured verbatim on 2026-08-27 from
# `python -m pytest src/playground -q` over the two trees. Absolute temp paths
# and `..\..\` traversals are LEFT IN: they are the noise a text comparator has
# to survive, so removing them would make every assertion below inert.
# ---------------------------------------------------------------------------

_PROJECT_CMD = "python -m pytest src/playground -q"

# head: the suite ran, three assertions failed. Exit code 1 (measured).
_HEAD_TESTS_FAILED = (
    ".........................................F........F.F........            "
    "[100%]\n"
    "=========================== short test summary info "
    "===========================\n"
    "FAILED src/playground/test_hm_core.py::test_core[40] - AssertionError: "
    "assert...\n"
    "FAILED src/playground/test_hm_core.py::test_core[49] - AssertionError: "
    "assert...\n"
    "FAILED src/playground/test_hm_core.py::test_core[51] - AssertionError: "
    "assert...\n"
    "3 failed, 58 passed in 0.22s\n"
)
_HEAD_EXIT = 1

# The SAME command on the SAME tree a second time. Only the duration moved --
# and that alone is enough to make `head_output == base_output` report a
# difference that does not exist.
_HEAD_TESTS_FAILED_RERUN = _HEAD_TESTS_FAILED.replace("in 0.22s", "in 0.20s")

# base: the suite never ran. Exit code 2 (measured).
_BASE_CANNOT_COLLECT = (
    "\n"
    "=================================== ERRORS "
    "====================================\n"
    "_________________ ERROR collecting src/playground/test_hm.py "
    "__________________\n"
    "ImportError while importing test module "
    "'C:\\Users\\atmerie\\AppData\\Local\\Temp\\claude\\"
    "C--working-space-praxis\\scratchpad\\failcmp\\base\\src\\playground\\"
    "test_hm.py'.\n"
    "Hint: make sure your test modules/packages have valid Python names.\n"
    "Traceback:\n"
    "..\\..\\..\\..\\..\\..\\..\\..\\Roaming\\uv\\python\\"
    "cpython-3.13-windows-x86_64-none\\Lib\\importlib\\__init__.py:88: in "
    "import_module\n"
    "    return _bootstrap._gcd_import(name[level:], package, level)\n"
    "src\\playground\\test_hm.py:1: in <module>\n"
    "    from hm_core import infer_type\n"
    "E   ModuleNotFoundError: No module named 'hm_core'\n"
    "=========================== short test summary info "
    "===========================\n"
    "ERROR src/playground/test_hm.py\n"
    "!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection "
    "!!!!!!!!!!!!!!!!!!!!\n"
    "1 error in 0.20s\n"
)
_BASE_EXIT = 2

# An HONEST worker on the same repository: it left the three pre-existing
# failures alone and added twenty PASSING tests. The progress line and the
# count line both move, so a text comparison flags it as different and charges
# the worker for repository health. The exit code does not move. (measured)
_HEAD_PLUS_PASSING_TESTS = (
    ".........................................F........F.F................... "
    "[ 88%]\n"
    ".........                                                                "
    "[100%]\n"
    "=========================== short test summary info "
    "===========================\n"
    "FAILED src/playground/test_hm_core.py::test_core[40] - AssertionError: "
    "assert...\n"
    "FAILED src/playground/test_hm_core.py::test_core[49] - AssertionError: "
    "assert...\n"
    "FAILED src/playground/test_hm_core.py::test_core[51] - AssertionError: "
    "assert...\n"
    "3 failed, 78 passed in 0.23s\n"
)

# Leaf 3's declared verification, copied from the source plan's Acceptance
# line. It reduces to the project command byte for byte.
_LEAF_3_VERIFICATION = f"Run `{_PROJECT_CMD}` and confirm all tests pass."

# The shape the decompose prompt actually teaches: narrow, and able to
# discriminate. Its signal must survive this change untouched.
_LEAF_NARROW_VERIFICATION = (
    "Run `python -m pytest src/playground/test_hm_core.py -q` and confirm it passes."
)


# ---------------------------------------------------------------------------
# 0. The premise, asserted rather than assumed.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_naive_output_equality_would_flag_an_unchanged_tree_as_different():
    """The measurement that rules text comparison out, pinned as a test.

    Two runs of one command over one unchanged tree. If the engine compared
    OUTPUT it would call these two different failures and attribute the second
    to whoever happened to be under review. This asserts the trap exists, so
    nobody re-opens the question by reasoning about it.
    """
    assert _HEAD_TESTS_FAILED != _HEAD_TESTS_FAILED_RERUN
    assert compare_failures(_HEAD_EXIT, _HEAD_EXIT) is FailureComparison.FAILED_ALIKE


@pytest.mark.unit
def test_the_measured_leaf_three_verification_is_the_project_command():
    """The PRECONDITION for every leaf-3 test below.

    Without this the end-to-end tests would exercise a fixture that never had
    the defect in it -- a mutation that never reaches the code under test.
    """
    assert shell_command_for_verification(_LEAF_3_VERIFICATION) == _PROJECT_CMD
    assert restates_project_command(_LEAF_3_VERIFICATION, _PROJECT_CMD) is True
    assert restates_project_command(_LEAF_NARROW_VERIFICATION, _PROJECT_CMD) is False
    # A leaf that declared NOTHING is the third state, and it is not this one.
    assert restates_project_command(None, _PROJECT_CMD) is False


# ---------------------------------------------------------------------------
# 1. The comparator itself.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_measured_leaf_three_shape_is_two_different_failures():
    """1 (tests failed) against 2 (never collected). THE measured case."""
    assert (
        compare_failures(_HEAD_EXIT, _BASE_EXIT) is FailureComparison.FAILED_DIFFERENTLY
    )


@pytest.mark.unit
def test_an_honest_worker_adding_passing_tests_is_not_reported_as_different():
    """The false positive that would be worse than the defect being fixed.

    The outputs differ in two places and the exit codes do not. Charging a
    worker for repository health is the error this repository has already made
    once, so this is asserted from the OUTPUT side too: a future comparator
    that starts reading text must fail here.
    """
    assert _HEAD_PLUS_PASSING_TESTS != _HEAD_TESTS_FAILED
    assert compare_failures(1, 1) is FailureComparison.FAILED_ALIKE


@pytest.mark.unit
@pytest.mark.parametrize(
    ("head", "base"),
    [(None, 2), (1, None), (None, None)],
)
def test_a_missing_exit_code_on_either_side_is_incomparable(
    head: int | None, base: int | None
):
    """Fail closed. An unanswered question is never reported as an answer."""
    assert compare_failures(head, base) is FailureComparison.INCOMPARABLE


@pytest.mark.unit
@pytest.mark.parametrize(("head", "base"), [(0, 1), (1, 0), (0, 0)])
def test_a_zero_exit_code_is_refused_rather_than_compared(head: int, base: int):
    """Zero means the run PASSED, so this function was asked the wrong question.

    Degrading to INCOMPARABLE rather than raising is deliberate: every caller
    is mid-review, an exception there would fail a task on a bug in Praxis, and
    INCOMPARABLE is exactly the behaviour that predates this comparison.
    """
    assert compare_failures(head, base) is FailureComparison.INCOMPARABLE


@pytest.mark.unit
def test_every_comparison_has_its_own_words():
    """Three outcomes, three sentences, and none may claim another's fact.

    ``fails identically`` is a CLAIM. Before this change it was printed for
    every red base, including bases nobody had compared, which is the same
    overclaim ``base_comparison_unavailable`` exists to prevent one level up.
    """
    alike = base_failure_clause(FailureComparison.FAILED_ALIKE, "main", 1, 1)
    differ = base_failure_clause(FailureComparison.FAILED_DIFFERENTLY, "main", 1, 2)
    unknown = base_failure_clause(FailureComparison.INCOMPARABLE, "main", None, None)

    assert "identically" in alike
    assert "identically" not in differ
    assert "identically" not in unknown
    # The DIFFERENT sentence has to carry the evidence, or a human reading the
    # merge gate cannot tell whether to believe it.
    assert "1" in differ
    assert "2" in differ
    assert len({alike, differ, unknown}) == 3


# ---------------------------------------------------------------------------
# 2. The plumbing. No mocks: a comparator fed None forever is silently inert.
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_run_verify_reports_the_runners_own_exit_code(tmp_path: Path):
    """A REAL subprocess, because this is the seam that goes silently inert.

    If ``run_verify`` ever stops carrying the code, every comparison degrades
    to INCOMPARABLE, the new arm never fires in production, and every test that
    supplies codes by hand stays green. Only a real run can catch that.
    """
    two = await run_verify(str(tmp_path), 'python -c "import sys; sys.exit(2)"')
    one = await run_verify(str(tmp_path), 'python -c "import sys; sys.exit(1)"')

    assert run_exit_code(two) == 2
    assert run_exit_code(one) == 1
    assert compare_failures(run_exit_code(one), run_exit_code(two)) is (
        FailureComparison.FAILED_DIFFERENTLY
    )


@pytest.mark.unit
async def test_run_verify_reports_no_exit_code_for_a_timeout(tmp_path: Path):
    """A KILLED process's return code is not a classification.

    Measured on Windows: killing a hung process yields returncode 1, which is
    indistinguishable from "the tests failed". Reporting it would make a
    timeout on one side and a plain test failure on the other read as the same
    failure, and a timeout against a collection error read as a real
    difference. The runner never classified this, so neither do we.
    """
    timed_out = await run_verify(
        str(tmp_path), 'python -c "import time; time.sleep(30)"', timeout=0.5
    )

    assert run_exit_code(timed_out) is None


@pytest.mark.unit
async def test_run_verify_still_unpacks_as_a_two_tuple(tmp_path: Path):
    """The compatibility contract that keeps every existing call site honest.

    ``passed, output = await run_verify(...)`` is spelled at several seats and
    mocked as a plain 2-tuple across the suite. Had the return become a 3-tuple
    the mocks would have had to change en masse; had the call sites switched to
    a differently-named function the mocks would have gone INERT and the real
    command would have been shelled inside the test run.
    """
    passed, output = await run_verify(str(tmp_path), "python -c \"print('ok')\"")

    assert passed is True
    assert "ok" in output


@pytest.mark.unit
def test_run_exit_code_reports_unknown_for_a_plain_tuple():
    """A 2-tuple carries no code, and that is INCOMPARABLE, never a guess."""
    assert run_exit_code((False, "1 failed")) is None


# ---------------------------------------------------------------------------
# 3. The review seat, end to end. Helpers mirror
#    tests/test_leaf_check_that_is_the_project_command.py.
# ---------------------------------------------------------------------------


def _github_backend() -> Any:
    async def _checkout(_ref: Any, dest: str) -> None:
        Path(dest, "src").mkdir(parents=True, exist_ok=True)
        Path(dest, "src", "a.py").write_text("x = 1\n", encoding="utf-8")

    backend = AsyncMock()
    backend.name = "github"
    backend.get_diff.return_value = "diff --git a/src/a.py b/src/a.py\n+x = 1\n"
    backend.checkout.side_effect = _checkout
    return backend


def _gated(project: dict[str, Any]) -> dict[str, Any]:
    gated = dict(project)
    gated["verify_cmd"] = _PROJECT_CMD
    return gated


async def _declare(orch: Any, task_id: str, **fields: Any) -> None:
    task = await orch._tq.get_task(task_id)
    plan = await orch._tq.get_plan(task["plan_id"])
    graph = json.loads(plan["opus_plan"])
    graph["tasks"][0].update(fields)
    await orch._tq._db.execute(
        "UPDATE plans SET opus_plan = ? WHERE id = ?",
        (json.dumps(graph), task["plan_id"]),
    )


def _head_run(orch: Any, monkeypatch: Any, code: int | None) -> AsyncMock:
    """Make the PROJECT command fail on the PR head with ``code``."""
    run = AsyncMock(return_value=VerifyRun(False, _HEAD_TESTS_FAILED, code))
    monkeypatch.setattr(review_mod, "run_verify", run)
    return run


def _base_run(orch: Any, code: int | None, output: str = _BASE_CANNOT_COLLECT) -> None:
    """Make the same command fail on the base branch with ``code``."""

    async def _stub(*_args: Any, **_kwargs: Any) -> _PlanVerifyResult:
        return _PlanVerifyResult("failed", output, returncode=code)

    orch._verify_plan_branch = _stub


async def _review(orch: Any, task_id: str, project: dict[str, Any]) -> None:
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)
    await orch.review_task(task_id, project)


@pytest.mark.unit
async def test_the_leaf_three_shape_is_attributed_and_reaches_triage(
    orchestrator_fixture, monkeypatch
):
    """THE measured case, end to end, at the seat that decided it wrongly.

    A leaf whose declared acceptance IS the project command, a head that failed
    its assertions, a base that could not collect at all. Before this change
    the two were "identical" on status alone, the task PASSED review, and no
    outcome row and no triage call were ever made -- which is why ``split`` had
    never been observed on any run.
    """
    orch, task_id, project = orchestrator_fixture
    await _declare(orch, task_id, verification=_LEAF_3_VERIFICATION)
    orch._resolve_backend = lambda _repo_url: _github_backend()
    run = _head_run(orch, monkeypatch, _HEAD_EXIT)
    _base_run(orch, _BASE_EXIT)
    await orch._tq.retry_task(task_id)
    # ``human`` rather than ``split`` for the assertion, though ``split`` is
    # the decision this arm exists to make reachable: ``human`` is terminal and
    # is the one end state NO other path here can produce. At attempt 2 of a
    # max_retries=3 project the plain path REQUEUES, so a test using ``retry``
    # would pass identically whether the brain was asked or not. (A valid
    # ``split`` payload needs two to four full ``LeafTask`` children, which is
    # the split machinery's own tests to build, not this one's.)
    orch._triage_leaf = AsyncMock(
        return_value=review_mod.TriageDecision(decision="human", reason="too large")
    )

    await _review(orch, task_id, _gated(project))

    task = await orch._tq.get_task(task_id)
    assert task["status"] == TaskStatus.FAILED, (
        "the head failed its own declared acceptance and the base failed a "
        "DIFFERENT way, so nothing here excuses it as pre-existing"
    )
    assert int(task["attempt"]) < int(project["max_retries"]), (
        "the distinguishing fact: this leaf still had a retry left, so only "
        "triage could have made it terminal"
    )
    assert task["triage_decision"] == "human"
    orch._triage_leaf.assert_awaited_once()
    row = await orch._tq._db.fetch_one(
        "SELECT * FROM task_outcomes WHERE task_id = ?", (task_id,)
    )
    assert row is not None, "an attributed verify failure must be countable"
    assert row["outcome"] == "fail"
    assert row["failure_class"] == "verify_fail"
    # The identical command is never shelled a second time: it was already
    # shown red on both trees, and re-running it is the restatement
    # `discriminating_leaf_command` exists to refuse.
    assert run.await_count == 1


@pytest.mark.unit
async def test_a_base_that_failed_the_same_way_is_still_not_attributed(
    orchestrator_fixture, monkeypatch
):
    """The Hindley-Milner case, unchanged. This is the regression guard.

    Leaf 1 of a dependent chain writes exactly its declared scope and the
    project command is red on head and base for a SIBLING's contract, with the
    same exit code. Charging it is the original defect, and no part of this
    change may reintroduce it.
    """
    orch, task_id, project = orchestrator_fixture
    await _declare(orch, task_id, verification=_LEAF_3_VERIFICATION)
    orch._resolve_backend = lambda _repo_url: _github_backend()
    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "fine"}
    _head_run(orch, monkeypatch, _HEAD_EXIT)
    _base_run(orch, _HEAD_EXIT, _HEAD_TESTS_FAILED_RERUN)
    orch._triage_leaf = AsyncMock()

    await _review(orch, task_id, _gated(project))

    task = await orch._tq.get_task(task_id)
    assert task["status"] == TaskStatus.PASSED
    assert "not attributed to this task" in (task["review_feedback"] or "")
    orch._triage_leaf.assert_not_awaited()


@pytest.mark.unit
async def test_two_failures_that_cannot_be_compared_are_not_attributed(
    orchestrator_fixture, monkeypatch
):
    """Fail closed. No exit code on either side is the pre-existing behaviour.

    This is the arm every mocked test in the suite lands in, which is exactly
    why it must be pinned: if "unknown" ever came to mean "different", the
    whole suite would start charging workers and nothing would look wrong.
    """
    orch, task_id, project = orchestrator_fixture
    await _declare(orch, task_id, verification=_LEAF_3_VERIFICATION)
    orch._resolve_backend = lambda _repo_url: _github_backend()
    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "fine"}
    _head_run(orch, monkeypatch, None)
    _base_run(orch, None)
    orch._triage_leaf = AsyncMock()

    await _review(orch, task_id, _gated(project))

    task = await orch._tq.get_task(task_id)
    assert task["status"] == TaskStatus.PASSED
    assert "not attributed to this task" in (task["review_feedback"] or "")
    orch._triage_leaf.assert_not_awaited()


@pytest.mark.unit
async def test_a_leaf_declaring_nothing_is_not_attributed_by_the_comparison(
    orchestrator_fixture, monkeypatch
):
    """The BOUND on the new arm, and the argument for why it is where it is.

    A differently-failing base licenses attribution only where the leaf ITSELF
    declared the project command as its acceptance -- then the bar is the
    leaf's own, not one Praxis imposed. Declaring nothing is the norm on every
    path but decomposition (plan_spec, the improvement loop, a direct dispatch
    that omitted it), and those leaves have made no acceptance claim at all.
    Charging them on a signal designed for the whole-suite leaf would change
    behaviour for the majority population.
    """
    orch, task_id, project = orchestrator_fixture
    await _declare(orch, task_id, verification=None)
    orch._resolve_backend = lambda _repo_url: _github_backend()
    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "fine"}
    _head_run(orch, monkeypatch, _HEAD_EXIT)
    _base_run(orch, _BASE_EXIT)
    orch._triage_leaf = AsyncMock()

    await _review(orch, task_id, _gated(project))

    task = await orch._tq.get_task(task_id)
    assert task["status"] == TaskStatus.PASSED
    orch._triage_leaf.assert_not_awaited()


# ---------------------------------------------------------------------------
# 4. The wording: two different states rendered identically sent an operator
#    to add a verification that already existed.
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_a_nondiscriminating_check_is_not_reported_as_no_check_at_all(
    orchestrator_fixture, monkeypatch
):
    """Leaf 3 DID declare a verification, and was told it had declared none.

    "declared nothing" and "declared something that cannot discriminate" are
    different facts with different remedies. Rendering them identically sends
    the operator to write a check the plan already contains, and hides the one
    thing they could act on: that their leaf's acceptance and their project's
    verify command are the same string.
    """
    orch, task_id, project = orchestrator_fixture
    await _declare(orch, task_id, verification=_LEAF_3_VERIFICATION)
    orch._resolve_backend = lambda _repo_url: _github_backend()
    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "fine"}
    _head_run(orch, monkeypatch, _HEAD_EXIT)
    _base_run(orch, _HEAD_EXIT, _HEAD_TESTS_FAILED_RERUN)

    await _review(orch, task_id, _gated(project))

    stored = (await orch._tq.get_task(task_id))["review_feedback"] or ""
    assert LEAF_CHECK_NONDISCRIMINATING in stored
    assert LEAF_CHECK_NONE not in stored


@pytest.mark.unit
async def test_a_leaf_that_really_declared_nothing_still_says_so(
    orchestrator_fixture, monkeypatch
):
    """The other half. Distinct wording is worth nothing if only one is used."""
    orch, task_id, project = orchestrator_fixture
    await _declare(orch, task_id, verification=None)
    orch._resolve_backend = lambda _repo_url: _github_backend()
    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "fine"}
    _head_run(orch, monkeypatch, _HEAD_EXIT)
    _base_run(orch, _HEAD_EXIT, _HEAD_TESTS_FAILED_RERUN)

    await _review(orch, task_id, _gated(project))

    stored = (await orch._tq.get_task(task_id))["review_feedback"] or ""
    assert LEAF_CHECK_NONE in stored
    assert LEAF_CHECK_NONDISCRIMINATING not in stored


@pytest.mark.unit
def test_the_two_leaf_check_phrases_are_not_substrings_of_each_other():
    """Or the assertions above pass whichever phrase the code emits.

    Same family as the rich-markup fixture that renders verbatim whatever the
    code does: an assertion that cannot distinguish its two cases is inert.
    """
    assert LEAF_CHECK_NONE not in LEAF_CHECK_NONDISCRIMINATING
    assert LEAF_CHECK_NONDISCRIMINATING not in LEAF_CHECK_NONE


# ---------------------------------------------------------------------------
# 5. One rule, every seat. A seat that stays green has not adopted it.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("head_code", "base_code", "expected"),
    [
        (1, 1, FailureComparison.FAILED_ALIKE),
        (1, 2, FailureComparison.FAILED_DIFFERENTLY),
        (2, 1, FailureComparison.FAILED_DIFFERENTLY),
        (None, 1, FailureComparison.INCOMPARABLE),
        (1, None, FailureComparison.INCOMPARABLE),
    ],
)
def test_every_seat_classifies_a_red_base_the_same_way(
    head_code: int | None, base_code: int | None, expected: FailureComparison
):
    """The wave gate and the plan backstop must agree with the comparator.

    Nothing else holds the three seats together: they live in two modules and
    reach the question by different routes. Driving all of them over one table
    is what makes a mutation of the shared rule turn every consumer red.
    """
    head = _PlanVerifyResult("failed", _HEAD_TESTS_FAILED, returncode=head_code)
    base = _PlanVerifyResult("failed", _BASE_CANNOT_COLLECT, returncode=base_code)
    clause = base_failure_clause(expected, "main", head_code, base_code)

    wave = attribute_wave_verify_failure(head, base, "main")
    plan = attribute_plan_verify_failure(head, base, "main")

    assert clause in wave.detail, "the wave gate did not adopt the shared clause"
    assert clause in plan.detail, "the plan backstop did not adopt the shared clause"


@pytest.mark.unit
def test_the_wave_gate_never_parks_a_plan_on_a_differently_failing_base():
    """The licence the wave gate deliberately does NOT take.

    A memoized park is PERMANENT: ``merged_count`` cannot advance while the
    wave is parked, so nothing can ever clear it, and every leaf stays a
    healthy PENDING while the plan reads ACTIVE. That is the shape this gate
    was already burned by. At plan scope there is no leaf whose own declared
    acceptance licenses the charge, so a changed failure mode buys a truthful
    SENTENCE and nothing more.
    """
    head = _PlanVerifyResult("failed", _HEAD_TESTS_FAILED, returncode=1)
    base = _PlanVerifyResult("failed", _BASE_CANNOT_COLLECT, returncode=2)

    decision = attribute_wave_verify_failure(head, base, "main")

    assert decision.park is False
    assert "identically" not in decision.detail


@pytest.mark.unit
def test_the_plan_backstop_does_alarm_on_a_differently_failing_base():
    """The licence the plan backstop DOES take, and why the two differ.

    This seat is advisory: the integration PR is opened on every arm, before
    this existed and after. So the cost of alarming is an operator's attention
    and the cost of staying silent is a real cross-leaf regression that changed
    the failure class going unreported.
    """
    head = _PlanVerifyResult("failed", _HEAD_TESTS_FAILED, returncode=1)
    base = _PlanVerifyResult("failed", _BASE_CANNOT_COLLECT, returncode=2)

    decision = attribute_plan_verify_failure(head, base, "main")

    assert decision.alarm is True
    assert decision.reported_status == "failed"
