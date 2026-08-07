"""One ``repo_url`` policy for every request schema.

Three sibling request schemas used to carry three different policies:
``ProjectCreate`` allowed ``https://`` and scp-style ``git@host:path`` only,
``DispatchRequest`` also allowed ``http://`` and ``ssh://`` and checked the git
option-injection fragments only as a PREFIX, and ``ExecutePlanRequest`` had no
validator at all.  Disagreeing copies of a security control are how the weakest
copy becomes the real one, so there is now exactly one implementation and every
schema calls it.

The policy is the STRICTEST of the three, plus one deliberate widening: a local
filesystem path is syntactically valid.  It has to be, because the local git
backend (``core/git_backend``) exists and is otherwise unreachable through the
REST surface.  A pydantic validator cannot see runtime settings, so this module
only CLASSIFIES a local path; whether one is admitted is decided by
``Settings.allow_local_repo_paths`` at the endpoint layer
(``core/preflight.assert_repo_url_allowed``), and that setting defaults to off.
"""

from __future__ import annotations

from enum import Enum

from orchestrator.core.git_backend import is_local_repo_url, local_repo_path


class RepoUrlKind(Enum):
    """Where a validated ``repo_url`` points."""

    REMOTE = "remote"
    LOCAL = "local"


# git option injection: a ``repo_url`` carrying one of these is never
# legitimate.  git receives the URL as a single argv element, so this is
# defense in depth rather than a live hole, but the check is a CONTAINMENT
# check, not a prefix check: ``https://github.com/u/r --upload-pack=/bin/sh``
# is exactly the form the prefix-only version let through.
DANGEROUS_FRAGMENTS: tuple[str, ...] = ("--upload-pack", "--config")

# Transports refused outright, each with the reason it is refused.  Matched
# against a LOWERCASED copy of the candidate so a cased variant cannot slip
# past; the accept branches below deliberately stay case-sensitive, so
# unifying the policy never widens what is accepted.
BLOCKED_TRANSPORTS: tuple[tuple[str, str], ...] = (
    ("ext::", "the 'ext::' transport executes an arbitrary command"),
    ("git://", "the 'git://' protocol is unauthenticated and can be MITM'd"),
    ("ssh://", "'ssh://' is not accepted; use the scp-style 'git@host:path'"),
    ("http://", "plaintext 'http://' is not accepted; use 'https://'"),
)

_REJECTED = (
    "repo_url must be 'https://host/owner/repo' or 'git@host:owner/repo'"
    " (a local filesystem path is accepted only when allow_local_repo_paths"
    " is enabled)"
)


def classify_repo_url(value: str) -> tuple[str, RepoUrlKind]:
    """Validate ``value`` and say whether it names a remote or a local repo.

    Args:
        value: The caller-supplied repository URL or path.

    Returns:
        A ``(stripped_value, kind)`` pair.

    Raises:
        ValueError: If the value is empty, carries a git option-injection
            fragment, uses a blocked transport, or matches no accepted form.
    """
    candidate = (value or "").strip()
    if not candidate:
        msg = "repo_url must not be empty"
        raise ValueError(msg)

    # Before anything else, including the local branch: an option fragment is
    # illegitimate on every form, and the local branch is the widest one.
    lowered = candidate.lower()
    for fragment in DANGEROUS_FRAGMENTS:
        if fragment in lowered:
            msg = f"repo_url must not contain '{fragment}'"
            raise ValueError(msg)

    for prefix, reason in BLOCKED_TRANSPORTS:
        if lowered.startswith(prefix):
            msg = f"repo_url rejected: {reason}"
            raise ValueError(msg)

    # scp-style SSH: git@host:path
    if candidate.startswith("git@"):
        if ":" not in candidate.split("@", 1)[1]:
            msg = "invalid scp-style repo_url: expected git@host:path"
            raise ValueError(msg)
        return candidate, RepoUrlKind.REMOTE

    if candidate.startswith("https://"):
        rest = candidate[len("https://") :]
        if not rest or "/" not in rest:
            msg = "repo_url must be a valid https:// URL with a path"
            raise ValueError(msg)
        return candidate, RepoUrlKind.REMOTE

    if is_local_repo_url(candidate):
        # Judge the DECODED form, because that is what reaches git.
        # ``local_repo_path`` percent-decodes and expands ``~``, so checking
        # only the raw candidate leaves a gap: ``file://%2D%2Dupload-pack=...``
        # decodes to ``--upload-pack=...``. The "single argv element" argument
        # that protects the remote forms does NOT protect this one, because an
        # argv element beginning with a dash is read by git as an OPTION, not
        # as the repository. Measured: ``git clone --no-single-branch
        # --upload-pack=/bin/sh dest`` reports ``repository 'dest' does not
        # exist``, i.e. the path was consumed as a flag.
        decoded = local_repo_path(candidate)
        decoded_lowered = decoded.lower()
        for fragment in DANGEROUS_FRAGMENTS:
            if fragment in decoded_lowered:
                msg = f"repo_url must not decode to a path containing '{fragment}'"
                raise ValueError(msg)
        if decoded.startswith("-"):
            msg = (
                "repo_url must not decode to a path beginning with '-'; "
                "git would read it as an option, not as the repository"
            )
            raise ValueError(msg)
        return candidate, RepoUrlKind.LOCAL

    raise ValueError(_REJECTED)


def validate_repo_url(value: str) -> str:
    """Pydantic ``field_validator`` body shared by every request schema.

    Args:
        value: The caller-supplied repository URL or path.

    Returns:
        The stripped value.

    Raises:
        ValueError: On any value :func:`classify_repo_url` refuses.
    """
    return classify_repo_url(value)[0]
