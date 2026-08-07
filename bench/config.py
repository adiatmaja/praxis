"""Static configuration for the Praxis decomposition benchmark.

Dev-only: this package is excluded from the orchestrator image and from the
coverage gate.  Its numbers are the experiment's design, so they live in code
and get committed, not in a shell script someone retypes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


# Where prepared bare repos and run artifacts live. Overridable per machine via
# the PRAXIS_BENCH_ROOT environment variable, read in bench/prepare.py.
DEFAULT_BENCH_ROOT = Path("bench/.work")

# Gold-patch size buckets. The lower boundary is the one published in
# SWE-bench Goes Live! (arXiv 2505.23419); the upper one is re-cut for the
# corpus actually being sampled. See ``stratum_for``.
PATCH_SIZE_STRATA: tuple[str, ...] = ("small", "medium", "large")

# Repository size buckets, by tracked file count. Ordinal names: they mean
# "the smaller / middle / larger third of this corpus", not absolute sizes.
REPO_SIZE_STRATA: tuple[str, ...] = ("tiny", "mid", "big")


@dataclass(frozen=True)
class Condition:
    """One arm of the within-subject design.

    Same tasks, same worker, same brain across arms; only these three switches
    move.  ``A`` and ``C`` are a MATCHED PAIR: both run without a verify gate,
    so the A-versus-B and B-versus-C comparisons isolate different things
    without confounding each other.
    """

    key: str
    label: str
    decompose: bool
    verify_gate: bool
    adaptive_split: bool


CONDITIONS: tuple[Condition, ...] = (
    Condition("A", "monolithic baseline", False, False, False),
    Condition("B", "praxis decomposition", True, True, False),
    Condition("C", "decomposition, no verify gate", True, False, False),
    Condition("D", "decomposition plus adaptive split", True, True, True),
)


@dataclass(frozen=True)
class Worker:
    """One implementer configuration."""

    key: str
    harness: str
    model: str


# Two workers, so the capability claim is comparative rather than anecdotal:
# the reference local open-weight model and a cheap hosted mid-tier.
WORKERS: tuple[Worker, ...] = (
    Worker("local-openweight", "opencode", "qwen3.6-27b"),
    Worker("hosted-flash", "agy", "Gemini 3.6 Flash (High)"),
)

# Runs with temperature above zero get two seeds; both are reported.
SEEDS: tuple[int, ...] = (1, 2)

# Fixed sample seed, published so the draw is reproducible.
SAMPLE_SEED = 20260806

# Per-stratum sample sizes. These are the pre-registered knob; the resulting
# TOTAL is a consequence of how many cells the corpus actually populates, and is
# not itself a target. With the boundaries below, all 9 cells populate against
# SWE-bench Lite (thinnest cell: 6 instances), so the pilot draws 36 and a full
# run would draw 123 rather than 9 times 16, because three cells hold fewer than
# 16. Under the previously published boundaries only 4 cells populated and the
# pilot drew 16. See bench/README.md, "Stratification".
PILOT_PER_STRATUM = 4
FULL_PER_STRATUM = 16


def stratum_for(files: int, loc: int, repo_files: int) -> tuple[str, str]:
    """Return ``(patch_size_stratum, repo_size_stratum)`` for one instance.

    The lower boundaries are the published ones from arXiv 2505.23419: a
    single-file patch under 5 lines resolves about 48 percent of the time, and
    3 or more files or 100 or more LOC drops under 10 percent.  The buckets are
    meant to straddle those cliffs so the expected effect concentrates in the
    middle cell.

    The two UPPER boundaries are re-cut for SWE-bench Lite, and this is a
    deliberate, documented deviation.  arXiv 2505.23419 describes FULL
    SWE-bench; Lite is filtered to single-file patches, so measured over all
    300 Lite instances on 2026-08-07 every gold patch touches exactly 1 file,
    the largest is 76 changed lines against a median of 6, and the smallest
    repo is ``psf/requests`` at 121 tracked files.  Under the published
    boundaries the ``large`` patch bucket and the ``tiny`` repo bucket are
    therefore STRUCTURALLY EMPTY: only 4 of the 9 cells populate, and 5 of
    every table's rows carry no evidence at all.

    So the upper patch cut moves from 100 lines to 15 (Lite's 84th percentile),
    and the repo cuts move from 100/500 to 500/2000.  Note 500 is itself a
    published boundary that Lite does straddle, with 27 instances below it, so
    only the outer edges are corpus-fitted.  The ``files >= 3`` and
    ``files == 1`` clauses are kept unchanged so a corpus that does span
    multiple files still strata the published way.

    This re-cut was made BEFORE any outcome was observed, which is what makes
    it legitimate; it is recorded here, in bench/README.md, and in the plan's
    execution record so it can never be mistaken for a post-hoc adjustment.

    Args:
        files: Number of files the gold patch touches.
        loc: Number of changed lines in the gold patch.
        repo_files: Tracked file count in the repository at the base commit.

    Returns:
        The ``(patch_size, repo_size)`` bucket names.
    """
    if files >= 3 or loc > 15:
        size = "large"
    elif files == 1 and loc < 5:
        size = "small"
    else:
        size = "medium"

    if repo_files < 500:
        repo = "tiny"
    elif repo_files < 2000:
        repo = "mid"
    else:
        repo = "big"
    return size, repo
