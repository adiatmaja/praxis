# Merge-Gated by Default Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Praxis park a reviewed PR for explicit human approval by default instead of auto-merging, with an opt-in `auto_merge` flag that can never auto-merge into a protected branch.

**Architecture:** On Opus review PASS, a task now lands in the existing-but-unused `PASSED` status (a parked PR awaiting human merge) and emits a `task_awaiting_merge` event, instead of merging immediately. A new per-project `auto_merge` flag (default off) restores the old behavior, gated by a hard rule that auto-merge never targets the repo default/protected branch. New REST endpoints approve or reject the parked merge; the MCP `poll_task` surfaces the `awaiting_merge` state so a main brain can relay it for approval.

**Tech Stack:** Python 3.11, FastAPI, aiosqlite (raw SQL, no ORM), pytest + pytest-asyncio (`asyncio_mode = "auto"`), gh CLI via `core/git_ops.py`.

---

## Context for the implementer (read first)

You have zero prior context, so here is everything you need.

**The merge happens in one place today:** `src/orchestrator/core/orchestrator.py`, method `review_task` (around lines 246-352). On `verdict == "pass"` it calls `self._git.merge_pr(...)`, sets status `MERGED`, syncs the plan checkbox, and publishes `task_completed`. This plan splits that into "gated" (default) vs "auto-merge" (opt-in) paths.

**Key existing facts (already true, do NOT re-implement):**
- `TaskStatus` enum (`src/orchestrator/models/schemas.py:19`) already has `PASSED = "passed"`. It is currently dead — the review path jumps straight to `MERGED`. We repurpose it as "reviewed, awaiting human merge."
- Dependency ordering already requires upstream tasks to be `MERGED`: `TaskQueue.get_dispatchable_tasks` (`task_queue.py:168-170`) and `all_tasks_done` (`task_queue.py:175-179`). So a task parked at `PASSED` already correctly blocks its dependents. We add a **regression test** for this, not new logic.
- `tasks.review_feedback` column already exists and is what the MCP `poll_task` returns as `review`. We reuse it to hold the review summary on the gated PASS path. We add only one new task column: `approved_at`.
- `app.state.orchestrator`, `app.state.task_queue`, `app.state.db` are all set in `main.py` lifespan and in `tests/conftest.py`. API endpoints reach them via `request.app.state.*`.
- DB schema migrations are additive `ALTER TABLE ... ADD COLUMN` statements in `Database.initialize()` (`database.py:159-184`), each wrapped in `contextlib.suppress(Exception)` so re-running is safe. Add new columns there.
- `git_ops.merge_pr(workspace, pr_number, repo=None)` runs `gh pr merge --squash --delete-branch`. `git_ops.comment_on_pr(...)`, `git_ops.extract_pr_number(pr_url)`, and `git_ops.repo_slug(url)` already exist and are used by `review_task`.
- The PR base branch for a task is the plan's `plan_branch_name` column (the `plan/...` branch the agent branched off and PRs back into). In `review_task` the plan row is already fetched into the local variable `plan`.

**Conventions:** ruff format, line length 88, `X | Y` unions, Google-style docstrings, `logging` not `print`, catch specific exceptions. Run tests with `uv run pytest`. Format with `uv run ruff format src/ tests/` and `uv run ruff check --fix src/ tests/`.

---

## File Structure

- **Create** `src/orchestrator/core/merge_policy.py` — pure functions: `is_protected_branch(branch, default_branch)` and `auto_merge_eligible(project, base_branch)`. No I/O, fully unit-testable.
- **Create** `tests/test_merge_policy.py` — unit tests for the above.
- **Modify** `src/orchestrator/database.py` — add `projects.auto_merge` and `tasks.approved_at` columns.
- **Modify** `src/orchestrator/models/schemas.py` — `auto_merge` field on `ProjectCreate` / `ProjectUpdate` / `ProjectResponse`.
- **Modify** `src/orchestrator/api/projects.py` — persist `auto_merge` on create and update.
- **Modify** `src/orchestrator/core/task_queue.py` — `mark_passed`, `mark_merged` helpers.
- **Modify** `src/orchestrator/core/orchestrator.py` — gated vs auto-merge branch in `review_task`; new `approve_task_merge` and `reject_task_merge` methods.
- **Modify** `src/orchestrator/api/tasks.py` — `POST /tasks/{id}/approve-merge`, `POST /tasks/{id}/reject-merge`.
- **Modify** `src/orchestrator/api/plans.py` — `POST /plans/{id}/approve-merges` (batch).
- **Modify** `src/mcp_server/server.py` — `poll_task_impl` maps `passed` → `awaiting_merge` and adds `branch`/`verdict`.
- **Modify** `.env.example`, `docs/deployment.md`, `CLAUDE.md` — least-privilege token guidance + gotcha.

---

### Task 1: Merge-policy pure functions

**Files:**
- Create: `src/orchestrator/core/merge_policy.py`
- Test: `tests/test_merge_policy.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_merge_policy.py
"""Unit tests for merge-policy pure functions."""

from __future__ import annotations

import pytest

from orchestrator.core.merge_policy import auto_merge_eligible, is_protected_branch


@pytest.mark.parametrize(
    ("branch", "default_branch", "expected"),
    [
        ("main", "main", True),
        ("MAIN", "main", True),
        ("master", "develop", True),
        ("release", "main", True),
        ("release/2.0", "main", True),
        ("release-hotfix", "main", True),
        ("develop", "develop", True),  # matches project default
        ("plan/mcp-foo", "main", False),
        ("agent/add-thing", "main", False),
        ("", "main", False),
        (None, "main", False),
    ],
)
def test_is_protected_branch(
    branch: str | None, default_branch: str, expected: bool
) -> None:
    assert is_protected_branch(branch, default_branch) is expected


def test_auto_merge_eligible_off_by_default() -> None:
    project = {"auto_merge": 0, "default_branch": "main"}
    assert auto_merge_eligible(project, "plan/mcp-foo") is False


def test_auto_merge_eligible_on_for_nonprotected_base() -> None:
    project = {"auto_merge": 1, "default_branch": "main"}
    assert auto_merge_eligible(project, "plan/mcp-foo") is True


def test_auto_merge_eligible_blocked_for_protected_base() -> None:
    project = {"auto_merge": 1, "default_branch": "main"}
    assert auto_merge_eligible(project, "main") is False


def test_auto_merge_eligible_none_base_is_protected() -> None:
    # Unknown base is treated as protected (fail safe).
    project = {"auto_merge": 1, "default_branch": "main"}
    assert auto_merge_eligible(project, None) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_merge_policy.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.core.merge_policy'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/orchestrator/core/merge_policy.py
"""Pure policy functions deciding whether a reviewed task may auto-merge.

These functions perform no I/O so they can be unit-tested in isolation. The
core security rule lives here: auto-merge may never target a protected branch.
"""

from __future__ import annotations

import fnmatch
from typing import Any


# Branch-name patterns always treated as protected, regardless of project
# default. Matched case-insensitively with shell-style globbing.
_PROTECTED_PATTERNS: tuple[str, ...] = ("main", "master", "release", "release*")


def is_protected_branch(branch: str | None, default_branch: str) -> bool:
    """Return True if ``branch`` must never be auto-merged into.

    A branch is protected when it is empty/unknown, equals the project's
    configured default branch, or matches a built-in protected pattern
    (``main``, ``master``, ``release*``). Matching is case-insensitive.

    Args:
        branch: The merge-target branch name, or None if unknown.
        default_branch: The project's configured default branch.

    Returns:
        True if the branch is protected (auto-merge forbidden).
    """
    if not branch:
        return True
    candidate = branch.strip().lower()
    if candidate == default_branch.strip().lower():
        return True
    return any(
        fnmatch.fnmatch(candidate, pattern.lower()) for pattern in _PROTECTED_PATTERNS
    )


def auto_merge_eligible(project: dict[str, Any], base_branch: str | None) -> bool:
    """Return True if a reviewed task in ``project`` may auto-merge to ``base_branch``.

    Eligible only when the project opted into ``auto_merge`` AND the merge
    target is not a protected branch. An unknown base branch is treated as
    protected (fail safe).

    Args:
        project: The project row (must expose ``auto_merge`` and
            ``default_branch``).
        base_branch: The PR's merge-target branch, or None if unknown.

    Returns:
        True if auto-merge is permitted; False means the gated path applies.
    """
    if not bool(project.get("auto_merge", 0)):
        return False
    return not is_protected_branch(base_branch, str(project.get("default_branch", "")))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_merge_policy.py -v`
Expected: PASS (all parametrized cases + 4 eligibility tests)

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/merge_policy.py tests/test_merge_policy.py
git commit -m "feat: add merge-policy with protected-branch carve-out"
```

---

### Task 2: Database columns (auto_merge, approved_at)

**Files:**
- Modify: `src/orchestrator/database.py:159-184`
- Test: `tests/test_database.py` (create if absent)

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_database.py  (add to existing file if present)
"""Schema-migration tests."""

from __future__ import annotations

import pytest

from orchestrator.database import Database


@pytest.mark.asyncio
async def test_new_columns_exist(tmp_path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    await db.initialize()
    try:
        proj_cols = {
            row["name"]
            for row in await db.fetch_all("PRAGMA table_info(projects)")
        }
        task_cols = {
            row["name"] for row in await db.fetch_all("PRAGMA table_info(tasks)")
        }
        assert "auto_merge" in proj_cols
        assert "approved_at" in task_cols
    finally:
        await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_database.py::test_new_columns_exist -v`
Expected: FAIL on `assert "auto_merge" in proj_cols`

- [ ] **Step 3: Add the columns**

In `src/orchestrator/database.py`, inside `initialize()`, extend the additive-column loop (the `for table, column_ddls in (...)` block at lines 159-179). Add `"auto_merge INTEGER NOT NULL DEFAULT 0"` to the `projects` tuple and `"approved_at TEXT"` to the `tasks` tuple:

```python
        for table, column_ddls in (
            (
                "projects",
                (
                    "agent_model TEXT",
                    "agent_model_effort TEXT",
                    "harness TEXT NOT NULL DEFAULT 'opencode'",
                    "auto_merge INTEGER NOT NULL DEFAULT 0",
                ),
            ),
            ("plans", ("spec_path TEXT", "plan_path TEXT")),
            (
                "tasks",
                (
                    "needs_stronger_model INTEGER DEFAULT 0",
                    "escalation_state TEXT",
                    "escalated_to TEXT",
                    "checklist TEXT",
                    "progress_note TEXT",
                    "approved_at TEXT",
                ),
            ),
        ):
```

Also add `auto_merge INTEGER NOT NULL DEFAULT 0` to the `CREATE TABLE IF NOT EXISTS projects` statement (after the `approval_gate` line, ~line 33) so fresh databases get it too:

```python
        approval_gate INTEGER NOT NULL DEFAULT 1,
        auto_merge INTEGER NOT NULL DEFAULT 0,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_database.py::test_new_columns_exist -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/database.py tests/test_database.py
git commit -m "feat: add projects.auto_merge and tasks.approved_at columns"
```

---

### Task 3: auto_merge on project schemas + API

**Files:**
- Modify: `src/orchestrator/models/schemas.py:71-85` (ProjectCreate), `:150-164` (ProjectUpdate), `:208-225` (ProjectResponse)
- Modify: `src/orchestrator/api/projects.py:35-57` (insert)
- Test: `tests/test_api_projects.py`

**Depends on:** Task 2

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_projects.py  (add these tests)
def test_create_project_auto_merge_defaults_false(client, auth_headers):
    resp = client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "amrepo",
            "repo_url": "https://github.com/user/amrepo",
            "model_name": "qwen3-32b",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["auto_merge"] is False


def test_update_project_auto_merge(client, auth_headers, seeded_project_id):
    resp = client.patch(
        f"/api/projects/{seeded_project_id}",
        headers=auth_headers,
        json={"auto_merge": True},
    )
    assert resp.status_code == 200
    assert resp.json()["auto_merge"] is True
```

> Note: use whatever project-creation/seeding fixtures already exist in this test
> file (e.g. `client`, `auth_headers`, and the existing seeded-project fixture).
> Match the names used by the other tests in `tests/test_api_projects.py`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_projects.py -k auto_merge -v`
Expected: FAIL — `auto_merge` missing from response (KeyError / 422 on PATCH).

- [ ] **Step 3: Implementation**

In `schemas.py`, add to `ProjectCreate` (after `approval_gate: bool = True`):

```python
    auto_merge: bool = False
```

Add to `ProjectUpdate` (after `approval_gate: bool | None = None`):

```python
    auto_merge: bool | None = None
```

Add to `ProjectResponse` (after `approval_gate: bool`):

```python
    auto_merge: bool = False
```

In `api/projects.py` `create_project`, add `auto_merge` to the INSERT column list and values. Change the INSERT to include the column and bind `body.auto_merge`:

```python
    await db.execute(
        """INSERT INTO projects
           (id, user_id, name, repo_url, default_branch, approval_gate,
            auto_merge, confidence_threshold, max_retries, max_improvement_cycles,
            lm_studio_url, model_name, harness, agent_model, agent_model_effort)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            project_id,
            user["id"],
            body.name,
            body.repo_url,
            body.default_branch,
            body.approval_gate,
            body.auto_merge,
            body.confidence_threshold,
            body.max_retries,
            body.max_improvement_cycles,
            body.lm_studio_url,
            body.model_name,
            body.harness,
            body.agent_model,
            body.agent_model_effort,
        ),
    )
```

For the PATCH/update handler in `api/projects.py`: find the `update_project` function (it builds a dynamic `SET` clause from the non-None fields of `ProjectUpdate`). Ensure `auto_merge` is included in the set of updatable columns. If the handler iterates `body.model_dump(exclude_unset=True)`, no change is needed beyond the schema field; if it has an explicit allow-list of columns, add `"auto_merge"` to it. Read the function and follow its existing pattern.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_api_projects.py -k auto_merge -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/models/schemas.py src/orchestrator/api/projects.py tests/test_api_projects.py
git commit -m "feat: expose auto_merge on project create/update/response"
```

---

### Task 4: TaskQueue mark_passed / mark_merged helpers

**Files:**
- Modify: `src/orchestrator/core/task_queue.py` (near `update_task_status`, ~line 112)
- Test: `tests/test_task_queue.py`

**Depends on:** Task 2

- [ ] **Step 1: Write the failing test**

```python
# tests/test_task_queue.py  (add tests; reuse existing queue/seeded fixtures)
import pytest

from orchestrator.models.schemas import TaskStatus


@pytest.mark.asyncio
async def test_mark_passed_sets_status_and_feedback(task_queue, seeded_task_id):
    await task_queue.mark_passed(seeded_task_id, "looks good")
    task = await task_queue.get_task(seeded_task_id)
    assert task["status"] == TaskStatus.PASSED
    assert task["review_feedback"] == "looks good"


@pytest.mark.asyncio
async def test_mark_merged_sets_status_and_approved_at(task_queue, seeded_task_id):
    await task_queue.mark_merged(seeded_task_id)
    task = await task_queue.get_task(seeded_task_id)
    assert task["status"] == TaskStatus.MERGED
    assert task["approved_at"] is not None
```

> Use the existing `task_queue` and seeded-task fixtures from `tests/conftest.py`
> / `tests/test_task_queue.py`. Match their actual names.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_task_queue.py -k "mark_passed or mark_merged" -v`
Expected: FAIL — `AttributeError: 'TaskQueue' object has no attribute 'mark_passed'`

- [ ] **Step 3: Implementation**

Add to `TaskQueue` in `task_queue.py`, right after `update_task_status`:

```python
    async def mark_passed(self, task_id: str, feedback: str) -> None:
        """Park a reviewed-clean task awaiting human merge approval."""
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """UPDATE tasks
               SET status = ?, review_feedback = ?, updated_at = ?
               WHERE id = ?""",
            (TaskStatus.PASSED, feedback, now, task_id),
        )

    async def mark_merged(self, task_id: str) -> None:
        """Mark a task merged and stamp the approval time."""
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """UPDATE tasks
               SET status = ?, approved_at = ?, updated_at = ?
               WHERE id = ?""",
            (TaskStatus.MERGED, now, now, task_id),
        )
```

(`datetime`, `UTC`, and `TaskStatus` are already imported in this file.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_task_queue.py -k "mark_passed or mark_merged" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/task_queue.py tests/test_task_queue.py
git commit -m "feat: add TaskQueue.mark_passed and mark_merged"
```

---

### Task 5: Gate review_task (default park, opt-in auto-merge)

**Files:**
- Modify: `src/orchestrator/core/orchestrator.py:321-332` (the `if verdict == "pass"` block)
- Test: `tests/test_orchestrator.py`

**Depends on:** Task 1, Task 4

- [ ] **Step 1: Write the failing test**

```python
# tests/test_orchestrator.py  (add tests; reuse existing orchestrator-build helpers)
import pytest

from orchestrator.models.schemas import TaskStatus


@pytest.mark.asyncio
async def test_review_pass_parks_when_not_auto_merge(orch_passing_review):
    """Default (auto_merge off): PASS parks at PASSED and never merges."""
    orch, mocks = orch_passing_review(auto_merge=0, base_branch="plan/x")
    await orch.review_task(mocks.task_id, mocks.project)
    mocks.git.merge_pr.assert_not_called()
    task = await orch._tq.get_task(mocks.task_id)
    assert task["status"] == TaskStatus.PASSED
    assert any(e["type"] == "task_awaiting_merge" for e in mocks.published)


@pytest.mark.asyncio
async def test_review_pass_auto_merges_nonprotected(orch_passing_review):
    orch, mocks = orch_passing_review(auto_merge=1, base_branch="plan/x")
    await orch.review_task(mocks.task_id, mocks.project)
    mocks.git.merge_pr.assert_called_once()
    task = await orch._tq.get_task(mocks.task_id)
    assert task["status"] == TaskStatus.MERGED


@pytest.mark.asyncio
async def test_review_pass_auto_merge_blocked_on_protected(orch_passing_review):
    orch, mocks = orch_passing_review(auto_merge=1, base_branch="main")
    await orch.review_task(mocks.task_id, mocks.project)
    mocks.git.merge_pr.assert_not_called()
    task = await orch._tq.get_task(mocks.task_id)
    assert task["status"] == TaskStatus.PASSED
```

> **Fixture note:** `tests/test_orchestrator.py` already constructs an
> `Orchestrator` with mocked `git`/`opus`/`bus` for the existing review tests.
> Build `orch_passing_review` as a local helper/fixture that: seeds a plan with
> `plan_branch_name=base_branch` and a task in `REVIEWING` with a `pr_url`;
> stubs `opus.is_available()`→True and `opus.review_diff()`→`{"verdict":"pass","feedback":"ok"}`;
> stubs `git.extract_pr_number`, `git.repo_slug`, `git.clone_pr_head`,
> `git.get_pr_diff`, `git.merge_pr`; captures `bus.publish` calls into a list;
> and passes a `project` dict with `auto_merge` and `default_branch="main"`.
> Mirror the setup of the existing `review_task` test in this file.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_orchestrator.py -k "review_pass" -v`
Expected: FAIL — current code always merges, so the park/blocked tests fail.

- [ ] **Step 3: Implementation**

In `orchestrator.py`, add the import near the other `core` imports at the top:

```python
from orchestrator.core.merge_policy import auto_merge_eligible
```

Replace the `if verdict == "pass":` block (lines 321-332) with:

```python
        if verdict == "pass":
            base_branch = plan.get("plan_branch_name") if plan else None
            if auto_merge_eligible(project, base_branch):
                await self._git.merge_pr(".", pr_number, repo=repo)
                await self._tq.mark_merged(task_id)
                await self._sync_plan_checkbox(task)
                self._bus.publish(
                    {
                        "type": "task_completed",
                        "task_id": task_id,
                        "pr_url": task["pr_url"],
                    }
                )
                return
            # Default: park the reviewed PR for explicit human approval.
            await self._tq.mark_passed(task_id, feedback)
            self._bus.publish(
                {
                    "type": "task_awaiting_merge",
                    "task_id": task_id,
                    "pr_url": task["pr_url"],
                    "verdict": verdict,
                    "review_summary": feedback,
                    "branch": task["branch_name"],
                }
            )
            return
```

Note: `plan` is the local variable already assigned earlier in `review_task`
(`plan = await self._tq.get_plan(task["plan_id"])`). `mark_merged` replaces the
old `update_task_status(task_id, TaskStatus.MERGED)` so the merged path also
stamps `approved_at`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_orchestrator.py -k "review_pass" -v`
Expected: PASS (park / auto-merge / protected-blocked all green)

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: gate merge behind auto_merge + protected-branch rule"
```

---

### Task 6: Orchestrator approve/reject merge methods

**Files:**
- Modify: `src/orchestrator/core/orchestrator.py` (add two methods near `review_task`)
- Test: `tests/test_orchestrator.py`

**Depends on:** Task 4

- [ ] **Step 1: Write the failing test**

```python
# tests/test_orchestrator.py  (add)
import pytest

from orchestrator.models.schemas import TaskStatus


@pytest.mark.asyncio
async def test_approve_task_merge_merges_passed(orch_parked_task):
    orch, mocks = orch_parked_task()  # task already in PASSED with pr_url
    await orch.approve_task_merge(mocks.task_id, mocks.project)
    mocks.git.merge_pr.assert_called_once()
    task = await orch._tq.get_task(mocks.task_id)
    assert task["status"] == TaskStatus.MERGED
    assert task["approved_at"] is not None


@pytest.mark.asyncio
async def test_approve_task_merge_rejects_non_passed(orch_parked_task):
    orch, mocks = orch_parked_task(status=TaskStatus.REVIEWING)
    with pytest.raises(ValueError, match="not awaiting merge"):
        await orch.approve_task_merge(mocks.task_id, mocks.project)
    mocks.git.merge_pr.assert_not_called()


@pytest.mark.asyncio
async def test_reject_task_merge_comments_and_fails(orch_parked_task):
    orch, mocks = orch_parked_task()
    await orch.reject_task_merge(mocks.task_id, mocks.project, "please redo")
    mocks.git.comment_on_pr.assert_called_once()
    task = await orch._tq.get_task(mocks.task_id)
    # FAILED, then retried back to PENDING if attempts remain.
    assert task["status"] in (TaskStatus.FAILED, TaskStatus.PENDING)
```

> Build `orch_parked_task` like `orch_passing_review` from Task 5, but seed the
> task already in `PASSED` (or the passed `status=`) with a `pr_url`, and stub
> `git.comment_on_pr`. `project` carries `max_retries` (e.g. 3) and `repo_url`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_orchestrator.py -k "task_merge" -v`
Expected: FAIL — `AttributeError: ... has no attribute 'approve_task_merge'`

- [ ] **Step 3: Implementation**

Add these two methods to `Orchestrator` (place them right after `review_task`):

```python
    async def approve_task_merge(
        self, task_id: str, project: dict[str, Any]
    ) -> None:
        """Merge a human-approved, review-passed task.

        Raises:
            ValueError: If the task is missing or not in the PASSED state.
        """
        task = await self._tq.get_task(task_id)
        if task is None:
            raise ValueError(f"Task {task_id} not found")
        if task["status"] != TaskStatus.PASSED or task["pr_url"] is None:
            raise ValueError(f"Task {task_id} is not awaiting merge")

        pr_number = await self._git.extract_pr_number(task["pr_url"])
        repo = self._git.repo_slug(task["pr_url"]) or self._git.repo_slug(
            project["repo_url"]
        )
        await self._git.merge_pr(".", pr_number, repo=repo)
        await self._tq.mark_merged(task_id)
        await self._sync_plan_checkbox(task)
        self._bus.publish(
            {
                "type": "task_completed",
                "task_id": task_id,
                "pr_url": task["pr_url"],
            }
        )

    async def reject_task_merge(
        self, task_id: str, project: dict[str, Any], feedback: str | None = None
    ) -> None:
        """Reject a parked merge: comment, fail, and re-dispatch if attempts remain.

        Raises:
            ValueError: If the task is missing or not in the PASSED state.
        """
        task = await self._tq.get_task(task_id)
        if task is None:
            raise ValueError(f"Task {task_id} not found")
        if task["status"] != TaskStatus.PASSED or task["pr_url"] is None:
            raise ValueError(f"Task {task_id} is not awaiting merge")

        message = feedback or "Merge rejected by user."
        pr_number = await self._git.extract_pr_number(task["pr_url"])
        repo = self._git.repo_slug(task["pr_url"]) or self._git.repo_slug(
            project["repo_url"]
        )
        await self._git.comment_on_pr(".", pr_number, message, repo=repo)
        await self._tq.fail_task(task_id, message)
        if int(task["attempt"]) < int(project["max_retries"]):
            await self._tq.retry_task(task_id)
            self._bus.publish(
                {
                    "type": "task_retry",
                    "task_id": task_id,
                    "attempt": int(task["attempt"]) + 1,
                }
            )
        else:
            self._bus.publish(
                {"type": "task_failed", "task_id": task_id, "feedback": message}
            )
```

(`Any` and `TaskStatus` are already imported in `orchestrator.py`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_orchestrator.py -k "task_merge" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: add approve_task_merge and reject_task_merge"
```

---

### Task 7: Task-level approve/reject REST endpoints

**Files:**
- Modify: `src/orchestrator/api/tasks.py`
- Test: `tests/test_api_tasks.py`

**Depends on:** Task 6

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_tasks.py  (add)
def test_approve_merge_endpoint(client, auth_headers, monkeypatch):
    called = {}

    async def fake_approve(task_id, project):
        called["task_id"] = task_id

    # A task in PASSED status must already be seeded; reuse the file's
    # task-seeding helper and set its status to "passed".
    task_id = _seed_passed_task(client)  # use the file's existing seed pattern
    monkeypatch.setattr(
        client.app.state.orchestrator, "approve_task_merge", fake_approve
    )
    resp = client.post(
        f"/api/tasks/{task_id}/approve-merge", headers=auth_headers
    )
    assert resp.status_code == 200
    assert called["task_id"] == task_id


def test_approve_merge_unknown_task_404(client, auth_headers):
    resp = client.post(
        "/api/tasks/does-not-exist/approve-merge", headers=auth_headers
    )
    assert resp.status_code == 404


def test_reject_merge_endpoint(client, auth_headers, monkeypatch):
    captured = {}

    async def fake_reject(task_id, project, feedback):
        captured["feedback"] = feedback

    task_id = _seed_passed_task(client)
    monkeypatch.setattr(
        client.app.state.orchestrator, "reject_task_merge", fake_reject
    )
    resp = client.post(
        f"/api/tasks/{task_id}/reject-merge",
        headers=auth_headers,
        json={"feedback": "redo"},
    )
    assert resp.status_code == 200
    assert captured["feedback"] == "redo"
```

> Implement `_seed_passed_task` (or inline it) using the same INSERT pattern the
> other tests in `tests/test_api_tasks.py` use to create a plan + task, setting
> the task `status='passed'`, a `pr_url`, and linking it to a seeded project.
> If `app.state.orchestrator` is None in the test app, set it to a `Mock()` in
> the test before monkeypatching (mirror how other tests handle optional state).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_tasks.py -k "approve_merge or reject_merge" -v`
Expected: FAIL — 404/405 (routes do not exist yet).

- [ ] **Step 3: Implementation**

In `api/tasks.py`, add a request model and two endpoints. Add the import for the body model at the top (after existing imports):

```python
from pydantic import BaseModel


class RejectMergeRequest(BaseModel):
    """Optional feedback when rejecting a parked merge."""

    feedback: str | None = None
```

Then the endpoints (place after the existing task routes):

```python
async def _resolve_task_and_project(
    request: Request, task_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load a task and its owning project, or raise 404."""
    queue = request.app.state.task_queue
    db = request.app.state.db
    task = await queue.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        )
    plan = await queue.get_plan(task["plan_id"])
    project = None
    if plan is not None:
        project = await db.fetch_one(
            "SELECT * FROM projects WHERE id = ?", (plan["project_id"],)
        )
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )
    return task, project


@router.post("/tasks/{task_id}/approve-merge")
async def approve_merge(request: Request, task_id: str) -> dict[str, Any]:
    """Approve and merge a review-passed, parked task."""
    _, project = await _resolve_task_and_project(request, task_id)
    orchestrator = request.app.state.orchestrator
    if orchestrator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Orchestrator unavailable",
        )
    try:
        await orchestrator.approve_task_merge(task_id, project)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except Exception as exc:  # noqa: BLE001 - surface merge failure as 502
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"merge failed: {exc}"
        ) from exc
    return {"task_id": task_id, "status": "merged"}


@router.post("/tasks/{task_id}/reject-merge")
async def reject_merge(
    request: Request, task_id: str, body: RejectMergeRequest
) -> dict[str, Any]:
    """Reject a parked merge; re-dispatched if retry attempts remain."""
    _, project = await _resolve_task_and_project(request, task_id)
    orchestrator = request.app.state.orchestrator
    if orchestrator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Orchestrator unavailable",
        )
    try:
        await orchestrator.reject_task_merge(task_id, project, body.feedback)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    return {"task_id": task_id, "status": "rejected"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_api_tasks.py -k "approve_merge or reject_merge" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/api/tasks.py tests/test_api_tasks.py
git commit -m "feat: add task approve-merge and reject-merge endpoints"
```

---

### Task 8: Plan-level approve-merges (batch)

**Files:**
- Modify: `src/orchestrator/api/plans.py`
- Test: `tests/test_api_plans.py`

**Depends on:** Task 6, Task 7

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_plans.py  (add)
def test_approve_merges_batch(client, auth_headers, monkeypatch):
    approved = []

    async def fake_approve(task_id, project):
        approved.append(task_id)

    plan_id, passed_ids = _seed_plan_with_passed_tasks(client, n=2)
    monkeypatch.setattr(
        client.app.state.orchestrator, "approve_task_merge", fake_approve
    )
    resp = client.post(
        f"/api/plans/{plan_id}/approve-merges", headers=auth_headers
    )
    assert resp.status_code == 200
    assert sorted(approved) == sorted(passed_ids)
    assert resp.json()["approved"] == 2
```

> `_seed_plan_with_passed_tasks` seeds one plan + N tasks in `passed` status with
> `pr_url`s, linked to a seeded project. Reuse the INSERT pattern from the
> existing plan/task seeding in `tests/test_api_plans.py`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_plans.py -k approve_merges -v`
Expected: FAIL — route does not exist (404).

- [ ] **Step 3: Implementation**

Add to `api/plans.py`:

```python
@router.post("/plans/{plan_id}/approve-merges")
async def approve_merges(request: Request, plan_id: str) -> dict[str, Any]:
    """Approve and merge every review-passed task in a plan."""
    queue = request.app.state.task_queue
    db = request.app.state.db
    orchestrator = request.app.state.orchestrator
    plan = await queue.get_plan(plan_id)
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found"
        )
    if orchestrator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Orchestrator unavailable",
        )
    project = await db.fetch_one(
        "SELECT * FROM projects WHERE id = ?", (plan["project_id"],)
    )
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )
    tasks = await queue.get_tasks_for_plan(plan_id)
    approved = 0
    errors: list[dict[str, str]] = []
    for task in tasks:
        if task["status"] != TaskStatus.PASSED:
            continue
        try:
            await orchestrator.approve_task_merge(task["id"], project)
            approved += 1
        except Exception as exc:  # noqa: BLE001 - collect, keep going
            errors.append({"task_id": task["id"], "error": str(exc)})
    return {"plan_id": plan_id, "approved": approved, "errors": errors}
```

Ensure `TaskStatus` is imported in `plans.py` (add `from orchestrator.models.schemas import ... TaskStatus` to the existing schemas import if not already present).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_api_plans.py -k approve_merges -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/api/plans.py tests/test_api_plans.py
git commit -m "feat: add plan-level approve-merges batch endpoint"
```

---

### Task 9: MCP poll_task surfaces awaiting_merge

**Files:**
- Modify: `src/mcp_server/server.py:90-101` (`poll_task_impl`)
- Test: `tests/test_mcp_server.py` (or the existing MCP test file)

**Depends on:** Task 5

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mcp_server.py  (add; reuse the file's fake-client pattern)
import pytest

from mcp_server.server import poll_task_impl


@pytest.mark.asyncio
async def test_poll_task_maps_passed_to_awaiting_merge():
    class FakeClient:
        async def get(self, path):
            return {
                "status": "passed",
                "pr_url": "https://github.com/u/r/pull/5",
                "review_feedback": "looks good",
                "branch_name": "agent/foo",
            }

    out = await poll_task_impl(FakeClient(), "t1")
    assert out["status"] == "awaiting_merge"
    assert out["pr_url"].endswith("/pull/5")
    assert out["review"] == "looks good"
    assert out["branch"] == "agent/foo"
    assert out["verdict"] == "pass"


@pytest.mark.asyncio
async def test_poll_task_passthrough_non_passed():
    class FakeClient:
        async def get(self, path):
            return {"status": "in_progress", "pr_url": None, "review_feedback": None}

    out = await poll_task_impl(FakeClient(), "t1")
    assert out["status"] == "in_progress"
    assert out["verdict"] is None
```

> Match the existing MCP test file's name and its fake-client convention (look at
> how other `*_impl` functions are tested there).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_server.py -k poll_task -v`
Expected: FAIL — current impl returns `status="passed"`, no `branch`/`verdict`.

- [ ] **Step 3: Implementation**

Replace the return block of `poll_task_impl` (`server.py:90-101`) with:

```python
async def poll_task_impl(client: Any, task_id: str) -> dict[str, Any]:
    """Return the current status, PR URL, and review of a dispatched task."""
    task = await client.get(f"/api/tasks/{task_id}")
    raw_status = task.get("status")
    awaiting = raw_status == "passed"
    return {
        "task_id": task_id,
        "status": "awaiting_merge" if awaiting else raw_status,
        "pr_url": task.get("pr_url"),
        "review": task.get("review_feedback"),
        "branch": task.get("branch_name"),
        "verdict": "pass" if awaiting else None,
    }
```

> Keep the existing fetch line if the current code differs slightly; the key
> change is mapping `passed`→`awaiting_merge` and adding `branch` + `verdict`.
> Also update the `poll_task` tool docstring (around line 214) to mention the
> `awaiting_merge` status and that the caller should relay the PR for approval.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_server.py -k poll_task -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mcp_server/server.py tests/test_mcp_server.py
git commit -m "feat: MCP poll_task surfaces awaiting_merge state"
```

---

### Task 10: Dependency-gating regression test

**Files:**
- Test: `tests/test_task_queue.py`

**Depends on:** Task 4

- [ ] **Step 1: Write the test (documents existing safe behavior)**

```python
# tests/test_task_queue.py  (add)
import pytest

from orchestrator.models.schemas import TaskStatus


@pytest.mark.asyncio
async def test_passed_task_does_not_unblock_dependents(task_queue, seeded_project_id):
    """A dependent task stays blocked until its upstream is MERGED, not PASSED."""
    plan_id = await task_queue.create_plan(seeded_project_id, source="user")
    opus_plan = {
        "tasks": [
            {"title": "A", "description": "a", "slug": "a", "depends_on": []},
            {"title": "B", "description": "b", "slug": "b", "depends_on": ["a"]},
        ]
    }
    await task_queue.activate_plan(plan_id, opus_plan, "plan/dep-test")
    tasks = await task_queue.get_tasks_for_plan(plan_id)
    task_a = tasks[0]

    # Park A at PASSED — B must still NOT be dispatchable.
    await task_queue.mark_passed(task_a["id"], "ok")
    dispatchable = await task_queue.get_dispatchable_tasks(plan_id)
    assert all(t["title"] != "B" for t in dispatchable)

    # Merge A — now B becomes dispatchable.
    await task_queue.mark_merged(task_a["id"])
    dispatchable = await task_queue.get_dispatchable_tasks(plan_id)
    assert any(t["title"] == "B" for t in dispatchable)
```

> Use the existing `task_queue` fixture and a seeded project id fixture. If
> `activate_plan`'s signature differs, match its actual parameters (see
> `task_queue.py:69`).

- [ ] **Step 2: Run test to verify it passes (no code change expected)**

Run: `uv run pytest tests/test_task_queue.py -k passed_task_does_not_unblock -v`
Expected: PASS — confirms `get_dispatchable_tasks` already gates on `MERGED`.

If it FAILS, the dependency gate is not MERGED-based; fix `get_dispatchable_tasks`
(`task_queue.py:168-170`) so the `all(...)` comparison requires
`TaskStatus.MERGED`, then re-run.

- [ ] **Step 3: Commit**

```bash
git add tests/test_task_queue.py
git commit -m "test: parked PASSED task does not unblock dependents"
```

---

### Task 11: Security docs + CLAUDE.md gotcha

**Files:**
- Modify: `.env.example`, `docs/deployment.md`, `CLAUDE.md`

**Depends on:** Task 3, Task 5

- [ ] **Step 1: Add token guidance to `.env.example`**

Add a comment block near the `GITHUB_TOKEN` / `GH_TOKEN` entry:

```bash
# SECURITY: scope this token least-privilege — contents:write +
# pull_requests:write only. Do NOT grant admin or branch-protection-bypass.
# Praxis parks reviewed PRs for human approval by default (auto_merge off) and
# never auto-merges into a protected branch (main/master/release*). Pair this
# with GitHub branch protection on your default branch so the gate is enforced
# by GitHub even if orchestrator logic is bypassed.
```

- [ ] **Step 2: Add a "Merge approval & security" section to `docs/deployment.md`**

Document: default behavior (park at PASSED, human approves via
`POST /api/tasks/{id}/approve-merge` or the dashboard); the `auto_merge`
per-project opt-in and its protected-branch carve-out; the batch
`POST /api/plans/{id}/approve-merges`; and the least-privilege token + branch
protection recommendation (defense in depth).

- [ ] **Step 3: Add a Gotcha to `CLAUDE.md`**

Under the **Gotchas** section, add:

```markdown
- **Merge is gated by default** — review PASS parks a task at `PASSED` (PR left
  open, `task_awaiting_merge` event emitted), it does NOT auto-merge. Merge
  happens only via `POST /api/tasks/{id}/approve-merge` (or the dashboard /
  plan-level `approve-merges`), or when a project sets `auto_merge=True`. Even
  with `auto_merge=True`, Praxis never auto-merges into a protected branch
  (project default / `main` / `master` / `release*`) — `core/merge_policy.py`.
  MCP `poll_task` reports this as `status: awaiting_merge` so a main brain can
  relay the PR for approval.
```

- [ ] **Step 4: Update the Task State Machine diagram in `CLAUDE.md`**

Change the state machine block to show the gate:

```
PENDING -> IN_PROGRESS -> REVIEWING -> PASSED -> (human approve) -> MERGED
                                    -> FAILED -> (re-dispatch, max 3)
```

- [ ] **Step 5: Commit**

```bash
git add .env.example docs/deployment.md CLAUDE.md
git commit -m "docs: document merge-gate default + least-privilege token"
```

---

### Task 12: Full-suite verification

**Files:** none (verification only)

**Depends on:** Task 1-11

- [ ] **Step 1: Run the full suite with coverage**

Run: `uv run pytest --cov=orchestrator --cov-report=term-missing -v`
Expected: all green, coverage not below the current 88% baseline.

- [ ] **Step 2: Lint, format, type-check**

```bash
uv run ruff format src/ tests/
uv run ruff check --fix src/ tests/
uv run mypy src/orchestrator/ --ignore-missing-imports
```
Expected: no errors.

- [ ] **Step 3: Commit any formatting/lint fixups**

```bash
git add -A
git commit -m "chore: format and lint merge-gate changes" || echo "nothing to commit"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1 (merge_policy), Task 2 (DB columns) — no dependencies, run in parallel.
- **Wave 2:** Task 3 (schemas/API, needs Task 2), Task 4 (TaskQueue helpers, needs Task 2) — parallel.
- **Wave 3:** Task 5 (gate review_task, needs Task 1 + Task 4), Task 6 (approve/reject methods, needs Task 4), Task 10 (dependency regression test, needs Task 4) — parallel.
- **Wave 4:** Task 7 (task endpoints, needs Task 6), Task 9 (MCP poll, needs Task 5) — parallel.
- **Wave 5:** Task 8 (batch endpoint, needs Task 6 + Task 7), Task 11 (docs, needs Task 3 + Task 5) — parallel.
- **Wave 6:** Task 12 (full-suite verification, needs all).

---

## Notes

- **Reuse existing fixtures.** Every API/queue test references fixtures already
  defined in `tests/conftest.py` and the per-file seeding helpers. Do not invent
  new DB plumbing — match the names and patterns the neighboring tests use.
- **`review_feedback` is the review summary.** We intentionally did not add a
  `review_summary` column; `review_feedback` already exists and is what
  `poll_task` returns. The gated PASS path writes the feedback there via
  `mark_passed`.
- **Dependency semantics needed no new code** (Task 10 is a guard test) because
  `get_dispatchable_tasks` and `all_tasks_done` already key on `MERGED`.
- **Dashboard buttons are out of scope for this plan.** The spec mentions a
  dashboard Approve/Reject affordance; the REST + MCP surface here is the
  contract. Wiring `web/index.html` can follow as a separate UI task once these
  endpoints are merged.
