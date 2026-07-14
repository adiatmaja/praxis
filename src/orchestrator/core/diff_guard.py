"""Flag PRs that delete large chunks from existing files, detect new dependencies,
and catch leaked secrets.

A weak worker can silently truncate a config/source file. The reviewer brain
should catch this, but this deterministic guard is a cheap hard backstop.

The guard is ADVISORY when the brain already returned PASS: in that case we
surface the flagged files as extra context for a second targeted review pass
rather than unconditionally flipping the verdict. A hard flip only happens when
the brain itself returned FAIL (belt-and-suspenders) or when the caller
explicitly requests hard-block mode.

Delete-and-replace patterns (net additions >= raw deletions) are exempt
because a refactor that moves code from N files into M new files is not
destructive, even when individual file deletion counts are high.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import TypeAlias


_Path: TypeAlias = str

_OLD = re.compile(r"^--- a/(.+)$")

# ---------------------------------------------------------------------------
# Manifest / lockfile path patterns (case-insensitive matching done at runtime)
# ---------------------------------------------------------------------------
_MANIFEST_NAMES = frozenset(
    {
        "requirements.txt",
        "requirements.in",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Gemfile",
        "Gemfile.lock",
        "Pipfile",
        "Pipfile.lock",
        "poetry.lock",
        "Cargo.toml",
        "Cargo.lock",
        "go.mod",
        "go.sum",
        "Gopkg.toml",
        "Gopkg.lock",
        "composer.json",
        "composer.lock",
        "Mixfile",
        "rebar.config",
        "pubspec.yaml",
        "pubspec.lock",
    }
)

# Regexes that recognize a "dependency-looking" added line in a manifest.
# Intentionally broad so we catch the common shapes without being fragile.
_DEP_LINE = re.compile(
    r"""
    ^                                       # package name (pip / npm / cargo / …)
    (?:
        [A-Za-z0-9_][A-Za-z0-9._-]*\s*[>=<!~]+   # pkg>=ver  (pip)
        |
        "[A-Za-z0-9_][A-Za-z0-9._-]*"\s*:\s*"[^"]*"   # "pkg": "ver"  (npm)
        |
        \[dependencies\]                                    # section header (toml)
        |
        dependencies\s*=                                    # key = …  (toml)
        |
        gem\s+"                                          # gem "…"  (bundler)
        |
        require\s\s*\[                                   # require [ …  (bundler)
        |
        ^\s*"[A-Za-z0-9_][A-Za-z0-9._-]*"\s*:            # pkg: …  (Gemfile.lock)
        |
        # PEP621 array item: "pkg", "pkg>=1.0", "pkg[extra] >= 1.0; marker"
        ^\s*"[A-Za-z0-9_][A-Za-z0-9._-]*(?:\[[^\]]*\])?(?:\s*[><=!~;@][^"]*)?"
    )
    """,
    re.VERBOSE,
)

# ---------------------------------------------------------------------------
# Secret signatures (signature-based only — no entropy check)
# ---------------------------------------------------------------------------

# Private-key block delimiters
_PRIVATE_KEY_BEGIN = re.compile(r"-----BEGIN\s+\w+\s*PRIVATE\s+KEY-----")

# Provider-specific token prefixes
_TOKEN_PREFIX = re.compile(
    r"""
    (?:
        AKIA[A-Z0-9]{16}          # AWS access key ID
        |
        ghp_[A-Za-z0-9]{36}      # GitHub personal access token
        |
        xox[baprs]-[A-Za-z0-9-]+ # Slack token
        |
        sk-[A-Za-z0-9]{20,}      # OpenAI / generic secret key
    )
    """,
    re.VERBOSE,
)

# Keyword assignments like  password = "..."  or  api_key: '...'
_SECRET_ASSIGN = re.compile(
    r"""
    (?:
        password
        |
        secret
        |
        api[_-]?key
        |
        auth[_-]?token
        |
        access[_-]?token
        |
        private[_-]?key
    )
    \s*[=:]\s*
    ['\"][^'\"]{4,}['\"]
    """,
    re.VERBOSE | re.IGNORECASE,
)


def destructive_deletions(diff: str, threshold: int = 40) -> list[str]:
    """Return paths with a NET loss above ``threshold`` lines.

    A file is only flagged when its raw deletion count exceeds ``threshold``
    AND the entire diff has more deletions than additions (i.e. the PR is a
    net shrink, not a delete-and-replace refactor). Files introduced from
    ``/dev/null`` are never flagged.

    Args:
        diff: Unified diff text.
        threshold: Minimum per-file raw deletion count to consider flagging.

    Returns:
        List of file paths that are genuinely destructive.
    """
    removals: dict[str, int] = {}
    additions: dict[str, int] = {}
    current: str | None = None
    total_additions = 0
    total_deletions = 0

    for line in diff.splitlines():
        m = _OLD.match(line)
        if m:
            current = m.group(1)
            removals.setdefault(current, 0)
            additions.setdefault(current, 0)
            continue
        if line.startswith("--- /dev/null"):
            current = None
            continue
        if current and line.startswith("-") and not line.startswith("---"):
            removals[current] += 1
            total_deletions += 1
        elif current and line.startswith("+") and not line.startswith("+++"):
            additions[current] += 1
            total_additions += 1

    # If the diff as a whole adds at least as many lines as it removes, treat
    # it as a delete-and-replace refactor: no single file counts as destructive.
    if total_additions >= total_deletions:
        return []

    flagged = []
    for path, n in removals.items():
        if n <= threshold:
            continue
        # Per-file: if additions roughly match deletions, it is a rewrite, not
        # a truncation (allow 30% slack so minor consolidations still pass).
        file_adds = additions.get(path, 0)
        if file_adds >= n * 0.7:
            continue
        flagged.append(path)
    return flagged


# ---------------------------------------------------------------------------
# added_dependencies
# ---------------------------------------------------------------------------


def _is_manifest(path: _Path) -> bool:
    """Return True if *path* looks like a dependency manifest or lockfile."""
    name = PurePosixPath(path).name
    return name.lower() in {n.lower() for n in _MANIFEST_NAMES}


def added_dependencies(diff: str) -> list[str]:
    """Return manifest paths that gained dependency-looking added lines.

    Only lines prefixed with ``+`` in recognised manifest / lockfile paths are
    inspected.  Pure hash digests (e.g. sha256:…) are excluded so lockfile
    churn does not produce false positives.

    Args:
        diff: Unified diff text.

    Returns:
        List of manifest paths that appear to have added dependencies.
    """
    flagged: set[_Path] = set()
    current: _Path | None = None

    for line in diff.splitlines():
        m = _OLD.match(line)
        if m:
            current = m.group(1)
            continue
        if line.startswith("--- /dev/null"):
            current = None
            continue
        if (
            current
            and line.startswith("+")
            and not line.startswith("+++")
            and _is_manifest(current)
            and _DEP_LINE.search(line[1:])
        ):
            flagged.add(current)

    return list(flagged)


# ---------------------------------------------------------------------------
# detect_secrets
# ---------------------------------------------------------------------------


def detect_secrets(diff: str) -> list[str]:
    """Return paths with added lines that match known secret signatures.

    This is deliberately signature-based (no entropy check) so that lockfile
    sha256 digests and other high-entropy-but-harmless strings do not trip.

    Matches:

    * Private-key block delimiters (BEGIN … PRIVATE KEY)
    * Provider token prefixes (AKIA, ghp_, xox-, sk-)
    * Keyword assignments (password, secret, api_key, auth_token, …)

    Args:
        diff: Unified diff text.

    Returns:
        List of paths that appear to contain added secrets.
    """
    flagged: set[_Path] = set()
    current: _Path | None = None

    for line in diff.splitlines():
        m = _OLD.match(line)
        if m:
            current = m.group(1)
            continue
        if line.startswith("--- /dev/null"):
            current = None
            continue
        if current and line.startswith("+") and not line.startswith("+++"):
            content = line[1:]  # strip the '+' prefix
            if (
                _PRIVATE_KEY_BEGIN.search(content)
                or _TOKEN_PREFIX.search(content)
                or _SECRET_ASSIGN.search(content)
            ):
                flagged.add(current)

    return list(flagged)
