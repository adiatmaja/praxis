"""Shared read-only remote preflight for GitHub-backed dispatch paths.

Praxis orchestrates github.com repositories only. Every worker container clones
the repo from its GitHub remote, so a dispatch against an unreachable repo, a
missing or expired credential, a non-github.com URL, or a missing base branch is
doomed. This module validates all of that with read-only remote calls BEFORE any
container is spawned, and raises a typed :class:`PreflightError` that the API
layer maps to an HTTP status. It stays FastAPI-free so it can be unit-tested in
isolation.
"""

from __future__ import annotations

from enum import Enum


# Token values that mean "no real GitHub credential is configured".
_PLACEHOLDER_TOKENS: frozenset[str] = frozenset({"placeholder", ""})


class PreflightKind(Enum):
    """Category of a preflight failure, used to pick an HTTP status + message."""

    NOT_GITHUB = "not_github"
    AUTH = "auth"
    NETWORK = "network"
    MISSING_BRANCH = "missing_branch"
    MISSING_FILE = "missing_file"
    BASE_SHA_MISMATCH = "base_sha_mismatch"


class PreflightError(Exception):
    """A preflight check failed. Carries a machine-readable ``kind``."""

    def __init__(self, kind: PreflightKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


# kind -> HTTP status. 422 for actionable config/credential problems that will
# not self-heal on retry; 502 for transient upstream failures; 409 for the
# base-sha race (unchanged from git-state-awareness).
_STATUS_FOR_KIND: dict[PreflightKind, int] = {
    PreflightKind.NOT_GITHUB: 422,
    PreflightKind.MISSING_BRANCH: 422,
    PreflightKind.MISSING_FILE: 422,
    PreflightKind.AUTH: 422,
    PreflightKind.NETWORK: 502,
    PreflightKind.BASE_SHA_MISMATCH: 409,
}


def status_and_detail(exc: PreflightError) -> tuple[int, str]:
    """Return ``(http_status, detail)`` for a :class:`PreflightError`."""
    return _STATUS_FOR_KIND[exc.kind], str(exc)


# Substrings that indicate an auth/authorization problem in git ls-remote stderr.
# A private repo without access reads back as "Repository not found", so it is an
# AUTH signal here, not a MISSING_BRANCH one.
_AUTH_MARKERS: tuple[str, ...] = (
    "repository not found",
    "could not read username",
    "authentication failed",
    "permission",
    "403",
    "access denied",
    "invalid username or password",
)


def classify_ls_remote_stderr(msg: str) -> PreflightKind:
    """Classify git/ls-remote stderr as :attr:`AUTH` or :attr:`NETWORK`.

    Unknown transport failures default to NETWORK, so we treat them as retryable
    rather than falsely asserting a credential problem.
    """
    lowered = msg.lower()
    if any(marker in lowered for marker in _AUTH_MARKERS):
        return PreflightKind.AUTH
    return PreflightKind.NETWORK


def credential_configured(settings: object) -> bool:
    """True when a real GitHub credential (App or non-placeholder PAT) is set."""
    has_app = bool(
        getattr(settings, "github_app_id", None)
        and getattr(settings, "github_app_private_key", None)
    )
    token = (getattr(settings, "github_token", "") or "").strip().lower()
    return has_app or token not in _PLACEHOLDER_TOKENS
