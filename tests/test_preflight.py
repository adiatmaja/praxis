"""Unit tests for the shared remote preflight module."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from orchestrator.core.preflight import (
    PreflightError,
    PreflightKind,
    classify_ls_remote_stderr,
    status_and_detail,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("remote: Repository not found.", PreflightKind.AUTH),
        ("fatal: could not read Username for 'https://github.com'", PreflightKind.AUTH),
        ("remote: Permission to owner/repo denied", PreflightKind.AUTH),
        ("The requested URL returned error: 403", PreflightKind.AUTH),
        (
            "fatal: Authentication failed for 'https://github.com/o/r'",
            PreflightKind.AUTH,
        ),
        (
            "fatal: unable to access ...: Could not resolve host: github.com",
            PreflightKind.NETWORK,
        ),
        (
            "fatal: unable to access ...: Failed to connect ... Connection timed out",
            PreflightKind.NETWORK,
        ),
        (
            "ssh: connect to host github.com port 22: Connection refused",
            PreflightKind.NETWORK,
        ),
        ("some entirely unrecognized failure text", PreflightKind.NETWORK),
    ],
)
def test_classify_ls_remote_stderr(stderr: str, expected: PreflightKind) -> None:
    assert classify_ls_remote_stderr(stderr) == expected


@pytest.mark.unit
def test_preflight_error_carries_kind_and_message() -> None:
    err = PreflightError(PreflightKind.NOT_GITHUB, "nope")
    assert err.kind is PreflightKind.NOT_GITHUB
    assert str(err) == "nope"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("kind", "code"),
    [
        (PreflightKind.NOT_GITHUB, 422),
        (PreflightKind.MISSING_BRANCH, 422),
        (PreflightKind.MISSING_FILE, 422),
        (PreflightKind.AUTH, 422),
        (PreflightKind.NETWORK, 502),
        (PreflightKind.BASE_SHA_MISMATCH, 409),
    ],
)
def test_status_and_detail_maps_kind(kind: PreflightKind, code: int) -> None:
    status_code, detail = status_and_detail(PreflightError(kind, "msg"))
    assert status_code == code
    assert detail == "msg"


# ---------------------------------------------------------------------------
# preflight_remote
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_preflight_remote_not_github() -> None:
    """Non-GitHub URL raises PreflightError(NOT_GITHUB)."""
    from orchestrator.core.preflight import preflight_remote

    git = AsyncMock()
    with pytest.raises(PreflightError) as exc_info:
        await preflight_remote(git, "https://gitlab.com/o/r", base="main")
    assert exc_info.value.kind is PreflightKind.NOT_GITHUB


@pytest.mark.unit
async def test_preflight_remote_no_credential_warns() -> None:
    """credential_configured=False short-circuits with a warning."""
    from orchestrator.core.preflight import preflight_remote

    git = AsyncMock()
    warnings = await preflight_remote(
        git,
        "https://github.com/o/r",
        base="main",
        credential_configured=False,
    )
    assert len(warnings) == 1
    assert "credential" in warnings[0].lower()


@pytest.mark.unit
async def test_preflight_remote_auth_error() -> None:
    """remote_head_sha RuntimeError with auth stderr -> AUTH error."""
    from orchestrator.core.preflight import preflight_remote

    git = AsyncMock()
    git.remote_head_sha = AsyncMock(
        side_effect=RuntimeError("fatal: Authentication failed")
    )
    with pytest.raises(PreflightError) as exc_info:
        await preflight_remote(
            git,
            "https://github.com/o/r",
            base="main",
            credential_configured=True,
        )
    assert exc_info.value.kind is PreflightKind.AUTH


@pytest.mark.unit
async def test_preflight_remote_network_error() -> None:
    """remote_head_sha RuntimeError with network stderr -> NETWORK error."""
    from orchestrator.core.preflight import preflight_remote

    git = AsyncMock()
    git.remote_head_sha = AsyncMock(
        side_effect=RuntimeError("fatal: unable to access: Connection timed out")
    )
    with pytest.raises(PreflightError) as exc_info:
        await preflight_remote(
            git,
            "https://github.com/o/r",
            base="main",
            credential_configured=True,
        )
    assert exc_info.value.kind is PreflightKind.NETWORK


@pytest.mark.unit
async def test_preflight_remote_missing_base_branch() -> None:
    """remote_head_sha returns None -> MISSING_BRANCH error."""
    from orchestrator.core.preflight import preflight_remote

    git = AsyncMock()
    git.remote_head_sha = AsyncMock(return_value=None)
    with pytest.raises(PreflightError) as exc_info:
        await preflight_remote(
            git,
            "https://github.com/o/r",
            base="main",
            credential_configured=True,
        )
    assert exc_info.value.kind is PreflightKind.MISSING_BRANCH


@pytest.mark.unit
async def test_preflight_remote_missing_named_branch() -> None:
    """Named branch not found -> MISSING_BRANCH error."""
    from orchestrator.core.preflight import preflight_remote

    git = AsyncMock()
    git.remote_head_sha = AsyncMock(return_value="abc123")
    git.remote_branch_exists = AsyncMock(return_value=False)
    with pytest.raises(PreflightError) as exc_info:
        await preflight_remote(
            git,
            "https://github.com/o/r",
            base="main",
            branch="feature/x",
            credential_configured=True,
        )
    assert exc_info.value.kind is PreflightKind.MISSING_BRANCH


@pytest.mark.unit
async def test_preflight_remote_missing_plan_file() -> None:
    """plan_path not found -> MISSING_FILE error."""
    from orchestrator.core.preflight import preflight_remote

    git = AsyncMock()
    git.remote_head_sha = AsyncMock(return_value="abc123")
    git.remote_file_exists = AsyncMock(return_value=False)
    with pytest.raises(PreflightError) as exc_info:
        await preflight_remote(
            git,
            "https://github.com/o/r",
            base="main",
            plan_path="plans/2026-07-01-test.md",
            credential_configured=True,
        )
    assert exc_info.value.kind is PreflightKind.MISSING_FILE


@pytest.mark.unit
async def test_preflight_remote_base_sha_mismatch() -> None:
    """expected_base_sha doesn't match (even prefix) -> BASE_SHA_MISMATCH error."""
    from orchestrator.core.preflight import preflight_remote

    git = AsyncMock()
    git.remote_head_sha = AsyncMock(return_value="abcdef1234567890")
    with pytest.raises(PreflightError) as exc_info:
        await preflight_remote(
            git,
            "https://github.com/o/r",
            base="main",
            expected_base_sha="deadbeef",
            credential_configured=True,
        )
    assert exc_info.value.kind is PreflightKind.BASE_SHA_MISMATCH


@pytest.mark.unit
async def test_preflight_remote_base_sha_prefix_match_ok() -> None:
    """expected_base_sha matches as prefix of remote sha -> no error."""
    from orchestrator.core.preflight import preflight_remote

    git = AsyncMock()
    git.remote_head_sha = AsyncMock(return_value="abcdef1234567890")
    warnings = await preflight_remote(
        git,
        "https://github.com/o/r",
        base="main",
        expected_base_sha="abcdef",
        credential_configured=True,
    )
    assert warnings is not None


@pytest.mark.unit
async def test_preflight_remote_base_sha_full_match_ok() -> None:
    """expected_base_sha full match -> no error."""
    from orchestrator.core.preflight import preflight_remote

    git = AsyncMock()
    git.remote_head_sha = AsyncMock(return_value="abcdef1234567890")
    warnings = await preflight_remote(
        git,
        "https://github.com/o/r",
        base="main",
        expected_base_sha="abcdef1234567890",
        credential_configured=True,
    )
    assert warnings is not None


@pytest.mark.unit
async def test_preflight_remote_success_returns_warnings() -> None:
    """Happy path returns a (possibly empty) warnings list."""
    from orchestrator.core.preflight import preflight_remote

    git = AsyncMock()
    git.remote_head_sha = AsyncMock(return_value="abcdef1234567890")
    warnings = await preflight_remote(
        git,
        "https://github.com/o/r",
        base="main",
        credential_configured=True,
    )
    assert isinstance(warnings, list)


@pytest.mark.unit
async def test_preflight_remote_reuses_sha_for_base_check() -> None:
    """Step-6 base-sha guard reuses the step-3 sha, not a second call."""
    from orchestrator.core.preflight import preflight_remote

    git = AsyncMock()
    git.remote_head_sha = AsyncMock(return_value="abcdef1234567890")
    await preflight_remote(
        git,
        "https://github.com/o/r",
        base="main",
        expected_base_sha="abcdef",
        credential_configured=True,
    )
    assert git.remote_head_sha.call_count == 1
