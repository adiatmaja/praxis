"""GitHub credential providers.

Turns configuration plus a repository reference into a usable GitHub token.
Two backends are provided: a legacy static Personal Access Token, and a GitHub
App backend that mints short-lived, repo-scoped installation tokens.
"""

from __future__ import annotations

import re
from typing import Protocol


class CredentialError(RuntimeError):
    """Raised when a GitHub credential cannot be resolved or minted."""


def repo_slug_from_url(repo_url: str) -> str:
    """Extract an ``owner/repo`` slug from a GitHub URL, SSH URL, or bare slug.

    Args:
        repo_url: e.g. ``https://github.com/owner/repo(.git)``,
            ``git@github.com:owner/repo.git``, or ``owner/repo``.

    Returns:
        The ``owner/repo`` slug.

    Raises:
        CredentialError: If no ``owner/repo`` can be extracted.
    """
    text = repo_url.strip()
    if text.endswith(".git"):
        text = text[: -len(".git")]
    match = re.search(r"github\.com[/:]([^/]+/[^/]+?)(?:/|$)", text)
    if match:
        return match.group(1)
    if re.fullmatch(r"[^/\s]+/[^/\s]+", text):
        return text
    msg = f"cannot extract owner/repo from: {repo_url!r}"
    raise CredentialError(msg)


class GitHubCredentialProvider(Protocol):
    """Resolves a GitHub token usable for a given repository."""

    async def token_for_repo(self, repo_url: str) -> str:
        """Return a token authorized for ``repo_url``."""
        ...


class PatCredentialProvider:
    """Returns a single static Personal Access Token for every repo.

    This preserves the legacy behavior: the same broad token is used everywhere.
    """

    def __init__(self, token: str) -> None:
        self._token = token

    async def token_for_repo(self, repo_url: str) -> str:  # noqa: ARG002
        return self._token
