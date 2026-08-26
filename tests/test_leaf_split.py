"""Split rewiring is pure and positional.

get_dispatchable_tasks maps opus_plan["tasks"] to DB rows BY INDEX, so children
must be APPENDED and the superseded parent must never be removed.
"""

import pytest

from orchestrator.core.leaf_split import child_slugs, rewire_plan_for_split
from orchestrator.models.schemas import LeafTask


def _plan() -> dict:
    return {
        "tasks": [
            {
                "id": "a",
                "slug": "a",
                "title": "A",
                "description": "A",
                "depends_on": [],
            },
            {
                "id": "b",
                "slug": "b",
                "title": "B",
                "description": "B",
                "depends_on": ["a"],
            },
            {
                "id": "c",
                "slug": "c",
                "title": "C",
                "description": "C",
                "depends_on": ["b"],
            },
        ]
    }


def _children() -> list[LeafTask]:
    return [
        LeafTask(id="x1", title="B part one", plan_text="Goal: one"),
        LeafTask(id="x2", title="B part two", plan_text="Goal: two", depends_on=["x1"]),
    ]


@pytest.mark.unit
def test_child_slugs_are_parent_suffixed_and_ordered():
    assert child_slugs("my-leaf", 3) == ["my-leaf-s1", "my-leaf-s2", "my-leaf-s3"]


@pytest.mark.unit
def test_children_are_appended_not_inserted():
    plan = _plan()
    rewire_plan_for_split(plan, "b", _children())
    assert [t["slug"] for t in plan["tasks"]] == ["a", "b", "c", "b-s1", "b-s2"]


@pytest.mark.unit
def test_the_parent_is_never_removed():
    plan = _plan()
    rewire_plan_for_split(plan, "b", _children())
    assert any(t["slug"] == "b" for t in plan["tasks"])


@pytest.mark.unit
def test_children_inherit_the_parents_dependencies():
    plan = _plan()
    rewire_plan_for_split(plan, "b", _children())
    by_slug = {t["slug"]: t for t in plan["tasks"]}
    # First child inherits the parent's deps verbatim.
    assert by_slug["b-s1"]["depends_on"] == ["a"]
    # A child that declared an internal dependency keeps it, remapped to slugs,
    # in addition to the inherited parent deps.
    assert set(by_slug["b-s2"]["depends_on"]) == {"a", "b-s1"}


@pytest.mark.unit
def test_dependents_of_the_parent_now_depend_on_every_child():
    plan = _plan()
    rewire_plan_for_split(plan, "b", _children())
    by_slug = {t["slug"]: t for t in plan["tasks"]}
    assert set(by_slug["c"]["depends_on"]) == {"b-s1", "b-s2"}
    assert "b" not in by_slug["c"]["depends_on"]


@pytest.mark.unit
def test_children_carry_the_parent_slug_as_parent_slug():
    plan = _plan()
    rewire_plan_for_split(plan, "b", _children())
    by_slug = {t["slug"]: t for t in plan["tasks"]}
    assert by_slug["b-s1"]["parent_slug"] == "b"
    assert by_slug["b-s2"]["parent_slug"] == "b"


@pytest.mark.unit
def test_children_keep_their_leaf_contract_fields():
    plan = _plan()
    rewire_plan_for_split(plan, "b", _children())
    by_slug = {t["slug"]: t for t in plan["tasks"]}
    assert by_slug["b-s1"]["plan_text"] == "Goal: one"
    assert by_slug["b-s1"]["leaf_type"] == "generic"


@pytest.mark.unit
def test_an_unknown_parent_slug_raises():
    with pytest.raises(KeyError):
        rewire_plan_for_split(_plan(), "nope", _children())


@pytest.mark.unit
def test_rewiring_returns_the_children_in_append_order():
    plan = _plan()
    appended = rewire_plan_for_split(plan, "b", _children())
    assert [t["slug"] for t in appended] == ["b-s1", "b-s2"]


# Beyond the plan text: the invariants above fail silently, so pin the
# adjacent ways the positional mapping can be corrupted without an error.


@pytest.mark.unit
def test_a_rejected_call_leaves_the_graph_untouched():
    """A raise must not half-rewire: the caller may retry or fall back."""
    plan = _plan()
    before = [dict(task) for task in plan["tasks"]]
    with pytest.raises(KeyError):
        rewire_plan_for_split(plan, "nope", _children())
    assert plan["tasks"] == before


@pytest.mark.unit
def test_a_second_split_of_the_same_parent_raises_instead_of_duplicating():
    """Child slugs are deterministic, so a re-split would append duplicates.

    Two rows sharing a slug collapse ``slug_to_task`` in
    ``get_dispatchable_tasks`` and orphan the earlier row.  Fail loudly.
    """
    plan = _plan()
    rewire_plan_for_split(plan, "b", _children())
    with pytest.raises(ValueError, match="already exist"):
        rewire_plan_for_split(plan, "b", _children())
    assert [t["slug"] for t in plan["tasks"]] == ["a", "b", "c", "b-s1", "b-s2"]


@pytest.mark.unit
def test_children_sharing_one_id_raise_instead_of_collapsing_the_map():
    """Nothing makes the brain's child ids unique, and the map is last-wins.

    ``LeafTask.id`` is a bare ``str`` and the triage prompt only says a child's
    ``depends_on`` must name SIBLINGS, never that the ids must differ.  Two
    children carrying one id collapse ``id_to_slug``, so a sibling edge points
    at whichever child came LAST: the dependent child is ordered after the wrong
    sibling, runs against work that was never built, and fails its own
    verification with nothing anywhere naming the miswiring.  Same doctrine as
    the duplicate-slug rejection above: fail loudly rather than corrupt the map.
    """
    children = [
        LeafTask(id="x1", title="B part one", plan_text="Goal: one"),
        LeafTask(id="x1", title="B part two", plan_text="Goal: two", depends_on=["x1"]),
    ]
    plan = _plan()
    before = [dict(task) for task in plan["tasks"]]
    with pytest.raises(ValueError, match="duplicate"):
        rewire_plan_for_split(plan, "b", children)
    assert plan["tasks"] == before


@pytest.mark.unit
def test_the_parent_keeps_its_own_position_and_dependencies():
    """The superseded parent stays at index 1 with its edges intact."""
    plan = _plan()
    rewire_plan_for_split(plan, "b", _children())
    assert plan["tasks"][1]["slug"] == "b"
    assert plan["tasks"][1]["depends_on"] == ["a"]


@pytest.mark.unit
def test_a_dependent_keeps_its_other_dependencies_and_gains_no_duplicates():
    plan = _plan()
    plan["tasks"].append(
        {
            "id": "d",
            "slug": "d",
            "title": "D",
            "description": "D",
            "depends_on": ["a", "b", "c"],
        }
    )
    rewire_plan_for_split(plan, "b", _children())
    by_slug = {t["slug"]: t for t in plan["tasks"]}
    deps = by_slug["d"]["depends_on"]
    assert deps == ["a", "b-s1", "b-s2", "c"]
    assert len(deps) == len(set(deps))


@pytest.mark.unit
def test_a_child_dependency_on_an_unknown_id_is_dropped_not_dangled():
    """An unresolvable child dep would wedge the whole plan at dispatch."""
    children = [
        LeafTask(id="x1", title="B part one", depends_on=["ghost"]),
        LeafTask(id="x2", title="B part two", depends_on=["x1"]),
    ]
    plan = _plan()
    rewire_plan_for_split(plan, "b", children)
    by_slug = {t["slug"]: t for t in plan["tasks"]}
    known = {t["slug"] for t in plan["tasks"]}
    assert by_slug["b-s1"]["depends_on"] == ["a"]
    for task in plan["tasks"]:
        assert set(task["depends_on"]) <= known
