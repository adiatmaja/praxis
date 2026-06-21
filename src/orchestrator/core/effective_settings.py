"""Live settings that merge env-based Settings with DB overrides."""

from __future__ import annotations

from typing import Any

from orchestrator.config import Settings
from orchestrator.database import Database


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
