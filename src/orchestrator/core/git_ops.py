"""Git and GitHub CLI subprocess operations."""

from __future__ import annotations

import asyncio
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


def commit_and_push(
    workspace: str,
    token: str,
    message: str,
    paths: list[str] | None = None,
) -> None:
    """Stage, commit, and push changes in ``workspace`` using token auth.

    Stages ``paths`` (or everything when ``paths`` is None), commits with
    ``message``, and pushes via the token-auth credential helper.
    """
    env = {**os.environ, "GH_TOKEN": token}
    add_args = ["git", "-C", workspace, "add"]
    add_args += paths if paths else ["-A"]
    subprocess.run(add_args, check=True, capture_output=True, text=True)  # noqa: S603
    subprocess.run(  # noqa: S603
        ["git", "-C", workspace, "commit", "-m", message],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(  # noqa: S603
        ["git", *_token_git_args(), "-C", workspace, "push"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    logger.info("Committed and pushed in %s", workspace)


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
        logger.info("Created PR: %s", stdout)
        return stdout.strip()

    async def merge_pr(
        self, workspace: str, pr_number: int, repo: str | None = None
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
                "merge",
                str(pr_number),
                "--squash",
                "--delete-branch",
                *(["--repo", repo] if repo else []),
            ],
            cwd=workspace,
            token=token,
        )
        logger.info("Merged PR #%d", pr_number)

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
            RuntimeError: If the ``gh`` command exits with a non-zero code.
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
        logger.info("Opened integration PR: %s", stdout.strip())
        return stdout.strip()

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
