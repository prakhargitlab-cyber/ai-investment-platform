import os

import yaml


_HERE = os.path.dirname(os.path.abspath(__file__))
_VALUES = os.path.join(os.path.dirname(_HERE), "values.yaml")


def _research_entry():
    with open(_VALUES, "r", encoding="utf-8") as fh:
        values = yaml.safe_load(fh)
    return next(s for s in values["aiServices"] if s["name"] == "research-engine")


def test_research_engine_uses_dedicated_liveness_and_existing_health_for_readiness_startup():
    probes = _research_entry()["probes"]
    assert probes["liveness"]["path"] == "/health/live"
    assert probes["readiness"]["path"] == "/health"
    assert probes["startup"]["path"] == "/health"
