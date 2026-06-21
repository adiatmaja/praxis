# Unified Plan Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge the Specs, Plan Docs, and Plans dashboard views into one Plans view where each object moves through Spec → Plan → Run, with repo markdown as the source of truth and any `plan.md` promotable into an executable run.

**Architecture:** A lifecycle object is anchored on a spec markdown file in the *target project repo*. A generated `plan.md` links back to its spec via `spec_path:` YAML front-matter. "Promote to Run" reads the `plan.md`, derives tasks (deterministic parser first; local LM Studio JSON-schema fallback), and creates + dispatches a DB plan that references `spec_path`/`plan_path` — reusing the existing `TaskQueue.activate_plan` machinery so the Run view is unchanged.

**Tech Stack:** Python 3.11, FastAPI, aiosqlite (raw SQL, no ORM), Typer CLI, single-file HTML/JS dashboard, Docker SDK, `claude -p` CLI, LM Studio (OpenAI-compatible) for local models, pytest + pytest-asyncio (`asyncio_mode = "auto"`).

---

## Context for a Zero-Context Engineer

**You are on branch `feat/unified-plan-lifecycle`.** Do not switch branches. The design spec is at `docs/superpowers/specs/2026-06-21-unified-plan-lifecycle-design.md` — read it first.

**Repo layout (only what you need):**
- `src/orchestrator/api/` — FastAPI routers. `plans.py` (plan CRUD), `specs.py` (Create-Spec chat + generate_plan), `docs.py` (doc index), `projects.py`. Routers are included in `src/orchestrator/main.py`.
- `src/orchestrator/core/` — `task_queue.py` (plan/task/run SQLite lifecycle), `brainstorm.py` (clones target repo, runs `claude -p`, writes/commits docs), `orchestrator.py` (the main loop; `plan_and_activate` calls Opus then `activate_plan`), `markdown_utils.py` (pure markdown helpers), `doc_indexer.py`, `git_ops.py` (`clone_with_token`, `commit_and_push`).
- `src/orchestrator/database.py` — `MIGRATIONS` tuple of `CREATE TABLE IF NOT EXISTS` strings; `initialize()` runs them then does additive `ALTER TABLE ... ADD COLUMN` inside `contextlib.suppress(Exception)`.
- `src/orchestrator/models/schemas.py` — Pydantic request/response models.
- `web/index.html` — the entire dashboard (one file). `switchView(name)` swaps views; `renderDocs`, `renderPlansView`, `renderPlanDetail`, `renderOpusPlan` already exist; `api(method, path, body)` is the fetch helper; `esc()` HTML-escapes.
- `tests/` — pytest. `conftest.py` provides `db`, `client` (httpx AsyncClient wired to `app`), `auth_headers`, and `test_settings` fixtures.

**Commands (run from repo root):**
```bash
uv run pytest tests/<file>::<test> -v          # single test
uv run pytest --cov=orchestrator -q            # full suite + coverage (target >=80%)
uv run ruff format src/ tests/                 # format
uv run ruff check --fix src/ tests/            # lint
uv run mypy src/orchestrator/ --ignore-missing-imports
```
Commit messages: `<type>: <desc>` (feat/fix/refactor/docs/test/chore). Do **not** add a Co-Authored-By trailer (attribution is disabled in this user's global settings). Commit after every task.

**Key existing facts to rely on:**
- `TaskQueue.create_plan(project_id, spec, source="user", ...) -> plan_id` inserts a `plans` row.
- `TaskQueue.activate_plan(plan_id, opus_plan: dict, plan_branch_name: str)` sets `status=ACTIVE`, stores `opus_plan` JSON, and inserts one `tasks` row per `opus_plan["tasks"]` (each task dict has `title`, `description`, `slug`, optional `depends_on`). The orchestrator loop then dispatches active plans. **Promote reuses this exact method** — it just supplies a derived `opus_plan` instead of an Opus-generated one.
- `opus_plan` dict shape: `{"plan_summary": str, "plan_slug": str, "tasks": [{"title","slug","description","depends_on":[...]}]}`.
- `brainstorm.BrainstormManager` clones the target repo with `clone_with_token(repo_url, dest, token, depth=...)` from `core/git_ops.py`, operates, then `shutil.rmtree(workspace, ignore_errors=True)`. Its `_base` is a temp workspace dir; `_token` is the GitHub token.
- `markdown_utils` already has `checklist_progress(text)->(done,total)`, `extract_title(text)`, `content_hash`, `classify_by_marker`.
- Project rows have `repo_url` and `lm_studio_url` columns (LM Studio base, e.g. `http://host.docker.internal:1234`).

---

## File Structure

- **Create** `src/orchestrator/core/plan_derive.py` — derive a task list from a `plan.md` (deterministic parse + local LM Studio fallback). One responsibility: markdown → `opus_plan` dict.
- **Create** `tests/test_plan_derive.py` — unit tests for the parser and the fallback.
- **Modify** `src/orchestrator/core/markdown_utils.py` — add `extract_frontmatter_field`.
- **Modify** `tests/test_markdown_utils.py` — test the new helper.
- **Modify** `src/orchestrator/database.py` — add `spec_path` / `plan_path` columns to `plans`.
- **Modify** `tests/test_database.py` — assert the new columns exist.
- **Modify** `src/orchestrator/core/brainstorm.py` — add `list_lifecycle_docs` + `read_doc`; stamp `spec_path:` front-matter instruction into `PLAN_BOOTSTRAP`.
- **Modify** `tests/test_brainstorm.py` — test `list_lifecycle_docs` / `read_doc` with a fake clone.
- **Create** `src/orchestrator/api/lifecycle.py` — `GET /api/projects/{id}/lifecycle` (aggregate specs+plans+runs).
- **Create** `tests/test_api_lifecycle.py` — endpoint tests.
- **Modify** `src/orchestrator/api/plans.py` — add `POST /api/plans/promote`.
- **Modify** `tests/test_api_plans.py` — promote tests.
- **Modify** `src/orchestrator/main.py` — include the `lifecycle` router.
- **Modify** `src/orchestrator/core/doc_indexer.py` — restrict scan to `specs/` and `plans/` dirs.
- **Modify** `tests/test_doc_indexer.py` — assert top-level docs excluded.
- **Modify** `web/index.html` — markdown renderer, unified Plans view, Promote wiring, nav cleanup.

---

## Task 1: `extract_frontmatter_field` markdown helper

**Files:**
- Modify: `src/orchestrator/core/markdown_utils.py`
- Test: `tests/test_markdown_utils.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test** — append to `tests/test_markdown_utils.py`:

```python
from orchestrator.core.markdown_utils import extract_frontmatter_field


def test_extract_frontmatter_field_present():
    text = "---\nspec_path: docs/specs/x.md\ntype: plan\n---\n# Plan\nbody"
    assert extract_frontmatter_field(text, "spec_path") == "docs/specs/x.md"


def test_extract_frontmatter_field_absent():
    assert extract_frontmatter_field("# No frontmatter\nbody", "spec_path") is None


def test_extract_frontmatter_field_quoted():
    text = '---\nspec_path: "docs/specs/y.md"\n---\nbody'
    assert extract_frontmatter_field(text, "spec_path") == "docs/specs/y.md"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_markdown_utils.py -k frontmatter_field -v`
Expected: FAIL with `ImportError: cannot import name 'extract_frontmatter_field'`.

- [ ] **Step 3: Write minimal implementation** — add to `src/orchestrator/core/markdown_utils.py`:

```python
def extract_frontmatter_field(text: str, field: str) -> str | None:
    """Return a top-level YAML front-matter scalar field, or None.

    Only parses the leading ``---``-delimited block. Strips surrounding
    single or double quotes from the value.
    """
    fm = _FRONTMATTER_TYPE.search(text)  # reuses the leading --- ... --- matcher
    if not fm:
        return None
    pattern = re.compile(rf"^{re.escape(field)}:\s*(.+?)\s*$", re.MULTILINE)
    match = pattern.search(fm.group(1))
    if not match:
        return None
    return match.group(1).strip().strip("\"'")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_markdown_utils.py -k frontmatter_field -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/markdown_utils.py tests/test_markdown_utils.py
git commit -m "feat: add extract_frontmatter_field markdown helper"
```

---

## Task 2: Deterministic plan-task parser

**Files:**
- Create: `src/orchestrator/core/plan_derive.py`
- Test: `tests/test_plan_derive.py`

**Depends on:** None

The parser turns a `plan.md` into an `opus_plan` dict. It recognizes `### Task N: Title` / `## Task N: Title` headings (the format produced by the writing-plans skill) and falls back to top-level `- [ ]` checkbox items. Slugs are derived from titles.

- [ ] **Step 1: Write the failing test** — create `tests/test_plan_derive.py`:

```python
import pytest

from orchestrator.core.plan_derive import parse_plan_tasks, slugify


def test_slugify():
    assert slugify("Add Input Validation!") == "add-input-validation"


def test_parse_task_headings():
    text = (
        "# My Plan\n\n"
        "### Task 1: Add validation\n\nValidate the registration body.\n\n"
        "### Task 2: Add tests\n\nWrite pytest cases.\n"
    )
    tasks = parse_plan_tasks(text)
    assert [t["title"] for t in tasks] == ["Add validation", "Add tests"]
    assert tasks[0]["slug"] == "add-validation"
    assert "Validate the registration body." in tasks[0]["description"]


def test_parse_falls_back_to_checkboxes():
    text = "# Plan\n\n- [ ] First thing to do\n- [x] Second thing\n"
    tasks = parse_plan_tasks(text)
    assert [t["title"] for t in tasks] == ["First thing to do", "Second thing"]


def test_parse_returns_empty_when_unstructured():
    assert parse_plan_tasks("# Plan\n\nJust prose, no tasks.") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_plan_derive.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.core.plan_derive'`.

- [ ] **Step 3: Write minimal implementation** — create `src/orchestrator/core/plan_derive.py`:

```python
"""Derive an opus_plan task list from a plan.md document.

Deterministic parsing first; a local LM Studio fallback (added in a later
task) handles unstructured plans. The output dict matches the shape
``TaskQueue.activate_plan`` expects:
``{"plan_summary", "plan_slug", "tasks": [{"title","slug","description","depends_on"}]}``.
"""

from __future__ import annotations

import re


_TASK_HEADING = re.compile(
    r"^#{2,4}\s+Task\s+\d+\s*[:.\-]\s*(.+?)\s*$", re.MULTILINE
)
_CHECKBOX_ITEM = re.compile(r"^\s*-\s\[(?: |x|X)\]\s+(.+?)\s*$", re.MULTILINE)


def slugify(title: str) -> str:
    """Return a url-safe slug derived from a task title."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return cleaned or "task"


def parse_plan_tasks(text: str) -> list[dict[str, str | list[str]]]:
    """Parse a plan.md into a task list. Returns [] when unstructured."""
    headings = list(_TASK_HEADING.finditer(text))
    tasks: list[dict[str, str | list[str]]] = []
    if headings:
        for index, match in enumerate(headings):
            title = match.group(1).strip()
            start = match.end()
            end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            description = text[start:end].strip() or title
            tasks.append(
                {
                    "title": title,
                    "slug": slugify(title),
                    "description": description,
                    "depends_on": [],
                }
            )
        return tasks
    for match in _CHECKBOX_ITEM.finditer(text):
        title = match.group(1).strip()
        tasks.append(
            {
                "title": title,
                "slug": slugify(title),
                "description": title,
                "depends_on": [],
            }
        )
    return tasks
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_plan_derive.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/plan_derive.py tests/test_plan_derive.py
git commit -m "feat: deterministic plan.md task parser"
```

---

## Task 3: Local LM Studio fallback + `derive_opus_plan`

**Files:**
- Modify: `src/orchestrator/core/plan_derive.py`
- Test: `tests/test_plan_derive.py`

**Depends on:** Task 2

`derive_opus_plan` is the public entry point: try the deterministic parser; if it yields no tasks, call the local LM Studio model with a JSON-schema-constrained request. Raises `PlanDeriveError` if both produce nothing.

- [ ] **Step 1: Write the failing test** — append to `tests/test_plan_derive.py`:

```python
from orchestrator.core.plan_derive import PlanDeriveError, derive_opus_plan


async def test_derive_uses_deterministic_when_structured():
    text = "# Plan\n\n### Task 1: Do thing\n\nDetails here.\n"
    plan = await derive_opus_plan(text, lm_studio_url="http://unused:1234")
    assert plan["tasks"][0]["title"] == "Do thing"
    assert "plan_slug" in plan


async def test_derive_calls_lm_studio_when_unstructured(mocker):
    text = "# Plan\n\nUnstructured prose with no tasks."
    fake_tasks = {
        "tasks": [{"title": "Inferred", "slug": "inferred",
                   "description": "d", "depends_on": []}]
    }
    payload = {
        "choices": [{"message": {"content": __import__("json").dumps(fake_tasks)}}]
    }
    mock_resp = mocker.Mock()
    mock_resp.json.return_value = payload
    mock_resp.raise_for_status.return_value = None
    post = mocker.patch("httpx.AsyncClient.post",
                        new=mocker.AsyncMock(return_value=mock_resp))
    plan = await derive_opus_plan(text, lm_studio_url="http://lm:1234")
    assert plan["tasks"][0]["title"] == "Inferred"
    post.assert_awaited()


async def test_derive_raises_when_nothing_derivable(mocker):
    text = "# Plan\n\nprose"
    payload = {"choices": [{"message": {"content": '{"tasks": []}'}}]}
    mock_resp = mocker.Mock()
    mock_resp.json.return_value = payload
    mock_resp.raise_for_status.return_value = None
    mocker.patch("httpx.AsyncClient.post",
                 new=mocker.AsyncMock(return_value=mock_resp))
    with pytest.raises(PlanDeriveError):
        await derive_opus_plan(text, lm_studio_url="http://lm:1234")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_plan_derive.py -k derive -v`
Expected: FAIL with `ImportError: cannot import name 'derive_opus_plan'`.

- [ ] **Step 3: Write minimal implementation** — append to `src/orchestrator/core/plan_derive.py` (add `import json`, `import logging`, `import httpx` to the top imports):

```python
logger = logging.getLogger(__name__)

_TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "slug": {"type": "string"},
                    "description": {"type": "string"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "description"],
            },
        }
    },
    "required": ["tasks"],
}

_DERIVE_PROMPT = (
    "Extract the implementation tasks from this plan document. "
    "Return JSON with a 'tasks' array; each task has title, slug "
    "(url-safe), description, and depends_on (array of slugs, may be empty). "
    "Do not invent tasks that are not in the document.\n\n---\n{text}"
)


class PlanDeriveError(Exception):
    """Raised when no tasks can be derived from a plan document."""


def _finalize(tasks: list[dict], text: str) -> dict:
    from orchestrator.core.markdown_utils import extract_title

    for task in tasks:
        task.setdefault("slug", slugify(str(task["title"])))
        task.setdefault("depends_on", [])
        task.setdefault("description", str(task["title"]))
    summary = extract_title(text) or "Derived plan"
    return {"plan_summary": summary, "plan_slug": slugify(summary), "tasks": tasks}


async def _derive_via_lm_studio(text: str, lm_studio_url: str) -> list[dict]:
    url = lm_studio_url.rstrip("/") + "/v1/chat/completions"
    body = {
        "messages": [{"role": "user", "content": _DERIVE_PROMPT.format(text=text[:8000])}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "tasks", "schema": _TASK_SCHEMA},
        },
        "temperature": 0,
    }
    async with httpx.AsyncClient(timeout=120) as http:
        resp = await http.post(url, json=body)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
    return list(json.loads(content).get("tasks", []))


async def derive_opus_plan(text: str, lm_studio_url: str) -> dict:
    """Derive an opus_plan dict from a plan.md; parser first, local LLM fallback."""
    tasks = parse_plan_tasks(text)
    if not tasks:
        logger.info("Plan unstructured; falling back to local LM Studio derivation")
        tasks = await _derive_via_lm_studio(text, lm_studio_url)
    if not tasks:
        message = "No tasks could be derived from the plan document"
        raise PlanDeriveError(message)
    return _finalize(tasks, text)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_plan_derive.py -v`
Expected: PASS (7 passed total).

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/plan_derive.py tests/test_plan_derive.py
git commit -m "feat: local LM Studio fallback for plan task derivation"
```

---

## Task 4: Add `spec_path` / `plan_path` columns to `plans`

**Files:**
- Modify: `src/orchestrator/database.py:154-162` (the additive ALTER loop)
- Test: `tests/test_database.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test** — append to `tests/test_database.py`:

```python
async def test_plans_has_path_columns(db):
    rows = await db.fetch_all("PRAGMA table_info(plans)")
    names = {r["name"] for r in rows}
    assert "spec_path" in names
    assert "plan_path" in names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_database.py::test_plans_has_path_columns -v`
Expected: FAIL (assert ... in names).

- [ ] **Step 3: Write minimal implementation** — in `src/orchestrator/database.py`, extend the additive-column loop (currently iterating project columns) to also add plan columns. Replace the existing loop body so it covers both tables:

```python
        for table, column_ddls in (
            (
                "projects",
                (
                    "agent_model TEXT",
                    "agent_model_effort TEXT",
                    "harness TEXT NOT NULL DEFAULT 'aider'",
                ),
            ),
            ("plans", ("spec_path TEXT", "plan_path TEXT")),
        ):
            for ddl in column_ddls:
                with contextlib.suppress(Exception):
                    await connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {ddl}"  # noqa: S608
                    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_database.py::test_plans_has_path_columns -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/database.py tests/test_database.py
git commit -m "feat: add spec_path/plan_path columns to plans"
```

---

## Task 5: BrainstormManager `read_doc` + `list_lifecycle_docs`

**Files:**
- Modify: `src/orchestrator/core/brainstorm.py`
- Test: `tests/test_brainstorm.py`

**Depends on:** Task 1

These clone the target repo, read/list `docs/**/specs/*.md` and `docs/**/plans/*.md`, and clean up — mirroring `write_and_commit`/`generate_plan`. `list_lifecycle_docs` returns per-file metadata (path, title, category, done/total, and `spec_path` front-matter for plan docs).

- [ ] **Step 1: Write the failing test** — append to `tests/test_brainstorm.py`:

```python
from pathlib import Path

from orchestrator.core.brainstorm import BrainstormManager


def _seed_repo(workspace: str) -> None:
    root = Path(workspace)
    (root / "docs" / "superpowers" / "specs").mkdir(parents=True)
    (root / "docs" / "superpowers" / "plans").mkdir(parents=True)
    (root / "docs" / "superpowers" / "specs" / "x-design.md").write_text(
        "# X Design\n\nwhat to build", encoding="utf-8"
    )
    (root / "docs" / "superpowers" / "plans" / "x.md").write_text(
        "---\nspec_path: docs/superpowers/specs/x-design.md\n---\n"
        "# X Plan\n- [x] a\n- [ ] b\n",
        encoding="utf-8",
    )


def test_list_lifecycle_docs(tmp_path, mocker):
    mgr = BrainstormManager(str(tmp_path / "ws"), event_bus=None, github_token="t")
    mocker.patch.object(mgr, "_clone_repo", side_effect=lambda url, dest: _seed_repo(dest))
    docs = mgr.list_lifecycle_docs("https://example.com/repo.git")
    specs = [d for d in docs if d["category"] == "spec"]
    plans = [d for d in docs if d["category"] == "plan"]
    assert specs[0]["path"] == "docs/superpowers/specs/x-design.md"
    assert plans[0]["spec_path"] == "docs/superpowers/specs/x-design.md"
    assert (plans[0]["done_count"], plans[0]["total_count"]) == (1, 2)


def test_read_doc(tmp_path, mocker):
    mgr = BrainstormManager(str(tmp_path / "ws"), event_bus=None, github_token="t")
    mocker.patch.object(mgr, "_clone_repo", side_effect=lambda url, dest: _seed_repo(dest))
    content = mgr.read_doc("https://example.com/repo.git", "docs/superpowers/plans/x.md")
    assert "# X Plan" in content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_brainstorm.py -k "lifecycle_docs or read_doc" -v`
Expected: FAIL with `AttributeError: 'BrainstormManager' object has no attribute 'list_lifecycle_docs'`.

- [ ] **Step 3: Write minimal implementation** — add these methods to `BrainstormManager` in `src/orchestrator/core/brainstorm.py` (ensure `from orchestrator.core.markdown_utils import checklist_progress, extract_title, extract_frontmatter_field` is imported at the top):

```python
    def read_doc(self, repo_url: str, path: str) -> str:
        """Clone the repo and return one doc's raw markdown."""
        session_id = uuid.uuid4().hex
        workspace = str(Path(self._base) / session_id)
        Path(workspace).mkdir(parents=True, exist_ok=True)
        self._clone_repo(repo_url, workspace)
        try:
            ws_root = Path(workspace).resolve()
            target = (ws_root / path).resolve()
            if not target.is_relative_to(ws_root) or not target.is_file():
                msg = f"doc not found: {path}"
                raise FileNotFoundError(msg)
            return target.read_text(encoding="utf-8")
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def list_lifecycle_docs(self, repo_url: str) -> list[dict]:
        """Clone the repo and list spec/plan markdown with metadata."""
        session_id = uuid.uuid4().hex
        workspace = str(Path(self._base) / session_id)
        Path(workspace).mkdir(parents=True, exist_ok=True)
        self._clone_repo(repo_url, workspace)
        try:
            root = Path(workspace)
            out: list[dict] = []
            for category in ("spec", "plan"):
                folder = f"{category}s"
                for file in sorted(root.rglob(f"*/{folder}/*.md")):
                    rel = str(file.relative_to(root)).replace("\\", "/")
                    text = file.read_text(encoding="utf-8")
                    done, total = checklist_progress(text)
                    out.append(
                        {
                            "path": rel,
                            "category": category,
                            "title": extract_title(text) or rel,
                            "done_count": done,
                            "total_count": total,
                            "spec_path": extract_frontmatter_field(text, "spec_path"),
                        }
                    )
            return out
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_brainstorm.py -k "lifecycle_docs or read_doc" -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/brainstorm.py tests/test_brainstorm.py
git commit -m "feat: BrainstormManager read_doc and list_lifecycle_docs"
```

---

## Task 6: `spec_path` front-matter in generated plans

**Files:**
- Modify: `src/orchestrator/core/brainstorm.py:19-25` (`PLAN_BOOTSTRAP`)
- Test: `tests/test_brainstorm.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test** — append to `tests/test_brainstorm.py`:

```python
from orchestrator.core.brainstorm import PLAN_BOOTSTRAP


def test_plan_bootstrap_requests_spec_path_frontmatter():
    prompt = PLAN_BOOTSTRAP.format(spec_path="docs/specs/x.md", notes="none")
    assert "spec_path" in prompt
    assert "front-matter" in prompt.lower() or "frontmatter" in prompt.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_brainstorm.py::test_plan_bootstrap_requests_spec_path_frontmatter -v`
Expected: FAIL.

- [ ] **Step 3: Write minimal implementation** — replace `PLAN_BOOTSTRAP` in `src/orchestrator/core/brainstorm.py`:

```python
PLAN_BOOTSTRAP = (
    "Use the superpowers:writing-plans skill. Read the spec at {spec_path}. "
    "Produce a fully self-contained implementation plan — every task executes in a fresh "
    "container with zero prior context, so embed all needed file paths, background, and "
    "acceptance criteria per task. Honor these extra notes: {notes}. "
    "At the very top of the plan file, add YAML front-matter linking back to the spec, "
    "exactly: ---\\nspec_path: {spec_path}\\ntype: plan\\n--- . "
    "Write the plan to docs/superpowers/plans/, commit it, and push it."
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_brainstorm.py::test_plan_bootstrap_requests_spec_path_frontmatter -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/brainstorm.py tests/test_brainstorm.py
git commit -m "feat: generated plans link to spec via front-matter"
```

---

## Task 7: Lifecycle aggregation endpoint

**Files:**
- Create: `src/orchestrator/api/lifecycle.py`
- Create: `tests/test_api_lifecycle.py`
- Modify: `src/orchestrator/main.py` (include the router)

**Depends on:** Task 4, Task 5

Returns one entry per spec doc, joined to its plan doc (by `spec_path` front-matter) and to any DB plan (by `plan_path`), with the furthest stage. Uses `app.state.brainstorm.list_lifecycle_docs` and `task_queue`.

- [ ] **Step 1: Write the failing test** — create `tests/test_api_lifecycle.py`:

```python
import pytest

from tests.conftest import seed_user


@pytest.fixture
async def project_id(db, client):
    await seed_user(db)
    import uuid
    pid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url, model_name) "
        "VALUES (?, ?, ?, ?, ?)",
        (pid, "test-user", "proj", "https://example.com/r.git", "qwen"),
    )
    return pid


async def test_lifecycle_aggregates_specs_plans_runs(db, client, auth_headers,
                                                      project_id, mocker):
    docs = [
        {"path": "docs/superpowers/specs/x-design.md", "category": "spec",
         "title": "X", "done_count": 0, "total_count": 0, "spec_path": None},
        {"path": "docs/superpowers/plans/x.md", "category": "plan", "title": "X Plan",
         "done_count": 1, "total_count": 3,
         "spec_path": "docs/superpowers/specs/x-design.md"},
    ]
    mocker.patch.object(client.app.state.brainstorm, "list_lifecycle_docs",
                        return_value=docs)
    resp = await client.get(f"/api/projects/{project_id}/lifecycle",
                            headers=auth_headers)
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    item = items[0]
    assert item["spec_path"] == "docs/superpowers/specs/x-design.md"
    assert item["plan_path"] == "docs/superpowers/plans/x.md"
    assert item["stage"] == "plan"  # no DB run yet
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_lifecycle.py -v`
Expected: FAIL with 404 (route not registered).

- [ ] **Step 3: Write minimal implementation** — create `src/orchestrator/api/lifecycle.py`:

```python
"""Aggregate spec docs, plan docs, and DB runs into lifecycle objects."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from orchestrator.api.auth import verify_token


router = APIRouter(tags=["lifecycle"], dependencies=[Depends(verify_token)])


@router.get("/projects/{project_id}/lifecycle")
async def list_lifecycle(request: Request, project_id: str) -> list[dict[str, Any]]:
    """One entry per spec, joined to its plan doc and DB run."""
    db = request.app.state.db
    project = await db.fetch_one(
        "SELECT repo_url FROM projects WHERE id = ?", (project_id,)
    )
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )

    try:
        docs = request.app.state.brainstorm.list_lifecycle_docs(project["repo_url"])
    except Exception as exc:  # noqa: BLE001 - surface clone/git failure as 502
        raise HTTPException(status_code=502, detail=f"repo read failed: {exc}") from exc

    specs = [d for d in docs if d["category"] == "spec"]
    plans_by_spec = {
        d["spec_path"]: d for d in docs if d["category"] == "plan" and d.get("spec_path")
    }
    db_plans = await request.app.state.task_queue.get_plans_for_project(project_id)
    runs_by_plan_path = {p["plan_path"]: p for p in db_plans if p.get("plan_path")}

    items: list[dict[str, Any]] = []
    for spec in specs:
        plan_doc = plans_by_spec.get(spec["path"])
        plan_path = plan_doc["path"] if plan_doc else None
        run = runs_by_plan_path.get(plan_path) if plan_path else None
        stage = "run" if run else "plan" if plan_doc else "spec"
        items.append(
            {
                "spec_path": spec["path"],
                "title": spec["title"],
                "plan_path": plan_path,
                "plan_progress": (
                    [plan_doc["done_count"], plan_doc["total_count"]]
                    if plan_doc
                    else None
                ),
                "run_id": run["id"] if run else None,
                "run_status": run["status"] if run else None,
                "stage": stage,
            }
        )
    return items
```

- [ ] **Step 4: Register the router** — in `src/orchestrator/main.py`, find where routers are included (e.g. `app.include_router(plans.router)`) and add alongside them:

```python
from orchestrator.api import lifecycle  # add to the existing api imports

app.include_router(lifecycle.router, prefix="/api")
```

(Match the existing prefix convention — `plans.router` is included with `prefix="/api"`; mirror exactly what the neighboring `include_router` calls use.)

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_api_lifecycle.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/api/lifecycle.py tests/test_api_lifecycle.py src/orchestrator/main.py
git commit -m "feat: lifecycle aggregation endpoint"
```

---

## Task 8: Promote-to-Run endpoint

**Files:**
- Modify: `src/orchestrator/api/plans.py`
- Test: `tests/test_api_plans.py`

**Depends on:** Task 3, Task 4, Task 5

`POST /api/plans/promote {project_id, plan_path}`: read `plan.md` from the repo, derive the opus_plan, create a DB plan storing `spec_path`/`plan_path`, then `activate_plan` to create tasks and let the loop dispatch. Idempotent: if a plan already references this `plan_path`, return it.

- [ ] **Step 1: Write the failing test** — append to `tests/test_api_plans.py` (reuse existing project/seed fixtures in that file; if none, mirror the `project_id` fixture from `test_api_lifecycle.py`):

```python
async def test_promote_creates_and_activates_run(db, client, auth_headers, mocker):
    from tests.conftest import seed_user
    import uuid

    await seed_user(db)
    pid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url, model_name, lm_studio_url) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (pid, "test-user", "p", "https://example.com/r.git", "qwen", "http://lm:1234"),
    )
    plan_md = (
        "---\nspec_path: docs/superpowers/specs/x-design.md\n---\n"
        "# X Plan\n\n### Task 1: Do thing\n\nDetails.\n"
    )
    mocker.patch.object(client.app.state.brainstorm, "read_doc", return_value=plan_md)

    resp = await client.post(
        "/api/plans/promote",
        headers=auth_headers,
        json={"project_id": pid, "plan_path": "docs/superpowers/plans/x.md"},
    )
    assert resp.status_code == 201
    plan = resp.json()
    row = await db.fetch_one("SELECT * FROM plans WHERE id = ?", (plan["id"],))
    assert row["plan_path"] == "docs/superpowers/plans/x.md"
    assert row["spec_path"] == "docs/superpowers/specs/x-design.md"
    assert row["status"] == "active"
    tasks = await db.fetch_all("SELECT * FROM tasks WHERE plan_id = ?", (plan["id"],))
    assert len(tasks) == 1


async def test_promote_missing_doc_returns_404(db, client, auth_headers, mocker):
    from tests.conftest import seed_user
    import uuid

    await seed_user(db)
    pid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url, model_name) "
        "VALUES (?, ?, ?, ?, ?)",
        (pid, "test-user", "p", "https://example.com/r.git", "qwen"),
    )
    mocker.patch.object(client.app.state.brainstorm, "read_doc",
                        side_effect=FileNotFoundError("nope"))
    resp = await client.post(
        "/api/plans/promote",
        headers=auth_headers,
        json={"project_id": pid, "plan_path": "docs/superpowers/plans/missing.md"},
    )
    assert resp.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_plans.py -k promote -v`
Expected: FAIL with 404/405 (route missing).

- [ ] **Step 3: Write minimal implementation** — add to `src/orchestrator/api/plans.py` (add imports `from pydantic import BaseModel`, `from orchestrator.core.plan_derive import derive_opus_plan, PlanDeriveError`, `from datetime import UTC, datetime`):

```python
class PromoteRequest(BaseModel):
    project_id: str
    plan_path: str


@router.post("/plans/promote", status_code=status.HTTP_201_CREATED,
             response_model=PlanResponse)
async def promote_plan(request: Request, body: PromoteRequest) -> dict[str, Any]:
    """Derive tasks from a plan.md and create + activate a runnable plan."""
    db = request.app.state.db
    queue = request.app.state.task_queue
    project = await db.fetch_one(
        "SELECT repo_url, lm_studio_url FROM projects WHERE id = ?",
        (body.project_id,),
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    # Idempotency: reuse an existing run for this plan_path.
    existing = await db.fetch_one(
        "SELECT * FROM plans WHERE project_id = ? AND plan_path = ?",
        (body.project_id, body.plan_path),
    )
    if existing is not None:
        return cast(dict[str, Any], existing)

    try:
        plan_md = request.app.state.brainstorm.read_doc(
            project["repo_url"], body.plan_path
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - clone/git failure
        raise HTTPException(status_code=502, detail=f"repo read failed: {exc}") from exc

    try:
        opus_plan = await derive_opus_plan(
            plan_md, lm_studio_url=project["lm_studio_url"] or ""
        )
    except PlanDeriveError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - local LLM unreachable
        raise HTTPException(status_code=502, detail=f"task derivation failed: {exc}") from exc

    from orchestrator.core.markdown_utils import extract_frontmatter_field

    spec_path = extract_frontmatter_field(plan_md, "spec_path")
    plan_id = await queue.create_plan(
        body.project_id, opus_plan["plan_summary"], source="promoted"
    )
    await db.execute(
        "UPDATE plans SET spec_path = ?, plan_path = ? WHERE id = ?",
        (spec_path, body.plan_path, plan_id),
    )
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    branch = f"plan/{today}-{opus_plan['plan_slug']}"
    await queue.activate_plan(plan_id, opus_plan, branch)

    plan = await queue.get_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=500, detail="promotion failed")
    return cast(dict[str, Any], plan)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_api_plans.py -k promote -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/api/plans.py tests/test_api_plans.py
git commit -m "feat: promote plan.md to a runnable DB plan"
```

---

## Task 9: Restrict DocIndexer to specs/ and plans/ dirs

**Files:**
- Modify: `src/orchestrator/core/doc_indexer.py:41-48` (the scan loop)
- Test: `tests/test_doc_indexer.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test** — append to `tests/test_doc_indexer.py`:

```python
async def test_scan_excludes_top_level_docs(db, tmp_path, mocker):
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "a.md").write_text("# A\n\nspec", encoding="utf-8")
    (tmp_path / "workflow.md").write_text("# Workflow\n\nreference", encoding="utf-8")
    indexer = DocIndexer(db=db, docs_root=str(tmp_path), classify=mocker.AsyncMock())
    await indexer.scan()
    paths = {r["path"] for r in await db.fetch_all("SELECT path FROM doc_index")}
    assert not any(p.endswith("workflow.md") for p in paths)
    assert any(p.endswith("specs/a.md") for p in paths)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_doc_indexer.py::test_scan_excludes_top_level_docs -v`
Expected: FAIL (workflow.md indexed).

- [ ] **Step 3: Write minimal implementation** — in `src/orchestrator/core/doc_indexer.py`, inside the `for file in sorted(self._root.rglob("*.md")):` loop, immediately after computing `rel`, add the guard:

```python
            if "/specs/" not in f"/{rel}" and "/plans/" not in f"/{rel}":
                continue
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_doc_indexer.py -v`
Expected: PASS (existing tests still pass; new test passes).

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/doc_indexer.py tests/test_doc_indexer.py
git commit -m "fix: index only specs/ and plans/ docs"
```

---

## Task 10: Client-side markdown renderer

**Files:**
- Modify: `web/index.html` (add a `renderMarkdown(text)` JS function near the other render helpers)

**Depends on:** None

No JS test harness exists; verify by eye in Task 13's Playwright walk. Keep the renderer tiny — headings, bold/italic, inline code, fenced code, lists, and checkboxes.

- [ ] **Step 1: Add the renderer** — in `web/index.html`, inside the `<script>` block near `function esc(`:

```javascript
    function renderMarkdown(src) {
      const lines = (src || "").replace(/\r\n/g, "\n").split("\n");
      let html = "", inCode = false, inList = false;
      const inline = (s) => esc(s)
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
        .replace(/\*([^*]+)\*/g, '<em>$1</em>');
      for (const raw of lines) {
        if (raw.trim().startsWith("```")) {
          if (!inCode) { if (inList) { html += "</ul>"; inList = false; } html += "<pre><code>"; inCode = true; }
          else { html += "</code></pre>"; inCode = false; }
          continue;
        }
        if (inCode) { html += esc(raw) + "\n"; continue; }
        const cb = raw.match(/^\s*-\s\[( |x|X)\]\s+(.*)$/);
        const li = raw.match(/^\s*-\s+(.*)$/);
        const h = raw.match(/^(#{1,6})\s+(.*)$/);
        if (h) { if (inList) { html += "</ul>"; inList = false; } html += "<h" + h[1].length + ">" + inline(h[2]) + "</h" + h[1].length + ">"; continue; }
        if (cb) { if (!inList) { html += "<ul class='md-checks'>"; inList = true; } const done = cb[1] !== " "; html += "<li>" + (done ? "☑" : "☐") + " " + inline(cb[2]) + "</li>"; continue; }
        if (li) { if (!inList) { html += "<ul>"; inList = true; } html += "<li>" + inline(li[1]) + "</li>"; continue; }
        if (inList) { html += "</ul>"; inList = false; }
        if (raw.trim()) html += "<p>" + inline(raw) + "</p>";
      }
      if (inList) html += "</ul>";
      if (inCode) html += "</code></pre>";
      return html;
    }
```

- [ ] **Step 2: Verify it loads** — `uv run uvicorn orchestrator.main:app --port 8099` then open `http://127.0.0.1:8099`; no console errors. (Full visual check is Task 13.)

- [ ] **Step 3: Commit**

```bash
git add web/index.html
git commit -m "feat: client-side markdown renderer"
```

---

## Task 11: Unified Plans view (list + Spec|Plan|Run segments)

**Files:**
- Modify: `web/index.html` (replace `renderPlansView` usage path; add `loadLifecycle`, `renderLifecycleView`, `renderLifecycleDetail`, `lifecycleSegment` state)

**Depends on:** Task 7, Task 10

The Plans nav item now renders the lifecycle. The master list shows one row per lifecycle object (title + furthest-stage chip). The detail pane has a `[ Spec | Plan | Run ]` segmented control. Spec/Plan segments render markdown via `renderMarkdown` (fetched through a small raw-doc call); Run reuses `renderPlanDetail`.

- [ ] **Step 1: Add state + loader** — near the other `let` view-state declarations:

```javascript
    let lifecycleItems = [];
    let selectedLifecycle = null;
    let lifecycleSegment = "spec";

    async function loadLifecycle() {
      if (!selectedProjectId) { lifecycleItems = []; renderLifecycleView(); return; }
      lifecycleItems = await api("GET", "/api/projects/" + selectedProjectId + "/lifecycle");
      if (!selectedLifecycle && lifecycleItems.length) selectedLifecycle = lifecycleItems[0].spec_path;
      renderLifecycleView();
    }
```

- [ ] **Step 2: Add the view renderer**:

```javascript
    function renderLifecycleView() {
      const c = document.getElementById("view-container");
      const rows = lifecycleItems.map(it =>
        '<button class="master-row' + (selectedLifecycle === it.spec_path ? ' selected' : '') +
        '" type="button" onclick="selectLifecycle(\'' + esc(it.spec_path) + '\')">' +
        '<div class="row-main"><div class="row-name">' + esc(it.title) + '</div>' +
        '<div class="row-meta">' + esc(it.stage) + '</div></div>' + badge(it.stage) + '</button>'
      ).join("") || '<div class="empty-list">No specs yet — use + Create Spec</div>';
      const detail = selectedLifecycle
        ? renderLifecycleDetail(lifecycleItems.find(i => i.spec_path === selectedLifecycle))
        : '<div class="detail-empty">Select an item</div>';
      c.innerHTML =
        '<div class="master-panel"><div class="master-header">' +
        '<div class="master-title">' + lifecycleItems.length + ' Plans</div>' +
        '<button class="btn btn-compact" type="button" onclick="loadLifecycle()">Refresh</button></div>' +
        '<div class="master-list">' + rows + '</div></div>' +
        '<div class="detail-panel">' + detail + '</div>';
    }

    function selectLifecycle(specPath) { selectedLifecycle = specPath; lifecycleSegment = "spec"; renderLifecycleView(); loadLifecycleDoc(); }
    function setLifecycleSegment(seg) { lifecycleSegment = seg; renderLifecycleView(); loadLifecycleDoc(); }
```

- [ ] **Step 3: Add the detail renderer + doc loader** (Run reuses the existing `plans` array + `renderPlanDetail`):

```javascript
    let lifecycleDocCache = {};

    function renderLifecycleDetail(it) {
      if (!it) return '<div class="detail-empty">Not found</div>';
      const seg = (k, label, on) => '<button class="json-toggle-btn' + (lifecycleSegment === k ? ' active' : '') +
        '"' + (on ? '' : ' disabled') + ' type="button" onclick="setLifecycleSegment(\'' + k + '\')">' + label + '</button>';
      const segmented = '<div class="json-toggle">' + seg("spec", "Spec", true) +
        seg("plan", "Plan", !!it.plan_path) + seg("run", "Run", !!it.run_id) + '</div>';
      let body = "";
      if (lifecycleSegment === "run") {
        const run = plans.find(p => p.id === it.run_id);
        body = run ? renderPlanDetail(run) : '<div class="detail-empty">Run not loaded — open Refresh</div>';
      } else {
        const path = lifecycleSegment === "spec" ? it.spec_path : it.plan_path;
        const cached = lifecycleDocCache[path];
        const promote = (lifecycleSegment === "plan" && it.plan_path && !it.run_id)
          ? '<div class="detail-actions"><button class="btn btn-primary" type="button" onclick="promoteLifecycle(\'' + esc(it.plan_path) + '\')">Promote to Run</button></div>'
          : "";
        body = '<div class="detail-content"><div class="detail-card">' +
          (cached === undefined ? "Loading…" : renderMarkdown(cached)) + '</div>' + promote + '</div>';
      }
      return '<div class="detail-header"><div class="detail-title">' + esc(it.title) + '</div>' +
        '<div style="margin-top:8px;">' + segmented + '</div></div>' + body;
    }

    async function loadLifecycleDoc() {
      const it = lifecycleItems.find(i => i.spec_path === selectedLifecycle);
      if (!it || lifecycleSegment === "run") return;
      const path = lifecycleSegment === "spec" ? it.spec_path : it.plan_path;
      if (!path || lifecycleDocCache[path] !== undefined) { renderLifecycleView(); return; }
      try {
        const doc = await api("GET", "/api/docs/raw?path=" + encodeURIComponent(path));
        lifecycleDocCache[path] = doc.content;
      } catch (e) { lifecycleDocCache[path] = "_Could not load " + path + "_"; }
      renderLifecycleView();
    }
```

> Note: `/api/docs/raw` reads from the orchestrator's local docs root. If the target-repo docs are not mirrored locally, add a thin `GET /api/projects/{id}/doc-raw?path=` that calls `brainstorm.read_doc` and use it here instead. Confirm which during implementation by testing against a real project; prefer the per-project endpoint if the local read 404s.

- [ ] **Step 4: Add promote handler**:

```javascript
    async function promoteLifecycle(planPath) {
      const btn = event.target; btn.disabled = true; btn.textContent = "Promoting…";
      try {
        await api("POST", "/api/plans/promote", { project_id: selectedProjectId, plan_path: planPath });
        await loadPlans(); await loadLifecycle();
      } catch (e) { btn.disabled = false; btn.textContent = "Promote to Run"; alert("Promote failed: " + e.message); }
    }
```

- [ ] **Step 5: Commit**

```bash
git add web/index.html
git commit -m "feat: unified Spec|Plan|Run lifecycle view"
```

---

## Task 12: Wire nav to lifecycle; remove Specs/Plan Docs items

**Files:**
- Modify: `web/index.html` (`switchView`, sidebar nav buttons)

**Depends on:** Task 11

- [ ] **Step 1: Repoint the Plans view** — in `switchView`, change the `plans` branch to load the lifecycle and pre-load DB plans (so Run segments resolve):

```javascript
      } else if (name === "plans") {
        setTopbar("Plans", "+ Create Spec");
        await loadPlans();
        await loadLifecycle();
```

- [ ] **Step 2: Remove the Specs and Plan Docs nav buttons** — delete the two `<button ... data-view="specs">` and `<button ... data-view="docplans">` lines in the sidebar (around `web/index.html:919-920`). Leave `specs`/`docplans` handlers in `switchView` harmlessly unused, or delete those branches too.

- [ ] **Step 3: Point the topbar "+ Create Spec" action at the existing chat** — ensure the Plans view's primary action calls `openSpecChat()` (the Create-Spec chat already exists). If `setTopbar`'s action wiring is generic, set the handler so clicking it opens the spec chat while in the Plans view.

- [ ] **Step 4: Verify** — start the server and confirm the sidebar shows `Dashboard · Projects · Plans · Tasks · Live Logs · Memory`; clicking Plans renders the lifecycle list. (Full check in Task 13.)

- [ ] **Step 5: Commit**

```bash
git add web/index.html
git commit -m "feat: nav shows unified Plans; remove Specs/Plan Docs items"
```

---

## Task 13: Playwright verification walk

**Files:**
- Create: `scripts/verify_lifecycle.js` (throwaway verification harness)

**Depends on:** Task 11, Task 12

No automated JS tests exist; this is a manual visual gate using the same approach used during design QC. Requires Node Playwright (`npm i playwright` in a scratch dir, `npx playwright install chromium`).

- [ ] **Step 1: Start the server with a token**

```bash
# .env must set AUTH_TOKEN; default seeded user token = AUTH_TOKEN
uv run uvicorn orchestrator.main:app --host 127.0.0.1 --port 8099
```

- [ ] **Step 2: Write the walk script** `scripts/verify_lifecycle.js`:

```javascript
const { chromium } = require("playwright");
const BASE = "http://127.0.0.1:8099";
const TOKEN = process.env.AUTH_TOKEN || "local-dev-token-praxis";
(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ viewport: { width: 1440, height: 900 } });
  await ctx.addInitScript(t => { localStorage.setItem("praxis_token", t); localStorage.setItem("praxis_theme", "light"); }, TOKEN);
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", e => errors.push(e.message));
  page.on("console", m => { if (m.type() === "error") errors.push(m.text()); });
  await page.goto(BASE, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(1500);
  await page.evaluate(() => window.switchView("plans"));
  await page.waitForTimeout(2000);
  await page.screenshot({ path: "scripts/lifecycle.png" });
  const navText = await page.$$eval(".nav-item", els => els.map(e => e.textContent.trim()));
  console.log("NAV:", navText.join(" | "));
  console.log("ERRORS:", errors.length, errors.slice(0, 10));
  await b.close();
})();
```

- [ ] **Step 3: Run it and confirm**

Run: `node scripts/verify_lifecycle.js`
Expected: `NAV:` contains `Dashboard · Projects · Plans · Tasks · Live Logs · Memory` and **no** Specs / Plan Docs; `ERRORS: 0`. Open `scripts/lifecycle.png` and confirm the Plans view shows the master list + `[ Spec | Plan | Run ]` segmented detail.

- [ ] **Step 4: Run the full backend suite + lint + types**

```bash
uv run pytest --cov=orchestrator -q     # expect >=80% coverage, all green
uv run ruff format src/ tests/
uv run ruff check --fix src/ tests/
uv run mypy src/orchestrator/ --ignore-missing-imports
```

- [ ] **Step 5: Commit**

```bash
git add scripts/verify_lifecycle.js
git commit -m "test: Playwright verification walk for lifecycle view"
```

---

## Parallel Execution Map

- **Wave 1 (no dependencies):** Task 1, Task 2, Task 4, Task 6, Task 9, Task 10
- **Wave 2:** Task 3 (Task 2), Task 5 (Task 1)
- **Wave 3:** Task 7 (Task 4, Task 5), Task 8 (Task 3, Task 4, Task 5)
- **Wave 4:** Task 11 (Task 7, Task 10)
- **Wave 5:** Task 12 (Task 11)
- **Wave 6:** Task 13 (Task 11, Task 12)

## Post-Implementation

- [ ] Full suite green, coverage ≥ 80%, ruff + mypy clean.
- [ ] Update `CLAUDE.md` Project Structure + Gotchas: new `core/plan_derive.py`, `api/lifecycle.py`, Promote endpoint, unified Plans view, doc-index scoping.
- [ ] Open a PR from `feat/unified-plan-lifecycle` → `main`; squash-merge.
- [ ] Then rebase `feat/db-execution-ledger` and `feat/llm-router-settings` on updated `main` before starting Spec 2 / Spec 3.

## Self-Review Notes (author)

- Spec coverage: unified view (T7,T11,T12), markdown render (T10), Promote bridge (T3,T8), derive deterministic+local (T2,T3), front-matter link (T6, consumed in T5/T7), `spec_path`/`plan_path` (T4,T8), classifier scope fix (T9), error handling 404/422/502 + idempotency (T8), progress % (T5 done/total surfaced in T7). All spec sections map to a task.
- Known open point flagged inline in Task 11 Step 3: `/api/docs/raw` reads the orchestrator-local docs root, but lifecycle docs live in the target repo — add a per-project `doc-raw` endpoint (calling `brainstorm.read_doc`) if the local read 404s. This is the one place to validate against a real repo during execution.
