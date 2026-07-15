"""Tests for diff_stats — parse unified diff into (files_changed, loc_changed)."""

from __future__ import annotations

from orchestrator.core.diff_stats import diff_stats


def test_empty_diff() -> None:
    assert diff_stats("") == (0, 0)


def test_two_file_diff() -> None:
    diff = """\
--- a/file1.txt
+++ b/file1.txt
@@ -1,3 +1,4 @@
 line1
+added1
+added2
-removed1
--- a/file2.txt
+++ b/file2.txt
@@ -1,2 +1,2 @@
-shared
+replaced
 line2
"""
    files_changed, loc_changed = diff_stats(diff)
    assert files_changed == 2
    # file1: +2 -1 = 3, file2: +1 -1 = 2  => 5 content lines
    assert loc_changed == 5


def test_headers_not_counted_as_loc() -> None:
    """The +++ and --- lines themselves must NOT count toward loc_changed."""
    diff = """\
--- a/single.txt
+++ b/single.txt
@@ -1,1 +1,2 @@
 original
+new_line
"""
    files_changed, loc_changed = diff_stats(diff)
    assert files_changed == 1
    assert loc_changed == 1  # only the '+' line, not the header lines


def test_new_file_from_dev_null() -> None:
    """A new file (--- /dev/null) counts as 1 file."""
    diff = """\
--- /dev/null
+++ b/new_file.py
@@ -0,0 +1,3 @@
+line1
+line2
+line3
"""
    files_changed, loc_changed = diff_stats(diff)
    assert files_changed == 1
    assert loc_changed == 3
