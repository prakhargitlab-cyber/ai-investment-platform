#!/usr/bin/env python3
"""Read-only actual Helm candidate / Kubernetes strategic three-way patch audit.

Requires PyYAML, Docker, a locally compiled trusted Linux Go patch calculator,
and the pinned k3d server. No Kubernetes writes. Never prints manifest bytes.
The output is diagnostic: it does not authorize helm upgrade.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import yaml

RELEASE = "ai-investment-platform"
NAMESPACE = "ai-investment"
SERVER = "k3d-ai-investment-dev-server-0"
HELM_IMAGE = "alpine/helm:4.2.3"
K3S_CONFIG = "/etc/rancher/k3s/k3s.yaml"
KINDS = ("deployments,statefulsets,services,serviceaccounts,"
         "persistentvolumeclaims,secrets,configmaps,roles,rolebindings,ingresses")
ALLOWED = {"Deployment/api-gateway", "Deployment/auth-service"}
PHASE = "START"


def phase(label):
    global PHASE
    PHASE = label
    print("AUDIT_STAGE:", label, flush=True)


class Blocked(Exception):
    pass


def call(args, *, data=None, timeout=150):
    try:
        p = subprocess.run(args, input=data, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Blocked("COMMAND_FAILED_OR_TIMED_OUT") from exc
    if p.returncode:
        raise Blocked("READ_ONLY_COMMAND_RETURNED_FAILURE")
    return p.stdout


def resource_id(item):
    return (item["apiVersion"], item["kind"], item["metadata"]["name"])


def parse_docs(raw):
    docs = [d for d in yaml.safe_load_all(raw) if isinstance(d, dict)]
    result = {resource_id(d): d for d in docs}
    if len(result) != len(docs):
        raise Blocked("DUPLICATE_RESOURCE_ID")
    return result


def kube(*arguments):
    return call(["docker", "exec", SERVER, "/bin/kubectl",
                 "--kubeconfig=" + K3S_CONFIG, "-n", NAMESPACE, *arguments])


def helm(base, *arguments):
    return call(base + list(arguments))



def helm_dry_run(base, *arguments):
    """Classify failed Helm invocations without disclosing raw stderr."""
    try:
        result = subprocess.run(base + list(arguments),
                                capture_output=True, timeout=150, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Blocked("HELM_DRY_RUN_COMMAND_UNAVAILABLE_OR_TIMEOUT") from exc
    if result.returncode:
        # Only emit predefined categories. Never dump stderr: it could include
        # rendered Secret manifests, credentials, or request details.
        message = (result.stderr + b"\\n" + result.stdout).lower()
        categories = (
            (b"unknown flag", "UNSUPPORTED_HELM_FLAG"),
            (b"unknown command", "UNSUPPORTED_HELM_COMMAND"),
            (b"requires", "HELM_REQUIRED_ARGUMENT_OR_CONFIG_MISSING"),
            (b"values don't meet", "CHART_VALUES_SCHEMA_REJECTED"),
            (b"schema(s)", "CHART_VALUES_SCHEMA_REJECTED"),
            (b"cannot load values", "CHART_VALUES_LOADING_FAILED"),
            (b"error converting yaml to json", "CHART_YAML_PARSE_FAILED"),
            (b"failed parsing --set", "HELM_SET_PARSING_FAILED"),
            (b"failed parsing --set-json", "HELM_SET_JSON_PARSING_FAILED"),
            (b"error parsing --set", "HELM_SET_PARSING_FAILED"),
            (b"failed to parse", "HELM_INPUT_PARSING_FAILED"),
            (b"failed to load", "CHART_LOAD_FAILED"),
            (b"no chart", "HELM_CHART_NOT_MOUNTED"),
            (b"does not exist", "HELM_REFERENCED_PATH_OR_RESOURCE_MISSING"),
            (b"is not a directory", "HELM_CHART_MOUNT_INVALID"),
            (b"cannot read", "HELM_FILE_NOT_READABLE"),
            (b"permission denied", "HELM_FILE_OR_API_PERMISSION_DENIED"),
            (b"error calling include", "CHART_HELPER_TEMPLATE_FAILED"),
            (b"coalesce.go", "HELM_VALUES_MERGE_WARNING_OR_ERROR"),
            (b"nil pointer", "CHART_NIL_POINTER"),
            (b"cannot overwrite table", "HELM_VALUES_TYPE_CONFLICT"),
            (b"unknown shorthand flag", "UNSUPPORTED_HELM_FLAG"),
            (b"yaml parse error", "CHART_YAML_PARSE_FAILED"),
            (b"parse error at", "CHART_TEMPLATE_PARSE_FAILED"),
            (b"nil pointer evaluating", "CHART_TEMPLATE_VALUE_MISSING"),
            (b"can't evaluate field", "CHART_TEMPLATE_VALUE_MISSING"),
            (b"template:", "CHART_TEMPLATE_RENDER_FAILED"),
            (b"failed to download", "CHART_DEPENDENCY_MISSING"),
            (b"forbidden", "KUBERNETES_PERMISSION_DENIED"),
            (b"admission webhook", "KUBERNETES_ADMISSION_BLOCKED"),
            (b"context deadline exceeded", "KUBERNETES_API_TIMEOUT"),
            (b"connection refused", "KUBERNETES_API_UNREACHABLE"),
            (b"no route to host", "KUBERNETES_API_UNREACHABLE"),
            (b"timed out", "KUBERNETES_API_TIMEOUT"),
            (b"cannot re-use a name", "HELM_RELEASE_STATE_CONFLICT"),
            (b"has no deployed releases", "HELM_RELEASE_NOT_DEPLOYED"),
            (b"not found", "HELM_REFERENCED_RESOURCE_NOT_FOUND"),
            (b"invalid value", "HELM_VALUE_INVALID"),
            (b"invalid type", "HELM_VALUE_TYPE_INVALID"),
            (b"invalid character", "HELM_INPUT_JSON_INVALID"),
            (b"failed to create", "DRY_RUN_RESOURCE_VALIDATION_FAILED"),
            (b"cannot patch", "HELM_PATCH_REJECTED"),
            (b"cannot update", "HELM_RESOURCE_UPDATE_REJECTED"),
            (b"cannot be imported", "HELM_RESOURCE_OWNERSHIP_CONFLICT"),
            (b"invalid ownership metadata", "HELM_RESOURCE_OWNERSHIP_CONFLICT"),
            (b"another operation", "HELM_RELEASE_OPERATION_IN_PROGRESS"),
            (b"cannot reuse", "HELM_RELEASE_NAME_CONFLICT"),
            (b"cannot re-use", "HELM_RELEASE_NAME_CONFLICT"),
            (b"resource mapping not found", "HELM_RESOURCE_API_MAPPING_MISSING"),
            (b"no matches for kind", "HELM_RESOURCE_API_MAPPING_MISSING"),
            (b"unable to recognize", "HELM_RESOURCE_API_MAPPING_MISSING"),
            (b"cannot unmarshal", "HELM_VALUES_OR_MANIFEST_UNMARSHAL_FAILED"),
            (b"failed to unmarshal", "HELM_VALUES_OR_MANIFEST_UNMARSHAL_FAILED"),
            (b"failed to replace object", "HELM_PATCH_REJECTED"),
            (b"failed to get", "HELM_GET_RESOURCE_FAILED"),
            (b"unable to continue with update", "HELM_UPGRADE_OWNERSHIP_OR_CONFLICT"),
            (b"pre-upgrade hooks failed", "HELM_PRE_UPGRADE_HOOK_FAILED"),
            (b"post-upgrade hooks failed", "HELM_POST_UPGRADE_HOOK_FAILED"),
            (b"lookup failed", "HELM_LOOKUP_FAILED"),
            (b"unable to lookup", "HELM_LOOKUP_FAILED"),
            (b"not a valid chart", "HELM_INVALID_CHART"),
            (b"chart metadata", "HELM_CHART_METADATA_INVALID"),
            (b"invalid manifest", "HELM_MANIFEST_INVALID"),
            (b"release content", "HELM_RELEASE_CONTENT_INVALID"),
            (b"cannot restore", "HELM_RELEASE_RESTORE_FAILED"),
            (b"has been deleted", "HELM_RELEASE_REMOVED"),
            (b"malformed", "HELM_INPUT_MALFORMED"),
            (b"error validating", "DRY_RUN_RESOURCE_VALIDATION_FAILED"),
            (b"unable to build kubernetes objects", "DRY_RUN_RESOURCE_VALIDATION_FAILED"),
        )
        for pattern, code in categories:
            if pattern in message:
                raise Blocked(code)
        # Inspect only fixed YAML error signatures and safe numeric locations.
        # Never echo Helm's free-form text, values, or chart content.
        import re
        yaml_signatures = (
            (b"yaml: unmarshal errors", "YAML_TYPE_UNMARSHAL"),
            (b"cannot unmarshal", "YAML_TYPE_UNMARSHAL"),
            (b"mapping values are not allowed", "YAML_MAPPING_SYNTAX"),
            (b"did not find expected key", "YAML_EXPECTED_KEY_MISSING"),
            (b"did not find expected '-' indicator", "YAML_LIST_STRUCTURE"),
            (b"did not find expected", "YAML_STRUCTURE_ERROR"),
            (b"found character that cannot start any token", "YAML_INVALID_TOKEN"),
            (b"could not find expected", "YAML_STRUCTURE_ERROR"),
            (b"yaml: line", "YAML_LINE_ERROR"),
            (b"yaml error", "YAML_GENERAL_ERROR"),
            (b"error parsing yaml", "YAML_PARSE_FAILURE"),
            (b"error converting yaml", "YAML_CONVERSION_FAILURE"),
        )
        for signature, code in yaml_signatures:
            if signature in message:
                print("HELM_YAML_FAILURE_CLASS:", code, flush=True)
                break
        lines = re.findall(rb"\\bline[ :]+(\\d{1,6})\\b", message)
        if lines:
            print("HELM_YAML_ERROR_LINES:",
                  ",".join(str(int(n)) for n in lines[:4]), flush=True)
        # Extract only a safe Helm chart template basename (not an arbitrary
        # filepath, username, or YAML snippet).
        template_names = re.findall(
            rb"templates/([a-z][a-z0-9_-]{0,60}\\.ya?ml)", message)
        if template_names:
            print("HELM_ERROR_TEMPLATE_NAMES:",
                  ",".join(n.decode("ascii") for n in dict.fromkeys(template_names)),
                  flush=True)
        # If Helm's phrasing is unknown, return only approved vocabulary
        # present in the diagnostic, never the raw error or identifiers.
        import re
        whitelist = {
            "upgrade", "failed", "error", "resource", "release", "client",
            "server", "patch", "create", "update", "apply", "values", "chart",
            "template", "invalid", "yaml", "json", "type", "unmarshal",
            "conflict", "existing", "exists", "owner", "ownership", "metadata",
            "missing", "required", "secret", "configmap", "deployment",
            "statefulset", "persistentvolumeclaim", "service", "forbidden",
            "unauthorized", "permission", "denied", "version", "api", "kind",
            "not", "found", "cannot", "unable", "nil", "map", "string", "int",
            "object", "expect", "expected", "already", "in", "progress",
            "admission", "webhook", "validation", "schema", "parse",
            "reconcile", "timeout", "connection", "refused", "operation",
        }
        words = [w.decode("ascii") for w in re.findall(rb"[a-z]{2,28}", message)
                 if w.decode("ascii") in whitelist]
        # Repeated words and unrecognized tokens add no useful evidence.
        unique = list(dict.fromkeys(words))[:20]
        print("HELM_FAILURE_SAFE_KEYWORDS:", ",".join(unique) or "none",
              flush=True)
        # Both probes are read-only; never display or persist their output.
        client_args = [a.replace("--dry-run=server", "--dry-run=client")
                       for a in arguments]
        try:
            client = subprocess.run(base + client_args, capture_output=True,
                                    timeout=150, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Blocked("CLIENT_DRY_RUN_DIAGNOSTIC_UNAVAILABLE") from exc
        if client.returncode == 0:
            raise Blocked("SERVER_DRY_RUN_FAILED_BUT_CLIENT_SUCCEEDED")

        # Inspect chart rendering separately, without kubeconfig privileges.
        # Helm flags must appear after the chart/release arguments.
        lint_base = list(base)
        kube_flag = lint_base.index("--kubeconfig")
        del lint_base[kube_flag:kube_flag + 2]
        try:
            lint = subprocess.run(lint_base + ["lint", "/chart", "--quiet"],
                                  capture_output=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Blocked("HELM_LINT_DIAGNOSTIC_UNAVAILABLE") from exc
        if lint.returncode != 0:
            raise Blocked("HELM_CHART_LINT_FAILED_ALSO")

        opts = list(arguments)
        try:
            idx = opts.index("--set-string")
            settings = opts[idx:]
            check = subprocess.run(lint_base + [
                "template", "aip-diagnostic", "/chart", "--namespace",
                NAMESPACE, *settings], capture_output=True, timeout=120,
                check=False)
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            raise Blocked("HELM_TEMPLATE_DIAGNOSTIC_UNAVAILABLE") from exc
        if check.returncode != 0:
            raise Blocked("HELM_TEMPLATE_WITH_NEW_OVERRIDES_FAILED")
        # Compare the existing release configuration against a clean defaults
        # dry run. Both probes are read-only and their output is suppressed.
        original_args = list(arguments)
        option_start = original_args.index("--set-string")
        baseline_args = original_args[:option_start]
        baseline_args = [("--dry-run=client" if arg == "--dry-run=server"
                          else arg) for arg in baseline_args]
        reset_args = [("--reset-values" if arg == "--reuse-values"
                       else "--dry-run=client" if arg == "--dry-run=server"
                       else arg) for arg in original_args]
        try:
            baseline = subprocess.run(base + baseline_args, capture_output=True,
                                      timeout=150, check=False)
            reset = subprocess.run(base + reset_args, capture_output=True,
                                   timeout=150, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Blocked("HELM_RELEASE_VALUES_ISOLATION_UNAVAILABLE") from exc
        print("BASELINE_REUSE_VALUES_DRY_RUN:",
              "PASS" if baseline.returncode == 0 else "FAIL", flush=True)
        print("NEW_OVERRIDES_RESET_VALUES_DRY_RUN:",
              "PASS" if reset.returncode == 0 else "FAIL", flush=True)
        import re
        # Report only a numeric YAML line if Helm emitted one.
        match = re.search(rb"\\byaml\\s*:\\s*line\\s+(\\d{1,6})\\s*:",
                          message)
        if match:
            print("HELM_YAML_LINE_NUMBER:", int(match.group(1)), flush=True)
        raise Blocked("HELM_TEMPLATE_OK_BUT_UPGRADE_DRY_RUN_FAILED")
    return result.stdout


def calculator(executable, resources):
    # The Go implementation is trusted local code, isolated from the network,
    # filesystems and host credentials. Sensitive docs only enter via stdin.
    payload = json.dumps({"resources": resources}, separators=(",", ":")).encode()
    command = ["docker", "run", "--rm", "-i", "--network", "none",
               "--read-only", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges", "--user", "65534:65534",
               "--mount", "type=bind,source=" + str(executable.resolve())
               + ",target=/usr/local/bin/helm-patch-impact,readonly",
               "--entrypoint", "/usr/local/bin/helm-patch-impact", "golang:1.23-alpine"]
    try:
        p = subprocess.run(command, input=payload, capture_output=True, timeout=150)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Blocked("STRATEGIC_PATCH_CALCULATOR_UNAVAILABLE") from exc
    if p.returncode not in (0, 3):
        raise Blocked("STRATEGIC_PATCH_CALCULATION_FAILED")
    try:
        result = json.loads(p.stdout)
    except ValueError as exc:
        raise Blocked("STRATEGIC_PATCH_REPORT_NOT_JSON") from exc
    if result.get("version") != "strategic-three-way-v1":
        raise Blocked("STRATEGIC_PATCH_REPORT_VERSION_UNEXPECTED")
    if result.get("blocked") is not (p.returncode == 3):
        raise Blocked("STRATEGIC_PATCH_EXIT_MISMATCH")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chart", type=Path, required=True)
    parser.add_argument("--calculator", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    arguments = parser.parse_args()
    phase("READ_RELEASE_SPEC")
    spec = json.loads(arguments.spec.read_text(encoding="utf-8"))
    if (spec.get("revision") != 1 or spec.get("release") != RELEASE
            or spec.get("namespace") != NAMESPACE
            or set(spec.get("images", {})) != {"api-gateway", "auth-service"}):
        raise Blocked("RELEASE_SPEC_SCOPE_INVALID")
    if not arguments.chart.is_dir() or not arguments.calculator.is_file():
        raise Blocked("HELM_CHART_OR_CALCULATOR_MISSING")
    phase("READ_K3D_KUBECONFIG")
    # Source chart is mounted read-only. No base64 contents printed or written.
    config = call(["docker", "exec", SERVER, "/bin/cat", K3S_CONFIG])
    if b"server: https://127.0.0.1:6443" not in config:
        raise Blocked("K3D_KUBECONFIG_ENDPOINT_UNEXPECTED")
    with tempfile.TemporaryDirectory(prefix="aip-impact-") as directory:
        kubeconfig = Path(directory) / "config"
        kubeconfig.write_bytes(config)
        base = ["docker", "run", "--rm", "--network", "container:" + SERVER,
                "--mount", "type=bind,source=" + str(kubeconfig)
                + ",target=/tmp/kubeconfig,readonly",
                "--mount", "type=bind,source=" + str(arguments.chart.resolve())
                + ",target=/chart,readonly",
                "--entrypoint", "helm", HELM_IMAGE, "--kubeconfig", "/tmp/kubeconfig"]
        phase("READ_HELM_REVISION_ONE_MANIFEST")
        old = parse_docs(helm(base, "get", "manifest", RELEASE, "--namespace",
                              NAMESPACE, "--revision", "1"))
        tag = spec["images"]["auth-service"]["image"].rsplit(":", 1)[-1]
        if tag != spec["images"]["api-gateway"]["image"].rsplit(":", 1)[-1]:
            raise Blocked("SERVICE_TAG_MISMATCH")
        origins = json.dumps(spec["origins"])
        phase("HELM_SERVER_DRY_RUN")
        preview = helm_dry_run(base, "upgrade", RELEASE, "/chart", "--namespace",
                       NAMESPACE, "--reuse-values", "--dry-run=server",
                       "--set-string", "workloadOverrides.auth-service.imageTagOverride=" + tag,
                       "--set-string", "workloadOverrides.api-gateway.imageTagOverride=" + tag,
                       "--set-json", "apiGateway.cors.allowedOrigins=" + origins)
    phase("PARSE_HELM_DRY_RUN")
    marker = b"\nMANIFEST:\n"
    if marker not in preview:
        raise Blocked("HELM_DRY_RUN_MANIFEST_NOT_FOUND")
    proposed = parse_docs(preview.split(marker, 1)[1].split(b"\nNOTES:\n", 1)[0])
    phase("READ_LIVE_RESOURCES")
    live_items = json.loads(kube("get", KINDS, "-o", "json"))["items"]
    live = {resource_id(d): d for d in live_items}
    if len(old) != 54 or set(old) != set(proposed) or set(old) - set(live):
        raise Blocked("HELM_RESOURCE_IDENTITY_MISMATCH")
    resources = [{"old": old[k], "new": proposed[k], "live": live[k]}
                 for k in sorted(old)]
    phase("STRATEGIC_PATCH_CALCULATION")
    result = calculator(arguments.calculator, resources)
    phase("VALIDATE_PATCH_REPORT")
    if result.get("resources") != 54:
        raise Blocked("PATCH_RESOURCE_COUNT_INVALID")
    risk = result.get("risk_names")
    categories = result.get("patch_categories")
    if not isinstance(risk, list) or not isinstance(categories, dict):
        raise Blocked("PATCH_REPORT_SCHEMA_INVALID")
    known = {k[1] + "/" + k[2] for k in old}
    if any(name not in known for name in risk):
        raise Blocked("UNEXPECTED_PATCH_RESOURCE_NAME")
    # Do not whitelist any patch based on a broad category label alone.
    # Report nonempty patch resources including verified noop candidates.
    changed = sorted(categories)
    if not set(changed).issubset(known):
        raise Blocked("PATCH_CATEGORY_RESOURCE_NAME_INVALID")
    print("MANAGED_HELM_RESOURCES:", len(old))
    print("ACTUAL_STRATEGIC_THREE_WAY_NONEMPTY_PATCHES:", result.get("nonempty"))
    print("UNRESOLVED_PATCH_RESOURCES:", len(risk))
    for name in changed:
        print("PATCH_RESOURCE:", name)
    unexpected = sorted(set(changed) - ALLOWED)
    print("OUTSIDE_TWO_SERVICE_SCOPE:", len(unexpected))
    for name in unexpected:
        print("UNRELATED_RESOURCE_PATCH:", name)
    print("NO_PATCH_BYTES_OR_SECRET_VALUES_PRINTED")
    print("NO_KUBERNETES_WRITES_PERFORMED")
    print("NO_DEPLOYMENT_AUTHORIZATION")
    # Even a two-service-only patch requires exact container field-level
    # verification and operator approval; this diagnostic never greenlights.
    return 3 if changed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Never print exception strings: Helm or YAML errors may contain Secrets.
        print("HELM_IMPACT_AUDIT_BLOCKED_AT:", PHASE, file=sys.stderr)
        if isinstance(sys.exc_info()[1], Blocked):
            print("HELM_AUDIT_REASON:", str(sys.exc_info()[1]), file=sys.stderr)
        print("NO_KUBERNETES_WRITES_PERFORMED", file=sys.stderr)
        sys.exit(2)
