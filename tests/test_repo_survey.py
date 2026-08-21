"""The survey that gives the improvement loop a repository to reason about.

Walkthrough #7 found `check_improvements` proposing five tasks for a seven-file
helper repo that described Praxis itself: a Caddyfile CSP header, auth rate
limiting, bcrypt token hashing, a Database transaction manager. None of those
things exist in the target repo. The cause was not a bad prompt, it was an
absent one: the entire input was three strings, the project name, the repo URL
and a plan path. Nothing cloned the repository or read a single file of it, so
the planner answered from the only codebase in its context.

This module is the missing input. The tests below pin the two properties that
make it useful rather than merely present: it must name FILES THAT ACTUALLY
EXIST in the tree it was given, and when it cannot fit everything it must SAY
how much it dropped, because a survey that silently truncates reads to the
planner as a complete picture of a smaller project.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.core.repo_survey import build_repo_survey


def _tree(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


@pytest.mark.unit
def test_the_survey_names_files_that_actually_exist(tmp_path: Path) -> None:
    """The whole point: paths the planner can check against reality."""
    _tree(
        tmp_path,
        {
            "README.md": "# playground\n\nA scratch repo.\n",
            "src/playground/greet.py": "def greet(name): ...\n",
            "src/playground/duration.py": "def format_duration(s): ...\n",
        },
    )

    survey = build_repo_survey(tmp_path)

    assert "src/playground/greet.py" in survey
    assert "src/playground/duration.py" in survey
    assert "README.md" in survey


@pytest.mark.unit
def test_paths_are_posix_and_repo_relative(tmp_path: Path) -> None:
    """A Windows-separated or absolute path is not checkable by the planner.

    The orchestrator runs in Linux but this suite also runs on windows-latest,
    and a survey built there would otherwise carry backslashes and a tmp path
    that means nothing outside the machine that produced it.
    """
    _tree(tmp_path, {"src/pkg/mod.py": "x = 1\n"})

    survey = build_repo_survey(tmp_path)

    assert "src/pkg/mod.py" in survey
    assert "\\" not in survey
    assert str(tmp_path) not in survey


@pytest.mark.unit
def test_noise_directories_are_excluded(tmp_path: Path) -> None:
    """Otherwise the budget is spent on vendored and generated files.

    `.git` is the one that matters most: it is thousands of objects, and
    including it would push every real source file past the cap.
    """
    _tree(
        tmp_path,
        {
            "src/real.py": "x = 1\n",
            ".git/objects/ab/cdef": "binary-ish\n",
            "node_modules/left-pad/index.js": "module.exports = 1\n",
            "src/__pycache__/real.cpython-311.pyc": "cached\n",
            ".venv/lib/site-packages/thing.py": "y = 2\n",
        },
    )

    survey = build_repo_survey(tmp_path)

    assert "src/real.py" in survey
    for noise in (".git/", "node_modules/", "__pycache__/", ".venv/"):
        assert noise not in survey, f"{noise} should not be surveyed"


@pytest.mark.unit
def test_truncation_is_stated_never_silent(tmp_path: Path) -> None:
    """A silently truncated survey reads as a complete picture of a small repo.

    That is the failure this whole module exists to prevent, one level down: an
    incomplete input that does not announce itself gets reasoned about as if it
    were complete.
    """
    _tree(tmp_path, {f"src/mod_{i:03d}.py": "x = 1\n" for i in range(500)})

    survey = build_repo_survey(tmp_path, max_files=50)

    assert survey.count("src/mod_") <= 50
    assert "450 more" in survey, (
        "the survey must state how many files it dropped, got:\n" + survey[-400:]
    )


@pytest.mark.unit
def test_key_file_contents_are_included_and_bounded(tmp_path: Path) -> None:
    """A file list alone does not say what the project IS.

    README and the manifest are what distinguish "an Indonesian FMCG reporting
    dashboard" from "a scratch repo of helper functions", which is precisely
    the distinction the improvement loop got wrong.
    """
    _tree(
        tmp_path,
        {
            "README.md": "# playground\n\nScratch repo for Praxis e2e runs.\n",
            "pyproject.toml": '[project]\nname = "playground"\n',
            "src/mod.py": "x = 1\n",
        },
    )

    survey = build_repo_survey(tmp_path)

    assert "Scratch repo for Praxis e2e runs." in survey
    assert 'name = "playground"' in survey


@pytest.mark.unit
def test_a_huge_key_file_cannot_blow_the_prompt(tmp_path: Path) -> None:
    """The excerpt is capped per file, and the cap is announced."""
    _tree(tmp_path, {"README.md": "A" * 50_000, "src/mod.py": "x = 1\n"})

    survey = build_repo_survey(tmp_path)

    assert len(survey) < 20_000, "one big README must not dominate the survey"
    assert "truncated" in survey.lower()


@pytest.mark.unit
def test_the_survey_is_deterministic(tmp_path: Path) -> None:
    """Two runs over the same tree must agree.

    Filesystem iteration order is not sorted on every platform, and an unstable
    survey would make the improvement loop's output irreproducible for no
    reason.
    """
    _tree(tmp_path, {f"src/{name}.py": "x = 1\n" for name in "cadbe"})

    assert build_repo_survey(tmp_path) == build_repo_survey(tmp_path)


@pytest.mark.unit
def test_an_empty_repo_says_so_rather_than_returning_nothing(tmp_path: Path) -> None:
    """An empty string is indistinguishable from "the survey failed".

    The caller decides whether to proceed based on whether it got a survey, so
    "this repo is empty" has to be a POSITIVE answer, not a falsy one.
    """
    survey = build_repo_survey(tmp_path)

    assert survey.strip(), "an empty repo must still produce a survey"
    assert "no files" in survey.lower()


@pytest.mark.unit
def test_binary_files_are_listed_but_never_excerpted(tmp_path: Path) -> None:
    """A decode error mid-survey would take down the whole improvement pass."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff\xfe")
    _tree(tmp_path, {"README.md": "# hi\n"})

    survey = build_repo_survey(tmp_path)

    assert "assets/logo.png" in survey
