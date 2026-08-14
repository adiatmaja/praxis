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

_TASK_HEADING = re.compile(
    r"^#{2,4}\s+Task\s+(?P<number>\d+)\s*[:.\-]\s*(?P<title>.+?)\s*$", re.MULTILINE
)
_CHECKBOX_ITEM = re.compile(r"^\s*-\s\[(?: |x|X)\]\s+(.+?)\s*$", re.MULTILINE)

# Matches the generator's "**Depends on:** Task 1" line, tolerating a leading
# list bullet, absent or differently placed emphasis markers, and a missing
# colon. Horizontal whitespace only: \s would let the match run past the end
# of the line and swallow the next one.
_DEPENDS_LINE = re.compile(
    r"^[ \t]*(?:[-*+][ \t]+)?[*_]{0,2}Depends[ \t]+on[*_]{0,2}"
    r"[ \t]*:?[ \t]*[*_]{0,2}[ \t]*(?P<refs>.*)$",
    re.IGNORECASE | re.MULTILINE,
)
_TASK_REFERENCE = re.compile(r"\bTask[ \t]+(\d+)\b", re.IGNORECASE)


def slugify(title: str) -> str:
    """Return a url-safe slug derived from a task title."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return cleaned or "task"


def _referenced_task_numbers(body: str) -> list[int]:
    """Return the task numbers named on this body's first "Depends on" line.

    Args:
        body: The task's own body slice, never the whole document. Searching
            the document would give every task the first dependency line it
            contains.

    Returns:
        Every ``Task <number>`` reference on that line, in the order written.
        "None", an empty line, and a body with no such line all yield [].
    """
    line = _DEPENDS_LINE.search(body)
    if line is None:
        return []
    return [int(number) for number in _TASK_REFERENCE.findall(line.group("refs"))]


def _resolve_dependencies(
    body: str, own_number: int, slug_by_number: dict[int, str]
) -> list[str]:
    """Resolve a task body's stated dependencies to slugs in the same plan.

    Args:
        body: The task's own body slice.
        own_number: The number in this task's own heading.
        slug_by_number: Heading number to slug for every task in the plan.

    Returns:
        Deduplicated slugs, order preserved. A reference to a number that no
        heading declares is dropped rather than passed through, because
        ``TaskQueue.get_dispatchable_tasks`` raises on a slug it cannot
        resolve and would wedge dispatch for the entire plan. A task naming
        its own number is dropped too: it could never become dispatchable.
    """
    resolved: list[str] = []
    for number in _referenced_task_numbers(body):
        if number == own_number:
            continue
        slug = slug_by_number.get(number)
        if slug is None or slug in resolved:
            continue
        resolved.append(slug)
    return resolved


def parse_plan_tasks(text: str) -> list[dict[str, str | list[str]]]:
    """Parse a plan.md into a task list. Returns [] when unstructured.

    Dependencies are resolved by heading NUMBER: a ``**Depends on:** Task 1``
    line resolves to the slug of the task whose heading reads ``Task 1``.
    Title text is deliberately not matched as a fallback, because a loose
    text match can invent a dependency the document never stated, and an
    invented edge either serializes work needlessly or forms a cycle that
    silently stalls the plan forever. Resolution needs two passes so that a
    forward reference resolves.
    """
    headings = list(_TASK_HEADING.finditer(text))
    tasks: list[dict[str, str | list[str]]] = []
    if headings:
        bodies: list[str] = []
        numbers: list[int] = []
        for index, match in enumerate(headings):
            title = match.group("title").strip()
            start = match.end()
            end = (
                headings[index + 1].start() if index + 1 < len(headings) else len(text)
            )
            body = text[start:end]
            bodies.append(body)
            numbers.append(int(match.group("number")))
            tasks.append(
                {
                    "title": title,
                    "slug": slugify(title),
                    "description": body.strip() or title,
                    "depends_on": [],
                }
            )
        slug_by_number: dict[int, str] = {}
        for index, number in enumerate(numbers):
            # Duplicate heading numbers keep the first, matching the way a
            # reader resolves "Task 1" to the first heading that claims it.
            slug_by_number.setdefault(number, str(tasks[index]["slug"]))
        for index, task in enumerate(tasks):
            task["depends_on"] = _resolve_dependencies(
                bodies[index], numbers[index], slug_by_number
            )
        return tasks
    # The checkbox branch keeps depends_on empty: a checklist item is a single
    # line with no body, so there is nowhere for a dependency line to live.
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
