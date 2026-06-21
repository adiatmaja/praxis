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
