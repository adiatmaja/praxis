"""Tests for LeafTask schema and parse_review_response integration."""

import pytest

from orchestrator.core.plan_review import PlanReviewError, parse_review_response
from orchestrator.models.schemas import LEAF_SCHEMA_VERSION, LeafChecklistItem, LeafTask


# ---------------------------------------------------------------------------
# LEAF_SCHEMA_VERSION
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_leaf_schema_version_is_int():
    assert isinstance(LEAF_SCHEMA_VERSION, int)
    assert LEAF_SCHEMA_VERSION >= 1


# ---------------------------------------------------------------------------
# LeafChecklistItem
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_leaf_checklist_item_minimal():
    item = LeafChecklistItem(text="write tests")
    assert item.text == "write tests"


# ---------------------------------------------------------------------------
# LeafTask — minimal required fields
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_leaf_task_minimal():
    leaf = LeafTask(id="t1", title="Add model")
    assert leaf.id == "t1"
    assert leaf.title == "Add model"
    assert leaf.schema_version == LEAF_SCHEMA_VERSION
    # Title-derived defaults
    assert leaf.description == "Add model"
    assert leaf.plan_text == "Add model"
    assert leaf.checklist == [LeafChecklistItem(text="Add model")]


# ---------------------------------------------------------------------------
# LeafTask — all fields provided
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_leaf_task_full():
    leaf = LeafTask(
        id="t2",
        title="Build API",
        description="Implement the REST API",
        plan_text="def get_users(): ...",
        depends_on=["t1"],
        checklist=[
            LeafChecklistItem(text="write endpoint"),
            LeafChecklistItem(text="add tests"),
        ],
        needs_stronger_model=True,
        files=["src/api.py"],
        task_type="feature",
        estimated_loc=120,
        verification="pytest tests/api/",
    )
    assert leaf.id == "t2"
    assert leaf.needs_stronger_model is True
    assert leaf.depends_on == ["t1"]
    assert len(leaf.checklist) == 2
    assert leaf.task_type == "feature"
    assert leaf.estimated_loc == 120


# ---------------------------------------------------------------------------
# LeafTask — extra fields are allowed
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_leaf_task_extra_fields_allowed():
    leaf = LeafTask(id="t3", title="X", custom_field="kept")
    assert leaf.custom_field == "kept"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# LeafTask — model_dump round-trip
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_leaf_task_model_dump_contains_required_keys():
    leaf = LeafTask(id="t4", title="Y")
    d = leaf.model_dump()
    assert d["id"] == "t4"
    assert d["title"] == "Y"
    assert d["schema_version"] == LEAF_SCHEMA_VERSION
    assert "description" in d
    assert "plan_text" in d
    assert "checklist" in d
    assert "depends_on" in d


# ---------------------------------------------------------------------------
# LeafTask — title-derived defaults with explicit description but no plan_text
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_leaf_task_defaults_description_set_plan_text_from_description():
    leaf = LeafTask(id="t5", title="Z", description="custom desc")
    assert leaf.description == "custom desc"
    assert leaf.plan_text == "custom desc"


# ---------------------------------------------------------------------------
# parse_review_response — integration with LeafTask
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_review_response_returns_leaf_dumps():
    raw = '{"tasks": [{"id": "t1", "title": "Add model", "description": "write the model"}]}'
    result = parse_review_response(raw)
    tasks = result["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["id"] == "t1"
    assert tasks[0]["schema_version"] == LEAF_SCHEMA_VERSION
    assert tasks[0]["description"] == "write the model"


@pytest.mark.unit
def test_parse_review_response_extra_fields_preserved():
    raw = '{"tasks": [{"id": "t1", "title": "A", "custom": "val"}]}'
    result = parse_review_response(raw)
    assert result["tasks"][0]["custom"] == "val"


@pytest.mark.unit
def test_parse_review_response_rejects_non_dict_task():
    raw = '{"tasks": ["not a dict"]}'
    with pytest.raises(PlanReviewError):
        parse_review_response(raw)


@pytest.mark.unit
def test_parse_review_response_rejects_missing_id():
    raw = '{"tasks": [{"title": "X"}]}'
    with pytest.raises(PlanReviewError):
        parse_review_response(raw)


@pytest.mark.unit
def test_parse_review_response_rejects_missing_title():
    raw = '{"tasks": [{"id": "t1"}]}'
    with pytest.raises(PlanReviewError):
        parse_review_response(raw)


@pytest.mark.unit
def test_leaf_type_enum_has_the_eight_standard_values():
    from orchestrator.models.schemas import LeafType

    assert {t.value for t in LeafType} == {
        "bugfix_repro",
        "function_add",
        "endpoint_add",
        "refactor_rename",
        "test_add",
        "config_change",
        "doc_change",
        "generic",
    }


@pytest.mark.unit
def test_leaf_task_defaults_leaf_type_to_generic():
    from orchestrator.models.schemas import LeafTask, LeafType

    leaf = LeafTask(id="t1", title="Add a helper")
    assert leaf.leaf_type is LeafType.GENERIC


@pytest.mark.unit
def test_leaf_task_accepts_a_declared_leaf_type():
    from orchestrator.models.schemas import LeafTask, LeafType

    leaf = LeafTask(id="t1", title="Fix the off-by-one", leaf_type="bugfix_repro")
    assert leaf.leaf_type is LeafType.BUGFIX_REPRO


@pytest.mark.unit
def test_leaf_task_rejects_an_unknown_leaf_type():
    from pydantic import ValidationError

    from orchestrator.models.schemas import LeafTask

    with pytest.raises(ValidationError):
        LeafTask(id="t1", title="x", leaf_type="not_a_real_type")


@pytest.mark.unit
def test_leaf_task_neighbor_contracts_defaults_to_none():
    from orchestrator.models.schemas import LeafTask

    assert LeafTask(id="t1", title="x").neighbor_contracts is None


@pytest.mark.unit
def test_leaf_schema_version_is_two():
    from orchestrator.models.schemas import LEAF_SCHEMA_VERSION

    assert LEAF_SCHEMA_VERSION == 2
