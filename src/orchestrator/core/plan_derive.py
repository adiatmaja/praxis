"""Derive an opus_plan task list from a plan.md document.

Deterministic parsing first; a local LM Studio fallback (added in a later
task) handles unstructured plans. The output dict matches the shape
``TaskQueue.activate_plan`` expects:
``{"plan_summary", "plan_slug", "tasks": [{"title","slug","description","depends_on"}]}``.
"""

from __future__ import annotations

import json
import logging
import re

import httpx


logger = logging.getLogger(__name__)

_TASK_HEADING = re.compile(r"^#{2,4}\s+Task\s+\d+\s*[:.\-]\s*(.+?)\s*$", re.MULTILINE)
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
            end = (
                headings[index + 1].start() if index + 1 < len(headings) else len(text)
            )
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
        "messages": [
            {"role": "user", "content": _DERIVE_PROMPT.format(text=text[:8000])}
        ],
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
