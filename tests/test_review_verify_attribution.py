"""A project verify command is the bar for a REGRESSION, not for a leaf.

Measured live twice on ``adiatmaja/playground``. A two-leaf Hindley-Milner plan
was decomposed into a dependent chain. Leaf 1 wrote 322 lines of exactly its
declared scope and was FAILED, because the project's ``pytest`` collects an
acceptance file importing ``infer_type`` -- which is LEAF 2's contract. The base
branch failed the identical command identically, so the gate charged a leaf with
a failure that pre-existed on the branch it was cut from. Capability-aware
decomposition produces dependent chains exactly when it is doing its most
valuable work, so the mechanism defeated itself precisely where it mattered.

Meanwhile the decomposer emits a correct per-leaf ``verification`` (leaf 1 got
``python -c "from playground.hm import TypeVar, ...; print('ok')"``), the
decomposition standard HARD-requires it and F3 validates that it is runnable --
and nothing ever ran it.

Every assertion here is a POSITIVE fact about what was decided and on what
evidence. "The task did not fail" is true of a review that never ran at all, so
no test rests on it alone.
"""
# ruff: noqa: S101

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from orchestrator.core import orchestrator_review as review_mod
from orchestrator.core.capability_history import fetch_recent_outcomes
from orchestrator.core.leaf_validator import (
    is_runnable_verification,
    shell_command_for_verification,
)
from orchestrator.core.orchestrator_review import _PlanVerifyResult
from orchestrator.models.schemas import TaskStatus


_DIFF = "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n+x = 1\n"


# ---------------------------------------------------------------------------
# 1. What Praxis is willing to hand to a shell, and what it refuses.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "prose",
    [
        pytest.param("the module imports cleanly", id="declarative"),
        pytest.param("all existing tests still pass", id="all-tests-pass"),
        pytest.param("TypeVar, Con and Fun are importable", id="names-symbols"),
        # "Run `pytest -q` and confirm it passes" USED to sit here, and it was
        # pinning the defect rather than the contract: the decompose prompt
        # teaches that exact shape in its own worked example, so refusing it
        # made the review path's positive signal unreachable on the decompose
        # path. It is now accepted, and lives in
        # tests/test_leaf_validator_verification_contract.py. What survives here
        # is the case that is genuinely a guess: backticks around a FILE, which
        # a leaf writes far more often than it names a script.
        pytest.param(
            "Confirm `src/client.py` defines retry_on_429", id="backticked-file-path"
        ),
        pytest.param("step one: build\nstep two: test", id="two-lines"),
        pytest.param("cd subdir && pytest -q", id="unrecognised-leading-token"),
    ],
)
def test_prose_the_validator_accepts_is_still_never_shelled(prose: str):
    """The single decision this fix rests on, and the one it could invert.

    Both halves are asserted on the SAME value, so the test creates the
    difference it claims rather than inheriting one from a fixture: the
    validator says this is good enough to let a leaf through, and Praxis still
    refuses to run it. Handing ``"the module imports cleanly"`` to a shell
    yields ``the: command not found``, exit 127, and a task FAILED on evidence
    Praxis fabricated about a worker -- a new false accusation in place of the
    old one.

    ``is_runnable_verification`` is asserted True first because if it ever
    starts rejecting these, the second assertion becomes vacuous and this test
    would keep passing while guarding nothing.
    """
    assert is_runnable_verification(prose) is True
    assert shell_command_for_verification(prose) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # The measured leaf-1 command, verbatim from the live plan.
        pytest.param(
            "python -c \"from playground.hm import TypeVar; print('ok')\"",
            "python -c \"from playground.hm import TypeVar; print('ok')\"",
            id="the-measured-command",
        ),
        pytest.param(
            "pytest -q tests/test_hm.py", "pytest -q tests/test_hm.py", id="pytest"
        ),
        pytest.param("`uv run pytest -q`", "uv run pytest -q", id="backtick-wrapped"),
        pytest.param("  npm test  ", "npm test", id="stripped"),
        pytest.param(
            "PYTHONPATH=src pytest -q", "PYTHONPATH=src pytest -q", id="env-prefix"
        ),
        pytest.param("./scripts/check.sh", "./scripts/check.sh", id="path-executable"),
        # Found by running this against a REAL repository rather than by
        # reading: a program whose path contains a space is quoted, and the
        # quote belongs to the shell, not to the program's name. Refusing it
        # reported a leaf that HAD declared a check as declaring none.
        pytest.param(
            '"C:\\venv\\Scripts\\python.exe" -c "import leaf"',
            '"C:\\venv\\Scripts\\python.exe" -c "import leaf"',
            id="quoted-windows-path",
        ),
        pytest.param(
            "'/opt/my tools/check' --all",
            "'/opt/my tools/check' --all",
            id="quoted-posix-path",
        ),
        pytest.param("python3.11 -m pytest", "python3.11 -m pytest", id="versioned"),
        pytest.param(
            "test -f dist/bundle.js", "test -f dist/bundle.js", id="posix-test"
        ),
    ],
)
def test_a_real_command_is_accepted_verbatim(value: str, expected: str):
    """Refusing everything would be safe and useless: the positive half."""
    assert shell_command_for_verification(value) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        pytest.param(None, id="none"),
        pytest.param("   ", id="blank"),
        pytest.param({"cmd": "pytest -q"}, id="dict"),
        pytest.param(["pytest", "-q"], id="list"),
        pytest.param(42, id="int"),
    ],
)
def test_a_value_that_is_not_a_command_string_is_absent(value: Any):
    """Raw brain JSON is any shape, and a repr must never reach a shell."""
    assert shell_command_for_verification(value) is None


# ---------------------------------------------------------------------------
# 2. The review path: who a red project command belongs to.
# ---------------------------------------------------------------------------


def _backend() -> Any:
    """A GitHub backend whose PR-head checkout is a real directory on disk."""

    async def _checkout(_ref: Any, dest: str) -> None:
        Path(dest, "src").mkdir(parents=True, exist_ok=True)
        Path(dest, "src", "a.py").write_text("x = 1\n", encoding="utf-8")

    backend = AsyncMock()
    backend.name = "github"
    backend.get_diff.return_value = _DIFF
    backend.checkout.side_effect = _checkout
    return backend


def _base_verify(orch: Any, result: _PlanVerifyResult) -> list[tuple[Any, ...]]:
    """Answer the base-branch comparison with ``result``, recording its args.

    Patched at the METHOD, not at ``run_verify``: the two verify runs in this
    path answer different questions about different trees, and a single module
    level stub would make them indistinguishable -- which is the exact
    conflation the fix exists to end.
    """
    calls: list[tuple[Any, ...]] = []

    async def _fake(*args: Any, **kwargs: Any) -> _PlanVerifyResult:
        calls.append(args)
        return result

    orch._verify_plan_branch = _fake
    return calls


async def _declare(orch: Any, task_id: str, **fields: Any) -> None:
    """Rewrite this task's entry in the plan graph with ``fields``."""
    task = await orch._tq.get_task(task_id)
    plan = await orch._tq.get_plan(task["plan_id"])
    graph = json.loads(plan["opus_plan"])
    graph["tasks"][0].update(fields)
    await orch._tq._db.execute(
        "UPDATE plans SET opus_plan = ? WHERE id = ?",
        (json.dumps(graph), task["plan_id"]),
    )


async def _review(orch: Any, task_id: str, project: dict[str, Any]) -> None:
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)
    await orch.review_task(task_id, project)


def _gated(project: dict[str, Any]) -> dict[str, Any]:
    gated = dict(project)
    gated["verify_cmd"] = "python -m pytest src/playground -q"
    return gated


@pytest.mark.unit
async def test_a_failure_that_is_new_on_this_head_still_fails_the_task(
    orchestrator_fixture, monkeypatch
):
    """THE unchanged path, and the one the whole fix must not weaken.

    The project command PASSES on the base branch and fails on the PR head, so
    this task is the only thing that changed and the failure is its own. The
    brain is never asked, exactly as before.
    """
    orch, task_id, project = orchestrator_fixture
    orch._resolve_backend = lambda _repo_url: _backend()
    monkeypatch.setattr(
        review_mod, "run_verify", AsyncMock(return_value=(False, "E   ImportError"))
    )
    _base_verify(orch, _PlanVerifyResult("passed"))

    await _review(orch, task_id, _gated(project))

    orch._opus.review_diff.assert_not_awaited()
    updated = await orch._tq.get_task(task_id)
    assert updated["status"] in (TaskStatus.FAILED, TaskStatus.PENDING)
    stored = updated["review_feedback"] or ""
    assert "ImportError" in stored
    # The reason it was charged here, stated: the command is green on the base.
    assert "PASSES on plan/x" in stored


@pytest.mark.unit
async def test_a_failure_that_pre_exists_on_the_base_is_not_charged_to_the_task(
    orchestrator_fixture, monkeypatch
):
    """The measured defect, inverted.

    The identical command fails identically on the branch this work was cut
    from, and the leaf declares no runnable verification of its own. The gate
    therefore establishes nothing about this task, the review proceeds to the
    brain, and the human at the merge gate is TOLD -- in the stored feedback,
    which is what ``praxis task``, MCP ``poll_task`` and the dashboard render.
    """
    orch, task_id, project = orchestrator_fixture
    orch._resolve_backend = lambda _repo_url: _backend()
    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "looks right"}
    monkeypatch.setattr(
        review_mod, "run_verify", AsyncMock(return_value=(False, "E   ImportError"))
    )
    _base_verify(orch, _PlanVerifyResult("failed", "E   ImportError"))

    await _review(orch, task_id, _gated(project))

    # The review really did proceed; without this, a task stuck REVIEWING would
    # satisfy every assertion below by never reaching a verdict.
    orch._opus.review_diff.assert_awaited_once()
    updated = await orch._tq.get_task(task_id)
    assert updated["status"] == TaskStatus.PASSED
    stored = updated["review_feedback"] or ""
    assert "fails identically on plan/x" in stored
    assert "not attributed to this task" in stored
    assert "declared no runnable verification of its own" in stored


@pytest.mark.unit
async def test_the_leaf_runs_its_own_declared_verification_in_the_head_checkout(
    orchestrator_fixture, monkeypatch
):
    """The positive signal the decomposer has always emitted and nobody ran.

    Two facts, and the second is the one that goes wrong silently: the leaf's
    OWN command is what ran, and it ran in the SAME directory the project
    command ran in. A second fetch could observe a different state of the
    branch and decide a leaf's fate from a mixture of the two.
    """
    orch, task_id, project = orchestrator_fixture
    await _declare(orch, task_id, verification="python -c \"import a; print('ok')\"")
    orch._resolve_backend = lambda _repo_url: _backend()
    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "ok"}
    verify = AsyncMock(side_effect=[(False, "E   ImportError"), (True, "ok")])
    monkeypatch.setattr(review_mod, "run_verify", verify)
    _base_verify(orch, _PlanVerifyResult("failed", "E   ImportError"))

    await _review(orch, task_id, _gated(project))

    assert verify.await_count == 2
    head_call, leaf_call = verify.await_args_list
    assert head_call.args[1] == "python -m pytest src/playground -q"
    assert leaf_call.args[1] == "python -c \"import a; print('ok')\""
    assert leaf_call.args[0] == head_call.args[0], "the leaf check refetched the branch"
    updated = await orch._tq.get_task(task_id)
    assert updated["status"] == TaskStatus.PASSED
    assert "own verification passed" in (updated["review_feedback"] or "")


@pytest.mark.unit
async def test_a_leaf_whose_own_verification_fails_is_failed_on_that_evidence(
    orchestrator_fixture, monkeypatch
):
    """The leaf's own check is evidence ABOUT the leaf, so it decides.

    The stored reason must carry the DECLARED command's output. Reporting the
    project command's output here is what sent a stack trace about a sibling's
    contract to the next worker and to the triage brain.
    """
    orch, task_id, project = orchestrator_fixture
    await _declare(orch, task_id, verification='python -c "import a"')
    orch._resolve_backend = lambda _repo_url: _backend()
    monkeypatch.setattr(
        review_mod,
        "run_verify",
        AsyncMock(
            side_effect=[
                (False, "sibling contract missing: infer_type"),
                (False, "ModuleNotFoundError: no module named a"),
            ]
        ),
    )
    _base_verify(orch, _PlanVerifyResult("failed", "sibling contract missing"))

    await _review(orch, task_id, _gated(project))

    orch._opus.review_diff.assert_not_awaited()
    updated = await orch._tq.get_task(task_id)
    assert updated["status"] in (TaskStatus.FAILED, TaskStatus.PENDING)
    stored = updated["review_feedback"] or ""
    assert "ModuleNotFoundError" in stored
    assert "infer_type" not in stored, (
        "the sibling's failure was reported as this leaf's"
    )
    rows = await orch._tq._db.fetch_all(
        "SELECT * FROM task_outcomes WHERE task_id = ?", (task_id,)
    )
    assert [r["failure_class"] for r in rows] == ["verify_fail"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "verdict",
    [
        pytest.param(_PlanVerifyResult("error"), id="error"),
        pytest.param(
            _PlanVerifyResult("skipped", reason="no GitHub token for repo"),
            id="skipped-no-token",
        ),
    ],
)
async def test_a_comparison_that_could_not_be_made_fails_closed_and_says_so(
    orchestrator_fixture, monkeypatch, verdict
):
    """An unanswered question must never buy a task a pass.

    ``error`` and every skip mean the gate did not produce an ANSWER about the
    base branch. The old behaviour stands, and the feedback says the comparison
    is missing rather than implying it was made -- that string is injected
    verbatim into the next worker's prompt.
    """
    orch, task_id, project = orchestrator_fixture
    orch._resolve_backend = lambda _repo_url: _backend()
    monkeypatch.setattr(
        review_mod, "run_verify", AsyncMock(return_value=(False, "E   ImportError"))
    )
    _base_verify(orch, verdict)

    await _review(orch, task_id, _gated(project))

    orch._opus.review_diff.assert_not_awaited()
    updated = await orch._tq.get_task(task_id)
    assert updated["status"] in (TaskStatus.FAILED, TaskStatus.PENDING)
    stored = updated["review_feedback"] or ""
    assert "ImportError" in stored
    assert "could NOT be established" in stored


@pytest.mark.unit
async def test_an_unestablished_attribution_is_recorded_but_never_counted(
    orchestrator_fixture, monkeypatch
):
    """Saying "could NOT be established" and then recording it as established.

    The feedback above states in words that whether this failure pre-dates the
    task is unknown, because the base branch could not be ASKED. The row written
    beside it said ``verify_fail``, which ``failure_taxonomy`` counts against the
    worker -- so the capability gate was fed an unanswered question as an answer.
    ``handle_declined_no_change`` applies the opposite rule to the same
    uncertainty one seat over.

    Both halves are asserted on the SAME run, so the test creates the difference
    it claims rather than inheriting one: the row EXISTS (withdrawing a claim is
    not the same as losing the attempt, and a silently missing row is its own
    defect) and ``fetch_recent_outcomes`` -- the query the capability gate
    actually issues -- does not return it. Asserting only the NULL would pass for
    a row that was never written at all.
    """
    orch, task_id, project = orchestrator_fixture
    orch._resolve_backend = lambda _repo_url: _backend()
    monkeypatch.setattr(
        review_mod, "run_verify", AsyncMock(return_value=(False, "E   ImportError"))
    )
    _base_verify(orch, _PlanVerifyResult("error"))

    await _review(orch, task_id, _gated(project))

    rows = await orch._tq._db.fetch_all(
        "SELECT * FROM task_outcomes WHERE task_id = ?", (task_id,)
    )
    assert [(r["outcome"], r["failure_class"]) for r in rows] == [("fail", None)]
    counted = await fetch_recent_outcomes(
        orch._tq._db, str(project["model_name"]), str(project["id"])
    )
    assert counted == [], (
        "an attribution nobody could establish was handed to the capability "
        "gate as a measured worker failure"
    )


@pytest.mark.unit
async def test_prose_is_never_shelled_on_the_review_path_either(
    orchestrator_fixture, monkeypatch
):
    """The helper's refusal reaches the CALL SITE, not just the helper.

    A guard on a helper does not guard the call site. ``run_verify`` is awaited
    exactly ONCE -- the project command -- which is only true if the prose was
    treated as absent rather than handed to a shell.
    """
    orch, task_id, project = orchestrator_fixture
    await _declare(orch, task_id, verification="the module imports cleanly")
    orch._resolve_backend = lambda _repo_url: _backend()
    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "ok"}
    verify = AsyncMock(return_value=(False, "E   ImportError"))
    monkeypatch.setattr(review_mod, "run_verify", verify)
    _base_verify(orch, _PlanVerifyResult("failed"))

    await _review(orch, task_id, _gated(project))

    assert verify.await_count == 1
    updated = await orch._tq.get_task(task_id)
    assert updated["status"] == TaskStatus.PASSED
    assert "declared no runnable verification" in (updated["review_feedback"] or "")


@pytest.mark.unit
async def test_the_declared_verification_of_the_right_leaf_is_the_one_that_runs(
    orchestrator_fixture, monkeypatch
):
    """The graph join is positional, and judging a row by a sibling's contract
    is the failure mode that looks exactly like working.

    Two leaves, two different declared commands. The reviewed row is the FIRST,
    so its command -- not the second's -- must be the one shelled.
    """
    orch, task_id, project = orchestrator_fixture
    task = await orch._tq.get_task(task_id)
    plan_id = task["plan_id"]
    plan = await orch._tq.get_plan(plan_id)
    graph = json.loads(plan["opus_plan"])
    graph["tasks"][0]["verification"] = "pytest -q tests/test_leaf_one.py"
    graph["tasks"].append(
        {
            "id": "b",
            "slug": "b",
            "title": "B",
            "description": "Second",
            "depends_on": ["a"],
            "verification": "pytest -q tests/test_leaf_two.py",
        }
    )
    await orch._tq._db.execute(
        "UPDATE plans SET opus_plan = ? WHERE id = ?", (json.dumps(graph), plan_id)
    )
    orch._resolve_backend = lambda _repo_url: _backend()
    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "ok"}
    verify = AsyncMock(side_effect=[(False, "E   ImportError"), (True, "1 passed")])
    monkeypatch.setattr(review_mod, "run_verify", verify)
    _base_verify(orch, _PlanVerifyResult("failed"))

    await _review(orch, task_id, _gated(project))

    assert verify.await_args_list[1].args[1] == "pytest -q tests/test_leaf_one.py"


# ---------------------------------------------------------------------------
# 3. WHICH branch the comparison is made against.
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_the_comparison_runs_against_the_plan_branch_for_a_github_pr(
    orchestrator_fixture, monkeypatch
):
    """A GitHub PR URL encodes no base, so the plan branch is the fallback.

    Comparing against the WRONG branch is how this whole mechanism goes
    silently wrong: "the same command fails there too" is evidence only when
    "there" is the tree this work was cut from. So the exact arguments are
    asserted, not merely that some comparison happened.
    """
    orch, task_id, project = orchestrator_fixture
    orch._resolve_backend = lambda _repo_url: _backend()
    monkeypatch.setattr(
        review_mod, "run_verify", AsyncMock(return_value=(False, "boom"))
    )
    calls = _base_verify(orch, _PlanVerifyResult("passed"))

    await _review(orch, task_id, _gated(project))

    assert calls == [
        (
            "https://github.com/o/r",
            "plan/x",
            "python -m pytest src/playground -q",
        )
    ]


@pytest.mark.unit
async def test_a_local_ref_names_its_own_base_and_that_base_wins(
    orchestrator_fixture, monkeypatch
):
    """``praxis-local://`` carries the base the merge actually writes to.

    In single-branch mode the plan branch and the PR base DIVERGE, so taking
    the plan branch when the ref names one would compare against a tree the
    merge never touches.
    """
    orch, task_id, project = orchestrator_fixture
    await orch._tq.set_task_pr_url(
        task_id, "praxis-local://pr?branch=work%2Fshared&base=release%2F2"
    )
    backend = _backend()
    backend.name = "local"
    orch._resolve_backend = lambda _repo_url: backend
    monkeypatch.setattr(
        review_mod, "run_verify", AsyncMock(return_value=(False, "boom"))
    )
    calls = _base_verify(orch, _PlanVerifyResult("passed"))

    await _review(orch, task_id, _gated(project))

    assert [args[1] for args in calls] == ["release/2"]


@pytest.mark.unit
async def test_with_no_plan_branch_the_project_default_is_the_base(
    orchestrator_fixture, monkeypatch
):
    """The last fallback, and the same one ``no_change_outcome`` already uses.

    Two seats answering "which branch was this cut from" must not disagree
    about a plan with no branch recorded.
    """
    orch, task_id, project = orchestrator_fixture
    task = await orch._tq.get_task(task_id)
    await orch._tq._db.execute(
        "UPDATE plans SET plan_branch_name = NULL WHERE id = ?", (task["plan_id"],)
    )
    orch._resolve_backend = lambda _repo_url: _backend()
    monkeypatch.setattr(
        review_mod, "run_verify", AsyncMock(return_value=(False, "boom"))
    )
    calls = _base_verify(orch, _PlanVerifyResult("passed"))

    await _review(orch, task_id, _gated(project))

    assert [args[1] for args in calls] == ["main"]
