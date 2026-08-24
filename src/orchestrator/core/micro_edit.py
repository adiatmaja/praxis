"""The micro-edit lane: the brain commits, and the loop still governs it.

Auto-delegate mode has one lane, and it spends a container spawn, a full clone,
a worker turn, a push and a first-tier review on every task regardless of size.
For a typo in a docstring or a config value that needs bumping from 3 to 5 that
is disproportionate by roughly two orders of magnitude. The user's framing on
2026-08-21: delegating a one-line edit is not delegation, it is ceremony.

The naive fix would be worse than the problem. "Small changes skip the loop"
would falsify the product's central claim, which is that every change is
governed. So the shape is fixed by the promise:

    **The micro-edit lane skips the WORKER. It never skips the governance.**

What that means concretely, and every row is enforced somewhere rather than
promised here:

- No container and no worker turn. This module commits the change directly.
- The verify gate runs unchanged, on the same fail-closed terms, because the
  task goes to REVIEWING and ``orchestrator_review.review_task`` gates it like
  any other.
- A review runs and its verdict is recorded, at the re-review tier.
- The change reaches the human merge gate on the same pull request.
- The outcome row is attributed to the BRAIN, via
  ``tasks.implement_harness``/``implement_model``, so the capability
  calibration loop is never taught that the configured worker succeeded at a
  task it never saw.

Spec, including the four corrections this module is written against:
``docs/superpowers/specs/2026-08-21-micro-edit-lane.md``.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.core.git_ops import (
    checkout_branch,
    commit_and_push,
    local_head_sha,
)


logger = logging.getLogger(__name__)


#: Recorded in ``tasks.implement_harness`` and ``tasks.implement_model`` for a
#: change the lane committed. Two jobs, and both matter.
#:
#: It keeps the calibration signal truthful: ``orchestrator_review._record``
#: passes these columns straight into ``record_outcome``, so without them a
#: micro edit would be filed against the project's configured worker model and
#: teach the capability loop that the worker succeeded at a task it never saw.
#: ``capability_history`` selects ``WHERE model_name = ?``, so this sentinel is
#: never folded into a real model's history; it is simply never selected.
#:
#: And it is the marker the rest of the loop reads to know a task took this
#: lane, which is why it lives here rather than being spelled out at each site.
#: Praxis cannot know which model the calling brain runs, so the honest value
#: names the ROLE and claims nothing about the model.
BRAIN_IMPLEMENTER = "brain"


class MicroEditError(Exception):
    """The lane could not apply the edit. Carries an operator-facing message."""


@dataclass(frozen=True)
class MicroEditResult:
    """What the lane did, as facts rather than as a verdict.

    Attributes:
        committed: True when a commit was made and pushed. False means the
            index was already clean, which for a micro edit means the file
            already held the requested content. That is a FACT and not a
            failure; what it MEANS is decided by the caller, through the same
            no-change governance a worker's empty diff goes through.
        base_sha: The commit the branch was at BEFORE this change, or None
            when nothing was committed. This is the lane's own base sha, read
            after checkout and before the write.
        pr_url: The pull request carrying the commit, reused when one was
            already open for this branch.
        path: The file the lane wrote, echoed back for logging.
    """

    committed: bool
    base_sha: str | None
    pr_url: str | None
    path: str


def resolve_target(workspace: str, path: str) -> Path:
    """Resolve ``path`` inside ``workspace``, refusing anything that escapes.

    The path arrives from a caller, so it is untrusted input to a filesystem
    write. Same guard, and the same reason, as
    ``brainstorm.write_and_commit``.

    Raises:
        MicroEditError: If ``path`` is absolute or climbs out of the clone.
    """
    root = Path(workspace).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        msg = f"micro-edit path escapes the workspace: {path!r}"
        raise MicroEditError(msg)
    return target


async def apply_micro_edit(
    git: Any,
    *,
    repo_url: str,
    branch: str,
    base_branch: str,
    path: str,
    content: str,
    commit_message: str,
    pr_title: str,
    pr_body: str,
    existing_pr: str | None = None,
) -> MicroEditResult:
    """Commit ``content`` to ``path`` on ``branch`` and make sure a PR carries it.

    Clones server-side into a temporary workspace rather than reaching for a
    contents API. Correction 1 in the spec: ``brainstorm.write_and_commit``,
    cited there as proof that the API path was already proven on this
    codebase, in fact clones too, so the clone is the proven mechanism and the
    only one.

    Args:
        git: A ``GitOps``.
        repo_url: The project's repository.
        branch: The shared work branch to commit on. Created from
            ``base_branch`` when it does not exist on the remote yet.
        base_branch: The branch ``branch`` is cut from, and the PR's base.
        path: Repository-relative path of the single file to write.
        content: Its full new content.
        commit_message: The commit subject.
        pr_title: Title for a pull request, used only when one must be opened.
        pr_body: Body for the same.
        existing_pr: An already-open pull request for this branch, when the
            caller positively found one. None means "open one if a commit was
            made", never "there is definitely none".

    Returns:
        A :class:`MicroEditResult`.

    Raises:
        MicroEditError: If the path escapes the workspace, or if the remote
            could not be asked whether ``branch`` exists. Not being able to
            ask is not an answer, and guessing would either commit onto a
            branch cut from the wrong place or fail to create one at all.
    """
    try:
        branch_head = await git.remote_head_sha(repo_url, branch)
    except Exception as exc:
        msg = (
            f"could not determine whether {branch} exists on the remote "
            f"({exc}); refusing to commit a micro edit without knowing "
            "which branch it would land on"
        )
        raise MicroEditError(msg) from exc

    token = await git._token_for_repo(repo_url)

    with tempfile.TemporaryDirectory() as workspace:
        await git.clone_repo(repo_url, workspace)

        if isinstance(branch_head, str) and branch_head:
            # The branch is already there, carrying other tasks' commits in
            # single-branch mode. Check it out rather than trying to create it.
            checkout_branch(workspace, branch, token)
        else:
            await git.create_branch(workspace, branch, base_branch)

        # BEFORE the write, and this ordering is the point. The review is
        # bounded to ``review_base_sha..head``, so a sha read after the commit
        # would already contain the change and the range would be empty, which
        # reviews as a trivially passing change.
        base_sha = local_head_sha(workspace)

        target = resolve_target(workspace, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # newline="" so the bytes committed are exactly the bytes given. The
        # default would rewrite every "\n" to "\r\n" on Windows, and a repo
        # pinned to LF would take a whole-file line-ending change as the diff.
        with target.open("w", encoding="utf-8", newline="") as handle:
            handle.write(content)

        committed = commit_and_push(workspace, token, commit_message, paths=[path])
        if not committed:
            # A FACT, not a failure: the file already held this content, so
            # there is nothing to commit, nothing to review, and no PR to open.
            logger.info(
                "micro edit on %s left %s unchanged; nothing was committed",
                branch,
                path,
            )
            return MicroEditResult(
                committed=False, base_sha=None, pr_url=None, path=path
            )

        pr_url = existing_pr
        if pr_url is None:
            pr_url = await git.create_pr(
                workspace, pr_title, pr_body, base_branch, branch
            )

    return MicroEditResult(committed=True, base_sha=base_sha, pr_url=pr_url, path=path)
