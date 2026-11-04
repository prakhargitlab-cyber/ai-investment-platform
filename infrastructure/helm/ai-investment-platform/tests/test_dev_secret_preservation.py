"""Real Helm lookup against a loopback-only synthetic Kubernetes API.

No real kubeconfig, cluster, credential, database, or deployment is accessed.
Rendered manifests remain captured in memory; all credential values below are
invented unit-test inputs. The fake API rejects and records every mutation.
"""
import base64
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import threading
from urllib.parse import unquote, urlsplit

import pytest
import yaml


CHART = Path(__file__).resolve().parents[1]
NAMESPACE = "synthetic-dev-secret-test"
SECRET_KEYS = {"dev-postgres": "POSTGRES_PASSWORD", "dev-searxng": "SEARXNG_SECRET"}
BOOTSTRAP = {"dev-postgres": "synthetic-postgres-bootstrap-only",
             "dev-searxng": "synthetic-searxng-bootstrap-only"}
EXISTING = {"dev-postgres": "synthetic-existing-postgres-credential",
            "dev-searxng": "synthetic-existing-searxng-credential"}
RESEARCH_IMAGE = "localhost:5001/ai-investment/research-engine:etf-eq-rl-20261104-210859"
DISCOVERY = {
    "v1": {"secrets": "Secret", "configmaps": "ConfigMap", "services": "Service",
           "serviceaccounts": "ServiceAccount", "persistentvolumeclaims": "PersistentVolumeClaim"},
    "apps/v1": {"deployments": "Deployment", "statefulsets": "StatefulSet"},
    "networking.k8s.io/v1": {"ingresses": "Ingress"},
    "rbac.authorization.k8s.io/v1": {"roles": "Role", "rolebindings": "RoleBinding"},
}
DEPLOYMENT_NAMES = frozenset({
    "api-gateway", "auth-service", "broker-service", "company-service", "frontend",
    "ibkr-connector", "kafka", "mcp-gateway", "notification-service", "portfolio-optimizer",
    "portfolio-service", "ranking-engine", "recommendation-service", "redis",
    "research-engine", "research-service", "risk-service", "searxng", "valuation-engine",
    "yahoo-finance-mcp",
})
EXPECTED_RESOURCE_IDS = (
    {("apps/v1", "Deployment", name) for name in DEPLOYMENT_NAMES}
    | {("v1", "Service", name) for name in DEPLOYMENT_NAMES | {"postgres"}}
    | {("apps/v1", "StatefulSet", "postgres"),
       ("networking.k8s.io/v1", "Ingress", "api-gateway"),
       ("rbac.authorization.k8s.io/v1", "Role", "ibkr-runtime-orchestrator"),
       ("rbac.authorization.k8s.io/v1", "RoleBinding", "ibkr-runtime-orchestrator"),
       ("v1", "ConfigMap", "ibkr-runtime-spec"),
       ("v1", "ConfigMap", "postgres-fresh-provisioning"),
       ("v1", "ConfigMap", "searxng-settings"),
       ("v1", "PersistentVolumeClaim", "postgres-data-dev"),
       ("v1", "Secret", "aip-auth-jwt"),
       ("v1", "Secret", "dev-postgres"),
       ("v1", "Secret", "dev-searxng"),
       ("v1", "ServiceAccount", "ai-investment-platform"),
       ("v1", "ServiceAccount", "broker-service-runtime-orchestrator")}
)


def _encoded(value):
    return base64.b64encode(value.encode()).decode()


def _secret(name):
    return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": name, "namespace": NAMESPACE,
                         "labels": {"app.kubernetes.io/managed-by": "Helm"},
                         "annotations": {"meta.helm.sh/release-name": "synthetic-secret-review",
                                         "meta.helm.sh/release-namespace": NAMESPACE}},
            "data": {SECRET_KEYS[name]: _encoded(EXISTING[name])}}


class _SyntheticApi:
    def __init__(self):
        self.secrets = {name: _secret(name) for name in SECRET_KEYS}
        self.errors = {}
        self.requests = []

    def response(self, method, target):
        path = unquote(urlsplit(target).path)
        self.requests.append((method, path))
        if method != "GET":
            return 405, self.status(405, "MethodNotAllowed")
        if path == "/version":
            return 200, {"major": "1", "minor": "34", "gitVersion": "v1.34.0"}
        if path == "/api":
            return 200, {"kind": "APIVersions", "apiVersion": "v1", "versions": ["v1"],
                         "serverAddressByClientCIDRs": []}
        if path == "/apis":
            groups = []
            for version in DISCOVERY:
                if "/" not in version:
                    continue
                group, number = version.split("/")
                supported = {"groupVersion": version, "version": number}
                groups.append({"name": group, "versions": [supported], "preferredVersion": supported})
            return 200, {"kind": "APIGroupList", "apiVersion": "v1", "groups": groups}
        for version, kinds in DISCOVERY.items():
            endpoint = "/api/v1" if version == "v1" else "/apis/" + version
            if path == endpoint:
                return 200, {"kind": "APIResourceList", "apiVersion": "v1", "groupVersion": version,
                             "resources": [{"name": name, "singularName": kind.lower(),
                                            "namespaced": True, "kind": kind, "verbs": ["get"]}
                                           for name, kind in kinds.items()]}
        prefix = f"/api/v1/namespaces/{NAMESPACE}/secrets/"
        if path.startswith(prefix):
            name = path[len(prefix):]
            if name in self.errors:
                code = self.errors[name]
                return code, self.status(code, "Forbidden" if code == 403 else "ServiceUnavailable")
            if name in self.secrets:
                return 200, self.secrets[name]
        return 404, self.status(404, "NotFound")

    @staticmethod
    def status(code, reason):
        return {"kind": "Status", "apiVersion": "v1", "status": "Failure", "code": code,
                "reason": reason, "message": f"synthetic offline API {reason}"}

    @property
    def looked_up_names(self):
        return [path.rsplit("/", 1)[-1] for method, path in self.requests
                if method == "GET" and "/secrets/" in path]


@pytest.fixture
def api(tmp_path):
    state = _SyntheticApi()

    class Handler(BaseHTTPRequestHandler):
        def _respond(self):
            code, body = state.response(self.command, self.path)
            payload = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = _respond
        do_POST = _respond
        do_PUT = _respond
        do_PATCH = _respond
        do_DELETE = _respond

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    state.address = f"http://127.0.0.1:{server.server_port}"
    state.kubeconfig = tmp_path / "synthetic-kubeconfig.json"
    state.kubeconfig.write_text(json.dumps({
        "apiVersion": "v1", "kind": "Config", "current-context": "synthetic-only",
        "clusters": [{"name": "synthetic-only", "cluster": {"server": state.address}}],
        "users": [{"name": "synthetic-only", "user": {}}],
        "contexts": [{"name": "synthetic-only", "context": {
            "cluster": "synthetic-only", "user": "synthetic-only", "namespace": NAMESPACE}}],
    }))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive(), "Synthetic API did not shut down"
        assert all(method == "GET" for method, _ in state.requests), "Helm attempted an API mutation"


def _render(api, *, upgrade=False, explicit=False, overrides=None, chart=CHART):
    values = {
        "auth": {"devJwtSecret": "synthetic-jwt-explicit-test-only"},
        "aiServiceImageOverrides": {"research-engine": {"tag": "etf-eq-rl-20261104-210859"}},
        "devDependencies": {
            "postgres": {"password": BOOTSTRAP["dev-postgres"] if explicit else ""},
            "searxng": {"secretKey": BOOTSTRAP["dev-searxng"] if explicit else ""},
        },
    }
    for name, value in (overrides or {}).items():
        if name == "dev-postgres":
            values["devDependencies"]["postgres"]["password"] = value
        else:
            values["devDependencies"]["searxng"]["secretKey"] = value
    helm = shutil.which("helm")
    assert helm is not None, "Helm CLI is required for the real lookup regression"
    command = [helm, "template", "synthetic-secret-review", str(chart),
               "-f", str(chart / "values-dev.yaml"), "-f", "-",
               "--dry-run=server", "--disable-openapi-validation", "--timeout", "10s",
               "--namespace", NAMESPACE, "--kubeconfig", str(api.kubeconfig),
               "--kube-context", "synthetic-only", "--kube-apiserver", api.address]
    if upgrade:
        command.append("--is-upgrade")
    env = os.environ.copy()
    env.update(KUBECONFIG=str(api.kubeconfig), NO_PROXY="127.0.0.1,localhost",
               no_proxy="127.0.0.1,localhost", HELM_DRIVER="secret")
    return subprocess.run(command, input=json.dumps(values), text=True, capture_output=True,
                          timeout=30, env=env)


def _documents(result):
    assert result.returncode == 0, "Helm render failed; manifests intentionally withheld"
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def _rendered_secrets(result):
    return {item["metadata"]["name"]: item for item in _documents(result) if item["kind"] == "Secret"}


def _assert_failed_without_credentials(result, marker):
    assert result.returncode != 0
    assert marker in result.stderr
    assert not result.stdout.strip(), "Failed render exposed partial manifests"
    for value in list(EXISTING.values()) + list(BOOTSTRAP.values()):
        assert value not in result.stderr
        assert _encoded(value) not in result.stderr


@pytest.mark.parametrize("explicit", [False, True])
def test_initial_install_bootstraps_only_absent_secrets_via_real_lookup(api, explicit):
    api.secrets.clear()
    secrets = _rendered_secrets(_render(api, explicit=explicit))
    assert set(SECRET_KEYS) <= set(api.looked_up_names)
    for name, key in SECRET_KEYS.items():
        assert "stringData" not in secrets[name]
        encoded = secrets[name]["data"][key]
        value = base64.b64decode(encoded, validate=True).decode()
        if explicit:
            assert value == BOOTSTRAP[name]
        else:
            assert len(value) == (32 if name == "dev-postgres" else 48)
            assert value.isascii() and value.isalnum()


@pytest.mark.parametrize("upgrade", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_existing_secrets_are_reused_verbatim_on_install_and_upgrade(api, upgrade, explicit):
    secrets = _rendered_secrets(_render(api, upgrade=upgrade, explicit=explicit))
    assert set(SECRET_KEYS) <= set(api.looked_up_names)
    for name, key in SECRET_KEYS.items():
        assert secrets[name]["type"] == "Opaque"
        assert secrets[name]["data"][key] == api.secrets[name]["data"][key]
        assert "stringData" not in secrets[name]
    # Helm 4 additionally GETs rendered objects for native ownership checks;
    # those read-only GETs include the unrelated, explicitly configured JWT.
    assert set(api.looked_up_names) <= set(SECRET_KEYS) | {"aip-auth-jwt"}


@pytest.mark.parametrize("name", SECRET_KEYS)
@pytest.mark.parametrize("raw", [b"synthetic-line-one\nline-two\n", b"\x00\xff\x80synthetic-binary\x00"])
def test_existing_secret_bytes_are_preserved_without_decoding_or_trimming_output(api, name, raw):
    encoded = base64.b64encode(raw).decode()
    api.secrets[name]["data"][SECRET_KEYS[name]] = encoded
    secrets = _rendered_secrets(_render(api, upgrade=True, explicit=True))
    assert secrets[name]["data"][SECRET_KEYS[name]] == encoded


@pytest.mark.parametrize("name", SECRET_KEYS)
@pytest.mark.parametrize("explicit", [False, True])
def test_upgrade_missing_secret_fails_even_with_explicit_bootstrap_value(api, name, explicit):
    api.secrets.pop(name)
    result = _render(api, upgrade=True, explicit=explicit)
    _assert_failed_without_credentials(result, "DEV_CREDENTIAL_MISSING_ON_UPGRADE")
    assert name in api.looked_up_names


@pytest.mark.parametrize("name", SECRET_KEYS)
@pytest.mark.parametrize("upgrade", [False, True])
@pytest.mark.parametrize("malformed", [
    "missing_data", "null_data", "list_data", "missing_key", "empty_key", "nonstring_key",
    "bad_base64", "noncanonical_base64", "wrong_type", "missing_type", "wrong_name",
    "wrong_namespace", "wrong_kind", "wrong_version",
])
def test_malformed_existing_secret_fails_instead_of_using_explicit_value(api, name, upgrade, malformed):
    secret = deepcopy(api.secrets[name])
    key = SECRET_KEYS[name]
    if malformed == "missing_data":
        secret.pop("data")
    elif malformed == "null_data":
        secret["data"] = None
    elif malformed == "list_data":
        secret["data"] = ["synthetic-invalid-data-map"]
    elif malformed == "missing_key":
        secret["data"] = {"other-key": _encoded("synthetic-unrelated")}
    elif malformed == "empty_key":
        secret["data"][key] = ""
    elif malformed == "nonstring_key":
        secret["data"][key] = 23
    elif malformed == "bad_base64":
        secret["data"][key] = "invalid%%%base64"
    elif malformed == "noncanonical_base64":
        secret["data"][key] += "\n"
    elif malformed == "wrong_type":
        secret["type"] = "kubernetes.io/tls"
    elif malformed == "missing_type":
        secret.pop("type")
    elif malformed == "wrong_name":
        secret["metadata"]["name"] = "synthetic-other-secret"
    elif malformed == "wrong_namespace":
        secret["metadata"]["namespace"] = "synthetic-other-namespace"
    elif malformed == "wrong_kind":
        secret["kind"] = "ConfigMap"
    else:
        secret["apiVersion"] = "apps/v1"
    api.secrets[name] = secret
    result = _render(api, upgrade=upgrade, explicit=True)
    _assert_failed_without_credentials(result, "DEV_CREDENTIAL_MALFORMED_SECRET")
    assert name in api.looked_up_names


@pytest.mark.parametrize("name", SECRET_KEYS)
@pytest.mark.parametrize("invalid", [True, 23, ["synthetic-list"], {"synthetic": "map"}])
def test_absent_secret_rejects_malformed_explicit_install_value(api, name, invalid):
    api.secrets.pop(name)
    result = _render(api, overrides={name: invalid})
    _assert_failed_without_credentials(result, "DEV_CREDENTIAL_INVALID_VALUE")


@pytest.mark.parametrize("name", SECRET_KEYS)
@pytest.mark.parametrize("upgrade", [False, True])
@pytest.mark.parametrize("code", [403, 503])
def test_lookup_access_or_server_failure_cannot_generate_or_use_explicit_credentials(api, name, upgrade, code):
    api.errors[name] = code
    result = _render(api, upgrade=upgrade, explicit=True)
    reason = "Forbidden" if code == 403 else "ServiceUnavailable"
    _assert_failed_without_credentials(result, "synthetic offline API " + reason)
    assert name in api.looked_up_names


@pytest.mark.parametrize("upgrade", [False, True])
def test_secret_preservation_keeps_54_resource_identities_image_and_five_flags(api, upgrade):
    resources = _documents(_render(api, upgrade=upgrade, explicit=True))
    identities = [(item["apiVersion"], item["kind"], item["metadata"]["name"]) for item in resources]
    assert len(identities) == len(set(identities)) == 54
    assert set(identities) == EXPECTED_RESOURCE_IDS
    assert all(item["metadata"].get("namespace", NAMESPACE) == NAMESPACE for item in resources)
    research = next(item for item in resources
                    if item["kind"] == "Deployment" and item["metadata"]["name"] == "research-engine")
    pod = research["spec"]["template"]["spec"]
    container = next(item for item in pod["containers"] if item["name"] == "research-engine")
    assert container["image"] == RESEARCH_IMAGE
    assert pod["initContainers"]
    assert all(item["image"] == RESEARCH_IMAGE for item in pod["initContainers"])
    environment = {item["name"]: item.get("value") for item in container["env"]}
    for key, value in {
        "AIP_RESEARCH_OPPORTUNITY_RECOVERY_PAUSED": "true",
        "AIP_RESEARCH_OPPORTUNITY_SCHEDULER_ENABLED": "false",
        "AIP_RESEARCH_EQUITY_MANUAL_EXECUTION_ENABLED": "true",
        "AIP_RESEARCH_ETF_MANUAL_EXECUTION_ENABLED": "true",
        "AIP_RESEARCH_ETF_FULL_UNIVERSE_EXECUTION_ENABLED": "true",
    }.items():
        assert environment[key] == value


def test_secret_reuse_and_bootstrap_overrides_do_not_change_other_resources(api):
    installed = _documents(_render(api))
    upgraded = _documents(_render(api, upgrade=True, explicit=True))

    def unrelated(resources):
        return {(item["kind"], item["metadata"]["name"]): item for item in resources
                if not (item["kind"] == "Secret" and item["metadata"]["name"] in SECRET_KEYS)}

    assert unrelated(installed) == unrelated(upgraded)


def test_reviewed_change_matches_head_resources_after_secret_encoding_normalization(api, tmp_path):
    # Read only the committed chart into a temporary fixture. No real release
    # manifest, kubeconfig, Secret, backup, or cluster is consulted.
    repository = CHART.parents[2]
    relative_chart = CHART.relative_to(repository).as_posix()
    archived = subprocess.run(["git", "archive", "--format=tar", "HEAD", relative_chart],
                              cwd=repository, capture_output=True, timeout=15)
    assert archived.returncode == 0, "Unable to construct the committed chart fixture"
    destination = tmp_path / "committed-chart-fixture"
    with tarfile.open(fileobj=io.BytesIO(archived.stdout), mode="r:") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            path = PurePosixPath(member.name)
            assert not path.is_absolute() and ".." not in path.parts
            assert path.as_posix().startswith(relative_chart + "/")
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(member).read())
    baseline_chart = destination.joinpath(*PurePosixPath(relative_chart).parts)
    # The prior template receives the same synthetic bytes now held by the
    # fake API; the new template must preserve them despite replacement input.
    baseline = _documents(_render(api, chart=baseline_chart, overrides=EXISTING))
    updated = _documents(_render(api, upgrade=True, explicit=True))

    def normalized(resources):
        result = {}
        for item in deepcopy(resources):
            if item["kind"] == "Secret":
                data = item.setdefault("data", {})
                data.update({key: _encoded(value) for key, value in item.pop("stringData", {}).items()})
            result[(item["apiVersion"], item["kind"], item["metadata"]["name"])] = item
        return result

    assert normalized(baseline) == normalized(updated)
