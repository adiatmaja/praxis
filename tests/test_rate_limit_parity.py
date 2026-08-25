"""The two rate-limit call sites must reach the SAME verdict.

A subscription rate limit is a normal, self-healing state: ``opus_bridge``
queues the brain call and resumes itself, and ``doctor`` reports amber, not
red.  Two consumers therefore have to answer "is this output a rate limit?"
identically, or a throttled newcomer gets a RED doctor row accusing a blocking
Claude Code hook that does not exist, and ``praxis init`` (which exits with
doctor's status code) fails a correct install.

Sharing the SIGNATURE STRINGS is not enough to guarantee that, which is what
this file exists to prove.  The brain also treats ``exit != 0`` plus the word
"limit" as a rate limit, and a consumer that copies the strings but drops that
clause (or never takes the exit code) diverges on three of the four wordings
below while still importing the shared constant.  Both verdicts are computed in
one body here so agreement is asserted, not assumed.
"""
# ruff: noqa: S101

from __future__ import annotations

import pytest

# Bound at import time, BEFORE conftest's `no_live_planner_round_trip` replaces
# the module attribute with a tripwire; see tests/test_api_system.py.
from orchestrator.api.system import probe_provider_roundtrip as real_roundtrip
from orchestrator.core.opus_bridge import OpusBridge
from orchestrator.database import Database


#: Real wordings a throttled Claude subscription answers with, all on exit 1.
#: Only the first matches a RATE_LIMIT_SIGNATURES substring; the other three
#: are caught solely by the exit-code clause, which is exactly why the doctor
#: probe has to be handed the exit code.
RATE_LIMITED_WORDINGS = (
    "Claude usage limit reached. Resets at 5pm.",
    "You have reached your 5-hour limit. Try again at 18:00.",
    "Error: weekly limit exceeded for this account",
    "Quota limit hit; please wait.",
)

#: Negative controls.  Without these the parity assertion is satisfied by a
#: predicate that answers True to everything, which would turn every genuinely
#: broken planner amber and hide the failure doctor exists to find.
NOT_RATE_LIMITED_WORDINGS = (
    "Blocked by policy",
    "Error: connection reset by peer",
)


async def _doctor_verdict(
    mocker: pytest.MonkeyPatch, code: int, stderr: str
) -> tuple[bool, bool | None]:
    """Run the real doctor round-trip body over one fake `claude -p` result."""
    import orchestrator.api.system as sys_mod

    sys_mod._roundtrip_probe_cache.clear()
    mocker.patch("orchestrator.api.system.shutil.which", side_effect=lambda name: name)
    proc = mocker.MagicMock()
    proc.returncode = code
    proc.communicate = mocker.AsyncMock(return_value=(b"", stderr.encode()))
    proc.wait = mocker.AsyncMock(return_value=code)

    async def _fake_exec(*args: object, **kwargs: object) -> object:
        return proc

    mocker.patch("asyncio.create_subprocess_exec", new=_fake_exec)
    result = await real_roundtrip("claude")
    return result.rate_limited, result.ok


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stderr", "expected"),
    [(wording, True) for wording in RATE_LIMITED_WORDINGS]
    + [(wording, False) for wording in NOT_RATE_LIMITED_WORDINGS],
)
async def test_doctor_and_the_brain_agree_on_what_a_rate_limit_is(
    mocker: pytest.MonkeyPatch,
    db: Database,
    stderr: str,
    expected: bool,
) -> None:
    """Same output, same exit code, same verdict at both call sites."""
    doctor_rate_limited, doctor_ok = await _doctor_verdict(mocker, 1, stderr)
    brain_rate_limited = await OpusBridge(db)._check_and_handle_rate_limit(
        1, "", stderr
    )

    assert doctor_rate_limited == brain_rate_limited == expected
    # A rate limit is "not probed", so the install-plus-auth verdict stands and
    # the row is amber.  Anything else on a non-zero exit is a genuine red.
    assert doctor_ok is (None if expected else False)


@pytest.mark.unit
@pytest.mark.parametrize("stderr", RATE_LIMITED_WORDINGS)
async def test_the_brain_parks_opus_state_on_every_wording(
    db: Database, stderr: str
) -> None:
    """Agreement is worthless if the shared verdict never reaches the state row."""
    assert await OpusBridge(db)._check_and_handle_rate_limit(1, "", stderr) is True

    state = await db.fetch_one("SELECT status, resume_at FROM opus_state WHERE id = 1")
    assert state is not None
    assert state["status"] == "rate_limited"
    assert state["resume_at"]


@pytest.mark.unit
async def test_the_exit_code_clause_does_not_fire_on_a_clean_exit_zero(
    mocker: pytest.MonkeyPatch, db: Database
) -> None:
    """The bare word "limit" is only evidence when the call FAILED.

    ``claude`` answering a prompt that happens to discuss limits exits 0, and
    reading that as a throttle would park the brain and amber the doctor row
    over nothing.

    Named for the clause it actually guards. It used to be called "a clean exit
    zero is never a rate limit", which is a claim this fixture cannot support
    and which is FALSE of the code: ``is_rate_limited``'s first clause is not
    gated on the exit code at all, so an exit-0 response carrying a full
    signature is read as a throttle. That gap is pinned directly below rather
    than hidden behind a name.
    """
    chatty = "your context limit is 200k tokens"
    doctor_rate_limited, _ = await _doctor_verdict(mocker, 0, chatty)
    brain_rate_limited = await OpusBridge(db)._check_and_handle_rate_limit(
        0, "", chatty
    )

    assert doctor_rate_limited == brain_rate_limited is False


@pytest.mark.unit
@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN TRAP, assigned as separate work: is_rate_limited's signature "
        "clause is not gated on the exit code, so a SUCCESSFUL response "
        "carrying a signature is read as a throttle. Latent rather than live "
        "-- main.py always passes router=, so the legacy _run_claude arm this "
        "reaches is unused on every install, and the only other consumer is "
        "the doctor round trip, whose prompt can only answer 'PONG'. Gate the "
        "clause and this XPASSes: delete the marker then, do not delete the "
        "test."
    ),
)
async def test_a_successful_answer_carrying_a_signature_is_not_a_rate_limit(
    mocker: pytest.MonkeyPatch, db: Database
) -> None:
    """What "a clean exit zero is never a rate limit" would actually require.

    The wording matters: "your context limit is 200k tokens" trips only the
    exit-code clause and so passes today, which is exactly why the test above
    could never have caught this. A phrase containing a real SIGNATURE is
    classified as a throttle on a successful call, and the router path avoids
    it only because it asks the predicate about failed calls alone.
    """
    chatty = "your rate limit is 200 requests per minute"
    doctor_rate_limited, _ = await _doctor_verdict(mocker, 0, chatty)
    brain_rate_limited = await OpusBridge(db)._check_and_handle_rate_limit(
        0, "", chatty
    )

    assert doctor_rate_limited == brain_rate_limited is False
