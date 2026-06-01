"""In-memory pub/sub event bus for server-sent events."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any


logger = logging.getLogger(__name__)


class EventBus:
    """Broadcast JSON-serializable events to active SSE subscribers."""

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """Register a subscriber queue."""

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers.append(queue)
        logger.debug("New event subscriber, total: %d", len(self._subscribers))
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Remove a subscriber queue if it is active."""

        if queue in self._subscribers:
            self._subscribers.remove(queue)
            logger.debug("Event subscriber removed, total: %d", len(self._subscribers))

    def publish(self, event: dict[str, Any]) -> None:
        """Publish an event to all active subscribers."""

        payload = dict(event)
        payload.setdefault("timestamp", datetime.now(UTC).isoformat())
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                logger.warning("Subscriber queue full, dropping event")

    @property
    def subscriber_count(self) -> int:
        """Return the number of active subscribers."""

        return len(self._subscribers)
