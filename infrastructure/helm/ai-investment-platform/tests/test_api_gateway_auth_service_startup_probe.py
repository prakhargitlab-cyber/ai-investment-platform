"""Provider-free configuration test for the DEV api-gateway / auth-service
startup-probe fix (2026-10-20).

Root cause (established via a read-only kubectl diagnostic, no login POST,
no restart, no deploy, no account change -- see
ops/availability-root-cause-20261020.ps1 and its captured output):

  * api-gateway: restartCount=13; repeated "Startup probe failed" /
    "Killing: ... failed startup probe/liveness probe" events.
  * auth-service: restartCount=12; prior container's
    lastState.terminated == {reason: Error, exitCode: 137}, lifespan
    ~183s -- within seconds of the inherited javaServiceDefaults startup
    failureThreshold: 36 (180s at periodSeconds: 5) window. auth-service's
    own logs (HikariPool-1 start, Flyway DB connection) confirm it was
    still mid-boot (DB pool + migrations), not crashed or hung.
  * Both: CPU/memory usage negligible relative to limits (3m/500m CPU,
    145-231Mi/768Mi memory) and node MemoryPressure/DiskPressure both
    False on every node -- ruling out OOMKilled and node resource
    pressure. Postgres itself was healthy and reachable -- ruling out a
    DB outage.

This is the same class of problem already fixed for portfolio-service
(see test_portfolio_service_startup_probe.py): the inherited
javaServiceDefaults startup-probe tolerance window is too tight for this
service's real Spring Boot (+ Hikari/Flyway) cold-start time on this dev
cluster. The fix mirrors the already-approved portfolio-service pattern
exactly: override ONLY probes.startup.failureThreshold, leaving
periodSeconds/timeoutSeconds and the readiness/liveness thresholds
inherited unchanged.

This is a values.yaml assertion only -- no chart rendering, no providers,
no deploy, no opportunity cycle.
"""
from __future__ import annotations

import os

import pytest
import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
_VALUES = os.path.join(os.path.dirname(_HERE), "values.yaml")

_FIXED_SERVICES = ("api-gateway", "auth-service")


def _values() -> dict:
    with open(_VALUES, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _entry(name: str) -> dict:
    services = _values()["javaServices"]
    matches = [s for s in services if s.get("name") == name]
    assert matches, f"{name} entry missing from javaServices"
    return matches[0]


@pytest.mark.parametrize("name", _FIXED_SERVICES)
def test_startup_failure_threshold_is_60(name):
    entry = _entry(name)
    startup = entry.get("probes", {}).get("startup", {})
    assert startup["failureThreshold"] == 60


@pytest.mark.parametrize("name", _FIXED_SERVICES)
def test_startup_does_not_override_period_or_timeout(name):
    """periodSeconds=5 / timeoutSeconds=3 must still be inherited from
    javaServiceDefaults -- only failureThreshold was changed."""
    entry = _entry(name)
    startup = entry.get("probes", {}).get("startup", {})
    assert "periodSeconds" not in startup
    assert "timeoutSeconds" not in startup
    default_startup = _values()["javaServiceDefaults"]["probes"]["startup"]
    assert default_startup["periodSeconds"] == 5
    assert default_startup["timeoutSeconds"] == 3


@pytest.mark.parametrize("name", _FIXED_SERVICES)
def test_readiness_and_liveness_thresholds_unchanged(name):
    """readiness/liveness failureThreshold must still be the inherited 3."""
    entry = _entry(name)
    probes = entry.get("probes", {})
    assert "readiness" not in probes
    assert "liveness" not in probes
    defaults = _values()["javaServiceDefaults"]["probes"]
    assert defaults["readiness"]["failureThreshold"] == 3
    assert defaults["liveness"]["failureThreshold"] == 3


@pytest.mark.parametrize("name", _FIXED_SERVICES)
def test_override_shape_is_minimal(name):
    """Only `probes.startup.failureThreshold` is set -- nothing broader."""
    entry = _entry(name)
    assert set(entry["probes"].keys()) == {"startup"}
    assert set(entry["probes"]["startup"].keys()) == {"failureThreshold"}
