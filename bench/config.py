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

# Gold-patch size buckets, per SWE-bench Goes Live! (arXiv 2505.23419).
PATCH_SIZE_STRATA: tuple[str, ...] = ("small", "medium", "large")

# Repository size buckets, by tracked file count.
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
# not itself a target. Measured 2026-08-07: SWE-bench Lite populates only 4 of
# the 9 cells (every Lite gold patch touches exactly 1 file, and no Lite repo
# has under 100 tracked files), so the pilot draws 16 and a full run would draw
# 64, not the 30 and 144 an unfiltered 9-cell corpus would give. See
# bench/README.md, "Stratification".
PILOT_PER_STRATUM = 4
FULL_PER_STRATUM = 16


def stratum_for(files: int, loc: int, repo_files: int) -> tuple[str, str]:
    """Return ``(patch_size_stratum, repo_size_stratum)`` for one instance.

    Boundaries follow arXiv 2505.23419: a single-file patch under 5 lines
    resolves about 48 percent of the time; 3 or more files or 100 or more LOC
    drops under 10 percent.  The buckets are chosen to straddle those cliffs so
    the expected effect concentrates in the middle cell.
    """
    if files >= 3 or loc > 100:
        size = "large"
    elif files == 1 and loc < 5:
        size = "small"
    else:
        size = "medium"

    if repo_files < 100:
        repo = "tiny"
    elif repo_files < 500:
        repo = "mid"
    else:
        repo = "big"
    return size, repo
