"""Bench config is data, and the strata must partition the space exactly once."""

import pytest

from bench.config import (
    CONDITIONS,
    PATCH_SIZE_STRATA,
    REPO_SIZE_STRATA,
    WORKERS,
    stratum_for,
)


@pytest.mark.unit
def test_the_four_conditions_are_declared():
    assert [c.key for c in CONDITIONS] == ["A", "B", "C", "D"]


@pytest.mark.unit
def test_condition_a_and_c_both_run_without_a_verify_gate():
    """A and C must be a matched pair or the ablation is confounded."""
    by_key = {c.key: c for c in CONDITIONS}
    assert by_key["A"].verify_gate is False
    assert by_key["C"].verify_gate is False
    assert by_key["B"].verify_gate is True
    assert by_key["D"].verify_gate is True


@pytest.mark.unit
def test_only_condition_a_is_monolithic():
    by_key = {c.key: c for c in CONDITIONS}
    assert by_key["A"].decompose is False
    assert all(by_key[k].decompose for k in ("B", "C", "D"))


@pytest.mark.unit
def test_only_condition_d_enables_adaptive_split():
    by_key = {c.key: c for c in CONDITIONS}
    assert by_key["D"].adaptive_split is True
    assert not any(by_key[k].adaptive_split for k in ("A", "B", "C"))


@pytest.mark.unit
@pytest.mark.parametrize(
    ("files", "loc", "expected"),
    [
        (1, 3, "small"),
        (1, 4, "small"),
        (1, 5, "medium"),
        (2, 10, "medium"),
        (2, 100, "medium"),
        (2, 101, "large"),
        (3, 5, "large"),
        (7, 400, "large"),
    ],
)
def test_patch_size_strata_partition_the_space(files, loc, expected):
    assert stratum_for(files, loc, repo_files=50)[0] == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("repo_files", "expected"),
    [(1, "tiny"), (99, "tiny"), (100, "mid"), (500, "big"), (5000, "big")],
)
def test_repo_size_strata_partition_the_space(repo_files, expected):
    assert stratum_for(1, 3, repo_files=repo_files)[1] == expected


@pytest.mark.unit
def test_every_stratum_name_is_declared():
    names = {
        s
        for s, _ in [stratum_for(f, loc, 50) for f, loc in [(1, 3), (2, 50), (5, 200)]]
    }
    assert names <= set(PATCH_SIZE_STRATA)


@pytest.mark.unit
def test_every_repo_stratum_name_is_declared():
    names = {
        r
        for _, r in [
            stratum_for(f, loc, rf)
            for f, loc, rf in [(1, 3, 50), (2, 50, 200), (5, 200, 600)]
        ]
    }
    assert names <= set(REPO_SIZE_STRATA)


@pytest.mark.unit
def test_two_workers_are_declared_for_a_comparative_claim():
    assert len(WORKERS) == 2
    assert {w.harness for w in WORKERS} == {"opencode", "agy"}
