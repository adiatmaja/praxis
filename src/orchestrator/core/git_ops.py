"""Git and GitHub CLI subprocess operations."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx

from orchestrator.core.github_credentials import (
    GitHubCredentialProvider,
    PatCredentialProvider,
)


# Transient GitHub merge-race signatures (case-insensitive).
_TRANSIENT_MERGE_PATTERNS: tuple[str, ...] = (
    "base branch was modified",
    "not mergeable",
    "pull request is not mergeable",
    "try again",
    "please try again",
    "merge already in progress",
    "unexpected error",
    # GitHub's own 504 wording. It says "resubmitting", not "try again", so it
    # matched nothing and raised on the first attempt. Seen on two of three
    # merges during newcomer walkthrough #4. The two 504 forms are ANCHORED to
    # how gh renders the number, because a bare "504" matches arbitrary digits;
    # both forms are needed, since gh's status-line rendering carries neither
    # of the prose phrases below.
    "http 504",
    "status code: 504",
    "gateway timeout",
    "resubmitting your request",
)

# Retry tuning — monkeypatched in tests.
_MERGE_MAX_ATTEMPTS: int = 3
_MERGE_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0)


async def _merge_sleep(seconds: float) -> None:
    """Thin wrapper around asyncio.sleep so tests can monkeypatch it."""
    await asyncio.sleep(seconds)


def _pr_url_from_output(stdout: str, *, action: str, target: str) -> str:
    """Return the pull-request URL ``gh`` printed, or raise.

    ``gh pr create`` exits 0 and prints the new pull request's URL on stdout.
    An exit code of 0 with no URL in the output is not a created PR: it is a
    ``gh`` that did nothing and said nothing. Returning "" from there logged
    "Opened integration PR: " and handed the caller a falsy value, which
    ``on_plan_completed`` reads as "there is nothing to record" and skips
    ``set_plan_integration_pr`` over, leaving ``plans.integration_pr_url``
    NULL. That column is what every read-only surface filters on, so the log
    claimed a PR while ``praxis pending`` and ``merge-plan`` reported nothing
    to approve.

    The emptiness of the OUTPUT is the signal, never the exit status: the same
    rule the harness entrypoints state for their ``gh pr list`` lookup.

    Scans lines rather than taking the whole of stdout because ``gh`` prints
    advisory lines around the URL often enough that a strict whole-output rule
    would turn working runs into failures, which is the opposite defect.

    Args:
        stdout: The command's captured standard output.
        action: The command being reported, for the error message.
        target: Which branches or repository it was run for.

    Returns:
        The first URL on stdout.

    Raises:
        RuntimeError: If no line of ``stdout`` looks like a URL.
    """
    for line in stdout.splitlines():
        candidate = line.strip()
        if candidate.startswith(("https://", "http://")):
            return candidate
    msg = (
        f"{action} exited 0 but printed no pull-request URL for {target}; "
        f"output was {stdout.strip()!r}. No pull request was opened."
    )
    raise RuntimeError(msg)


if TYPE_CHECKING:
    from orchestrator.core.progress_handover import Commit


logger = logging.getLogger(__name__)


# A git credential helper that supplies the token from the GH_TOKEN env var at
# call time. This keeps the token OUT of the persisted remote URL (.git/config)
# and OUT of process argv — it lives only in the child process environment.
_CREDENTIAL_HELPER = (
    "!f() { echo username=x-access-token; echo password=$GH_TOKEN; }; f"
)


def _token_git_args() -> list[str]:
    """Return ``git -c ...`` args that wire up token auth via GH_TOKEN."""
    # The empty assignment first clears any inherited helper, then installs ours.
    return ["-c", "credential.helper=", "-c", f"credential.helper={_CREDENTIAL_HELPER}"]


def clone_with_token(repo_url: str, dest: str, token: str, depth: int = 50) -> None:
    """Clone ``repo_url`` into ``dest`` using token auth without leaking the token.

    The token is passed via the ``GH_TOKEN`` environment variable and consumed by
    a git credential helper, so it is never written to the cloned repo's
    ``.git/config`` and never appears in process arguments or error output.
    """
    env = {**os.environ, "GH_TOKEN": token}
    cmd = [
        "git",
        *_token_git_args(),
        "clone",
        "--depth",
        str(depth),
        repo_url,
        dest,
    ]
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        cmd, check=True, capture_output=True, text=True, env=env
    )
    logger.info("Cloned %s to %s", repo_url, dest)


def checkout_branch(workspace: str, branch: str, token: str) -> None:
    """Fetch and check out ``branch`` in an existing clone at ``workspace``.

    ``clone_with_token`` only brings down the default branch, so the plan
    branch must be fetched explicitly before it can be checked out. The token
    is supplied via ``GH_TOKEN`` for the fetch, consistent with the other
    helpers here.

    A plain ``git fetch origin <branch>`` only advances ``FETCH_HEAD``; it does
    NOT create a local ``<branch>`` ref or a ``refs/remotes/origin/<branch>``
    tracking ref, so a subsequent ``git checkout <branch>`` fails with
    ``pathspec ... did not match`` (exit 1). We therefore check out the freshly
    fetched commit directly via ``FETCH_HEAD``, creating/resetting the local
    branch pointer with ``-B``. Both steps ``check=True`` so a genuine
    fetch/checkout failure raises instead of being silently swallowed.
    """
    env = {**os.environ, "GH_TOKEN": token}
    subprocess.run(  # noqa: S603
        ["git", *_token_git_args(), "-C", workspace, "fetch", "origin", branch],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    subprocess.run(  # noqa: S603
        ["git", "-C", workspace, "checkout", "-B", branch, "FETCH_HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    logger.info("Checked out branch %s in %s", branch, workspace)


def _nothing_staged(workspace: str) -> bool:
    """True only on a POSITIVE answer that the index holds no changes.

    ``git diff --cached --quiet`` exits 0 for an empty index and 1 when it
    holds something, and it says so in exit codes rather than in prose, so it
    cannot be defeated by a locale that translates "nothing to commit". Any
    other exit code means the question was not answered; that falls through to
    attempting the commit, which is the safe direction: a real failure then
    still raises rather than being silently reported as "unchanged".
    """
    probe = subprocess.run(  # noqa: S603
        ["git", "-C", workspace, "diff", "--cached", "--quiet"],
        check=False,
        capture_output=True,
        text=True,
    )
    return probe.returncode == 0


def local_head_sha(workspace: str) -> str:
    """Return the commit ``HEAD`` points at in an existing clone.

    Used by the micro-edit lane to record where its own work starts, read
    AFTER the branch is checked out and BEFORE anything is written. The
    ordering is the whole point: a sha read after the commit already contains
    the change, and the review range bounded by it would be empty, which
    reviews as a trivially passing change.

    Raises:
        subprocess.CalledProcessError: If ``git rev-parse`` fails. A sha that
            could not be read is not a sha, and the caller must not fall back
            to a guess: NULL already means "review the whole pull request".
    """
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", workspace, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def commit_and_push(
    workspace: str,
    token: str,
    message: str,
    paths: list[str] | None = None,
) -> bool:
    """Stage, commit, and push changes in ``workspace`` using token auth.

    Stages ``paths`` (or everything when ``paths`` is None), commits with
    ``message``, and pushes via the token-auth credential helper.

    "Nothing changed" is a FACT, not a failure. ``git commit`` exits 1 on a
    clean tree, so under ``check=True`` it raised ``CalledProcessError``, and
    the two callers that write operator-authored text propagated that as a bare
    500: saving a spec in the dashboard editor without editing it, and
    approving a context draft the planner had produced empty, both answered
    "Internal Server Error" for a no-op. The caller needs to be able to tell
    "I committed" from "there was nothing to commit", so this reports it
    instead of raising.

    Args:
        workspace: A clone with a checked-out branch.
        token: A token with push rights on the remote.
        message: The commit subject.
        paths: Paths to stage, or None to stage everything.

    Returns:
        True when a commit was made and pushed; False when the index was
        already clean, in which case nothing was pushed.

    Raises:
        subprocess.CalledProcessError: If staging, committing, or pushing
            fails for any reason other than there being nothing to commit.
    """
    env = {**os.environ, "GH_TOKEN": token}
    if not stage_and_commit(workspace, message, paths):
        return False
    subprocess.run(  # noqa: S603
        ["git", *_token_git_args(), "-C", workspace, "push"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    logger.info("Committed and pushed in %s", workspace)
    return True


def stage_and_commit(
    workspace: str, message: str, paths: list[str] | None = None
) -> bool:
    """Stage and commit, WITHOUT pushing. Returns False on a clean index.

    Split out of :func:`commit_and_push` for callers that must choose their own
    push, and there is a real difference between them: the bare ``git push``
    above works only on a branch that HAS an upstream. A branch checked out by
    :func:`checkout_branch` (``-B`` from ``FETCH_HEAD``) or created by
    ``GitOps.create_branch`` (``checkout -b``) has none, and the push exits 128.

    Measured live in walkthrough #13: the micro-edit lane checked out a shared
    work branch, committed cleanly, and died on the push with exit 128, having
    already written the commit. Seventeen unit tests passed, every one of them
    with ``commit_and_push`` mocked. Such callers pair this with
    ``GitOps.push_branch``, which pushes ``-u origin <branch>`` explicitly.

    Args:
        workspace: A clone with a checked-out branch.
        message: The commit subject.
        paths: Paths to stage, or None to stage everything.

    Returns:
        True when a commit was made; False when the index was already clean,
        which is a FACT and not a failure.

    Raises:
        subprocess.CalledProcessError: If staging or committing fails for any
            reason other than there being nothing to commit.
    """
    add_args = ["git", "-C", workspace, "add"]
    add_args += paths if paths else ["-A"]
    subprocess.run(add_args, check=True, capture_output=True, text=True)  # noqa: S603
    if _nothing_staged(workspace):
        logger.info("Nothing to commit in %s; skipping commit", workspace)
        return False
    subprocess.run(  # noqa: S603
        ["git", "-C", workspace, "commit", "-m", message],
        check=True,
        capture_output=True,
        text=True,
    )
    return True


def compare_url(repo_url: str, base: str, head: str) -> str:
    """Build a GitHub compare URL for base...head (no network call).

    Args:
        repo_url: Full GitHub repository URL (HTTPS or with .git suffix).
        base: Base branch name.
        head: Head branch name.

    Returns:
        A GitHub compare URL with ``?expand=1`` to pre-fill a PR description.
    """
    slug = repo_url.rstrip("/").removesuffix(".git").split("github.com/")[-1]
    return f"https://github.com/{slug}/compare/{base}...{head}?expand=1"


def flip_checklist_item(markdown: str, item_text: str) -> str:
    """Mark the matching ``- [ ]`` checklist line as done.

    Args:
        markdown: The full markdown text of a plan file.
        item_text: The exact text after ``- [ ] `` to match.

    Returns:
        The markdown with the matching unchecked item replaced by ``- [x]``.
    """
    needle_unchecked = f"- [ ] {item_text}"
    needle_checked = f"- [x] {item_text}"
    return markdown.replace(needle_unchecked, needle_checked)


class GitOps:
    """Git and GitHub CLI operations for branch and PR management."""

    def __init__(self, credentials: GitHubCredentialProvider | str) -> None:
        if isinstance(credentials, str):
            credentials = PatCredentialProvider(credentials)
        self._provider: GitHubCredentialProvider = credentials

    async def _token_for_repo(self, repo_ref: str) -> str:
        """Resolve a token for ``repo_ref`` from the credential provider."""
        return await self._provider.token_for_repo(repo_ref)

    async def _token_for_workspace(self, workspace: str) -> str:
        """Resolve a token for the repo whose origin is ``workspace``."""
        origin = await self._run_checked(
            ["git", "-C", workspace, "remote", "get-url", "origin"]
        )
        return await self._provider.token_for_repo(origin)

    async def _run_command(
        self,
        cmd: list[str],
        cwd: str | None = None,
        token: str | None = None,
    ) -> tuple[int, str, str]:
        env = os.environ.copy()
        if token is not None:
            env["GH_TOKEN"] = token
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate()
        return (
            proc.returncode or 0,
            stdout.decode().strip(),
            stderr.decode().strip(),
        )

    async def _run_checked(
        self, cmd: list[str], cwd: str | None = None, token: str | None = None
    ) -> str:
        code, stdout, stderr = await self._run_command(cmd, cwd=cwd, token=token)
        if code != 0:
            message = f"Git command failed (exit {code}): {' '.join(cmd)}\n{stderr}"
            raise RuntimeError(message)
        return stdout

    async def clone_repo(self, repo_url: str, workspace: str) -> None:
        token = await self._token_for_repo(repo_url)
        await self._run_checked(["git", "clone", repo_url, workspace], token=token)
        logger.info("Cloned %s to %s", repo_url, workspace)

    async def create_branch(
        self,
        workspace: str,
        branch: str,
        base: str = "main",
    ) -> None:
        await self._run_checked(["git", "checkout", base], cwd=workspace)
        token = await self._token_for_workspace(workspace)
        await self._run_checked(
            ["git", "pull", "origin", base], cwd=workspace, token=token
        )
        await self._run_checked(["git", "checkout", "-b", branch], cwd=workspace)
        logger.info("Created branch %s from %s", branch, base)

    async def push_branch(self, workspace: str, branch: str) -> None:
        token = await self._token_for_workspace(workspace)
        await self._run_checked(
            ["git", "push", "-u", "origin", branch], cwd=workspace, token=token
        )
        logger.info("Pushed branch %s", branch)

    async def create_pr(
        self,
        workspace: str,
        title: str,
        body: str,
        base: str,
        head: str,
    ) -> str:
        token = await self._token_for_workspace(workspace)
        stdout = await self._run_checked(
            [
                "gh",
                "pr",
                "create",
                "--title",
                title,
                "--body",
                body,
                "--base",
                base,
                "--head",
                head,
            ],
            cwd=workspace,
            token=token,
        )
        pr_url = _pr_url_from_output(
            stdout, action="gh pr create", target=f"head={head}, base={base}"
        )
        logger.info("Created PR: %s", pr_url)
        return pr_url

    async def _pr_is_merged(
        self, workspace: str, pr_number: int, repo: str | None, token: str | None
    ) -> bool:
        """Ask GitHub whether a PR is already merged.

        ``gh pr merge`` can time out AFTER GitHub has performed the merge, so a
        non-zero exit is not evidence the merge did not happen. Any failure to
        answer returns False, which keeps the caller failing closed.

        Args:
            workspace: Working directory to run ``gh`` from.
            pr_number: Pull request number to inspect.
            repo: ``owner/name`` slug, or None to infer from ``workspace``.
            token: GitHub token passed to the subprocess environment.

        Returns:
            True only when GitHub answers that the PR state is MERGED.
        """
        cmd = [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--json",
            "state",
            *(["--repo", repo] if repo else []),
        ]
        try:
            code, stdout, _ = await self._run_command(cmd, cwd=workspace, token=token)
        except OSError as spawn_error:
            # gh missing from PATH, workspace already cleaned up, spawn refused.
            # Not being able to ask is not evidence of a merge.
            logger.warning(
                "Could not ask GitHub whether PR #%d is merged: %s",
                pr_number,
                spawn_error,
            )
            return False
        if code != 0:
            return False
        try:
            return str(json.loads(stdout).get("state", "")).upper() == "MERGED"
        except (ValueError, AttributeError):
            return False

    async def merge_pr(
        self, workspace: str, pr_number: int, repo: str | None = None
    ) -> None:
        token = (
            await self._token_for_repo(repo)
            if repo
            else await self._token_for_workspace(workspace)
        )
        cmd = [
            "gh",
            "pr",
            "merge",
            str(pr_number),
            "--squash",
            "--delete-branch",
            *(["--repo", repo] if repo else []),
        ]
        last_exc: RuntimeError | None = None
        for attempt in range(_MERGE_MAX_ATTEMPTS):
            code, stdout, stderr = await self._run_command(
                cmd, cwd=workspace, token=token
            )
            if code == 0:
                logger.info("Merged PR #%d", pr_number)
                return
            message = f"Git command failed (exit {code}): {' '.join(cmd)}\n{stderr}"
            exc = RuntimeError(message)
            # gh can fail AFTER GitHub merged (a 504 on the response, not the
            # merge). GitHub's own answer outranks gh's exit code.
            if await self._pr_is_merged(workspace, pr_number, repo, token):
                logger.info(
                    "PR #%d reported a merge error but GitHub says it is merged; "
                    "treating as success: %s",
                    pr_number,
                    stderr.strip(),
                )
                return
            stderr_lower = stderr.lower()
            if not any(pat in stderr_lower for pat in _TRANSIENT_MERGE_PATTERNS):
                raise exc
            last_exc = exc
            if attempt < _MERGE_MAX_ATTEMPTS - 1:
                backoff = _MERGE_BACKOFF_SECONDS[
                    min(attempt, len(_MERGE_BACKOFF_SECONDS) - 1)
                ]
                logger.warning(
                    "Transient merge error on PR #%d (attempt %d/%d), "
                    "retrying in %.1fs: %s",
                    pr_number,
                    attempt + 1,
                    _MERGE_MAX_ATTEMPTS,
                    backoff,
                    stderr,
                )
                await _merge_sleep(backoff)
        if last_exc is not None:
            if await self._pr_is_merged(workspace, pr_number, repo, token):
                logger.info(
                    "PR #%d exhausted merge retries but GitHub says it is merged",
                    pr_number,
                )
                return
            raise last_exc
        err_msg = f"Git command failed: {' '.join(cmd)}\nExhausted {_MERGE_MAX_ATTEMPTS} attempts"
        raise RuntimeError(err_msg)

    async def comment_on_pr(
        self,
        workspace: str,
        pr_number: int,
        comment: str,
        repo: str | None = None,
    ) -> None:
        token = (
            await self._token_for_repo(repo)
            if repo
            else await self._token_for_workspace(workspace)
        )
        await self._run_checked(
            [
                "gh",
                "pr",
                "comment",
                str(pr_number),
                "--body",
                comment,
                *(["--repo", repo] if repo else []),
            ],
            cwd=workspace,
            token=token,
        )
        logger.info("Commented on PR #%d", pr_number)

    async def get_pr_diff(
        self, workspace: str, pr_number: int, repo: str | None = None
    ) -> str:
        token = (
            await self._token_for_repo(repo)
            if repo
            else await self._token_for_workspace(workspace)
        )
        return await self._run_checked(
            ["gh", "pr", "diff", str(pr_number), *(["--repo", repo] if repo else [])],
            cwd=workspace,
            token=token,
        )

    async def pr_head_sha(self, pr_number: int, repo: str) -> str:
        """Return the commit at the head of a pull request.

        The head SHA rather than the head branch name: a branch can move
        between this call and the compare that follows it, and it can be
        deleted outright once the pull request is merged. The SHA cannot.

        Args:
            pr_number: The pull request number.
            repo: The ``owner/name`` slug. Required, like every other ``gh pr``
                call here: without ``--repo`` gh resolves against the
                orchestrator's own working directory.

        Returns:
            The head commit sha.

        Raises:
            RuntimeError: If the ``gh`` call fails.
        """
        token = await self._token_for_repo(repo)
        return await self._run_checked(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--repo",
                repo,
                "--json",
                "headRefOid",
                "--jq",
                ".headRefOid",
            ],
            token=token,
        )

    async def compare_merge_base(self, repo: str, base: str, head: str) -> str:
        """Return the merge base GitHub computed for ``base...head``.

        This is how ancestry is decided without a clone. When ``base`` is an
        ancestor of ``head`` the merge base IS ``base``; when it is not (a force
        push, a recreated branch), GitHub still answers, from an older commit,
        so the comparison silently widens instead of failing. Comparing the two
        is what makes that visible.

        Args:
            repo: The ``owner/name`` slug.
            base: The commit the range starts after.
            head: The commit the range ends at.

        Returns:
            The merge base commit sha.

        Raises:
            RuntimeError: If the ``gh`` call fails.
        """
        token = await self._token_for_repo(repo)
        return await self._run_checked(
            [
                "gh",
                "api",
                f"repos/{repo}/compare/{base}...{head}",
                "--jq",
                ".merge_base_commit.sha",
            ],
            token=token,
        )

    async def compare_diff(self, repo: str, base: str, head: str) -> str:
        """Return the unified diff between two commits, read from the remote.

        Args:
            repo: The ``owner/name`` slug.
            base: The commit the range starts after.
            head: The commit the range ends at.

        Returns:
            The unified diff, in the same format ``gh pr diff`` produces, so a
            reviewer cannot tell which of the two produced it.

        Raises:
            RuntimeError: If the ``gh`` call fails.
        """
        token = await self._token_for_repo(repo)
        return await self._run_checked(
            [
                "gh",
                "api",
                f"repos/{repo}/compare/{base}...{head}",
                "-H",
                "Accept: application/vnd.github.v3.diff",
            ],
            token=token,
        )

    @staticmethod
    def repo_slug(repo_url: str) -> str | None:
        """Extract an ``owner/name`` slug from a GitHub repo or PR URL."""
        url_str = repo_url.strip()
        if url_str.startswith("git@github.com:"):
            url_str = "ssh://" + url_str.replace("git@github.com:", "git@github.com/")
        elif "://" not in url_str:
            url_str = "https://" + url_str

        try:
            parsed = urlparse(url_str)
        except Exception:
            return None

        if parsed.hostname != "github.com":
            return None

        path = parsed.path.lstrip("/")
        parts = path.split("/")
        if len(parts) < 2:
            return None
        owner = parts[0]
        repo = parts[1]
        if repo.endswith(".git"):
            repo = repo[:-4]
        return f"{owner}/{repo}"

    async def get_changed_files(
        self,
        workspace: str,
        base: str,
        head: str,
    ) -> list[str]:
        stdout = await self._run_checked(
            ["git", "diff", "--name-only", f"{base}...{head}"],
            cwd=workspace,
        )
        return [line for line in stdout.splitlines() if line]

    async def extract_pr_number(self, pr_url: str) -> int:
        return int(pr_url.rstrip("/").split("/")[-1])

    async def clone_pr_head(self, pr_url: str, dest: str) -> str:
        """Clone the repo and check out the PR's head ref into ``dest``.

        Gives the reviewer brain a real git checkout to reason against, instead
        of the orchestrator's own /app cwd. Returns ``dest`` on success.

        Args:
            pr_url: Full GitHub PR URL (e.g. https://github.com/owner/repo/pull/42).
            dest: Directory path to clone into (must be empty if it exists).

        Returns:
            ``dest`` on success.

        Raises:
            RuntimeError: If the clone or checkout fails.
        """
        repo = self.repo_slug(pr_url)
        if repo is None:
            msg = f"cannot extract repo slug from PR URL: {pr_url}"
            raise RuntimeError(msg)
        token = await self._token_for_repo(repo)
        pr_number = await self.extract_pr_number(pr_url)
        clone_url = f"https://github.com/{repo}.git"
        cmd_clone = [
            "git",
            *_token_git_args(),
            "clone",
            "--depth",
            "1",
            "--no-single-branch",
            clone_url,
            dest,
        ]
        code, _, stderr = await self._run_command(cmd_clone, token=token)
        if code != 0:
            msg = f"clone failed (exit {code}) for {clone_url}: {stderr}"
            raise RuntimeError(msg)
        await self._run_checked(
            ["gh", "pr", "checkout", str(pr_number), "--repo", repo],
            cwd=dest,
            token=token,
        )
        logger.info("Cloned PR #%d head into %s", pr_number, dest)
        return dest

    async def remote_branch_exists(self, repo_url: str, branch: str) -> bool:
        """Check whether ``branch`` exists on the remote at ``repo_url``.

        Runs ``git ls-remote --heads <repo_url> <branch>`` via the token-auth
        credential helper and returns True if the output contains a matching
        ``refs/heads/<branch>`` line.

        Args:
            repo_url: HTTPS or SSH URL of the remote repository.
            branch: Branch name to check (without ``refs/heads/`` prefix).

        Returns:
            True if the branch exists on the remote, False if absent.

        Raises:
            RuntimeError: If the git command exits with a non-zero code.
        """
        token = await self._token_for_repo(repo_url)
        cmd = [
            "git",
            *_token_git_args(),
            "ls-remote",
            "--heads",
            repo_url,
            branch,
        ]
        code, stdout, stderr = await self._run_command(cmd, token=token)
        if code != 0:
            msg = f"git ls-remote failed (exit {code}): {stderr}"
            raise RuntimeError(msg)
        ref = f"refs/heads/{branch}"
        return any(ref == line.split("\t")[-1] for line in stdout.splitlines() if line)

    async def list_remote_branches(self, repo_url: str) -> list[str]:
        """Return a list of branch names present on the remote at ``repo_url``.

        Runs ``git ls-remote --heads <repo_url>`` via the token-auth
        credential helper.

        Args:
            repo_url: HTTPS or SSH URL of the remote repository.

        Returns:
            List of branch names (without ``refs/heads/`` prefix).

        Raises:
            RuntimeError: If the git command exits with a non-zero code.
        """
        token = await self._token_for_repo(repo_url)
        cmd = [
            "git",
            *_token_git_args(),
            "ls-remote",
            "--heads",
            repo_url,
        ]
        code, stdout, stderr = await self._run_command(cmd, token=token)
        if code != 0:
            msg = f"git ls-remote failed (exit {code}): {stderr}"
            raise RuntimeError(msg)
        branches: list[str] = []
        for line in stdout.splitlines():
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) == 2 and parts[1].startswith("refs/heads/"):
                branches.append(parts[1].removeprefix("refs/heads/"))
        return branches

    async def delete_remote_branch(self, repo_url: str, branch: str) -> None:
        """Delete ``branch`` on the remote repository at ``repo_url``.

        Runs ``git push <repo_url> --delete <branch>`` via the token-auth
        credential helper.

        Args:
            repo_url: HTTPS or SSH URL of the remote repository.
            branch: Branch name to delete.

        Raises:
            RuntimeError: If the git push command exits with a non-zero code.
        """
        token = await self._token_for_repo(repo_url)
        cmd = [
            "git",
            *_token_git_args(),
            "push",
            repo_url,
            "--delete",
            branch,
        ]
        await self._run_checked(cmd, token=token)
        logger.info("Deleted remote branch %s on %s", branch, repo_url)

    async def remote_head_sha(self, repo_url: str, branch: str) -> str | None:
        """Return the commit sha at ``refs/heads/<branch>`` on the remote.

        Runs ``git ls-remote --heads <repo_url> <branch>`` (read-only, no
        clone) via the token-auth credential helper.

        Args:
            repo_url: HTTPS or SSH URL of the remote repository.
            branch: Branch name (without the ``refs/heads/`` prefix).

        Returns:
            The commit sha, or None if the branch is absent.

        Raises:
            RuntimeError: If the git command exits non-zero.
        """
        token = await self._token_for_repo(repo_url)
        cmd = [
            "git",
            *_token_git_args(),
            "ls-remote",
            "--heads",
            repo_url,
            branch,
        ]
        code, stdout, stderr = await self._run_command(cmd, token=token)
        if code != 0:
            msg = f"git ls-remote failed (exit {code}): {stderr}"
            raise RuntimeError(msg)
        ref = f"refs/heads/{branch}"
        for line in stdout.splitlines():
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) == 2 and parts[1] == ref:
                return parts[0]
        return None

    async def branch_commit_log(
        self, cwd: str, base_branch: str, branch: str
    ) -> list[Commit]:
        """Return commits on ``branch`` not on ``base_branch``, oldest first.

        Commit subjects are the spine of the progress handover, so we read them
        verbatim. Returns an empty list when the branch has no extra commits.

        Args:
            cwd: Working directory of the git repository.
            base_branch: The branch to exclude commits from (e.g. ``"main"``).
            branch: The branch whose extra commits to return.

        Returns:
            List of :class:`~orchestrator.core.progress_handover.Commit` objects
            ordered from oldest to newest.
        """
        from orchestrator.core.progress_handover import Commit

        out = await self._run_checked(
            [
                "git",
                "log",
                "--reverse",
                "--format=%H%x1f%s",
                f"{base_branch}..{branch}",
            ],
            cwd=cwd,
        )
        commits: list[Commit] = []
        for line in out.splitlines():
            if "\x1f" not in line:
                continue
            sha, subject = line.split("\x1f", 1)
            commits.append(Commit(sha=sha, subject=subject))
        return commits

    async def remote_branch_commit_log(
        self, repo_url: str, base_branch: str, branch: str
    ) -> list[Commit]:
        """Commits on ``branch`` and not on ``base_branch``, read from the REMOTE.

        The sibling ``branch_commit_log`` needs a local clone, and the only
        caller that wanted this data had none: ``_build_worker_bible`` passed
        ``"."``, which inside the orchestrator container is ``/app`` (no
        ``.git``, and no target repo anywhere on the filesystem). The read
        therefore raised on every dispatch and was swallowed into an empty
        list, so the progress handover rendered every checklist item unticked
        forever while the Bible told the worker that per-item commits were how
        progress survived a restart. Under bare uvicorn it was worse than
        empty: ``.`` is the Praxis repo, so the refspec resolved against
        Praxis's own branches.

        Uses ``gh api ... /compare/`` for the same reason
        ``open_integration_pr`` uses ``gh pr create --repo``: no workspace, no
        clone, one call.

        Args:
            repo_url: Full GitHub repository URL.
            base_branch: The branch to exclude commits from.
            branch: The branch whose extra commits to return.

        Returns:
            Commits ordered oldest first, as ``branch_commit_log`` returns.

        Raises:
            RuntimeError: If the ``gh`` call fails. Callers decide what an
                unreadable history means; it must never be reported as "no
                progress", which is a different fact.
        """
        from orchestrator.core.progress_handover import Commit

        slug = (
            self.repo_slug(repo_url)
            or repo_url.rstrip("/").removesuffix(".git").split("github.com/")[-1]
        )
        token = await self._token_for_repo(slug)
        out = await self._run_checked(
            [
                "gh",
                "api",
                f"repos/{slug}/compare/{base_branch}...{branch}",
                "--jq",
                # Same %H\x1f%s shape branch_commit_log parses, so the two
                # sources cannot drift in what the caller has to handle.
                r'.commits[] | .sha + "\u001f" + '
                r'(.commit.message | split("\n")[0])',
            ],
            token=token,
        )
        commits: list[Commit] = []
        for line in out.splitlines():
            if "\x1f" not in line:
                continue
            sha, subject = line.split("\x1f", 1)
            commits.append(Commit(sha=sha, subject=subject))
        return commits

    async def open_integration_pr(
        self,
        repo_url: str,
        base: str,
        head: str,
        title: str,
        body: str,
    ) -> str:
        """Open a plan-branch to default-branch PR without a local clone.

        Uses ``gh pr create --repo <slug>`` so no workspace is needed. Returns
        the PR URL. Raises on failure; callers wrap this best-effort.

        Args:
            repo_url: Full GitHub repository URL (HTTPS or with .git suffix).
            base: Base branch name (merge target, e.g. ``"main"``).
            head: Head branch name (plan branch to merge from).
            title: PR title.
            body: PR body text.

        Returns:
            The GitHub pull request URL.

        Raises:
            RuntimeError: If the ``gh`` command exits with a non-zero code, or
                exits 0 without printing a pull-request URL. The second case is
                the load-bearing one: the caller treats a falsy return as
                "nothing to record" and moves on silently.
        """
        slug = (
            self.repo_slug(repo_url)
            or repo_url.rstrip("/").removesuffix(".git").split("github.com/")[-1]
        )
        token = await self._token_for_repo(slug)
        stdout = await self._run_checked(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                slug,
                "--base",
                base,
                "--head",
                head,
                "--title",
                title,
                "--body",
                body,
            ],
            token=token,
        )
        pr_url = _pr_url_from_output(
            stdout,
            action="gh pr create",
            target=f"{slug} head={head}, base={base}",
        )
        logger.info("Opened integration PR: %s", pr_url)
        return pr_url

    async def remote_file_exists(self, repo_slug: str, branch: str, path: str) -> bool:
        """Check whether ``path`` exists on ``branch`` in a GitHub repo.

        Uses the GitHub Contents API
        ``GET /repos/{slug}/contents/{path}?ref={branch}``.
        Returns True on HTTP 200, False on 404, and raises RuntimeError for
        any other status or network failure.

        Args:
            repo_slug: GitHub ``owner/repo`` slug.
            branch: Branch ref to check the file on.
            path: Repository-relative file path (no leading slash).

        Returns:
            True if the file exists, False if it does not.

        Raises:
            RuntimeError: On unexpected HTTP status or network error.
        """
        if not re.match(r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$", repo_slug):
            msg = f"Invalid repository slug format: {repo_slug}"
            raise ValueError(msg)
        path_parts = path.replace("\\", "/").split("/")
        if (
            "." in path_parts
            or ".." in path_parts
            or not re.match(r"^[a-zA-Z0-9_.-]+(/[a-zA-Z0-9_.-]+)*$", path)
        ):
            msg = f"Invalid repository-relative path format: {path}"
            raise ValueError(msg)
        if not re.match(r"^[a-zA-Z0-9_.-]+(/[a-zA-Z0-9_.-]+)*$", branch):
            msg = f"Invalid branch format: {branch}"
            raise ValueError(msg)
        token = await self._token_for_repo(repo_slug)
        url = f"https://api.github.com/repos/{repo_slug}/contents/{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url, headers=headers, params={"ref": branch})
        except httpx.HTTPError as exc:
            msg = f"network error checking file on GitHub: {exc}"
            raise RuntimeError(msg) from exc
        if resp.status_code == 200:
            return True
        if resp.status_code == 404:
            return False
        msg = (
            f"unexpected GitHub API status {resp.status_code}"
            f" for {repo_slug}/{path}@{branch}"
        )
        raise RuntimeError(msg)

    async def remote_commit_meta(self, repo_slug: str, sha: str) -> dict[str, str]:
        """Return ``{subject, committed_at}`` for a commit via the GitHub API.

        Uses ``GET /repos/{slug}/commits/{sha}``. The subject is the first line
        of the commit message.

        Args:
            repo_slug: GitHub ``owner/repo`` slug.
            sha: Commit sha to look up.

        Returns:
            Dict with ``subject`` and ``committed_at`` (ISO-8601 string).

        Raises:
            RuntimeError: On unexpected HTTP status or network error.
        """
        if not re.match(r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$", repo_slug):
            msg = f"Invalid repository slug format: {repo_slug}"
            raise ValueError(msg)
        if not re.match(r"^[a-fA-F0-9]+$", sha):
            msg = f"Invalid commit sha format: {sha}"
            raise ValueError(msg)
        token = await self._token_for_repo(repo_slug)
        url = f"https://api.github.com/repos/{repo_slug}/commits/{sha}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            msg = f"network error fetching commit meta: {exc}"
            raise RuntimeError(msg) from exc
        if resp.status_code != 200:
            msg = (
                f"unexpected GitHub API status {resp.status_code} for {repo_slug}@{sha}"
            )
            raise RuntimeError(msg)
        commit = resp.json().get("commit", {})
        message = commit.get("message", "")
        subject = message.splitlines()[0] if message else ""
        committed_at = commit.get("committer", {}).get("date", "")
        return {"subject": subject, "committed_at": committed_at}
