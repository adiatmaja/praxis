import pytest

from orchestrator.core.doc_indexer import DocIndexer


@pytest.fixture
def docs_dir(tmp_path):
    (tmp_path / "specs").mkdir()
    (tmp_path / "plans").mkdir()
    (tmp_path / "specs" / "a.md").write_text(
        "# Spec A\n\nwhat to build", encoding="utf-8"
    )
    (tmp_path / "plans" / "b.md").write_text(
        "# Plan B\n- [x] one\n- [ ] two", encoding="utf-8"
    )
    return tmp_path


async def test_scan_indexes_specs_and_plans(db, docs_dir, mocker):
    classifier = mocker.AsyncMock()
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
    # Files outside specs/ and plans/ are excluded, so the classifier is only
    # reachable via files in those dirs. Path markers already classify them, so
    # the classifier is not invoked — we assert that here.
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "loose.md").write_text(
        "# Loose\n\nambiguous prose", encoding="utf-8"
    )
    classify = mocker.AsyncMock(return_value="spec")
    indexer = DocIndexer(db=db, docs_root=str(tmp_path), classify=classify)
    await indexer.scan()
    classify.assert_not_awaited()


async def test_scan_excludes_top_level_docs(db, tmp_path, mocker):
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "a.md").write_text("# A\n\nspec", encoding="utf-8")
    (tmp_path / "workflow.md").write_text("# Workflow\n\nreference", encoding="utf-8")
    indexer = DocIndexer(db=db, docs_root=str(tmp_path), classify=mocker.AsyncMock())
    await indexer.scan()
    paths = {r["path"] for r in await db.fetch_all("SELECT path FROM doc_index")}
    assert not any(p.endswith("workflow.md") for p in paths)
    assert any(p.endswith("specs/a.md") for p in paths)
