"""How widely used is the thing this diff changed?

Why this file exists
--------------------
Measured 2026-08-25. A worker was asked to fix a UI defect and changed one
shared component in a stylesheet::

    -.s-alert { ... display: flex; gap: var(--sp-2); align-items: flex-start; }
    +.s-alert { ... line-height: 1.5; display: block; }

The change was CORRECT. It fixed a real defect where raw text nodes became
independent flex items. But three hundred lines further down the same file,
``.mtel-demo-banner`` -- a documented layout modifier on ``.s-alert`` -- uses
``justify-content: center``, which only applies inside a flex container. The
parent stopped being one, the property went silently inert, and a compliance
disclaimer moved from centred to left-aligned on three pages.

The review gate passed it, and every statement in its feedback was TRUE. The
defect was simply not present in the diff: nothing in those five changed lines
indicates that a property in a different selector, in a block the diff never
shows, has become a no-op.

A check that cannot fire is worse than no check, because its green then reads as
verification when it is only a diff summary. This module supplies the one cheap
fact that makes the risk visible: how widely used the identifiers the diff
changes are, counted across the whole checkout the reviewer already has.

What it deliberately is NOT
---------------------------
Not a symbol resolver, not a call graph, not a cross-language parser. The scope
is deliberately narrow -- CSS selectors from changed rule heads, and
definition-site identifiers from changed ``def`` / ``class`` / ``function`` /
``export`` / ``const X =`` lines -- because that covers the observed class and a
general extractor is a rabbit hole. The counts are literal text matches, so they
are a hint about REACH, never a claim about dependency.

Two hard constraints, because this runs on every review
-------------------------------------------------------
FAIL OPEN. Every entry point here may raise; the CALLER catches. A review must
never wedge on a repo walk. See ``orchestrator_review._blast_radius_for_review``.

BOUNDED. The repository that produced the field report was 70 MB, mostly PNGs.
Every cap below is a named constant with the reason for its number, and a walk
that hits one reports ``complete=False`` so the renderer can say "at least N"
rather than stating an under-count as exact.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


logger = logging.getLogger(__name__)


#: How many identifiers reach the prompt. Ten is what a reader takes in before
#: the list stops being a signal and becomes a wall; the list is sorted by count
#: descending, so the widest-reaching identifier is never the one cut.
TOP_N = 10

#: An identifier occurring exactly once occurs only where it was defined, so it
#: carries no blast radius at all. Reporting it would pad the section with rows
#: that say "this is new", which the diff already said.
MIN_INTERESTING_COUNT = 2

#: Shortest BARE identifier whose text count can mean anything.
#:
#: MEASURED against a realistic Python/JS diff of this repository, where ranking
#: by raw count returned ``run`` 4159, ``state`` 1282, ``init`` 543 and
#: ``__init__`` 282, and dropped the one informative name. That is not a blast
#: radius, it is a collision count: this counter is text-based and cannot
#: resolve scope, so on a short generic name a huge number means LOW CONFIDENCE
#: in the match, not HIGH risk in the change. A reviewer handed ``run: 4159``
#: either burns turns on it or learns to skip the whole block.
#:
#: Bare identifiers only. A CSS selector carries its ``.``/``#`` sigil and is
#: usually hyphenated, so ``.s-alert`` at 373 is 373 real uses of one class,
#: which is exactly the case this module was built for.
MIN_IDENTIFIER_LENGTH = 4

#: Bare identifiers whose text count is noise in ANY codebase, regardless of
#: length. Not a style opinion and not tied to this repository: these are the
#: names every language and framework reuses, so a count over them measures the
#: corpus rather than the change. Kept deliberately short -- a long list starts
#: suppressing names that genuinely are the change (``render``, ``dispatch``),
#: and the length rule above already removes the worst offenders.
GENERIC_IDENTIFIERS: frozenset[str] = frozenset(
    {
        "main",
        "init",
        "setup",
        "start",
        "stop",
        "close",
        "open",
        "read",
        "write",
        "load",
        "save",
        "data",
        "value",
        "name",
        "item",
        "items",
        "result",
        "results",
        "test",
        "tests",
        "self",
        "args",
        "kwargs",
        "type",
        "state",
        "status",
        "error",
        "index",
        "count",
        "size",
        "length",
        "text",
        "path",
        "file",
        "line",
        "list",
        "dict",
        "config",
        "options",
        "params",
        "response",
        "request",
    }
)

#: Cap on identifiers carried from the diff into the walk. The walk cost is
#: linear in the number of files and effectively constant in the number of
#: identifiers (they are compiled into one alternation), but the EXTRACTION and
#: the report are not, and a 500-file refactor would otherwise build a pattern
#: thousands of branches wide for a section that shows ten rows.
MAX_IDENTIFIERS = 60

#: Cap on files opened. Large enough for a real application repository (Praxis
#: itself is ~800 tracked files); small enough that a checkout with a vendored
#: tree the exclusion list does not know about cannot run away.
MAX_FILES = 4000

#: Cap on total bytes read across the whole walk, 24 MB.
#:
#: MEASURED, not guessed, and the first number here that was wrong. It started
#: at 8 MB on the reasoning that this was "roughly the source text of a large
#: repository"; running the module against the praxis checkout itself read
#: 9.4 MB of entirely legitimate text (large plan documents, ``uv.lock``,
#: ``web/app.js``) and tripped the cap, so every count came back a lower bound
#: on an ordinary repository. 24 MB clears that with room, and costs ~1.3s of
#: single-pass regex, which leaves ``TIME_BUDGET_SECONDS`` as the real backstop
#: for a repository that is genuinely enormous.
MAX_TOTAL_BYTES = 24_000_000

#: Skip threshold for a single file, 512 KB. Past this a "text" file is a
#: minified bundle, a lockfile or a generated blob: its occurrence counts are
#: noise. SKIPPED on its ``stat`` size, not truncated to this length -- reading
#: the first 512 KB of a 400 MB git pack spends a sixteenth of the whole byte
#: budget to count nothing, and sixteen such files end the walk before it has
#: seen a line of source. Measured against this repository, where
#: ``bench/.work/repos`` holds bare clones: truncating cut the walk off after
#: 0.25s and made every count a lower bound.
MAX_FILE_BYTES = 512_000

#: Wall-clock budget for the walk, in seconds. This sits on the critical path of
#: every review, in front of a brain call that takes tens of seconds, so five is
#: generous for the work and still bounded if the filesystem is a slow mount.
TIME_BUDGET_SECONDS = 5.0

#: Bytes sniffed for a NUL before a file is treated as text. 8 KB is the usual
#: heuristic and covers the header of every binary format that matters here.
SNIFF_BYTES = 8192

#: Directory names never worth walking: version-control internals, vendored
#: dependencies, build output, caches. ``.git`` is the one that matters most --
#: on its own it can hold more objects than the entire source tree.
#:
#: ``.git`` is deliberately ABSENT: :func:`_excluded_dir` excludes anything
#: ENDING in ``.git``, which covers both the ordinary ``.git`` directory and a
#: bare clone's ``<name>.git`` (measured here: ``bench/.work/repos/*.git`` alone
#: is 2.4 GB). Listing it here as well would be a second guard derived from the
#: same data as the first, which is one guard for mutation testing and a
#: standing invitation to "fix" the wrong one.
EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        ".hg",
        ".svn",
        "node_modules",
        "bower_components",
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
        ".nuxt",
        ".idea",
        ".vscode",
        "target",
        "vendor",
        "coverage",
        "htmlcov",
    }
)

#: Extensions whose contents are not text worth counting. Checked BEFORE the
#: file is opened, so a 70 MB tree of PNGs costs a ``stat`` each rather than a
#: read each. The NUL sniff below is the second half of the guard: it catches a
#: binary wearing an innocent extension, which this list structurally cannot.
BINARY_SUFFIXES: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".webp",
        ".tif",
        ".tiff",
        ".avif",
        ".heic",
        ".pdf",
        ".zip",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
        ".tar",
        ".rar",
        ".jar",
        ".mp3",
        ".mp4",
        ".mov",
        ".avi",
        ".mkv",
        ".webm",
        ".wav",
        ".ogg",
        ".flac",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".class",
        ".pyc",
        ".pyo",
        ".o",
        ".a",
        ".lib",
        ".bin",
        ".dat",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".psd",
        ".ai",
        ".sketch",
        ".xlsx",
        ".docx",
        ".pptx",
        # Git object storage. Reachable whenever a checkout contains a nested
        # or bare repository, and individually enormous.
        ".pack",
        ".idx",
    }
)


def _excluded_dir(name: str) -> bool:
    """Whether ``os.walk`` should refuse to descend into a directory.

    ONE rule for git, not two: anything ending in ``.git`` is excluded, which
    covers the ordinary ``.git`` directory and a bare clone's ``<name>.git``
    alike. ``.git`` is therefore absent from ``EXCLUDED_DIRS`` on purpose --
    listing it in both places would make the set entry unfalsifiable, since a
    test deleting it would still pass on the suffix rule.
    """
    return name in EXCLUDED_DIRS or name.endswith(".git")


def is_reportable(identifier: str) -> bool:
    """Whether a text count of this identifier can carry any signal.

    Pure, and exported so the rule is testable on its own rather than only
    through a walk.

    The counter matches text, not symbols. For a distinctive name that is a good
    approximation of "how widely used is this thing"; for ``run`` it is a count
    of an English word across a corpus, and presenting it beside a real finding
    trains the reader to skip the section. Three rules, cheapest first:

    - A CSS selector always reports. Its ``.``/``#`` sigil plus the usual
      hyphenation makes it unambiguous text, which is why ``.s-alert`` at 373 is
      the motivating GOOD case rather than noise.
    - A dunder is a language protocol name (``__init__``, ``__str__``). Its
      count measures how many classes the repository has.
    - Everything else must clear ``MIN_IDENTIFIER_LENGTH`` and not be a
      ``GENERIC_IDENTIFIERS`` member.

    Args:
        identifier: An extracted identifier, sigil included for selectors.

    Returns:
        True when the identifier is distinctive enough to report.
    """
    if identifier[:1] in (".", "#"):
        return True
    if identifier.startswith("__") and identifier.endswith("__"):
        return False
    if len(identifier) < MIN_IDENTIFIER_LENGTH:
        return False
    return identifier.lower() not in GENERIC_IDENTIFIERS


@dataclass(frozen=True)
class Occurrence:
    """One changed identifier and how many times it appears in the checkout."""

    identifier: str
    count: int


@dataclass(frozen=True)
class BlastRadius:
    """The measurement, and every way it falls short of a complete one.

    Three of these four fields exist because a partial answer rendered as a
    whole one is the exact false statement this module was written to remove.
    Getting that right in the renderer and wrong in a caller one function away
    is how the first version of this shipped.

    Attributes:
        occurrences: The identifiers reported, ranked by count descending.
        complete: The walk read the whole checkout, so the counts are EXACT.
            False makes every count a lower bound, and makes an EMPTY
            ``occurrences`` mean nothing at all rather than "nothing is reused":
            a walk that was cut off cannot support that claim.
        omitted: Reused identifiers NOT in ``occurrences`` -- past ``TOP_N``,
            filtered by :func:`is_reportable`, or never measured because the
            diff exceeded ``MAX_IDENTIFIERS``. Non-zero means "there is more
            reach here than the list shows", so an empty list with a non-zero
            ``omitted`` must never read as "nothing is reused".
        identifiers: How many identifiers were actually measured. Zero means the
            diff defines nothing this check tracks, which is a different fact
            from "nothing it defines is reused" and is reported differently.
    """

    occurrences: tuple[Occurrence, ...]
    complete: bool
    omitted: int = 0
    identifiers: int = 0


@dataclass(frozen=True)
class OccurrenceCounts:
    """Raw per-identifier counts from a walk, before ranking and filtering.

    ``files_read`` is the sample size, and it is load-bearing rather than
    diagnostic: ``os.walk`` over a missing, empty or unreadable root yields
    nothing and RAISES nothing, so without it a walk that opened zero files
    returns all-zero counts that are indistinguishable from a real finding of
    "nothing here is reused". See :func:`count_occurrences`.
    """

    counts: dict[str, int]
    complete: bool
    files_read: int = 0


# A CSS rule head is a selector list up to the first ``{``. The excluded
# characters are what makes this a selector test rather than a brace test:
# ``=`` and ``(``/``)`` reject ``config.defaults = {``, ``if (ready) {`` and
# ``function boot() {``, each of which ends in a brace and contains something
# that looks like a class selector. ``@`` rejects ``@media (...) {``, which is
# an at-rule and names no selector.
_CSS_RULE_HEAD = re.compile(r"^([^{}@;=()]+)\{")

# ``.foo`` / ``#foo``. The first character after the sigil must be a letter or
# underscore, so ``.5rem`` and a decimal number are never selectors.
_CSS_SELECTOR = re.compile(r"[.#][A-Za-z_][A-Za-z0-9_-]*")

# Definition sites only. A USE site would report the reach of code the worker
# merely called, which is not what the reviewer needs to know.
_DEFINITION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)"),
    re.compile(r"^\s*(?:export\s+(?:default\s+)?)?class\s+([A-Za-z_]\w*)"),
    re.compile(
        r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s*\*?\s*"
        r"([A-Za-z_]\w*)"
    ),
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_]\w*)\s*="),
    re.compile(r"^\s*export\s+(?:default\s+)?([A-Za-z_]\w*)\s*[;,]?\s*$"),
)


def _changed_lines(diff: str) -> list[str]:
    """Return the content of added/removed lines, headers excluded.

    ``+++``/``---`` are file headers, not changes. A line with no ``+``/``-``
    prefix is CONTEXT: reporting the reach of a context line's identifier would
    attribute code the worker never touched to the worker, and on the diff that
    produced this module it would have named the very selector that broke as
    something the change modified.
    """
    changed: list[str] = []
    for line in diff.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if not line.startswith(("+", "-")):
            continue
        changed.append(line[1:])
    return changed


def extract_identifiers(diff: str) -> list[str]:
    """Pull the identifiers a diff DEFINES or redefines, in first-seen order.

    Pure: no filesystem, no clock. Deliberately scoped to CSS selectors from
    changed rule heads and definition-site identifiers, per
    ``docs``-level scope note in this module's docstring.

    Args:
        diff: A unified diff.

    Returns:
        Deduplicated identifiers in first-seen order, UNCAPPED. CSS selectors
        keep their ``.``/``#`` sigil, because that is what is counted in the
        checkout and what the reviewer will recognise.

        ``MAX_IDENTIFIERS`` is applied by :func:`measure_blast_radius`, not
        here, so that the overflow can be COUNTED and reported. Capping in a
        function whose return type is a bare list silently discarded the
        overflow, and a 200-identifier diff then produced 60 measurements
        labelled complete.
    """
    found: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        if name not in seen:
            seen.add(name)
            found.append(name)

    for content in _changed_lines(diff):
        head = _CSS_RULE_HEAD.match(content)
        if head is not None:
            for selector in _CSS_SELECTOR.findall(head.group(1)):
                _add(selector)
        for pattern in _DEFINITION_PATTERNS:
            match = pattern.match(content)
            if match is not None:
                _add(match.group(1))

    return found


def _occurrence_pattern(identifiers: Sequence[str]) -> re.Pattern[str]:
    """Compile every identifier into ONE alternation with named groups.

    One scan per file rather than one scan per file per identifier: with sixty
    identifiers and an eight-megabyte budget the naive form is half a gigabyte
    of regex work inside a five-second budget.

    A CSS selector needs no leading boundary (the ``.``/``#`` sigil is its own)
    but does need a trailing one, or ``.s-alert`` matches inside
    ``.s-alert-danger`` and every short class name is silently inflated. A bare
    identifier gets ``\\b`` on both sides so ``render`` does not match
    ``prerender``.
    """
    parts: list[str] = []
    for index, identifier in enumerate(identifiers):
        escaped = re.escape(identifier)
        if identifier[:1] in (".", "#"):
            body = rf"{escaped}(?![\w-])"
        else:
            body = rf"\b{escaped}\b"
        parts.append(f"(?P<g{index}>{body})")
    return re.compile("|".join(parts))


def count_in_text(text: str, pattern: re.Pattern[str]) -> dict[str, int]:
    """Count each group of ``pattern`` in ``text``. Pure, no filesystem.

    Args:
        text: File contents.
        pattern: The alternation from :func:`_occurrence_pattern`.

    Returns:
        ``{group_name: hits}`` for groups that matched at least once.
    """
    hits: dict[str, int] = {}
    for match in pattern.finditer(text):
        name = match.lastgroup
        if name is not None:
            hits[name] = hits.get(name, 0) + 1
    return hits


def _read_text_bounded(path: Path) -> str | None:
    """Read a text file, or return None when it is binary or over the cap.

    Binary is decided TWICE on purpose: by extension before opening (so a tree
    of images costs a ``stat`` each) and by a NUL sniff after (so a binary
    wearing an innocent extension is still skipped). Neither guard catches what
    the other catches.

    An over-size file is SKIPPED, never truncated. Truncating spent
    ``MAX_FILE_BYTES`` of the walk's budget per giant file to count noise, which
    on a real repository ended the walk before it reached the source.

    The ``is_file`` gate is not redundant with ``os.walk`` listing it under
    filenames: a FIFO, a device node or a socket is listed there too, and
    OPENING one blocks forever. The deadline is only consulted BETWEEN files, so
    a single blocking syscall is unbounded no matter what the time budget says.
    """
    if path.suffix.lower() in BINARY_SUFFIXES:
        return None
    if not path.is_file():
        return None
    if path.stat().st_size > MAX_FILE_BYTES:
        return None
    with path.open("rb") as handle:
        head = handle.read(SNIFF_BYTES)
        if b"\x00" in head:
            return None
        rest = handle.read(MAX_FILE_BYTES) if len(head) == SNIFF_BYTES else b""
    return (head + rest).decode("utf-8", errors="ignore")


def count_occurrences(
    root: Path | str,
    identifiers: Sequence[str],
    *,
    max_files: int = MAX_FILES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
    time_budget_seconds: float = TIME_BUDGET_SECONDS,
) -> OccurrenceCounts:
    """Count every identifier across a checkout, under hard caps.

    Args:
        root: The checkout to walk.
        identifiers: Identifiers to count, from :func:`extract_identifiers`.
        max_files: Cap on files opened.
        max_total_bytes: Cap on total bytes read.
        time_budget_seconds: Wall-clock cap on the whole walk.

    Returns:
        Counts for every identifier (zero included), how many files were read,
        and whether the walk finished. A cap reached makes the counts LOWER
        BOUNDS, which is why the flag travels with them rather than being logged
        and dropped.

        A walk that read ZERO files reports ``complete=False``. ``os.walk`` over
        a path that does not exist, holds nothing, or cannot be read yields
        nothing and raises nothing, so all-zero counts would otherwise be
        returned as a positive finding of "nothing here is reused" by a check
        that never opened a file. That is a check which structurally cannot
        fail, returning green, which is the defect this whole module exists to
        remove.

    Raises:
        OSError: The caller fails open; see the module docstring.
    """
    counts: dict[str, int] = dict.fromkeys(identifiers, 0)
    if not identifiers:
        return OccurrenceCounts(counts=counts, complete=True, files_read=0)

    pattern = _occurrence_pattern(identifiers)
    index_to_identifier = {f"g{i}": name for i, name in enumerate(identifiers)}
    deadline = time.monotonic() + time_budget_seconds
    files_read = 0
    bytes_read = 0
    complete = True

    for dirpath, dirnames, filenames in os.walk(str(root)):
        # In place, so os.walk never DESCENDS into an excluded tree. Filtering
        # after the fact still pays for reading every .git object's name.
        dirnames[:] = [d for d in dirnames if not _excluded_dir(d)]
        for filename in sorted(filenames):
            if (
                files_read >= max_files
                or bytes_read >= max_total_bytes
                or time.monotonic() >= deadline
            ):
                complete = False
                logger.debug(
                    "blast radius: walk of %s stopped early "
                    "(files=%d, bytes=%d); counts are lower bounds",
                    root,
                    files_read,
                    bytes_read,
                )
                return OccurrenceCounts(
                    counts=counts, complete=complete, files_read=files_read
                )
            path = Path(dirpath, filename)
            try:
                text = _read_text_bounded(path)
            except OSError:
                # A dangling symlink, a permission denial, a file deleted
                # between the walk and the open. One unreadable file must not
                # abandon the whole measurement.
                continue
            if text is None:
                continue
            files_read += 1
            # DECODED CHARACTERS, not bytes on disk, so ``MAX_TOTAL_BYTES`` is a
            # soft ceiling: a UTF-8 file of non-ASCII text counts fewer here
            # than it occupies. The gap is bounded and always in the safe
            # direction for the time budget, which is the real backstop.
            bytes_read += len(text)
            for group, hits in count_in_text(text, pattern).items():
                counts[index_to_identifier[group]] += hits

    if files_read == 0:
        # Not a finding. See this function's Returns section: a walk that opened
        # nothing has measured nothing, and all-zero counts must not be handed
        # on as "nothing here is reused".
        logger.warning(
            "blast radius: walk of %s read no files at all; reporting the "
            "measurement as incomplete rather than as 'nothing is reused'",
            root,
        )
        return OccurrenceCounts(counts=counts, complete=False, files_read=0)

    return OccurrenceCounts(counts=counts, complete=complete, files_read=files_read)


def measure_blast_radius(diff: str, root: Path | str) -> BlastRadius:
    """Extract, count, filter and rank, in one call.

    Args:
        diff: The diff under review.
        root: A clean checkout of the PR head.

    Returns:
        The top ``TOP_N`` REPORTABLE identifiers by count, excluding counts
        below ``MIN_INTERESTING_COUNT``, ordered by count descending then name
        so the section is byte-identical for an identical review.

        Every identifier dropped along the way is COUNTED into
        ``BlastRadius.omitted``: past ``TOP_N``, filtered by
        :func:`is_reportable`, or never measured because the diff carried more
        than ``MAX_IDENTIFIERS``. A cap that discards silently turns a partial
        report into an apparently exhaustive one, and an empty list into an
        apparent finding of "nothing is reused".

        Identifiers below ``MIN_INTERESTING_COUNT`` are NOT omissions: one
        occurrence is the definition itself, which is a measured answer of
        "not reused" rather than something withheld.

    Raises:
        OSError: Propagated from the walk. The caller fails open.
    """
    extracted = extract_identifiers(diff)
    if not extracted:
        # No walk at all: a prose-only diff has nothing whose reach could be
        # counted, and reading the repository to prove it is pure cost.
        # ``identifiers=0`` is what makes this distinguishable downstream from
        # "measured, and nothing it defines is reused".
        return BlastRadius(occurrences=(), complete=True, omitted=0, identifiers=0)

    selected = extracted[:MAX_IDENTIFIERS]
    unmeasured = len(extracted) - len(selected)
    measured = count_occurrences(root, selected)

    reused = [
        Occurrence(identifier=name, count=count)
        for name, count in measured.counts.items()
        if count >= MIN_INTERESTING_COUNT
    ]
    ranked = sorted(
        (occurrence for occurrence in reused if is_reportable(occurrence.identifier)),
        key=lambda occurrence: (-occurrence.count, occurrence.identifier),
    )
    shown = ranked[:TOP_N]
    return BlastRadius(
        occurrences=tuple(shown),
        complete=measured.complete,
        omitted=unmeasured + (len(reused) - len(shown)),
        identifiers=len(selected),
    )


def render_blast_radius(radius: BlastRadius) -> str:
    """Render the prompt section for a measurement that WAS taken.

    Brain-facing, so the floor-model register that governs
    ``core/agent_prompt.py`` and ``core/worker_bible.py`` does NOT apply here:
    this is written for a capable reader.

    NEVER returns "". Every empty-list case gets a sentence naming WHY it is
    empty, because the caller's only other option is to send nothing, and the
    prompt renders nothing as "Not measured for this review (no checkout was
    available, or the measurement failed)". Collapsing a real walk over a real
    checkout into that sentence is literally false, and it destroys the one
    distinction the caller documents as load-bearing: absent evidence versus
    evidence of absence. Four states, four sentences.

    Args:
        radius: The measurement. A caller with no measurement at all must pass
            ``None`` to the prompt instead of calling this.

    Returns:
        A markdown-ish block. Always non-empty.
    """
    if not radius.occurrences:
        return _render_empty_blast_radius(radius)

    qualifier = "" if radius.complete else "at least "
    lines = [
        "Repo-wide occurrences of the identifiers this diff defines or "
        "redefines, counted across the checkout:",
        "",
    ]
    lines += [
        f"- `{occurrence.identifier}` occurs {qualifier}{occurrence.count} "
        "times in this repository"
        for occurrence in radius.occurrences
    ]
    lines += [
        "",
        "Consider what else depends on the old behaviour, including code the "
        "diff does not show. A change can be correct everywhere the diff shows "
        "it and still make something elsewhere inert: a CSS property that only "
        "applies inside a flex container whose parent stopped being one, an "
        "override that no longer overrides, a caller relying on a shape that "
        "changed. If a count is high, say in your feedback what you checked "
        "and what you could not.",
        "",
        "These are literal text occurrences, not a call graph. Treat them as a "
        "hint about reach.",
    ]
    if radius.omitted:
        lines += [
            "",
            f"{radius.omitted} further changed {identifier_noun(radius.omitted)} "
            "are reused and not listed: past the top ten, too generically named "
            "for a text count to mean anything, or past the extraction cap. "
            "This list is not exhaustive.",
        ]
    if not radius.complete:
        lines += [
            "",
            "The walk hit its size or time cap, so these are lower bounds.",
        ]
    return "\n".join(lines)


def identifier_noun(count: int) -> str:
    """``identifier`` or ``identifiers``, for a countable clause."""
    return "identifier" if count == 1 else "identifiers"


def _render_empty_blast_radius(radius: BlastRadius) -> str:
    """Say WHY the list is empty. Four causes, four different meanings.

    Only the last is the finding "this change looks self-contained by text
    reach". Rendering the other three as that finding is the defect class this
    module exists to catch, committed by the module itself.
    """
    if not radius.complete:
        return (
            "The walk hit its size or time cap before it could establish "
            "anything, so NOTHING may be concluded from the absence of results "
            "here. This is not a finding that the change is contained."
        )
    if radius.identifiers == 0:
        return (
            "This diff defines or redefines no identifiers of the kinds this "
            "check tracks (CSS selectors, and function/class/const definition "
            "sites), so there was nothing whose reach could be measured. Judge "
            "the diff on its own; the absence of a measurement is not evidence "
            "the change is contained."
        )
    if radius.omitted:
        return (
            f"{radius.omitted} changed {identifier_noun(radius.omitted)} are "
            "reused elsewhere, but all of them are too generically named for a "
            "text count to mean anything, so none is listed. Reach here is "
            "real but unmeasurable by this check, NOT absent."
        )
    return (
        "Nothing this diff defines or redefines occurs more than once across "
        "the checkout. That is a measurement, not a missing one: by text reach "
        "the change looks self-contained. It does not rule out coupling the "
        "text cannot show, such as a dynamic lookup or a name built at runtime."
    )
