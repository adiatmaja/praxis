import pytest

from orchestrator.core.context_scrub import scrub_context


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
    assert len(out) <= 10_100  # cap + truncation notice
    assert "truncated" in out.lower()


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
    assert "https://api.example.com" in out


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
