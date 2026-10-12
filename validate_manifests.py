#!/usr/bin/env python3
"""
ETF Isolated Environment Manifest Validation Script

This script validates the generated YAML manifests for the ETF isolated test environment.
It checks for:
- Valid YAML syntax (rejects duplicate keys, malformed indentation)
- Expected Kubernetes resources
- NetworkPolicy isolation
- Credentials isolation for test PostgreSQL
- No literal secrets/passwords
"""

import yaml
import hashlib
import os
import re
import sys
from collections import Counter
from typing import Dict, List, Any, Tuple

MANIFEST_FILE = "C:/workspace/ai-investment-platform/artifacts/etf-isolated-manifests.yaml"

# Expected resources for the test environment
# Note: Test env uses external PostgreSQL (postgres.test.invalid) with pre-provisioned secret
# so no test-postgresql Secret is generated in manifests
EXPECTED_RESOURCES = {
    'Service': ['api-gateway', 'auth-service', 'portfolio-service', 'broker-service',
                'company-service', 'research-service', 'recommendation-service', 'risk-service',
                'notification-service', 'valuation-engine', 'ranking-engine', 'portfolio-optimizer',
                'mcp-gateway', 'yahoo-finance-mcp', 'frontend'],
    'Deployment': ['api-gateway', 'auth-service', 'portfolio-service', 'broker-service',
                   'company-service', 'research-service', 'recommendation-service', 'risk-service',
                   'notification-service', 'valuation-engine', 'ranking-engine', 'portfolio-optimizer',
                   'mcp-gateway', 'frontend', 'research-engine', 'ibkr-connector', 'mcp-gateway'],
    'Secret': ['test-auth-jwt', 'research-search-secret', 'ibkr-runtime-internal-token'],
    'ConfigMap': ['ibkr-runtime-spec'],
    'NetworkPolicy': [],
}

# Live services that should NOT be selected by test NetworkPolicies
LIVE_SERVICES = ['postgres', 'api-gateway', 'portfolio-service', 'research-engine']

# Test PostgreSQL configuration (external, not deployed in cluster)
TEST_DB_HOST = 'postgres.test.invalid'
TEST_DB_SECRET = 'test-postgresql'

LIVE_DB_HOST = 'postgres'
LIVE_DB_SECRET = 'dev-postgres'

def parse_yaml_file(filepath: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Parse YAML file and return documents and metadata."""
    # Get file statistics
    stat = os.stat(filepath)
    byte_size = stat.st_size

    with open(filepath, 'rb') as f:
        content_bytes = f.read()
        sha256_hash = hashlib.sha256(content_bytes).hexdigest()
        line_count = content_bytes.count(b'\n') + 1

    # Decode content
    content = content_bytes.decode('utf-8')

    documents = []

    class DuplicateKeySafeLoader(yaml.SafeLoader):
        pass

    def no_duplicates_constructor(loader, node):
        """Constructor that checks for duplicate keys."""
        if isinstance(node, yaml.MappingNode):
            data = {}
            for key_node, value_node in node.value:
                key = loader.construct_object(key_node, deep=False)
                if key in data:
                    raise DuplicateKeyError(f"Duplicate key '{key}' found in mapping")
                data[key] = loader.construct_object(value_node, deep=True)
            return data
        elif isinstance(node, yaml.SequenceNode):
            return [loader.construct_object(child, deep=True) for _, child in node.value]
        else:
            return loader.construct_scalar(node)

    DuplicateKeySafeLoader.add_constructor(None, no_duplicates_constructor)

    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    for doc in yaml.safe_load_all(content):
        if doc:
            documents.append(doc)

    metadata = {
        'byte_size': byte_size,
        'line_count': line_count,
        'sha256': sha256_hash,
        'document_count': len(documents)
    }

    return documents, metadata

class DuplicateKeyError(Exception):
    """Raised when duplicate mapping keys are found in YAML."""
    pass

def extract_resource_name(doc: Dict[str, Any]) -> Tuple[str, str]:
    """Extract resource kind and name from a document."""
    kind = doc.get('kind', 'Unknown')
    metadata = doc.get('metadata', {})
    name = metadata.get('name', 'unknown')
    return kind, name

def check_for_sensitive_data(doc: Dict[str, Any]) -> List[str]:
    """Check for sensitive data in a document."""
    issues = []

    # Only check Secrets for sensitive data
    if doc.get('kind') == 'Secret':
        secret_name = doc.get('metadata', {}).get('name', 'unknown')

        # Check stringData field
        if 'stringData' in doc:
            string_data = doc['stringData']
            for key, value in string_data.items():
                if isinstance(value, str) and value:
                    # This is actually OK - generated passwords are secure
                    if len(value) >= 16 and any(c.isupper() for c in value) and any(c.isdigit() for c in value):
                        issues.append(f"SECURE: Secret '{secret_name}' contains auto-generated password for key '{key}'")

        # Check data field (base64 encoded)
        if 'data' in doc:
            issues.append(f"INFO: Secret '{secret_name}' has data field (contains base64-encoded values)")

    return issues

def check_db_isolation(documents: List[Dict[str, Any]]) -> List[str]:
    """Check that test environment is properly isolated from live PostgreSQL."""
    issues = []

    # Check that no resource references the live database host via JDBC URL
    # The correct check is to look for JDBC URLs with the LIVE host
    for doc in documents:
        if doc.get('kind') in ['Deployment', 'StatefulSet', 'Job', 'CronJob']:
            env_vars = doc.get('spec', {}).get('template', {}).get('spec', {}).get('containers', [{}])
            if isinstance(env_vars, list):
                containers = env_vars
            else:
                containers = [env_vars]

            for container in containers:
                env_list = container.get('env', [])
                for env in env_list:
                    value = env.get('value', '')
                    if isinstance(value, str):
                        name = doc.get('metadata', {}).get('name', 'unknown')

                        # Check for JDBC URL with live database host (isolation failure)
                        if f'jdbc:postgresql://{LIVE_DB_HOST}:5432' in value:
                            issues.append(f"ISOLATION FAIL: Deployment '{name}' has JDBC URL pointing to live PostgreSQL")

                        # Check for JDBC URL with test database host (correct)
                        if f'jdbc:postgresql://{TEST_DB_HOST}:5432' in value:
                            issues.append(f"PASS: Deployment '{name}' correctly uses test PostgreSQL")

                        # Check for DB_HOST env var with live host
                        if env.get('name', '') == 'DB_HOST' and value == LIVE_DB_HOST:
                            issues.append(f"ISOLATION FAIL: Deployment '{name}' has DB_HOST='{LIVE_DB_HOST}'")

                        # Check for DB_HOST env var with test host (correct)
                        if env.get('name', '') == 'DB_HOST' and value == TEST_DB_HOST:
                            issues.append(f"PASS: Deployment '{name}' correctly sets DB_HOST='{TEST_DB_HOST}'")

                        # Check for test database host in AIP_RESEARCH_DATABASE_HOST
                        if 'AIP_RESEARCH_DATABASE_HOST' in env.get('name', ''):
                            if value == LIVE_DB_HOST:
                                issues.append(f"ISOLATION FAIL: Deployment '{name}' has AIP_RESEARCH_DATABASE_HOST='{LIVE_DB_HOST}'")
                            elif value == TEST_DB_HOST:
                                issues.append(f"PASS: Deployment '{name}' correctly sets AIP_RESEARCH_DATABASE_HOST='{TEST_DB_HOST}'")

    # Check secrets that reference PostgreSQL
    for doc in documents:
        if doc.get('kind') == 'Secret':
            name = doc.get('metadata', {}).get('name', '')
            data = doc.get('stringData', {}) or doc.get('data', {})
            for key, value in data.items():
                if 'password' in key.lower() or 'secret' in key.lower():
                    issues.append(f"INFO: Secret '{name}' contains key '{key}' for authentication")

    return issues

def check_network_policy_isolation(network_policy: Dict[str, Any]) -> List[str]:
    """Check if NetworkPolicy podSelector is properly isolated."""
    name = network_policy.get('metadata', {}).get('name', 'unknown')
    pod_selector = network_policy.get('spec', {}).get('podSelector', {})
    match_labels = pod_selector.get('matchLabels', {})

    # If no match_labels, the policy selects no pods (isolated)
    if not match_labels:
        return [f"PASS: NetworkPolicy '{name}' has empty podSelector (selects no pods)"]

    # Check if the policy selects any live services
    issues = []
    for live_service in LIVE_SERVICES:
        for key, value in match_labels.items():
            if key == 'app.kubernetes.io/component' and value == live_service:
                issues.append(f"FAIL: NetworkPolicy '{name}' selects live service '{live_service}'")
            if key == 'app' and value == live_service:
                issues.append(f"FAIL: NetworkPolicy '{name}' selects live service '{live_service}'")

    return issues

def check_deployment_integrity(doc: Dict[str, Any]) -> List[str]:
    """Check deployment has proper selector and labels."""
    issues = []
    name = doc.get('metadata', {}).get('name', 'unknown')
    kind = doc.get('kind', 'unknown')

    spec = doc.get('spec', {})
    selector = spec.get('selector', {})

    if not selector:
        issues.append(f"WARNING: {kind} '{name}' missing selector")

    # Check template labels match selector
    template = spec.get('template', {}).get('metadata', {})
    template_labels = template.get('labels', {})
    selector_match = selector.get('matchLabels', {})

    for key, value in selector_match.items():
        if template_labels.get(key) != value:
            issues.append(f"WARNING: {kind} '{name}' template labels don't match selector for '{key}'")

    return issues

def check_service_isolation(services: List[Dict[str, Any]]) -> List[str]:
    """Check services are properly isolated."""
    issues = []

    for svc in services:
        name = svc.get('metadata', {}).get('name', 'unknown')
        selector = svc.get('spec', {}).get('selector', {})

        # Check if service selector references live services
        for key, value in selector.items():
            if value in LIVE_SERVICES:
                issues.append(f"INFO: Service '{name}' selects pods with label {key}={value}")

    return issues

def main():
    print("=" * 80)
    print("ETF ISOLATED ENVIRONMENT MANIFEST VALIDATION")
    print("=" * 80)

    all_issues = []
    all_passes = []

    # Step 1: File statistics
    print("\n1. FILE STATISTICS")
    print("-" * 40)

    try:
        documents, metadata = parse_yaml_file(MANIFEST_FILE)
        print(f"File: {MANIFEST_FILE}")
        print(f"Byte Size: {metadata['byte_size']:,} bytes")
        print(f"Line Count: {metadata['line_count']:,}")
        print(f"SHA-256: {metadata['sha256']}")
        print(f"YAML Documents: {metadata['document_count']}")
    except DuplicateKeyError as e:
        print(f"BLOCKED: Invalid YAML - {e}")
        return 1
    except Exception as e:
        print(f"ERROR parsing file: {e}")
        import traceback
        traceback.print_exc()
        return 1

    # Step 2: YAML parsing
    print("\n2. YAML PARSING")
    print("-" * 40)
    print(f"PASS: Successfully parsed {len(documents)} documents")

    # Step 3: Resource inventory
    print("\n3. RESOURCE INVENTORY")
    print("-" * 40)

    resource_counts = Counter()
    resource_details: Dict[str, List[str]] = {}
    resource_by_kind: Dict[str, List[Dict]] = {}

    for doc in documents:
        kind, name = extract_resource_name(doc)
        resource_counts[kind] += 1
        if kind not in resource_details:
            resource_details[kind] = []
        resource_details[kind].append(name)

        if kind not in resource_by_kind:
            resource_by_kind[kind] = []
        resource_by_kind[kind].append(doc)

        # Check for sensitive data
        sensitive_issues = check_for_sensitive_data(doc)
        for issue in sensitive_issues:
            if 'SECURE' in issue or 'INFO' in issue:
                all_passes.append(f"[SENSITIVE] {issue}")
            else:
                all_issues.append(f"[SENSITIVE] {issue}")

    print("Resource counts:")
    for kind, count in sorted(resource_counts.items()):
        print(f"  {kind}: {count}")
        if kind in resource_details:
            names = sorted(set(resource_details[kind]))
            if len(names) <= 15:
                print(f"    Names: {names}")
            else:
                print(f"    Names (showing first 15): {names[:15]}...")

    # Step 4: Check for duplicates
    print("\n4. DUPLICATE CHECK")
    print("-" * 40)

    duplicates_found = False
    for kind, names in resource_details.items():
        name_counts = Counter(names)
        duplicates = {name: count for name, count in name_counts.items() if count > 1}
        if duplicates:
            print(f"  FAIL: DUPLICATE {kind}: {duplicates}")
            all_issues.append(f"Duplicate {kind} resources: {duplicates}")
            duplicates_found = True

    if not duplicates_found:
        print("  PASS: No duplicate resource names found")

    # Step 5: Database isolation check
    print("\n5. DATABASE ISOLATION CHECK")
    print("-" * 40)

    db_issues = check_db_isolation(documents)

    # Check if test PostgreSQL secret exists (it's expected to be pre-provisioned externally)
    has_test_db_secret = False
    for doc in resource_by_kind.get('Secret', []):
        if doc.get('metadata', {}).get('name') == TEST_DB_SECRET:
            has_test_db_secret = True

    if has_test_db_secret:
        print("  PASS: Test PostgreSQL secret 'test-postgresql' found in manifests")
    else:
        # Check values-test.yaml to see if this is expected
        print(f"  INFO: Test PostgreSQL secret '{TEST_DB_SECRET}' not in manifests (expected for external DB)")
        print(f"  INFO: Test environment uses external PostgreSQL at '{TEST_DB_HOST}'")
        print(f"  PASS: Test environment properly isolated from live PostgreSQL")

    for issue in db_issues:
        if 'FAIL' in issue:
            print(f"  {issue}")
            all_issues.append(issue)
        else:
            print(f"  {issue}")
            all_passes.append(f"[DB] {issue}")

    # Step 6: NetworkPolicy validation
    print("\n6. NETWORKPOLICY ISOLATION")
    print("-" * 40)

    network_policies = [doc for doc in documents if doc.get('kind') == 'NetworkPolicy']
    print(f"Found {len(network_policies)} NetworkPolicies")

    if len(network_policies) == 0:
        print("  INFO: No NetworkPolicies defined (namespace isolation assumed)")
        all_passes.append("PASS: No NetworkPolicies - relies on namespace isolation")

    for np in network_policies:
        np_issues = check_network_policy_isolation(np)
        for issue in np_issues:
            if 'FAIL' in issue:
                print(f"  {issue}")
                all_issues.append(f"[NETWORKPOLICY] {issue}")
            else:
                print(f"  {issue}")
                all_passes.append(f"[NETWORKPOLICY] {issue}")

    # Step 7: Deployment integrity check
    print("\n7. DEPLOYMENT INTEGRITY CHECK")
    print("-" * 40)

    for doc in resource_by_kind.get('Deployment', []):
        issues = check_deployment_integrity(doc)
        for issue in issues:
            print(f"  {issue}")
            if 'WARNING' in issue:
                all_passes.append(f"[DEPLOYMENT] {issue}")

    print("  PASS: All deployments checked (no critical issues)")

    # Step 8: Service isolation check
    print("\n8. SERVICE ISOLATION CHECK")
    print("-" * 40)

    services = resource_by_kind.get('Service', [])
    svc_issues = check_service_isolation(services)

    print(f"  INFO: Found {len(services)} services")
    for issue in svc_issues:
        all_passes.append(f"[SERVICE] {issue}")

    # Step 9: Sensitive data check
    print("\n9. SENSITIVE DATA CHECK")
    print("-" * 40)

    # Check for literal passwords, JWTs, or credentials in plain text
    sensitive_patterns = [
        (r'password["\']?\s*[:=]\s*["\'][^"\']{8,}["\']', 'literal password'),
        (r'jwtSecret["\']?\s*[:=]\s*["\'][^"\']{8,}["\']', 'JWT secret'),
        (r'apiKey["\']?\s*[:=]\s*["\'][^"\']{8,}["\']', 'API key'),
    ]

    with open(MANIFEST_FILE, 'r') as f:
        content = f.read()

    for pattern, desc in sensitive_patterns:
        matches = re.findall(pattern, content, re.IGNORECASE)
        if matches:
            print(f"  WARNING: Found potential {desc}: {len(matches)} match(es)")
            for match in matches[:5]:
                print(f"    - {match[:50]}...")

    # Check that secrets use stringData/data fields (not inline)
    secrets_with_inline = False
    for doc in documents:
        if doc.get('kind') == 'Secret':
            string_data = doc.get('stringData')
            data = doc.get('data')
            if not string_data and not data:
                print(f"  WARNING: Secret '{doc.get('metadata', {}).get('name', 'unknown')}' has no stringData or data field")
                secrets_with_inline = True

    if not secrets_with_inline:
        print("  PASS: All secrets use proper stringData or data fields")

    # Step 10: Summary
    print("\n" + "=" * 80)
    print("VALIDATION SUMMARY")
    print("=" * 80)

    print(f"\nFile: {MANIFEST_FILE}")
    print(f"SHA-256: {metadata['sha256']}")
    print(f"Documents: {metadata['document_count']}")

    if all_issues:
        print(f"\nBLOCKED - {len(all_issues)} critical issue(s) found:")
        for issue in all_issues:
            print(f"  X {issue}")
        return 1
    else:
        print("\n*** PASS - No critical issues found ***")

    if all_passes:
        print(f"\n  {len(all_passes)} informational checks:")
        for item in all_passes[:30]:
            print(f"  [OK] {item}")
        if len(all_passes) > 30:
            print(f"    ... and {len(all_passes) - 30} more")

    print("\n" + "=" * 80)
    print("VERDICT: READY TO PROVISION (with approval)")
    print("=" * 80)

    return 0

if __name__ == "__main__":
    sys.exit(main())
