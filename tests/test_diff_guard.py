import pytest

from orchestrator.core.diff_guard import (
    added_dependencies,
    destructive_deletions,
    detect_secrets,
)


@pytest.mark.unit
def test_flags_large_deletion_from_existing_file():
    diff = "\n".join(
        ["--- a/.env.example", "+++ b/.env.example"]
        + [f"-LINE{i}" for i in range(70)]
        + ["+KEY=1"]
    )
    flagged = destructive_deletions(diff, threshold=40)
    assert ".env.example" in flagged


@pytest.mark.unit
def test_ignores_new_file_and_small_edits():
    diff = "\n".join(
        ["--- /dev/null", "+++ b/new.py"] + [f"+LINE{i}" for i in range(70)]
    )
    assert destructive_deletions(diff, threshold=40) == []


@pytest.mark.unit
def test_delete_and_replace_refactor_not_flagged():
    """A file with many deletions but equal or more additions is a refactor, not truncation."""
    # 60 deleted lines, 65 added lines => net addition => not flagged
    diff = "\n".join(
        ["--- a/dispatch.py", "+++ b/dispatch.py"]
        + [f"-OLD{i}" for i in range(60)]
        + [f"+NEW{i}" for i in range(65)]
    )
    flagged = destructive_deletions(diff, threshold=40)
    assert "dispatch.py" not in flagged


@pytest.mark.unit
def test_diff_with_more_deletions_than_additions_is_flagged():
    """Net-deletion diff with large per-file removal is flagged."""
    # 60 deleted, 5 added => net deletion => flagged
    diff = "\n".join(
        ["--- a/big_file.py", "+++ b/big_file.py"]
        + [f"-DEL{i}" for i in range(60)]
        + [f"+ADD{i}" for i in range(5)]
    )
    flagged = destructive_deletions(diff, threshold=40)
    assert "big_file.py" in flagged


@pytest.mark.unit
def test_multiple_files_mixed_refactor():
    """Only net-shrinking files with large deletions are flagged in a mixed diff."""
    diff = "\n".join(
        # file1: 50 del, 55 add => refactor, not flagged
        ["--- a/file1.py", "+++ b/file1.py"]
        + [f"-D{i}" for i in range(50)]
        + [f"+A{i}" for i in range(55)]
        +
        # file2: 50 del, 2 add => truncation, flagged
        ["--- a/file2.py", "+++ b/file2.py"]
        + [f"-X{i}" for i in range(50)]
        + ["+Y=1", "+Z=2"]
    )
    flagged = destructive_deletions(diff, threshold=40)
    # net total: 100 del, 57 add => net deletion; file2 is flagged, file1 has near-match adds
    assert "file2.py" in flagged
    assert "file1.py" not in flagged


@pytest.mark.unit
def test_threshold_below_deletion_count_not_exceeded():
    # 30 deletions, threshold=40 => not flagged regardless of additions
    diff = "\n".join(
        ["--- a/small.py", "+++ b/small.py"] + [f"-L{i}" for i in range(30)]
    )
    assert destructive_deletions(diff, threshold=40) == []


# ---------------------------------------------------------------------------
# added_dependencies
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_added_dependencies_flags_pip_requirements_txt():
    diff = "\n".join(
        ["--- a/requirements.txt", "+++ b/requirements.txt"] + ["+requests>=2.31.0"]
    )
    result = added_dependencies(diff)
    assert "requirements.txt" in result


@pytest.mark.unit
def test_added_dependencies_flags_pyproject_toml():
    diff = "\n".join(
        ["--- a/pyproject.toml", "+++ b/pyproject.toml", '+dependencies = ["new-pkg"]']
    )
    result = added_dependencies(diff)
    assert "pyproject.toml" in result


@pytest.mark.unit
def test_added_dependencies_flags_package_json():
    diff = "\n".join(
        ["--- a/package.json", "+++ b/package.json", '+"new-pkg": "^1.0.0"']
    )
    result = added_dependencies(diff)
    assert "package.json" in result


@pytest.mark.unit
def test_added_dependencies_flags_gemfile():
    diff = "\n".join(["--- a/Gemfile", "+++ b/Gemfile", '+gem "rails"'])
    result = added_dependencies(diff)
    assert "Gemfile" in result


@pytest.mark.unit
def test_added_dependencies_ignores_removed_line():
    diff = "\n".join(
        ["--- a/requirements.txt", "+++ b/requirements.txt", "-requests>=2.31.0"]
    )
    result = added_dependencies(diff)
    assert result == []


@pytest.mark.unit
def test_added_dependencies_ignores_non_manifest_path():
    diff = "\n".join(["--- a/src/main.py", "+++ b/src/main.py", "+import requests"])
    result = added_dependencies(diff)
    assert result == []


@pytest.mark.unit
def test_added_dependencies_ignores_lockfile_sha256_digests():
    """Lockfile sha256 digests look random but are NOT new dependencies."""
    diff = "\n".join(
        [
            "--- a/pipfile.lock",
            "+++ b/pipfile.lock",
            '+    "sha256:a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",',
        ]
    )
    result = added_dependencies(diff)
    assert result == []


# ---------------------------------------------------------------------------
# detect_secrets
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_detect_secrets_private_key_block():
    diff = "\n".join(
        [
            "--- a/config.yml",
            "+++ b/config.yml",
            "+-----BEGIN RSA PRIVATE KEY-----",
            "+MIIEpAIBAAKCAQEA0Z3VS5JJcds3xfn/ygWyF...",
            "+-----END RSA PRIVATE KEY-----",
        ]
    )
    result = detect_secrets(diff)
    assert "config.yml" in result


@pytest.mark.unit
def test_detect_secrets_aws_access_key():
    diff = "\n".join(
        ["--- a/env.txt", "+++ b/env.txt", "+AWS_KEY=AKIAIOSFODNN7EXAMPLE"]
    )
    result = detect_secrets(diff)
    assert "env.txt" in result


@pytest.mark.unit
def test_detect_secrets_github_token():
    diff = "\n".join(
        [
            "--- a/script.sh",
            "+++ b/script.sh",
            '+TOKEN="ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"',
        ]
    )
    result = detect_secrets(diff)
    assert "script.sh" in result


@pytest.mark.unit
def test_detect_secrets_slack_token():
    diff = "\n".join(
        [
            "--- a/config.json",
            "+++ b/config.json",
            '+  "slack_token": "xoxb-123-456-abc"',
        ]
    )
    result = detect_secrets(diff)
    assert "config.json" in result


@pytest.mark.unit
def test_detect_secrets_openai_key():
    diff = "\n".join(
        ["--- a/.env", "+++ b/.env", "+OPENAI_API_KEY=sk-abc123def456ghi789jkl012"]
    )
    result = detect_secrets(diff)
    assert ".env" in result


@pytest.mark.unit
def test_detect_secrets_password_assignment():
    diff = "\n".join(
        ["--- a/settings.py", "+++ b/settings.py", '+password = "super_secret_123"']
    )
    result = detect_secrets(diff)
    assert "settings.py" in result


@pytest.mark.unit
def test_detect_secrets_api_key_assignment():
    diff = "\n".join(
        ["--- a/config.py", "+++ b/config.py", '+api_key = "abcdef123456"']
    )
    result = detect_secrets(diff)
    assert "config.py" in result


@pytest.mark.unit
def test_detect_secrets_ignores_removed_secret():
    """Removed lines should not trigger — secrets are being taken out."""
    diff = "\n".join(["--- a/.env", "+++ b/.env", "-AWS_KEY=AKIAIOSFODNN7EXAMPLE"])
    result = detect_secrets(diff)
    assert result == []


@pytest.mark.unit
def test_detect_secrets_ignores_lockfile_digests():
    """sha256 digests in lockfiles are high-entropy but NOT secrets."""
    diff = "\n".join(
        [
            "--- a/pipfile.lock",
            "+++ b/pipfile.lock",
            '+    "sha256:a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",',
        ]
    )
    result = detect_secrets(diff)
    assert result == []


@pytest.mark.unit
def test_detect_secrets_ignores_context_lines():
    """Unchanged context lines (no +/- prefix) should not be flagged."""
    diff = "\n".join(
        [
            "--- a/notes.txt",
            "+++ b/notes.txt",
            "+some change",
            " AKIAIOSFODNN7EXAMPLE",  # context line, not added
        ]
    )
    result = detect_secrets(diff)
    assert result == []
