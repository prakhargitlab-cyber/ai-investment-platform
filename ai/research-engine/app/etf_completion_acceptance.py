"""One-pass ETF completion acceptance using the *existing* readiness evaluator.

This is intentionally not an evidence provider or an alternative readiness
definition. It does not ingest, score, approve, mutate a registry, or save
a cycle; callers supply genuine canonical ETF rows and a read-only store.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from uuid import UUID

from app.etf_readiness import (
    ETC3_MANDATORY, ETC3_SUPPORTING, ETC3_CONTEXTUAL,
    EtfReadinessStatus, evaluate_etf_readiness,
)

MAX_AUDIT_ETFS = 500
MAX_EXAMPLE_BLOCKERS = 25


class _ReadThroughEtfCache:
    """Cache repeated per-instrument SELECTs within this one fixed-time audit.

    The ETF evaluator asks for the same price bars four times for derived
    metrics and the full listing universe once per instrument. This cache
    retains the original store's selection semantics and fetches each key
    only once. Nothing here issues writes or creates synthetic observations.
    """
    def __init__(self, store):
        self._store = store
        self._cache = {}

    def _get(self, key, fetch):
        if key not in self._cache:
            self._cache[key] = fetch()
        return self._cache[key]

    def etf_listings(self):
        return self._get(("listings",), self._store.etf_listings)

    def etf_attempts(self, instrument_id):
        return self._get(("attempts", instrument_id),
                         lambda: self._store.etf_attempts(instrument_id))

    def etf_facts(self, instrument_id, metric=None):
        return self._get(("facts", instrument_id, str(metric)),
                         lambda: self._store.etf_facts(instrument_id, metric))

    def etf_navs(self, instrument_id):
        return self._get(("navs", instrument_id),
                         lambda: self._store.etf_navs(instrument_id))

    def etf_holdings(self, instrument_id):
        return self._get(("holdings", instrument_id),
                         lambda: self._store.etf_holdings(instrument_id))

    def load_daily_market_bars(self, instrument_ids, **kwargs):
        key = ("bars", frozenset(instrument_ids), tuple(sorted(kwargs.items())))
        return self._get(key, lambda: self._store.load_daily_market_bars(instrument_ids, **kwargs))

    def __getattr__(self, name):
        return getattr(self._store, name)



def evaluate_etf_completion(canonical_rows, store, *, now: datetime | None = None) -> dict:
    """Produce a deterministic missing-evidence work queue, without acquisition.

    Uses exactly the application readiness evaluator; all mandatory statuses
    must be READY_FRESH. Supporting missing/NOT_IMPLEMENTED are exposed, never
    made up or converted into approval. A READ-ONLY persistence is required
    of callers (DB role/connection enforced separately).
    """
    now = now or datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("ETF_COMPLETION_CLOCK_INVALID")
    rows = list(canonical_rows)
    if not 1 <= len(rows) <= MAX_AUDIT_ETFS:
        raise ValueError("ETF_COMPLETION_SCOPE_INVALID")
    identities = set()
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("ETF_COMPLETION_CANONICAL_ROW_INVALID")
        if (row.get("assetType") != "ETF" or row.get("status") != "ACTIVE"
                or (row.get("primaryExchange") or row.get("exchange")) != "NSE"
                or not isinstance(row.get("isin"), str) or not row["isin"].strip()
                or not isinstance(row.get("primarySymbol"), str)
                or not row["primarySymbol"].strip()):
            raise ValueError("ETF_COMPLETION_CANONICAL_ROW_INVALID")
        try:
            key = UUID(str(row["globalInstrumentId"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("ETF_COMPLETION_CANONICAL_UUID_INVALID") from exc
        identity = (row["isin"], row["primarySymbol"])
        if key.int == 0 or key in identities or any(existing[2] == identity for existing in normalized):
            raise ValueError("ETF_COMPLETION_CANONICAL_AMBIGUOUS")
        identities.add(key)
        normalized.append((key, row["primarySymbol"], identity))

    store = _ReadThroughEtfCache(store)
    ready = 0
    mandatory_blockers = Counter()
    missing_supporting = Counter()
    supporting_statuses = Counter()
    blocked_examples = []
    total_supporting = 0
    for instrument_id, symbol, _ in sorted(normalized, key=lambda item: (item[1], str(item[0]))):
        statuses, summary = evaluate_etf_readiness(store, instrument_id, now=now)
        by_requirement = {s.requirement: s for s in statuses}
        if (len(by_requirement) != len(statuses)
                or set(by_requirement) != ETC3_MANDATORY | ETC3_SUPPORTING | ETC3_CONTEXTUAL):
            raise ValueError("ETF_COMPLETION_READINESS_CONTRACT_INCOMPLETE")
        mandatory = [
            s for s in statuses if s.role == "MANDATORY"
            and s.status != EtfReadinessStatus.READY_FRESH
        ]
        if bool(mandatory) == bool(summary.ready_for_analysis):
            raise ValueError("ETF_COMPLETION_READINESS_SUMMARY_CONFLICT")
        ready += int(summary.ready_for_analysis)
        for s in mandatory:
            mandatory_blockers[(s.requirement, str(s.status))] += 1
        if mandatory and len(blocked_examples) < MAX_EXAMPLE_BLOCKERS:
            blocked_examples.append({
                "ticker": symbol, "instrument_id": str(instrument_id),
                "blockers": [
                    {"requirement": s.requirement, "status": str(s.status), "reason": s.reason}
                    for s in sorted(mandatory, key=lambda s: s.requirement)
                ],
            })
        for s in statuses:
            if s.role == "SUPPORTING":
                total_supporting += 1
                supporting_statuses[(s.requirement, str(s.status))] += 1
                if s.status != EtfReadinessStatus.READY_FRESH:
                    missing_supporting[s.requirement] += 1

    return {
        "status": "READ_ONLY_ETF_COMPLETION_ACCEPTANCE",
        "as_of": now.isoformat(),
        "universe_count": len(rows),
        "ready_for_analysis": ready,
        "blocked_for_analysis": len(rows) - ready,
        "mandatory_blockers": [
            {"requirement": k[0], "status": k[1], "etfs": count}
            for k, count in sorted(mandatory_blockers.items())
        ],
        "supporting_not_fresh": dict(sorted(missing_supporting.items())),
        "supporting_statuses": [
            {"requirement": k[0], "status": k[1], "etfs": count}
            for k, count in sorted(supporting_statuses.items())
        ],
        "supporting_requirements_evaluated": total_supporting,
        "first_25_blocked": blocked_examples,
        "all_canonical_identities_scoped": True,
        "source_approval_granted": False,
        "ter_ingestion_authorized": False,
        "production_evidence_written": False,
        "radar_cycle_executed": False,
    }
