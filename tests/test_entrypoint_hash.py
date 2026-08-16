"""Tests for the entrypoint content hash used by the staleness check."""

from pathlib import Path

from orchestrator.core.entrypoint_hash import LABEL_KEY, hash_entrypoint


def test_hash_is_stable_for_identical_content(tmp_path: Path) -> None:
    a = tmp_path / "a.sh"
    b = tmp_path / "b.sh"
    a.write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
    b.write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
    assert hash_entrypoint(a) == hash_entrypoint(b)


def test_hash_ignores_mtime(tmp_path: Path) -> None:
    """The whole point: a re-checkout changes mtime, not content."""
    script = tmp_path / "entrypoint.sh"
    script.write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
    first = hash_entrypoint(script)
    import os

    os.utime(script, (0, 0))
    assert hash_entrypoint(script) == first


def test_hash_changes_when_content_changes(tmp_path: Path) -> None:
    script = tmp_path / "entrypoint.sh"
    script.write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
    first = hash_entrypoint(script)
    script.write_text("#!/bin/bash\necho CHANGED\n", encoding="utf-8")
    assert hash_entrypoint(script) != first


def test_hash_normalizes_line_endings(tmp_path: Path) -> None:
    """A CRLF checkout on Windows must hash the same as LF in the image.

    Without this the check would be red on every Windows clone for the
    opposite reason it is red today.
    """
    lf = tmp_path / "lf.sh"
    crlf = tmp_path / "crlf.sh"
    lf.write_bytes(b"#!/bin/bash\necho hi\n")
    crlf.write_bytes(b"#!/bin/bash\r\necho hi\r\n")
    assert hash_entrypoint(lf) == hash_entrypoint(crlf)


def test_missing_file_returns_none(tmp_path: Path) -> None:
    assert hash_entrypoint(tmp_path / "nope.sh") is None


def test_label_key_is_namespaced() -> None:
    assert LABEL_KEY == "org.praxis.entrypoint-sha256"
