"""A no-op must be refuted by the leaf's own declared edit locations.

Measured live on 2026-08-25. A task asked for a NEW subpackage of eleven
modules under ``src/playground/expr/``, none of which existed, and declared
``python -m pytest src/playground -q`` as its acceptance check. The worker wrote
nothing, ran that command, watched the repository's 294 pre-existing tests pass,
and reported ``no_changes``. ``no_change_outcome`` then verified the BASE BRANCH
with the project's ``verify_cmd`` -- the same command -- got a pass, and closed
the leaf as NO_CHANGES: terminal, SATISFIED, and therefore unblocking every
dependent leaf. Nothing had been written.

The verify command answers "is this repository healthy", not "was THIS task's
work done". For any leaf whose acceptance is not expressible as "the existing
suite passes", a healthy repository makes every empty diff read as "already
done". The leaf's own ``files`` list is the missing discriminator: a path the
leaf declared and the base branch does not carry proves the tree does not
satisfy the leaf, whatever the command reports.

Every test that can drives a REAL bare repo on disk, because the seam that went
wrong is precisely the one a stub would paper over. The positive control is
LAST and deliberately so: the measured "task 1 wrote task 2's files" shape is
the reason this whole mechanism exists, and a fix that fails those plans is
worse than the defect.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient

from orchestrator.core.event_bus import EventBus
from orchestrator.core.git_ops import GitOps
from orchestrator.core.github_credentials import PatCredentialProvider
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.orchestrator_review import (
    _SKIP_BENCH_MODE_DISABLED,
    _SKIP_NO_VERIFY_CMD,
    _DeclaredPathCheck,
    _no_op_evidence,
)
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus

# The same bare-repo builder the local verify-gate suite drives: ``main`` holds
# ``return 1`` in ``app.py`` and the pushed branch ``agent/fix`` holds
# ``return 2``. One definition of "a real local repo" in the suite.
from tests.test_local_mode_e2e import seeded as _seeded_bare_repo


bare_repo = _seeded_bare_repo

_REVIEW_LOGGER = "orchestrator.core.orchestrator_review"

# The plan branch the leaf was cut from. ``seeded`` pushes it; the gate keys on
# the branch STRING it is handed, never on the name's shape.
_PLAN_BRANCH = "agent/fix"

# Passes ONLY on the plan branch (``return 2`` exists nowhere else), so these
# tests still prove the gate ran in the right tree while reproducing the
# measured shape: a verify command that says the repository is healthy.
_VERIFY_PASSES = (
    f'"{sys.executable}" -c "'
    "import sys; src = open('app.py').read(); print(src); "
    "sys.exit(0 if 'return 2' in src else 1)\""
)

# The eleven-module case, shrunk: a path the leaf declares and the branch does
# not carry.
_ABSENT = "src/expr/lexer.py"

# The three shapes a finished path check can have, named once so the reason
# table below reads as the five facts it is pinning.
_NOTHING_DECIDED = _DeclaredPathCheck(unresolvable=("a.py",))
_ALL_PRESENT = _DeclaredPathCheck(present=("a.py",))
_ONE_MISSING = _DeclaredPathCheck(present=("a.py",), missing=("b.py",))


def _orchestrator(db: Database) -> Orchestrator:
    """An Orchestrator wired exactly as a credential-less local deployment is."""
    return Orchestrator(
        task_queue=TaskQueue(db),
        agent_manager=MagicMock(),
        opus_bridge=AsyncMock(),
        git_ops=GitOps(PatCredentialProvider("")),
        event_bus=EventBus(),
        context_sync=None,
    )


async def _seed(
    db: Database,
    repo_url: str,
    verify_cmd: str | None,
    first_files: Any = None,
    second_files: Any = None,
) -> tuple[TaskQueue, str, str, dict[str, Any]]:
    """Seed a two-leaf plan and return the queue, plan id, SECOND task id, project.

    Two leaves, not one, and only the second declares the absent path. The join
    from a task ROW back to its graph entry is POSITIONAL, so a lookup that
    drifted by one would read leaf 1's declaration -- which is satisfied -- and
    close the leaf under test as a no-op again.
    """
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u1", "User", "hash"),
    )
    await db.execute(
        """INSERT INTO projects
           (id, user_id, name, repo_url, model_name, max_retries,
            default_branch, verify_cmd)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("p1", "u1", "App", repo_url, "qwen3.8-27b", 3, "main", verify_cmd),
    )
    task_queue = TaskQueue(db)
    plan_id = await task_queue.create_plan("p1", "Build an expression evaluator")
    first: dict[str, Any] = {
        "title": "Write the lexer",
        "slug": "lexer",
        "description": "Write it",
        "depends_on": [],
    }
    second: dict[str, Any] = {
        "title": "Write the parser",
        "slug": "parser",
        "description": "Write it",
        "depends_on": ["lexer"],
    }
    if first_files is not None:
        first["files"] = first_files
    if second_files is not None:
        second["files"] = second_files
    await task_queue.activate_plan(
        plan_id,
        {"plan_summary": "Expr", "plan_slug": "expr", "tasks": [first, second]},
        _PLAN_BRANCH,
    )
    rows = await task_queue.get_tasks_for_plan(plan_id)
    project = await task_queue.get_project("p1")
    assert project is not None
    return task_queue, plan_id, str(rows[1]["id"]), dict(project)


@pytest.mark.integration
async def test_a_declared_path_absent_from_the_base_branch_refutes_the_no_op(
    db: Database, bare_repo: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """THE measured case, end to end, with nothing stubbed.

    A verify command that PASSES on the branch the leaf was cut from, and a
    leaf declaring a file that branch does not carry. Before this fix the
    passing command closed the leaf as NO_CHANGES, which is terminal, counts as
    satisfied, and releases every dependent leaf onto work that was never done.
    """
    orch = _orchestrator(db)
    task_queue, plan_id, task_id, project = await _seed(
        db,
        str(bare_repo),
        _VERIFY_PASSES,
        first_files=["app.py"],
        second_files=[_ABSENT],
    )
    plan = await task_queue.get_plan(plan_id)

    with caplog.at_level(logging.INFO, logger=_REVIEW_LOGGER):
        closed, why = await orch.no_change_outcome(task_id, project, plan)

    assert closed is False, (
        "a leaf asking for a file that does not exist was closed as "
        "terminally satisfied because the repository's own suite passed"
    )
    task = await task_queue.get_task(task_id)
    assert task is not None
    assert task["status"] != TaskStatus.NO_CHANGES
    # The reason is written to tasks.review_feedback, published, and injected
    # into the next worker's prompt by the Bible. It has to name the path, or
    # the retry is spent on a worker guessing what was wrong.
    assert _ABSENT in why, why
    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any(_ABSENT in m for m in warnings), warnings


@pytest.mark.integration
async def test_a_project_with_no_verify_cmd_still_gets_the_declared_paths_checked(
    db: Database, bare_repo: Any
) -> None:
    """The weakest link gains its first evidence.

    With no ``verify_cmd`` the only thing backing a no-op was the harness's
    clean exit, documented as the weakest link here. A declared file that the
    branch does not carry is better evidence than that, so the branch is now
    fetched for the path check alone rather than the gate returning before it
    does any I/O.
    """
    orch = _orchestrator(db)
    task_queue, plan_id, task_id, project = await _seed(
        db, str(bare_repo), None, second_files=[_ABSENT]
    )
    plan = await task_queue.get_plan(plan_id)

    closed, why = await orch.no_change_outcome(task_id, project, plan)

    assert closed is False, (
        "with no verify command the harness's clean exit was the only "
        "evidence, and a declared file that is not there outranks it"
    )
    assert _ABSENT in why, why


# ---------------------------------------------------------------------------
# The tree that could not be fetched. Asking for the declared paths is what
# now makes this method reach for a repository it used to skip without any I/O
# when no verify command was configured, so a fetch failure reaches a decision
# it never used to reach. The rule: the gate's own answer was already settled
# before that fetch, and a fetch that exists only to answer ``require_paths``
# must not change it. With a verify command configured NOTHING changes, because
# there the failure means a check the operator asked for did not run.
# ---------------------------------------------------------------------------


def _unfetchable(kind: str) -> Any:
    """Return a ``git_ops`` wired so a GitHub branch cannot be fetched.

    Three genuinely different faults, not one: they produce three different
    verdicts from the gate (two ``skipped`` reasons and an ``error``), and a
    rule that only covered one of them would leave the other two converting a
    leaf that used to close into a failure.
    """
    if kind == "no-provider":
        return None
    if kind == "no-token":
        return GitOps(PatCredentialProvider(""))
    provider = AsyncMock()
    provider.token_for_repo.return_value = "tok"
    git = MagicMock()
    git._provider = provider
    return git


def _orchestrator_with(db: Database, git: Any) -> Orchestrator:
    orch = Orchestrator(
        task_queue=TaskQueue(db),
        agent_manager=MagicMock(),
        opus_bridge=AsyncMock(),
        git_ops=git,
        event_bus=EventBus(),
        context_sync=None,
    )
    orch._git = git
    return orch


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["no-provider", "no-token", "clone-raises"])
@pytest.mark.parametrize(
    ("disabled_reason", "expected_reason"),
    [
        (None, _SKIP_NO_VERIFY_CMD),
        (_SKIP_BENCH_MODE_DISABLED, _SKIP_BENCH_MODE_DISABLED),
    ],
    ids=["no-verify-cmd", "bench-disabled"],
)
async def test_an_unfetchable_tree_leaves_the_gates_own_answer_alone(
    db: Database, kind: str, disabled_reason: str | None, expected_reason: str
) -> None:
    """With nothing to run, a failed fetch must report the skip it always did.

    Before ``require_paths`` existed this method returned here without touching
    the network at all. Asking for the declared paths is the only reason it now
    tries, so a failure of that attempt is a fact about the DEPLOYMENT, not
    about the leaf, and must not be dressed up as a verdict on it.

    Both skip reasons are covered because the downgrade has to report the one
    the CALLER established. A bench run that hardcoded ``no verify_cmd
    configured`` here would put a false statement about the project into its
    own published records.
    """
    orch = _orchestrator_with(db, _unfetchable(kind))

    result = await orch._verify_plan_branch(
        "https://github.com/u/a",
        "plan/x",
        None,
        disabled_reason=disabled_reason,
        require_paths=[_ABSENT],
    )

    assert result.status == "skipped", f"the gate had nothing to run: {result!r}"
    assert result.reason == expected_reason
    # None, not an empty check: the declared paths went UNANSWERED, and an
    # empty ``missing`` from a check that never ran must never be readable as
    # "everything the leaf declared is there".
    assert result.paths is None


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["no-provider", "no-token", "clone-raises"])
async def test_an_unfetchable_tree_still_refuses_when_a_command_was_configured(
    db: Database, kind: str
) -> None:
    """The countercase, and the reason the rule above is conditional.

    Here the operator DID configure a check and the gate could not reach the
    repository to run it. That is a broken deployment, the pre-existing
    fail-closed refusal, and it must survive untouched: a rule that downgraded
    every unfetchable tree would silently green exactly the leaves
    ``_no_op_evidence`` refuses on purpose.
    """
    orch = _orchestrator_with(db, _unfetchable(kind))

    result = await orch._verify_plan_branch(
        "https://github.com/u/a", "plan/x", "pytest -q", require_paths=[_ABSENT]
    )

    assert result.status in ("skipped", "error")
    assert result.reason != _SKIP_NO_VERIFY_CMD, (
        "a configured command that could not run was reported as no command "
        "being configured, which is a false statement about the project"
    )
    assert _no_op_evidence(result, "plan/x") is None, (
        "a leaf was closed as satisfied on a gate that could not reach the "
        "repository it was asked to check"
    )


@pytest.mark.integration
async def test_an_unfetchable_tree_closes_a_no_verify_cmd_leaf_as_it_used_to(
    db: Database,
) -> None:
    """The DECISION, not just the verdict: the leaf still closes.

    This is the behavior that predates the declared-path check, and the whole
    point of the rule is that asking a question we could not answer did not
    take it away. The stored reason says the check did not run.
    """
    orch = _orchestrator_with(db, _unfetchable("no-token"))
    task_queue, plan_id, task_id, project = await _seed(
        db, "https://github.com/u/a", None, second_files=[_ABSENT]
    )
    plan = await task_queue.get_plan(plan_id)

    closed, why = await orch.no_change_outcome(task_id, project, plan)

    assert closed is True, (
        "a leaf that used to close on the documented no-verify_cmd carve-out "
        "was failed because a check nobody could run did not run"
    )
    task = await task_queue.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.NO_CHANGES
    assert "could not be checked" in why, why
    assert _SKIP_NO_VERIFY_CMD in why, why


@pytest.mark.integration
async def test_an_unfetchable_tree_still_fails_a_leaf_whose_command_was_configured(
    db: Database,
) -> None:
    """The decision-level countercase, so the rule cannot be widened silently."""
    orch = _orchestrator_with(db, _unfetchable("no-token"))
    task_queue, plan_id, task_id, project = await _seed(
        db, "https://github.com/u/a", "pytest -q", second_files=[_ABSENT]
    )
    plan = await task_queue.get_plan(plan_id)

    closed, why = await orch.no_change_outcome(task_id, project, plan)

    assert closed is False
    task = await task_queue.get_task(task_id)
    assert task is not None
    assert task["status"] != TaskStatus.NO_CHANGES
    assert _SKIP_NO_VERIFY_CMD not in why, why


@pytest.mark.integration
async def test_the_gate_itself_reports_a_missing_declared_path(
    db: Database, bare_repo: Any
) -> None:
    """The HELPER's own truth, against a real branch and a real filesystem.

    Guarding only the decision above would leave the gate free to report an
    empty ``missing`` for everything: the decision would still be correct and
    would still never fire.
    """
    orch = _orchestrator(db)

    result = await orch._verify_plan_branch(
        str(bare_repo),
        _PLAN_BRANCH,
        _VERIFY_PASSES,
        require_paths=["app.py", _ABSENT],
    )

    assert result.status == "passed", f"the command itself must still pass: {result!r}"
    assert result.paths is not None
    assert result.paths.present == ("app.py",)
    assert result.paths.missing == (_ABSENT,)
    assert result.paths.checked == 2


@pytest.mark.integration
async def test_the_gate_answers_nothing_about_paths_when_none_are_declared(
    db: Database, bare_repo: Any
) -> None:
    """``None`` is not an empty tuple.

    Every pre-existing caller (the per-wave cross-leaf gate, the whole-plan
    backstop) passes no paths, and an empty ``missing`` from a check that never
    ran must never be readable as "everything the leaf declared is there".
    """
    orch = _orchestrator(db)

    result = await orch._verify_plan_branch(
        str(bare_repo), _PLAN_BRANCH, _VERIFY_PASSES
    )

    assert result.status == "passed"
    assert result.paths is None


async def test_no_change_outcome_refuses_on_a_missing_path_even_when_verify_passed(
    db: Database,
) -> None:
    """The DECISION, isolated from how the gate reaches its answer.

    The pair of facts that produced the measured false success, handed to the
    decision directly: the branch verified clean AND a declared location is
    absent. The second must win.
    """
    from orchestrator.core.orchestrator_review import (
        _DeclaredPathCheck,
        _PlanVerifyResult,
    )

    orch = _orchestrator(db)
    task_queue, plan_id, task_id, project = await _seed(
        db, "https://github.com/u/a", "pytest -q", second_files=[_ABSENT]
    )
    plan = await task_queue.get_plan(plan_id)

    async def _stub(*_args: Any, **_kwargs: Any) -> _PlanVerifyResult:
        return _PlanVerifyResult(
            "passed",
            output="294 passed",
            paths=_DeclaredPathCheck(missing=(_ABSENT,)),
        )

    orch._verify_plan_branch = _stub  # type: ignore[method-assign]

    closed, why = await orch.no_change_outcome(task_id, project, plan)

    assert closed is False
    assert _ABSENT in why, why
    task = await task_queue.get_task(task_id)
    assert task is not None
    assert task["status"] != TaskStatus.NO_CHANGES


async def test_the_declared_paths_of_the_right_row_reach_the_gate(
    db: Database,
) -> None:
    """The WIRING, and the positional join it rests on.

    ``tasks`` has no ``files`` column: the declaration lives in
    ``plans.opus_plan`` and the row is joined back to its graph entry by
    POSITION. A join that drifted by one would read leaf 1's declaration here,
    which is a different list, and the check would silently police the wrong
    leaf while every other test still passed.
    """
    from orchestrator.core.orchestrator_review import _PlanVerifyResult

    orch = _orchestrator(db)
    task_queue, plan_id, task_id, project = await _seed(
        db,
        "https://github.com/u/a",
        "pytest -q",
        first_files=["src/expr/tokens.py"],
        second_files=["src/expr/parser.py", "tests/test_parser.py"],
    )
    plan = await task_queue.get_plan(plan_id)
    handed: list[tuple[str, ...]] = []

    async def _stub(
        repo_url: str,
        branch: str,
        verify_cmd: str | None,
        disabled_reason: str | None = None,
        require_paths: Sequence[str] = (),
        leaf_verify_cmd: str | None = None,
    ) -> _PlanVerifyResult:
        handed.append(tuple(require_paths))
        return _PlanVerifyResult("passed")

    orch._verify_plan_branch = _stub  # type: ignore[method-assign]

    await orch.no_change_outcome(task_id, project, plan)

    assert handed == [("src/expr/parser.py", "tests/test_parser.py")], handed


async def test_a_task_that_declares_nothing_still_closes_and_says_so(
    db: Database,
) -> None:
    """The third answer, and the reason it is an answer rather than a refusal.

    Only the decomposition path produces ``files``. The plan_spec path, the
    improvement loop and a direct dispatch that omitted them all arrive here
    declaring nothing, so refusing those would fail exactly the plans this
    whole mechanism was built to stop failing. The check is skipped and the
    stored reason SAYS it was skipped, rather than leaving a stronger claim
    standing by silence.
    """
    from orchestrator.core.orchestrator_review import _PlanVerifyResult

    orch = _orchestrator(db)
    events = orch._bus.subscribe()
    task_queue, plan_id, task_id, project = await _seed(
        db, "https://github.com/u/a", "pytest -q"
    )
    plan = await task_queue.get_plan(plan_id)

    async def _stub(*_args: Any, **_kwargs: Any) -> _PlanVerifyResult:
        return _PlanVerifyResult("passed")

    orch._verify_plan_branch = _stub  # type: ignore[method-assign]

    closed, why = await orch.no_change_outcome(task_id, project, plan)

    assert closed is True
    assert "declared no edit locations" in why, why
    published = [events.get_nowait() for _ in range(events.qsize())]
    no_changes = next(e for e in published if e["type"] == "task_no_changes")
    # Published as well as stored: a no-op backed by a checked list of files
    # and one backed by nothing are worth different amounts of trust, and a
    # read-only surface must not have to re-derive which it got.
    assert no_changes["declared_paths_checked"] == 0
    assert no_changes["declared_paths_total"] == 0


@pytest.mark.integration
async def test_the_worker_callback_carries_the_refusal_all_the_way_to_the_row(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route the measured failure actually took, end to end.

    ``no_change_outcome`` is reached from three call sites and the one the
    harness entrypoints use is ``POST /api/internal/agent-done`` with
    ``status=no_changes``. That endpoint looks the plan row up itself, and a
    refusal there has to land on the task row as a retry rather than as a
    terminal success. Testing the method alone would leave every one of those
    links free to drop the answer.
    """
    from orchestrator.core.orchestrator_review import (
        _DeclaredPathCheck,
        _PlanVerifyResult,
    )
    from tests.conftest import seed_user

    monkeypatch.setattr(
        "orchestrator.api.projects.preflight_remote", AsyncMock(return_value=[])
    )
    await seed_user(db)
    project_resp = await client.post(
        "/api/projects",
        json={
            "name": "Expr",
            "repo_url": "https://github.com/u/expr",
            "model_name": "m",
            "max_retries": 3,
            "verify_cmd": "python -m pytest src/playground -q",
        },
        headers=auth_headers,
    )
    assert project_resp.status_code in (200, 201), project_resp.text
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]
    plan_id = await queue.create_plan(project_resp.json()["id"], "Expr")
    await queue.activate_plan(
        plan_id,
        {
            "plan_summary": "Expr",
            "plan_slug": "expr",
            "tasks": [
                {
                    "title": "Write the lexer",
                    "slug": "lexer",
                    "description": "Write it",
                    "depends_on": [],
                    "files": [_ABSENT],
                }
            ],
        },
        "plan/2026-08-25-expr",
    )
    task_id = (await queue.get_tasks_for_plan(plan_id))[0]["id"]
    await db.execute(
        "UPDATE tasks SET status = ?, attempt = ? WHERE id = ?",
        (TaskStatus.IN_PROGRESS, 1, task_id),
    )
    run_id = await queue.create_agent_run(task_id, "container-expr")

    handed: list[tuple[str, ...]] = []

    async def _gate(
        repo_url: str,
        branch: str,
        verify_cmd: str | None,
        disabled_reason: str | None = None,
        require_paths: Sequence[str] = (),
        leaf_verify_cmd: str | None = None,
    ) -> _PlanVerifyResult:
        # Exactly what the real gate returned in the measured run: the
        # repository's own suite passing on the branch the leaf was cut from.
        handed.append(tuple(require_paths))
        return _PlanVerifyResult(
            "passed",
            output="294 passed, 1 warning in 0.57s",
            paths=_check_paths_like_the_real_gate(require_paths),
        )

    def _check_paths_like_the_real_gate(
        require_paths: Sequence[str],
    ) -> _DeclaredPathCheck | None:
        # The declaration is echoed back as MISSING rather than recomputed, so
        # this stub decides nothing the real gate decides: the fact under test
        # is that the endpoint carries a refusal through, not how the gate
        # arrives at one. The real gate's own answer is pinned separately in
        # ``test_the_gate_itself_reports_a_missing_declared_path``.
        return (
            _DeclaredPathCheck(missing=tuple(require_paths)) if require_paths else None
        )

    monkeypatch.setattr(
        client.app.state.orchestrator,  # type: ignore[attr-defined]
        "_verify_plan_branch",
        _gate,
    )

    resp = await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json={"task_id": task_id, "run_id": run_id, "status": "no_changes"},
    )

    assert resp.status_code == 200
    # The endpoint found the plan, resolved the leaf's declaration and handed
    # it to the gate. Without this the refusal below could come from anywhere.
    assert handed == [(_ABSENT,)], handed
    task = await queue.get_task(task_id)
    assert task is not None
    assert task["status"] != TaskStatus.NO_CHANGES
    assert task["status"] == TaskStatus.PENDING
    assert int(task["attempt"]) == 2, "the refusal must go down the retry path"
    assert _ABSENT in (task["review_feedback"] or ""), task["review_feedback"]


# ---------------------------------------------------------------------------
# Which declarations this check may pronounce on at all. Everything below
# guards the same boundary from one side or the other: a shape it cannot
# decide must land in ``unresolvable``, never in ``missing``, because
# ``missing`` fails a leaf.
# ---------------------------------------------------------------------------


def _tree(tmp_path: Any) -> str:
    """A checkout-shaped directory: one file, one package dir, one sibling."""
    (tmp_path / "checkout" / "src" / "expr").mkdir(parents=True)
    (tmp_path / "checkout" / "src" / "expr" / "lexer.py").write_text(
        "x = 1\n", encoding="utf-8"
    )
    (tmp_path / "outside.txt").write_text("host file\n", encoding="utf-8")
    return str(tmp_path / "checkout")


@pytest.mark.parametrize(
    ("declaration", "bucket"),
    [
        ("src/expr/lexer.py", "present"),
        ("src/expr", "present"),
        ("src\\expr\\lexer.py", "present"),
        ("/src/expr/lexer.py", "present"),
        ("./src/expr/lexer.py", "present"),
        ("src/expr/lexer.py::tokenize", "present"),
        ("src/expr/parser.py", "missing"),
        ("src/expr/parser.py::parse", "missing"),
        ("src/**/*.py", "unresolvable"),
        ("src/expr/?exer.py", "unresolvable"),
        ("src/expr/[abc].py", "unresolvable"),
        ("../outside.txt", "unresolvable"),
        ("src/expr/../expr/lexer.py", "unresolvable"),
        ("C:/Windows/System32/drivers/etc/hosts", "unresolvable"),
        ("//host/share/thing.py", "unresolvable"),
    ],
    ids=[
        "file",
        "directory",
        "windows-separators",
        "root-anchored",
        "dot-prefixed",
        "symbol-suffix-present",
        "absent-file",
        "symbol-suffix-absent",
        "recursive-glob",
        "single-char-glob",
        "character-class",
        "parent-traversal",
        "inner-parent-segment",
        "drive-letter",
        "unc-path",
    ],
)
def test_a_declaration_lands_in_the_bucket_it_can_be_decided_in(
    tmp_path: Any, declaration: str, bucket: str
) -> None:
    """Only a plain path inside the checkout may be called present or missing.

    ``../outside.txt`` and the absolute forms are the security half as much as
    the correctness half: ``Path(root) / "/etc/passwd"`` is ``/etc/passwd``, so
    an unguarded join would stat the HOST filesystem and let a brain-authored
    string decide a governance outcome from a file the repository never had.

    ``inner-parent-segment`` is here because two guards would otherwise mask
    each other. The ``..`` rejection and the containment check both stop
    ``../outside.txt``, so neither is pinned by that case alone. This one
    resolves back INSIDE the checkout onto a file that is really there, so the
    containment check waves it through and only the ``..`` rejection decides
    it.
    """
    from orchestrator.core.orchestrator_review import _check_declared_paths

    result = _check_declared_paths(_tree(tmp_path), [declaration])

    assert getattr(result, bucket) == (declaration,), result
    # Verbatim, never normalized: the reason stored on the task has to name
    # what the brain actually wrote or nobody can grep the plan for it.
    for other in ("present", "missing", "unresolvable"):
        if other != bucket:
            assert getattr(result, other) == (), result


def test_an_undecidable_declaration_is_not_counted_as_checked(tmp_path: Any) -> None:
    """``checked`` is the only honest denominator.

    A leaf declaring nothing but globs establishes NOTHING. Counting those as
    checked would let the stored reason claim "all declared edit locations
    exist" on the strength of a check that decided none of them, which is the
    same shape of quiet overclaim this whole task exists to remove.
    """
    from orchestrator.core.orchestrator_review import _check_declared_paths

    result = _check_declared_paths(_tree(tmp_path), ["src/**/*.py", "docs/*.md"])

    assert result.checked == 0
    assert result.missing == ()
    assert len(result.unresolvable) == 2


@pytest.mark.parametrize(
    ("declared", "check", "expected"),
    [
        ((), None, "declared no edit locations"),
        (("a.py",), None, "could not be checked"),
        (("a.py",), _NOTHING_DECIDED, "none of its 1 declared edit locations"),
        (("a.py",), _ALL_PRESENT, "all 1 of its declared edit locations exist"),
        (
            ("a.py", "b.py"),
            _ONE_MISSING,
            "1 of its 2 declared edit locations are absent",
        ),
    ],
    ids=[
        "nothing-declared",
        "no-checkout",
        "all-undecidable",
        "all-present",
        "missing",
    ],
)
def test_the_stored_reason_states_what_the_check_established(
    declared: tuple[str, ...], check: Any, expected: str
) -> None:
    """Five distinct facts, five distinct sentences, none of them silence.

    This string is written to ``tasks.review_feedback``, rendered by the
    dashboard and returned by MCP. Collapsing any two of these renders a no-op
    backed by a checked list of files identically to one backed by nothing,
    which is the quiet overclaim the whole check exists to remove.

    The ``missing`` row is unreachable through ``no_change_outcome`` today,
    because that refuses before it builds the string. It is pinned anyway: a
    sentence whose truth depends on a caller ordering somewhere else is one
    reorder away from being a lie told to a human.
    """
    from orchestrator.core.orchestrator_review import _declared_paths_clause

    assert expected in _declared_paths_clause(declared, check, "plan/x")


@pytest.mark.integration
async def test_a_leaf_declaring_only_globs_still_closes_and_says_nothing_was_checked(
    db: Database, bare_repo: Any
) -> None:
    """The undecidable case reaches the DECISION as "unknown", not as a failure.

    Guarding the bucketing alone would leave ``no_change_outcome`` free to read
    an empty ``missing`` as a clean bill of health.
    """
    orch = _orchestrator(db)
    task_queue, plan_id, task_id, project = await _seed(
        db, str(bare_repo), _VERIFY_PASSES, second_files=["src/**/*.py"]
    )
    plan = await task_queue.get_plan(plan_id)

    closed, why = await orch.no_change_outcome(task_id, project, plan)

    assert closed is True
    assert "none of its 1 declared edit locations could be resolved" in why, why


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("src/a.py", ("src/a.py",)),
        (["src/a.py", "src/b.py"], ("src/a.py", "src/b.py")),
        ([{"path": "src/a.py"}, {"file": "src/b.py"}], ("src/a.py", "src/b.py")),
        ([None, "", 3, {"nope": "x"}, "src/c.py"], ("src/c.py",)),
        (17, ()),
        ({"path": "src/a.py"}, ()),
    ],
    ids=["bare-string", "list", "mappings", "mixed-junk", "int", "top-level-mapping"],
)
def test_the_worker_and_the_check_read_the_same_declaration(
    raw: Any, expected: tuple[str, ...]
) -> None:
    """ONE parser, or the worker is told one list and judged against another.

    ``_normalize_edit_locations`` builds the undroppable EDIT LOCATIONS bible
    section; ``declared_paths`` feeds the no-op check. A second parser is how a
    leaf comes to be told to write ``src/a.py`` and judged on something else.

    ``expected`` is spelled out rather than derived from either function.
    Asserting that one equals the other is true by construction once they share
    an implementation, and would stay true if that implementation returned
    nothing at all.
    """
    from orchestrator.core.orchestrator_dispatch import _normalize_edit_locations
    from orchestrator.core.plan_graph import declared_paths

    assert declared_paths(raw) == expected
    assert _normalize_edit_locations(raw) == ("\n".join(expected) or None)


# ---------------------------------------------------------------------------
# The positive control, LAST and deliberately so. Everything above makes the
# check able to REFUSE; this is the shape the mechanism was built for, and a
# fix that fails it fails plans whose work is already done -- which is worse
# than the defect, because it is the failure mode that was actually measured
# in four of four plans across both harnesses.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_task_one_wrote_task_twos_file_and_it_still_closes_as_a_no_op(
    db: Database, bare_repo: Any
) -> None:
    """The load-bearing case: the work is already there, so this is not a failure.

    ``app.py`` exists on the branch the leaf was cut from, exactly as it does
    when leaf 1 writes both the module and its tests. The leaf declared it, the
    branch carries it, the command passes: every piece of evidence agrees, and
    the leaf closes.
    """
    orch = _orchestrator(db)
    task_queue, plan_id, task_id, project = await _seed(
        db, str(bare_repo), _VERIFY_PASSES, second_files=["app.py"]
    )
    plan = await task_queue.get_plan(plan_id)

    closed, why = await orch.no_change_outcome(task_id, project, plan)

    assert closed is True, (
        "the leaf whose work leaf 1 already did was failed; this is the shape "
        "the no-op decision exists for and breaking it takes down plans that "
        "are already finished"
    )
    task = await task_queue.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.NO_CHANGES
    assert "all 1 of its declared edit locations exist" in why, why
    assert f"verify passed on {_PLAN_BRANCH}" in why, why
