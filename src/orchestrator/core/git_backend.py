"""Git hosting seam: GitHub PRs or a local bare repo.

The whole loop above this module (merge gate, verify gates, review flow,
outcome recording) is identical for both backends.  What differs is only the
plumbing that GitHub calls a pull request:

- ``github``: a real PR object, created and merged with the ``gh`` CLI.
- ``local``: the project's ``repo_url`` is a filesystem path to a BARE repo.
  There is no PR object, so a "PR" is just the (branch, base) pair recorded on
  the task; the diff is ``git diff base...branch`` from a fresh clone and the
  merge is a real ``git merge --squash`` executed in a clone and pushed.

Local mode exists because the benchmark needs to run 100+ instances without
rate-limiting a GitHub account, and because "evaluate Praxis without giving it
a GitHub credential" removes the single largest setup cliff.  GitHub stays the
default and the recommendation for real work: an inspectable PR is the unit of
trust.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from typing import Any, Protocol, cast
from urllib.parse import quote, unquote


logger = logging.getLogger(__name__)

# A local "PR" is encoded as a URL so it can live in the existing
# ``tasks.pr_url`` TEXT column with no schema change and stay greppable.
_LOCAL_PR_RE = re.compile(r"^praxis-local://pr\?branch=([^&]+)&base=([^&]+)$")

_WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")

_GITHUB_PR_RE = re.compile(r"^https://github\.com/([^/]+/[^/]+)/pull/(\d+)/?$")


def is_local_repo_url(repo_url: str) -> bool:
    """True when ``repo_url`` names a local bare repo rather than a remote host.

    Recognized: a ``file://`` URL, a POSIX absolute path, and a Windows drive
    path.  Everything else (https, ssh, scp-style) is remote.

    Args:
        repo_url: The project's configured repository URL or path.

    Returns:
        True if the URL points at the local filesystem.
    """
    value = (repo_url or "").strip()
    if not value:
        return False
    if value.startswith("file://"):
        return True
    if _WINDOWS_PATH_RE.match(value):
        return True
    return value.startswith("/")


def local_repo_path(repo_url: str) -> str:
    """Return the filesystem path for a local repo URL.

    Args:
        repo_url: A ``file://`` URL or a bare filesystem path.

    Returns:
        The path with any ``file://`` scheme and percent-encoding removed.
    """
    value = repo_url.strip()
    if value.startswith("file://"):
        value = unquote(value[len("file://") :])
        # file:///c:/x on Windows leaves a leading slash before the drive.
        if _WINDOWS_PATH_RE.match(value.lstrip("/")):
            value = value.lstrip("/")
    return value


@dataclass(frozen=True)
class PullRequestRef:
    """A reviewable change, in whichever form the backend expresses one."""

    backend: str
    branch: str
    base: str
    number: int | None = None
    repo: str | None = None

    def to_url(self) -> str:
        """Render the ref for storage in ``tasks.pr_url``.

        Returns:
            A GitHub PR URL, or a ``praxis-local://`` ref for local mode.
        """
        if self.backend == "github":
            return f"https://github.com/{self.repo}/pull/{self.number}"
        return (
            "praxis-local://pr"
            f"?branch={quote(self.branch, safe='')}"
            f"&base={quote(self.base, safe='')}"
        )

    @classmethod
    def from_url(cls, url: str) -> PullRequestRef:
        """Parse a stored ``pr_url`` back into a ref.

        Args:
            url: A GitHub PR URL or a ``praxis-local://`` ref.

        Returns:
            The parsed reference.

        Raises:
            ValueError: If the URL is neither a GitHub PR URL nor a local ref.
        """
        match = _LOCAL_PR_RE.match(url.strip())
        if match:
            return cls(
                backend="local",
                branch=unquote(match.group(1)),
                base=unquote(match.group(2)),
            )
        github = _GITHUB_PR_RE.match(url.strip())
        if github:
            return cls(
                backend="github",
                branch="",
                base="",
                number=int(github.group(2)),
                repo=github.group(1),
            )
        message = f"unrecognized pull-request reference: {url!r}"
        raise ValueError(message)


class GitBackend(Protocol):
    """What the review loop needs from a git host."""

    name: str

    async def get_diff(self, ref: PullRequestRef) -> str:
        """Return the unified diff of the change."""
        ...

    async def checkout(self, ref: PullRequestRef, dest: str) -> str:
        """Clone and check out the change's head into ``dest``; return ``dest``."""
        ...

    async def comment(self, ref: PullRequestRef, body: str) -> None:
        """Record review feedback against the change."""
        ...

    async def merge(self, ref: PullRequestRef) -> None:
        """Squash-merge the change into its base."""
        ...


class GitHubBackend:
    """The existing ``gh``-CLI behavior, unchanged, behind the protocol."""

    name = "github"

    def __init__(self, git_ops: Any) -> None:
        """Wrap a ``GitOps`` instance.

        Args:
            git_ops: The orchestrator's ``GitOps``, typed loosely so tests can
                pass a mock without importing the concrete class.
        """
        self._git = git_ops

    def _repo(self, ref: PullRequestRef) -> str | None:
        return ref.repo

    async def get_diff(self, ref: PullRequestRef) -> str:
        """Return ``gh pr diff`` output for the PR."""
        return cast(
            str, await self._git.get_pr_diff(".", ref.number, repo=self._repo(ref))
        )

    async def checkout(self, ref: PullRequestRef, dest: str) -> str:
        """Clone the PR head into ``dest``."""
        return cast(str, await self._git.clone_pr_head(ref.to_url(), dest))

    async def comment(self, ref: PullRequestRef, body: str) -> None:
        """Post ``body`` as a PR comment."""
        await self._git.comment_on_pr(".", ref.number, body, repo=self._repo(ref))

    async def merge(self, ref: PullRequestRef) -> None:
        """Squash-merge the PR and delete its branch."""
        await self._git.merge_pr(".", ref.number, repo=self._repo(ref))


class LocalGitBackend:
    """A bare repo on disk. No PR objects, same review and merge semantics."""

    name = "local"

    def __init__(self, repo_url: str) -> None:
        """Bind to a bare repo.

        Args:
            repo_url: A ``file://`` URL or filesystem path to the bare repo.
        """
        self._path = local_repo_path(repo_url)

    async def _run(
        self, cmd: list[str], cwd: str | None = None
    ) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        out, err = await proc.communicate()
        return (
            proc.returncode or 0,
            out.decode(errors="replace"),
            err.decode(errors="replace"),
        )

    async def _run_checked(self, cmd: list[str], cwd: str | None = None) -> str:
        code, out, err = await self._run(cmd, cwd=cwd)
        if code != 0:
            message = f"git command failed (exit {code}): {' '.join(cmd)}\n{err}"
            raise RuntimeError(message)
        return out.strip()

    async def get_diff(self, ref: PullRequestRef) -> str:
        """Diff the branch against its merge base, straight from the bare repo."""
        merge_base = await self._run_checked(
            ["git", "-C", self._path, "merge-base", ref.base, ref.branch]
        )
        return await self._run_checked(
            ["git", "-C", self._path, "diff", f"{merge_base}..{ref.branch}"]
        )

    async def checkout(self, ref: PullRequestRef, dest: str) -> str:
        """Clone the bare repo and check the branch out into ``dest``."""
        await self._run_checked(
            ["git", "clone", "--no-single-branch", self._path, dest]
        )
        await self._run_checked(["git", "checkout", ref.branch], cwd=dest)
        return dest

    async def comment(self, ref: PullRequestRef, body: str) -> None:
        """No PR object exists, so feedback lives only on the task row.

        The orchestrator already persists ``review_feedback`` before calling
        this, so a no-op here loses nothing.  Logged so a bench run's feedback
        is still greppable.
        """
        logger.info("local review feedback on %s: %s", ref.branch, body)

    async def merge(self, ref: PullRequestRef) -> None:
        """Squash-merge in a throwaway clone and push the base back.

        A bare repo cannot merge in place, so this clones, merges, pushes, and
        deletes the source branch, matching ``gh pr merge --squash
        --delete-branch``.
        """
        workdir = tempfile.mkdtemp(prefix="praxis-local-merge-")
        try:
            await self._run_checked(
                ["git", "clone", "--no-single-branch", self._path, workdir]
            )
            await self._run_checked(["git", "checkout", ref.base], cwd=workdir)
            await self._run_checked(
                ["git", "merge", "--squash", f"origin/{ref.branch}"], cwd=workdir
            )
            await self._run_checked(
                [
                    "git",
                    "-c",
                    "user.name=praxis",
                    "-c",
                    "user.email=praxis@localhost",
                    "commit",
                    "-m",
                    f"Merge {ref.branch} into {ref.base} (squash)",
                ],
                cwd=workdir,
            )
            await self._run_checked(["git", "push", "origin", ref.base], cwd=workdir)
            await self._run_checked(
                ["git", "push", "origin", "--delete", ref.branch], cwd=workdir
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


def resolve_backend(repo_url: str, git_ops: Any) -> GitBackend:
    """Return the backend for a project's ``repo_url``.

    GitHub is the default; a filesystem path selects local mode.

    Args:
        repo_url: The project's configured repository URL or path.
        git_ops: The orchestrator's ``GitOps``, used only by the GitHub backend.

    Returns:
        A ``GitBackend`` implementation.
    """
    if is_local_repo_url(repo_url):
        return LocalGitBackend(repo_url)
    return GitHubBackend(git_ops)
