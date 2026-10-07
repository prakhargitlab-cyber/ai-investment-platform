"""Provider-free configuration test for the DEV portfolio-service startup probe.

Guardian Review: this test pins the *exact* contract of the startup-probe
fix and guards against regression / over-broad edits:

  * portfolio-service startup failureThreshold == 60  (~5 min at 5s period)
  * periodSeconds / timeoutSeconds are NOT overridden on the service entry,
    so they still inherit javaServiceDefaults (periodSeconds: 5, timeoutSeconds: 3)
  * readiness + liveness failureThresholds are NOT touched on the service
    entry, so they still inherit javaServiceDefaults (failureThreshold: 3)
  * no OTHER javaService entry gained a `probes` override (the change is
    scoped to portfolio-service only)

This is a values.yaml assertion only -- no chart rendering, no providers,
no deploy, no opportunity cycle.
"""
from __future__ import annotations

import os

import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
_VALUES = os.path.join(os.path.dirname(_HERE), "values.yaml")


def _values() -> dict:
    with open(_VALUES, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _portfolio_entry() -> dict:
    services = _values()["javaServices"]
    matches = [s for s in services if s.get("name") == "portfolio-service"]
    assert matches, "portfolio-service entry missing from javaServices"
    return matches[0]


def test_portfolio_service_startup_failure_threshold_is_60():
    entry = _portfolio_entry()
    probes = entry.get("probes", {})
    startup = probes.get("startup", {})
    assert startup["failureThreshold"] == 60


def test_portfolio_service_startup_does_not_override_period_or_timeout():
    """periodSeconds=5 / timeoutSeconds=3 must still be inherited from
    javaServiceDefaults -- only failureThreshold was changed."""
    entry = _portfolio_entry()
    startup = entry.get("probes", {}).get("startup", {})
    # Not set on the service entry -> inherits the default (5s / 3s):
    assert "periodSeconds" not in startup
    assert "timeoutSeconds" not in startup
    # And the inherited defaults are what they were before:
    default_startup = _values()["javaServiceDefaults"]["probes"]["startup"]
    assert default_startup["periodSeconds"] == 5
    assert default_startup["timeoutSeconds"] == 3


def test_portfolio_service_readiness_and_liveness_thresholds_unchanged():
    """readiness/liveness failureThreshold must still be the inherited 3."""
    entry = _portfolio_entry()
    probes = entry.get("probes", {})
    # No readiness/liveness override on the service entry:
    assert "readiness" not in probes
    assert "liveness" not in probes
    defaults = _values()["javaServiceDefaults"]["probes"]
    assert defaults["readiness"]["failureThreshold"] == 3
    assert defaults["liveness"]["failureThreshold"] == 3


def test_no_other_java_service_gained_a_probe_override():
    """The override is scoped to portfolio-service only."""
    services = _values()["javaServices"]
    for s in services:
        if s["name"] == "portfolio-service":
            assert "probes" in s
            # only startup is overridden, and only failureThreshold:
            assert set(s["probes"].keys()) == {"startup"}
            assert set(s["probes"]["startup"].keys()) == {"failureThreshold"}
        else:
            assert "probes" not in s, (
                f"{s['name']} unexpectedly gained a probes override: {s.get('probes')}"
            )
