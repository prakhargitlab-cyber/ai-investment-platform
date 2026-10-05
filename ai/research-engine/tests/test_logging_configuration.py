import io
import json
import logging

import pytest
from pydantic import ValidationError

from app.settings import Settings, _StructuredLogFormatter, configure_application_logging


def test_research_log_level_defaults_to_warning() -> None:
    assert Settings().research_log_level == "WARNING"


def test_info_log_level_configures_app_logger_without_adding_handlers() -> None:
    root = logging.getLogger()
    app_logger = logging.getLogger("app")
    original_root, original_app, handlers = root.level, app_logger.level, list(root.handlers)
    try:
        configure_application_logging(Settings(research_log_level="info"))
        assert root.level == logging.INFO
        assert app_logger.getEffectiveLevel() == logging.INFO
        assert root.handlers == handlers
    finally:
        root.setLevel(original_root)
        app_logger.setLevel(original_app)


def test_invalid_research_log_level_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Unsupported research log level"):
        Settings(research_log_level="verbose")


def test_structured_logs_redact_headers_and_provider_query_tokens() -> None:
    record = logging.LogRecord(
        "app.provider", logging.ERROR, __file__, 1,
        "failure Authorization: Bearer header-secret url=https://provider.invalid/data?api_token=query-secret&fmt=json",
        (), None,
    )
    output = json.loads(_StructuredLogFormatter("research-engine", "AZURE").format(record))
    assert "header-secret" not in output["message"]
    assert "query-secret" not in output["message"]
    assert output["message"].count("<redacted>") == 2


def test_structured_logs_preserve_exception_type_message_and_traceback() -> None:
    # DI-OBS-1 regression: logger.exception() / exc_info=True must survive
    # the structured JSON formatter. Before this fix, the formatter never
    # read record.exc_info, so opportunity_cycle_failed (and every other
    # logger.exception call) silently dropped the exception class, message,
    # and traceback -- leaving no way to diagnose a crash once the raw pod
    # log buffer rotated past the event.
    logger = logging.getLogger("app.test_obs1")
    logger.setLevel(logging.ERROR)
    logger.propagate = False
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(_StructuredLogFormatter("research-engine", "AZURE"))
    logger.addHandler(handler)
    try:
        try:
            raise TypeError("synthetic failure token=header-secret")
        except TypeError:
            logger.exception("opportunity_cycle_failed cycleId=%s correlationId=%s errorType=%s",
                              "cycle-1", "corr-1", "TypeError")
    finally:
        logger.removeHandler(handler)

    output = json.loads(stream.getvalue().strip())
    assert output["exceptionType"] == "TypeError"
    assert "synthetic failure" in output["exceptionMessage"]
    assert "header-secret" not in output["exceptionMessage"]
    assert "token=<redacted>" in output["exceptionMessage"]
    assert "test_structured_logs_preserve_exception_type_message_and_traceback" in output["traceback"]
    assert "Traceback" in output["traceback"]


def test_structured_logs_without_exception_omit_exception_fields() -> None:
    record = logging.LogRecord("app.provider", logging.INFO, __file__, 1, "ordinary message", (), None)
    output = json.loads(_StructuredLogFormatter("research-engine", "AZURE").format(record))
    assert "exceptionType" not in output
    assert "exceptionMessage" not in output
    assert "traceback" not in output
