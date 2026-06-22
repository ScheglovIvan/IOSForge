"""Tests for structlog wiring (T-1.3, SPEC §8)."""

from __future__ import annotations

import json

import pytest

from iosforge.common.logging import bind_job_context, configure_logging, get_logger


def _parse_last_json_line(captured: str) -> dict[str, object]:
    lines = [line for line in captured.strip().splitlines() if line.strip()]
    assert lines, "expected at least one log line"
    return json.loads(lines[-1])


def test_logs_emitted_as_json(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(json_output=True)
    log = get_logger("test.json")
    log.info("hello", extra_field="value")

    record = _parse_last_json_line(capsys.readouterr().out)
    assert record["event"] == "hello"
    assert record["extra_field"] == "value"
    assert record["level"] == "info"
    assert "timestamp" in record
    assert record["logger"] == "test.json"


def test_job_context_present_in_log(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(json_output=True)
    log = bind_job_context(get_logger("test.ctx"), job_id="job-123", stage="discovery")
    log.info("stage started")

    record = _parse_last_json_line(capsys.readouterr().out)
    assert record["job_id"] == "job-123"
    assert record["stage"] == "discovery"
    assert record["event"] == "stage started"


def test_get_logger_configures_lazily(capsys: pytest.CaptureFixture[str]) -> None:
    """get_logger works (and emits JSON) even without an explicit configure call."""
    log = get_logger("lazy")
    log.warning("lazy init")
    record = _parse_last_json_line(capsys.readouterr().out)
    assert record["event"] == "lazy init"
    assert record["level"] == "warning"


def test_level_filtering(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="WARNING", json_output=True)
    log = get_logger("filtered")
    log.debug("should be dropped")
    log.error("should appear")

    out = capsys.readouterr().out
    assert "should be dropped" not in out
    record = _parse_last_json_line(out)
    assert record["event"] == "should appear"
