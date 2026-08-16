"""Every check is a named, independently failing unit with a fix hint.

Doctor's contract: read-only, never raises, one hint per red, and a non-zero
exit whenever anything is red. Every troubleshooting doc entry starts here.
"""

import pytest

from orchestrator.core.doctor import (
    CHECK_IDS,
    CheckResult,
    CheckStatus,
    run_checks,
)


@pytest.mark.unit
def test_the_expected_checks_are_registered_in_order():
    """Registry order is what the doctor table renders, so pin the sequence.

    A set comparison here passed with the first two entries swapped.
    """
    assert CHECK_IDS == (
        "docker_daemon",
        "orchestrator_health",
        "build_stamp",
        "agent_images",
        "agent_image_freshness",
        "auth_token",
        "git_credential",
        "planner_cli",
        "worker_endpoint",
        "callback_url",
        "config_mount",
        "env_drift",
    )


@pytest.mark.unit
def test_a_red_check_always_carries_a_fix_hint():
    result = CheckResult(
        check_id="docker_daemon", status=CheckStatus.RED, detail="not reachable"
    )
    assert result.hint, "every red result must tell the user what to do"


@pytest.mark.unit
def test_a_green_check_needs_no_hint():
    result = CheckResult(
        check_id="docker_daemon", status=CheckStatus.GREEN, detail="reachable"
    )
    assert result.hint == ""


@pytest.mark.unit
async def test_a_hintless_red_gets_that_checks_own_registry_hint():
    """A probe returning RED with no hint must get ITS hint, not a generic one.

    This is the contract clause 3 ("every RED result carries a fix hint") test
    that has teeth: asserting only truthiness passes even when every check
    ships the same docs pointer, which is what a probe layer written without
    per-probe ``hint=`` arguments would produce.
    """
    probes = _all_passing()
    probes["docker_daemon"] = lambda **_: CheckResult(
        check_id="docker_daemon",
        status=CheckStatus.RED,
        detail="cannot connect to the daemon",
    )

    results = await run_checks(probes=probes)

    docker = next(r for r in results if r.check_id == "docker_daemon")
    assert "Docker Desktop" in docker.hint
    assert "docs/reference.md" not in docker.hint


@pytest.mark.unit
def test_a_red_for_an_unregistered_check_falls_back_to_the_generic_hint():
    """The generic pointer is the last resort, not the default."""
    from orchestrator.core.doctor import GENERIC_HINT

    result = CheckResult(
        check_id="not_a_registered_check", status=CheckStatus.RED, detail="nope"
    )
    assert result.hint == GENERIC_HINT


@pytest.mark.unit
async def test_run_checks_returns_one_result_per_registered_check():
    results = await run_checks(probes=_all_failing())
    assert len(results) == len(CHECK_IDS)
    assert {r.check_id for r in results} == set(CHECK_IDS)


@pytest.mark.unit
async def test_run_checks_returns_results_in_registry_order():
    """``run_checks`` promises registry order and the doctor table renders it."""
    results = await run_checks(probes=_all_passing())
    assert [r.check_id for r in results] == list(CHECK_IDS)


@pytest.mark.unit
async def test_a_raising_probe_becomes_a_red_result_not_an_exception():
    """Doctor diagnoses a broken machine; it must not break on one."""

    def _boom(*args, **kwargs):
        message = "everything is on fire"
        raise RuntimeError(message)

    probes = dict.fromkeys(CHECK_IDS, _boom)
    results = await run_checks(probes=probes)
    assert all(r.status is CheckStatus.RED for r in results)
    assert all("on fire" in r.detail for r in results)


@pytest.mark.unit
async def test_all_green_reports_ok():
    from orchestrator.core.doctor import overall_status

    results = await run_checks(probes=_all_passing())
    assert overall_status(results) is CheckStatus.GREEN


@pytest.mark.unit
async def test_one_red_makes_the_overall_status_red():
    from orchestrator.core.doctor import overall_status

    probes = _all_passing()
    probes["worker_endpoint"] = lambda **_: CheckResult(
        check_id="worker_endpoint",
        status=CheckStatus.RED,
        detail="no model loaded",
        hint="load the configured model in LM Studio",
    )
    assert overall_status(await run_checks(probes=probes)) is CheckStatus.RED


@pytest.mark.unit
async def test_an_amber_check_does_not_make_the_overall_status_red():
    """Local mode has no GitHub credential; that is a note, not a failure."""
    from orchestrator.core.doctor import overall_status

    probes = _all_passing()
    probes["git_credential"] = lambda **_: CheckResult(
        check_id="git_credential",
        status=CheckStatus.AMBER,
        detail="local mode: no GitHub credential configured",
    )
    assert overall_status(await run_checks(probes=probes)) is CheckStatus.AMBER


@pytest.mark.unit
async def test_stale_agent_image_is_detected_from_the_entrypoint_mtime():
    from orchestrator.core.doctor import image_is_stale

    assert image_is_stale(image_built_at=100.0, entrypoint_mtime=200.0) is True
    assert image_is_stale(image_built_at=300.0, entrypoint_mtime=200.0) is False


@pytest.mark.unit
async def test_an_image_built_at_exactly_the_entrypoint_mtime_is_stale():
    """A tie cannot prove freshness either, so it counts as stale.

    Same reasoning as the unknown-build-time case below, and the boundary is
    reachable in practice: a build and an edit inside the same filesystem
    timestamp tick are indistinguishable.
    """
    from orchestrator.core.doctor import image_is_stale

    assert image_is_stale(image_built_at=200.0, entrypoint_mtime=200.0) is True


@pytest.mark.unit
async def test_a_missing_image_build_time_is_treated_as_stale():
    """Unknown build time means we cannot prove freshness; say so."""
    from orchestrator.core.doctor import image_is_stale

    assert image_is_stale(image_built_at=None, entrypoint_mtime=200.0) is True


@pytest.mark.unit
async def test_a_missing_probe_is_red_not_skipped():
    """A check with no probe registered must still appear, and be RED.

    This is the silent-disappearance guard: if a future caller (e.g. the
    Task 5 CLI wiring) forgets to supply a probe for a check, the check must
    still show up in the table as a red light rather than vanishing from the
    output entirely.
    """
    probes = _all_passing()
    del probes["worker_endpoint"]

    results = await run_checks(probes=probes)

    assert {r.check_id for r in results} == set(CHECK_IDS)
    missing = next(r for r in results if r.check_id == "worker_endpoint")
    assert missing.status is CheckStatus.RED
    assert missing.hint
    assert "no probe registered" in missing.detail


def _all_passing() -> dict:
    return {
        check_id: (
            lambda check_id=check_id, **_: CheckResult(
                check_id=check_id, status=CheckStatus.GREEN, detail="ok"
            )
        )
        for check_id in CHECK_IDS
    }


def _all_failing() -> dict:
    return {
        check_id: (
            lambda check_id=check_id, **_: CheckResult(
                check_id=check_id,
                status=CheckStatus.RED,
                detail="nope",
                hint="fix it",
            )
        )
        for check_id in CHECK_IDS
    }
