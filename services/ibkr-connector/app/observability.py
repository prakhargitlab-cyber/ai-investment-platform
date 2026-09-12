from __future__ import annotations

import json
import logging
import re
from contextvars import ContextVar, Token
from datetime import UTC, datetime


_REQUEST_ID: ContextVar[str | None] = ContextVar("aip_request_id", default=None)
_SENSITIVE = re.compile(
    r"(?i)\b(authorization|cookie|password|token|api[_-]?(?:key|token)|"
    r"access[_-]?token|refresh[_-]?token|request[_-]?token|client[_-]?secret)"
    r"\s*[:=]\s*(?:bearer\s+)?([^\s,;&]+)"
)


class StructuredFormatter(logging.Formatter):
    def __init__(self, service: str, environment: str) -> None:
        super().__init__()
        self.service = service
        self.environment = environment

    def format(self, record: logging.LogRecord) -> str:
        message = _SENSITIVE.sub(r"\1=<redacted>", record.getMessage()).replace("\r", " ").replace("\n", " ")
        return json.dumps({
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "service": self.service,
            "environment": self.environment,
            "level": record.levelname,
            "event": record.name,
            "requestId": _REQUEST_ID.get(),
            "message": message,
        }, separators=(",", ":"), default=str)


def configure_logging(service: str, environment: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(StructuredFormatter(service, environment))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def set_request_id(value: str) -> Token:
    return _REQUEST_ID.set(value)


def reset_request_id(token: Token) -> None:
    _REQUEST_ID.reset(token)
