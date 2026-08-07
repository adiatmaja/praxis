"""Grading uses the OFFICIAL harness. This module only extracts and delegates.

The load-bearing assertion here is
``test_a_prediction_for_a_repo_whose_main_advanced_carries_a_non_empty_patch``.
Every prediction's patch is ``git diff <base>...<result>``, and the base has to
be the instance's TRUE base commit, threaded in from the committed sample.  A
wrong base (for example falling back to ``"main"``, which makes the diff
``main...main``) produces an EMPTY patch for every instance, the official
harness grades every instance unresolved, and every condition reports 0 percent
while looking exactly like a real result.  Nothing else in this file notices:
the two ``extract_patch`` tests pass their base explicitly, so they stay green
under precisely that bug.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from bench.grade import (
    GradeResult,
    MissingBaseCommitError,
    extract_patch,
    load_bases,
    parse_official_report,
    select_report,
    write_predictions,
)
from bench.metrics import AttemptRecord


INSTANCE_ID = "x__y-1"

SAMPLES_DIR = Path(__file__).resolve().parents[2] / "bench" / "samples"


def _git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _record(**overrides) -> AttemptRecord:
    base = {
        "run_id": "run-1",
        "instance_id": INSTANCE_ID,
        "condition": "B",
        "worker": "local-openweight",
        "seed": 1,
        "stratum_patch": "small",
        "stratum_repo": "mid",
        "resolved": False,
        "plausible": False,
        "leaf_count": 2,
        "leaf_retries": 0,
        "whole_task_retries": 0,
        "clarifications": 0,
        "human_gate_touches": 0,
        "brain_tokens": 0,
        "worker_tokens": 0,
        "wall_clock_s": 12.5,
        "error": None,
    }
    base.update(overrides)
    return AttemptRecord(**base)


@pytest.fixture
def merged_repo(tmp_path):
    """A bare repo whose main carries one merged change over the base.

    Returned as ``(repos_dir, bare, base)``: the bare repo is named the way the
    runner names it, ``<repos_dir>/<instance_id>.git``, because that is the
    layout ``write_predictions`` walks.
    """
    work = tmp_path / "w"
    repos = tmp_path / "repos"
    repos.mkdir()
    bare = repos / f"{INSTANCE_ID}.git"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    _git("config", "user.email", "t@e.com", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git("add", ".", cwd=work)
    _git("commit", "-m", "base", cwd=work)
    base = _git("rev-parse", "HEAD", cwd=work)
    (work / "app.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    _git("commit", "-am", "the model's fix", cwd=work)
    # cwd is keyword-only with no default; the plan's fixture omitted it here.
    _git("clone", "--bare", str(work), str(bare), cwd=tmp_path)
    return repos, bare, base


# --------------------------------------------------------------------------
# Patch extraction
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_extract_patch_returns_the_change_against_base(merged_repo):
    _repos, bare, base = merged_repo
    patch = extract_patch(bare, base=base, head="main")
    assert "-    return 1" in patch
    assert "+    return 2" in patch


@pytest.mark.integration
def test_extract_patch_is_empty_when_nothing_changed(merged_repo):
    _repos, bare, base = merged_repo
    assert extract_patch(bare, base=base, head=base).strip() == ""


# --------------------------------------------------------------------------
# The prediction file, which is where a wrong base silently zeroes the run
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_a_prediction_for_a_repo_whose_main_advanced_carries_a_non_empty_patch(
    merged_repo, tmp_path
):
    """The base must come from the sample, not from probing the bare repo.

    A bare repo has no working tree, so no base-marker file can live in it and
    any such probe falls back to a default. With ``main`` as that default the
    diff is ``main...main``, which is empty for EVERY instance, and the whole
    matrix grades 0 percent while looking like a finished run.
    """
    repos, _bare, base = merged_repo
    out = tmp_path / "predictions.jsonl"
    write_predictions([_record()], repos, out, "praxis", bases={INSTANCE_ID: base})
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["instance_id"] == INSTANCE_ID
    assert rows[0]["model_name_or_path"] == "praxis"
    assert rows[0]["model_patch"].strip(), "the prediction carries an empty patch"
    assert "+    return 2" in rows[0]["model_patch"]


@pytest.mark.integration
def test_a_prediction_is_empty_when_the_result_never_left_the_base(
    merged_repo, tmp_path
):
    """An attempt that changed nothing must still produce a row, just an empty one."""
    repos, bare, base = merged_repo
    _git("update-ref", "refs/heads/main", base, cwd=bare)
    out = tmp_path / "predictions.jsonl"
    write_predictions([_record()], repos, out, "praxis", bases={INSTANCE_ID: base})
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["model_patch"].strip() == ""


@pytest.mark.unit
def test_write_predictions_refuses_an_instance_with_no_known_base(tmp_path):
    """Guessing a base is how a run publishes zeroes; refuse instead."""
    with pytest.raises(MissingBaseCommitError, match=INSTANCE_ID):
        write_predictions(
            [_record()], tmp_path, tmp_path / "p.jsonl", "praxis", bases={}
        )


@pytest.mark.unit
def test_a_missing_repo_still_produces_a_row_with_an_empty_patch(tmp_path):
    out = tmp_path / "predictions.jsonl"
    write_predictions(
        [_record()], tmp_path / "absent", out, "praxis", bases={INSTANCE_ID: "0" * 40}
    )
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["model_patch"] == ""


@pytest.mark.unit
def test_the_prediction_file_is_lf(tmp_path):
    out = tmp_path / "predictions.jsonl"
    write_predictions(
        [_record()], tmp_path / "absent", out, "praxis", bases={INSTANCE_ID: "0" * 40}
    )
    assert b"\r\n" not in out.read_bytes()


@pytest.mark.unit
def test_load_bases_reads_every_committed_sample_instance():
    bases = load_bases(SAMPLES_DIR / "lite-pilot-36.json")
    assert len(bases) == 36
    assert all(len(sha) == 40 for sha in bases.values())


# --------------------------------------------------------------------------
# Report selection and report parsing
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_select_report_returns_none_when_nothing_matched():
    assert select_report([]) is None


@pytest.mark.unit
def test_select_report_picks_the_newest_of_several_matches(tmp_path):
    older = tmp_path / "a.run-1.json"
    newer = tmp_path / "b.run-1.json"
    older.write_text("{}", encoding="utf-8")
    newer.write_text("{}", encoding="utf-8")
    os.utime(older, (1_600_000_000, 1_600_000_000))
    os.utime(newer, (1_700_000_000, 1_700_000_000))
    # Argument order must not decide the answer; glob order is arbitrary.
    assert select_report([older, newer]) == newer
    assert select_report([newer, older]) == newer


@pytest.mark.unit
def test_select_report_breaks_an_mtime_tie_by_name(tmp_path):
    first = tmp_path / "a.run-1.json"
    second = tmp_path / "b.run-1.json"
    for path in (first, second):
        path.write_text("{}", encoding="utf-8")
        os.utime(path, (1_600_000_000, 1_600_000_000))
    # Order-independence alone does not pin WHICH file wins: a comparator with
    # its name tiebreak polarity reversed is still self-consistent under
    # argument-order reversal and would pass this assertion unchanged.
    assert select_report([first, second]) == select_report([second, first])
    # Name it: on a tie, the alphabetically LAST name wins ("b" over "a").
    assert select_report([first, second]) == second
    assert select_report([second, first]) == second


@pytest.mark.unit
def test_parse_official_report_reads_a_resolved_instance():
    report = {
        "astropy__astropy-12907": {
            "patch_is_None": False,
            "patch_exists": True,
            "patch_successfully_applied": True,
            "resolved": True,
        }
    }
    result = parse_official_report(report, "astropy__astropy-12907")
    assert result == GradeResult(resolved=True, plausible=True, applied=True)


@pytest.mark.unit
def test_parse_official_report_reads_an_applied_but_unresolved_instance():
    report = {
        "x__y-1": {
            "patch_is_None": False,
            "patch_exists": True,
            "patch_successfully_applied": True,
            "resolved": False,
        }
    }
    result = parse_official_report(report, "x__y-1")
    assert result.resolved is False
    assert result.plausible is True


@pytest.mark.unit
def test_parse_official_report_reads_an_unapplied_patch():
    report = {
        "x__y-1": {
            "patch_is_None": False,
            "patch_exists": True,
            "patch_successfully_applied": False,
            "resolved": False,
        }
    }
    result = parse_official_report(report, "x__y-1")
    assert result.plausible is False


@pytest.mark.unit
def test_a_missing_instance_grades_as_unresolved_not_as_an_error():
    """A crashed attempt must count against the condition, not vanish."""
    result = parse_official_report({}, "x__y-1")
    assert result == GradeResult(resolved=False, plausible=False, applied=False)
