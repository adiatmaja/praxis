"""The review gate gets a signal it can see, and states what its green covers.

Two halves of one defect. A worker correctly changed ``.s-alert`` from a flex
container to ``display: block`` and silently killed a ``justify-content`` three
hundred lines away, in a block the diff never showed. The reviewer passed it and
every sentence of its feedback was true: the defect was not in the diff. So the
first half gives the reviewer the one fact that would have made the risk visible
(how widely used the changed identifier is), and the second half makes the PASS
state what it actually observed, because a green that reads as verification when
it is only a diff summary is actively misleading.

Every assertion here is a POSITIVE fact. The blast-radius path is required to
FAIL OPEN, which means its total failure and its total success both look like
"the review proceeded"; only an assertion naming an identifier and a count can
tell them apart.
"""
# ruff: noqa: S101

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from orchestrator.core import orchestrator_review as review_mod
from orchestrator.core.orchestrator_review import (
    _SKIP_CHECKOUT_UNAVAILABLE,
    _SKIP_NO_VERIFY_CMD,
)
from orchestrator.models.schemas import TaskStatus


# A rule head changed on both sides, plus a CONTEXT line naming the modifier
# that actually broke. The context line must not be reported as changed.
_DIFF = (
    "diff --git a/styles.css b/styles.css\n"
    "--- a/styles.css\n"
    "+++ b/styles.css\n"
    "-.s-alert { display: flex; align-items: flex-start; }\n"
    "+.s-alert { line-height: 1.5; display: block; }\n"
    " .mtel-demo-banner { justify-content: center; }\n"
)


def _checkout_writing(files: dict[str, str]) -> Any:
    """An async ``backend.checkout`` that populates the temp dir it is given."""

    async def _checkout(_ref: Any, dest: str) -> None:
        for name, body in files.items():
            target = Path(dest, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")

    return _checkout


def _passing_backend(*, checkout: Any) -> Any:
    backend = AsyncMock()
    backend.name = "github"
    backend.get_diff.return_value = _DIFF
    backend.checkout.side_effect = checkout
    return backend


async def _review(orch: Any, task_id: str, project: dict[str, Any]) -> None:
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)
    await orch.review_task(task_id, project)


# --------------------------------------------------------------------------
# 5a. The reviewer is told how far the thing it is looking at reaches.
# --------------------------------------------------------------------------


@pytest.mark.unit
async def test_the_reviewer_is_told_how_widely_used_the_changed_selector_is(
    orchestrator_fixture,
):
    """The whole feature, end to end, over a real checkout on disk.

    Four real occurrences of ``.s-alert`` are written into the directory
    ``review_task`` hands the backend, and the count that reaches the reviewer
    has to be that number. Asserting only that a section EXISTS would pass on a
    walk that found nothing, which is exactly the check-that-cannot-fire this
    task exists to remove.
    """
    orch, task_id, project = orchestrator_fixture
    backend = _passing_backend(
        checkout=_checkout_writing(
            {
                "styles.css": (
                    ".s-alert { display: block; }\n"
                    ".s-alert p { margin: 0; }\n"
                    ".mtel-demo-banner.s-alert { justify-content: center; }\n"
                ),
                "docs/ui.md": "The `.s-alert` component is shared across pages.\n",
            }
        )
    )
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign]
    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "ok"}

    await _review(orch, task_id, project)

    section = orch._opus.review_diff.await_args.kwargs["blast_radius"]
    assert section is not None
    assert "`.s-alert` occurs 4 times" in section
    # The context line's selector was never changed by this diff, so reporting
    # its reach would attribute somebody else's code to this worker.
    assert ".mtel-demo-banner" not in section


@pytest.mark.unit
async def test_a_blast_radius_failure_leaves_the_review_completely_unaffected(
    orchestrator_fixture, monkeypatch, caplog
):
    """THE test. A repo walk must never wedge a review.

    Fails open means the failure path and the success path both end in "the
    review proceeded", so this asserts BOTH halves that tell them apart: the
    reviewer was still awaited (so the review really did proceed), and the
    section is absent rather than a half-built string (so nothing invented a
    count it did not measure). Patched on the MIXIN module, which is where the
    name is looked up.
    """

    def _explode(_diff: str, _root: Any) -> Any:
        message = "the checkout vanished mid-walk"
        raise OSError(message)

    orch, task_id, project = orchestrator_fixture
    backend = _passing_backend(checkout=_checkout_writing({"a.css": ".s-alert {}\n"}))
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign]
    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "ok"}
    monkeypatch.setattr(review_mod, "measure_blast_radius", _explode)

    with caplog.at_level("WARNING", logger="orchestrator.core.orchestrator_review"):
        await _review(orch, task_id, project)

    orch._opus.review_diff.assert_awaited_once()
    assert orch._opus.review_diff.await_args.kwargs["blast_radius"] is None
    updated = await orch._tq.get_task(task_id)
    assert updated is not None
    assert updated["status"] == TaskStatus.PASSED
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("blast radius" in m for m in warnings)


@pytest.mark.unit
async def test_a_diff_only_review_measures_no_blast_radius_and_says_so(
    orchestrator_fixture,
):
    """No checkout, nothing to count. The reviewer must not be told otherwise."""
    orch, task_id, project = orchestrator_fixture
    backend = _passing_backend(checkout=RuntimeError("clone failed"))
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign]
    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "ok"}

    await _review(orch, task_id, project)

    orch._opus.review_diff.assert_awaited_once()
    assert orch._opus.review_diff.await_args.kwargs["blast_radius"] is None


@pytest.mark.unit
async def test_the_prompt_degrades_to_a_neutral_line_when_nothing_was_measured():
    """An empty heading reads as "we looked and it is contained"."""
    from orchestrator.core.opus_bridge import NO_BLAST_RADIUS_LINE, OpusBridge

    captured: list[str] = []

    class _Router:
        async def run(
            self,
            call_site: str,
            prompt: str,
            project_id: str | None = None,
            cwd: str | None = None,
        ) -> str:
            captured.append(prompt)
            return '{"verdict": "pass", "feedback": "ok"}'

    bridge = OpusBridge(db=AsyncMock(), router=_Router())  # type: ignore[arg-type]

    await bridge.review_diff("diff", "do the thing")

    assert NO_BLAST_RADIUS_LINE in captured[0]
    assert "occurs" not in captured[0]


@pytest.mark.unit
async def test_the_prompt_carries_the_measurement_when_there_is_one():
    """The sibling branch: a real section reaches the model unaltered."""
    from orchestrator.core.opus_bridge import NO_BLAST_RADIUS_LINE, OpusBridge

    captured: list[str] = []

    class _Router:
        async def run(
            self,
            call_site: str,
            prompt: str,
            project_id: str | None = None,
            cwd: str | None = None,
        ) -> str:
            captured.append(prompt)
            return '{"verdict": "pass", "feedback": "ok"}'

    bridge = OpusBridge(db=AsyncMock(), router=_Router())  # type: ignore[arg-type]

    await bridge.review_diff(
        "diff",
        "do the thing",
        blast_radius="- `.s-alert` occurs 373 times in this repository",
    )

    assert "`.s-alert` occurs 373 times in this repository" in captured[0]
    assert NO_BLAST_RADIUS_LINE not in captured[0]


@pytest.mark.unit
async def test_the_real_bridge_receives_the_measurement_from_the_real_review(
    orchestrator_fixture,
):
    """The seam every other test in this file mocks away.

    ``orch._opus`` is an ``AsyncMock`` everywhere else, and an ``AsyncMock``
    accepts ANY keyword silently. So if ``review_task`` passed a name
    ``OpusBridge.review_diff`` does not have, or if the prompt template were
    missing its ``{blast_radius}`` placeholder, every wiring test above would
    still be green and production would raise on the first review.

    This one runs the real bridge and the real template, and asserts the count
    in the text that actually reaches the provider.
    """
    from orchestrator.core.opus_bridge import OpusBridge

    prompts: list[str] = []

    class _Router:
        async def run(
            self,
            call_site: str,
            prompt: str,
            project_id: str | None = None,
            cwd: str | None = None,
        ) -> str:
            prompts.append(prompt)
            return '{"verdict": "pass", "feedback": "ok"}'

    orch, task_id, project = orchestrator_fixture
    backend = _passing_backend(
        checkout=_checkout_writing(
            {"styles.css": ".s-alert {}\n.s-alert p {}\n.s-alert b {}\n"}
        )
    )
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign]
    orch._opus = OpusBridge(db=orch._tq._db, router=_Router())  # type: ignore[arg-type]

    await _review(orch, task_id, project)

    assert len(prompts) == 1
    assert "`.s-alert` occurs 3 times in this repository" in prompts[0]
    updated = await orch._tq.get_task(task_id)
    assert updated is not None
    assert updated["status"] == TaskStatus.PASSED


# --------------------------------------------------------------------------
# 5b. The PASS states what it observed.
# --------------------------------------------------------------------------


@pytest.mark.unit
async def test_the_scope_statement_names_a_degraded_checkout(orchestrator_fixture):
    """The PR head could not be cloned, so the diff text was all there was.

    A configured verify gate could not run either, and that is the same fault,
    not a second one. Naming the checkout failure is what stops the reader
    concluding the operator simply configured nothing.
    """
    orch, task_id, project = orchestrator_fixture
    gated = dict(project)
    gated["verify_cmd"] = "pytest -q"
    backend = _passing_backend(checkout=RuntimeError("clone failed"))
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign]
    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "ok"}
    events: list[dict[str, Any]] = []
    orch._bus.publish = events.append  # type: ignore[method-assign]

    await _review(orch, task_id, gated)

    parked = [e for e in events if e.get("type") == "task_awaiting_merge"]
    assert len(parked) == 1
    scope = parked[0]["review_scope"]
    assert _SKIP_CHECKOUT_UNAVAILABLE in scope
    assert _SKIP_NO_VERIFY_CMD not in scope
    assert "diff text only" in scope


@pytest.mark.unit
async def test_the_scope_statement_names_an_unconfigured_verify_command(
    orchestrator_fixture,
):
    """The other case, and it must not read like the one above.

    Here the checkout worked and the project configured no verify command. If
    both cases rendered one generic "the gate did not run", the human at the
    merge gate could not tell an operator choice from a broken deployment.
    """
    orch, task_id, project = orchestrator_fixture
    backend = _passing_backend(checkout=_checkout_writing({"a.css": ".s-alert {}\n"}))
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign]
    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "ok"}
    events: list[dict[str, Any]] = []
    orch._bus.publish = events.append  # type: ignore[method-assign]

    await _review(orch, task_id, project)

    parked = [e for e in events if e.get("type") == "task_awaiting_merge"]
    assert len(parked) == 1
    scope = parked[0]["review_scope"]
    assert _SKIP_NO_VERIFY_CMD in scope
    assert _SKIP_CHECKOUT_UNAVAILABLE not in scope
    assert "clean checkout of the PR head" in scope


@pytest.mark.unit
async def test_the_scope_statement_reports_a_verify_gate_that_actually_ran(
    orchestrator_fixture, monkeypatch
):
    """A green that DID run the project's own command may say so."""
    orch, task_id, project = orchestrator_fixture
    gated = dict(project)
    gated["verify_cmd"] = "pytest -q"
    backend = _passing_backend(checkout=_checkout_writing({"a.css": ".s-alert {}\n"}))
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign]
    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "ok"}
    monkeypatch.setattr(
        review_mod, "run_verify", AsyncMock(return_value=(True, "3 passed"))
    )
    events: list[dict[str, Any]] = []
    orch._bus.publish = events.append  # type: ignore[method-assign]

    await _review(orch, task_id, gated)

    scope = next(e for e in events if e.get("type") == "task_awaiting_merge")[
        "review_scope"
    ]
    assert "verify gate passed (`pytest -q`)" in scope


@pytest.mark.unit
async def test_the_scope_statement_is_stored_where_the_approver_reads_it(
    orchestrator_fixture,
):
    """The event alone reaches nobody who was not watching the stream.

    ``tasks.review_feedback`` is what ``praxis task``, MCP ``poll_task`` and the
    dashboard render for a parked PR, so the sentence has to survive into the
    row or the person clicking approve never sees it.
    """
    orch, task_id, project = orchestrator_fixture
    backend = _passing_backend(checkout=_checkout_writing({"a.css": ".s-alert {}\n"}))
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign]
    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "looks fine"}

    await _review(orch, task_id, project)

    updated = await orch._tq.get_task(task_id)
    assert updated is not None
    stored = updated["review_feedback"] or ""
    assert "looks fine" in stored
    assert "Review scope:" in stored
    assert _SKIP_NO_VERIFY_CMD in stored


@pytest.mark.unit
async def test_a_failed_review_carries_no_scope_statement(orchestrator_fixture):
    """A FAIL is not parked at the merge gate, and its feedback goes to a WORKER.

    ``core/worker_bible`` injects ``review_feedback`` verbatim into the next
    worker's prompt. A sentence about what the REVIEW covered is noise there at
    best, and at worst a floor model reads "verify gate did not run" as an
    instruction.
    """
    orch, task_id, project = orchestrator_fixture
    backend = _passing_backend(checkout=_checkout_writing({"a.css": ".s-alert {}\n"}))
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign]
    orch._opus.review_diff.return_value = {"verdict": "fail", "feedback": "redo it"}

    await _review(orch, task_id, project)

    updated = await orch._tq.get_task(task_id)
    assert updated is not None
    assert "Review scope:" not in (updated["review_feedback"] or "")
