"""When a provider says its quota comes back, and what Praxis does until then.

``core/provider_errors`` answers "was this the endpoint's fault"; this module
answers the next question, "and when is it worth asking again". They are kept
apart because the first one is a CLASSIFICATION every re-queue seat must make
and the second is a HINT that is usually absent: a gateway 502 carries no
reset time, and the deferral built here must therefore be optional at every
call site.

Measured live on 2026-09-05 (round 13): agy answered every worker in about
three seconds with ``"error":"Individual quota reached. Please upgrade your
subscription to increase your limits. Resets in 1h15m27s."``. The wording is a
provider signal, so each run was re-queued without spending an attempt - and
re-dispatched on the next loop tick into the same exhausted quota. Five of
those spend ``PROVIDER_ERROR_RESPAWN_CAP`` in about two minutes, and the leaf
goes terminal with ``worker_endpoint_unreachable`` an hour and thirteen minutes
before the quota would have returned.

Two rules run through everything here, and both point the same way:

* **Anything unreadable yields NO hint**, which is today's behaviour exactly.
  A parse that guesses is worse than one that abstains, because the symptom of
  an over-long deferral is a leaf sitting PENDING on a plan that reads ACTIVE
  with a null ``error`` - this repository's most expensive shape. The fallback
  it declines to is already bounded by the respawn cap.
* **The deferral is CAPPED** (:data:`MAX_DEFERRAL_HOURS`). A provider that
  answers "Resets in 900h", or a line that happens to read that way, must not
  be able to park work indefinitely.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta


#: The longest a provider hint may park a task, in hours. A ceiling rather than
#: a judgement about quotas: past it the honest answer is that nobody knows,
#: and the un-deferred re-queue is bounded already.
MAX_DEFERRAL_HOURS: int = 6

#: The same ceiling as a duration, for comparisons.
MAX_DEFERRAL: timedelta = timedelta(hours=MAX_DEFERRAL_HOURS)

#: "Resets in 1h15m27s", and the h/m/s combinations around it.
#:
#: The cue phrase is REQUIRED. A bare duration is not a reset time - a worker
#: log is full of durations ("duration_seconds", a test name, a diff) - and
#: Praxis is dogfooded on itself, so this module's own docstring travels
#: through container logs. Every unit is optional individually and the whole
#: match is rejected when they are all absent, which is what makes
#: "Resets in a while" and "Resets in 1.5h" (no integer + unit anywhere) yield
#: nothing instead of a fabricated zero.
_RESET_HINT_RE = re.compile(
    r"resets?\s+in\s+"
    r"(?:(?P<hours>\d+)\s*h)?\s*"
    r"(?:(?P<minutes>\d+)\s*m)?\s*"
    r"(?:(?P<seconds>\d+)\s*s)?",
    re.IGNORECASE,
)


def parse_reset_hint(text: str) -> timedelta | None:
    """Return the reset duration ``text`` names, or None when it names none.

    Args:
        text: One log line - in practice the evidence line
            ``provider_errors.find_provider_signal`` matched on.

    Returns:
        The duration until the provider says the quota returns, or None when
        the line carries no readable hint. Zero is None too: "Resets in 0s"
        asks for the behaviour that already exists.
    """
    match = _RESET_HINT_RE.search(text)
    if match is None:
        return None
    parts = match.groupdict()
    total = timedelta(
        hours=int(parts["hours"] or 0),
        minutes=int(parts["minutes"] or 0),
        seconds=int(parts["seconds"] or 0),
    )
    # Zero is refused, and this is the ONE guard that needs to be: every unit
    # is individually optional, so "Resets in a while" and "Resets in 1.5h"
    # match the cue with no unit at all and arrive here as zero. Answering
    # None for them is what makes an unreadable hint behave exactly like no
    # hint, rather than like a deadline that has already passed.
    return total if total > timedelta(0) else None


def deferral_deadline(text: str, *, now: datetime | None = None) -> datetime | None:
    """Return the absolute UTC instant a task may next be dispatched.

    Args:
        text: The provider-error evidence line.
        now: The clock, for tests. Defaults to the current UTC time.

    Returns:
        An aware UTC datetime, or None when there is no hint or the hint is
        longer than :data:`MAX_DEFERRAL`. None means "re-queue exactly as
        before", never "dispatch is blocked".
    """
    hint = parse_reset_hint(text)
    if hint is None or hint > MAX_DEFERRAL:
        return None
    return (now or datetime.now(UTC)) + hint


def parse_retry_after(value: object) -> datetime | None:
    """Read a stored ``tasks.provider_retry_after`` back as an aware instant.

    A naive stamp is UTC, the same rule ``core/run_elapsed`` states for
    ``agent_runs``: SQLite hands text back without a zone, and reading it as
    local time moves the deadline by the viewer's offset.

    Args:
        value: The column, which is NULL for almost every row.

    Returns:
        An aware UTC datetime, or None when the value is absent or unreadable.
        Unreadable answers None on purpose: the safe direction for a value
        nobody can parse is to run the task, never to park it forever.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def remaining_seconds(value: object, *, now: datetime | None = None) -> float | None:
    """Seconds a task still has to wait on its provider, or None if it does not.

    ONE polarity, so no caller has to decide what an absent or broken value
    means: None is "nothing is holding this task", a positive float is "this
    many seconds of provider deferral remain".

    Args:
        value: The stored ``provider_retry_after`` column.
        now: The clock, for tests. Defaults to the current UTC time.

    Returns:
        The remaining wait in seconds, or None when there is no live deferral.
    """
    deadline = parse_retry_after(value)
    if deadline is None:
        return None
    remaining = (deadline - (now or datetime.now(UTC))).total_seconds()
    return remaining if remaining > 0 else None


def is_deferred(value: object, *, now: datetime | None = None) -> bool:
    """Whether a stored deadline is still in the future."""
    return remaining_seconds(value, now=now) is not None
