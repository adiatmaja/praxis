"""Two timing knobs, and proof that neither of them is decoration.

``callback_grace`` was a documented key in ``config/praxis.yaml``, a field on
``Settings``, and a hardcoded ``5.0`` in ``Orchestrator.__init__`` that nothing
ever overrode. Nothing read the field:

    rg -n "callback_grace" src/

returned the YAML comment, the Settings field, and the assignment - and no
wiring between them. ``loop_interval`` had exactly the same shape once and was
fixed; this is the same defect surviving next door, and it matters more now
because ``worker_timeout_minutes`` joins it. A ceiling on a worker's wall clock
that silently keeps its own default is worse than no ceiling: an operator who
raises it in the YAML believes a longer run is permitted and it is not.

Both halves are pinned, because they fail differently: the constructor has to
USE what it is handed, and ``main.py`` has to HAND it the configured value.
"""

# ruff: noqa: S101

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock

from orchestrator.config import Settings
from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator


MAIN_PY = (
    Path(__file__).resolve().parents[1] / "src" / "orchestrator" / "main.py"
).read_text(encoding="utf-8")


def _lifespan_orchestrator_call() -> str:
    """Return the ``Orchestrator(...)`` call main.py's lifespan actually makes.

    Scoped to the CALL, not the file: a ``settings.worker_timeout_minutes``
    that survives in an unrelated statement while the kwarg is gone is
    precisely the dead-knob state these tests exist to detect.

    COMMENTS ARE STRIPPED, and that is not tidiness. The first version of this
    guard searched the raw text and SURVIVED the most likely regression of all:
    commenting the kwarg out leaves the searched substring intact inside the
    comment, so the test stayed green through the exact defect it was written
    for. Measured, both kwargs, before this line existed.
    """
    start = MAIN_PY.index("Orchestrator(")
    depth = 0
    for index in range(start + len("Orchestrator"), len(MAIN_PY)):
        char = MAIN_PY[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                call = MAIN_PY[start : index + 1]
                return "\n".join(line.split("#", 1)[0] for line in call.splitlines())
    message = "unbalanced parentheses reading the Orchestrator call in main.py"
    raise AssertionError(message)


def _orch(**kwargs: object) -> Orchestrator:
    return Orchestrator(
        task_queue=AsyncMock(),
        agent_manager=None,
        opus_bridge=AsyncMock(),
        git_ops=AsyncMock(),
        event_bus=EventBus(),
        **kwargs,  # type: ignore[arg-type]
    )


def test_the_constructor_uses_the_callback_grace_it_is_handed() -> None:
    assert _orch(callback_grace=17)._callback_grace == 17.0


def test_the_constructor_uses_the_worker_timeout_it_is_handed() -> None:
    """Minutes in, seconds out: the seat that compares against it works in
    seconds, and doing the conversion at the boundary keeps one unit in the
    engine."""
    assert _orch(worker_timeout_minutes=90)._worker_timeout_seconds == 5400.0


def _field_default(name: str) -> int:
    """The FIELD's own default, with no YAML and no environment in it.

    ``Settings()`` overlays ``config/praxis.yaml``, so comparing anything
    against a constructed ``Settings`` compares it against the mounted file.
    That is the right question for one of these tests and the wrong one for
    the other, and conflating them made an earlier version of the YAML guard
    tautological: editing the YAML moved BOTH sides.
    """
    return int(Settings.model_fields[name].default)


def test_the_constructor_defaults_match_the_settings_field_defaults() -> None:
    """A caller that omits either must get what the configured default would
    have given, or the constructor becomes a second answer to a question the
    settings layer already answers - which is how ``loop_interval`` ended up
    with a hardcoded 5.0 beside a settings field claiming 30."""
    default = _orch()
    assert default._callback_grace == float(_field_default("callback_grace"))
    assert default._worker_timeout_seconds == float(
        _field_default("worker_timeout_minutes") * 60
    )


def test_main_passes_the_configured_callback_grace() -> None:
    assert "callback_grace=settings.callback_grace" in _lifespan_orchestrator_call(), (
        "the lifespan no longer passes the configured callback_grace, so the "
        "documented key is decoration again and the constructor default is the "
        "real value"
    )


def test_main_passes_the_configured_worker_timeout() -> None:
    call = _lifespan_orchestrator_call()
    assert "worker_timeout_minutes=settings.worker_timeout_minutes" in call, (
        "the lifespan no longer passes the configured worker timeout, so an "
        "operator raising it in praxis.yaml gets no change and believes a "
        "longer run is permitted when it is not"
    )


def test_the_shipped_yaml_declares_the_worker_timeout() -> None:
    """The mounted YAML is where an operator looks first, and a key that only
    exists as an env var is a key nobody discovers."""
    yaml_text = (
        Path(__file__).resolve().parents[1] / "config" / "praxis.yaml"
    ).read_text(encoding="utf-8")
    match = re.search(r"^worker_timeout_minutes:\s*(\d+)", yaml_text, re.MULTILINE)
    assert match is not None, "worker_timeout_minutes is not declared in praxis.yaml"
    assert int(match.group(1)) == _field_default("worker_timeout_minutes"), (
        "the shipped YAML and the field default disagree, so the bound in "
        "effect depends on whether the file happened to be mounted"
    )
