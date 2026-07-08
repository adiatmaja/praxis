"""Unit tests for the shared remote preflight module."""

from __future__ import annotations

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
