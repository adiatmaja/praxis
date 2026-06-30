# Worker Context Continuity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the local-LLM worker a durable, secret-scrubbed Static Bible plus a git-spine Progress Handover that survive both in-run compaction and cross-run re-dispatch, and never hand the worker more context than its model window can hold.

**Architecture:** Three new pure modules (`worker_bible`, `progress_handover`, `token_budget`) assemble/reconstruct/budget the worker's context. The orchestrator runs them at dispatch, writes the result into each harness's always-resent slot (OpenCode `AGENTS.md`, Aider `--read`), and the handover is regenerated deterministically from `git log` + a per-task checklist on every retry. Builds on the existing `detect_context_limit` and `context_scrub`.

**Tech Stack:** Python 3.11, FastAPI, pytest (`asyncio_mode=auto`), aiosqlite (raw SQL), Docker SDK, bash entrypoints, `git`/`gh` CLI.

**Spec:** `docs/superpowers/specs/2026-06-29-worker-context-continuity-design.md`

---

## Background (read this first — assumes zero prior context)

Praxis is an AI agent orchestrator. A **brain** (Claude via `claude -p`, or other providers via `core/llm_router.py`) plans and reviews; a **worker** (OpenCode or Aider in a one-shot Docker container) implements one task: clone → implement → commit → PR → POST `/api/internal/agent-done`. The loop lives in `src/orchestrator/core/orchestrator.py`; container spawn in `core/agent_manager.py`; entrypoints in `docker/<harness>-agent/entrypoint.sh`.

**Today** the worker sees only: the cloned GitHub repo, `TASK_PROMPT`, `PLAN_TEXT`/`PLAN_PATH`, and a curated `CONTEXT_TEXT` (scrubbed by `core/context_scrub.scrub_context`). Two gaps remain:
- **In-run:** OpenCode auto-compacts and can drop the goal/progress; Aider silently truncates on overflow and reports success on partial work.
- **Cross-run:** retries start cold — the next worker does not know what the previous attempt did.

**This plan adds:**
1. **Static Bible** — one scrubbed, budgeted markdown doc (goal, plan slice, caller context, repo memory, working agreement) written into the harness always-resent slot so it survives compaction.
2. **Progress Handover** — done/in-progress/to-do reconstructed deterministically from `git log` + a per-task checklist (worker may add only an untrusted "intent" line); regenerated on every dispatch/retry.
3. **Pre-flight token budgeting** — using the existing `detect_context_limit`, trim the Bible to fit or raise `ContextBudgetExceeded` (a clear failure now; Spec 2 turns it into a decomposition signal).

**Key conventions (from CLAUDE.md / CLAUDE.local.md):**
- Run with `uv run ...`. Tests: `uv run pytest ...`. Format: `uv run ruff format` (NOT `ruff fmt`). Lint: `uv run ruff check --fix`. Types: `uv run mypy src/orchestrator/ --ignore-missing-imports`.
- `pytest-asyncio` `asyncio_mode = "auto"` — async tests need no decorator. Mark `@pytest.mark.unit` / `@pytest.mark.integration`.
- Python 3.11, `X | Y` unions, built-in generics, Google-style docstrings, `logging` (never `print`), raw SQL via aiosqlite (no ORM), inline `ALTER TABLE` migrations guarded by `PRAGMA table_info`.
- **Both `aider-agent:latest` and `opencode-agent:latest` images are standalone and NOT in docker-compose — rebuild after any entrypoint change**, e.g. `docker build -t opencode-agent:latest -f docker/opencode-agent/Dockerfile docker/opencode-agent/`.
- Brain prompts go to the CLI via **stdin**, never argv (Windows ~32K argv limit).
- The `context_text`/`plan_text` plumbing (schemas → dispatch task dict → orchestrator → `spawn_agent` → env var → entrypoint) is the exact precedent to mirror for `bible_text`.

**Verify before starting:** open each file in the File Structure table and confirm the quoted line numbers still match (the codebase moves; anchors are from 2026-06-29).

---

## File Structure

| File | Responsibility |
|------|----------------|
| `src/orchestrator/core/token_budget.py` (new) | Estimate tokens; trim a prioritized section list to a budget; `ContextBudgetExceeded` |
| `src/orchestrator/core/progress_handover.py` (new) | Pure reconstruction of done/in-progress/to-do from `git log` subjects + a checklist |
| `src/orchestrator/core/worker_bible.py` (new) | Assemble + re-scrub + budget the Static Bible (folds handover in) |
| `src/orchestrator/core/git_ops.py` | Add `branch_commit_log(branch)` helper |
| `src/orchestrator/core/agent_manager.py` | Accept `bible_text`; pass as `BIBLE_TEXT` env; reuse `detect_context_limit` |
| `src/orchestrator/core/orchestrator.py` | At dispatch/retry: reconstruct handover, build bible, budget, pass to `spawn_agent` |
| `src/orchestrator/database.py` | `checklist` + `progress_note` columns on `tasks` (guarded migration) |
| `src/orchestrator/models/schemas.py` | `checklist` / `progress_note` on task DTOs |
| `docker/opencode-agent/entrypoint.sh` | Write `BIBLE_TEXT` into `AGENTS.md` (prepend, preserve repo's own) |
| `docker/aider-agent/entrypoint.sh` | `--read .praxis-bible.md` |

---

## Task 1: Token budget primitive

**Files:**
- Create: `src/orchestrator/core/token_budget.py`
- Test: `tests/test_token_budget.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_token_budget.py
import pytest

from orchestrator.core.token_budget import (
    ContextBudgetExceeded,
    Section,
    estimate_tokens,
    fit_sections,
)


@pytest.mark.unit
def test_estimate_tokens_is_chars_over_four():
    assert estimate_tokens("a" * 400) == 100


@pytest.mark.unit
def test_fit_returns_all_when_under_budget():
    sections = [Section("goal", "g" * 40, priority=0),
                Section("docs", "d" * 40, priority=9)]
    kept = fit_sections(sections, context_window=1000, reserve_fraction=0.5)
    assert {s.name for s in kept} == {"goal", "docs"}


@pytest.mark.unit
def test_fit_drops_lowest_priority_first():
    # window 1000, reserve 0.6 -> budget 400 tokens -> 1600 chars.
    sections = [
        Section("goal", "g" * 800, priority=0),    # must keep
        Section("ctx", "c" * 800, priority=1),     # keep if room
        Section("docs", "d" * 4000, priority=9),   # dropped first
    ]
    kept = fit_sections(sections, context_window=1000, reserve_fraction=0.6)
    names = {s.name for s in kept}
    assert "goal" in names
    assert "docs" not in names


@pytest.mark.unit
def test_fit_raises_when_floor_alone_overflows():
    sections = [Section("goal", "g" * 20000, priority=0, floor=True)]
    with pytest.raises(ContextBudgetExceeded):
        fit_sections(sections, context_window=1000, reserve_fraction=0.6)


@pytest.mark.unit
def test_floor_sections_never_dropped():
    sections = [
        Section("goal", "g" * 400, priority=0, floor=True),
        Section("docs", "d" * 400, priority=9),
    ]
    kept = fit_sections(sections, context_window=300, reserve_fraction=0.0)
    # budget 300 tokens = 1200 chars; goal(100)+docs(100)=200 fits -> both kept
    assert len(kept) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_token_budget.py -v`
Expected: FAIL with `ModuleNotFoundError: orchestrator.core.token_budget`

- [ ] **Step 3: Write minimal implementation**

```python
# src/orchestrator/core/token_budget.py
"""Estimate context size and trim prioritized sections to a model's window.

The worker must never be handed more than its loaded context window can hold;
overflow causes silent server-side truncation (Aider) or churny compaction
(OpenCode). We estimate cheaply (chars/4), reserve headroom for the agent's own
reasoning + edits, and drop the lowest-priority sections until the rest fit.
``floor`` sections are never dropped; if they alone overflow we raise.
"""

from __future__ import annotations

from dataclasses import dataclass


_CHARS_PER_TOKEN = 4


class ContextBudgetExceeded(Exception):
    """Raised when mandatory (floor) context alone exceeds the budget."""


@dataclass
class Section:
    """One prioritized chunk of worker context.

    Attributes:
        name: Identifier (for logging/tests).
        text: The content.
        priority: Lower = more important; high-priority kept, low dropped first.
        floor: If True, never dropped (dropping it would make context useless).
    """

    name: str
    text: str
    priority: int
    floor: bool = False


def estimate_tokens(text: str) -> int:
    """Return a conservative token estimate (≈4 chars/token)."""
    return len(text) // _CHARS_PER_TOKEN


def fit_sections(
    sections: list[Section],
    context_window: int,
    reserve_fraction: float = 0.6,
) -> list[Section]:
    """Return the highest-priority sections that fit the budget.

    Args:
        sections: Candidate sections.
        context_window: Model's loaded context window in tokens.
        reserve_fraction: Fraction of the window reserved for the agent's own
            reasoning and edits (so injected context uses ``1 - reserve``).

    Returns:
        The kept sections, original order preserved.

    Raises:
        ContextBudgetExceeded: If the floor sections alone exceed the budget.
    """
    budget = int(context_window * (1.0 - reserve_fraction))
    floor_cost = sum(estimate_tokens(s.text) for s in sections if s.floor)
    if floor_cost > budget:
        msg = f"floor context {floor_cost} tok exceeds budget {budget} tok"
        raise ContextBudgetExceeded(msg)

    kept = [s for s in sections if s.floor]
    remaining = budget - floor_cost
    for s in sorted(
        (s for s in sections if not s.floor), key=lambda s: s.priority
    ):
        cost = estimate_tokens(s.text)
        if cost <= remaining:
            kept.append(s)
            remaining -= cost
    # Restore original ordering.
    order = {id(s): i for i, s in enumerate(sections)}
    return sorted(kept, key=lambda s: order[id(s)])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_token_budget.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/token_budget.py tests/test_token_budget.py
git commit -m "feat: add token budget primitive for worker context"
```

---

## Task 2: Progress handover reconstruction

**Files:**
- Create: `src/orchestrator/core/progress_handover.py`
- Test: `tests/test_progress_handover.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_progress_handover.py
import pytest

from orchestrator.core.progress_handover import (
    ChecklistItem,
    Commit,
    render_handover,
)


@pytest.mark.unit
def test_fresh_run_all_todo():
    items = [ChecklistItem("Add model"), ChecklistItem("Add test")]
    out = render_handover(items, commits=[], worker_note=None)
    assert "PROGRESS (resume here)" in out
    assert "-> in progress: Add model" in out
    assert "[ ] Add test" in out
    assert "[x]" not in out


@pytest.mark.unit
def test_commit_subject_marks_item_done():
    items = [ChecklistItem("Add model"), ChecklistItem("Add test")]
    commits = [Commit(sha="abc1234", subject="agent: Add model")]
    out = render_handover(items, commits=commits, worker_note=None)
    assert "[x] Add model (abc1234)" in out
    assert "-> in progress: Add test" in out


@pytest.mark.unit
def test_worker_note_rendered_untrusted_and_never_marks_done():
    items = [ChecklistItem("Add model")]
    out = render_handover(
        items, commits=[], worker_note="I think the model is done"
    )
    assert "(worker note, unverified)" in out
    assert "I think the model is done" in out
    # No commit -> still not marked done.
    assert "[x]" not in out


@pytest.mark.unit
def test_substring_match_is_case_insensitive_and_trimmed():
    items = [ChecklistItem("Add the User model")]
    commits = [Commit(sha="deadbee", subject="agent: add the user MODEL done")]
    out = render_handover(items, commits=commits, worker_note=None)
    assert "[x] Add the User model (deadbee)" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_progress_handover.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# src/orchestrator/core/progress_handover.py
"""Reconstruct a worker's progress from ground truth (git + checklist).

This is a handover, NOT a model-written summary: completed items are derived
only from real commit subjects, so a weak worker cannot hallucinate progress.
The worker may contribute a single, clearly-marked, untrusted "current intent"
line which never marks an item done.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ChecklistItem:
    """One ordered step of a leaf task."""

    text: str


@dataclass
class Commit:
    """A commit on the task branch."""

    sha: str
    subject: str


def _is_done(item: ChecklistItem, commits: list[Commit]) -> str | None:
    """Return the short sha of the first commit naming ``item``, else None."""
    needle = item.text.strip().lower()
    for c in commits:
        if needle in c.subject.strip().lower():
            return c.sha[:7]
    return None


def render_handover(
    items: list[ChecklistItem],
    commits: list[Commit],
    worker_note: str | None,
) -> str:
    """Render the PROGRESS section from checklist + commits + optional note."""
    lines = ["# PROGRESS (resume here)", ""]
    in_progress_emitted = False
    for item in items:
        sha = _is_done(item, commits)
        if sha:
            lines.append(f"- [x] {item.text} ({sha})")
        elif not in_progress_emitted:
            lines.append(f"- [ ] -> in progress: {item.text}")
            in_progress_emitted = True
        else:
            lines.append(f"- [ ] {item.text}")
    if commits:
        lines += ["", f"Last action: {commits[-1].subject} ({commits[-1].sha[:7]})"]
    if worker_note and worker_note.strip():
        lines += ["", f"> (worker note, unverified) {worker_note.strip()}"]
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_progress_handover.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/progress_handover.py tests/test_progress_handover.py
git commit -m "feat: reconstruct worker progress handover from git + checklist"
```

---

## Task 3: `branch_commit_log` git helper

**Files:**
- Modify: `src/orchestrator/core/git_ops.py`
- Test: `tests/test_git_ops.py`

**Depends on:** Task 2 (uses `progress_handover.Commit`)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_git_ops.py  (append; reuses existing GitOps fixture/patterns)
import pytest

from orchestrator.core.progress_handover import Commit


@pytest.mark.unit
async def test_branch_commit_log_parses_sha_and_subject(git_ops, monkeypatch):
    async def fake_run(argv, cwd=None):
        # git log --format=%H%x1f%s base..branch
        return "abc123def\x1fagent: Add model\n789ghijkl\x1fagent: Add test\n"

    monkeypatch.setattr(git_ops, "_run_capture", fake_run)
    commits = await git_ops.branch_commit_log(".", "main", "agent/x")
    assert commits == [
        Commit(sha="abc123def", subject="agent: Add model"),
        Commit(sha="789ghijkl", subject="agent: Add test"),
    ]


@pytest.mark.unit
async def test_branch_commit_log_empty(git_ops, monkeypatch):
    async def fake_run(argv, cwd=None):
        return ""

    monkeypatch.setattr(git_ops, "_run_capture", fake_run)
    assert await git_ops.branch_commit_log(".", "main", "agent/x") == []
```

> If `git_ops` has no `_run_capture` helper, point the test at whatever existing
> async "run command, return stdout" method `GitOps` uses (check the top of
> `git_ops.py`) and match its name in Step 3.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_git_ops.py -k branch_commit_log -v`
Expected: FAIL with `AttributeError: ... no attribute 'branch_commit_log'`

- [ ] **Step 3: Add the helper**

In `src/orchestrator/core/git_ops.py`, add to `GitOps` (use the unit separator `\x1f` so subjects with spaces parse cleanly):

```python
    async def branch_commit_log(
        self, cwd: str, base_branch: str, branch: str
    ) -> list["Commit"]:
        """Return commits on ``branch`` not on ``base_branch``, oldest first.

        Commit subjects are the spine of the progress handover, so we read them
        verbatim. Returns an empty list when the branch has no extra commits.
        """
        from orchestrator.core.progress_handover import Commit

        out = await self._run_capture(
            [
                "git", "log", "--reverse", "--format=%H%x1f%s",
                f"{base_branch}..{branch}",
            ],
            cwd=cwd,
        )
        commits: list[Commit] = []
        for line in out.splitlines():
            if "\x1f" not in line:
                continue
            sha, subject = line.split("\x1f", 1)
            commits.append(Commit(sha=sha, subject=subject))
        return commits
```

(Add `from orchestrator.core.progress_handover import Commit` under `TYPE_CHECKING` at the top for the annotation if the module uses that pattern.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_git_ops.py -k branch_commit_log -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/git_ops.py tests/test_git_ops.py
git commit -m "feat: add branch_commit_log helper for handover reconstruction"
```

---

## Task 4: `checklist` + `progress_note` columns

**Files:**
- Modify: `src/orchestrator/database.py`
- Modify: `src/orchestrator/models/schemas.py`
- Test: `tests/test_database.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_database.py  (append; reuses existing in-memory db fixture)
import json

import pytest


@pytest.mark.integration
async def test_tasks_table_has_checklist_and_progress_note(db):
    cols = {row["name"] async for row in
            await db.execute("PRAGMA table_info(tasks)")}
    assert "checklist" in cols
    assert "progress_note" in cols
```

> Match the fixture name (`db`) and the row-access style to the existing
> `tests/test_database.py`. If `PRAGMA` rows are tuples there, index `[1]`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_database.py -k checklist -v`
Expected: FAIL (columns absent)

- [ ] **Step 3: Add the guarded migration**

In `src/orchestrator/database.py`, in `initialize()` after the `tasks` table creation, mirror the existing guarded-migration pattern:

```python
    cols = {
        row[1]
        for row in await (await db.execute("PRAGMA table_info(tasks)")).fetchall()
    }
    if "checklist" not in cols:
        await db.execute("ALTER TABLE tasks ADD COLUMN checklist TEXT")
    if "progress_note" not in cols:
        await db.execute("ALTER TABLE tasks ADD COLUMN progress_note TEXT")
```

(Match the exact cursor/fetch idiom already used for other `PRAGMA table_info` guards in this file.)

- [ ] **Step 4: Add to the task DTO**

In `src/orchestrator/models/schemas.py`, on the task response model, add:

```python
    checklist: list[dict] | None = None
    progress_note: str | None = None
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_database.py -k checklist -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/database.py src/orchestrator/models/schemas.py tests/test_database.py
git commit -m "feat: add checklist and progress_note columns to tasks"
```

---

## Task 5: Assemble the Static Bible

**Files:**
- Create: `src/orchestrator/core/worker_bible.py`
- Test: `tests/test_worker_bible.py`

**Depends on:** Task 1 (`token_budget`), Task 2 (`progress_handover`)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_worker_bible.py
import pytest

from orchestrator.core.worker_bible import BibleSources, build_bible


@pytest.mark.unit
def test_goal_is_first_and_scrubbed():
    src = BibleSources(
        goal="Add validation\nAPI_KEY=ghp_abcdef1234567890abcdef1234567890abcd",
        handover="# PROGRESS (resume here)\n- [ ] -> in progress: Add validation",
        plan_slice="Plan: validate input",
        caller_context="Use ruff.",
        repo_memory="# CLAUDE.md\nConventions here",
        context_window=8000,
    )
    bible = build_bible(src)
    assert bible.index("# GOAL") < bible.index("# PROGRESS")
    assert "ghp_abcdef" not in bible          # re-scrubbed
    assert "commit after each completed checklist item" in bible.lower()


@pytest.mark.unit
def test_low_priority_repo_memory_dropped_when_tight():
    src = BibleSources(
        goal="g" * 400,
        handover="h" * 400,
        plan_slice="p" * 400,
        caller_context="c" * 400,
        repo_memory="d" * 40000,            # huge -> dropped first
        context_window=1000,
    )
    bible = build_bible(src)
    assert "# GOAL" in bible
    assert "d" * 1000 not in bible          # repo memory dropped


@pytest.mark.unit
def test_none_sources_are_skipped():
    src = BibleSources(
        goal="Do x", handover="# PROGRESS", plan_slice=None,
        caller_context=None, repo_memory=None, context_window=8000,
    )
    bible = build_bible(src)
    assert "# GOAL" in bible
    assert "# PROGRESS" in bible
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_worker_bible.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# src/orchestrator/core/worker_bible.py
"""Assemble the worker's Static Bible: one scrubbed, budgeted reference doc.

The Bible is written into the harness's always-resent slot so the goal,
conventions, and progress survive compaction. Sources are prioritized; under a
tight token budget the least-important tail (repo memory, then plan slice) is
dropped, but the goal, handover, and caller context are floor sections.
"""

from __future__ import annotations

from dataclasses import dataclass

from orchestrator.core.context_scrub import scrub_context
from orchestrator.core.token_budget import Section, fit_sections


_WORKING_AGREEMENT = (
    "# WORKING AGREEMENT\n"
    "- Keep the GOAL above in view at all times.\n"
    "- Commit after each completed checklist item, naming the item in the "
    "commit subject (this is how progress is tracked across restarts).\n"
    "- Do NOT delete existing functionality or config the task did not ask "
    "you to remove.\n"
)


@dataclass
class BibleSources:
    """Raw inputs for the Bible, highest-value first."""

    goal: str
    handover: str
    context_window: int
    plan_slice: str | None = None
    caller_context: str | None = None
    repo_memory: str | None = None
    reserve_fraction: float = 0.6


def build_bible(src: BibleSources) -> str:
    """Return the assembled, scrubbed, budget-trimmed Bible markdown."""
    raw_sections: list[Section] = [
        Section("goal", f"# GOAL (do not lose this)\n{src.goal}", 0, floor=True),
        Section("handover", src.handover, 1, floor=True),
        Section("agreement", _WORKING_AGREEMENT, 2, floor=True),
    ]
    if src.caller_context:
        raw_sections.append(
            Section("caller", f"# CONTEXT\n{src.caller_context}", 3, floor=True)
        )
    if src.plan_slice:
        raw_sections.append(
            Section("plan", f"# PLAN\n{src.plan_slice}", 4)
        )
    if src.repo_memory:
        raw_sections.append(
            Section("repo", f"# REPO MEMORY\n{src.repo_memory}", 9)
        )

    # Re-scrub every section (repo files may carry secrets); scrub_context
    # returns None for blank input, so guard.
    for s in raw_sections:
        s.text = scrub_context(s.text) or s.text

    kept = fit_sections(raw_sections, src.context_window, src.reserve_fraction)
    return "\n\n".join(s.text for s in kept)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_worker_bible.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/worker_bible.py tests/test_worker_bible.py
git commit -m "feat: assemble scrubbed, budgeted worker Static Bible"
```

---

## Task 6: `bible_text` → container env

**Files:**
- Modify: `src/orchestrator/core/agent_manager.py:72-119`
- Test: `tests/test_agent_manager.py`

**Depends on:** Task 5

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_manager.py  (append; reuses existing fake docker fixture)
import pytest


@pytest.mark.unit
async def test_spawn_agent_sets_bible_env(agent_manager, fake_docker, monkeypatch):
    async def fake_detect(url, model):
        return None

    monkeypatch.setattr(
        "orchestrator.core.agent_manager.detect_context_limit", fake_detect
    )
    await agent_manager.spawn_agent(
        task_id="abcd1234",
        repo_url="https://github.com/o/r",
        branch="agent/x",
        base_branch="main",
        task_prompt="do x",
        model_name="qwen3",
        callback_url="http://cb/",
        bible_text="# GOAL\nDo x",
    )
    env = fake_docker.last_run_kwargs["environment"]
    assert env["BIBLE_TEXT"] == "# GOAL\nDo x"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_manager.py -k bible_env -v`
Expected: FAIL with `TypeError: unexpected keyword argument 'bible_text'`

- [ ] **Step 3: Add the param + env**

In `src/orchestrator/core/agent_manager.py`, in the `spawn_agent` signature after `context_text: str | None = None,` (`:85`):

```python
        bible_text: str | None = None,
```

After the `context_text` env block (`:111-112`):

```python
        if bible_text is not None:
            environment["BIBLE_TEXT"] = bible_text
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_agent_manager.py -k bible_env -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/agent_manager.py tests/test_agent_manager.py
git commit -m "feat: pass Static Bible to agent container as BIBLE_TEXT"
```

---

## Task 7: Orchestrator builds bible + handover at dispatch

**Files:**
- Modify: `src/orchestrator/core/orchestrator.py` (`dispatch_pending_tasks`, ~`:124-161`)
- Test: `tests/test_orchestrator.py`

**Depends on:** Task 3, Task 5, Task 6

- [ ] **Step 1: Write the failing test**

```python
# tests/test_orchestrator.py  (append)
import pytest


@pytest.mark.unit
async def test_dispatch_builds_bible_with_goal_and_handover(orchestrator, monkeypatch):
    captured = {}

    async def fake_spawn(**kwargs):
        captured.update(kwargs)
        return "container123"

    async def fake_commit_log(cwd, base, branch):
        return []  # fresh run

    monkeypatch.setattr(orchestrator._agents, "spawn_agent", fake_spawn)
    monkeypatch.setattr(orchestrator._git, "branch_commit_log", fake_commit_log)
    # Arrange one dispatchable task with a checklist via existing fixtures.
    # (Use the helper that seeds a pending task; set its description/title.)
    await orchestrator.dispatch_pending_tasks()
    bible = captured["bible_text"]
    assert "# GOAL" in bible
    assert "# PROGRESS (resume here)" in bible
```

> Wire the arrange step to the existing orchestrator test fixtures (the file
> already seeds projects/plans/tasks). The assertion is the contract: dispatch
> must pass a `bible_text` containing the goal and the handover.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_orchestrator.py -k builds_bible -v`
Expected: FAIL (`bible_text` not passed / KeyError)

- [ ] **Step 3: Build bible + handover in dispatch**

In `src/orchestrator/core/orchestrator.py`, inside `dispatch_pending_tasks`, before the `spawn_agent(...)` call, add:

```python
            from orchestrator.core.progress_handover import (
                ChecklistItem,
                render_handover,
            )
            from orchestrator.core.worker_bible import BibleSources, build_bible

            raw_checklist = plan_task.get("checklist") or [
                {"text": plan_task.get("description") or plan_task["title"]}
            ]
            items = [ChecklistItem(c["text"]) for c in raw_checklist]
            try:
                commits = await self._git.branch_commit_log(".", base_branch, branch)
            except Exception:  # noqa: BLE001 - fresh/absent branch -> no progress
                commits = []
            handover = render_handover(
                items, commits, plan_task.get("progress_note")
            )

            context_window = await detect_context_limit(
                lm_studio_url, project["model_name"]
            ) or 8192
            bible = build_bible(
                BibleSources(
                    goal=plan_task.get("description") or plan_task["title"],
                    handover=handover,
                    context_window=context_window,
                    plan_slice=plan_task.get("plan_text"),
                    caller_context=plan_task.get("context_text"),
                    repo_memory=None,  # repo files folded in by entrypoint --read
                )
            )
```

Then add `bible_text=bible,` to the `spawn_agent(...)` call. Import `detect_context_limit` from `orchestrator.core.agent_manager` at the top, and resolve `lm_studio_url` the same way `spawn_agent` does (via `self._effective_settings` if present). Wrap the whole bible build in a `try/except ContextBudgetExceeded` that calls `fail_task(task_id, "context for this task exceeds the local model's window; split the task")` and `continue` — this is the clear failure the spec mandates (Spec 2 will replace it with decomposition).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_orchestrator.py -k builds_bible -v`
Expected: PASS

- [ ] **Step 5: Run the full orchestrator suite (no regressions)**

Run: `uv run pytest tests/test_orchestrator.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/core/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: build worker bible and handover at dispatch with budget guard"
```

---

## Task 8: OpenCode entrypoint writes the Bible to AGENTS.md

**Files:**
- Modify: `docker/opencode-agent/entrypoint.sh:95-135`

**Depends on:** Task 6

> No unit harness for bash; verification is container rebuild + smoke. The image
> is standalone and MUST be rebuilt (CLAUDE.md gotcha).

- [ ] **Step 1: Write the Bible into AGENTS.md (always-resent slot)**

In `docker/opencode-agent/entrypoint.sh`, after the clone + branch creation and before `opencode run` (after `:118`, before `:120`), add:

```bash
echo "--- Writing Static Bible to AGENTS.md (persists across compaction) ---"
if [ -n "${BIBLE_TEXT:-}" ]; then
    bible_block=".praxis-bible-tmp.md"
    printf "%s\n" "${BIBLE_TEXT}" > "${bible_block}"
    if [ -f "${WORKSPACE}/AGENTS.md" ]; then
        # Preserve the repo's own AGENTS.md; prepend the Bible in a fenced block.
        {
            echo "<!-- praxis:bible:start -->"
            cat "${bible_block}"
            echo "<!-- praxis:bible:end -->"
            echo ""
            cat "${WORKSPACE}/AGENTS.md"
        } > "${WORKSPACE}/AGENTS.md.new"
        mv "${WORKSPACE}/AGENTS.md.new" "${WORKSPACE}/AGENTS.md"
    else
        {
            echo "<!-- praxis:bible:start -->"
            cat "${bible_block}"
            echo "<!-- praxis:bible:end -->"
        } > "${WORKSPACE}/AGENTS.md"
    fi
    rm -f "${bible_block}"
fi
```

- [ ] **Step 2: Rebuild the standalone image**

```bash
docker build -t opencode-agent:latest -f docker/opencode-agent/Dockerfile docker/opencode-agent/
```

Expected: build succeeds.

- [ ] **Step 3: Smoke-test the AGENTS.md write**

```bash
docker run --rm -e BIBLE_TEXT="# GOAL
Do x" --entrypoint bash opencode-agent:latest -c \
  'WORKSPACE=/home/agent/workspace; mkdir -p "$WORKSPACE"; printf "%s\n" "$BIBLE_TEXT" > "$WORKSPACE/AGENTS.md"; grep -q "# GOAL" "$WORKSPACE/AGENTS.md" && echo OK'
```

Expected: prints `OK`, exit 0. (Full path correctness is verified by the real run; this confirms the write mechanism.)

- [ ] **Step 4: Commit**

```bash
git add docker/opencode-agent/entrypoint.sh
git commit -m "feat: opencode writes Static Bible into AGENTS.md slot"
```

---

## Task 9: Aider entrypoint reads the Bible

**Files:**
- Modify: `docker/aider-agent/entrypoint.sh`

**Depends on:** Task 6

> Verification is container rebuild + smoke; standalone image MUST be rebuilt.

- [ ] **Step 1: Write the Bible file and add `--read`**

In `docker/aider-agent/entrypoint.sh`, in the `read_args` block (the one the `2026-06-24` plan added, near `:102-121`), after the `CONTEXT_TEXT` handling add:

```bash
# Static Bible: goal + handover + conventions, re-sent each message by Aider.
if [ -n "${BIBLE_TEXT:-}" ]; then
    printf "%s\n" "${BIBLE_TEXT}" > /home/agent/workspace/.praxis-bible.md
    read_args+=(--read ".praxis-bible.md")
fi
```

- [ ] **Step 2: Rebuild the standalone image**

```bash
docker build -t aider-agent:latest -f docker/aider-agent/Dockerfile docker/aider-agent/
```

Expected: build succeeds.

- [ ] **Step 3: Smoke-test**

```bash
docker run --rm -e BIBLE_TEXT="# GOAL
Do x" --entrypoint bash aider-agent:latest -c \
  'mkdir -p /home/agent/workspace && printf "%s\n" "$BIBLE_TEXT" > /home/agent/workspace/.praxis-bible.md && grep -q "# GOAL" /home/agent/workspace/.praxis-bible.md && echo OK'
```

Expected: prints `OK`, exit 0.

- [ ] **Step 4: Commit**

```bash
git add docker/aider-agent/entrypoint.sh
git commit -m "feat: aider reads Static Bible via --read"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1, Task 2, Task 4 (no dependencies)
- **Wave 2:** Task 3 (Task 2), Task 5 (Task 1, Task 2)
- **Wave 3:** Task 6 (Task 5)
- **Wave 4:** Task 7 (Task 3, Task 5, Task 6), Task 8 (Task 6), Task 9 (Task 6)

---

## Final Verification

After all tasks:

```bash
uv run ruff format src/ tests/ && uv run ruff check --fix src/ tests/
uv run mypy src/orchestrator/ --ignore-missing-imports
uv run pytest --cov=orchestrator --cov-report=term-missing -v
docker build -t opencode-agent:latest -f docker/opencode-agent/Dockerfile docker/opencode-agent/
docker build -t aider-agent:latest -f docker/aider-agent/Dockerfile docker/aider-agent/
```

Expected: format clean, mypy clean, all tests pass (coverage ≥ 80%), both images build.

## Notes / Out of Scope

- **Capability-aware decomposition + escalation + `execute_plan`** — Spec 2
  (`2026-06-29-capability-aware-execution-design.md`). `ContextBudgetExceeded` is a hard failure
  here; Spec 2 turns it into a decomposition/escalation signal.
- **Mid-run external git inspection** — not attempted; within-run survival relies on the
  always-resent AGENTS.md/`--read` slot + the per-item-commit working agreement.
- **No local/gitignored files mounted** — worker stays GitHub-only; all continuity is scrubbed prose.
