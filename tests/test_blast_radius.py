"""How widely used is the thing this diff changed?

The defect this exists for: a worker changed ``.s-alert`` from a flex container
to ``display: block``. The change was correct. Three hundred lines further down
the same stylesheet a documented modifier on ``.s-alert`` used
``justify-content: center``, which only applies inside a flex container, so it
went silently inert and a disclaimer moved from centred to left-aligned on three
pages.

The reviewer passed it, and every statement in its feedback was true. The defect
was not present in the diff, and nothing in the five changed lines indicates that
a property in a different selector, in a block the diff never shows, has become a
no-op. A check that structurally cannot observe the defect class is worse than no
check, because its green reads as verification.

So every test here asserts a POSITIVE fact: this identifier, this count. A test
that merely survives is indistinguishable from one that passes because the
feature did nothing.
"""
# ruff: noqa: S101

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orchestrator.core.blast_radius import (
    Occurrence,
    count_occurrences,
    extract_identifiers,
    measure_blast_radius,
    render_blast_radius,
)


# The shape the field report carried, kept realistic rather than minimal: a
# rule head changed on both sides of the diff, a declaration line inside it,
# and a CONTEXT line naming a second selector that this diff did not change.
_REALISTIC_DIFF = """\
diff --git a/styles.css b/styles.css
index 1111111..2222222 100644
--- a/styles.css
+++ b/styles.css
@@ -120,7 +120,7 @@
-.s-alert { display: flex; gap: var(--sp-2); align-items: flex-start; }
+.s-alert { line-height: 1.5; display: block; }
   color: var(--ink);
 .mtel-demo-banner { justify-content: center; }
diff --git a/render.py b/render.py
index 3333333..4444444 100644
--- a/render.py
+++ b/render.py
@@ -8,3 +8,4 @@
+def render_alert(body: str) -> str:
+    return body
 def untouched_helper() -> None:
"""


@pytest.mark.unit
def test_extraction_finds_a_css_selector_and_a_definition_from_a_real_diff():
    """The two shapes the task scopes to, from changed lines only."""
    found = extract_identifiers(_REALISTIC_DIFF)

    assert ".s-alert" in found
    assert "render_alert" in found


@pytest.mark.unit
def test_extraction_ignores_unchanged_context_lines():
    """``.mtel-demo-banner`` and ``untouched_helper`` are context, not change.

    Both appear in the diff text on lines beginning with a space. Extracting
    them would report the reach of code this task did not touch, and on the
    field-report diff it would have named the very selector that broke as
    something the worker changed.
    """
    found = extract_identifiers(_REALISTIC_DIFF)

    assert ".mtel-demo-banner" not in found
    assert "untouched_helper" not in found


@pytest.mark.unit
def test_extraction_does_not_mistake_a_brace_opening_line_for_a_css_rule():
    """A rule head is a selector list before ``{``, not anything before ``{``.

    ``config.defaults = {`` ends in a brace and contains ``.defaults``. Counting
    that as a CSS selector would put a made-up identifier and a made-up count in
    front of the reviewer, which is the same class of false statement the whole
    section exists to remove.
    """
    diff = (
        "--- a/app.js\n"
        "+++ b/app.js\n"
        "+config.defaults = {\n"
        "+if (ready) {\n"
        "+function boot() {\n"
    )

    found = extract_identifiers(diff)

    assert ".defaults" not in found
    assert "boot" in found


@pytest.mark.unit
def test_extraction_finds_the_javascript_definition_shapes():
    """``class``, ``const X =`` and ``export default X``, per the task scope."""
    diff = (
        "--- a/m.js\n"
        "+++ b/m.js\n"
        "+export const HEADER_HEIGHT = 48;\n"
        "+class AlertBanner {\n"
        "+export default alertRegistry;\n"
    )

    found = extract_identifiers(diff)

    assert "HEADER_HEIGHT" in found
    assert "AlertBanner" in found
    assert "alertRegistry" in found


@pytest.mark.unit
def test_counting_is_correct_over_a_small_tree(tmp_path: Path):
    """A real number over a real tree, not "it did not crash"."""
    (tmp_path / "styles.css").write_text(
        ".s-alert { display: block; }\n.s-alert p { margin: 0; }\n",
        encoding="utf-8",
    )
    (tmp_path / "docs.md").write_text(
        "The `.s-alert` component is shared.\n", encoding="utf-8"
    )

    counts = count_occurrences(tmp_path, [".s-alert"])

    assert counts.counts[".s-alert"] == 3
    assert counts.complete is True


@pytest.mark.unit
def test_counting_skips_a_binary_file_by_extension_and_by_null_byte(tmp_path: Path):
    """The field repo was 70 MB, mostly PNGs.

    Both guards are asserted through the NUMBER, not through a log line: a
    binary counted would make this 4 rather than 2, and a mere "did not raise"
    assertion would pass with either guard deleted.
    """
    (tmp_path / "a.css").write_text(
        ".s-alert { display: block; }\n.s-alert p {}\n", encoding="utf-8"
    )
    # Skipped by extension: real text inside a file no reviewer would read.
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n.s-alert\n")
    # Skipped by the null-byte sniff: an innocent extension, binary content.
    (tmp_path / "index.dat.txt").write_bytes(b".s-alert\x00\x00binary")

    counts = count_occurrences(tmp_path, [".s-alert"])

    assert counts.counts[".s-alert"] == 2


@pytest.mark.unit
def test_a_selector_prefix_is_not_counted_as_the_selector(tmp_path: Path):
    """``.s-alert-danger`` is a different class, not a use of ``.s-alert``.

    Plain substring counting inflated every short selector, and an inflated
    count is a claim about the repository that the reviewer cannot check.
    """
    (tmp_path / "a.css").write_text(
        ".s-alert {}\n.s-alert-danger {}\n.s-alert_wide {}\n", encoding="utf-8"
    )

    counts = count_occurrences(tmp_path, [".s-alert"])

    assert counts.counts[".s-alert"] == 1


@pytest.mark.unit
def test_the_git_directory_is_never_walked(tmp_path: Path):
    """``.git`` alone can hold more objects than the whole source tree."""
    (tmp_path / "a.css").write_text(".s-alert {}\n", encoding="utf-8")
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "COMMIT_EDITMSG").write_text(
        ".s-alert .s-alert .s-alert\n", encoding="utf-8"
    )

    counts = count_occurrences(tmp_path, [".s-alert"])

    assert counts.counts[".s-alert"] == 1


@pytest.mark.unit
def test_a_bare_clone_directory_is_never_walked(tmp_path: Path):
    """A bare clone is ``<name>.git``, which an exact-name set cannot match.

    Found by running this module against the praxis checkout itself:
    ``bench/.work/repos`` holds bare clones of SWE-bench repositories, 2.4 GB of
    pack files under directory names ending in ``.git`` rather than named
    ``.git``. The walk read them, blew its byte budget in a quarter of a second,
    and reported every count as a lower bound.
    """
    (tmp_path / "a.css").write_text(".s-alert {}\n", encoding="utf-8")
    bare = tmp_path / "bench" / "django__django-11815.git" / "objects"
    bare.mkdir(parents=True)
    (bare / "notes").write_text(".s-alert .s-alert .s-alert\n", encoding="utf-8")

    counts = count_occurrences(tmp_path, [".s-alert"])

    assert counts.counts[".s-alert"] == 1
    assert counts.complete is True


@pytest.mark.unit
def test_an_oversize_file_is_skipped_rather_than_truncated(tmp_path: Path):
    """Reading the first 512 KB of a 400 MB pack counts noise and ends the walk.

    Asserted through the byte budget, which is the fact that actually bit: with
    a two-file budget, a truncated giant consumes one of the two slots and the
    real source file after it is never reached. Skipping it leaves the budget
    for the file whose counts matter.
    """
    from orchestrator.core.blast_radius import MAX_FILE_BYTES

    # Sorted walk order puts the giant FIRST, so a truncating reader spends its
    # budget before it ever opens the stylesheet.
    (tmp_path / "aaa_giant.css").write_text(
        ".s-alert {}\n" + ("/* pad */\n" * (MAX_FILE_BYTES // 10)), encoding="utf-8"
    )
    (tmp_path / "zzz_real.css").write_text(
        ".s-alert {}\n.s-alert p {}\n", encoding="utf-8"
    )

    counts = count_occurrences(tmp_path, [".s-alert"], max_total_bytes=200_000)

    assert counts.counts[".s-alert"] == 2
    assert counts.complete is True


@pytest.mark.unit
def test_the_byte_budget_stops_the_walk_and_the_result_admits_it(tmp_path: Path):
    """The cap that keeps a 70 MB checkout off the review's critical path.

    Two facts, and the second is the one that matters: the walk STOPS, and the
    result says ``complete=False`` so the renderer writes "at least". A budget
    that stopped the walk while still reporting an exact count would state an
    under-count as fact, which is the failure this whole module exists to
    remove, one level down.
    """
    for name in ("a.css", "b.css", "c.css"):
        (tmp_path / name).write_text(".s-alert {}\n" + ("x" * 4000), encoding="utf-8")

    counts = count_occurrences(tmp_path, [".s-alert"], max_total_bytes=3000)

    assert counts.complete is False
    # The budget is checked BEFORE each read, so one 4012-byte file is read and
    # the walk stops in front of the second. Three would mean the budget was
    # never consulted at all.
    assert counts.counts[".s-alert"] == 1


@pytest.mark.unit
def test_the_file_count_budget_stops_the_walk_too(tmp_path: Path):
    """The sibling cap: many tiny files never trip the byte budget."""
    for index in range(10):
        (tmp_path / f"f{index}.css").write_text(".s-alert {}\n", encoding="utf-8")

    counts = count_occurrences(tmp_path, [".s-alert"], max_files=3)

    assert counts.complete is False
    assert counts.counts[".s-alert"] == 3


@pytest.mark.unit
def test_an_identifier_used_only_where_it_was_defined_is_dropped(tmp_path: Path):
    """One occurrence is the definition itself, and carries no signal."""
    (tmp_path / "m.py").write_text(
        "def render_alert() -> None:\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "shared.css").write_text(
        ".s-alert {}\n.s-alert p {}\n", encoding="utf-8"
    )
    diff = (
        "--- a/m.py\n"
        "+++ b/m.py\n"
        "+def render_alert() -> None:\n"
        "--- a/shared.css\n"
        "+++ b/shared.css\n"
        "+.s-alert {}\n"
    )

    result = measure_blast_radius(diff, tmp_path)

    assert [o.identifier for o in result.occurrences] == [".s-alert"]
    assert result.occurrences[0].count == 2


@pytest.mark.unit
def test_the_report_is_capped_and_ordered_by_count(tmp_path: Path):
    """Top N by count, so the widest-reaching identifier is never cut."""
    body = "\n".join(f".c{i} " * (30 - i) for i in range(20))
    (tmp_path / "a.css").write_text(body, encoding="utf-8")
    diff = "--- a/a.css\n+++ b/a.css\n" + "".join(f"+.c{i} {{}}\n" for i in range(20))

    result = measure_blast_radius(diff, tmp_path)

    assert len(result.occurrences) == 10
    assert result.occurrences[0].identifier == ".c0"
    counts = [o.count for o in result.occurrences]
    assert counts == sorted(counts, reverse=True)


@pytest.mark.unit
def test_the_rendered_section_names_the_identifier_and_its_count():
    """The register the reporter asked for, verbatim enough to be checkable."""
    rendered = render_blast_radius(
        _radius((Occurrence(".s-alert", 373), Occurrence("render_alert", 12)))
    )

    assert "`.s-alert` occurs 373 times" in rendered
    assert "`render_alert` occurs 12 times" in rendered
    assert "the diff does not show" in rendered


@pytest.mark.unit
def test_a_truncated_walk_says_at_least_rather_than_an_exact_number():
    """A cap reached under-states reach; stating it exactly would be a lie."""
    rendered = render_blast_radius(
        _radius((Occurrence(".s-alert", 373),), complete=False)
    )

    assert "occurs at least 373 times" in rendered


@pytest.mark.unit
def test_rendering_nothing_produces_an_empty_string_not_an_empty_heading():
    """The caller turns "" into the prompt's neutral line.

    An empty heading in the prompt reads as "we looked and the change is
    contained", which is the misleading-green failure again.
    """
    assert render_blast_radius(_radius(())) == ""


@pytest.mark.unit
def test_measuring_a_diff_with_no_extractable_identifier_never_walks(
    tmp_path: Path, monkeypatch
):
    """A doc-only diff has nothing to count, and must not read the repo.

    Asserted on the CALL, not on the result. An empty result is what a walk over
    an empty temp directory returns too, so a test that checked only the return
    value passed with the early return deleted, which is precisely the
    "it passed because the feature did nothing" trap.
    """
    from orchestrator.core import blast_radius as module

    calls: list[Any] = []

    def _spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        message = "the walk must not be reached for a diff with no identifiers"
        raise AssertionError(message)

    monkeypatch.setattr(module, "count_occurrences", _spy)
    diff = "--- a/README.md\n+++ b/README.md\n+Some prose about alerts.\n"

    result = measure_blast_radius(diff, tmp_path)

    assert calls == []
    assert result.occurrences == ()
    assert result.complete is True


def _radius(occurrences: tuple[Occurrence, ...], *, complete: bool = True):
    """Build a BlastRadius without re-importing it in every test."""
    from orchestrator.core.blast_radius import BlastRadius

    return BlastRadius(occurrences=occurrences, complete=complete)
