from orchestrator.core.llm_router import ProviderAuthError, ProviderOutputError
from orchestrator.core.provider_errors import is_provider_error, is_unavailability


def test_is_provider_error_gateway_signals_detected():
    assert is_provider_error("Something went wrong. HTTP 502 Bad Gateway")
    assert is_provider_error("Forbidden: request was blocked by a gateway or proxy")
    assert is_provider_error("Connection refused at 127.0.0.1")


def test_is_provider_error_normal_output_ignored():
    assert not is_provider_error("Successfully completed task")
    assert not is_provider_error("RuntimeError: Invalid argument")


def test_is_unavailability_auth_error_is_unavailability():
    assert is_unavailability(ProviderAuthError("Authentication failed"))


def test_is_unavailability_rate_limit_runtime_error_is_unavailability():
    assert is_unavailability(RuntimeError("API error: rate limit exceeded"))
    assert is_unavailability(RuntimeError("Service Unavailable"))


def test_is_unavailability_bad_output_provider_output_error_is_not_unavailability():
    assert not is_unavailability(ProviderOutputError("Malformed JSON response"))
