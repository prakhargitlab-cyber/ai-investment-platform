#!/usr/bin/env python3
"""Static LOCAL/TEST/AZURE configuration and Helm invariants.

This command renders manifests only. It never connects to a Kubernetes cluster
or an Azure subscription.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "infrastructure" / "helm" / "ai-investment-platform"


def fail(message: str) -> None:
    raise AssertionError(message)


def load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def merge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    result = dict(left)
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = value
    return result


def render(values_file: str, *extra: str) -> tuple[str, list[dict[str, Any]]]:
    command = [
        "helm", "template", "aip", str(CHART),
        "--namespace", "ai-investment", "-f", str(CHART / values_file), *extra,
    ]
    completed = subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
    documents = [item for item in yaml.safe_load_all(completed.stdout) if isinstance(item, dict)]
    return completed.stdout, documents


def render_must_fail(values_file: str, expected: str, *extra: str) -> None:
    command = [
        "helm", "template", "aip", str(CHART),
        "--namespace", "ai-investment", "-f", str(CHART / values_file), *extra,
    ]
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    assert completed.returncode != 0
    assert expected in completed.stderr


def resources(documents: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [document for document in documents if document.get("kind") == kind]


def deployment(documents: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for item in resources(documents, "Deployment"):
        if item.get("metadata", {}).get("name") == name:
            return item
    fail(f"Deployment {name!r} did not render")


def env_by_name(workload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    containers = workload["spec"]["template"]["spec"]["containers"]
    return {entry["name"]: entry for entry in containers[0].get("env", [])}


def value(env: dict[str, dict[str, Any]], name: str) -> str:
    if name not in env:
        fail(f"Environment variable {name} did not render")
    return str(env[name].get("value", ""))


def assert_profile_values() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    base = load_yaml(CHART / "values.yaml")
    local = merge(base, load_yaml(CHART / "values-dev.yaml"))
    test = merge(base, load_yaml(CHART / "values-test.yaml"))
    azure = merge(base, load_yaml(CHART / "values-azure.yaml"))
    assert local["global"]["environment"] == "LOCAL"
    assert local["global"]["springProfile"] == "LOCAL"
    assert test["global"]["environment"] == "TEST"
    assert test["global"]["springProfile"] == "TEST"
    assert azure["global"]["environment"] == "AZURE"
    assert azure["global"]["springProfile"] == "AZURE"
    assert azure["devDependencies"]["enabled"] is False
    return local, test, azure


def main() -> int:
    local_values, _, azure_values = assert_profile_values()
    local_text, local_docs = render("values-dev.yaml")
    azure_text, azure_docs = render("values-azure.yaml")
    _, kafka_docs = render(
        "values-azure.yaml", "--set", "kafka.enabled=true",
        "--set", "kafka.sslTruststoreSecretName=aip-kafka-ca",
    )
    _, yahoo_mcp_docs = render(
        "values-azure.yaml",
        "--set", "yahooFinanceMcp.enabled=true",
        "--set", "mcpGateway.externalProvidersEnabled=true",
        "--set", "mcpGateway.yahooFinance.enabled=true",
        "--set", "research.mcpAcquisition.enabled=true",
    )
    render_must_fail(
        "values-dev.yaml",
        "MCP gateway is INTERNAL_ONLY",
        "--set", "mcpGateway.ingress.enabled=true",
    )
    render_must_fail(
        "values-dev.yaml",
        "MCP gateway must use a ClusterIP Service",
        "--set", "mcpGateway.service.type=LoadBalancer",
    )
    render_must_fail(
        "values-dev.yaml",
        "First-party Yahoo Finance MCP is INTERNAL_ONLY",
        "--set", "yahooFinanceMcp.ingress.enabled=true",
    )
    render_must_fail(
        "values-dev.yaml",
        "First-party Yahoo Finance MCP must use a ClusterIP Service",
        "--set", "yahooFinanceMcp.service.type=LoadBalancer",
    )
    render_must_fail(
        "values-azure.yaml",
        "Yahoo Finance MCP requires the external provider gateway",
        "--set", "mcpGateway.yahooFinance.enabled=true",
    )

    assert "SecretProviderClass" not in {item.get("kind") for item in local_docs}
    assert "azure.workload.identity/use" not in local_text
    assert "AZURE_CLIENT_SECRET" not in azure_text
    assert "clientSecret" not in azure_text

    services = resources(azure_docs, "Service")
    assert services and all(item.get("spec", {}).get("type", "ClusterIP") == "ClusterIP" for item in services)
    local_mcp = deployment(local_docs, "mcp-gateway")
    azure_mcp = deployment(azure_docs, "mcp-gateway")
    local_mcp_services = [
        item for item in resources(local_docs, "Service")
        if item.get("metadata", {}).get("name") == "mcp-gateway"
    ]
    assert len(local_mcp_services) == 1 and local_mcp_services[0]["spec"]["type"] == "ClusterIP"
    mcp_services = [item for item in services if item.get("metadata", {}).get("name") == "mcp-gateway"]
    assert len(mcp_services) == 1 and mcp_services[0]["spec"]["type"] == "ClusterIP"
    ingress_names = {item["metadata"]["name"] for item in resources(azure_docs, "Ingress")}
    assert ingress_names == {"api-gateway"}
    assert "mcp-gateway" not in ingress_names

    local_mcp_env = env_by_name(local_mcp)
    azure_mcp_env = env_by_name(azure_mcp)
    assert value(local_mcp_env, "AIP_MCP_AUTHENTICATION_TYPE") == "LOCAL_SERVICE"
    assert value(local_mcp_env, "AIP_MCP_EXTERNAL_PROVIDERS_ENABLED") == "true"
    assert value(local_mcp_env, "AIP_MCP_YAHOO_ENABLED") == "true"
    assert "get_quote" in value(local_mcp_env, "AIP_MCP_YAHOO_CAPABILITIES_JSON")
    assert value(local_mcp_env, "AIP_MCP_YAHOO_ENDPOINT") == "http://yahoo-finance-mcp/mcp"
    assert value(azure_mcp_env, "AIP_MCP_AUTHENTICATION_TYPE") == "WORKLOAD_IDENTITY"
    assert value(azure_mcp_env, "AIP_MCP_EXTERNAL_PROVIDERS_ENABLED") == "false"
    assert value(azure_mcp_env, "AIP_MCP_YAHOO_ENABLED") == "false"
    assert "get_quote" in value(azure_mcp_env, "AIP_MCP_YAHOO_CAPABILITIES_JSON")
    assert "AIP_MCP_YAHOO_AUTH_TOKEN" not in azure_mcp_env
    yahoo_gateway_env = env_by_name(deployment(yahoo_mcp_docs, "mcp-gateway"))
    assert value(yahoo_gateway_env, "AIP_MCP_EXTERNAL_PROVIDERS_ENABLED") == "true"
    assert value(yahoo_gateway_env, "AIP_MCP_YAHOO_ENABLED") == "true"
    assert "get_quote" in value(yahoo_gateway_env, "AIP_MCP_YAHOO_CAPABILITIES_JSON")
    assert value(azure_mcp_env, "AIP_MCP_RESEARCH_BASE_URL") == "http://research-engine"
    assert value(azure_mcp_env, "AIP_FEATURE_MCP_ENABLED") == "true"
    assert value(azure_mcp_env, "OTEL_SDK_DISABLED") == "false"
    assert value(azure_mcp_env, "OTEL_EXPORTER_OTLP_ENDPOINT").startswith("https://")
    assert azure_mcp["spec"]["template"]["metadata"]["labels"]["azure.workload.identity/use"] == "true"
    mcp_volumes = azure_mcp["spec"]["template"]["spec"].get("volumes", [])
    assert any(volume.get("name") == "key-vault-secrets" for volume in mcp_volumes)
    mcp_mounts = azure_mcp["spec"]["template"]["spec"]["containers"][0].get("volumeMounts", [])
    assert any(mount.get("name") == "key-vault-secrets" and mount.get("readOnly") for mount in mcp_mounts)
    local_research_env = env_by_name(deployment(local_docs, "research-engine"))
    azure_research_env = env_by_name(deployment(azure_docs, "research-engine"))
    assert value(local_research_env, "AIP_RESEARCH_MCP_FIRST_ENABLED") == "true"
    assert value(azure_research_env, "AIP_RESEARCH_MCP_FIRST_ENABLED") == "false"
    assert value(azure_research_env, "AIP_RESEARCH_MCP_GATEWAY_BASE_URL") == "http://mcp-gateway"

    local_yahoo = deployment(local_docs, "yahoo-finance-mcp")
    azure_yahoo = deployment(yahoo_mcp_docs, "yahoo-finance-mcp")
    local_yahoo_services = [
        item for item in resources(local_docs, "Service")
        if item.get("metadata", {}).get("name") == "yahoo-finance-mcp"
    ]
    azure_yahoo_services = [
        item for item in resources(yahoo_mcp_docs, "Service")
        if item.get("metadata", {}).get("name") == "yahoo-finance-mcp"
    ]
    assert len(local_yahoo_services) == 1 and local_yahoo_services[0]["spec"]["type"] == "ClusterIP"
    assert len(azure_yahoo_services) == 1 and azure_yahoo_services[0]["spec"]["type"] == "ClusterIP"
    assert "yahoo-finance-mcp" not in ingress_names
    yahoo_env = env_by_name(azure_yahoo)
    assert value(yahoo_env, "AIP_YAHOO_MCP_TRANSPORT") == "streamable-http"
    assert value(yahoo_env, "OTEL_SDK_DISABLED") == "false"
    assert not any(
        marker in variable_name
        for variable_name in yahoo_env
        for marker in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "CLIENT_KEY")
    )
    yahoo_container = azure_yahoo["spec"]["template"]["spec"]["containers"][0]
    assert yahoo_container["securityContext"]["allowPrivilegeEscalation"] is False
    assert yahoo_container["securityContext"]["readOnlyRootFilesystem"] is True
    assert yahoo_container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert azure_yahoo["spec"]["template"]["spec"]["securityContext"]["runAsNonRoot"] is True
    yahoo_network_policies = {
        item["metadata"]["name"] for item in resources(yahoo_mcp_docs, "NetworkPolicy")
    }
    assert "yahoo-finance-mcp-internal-only" in yahoo_network_policies
    yahoo_hpas = {item["metadata"]["name"] for item in resources(yahoo_mcp_docs, "HorizontalPodAutoscaler")}
    yahoo_pdbs = {item["metadata"]["name"] for item in resources(yahoo_mcp_docs, "PodDisruptionBudget")}
    assert "yahoo-finance-mcp" in yahoo_hpas and "yahoo-finance-mcp" in yahoo_pdbs

    allowed_origins = azure_values["apiGateway"]["cors"]["allowedOrigins"]
    assert allowed_origins and "*" not in allowed_origins
    assert all("localhost" not in origin and "127.0.0.1" not in origin for origin in allowed_origins)

    auth_env = env_by_name(deployment(azure_docs, "auth-service"))
    assert "sslmode=verify-full" in value(auth_env, "SPRING_DATASOURCE_URL")
    assert value(auth_env, "DB_POOL_MAXIMUM_SIZE") == "6"
    portfolio_env = env_by_name(deployment(azure_docs, "portfolio-service"))
    assert value(portfolio_env, "SPRING_DATA_REDIS_SSL_ENABLED") == "true"
    kafka_env = env_by_name(deployment(kafka_docs, "api-gateway"))
    assert value(kafka_env, "AIP_KAFKA_SECURITY_PROTOCOL") == "SASL_SSL"
    assert value(kafka_env, "AIP_KAFKA_SASL_MECHANISM") == "PLAIN"
    assert value(kafka_env, "AIP_KAFKA_CONSUMER_GROUP_PREFIX") == "REPLACE_WITH_ENVIRONMENT_CONSUMER_GROUP_PREFIX"
    assert value(kafka_env, "AIP_KAFKA_CLIENT_DNS_LOOKUP") == "use_all_dns_ips"
    assert value(kafka_env, "AIP_KAFKA_REQUEST_TIMEOUT_MS") == "30000"
    assert value(kafka_env, "AIP_KAFKA_DELIVERY_TIMEOUT_MS") == "120000"
    assert value(kafka_env, "AIP_KAFKA_RETRIES") == "5"
    assert value(kafka_env, "AIP_KAFKA_SSL_TRUSTSTORE_LOCATION") == "/etc/ai-investment/kafka/KAFKA_TRUSTSTORE"
    kafka_gateway = deployment(kafka_docs, "api-gateway")
    kafka_volumes = kafka_gateway["spec"]["template"]["spec"].get("volumes", [])
    assert any(volume.get("secret", {}).get("secretName") == "aip-kafka-ca" for volume in kafka_volumes)

    local_gateway_env = env_by_name(deployment(local_docs, "api-gateway"))
    assert value(local_gateway_env, "OTEL_SDK_DISABLED") == "true"
    azure_gateway_env = env_by_name(deployment(azure_docs, "api-gateway"))
    assert value(azure_gateway_env, "GATEWAY_HTTP_CONNECT_TIMEOUT") == "3s"
    assert value(azure_gateway_env, "GATEWAY_HTTP_READ_TIMEOUT") == "30s"
    assert value(azure_gateway_env, "OTEL_SDK_DISABLED") == "false"
    assert value(azure_gateway_env, "OTEL_EXPORTER_OTLP_ENDPOINT").startswith("https://")

    azure_images = [
        container["image"]
        for item in resources(azure_docs, "Deployment")
        for container in item["spec"]["template"]["spec"]["containers"]
    ]
    local_images = [
        container["image"]
        for item in resources(local_docs, "Deployment")
        for container in item["spec"]["template"]["spec"]["containers"]
    ]
    assert azure_images and all(image.startswith("REPLACE_WITH_ACR_NAME.azurecr.io/") for image in azure_images)
    assert all(image.endswith(":REPLACE_WITH_GIT_SHA") for image in azure_images)
    application_local_images = [image for image in local_images if image.startswith("localhost:5001/")]
    assert len(application_local_images) >= 15
    assert azure_values["devDependencies"]["postgres"]["persistence"]["storageClass"] == ""
    assert "local-path" not in azure_text

    app_deployments = resources(azure_docs, "Deployment")
    assert len(app_deployments) >= 15
    for item in app_deployments:
        name = item["metadata"]["name"]
        pod_spec = item["spec"]["template"]["spec"]
        assert pod_spec.get("automountServiceAccountToken") is False, name
        assert pod_spec.get("securityContext", {}).get("runAsNonRoot") is True, name
        container = pod_spec["containers"][0]
        security = container.get("securityContext", {})
        assert security.get("allowPrivilegeEscalation") is False, name
        assert security.get("capabilities", {}).get("drop") == ["ALL"], name
        assert container.get("startupProbe"), name
        assert container.get("readinessProbe"), name
        assert container.get("livenessProbe"), name
        assert container.get("resources", {}).get("requests"), name
        assert container.get("resources", {}).get("limits"), name

    hpas = {item["metadata"]["name"] for item in resources(azure_docs, "HorizontalPodAutoscaler")}
    pdbs = {item["metadata"]["name"] for item in resources(azure_docs, "PodDisruptionBudget")}
    assert {"frontend", "api-gateway", "auth-service", "mcp-gateway"}.issubset(hpas)
    assert {"frontend", "api-gateway", "auth-service", "mcp-gateway"}.issubset(pdbs)
    assert resources(azure_docs, "NetworkPolicy")
    network_policy_names = {
        item["metadata"]["name"] for item in resources(azure_docs, "NetworkPolicy")
    }
    assert "mcp-gateway-internal-only" in network_policy_names

    for item in resources(azure_docs, "ConfigMap"):
        serialized = yaml.safe_dump(item).lower()
        assert not any(word in serialized for word in ("password", "client_secret", "api_key", "bearer "))

    sensitive_names = {
        "DB_PASSWORD", "SPRING_DATA_REDIS_PASSWORD", "AIP_KAFKA_PASSWORD",
        "AUTH_JWT_SECRET", "SMTP_USERNAME", "SMTP_PASSWORD", "AIP_EODHD_API_KEY",
        "AIP_INTERNAL_TOKEN", "ICICI_DIRECT_APP_KEY", "ICICI_DIRECT_SECRET_KEY",
    }
    for item in app_deployments:
        for entry in env_by_name(item).values():
            if entry["name"] in sensitive_names:
                assert "valueFrom" in entry and "value" not in entry, (item["metadata"]["name"], entry["name"])

    public_env_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (ROOT / "frontend").rglob("*")
        if path.is_file() and "node_modules" not in path.parts and ".next" not in path.parts
        and path.stat().st_size < 1_000_000
    )
    forbidden_public = ("PASSWORD", "SECRET", "PRIVATE_KEY", "DATABASE", "SMTP", "BROKER_TOKEN", "LLM_KEY")
    assert not any(f"NEXT_PUBLIC_{name}" in public_env_text for name in forbidden_public)

    dockerfiles = [
        path for path in ROOT.rglob("Dockerfile")
        if "node_modules" not in path.parts and ".next" not in path.parts
    ]
    assert len(dockerfiles) == 17
    assert ROOT / "ai" / "yahoo-finance-mcp" / "Dockerfile" in dockerfiles
    for dockerfile in dockerfiles:
        dockerfile_text = dockerfile.read_text(encoding="utf-8")
        assert re.search(r"^USER [0-9]+(?::[0-9]+)?$", dockerfile_text, re.MULTILINE), dockerfile

    azure_research = deployment(azure_docs, "research-engine")
    research_env = env_by_name(azure_research)
    assert value(research_env, "AIP_RESEARCH_DISTRIBUTED_LOCK_BACKEND") == "process"
    assert azure_research["spec"]["replicas"] == 1
    assert value(research_env, "AIP_YAHOO_SEARCH_URL").startswith("https://")
    assert value(research_env, "AIP_NSE_ANNOUNCEMENTS_URL").startswith("https://")
    assert value(azure_gateway_env, "AIP_FEATURE_SCHEDULED_JOBS_ENABLED") == "false"
    assert azure_values["auth"]["email"]["publicUrl"].startswith("https://")
    assert "callbackUrl" in azure_values["ibkr"]

    service_account = resources(azure_docs, "ServiceAccount")[0]
    annotations = service_account["metadata"].get("annotations", {})
    assert annotations.get("azure.workload.identity/client-id")
    spcs = resources(azure_docs, "SecretProviderClass")
    assert len(spcs) == 1
    spc_text = yaml.safe_dump(spcs[0]).lower()
    assert "clientsecret" not in spc_text and "password:" not in spc_text

    mcp_server_source = (ROOT / "ai" / "mcp-gateway" / "app" / "server.py").read_text(encoding="utf-8")
    assert '"externalProviderDependency": False' in mcp_server_source
    assert "place_order" not in mcp_server_source
    platform_script = (ROOT / "platform.ps1").read_text(encoding="utf-8")
    assert '"mcp-gateway"' in platform_script
    assert 'Name = "yahoo-finance-mcp"' in platform_script

    print(f"azure_readiness_static_assertions=54")
    print(f"local_manifest_documents={len(local_docs)}")
    print(f"azure_manifest_documents={len(azure_docs)}")
    print(f"azure_application_deployments={len(app_deployments)}")
    print("azure_readiness_validation=PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, subprocess.CalledProcessError) as error:
        print(f"azure_readiness_validation=FAIL: {error}", file=sys.stderr)
        raise SystemExit(1)
