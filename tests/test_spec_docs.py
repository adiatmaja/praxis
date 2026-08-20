"""Unit tests for rendering a submitted spec as a repo doc."""
# ruff: noqa: S101

from __future__ import annotations

from datetime import date

from orchestrator.core.spec_docs import (
    render_spec_doc,
    spec_doc_path,
    spec_slug,
    spec_title,
)


def test_title_uses_the_first_meaningful_line() -> None:
    assert spec_title("\n\n  Add rate limiting\nmore detail\n") == "Add rate limiting"


def test_title_unwraps_a_markdown_heading() -> None:
    assert spec_title("# Add rate limiting\n\nbody") == "Add rate limiting"


def test_title_is_truncated() -> None:
    title = spec_title("x" * 200)
    assert len(title) <= 75
    assert title.endswith("...")


def test_slug_is_url_safe() -> None:
    assert spec_slug("Add rate limiting to /api/login!") == (
        "add-rate-limiting-to-api-login"
    )


def test_slug_falls_back_when_nothing_survives() -> None:
    assert spec_slug("!!! ???") == "spec"


def test_doc_path_is_dated_unique_and_under_specs() -> None:
    path = spec_doc_path("Add auth", today=date(2026, 8, 20), unique="deadbeef")
    assert path == "docs/superpowers/specs/2026-08-20-add-auth-deadbeef.md"


def test_doc_path_is_unique_per_submission() -> None:
    first = spec_doc_path("Add auth", today=date(2026, 8, 20), unique="aaaaaaaa")
    second = spec_doc_path("Add auth", today=date(2026, 8, 20), unique="bbbbbbbb")
    assert first != second


def test_rendered_doc_embeds_the_spec_verbatim() -> None:
    spec = "Add auth.\n\n- use OAuth\n- store nothing in `localStorage`\n"
    doc = render_spec_doc(spec)
    assert doc.startswith("---\ntype: spec\n")
    assert spec.strip() in doc


def test_rendered_doc_survives_a_multiline_spec_with_code_fences() -> None:
    spec = "Add auth\n\n```python\ndef login():\n    ...\n```\n"
    assert "```python\ndef login():" in render_spec_doc(spec)
