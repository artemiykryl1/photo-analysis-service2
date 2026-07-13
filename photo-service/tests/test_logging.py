"""Tests for `app.core.logging`.

Verifies: JSON output on stdout, `LOG_LEVEL` is honored, `trace_id_var`
contextvar defaults to "-" and is picked up per-record by `TraceIdFilter`
under both the `trace_id` (constitution.md §3.1) and `request_id`
(feature-upload DoD) field names, and `setup_logging()` is safe to call
repeatedly (idempotent handler registration - important because tests
import `app.main`, which calls `setup_logging` at import time).
"""

import json
import logging

from app.core.logging import TraceIdFilter, setup_logging, trace_id_var


def test_setup_logging_sets_level_from_argument():
    setup_logging("DEBUG")
    assert logging.getLogger().level == logging.DEBUG

    setup_logging("WARNING")
    assert logging.getLogger().level == logging.WARNING

    setup_logging("INFO")  # restore a sane default for subsequent tests


def test_setup_logging_is_idempotent_no_duplicate_handlers():
    setup_logging("INFO")
    first_count = len(logging.getLogger().handlers)

    setup_logging("INFO")
    second_count = len(logging.getLogger().handlers)

    assert first_count == second_count == 1


def test_log_record_emits_valid_json_with_expected_fields(capsys):
    setup_logging("INFO")
    logger = logging.getLogger("test.logger")

    logger.info("hello world")

    captured = capsys.readouterr()
    line = captured.out.strip().splitlines()[-1]
    payload = json.loads(line)  # must be valid JSON

    assert payload["message"] == "hello world"
    assert payload["service"] == "photo-service"
    assert payload["name"] == "test.logger"
    assert "timestamp" in payload
    assert payload["level"] == "INFO"
    assert "trace_id" in payload
    assert "request_id" in payload


def test_trace_id_defaults_to_placeholder():
    assert trace_id_var.get() == "-"


def test_trace_id_filter_reflects_current_contextvar_value():
    token = trace_id_var.set("abc-123")
    try:
        record = logging.LogRecord(
            name="x", level=logging.INFO, pathname=__file__, lineno=1,
            msg="msg", args=(), exc_info=None,
        )
        TraceIdFilter().filter(record)
        assert record.trace_id == "abc-123"
        assert record.request_id == "abc-123"
        assert record.service == "photo-service"
    finally:
        trace_id_var.reset(token)


def test_log_output_reflects_trace_id_var_when_set(capsys):
    setup_logging("INFO")
    logger = logging.getLogger("test.logger.trace")

    token = trace_id_var.set("trace-xyz")
    try:
        logger.info("with trace")
    finally:
        trace_id_var.reset(token)

    captured = capsys.readouterr()
    line = captured.out.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["trace_id"] == "trace-xyz"
    assert payload["request_id"] == "trace-xyz"
