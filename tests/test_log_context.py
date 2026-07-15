"""Tests for log_context correlation-ID LoggerAdapter."""

from __future__ import annotations

import logging

import pytest

from orchestrator.core.log_context import task_logger


class TestTaskLogger:
    """Verify task_logger prefixes log records with correlation IDs."""

    def test_full_ids_prefix(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = task_logger(
            logging.getLogger(__name__),
            plan_id="p1",
            task_id="t1",
            run_id="r1",
        )
        with caplog.at_level(logging.INFO):
            logger.info("hello")
        record = caplog.records[0]
        assert "plan=p1" in record.message
        assert "task=t1" in record.message
        assert "run=r1" in record.message
        assert "hello" in record.message

    def test_task_only_skips_none_ids(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = task_logger(
            logging.getLogger(__name__),
            task_id="t9",
        )
        with caplog.at_level(logging.INFO):
            logger.info("work")
        record = caplog.records[0]
        assert "task=t9" in record.message
        assert "plan=" not in record.message
        assert "run=" not in record.message
        assert "work" in record.message
