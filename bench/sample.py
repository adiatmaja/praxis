"""Stratified sampling from a SWE-bench instance pool.

Stratification is pre-registered: the buckets, the per-cell counts, and the
seed are all fixed in ``bench/config.py`` and the drawn list is committed.
Nobody gets to look at the results and then re-draw.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from bench.config import PILOT_PER_STRATUM, SAMPLE_SEED, stratum_for


logger = logging.getLogger(__name__)


def draw_stratified(
    pool: list[dict[str, Any]], per_stratum: int, seed: int
) -> list[dict[str, Any]]:
    """Draw ``per_stratum`` instances from every populated stratum cell.

    Args:
        pool: Instance dicts carrying ``patch_files``, ``patch_loc``, and
            ``repo_files`` from the published SWE-bench metadata.
        per_stratum: Target count per cell.  A thinner cell contributes
            everything it has rather than raising.
        seed: Reproducibility seed; publish it with the sample.

    Returns:
        The drawn instances, each annotated with ``stratum_patch`` and
        ``stratum_repo``, sorted by instance id so the output is stable.
    """
    cells: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for entry in pool:
        patch, repo = stratum_for(
            int(entry["patch_files"]),
            int(entry["patch_loc"]),
            int(entry["repo_files"]),
        )
        annotated = {**entry, "stratum_patch": patch, "stratum_repo": repo}
        cells[(patch, repo)].append(annotated)

    rng = random.Random(seed)  # nosec B311 - reproducible sampling, not crypto
    drawn: list[dict[str, Any]] = []
    for cell in sorted(cells):
        # Sorting is load-bearing: the pool arrives in whatever row order the
        # source happened to hand back, and rng.sample's result depends on
        # that order. Without this sort, re-fetching the same pool in a
        # different row order would silently produce a different sample from
        # the same seed.
        candidates = sorted(cells[cell], key=lambda e: e["instance_id"])
        take = min(per_stratum, len(candidates))
        if take < per_stratum:
            logger.warning(
                "stratum %s has only %d instances, requested %d",
                cell,
                len(candidates),
                per_stratum,
            )
        drawn.extend(rng.sample(candidates, take))
    return sorted(drawn, key=lambda e: e["instance_id"])


def main(argv: list[str] | None = None) -> int:
    """CLI: draw a sample from a pool file and write a committed sample file."""
    parser = argparse.ArgumentParser(description="Draw a stratified bench sample")
    parser.add_argument("--pool", required=True, help="instance metadata JSON")
    parser.add_argument("--out", required=True, help="where to write the sample")
    parser.add_argument("--per-stratum", type=int, default=PILOT_PER_STRATUM)
    parser.add_argument("--seed", type=int, default=SAMPLE_SEED)
    parser.add_argument("--corpus", default="swe-bench-lite")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    pool = json.loads(Path(args.pool).read_text(encoding="utf-8"))
    instances = draw_stratified(pool, args.per_stratum, args.seed)
    per_cell_counts: dict[str, int] = defaultdict(int)
    for entry in instances:
        key = f"{entry['stratum_patch']}/{entry['stratum_repo']}"
        per_cell_counts[key] += 1
    Path(args.out).write_text(
        json.dumps(
            {
                "corpus": args.corpus,
                "seed": args.seed,
                "per_stratum": args.per_stratum,
                "per_cell_counts": dict(sorted(per_cell_counts.items())),
                "instances": instances,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    logger.info("wrote %d instances to %s", len(instances), args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
