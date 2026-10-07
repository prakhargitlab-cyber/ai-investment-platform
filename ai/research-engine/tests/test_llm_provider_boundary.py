"""Proves the LLM provider abstraction is config-driven, provider-neutral, and that
business/domain logic does not depend on any single vendor.

This is the architectural-readiness proof required for a future Llama integration:
`get_llm_provider` resolves purely from configuration, every registered provider
(including the Llama placeholder) satisfies the same `LlmProvider` Protocol, and no
module outside app/llm.py (and the inert /providers/llm introspection endpoint) imports
any of these provider classes -- i.e. nothing in portfolio/Equity Radar/ETF Radar/
Research Readiness/deterministic calculation/ranking code is coupled to a vendor SDK or
a hardcoded endpoint.
"""
import pathlib
import subprocess
import sys

from pydantic import BaseModel

from app.llm import (
    FutureAzureOpenAIProvider,
    FutureLlamaProvider,
    FutureOpenAIProvider,
    LlmProvider,
    OllamaProvider,
    _PROVIDER_REGISTRY,
    get_llm_provider,
    validate_structured_output,
)


class _Schema(BaseModel):
    value: str


def test_registry_resolves_known_providers_by_configuration_string():
    assert isinstance(get_llm_provider("ollama"), OllamaProvider)
    assert isinstance(get_llm_provider("openai"), FutureOpenAIProvider)
    assert isinstance(get_llm_provider("azure-openai"), FutureAzureOpenAIProvider)
    assert isinstance(get_llm_provider("llama"), FutureLlamaProvider)


def test_unknown_provider_name_falls_back_safely_rather_than_raising():
    resolved = get_llm_provider("some-future-vendor-nobody-registered-yet")
    assert isinstance(resolved, OllamaProvider)


def test_every_registered_provider_satisfies_the_same_protocol_structurally():
    # Structural (duck-typed) Protocol conformance: every provider exposes the same
    # single method with the same call signature, so swapping one in for another never
    # requires a caller-side change.
    for provider_class in _PROVIDER_REGISTRY.values():
        instance = provider_class()
        assert isinstance(instance, LlmProvider)
        assert instance.structured_extract("prompt", _Schema) is None


def test_llama_provider_is_an_inert_placeholder_not_a_real_integration():
    # Guards the explicit "do not integrate or run Llama" boundary: the stub never
    # performs network I/O, never reads model weights, and always returns None.
    provider = FutureLlamaProvider()
    assert provider.structured_extract("anything", _Schema) is None


def test_validate_structured_output_never_fabricates_on_bad_payload():
    assert validate_structured_output({"value": "x"}, _Schema) == _Schema(value="x")
    assert validate_structured_output({"wrong_key": 1}, _Schema) is None


def test_no_business_logic_module_imports_llm_providers_directly():
    # The only legitimate references to app.llm provider classes/functions are inside
    # app/llm.py itself and the inert introspection endpoint in app/main.py. If any
    # domain module (portfolio, equity/etf radar, research readiness, rule engines,
    # ranking) started importing a concrete provider class directly, that would be the
    # vendor-coupling this boundary exists to prevent.
    app_dir = pathlib.Path(__file__).resolve().parents[1] / "app"
    offenders = []
    needles = (
        "OllamaProvider",
        "FutureOpenAIProvider",
        "FutureAzureOpenAIProvider",
        "FutureLlamaProvider",
    )
    for path in app_dir.rglob("*.py"):
        if path.name in {"llm.py"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if path.name == "main.py":
            # main.py's /providers/llm endpoint only calls get_llm_provider() and
            # inspects the resolved instance's type name; it never imports or
            # references a concrete provider class by name.
            if "get_llm_provider" in text and not any(n in text for n in needles):
                continue
        if any(needle in text for needle in needles):
            offenders.append(str(path))
    assert offenders == [], f"Unexpected direct provider coupling in: {offenders}"


def test_providers_llm_endpoint_reports_resolved_provider_class():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        response = client.get("/providers/llm")
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "optional-not-called"
    assert body["provider"] == "ollama"
    assert body["resolved_provider_class"] == "OllamaProvider"


def test_llm_provider_is_configurable_via_environment(tmp_path):
    # Confirms Settings.llm_provider is sourced from the AIP_ environment prefix (not
    # hardcoded), satisfying the "externally configurable for Azure/Key Vault-backed
    # deployment" requirement.
    script = (
        "import os\n"
        "os.environ['AIP_LLM_PROVIDER'] = 'llama'\n"
        "from app.settings import Settings\n"
        "s = Settings()\n"
        "assert s.llm_provider == 'llama', s.llm_provider\n"
        "print('ENV_OVERRIDE_OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=pathlib.Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ENV_OVERRIDE_OK" in result.stdout
