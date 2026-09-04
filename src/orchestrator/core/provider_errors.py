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
(catastrophic for one). BOTH paths are now bounded by the shared respawn cap in
``orchestrator_reconcile`` - the callback path was bounded by nothing until
2026-08-27, when twelve consecutive provider-error callbacks against
``max_retries=3`` were measured leaving the task at ``pending``/``attempt=1``
every time. Being bounded makes a misclassification survivable; it does not make
it right, because the cap's terminal message names an endpoint that is
*unreachable*, which is a false statement about one that answered.

**``permanent_worker_config_error`` is NOT wired, and its premise was REFUTED
the day it was written. Do not wire it without new evidence.** It reads
``Error: Invalid model identifier "<model>"`` as PERMANENT. Measured hours later
on the same rig: the same model and the same endpoint that produced that line at
02:19 served real agentic work at 06:03. The model is absent from BOTH
``/api/v0/models`` and ``/v1/models`` and yet the completions endpoint accepts
it, so the model-list probe is not authoritative about what the endpoint will
serve and the refusal is a transient not-currently-loaded state.

(That endpoint is named in prose here rather than by its path on purpose:
``tests/test_thinking_explicit.py`` scans every ``.py`` file for the path and
requires an explicit ``reasoning_effort`` nearby. It strips ``#`` comments but
not docstrings, so writing the literal path in prose reports this module as a
payload site that never states its thinking level. The detector is right to err
that way - a false positive is loud, a false negative is silent - so prose gives
way, not the guard.)

Wiring it as written would fail tasks TERMINALLY on a transient condition, which
is strictly worse than the mis-attribution it was built to fix. What it would
need first is a test that distinguishes a permanently-absent model from a
not-currently-loaded one, and this module has no such signal today. The
attribution complaint behind it remains valid and unaddressed: a model the
endpoint refused by name never ran, so charging its capability record is wrong
whatever the duration.
"""

import re
from dataclasses import dataclass

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


#: Lines the harness ENTRYPOINT writes about its own callback to the
#: orchestrator, verbatim from ``docker/*/entrypoint.sh``. They carry the HTTP
#: status the ORCHESTRATOR (or a proxy in front of it) answered, so ``HTTP
#: 503`` on one of them says nothing about the model endpoint: the worker may
#: have finished its work and then failed to report it. Scanning them as
#: evidence classified the run as a worker-endpoint outage and re-queued it
#: without spending an attempt, which is the remedy for a different fault.
#: Matched on the exact prefix and only at line start; a diff context line
#: quoting the entrypoint begins with a space and is not excluded, on purpose,
#: because widening the exclusion is how a real signal starts getting skipped.
_CALLBACK_REPORT_PREFIXES: tuple[str, ...] = (
    "WARNING: callback attempt ",
    "ERROR: callback failed after ",
)

#: Longest evidence line carried into feedback, events and log lines. A worker
#: log line can be a whole JSON payload; the operator needs enough to find it.
_EVIDENCE_LIMIT = 300


@dataclass(frozen=True)
class ProviderSignal:
    """Which provider-error signal matched, and the log line it matched on.

    Both halves are carried to every surface that acts on the classification,
    because a re-queue that names only the worker's own reason ("Agent
    finished with status failed", which every non-zero exit produces) cannot
    be checked by anyone: the reason is not the evidence.
    """

    signal: str
    line: str


def find_provider_signal(text: str) -> ProviderSignal | None:
    """Return the first provider/gateway signal in ``text`` with its line.

    Args:
        text: Full container log text.

    Returns:
        The matched signal and the (stripped, bounded) line it appeared on,
        or None when no line carries one. The entrypoint's own callback
        report lines are skipped: see ``_CALLBACK_REPORT_PREFIXES``.
    """
    for raw_line in text.splitlines():
        if raw_line.startswith(_CALLBACK_REPORT_PREFIXES):
            continue
        for signal in _PROVIDER_SIGNALS:
            if signal in raw_line:
                return ProviderSignal(
                    signal=signal, line=raw_line.strip()[:_EVIDENCE_LIMIT]
                )
    return None


def is_provider_error(text: str) -> bool:
    """Return True if the text indicates a transient provider/gateway error."""
    return find_provider_signal(text) is not None


def provider_error_feedback(found: ProviderSignal, original_reason: str) -> str:
    """The sentence stored on the re-queued task, and injected into the next prompt.

    ``tasks.review_feedback`` is worker-facing guidance (the Bible injects it
    into the next attempt), so the ACTION comes first, the evidence second for
    the human reading the same column, and the worker's own reason last so it
    is never lost.

    Args:
        found: The matched signal and its line.
        original_reason: What the worker or the reconciler reported.

    Returns:
        One paragraph, action first.
    """
    # Bounded here as well as in ``find_provider_signal``: this string is
    # injected into the next worker prompt, so a caller constructing the
    # signal by hand must not be able to hand the worker a 5 KB log line.
    line = found.line[:_EVIDENCE_LIMIT]
    return (
        "Start this task again from the beginning; assume nothing from the "
        "previous attempt exists. That attempt ended in a transient error from "
        "the model endpoint or a gateway in front of it, which is not a fault "
        f"in the work and was not charged as an attempt (matched {found.signal!r} "
        f"in the worker log line {line!r}). "
        f"The reported reason was: {original_reason}"
    )


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
