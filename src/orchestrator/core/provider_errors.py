"""Shared predicates for classifying failures that are NOT about the worker.

There are THREE categories here, not two, and collapsing the third into the
first is a live hazard rather than a hypothetical one.

- **Transient provider/gateway errors** (``is_provider_error``). The endpoint
  did not answer, or a gateway refused to carry the request. A retry can fix
  it, so the caller re-queues WITHOUT consuming a retry attempt.
- **Transient provider unavailability by exception type**
  (``is_unavailability``), the brain-side equivalent.
- **Permanent worker misconfiguration** (``permanent_worker_config_error``).
  The endpoint answered immediately and refused by name something Praxis asked
  it for. Nothing about a retry changes it.

The third category exists because the first one's remedy is actively wrong for
it. ``is_provider_error`` returning True buys two things at once: the failure
is not charged to the worker's capability record (correct for a
misconfiguration), and the task is re-queued without consuming a retry
(catastrophic for one). On the reconcile path that re-queue is bounded by
``PROVIDER_ERROR_RESPAWN_CAP``; on the CALLBACK path in ``api/internal.py`` -
the path both shipped harness entrypoints actually take - it is bounded by
nothing. Measured 2026-08-27: twelve consecutive provider-error callbacks
against ``max_retries=3`` left the task at ``pending``/``attempt=1`` every
time. So a permanent condition classified as a transient one turns a task that
fails in three attempts into one that respawns a container every loop tick
forever, while the plan reads ACTIVE with a null ``error``.

**``permanent_worker_config_error`` is not wired yet.** It is a pure classifier
awaiting two call sites that this change deliberately did not touch:
``api/internal.py`` (the ``provider_error_run`` decision, which must consult it
BEFORE ``is_provider_error`` and fail the task terminally) and
``orchestrator_reconcile._resolve_failed_run_or_pause`` (same order, same
verdict). Until then the condition still charges the worker.
"""

import re

from orchestrator.core.llm_router import (
    ProviderAuthError,
    ProviderOutputError,
    ProviderRateLimitError,
)


_PROVIDER_SIGNALS: tuple[str, ...] = (
    "Forbidden: request was blocked by a gateway or proxy",
    "Error: Forbidden",
    "HTTP 403",
    "HTTP 429",
    "HTTP 502",
    "HTTP 503",
    "HTTP 504",
    "rate_limit_exceeded",
    "Too Many Requests",
    "Service Unavailable",
    "Bad Gateway",
    "Gateway Timeout",
    "Connection refused",
    "ECONNREFUSED",
    "ECONNRESET",
    "connect ENOENT",
)

_RATE_LIMIT_SIGNALS: tuple[str, ...] = (
    "rate limit",
    "usage limit",
    "too many requests",
)


def is_provider_error(text: str) -> bool:
    """Return True if the text indicates a transient provider/gateway error."""
    return any(signal in text for signal in _PROVIDER_SIGNALS)


#: The endpoint's own wording when it will not serve the model it was asked
#: for, ANCHORED to the start of a line and requiring the quoted model name.
#:
#: Anchored rather than scanned for as a substring, and that is the whole
#: design. Praxis is dogfooded on itself, so a worker's container log routinely
#: quotes this repository's source, its diffs and its test fixtures - all of
#: which contain this phrase the moment this module does. This project already
#: shipped the unanchored version of that mistake once: the protected-base
#: check ANDed two substrings over the whole worker transcript and fired on a
#: log where the two came from unrelated places. The recorded remedy was to
#: anchor on the tool's exact wording, which is what this does: a diff line
#: (``+Error: ...`` or a CONTEXT line, which begins with a space), a prose
#: mention and a quoted assertion all fail to match, because none of them
#: BEGINS its line with the error. The anchor is bare ``^`` for that reason:
#: tolerating leading whitespace would re-admit every diff context line, and
#: nothing in a real harness log is indented here.
_MODEL_NOT_SERVED_RE = re.compile(
    r'^Error: Invalid model identifier "(?P<model>[^"]+)"',
    re.MULTILINE,
)

#: SGR escape sequences, stripped before the pattern above is applied.
#:
#: Load-bearing, and measured rather than anticipated: the first version of
#: this predicate was written against a fixture that had been quoted into a
#: report with the colour codes already removed, and it did not match a single
#: real log. The bytes OpenCode actually writes are::
#:
#:     \x1b[91m\x1b[1mError: \x1b[0mInvalid model identifier "glm-4.7". ...
#:
#: so the escapes sit BETWEEN ``Error: `` and the phrase, and both the anchor
#: and the phrase itself miss. Verified against the stored ``agent_runs.logs``
#: of run ``7c7e1e1f`` on 2026-08-27: the un-stripped pattern returned False on
#: the log it was written for. Any fixture for this predicate must carry the
#: raw escapes; a sanitized one proves only that the sanitizer works.
_ANSI_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")


def permanent_worker_config_error(
    text: str, *, endpoint: str | None = None
) -> str | None:
    """Return a terminal reason when the log shows a permanent config fault.

    A permanent fault is one where the endpoint ANSWERED and refused by name
    something Praxis asked it for. The worker's capability was never
    exercised, so the failure must not be charged to it - and no retry can
    change the answer, so it must not be re-queued either.

    The live case this was written for is doubly Praxis's own doing: adaptive
    triage answered ``escalate`` and promoted a leaf to a model the configured
    endpoint does not serve, then two ``run_failed`` calibration rows were
    written against that model's record for runs in which it never ran.

    Args:
        text: The full container log for the run.
        endpoint: The worker endpoint the run was pointed at, when the caller
            knows it. Named in the reason because the fault is a property of
            the (model, endpoint) PAIR, and an operator told only "the model is
            invalid" still has to find the other half.

    Returns:
        A sentence naming the model, and the endpoint when one was supplied,
        or None when the log shows no permanent configuration fault. None is
        the answer for every ordinary worker failure and for every transient
        gateway error, both of which belong to other paths.
    """
    match = _MODEL_NOT_SERVED_RE.search(_ANSI_SGR_RE.sub("", text))
    if match is None:
        return None
    model = match.group("model")
    # Built by cases rather than by interpolating a possibly-None endpoint: an
    # f-string would put the literal "None" in front of an operator as though
    # it were a URL.
    where = f"the worker endpoint {endpoint}" if endpoint else "the worker endpoint"
    return (
        f"Permanent worker configuration fault: {where} does not serve the "
        f"model {model!r} and rejected the request by name, so the worker "
        "never ran. Retrying cannot change this. Load that model at the "
        "endpoint, or point this project (or the escalation rung that chose "
        "it) at a model the endpoint serves."
    )


def is_unavailability(exc: BaseException) -> bool:
    """Return True if the exception represents a transient provider unavailability."""
    if isinstance(exc, ProviderRateLimitError):
        # By TYPE, ahead of the wording scan below, and load-bearing: the
        # evidence for a throttle is frequently on the provider's STDOUT while
        # the exception message quotes stderr, so a message carrying no limit
        # wording at all is the normal case rather than the odd one. Reached
        # through the text branch instead, a stdout-only throttle would be
        # classified as an ordinary failure and charge the plan a retry.
        return True
    if isinstance(exc, ProviderAuthError):
        return True
    if isinstance(exc, ProviderOutputError):
        return False
    if isinstance(exc, RuntimeError):
        err_msg = str(exc)
        err_msg_lower = err_msg.lower()
        if any(signal in err_msg_lower for signal in _RATE_LIMIT_SIGNALS):
            return True
        if is_provider_error(err_msg):
            return True
    return False
