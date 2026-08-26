"""Git hosting seam: GitHub PRs or a local bare repo.

The whole loop above this module (merge gate, verify gates, review flow,
outcome recording) is identical for both backends.  What differs is only the
plumbing that GitHub calls a pull request:

- ``github``: a real PR object, created and merged with the ``gh`` CLI.
- ``local``: the project's ``repo_url`` is a filesystem path to a BARE repo.
  There is no PR object, so a "PR" is just the (branch, base) pair recorded on
  the task; the diff is taken from the bare repo directly (``git -C <bare>
  diff <merge-base>..branch``, equivalent to the three-dot
  ``base...branch``) and the merge is a real ``git merge --squash`` executed
  in a throwaway clone and pushed.

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
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast
from urllib.parse import quote, unquote


logger = logging.getLogger(__name__)

# A local "PR" is encoded as a URL so it can live in the existing
# ``tasks.pr_url`` TEXT column with no schema change and stay greppable.
_LOCAL_PR_RE = re.compile(r"^praxis-local://pr\?branch=([^&]+)&base=([^&]+)$")

_WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")

_GITHUB_PR_RE = re.compile(r"^https://github\.com/([^/]+/[^/]+)/pull/(\d+)/?$")

# What a backend logs when a recorded base sha cannot bound the range and the
# whole pull request is reviewed instead.  Shared so the two backends cannot
# drift into describing the same degradation in two ways, and so the caller
# has one phrase to grep for.  The alternative to falling back is returning an
# empty diff, which reviews as a trivially passing change and would let a
# broken change through the gate: the one outcome this must never have.
_WHOLE_PR_FALLBACK = (
    "base sha %s does not bound %s (%s); reviewing the whole pull request instead"
)


def _clear_readonly_and_retry(
    func: Callable[..., Any], path: str, _exc_info: Any
) -> None:
    """``shutil.rmtree`` error handler for a git clone's read-only files.

    git marks ``.git/objects/pack/*`` read-only, which makes the removal fail
    on Windows.  ``ignore_errors=True`` swallows that and leaks the whole
    clone, one per merge.  Clear the write bit and retry; if it still fails,
    log and let the rest of the tree be removed.

    Args:
        func: The ``os`` callable that failed (``os.unlink``, ``os.rmdir``).
        path: The path it failed on.
        _exc_info: The exception that was raised, unused.
    """
    try:
        os.chmod(path, os.stat(path).st_mode | stat.S_IWRITE)
        func(path)
    except OSError:
        logger.warning("could not remove %s while cleaning up a temp clone", path)


def is_local_repo_url(repo_url: str) -> bool:
    """True when ``repo_url`` names a local bare repo rather than a remote host.

    Recognized: a ``file://`` URL, a POSIX absolute path, a Windows drive
    path, a UNC share and a ``~``-relative path.  Everything else (https, ssh,
    scp-style) is remote.  A relative path is deliberately NOT recognized:
    ``repos/x.git`` is ambiguous with a remote shorthand, and misrouting a real
    remote into local mode is the worse failure.

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
    if value.startswith("\\\\"):
        return True
    if value.startswith("~"):
        return True
    if _WINDOWS_PATH_RE.match(value):
        return True
    return value.startswith("/")


def local_repo_path(repo_url: str) -> str:
    """Return the filesystem path for a local repo URL.

    Args:
        repo_url: A ``file://`` URL or a bare filesystem path.

    Returns:
        The path with any ``file://`` scheme and percent-encoding removed and
        any leading ``~`` expanded.  git is invoked without a shell, so an
        unexpanded ``~`` would be taken literally.
    """
    value = repo_url.strip()
    if value.startswith("file://"):
        value = unquote(value[len("file://") :])
        # file:///c:/x on Windows leaves a leading slash before the drive.
        if _WINDOWS_PATH_RE.match(value.lstrip("/")):
            value = value.lstrip("/")
    if value.startswith("~"):
        value = os.path.expanduser(value)
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

        Raises:
            ValueError: If ``backend`` is neither ``github`` nor ``local``.
                Falling through to the local form would discard ``repo`` and
                ``number`` and write a local ref for a real GitHub PR.  Also if
                a GitHub ref carries no repo, which would otherwise render
                ``https://github.com/None/pull/42`` into ``tasks.pr_url``.  A
                local ref never carries a repo, so this is checked only on the
                GitHub branch.
        """
        if self.backend == "github":
            if self.repo is None:
                message = f"GitHub pull-request ref carries no repo: {self!r}"
                raise ValueError(message)
            return f"https://github.com/{self.repo}/pull/{self.number}"
        if self.backend == "local":
            return (
                "praxis-local://pr"
                f"?branch={quote(self.branch, safe='')}"
                f"&base={quote(self.base, safe='')}"
            )
        message = f"unknown pull-request backend: {self.backend!r}"
        raise ValueError(message)

    @classmethod
    def from_url(cls, url: str) -> PullRequestRef:
        """Parse a stored ``pr_url`` back into a ref.

        Args:
            url: A GitHub PR URL or a ``praxis-local://`` ref.

        Returns:
            The parsed reference.

        Raises:
            ValueError: If the URL is missing, not a string, or neither a
                GitHub PR URL nor a local ref.  ``tasks.pr_url`` is nullable,
                so callers guard with ``except ValueError``.
        """
        if not url or not isinstance(url, str):
            message = f"unrecognized pull-request reference: {url!r}"
            raise ValueError(message)
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

    async def head_sha(self, branch: str) -> str | None:
        """Return the commit at ``branch`` on the remote, or None if absent."""
        ...

    async def get_diff_since(self, ref: PullRequestRef, base_sha: str) -> str:
        """Return the diff the commits after ``base_sha`` produced."""
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

    async def open_integration_pr(
        self, base: str, head: str, title: str, body: str
    ) -> str:
        """Open the change that carries ``head`` onto ``base``; return its ref.

        The last link of the loop, and the one that was not on this seam. Every
        other git operation here already routes through the backend, so the
        integration stage reaching straight for ``GitOps`` meant a local
        project got the whole governed loop except its final step.

        Returns a string storable in ``plans.integration_pr_url`` and parseable
        by :meth:`PullRequestRef.from_url`, so ``praxis merge-plan`` can land it
        through :meth:`merge` on either backend. Raises rather than returning a
        falsy value: the caller records the reason on the plan row, and a
        silent "no reference" is exactly the outcome this seam exists to end.
        """
        ...


class GitHubBackend:
    """The existing ``gh``-CLI behavior, unchanged, behind the protocol."""

    name = "github"

    def __init__(self, git_ops: Any, repo_url: str | None = None) -> None:
        """Wrap a ``GitOps`` instance.

        Args:
            git_ops: The orchestrator's ``GitOps``, typed loosely so tests can
                pass a mock without importing the concrete class.
            repo_url: The project's repository URL. Needed only by
                :meth:`head_sha`, which reads a branch that has no pull request
                yet and so cannot get the repository from a ``PullRequestRef``.
                Optional so the many existing call sites that construct this
                class with a mock keep working; ``head_sha`` reports None
                without it rather than guessing a repository.
        """
        self._git = git_ops
        self._repo_url = repo_url

    def _repo(self, ref: PullRequestRef) -> str:
        """Return the ``owner/name`` slug every ``gh pr`` call must target.

        Args:
            ref: The pull-request reference being acted on.

        Returns:
            The repository slug.

        Raises:
            ValueError: If the ref carries no repo.  ``gh`` would then omit
                ``--repo`` and resolve against the orchestrator's own working
                directory, acting on the wrong repository.

                The check keys on ``repo is None`` alone, deliberately NOT on
                ``ref.backend``: the backend is resolved from the project's
                ``repo_url`` while the ref is parsed from the task's
                ``pr_url``, two independent sources that can disagree (editing
                a project's repo_url while tasks exist is enough).  A ref
                tagged ``local`` reaching this class is exactly that
                disagreement, and it carries no repo, so gating on the tag let
                the wrong-repo call through.
        """
        if ref.repo is None:
            message = (
                "pull-request ref carries no repo; refusing to run gh "
                f"against the orchestrator's own working directory: {ref!r}"
            )
            raise ValueError(message)
        return ref.repo

    async def get_diff(self, ref: PullRequestRef) -> str:
        """Return ``gh pr diff`` output for the PR."""
        return cast(
            str, await self._git.get_pr_diff(".", ref.number, repo=self._repo(ref))
        )

    async def head_sha(self, branch: str) -> str | None:
        """Return the commit at ``refs/heads/<branch>`` on the remote.

        Args:
            branch: Branch name, without the ``refs/heads/`` prefix.

        Returns:
            The commit sha, None if the branch does not exist on the remote,
            and None when this backend was built without a repository URL (an
            older call site or a test double), because a branch head has no
            meaning without one.

        Raises:
            RuntimeError: If the underlying ``git ls-remote`` fails. The caller
                decides what an unreadable remote means; it must not be
                confused with "the branch is not there", which is the ordinary
                first-dispatch case and has a different consequence.
        """
        if not self._repo_url:
            return None
        return cast(str | None, await self._git.remote_head_sha(self._repo_url, branch))

    async def get_diff_since(self, ref: PullRequestRef, base_sha: str) -> str:
        """Return the diff of the commits added to the PR after ``base_sha``.

        Three ``gh`` calls, which is noise beside the clone and the model call
        the review already spends: the pull request's head sha, the merge base
        GitHub computes for ``base_sha...head``, and the diff itself.

        The middle call is the one that matters. GitHub's compare endpoint
        answers even when ``base_sha`` is NOT an ancestor of the head, taking
        the range from an older merge base instead, so nothing fails and the
        range silently widens. Comparing the reported merge base with the sha we
        recorded is what makes that visible.

        Args:
            ref: The pull request being reviewed.
            base_sha: The commit this task's own work starts after.

        Returns:
            The range-bounded diff, or the whole pull-request diff when the
            range could not be established. Never an empty string standing in
            for a failure: an empty diff reviews as a trivially passing change.
        """
        repo = self._repo(ref)
        try:
            head = await self._git.pr_head_sha(ref.number, repo=repo)
            merge_base = await self._git.compare_merge_base(repo, base_sha, head)
            if str(merge_base).strip() == base_sha:
                return cast(str, await self._git.compare_diff(repo, base_sha, head))
            reason = f"github reports the merge base as {str(merge_base).strip()}"
        except Exception as exc:  # noqa: BLE001 - degrade, never review nothing
            reason = f"{type(exc).__name__}: {exc}"
        logger.warning(_WHOLE_PR_FALLBACK, base_sha, ref.to_url(), reason)
        return await self.get_diff(ref)

    async def checkout(self, ref: PullRequestRef, dest: str) -> str:
        """Clone the PR head into ``dest``.

        The URL carries the repo, so there is no ``--repo`` argument to omit,
        but the same guard still applies: a local ref would otherwise render a
        ``praxis-local://`` URL and hand it to ``gh pr checkout``.
        """
        self._repo(ref)
        return cast(str, await self._git.clone_pr_head(ref.to_url(), dest))

    async def comment(self, ref: PullRequestRef, body: str) -> None:
        """Post ``body`` as a PR comment."""
        await self._git.comment_on_pr(".", ref.number, body, repo=self._repo(ref))

    async def merge(self, ref: PullRequestRef) -> None:
        """Squash-merge the PR and delete its branch."""
        await self._git.merge_pr(".", ref.number, repo=self._repo(ref))

    async def pull_request_state(self, ref: PullRequestRef) -> str | None:
        """Return what GitHub says the pull request's state is.

        Deliberately NOT on the ``GitBackend`` protocol, and NOT implemented on
        ``LocalGitBackend``. A bare repo has no pull-request object, so there is
        no state to report and nothing that could have changed it behind
        Praxis's back: the merge gate is reconciled against a HUMAN acting in a
        hosting provider's UI, and a bare repo has no UI. Answering "unknown"
        for local mode would be the same fabrication as answering "open", one
        step quieter, and it would put every local row into a retry-with-backoff
        path built for an unreachable remote. Callers skip local refs by
        ``ref.backend`` instead; the absence of this method is the second,
        independent guard for a backend double or a local project whose task
        somehow carries a GitHub ref.

        Args:
            ref: The pull request to ask about.

        Returns:
            ``"OPEN"``, ``"CLOSED"``, ``"MERGED"``, or None when the question
            could not be answered.

        Raises:
            ValueError: If the ref carries no repo. Same guard as every other
                ``gh`` call on this class: without ``--repo`` the call resolves
                against the orchestrator's own working directory.
        """
        return cast(
            str | None, await self._git.pr_state(ref.number, repo=self._repo(ref))
        )

    async def open_integration_pr(
        self, base: str, head: str, title: str, body: str
    ) -> str:
        """Open the plan's integration PR with ``gh pr create``.

        Unchanged behavior behind the protocol: the same ``GitOps`` call the
        review mixin used to make directly, with the repository this backend is
        bound to rather than one passed in beside it.

        Args:
            base: The branch the plan's work has to reach.
            head: The plan branch the work accumulated on.
            title: PR title.
            body: PR body text.

        Returns:
            The GitHub pull request URL.

        Raises:
            ValueError: If this backend was built without a repository URL.
                ``gh pr create`` would then carry no ``--repo`` and resolve
                against the orchestrator's own working directory, which is the
                same wrong-repository fault :meth:`_repo` refuses.
            RuntimeError: Whatever ``gh`` failed with, unwrapped. The caller
                records it on the plan row instead of swallowing it.
        """
        if not self._repo_url:
            message = (
                "GitHub backend was built without a repository URL; refusing to "
                "run gh pr create against the orchestrator's own working directory"
            )
            raise ValueError(message)
        return cast(
            str,
            await self._git.open_integration_pr(
                repo_url=self._repo_url,
                base=base,
                head=head,
                title=title,
                body=body,
            ),
        )


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
            # git reports "CONFLICT (content): ..." and "nothing to commit" on
            # STDOUT, so a stderr-only message explains nothing at all.
            parts = []
            if out.strip():
                parts.append(f"stdout:\n{out.strip()}")
            if err.strip():
                parts.append(f"stderr:\n{err.strip()}")
            detail = "\n".join(parts) if parts else "(no output)"
            message = f"git command failed (exit {code}): {' '.join(cmd)}\n{detail}"
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

    async def head_sha(self, branch: str) -> str | None:
        """Return the commit at ``refs/heads/<branch>`` in the bare repo.

        Args:
            branch: Branch name, without the ``refs/heads/`` prefix.

        Returns:
            The commit sha, or None if the branch does not exist. ``rev-parse``
            exits non-zero for an unknown ref, which here is a fact and not an
            error: the first task on a fresh branch always lands on it.
        """
        code, out, _err = await self._run(
            ["git", "-C", self._path, "rev-parse", "--verify", f"refs/heads/{branch}"]
        )
        if code != 0:
            return None
        return out.strip() or None

    async def get_diff_since(self, ref: PullRequestRef, base_sha: str) -> str:
        """Return the diff of the commits added to the branch after ``base_sha``.

        ``merge-base --is-ancestor`` decides the range first. Without it a
        two-dot diff against a DIVERGED commit still succeeds, silently
        describing the branch against an unrelated line of history, and against
        a commit the repository has never seen it fails with a message that
        would then have to be interpreted anyway.

        Args:
            ref: The change being reviewed.
            base_sha: The commit this task's own work starts after.

        Returns:
            The range-bounded diff, or the whole branch diff when ``base_sha``
            does not bound the range. Never an empty string standing in for a
            failure: an empty diff reviews as a trivially passing change.
        """
        code, _out, err = await self._run(
            [
                "git",
                "-C",
                self._path,
                "merge-base",
                "--is-ancestor",
                base_sha,
                ref.branch,
            ]
        )
        if code == 0:
            return await self._run_checked(
                ["git", "-C", self._path, "diff", f"{base_sha}..{ref.branch}"]
            )
        reason = err.strip() or f"not an ancestor (git exit {code})"
        logger.warning(_WHOLE_PR_FALLBACK, base_sha, ref.branch, reason)
        return await self.get_diff(ref)

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
            # Identity goes on the CLONE, not just the commit.  `git merge`
            # validates the committer up front and aborts with exit 128
            # ("Committer identity unknown") before it ever reaches the commit
            # that carried the `-c` flags.  A fresh container or CI runner has
            # no global identity, so local-mode merges failed there while
            # passing on any developer machine that happened to have one.
            await self._run_checked(
                ["git", "config", "user.name", "praxis"], cwd=workdir
            )
            await self._run_checked(
                ["git", "config", "user.email", "praxis@localhost"], cwd=workdir
            )
            await self._run_checked(["git", "checkout", ref.base], cwd=workdir)
            await self._run_checked(
                ["git", "merge", "--squash", f"origin/{ref.branch}"], cwd=workdir
            )
            await self._run_checked(
                [
                    "git",
                    "commit",
                    "-m",
                    f"Merge {ref.branch} into {ref.base} (squash)",
                ],
                cwd=workdir,
            )
            await self._run_checked(["git", "push", "origin", ref.base], cwd=workdir)
            # The base push is the merge.  Deleting the source branch is
            # cleanup: raising here would leave the task un-MERGED, and every
            # retry then hits a no-op squash followed by a failing commit.
            code, _out, err = await self._run(
                ["git", "push", "origin", "--delete", ref.branch], cwd=workdir
            )
            if code != 0:
                logger.warning(
                    "merged %s into %s but could not delete the branch (exit %d): %s",
                    ref.branch,
                    ref.base,
                    code,
                    err.strip(),
                )
        finally:
            shutil.rmtree(workdir, onerror=_clear_readonly_and_retry)

    async def open_integration_pr(
        self, base: str, head: str, title: str, body: str
    ) -> str:
        """Return the mergeable reference that carries ``head`` onto ``base``.

        A bare repo has no pull-request object, and this deliberately does not
        invent one. It returns the SAME ``praxis-local://`` reference every
        task on this backend already uses: :meth:`PullRequestRef.from_url`
        parses it and :meth:`merge` squash-merges it, so ``praxis merge-plan``
        lands a local plan through exactly the code that lands a GitHub one.
        What local mode lacks is the review SURFACE, not the merge.

        The head branch is checked first, and that check is the honest part.
        "There is no PR object" is not a licence to hand back a reference to a
        branch this repository does not have: the string would be stored on
        ``plans.integration_pr_url`` and handed to the merge verb days later,
        where it would fail talking about a merge rather than about a branch
        that never existed. ``gh pr create`` refuses the same case outright
        with "Head ref must be a branch".

        Args:
            base: The branch the plan's work has to reach.
            head: The plan branch the work accumulated on.
            title: The title a pull request would have carried.
            body: The body a pull request would have carried.

        Returns:
            A ``praxis-local://`` reference, storable in
            ``plans.integration_pr_url``.

        Raises:
            RuntimeError: If ``head`` is not a branch in this repository.
        """
        if await self.head_sha(head) is None:
            message = (
                f"cannot open an integration reference: {head} is not a branch "
                f"in {self._path}"
            )
            raise RuntimeError(message)
        url = PullRequestRef(backend="local", branch=head, base=base).to_url()
        # Logged for the same reason ``comment`` logs its body: there is no
        # pull-request page carrying this text, so the log is the only place it
        # survives at all.
        logger.info(
            "local integration reference for %s onto %s: %s (%s)",
            head,
            base,
            url,
            title,
        )
        logger.debug("local integration body: %s", body)
        return url


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
    return GitHubBackend(git_ops, repo_url)
