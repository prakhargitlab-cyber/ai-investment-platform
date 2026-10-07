from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ValidationError


@runtime_checkable
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


class FutureLlamaProvider:
    """Architectural placeholder proving the LlmProvider contract can be satisfied by a
    future self-hosted/Llama-family implementation without any business-logic change.

    This class intentionally does NOT run inference, select a hosting vendor, download
    model weights, or install an inference stack. It exists only so that
    `get_llm_provider()` can demonstrate config-driven provider registration end to end.
    A real implementation would replace `structured_extract` with an actual call to
    whatever endpoint/credentials are supplied via configuration (e.g. Key Vault-sourced
    settings); until then it returns None, same as the other Future* stubs.
    """

    def structured_extract(self, prompt: str, schema: type[BaseModel]) -> BaseModel | None:
        return None


# Registry mapping a configuration value (Settings.llm_provider, sourced from the
# AIP_LLM_PROVIDER environment variable / Key Vault-backed config in deployment) to a
# concrete LlmProvider implementation. Adding a real future provider (e.g. Llama) is
# intended to require only: implement the LlmProvider contract, register it below under
# its configuration key, and supply its endpoint/credentials via configuration — no
# changes to portfolio logic, Equity Radar, ETF Radar, Research Readiness, deterministic
# calculations, or recommendation/ranking engines, none of which call this module.
_PROVIDER_REGISTRY: dict[str, type] = {
    "ollama": OllamaProvider,
    "openai": FutureOpenAIProvider,
    "azure-openai": FutureAzureOpenAIProvider,
    "llama": FutureLlamaProvider,
}


def get_llm_provider(provider_name: str) -> LlmProvider:
    """Resolve a configuration value to a concrete LlmProvider instance.

    Unknown provider names fall back to OllamaProvider (the current default/inert
    provider) rather than raising, since no caller currently depends on this value being
    authoritative (see GET /providers/llm, mode="optional-not-called").
    """
    provider_class = _PROVIDER_REGISTRY.get(provider_name, OllamaProvider)
    return provider_class()


def validate_structured_output(payload: dict[str, Any], schema: type[BaseModel]) -> BaseModel | None:
    try:
        return schema.model_validate(payload)
    except ValidationError:
        return None
