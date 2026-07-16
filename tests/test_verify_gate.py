"""Tests for the deterministic mechanical verification gate."""

from __future__ import annotations

import sys

import pytest

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
