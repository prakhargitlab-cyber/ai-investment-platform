from typing import Any, Protocol

from pydantic import BaseModel, ValidationError


class LlmProvider(Protocol):
    def structured_extract(self, prompt: str, schema: type[BaseModel]) -> BaseModel | None:
        """Return schema-validated extraction output or None without fabricating missing data."""


class OllamaProvider:
    def structured_extract(self, prompt: str, schema: type[BaseModel]) -> BaseModel | None:
        return None


class FutureOpenAIProvider:
    def structured_extract(self, prompt: str, schema: type[BaseModel]) -> BaseModel | None:
        return None


class FutureAzureOpenAIProvider:
    def structured_extract(self, prompt: str, schema: type[BaseModel]) -> BaseModel | None:
        return None


def validate_structured_output(payload: dict[str, Any], schema: type[BaseModel]) -> BaseModel | None:
    try:
        return schema.model_validate(payload)
    except ValidationError:
        return None
