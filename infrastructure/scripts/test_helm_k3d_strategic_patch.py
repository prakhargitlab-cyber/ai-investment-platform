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


def test_safe_diagnostic_kinds_do_not_echo_exception_message():
    import json
    import subprocess

    assert patch.safe_failure_kind(KeyError("POSTGRES_PASSWORD=secret")) == "KEY_ERROR"
    assert patch.safe_failure_kind(TypeError("secret-value")) == "TYPE_ERROR"
    assert patch.safe_failure_kind(
        json.JSONDecodeError("private data", "sensitive json", 1)
    ) == "JSON_DECODE_ERROR"
    assert patch.safe_failure_kind(
        subprocess.TimeoutExpired(["private", "cli"], 1)
    ) == "SUBPROCESS_TIMEOUT"


def test_diagnostic_phase_is_allowlisted():
    patch.phase("EXECUTE_CALCULATOR")
    assert patch.PHASE == "EXECUTE_CALCULATOR"
    import pytest
    with pytest.raises(RuntimeError, match="INVALID_DIAGNOSTIC_PHASE"):
        patch.phase("secret=never-print")


def test_safe_numeric_os_error_without_path_disclosure():
    err = OSError(2, "credential path not safe to disclose")
    assert patch.safe_windows_error_code(err) == "ERRNO_2"
    assert patch.safe_windows_error_code(ValueError("secret")) == "NOT_AVAILABLE"
