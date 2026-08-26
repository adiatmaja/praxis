"""Live settings that merge env-based Settings with DB overrides."""

from __future__ import annotations

import json
import logging
import math
from typing import TYPE_CHECKING, Any, cast

from orchestrator.config import Settings
from orchestrator.core.context_window import (
    YAML_KEY as CONTEXT_WINDOWS_KEY,
)
from orchestrator.core.context_window import (
    DeclaredWindows,
    parse_declared_windows,
    positive_window,
)
from orchestrator.database import Database
from orchestrator.models.schemas import CapabilityProfile


logger = logging.getLogger(__name__)

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


#: DB key that used to hold EVERY role's chain in one row.
#:
#: It is still read, because an install that ever ran ``praxis config
#: set-role`` has one, and dropping it on upgrade would silently re-route
#: that install's brain calls. It is no longer WRITTEN: every writer of
#: ``PUT /api/settings/roles`` sends the whole effective map back (they read
#: it, change one key, and PUT it), so storing the body wholesale pinned
#: every role in the database after a single ``set-role`` and left the
#: mounted ``config/praxis.yaml`` unable to change any chain again -- which
#: is the documented way to change them, and the reason the file is mounted
#: rather than baked. ``api.settings.put_roles`` consumes this row and
#: re-expresses it per role.
LEGACY_ROLE_CHAINS_KEY = "models.roles"

#: Prefix of the per-role override keys (``models.roles.plan``, ...).
#:
#: Deliberately a strict extension of :data:`LEGACY_ROLE_CHAINS_KEY`, so the
#: existing ``DELETE ... WHERE key LIKE 'models.%'`` reset path keeps
#: clearing role chains. It cannot collide with the per-call-site keys
#: (``models.<call_site>``): no call site is named ``roles.<anything>``.
ROLE_CHAIN_KEY_PREFIX = "models.roles."

#: DB key holding the model registry. Unlike the role chains this stays
#: wholesale, and the difference is in the data, not in the effort: a list
#: has no per-entry identity that "absent means use the settings file" could
#: hang off, and a per-entry merge could no longer express REMOVING a model
#: the file declares -- which ``PUT /api/settings/registry`` documents as its
#: behaviour. What it does share is the rule below: a registry equal to the
#: file's is not stored at all.
REGISTRY_KEY = "models.registry"


def role_chain_key(role: str) -> str:
    """Return the settings_overrides key holding one role's chain."""
    return f"{ROLE_CHAIN_KEY_PREFIX}{role}"


def _as_threshold(value: Any, default: float) -> float:
    """Return a difficulty threshold, or its default when the value is unusable.

    Same rule as ``difficulty.resolve_weights`` and for the same reason: a bare
    ``float()`` on operator YAML raised out of ``difficulty_config`` and wedged
    decomposition for every plan on the install, which is exactly what
    ``build_scorer``'s docstring promises cannot happen. A non-finite value is
    rejected too: NaN makes every comparison False, so the gate stops flagging
    anything while reading as though it ran.

    Args:
        value: The raw YAML value, possibly absent, of the wrong type, or NaN.
        default: The shipped threshold to fall back to.

    Returns:
        The usable threshold.
    """
    if value is None:
        return default
    try:
        resolved = float(value)
    except (TypeError, ValueError):
        logger.warning(
            "Ignoring unusable difficulty threshold %r; keeping %s", value, default
        )
        return default
    if not math.isfinite(resolved):
        logger.warning(
            "Ignoring non-finite difficulty threshold %r; keeping %s", value, default
        )
        return default
    return resolved


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

    async def _yaml_models(self) -> dict[str, Any]:
        """Return the settings file's ``models`` block, or ``{}``."""
        yaml_data = await self._get_yaml()
        models = yaml_data.get("models")
        return models if isinstance(models, dict) else {}

    async def settings_file_registered_models(self) -> list[dict[str, Any]]:
        """Return the registry the settings FILE declares, ignoring the DB.

        The write path needs this to tell an operator's own registry apart
        from the file's: a body equal to the file is not an override at all,
        and storing one would freeze the mounted file out of a surface it is
        supposed to own.
        """
        registry = (await self._yaml_models()).get("registry")
        return cast(list[dict[str, Any]], registry) if registry else []

    async def registered_models(self) -> list[dict[str, Any]]:
        override = await self._get_override(REGISTRY_KEY)
        if override is not None:
            return cast(list[dict[str, Any]], json.loads(override))
        return await self.settings_file_registered_models()

    async def settings_file_role_chains(self) -> dict[str, list[str]]:
        """Return the chains the settings FILE declares, ignoring the DB.

        Same job as :meth:`settings_file_registered_models`: it is the
        baseline a submitted chain is compared against, so that only a chain
        that actually differs from the file is stored.
        """
        chains = (await self._yaml_models()).get("roles")
        return cast(dict[str, list[str]], chains) if chains else {}

    async def role_chain_overrides(self) -> dict[str, list[str]]:
        """Return the per-role chains held in the database, keyed by role."""
        rows = await self._db.fetch_all(
            "SELECT key, value FROM settings_overrides WHERE key LIKE ?",
            (f"{ROLE_CHAIN_KEY_PREFIX}%",),
        )
        stored: dict[str, list[str]] = {}
        for row in rows:
            value = row["value"]
            if not value:
                continue
            role = str(row["key"])[len(ROLE_CHAIN_KEY_PREFIX) :]
            if role:
                stored[role] = cast(list[str], json.loads(value))
        return stored

    async def role_chains(self) -> dict[str, list[str]]:
        """Return the effective per-role fallback chains.

        Layered per role: a stored per-role override wins, otherwise the
        settings file decides. The legacy wholesale row (see
        :data:`LEGACY_ROLE_CHAINS_KEY`) replaces the file's map entirely when
        present, exactly as it did before it stopped being written -- an
        upgraded install must resolve to what it resolved to yesterday, and
        the file's chains must not reappear underneath it uninvited.
        """
        legacy = await self._get_override(LEGACY_ROLE_CHAINS_KEY)
        overrides = await self.role_chain_overrides()
        if legacy is None:
            chains = dict(await self.settings_file_role_chains())
        elif not overrides:
            # Unchanged from the pre-split behaviour, down to not reading
            # the settings file at all.
            return cast(dict[str, list[str]], json.loads(legacy))
        else:
            chains = cast(dict[str, list[str]], json.loads(legacy))
        chains.update(overrides)
        return chains

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
        self,
        project_id: str | None,
        model: str | None = None,
        harness: str | None = None,
        project_context_window: int | None = None,
    ) -> CapabilityProfile:
        """Resolve the capability profile: project window -> declared -> YAML.

        The ``context_window`` field takes layers the rest of the profile does
        not, and they are the SAME layers the dispatch gate uses (steps 1 to 3
        of ``core/context_window``), so the two seams cannot answer differently
        about one model. The probe is deliberately absent: this is a config
        read on the planning path, not a dispatch, and it must not make a
        network call per decomposition.

        The ``capability.default`` block underneath is one number for every
        model on the install (8192 as shipped, sized for a local open-weight
        worker), and it feeds the difficulty scorer's ``context_ratio``
        denominator, the decomposer's per-leaf budget, leaf triage, and the
        capability plan review. Every project inherited it, so a cloud-model
        plan was split as aggressively as an 8 K worker would need - the
        quieter half of the defect that failed 14 KB agy dispatches.

        ``harness`` is what makes the third tier reachable here. Without it a
        model-string variant nobody enumerated (``Gemini 3.7 Pro``, or the agy
        id spelling ``gemini-3.7-flash-high``) resolved at the dispatch gate and
        was still decomposed at 8192 - the same defect one seam over.

        Args:
            project_id: Optional project scope for per-project overrides.
            model: Model name key; defaults to ``"default"``.
            harness: The harness that will run this model, enabling the
                per-harness declaration tier. None skips that tier only.
            project_context_window: The project row's ``context_window``
                column. Wins over everything here exactly as it does at the
                gate, INCLUDING over a per-project capability override: an
                operator who states a window means it, and a seam where it
                won in one place and lost in the other is the disagreement
                this whole change exists to remove.

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
        declared = parse_declared_windows(yaml_data.get(CONTEXT_WINDOWS_KEY))
        by_model = declared.for_model(model_name)
        by_harness = declared.for_harness(harness)
        if by_model is not None:
            data["context_window"] = by_model
        elif by_harness is not None:
            data["context_window"] = by_harness
        data.update(override_data)
        # Applied AFTER the override merge, because it outranks it. Validated
        # through the same coercion the gate uses, so a NULL column, a zero, or
        # a mock all read as "not stated" rather than as a one-token window.
        column = positive_window(project_context_window)
        if column is not None:
            data["context_window"] = column
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
        from orchestrator.core.difficulty import resolve_bias, resolve_weights

        yaml_data = await self._get_yaml()
        raw = yaml_data.get("difficulty") or {}
        if not isinstance(raw, dict):
            raw = {}
        # This is the seat a YAML typo actually reaches, and a bare float() here
        # raised BEFORE anything downstream could degrade: an operator writing
        # `loc_ratio: high` (a natural mistake, since `agentic_coding: "high"`
        # appears in the capability snapshot two blocks up) got a ValueError out
        # of this method, into the per-plan quarantine, and decomposition wedged
        # on every plan on the install. ``difficulty.build_scorer`` promises in
        # its docstring that a typo degrades the score and never wedges
        # decomposition; that promise is only true if the value is resolved by
        # the SAME functions here, so an unusable weight keeps its grounded
        # default and says so rather than raising or silently becoming zero.
        return {
            "weights": resolve_weights(raw),
            "bias": resolve_bias(raw),
            "reject_below": _as_threshold(raw.get("reject_below"), 0.35),
            "flag_below": _as_threshold(raw.get("flag_below"), 0.55),
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
