"""A NEW project created via execute-plan must honor the CONFIGURED worker.

Defect: `_create_or_reuse_project`'s new-project branch defaulted an omitted
harness to `default_harness_id()` (the hardcoded registry default,
"opencode"), never consulting the deployment's configured default worker
(`EffectiveSettings.auto_delegate_worker()`, backed by
`default_worker_harness`/`default_worker_model`). `add-project`
(`POST /api/projects`) already honors that configured default
(`resolved_harness = body.harness or settings.default_worker_harness`), so
the same omitted field meant two different harnesses depending on which
entry point created the project, and "which harness ran this" became
unanswerable.

Fix: a new project with no explicit harness gets the configured default
worker's harness, falling back to `default_harness_id()` only when no
default worker is configured (`default_worker_model` empty, the existing
convention `POST /api/projects` already uses to mean "unconfigured", see
`api/projects.py`'s `model_name is required and no default_worker_model is
configured` error).

The EXISTING-project carve-out (an omitted harness must never re-point an
already-configured project) is untouched by this fix and is covered by
`tests/test_harness_parity.py::test_omitted_harness_preserves_the_projects_configured_harness`
and `::test_explicit_harness_still_overrides_the_projects_configured_harness`,
both of which call `_create_or_reuse_project` with no `default_worker`
argument at all, so this fix's new optional parameter must default to
"behave as before" to keep them green.
"""

from __future__ import annotations

import pytest

from orchestrator.api.execute_plan import _create_or_reuse_project
from orchestrator.config import Settings
from orchestrator.core.effective_settings import EffectiveSettings
from orchestrator.core.harnesses import default_harness_id
from orchestrator.database import Database
from tests.conftest import seed_user


def _settings(tmp_path, **overrides: object) -> Settings:
    """Build an isolated Settings, immune to the repo's real config/praxis.yaml.

    Mirrors the pattern in tests/test_config_default_worker.py.
    """
    empty_yaml = tmp_path / "praxis.yaml"
    empty_yaml.write_text("", encoding="utf-8")
    return Settings(
        _env_file=None, yaml_path=str(empty_yaml), auth_token="t", **overrides
    )


@pytest.mark.integration
async def test_new_project_uses_configured_default_worker_harness(
    db: Database, tmp_path
) -> None:
    """A configured default worker's harness wins over the registry default."""
    await seed_user(db)
    settings = _settings(
        tmp_path,
        default_worker_harness="agy",
        default_worker_model="Gemini 3.7 Flash (High)",
    )
    effective_settings = EffectiveSettings(settings, db)
    assert default_harness_id() != "agy"  # otherwise this test proves nothing

    project_id = await _create_or_reuse_project(
        db,
        "https://github.com/o/new-agy-project",
        None,
        "qwen3.6-27b",
        harness=None,
        default_worker=effective_settings.auto_delegate_worker(),
    )

    row = await db.fetch_one("SELECT harness FROM projects WHERE id = ?", (project_id,))
    assert row is not None
    assert row["harness"] == "agy"


@pytest.mark.integration
async def test_new_project_falls_back_to_registry_default_when_unconfigured(
    db: Database, tmp_path
) -> None:
    """No configured default worker (empty model) => registry default, not

    the field's harness value alone. default_worker_harness is set to a
    non-default value here specifically so a fix that ignores the "is a
    worker actually configured" question and just reads the harness field
    would fail this test.
    """
    await seed_user(db)
    settings = _settings(
        tmp_path,
        default_worker_harness="agy",
        default_worker_model="",
    )
    effective_settings = EffectiveSettings(settings, db)

    project_id = await _create_or_reuse_project(
        db,
        "https://github.com/o/new-unconfigured-project",
        None,
        "qwen3.6-27b",
        harness=None,
        default_worker=effective_settings.auto_delegate_worker(),
    )

    row = await db.fetch_one("SELECT harness FROM projects WHERE id = ?", (project_id,))
    assert row is not None
    assert row["harness"] == default_harness_id()


@pytest.mark.integration
async def test_new_project_explicit_harness_still_wins(db: Database, tmp_path) -> None:
    """A caller-supplied harness is a real preference and beats the default worker."""
    await seed_user(db)
    settings = _settings(
        tmp_path,
        default_worker_harness="agy",
        default_worker_model="Gemini 3.7 Flash (High)",
    )
    effective_settings = EffectiveSettings(settings, db)

    project_id = await _create_or_reuse_project(
        db,
        "https://github.com/o/new-explicit-project",
        None,
        "qwen3.6-27b",
        harness="opencode",
        default_worker=effective_settings.auto_delegate_worker(),
    )

    row = await db.fetch_one("SELECT harness FROM projects WHERE id = ?", (project_id,))
    assert row is not None
    assert row["harness"] == "opencode"


@pytest.mark.integration
async def test_new_project_without_default_worker_arg_keeps_registry_default(
    db: Database,
) -> None:
    """Omitting the new default_worker argument entirely must not change

    behavior for any caller that has not been updated to pass it (backward
    compatibility for the two existing tests in test_harness_parity.py that
    call `_create_or_reuse_project` with only the original five arguments).
    """
    await seed_user(db)

    project_id = await _create_or_reuse_project(
        db,
        "https://github.com/o/new-legacy-call-project",
        None,
        "qwen3.6-27b",
        harness=None,
    )

    row = await db.fetch_one("SELECT harness FROM projects WHERE id = ?", (project_id,))
    assert row is not None
    assert row["harness"] == default_harness_id()
