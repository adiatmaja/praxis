"""GitHub credential providers.

Turns configuration plus a repository reference into a usable GitHub token.
Two backends are provided: a legacy static Personal Access Token, and a GitHub
App backend that mints short-lived, repo-scoped installation tokens.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import httpx
import jwt


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


logger = logging.getLogger(__name__)

_API_BASE = "https://api.github.com"
# Refresh a cached token when fewer than this many seconds of life remain.
_REFRESH_MARGIN_S = 300
_ACCEPT = "application/vnd.github+json"


class GitHubAppCredentialProvider:
    """Mints short-lived, repo-scoped GitHub App installation tokens.

    The App private key never leaves this process. Per repo we sign a short JWT,
    resolve the installation id (cached), and mint a token scoped to that single
    repository with ``contents:write`` + ``pull_requests:write``. Minted tokens
    are cached per repo and refreshed shortly before they expire.
    """

    def __init__(
        self,
        app_id: str,
        private_key_pem: str,
        installation_id: int | None = None,
        *,
        clock: Callable[[], float] = time.time,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._app_id = app_id
        self._private_key_pem = private_key_pem
        self._fixed_installation_id = installation_id
        self._clock = clock
        self._transport = transport
        self._install_ids: dict[str, int] = {}
        # slug -> (token, expiry_epoch_seconds)
        self._token_cache: dict[str, tuple[str, float]] = {}

    def _app_jwt(self) -> str:
        now = int(self._clock())
        payload = {"iat": now - 60, "exp": now + 540, "iss": self._app_id}
        return jwt.encode(payload, self._private_key_pem, algorithm="RS256")

    def _client(self, token: str) -> httpx.AsyncClient:
        headers = {"Authorization": f"Bearer {token}", "Accept": _ACCEPT}
        return httpx.AsyncClient(
            base_url=_API_BASE,
            headers=headers,
            timeout=15,
            transport=self._transport,
        )

    async def _installation_id_for(self, slug: str) -> int:
        if self._fixed_installation_id is not None:
            return self._fixed_installation_id
        if slug in self._install_ids:
            return self._install_ids[slug]
        async with self._client(self._app_jwt()) as client:
            resp = await client.get(f"/repos/{slug}/installation")
        if resp.status_code != 200:
            msg = (
                f"cannot resolve GitHub App installation for {slug} "
                f"(status {resp.status_code}); is the App installed on that repo?"
            )
            raise CredentialError(msg)
        install_id = int(resp.json()["id"])
        self._install_ids[slug] = install_id
        return install_id

    @staticmethod
    def _parse_expiry(expires_at: str) -> float:
        # GitHub returns UTC (trailing ``Z``); parse as UTC so the epoch aligns
        # with ``self._clock()`` (real UTC epoch) regardless of the host's local
        # timezone. ``time.mktime`` would (wrongly) treat the struct as local.
        return (
            datetime.strptime(expires_at, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=UTC)
            .timestamp()
        )

    async def token_for_repo(self, repo_url: str) -> str:
        """Return a short-lived installation token scoped to ``repo_url``.

        Args:
            repo_url: GitHub HTTPS URL, SSH URL, or ``owner/repo`` slug.

        Returns:
            A ``ghs_*`` installation token valid for up to one hour.

        Raises:
            CredentialError: If the installation cannot be resolved or minting fails.
        """
        slug = repo_slug_from_url(repo_url)
        cached = self._token_cache.get(slug)
        if cached is not None:
            token, expiry = cached
            if expiry - self._clock() > _REFRESH_MARGIN_S:
                return token
        install_id = await self._installation_id_for(slug)
        repo_name = slug.split("/", 1)[1]
        body = {
            "repositories": [repo_name],
            "permissions": {"contents": "write", "pull_requests": "write"},
        }
        async with self._client(self._app_jwt()) as client:
            resp = await client.post(
                f"/app/installations/{install_id}/access_tokens", json=body
            )
        if resp.status_code not in (200, 201):
            msg = (
                f"failed to mint installation token for {slug} "
                f"(status {resp.status_code}): {resp.text}"
            )
            raise CredentialError(msg)
        data = resp.json()
        token = str(data["token"])
        expiry = self._parse_expiry(data["expires_at"])
        self._token_cache[slug] = (token, expiry)
        logger.info(
            "Minted installation token for %s (expires %s)", slug, data["expires_at"]
        )
        return token


def _load_private_key(value: str) -> str:
    """Return PEM contents, reading from a file path when ``value`` is one.

    A value containing a PEM header is used verbatim; otherwise, if it points at
    an existing file, that file is read.
    """
    if "BEGIN" in value:
        return value
    path = Path(value)
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return value


def build_credential_provider(settings: object) -> GitHubCredentialProvider:
    """Choose a credential backend from settings.

    Precedence: GitHub App (when ``github_app_id`` and ``github_app_private_key``
    are both set) beats the legacy PAT. Raises when neither is configured.

    Args:
        settings: Any object exposing ``github_token``, ``github_app_id``,
            ``github_app_private_key``, and ``github_app_installation_id``
            attributes (missing attributes are treated as ``None``).

    Returns:
        A configured :class:`GitHubCredentialProvider`.

    Raises:
        CredentialError: If neither App nor PAT credentials are present.
    """
    app_id = getattr(settings, "github_app_id", None)
    private_key = getattr(settings, "github_app_private_key", None)
    if app_id and private_key:
        install_id = getattr(settings, "github_app_installation_id", None)
        return GitHubAppCredentialProvider(
            app_id=str(app_id),
            private_key_pem=_load_private_key(str(private_key)),
            installation_id=install_id,
        )
    token = getattr(settings, "github_token", None)
    if token:
        return PatCredentialProvider(str(token))
    msg = (
        "no GitHub credentials configured: set GITHUB_TOKEN, or "
        "GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY"
    )
    raise CredentialError(msg)
