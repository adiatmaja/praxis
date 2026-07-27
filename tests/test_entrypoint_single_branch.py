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
