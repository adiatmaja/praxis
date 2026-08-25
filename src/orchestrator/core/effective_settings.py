"""Live settings that merge env-based Settings with DB overrides."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from orchestrator.config import Settings
from orchestrator.core.context_window import (
    YAML_KEY as CONTEXT_WINDOWS_KEY,
)
from orchestrator.core.context_window import (
    DeclaredWindows,
    parse_declared_windows,
)
from orchestrator.database import Database
from orchestrator.models.schemas import CapabilityProfile


if TYPE_CHECKING:
    from orchestrator.core.worker_presets import WorkerPreset


# Keys that callers are allowed to override at runtime.
# NEVER include auth_token or github_token.
# Live consumers (no restart needed): lm_studio_url (AgentManager.spawn_agent),
#   agent_model / agent_model_effort (OpusBridge._run_claude_raw).
# Restart-only: host, port, database_url (read only at process start).
EDITABLE_KEYS: frozenset[str] = frozenset(
    {
        "lm_studio_url",
        "agent_model",
        "agent_model_effort",
        "docs_root",
        "memory_md_path",
        "brainstorm_workspace",
        "auto_delegate.enabled",
    }
)


class EffectiveSettings:
    """Return effective config values, preferring DB overrides over env Settings."""

    def __init__(self, settings: Settings, db: Database) -> None:
        self._settings = settings
        self._db = db

    async def _get_override(self, key: str) -> str | None:
        """Return stored override value, or None if absent / empty string."""
        row = await self._db.fetch_one(
            "SELECT value FROM settings_overrides WHERE key = ?", (key,)
        )
        if row is None:
            return None
        val = row["value"]
        # Treat empty string as "unset"
        return val if val else None

    async def lm_studio_url(self) -> str:
        return (
            await self._get_override("lm_studio_url")
        ) or self._settings.lm_studio_url

    async def agent_model(self) -> str:
        return (await self._get_override("agent_model")) or self._settings.agent_model

    async def agent_model_effort(self) -> str | None:
        override = await self._get_override("agent_model_effort")
        if override is not None:
            return override
        return self._settings.agent_model_effort

    async def docs_root(self) -> str:
        return (await self._get_override("docs_root")) or self._settings.docs_root

    async def memory_md_path(self) -> str:
        return (
            await self._get_override("memory_md_path")
        ) or self._settings.memory_md_path

    async def brainstorm_workspace(self) -> str:
        return (
            await self._get_override("brainstorm_workspace")
        ) or self._settings.brainstorm_workspace

    async def auto_delegate_enabled(self) -> bool:
        """Return True when auto-delegate mode is toggled on."""
        return (await self._get_override("auto_delegate.enabled")) == "true"

    def auto_delegate_worker(self) -> dict[str, str]:
        """Return the worker used in auto-delegate mode (the global default)."""
        return {
            "harness": self._settings.default_worker_harness,
            "model": self._settings.default_worker_model,
        }

    async def worker_presets(self) -> list[WorkerPreset]:
        """Return the configured worker presets, parsed and validated."""
        from orchestrator.core.worker_presets import parse_presets

        yaml_data = await self._get_yaml()
        return parse_presets(yaml_data.get("worker_presets") or [])

    async def all_editable(self) -> dict[str, dict[str, Any]]:
        """Return {key: {"value": <effective>, "overridden": <bool>}} for editable keys."""
        rows = await self._db.fetch_all("SELECT key, value FROM settings_overrides")
        overrides: dict[str, str | None] = {}
        for row in rows:
            val = row["value"]
            overrides[row["key"]] = val if val else None

        accessors = {
            "lm_studio_url": self.lm_studio_url,
            "agent_model": self.agent_model,
            "agent_model_effort": self.agent_model_effort,
            "docs_root": self.docs_root,
            "memory_md_path": self.memory_md_path,
            "brainstorm_workspace": self.brainstorm_workspace,
            "auto_delegate.enabled": self.auto_delegate_enabled,
        }

        result: dict[str, dict[str, Any]] = {}
        for key, accessor in accessors.items():
            value = await accessor()
            overridden = key in overrides and overrides[key] is not None
            result[key] = {"value": value, "overridden": overridden}
        return result

    async def call_site_config(self, call_site: str, project_id: str | None) -> dict:
        """Resolve a brain call-site to {provider, model, effort}.

        Global settings_overrides (key ``models.<call_site>``) override the
        built-in defaults. Per-project overrides can extend this later.
        """
        import json

        from orchestrator.core.llm_router import CALL_SITE_DEFAULTS

        default = dict(CALL_SITE_DEFAULTS[call_site])
        row = await self._db.fetch_one(
            "SELECT value FROM settings_overrides WHERE key = ?",
            (f"models.{call_site}",),
        )
        if row and row["value"]:
            default.update(json.loads(row["value"]))
        return default

    async def _get_yaml(self) -> dict:
        """Return the raw YAML settings dict for capability/escalation lookups."""
        from orchestrator.core.settings_file import (
            config_file_path,
            load_yaml_settings,
        )

        return load_yaml_settings(config_file_path())

    async def registered_models(self) -> list[dict[str, Any]]:
        override = await self._get_override("models.registry")
        if override is not None:
            import json
            from typing import cast

            return cast(list[dict[str, Any]], json.loads(override))
        yaml_data = await self._get_yaml()
        from typing import cast

        return cast(
            list[dict[str, Any]], yaml_data.get("models", {}).get("registry", [])
        )

    async def role_chains(self) -> dict[str, list[str]]:
        override = await self._get_override("models.roles")
        if override is not None:
            import json
            from typing import cast

            return cast(dict[str, list[str]], json.loads(override))
        yaml_data = await self._get_yaml()
        from typing import cast

        return cast(dict[str, list[str]], yaml_data.get("models", {}).get("roles", {}))

    async def call_site_chain(
        self, call_site: str, project_id: str | None
    ) -> list[dict[str, Any]]:
        from orchestrator.core.roles import ROLE_OF_CALL_SITE

        role = ROLE_OF_CALL_SITE.get(call_site)
        if not role:
            return [await self.call_site_config(call_site, project_id)]

        chains = await self.role_chains()
        model_names = chains.get(role)
        if not model_names:
            return [await self.call_site_config(call_site, project_id)]

        registered = await self.registered_models()
        registry_dict = {
            item["name"]: {
                "provider": item["provider"],
                "model": item["model"],
                "effort": item["effort"],
            }
            for item in registered
            if "name" in item
        }

        chain_resolved = []
        for name in model_names:
            if name in registry_dict:
                chain_resolved.append(registry_dict[name])

        if not chain_resolved:
            return [await self.call_site_config(call_site, project_id)]

        return chain_resolved

    async def declared_context_windows(self) -> DeclaredWindows:
        """Return the declared per-model / per-harness context windows.

        The settings file is MOUNTED, so declaring a window for a new cloud
        model is a YAML edit plus ``docker compose restart`` and never a
        rebuild. Whatever the file declares is merged over the shipped
        defaults in ``core/context_window``.
        """
        yaml_data = await self._get_yaml()
        return parse_declared_windows(yaml_data.get(CONTEXT_WINDOWS_KEY))

    async def capability_profile(
        self, project_id: str | None, model: str | None = None
    ) -> CapabilityProfile:
        """Resolve the capability profile: project override -> declared -> YAML.

        The ``context_window`` field takes one extra layer that the rest of the
        profile does not: a window DECLARED for this model name beats the
        ``capability.default`` block. That default is one number for every
        model on the install (8192 as shipped, sized for a local open-weight
        worker), and it feeds the difficulty scorer's ``context_ratio``
        denominator, the decomposer's per-leaf budget, leaf triage, and the
        capability plan review. A cloud-harness project inherited it and was
        therefore split more aggressively than its model ever needed - the
        quieter half of the same defect that failed 14 KB agy dispatches at
        dispatch time. A per-project capability override still wins over both.

        Args:
            project_id: Optional project scope for per-project overrides.
            model: Model name key; defaults to ``"default"``.

        Returns:
            A populated CapabilityProfile instance.
        """
        import json

        model_name = model or "default"
        yaml_data = await self._get_yaml()
        defaults: dict = yaml_data.get("capability", {}).get("default", {})

        override_data: dict = {}
        if project_id is not None:
            raw = await self._get_override(
                f"project.{project_id}.capability.{model_name}"
            )
            if raw:
                override_data = json.loads(raw)

        data = {**defaults}
        # Keyed by MODEL only: this seam has no harness in scope (its callers
        # pass a model name and nothing else), so the per-harness fallback is
        # applied at dispatch, where the harness is known. A model that is
        # declared nowhere leaves the YAML default exactly as it was.
        declared = parse_declared_windows(yaml_data.get(CONTEXT_WINDOWS_KEY)).for_model(
            model_name
        )
        if declared is not None:
            data["context_window"] = declared
        data.update(override_data)
        return CapabilityProfile(model_name=model_name, **data)

    async def escalation_policy(self, project_id: str | None) -> str:
        """Resolve the escalation policy: project override -> YAML default -> ``"block"``.

        Args:
            project_id: Optional project scope for per-project overrides.

        Returns:
            One of ``"block"``, ``"brain"``, or ``"paid_fallback"``.
        """
        if project_id is not None:
            override = await self._get_override(
                f"project.{project_id}.escalation.policy"
            )
            if override:
                return str(override)
        yaml_data = await self._get_yaml()
        return str(yaml_data.get("escalation", {}).get("policy", "block"))

    async def implement_escalation(self) -> list[dict[str, Any]]:
        """Return the ordered implementer escalation ladder from YAML.

        Returns an empty list when unconfigured, which means "never escalate";
        a triage ``escalate`` decision then falls through to ``human``.
        """
        yaml_data = await self._get_yaml()
        ladder = yaml_data.get("implement_escalation") or []
        return list(ladder) if isinstance(ladder, list) else []

    async def max_leaves_per_plan(self) -> int:
        """Return the hard ceiling on total leaves in one plan (default 24)."""
        yaml_data = await self._get_yaml()
        raw = yaml_data.get("max_leaves_per_plan", 24)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 24

    async def difficulty_config(self) -> dict[str, Any]:
        """Return the difficulty scorer's weights, bias, and gate thresholds.

        Falls back to the module defaults key by key, so a partial YAML block
        (or none at all) still produces a usable scorer.
        """
        from orchestrator.core.difficulty import DEFAULT_BIAS, DEFAULT_WEIGHTS

        yaml_data = await self._get_yaml()
        raw = yaml_data.get("difficulty") or {}
        if not isinstance(raw, dict):
            raw = {}
        weights = {**DEFAULT_WEIGHTS}
        for name, value in (raw.get("weights") or {}).items():
            if name in weights:
                weights[name] = float(value)
        return {
            "weights": weights,
            "bias": float(raw.get("bias", DEFAULT_BIAS)),
            "reject_below": float(raw.get("reject_below", 0.35)),
            "flag_below": float(raw.get("flag_below", 0.55)),
        }

    async def approvals_digest_interval_h(self) -> float:
        """Return hours between ``approvals_digest`` SSE events, YAML-configurable.

        Defaults to 6 hours. Parked work stays visible on ``poll_task``,
        ``poll_plan``, ``praxis pending``, and the dashboard badge
        continuously; this only rate-limits the SSE event itself.
        """
        yaml_data = await self._get_yaml()
        return float(yaml_data.get("approvals_digest_interval_h", 6.0))

    async def set_override(self, key: str, value: str | None) -> None:
        """Upsert an override (value=None deletes the override row)."""
        if value is None:
            await self._db.execute(
                "DELETE FROM settings_overrides WHERE key = ?", (key,)
            )
        else:
            await self._db.execute(
                """
                INSERT INTO settings_overrides (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (key, value),
            )
