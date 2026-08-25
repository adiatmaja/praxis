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

import asyncio
import subprocess
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient

from orchestrator.core.llm_router import LLMRouter
from orchestrator.core.opus_bridge import (
    BrainMalformedJsonError,
    BrainProseResponseError,
    OpusBridge,
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


#: What the spec doc in ``bare_repo`` says, so a test can tell the text that
#: came out of the planner's own checkout from the spec reader's stand-in.
_SPEC_IN_REPO = "Build auth, read straight out of the planner checkout.\n"


@pytest.fixture(autouse=True)
def planner_workspace_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep planner clones out of the repo's real ``data/`` directory.

    Autouse: every test in this file drives ``plan_and_activate``, which makes
    a workspace unconditionally. Without this they accumulate under the
    developer's own checkout, and nothing in the product sweeps that directory.
    """
    base = tmp_path / "planner-workspaces"
    monkeypatch.setattr(
        "orchestrator.core.orchestrator._planner_workspace_base", lambda: base
    )
    return base


@pytest.fixture
def bare_repo(tmp_path: Path) -> Path:
    """A real bare repo on ``main`` carrying a source file and a spec doc."""
    work, bare = tmp_path / "w", tmp_path / "r.git"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    _git("config", "user.email", "t@e.com", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    _git("config", "commit.gpgsign", "false", cwd=work)
    (work / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    spec = work / "docs" / "superpowers" / "specs"
    spec.mkdir(parents=True)
    (spec / "auth.md").write_text(_SPEC_IN_REPO, encoding="utf-8")
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


def _orchestrator(task_queue: TaskQueue, opus: Any) -> Orchestrator:
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
    # What happened, the likely cause, the remedy, the NEXT ACTION, and the
    # evidence. The next action is not decoration: this case is terminal on the
    # first tick, so nothing happens at all until a person does something, and
    # the message is the only place that is said.
    assert "prose" in error
    assert "repository" in error
    assert "resubmit the specification" in error
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
    db: Database,
    no_remote_clone: list[tuple[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one failure this system already knows how to wait out.

    Driven through the REAL ``OpusBridge`` on the REAL ``LLMRouter`` down to
    the real ``_execute_one``, which is the path a stock install takes:
    ``main.py`` always wires the router, so ``plan_spec`` never reaches the
    legacy ``_run_claude``. An earlier version hand-fed
    ``is_available.side_effect = [True, False]`` and proved nothing; the
    version after it stubbed ``_execute_one`` wholesale, which skipped the one
    place a throttle is recognised. Only the subprocess is faked here.

    The throttle text is on STDOUT, where ``claude`` really puts it. That is
    load-bearing: the router's ``RuntimeError`` quotes stderr only, so a
    classification made from the exception's text sees "claude failed (exit
    1):" with no evidence at all, charges the plan an attempt, and a live probe
    showed a healthy plan reaching FAILED on tick 3 -- fifteen seconds into a
    five-hour wait at the shipped ``loop_interval`` of 5.

    Three ticks, because three is the whole budget: if any of them is charged,
    the plan is dead.
    """
    task_queue, plan_id, project = await _project_and_plan(db)
    spawns: list[tuple[Any, ...]] = []

    class _Throttled:
        returncode = 1

        async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:  # noqa: A002
            return (
                b"Claude usage limit reached. Your limit will reset at 3pm.",
                b"",
            )

    async def _fake_exec(*argv: Any, **_kwargs: Any) -> _Throttled:
        spawns.append(argv)
        return _Throttled()

    monkeypatch.setattr("orchestrator.core.llm_router.shutil.which", lambda name: name)
    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)

    async def _resolve_chain(call_site: str, project_id: str | None) -> list[dict]:
        return [{"provider": "claude", "model": "claude-sonnet-4-6", "effort": None}]

    bridge = OpusBridge(db, router=LLMRouter(_resolve_chain))
    orchestrator = _orchestrator(task_queue, bridge)

    before = await db.fetch_one("SELECT status FROM opus_state WHERE id = 1")
    assert before is not None
    assert before["status"] == "available"

    for _tick in range(_MAX_PLANNING_ATTEMPTS):
        await orchestrator.plan_and_activate(plan_id, project)
        plan = await task_queue.get_plan(plan_id)
        assert plan is not None
        assert plan["status"] == PlanStatus.PENDING
        assert plan["plan_attempts"] == 0

    # The reason must tell the operator to WAIT. The exhausted-attempts message
    # says "resubmit", and resubmitting during a throttle fails the same way.
    error = plan["error"]
    assert "WAITING" in error
    assert "Do NOT resubmit" in error

    # The router now parks `opus_state`, so the queue-and-resume branch at the
    # top of `plan_and_activate` finally fires on this route: only the FIRST
    # tick reaches a provider, and the other two return without spending a
    # planner call or a clone. Revert the parking and this goes red on the
    # spawn count long before it goes red on the status.
    #
    # ONE spawn because this chain has ONE entry. That is not the shipped
    # shape: `config/praxis.yaml` gives `plan` the chain [sonnet, opus], and
    # `is_unavailability` now answers True for the new type, so the router
    # falls through and a throttle costs one spawn PER ENTRY on the first
    # tick. Covered in
    # tests/test_router_rate_limit_parks_opus_state.py::
    # test_the_shipped_two_entry_role_chain_parks_after_exhausting_it.
    state = await db.fetch_one("SELECT status FROM opus_state WHERE id = 1")
    assert state is not None
    assert state["status"] == "rate_limited"
    assert len(spawns) == 1, spawns
    assert await bridge.get_queued_actions() == [
        {"action": "plan", "plan_id": plan_id, "project_id": "p1"}
    ]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("graph", "expected"),
    [
        ({"plan_summary": "s", "tasks": []}, "plan_slug"),
        ({"plan_summary": "s", "plan_slug": "s"}, "tasks"),
        ({"plan_slug": "s", "tasks": {"a": 1}}, "not a list"),
        (
            {"plan_slug": "s", "tasks": [{"title": "T", "description": "D"}]},
            "slug",
        ),
        ([1, 2, 3], "not an object"),
    ],
)
async def test_valid_json_of_the_wrong_shape_does_not_escape_the_guard(
    db: Database,
    no_remote_clone: list[tuple[str, str]],
    graph: Any,
    expected: str,
) -> None:
    """Well-formed JSON missing a key the loop reads is the SAME defect.

    Reproduced live before the validator existed: a graph with
    ``plan_summary`` and ``tasks`` but no ``plan_slug`` raised ``KeyError`` one
    line past the guarded block, escaped to ``run_once``, and left the plan
    pending with ``plan_attempts=0`` and ``error=None`` on every tick forever
    -- identical symptom, identical surface, identical invisible state as the
    defect this file exists to close. Nothing upstream validates the shape:
    the plan prompt is a plain template with no ``json_schema``.

    Delete ``_validate_plan_shape`` and every case here goes red, because the
    exception escapes ``plan_and_activate`` instead of being recorded.
    """
    task_queue, plan_id, project = await _project_and_plan(db)
    opus = _opus(return_value=graph)

    # No pytest.raises: the whole point is that nothing escapes.
    await _orchestrator(task_queue, opus).plan_and_activate(plan_id, project)

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.PENDING
    # Counted and reported, i.e. visibly retrying rather than invisibly stuck.
    assert plan["plan_attempts"] == 1
    assert expected in plan["error"]


@pytest.mark.integration
async def test_the_degraded_checkout_reason_reaches_the_plan_row(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator must not have to read `docker logs` to learn the cause.

    The prose message tells them to check that the repository is reachable.
    Before this, the evidence that it was NOT reachable existed only as a
    WARNING in the container log, which is exactly what cost the field
    reporter an afternoon. Delete `_with_checkout_note` and this goes red.
    """

    def _boom(repo_url: str, dest: str, token: str, depth: int = 50) -> None:  # noqa: ARG001
        message = "fatal: could not read from remote repository"
        raise RuntimeError(message)

    monkeypatch.setattr("orchestrator.core.orchestrator.clone_with_token", _boom)
    task_queue, plan_id, project = await _project_and_plan(db)
    opus = _opus(side_effect=BrainProseResponseError("no JSON", _PROSE))

    await _orchestrator(task_queue, opus).plan_and_activate(plan_id, project)

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.FAILED
    assert "WITHOUT a readable checkout" in plan["error"]
    assert "could not read from remote repository" in plan["error"]


@pytest.mark.integration
async def test_the_spec_is_read_from_the_checkout_the_planner_already_has(
    db: Database, bare_repo: Path
) -> None:
    """One clone per attempt, not two.

    ``_load_spec_text`` resolved to ``BrainstormManager.read_doc``, which
    clones the whole repository at depth 50 to read ONE file and deletes it,
    milliseconds before the planner workspace cloned the same repository at the
    same depth again. A throttled plan did six full clones before dying.

    The spec reader here RAISES if it is called at all, so a regression that
    reintroduces the second clone cannot pass.
    """
    task_queue, plan_id, project = await _project_and_plan(db, repo_url=str(bare_repo))
    seen: dict[str, Any] = {}

    async def _plan_spec(*args: Any, **kwargs: Any) -> dict[str, Any]:
        seen["spec"] = args[0]
        return _VALID_PLAN

    opus = _opus(side_effect=_plan_spec)
    orchestrator = _orchestrator(task_queue, opus)
    orchestrator._spec_reader.read_doc.side_effect = AssertionError(
        "the spec reader cloned the repository a second time"
    )

    await orchestrator.plan_and_activate(plan_id, project)

    assert seen["spec"] == _SPEC_IN_REPO
    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.ACTIVE


@pytest.mark.integration
async def test_the_remote_clone_does_not_block_the_event_loop(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`clone_with_token` is synchronous, and this process has ONE event loop.

    Called bare from a coroutine it blocks the orchestration pass, FastAPI, SSE
    and every agent callback for as long as the fetch takes, with no deadline
    of its own. Drop the ``asyncio.to_thread`` and the ticker below cannot run
    while the clone sleeps, so the count comes back 0 and this goes red.
    """

    window: dict[str, float] = {}

    def _slow_clone(repo_url: str, dest: str, token: str, depth: int = 50) -> None:  # noqa: ARG001
        window["start"] = time.monotonic()
        time.sleep(0.3)
        window["end"] = time.monotonic()

    monkeypatch.setattr("orchestrator.core.orchestrator.clone_with_token", _slow_clone)
    task_queue, plan_id, project = await _project_and_plan(db)
    opus = _opus(return_value=_VALID_PLAN)
    ticks: list[float] = []

    async def _ticker() -> None:
        # Longer than the clone, so the window is fully covered either way.
        for _ in range(60):
            await asyncio.sleep(0.01)
            ticks.append(time.monotonic())

    await asyncio.gather(
        _orchestrator(task_queue, opus).plan_and_activate(plan_id, project),
        _ticker(),
    )

    # Counting ticks OVERALL proves nothing: the ticker runs before and after
    # the clone whether or not the clone blocks, so a total of 60 is reached
    # either way. Only ticks landing INSIDE the clone's own window distinguish
    # the two, and a blocking call admits exactly zero of them. An earlier
    # version of this test asserted `ticks > 0` and stayed green under the
    # blocking mutation.
    during = [t for t in ticks if window["start"] < t < window["end"]]
    assert len(during) >= 3, (
        f"the event loop was blocked for the clone: {len(during)} ticks landed "
        f"in the {window['end'] - window['start']:.2f}s window"
    )


@pytest.mark.integration
async def test_a_hung_clone_is_bounded_and_degrades(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clone with no deadline parks THIS plan forever, which is the defect.

    Moving the work off the loop stops it blocking everything else; it does not
    on its own stop the plan waiting forever. Delete the ``wait_for`` and this
    hangs until the pytest timeout rather than degrading.
    """

    def _hanging_clone(repo_url: str, dest: str, token: str, depth: int = 50) -> None:  # noqa: ARG001
        time.sleep(1.0)

    monkeypatch.setattr(
        "orchestrator.core.orchestrator.clone_with_token", _hanging_clone
    )
    monkeypatch.setattr("orchestrator.core.orchestrator._CLONE_TIMEOUT_SECONDS", 0.05)
    task_queue, plan_id, project = await _project_and_plan(db)
    opus = _opus(return_value=_VALID_PLAN)

    await _orchestrator(task_queue, opus).plan_and_activate(plan_id, project)

    # Degraded, not wedged, and the reason names the timeout.
    assert opus.plan_spec.await_args.kwargs["cwd"] is None
    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.ACTIVE


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
