"""Both agent images must give the worker the same runnable python toolchain.

The env contract ``AgentManager.spawn_agent`` sets is harness-agnostic and the
Bible it ships tells the worker to run the acceptance command without knowing
which image it is in, so ``python -m pytest`` existing must not depend on the
harness a project happens to be configured with.  Measured live 2026-08-14 on
``psf__requests-2148``: with no pytest in the image the worker burned several
turns hunting for one and then rewrote four unrelated files to make the suite
collect, contaminating the graded diff.
"""

import re
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
AGENT_IMAGES = ("opencode-agent", "agy-agent")

# The bench verify command, and the form a worker reaches for by reflex.
# ``python`` is a Debian name that does not exist without python-is-python3,
# and pytest has to be importable by that same interpreter, so neither package
# is sufficient alone.
REQUIRED = frozenset({"python3", "python-is-python3", "python3-pytest"})


def _apt_packages(image: str) -> frozenset[str]:
    """Return every package name passed to ``apt-get install`` in a Dockerfile.

    Parsed rather than substring-matched so that naming a package in a comment,
    or installing it in only one of the two images, cannot pass.
    """
    text = (REPO / "docker" / image / "Dockerfile").read_text(encoding="utf-8")
    # Drop comments first: a package named in prose is not an installed one.
    body = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    # Join the shell line continuations so a multi-line install list is one run.
    body = body.replace("\\\n", " ")
    found: set[str] = set()
    for match in re.finditer(r"apt-get install\s+([^\n]*)", body):
        for token in match.group(1).split():
            if token.startswith("-") or token in {"&&", "|", ">"}:
                continue
            found.add(token)
    return frozenset(found)


@pytest.mark.unit
@pytest.mark.parametrize("image", AGENT_IMAGES)
def test_the_agent_image_installs_a_runnable_pytest(image: str) -> None:
    missing = REQUIRED - _apt_packages(image)
    assert not missing, f"docker/{image}/Dockerfile does not install {sorted(missing)}"


@pytest.mark.unit
def test_neither_agent_image_gets_the_toolchain_the_other_lacks() -> None:
    """Parity is the invariant; a one-sided edit is the realistic regression.

    The per-image test above still passes when someone adds a package to one
    Dockerfile only, as long as both keep the three required names.  This one
    fails on any divergence in the python toolchain between the two images,
    which is what makes ``python -m pytest`` a property of Praxis rather than
    of the harness a project picked.
    """
    opencode, agy = (_apt_packages(image) for image in AGENT_IMAGES)
    python_only = {
        pkg for pkg in opencode ^ agy if pkg.startswith(("python", "py3", "pytest"))
    }
    assert not python_only, (
        f"the two agent images disagree on {sorted(python_only)}; "
        "a worker's harness must not decide whether pytest exists"
    )
