"""A PR url is what ``gh`` printed, never what its exit code implied.

``gh pr create`` exits 0 and prints the new pull request's URL. An exit code of
0 with no URL on stdout is not a created PR, and both PR-opening call sites
used to return that empty string:

- ``create_pr`` logged ``Created PR: `` and handed "" back.
- ``open_integration_pr`` logged ``Opened integration PR: `` and handed ""
  back, after which ``on_plan_completed`` guarded with ``if pr_url:`` and
  SILENTLY skipped ``set_plan_integration_pr``. The plan then sat with
  ``integration_pr_url`` NULL, which is what every read-only surface
  (``praxis pending``, ``merge-plan``, the dashboard) filters on: the log said
  the PR was opened and every surface said there was nothing to approve.

This is the house rule the harness entrypoints already state for ``gh pr
list``: the emptiness of the OUTPUT is the signal, never the exit status.
"""
# ruff: noqa: S101

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.core.git_ops import GitOps


async def _open_integration_pr(git: GitOps) -> str:
    return await git.open_integration_pr(
        repo_url="https://github.com/o/r",
        base="main",
        head="plan/feature-x",
        title="Integrate plan/feature-x",
        body="Auto-opened by Praxis on plan completion.",
    )


async def _create_pr(git: GitOps) -> str:
    return await git.create_pr(
        "/tmp/workspace",  # noqa: S108 - argv only, never touched on disk
        title="feat: login page",
        body="Implements login",
        base="plan/2026-06-01-auth",
        head="agent/login",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "stdout",
    ["", "   \n", "Warning: 1 uncommitted change\n"],
    ids=["empty", "whitespace", "no-url-in-output"],
)
@patch("orchestrator.core.git_ops.GitOps._run_command")
async def test_an_integration_pr_with_no_url_in_the_output_raises(
    mock_run: AsyncMock, stdout: str
) -> None:
    """The one that loses the whole plan: "" reads as "no PR was opened".

    The caller treats a falsy return as "nothing to record" and moves on
    without a warning, so the failure has to arrive as a failure.
    """
    mock_run.return_value = (0, stdout, "")
    git = GitOps("ghp_test")

    with pytest.raises(RuntimeError) as excinfo:
        await _open_integration_pr(git)

    message = str(excinfo.value)
    assert "plan/feature-x" in message, message
    assert "main" in message, message


@pytest.mark.unit
@pytest.mark.parametrize(
    "stdout",
    ["", "   \n", "Warning: 1 uncommitted change\n"],
    ids=["empty", "whitespace", "no-url-in-output"],
)
@patch("orchestrator.core.git_ops.GitOps._run_command")
async def test_a_task_pr_with_no_url_in_the_output_raises(
    mock_run: AsyncMock, stdout: str
) -> None:
    """Same shape, second call site.

    Its own scenario on purpose: two call sites sharing one helper are still
    two chances for one of them to keep returning "" unnoticed, and this is
    the one whose "" would be stored as a task's ``pr_url``.
    """
    mock_run.return_value = (0, stdout, "")
    git = GitOps("ghp_test")

    with pytest.raises(RuntimeError) as excinfo:
        await _create_pr(git)

    message = str(excinfo.value)
    assert "agent/login" in message, message


@pytest.mark.unit
@patch("orchestrator.core.git_ops.GitOps._run_command")
async def test_a_url_printed_after_gh_chatter_is_still_returned(
    mock_run: AsyncMock,
) -> None:
    """The working branch: a real URL must survive, wherever on stdout it lands.

    ``gh`` prints advisory lines around the URL often enough that a strict
    "the whole of stdout is the url" rule would turn working runs into
    failures, which is the opposite defect.
    """
    mock_run.return_value = (
        0,
        "Warning: 3 uncommitted changes\nhttps://github.com/o/r/pull/42\n",
        "",
    )
    git = GitOps("ghp_test")

    assert await _open_integration_pr(git) == "https://github.com/o/r/pull/42"
