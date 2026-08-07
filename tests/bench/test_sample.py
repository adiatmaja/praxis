"""The draw must be reproducible and balanced, or the strata mean nothing."""

import json
import random
from pathlib import Path

import pytest

from bench.config import PILOT_PER_STRATUM
from bench.sample import draw_stratified, main


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
    """All NINE cells are populated after the boundaries were re-cut for Lite.

    With the published full-SWE-bench boundaries only 4 of 9 cells populated
    and the pilot drew 16. The re-cut (patch loc under 5 / 5 to 15 / over 15,
    repo files under 500 / 500 to 2000 / 2000 and over) spans Lite's real
    distribution, so every cell contributes. The pilot was cut from 4 per cell
    to 2 on 2026-08-08, before any outcome was observed: it exists to prove the
    loop runs live and to produce cost numbers, and power is the full run's job.
    """
    path = Path("bench/samples/lite-pilot-18.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["seed"] == 20260806
    assert data["corpus"] == "swe-bench-lite"
    assert data["per_stratum"] == PILOT_PER_STRATUM
    assert PILOT_PER_STRATUM == 2
    assert len(data["instances"]) == 18
    assert sum(data["per_cell_counts"].values()) == 18
    assert len(data["per_cell_counts"]) == 9, data["per_cell_counts"]
    assert set(data["per_cell_counts"].values()) == {PILOT_PER_STRATUM}
    for entry in data["instances"]:
        assert entry["instance_id"]
        assert entry["upstream"]
        assert len(entry["base_commit"]) >= 7
        assert entry["stratum_patch"] in {"small", "medium", "large"}
        assert entry["stratum_repo"] in {"tiny", "mid", "big"}


@pytest.mark.unit
def test_every_committed_entry_agrees_with_the_live_stratum_function():
    """The stored labels must be what ``stratum_for`` computes TODAY.

    A boundary edit that is not followed by a re-draw leaves a committed
    sample whose labels describe the old cut. Nothing else notices: the file
    still parses, still has 18 entries, and the report still groups by the
    stored label, so every table would be captioned with boundaries that were
    never used.
    """
    from bench.config import stratum_for

    data = json.loads(
        Path("bench/samples/lite-pilot-18.json").read_text(encoding="utf-8")
    )
    mismatches = [
        entry["instance_id"]
        for entry in data["instances"]
        if stratum_for(
            int(entry["patch_files"]), int(entry["patch_loc"]), int(entry["repo_files"])
        )
        != (entry["stratum_patch"], entry["stratum_repo"])
    ]
    assert mismatches == [], mismatches


@pytest.mark.unit
def test_every_committed_entry_carries_its_issue_text():
    """A sample without ``problem_statement`` cannot be run.

    ``bench.runner`` refuses such an entry, but only once a run is already
    under way. The sample is a committed artifact, so the absence is a defect
    that is visible now, and re-drawing without re-enriching is exactly how it
    reappears. See ``bench/enrich.py``.
    """
    data = json.loads(
        Path("bench/samples/lite-pilot-18.json").read_text(encoding="utf-8")
    )
    empty = [
        entry["instance_id"]
        for entry in data["instances"]
        if not str(entry.get("problem_statement", "")).strip()
    ]
    assert empty == [], empty


@pytest.mark.unit
def test_the_written_sample_file_uses_lf_line_endings_only(tmp_path):
    """The drawn sample is a COMMITTED artifact, and this repo is LF.

    ``Path.write_text`` without an explicit ``newline`` translates line feeds
    to ``os.linesep``, so on Windows the committed sample lands as CRLF against
    a repo whose ``core.autocrlf`` is false. It reads back fine through ``json``
    and through ``splitlines``, so nothing else in this file can see it. The
    first committed draw was in fact CRLF for exactly this reason.
    """
    pool = tmp_path / "pool.json"
    pool.write_text(json.dumps(_pool(30)), encoding="utf-8", newline="\n")
    out = tmp_path / "sample.json"

    assert main(["--pool", str(pool), "--out", str(out), "--per-stratum", "1"]) == 0

    raw = out.read_bytes()
    assert b"\r\n" not in raw
    assert raw.endswith(b"\n")
    assert json.loads(raw.decode("utf-8"))["instances"]
