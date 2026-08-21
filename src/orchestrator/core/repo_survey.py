"""A bounded, factual survey of a cloned repository.

Why this file exists
--------------------
The autonomous improvement loop used to ask "what should we build next?" while
supplying three strings: the project name, the repo URL and a plan path. It
never cloned the repository or read a file of it. Measured in walkthrough #7
(2026-08-21): asked about ``playground``, seven files of helper functions, it
proposed hashing auth tokens with bcrypt, adding a transaction context manager
to the Database class, adding a Content-Security-Policy header to the Caddyfile,
and rate-limiting the auth endpoints. None of those exist in that repo. Every
one of them describes Praxis itself, because with no information about the
target the only codebase in the planner's context is the one it can see.

So this is not a prompt-tuning fix. The prompt was fine; it had nothing to
reason about. This module supplies the missing input.

What it deliberately is NOT
---------------------------
It is not a code-understanding pass, an embedding index, or a dependency graph.
It is the cheapest thing that makes the planner's claims CHECKABLE: real paths
and a short excerpt of the files that say what the project is. Anything richer
can be added later against evidence that this was insufficient.

Bounded on purpose, and the bounds ANNOUNCE themselves. A survey that silently
truncates is read as a complete picture of a smaller project, which is a subtler
version of the very failure above.
"""

from __future__ import annotations

from pathlib import Path


#: Directory names never worth a survey slot: version control internals,
#: vendored dependencies, build output and caches. ``.git`` is the one that
#: matters most, being thousands of objects that would push every real source
#: file past the cap.
_EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "dist",
        "build",
        ".next",
        ".idea",
        ".vscode",
        "target",
        "vendor",
    }
)

#: Files whose CONTENT says what the project is, rather than merely that it
#: exists. Checked at the repo root only: a README three levels down describes
#: a subpackage, not the project.
_KEY_FILES: tuple[str, ...] = (
    "README.md",
    "README.rst",
    "README",
    "pyproject.toml",
    "package.json",
    "go.mod",
    "Cargo.toml",
    "composer.json",
    "pom.xml",
    "CLAUDE.md",
    "AGENTS.md",
)

_DEFAULT_MAX_FILES = 400
_DEFAULT_EXCERPT_CHARS = 2000


def _iter_files(root: Path) -> list[str]:
    """Return repo-relative POSIX paths, sorted, with noise directories pruned.

    Sorted because filesystem iteration order is not stable across platforms
    and an unstable survey makes the improvement loop irreproducible for no
    reason. POSIX-separated and relative because an absolute Windows path is
    not something the planner can check against the repository.
    """
    found: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in _EXCLUDED_DIRS for part in rel.parts[:-1]):
            continue
        if rel.parts and rel.parts[0] in _EXCLUDED_DIRS:
            continue
        found.append(rel.as_posix())
    return sorted(found)


def _excerpt(path: Path, limit: int) -> str | None:
    """Read up to ``limit`` characters of a text file, or None if unreadable.

    A binary file (an image, a compiled artifact) is not an error and must not
    take down the whole improvement pass, so it is skipped rather than raised.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if len(text) > limit:
        return text[:limit] + f"\n... [truncated, {len(text) - limit} more characters]"
    return text


def build_repo_survey(
    root: Path,
    *,
    max_files: int = _DEFAULT_MAX_FILES,
    excerpt_chars: int = _DEFAULT_EXCERPT_CHARS,
) -> str:
    """Describe a cloned repository factually enough to reason about.

    Args:
        root: The cloned working tree.
        max_files: Cap on listed paths. Overflow is REPORTED, never dropped
            silently.
        excerpt_chars: Per-file cap on key-file content.

    Returns:
        A human-readable survey. Always non-empty: an empty repository yields a
        positive "no files" statement rather than "", because the caller
        decides whether to proceed on whether a survey exists, and a falsy
        success is indistinguishable from a failure.
    """
    files = _iter_files(root)
    lines: list[str] = []

    if not files:
        lines.append("Repository contents: no files found in the checkout.")
        return "\n".join(lines)

    shown = files[:max_files]
    lines.append(f"Repository contents ({len(files)} files):")
    lines.extend(f"  {rel}" for rel in shown)
    if len(files) > max_files:
        lines.append(f"  ... and {len(files) - max_files} more files not listed")

    for name in _KEY_FILES:
        candidate = root / name
        if not candidate.is_file():
            continue
        body = _excerpt(candidate, excerpt_chars)
        if body is None:
            continue
        lines.append("")
        lines.append(f"--- {name} ---")
        lines.append(body.rstrip())

    return "\n".join(lines)
