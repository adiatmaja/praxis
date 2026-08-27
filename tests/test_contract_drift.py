"""Tests for the merge-gate contract-drift check (core/contract_drift)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.core.contract_drift import (
    NO_PLAN_DOCUMENT,
    PLAN_AUTHORISES_NOTHING,
    ContractDrift,
    as_payload,
    assess,
    changed_paths,
    plan_authorised_paths,
    plan_mentioned_paths,
    summary_line,
)


def _corpus_plan(plan_id: str) -> str:
    """Return one real plan document from the decomposition corpus."""
    corpus = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "decompose"
            / "plan_text_backing_corpus.json"
        ).read_text(encoding="utf-8")
    )
    return next(p for p in corpus if p["plan_id"] == plan_id)["source_plan"]


def _diff_touching(*paths: str) -> str:
    """Build a minimal unified diff that edits each of *paths*."""
    return "\n".join(
        f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n-old\n+new"
        for p in paths
    )


# ---------------------------------------------------------------------------
# changed_paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_changed_paths_reads_both_diff_sides():
    assert changed_paths(_diff_touching("src/a.py", "src/b.py")) == [
        "src/a.py",
        "src/b.py",
    ]


@pytest.mark.unit
def test_changed_paths_sees_a_file_the_worker_created():
    """A created file has ``--- /dev/null`` and a path only on the ``+++`` side.

    Load-bearing, not defensive: a worker can replace a contract by deleting
    the file and adding it back, and reading only the ``---`` side would show
    the deletion of ``/dev/null`` and miss the path entirely.
    """
    diff = (
        "diff --git a/src/new.py b/src/new.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/src/new.py\n"
        "@@ -0,0 +1 @@\n"
        "+x = 1"
    )
    assert changed_paths(diff) == ["src/new.py"]


@pytest.mark.unit
def test_changed_paths_is_empty_for_an_empty_diff():
    assert changed_paths("") == []


# ---------------------------------------------------------------------------
# reading the plan document
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_authorised_paths_come_only_from_files_lines():
    plan = (
        "# Plan\n\n"
        "`src/contract_test.py` is the acceptance bar. Do not edit it.\n\n"
        "## Task 1: build it\n"
        "Files: `src/thing.py`\n"
        "Steps:\n- write src/thing.py\n"
    )
    assert plan_authorised_paths(plan) == {"src/thing.py"}
    assert "src/contract_test.py" in plan_mentioned_paths(plan)
    assert "src/contract_test.py" not in plan_authorised_paths(plan)


@pytest.mark.unit
@pytest.mark.parametrize(
    "line",
    [
        "Files: `src/thing.py`",
        "- Files: src/thing.py",
        "**Files**",
        "  files: src/thing.py",
    ],
)
def test_files_label_survives_the_decoration_real_plans_use(line: str):
    """Real plans write the label four different ways; all four are the label.

    ``**Files**`` on its own line is the shape ``01029a25`` used, and it
    carries no path - the paths are on the bullet lines under it. It is
    included here to pin that the LABEL matches, which is what
    ``plan_authorises_nothing`` keys on.
    """
    from orchestrator.core.contract_drift import _FILES_LABEL_RE

    assert _FILES_LABEL_RE.match(line) is not None


@pytest.mark.unit
def test_a_prose_sentence_ending_in_a_path_is_not_a_files_line():
    plan = "Files are important.\nThe module is src/thing.py somewhere.\n"
    assert plan_authorised_paths(plan) == set()


# ---------------------------------------------------------------------------
# ungradable is an ANSWER
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_plan_document_is_ungradable_and_says_so():
    drift = assess(_diff_touching("src/a.py"), None)
    assert drift.gradable is False
    assert drift.why_not == NO_PLAN_DOCUMENT
    assert drift.clean is False, "ungradable must never read as a clean result"


@pytest.mark.unit
def test_a_plan_that_authorises_nothing_is_ungradable_and_says_so():
    drift = assess(_diff_touching("src/a.py"), "# Plan\n\nBuild a thing.\n")
    assert drift.gradable is False
    assert drift.why_not == PLAN_AUTHORISES_NOTHING
    assert drift.clean is False


# ---------------------------------------------------------------------------
# the two tiers, on the REAL artefacts
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_round_seven_fabrication_lands_in_the_strong_tier():
    """playground PR #103, against the plan document that forbade the edit.

    The plan named ``src/playground/test_hm.py`` as its contract, gave its one
    task ``Files: src/playground/hm.py``, and the merged diff touched both.
    This is the case the whole check exists for, and the plan text is the real
    one from ``plans.pending_input``, not a retyped approximation.
    """
    drift = assess(
        _diff_touching("src/playground/hm.py", "src/playground/test_hm.py"),
        _corpus_plan("2ea05b85"),
    )
    assert drift.gradable is True
    assert drift.named_not_authorised == ["src/playground/test_hm.py"]
    assert drift.unmentioned == []
    assert "acceptance contract" in summary_line(drift)


@pytest.mark.unit
def test_a_diff_inside_the_authorised_path_is_silent():
    """The SAME plan, with the diff the plan actually asked for.

    Only the file list distinguishes this from the case above, which is the
    point: the check keys on paths, so the fabricating plan and an honest
    attempt at it differ by exactly the file that should never have been
    touched. (Playground PRs #105 and #106, the honest work on this repo,
    likewise changed only ``src/playground/hm.py``; they belong to later plans,
    so their own documents are not the one loaded here.)
    """
    drift = assess(_diff_touching("src/playground/hm.py"), _corpus_plan("2ea05b85"))
    assert drift.clean is True
    assert "stayed inside" in summary_line(drift)


@pytest.mark.unit
def test_a_faithful_leaf_adding_an_init_lands_in_the_weak_tier():
    """The known benign case must not read like the fabrication.

    The replay decomposition (plan ``8d4ee3b1``) has a faithful leaf that
    declares ``src/playground/__init__.py`` to export its new module - a path
    the plan authorises nowhere. That is the false positive that killed this
    signal as a VALIDATOR rule. Here it is separated by construction: the plan
    never names it, so it is unmentioned, not named-but-unauthorised.
    """
    drift = assess(
        _diff_touching("src/playground/hm.py", "src/playground/__init__.py"),
        _corpus_plan("8d4ee3b1"),
    )
    assert drift.named_not_authorised == []
    assert drift.unmentioned == ["src/playground/__init__.py"]
    assert "usually fine" in summary_line(drift)


@pytest.mark.unit
def test_an_abbreviated_plan_reference_is_the_same_file():
    """A plan writes ``hm.py``; a diff always writes the repo-root path."""
    plan = "# Plan\n\n## Task 1\nFiles: hm.py\nSteps:\n- write it\n"
    assert assess(_diff_touching("src/playground/hm.py"), plan).clean is True


@pytest.mark.unit
def test_suffix_matching_stops_at_a_path_boundary():
    """``test_hm.py`` must not be satisfied by a plan that authorised ``hm.py``.

    The plan here names the file the ABBREVIATED way, and that is what makes
    this guard capable of failing: with the boundary relaxed to a bare
    ``endswith``, ``src/playground/test_hm.py`` ends with ``hm.py`` and the
    check silently authorises the one file the round-7 plan forbade. Written
    first with the plan naming the full path, where the mutation changes
    nothing and the guard was green either way.
    """
    plan = "# Plan\n\n## Task 1\nFiles: hm.py\nSteps:\n- write it\n"
    drift = assess(_diff_touching("src/playground/test_hm.py"), plan)
    assert drift.unmentioned == ["src/playground/test_hm.py"]


# ---------------------------------------------------------------------------
# payload
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_payload_carries_the_reason_when_ungradable():
    payload = as_payload(ContractDrift(gradable=False, why_not=NO_PLAN_DOCUMENT))
    assert payload["gradable"] is False
    assert payload["summary"] == NO_PLAN_DOCUMENT
