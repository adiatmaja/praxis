"""Guards for the two ways ``bench.grade`` could invoke the official harness and
get a silent zero back.

Both were found by RUNNING the harness rather than by reading it (2026-08-26,
swebench 5.0.2, one instance carrying the upstream GOLD patch so it could not
fail on its merits). Neither is visible to a test that mocks the subprocess:
the first kills the harness before any instance is evaluated, the second lets
every instance run and grade unresolved for a reason that has nothing to do
with the patch. That second shape is exactly what ``bench/grade.py`` exists to
prevent, so it must not be reachable by accident.
"""
# ruff: noqa: S101

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from bench import grade


#: Raised by a stub standing where the real work begins, so a test can prove
#: the refusal did NOT fire without doing any of that work.
_PAST_REFUSAL = "stop here, past the refusal"


def test_the_default_dataset_is_one_the_current_harness_can_evaluate() -> None:
    """``princeton-nlp/SWE-bench_Lite`` rows carry no ``image`` column.

    ``swebench.harness.utils.make_test_spec`` reads ``instance["image"]``, so
    the legacy dataset name raises ``KeyError: 'image'`` and the run dies
    before a single instance is graded. Pinning the name is the point: this
    default is the one argument nobody passes, so it is the one that has to be
    right.
    """
    assert grade.DEFAULT_DATASET == "SWE-bench/SWE-bench_Lite"
    assert "princeton-nlp" not in grade.DEFAULT_DATASET


def test_the_cli_default_is_the_module_constant_not_a_second_copy() -> None:
    """The parser must not carry its own literal.

    A duplicated dataset name is the ordinary way this regresses: the constant
    gets corrected, the ``argparse`` default keeps the stale string, and every
    real invocation still uses the stale one because nobody passes ``--dataset``.
    """
    source = Path(grade.__file__).read_text(encoding="utf-8")
    assert '"--dataset", default=grade.DEFAULT_DATASET' not in source
    assert 'parser.add_argument("--dataset", default=DEFAULT_DATASET)' in source
    # And no second spelling of a dataset id anywhere in the argument setup.
    assert source.count("SWE-bench/SWE-bench_Lite") == 1


def test_main_refuses_to_invoke_the_harness_on_native_windows(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A Windows run grades every instance unresolved and looks honest.

    The harness writes ``eval.sh`` and the test patch in text mode, so they
    arrive in the Linux eval container with CRLF, every ``git apply`` fails,
    and the number published is a silent zero. Refuse instead.
    """
    monkeypatch.setattr(grade.sys, "platform", "win32")

    spawned = "the harness must not be invoked on win32"

    def _never(*args: object, **kwargs: object) -> None:
        raise AssertionError(spawned)

    monkeypatch.setattr(grade.subprocess, "run", _never)
    # read_records would be reached only if the refusal did not fire first.
    monkeypatch.setattr(grade, "read_records", _never)

    with caplog.at_level(logging.ERROR):
        exit_code = grade.main(["--run", "runs/x", "--sample", "s.json"])

    assert exit_code == 1
    # caplog.text is NOT the ERROR record: other fixtures log at INFO and a
    # substring check against the whole buffer passes on someone else's line.
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert errors, "the refusal must be logged at ERROR, not swallowed"
    assert any("native Windows" in message for message in errors)
    # A refusal that does not name a way forward is a dead end, and the
    # workaround is not guessable from the error.
    assert any("docker run" in message for message in errors)


def test_the_windows_refusal_can_be_overridden_deliberately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control for the test above.

    Without this, "correctly refused" and "``main`` is broken on every
    platform" are indistinguishable: both make the assertions above pass.
    ``--allow-windows-harness`` must carry the run PAST the refusal and into
    the real work, which here is the first thing ``main`` does afterwards.
    """
    monkeypatch.setattr(grade.sys, "platform", "win32")
    reached = []

    def _record_reached(path: Path) -> list[object]:
        reached.append(path)
        raise RuntimeError(_PAST_REFUSAL)

    monkeypatch.setattr(grade, "read_records", _record_reached)

    with pytest.raises(RuntimeError, match="past the refusal"):
        grade.main(["--run", "runs/x", "--sample", "s.json", "--allow-windows-harness"])
    assert reached, "the override did not carry the run past the refusal"


def test_a_non_windows_platform_is_not_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard must be scoped to the platform that has the defect."""
    monkeypatch.setattr(grade.sys, "platform", "linux")
    reached = []

    def _record_reached(path: Path) -> list[object]:
        reached.append(path)
        raise RuntimeError(_PAST_REFUSAL)

    monkeypatch.setattr(grade, "read_records", _record_reached)

    with pytest.raises(RuntimeError, match="past the refusal"):
        grade.main(["--run", "runs/x", "--sample", "s.json"])
    assert reached, "linux must not be refused"
