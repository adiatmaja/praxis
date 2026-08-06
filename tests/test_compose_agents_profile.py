"""Compose must know how to build every image AgentManager can spawn.

The agent images being standalone is the single most common stale-image
failure in this project. One documented build command fixes that.
"""

from pathlib import Path

import pytest
import yaml


REPO = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))


def _service(name: str) -> dict:
    return COMPOSE["services"][name]


@pytest.mark.unit
@pytest.mark.parametrize("name", ["opencode-agent", "agy-agent"])
def test_the_agent_image_has_a_compose_service(name):
    assert name in COMPOSE["services"]


@pytest.mark.unit
@pytest.mark.parametrize("name", ["opencode-agent", "agy-agent"])
def test_the_agent_service_is_behind_the_agents_profile(name):
    assert _service(name)["profiles"] == ["agents"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "tag"),
    [("opencode-agent", "opencode-agent:latest"), ("agy-agent", "agy-agent:latest")],
)
def test_the_image_tag_matches_what_the_registry_spawns(name, tag):
    """A tag mismatch means compose builds an image nothing ever runs."""
    from orchestrator.core.harnesses import REGISTRY

    assert _service(name)["image"] == tag
    assert any(spec.image == tag for spec in REGISTRY.values())


@pytest.mark.unit
@pytest.mark.parametrize("name", ["opencode-agent", "agy-agent"])
def test_the_agent_service_builds_from_its_own_directory(name):
    build = _service(name)["build"]
    assert build["context"] == f"docker/{name}"
    assert build["dockerfile"] == "Dockerfile"


@pytest.mark.unit
@pytest.mark.parametrize("name", ["opencode-agent", "agy-agent"])
def test_the_agent_service_never_starts_on_a_plain_up(name):
    """These are build targets, not long-running services."""
    service = _service(name)
    assert service.get("command") in (["true"], "true")
    assert service.get("restart", "no") == "no"


@pytest.mark.unit
def test_the_orchestrator_is_not_in_the_agents_profile():
    """`docker compose up -d` must still start the orchestrator."""
    assert "profiles" not in _service("orchestrator")
