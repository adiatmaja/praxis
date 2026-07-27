from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "path",
    [
        "docker/agy-agent/entrypoint.sh",
        "docker/opencode-agent/entrypoint.sh",
    ],
)
def test_single_branch_reuses_existing_remote(path: str) -> None:
    script = Path(path).read_text(encoding="utf-8")
    assert "SINGLE_BRANCH" in script
    assert "origin/${BRANCH}" in script or "origin/$BRANCH" in script


@pytest.mark.parametrize(
    "path",
    [
        "docker/agy-agent/entrypoint.sh",
        "docker/opencode-agent/entrypoint.sh",
    ],
)
def test_protected_base_guard_exempts_single_branch(path: str) -> None:
    """The protected-base guard must NOT fire in single-branch mode.

    In auto-delegate single-branch mode the base branch legitimately IS the
    default branch (main): the plan branch is the single feature branch cut
    from main and opening its PR back at it. The guard is for two-tier mode
    only, so it must be wrapped in a SINGLE_BRANCH != "1" condition.
    """
    script = Path(path).read_text(encoding="utf-8")
    assert 'if [ "${SINGLE_BRANCH:-0}" != "1" ]; then' in script
    # The protected-base case must live inside that conditional block.
    guard_pos = script.index('if [ "${SINGLE_BRANCH:-0}" != "1" ]; then')
    case_pos = script.index("main | master | release*)")
    assert guard_pos < case_pos


@pytest.mark.parametrize(
    "path",
    [
        "docker/agy-agent/entrypoint.sh",
        "docker/opencode-agent/entrypoint.sh",
    ],
)
def test_base_branch_checkout_is_create_or_reset(path: str) -> None:
    """Base-branch checkout must use -B, not -b.

    In single-branch mode BASE_BRANCH is the default branch (e.g. main), which
    already exists locally after clone; a plain ``git checkout -b main`` fatals
    with "a branch named 'main' already exists". ``-B`` creates-or-resets.
    """
    script = Path(path).read_text(encoding="utf-8")
    assert 'git checkout -B "${BASE_BRANCH}" "origin/${BASE_BRANCH}"' in script
    assert 'git checkout -b "${BASE_BRANCH}" "origin/${BASE_BRANCH}"' not in script
