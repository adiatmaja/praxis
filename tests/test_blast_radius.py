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
def test_a_non_regular_file_is_never_opened(tmp_path: Path):
    """OPENING a FIFO blocks forever, and the deadline is only read BETWEEN files.

    So the time budget cannot bound a single blocking syscall, and "bounded on a
    slow mount" is only true of a uniformly slow mount. A directory is the
    portable stand-in here: the guard is about refusing to open anything that is
    not a regular file, and without it this raises instead of returning None.
    """
    from orchestrator.core.blast_radius import _read_text_bounded

    (tmp_path / "subdir").mkdir()

    assert _read_text_bounded(tmp_path / "subdir") is None


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


class _Clock:
    """A monotonic clock that returns scripted values, last value repeating.

    Substituted for the module's reference to ``time``, never for the stdlib
    module itself: patching ``time.monotonic`` globally would also move the
    clock under pytest-timeout's watchdog thread.
    """

    def __init__(self, ticks: list[float]) -> None:
        self._ticks = list(ticks)

    def monotonic(self) -> float:
        return self._ticks.pop(0) if len(self._ticks) > 1 else self._ticks[0]


@pytest.mark.unit
def test_the_wall_clock_budget_stops_the_walk_and_the_result_admits_it(
    tmp_path: Path, monkeypatch
):
    """The third cap the task named, and the only one that had no test.

    Deleting ``or time.monotonic() >= deadline`` left every other test in this
    file green, so the wall clock was shipped unpinned. It is the cap that
    matters most on a slow network mount, where the file and byte budgets are
    both satisfied and each read still takes seconds.
    """
    from orchestrator.core import blast_radius as module

    for name in ("a.css", "b.css", "c.css"):
        (tmp_path / name).write_text(".s-alert {}\n", encoding="utf-8")
    # Call 1 sets the deadline (0.0 + 5.0). Call 2 clears it, so one file is
    # read. Call 3 onward is past it, so the walk stops with a PARTIAL count.
    monkeypatch.setattr(module, "time", _Clock([0.0, 0.0, 999.0]))

    counts = count_occurrences(tmp_path, [".s-alert"], time_budget_seconds=5.0)

    assert counts.complete is False
    assert counts.files_read == 1
    assert counts.counts[".s-alert"] == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "root_kind", ["missing", "empty", "only-binaries"], ids=["missing", "empty", "png"]
)
def test_a_walk_that_read_no_files_is_not_reported_as_a_finding(
    tmp_path: Path, root_kind: str
):
    """``os.walk`` over nothing yields nothing and RAISES nothing.

    All three roots produce all-zero counts, which
    ``measure_blast_radius`` would otherwise rank into an empty list, and which
    the review layer documents as "measured, and nothing the diff changed
    occurs more than once. That IS a finding". A check that structurally cannot
    fail, returning green, is the exact thing the field report is about.
    """
    if root_kind == "missing":
        root = tmp_path / "not-a-real-checkout"
    elif root_kind == "empty":
        root = tmp_path / "empty"
        root.mkdir()
    else:
        root = tmp_path / "images"
        root.mkdir()
        (root / "logo.png").write_bytes(b"\x89PNG\r\n.s-alert\n")

    counts = count_occurrences(root, [".s-alert"])

    assert counts.files_read == 0
    assert counts.complete is False, (
        "a walk that opened no file has measured nothing, and must not hand on "
        "all-zero counts as a completed measurement"
    )


@pytest.mark.unit
def test_a_diff_carrying_more_identifiers_than_the_cap_says_so(tmp_path: Path):
    """``MAX_IDENTIFIERS`` truncation used to vanish into a bare list slice.

    A 200-identifier diff then produced 60 measurements labelled complete,
    which contradicts this module's own docstring promise that a walk hitting a
    cap announces it.
    """
    from orchestrator.core.blast_radius import MAX_IDENTIFIERS

    over = MAX_IDENTIFIERS + 7
    (tmp_path / "a.css").write_text(
        "".join(f".widget{i} .widget{i} {{}}\n" for i in range(over)), encoding="utf-8"
    )
    diff = "--- a/a.css\n+++ b/a.css\n" + "".join(
        f"+.widget{i} {{}}\n" for i in range(over)
    )

    result = measure_blast_radius(diff, tmp_path)

    assert result.identifiers == MAX_IDENTIFIERS
    # 7 never measured, plus everything measured-and-reused past the top ten.
    assert result.omitted == 7 + (MAX_IDENTIFIERS - len(result.occurrences))


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


# A realistic praxis-shaped diff: a class gains a constructor, a run method, a
# module-level factory, and the front end gains a store and a helper. Every name
# in it except `Thing` is one this repository already uses thousands of times.
_PY_JS_DIFF = """\
diff --git a/src/app/engine.py b/src/app/engine.py
index 1111111..2222222 100644
--- a/src/app/engine.py
+++ b/src/app/engine.py
@@ -1,3 +1,12 @@
-class Thing:
+class Thing(Base):
+    def __init__(self, state: str) -> None:
+        self.state = state
+
+    def run(self) -> str:
+        return self.state
+
+def init(config: dict) -> Thing:
+    return Thing(config["state"])
diff --git a/web/app.js b/web/app.js
index 3333333..4444444 100644
--- a/web/app.js
+++ b/web/app.js
@@ -3,2 +3,4 @@
+export const state = {};
+function run(payload) { return payload; }
"""


def _generic_heavy_tree(root: Path) -> None:
    """A small tree shaped like a real codebase: generic names everywhere."""
    pkg = root / "src" / "app"
    pkg.mkdir(parents=True)
    (pkg / "engine.py").write_text(
        "class Thing:\n"
        "    def __init__(self, state):\n"
        "        self.state = state\n"
        "    def run(self):\n"
        "        return self.state\n",
        encoding="utf-8",
    )
    # `Thing` is referenced once more, which is what makes it a real finding.
    (pkg / "factory.py").write_text(
        "from .engine import Thing\n\n"
        "def init(config):\n"
        "    return Thing(config['state'])\n",
        encoding="utf-8",
    )
    # Bulk: the generic names repeated the way a real repository repeats them.
    (pkg / "runner.py").write_text(
        "\n".join(
            f"def run_{i}(state): init(state); run(state); state.run()"
            for i in range(40)
        ),
        encoding="utf-8",
    )
    (root / "web").mkdir()
    (root / "web" / "app.js").write_text(
        "\n".join(
            f"export const state{i} = run(state); function init{i}() {{}}"
            for i in range(40)
        ),
        encoding="utf-8",
    )


@pytest.mark.unit
def test_generic_names_do_not_crowd_out_the_informative_one(tmp_path: Path):
    """The acceptance test for ranking, on a realistic Python/JS diff.

    Ranking by raw count systematically promotes the LEAST distinctive names:
    against this repository the section returned ``run`` 4159, ``state`` 1282,
    ``init`` 543, ``__init__`` 282, and dropped the one name a reviewer could
    act on. The section then tells the reader "if a count is high, say what you
    checked", so a reviewer handed ``run: 4159`` either burns turns or learns to
    skip the block. The CSS case worked; most diffs in this repository are
    Python.
    """
    _generic_heavy_tree(tmp_path)

    result = measure_blast_radius(_PY_JS_DIFF, tmp_path)
    reported = [occurrence.identifier for occurrence in result.occurrences]

    assert "Thing" in reported, f"the informative name was dropped: {reported}"
    for noise in ("run", "state", "init", "__init__"):
        assert noise not in reported, f"{noise} crowded out the signal: {reported}"
    # Suppressed, not absent: those names ARE reused, and saying nothing about
    # them would be the "nothing is reused" lie in a different place.
    assert result.omitted > 0


@pytest.mark.unit
@pytest.mark.parametrize(
    ("identifier", "expected"),
    [
        pytest.param(".s-alert", True, id="css-selector-always"),
        # The case that makes the selector carve-out load-bearing rather than
        # decorative: without it a two-character utility class is dropped by the
        # length rule, and utility classes are exactly the shared, widely reused
        # ones whose reach a reviewer most needs.
        pytest.param(".m0", True, id="short-css-selector"),
        pytest.param("#id", True, id="short-css-id"),
        pytest.param("__init__", False, id="dunder"),
        pytest.param("run", False, id="too-short"),
        pytest.param("init", False, id="generic-at-min-length"),
        pytest.param("state", False, id="generic-above-min-length"),
        pytest.param("Thing", True, id="distinctive"),
        pytest.param("normalize_verify_cmd", True, id="distinctive-long"),
    ],
)
def test_reportability_rules(identifier: str, expected: bool):
    """Each rule pinned on its own, so one cannot silently subsume another."""
    from orchestrator.core.blast_radius import is_reportable

    assert is_reportable(identifier) is expected


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
def test_a_measured_empty_result_is_never_rendered_as_an_absent_measurement():
    """The four empty cases mean four different things, and say so.

    This test previously asserted ``render_blast_radius(...) == ""``, and that
    was the bug: the caller turned "" into the prompt's absent-section line,
    "Not measured for this review (no checkout was available, or the
    measurement failed)". A real walk over a real checkout that found nothing
    reused therefore told the reviewer no measurement had been taken, which is
    literally false and collapses the distinction between absent evidence and
    evidence of absence.
    """
    self_contained = render_blast_radius(_radius((), identifiers=3))
    cut_off = render_blast_radius(_radius((), complete=False, identifiers=3))
    nothing_tracked = render_blast_radius(_radius((), identifiers=0))
    all_generic = render_blast_radius(_radius((), identifiers=3, omitted=3))

    # Each is a real sentence, and no two of them are the same sentence.
    assert len({self_contained, cut_off, nothing_tracked, all_generic}) == 4
    assert all(len(text) > 40 for text in (self_contained, cut_off, nothing_tracked))

    # Only ONE of them is the finding "this change looks self-contained".
    assert "self-contained" in self_contained
    assert "NOTHING may be concluded" in cut_off
    assert "self-contained" not in cut_off
    assert "no identifiers of the kinds this check tracks" in nothing_tracked
    assert "self-contained" not in nothing_tracked
    assert "too generically named" in all_generic
    assert "NOT absent" in all_generic


@pytest.mark.unit
def test_the_list_says_when_it_is_not_the_whole_answer():
    """A TOP_N-capped, generic-filtered list must not read as exhaustive."""
    rendered = render_blast_radius(
        _radius((Occurrence(".s-alert", 373),), identifiers=14, omitted=13)
    )

    assert "13 further changed identifiers" in rendered
    assert "not exhaustive" in rendered


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


def _radius(
    occurrences: tuple[Occurrence, ...],
    *,
    complete: bool = True,
    omitted: int = 0,
    identifiers: int | None = None,
):
    """Build a BlastRadius without re-importing it in every test.

    ``identifiers`` defaults to the number shown, so a test that does not care
    about the "diff defines nothing" case never has to say so.
    """
    from orchestrator.core.blast_radius import BlastRadius

    return BlastRadius(
        occurrences=occurrences,
        complete=complete,
        omitted=omitted,
        identifiers=len(occurrences) if identifiers is None else identifiers,
    )
