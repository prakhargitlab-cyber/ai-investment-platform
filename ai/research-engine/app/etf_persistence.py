"""ETF-only append-only evidence store, shared SQLite/PostgreSQL SQL dialect.

PostgreSQL obtains these tables exclusively from research-service Flyway V20.
JSON payloads retain optional provenance without coercing unknowns to zero.
"""
from app.etf_evidence import (
    EtfFact, EtfNavObservation, EtfHoldingsSnapshot, EtfListing, EtfAcquisitionAttempt,
    evidence_id, evidence_precedence,
)


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS etf_fact_observations (
    evidence_id TEXT PRIMARY KEY, instrument_id TEXT NOT NULL, metric TEXT NOT NULL,
    as_of_date TEXT, provider TEXT NOT NULL, source_identity TEXT NOT NULL,
    authority TEXT NOT NULL, published_at TEXT, retrieved_at TEXT NOT NULL,
    source_mode TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_etf_facts_lookup ON etf_fact_observations (instrument_id, metric, as_of_date);
CREATE TABLE IF NOT EXISTS etf_nav_observations (
    evidence_id TEXT PRIMARY KEY, instrument_id TEXT NOT NULL, nav TEXT NOT NULL,
    currency TEXT NOT NULL, nav_date TEXT NOT NULL, provider TEXT NOT NULL,
    source_identity TEXT NOT NULL, authority TEXT NOT NULL, published_at TEXT,
    retrieved_at TEXT NOT NULL, source_mode TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_etf_nav_lookup ON etf_nav_observations (instrument_id, nav_date);
CREATE TABLE IF NOT EXISTS etf_holdings_snapshots (
    evidence_id TEXT PRIMARY KEY, instrument_id TEXT NOT NULL, as_of_date TEXT NOT NULL,
    provider TEXT NOT NULL, source_identity TEXT NOT NULL, authority TEXT NOT NULL,
    published_at TEXT, retrieved_at TEXT NOT NULL, source_mode TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_etf_holdings_lookup ON etf_holdings_snapshots (instrument_id, as_of_date);
CREATE TABLE IF NOT EXISTS etf_holdings_positions (
    snapshot_id TEXT NOT NULL REFERENCES etf_holdings_snapshots(evidence_id),
    position_number INTEGER NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, position_number)
);
CREATE TABLE IF NOT EXISTS etf_listing_observations (
    evidence_id TEXT PRIMARY KEY, exchange TEXT NOT NULL CHECK(exchange = 'NSE'),
    isin TEXT NOT NULL, symbol TEXT NOT NULL, instrument_id TEXT,
    asset_type TEXT NOT NULL CHECK(asset_type = 'ETF'), retrieved_at TEXT NOT NULL,
    source_mode TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_etf_listing_identity ON etf_listing_observations (exchange, isin);
CREATE TABLE IF NOT EXISTS etf_acquisition_attempts (
    attempt_id TEXT PRIMARY KEY, instrument_id TEXT, metric TEXT NOT NULL,
    provider TEXT NOT NULL, attempted_at TEXT NOT NULL, outcome TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_etf_attempt_lookup ON etf_acquisition_attempts (instrument_id, metric, attempted_at);
"""


class EtfPersistenceMixin:
    def _insert_etf_evidence(self, evidence):
        key = evidence_id(evidence)
        p = evidence.provenance
        common = dict(evidence_id=key, instrument_id=str(evidence.instrument_id) if evidence.instrument_id else None,
                      provider=p.provider, source_identity=p.source_identity, authority=p.authority,
                      published_at=p.published_at.isoformat() if p.published_at else None,
                      retrieved_at=p.retrieved_at.isoformat(), source_mode=p.source_mode,
                      payload=evidence.model_dump_json())
        if isinstance(evidence, EtfFact):
            table = "etf_fact_observations"
            common.update(metric=evidence.metric, as_of_date=evidence.as_of_date.isoformat() if evidence.as_of_date else None)
        elif isinstance(evidence, EtfNavObservation):
            table = "etf_nav_observations"
            common.update(nav=str(evidence.nav), currency=evidence.currency, nav_date=evidence.nav_date.isoformat())
        elif isinstance(evidence, EtfHoldingsSnapshot):
            table = "etf_holdings_snapshots"
            common.update(as_of_date=evidence.as_of_date.isoformat())
        elif isinstance(evidence, EtfListing):
            table = "etf_listing_observations"
            common = {k: common[k] for k in ("evidence_id", "instrument_id", "retrieved_at", "source_mode", "payload")}
            common.update(exchange=evidence.exchange, isin=evidence.isin, symbol=evidence.symbol, asset_type=evidence.asset_type)
        else:
            raise TypeError("ETF evidence model required")
        self._connection.execute(
            f"INSERT INTO {table} ({','.join(common)}) VALUES ({','.join('?' for _ in common)}) ON CONFLICT DO NOTHING",
            tuple(common.values()))
        if isinstance(evidence, EtfHoldingsSnapshot):
            # Canonical order matches the revision hash, independent of row order.
            for number, holding in enumerate(sorted(evidence.holdings, key=lambda h: h.model_dump_json())):
                self._connection.execute(
                    "INSERT INTO etf_holdings_positions (snapshot_id,position_number,payload) VALUES (?,?,?) ON CONFLICT DO NOTHING",
                    (key, number, holding.model_dump_json()))
        return key

    def save_etf_evidence(self, evidence):
        with self._connection:
            return self._insert_etf_evidence(evidence)

    def save_etf_acquisition(self, evidence, attempt: EtfAcquisitionAttempt):
        """Evidence and the attempt outcome commit or roll back together."""
        if attempt.evidence_ids != [evidence_id(e) for e in evidence]:
            raise ValueError("Attempt must identify exactly the evidence being persisted")
        if any(e.instrument_id != attempt.instrument_id for e in evidence) and attempt.metric != "DISCOVERY":
            raise ValueError("Acquisition instrument mismatch")
        with self._connection:
            for item in evidence:
                self._insert_etf_evidence(item)
            self._connection.execute(
                """INSERT INTO etf_acquisition_attempts
                (attempt_id,instrument_id,metric,provider,attempted_at,outcome,payload) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT DO NOTHING""",
                (str(attempt.attempt_id), str(attempt.instrument_id) if attempt.instrument_id else None,
                 attempt.metric, attempt.provider, attempt.attempted_at.isoformat(), attempt.outcome, attempt.model_dump_json()))

    def _etf_rows(self, table, model, instrument_id, *, source_mode="REAL", metric=None):
        query = f"SELECT payload FROM {table} WHERE instrument_id=? AND source_mode=?"
        args = [str(instrument_id), source_mode]
        if metric is not None:
            query += " AND metric=?"
            args.append(str(metric))
        with self._connection:
            rows = self._connection.execute(query, tuple(args)).fetchall()
        return sorted((model.model_validate_json(row["payload"]) for row in rows), key=evidence_precedence, reverse=True)

    def etf_facts(self, instrument_id, metric=None, *, source_mode="REAL"):
        return self._etf_rows("etf_fact_observations", EtfFact, instrument_id, metric=metric, source_mode=source_mode)

    def etf_navs(self, instrument_id, *, source_mode="REAL"):
        return self._etf_rows("etf_nav_observations", EtfNavObservation, instrument_id, source_mode=source_mode)

    def etf_holdings(self, instrument_id, *, source_mode="REAL"):
        return self._etf_rows("etf_holdings_snapshots", EtfHoldingsSnapshot, instrument_id, source_mode=source_mode)

    def etf_listings(self, *, source_mode="REAL"):
        with self._connection:
            rows = self._connection.execute("SELECT payload FROM etf_listing_observations WHERE source_mode=?", (source_mode,)).fetchall()
        revisions = [EtfListing.model_validate_json(row["payload"]) for row in rows]
        selected = {}
        for listing in sorted(revisions, key=lambda item: (item.instrument_id is not None,
                item.provenance.retrieved_at, evidence_id(item)), reverse=True):
            selected.setdefault((listing.exchange, listing.isin), listing)
        return [selected[key] for key in sorted(selected)]

    def etf_attempts(self, instrument_id=None):
        with self._connection:
            rows = self._connection.execute(
                "SELECT payload FROM etf_acquisition_attempts WHERE "
                + ("instrument_id IS NULL" if instrument_id is None else "instrument_id=?")
                + " ORDER BY attempted_at DESC, attempt_id DESC",
                () if instrument_id is None else (str(instrument_id),)).fetchall()
        return [EtfAcquisitionAttempt.model_validate_json(row["payload"]) for row in rows]
