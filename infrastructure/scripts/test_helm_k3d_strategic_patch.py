"""Offline tests: only hardcoded error labels may be printed."""
import helm_k3d_strategic_patch as patch


def test_safe_validator_failure_code():
    assert patch.safe_failure_code(RuntimeError("INVALID_RESEARCH_AUDIT_LABELS")) == (
        "INVALID_RESEARCH_AUDIT_LABELS"
    )


def test_never_echo_secret_bearing_runtime_error():
    err = RuntimeError("SECRET_PASSWORD=plaintext-value")
    assert patch.safe_failure_code(err) == "UNCLASSIFIED_READ_ONLY_CHECK_FAILURE"


def test_never_echo_os_or_parser_errors():
    assert patch.safe_failure_code(OSError("private kubeconfig")) == (
        "UNCLASSIFIED_READ_ONLY_CHECK_FAILURE"
    )


def test_never_echo_runtime_error_with_extra_arguments():
    assert patch.safe_failure_code(
        RuntimeError("INVALID_RESEARCH_AUDIT_LABELS", "sensitive context")
    ) == "UNCLASSIFIED_READ_ONLY_CHECK_FAILURE"
