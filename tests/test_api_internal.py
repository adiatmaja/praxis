"""Tests for /api/internal/agent-done callback endpoint."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from orchestrator.api.internal import _resolved_as_no_op
from orchestrator.core.clarification_states import ANSWERED_BY_BRAIN
from orchestrator.core.session_resume import resolve_resume_session
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus
from tests.conftest import seed_user


@pytest.fixture(autouse=True)
def _mock_preflight(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    m = AsyncMock(return_value=[])
    monkeypatch.setattr("orchestrator.api.projects.preflight_remote", m)
    return m


async def _setup_plan_with_task(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    harness: str | None = None,
) -> tuple[str, str]:
    """Create a project + plan + in-progress task; return (plan_id, task_id).

    ``harness`` pins the project's harness explicitly. Left unset, project
    creation resolves it from settings.default_worker_harness, which is a
    deployment-configurable default (config/praxis.yaml) that callers
    shouldn't have to know about unless the test cares which harness wins.
    """
    await seed_user(db)
    payload: dict[str, str] = {
        "name": "App",
        "repo_url": "https://github.com/u/a",
        "model_name": "m",
    }
    if harness is not None:
        payload["harness"] = harness
    project = await client.post(
        "/api/projects",
        json=payload,
        headers=auth_headers,
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]
    plan_id = await queue.create_plan(project.json()["id"], "Build auth")
    await queue.activate_plan(
        plan_id,
        {
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
        },
        "plan/2026-06-01-auth",
    )
    task_id = (await queue.get_tasks_for_plan(plan_id))[0]["id"]
    await queue.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    await queue.create_agent_run(task_id, "container-abc")
    return plan_id, task_id


async def _seed_in_progress_task(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    attempt: int = 1,
    max_retries: int = 3,
) -> tuple[str, str]:
    """Create project+plan+task(in_progress)+run; return (task_id, run_id)."""
    await seed_user(db)
    project_resp = await client.post(
        "/api/projects",
        json={
            "name": "RetryApp",
            "repo_url": "https://github.com/u/retry",
            "model_name": "m",
            "max_retries": max_retries,
        },
        headers=auth_headers,
    )
    project_id = project_resp.json()["id"]
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]
    plan_id = await queue.create_plan(project_id, "Retry plan")
    await queue.activate_plan(
        plan_id,
        {
            "plan_summary": "Retry",
            "plan_slug": "retry",
            "tasks": [
                {
                    "title": "Do thing",
                    "slug": "do-thing",
                    "description": "Do the thing",
                    "depends_on": [],
                }
            ],
        },
        "plan/2026-07-04-retry",
    )
    task_id = (await queue.get_tasks_for_plan(plan_id))[0]["id"]
    # Set attempt to the requested value
    await db.execute(
        "UPDATE tasks SET status = ?, attempt = ? WHERE id = ?",
        (TaskStatus.IN_PROGRESS, attempt, task_id),
    )
    run_id = await queue.create_agent_run(task_id, "container-retry")
    return task_id, run_id


@pytest.mark.integration
async def test_failed_callback_retries_when_budget_remains(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=1, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]

    resp = await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json={"task_id": task_id, "run_id": run_id, "status": "failed"},
    )
    assert resp.status_code == 200

    task = await queue.get_task(task_id)
    assert task["status"] == TaskStatus.PENDING
    assert int(task["attempt"]) == 2


@pytest.mark.integration
async def test_failed_callback_marks_failed_when_budget_exhausted(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=3, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]

    resp = await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json={"task_id": task_id, "run_id": run_id, "status": "failed"},
    )
    assert resp.status_code == 200

    task = await queue.get_task(task_id)
    assert task["status"] == TaskStatus.FAILED


@pytest.mark.integration
async def test_no_changes_callback_closes_the_task_as_a_no_op(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seam: a `no_changes` callback must not land in the retry branch.

    Both halves are correct on their own and the wiring between them is
    invisible. A worker reporting a status the router does not know silently
    falls through to retry/fail, which is exactly the defect being fixed.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=1, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]

    async def _accept(
        task_id_arg: str, project: dict, plan: dict | None
    ) -> tuple[bool, str]:
        await queue.mark_no_changes(task_id_arg, "already satisfied")
        return True, "verify passed on the base branch"

    # ``no_change_outcome``, not the ``resolve_no_change_run`` wrapper: the
    # callback path takes the reason as well as the answer. Patching the
    # wrapper leaves the real check running and the test measures nothing.
    monkeypatch.setattr(client.app.state.orchestrator, "no_change_outcome", _accept)

    resp = await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json={"task_id": task_id, "run_id": run_id, "status": "no_changes"},
    )
    assert resp.status_code == 200

    task = await queue.get_task(task_id)
    assert task["status"] == TaskStatus.NO_CHANGES
    assert int(task["attempt"]) == 1, "a no-op must not consume a retry"


@pytest.mark.integration
async def test_a_rejected_no_changes_callback_falls_through_to_the_failure_path(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A no-op is terminal with no PR and no review, so it needs a POSITIVE yes.

    Anything else, including the resolver raising, has to reach the ordinary
    retry path. Invert this and a worker that produced nothing when something
    was needed closes its leaf clean and the plan reports success.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=1, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]

    async def _explode(
        task_id_arg: str, project: dict, plan: dict | None
    ) -> tuple[bool, str]:
        msg = "verify blew up"
        raise RuntimeError(msg)

    monkeypatch.setattr(client.app.state.orchestrator, "no_change_outcome", _explode)

    resp = await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json={"task_id": task_id, "run_id": run_id, "status": "no_changes"},
    )
    assert resp.status_code == 200

    task = await queue.get_task(task_id)
    assert task["status"] == TaskStatus.PENDING
    assert int(task["attempt"]) == 2


@pytest.mark.integration
async def test_a_declined_no_change_records_which_fact_declined(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stored reason names what happened, not one fixed sentence.

    The check declines for at least six unrelated facts and only ONE of them is
    "the branch did not verify clean". This path asserted that one for all of
    them, and ``core/worker_bible`` injects the stored feedback verbatim into
    the next worker's prompt, so a worker was sent to fix a verification nobody
    had run. The review path was corrected for exactly this on 2026-08-24 while
    this path, the one both harness entrypoints actually take, kept the string.

    Asserted on the retries-exhausted branch because that is where the feedback
    is written; the retry branch stores none.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=1, max_retries=1
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]

    async def _declined(
        task_id_arg: str, project: dict, plan: dict | None
    ) -> tuple[bool, str]:
        return False, "the verify gate on plan/x was skipped (no credential)"

    monkeypatch.setattr(client.app.state.orchestrator, "no_change_outcome", _declined)

    resp = await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json={"task_id": task_id, "run_id": run_id, "status": "no_changes"},
    )
    assert resp.status_code == 200

    task = await queue.get_task(task_id)
    assert task["status"] == TaskStatus.FAILED
    feedback = task["review_feedback"] or ""
    assert "was skipped" in feedback
    assert "did not verify clean" not in feedback


@pytest.mark.integration
async def test_a_completed_callback_with_a_pr_url_moves_the_task_to_reviewing(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """The working branch of the guard below, so the fix cannot over-reach.

    Delete this and a guard that failed EVERY completed callback would look
    correct: the wedge case would be caught and the normal case would go
    unnoticed until a real run parked nothing for review.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=1, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]

    resp = await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json={
            "task_id": task_id,
            "run_id": run_id,
            "status": "completed",
            "pr_url": "https://github.com/u/retry/pull/7",
        },
    )
    assert resp.status_code == 200

    task = await queue.get_task(task_id)
    assert task["status"] == TaskStatus.REVIEWING
    assert task["pr_url"] == "https://github.com/u/retry/pull/7"


@pytest.mark.integration
async def test_a_completed_callback_with_no_pr_url_fails_instead_of_wedging(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """REVIEWING with a NULL pr_url is a permanent wedge, not a review.

    ``review_task`` returns immediately when ``pr_url`` is None and is
    re-entered on every loop tick, while REVIEWING counts as ACTIVE: the plan
    never completes and ``plan_stalled`` stays suppressed because it requires
    ``not active``. The only symptom is silence.

    Asserted on the row the callback wrote, not on a log line or a summary:
    the status carried forward is the thing that wedges.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=3, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]

    resp = await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json={"task_id": task_id, "run_id": run_id, "status": "completed"},
    )
    assert resp.status_code == 200

    task = await queue.get_task(task_id)
    assert task["status"] != TaskStatus.REVIEWING, (
        "a task with no pull request was parked for a review that can never "
        "start, and the plan can never complete"
    )
    assert task["status"] == TaskStatus.FAILED
    feedback = task["review_feedback"] or ""
    assert "pull request" in feedback.lower(), feedback
    assert "no pull-request URL" in feedback, (
        f"the stored reason does not say what happened: {feedback!r}"
    )


@pytest.mark.integration
async def test_a_completed_callback_with_no_pr_url_retries_when_budget_remains(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """A lost PR url is worth another attempt, exactly like any other failure.

    The point of failing rather than parking is that the plan can PROGRESS.
    A fix that only marked the task FAILED terminally would still be wrong for
    a task with retries left.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=1, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]

    resp = await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json={"task_id": task_id, "run_id": run_id, "status": "completed"},
    )
    assert resp.status_code == 200

    task = await queue.get_task(task_id)
    assert task["status"] == TaskStatus.PENDING
    assert int(task["attempt"]) == 2


@pytest.mark.integration
async def test_a_completed_callback_may_rely_on_a_pr_url_stored_earlier(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """Both harnesses REUSE an open PR across retries, so silence is not loss.

    A resumed or retried run whose callback omits ``pr_url`` still has a
    reviewable pull request on the row. Failing that task would turn a working
    retry into a dead one, which is why the guard reads the effective url and
    not just the payload field.
    """
    task_id, run_id = await _seed_in_progress_task(
        client, db, auth_headers, attempt=1, max_retries=3
    )
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]
    await queue.set_task_pr_url(task_id, "https://github.com/u/retry/pull/3")

    resp = await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json={"task_id": task_id, "run_id": run_id, "status": "completed"},
    )
    assert resp.status_code == 200

    task = await queue.get_task(task_id)
    assert task["status"] == TaskStatus.REVIEWING
    assert task["pr_url"] == "https://github.com/u/retry/pull/3"


@pytest.mark.integration
async def test_agent_done_needs_clarification_parks_task(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    _, task_id = await _setup_plan_with_task(client, db, auth_headers)
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]

    resp = await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json={
            "task_id": task_id,
            "status": "needs_clarification",
            "question": "Which config file holds the API base?",
        },
    )
    assert resp.status_code == 200

    task = await queue.get_task(task_id)
    assert task["status"] == TaskStatus.NEEDS_CLARIFICATION
    assert task["clarification_question"] == "Which config file holds the API base?"


@pytest.mark.integration
async def test_agent_done_persists_session_id_with_project_harness(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """The callback's session_id is stored paired with the project's REAL harness.

    Pinned to "agy", which is deliberately NOT what default_harness_id()
    returns ("opencode"): if the fallback default were used instead of the
    project's actual harness, this assertion would catch it.
    """
    _, task_id = await _setup_plan_with_task(client, db, auth_headers, harness="agy")
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]

    resp = await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json={
            "task_id": task_id,
            "status": "needs_clarification",
            "question": "Which config file holds the API base?",
            "session_id": "ses_live_123",
        },
    )
    assert resp.status_code == 200

    task = await queue.get_task(task_id)
    assert task["worker_session_id"] == "ses_live_123"
    assert task["worker_session_harness"] == "agy"


@pytest.mark.integration
async def test_agent_done_without_session_id_leaves_existing_handle_untouched(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """A callback with no session_id must not clobber a previously-stored handle.

    A fresh task's columns are NULL either way, so that alone proves nothing.
    Instead, first send a REAL prior callback that carries a session_id (going
    through the same endpoint code under test, not a direct TaskQueue call),
    THEN send a second callback with no session_id, then assert the first
    call's value survived. If the persistence guard were ever deleted, the
    first callback would never have stored anything and this assertion would
    fail on a None, not silently pass on an untouched-but-still-NULL column.
    """
    _, task_id = await _setup_plan_with_task(client, db, auth_headers, harness="agy")
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]

    first = await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json={
            "task_id": task_id,
            "status": "needs_clarification",
            "question": "first turn",
            "session_id": "ses_prior_456",
        },
    )
    assert first.status_code == 200

    # The pr_url is load-bearing here even though this test is about the
    # session handle: a ``completed`` callback with no pull request is a
    # FAILURE now (there is nothing to review), and the failure path clears
    # the worker session handle on purpose. Keep it, or this test stops
    # exercising the no-session_id branch it exists for.
    second = await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json={
            "task_id": task_id,
            "status": "completed",
            "pr_url": "https://github.com/u/a/pull/9",
        },
    )
    assert second.status_code == 200

    task = await queue.get_task(task_id)
    assert task["worker_session_id"] == "ses_prior_456"
    assert task["worker_session_harness"] == "agy"


@pytest.mark.integration
async def test_plain_failure_retry_after_resume_clears_session_handle(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """A resumed run that then fails ordinarily must not stay resumable.

    Reproduces the exact defect: a task was clarified once (state=
    answered_by_brain, a stored session handle, attempt below max_retries),
    so the immediately-following resume dispatch was correct per design.
    That resumed run then fails for an unrelated reason (flaky test, model
    gives up) with NO session_id on the callback -- an ordinary crash, not a
    fresh BLOCKED checkpoint. Because attempt < max_retries, the callback
    takes the plain-retry branch (queue.retry_task), not fail_task. Before
    the fix, retry_task never touched worker_session_id/harness, so the
    stale handle plus the still-answered_by_brain clarification_state would
    let the NEXT dispatch wrongly resume a conversation about a branch that
    retry_task just rebuilt from base.
    """
    _, task_id = await _setup_plan_with_task(client, db, auth_headers, harness="agy")
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]

    # Seed the state right after a successful resume dispatch.
    await queue.record_worker_session(task_id, "ses_resumed_789", "agy")
    await db.execute(
        "UPDATE tasks SET clarification_state = ?, attempt = ? WHERE id = ?",
        (ANSWERED_BY_BRAIN, 1, task_id),
    )
    seeded = await queue.get_task(task_id)
    assert seeded["clarification_state"] == ANSWERED_BY_BRAIN
    assert seeded["worker_session_id"] == "ses_resumed_789"
    assert seeded["worker_session_harness"] == "agy"
    assert int(seeded["attempt"]) == 1

    # Ordinary crash: status="failed", no session_id at all.
    resp = await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json={"task_id": task_id, "status": "failed"},
    )
    assert resp.status_code == 200

    task = await queue.get_task(task_id)
    assert task["status"] == TaskStatus.PENDING
    assert int(task["attempt"]) == 2
    assert task["worker_session_id"] is None

    # The property that actually matters: the gate refuses to resume.
    assert resolve_resume_session(task, "agy") is None


class _BrokenDocker:
    """An agent manager whose Docker calls fail the way a restarted daemon does.

    ``AgentManager`` catches only ``docker.errors.NotFound``; an ``APIError`` or
    a ``DockerException`` from a daemon that went away propagates. Modelled as a
    plain ``RuntimeError`` so the test does not depend on the docker SDK being
    importable, which is the same shape the endpoint has to survive.
    """

    def __init__(self) -> None:
        self.logs_calls = 0
        self.cleanup_calls = 0

    def get_container_logs(self, container_id: str) -> str:
        self.logs_calls += 1
        message = "Error while fetching server API version"
        raise RuntimeError(message)

    def cleanup_container(self, container_id: str) -> None:
        self.cleanup_calls += 1
        message = "Error while fetching server API version"
        raise RuntimeError(message)


@pytest.mark.integration
async def test_a_docker_hiccup_reading_logs_never_strands_the_result(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """Telemetry must not be able to lose the result it describes.

    ``get_container_logs`` runs BEFORE ``complete_agent_run`` and before the PR
    url is stored, so an uncaught Docker error aborted the callback with none of
    the worker's outcome recorded: the finished PR was stranded and the task sat
    IN_PROGRESS until the reconcile sweep. Docker Desktop restarts and WSL2
    clock resyncs are documented as routine on this platform.
    """
    task_id, run_id = await _seed_in_progress_task(client, db, auth_headers)
    broken = _BrokenDocker()
    client.app.state.agent_manager = broken  # type: ignore[attr-defined]

    resp = await client.post(
        "/api/internal/agent-done",
        json={
            "task_id": task_id,
            "run_id": run_id,
            "status": "completed",
            "pr_url": "https://github.com/u/retry/pull/7",
        },
        headers={"X-Praxis-Callback-Token": auth_headers["Authorization"].split()[1]},
    )

    assert broken.logs_calls == 1
    assert resp.status_code == 200
    # The outcome was recorded despite the log read failing.
    task = await db.fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    assert task["pr_url"] == "https://github.com/u/retry/pull/7"
    assert task["status"] != TaskStatus.IN_PROGRESS.value


@pytest.mark.integration
async def test_a_docker_hiccup_removing_the_container_never_replays_the_callback(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """Cleanup runs AFTER the whole state machine has committed.

    Raising there answered 500, which the harness entrypoints treat as "not
    delivered" and retry five times (``CALLBACK_MAX_ATTEMPTS``), replaying a
    fully-processed callback: five run completions, five retry-or-fail
    decisions, and the retry budget spent up to five times over on one worker
    run. Housekeeping is not a verdict.
    """
    task_id, run_id = await _seed_in_progress_task(client, db, auth_headers)
    broken = _BrokenDocker()
    # Only cleanup fails here, so this test isolates the second call site.
    broken.get_container_logs = lambda _cid: "log body"  # type: ignore[assignment] # noqa: ARG005
    client.app.state.agent_manager = broken  # type: ignore[attr-defined]

    resp = await client.post(
        "/api/internal/agent-done",
        json={
            "task_id": task_id,
            "run_id": run_id,
            "status": "completed",
            "pr_url": "https://github.com/u/retry/pull/8",
        },
        headers={"X-Praxis-Callback-Token": auth_headers["Authorization"].split()[1]},
    )

    assert broken.cleanup_calls == 1
    assert resp.status_code == 200


@pytest.mark.unit
async def test_the_no_op_resolver_cannot_be_made_to_forge_a_log_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The scrub belongs to the log boundary, not to the handler that calls it.

    ``agent_done`` sanitizes ``task_id`` once at the top and logs the sanitized
    copy everywhere, which reads as whole-handler protection. It then passed
    the RAW ``body.task_id`` to this helper, which logs it too, so one path was
    guarded and its twin was not. Asserting here rather than through the
    endpoint is deliberate: the endpoint's ``WHERE id = ?`` lookup rejects a
    forged id before this line is reachable, so a test driven through the
    callback would pass whether or not the scrub exists.
    """
    forged = "t-1\nERROR:orchestrator:task t-9 merged to main by admin"

    async def _explode(task_id_arg: str, project: dict, plan: dict | None) -> None:
        msg = "resolver down"
        raise RuntimeError(msg)

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                orchestrator=SimpleNamespace(no_change_outcome=_explode)
            )
        )
    )

    with caplog.at_level(logging.ERROR, logger="orchestrator.api.internal"):
        closed, why = await _resolved_as_no_op(
            request,  # type: ignore[arg-type]
            forged,
            {"id": "p-1"},
            None,
        )

    assert closed is False
    assert "raised" in why
    logged = [r for r in caplog.records if "no-change resolution failed" in r.message]
    assert len(logged) == 1
    rendered = logged[0].getMessage()
    assert "\n" not in rendered
    assert "\r" not in rendered
    assert "ERROR:orchestrator:task t-9 merged" in rendered
