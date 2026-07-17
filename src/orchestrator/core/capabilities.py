import json
import logging
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

# Assuming the root directory is 4 levels up from this file (src/orchestrator/core/capabilities.py)
DEFAULT_PATH = str(
    Path(__file__).parent.parent.parent.parent / "config" / "model_capabilities.json"
)


class CapabilityCatalog:
    def __init__(self, path: str = DEFAULT_PATH) -> None:
        self.path = path
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
