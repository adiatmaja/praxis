# Docs-Aware Specs & Plans Views Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `docs/` the source of truth for specs and plans: scan markdown, classify each file (deterministic markers first, Haiku fallback), parse plan checklists into progress, index it in SQLite, and surface Specs/Plans views with progress bars and a Refresh button.

**Architecture:** A `DocIndexer` service walks a configurable docs root, hashes each file, skips unchanged ones, classifies the rest (markers → Haiku via `OpusBridge` → local LM Studio fallback), parses `- [ ]`/`- [x]` into progress, and upserts a thin `doc_index` row. A `/api/docs` router lists/serves/refreshes. The dashboard gains Specs and Plans views.

**Tech Stack:** Python 3.11, FastAPI, aiosqlite (raw SQL), `claude -p --model claude-haiku-4-5` (subscription, via `OpusBridge`), LM Studio fallback (OpenAI-compatible), single-file HTML dashboard.

**Plan-level dependency:** Plan 2 (OpusBridge accepts a per-call `model`) — the Haiku classification call reuses it.

---

### Task 1: `doc_index` table

**Files:**
- Modify: `src/orchestrator/database.py` (append to `MIGRATIONS`, ends line 93)
- Test: `tests/test_database.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_database.py  (add)
async def test_doc_index_table_exists(db):
    rows = await db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='doc_index'"
    )
    assert len(rows) == 1
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_database.py::test_doc_index_table_exists -v`
Expected: FAIL — table missing.

- [ ] **Step 3: Append the migration**

In `src/orchestrator/database.py`, add this string as the last element of the `MIGRATIONS` tuple (before the closing `)` on line 93):

```python
    """
    CREATE TABLE IF NOT EXISTS doc_index (
        path TEXT PRIMARY KEY,
        category TEXT NOT NULL,
        title TEXT,
        content_hash TEXT NOT NULL,
        branch TEXT,
        done_count INTEGER NOT NULL DEFAULT 0,
        total_count INTEGER NOT NULL DEFAULT 0,
        classified_by TEXT NOT NULL DEFAULT 'marker',
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
```

- [ ] **Step 4: Run tests and confirm pass**

Run: `uv run pytest tests/test_database.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/database.py tests/test_database.py
git commit -m "feat: add doc_index table"
```

---

### Task 2: Pure markdown helpers (hash, title, checklist progress)

**Files:**
- Create: `src/orchestrator/core/markdown_utils.py`
- Test: `tests/test_markdown_utils.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_markdown_utils.py
from orchestrator.core.markdown_utils import content_hash, extract_title, checklist_progress

def test_content_hash_stable():
    assert content_hash("abc") == content_hash("abc")
    assert content_hash("abc") != content_hash("abd")

def test_extract_title_from_h1():
    assert extract_title("# My Spec\n\nbody") == "My Spec"

def test_extract_title_none_when_absent():
    assert extract_title("no heading here") is None

def test_checklist_progress_counts_checkboxes():
    md = "- [x] done\n- [ ] todo\n- [X] also done\nnot a box"
    assert checklist_progress(md) == (2, 3)

def test_checklist_progress_zero_when_none():
    assert checklist_progress("plain text") == (0, 0)
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_markdown_utils.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the helpers**

```python
# src/orchestrator/core/markdown_utils.py
"""Pure helpers for parsing markdown docs."""

from __future__ import annotations

import hashlib
import re


_CHECKBOX = re.compile(r"^\s*-\s\[( |x|X)\]\s", re.MULTILINE)
_H1 = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def content_hash(text: str) -> str:
    """Return a stable hex digest of the file content."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_title(text: str) -> str | None:
    """Return the first H1 heading text, or None."""
    match = _H1.search(text)
    return match.group(1) if match else None


def checklist_progress(text: str) -> tuple[int, int]:
    """Return (done, total) markdown task checkboxes."""
    boxes = _CHECKBOX.findall(text)
    done = sum(1 for state in boxes if state in ("x", "X"))
    return done, len(boxes)
```

- [ ] **Step 4: Run tests and confirm pass**

Run: `uv run pytest tests/test_markdown_utils.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/markdown_utils.py tests/test_markdown_utils.py
git commit -m "feat: add markdown parsing helpers"
```

---

### Task 3: Deterministic classifier

**Files:**
- Modify: `src/orchestrator/core/markdown_utils.py`
- Test: `tests/test_markdown_utils.py`

**Depends on:** Task 2

Returns `"spec"`, `"plan"`, or `None` (ambiguous → needs LLM).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_markdown_utils.py  (add)
from orchestrator.core.markdown_utils import classify_by_marker

def test_classify_plan_dir():
    assert classify_by_marker("docs/superpowers/plans/x.md", "# x") == "plan"

def test_classify_spec_dir():
    assert classify_by_marker("docs/superpowers/specs/x.md", "# x") == "spec"

def test_classify_plan_by_checklist():
    assert classify_by_marker("docs/notes/x.md", "## Tasks\n- [ ] do it") == "plan"

def test_classify_frontmatter_type():
    assert classify_by_marker("docs/x.md", "---\ntype: spec\n---\n# x") == "spec"

def test_classify_ambiguous_returns_none():
    assert classify_by_marker("docs/random.md", "just prose") is None
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_markdown_utils.py -v`
Expected: FAIL — `classify_by_marker` undefined.

- [ ] **Step 3: Implement it**

```python
# add to src/orchestrator/core/markdown_utils.py
_FRONTMATTER_TYPE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
_TYPE_LINE = re.compile(r"^type:\s*(spec|plan)\s*$", re.MULTILINE)
_TASKS_HEADING = re.compile(r"^##\s+Tasks\b", re.MULTILINE | re.IGNORECASE)


def classify_by_marker(path: str, text: str) -> str | None:
    """Deterministic classification; None when ambiguous."""
    fm = _FRONTMATTER_TYPE.search(text)
    if fm:
        type_match = _TYPE_LINE.search(fm.group(1))
        if type_match:
            return type_match.group(1)
    normalized = path.replace("\\", "/")
    if "/plans/" in normalized:
        return "plan"
    if "/specs/" in normalized:
        return "spec"
    if _TASKS_HEADING.search(text) and checklist_progress(text)[1] > 0:
        return "plan"
    return None
```

- [ ] **Step 4: Run tests and confirm pass**

Run: `uv run pytest tests/test_markdown_utils.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/markdown_utils.py tests/test_markdown_utils.py
git commit -m "feat: add deterministic doc classifier"
```

---

### Task 4: Haiku classification with local fallback

**Files:**
- Modify: `src/orchestrator/core/opus_bridge.py` (add `classify_doc`)
- Test: `tests/test_opus_bridge.py`

**Depends on:** Plan 2 Task 5 (per-call `model` support)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_opus_bridge.py  (add)
import asyncio

def test_classify_doc_uses_haiku(mocker):
    from orchestrator.core.opus_bridge import OpusBridge
    bridge = OpusBridge(db=mocker.MagicMock())
    run = mocker.patch.object(
        bridge, "_run_claude", new=mocker.AsyncMock(return_value="plan")
    )
    result = asyncio.get_event_loop().run_until_complete(
        bridge.classify_doc("some ambiguous markdown")
    )
    assert result == "plan"
    assert run.call_args.kwargs.get("model") == "claude-haiku-4-5"

def test_classify_doc_normalizes_unexpected_to_other(mocker):
    from orchestrator.core.opus_bridge import OpusBridge
    bridge = OpusBridge(db=mocker.MagicMock())
    mocker.patch.object(bridge, "_run_claude", new=mocker.AsyncMock(return_value="garbage"))
    result = asyncio.get_event_loop().run_until_complete(bridge.classify_doc("x"))
    assert result == "other"
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_opus_bridge.py -v`
Expected: FAIL — `classify_doc` undefined.

- [ ] **Step 3: Implement `classify_doc`**

```python
# add to OpusBridge in src/orchestrator/core/opus_bridge.py
    CLASSIFY_PROMPT = (
        "Classify this markdown document as exactly one word: 'spec', 'plan', or 'other'. "
        "A spec describes WHAT to build; a plan is a step-by-step implementation checklist. "
        "Reply with only the single word.\n\n---\n{text}"
    )

    async def classify_doc(self, text: str) -> str:
        """Classify ambiguous markdown via Haiku; returns spec|plan|other."""
        prompt = self.CLASSIFY_PROMPT.format(text=text[:4000])
        raw = (await self._run_claude(prompt, model="claude-haiku-4-5")).strip().lower()
        for category in ("spec", "plan", "other"):
            if category in raw:
                return category
        return "other"
```

(Local LM Studio fallback is wired in Task 5, where the indexer catches a classification error and falls back — keeping the bridge focused on the subscription path.)

- [ ] **Step 4: Run tests and confirm pass**

Run: `uv run pytest tests/test_opus_bridge.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/opus_bridge.py tests/test_opus_bridge.py
git commit -m "feat: add Haiku doc classification to OpusBridge"
```

---

### Task 5: `DocIndexer` service (scan, cache, classify, upsert)

**Files:**
- Create: `src/orchestrator/core/doc_indexer.py`
- Modify: `src/orchestrator/config.py` (add `docs_root`)
- Test: `tests/test_doc_indexer.py`

**Depends on:** Task 1, Task 3, Task 4

- [ ] **Step 1: Add the `docs_root` setting**

In `src/orchestrator/config.py` add: `docs_root: str = "docs"`.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_doc_indexer.py
import pytest
from orchestrator.core.doc_indexer import DocIndexer

@pytest.fixture
def docs_dir(tmp_path):
    (tmp_path / "specs").mkdir()
    (tmp_path / "plans").mkdir()
    (tmp_path / "specs" / "a.md").write_text("# Spec A\n\nwhat to build", encoding="utf-8")
    (tmp_path / "plans" / "b.md").write_text("# Plan B\n- [x] one\n- [ ] two", encoding="utf-8")
    return tmp_path

async def test_scan_indexes_specs_and_plans(db, docs_dir, mocker):
    classifier = mocker.AsyncMock()  # not called — both are marker-classified
    indexer = DocIndexer(db=db, docs_root=str(docs_dir), classify=classifier)
    await indexer.scan()
    rows = {r["path"]: r for r in await db.fetch_all("SELECT * FROM doc_index")}
    assert any(r["category"] == "spec" for r in rows.values())
    plan = next(r for r in rows.values() if r["category"] == "plan")
    assert (plan["done_count"], plan["total_count"]) == (1, 2)
    classifier.assert_not_awaited()

async def test_scan_skips_unchanged(db, docs_dir, mocker):
    indexer = DocIndexer(db=db, docs_root=str(docs_dir), classify=mocker.AsyncMock())
    first = await indexer.scan()
    second = await indexer.scan()
    assert first["scanned"] >= 2
    assert second["reused"] >= 2

async def test_scan_calls_classifier_for_ambiguous(db, tmp_path, mocker):
    (tmp_path / "loose.md").write_text("# Loose\n\nambiguous prose", encoding="utf-8")
    classify = mocker.AsyncMock(return_value="spec")
    indexer = DocIndexer(db=db, docs_root=str(tmp_path), classify=classify)
    await indexer.scan()
    classify.assert_awaited()
```

- [ ] **Step 3: Run it and confirm it fails**

Run: `uv run pytest tests/test_doc_indexer.py -v`
Expected: FAIL — module not found.

- [ ] **Step 4: Implement the indexer**

```python
# src/orchestrator/core/doc_indexer.py
"""Scan a docs root, classify markdown, and index it in SQLite."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from orchestrator.core.markdown_utils import (
    checklist_progress,
    classify_by_marker,
    content_hash,
    extract_title,
)
from orchestrator.database import Database


logger = logging.getLogger(__name__)

Classifier = Callable[[str], Awaitable[str]]


class DocIndexer:
    """Walks docs_root, classifies markdown, upserts doc_index rows."""

    def __init__(self, db: Database, docs_root: str, classify: Classifier) -> None:
        self._db = db
        self._root = Path(docs_root)
        self._classify = classify

    async def scan(self) -> dict[str, int]:
        scanned = reused = 0
        existing = {
            row["path"]: row["content_hash"]
            for row in await self._db.fetch_all("SELECT path, content_hash FROM doc_index")
        }
        if not self._root.exists():
            return {"scanned": 0, "reused": 0}
        for file in sorted(self._root.rglob("*.md")):
            rel = str(file.relative_to(self._root.parent)).replace("\\", "/")
            text = file.read_text(encoding="utf-8")
            digest = content_hash(text)
            if existing.get(rel) == digest:
                reused += 1
                continue
            category = classify_by_marker(rel, text)
            classified_by = "marker"
            if category is None:
                try:
                    category = await self._classify(text)
                    classified_by = "haiku"
                except Exception as exc:  # noqa: BLE001 - LLM/CLI failure
                    logger.warning("Classification failed for %s: %s", rel, exc)
                    category = "other"
                    classified_by = "fallback"
            done, total = checklist_progress(text)
            await self._upsert(rel, category, extract_title(text), digest, done, total, classified_by)
            scanned += 1
        return {"scanned": scanned, "reused": reused}

    async def _upsert(self, path, category, title, digest, done, total, by) -> None:
        await self._db.execute(
            """
            INSERT INTO doc_index (path, category, title, content_hash, done_count,
                                   total_count, classified_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(path) DO UPDATE SET
                category=excluded.category, title=excluded.title,
                content_hash=excluded.content_hash, done_count=excluded.done_count,
                total_count=excluded.total_count, classified_by=excluded.classified_by,
                updated_at=CURRENT_TIMESTAMP
            """,
            (path, category, title, digest, done, total, by),
        )
```

- [ ] **Step 5: Run tests and confirm pass**

Run: `uv run pytest tests/test_doc_indexer.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/core/doc_indexer.py src/orchestrator/config.py tests/test_doc_indexer.py
git commit -m "feat: add DocIndexer scan/cache/classify/upsert"
```

---

### Task 6: `/api/docs` router

**Files:**
- Create: `src/orchestrator/api/docs.py`
- Modify: `src/orchestrator/models/schemas.py` (add `DocResponse`)
- Test: `tests/test_api_docs.py`

**Depends on:** Task 5

- [ ] **Step 1: Add the response schema**

```python
# add to src/orchestrator/models/schemas.py
class DocResponse(BaseModel):
    path: str
    category: str
    title: str | None = None
    branch: str | None = None
    done_count: int = 0
    total_count: int = 0
    classified_by: str = "marker"
    updated_at: str | None = None
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_api_docs.py
def test_list_docs_filtered(client, auth_headers, db_with_docs):
    r = client.get("/api/docs?category=plan", headers=auth_headers)
    assert r.status_code == 200
    assert all(d["category"] == "plan" for d in r.json())

def test_refresh_docs(client, auth_headers):
    r = client.post("/api/docs/refresh", headers=auth_headers)
    assert r.status_code == 200
    assert "scanned" in r.json()
```

(Add a `db_with_docs` fixture in `tests/conftest.py` that inserts two `doc_index` rows — one spec, one plan.)

- [ ] **Step 3: Run it and confirm it fails**

Run: `uv run pytest tests/test_api_docs.py -v`
Expected: FAIL — 404, route not registered.

- [ ] **Step 4: Implement the router**

```python
# src/orchestrator/api/docs.py
"""Docs index API: list, raw content, refresh."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from orchestrator.api.auth import verify_token
from orchestrator.models.schemas import DocResponse


router = APIRouter(prefix="/api/docs", tags=["docs"])


@router.get("", response_model=list[DocResponse])
async def list_docs(request: Request, category: str | None = None,
                    _: None = Depends(verify_token)) -> list[dict]:
    db = request.app.state.db
    if category:
        return await db.fetch_all(
            "SELECT * FROM doc_index WHERE category = ? ORDER BY updated_at DESC",
            (category,),
        )
    return await db.fetch_all("SELECT * FROM doc_index ORDER BY updated_at DESC")


@router.post("/refresh")
async def refresh_docs(request: Request, _: None = Depends(verify_token)) -> dict:
    return await request.app.state.doc_indexer.scan()


@router.get("/raw")
async def raw_doc(request: Request, path: str, _: None = Depends(verify_token)) -> dict:
    settings = request.app.state.settings
    root = Path(settings.docs_root).parent
    target = (root / path).resolve()
    if not str(target).startswith(str(root.resolve())) or not target.is_file():
        raise HTTPException(status_code=404, detail="doc not found")
    return {"path": path, "content": target.read_text(encoding="utf-8")}
```

(Match the auth dependency to how `api/projects.py` imports it — use the same `verify_token`/`Depends` symbol that the existing routers use.)

- [ ] **Step 5: Register the router**

In `src/orchestrator/main.py`, where other routers are included (the `app.include_router(...)` block), add:

```python
    from orchestrator.api import docs as docs_api
    app.include_router(docs_api.router)
```

- [ ] **Step 6: Run tests and confirm pass**

Run: `uv run pytest tests/test_api_docs.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator/api/docs.py src/orchestrator/models/schemas.py src/orchestrator/main.py tests/test_api_docs.py tests/conftest.py
git commit -m "feat: add /api/docs list/raw/refresh router"
```

---

### Task 7: Wire the indexer into startup

**Files:**
- Modify: `src/orchestrator/main.py` (lifespan, after `OpusBridge` is constructed, ~line 56)
- Test: `tests/test_main_lifespan.py` (or extend existing startup test)

**Depends on:** Task 5, Task 6

- [ ] **Step 1: Write the failing test**

```python
# tests/test_main_lifespan.py  (add)
def test_app_has_doc_indexer(client):
    # client fixture builds the app via lifespan
    assert client.app.state.doc_indexer is not None
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_main_lifespan.py::test_app_has_doc_indexer -v`
Expected: FAIL — `app.state` has no `doc_indexer`.

- [ ] **Step 3: Construct the indexer and run an initial scan**

In `src/orchestrator/main.py` lifespan, after `app.state.opus_bridge = OpusBridge(...)`:

```python
    from orchestrator.core.doc_indexer import DocIndexer
    app.state.doc_indexer = DocIndexer(
        db=database,
        docs_root=settings.docs_root,
        classify=app.state.opus_bridge.classify_doc,
    )
    try:
        await app.state.doc_indexer.scan()
    except Exception as exc:  # noqa: BLE001 - non-fatal at startup
        logger.warning("Initial doc scan failed: %s", exc)
```

- [ ] **Step 4: Run tests and confirm pass**

Run: `uv run pytest tests/test_main_lifespan.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/main.py tests/test_main_lifespan.py
git commit -m "feat: wire DocIndexer into startup with initial scan"
```

---

### Task 8: Specs & Plans dashboard views

**Files:**
- Modify: `web/index.html` (nav items ~510-514, `switchView`, new render functions)

**Depends on:** Task 6

- [ ] **Step 1: Add nav items**

After the Plans nav button (`web/index.html:512`), add:

```html
      <button class="nav-item" type="button" data-view="specs" onclick="switchView('specs')"><span class="nav-icon">F</span>Specs</button>
      <button class="nav-item" type="button" data-view="docplans" onclick="switchView('docplans')"><span class="nav-icon">N</span>Plan Docs</button>
```

- [ ] **Step 2: Add render functions and route them in `switchView`**

In the `switchView` dispatch, add cases for `specs` and `docplans` calling `renderDocs("spec")` / `renderDocs("plan")`. Add:

```javascript
    async function renderDocs(category) {
      const docs = await api("GET", "/api/docs?category=" + category);
      const rows = docs.map(d => {
        const pct = d.total_count ? Math.round(100 * d.done_count / d.total_count) : 0;
        const bar = category === "plan"
          ? '<div class="doc-bar"><div class="doc-bar-fill" style="width:' + pct + '%"></div></div>'
            + '<span class="doc-bar-label">' + d.done_count + '/' + d.total_count + '</span>'
          : "";
        const llm = d.classified_by === "haiku" ? ' <span class="doc-tag">haiku</span>' : "";
        return '<div class="row" onclick="openDoc(\'' + esc(d.path) + '\')">' +
          '<div class="row-main"><div class="row-name">' + esc(d.title || d.path) + '</div>' +
          '<div class="row-sub">' + esc(d.path) + llm + '</div></div>' + bar + '</div>';
      }).join("");
      document.getElementById("view-container").innerHTML =
        '<div class="list-header"><h2>' + (category === "spec" ? "Specs" : "Plan Docs") + '</h2>' +
        '<button class="btn" type="button" onclick="refreshDocs()">Refresh</button></div>' +
        '<div class="list">' + (rows || '<div class="empty">No ' + category + ' docs found</div>') + '</div>';
    }

    async function refreshDocs() {
      await api("POST", "/api/docs/refresh");
      const v = currentView === "specs" ? "spec" : "plan";
      renderDocs(v);
    }

    async function openDoc(path) {
      const doc = await api("GET", "/api/docs/raw?path=" + encodeURIComponent(path));
      alert(doc.content.slice(0, 4000));  // v1: simple viewer; Unit C replaces with editor
    }
```

- [ ] **Step 3: Add minimal styles**

In the `<style>` block add:

```css
    .doc-bar { width: 120px; height: 6px; background: var(--border-subtle); border-radius: 3px; overflow: hidden; }
    .doc-bar-fill { height: 100%; background: var(--badge-passed-text); }
    .doc-bar-label { font-size: 11px; color: var(--text-faint); margin-left: 8px; }
    .doc-tag { font-size: 10px; color: var(--text-faint); border: 1px solid var(--border); border-radius: 4px; padding: 0 4px; }
    .list-header { display: flex; align-items: center; justify-content: space-between; padding: 16px 20px; border-bottom: 1px solid var(--border); }
```

- [ ] **Step 4: Verify in the browser**

Start the server, open the dashboard, click **Specs** and **Plan Docs**. Expected: the specs/plans under `docs/` appear; plan rows show progress bars (e.g. the plan files from this epic); Refresh re-scans; Haiku-classified files (if any) show a `haiku` tag.

- [ ] **Step 5: Commit**

```bash
git add web/index.html
git commit -m "feat(ui): add Specs and Plan Docs views with progress + refresh"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1, Task 2 (no dependencies)
- **Wave 2:** Task 3 (Task 2), Task 4 (Plan 2 Task 5)
- **Wave 3:** Task 5 (Task 1, Task 3, Task 4)
- **Wave 4:** Task 6 (Task 5)
- **Wave 5:** Task 7 (Task 5, Task 6), Task 8 (Task 6)

## Notes

- **Scan root is configurable** (`docs_root`, default `docs`). Indexing each *target repo's*
  `docs/` (after Unit C writes specs into cloned repos) is a follow-up tie-in with Plan 4 — out
  of scope here so this plan ships standalone.
- Local LM Studio fallback for classification lives in `DocIndexer.scan` (the `except` →
  `fallback` path). A richer LM Studio call can replace the `"other"` fallback later without
  changing the interface.
- Per LLM policy, classification uses `claude -p --model claude-haiku-4-5` (subscription) only —
  no API key.
- The simple `alert()` doc viewer in Task 8 is intentional for v1; Unit C (Plan 4) replaces it
  with the spec editor / chat refine flow.
