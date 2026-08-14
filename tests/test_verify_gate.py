"""Tests for the deterministic mechanical verification gate."""

from __future__ import annotations

import logging
import sys
from typing import Any

import pytest

from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.orchestrator_review import (
    _SKIP_BENCH_MODE_DISABLED,
    _SKIP_CHECKOUT_UNAVAILABLE,
    _SKIP_NO_VERIFY_CMD,
)
from orchestrator.core.verify_gate import run_verify


@pytest.mark.asyncio
async def test_run_verify_passes_on_zero_exit(tmp_path) -> None:
    cmd = f'"{sys.executable}" -c "print(\'ok\')"'
    passed, output = await run_verify(str(tmp_path), cmd)
    assert passed is True
    assert "ok" in output


@pytest.mark.asyncio
async def test_run_verify_fails_on_nonzero_exit(tmp_path) -> None:
    cmd = f'"{sys.executable}" -c "import sys; print(\'boom\'); sys.exit(1)"'
    passed, output = await run_verify(str(tmp_path), cmd)
    assert passed is False
    assert "boom" in output


@pytest.mark.asyncio
async def test_run_verify_times_out(tmp_path) -> None:
    cmd = f'"{sys.executable}" -c "import time; time.sleep(5)"'
    passed, output = await run_verify(str(tmp_path), cmd, timeout=0.5)
    assert passed is False
    assert "timed out" in output.lower()


@pytest.mark.asyncio
async def test_run_verify_passes_on_pytest_no_tests_collected(tmp_path) -> None:
    # pytest exits 5 ("no tests collected") for docs-only changes; that must not
    # fail the mechanical gate, or docs-only leaves loop forever on re-dispatch.
    cmd = (
        f'"{sys.executable}" -c "import sys; '
        "print('no tests ran in 0.01s'); sys.exit(5)\""
    )
    passed, output = await run_verify(str(tmp_path), cmd)
    assert passed is True
    assert "no tests ran" in output


@pytest.mark.asyncio
async def test_run_verify_fails_on_exit_5_without_pytest_signal(tmp_path) -> None:
    # A bare exit 5 that is NOT pytest's no-tests-collected must still fail.
    cmd = f'"{sys.executable}" -c "import sys; print(\'unrelated\'); sys.exit(5)"'
    passed, output = await run_verify(str(tmp_path), cmd)
    assert passed is False
    assert "unrelated" in output


@pytest.mark.asyncio
async def test_run_verify_truncates_long_output(tmp_path) -> None:
    cmd = f'"{sys.executable}" -c "print(\'x\' * 20000)"'
    passed, output = await run_verify(str(tmp_path), cmd)
    assert passed is True
    assert len(output) < 12000
    assert "truncated" in output.lower()


# ---------------------------------------------------------------------------
# run_verify's own logging: nothing at all was logged from this module
# before this task, including the two internal special cases below that a
# caller-side "failed"/"passed" log cannot explain on its own (a timeout
# looks like any other failure; the pytest exit-5 carve-out looks like any
# other pass).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_verify_logs_the_timeout_at_warning(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    cmd = f'"{sys.executable}" -c "import time; time.sleep(5)"'
    with caplog.at_level(logging.WARNING, logger="orchestrator.core.verify_gate"):
        passed, _ = await run_verify(str(tmp_path), cmd, timeout=0.5)
    assert passed is False
    assert any("timed out" in m and cmd in m for m in caplog.messages), caplog.messages


@pytest.mark.asyncio
async def test_run_verify_logs_the_no_tests_collected_pass_at_info(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    cmd = (
        f'"{sys.executable}" -c "import sys; '
        "print('no tests ran in 0.01s'); sys.exit(5)\""
    )
    with caplog.at_level(logging.INFO, logger="orchestrator.core.verify_gate"):
        passed, _ = await run_verify(str(tmp_path), cmd)
    assert passed is True
    assert any("no tests collected" in m and cmd in m for m in caplog.messages), (
        caplog.messages
    )


# ---------------------------------------------------------------------------
# The per-task gate in ReviewMixin.review_task: the OTHER verify-gate call
# site (the plan-branch backstop lives in test_plan_verify_gate_local.py).
# Before this task it logged nothing at all for any of its four outcomes.
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_review_task_logs_passed_at_info(
    orchestrator_fixture: tuple[Orchestrator, str, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("PRAXIS_BENCH", raising=False)
    monkeypatch.delenv("PRAXIS_BENCH_DISABLE_VERIFY", raising=False)
    orch, task_id, project = orchestrator_fixture
    local = dict(project)
    local["verify_cmd"] = "pytest -q"

    async def _fake_verify(checkout: str, cmd: str) -> tuple[bool, str]:
        return True, "1 passed"

    import orchestrator.core.orchestrator_review as review_module

    monkeypatch.setattr(review_module, "run_verify", _fake_verify)
    orch._git.clone_pr_head.side_effect = None
    orch._git.clone_pr_head.return_value = "/tmp/x"

    with caplog.at_level(logging.INFO, logger="orchestrator.core.orchestrator_review"):
        await orch.review_task(task_id, local)

    assert any(
        "verify gate passed" in m and f"task={task_id}" in m for m in caplog.messages
    ), caplog.messages
    assert not any("verify gate failed" in m for m in caplog.messages)
    assert not any("verify gate skipped" in m for m in caplog.messages)


@pytest.mark.unit
async def test_review_task_logs_failed_at_warning(
    orchestrator_fixture: tuple[Orchestrator, str, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("PRAXIS_BENCH", raising=False)
    monkeypatch.delenv("PRAXIS_BENCH_DISABLE_VERIFY", raising=False)
    orch, task_id, project = orchestrator_fixture
    local = dict(project)
    local["verify_cmd"] = "pytest -q"

    async def _fake_verify(checkout: str, cmd: str) -> tuple[bool, str]:
        return False, "boom explanation"

    import orchestrator.core.orchestrator_review as review_module

    monkeypatch.setattr(review_module, "run_verify", _fake_verify)
    orch._git.clone_pr_head.side_effect = None
    orch._git.clone_pr_head.return_value = "/tmp/x"

    with caplog.at_level(logging.INFO, logger="orchestrator.core.orchestrator_review"):
        await orch.review_task(task_id, local)

    warnings = [
        m
        for r, m in zip(caplog.records, caplog.messages, strict=True)
        if r.levelno >= logging.WARNING
    ]
    assert any(
        "verify gate failed" in m and "boom explanation" in m for m in warnings
    ), warnings
    assert not any("verify gate passed" in m for m in caplog.messages)
    assert not any("verify gate skipped" in m for m in caplog.messages)


@pytest.mark.unit
async def test_review_task_logs_skip_reason_when_no_verify_cmd(
    orchestrator_fixture: tuple[Orchestrator, str, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("PRAXIS_BENCH", raising=False)
    monkeypatch.delenv("PRAXIS_BENCH_DISABLE_VERIFY", raising=False)
    orch, task_id, project = orchestrator_fixture
    local = dict(project)
    local["verify_cmd"] = None
    orch._git.clone_pr_head.side_effect = None
    orch._git.clone_pr_head.return_value = "/tmp/x"

    with caplog.at_level(logging.INFO, logger="orchestrator.core.orchestrator_review"):
        await orch.review_task(task_id, local)

    assert any(
        "verify gate skipped" in m and _SKIP_NO_VERIFY_CMD in m for m in caplog.messages
    ), caplog.messages
    assert not any("verify gate passed" in m for m in caplog.messages)


@pytest.mark.unit
async def test_review_task_logs_a_different_skip_reason_for_bench_mode(
    orchestrator_fixture: tuple[Orchestrator, str, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("PRAXIS_BENCH", "1")
    monkeypatch.setenv("PRAXIS_BENCH_DISABLE_VERIFY", "1")
    orch, task_id, project = orchestrator_fixture
    local = dict(project)
    local["verify_cmd"] = "pytest -q"

    with caplog.at_level(logging.INFO, logger="orchestrator.core.orchestrator_review"):
        await orch.review_task(task_id, local)

    assert any(
        "verify gate skipped" in m and _SKIP_BENCH_MODE_DISABLED in m
        for m in caplog.messages
    ), caplog.messages
    # The distinguishing invariant: a project with genuinely no verify_cmd
    # must log a DIFFERENT reason than bench mode forcing the gate off, or an
    # operator grepping the log cannot tell the two apart.
    assert not any(_SKIP_NO_VERIFY_CMD in m for m in caplog.messages)


@pytest.mark.unit
async def test_review_task_logs_skip_reason_when_checkout_fails(
    orchestrator_fixture: tuple[Orchestrator, str, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("PRAXIS_BENCH", raising=False)
    monkeypatch.delenv("PRAXIS_BENCH_DISABLE_VERIFY", raising=False)
    orch, task_id, project = orchestrator_fixture
    local = dict(project)
    local["verify_cmd"] = "pytest -q"
    # orchestrator_fixture's default: clone_pr_head raises, so checkout is
    # None even though verify_cmd is configured -- a silent skip today.

    with caplog.at_level(logging.INFO, logger="orchestrator.core.orchestrator_review"):
        await orch.review_task(task_id, local)

    assert any(
        "verify gate skipped" in m and _SKIP_CHECKOUT_UNAVAILABLE in m
        for m in caplog.messages
    ), caplog.messages


# ---------------------------------------------------------------------------
# The REVIEW VERDICT itself (not the verify gate): before this task, a PASS
# was discoverable only via GET /api/approvals/pending or the dashboard, and
# a review that parked at the merge gate for human approval said nothing at
# all -- a terminal-only operator could not tell "parked, awaiting approval"
# from "hung".  ``orchestrator_fixture``'s default project has ``auto_merge``
# unset (DB default 0), so a PASS verdict always takes the park branch here,
# never the auto-merge branch; that is what makes it usable to assert both
# the verdict line and the park line off of one PASS run.
# ---------------------------------------------------------------------------

_FIXTURE_PR_URL = "https://github.com/o/r/pull/1"


@pytest.mark.unit
async def test_review_task_logs_pass_verdict_and_the_merge_gate_park(
    orchestrator_fixture: tuple[Orchestrator, str, dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    orch, task_id, project = orchestrator_fixture
    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "lgtm"}

    with caplog.at_level(logging.INFO, logger="orchestrator.core.orchestrator_review"):
        await orch.review_task(task_id, project)

    assert any(
        "review verdict: pass" in m and f"task={task_id}" in m and _FIXTURE_PR_URL in m
        for m in caplog.messages
    ), caplog.messages
    assert any(
        "parked at merge gate" in m and f"task={task_id}" in m and _FIXTURE_PR_URL in m
        for m in caplog.messages
    ), caplog.messages
    # The wrong verdict word must not appear anywhere in this run's log.
    assert not any("review verdict: fail" in m for m in caplog.messages)


@pytest.mark.unit
async def test_review_task_logs_fail_verdict_at_warning(
    orchestrator_fixture: tuple[Orchestrator, str, dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # orchestrator_fixture's default opus stub returns {"verdict": "fail", ...}.
    orch, task_id, project = orchestrator_fixture

    with caplog.at_level(logging.INFO, logger="orchestrator.core.orchestrator_review"):
        await orch.review_task(task_id, project)

    warnings = [
        m
        for r, m in zip(caplog.records, caplog.messages, strict=True)
        if r.levelno >= logging.WARNING
    ]
    assert any(
        "review verdict: fail" in m and f"task={task_id}" in m and _FIXTURE_PR_URL in m
        for m in warnings
    ), warnings
    # A fail verdict must never take the park branch, and the wrong verdict
    # word must not appear.
    assert not any("review verdict: pass" in m for m in caplog.messages)
    assert not any("parked at merge gate" in m for m in caplog.messages)
