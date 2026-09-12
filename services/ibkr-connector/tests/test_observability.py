import json
import logging

from app.observability import StructuredFormatter


def test_structured_logs_redact_headers_and_provider_query_tokens() -> None:
    record = logging.LogRecord(
        "app.gateway", logging.ERROR, __file__, 1,
        "failure Authorization: Bearer header-secret url=https://provider.invalid/data?api_token=query-secret&fmt=json",
        (), None,
    )
    output = json.loads(StructuredFormatter("ibkr-connector", "AZURE").format(record))
    assert "header-secret" not in output["message"]
    assert "query-secret" not in output["message"]
    assert output["message"].count("<redacted>") == 2
