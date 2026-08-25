import pytest

from orchestrator.core.context_scrub import (
    _DEFAULT_MAX_CHARS,
    _TYPICAL_LOCAL_MODEL_WINDOW_TOKENS,
    resolve_scrub_cap,
    scrub_context,
)
from orchestrator.core.token_budget import worker_budget


@pytest.mark.unit
def test_redacts_env_assignments():
    raw = "Use the db.\nAPI_KEY=ghp_abcdef1234567890abcdef1234567890abcd\nDone."
    out = scrub_context(raw)
    assert "ghp_abcdef1234567890abcdef1234567890abcd" not in out
    assert "[REDACTED]" in out
    assert "Use the db." in out  # non-secret prose preserved


@pytest.mark.unit
def test_redacts_known_token_shapes():
    raw = "token sk-ABCDEFGHIJKLMNOPQRSTUVWX and AKIAIOSFODNN7EXAMPLE here"
    out = scrub_context(raw)
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWX" not in out
    assert "AKIAIOSFODNN7EXAMPLE" not in out


@pytest.mark.unit
def test_redacts_private_key_block():
    raw = "-----BEGIN PRIVATE KEY-----\nMIIEv...\n-----END PRIVATE KEY-----"
    out = scrub_context(raw)
    assert "MIIEv" not in out
    assert "[REDACTED PRIVATE KEY]" in out


@pytest.mark.unit
def test_caps_size():
    raw = "x" * 50_000
    out = scrub_context(raw, max_chars=10_000)
    assert out.startswith("x" * 10_000)
    assert "x" * 10_001 not in out  # nothing beyond the cap survives
    assert "truncated" in out.lower()


@pytest.mark.unit
def test_resolve_scrub_cap_unknown_window_uses_conservative_default():
    """None means nobody could establish a window (core/context_window.py);
    this must still cap, not go unlimited, and must say WHY out loud."""
    cap = resolve_scrub_cap(None)
    assert cap.max_chars == _DEFAULT_MAX_CHARS
    assert "unknown" in cap.reason


@pytest.mark.unit
def test_resolve_scrub_cap_typical_local_window_matches_todays_flat_cap():
    """Pin the current number: an 8K LM Studio project (the shipped
    ``capability.default.context_window``) must land on the exact cap this
    module has always used, or a future ratio change silently resizes every
    existing install without a single test noticing."""
    cap = resolve_scrub_cap(_TYPICAL_LOCAL_MODEL_WINDOW_TOKENS)
    assert cap.max_chars == _DEFAULT_MAX_CHARS == 12_000


@pytest.mark.unit
def test_resolve_scrub_cap_large_window_grows_well_past_the_default():
    """The reporter's case: a declared million-token window must not leave
    the worker sized like an 8K local model. Pin the exact figure so a
    ratio change is caught, not just "some number bigger than 12000"."""
    cap = resolve_scrub_cap(1_000_000)
    assert cap.max_chars == worker_budget(1_000_000) * 4
    assert cap.max_chars == 3_868_928
    assert cap.max_chars > _DEFAULT_MAX_CHARS


@pytest.mark.unit
def test_a_14kb_spec_survives_intact_on_a_million_token_window():
    """The reporter's exact shape: a legitimately-sized spec must pass through
    a large-window cap untouched, not truncated to what an 8K floor model
    would need. 14 409 chars mirrors the reported instructions body."""
    raw = "y" * 14_409
    cap = resolve_scrub_cap(1_000_000)
    out = scrub_context(raw, cap.max_chars, cap_reason=cap.reason)
    assert out == raw
    assert "truncated" not in out.lower()


@pytest.mark.unit
def test_the_same_14kb_spec_is_truncated_on_a_typical_local_window():
    """Same input, an 8K window: today's flat cap still bites, unchanged."""
    raw = "y" * 14_409
    cap = resolve_scrub_cap(_TYPICAL_LOCAL_MODEL_WINDOW_TOKENS)
    out = scrub_context(raw, cap.max_chars, cap_reason=cap.reason)
    assert out.startswith("y" * 12_000)
    assert "y" * 12_001 not in out
    assert "truncated" in out.lower()


@pytest.mark.unit
def test_truncation_notice_names_the_cap_and_the_reason():
    """A doctor-style diagnostic: the notice must name the cause AND the cap,
    not just announce that something was cut."""
    raw = "z" * 100
    out = scrub_context(raw, max_chars=10, cap_reason="a 512-token context window")
    assert "10" in out
    assert "a 512-token context window" in out


@pytest.mark.unit
def test_none_and_empty():
    assert scrub_context(None) is None
    assert scrub_context("   ") is None


@pytest.mark.unit
def test_preserves_lowercase_yaml_prose():
    raw = "host: localhost\npath: /usr/local/bin\nurl: https://api.example.com"
    out = scrub_context(raw)
    assert "localhost" in out
    assert "/usr/local/bin" in out
    assert "https:" + "//api.example.com" in out


@pytest.mark.unit
def test_redacts_bare_env_assignment():
    raw = "SOME_SECRET=notarecognizedtokenprefix12345"
    out = scrub_context(raw)
    assert "notarecognizedtokenprefix12345" not in out
    assert "SOME_SECRET=[REDACTED]" in out


@pytest.mark.unit
def test_redacts_modern_openai_anthropic_keys():
    raw = "key sk-proj-ABCDEFGHIJKLMNOP1234 and sk-ant-api03-ABCDEFGHIJKLMNOP done"
    out = scrub_context(raw)
    assert "sk-proj-ABCDEFGHIJKLMNOP1234" not in out
    assert "sk-ant-api03-ABCDEFGHIJKLMNOP" not in out


@pytest.mark.unit
def test_preserves_url_and_path_config_values():
    raw = "LM_STUDIO_URL: http://host.docker.internal:1234/v1\nWORKSPACE: /home/agent/workspace"
    out = scrub_context(raw)
    assert "http://host.docker.internal:1234/v1" in out
    assert "/home/agent/workspace" in out
