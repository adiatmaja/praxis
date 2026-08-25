import json
import logging

import pytest

from orchestrator.core.plan_derive import (
    PlanDeriveError,
    derive_opus_plan,
    parse_plan_tasks,
    slugify,
)


PARSER_LOGGER = "orchestrator.core.plan_derive"


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


async def test_derive_uses_deterministic_when_structured():
    text = "# Plan\n\n### Task 1: Do thing\n\nDetails here.\n"
    plan = await derive_opus_plan(text, lm_studio_url="http://unused:1234")
    assert plan["tasks"][0]["title"] == "Do thing"
    assert "plan_slug" in plan


async def test_derive_calls_lm_studio_when_unstructured(mocker):
    text = "# Plan\n\nUnstructured prose with no tasks."
    fake_tasks = {
        "tasks": [
            {
                "title": "Inferred",
                "slug": "inferred",
                "description": "d",
                "depends_on": [],
            }
        ]
    }
    payload = {"choices": [{"message": {"content": json.dumps(fake_tasks)}}]}
    mock_resp = mocker.Mock()
    mock_resp.json.return_value = payload
    mock_resp.raise_for_status.return_value = None
    post = mocker.patch(
        "httpx.AsyncClient.post", new=mocker.AsyncMock(return_value=mock_resp)
    )
    plan = await derive_opus_plan(text, lm_studio_url="http://lm:1234")
    assert plan["tasks"][0]["title"] == "Inferred"
    post.assert_awaited()


async def test_derive_raises_when_nothing_derivable(mocker):
    text = "# Plan\n\nprose"
    payload = {"choices": [{"message": {"content": '{"tasks": []}'}}]}
    mock_resp = mocker.Mock()
    mock_resp.json.return_value = payload
    mock_resp.raise_for_status.return_value = None
    mocker.patch("httpx.AsyncClient.post", new=mocker.AsyncMock(return_value=mock_resp))
    with pytest.raises(PlanDeriveError):
        await derive_opus_plan(text, lm_studio_url="http://lm:1234")


def test_parse_plan_tasks_reads_a_stated_dependency() -> None:
    text = (
        "## Task 1: Do the thing\n\n"
        "**Depends on:** None\n\nBody.\n\n"
        "## Task 2: Verify the thing\n\n"
        "**Depends on:** Task 1\n\nBody.\n"
    )
    tasks = parse_plan_tasks(text)
    assert [t["slug"] for t in tasks] == ["do-the-thing", "verify-the-thing"]
    assert tasks[0]["depends_on"] == []
    assert tasks[1]["depends_on"] == ["do-the-thing"]
    # Reading the line must not rewrite the body the worker is handed.
    assert "**Depends on:** Task 1" in tasks[1]["description"]


def test_parse_plan_tasks_depends_on_none_is_empty() -> None:
    text = "## Task 1: Solo\n\n**Depends on:** None\n\nBody.\n"
    assert parse_plan_tasks(text)[0]["depends_on"] == []


def test_parse_plan_tasks_unresolvable_dependency_is_dropped() -> None:
    """A named task that does not exist must not become a phantom slug.

    ``TaskQueue.get_dispatchable_tasks`` resolves depends_on by slug against
    the same task list and raises ValueError on a slug it cannot find, so a
    phantom would wedge dispatch for the whole plan, not merely leave one
    task unordered.
    """
    text = "## Task 1: Real\n\n**Depends on:** Task 9\n\nBody.\n"
    assert parse_plan_tasks(text)[0]["depends_on"] == []


def test_parse_plan_tasks_self_dependency_is_dropped() -> None:
    """A task naming its own number would be permanently undispatchable."""
    text = "## Task 1: Loop\n\n**Depends on:** Task 1\n\nBody.\n"
    assert parse_plan_tasks(text)[0]["depends_on"] == []


def test_parse_plan_tasks_reads_several_dependencies_from_one_line() -> None:
    text = (
        "## Task 1: Alpha\n\nBody.\n\n"
        "## Task 2: Beta\n\nBody.\n\n"
        "## Task 3: Gamma\n\n**Depends on:** Task 1 and Task 2\n\nBody.\n"
    )
    tasks = parse_plan_tasks(text)
    assert tasks[2]["depends_on"] == ["alpha", "beta"]


def test_parse_plan_tasks_reads_dependency_with_the_colon_outside_the_bold() -> None:
    text = (
        "## Task 1: Alpha\n\nBody.\n\n"
        "## Task 2: Beta\n\n**Depends on**: Task 1\n\nBody.\n"
    )
    assert parse_plan_tasks(text)[1]["depends_on"] == ["alpha"]


def test_parse_plan_tasks_dependency_does_not_leak_across_sections() -> None:
    """The dependency line is read from the task's own body slice only.

    Searching the whole document would hand every task the first dependency
    line the document contains, silently serializing tasks that stated
    nothing and inverting the order of the ones that did.
    """
    text = (
        "## Task 1: Alpha\n\nBody.\n\n"
        "## Task 2: Beta\n\nBody.\n\n"
        "## Task 3: Gamma\n\n**Depends on:** Task 2\n\nBody.\n"
    )
    tasks = parse_plan_tasks(text)
    assert tasks[0]["depends_on"] == []
    assert tasks[1]["depends_on"] == []
    assert tasks[2]["depends_on"] == ["beta"]


def test_parse_plan_tasks_checkbox_branch_has_no_dependencies() -> None:
    """The checkbox branch reads no dependency, and the fixture proves the claim.

    The previous fixture contained no dependency text at all, so it passed
    under every mutation including one that made resolution return a constant.
    The item text below is a live trap: run through the heading branch the same
    words DO produce an edge, so a checkbox branch that resolved dependencies
    (numbering items positionally, say) would return ``['first-thing']`` here.
    """
    heading_equivalent = (
        "## Task 1: First thing\n\nBody.\n\n"
        "## Task 2: Second thing\n\nDepends on Task 1\n"
    )
    assert parse_plan_tasks(heading_equivalent)[1]["depends_on"] == ["first-thing"]

    checkbox = "# Plan\n\n- [ ] First thing\n- [ ] Depends on Task 1\n"
    assert [t["depends_on"] for t in parse_plan_tasks(checkbox)] == [[], []]


# --- D1: a stated dependency is read, prose and parentheticals are not -------


def test_parse_plan_tasks_none_with_a_parenthetical_task_is_empty() -> None:
    """ "None (independent of Task 1)" states NO dependency.

    Reading the parenthetical inverts the line's meaning. This repository's own
    corpus carries "**Depends on:** None (independent of Tasks 1-5)", one word
    away from the singular form that was mis-read.
    """
    text = (
        "## Task 1: Alpha\n\nBody.\n\n"
        "## Task 2: Beta\n\n**Depends on:** None (independent of Task 1)\n\nBody.\n"
    )
    assert parse_plan_tasks(text)[1]["depends_on"] == []


def test_parse_plan_tasks_plural_none_with_a_parenthetical_range_is_empty() -> None:
    """The exact line in docs/superpowers/plans/2026-07-02-refactor-*.md."""
    text = (
        "## Task 1: Alpha\n\nBody.\n\n"
        "## Task 2: Beta\n\nBody.\n\n"
        "## Task 3: Gamma\n\n"
        "**Depends on:** None (independent of Tasks 1-2)\n\nBody.\n"
    )
    assert parse_plan_tasks(text)[2]["depends_on"] == []


def test_parse_plan_tasks_rejects_a_prose_dependency_line(caplog) -> None:
    """Prose that merely mentions a task states no edge; it must warn, not guess."""
    text = (
        "## Task 1: Alpha\n\nBody.\n\n"
        "## Task 2: Beta\n\n"
        "**Depends on** the outcome of Task 1 being wrong.\n\nBody.\n"
    )
    with caplog.at_level(logging.WARNING, logger=PARSER_LOGGER):
        tasks = parse_plan_tasks(text)
    assert tasks[1]["depends_on"] == []
    assert "unparseable dependency line" in caplog.text
    assert "the outcome of Task 1 being wrong." in caplog.text


def test_parse_plan_tasks_none_marker_beats_a_later_reference(caplog) -> None:
    """A line LEADING with a none-marker states nothing, whatever follows.

    This is why the none-marker test must run BEFORE the task-reference test:
    a line that denies a dependency is the line most likely to also mention
    one, and reading "Task 2" here inverts what the sentence says.
    """
    text = (
        "## Task 1: Alpha\n\nBody.\n\n"
        "## Task 2: Beta\n\nBody.\n\n"
        "## Task 3: Gamma\n\n"
        "**Depends on** nothing; unlike Task 2 this is standalone.\n\nBody.\n"
    )
    with caplog.at_level(logging.WARNING, logger=PARSER_LOGGER):
        tasks = parse_plan_tasks(text)
    assert tasks[2]["depends_on"] == []
    assert "states none" in caplog.text


def test_parse_plan_tasks_plain_none_does_not_warn(caplog) -> None:
    """The commonest line in the corpus must not produce a warning."""
    text = "## Task 1: Solo\n\n**Depends on:** None\n\nBody.\n"
    with caplog.at_level(logging.WARNING, logger=PARSER_LOGGER):
        tasks = parse_plan_tasks(text)
    assert tasks[0]["depends_on"] == []
    assert caplog.text == ""


def test_parse_plan_tasks_reads_a_reference_with_trailing_commentary(caplog) -> None:
    """The exact line at docs/superpowers/plans/2026-08-06-usable-praxis-product.md.

    It leads with a reference, so the line is well formed and the commentary
    after it is commentary. The cross-plan "Task 17" names no heading in this
    document and is dropped as unresolvable, leaving the real edge on task 9.
    A well-formed line must not warn.
    """
    text = "".join(f"## Task {n}: T{n}\n\nBody.\n\n" for n in range(1, 10)) + (
        "## Task 10: Restructure the docs\n\n"
        "**Depends on:** Task 9, and the benchmark plan's Task 17 "
        "(the report must exist to be linked)\n\nBody.\n"
    )
    with caplog.at_level(logging.WARNING, logger=PARSER_LOGGER):
        tasks = parse_plan_tasks(text)
    assert tasks[9]["depends_on"] == ["t9"]
    assert caplog.text == ""


def test_parse_plan_tasks_ignores_a_task_named_only_inside_a_parenthetical() -> None:
    """A parenthetical is commentary even when it names a task.

    Under leading-token classification rule 2 no longer inspects the leftover
    text, so parenthetical stripping is the ONLY thing left standing between
    "not Task 4" and a fabricated edge on task 4. Before the leading-token
    change the prose guard covered this case as well; it no longer does, so
    this fixture is what pins it.
    """
    text = (
        "## Task 1: Alpha\n\nBody.\n\n"
        "## Task 2: Beta\n\nBody.\n\n"
        "## Task 3: Gamma\n\nBody.\n\n"
        "## Task 4: Delta\n\nBody.\n\n"
        "## Task 5: Epsilon\n\n"
        "**Depends on:** Task 3 (not Task 4, which is independent)\n\nBody.\n"
    )
    assert parse_plan_tasks(text)[4]["depends_on"] == ["gamma"]


def test_parse_plan_tasks_looks_past_leftover_emphasis_markers() -> None:
    """`_DEPENDS_LINE` consumes at most two emphasis characters.

    A third one would sit in front of the first real token and defeat the
    leading-token test, turning a well-formed line into a prose rejection.
    """
    text = (
        "## Task 1: Alpha\n\nBody.\n\n## Task 2: Beta\n\n**Depends on:** ***Task 1***\n"
    )
    assert parse_plan_tasks(text)[1]["depends_on"] == ["alpha"]


def test_parse_plan_tasks_accepts_a_qualified_reference() -> None:
    """A qualified line states a dependency, so it becomes an edge.

    This is the accepted cost of leading-token classification: with cycles
    broken, over-serializing is visible and harmless while under-ordering is
    the defect this parser exists to fix.
    """
    text = (
        "## Task 1: Alpha\n\nBody.\n\n"
        "## Task 2: Beta\n\nBody.\n\n"
        "## Task 3: Gamma\n\n"
        "**Depends on** Task 2 only for the fixture, not for ordering\n\nBody.\n"
    )
    assert parse_plan_tasks(text)[2]["depends_on"] == ["beta"]


# --- D3: the plural and range forms ------------------------------------------


def test_parse_plan_tasks_reads_a_plural_task_list() -> None:
    text = (
        "## Task 1: Alpha\n\nBody.\n\n"
        "## Task 2: Beta\n\nBody.\n\n"
        "## Task 3: Gamma\n\nBody.\n\n"
        "## Task 4: Delta\n\n**Depends on:** Tasks 1, 2 and 3\n\nBody.\n"
    )
    assert parse_plan_tasks(text)[3]["depends_on"] == ["alpha", "beta", "gamma"]


def test_parse_plan_tasks_reads_a_task_range() -> None:
    text = (
        "".join(f"## Task {n}: T{n}\n\nBody.\n\n" for n in range(1, 6))
        + "## Task 6: Zeta\n\n**Depends on:** Tasks 1-5\n\nBody.\n"
    )
    assert parse_plan_tasks(text)[5]["depends_on"] == ["t1", "t2", "t3", "t4", "t5"]


def test_parse_plan_tasks_reads_a_range_followed_by_a_parenthetical() -> None:
    """The exact line at docs/superpowers/plans/2026-07-02-github-actions-*.md:397."""
    text = "".join(f"## Task {n}: T{n}\n\nBody.\n\n" for n in range(1, 6)) + (
        "## Task 6: Zeta\n\n"
        "**Depends on:** Tasks 1-5 (run it last so it validates all the new "
        "workflows in one pass; it will also catch mistakes made in them)\n\nBody.\n"
    )
    assert parse_plan_tasks(text)[5]["depends_on"] == ["t1", "t2", "t3", "t4", "t5"]


def test_parse_plan_tasks_rejects_an_inverted_range(caplog) -> None:
    text = (
        "".join(f"## Task {n}: T{n}\n\nBody.\n\n" for n in range(1, 6))
        + "## Task 6: Zeta\n\n**Depends on:** Tasks 5-1\n\nBody.\n"
    )
    with caplog.at_level(logging.WARNING, logger=PARSER_LOGGER):
        tasks = parse_plan_tasks(text)
    assert tasks[5]["depends_on"] == []
    assert "implausible task range" in caplog.text


def test_parse_plan_tasks_rejects_an_enormous_range(caplog) -> None:
    """An enormous span must not be materialized, only refused."""
    text = "## Task 1: Alpha\n\nBody.\n\n## Task 2: Beta\n\n**Depends on:** Tasks 1-99999\n"
    with caplog.at_level(logging.WARNING, logger=PARSER_LOGGER):
        tasks = parse_plan_tasks(text)
    assert tasks[1]["depends_on"] == []
    assert "implausible task range" in caplog.text


# --- D2 / F5: a cycle is broken deterministically, never left to hang ---------


def test_parse_plan_tasks_breaks_a_two_task_cycle(caplog) -> None:
    """A cycle would stall the plan silently, so the later edge is dropped.

    ``TaskQueue.get_dispatchable_tasks`` filters rather than validates, so a
    cycle yields an empty dispatchable list; ``plan_stalled`` needs a FAILED
    task and a cycle has none, so nothing is ever published. Failing toward an
    order is right; failing toward a hang is not.
    """
    text = (
        "## Task 1: Alpha\n\n**Depends on:** Task 2\n\nBody.\n\n"
        "## Task 2: Beta\n\n**Depends on:** Task 1\n\nBody.\n"
    )
    with caplog.at_level(logging.WARNING, logger=PARSER_LOGGER):
        tasks = parse_plan_tasks(text)
    assert tasks[0]["depends_on"] == ["beta"]
    assert tasks[1]["depends_on"] == []
    assert "close a cycle" in caplog.text


def test_parse_plan_tasks_breaks_a_three_task_cycle() -> None:
    text = (
        "## Task 1: Alpha\n\n**Depends on:** Task 2\n\nBody.\n\n"
        "## Task 2: Beta\n\n**Depends on:** Task 3\n\nBody.\n\n"
        "## Task 3: Gamma\n\n**Depends on:** Task 1\n\nBody.\n"
    )
    tasks = parse_plan_tasks(text)
    edges = {str(t["slug"]): list(t["depends_on"]) for t in tasks}
    assert edges == {"alpha": ["beta"], "beta": ["gamma"], "gamma": []}
    assert _is_acyclic(edges)


def _is_acyclic(edges: dict[str, list[str]]) -> bool:
    colour: dict[str, int] = {}

    def visit(node: str) -> bool:
        colour[node] = 1
        for nxt in edges.get(node, []):
            if colour.get(nxt) == 1:
                return False
            if colour.get(nxt) is None and not visit(nxt):
                return False
        colour[node] = 2
        return True

    return all(visit(n) for n in edges if colour.get(n) is None)


# --- D4: two tasks must never share a slug, whatever they are titled ---------


def test_parse_plan_tasks_gives_two_tasks_with_one_title_distinct_slugs() -> None:
    """A slug is an identity: ``activate_plan`` names a branch ``agent/{slug}``.

    Two rows sharing one collapse the positional graph-to-row map in
    ``get_dispatchable_tasks``, which orphans the earlier row and returns the
    later one twice. The first claimant keeps the bare slug so an edge that
    already resolved to it still does.
    """
    text = (
        "## Task 1: Same title\n\nBody.\n\n"
        "## Task 2: Same title\n\n**Depends on:** Task 1\n\nBody.\n"
    )
    tasks = parse_plan_tasks(text)
    assert [t["slug"] for t in tasks] == ["same-title", "same-title-2"]


def test_parse_plan_tasks_keeps_the_edge_a_shared_slug_used_to_swallow(
    caplog,
) -> None:
    """The document's own ordering must survive a repeated title.

    While both tasks slugified alike, task 2's genuine ``Depends on: Task 1``
    resolved to task 2's OWN slug and ``_sanitize_dependency_graph`` discarded
    it as a self edge, so the two ran in parallel against a plan that had
    ordered them.
    """
    text = (
        "## Task 1: Same title\n\nBody.\n\n"
        "## Task 2: Same title\n\n**Depends on:** Task 1\n\nBody.\n"
    )
    with caplog.at_level(logging.WARNING, logger=PARSER_LOGGER):
        tasks = parse_plan_tasks(text)
    assert tasks[1]["depends_on"] == ["same-title"]
    assert "self dependency" not in caplog.text


def test_parse_plan_tasks_separates_titles_that_slugify_to_the_same_thing() -> None:
    """``slugify`` reduces every title with no alphanumerics at all to "task".

    So two headings need not look alike to collide, and a plan that titles its
    steps with symbols produces the collision without repeating a word.
    """
    text = "## Task 1: ***\n\nBody.\n\n## Task 2: ///\n\nBody.\n"
    slugs = [t["slug"] for t in parse_plan_tasks(text)]
    assert slugs == ["task", "task-2"]


def test_parse_plan_tasks_separates_duplicate_checklist_items() -> None:
    """A checklist is exactly where a repeated line goes unnoticed."""
    text = "# Plan\n\n- [ ] Run the migration\n- [ ] Run the migration\n"
    slugs = [t["slug"] for t in parse_plan_tasks(text)]
    assert slugs == ["run-the-migration", "run-the-migration-2"]


def test_parse_plan_tasks_leaves_distinct_titles_alone() -> None:
    """Positive control: uniquing must not rename anything that was fine.

    A suffix applied unconditionally would satisfy every assertion above while
    changing the branch name of every task in every plan.
    """
    text = "## Task 1: Alpha\n\nBody.\n\n## Task 2: Beta\n\nBody.\n"
    assert [t["slug"] for t in parse_plan_tasks(text)] == ["alpha", "beta"]


# --- F1: a task body is bounded by heading level, not only by task headings ---


def test_parse_plan_tasks_body_stops_at_the_next_same_level_heading() -> None:
    """The LAST task's body used to run to the end of the document.

    A "Depends on" line in a trailing "Parallel Execution Map" or "Closeout"
    section was therefore attributed to whichever task happened to be last.
    """
    text = (
        "## Task 1: Alpha\n\nBody.\n\n"
        "## Task 2: Beta\n\nBody.\n\n"
        "## Parallel Execution Map\n\n**Depends on:** Task 1\n"
    )
    tasks = parse_plan_tasks(text)
    assert [t["slug"] for t in tasks] == ["alpha", "beta"]
    assert tasks[1]["depends_on"] == []


def test_parse_plan_tasks_body_keeps_its_deeper_subsections() -> None:
    """A `#### Steps` subsection is deeper than its `## Task N` and stays in."""
    text = (
        "## Task 1: Alpha\n\nBody.\n\n"
        "## Task 2: Beta\n\n#### Steps\n\n**Depends on:** Task 1\n\nMore.\n\n"
        "## Closeout\n\n**Depends on:** Task 2\n"
    )
    tasks = parse_plan_tasks(text)
    assert len(tasks) == 2
    assert tasks[1]["depends_on"] == ["alpha"]
    assert "#### Steps" in tasks[1]["description"]


# --- F2: a fenced example is not a stated dependency --------------------------


def test_parse_plan_tasks_ignores_a_fenced_dependency_example() -> None:
    """A plan that SHOWS the dependency syntax must not be read as using it."""
    text = (
        "## Task 1: Alpha\n\nBody.\n\n"
        "## Task 2: Beta\n\nWrite a line shaped like this:\n\n"
        "```markdown\n**Depends on:** Task 1\n```\n\nBody.\n"
    )
    tasks = parse_plan_tasks(text)
    assert tasks[1]["depends_on"] == []
    # The body handed to the worker keeps the example verbatim.
    assert "```markdown" in tasks[1]["description"]


def test_parse_plan_tasks_reads_a_real_line_after_a_fenced_example() -> None:
    text = (
        "## Task 1: Alpha\n\nBody.\n\n"
        "## Task 2: Beta\n\n```markdown\n**Depends on:** Task 99\n```\n\n"
        "**Depends on:** Task 1\n"
    )
    assert parse_plan_tasks(text)[1]["depends_on"] == ["alpha"]


# --- D5: the three claims nothing pinned -------------------------------------


def test_parse_plan_tasks_resolves_by_heading_number_not_position() -> None:
    """Every existing fixture numbered tasks 1..N, so the two coincided."""
    text = (
        "## Task 10: Alpha\n\nBody.\n\n"
        "## Task 20: Beta\n\n**Depends on:** Task 10\n\nBody.\n"
    )
    assert parse_plan_tasks(text)[1]["depends_on"] == ["alpha"]


def test_parse_plan_tasks_uses_the_first_dependency_line_in_a_body() -> None:
    """First-match is what makes a body's later restatement harmless."""
    text = (
        "## Task 1: Alpha\n\nBody.\n\n"
        "## Task 2: Beta\n\nBody.\n\n"
        "## Task 3: Gamma\n\n**Depends on:** Task 1\n\n"
        "Restated later in the body:\n\n**Depends on:** Task 2\n"
    )
    assert parse_plan_tasks(text)[2]["depends_on"] == ["alpha"]


# --- D7 / F7: the LM Studio fallback cannot wedge the loop --------------------


async def test_derive_drops_a_dangling_dependency_from_the_fallback(
    mocker, caplog
) -> None:
    """A model-invented slug raises in get_dispatchable_tasks and wedges run_once.

    ``Orchestrator.run_once`` has no per-plan try/except, so the ValueError
    aborts the pass for EVERY runnable plan and logs a traceback every interval
    forever. The parser drops unresolvable references for exactly this reason;
    the fallback path must do the same.
    """
    fake = {
        "tasks": [
            {"title": "One", "slug": "one", "description": "d", "depends_on": []},
            {
                "title": "Two",
                "slug": "two",
                "description": "d",
                "depends_on": ["one", "hallucinated"],
            },
        ]
    }
    payload = {"choices": [{"message": {"content": json.dumps(fake)}}]}
    mock_resp = mocker.Mock()
    mock_resp.json.return_value = payload
    mock_resp.raise_for_status.return_value = None
    mocker.patch("httpx.AsyncClient.post", new=mocker.AsyncMock(return_value=mock_resp))
    with caplog.at_level(logging.WARNING, logger=PARSER_LOGGER):
        plan = await derive_opus_plan("# Plan\n\nprose", lm_studio_url="http://lm:1234")
    assert plan["tasks"][1]["depends_on"] == ["one"]
    assert "no such task" in caplog.text


async def _derive_from_fallback(mocker, fake: dict) -> dict:
    """Run ``derive_opus_plan`` with the LM Studio call answering ``fake``."""
    payload = {"choices": [{"message": {"content": json.dumps(fake)}}]}
    mock_resp = mocker.Mock()
    mock_resp.json.return_value = payload
    mock_resp.raise_for_status.return_value = None
    mocker.patch("httpx.AsyncClient.post", new=mocker.AsyncMock(return_value=mock_resp))
    return await derive_opus_plan("# Plan\n\nprose", lm_studio_url="http://lm:1234")


async def test_derive_uniques_a_slug_the_fallback_repeated(mocker) -> None:
    """The model picks these slugs itself and nothing made it pick two.

    A repeat raises nowhere: it silently collapses the positional map in
    ``get_dispatchable_tasks``. The existing edge onto "one" keeps resolving to
    the FIRST claimant, so renaming the later duplicate orphans nothing.
    """
    fake = {
        "tasks": [
            {"title": "One", "slug": "one", "description": "d", "depends_on": []},
            {"title": "Another", "slug": "one", "description": "d", "depends_on": []},
            {
                "title": "Three",
                "slug": "three",
                "description": "d",
                "depends_on": ["one"],
            },
        ]
    }
    plan = await _derive_from_fallback(mocker, fake)
    assert [t["slug"] for t in plan["tasks"]] == ["one", "one-2", "three"]
    assert plan["tasks"][2]["depends_on"] == ["one"]


async def test_derive_replaces_an_empty_slug_the_fallback_emitted(mocker) -> None:
    """``agent/`` is not a branch name, and "" repeats as readily as any slug."""
    fake = {
        "tasks": [
            {"title": "Real Work", "slug": "", "description": "d", "depends_on": []}
        ]
    }
    plan = await _derive_from_fallback(mocker, fake)
    assert plan["tasks"][0]["slug"] == "real-work"


async def test_derive_still_drops_a_slug_level_self_dependency(mocker) -> None:
    """A model can name its OWN slug, and that edge can never be satisfied.

    Uniquing runs BEFORE the sanitizing pass, so this guard has to survive it:
    the fallback is the one path where a slug-level self edge is still
    reachable now that the parser cannot mint duplicate slugs.
    """
    fake = {
        "tasks": [
            {
                "title": "Loop",
                "slug": "loop",
                "description": "d",
                "depends_on": ["loop"],
            }
        ]
    }
    plan = await _derive_from_fallback(mocker, fake)
    assert plan["tasks"][0]["depends_on"] == []


async def test_derive_breaks_a_cycle_from_the_fallback(mocker) -> None:
    """The fallback can invent a cycle too, and a cycle hangs silently."""
    fake = {
        "tasks": [
            {"title": "One", "slug": "one", "description": "d", "depends_on": ["two"]},
            {"title": "Two", "slug": "two", "description": "d", "depends_on": ["one"]},
        ]
    }
    payload = {"choices": [{"message": {"content": json.dumps(fake)}}]}
    mock_resp = mocker.Mock()
    mock_resp.json.return_value = payload
    mock_resp.raise_for_status.return_value = None
    mocker.patch("httpx.AsyncClient.post", new=mocker.AsyncMock(return_value=mock_resp))
    plan = await derive_opus_plan("# Plan\n\nprose", lm_studio_url="http://lm:1234")
    edges = {t["slug"]: t["depends_on"] for t in plan["tasks"]}
    assert edges == {"one": ["two"], "two": []}


def test_parse_plan_tasks_honours_a_none_marker_only_at_the_leading_token() -> None:
    """A none-marker LATER on the line is commentary, not a denial.

    The leading token alone decides how a dependency line is read: a line that
    OPENS with a task reference states a real dependency whatever it goes on to
    mention, and only a line that OPENS with a none-marker denies one. Without
    this, matching the none-marker anywhere on the line would silently discard a
    stated edge. Nothing else in this module distinguishes the two, which a
    mutation turning that match into a search proved by failing no test.
    """
    stated = (
        "## Task 1: Alpha\n\nBody.\n\n"
        "## Task 2: Beta\n\n**Depends on:** Task 1, but none of the others\n\nBody.\n"
    )
    assert parse_plan_tasks(stated)[1]["depends_on"] == ["alpha"]

    denial = (
        "## Task 1: Alpha\n\nBody.\n\n"
        "## Task 2: Beta\n\n**Depends on:** none of Task 1\n\nBody.\n"
    )
    assert parse_plan_tasks(denial)[1]["depends_on"] == []
