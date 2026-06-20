from orchestrator.core.markdown_utils import (
    checklist_progress,
    classify_by_marker,
    content_hash,
    extract_title,
)


def test_content_hash_stable():
    assert content_hash("abc") == content_hash("abc")
    assert content_hash("abc") != content_hash("abd")


def test_extract_title_from_h1():
    assert extract_title("# My Spec\n\nbody") == "My Spec"


def test_extract_title_none_when_absent():
    assert extract_title("no heading here") is None


def test_checklist_progress_counts_checkboxes():
    md = "- [x] done\n- [ ] todo\n- [X] also done\nnot a box"
    assert checklist_progress(md) == (2, 3)


def test_checklist_progress_zero_when_none():
    assert checklist_progress("plain text") == (0, 0)


def test_classify_plan_dir():
    assert classify_by_marker("docs/superpowers/plans/x.md", "# x") == "plan"


def test_classify_spec_dir():
    assert classify_by_marker("docs/superpowers/specs/x.md", "# x") == "spec"


def test_classify_plan_by_checklist():
    assert classify_by_marker("docs/notes/x.md", "## Tasks\n- [ ] do it") == "plan"


def test_classify_frontmatter_type():
    assert classify_by_marker("docs/x.md", "---\ntype: spec\n---\n# x") == "spec"


def test_classify_ambiguous_returns_none():
    assert classify_by_marker("docs/random.md", "just prose") is None
