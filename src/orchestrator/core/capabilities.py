import json
import logging
from typing import Any

from orchestrator.core.settings_file import capabilities_file_path


logger = logging.getLogger(__name__)


class CapabilityCatalog:
    def __init__(self, path: str | None = None) -> None:
        # Resolved at call time via the shared settings_file seam, not frozen
        # into a keyword-argument default at import time: the latter would
        # keep serving whatever path was current on first import even if
        # PRAXIS_CAPABILITIES_PATH changed afterward (see
        # settings_file.config_file_path for the same reasoning).
        self.path = path if path is not None else capabilities_file_path()
        self._data: dict[str, dict[str, Any]] = {}
        self.as_of: str | None = None
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self._data = data.get("models", {})
            self.as_of = data.get("_meta", {}).get("as_of")
        except FileNotFoundError:
            logger.info(
                "Capability snapshot not found at %s. Using empty catalog.", self.path
            )
            self._data = {}
            self.as_of = None
        except json.JSONDecodeError as e:
            logger.warning(
                "Failed to parse capability snapshot at %s: %s. Using empty catalog.",
                self.path,
                e,
            )
            self._data = {}
            self.as_of = None
        except Exception as e:
            logger.warning(
                "Unexpected error loading capability snapshot at %s: %s. Using empty catalog.",
                self.path,
                e,
            )
            self._data = {}
            self.as_of = None

    def for_model(self, model_id: str) -> dict[str, Any] | None:
        return self._data.get(model_id)

    def all(self) -> dict[str, dict[str, Any]]:
        return self._data.copy()
