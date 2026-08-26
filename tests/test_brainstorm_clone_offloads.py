"""The improvement loop's repository clone must not block the event loop.

``BrainstormManager.survey_repo`` is awaited from ``check_improvements``, which
runs inside the single orchestration pass this process has. Its clone goes
through ``clone_with_token``, a synchronous ``subprocess.run`` with no deadline
of its own. Called bare from a coroutine that blocks FastAPI, SSE and every
agent callback for the length of the fetch -- the identical hazard already
recognised and fixed one seat over in ``Orchestrator._clone_for_planning``.

The two properties are separate and neither implies the other:

* OFF THE LOOP, so the rest of the process keeps running while git works;
* BOUNDED, because a clone that hangs never raises, and an exception is the only
  thing the improvement loop's fail-closed ``except`` can act on. Without a
  deadline the loop does not fall back to no proposal, it stops answering.

Both are asserted behaviourally: the first by whether another coroutine gets to
run during the clone, the second by whether the call raises. Asserting that
``asyncio.to_thread`` appears in the source would pass on an implementation that
awaited it and then blocked anyway.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from orchestrator.core import brainstorm
from orchestrator.core.brainstorm import BrainstormManager
from orchestrator.core.event_bus import EventBus


REPO = "https://github.com/u/a"

# Long enough that a blocked loop is unmistakable, short enough to keep the
# suite quick. A heartbeat at 10 ms should tick dozens of times inside it.
_CLONE_SECONDS = 0.4


def _manager(tmp_path: Path) -> BrainstormManager:
    return BrainstormManager(str(tmp_path), EventBus(), "pat-token")


def _blocking_clone(repo_url: str, dest: str, token: str, depth: int = 50) -> None:
    """Stand in for git: hold the calling THREAD for a while, like a real fetch."""
    Path(dest).mkdir(parents=True, exist_ok=True)
    (Path(dest) / "README.md").write_text("# repo\n", encoding="utf-8")
    time.sleep(_CLONE_SECONDS)


@pytest.mark.unit
async def test_the_clone_does_not_stall_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another coroutine must keep running while the clone runs."""
    monkeypatch.setattr(brainstorm, "clone_with_token", _blocking_clone)
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    try:
        survey = await _manager(tmp_path).survey_repo(REPO)
    finally:
        beat.cancel()

    assert "README.md" in survey
    assert ticks >= 5, (
        "the event loop was blocked for the whole clone: nothing else in this "
        f"process could run, and only {ticks} heartbeat(s) got through"
    )


@pytest.mark.unit
async def test_a_hanging_clone_raises_instead_of_never_returning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hang has to become an exception, or fail-closed cannot fire.

    ``_repo_survey`` guards ``survey_repo`` with ``except Exception`` and treats
    any failure as "no evidence, propose nothing". A clone that never returns
    produces no exception at all, so that guard is unreachable and the caller
    waits forever.
    """
    monkeypatch.setattr(brainstorm, "clone_with_token", _blocking_clone)
    monkeypatch.setattr(brainstorm, "_CLONE_TIMEOUT_SECONDS", 0.05)

    with pytest.raises(TimeoutError):
        await _manager(tmp_path).survey_repo(REPO)


@pytest.mark.unit
async def test_a_quick_clone_is_untouched_by_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The positive control: the timeout must not become a coin toss.

    Without this, a deadline of zero would satisfy the test above.
    """

    def _quick_clone(repo_url: str, dest: str, token: str, depth: int = 50) -> None:
        Path(dest).mkdir(parents=True, exist_ok=True)
        (Path(dest) / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    monkeypatch.setattr(brainstorm, "clone_with_token", _quick_clone)

    survey = await _manager(tmp_path).survey_repo(REPO)

    assert "pyproject.toml" in survey
