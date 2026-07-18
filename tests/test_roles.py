import pytest

from orchestrator.core.llm_router import CALL_SITE_DEFAULTS
from orchestrator.core.roles import MODEL_ROLES, ROLE_OF_CALL_SITE


@pytest.mark.unit
def test_model_roles_equals_golden_set():
    assert MODEL_ROLES == ("plan", "review", "implement")


@pytest.mark.unit
def test_every_call_site_has_a_role():
    for call_site in CALL_SITE_DEFAULTS:
        assert call_site in ROLE_OF_CALL_SITE, (
            f"Call-site '{call_site}' is missing a role mapping."
        )


@pytest.mark.unit
def test_every_role_value_is_valid():
    for call_site, role in ROLE_OF_CALL_SITE.items():
        assert role in MODEL_ROLES, (
            f"Role '{role}' for call-site '{call_site}' is not in MODEL_ROLES."
        )
