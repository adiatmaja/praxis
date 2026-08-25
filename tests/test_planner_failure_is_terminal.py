"""A permanent planner failure must not look like progress.

``plan_and_activate`` used to wrap only ``_load_spec_text``.  When the planner
answered in prose instead of JSON, ``_extract_json`` raised, the exception
escaped to ``run_once``'s per-plan guard, and the plan stayed PENDING.  From
outside, ``praxis plans`` said ``active`` and ``praxis tasks`` said "has no
tasks yet" -- which is exactly what both say during a HEALTHY decomposition.
The plan then retried on every tick forever.

The field case: ``claude -p`` ran with cwd ``/app`` while the repository was at
``/run/desktop/mnt/host/c/...``, so the model replied with a permission request
in prose.  That never becomes JSON on retry, which is why the classification
here is structural (was there any JSON at all?) rather than a keyword scan.

These tests pin three separable facts:

* a response with NO JSON in it fails the plan on the FIRST tick, with the
  planner's own words on ``plans.error``;
* a response whose JSON span merely failed to parse is retried, and only goes
  terminal once the bounded attempt budget is spent;
* the planner is handed a real checkout of the repository, and a clone that
  fails degrades to planning without one rather than wedging the loop.
"""

# ruff: noqa: S101

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient

from orchestrator.core.opus_bridge import (
    BrainMalformedJsonError,
    BrainProseResponseError,
)
from orchestrator.core.orchestrator import _MAX_PLANNING_ATTEMPTS, Orchestrator
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import PlanStatus


#: The response that produced the field report, near enough.  Deliberately
#: contains the word "permission": nothing in the implementation may match on
#: it, and a test that used neutral prose could not detect a keyword scan
#: sneaking back in.
_PROSE = (
    "I need permission to access "
    "/run/desktop/mnt/host/c/working-space/demo before I can read the "
    "repository. Please grant access to that directory and ask me again."
)

_VALID_PLAN: dict[str, Any] = {
    "plan_summary": "Auth",
    "plan_slug": "auth",
    "tasks": [
        {
            "title": "Login",
            "slug": "login",
            "description": "Build login",
            "depends_on": [],
        }
    ],
}


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def bare_repo(tmp_path: Path) -> Path:
    """A real bare repo on ``main`` carrying one recognisable file."""
    work, bare = tmp_path / "w", tmp_path / "r.git"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    _git("config", "user.email", "t@e.com", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    _git("config", "commit.gpgsign", "false", cwd=work)
    (work / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git("add", ".", cwd=work)
    _git("commit", "-m", "base", cwd=work)
    _git("clone", "--bare", str(work), str(bare), cwd=tmp_path)
    return bare


@pytest.fixture
def no_remote_clone(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Stub the REMOTE clone helper; returns the (repo_url, dest) pairs seen.

    Only the remote arm is stubbed.  The local arm is exercised for real by
    ``test_the_planner_is_handed_a_real_checkout_of_the_repository`` -- without
    that one test every assertion here would still pass if the clone never
    worked at all, because a failed clone degrades silently to ``cwd=None``.
    """
    seen: list[tuple[str, str]] = []

    def _fake_clone(repo_url: str, dest: str, token: str, depth: int = 50) -> None:  # noqa: ARG001
        seen.append((repo_url, dest))

    monkeypatch.setattr("orchestrator.core.orchestrator.clone_with_token", _fake_clone)
    return seen


async def _project_and_plan(
    db: Database, repo_url: str = "https://github.com/u/a"
) -> tuple[TaskQueue, str, dict[str, Any]]:
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u1", "User", "hash"),
    )
    await db.execute(
        """INSERT INTO projects (id, user_id, name, repo_url, model_name)
           VALUES (?, ?, ?, ?, ?)""",
        ("p1", "u1", "App", repo_url, "deepseek"),
    )
    task_queue = TaskQueue(db)
    plan_id = await task_queue.create_plan(
        "p1", "Build auth", spec_path="docs/superpowers/specs/auth.md"
    )
    project = await db.fetch_one("SELECT * FROM projects WHERE id = 'p1'")
    assert project is not None
    return task_queue, plan_id, project


def _spec_reader() -> AsyncMock:
    reader = AsyncMock()
    reader.read_doc.return_value = "Build auth"
    return reader


def _orchestrator(task_queue: TaskQueue, opus: AsyncMock) -> Orchestrator:
    return Orchestrator(
        task_queue=task_queue,
        agent_manager=MagicMock(),
        opus_bridge=opus,
        git_ops=AsyncMock(),
        event_bus=MagicMock(),
        spec_reader=_spec_reader(),
    )


def _opus(**kwargs: Any) -> AsyncMock:
    opus = AsyncMock()
    opus.is_available.return_value = True
    for key, value in kwargs.items():
        setattr(opus.plan_spec, key, value)
    return opus


@pytest.mark.integration
async def test_a_prose_response_fails_the_plan_on_the_first_tick(
    db: Database, no_remote_clone: list[tuple[str, str]]
) -> None:
    """No JSON at all is a refusal, and a refusal is permanent.

    Delete the ``BrainProseResponseError`` arm in ``plan_and_activate`` and
    this goes red twice over: the plan stays PENDING (the original defect) and
    the planner's own words never reach ``plans.error``.
    """
    task_queue, plan_id, project = await _project_and_plan(db)
    opus = _opus(side_effect=BrainProseResponseError("no JSON", _PROSE))

    await _orchestrator(task_queue, opus).plan_and_activate(plan_id, project)

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.FAILED
    # Terminal on the FIRST occurrence: no attempt is spent waiting for a
    # retry that cannot succeed.
    assert plan["plan_attempts"] == 0
    assert opus.plan_spec.await_count == 1
    error = plan["error"]
    # What happened, the likely cause, the remedy, and the evidence.
    assert "prose" in error
    assert "repository" in error
    assert "Please grant access to that directory" in error


@pytest.mark.integration
async def test_the_prose_verdict_is_structural_not_a_keyword_scan(
    db: Database, no_remote_clone: list[tuple[str, str]]
) -> None:
    """Prose that never says "permission" is still terminal.

    The observed failure said "permission"; the rule must not.  Replace the
    exception-type check with a substring match on the excerpt and this goes
    red.
    """
    task_queue, plan_id, project = await _project_and_plan(db)
    opus = _opus(
        side_effect=BrainProseResponseError(
            "no JSON", "Sorry, I cannot help with that request."
        )
    )

    await _orchestrator(task_queue, opus).plan_and_activate(plan_id, project)

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.FAILED
    assert "Sorry, I cannot help with that request." in plan["error"]


@pytest.mark.integration
async def test_malformed_json_retries_and_only_fails_on_the_third_attempt(
    db: Database, no_remote_clone: list[tuple[str, str]]
) -> None:
    """A truncated JSON span is worth retrying, but not forever.

    Delete the ``plan_attempts`` bound and the third tick leaves the plan
    PENDING, which is the forever-retry the whole task exists to stop.
    """
    task_queue, plan_id, project = await _project_and_plan(db)
    opus = _opus(side_effect=BrainMalformedJsonError("bad span", '{"tasks": ['))
    orchestrator = _orchestrator(task_queue, opus)

    for expected_attempts in range(1, _MAX_PLANNING_ATTEMPTS):
        await orchestrator.plan_and_activate(plan_id, project)
        plan = await task_queue.get_plan(plan_id)
        assert plan is not None
        assert plan["status"] == PlanStatus.PENDING, expected_attempts
        assert plan["plan_attempts"] == expected_attempts
        # The reason is recorded on every attempt, not only the last one:
        # a plan retrying silently is the same invisible state as a plan
        # wedged silently.
        assert plan["error"]

    await orchestrator.plan_and_activate(plan_id, project)

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.FAILED
    assert plan["plan_attempts"] == _MAX_PLANNING_ATTEMPTS
    assert str(_MAX_PLANNING_ATTEMPTS) in plan["error"]
    assert "bad span" in plan["error"]


@pytest.mark.integration
async def test_a_plan_that_recovers_does_not_carry_a_stale_attempt_count(
    db: Database, no_remote_clone: list[tuple[str, str]]
) -> None:
    """Attempt two succeeds, so the count goes back to zero.

    Delete ``reset_plan_attempts`` and a plan that fails once then works
    forever is one transient failure away from a terminal verdict.
    """
    task_queue, plan_id, project = await _project_and_plan(db)
    opus = _opus(side_effect=[BrainMalformedJsonError("bad span", "{"), _VALID_PLAN])
    orchestrator = _orchestrator(task_queue, opus)

    await orchestrator.plan_and_activate(plan_id, project)
    after_failure = await task_queue.get_plan(plan_id)
    assert after_failure is not None
    assert after_failure["plan_attempts"] == 1

    await orchestrator.plan_and_activate(plan_id, project)

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.ACTIVE
    assert plan["plan_attempts"] == 0


@pytest.mark.integration
async def test_a_rate_limit_does_not_consume_an_attempt(
    db: Database, no_remote_clone: list[tuple[str, str]]
) -> None:
    """The one failure this system already knows how to wait out.

    A subscription limit lasts five hours; the loop ticks every five seconds.
    Counting it would burn the whole budget in fifteen seconds and fail a plan
    that had nothing wrong with it.  Delete the exemption and this goes red.
    """
    task_queue, plan_id, project = await _project_and_plan(db)
    opus = _opus(side_effect=RuntimeError("Opus rate limited"))
    # Available when the pass starts, rate limited by the time it fails:
    # `_check_and_handle_rate_limit` writes `opus_state` BEFORE raising, so
    # this is exactly what the loop sees in production.
    opus.is_available.side_effect = [True, False]

    await _orchestrator(task_queue, opus).plan_and_activate(plan_id, project)

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.PENDING
    assert plan["plan_attempts"] == 0


@pytest.mark.unit
async def test_bump_plan_attempts_returns_the_new_count(db: Database) -> None:
    """The bound is read from the return value, so it must be the NEW count.

    Return the count from BEFORE the increment and the plan gets one extra
    attempt forever -- off by one in the direction nobody notices.
    """
    task_queue, plan_id, _project = await _project_and_plan(db)

    assert await task_queue.bump_plan_attempts(plan_id) == 1
    assert await task_queue.bump_plan_attempts(plan_id) == 2

    await task_queue.reset_plan_attempts(plan_id)
    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["plan_attempts"] == 0


@pytest.mark.integration
async def test_the_planner_is_handed_a_real_checkout_of_the_repository(
    db: Database, bare_repo: Path
) -> None:
    """The cwd is a real clone, not a directory that exists only in a mock.

    This is the one test in this file that does no clone stubbing.  Every
    other assertion here would still pass if the clone never worked, because a
    failed clone degrades silently to ``cwd=None`` -- which is precisely the
    "the model was asked to reason about a path it cannot open" state the
    field report described.
    """
    task_queue, plan_id, project = await _project_and_plan(db, repo_url=str(bare_repo))
    seen: dict[str, Any] = {}

    async def _plan_spec(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        cwd = kwargs["cwd"]
        seen["cwd"] = cwd
        seen["listing"] = sorted(p.name for p in Path(cwd).iterdir())
        return _VALID_PLAN

    opus = _opus(side_effect=_plan_spec)

    await _orchestrator(task_queue, opus).plan_and_activate(plan_id, project)

    assert "app.py" in seen["listing"], seen
    # The workspace does not outlive the call.
    assert not Path(seen["cwd"]).exists()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        (None, "no git credential provider is configured"),
        ("empty-token", "no credential is available for"),
    ],
)
async def test_a_missing_credential_degrades_with_a_named_reason(
    db: Database,
    caplog: pytest.LogCaptureFixture,
    provider: str | None,
    expected: str,
) -> None:
    """Both refusals must SAY what is missing, and neither may reach git.

    An unauthenticated ``git clone`` of a private remote can sit waiting on a
    credential prompt, and the orchestration loop is one coroutine: a blocked
    clone stalls every other plan. Deleting either guard replaces the named
    reason with an ``AttributeError``/prompt-hang, and this goes red on the
    message.
    """
    task_queue, plan_id, project = await _project_and_plan(db)
    opus = _opus(return_value=_VALID_PLAN)
    orchestrator = _orchestrator(task_queue, opus)
    if provider is None:
        orchestrator._git = MagicMock(spec=[])  # no `_provider` attribute at all
    else:
        git = MagicMock()
        git._provider.token_for_repo = AsyncMock(return_value="")
        orchestrator._git = git

    with caplog.at_level("WARNING", logger="orchestrator.core.orchestrator"):
        await orchestrator.plan_and_activate(plan_id, project)

    assert expected in caplog.text
    # Degraded, not wedged: the plan still activated, just without a checkout.
    assert opus.plan_spec.await_args.kwargs["cwd"] is None
    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.ACTIVE


@pytest.mark.integration
async def test_the_plans_api_reports_the_error_and_the_attempt_count(
    db: Database, client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Both facts must cross the wire, under exactly these names.

    ``praxis plans`` and MCP ``poll_plan`` read this response; a field missing
    from ``PlanResponse`` is filtered out by FastAPI's ``response_model`` and
    the caller sees a plan that failed for no stated reason. Delete either
    field from the model and this goes red.
    """
    task_queue, plan_id, _project = await _project_and_plan(db)
    await task_queue.set_plan_error(plan_id, "the planner answered in prose")
    await task_queue.bump_plan_attempts(plan_id)
    await task_queue.bump_plan_attempts(plan_id)

    response = await client.get(f"/api/plans/{plan_id}", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["error"] == "the planner answered in prose"
    assert body["plan_attempts"] == 2


@pytest.mark.integration
async def test_a_failed_clone_still_plans(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Degrade, never wedge: no repository is worse planning, not no planning.

    Delete the try/except around the clone and an unreachable repository
    raises out of ``plan_and_activate``, which is the same escape-to-run_once
    the rest of this file exists to close.
    """

    def _boom(repo_url: str, dest: str, token: str, depth: int = 50) -> None:  # noqa: ARG001
        message = "fatal: could not read from remote repository"
        raise RuntimeError(message)

    monkeypatch.setattr("orchestrator.core.orchestrator.clone_with_token", _boom)
    task_queue, plan_id, project = await _project_and_plan(db)
    opus = _opus(return_value=_VALID_PLAN)

    await _orchestrator(task_queue, opus).plan_and_activate(plan_id, project)

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.ACTIVE
    assert opus.plan_spec.await_args.kwargs["cwd"] is None
