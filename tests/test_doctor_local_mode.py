"""Doctor's "is this a local deployment" question must use the one predicate.

``_is_local_mode`` carried a fourth, disagreeing copy of the local-repo test:
a SQL ``repo_url NOT LIKE 'file://%'``, which recognizes one of the five forms
``core/git_backend.is_local_repo_url`` accepts. The benchmark registers plain
filesystem paths, not ``file://`` URLs, so the single deployment shaped most
like local mode (every project local, no GitHub credential by design) was the
one doctor reported a false credential problem for.
"""

from __future__ import annotations

from typing import Any

import pytest

from orchestrator.api.doctor import _is_local_mode
from tests.conftest import seed_user


async def _add_project(db: Any, repo_url: str, name: str) -> None:
    await db.execute(
        """INSERT INTO projects (id, user_id, name, repo_url, default_branch)
           VALUES (?, ?, ?, ?, ?)""",
        (name, "test-user", name, repo_url, "main"),
    )


@pytest.mark.unit
async def test_no_projects_at_all_reads_as_local(db: Any) -> None:
    assert await _is_local_mode(db) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "repo_url",
    [
        pytest.param("file:///srv/praxis/r.git", id="file-url"),
        pytest.param("/srv/praxis/r.git", id="posix-absolute"),
        pytest.param(r"C:\repos\r.git", id="windows-drive"),
        pytest.param(r"\\fileserver\share\r.git", id="unc-share"),
        pytest.param("~/repos/r.git", id="tilde"),
    ],
)
async def test_every_local_form_counts_as_local(db: Any, repo_url: str) -> None:
    """All five, not just ``file://``. The bench uses the second one."""
    await seed_user(db)
    await _add_project(db, repo_url, "p1")
    assert await _is_local_mode(db) is True


@pytest.mark.unit
async def test_one_remote_project_is_enough_to_leave_local_mode(db: Any) -> None:
    await seed_user(db)
    await _add_project(db, "/srv/praxis/r.git", "p1")
    await _add_project(db, "https://github.com/o/r", "p2")
    assert await _is_local_mode(db) is False
