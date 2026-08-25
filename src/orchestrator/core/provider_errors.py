"""Shared predicates for identifying transient provider and gateway errors."""

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
