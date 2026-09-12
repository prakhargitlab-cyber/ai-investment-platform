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
