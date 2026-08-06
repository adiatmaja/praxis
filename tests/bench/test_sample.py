"""The draw must be reproducible and balanced, or the strata mean nothing."""

import json
import random
from pathlib import Path

import pytest

from bench.sample import draw_stratified


def _pool(n: int = 400) -> list[dict]:
    """A synthetic instance pool spanning every stratum cell."""
    pool = []
    for i in range(n):
        pool.append(
            {
                "instance_id": f"repo__proj-{i}",
                "upstream": f"https://example.invalid/{i}.git",
                "base_commit": f"{i:040d}",
                "patch_files": (i % 3) + 1,
                "patch_loc": [3, 50, 400][i % 3],
                "repo_files": [40, 250, 900][i % 3],
            }
        )
    return pool


@pytest.mark.unit
def test_the_same_seed_draws_the_same_sample():
    a = draw_stratified(_pool(), per_stratum=2, seed=42)
    b = draw_stratified(_pool(), per_stratum=2, seed=42)
    assert [i["instance_id"] for i in a] == [i["instance_id"] for i in b]


@pytest.mark.unit
def test_a_different_seed_draws_a_different_sample():
    a = draw_stratified(_pool(), per_stratum=2, seed=42)
    b = draw_stratified(_pool(), per_stratum=2, seed=43)
    assert [i["instance_id"] for i in a] != [i["instance_id"] for i in b]


@pytest.mark.unit
def test_every_populated_cell_gets_the_requested_count():
    sample = draw_stratified(_pool(), per_stratum=2, seed=42)
    from collections import Counter

    counts = Counter((i["stratum_patch"], i["stratum_repo"]) for i in sample)
    assert all(c == 2 for c in counts.values())


@pytest.mark.unit
def test_each_instance_is_drawn_at_most_once():
    sample = draw_stratified(_pool(), per_stratum=3, seed=42)
    ids = [i["instance_id"] for i in sample]
    assert len(ids) == len(set(ids))


@pytest.mark.unit
def test_every_drawn_instance_carries_its_stratum_labels():
    for entry in draw_stratified(_pool(), per_stratum=1, seed=42):
        assert entry["stratum_patch"] in {"small", "medium", "large"}
        assert entry["stratum_repo"] in {"tiny", "mid", "big"}


@pytest.mark.unit
def test_a_thin_cell_takes_everything_it_has_without_raising():
    thin = _pool(4)
    sample = draw_stratified(thin, per_stratum=99, seed=42)
    assert len(sample) == len(thin)


@pytest.mark.unit
def test_a_shuffled_pool_order_draws_the_same_sample():
    """The pool arrives in whatever row order the source happens to hand back.

    ``draw_stratified`` sorts candidates by ``instance_id`` before sampling
    specifically so the draw does not depend on that incoming order. Without
    that sort, re-fetching the same pool in a different row order would
    silently produce a different sample from the same seed.
    """
    pool = _pool()
    shuffled = list(pool)
    random.Random(7).shuffle(shuffled)

    a = draw_stratified(pool, per_stratum=2, seed=42)
    b = draw_stratified(shuffled, per_stratum=2, seed=42)
    assert [i["instance_id"] for i in a] == [i["instance_id"] for i in b]


@pytest.mark.unit
def test_the_committed_pilot_sample_is_valid():
    """SWE-bench Lite is single-file-patch only, so only 4 of the 9 stratum
    cells (small/medium patch size crossed with mid/big repo size) are ever
    populated: no gold patch in Lite touches 3+ files or exceeds 100 lines,
    and no Lite repo has fewer than 100 tracked files. At per_stratum=4 the
    honest draw is 4 cells times 4, sixteen instances, not thirty.
    """
    path = Path("bench/samples/lite-pilot-16.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["seed"] == 20260806
    assert data["corpus"] == "swe-bench-lite"
    assert data["per_stratum"] == 4
    assert len(data["instances"]) == 16
    assert sum(data["per_cell_counts"].values()) == 16
    for entry in data["instances"]:
        assert entry["instance_id"]
        assert entry["upstream"]
        assert len(entry["base_commit"]) >= 7
        assert entry["stratum_patch"] in {"small", "medium", "large"}
        assert entry["stratum_repo"] in {"tiny", "mid", "big"}
