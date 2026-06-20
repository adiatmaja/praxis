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
            for row in await self._db.fetch_all(
                "SELECT path, content_hash FROM doc_index"
            )
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
            await self._upsert(
                rel, category, extract_title(text), digest, done, total, classified_by
            )
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
