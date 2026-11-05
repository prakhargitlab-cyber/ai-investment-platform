"""Synthetic safety tests for no-value field-level reconciliation report."""
import helm_k3d_reconcile_fields as fields


def test_differences_never_include_values():
    before = {"template": {"spec": {"containers": [
        {"name": "auth-service", "image": "private-before-image",
         "env": [{"name": "API_TOKEN", "value": "private-old-value"}]}
    ]}}}
    after = {"template": {"spec": {"containers": [
        {"name": "auth-service", "image": "private-after-image",
         "env": [{"name": "API_TOKEN", "value": "private-new-value"}]}
    ]}}}
    paths = fields.differences(before, after)
    output = "\n".join(paths)
    assert "containers[auth-service].image" in output
    assert "env[API_TOKEN].value" in output
    assert "private-" not in output


def test_unsafe_names_are_redacted():
    assert fields.safe_name("PASSWORD=do-not-print") == "REDACTED_NAME"
    assert fields.safe_name("NORMAL_ENV") == "NORMAL_ENV"


def test_name_keyed_lists_do_not_confuse_reordering():
    a = {"env": [{"name": "A", "value": "one"}, {"name": "B", "value": "two"}]}
    b = {"env": [{"name": "B", "value": "two"}, {"name": "A", "value": "one"}]}
    assert fields.differences(a, b) == []
