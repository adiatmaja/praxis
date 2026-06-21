"""One-time backfill: persist legacy plans.spec text to repo spec docs."""

from __future__ import annotations

from typing import Any

from orchestrator.database import Database


async def backfill_legacy_specs(db: Database, brainstorm: Any) -> int:
    """For rows with spec text but no spec_path, write a spec doc + set spec_path."""
    cols = {r["name"] for r in await db.fetch_all("PRAGMA table_info(plans)")}
    if "spec" not in cols:
        return 0
    rows = await db.fetch_all(
        "SELECT p.id, p.spec, pr.repo_url FROM plans p "
        "JOIN projects pr ON pr.id = p.project_id "
        "WHERE p.spec_path IS NULL AND p.spec IS NOT NULL AND p.spec != ''"
    )
    count = 0
    for row in rows:
        path = f"docs/superpowers/specs/{row['id']}-legacy.md"
        content = f"# Legacy Spec\n\n{row['spec']}\n"
        brainstorm.write_and_commit(row["repo_url"], path, content)
        await db.execute(
            "UPDATE plans SET spec_path = ? WHERE id = ?", (path, row["id"])
        )
        count += 1
    return count
