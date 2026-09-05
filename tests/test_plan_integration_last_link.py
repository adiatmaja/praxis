"""The last link of the loop: the integration pull request.

Two defects, one surface.

**A failed integration was indistinguishable from a correct no-op.**
``process_plan_once`` writes COMPLETED before calling ``on_plan_completed`` and
wraps that call in a warn-only ``except``; inside, a failed ``open_integration_pr``
was warn-only too. Neither wrote ``plans.error``, and both the
nothing-to-integrate branch and the failure branch published
``plan_integration_ready`` with ``pr_url: None`` -- the SAME payload. So a
completed plan with ``integration_pr_url IS NULL`` was ambiguous between "the
work landed via the task PRs or was a no-op" and "the PR could not be opened
and the work is STRANDED on the plan branch", and the only evidence separating
them was one ``docker logs`` line. Four such plans were found in a live
database.

**A local-repo project could never get an integration PR at all.**
``on_plan_completed`` called ``self._git.open_integration_pr`` -- ``GitOps``,
GitHub-only via ``gh pr create`` -- instead of the backend seam every other git
operation on this path goes through. For a local project the slug was built by
splitting the repo URL on ``"github.com/"``, which for a filesystem path yields
the path itself, and the loop shelled ``gh pr create --repo C:\\...\\r``.
Measured before the fix::

    gh: expected the "[HOST/]OWNER/REPO" format, got "C:\\...\\r"

Result: a warning, ``pr_url`` None, plan COMPLETED, work stranded. Local mode
got the whole governed loop EXCEPT its last link.

The local tests here drive a REAL bare repo through the REAL path and never
mock ``open_integration_pr``. That mock is where the defect lived: every
existing test of this method wired it away, including the only one that used a
real local repo, which said so in a comment.
"""
# ruff: noqa: S101

from __future__ import annotations

import logging
import subprocess
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.core.event_bus import EventBus
from orchestrator.core.git_backend import (
    GitHubBackend,
    LocalGitBackend,
    PullRequestRef,
)
from orchestrator.core.git_ops import GitOps
from orchestrator.core.github_credentials import PatCredentialProvider
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.orchestrator_review import (
    _INTEGRATION_FAILED,
    _INTEGRATION_NOTHING,
    _INTEGRATION_OPENED,
)
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from tests.test_local_mode_e2e import seeded as _seeded_bare_repo


_REVIEW_LOGGER = "orchestrator.core.orchestrator_review"

# Re-exported under a local name so pytest registers the fixture in this module
# without shadowing the import in every signature (ruff F811).
bare_repo = _seeded_bare_repo

# ``seeded`` pushes ``agent/fix`` (holding ``return 2``) onto a bare repo whose
# ``main`` holds ``return 1``. The plan branch keys on the string it is handed
# and never on the name's shape, so this stands in for the accumulated plan
# branch without a second fixture.
_LOCAL_PLAN_BRANCH = "agent/fix"

_GITHUB_REPO = "https://github.com/o/r"
_GITHUB_PLAN_BRANCH = "plan/2026-08-25-widget"
_BASE_SHA = "a" * 40
_HEAD_SHA = "b" * 40


def _git_show(bare: Any, ref: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "show", ref],  # noqa: S607
        cwd=bare,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


async def _seed(db: Database, repo_url: str, plan_branch: str) -> tuple[TaskQueue, str]:
    """One activated plan on ``plan_branch`` for a project at ``repo_url``."""
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u1", "User", "hash"),
    )
    await db.execute(
        """INSERT INTO projects
           (id, user_id, name, repo_url, model_name, max_retries, default_branch)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("p1", "u1", "App", repo_url, "qwen3.8-27b", 3, "main"),
    )
    task_queue = TaskQueue(db)
    plan_id = await task_queue.create_plan("p1", "Build auth")
    await task_queue.activate_plan(
        plan_id,
        {
            "plan_summary": "Auth",
            "plan_slug": "auth",
            "tasks": [
                {
                    "title": "Login",
                    "slug": "login",
                    "description": "d",
                    "depends_on": [],
                }
            ],
        },
        plan_branch,
    )
    return task_queue, plan_id


def _github_git(*, head_sha: str | None, open_result: Any) -> MagicMock:
    """A ``GitOps`` double for the GitHub path.

    ``MagicMock`` with explicit ``AsyncMock`` members rather than a bare
    ``AsyncMock``: a bare one hands back the same truthy sentinel for every
    attribute, which is how ``_nothing_to_integrate_reason`` once compared two
    "equal" SHAs and skipped integration for every plan with its own tests
    green.
    """
    git = MagicMock()
    git.remote_head_sha = AsyncMock(
        side_effect=lambda _repo, branch: _BASE_SHA if branch == "main" else head_sha
    )
    if isinstance(open_result, BaseException):
        git.open_integration_pr = AsyncMock(side_effect=open_result)
    else:
        git.open_integration_pr = AsyncMock(return_value=open_result)
    git.repo_slug = MagicMock(return_value="o/r")
    return git


def _orchestrator(
    task_queue: TaskQueue, git: Any, bus: EventBus, *, neutralise_gate: bool
) -> Orchestrator:
    orch = Orchestrator(
        task_queue=task_queue,
        agent_manager=MagicMock(),
        opus_bridge=AsyncMock(),
        git_ops=git,
        event_bus=bus,
        context_sync=None,
    )
    if neutralise_gate:
        # The whole-plan verify gate and the already-open-PR lookup have their
        # own tests; neutralise them so these assert only the integration
        # decision. The local tests below neutralise NEITHER.
        orch._verify_plan_branch = AsyncMock(  # type: ignore[method-assign]
            return_value=MagicMock(status="skipped", output="")
        )
        orch._existing_integration_pr = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return orch


def _drain(bus: EventBus) -> Any:
    queue = bus.subscribe()

    def _events() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while not queue.empty():
            out.append(queue.get_nowait())
        return out

    return _events


def _ready(events: list[dict[str, Any]]) -> dict[str, Any]:
    return next(e for e in events if e["type"] == "plan_integration_ready")


# ---------------------------------------------------------------------------
# Defect 2: a failed integration must be recorded and must not read as a no-op.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_failed_integration_pr_is_recorded_on_the_plan_row(
    db: Database,
) -> None:
    """The plan row is the only surface a reader reaches minutes later.

    The SSE event goes to whoever is subscribed at that instant and the log
    line is not a surface any user reaches, so before this the stranding was
    discoverable only by an operator who already suspected it.
    """
    task_queue, plan_id = await _seed(db, _GITHUB_REPO, _GITHUB_PLAN_BRANCH)
    git = _github_git(head_sha=_HEAD_SHA, open_result=RuntimeError("gh auth fail"))
    bus = EventBus()

    await _orchestrator(task_queue, git, bus, neutralise_gate=True).on_plan_completed(
        plan_id
    )

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    error = str(plan["error"] or "")
    assert "gh auth fail" in error
    assert plan["integration_state"] == _INTEGRATION_FAILED
    # Names the branch the work is stranded on and the base it never reached,
    # or the operator has a reason with nothing to act on.
    assert _GITHUB_PLAN_BRANCH in error
    assert "main" in error


@pytest.mark.integration
async def test_a_failure_and_a_no_op_publish_different_integration_statuses(
    db: Database,
) -> None:
    """The two ``pr_url: None`` cases were the SAME payload.

    Both branches fall through to one publish, so a consumer could not tell
    "the work landed via the task PRs, nothing to do" from "the PR could not be
    opened and the work is stranded".
    """
    task_queue, plan_id = await _seed(db, _GITHUB_REPO, _GITHUB_PLAN_BRANCH)
    failed_bus = EventBus()
    failed_events = _drain(failed_bus)
    await _orchestrator(
        task_queue,
        _github_git(head_sha=_HEAD_SHA, open_result=RuntimeError("gh auth fail")),
        failed_bus,
        neutralise_gate=True,
    ).on_plan_completed(plan_id)

    nothing_bus = EventBus()
    nothing_events = _drain(nothing_bus)
    await _orchestrator(
        task_queue,
        # The plan branch resolves to the base's own SHA: every task closed as
        # a no-op, so there is genuinely nothing to open a PR for.
        _github_git(head_sha=_BASE_SHA, open_result="https://github.com/o/r/pull/9"),
        nothing_bus,
        neutralise_gate=True,
    ).on_plan_completed(plan_id)

    failed = _ready(failed_events())
    nothing = _ready(nothing_events())
    assert failed["pr_url"] is None
    assert nothing["pr_url"] is None
    assert failed["integration_status"] == _INTEGRATION_FAILED
    assert nothing["integration_status"] == _INTEGRATION_NOTHING
    # Each carries the reason it reached that status, so a consumer never has
    # to go back to the log to find out which fact settled it.
    assert "gh auth fail" in str(failed["integration_detail"])
    assert "identical to base" in str(nothing["integration_detail"])


@pytest.mark.integration
async def test_nothing_to_integrate_is_not_written_as_a_plan_error(
    db: Database,
) -> None:
    """A correct outcome must not be filed as a fault.

    ``plans.error`` is read by ``PlanResponse`` and MCP ``poll_plan``. Writing
    a no-op there would re-create the confusion from the other direction: every
    correctly-completed single-branch plan would report an error.
    """
    task_queue, plan_id = await _seed(db, _GITHUB_REPO, _GITHUB_PLAN_BRANCH)
    git = _github_git(head_sha=_BASE_SHA, open_result="https://github.com/o/r/pull/9")
    bus = EventBus()

    await _orchestrator(task_queue, git, bus, neutralise_gate=True).on_plan_completed(
        plan_id
    )

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["error"] is None
    git.open_integration_pr.assert_not_awaited()
    # The POSITIVE record of that correct outcome. Without it a completed plan
    # with no URL and no error was ambiguous between "nothing to integrate"
    # and "the integration stage is running right now": a wait on the plan
    # returned "nothing more will happen" 30 s before PR #13 existed.
    assert plan["integration_state"] == _INTEGRATION_NOTHING


# ---------------------------------------------------------------------------
# Defect 3: local mode reaches the last link too. No test below mocks
# ``open_integration_pr``; that mock is the defect.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_local_plan_gets_a_mergeable_integration_reference(
    db: Database, bare_repo: Any
) -> None:
    """A real bare repo through the real path, with real ``GitOps``.

    Before the fix this recorded nothing and logged ``Integration PR open
    failed`` after handing ``gh`` a filesystem path as an ``owner/repo`` slug.
    """
    task_queue, plan_id = await _seed(db, str(bare_repo), _LOCAL_PLAN_BRANCH)
    bus = EventBus()
    events = _drain(bus)
    orch = _orchestrator(
        task_queue,
        GitOps(PatCredentialProvider("")),
        bus,
        neutralise_gate=False,
    )

    await orch.on_plan_completed(plan_id)

    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    expected = PullRequestRef(
        backend="local", branch=_LOCAL_PLAN_BRANCH, base="main"
    ).to_url()
    assert plan["integration_pr_url"] == expected
    ready = _ready(events())
    assert ready["pr_url"] == expected
    assert ready["integration_status"] == _INTEGRATION_OPENED


@pytest.mark.integration
async def test_the_local_integration_reference_actually_lands_the_work(
    db: Database, bare_repo: Any
) -> None:
    """The proof that the reference is real rather than a plausible string.

    ``approve_plan_integration`` parses whatever ``on_plan_completed`` stored
    and hands it to ``backend.merge``. If the stored value were decorative the
    merge would raise or land nothing; here ``main`` must actually acquire the
    plan branch's content.
    """
    task_queue, plan_id = await _seed(db, str(bare_repo), _LOCAL_PLAN_BRANCH)
    bus = EventBus()
    orch = _orchestrator(
        task_queue, GitOps(PatCredentialProvider("")), bus, neutralise_gate=False
    )
    project = await task_queue.get_project("p1")
    assert project is not None

    await orch.on_plan_completed(plan_id)
    assert "return 1" in _git_show(bare_repo, "main:app.py")

    await orch.approve_plan_integration(plan_id, dict(project))

    assert "return 2" in _git_show(bare_repo, "main:app.py")
    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["integration_merged_at"] is not None


@pytest.mark.integration
async def test_a_local_plan_is_not_offered_a_fabricated_github_link(
    db: Database, bare_repo: Any
) -> None:
    """The other invented URL on the same payload.

    ``compare_url`` split the repo URL on ``"github.com/"``, found nothing in a
    filesystem path, and handed the path back as the slug, so the event carried
    ``https://github.com/C:\\...\\r/compare/main...agent/fix?expand=1`` -- a
    link that looks real and 404s, published beside a genuine integration
    reference. There is no compare page for a bare repo, and saying so is the
    only honest answer.
    """
    task_queue, plan_id = await _seed(db, str(bare_repo), _LOCAL_PLAN_BRANCH)
    bus = EventBus()
    events = _drain(bus)
    orch = _orchestrator(
        task_queue, GitOps(PatCredentialProvider("")), bus, neutralise_gate=False
    )

    await orch.on_plan_completed(plan_id)

    assert _ready(events())["compare_url"] is None


@pytest.mark.integration
async def test_a_local_plan_never_shells_out_to_gh(
    db: Database, bare_repo: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The observable signature of the defect, asserted directly.

    ``gh`` is installed on this machine and refuses the malformed slug locally,
    so the old path produced this warning without a network call and with the
    suite green.
    """
    task_queue, plan_id = await _seed(db, str(bare_repo), _LOCAL_PLAN_BRANCH)
    bus = EventBus()
    orch = _orchestrator(
        task_queue, GitOps(PatCredentialProvider("")), bus, neutralise_gate=False
    )

    with caplog.at_level(logging.INFO, logger=_REVIEW_LOGGER):
        await orch.on_plan_completed(plan_id)

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert not any("Integration PR open failed" in m for m in warnings), warnings
    assert not any("gh pr create" in m for m in warnings), warnings


@pytest.mark.integration
async def test_the_local_backend_refuses_to_reference_a_branch_it_cannot_see(
    bare_repo: Any,
) -> None:
    """ "There is no PR object" is not a licence to invent one.

    A reference to a branch the repository does not have would be stored on the
    plan row and handed to ``praxis merge-plan`` later, where it would fail with
    a message about merging rather than about the branch never existing.
    """
    backend = LocalGitBackend(str(bare_repo))

    with pytest.raises(RuntimeError, match="never-pushed"):
        await backend.open_integration_pr(
            base="main", head="plan/never-pushed", title="t", body="b"
        )


@pytest.mark.integration
async def test_a_github_backend_without_a_repository_refuses_rather_than_guessing() -> (
    None
):
    """The same refusal ``_repo`` makes, on the method that now joins the seam.

    Without a repository ``gh pr create`` carries no ``--repo`` and resolves
    against the orchestrator's own working directory, which would open a pull
    request on Praxis itself.
    """
    backend = GitHubBackend(AsyncMock())

    with pytest.raises(ValueError, match="repository URL"):
        await backend.open_integration_pr(base="main", head="x", title="t", body="b")


@pytest.mark.integration
async def test_the_github_path_still_reaches_gh_pr_create(db: Database) -> None:
    """Shared positive control, last on purpose.

    Every assertion above is about something NOT happening or about local mode.
    Routing GitHub through the new seam incorrectly (or routing everything to
    the local backend) would satisfy them all while silently ending GitHub
    integration PRs, which is the failure this change is supposed to remove.
    """
    task_queue, plan_id = await _seed(db, _GITHUB_REPO, _GITHUB_PLAN_BRANCH)
    git = _github_git(head_sha=_HEAD_SHA, open_result="https://github.com/o/r/pull/5")
    bus = EventBus()
    events = _drain(bus)

    await _orchestrator(task_queue, git, bus, neutralise_gate=True).on_plan_completed(
        plan_id
    )

    git.open_integration_pr.assert_awaited_once()
    kwargs = git.open_integration_pr.await_args.kwargs
    assert kwargs["repo_url"] == _GITHUB_REPO
    assert kwargs["head"] == _GITHUB_PLAN_BRANCH
    assert kwargs["base"] == "main"
    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["integration_pr_url"] == "https://github.com/o/r/pull/5"
    assert plan["error"] is None
    ready = _ready(events())
    assert ready["integration_status"] == _INTEGRATION_OPENED


@pytest.mark.integration
async def test_the_opened_outcome_is_recorded_on_the_plan_row(db: Database) -> None:
    task_queue, plan_id = await _seed(db, _GITHUB_REPO, _GITHUB_PLAN_BRANCH)
    git = _github_git(head_sha=_HEAD_SHA, open_result="https://github.com/o/r/pull/9")
    await _orchestrator(
        task_queue, git, EventBus(), neutralise_gate=True
    ).on_plan_completed(plan_id)
    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["integration_pr_url"] == "https://github.com/o/r/pull/9"
    assert plan["integration_state"] == _INTEGRATION_OPENED


@pytest.mark.integration
async def test_an_early_return_still_records_that_the_stage_ran(db: Database) -> None:
    """A plan with no branch never reaches the four outcomes; before this it
    recorded nothing, indistinguishable from a stage still running."""
    task_queue, plan_id = await _seed(db, _GITHUB_REPO, _GITHUB_PLAN_BRANCH)
    await db.execute(
        "UPDATE plans SET plan_branch_name = NULL WHERE id = ?", (plan_id,)
    )
    git = _github_git(head_sha=_HEAD_SHA, open_result="https://github.com/o/r/pull/9")
    await _orchestrator(
        task_queue, git, EventBus(), neutralise_gate=True
    ).on_plan_completed(plan_id)
    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["integration_state"] == "skipped"
    git.open_integration_pr.assert_not_awaited()


@pytest.mark.integration
async def test_a_recorded_outcome_is_not_overwritten_by_a_later_early_return(
    db: Database,
) -> None:
    """``approve_merges`` and a retried tick can re-enter the stage; a plan
    whose PR is already recorded must keep ``opened``, not become ``skipped``."""
    task_queue, plan_id = await _seed(db, _GITHUB_REPO, _GITHUB_PLAN_BRANCH)
    git = _github_git(head_sha=_HEAD_SHA, open_result="https://github.com/o/r/pull/9")
    await _orchestrator(
        task_queue, git, EventBus(), neutralise_gate=True
    ).on_plan_completed(plan_id)
    await db.execute(
        "UPDATE plans SET plan_branch_name = NULL WHERE id = ?", (plan_id,)
    )
    await _orchestrator(
        task_queue, git, EventBus(), neutralise_gate=True
    ).on_plan_completed(plan_id)
    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["integration_state"] == _INTEGRATION_OPENED


@pytest.mark.integration
async def test_the_outcome_is_recorded_before_the_context_sync_draft_runs(
    db: Database,
) -> None:
    """The stage keeps working after it decides (a clone and a brain call to
    draft the context sync, minutes on a real repo). Observed live: PR #16
    opened at 09:38:24 and ``integration_state`` stayed NULL until the draft
    finished, so for a no-PR outcome a wait would have said "integrating" for
    the whole draft. The record must land the moment the outcome is decided;
    the ``finally`` is the backstop for early returns, not the primary seat."""
    task_queue, plan_id = await _seed(db, _GITHUB_REPO, _GITHUB_PLAN_BRANCH)
    git = _github_git(head_sha=_HEAD_SHA, open_result="https://github.com/o/r/pull/9")
    orch = _orchestrator(task_queue, git, EventBus(), neutralise_gate=True)
    seen: list[str | None] = []

    class _Sync:
        async def draft(self, repo_url: str, summary: str) -> dict[str, Any]:
            plan = await task_queue.get_plan(plan_id)
            seen.append(plan["integration_state"] if plan else None)
            return {"draft_id": "d1", "path": "docs/x.md", "content": "x"}

    orch._context_sync = _Sync()
    await orch.on_plan_completed(plan_id)
    # The draft is a background follow-up now; the record still precedes it.
    await orch.drain_background()
    assert seen == [_INTEGRATION_OPENED]
