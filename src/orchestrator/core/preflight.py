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

from orchestrator.core.git_ops import GitOps


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


async def preflight_remote(
    git: object,
    repo_url: str,
    *,
    base: str,
    branch: str | None = None,
    plan_path: str | None = None,
    expected_base_sha: str | None = None,
    credential_configured: bool = True,
) -> list[str]:
    """Run cheapest-first read-only remote checks before spawning a worker.

    Steps (fail-fast):
        1. GitHub-only via ``GitOps.repo_slug``.
        2. Skip + warn when no credential.
        3. Reachability / auth / base branch via one ``remote_head_sha`` call.
        4. Named branch via ``remote_branch_exists`` (only if ``branch`` given).
        5. Plan file via ``remote_file_exists`` (only if ``plan_path`` given).
        6. Optional prefix-tolerant base-sha guard reusing the step-3 sha.

    Args:
        git: A ``GitOps`` instance (or mock) providing ``remote_head_sha``,
            ``remote_branch_exists``, and ``remote_file_exists``.
        repo_url: HTTPS or SSH URL of the GitHub repository.
        base: Base branch name to verify.
        branch: Optional named branch (e.g. plan branch) to verify.
        plan_path: Optional repository-relative path to verify.
        expected_base_sha: Optional local sha to validate against remote head.
        credential_configured: Pre-computed flag from :func:`credential_configured`.

    Returns:
        List of warning messages (empty on clean run, non-empty when checks
        were skipped).

    Raises:
        PreflightError: On a fatal check failure.
    """
    warnings: list[str] = []

    # Step 1: GitHub-only
    slug = GitOps.repo_slug(repo_url)
    if slug is None:
        raise PreflightError(
            PreflightKind.NOT_GITHUB,
            f"non-GitHub repository URL: {repo_url}",
        )

    # Step 2: credential check
    if not credential_configured:
        warnings.append("GitHub credential not configured; remote checks skipped")
        return warnings

    # Step 3: reachability / auth / base branch (single remote_head_sha call)
    try:
        head_sha = await git.remote_head_sha(repo_url, base)
    except RuntimeError as exc:
        kind = classify_ls_remote_stderr(str(exc))
        raise PreflightError(kind, str(exc)) from exc

    if head_sha is None:
        raise PreflightError(
            PreflightKind.MISSING_BRANCH,
            f"base branch not found on remote: {base}",
        )

    # Step 4: named branch (optional)
    if branch is not None:  # noqa: SIM102
        if not await git.remote_branch_exists(repo_url, branch):
            raise PreflightError(
                PreflightKind.MISSING_BRANCH,
                f"branch not found on remote: {branch}",
            )

    # Step 5: plan file (optional)
    if plan_path is not None:  # noqa: SIM102
        if not await git.remote_file_exists(slug, base, plan_path):
            raise PreflightError(
                PreflightKind.MISSING_FILE,
                f"plan file not found on remote: {plan_path}",
            )

    # Step 6: base-sha guard (optional, prefix-tolerant)
    if expected_base_sha is not None:
        expected = expected_base_sha.strip()
        if expected and not head_sha.startswith(expected):
            raise PreflightError(
                PreflightKind.BASE_SHA_MISMATCH,
                f"expected base SHA {expected!r} does not match "
                f"remote head {head_sha!r}",
            )

    return warnings
