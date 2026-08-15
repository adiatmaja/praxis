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

from orchestrator.core.thinking import effort_param


logger = logging.getLogger(__name__)

_TASK_HEADING = re.compile(
    r"^(?P<hashes>#{2,4})\s+Task\s+(?P<number>\d+)\s*[:.\-]\s*(?P<title>.+?)\s*$",
    re.MULTILINE,
)
_ANY_HEADING = re.compile(r"^(?P<hashes>#{1,6})[ \t]+\S", re.MULTILINE)
_CHECKBOX_ITEM = re.compile(r"^\s*-\s\[(?: |x|X)\]\s+(.+?)\s*$", re.MULTILINE)
_FENCE_DELIMITER = re.compile(r"^[ \t]*(?P<fence>`{3,}|~{3,})[^\n]*$", re.MULTILINE)

# Matches the generator's "**Depends on:** Task 1" line, tolerating a leading
# list bullet, absent or differently placed emphasis markers, and a missing
# colon. Horizontal whitespace only: \s would let the match run past the end
# of the line and swallow the next one.
_DEPENDS_LINE = re.compile(
    r"^[ \t]*(?:[-*+][ \t]+)?[*_]{0,2}Depends[ \t]+on[*_]{0,2}"
    r"[ \t]*:?[ \t]*[*_]{0,2}[ \t]*(?P<refs>.*)$",
    re.IGNORECASE | re.MULTILINE,
)
_PARENTHETICAL = re.compile(r"\([^)]*\)")
_TASK_KEYWORD = re.compile(r"\bTasks?\b", re.IGNORECASE)
# Hyphen, en dash, and em dash all appear as range separators in plan
# documents. Spelled by name so this module stays ASCII.
_RANGE_DASH = "-\N{EN DASH}\N{EM DASH}"
_NUMBER_OR_RANGE = re.compile(
    r"[ \t]*(?P<first>\d+)"
    rf"(?:[ \t]*(?:[{_RANGE_DASH}]|to\b)[ \t]*(?P<last>\d+))?",
    re.IGNORECASE,
)
_LIST_SEPARATOR = re.compile(
    r"[ \t]*(?:,[ \t]*and\b|,|&|\+|and\b)[ \t]*", re.IGNORECASE
)
# Words a dependency line may carry without stating anything: an explicit "no
# dependency" marker, and the connectors that join two references.
_LEFTOVER_NOISE = re.compile(r"\b(?:none|nothing|n/?a|and|plus)\b", re.IGNORECASE)
_ALPHABETIC = re.compile(r"[A-Za-z]")
# Emphasis, bullets and punctuation that can sit in front of the line's first
# real word without being part of it.
_LEADING_NOISE = re.compile(r"^[\s*_:,;.\-]*")
_NONE_MARKER = re.compile(r"(?:none|nothing|n/?a)\b", re.IGNORECASE)

# A plan whose one dependency line names more than this many tasks is stating
# something the range grammar was not meant to express (or is a typo). Refusing
# beats materializing "Tasks 1-99999".
_MAX_RANGE_SPAN = 64


def slugify(title: str) -> str:
    """Return a url-safe slug derived from a task title."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return cleaned or "task"


def _mask_fenced_code(text: str) -> str:
    """Blank out fenced code blocks, preserving every character offset.

    Offsets are preserved so a mask can be sliced with positions found in the
    raw text. A closing fence must use the same character and be at least as
    long as the opening one, which is what keeps a ```` ```` ```` block that
    wraps an embedded markdown document (this repository's plan convention)
    from being closed by the ``` fences inside it. An unterminated fence masks
    to the end of the document: a dependency line that may or may not be inside
    a code block is not a dependency line worth acting on.
    """
    chars = list(text)

    def blank(start: int, end: int) -> None:
        for position in range(start, end):
            if chars[position] != "\n":
                chars[position] = " "

    open_at: int | None = None
    open_fence = ""
    for match in _FENCE_DELIMITER.finditer(text):
        fence = match.group("fence")
        if open_at is None:
            open_at, open_fence = match.start(), fence
            continue
        if fence[0] != open_fence[0] or len(fence) < len(open_fence):
            continue
        blank(open_at, match.end())
        open_at = None
    if open_at is not None:
        blank(open_at, len(text))
    return "".join(chars)


def _expand_range(first: int, last: int) -> list[int] | None:
    """Return the numbers a "Tasks a-b" range names, or None if implausible."""
    if last < first or last - first + 1 > _MAX_RANGE_SPAN:
        logger.warning(
            "Ignoring an implausible task range %d-%d in a dependency line",
            first,
            last,
        )
        return None
    return list(range(first, last + 1))


def _consume_number_list(text: str, position: int) -> tuple[list[int] | None, int]:
    """Read the number list that follows a "Task"/"Tasks" keyword.

    Args:
        text: The dependency line's remainder.
        position: The offset just past the keyword.

    Returns:
        ``(None, position)`` when no number follows the keyword at all, so the
        caller leaves the keyword in the leftover text for the prose check.
        Otherwise ``(numbers, end)`` where ``end`` is the offset just past the
        consumed list; ``numbers`` is empty when a range was refused, which
        still counts as consumed so a refused range does not read as prose.
    """
    numbers: list[int] = []
    cursor = position
    consumed = False
    while True:
        match = _NUMBER_OR_RANGE.match(text, cursor)
        if match is None:
            break
        consumed = True
        first = int(match.group("first"))
        last = match.group("last")
        if last is None:
            numbers.append(first)
        else:
            expanded = _expand_range(first, int(last))
            if expanded is None:
                return [], match.end()
            numbers.extend(expanded)
        cursor = match.end()
        separator = _LIST_SEPARATOR.match(text, cursor)
        if separator is None or _NUMBER_OR_RANGE.match(text, separator.end()) is None:
            break
        cursor = separator.end()
    if not consumed:
        return None, position
    return numbers, cursor


def _extract_task_references(remainder: str) -> tuple[list[int], str]:
    """Split a dependency line into the tasks it names and the rest of the line.

    Returns:
        ``(numbers, leftover)``. ``leftover`` is the line with every consumed
        reference removed, which is what the prose check inspects.
    """
    numbers: list[int] = []
    pieces: list[str] = []
    cursor = 0
    for keyword in _TASK_KEYWORD.finditer(remainder):
        if keyword.start() < cursor:
            continue
        found, end = _consume_number_list(remainder, keyword.end())
        if found is None:
            continue
        pieces.append(remainder[cursor : keyword.start()])
        numbers.extend(found)
        cursor = end
    pieces.append(remainder[cursor:])
    return numbers, "".join(pieces)


def _stated_dependency_numbers(body: str, own_slug: str) -> list[int]:
    """Return the task numbers this body's FIRST "Depends on" line states.

    Parenthesised spans are dropped first, then the line's LEADING token
    decides how the rest is read. It is the ANCHORING of both tests at that
    leading token, not their order, that carries the safety here: both match
    at the same offset against disjoint token sets, so they cannot both fire
    and swapping them changes nothing. Verified by mutation: reordering them
    fails no test, while turning either ``match`` into a ``search`` changes
    real behavior. Classify by the leading token only:

    1. A none-marker ("None", "nothing") means the line states no dependency,
       whatever it goes on to say. ``None (independent of Task 1)`` and
       ``nothing; unlike Task 2 this is standalone`` both name a task while
       denying it, so reading their references inverts the line's meaning.
       It is written first because a line that denies a dependency is the one
       most likely to also mention one, which makes it the case a reader
       should meet first. That is presentation, not correctness: see above.
    2. A task reference means the line is well formed: every reference on it
       counts and trailing prose is commentary. ``Task 9, and the benchmark
       plan's Task 17 (the report must exist to be linked)`` states a real
       dependency on task 9, and a reference no heading declares is dropped
       later anyway. This deliberately accepts a QUALIFIED line such as
       ``Task 2 only for the fixture, not for ordering`` as an edge. That is a
       decision, not an oversight: now that cycles are broken (see
       ``_sanitize_dependency_graph``) the worst case of a false positive is
       unnecessary serialization, which is visible and harmless, while a false
       negative dispatches in parallel the work the document ordered.
    3. Anything else LEADS with prose (``the outcome of Task 1 being wrong``),
       states nothing, and is refused with a warning.

    Only the FIRST such line in the body is read, so a body that restates its
    dependencies later cannot have the restatement win.
    """
    line = _DEPENDS_LINE.search(body)
    if line is None:
        return []
    remainder = line.group("refs")
    stated = _PARENTHETICAL.sub(" ", remainder)
    noise = _LEADING_NOISE.match(stated)
    lead = noise.end() if noise is not None else 0

    if _NONE_MARKER.match(stated, lead) is not None:
        numbers, leftover = _extract_task_references(stated)
        if numbers or _ALPHABETIC.search(_LEFTOVER_NOISE.sub(" ", leftover)):
            logger.warning(
                "Task %r: dependency line states none, ignoring what else it names: %r",
                own_slug,
                remainder.strip(),
            )
        return []

    keyword = _TASK_KEYWORD.match(stated, lead)
    if keyword is not None and _NUMBER_OR_RANGE.match(stated, keyword.end()):
        numbers, _leftover = _extract_task_references(stated)
        return numbers

    logger.warning(
        "Task %r: ignoring an unparseable dependency line: %r",
        own_slug,
        remainder.strip(),
    )
    return []


def _resolve_dependencies(
    body: str, own_number: int, own_slug: str, slug_by_number: dict[int, str]
) -> list[str]:
    """Resolve a task body's stated dependencies to slugs in the same plan.

    Args:
        body: The task's own body slice, fence-masked and bounded.
        own_number: The number in this task's own heading.
        own_slug: This task's own slug.
        slug_by_number: Heading number to slug for every task in the plan.

    Returns:
        Deduplicated slugs, order preserved. A reference to a number that no
        heading declares is dropped rather than passed through, because
        ``TaskQueue.get_dispatchable_tasks`` raises on a slug it cannot
        resolve and would wedge dispatch for the entire plan. A task naming
        its own number is dropped too: it could never become dispatchable.
        Self references that survive as a SLUG, and cycles, are dropped by
        ``_sanitize_dependency_graph``.
    """
    resolved: list[str] = []
    for number in _stated_dependency_numbers(body, own_slug):
        if number == own_number:
            continue
        slug = slug_by_number.get(number)
        if slug is None or slug in resolved:
            continue
        resolved.append(slug)
    return resolved


def _reaches(graph: dict[str, list[str]], start: str, target: str) -> bool:
    """True when ``target`` is transitively reachable from ``start``'s edges.

    A node is NOT considered to reach itself: a self edge is dropped by an
    explicit check so that removing it is visible, rather than being absorbed
    silently by the cycle check.
    """
    seen: set[str] = set()
    stack = list(graph.get(start, ()))
    while stack:
        node = stack.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(graph.get(node, ()))
    return False


def _sanitize_dependency_graph(tasks: list[dict]) -> None:
    """Drop unknown, self, and cycle-closing edges in place, in document order.

    Every one of these wedges the loop silently rather than loudly:

    * an unknown slug makes ``TaskQueue.get_dispatchable_tasks`` raise, and
      ``Orchestrator.run_once`` has no per-plan try/except, so the exception
      aborts the pass for EVERY runnable plan, every interval, forever;
    * a self edge can never be satisfied;
    * a cycle yields an empty dispatchable list without raising, and
      ``plan_stalled`` requires a FAILED task, which a cycle never produces,
      so the plan sits ACTIVE emitting nothing at all.

    Edges are considered in document order and an edge is accepted only if it
    does not close a cycle against the edges already accepted, which makes the
    outcome deterministic: given ``1 -> 2`` and ``2 -> 1``, the first is kept
    and the second dropped, so the plan runs in an order instead of hanging.
    """
    known = {str(task["slug"]) for task in tasks}
    accepted: dict[str, list[str]] = {}
    for task in tasks:
        slug = str(task["slug"])
        raw = task.get("depends_on")
        if not isinstance(raw, list):
            if raw:
                logger.warning(
                    "Task %r: discarding a depends_on that is not a list: %r", slug, raw
                )
            raw = []
        kept: list[str] = []
        for entry in raw:
            dependency = str(entry)
            if dependency not in known:
                logger.warning(
                    "Dropping dependency %r of task %r: no such task in the plan",
                    dependency,
                    slug,
                )
                continue
            if dependency == slug:
                logger.warning("Dropping self dependency on %r", slug)
                continue
            if dependency in kept:
                continue
            if _reaches(accepted, dependency, slug):
                logger.warning(
                    "Dropping dependency of %r on %r: it would close a cycle",
                    slug,
                    dependency,
                )
                continue
            kept.append(dependency)
            accepted.setdefault(slug, []).append(dependency)
        task["depends_on"] = kept


def _task_body_bounds(
    masked: str, headings: list[re.Match[str]], index: int
) -> tuple[int, int]:
    """Return the [start, end) slice of the document that is this task's body.

    The body ends at the next TASK heading, or at the next heading of the same
    or a shallower level, whichever comes first. Bounding only on task headings
    let the last task's body run to the end of the document, so a "Depends on"
    line in a trailing "Parallel Execution Map" or "Closeout" section was read
    as that task's own. Deeper subsections (a ``#### Steps`` under a ``###
    Task N``) stay inside, which is what a reader means by them.
    """
    match = headings[index]
    level = len(match.group("hashes"))
    start = match.end()
    end = headings[index + 1].start() if index + 1 < len(headings) else len(masked)
    for heading in _ANY_HEADING.finditer(masked, start):
        if len(heading.group("hashes")) <= level:
            end = min(end, heading.start())
            break
    return start, max(start, end)


def parse_plan_tasks(text: str) -> list[dict[str, str | list[str]]]:
    """Parse a plan.md into a task list. Returns [] when unstructured.

    Dependencies are resolved by heading NUMBER: a ``**Depends on:** Task 1``
    line resolves to the slug of the task whose heading reads ``Task 1``.
    Title text is deliberately not matched as a fallback, because a loose
    text match can invent a dependency the document never stated, and an
    invented edge either serializes work needlessly or forms a cycle that
    silently stalls the plan forever. Resolution needs two passes so that a
    forward reference resolves.

    The dependency line is looked for in a FENCE-MASKED copy of the document,
    so a plan that shows the ``**Depends on:**`` syntax in a code block is not
    read as using it. The description handed to the worker is still the raw
    body, examples and all.
    """
    headings = list(_TASK_HEADING.finditer(text))
    tasks: list[dict[str, str | list[str]]] = []
    if headings:
        masked = _mask_fenced_code(text)
        bodies: list[str] = []
        numbers: list[int] = []
        for index, match in enumerate(headings):
            title = match.group("title").strip()
            start, end = _task_body_bounds(masked, headings, index)
            bodies.append(masked[start:end])
            numbers.append(int(match.group("number")))
            tasks.append(
                {
                    "title": title,
                    "slug": slugify(title),
                    "description": text[start:end].strip() or title,
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
                bodies[index], numbers[index], str(task["slug"]), slug_by_number
            )
        _sanitize_dependency_graph(tasks)
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
    """Fill in defaults and sanitize the dependency graph before it is stored.

    The sanitizing pass is what makes the LM Studio fallback safe. That path
    never went through the parser's dependency resolution, so a model-invented
    slug reached ``activate_plan`` unchecked, and the reason the parser drops
    an unresolvable reference (``get_dispatchable_tasks`` raises, and
    ``run_once`` has no per-plan try/except, so one bad plan aborts the pass
    for every plan, every interval) applies to the fallback verbatim.
    """
    from orchestrator.core.markdown_utils import extract_title

    for task in tasks:
        task.setdefault("slug", slugify(str(task["title"])))
        task.setdefault("depends_on", [])
        task.setdefault("description", str(task["title"]))
    # Runs only once every slug is known: an entry is judged against the final
    # slug set, not against whichever slugs happened to be set already.
    _sanitize_dependency_graph(tasks)
    summary = extract_title(text) or "Derived plan"
    return {"plan_summary": summary, "plan_slug": slugify(summary), "tasks": tasks}


async def _derive_via_lm_studio(text: str, lm_studio_url: str) -> list[dict]:
    url = lm_studio_url.rstrip("/") + "/v1/chat/completions"
    # Structural extraction into a fixed schema: there is nothing here worth
    # reasoning about, and thinking actively breaks it. MEASURED on qwen3.8-27b
    # (2026-08-15) with this exact payload: omitting `reasoning_effort` spent
    # ~398 tokens reasoning and returned EMPTY content, which fails json.loads
    # below with a JSONDecodeError. At `none` the same call returns a clean
    # 8-task object. See core/thinking.py.
    body = {
        "messages": [
            {"role": "user", "content": _DERIVE_PROMPT.format(text=text[:8000])}
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "tasks", "schema": _TASK_SCHEMA},
        },
        "temperature": 0,
        **effort_param(None),
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
