"""Golden-file regression for parse_review_response.

Asserts that a sample brain response fixture parses to the expected leaf
graph fixture, locking in the exact output shape of the decomposer.
"""

import json
from pathlib import Path

import pytest

from orchestrator.core.plan_review import parse_review_response


FIXTURES = Path(__file__).parent / "fixtures" / "decompose"


@pytest.mark.unit
def test_sample_plan_parses_to_expected_leaf_graph():
    raw = (FIXTURES / "sample_plan_response.json").read_text(encoding="utf-8")
    expected = json.loads(
        (FIXTURES / "expected_leaf_graph.json").read_text(encoding="utf-8")
    )
    assert parse_review_response(raw) == expected
