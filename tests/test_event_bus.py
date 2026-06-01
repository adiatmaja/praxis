"""Event bus tests."""
# ruff: noqa: S101

from __future__ import annotations

import asyncio

import pytest

from orchestrator.core.event_bus import EventBus


@pytest.mark.unit
class TestEventBus:
    async def test_subscribe_and_receive(self) -> None:
        bus = EventBus()
        queue = bus.subscribe()

        bus.publish({"type": "task_started", "task_id": "t1"})
        event = await asyncio.wait_for(queue.get(), timeout=1.0)

        assert event["type"] == "task_started"
        assert event["task_id"] == "t1"
        assert "timestamp" in event

    async def test_multiple_subscribers(self) -> None:
        bus = EventBus()
        q1 = bus.subscribe()
        q2 = bus.subscribe()

        bus.publish({"type": "test", "data": "hello"})
        e1 = await asyncio.wait_for(q1.get(), timeout=1.0)
        e2 = await asyncio.wait_for(q2.get(), timeout=1.0)

        assert e1["data"] == "hello"
        assert e2["data"] == "hello"

    async def test_unsubscribe(self) -> None:
        bus = EventBus()
        queue = bus.subscribe()

        bus.unsubscribe(queue)
        bus.publish({"type": "test"})

        assert queue.empty()

    async def test_publish_with_no_subscribers(self) -> None:
        bus = EventBus()

        bus.publish({"type": "test"})

    async def test_subscriber_count(self) -> None:
        bus = EventBus()
        q1 = bus.subscribe()
        bus.subscribe()

        assert bus.subscriber_count == 2
        bus.unsubscribe(q1)
        assert bus.subscriber_count == 1
