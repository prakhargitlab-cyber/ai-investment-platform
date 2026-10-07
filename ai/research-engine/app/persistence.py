from __future__ import annotations

import sqlite3
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Protocol
from uuid import UUID, uuid4

from app.models import (
    DocumentStatus,
    DocumentType,
    EventImpact,
    ReliabilityLevel,
    ResearchDocument,
    ResearchEvent,
    ResearchEvidenceSource,
    ResearchEventType,
    ResearchLifecycleStatus,
    ShareholdingCategory,
    ShareholdingSnapshot,
    ShareholdingSnapshotValue,
    SourceClassification,
    SourceMode,
    SourceType,
    TimeHorizon,
    StructuredMarketSnapshot,
    StructuredMarketSnapshotRecord,
    MarketPriceObservation,
    DailyMarketBar,
)
from app.settings import Settings
from app.fact_precedence import FinancialFact, FinancialFactKey, FactSourceTier, merge_fact, newer_official_disclosure
from app.models import ProvenancedValue


@dataclass(frozen=True)
class RefreshRun:
    refresh_run_id: UUID
    instrument_id: UUID
    company_id: UUID
    started_at: datetime
    correlation_id: str | None
    mode: str


class ResearchPersistence(Protocol):
    def load_documents(self) -> list[ResearchDocument]:
        ...

    def load_document(self, document_id: UUID) -> ResearchDocument | None:
        ...

    def load_documents_by_ids(self, document_ids: list[UUID]) -> list[ResearchDocument]:
        ...

    def load_document_identities(self) -> list[tuple[UUID, UUID | None, str, str, str, datetime | None]]:
        ...

    def load_events(self, instrument_ids: set[UUID] | None = None) -> list[ResearchEvent]:
        ...

    def upsert_document(self, document: ResearchDocument) -> bool:
        ...

    def reconcile_financial_facts_for_source(
        self,
        instrument_id: UUID,
        source_identity: str,
        facts: list[FinancialFact],
        *,
        allow_official_revision: bool = False,
        complete_scopes: tuple = (),
    ) -> int:
        """Atomically upsert source facts and reconcile explicitly complete scopes."""
        ...

    def upsert_event(self, event: ResearchEvent) -> bool:
        ...

    def load_shareholding_snapshots(self, instrument_ids: set[UUID] | None = None) -> list[ShareholdingSnapshot]:
        ...

    def upsert_shareholding_snapshot(self, snapshot: ShareholdingSnapshot) -> bool:
        ...

    def upsert_manual_evidence_draft(
        self,
        *,
        draft_id: UUID,
        evidence_type: str,
        content_hash: str,
        original_filename: str,
        content_type: str,
        instrument_id: UUID | None,
        reporting_period: datetime | None,
        extraction_method: str,
        extraction_results: str,
        validation_results: str,
        corrections: str,
        accepted_at: datetime,
    ) -> bool:
        ...

    def load_manual_evidence_drafts(self, evidence_type: str | None = None) -> list[Any]:
        ...

    def start_refresh_run(
        self,
        *,
        instrument_id: UUID,
        company_id: UUID,
        correlation_id: str | None,
        mode: str,
    ) -> RefreshRun:
        ...

    def complete_refresh_run(
        self,
        run: RefreshRun,
        *,
        status: str,
        documents_discovered: int,
        documents_accepted: int,
        events_extracted: int,
        events_created: int,
        events_updated: int,
        deduplicated_count: int,
        safe_error_code: str | None = None,
        safe_error_message: str | None = None,
    ) -> None:
        ...

    def load_financial_facts(self, instrument_ids: set[UUID] | None = None) -> list[FinancialFact]: ...
    def upsert_financial_fact(self, fact: FinancialFact, *, allow_same_tier_correction: bool = False) -> bool: ...
    def load_structured_market_snapshots(self, instrument_ids: set[UUID] | None = None) -> list[StructuredMarketSnapshotRecord]: ...
    def upsert_structured_market_snapshot(self, record: StructuredMarketSnapshotRecord) -> None: ...
    def load_market_price_observations(self, instrument_ids: set[UUID] | None = None) -> list[MarketPriceObservation]: ...
    def load_market_price_coverage(self, instrument_ids: set[UUID]) -> dict[UUID, tuple[datetime, datetime, int]]: ...
    def upsert_market_price_observation(self, observation: MarketPriceObservation) -> None: ...
    def upsert_daily_market_bar(self, bar: DailyMarketBar) -> None: ...
    def upsert_daily_market_bars(self, bars: list[DailyMarketBar]) -> int: ...
    def load_daily_market_bars(self, instrument_ids: set[UUID], *, start_date: date | None = None,
                              end_date: date | None = None, provider: str | None = None) -> list[DailyMarketBar]: ...
    def record_structured_market_failure(self, instrument_id: UUID, provider: str, attempted_at: datetime, code: str, message: str) -> None: ...
    def load_market_schedules(self, markets: set[str] | None = None): ...
    def load_market_calendar_exceptions(self, markets: set[str] | None = None): ...
    def load_stock_rule_engine_result(
        self, global_instrument_id: UUID, rule_engine_version: str, input_fingerprint: str
    ) -> dict[str, Any] | None: ...
    def upsert_stock_rule_engine_result(self, result: dict[str, Any]) -> None: ...


class DisabledResearchPersistence:
    def opportunity_current(self):
        return dict(generated_at=None, best_buy_today=None, top_short_term=[], top_long_term=[], top_exit=[], previous_recommendations=[])

    def recommendation_history(self, instrument_id=None): return []
    def radar_recommendation_history(self, instrument_id=None): return []
    def recommendation_states(self): return []
    def portfolio_recommendation_signals(self, *, instrument_ids=None): return []
    def backtests(self): return []

    def __init__(self) -> None:
        self._stock_rule_engine_results: dict[tuple[str, str, str], dict[str, Any]] = {}
    def load_documents(self) -> list[ResearchDocument]:
        return []

    def load_document(self, document_id: UUID) -> ResearchDocument | None:
        return None

    # Nothing is stored: the in-memory copy of a document is the only copy,
    # so the repository must never evict it (see app/document_cache.py).
    durable_documents = False

    def load_documents_by_ids(self, document_ids: list[UUID]) -> list[ResearchDocument]:
        return []

    def load_document_identities(self) -> list[tuple[UUID, UUID | None, str, str, str, datetime | None]]:
        return []

    def load_events(self, instrument_ids: set[UUID] | None = None) -> list[ResearchEvent]:
        return []

    def upsert_document(self, document: ResearchDocument) -> bool:
        return True

    def upsert_event(self, event: ResearchEvent) -> bool:
        return True

    def load_shareholding_snapshots(self, instrument_ids: set[UUID] | None = None) -> list[ShareholdingSnapshot]:
        return []

    def upsert_shareholding_snapshot(self, snapshot: ShareholdingSnapshot) -> bool:
        return True

    def upsert_manual_evidence_draft(self, **kwargs) -> bool:
        return True

    def load_manual_evidence_drafts(self, evidence_type=None) -> list[Any]:
        return []

    def start_refresh_run(
        self,
        *,
        instrument_id: UUID,
        company_id: UUID,
        correlation_id: str | None,
        mode: str,
    ) -> RefreshRun:
        return RefreshRun(uuid4(), instrument_id, company_id, datetime.now(timezone.utc), correlation_id, mode)

    def complete_refresh_run(self, run: RefreshRun, **kwargs) -> None:
        return None

    def load_financial_facts(self, instrument_ids=None): return []
    def upsert_financial_fact(self, fact, **kwargs): return True
    def load_structured_market_snapshots(self, instrument_ids=None): return []
    def upsert_structured_market_snapshot(self, record): return None
    def load_market_price_observations(self, instrument_ids=None): return []
    def load_market_price_coverage(self, instrument_ids): return {}
    def upsert_market_price_observation(self, observation): return None
    def upsert_daily_market_bar(self, bar): return None
    def upsert_daily_market_bars(self, bars): return 0
    def load_daily_market_bars(self, instrument_ids, *, start_date=None, end_date=None, provider=None): return []
    def record_structured_market_failure(self, *args): return None
    def load_market_schedules(self, markets=None): return []
    def load_market_calendar_exceptions(self, markets=None): return []
    def load_stock_rule_engine_result(self, global_instrument_id, rule_engine_version, input_fingerprint):
        value = self._stock_rule_engine_results.get(
            (str(global_instrument_id), rule_engine_version, input_fingerprint)
        )
        return dict(value) if value is not None else None
    def upsert_stock_rule_engine_result(self, result):
        _assert_global_score_public(result)
        key = (
            str(result["global_instrument_id"]),
            str(result["rule_engine_version"]),
            str(result["input_fingerprint"]),
        )
        self._stock_rule_engine_results.setdefault(key, dict(result))

    def record_global_market_scan(self, scan):
        return None

    def global_market_scans(self):
        return []

    def persist_global_suggestion_lifecycle(self, scan, cards):
        return None

    def global_current_suggestions(self, *, horizon=None):
        return []

    def global_opportunity_radar(self):
        # No persistence backend -- there is no cycle history to draw
        # previous_recommendations from, so it is always [], matching
        # opportunity_current()'s own empty-state fallback above.
        return dict(best_buy_today=None, top_short_term=[], top_long_term=[],
                    top_exit=[], top_short_term_count=0, top_long_term_count=0,
                    top_exit_count=0, previous_recommendations=[], generated_at=None,
                    last_processed_at=None, source_scan_id=None)


from app.news_persistence import NewsPersistenceMixin, sqlite_schema as news_sqlite_schema
from app.opportunity_persistence import OpportunityPersistenceMixin, SCHEMA as OPPORTUNITY_SCHEMA, SUGGESTION_SCHEMA
from app.cycle_checkpoint import CycleRunPersistenceMixin, SQLITE_SCHEMA as CYCLE_RUN_SCHEMA
from app.macro_persistence import MacroPersistenceMixin, SQLITE_SCHEMA as MACRO_SCHEMA
from app.macro_event_persistence import MacroEventPersistenceMixin, SQLITE_SCHEMA as MACRO_EVENT_SCHEMA
from app.etf_persistence import EtfPersistenceMixin, SQLITE_SCHEMA as ETF_SQLITE_SCHEMA


class SqliteResearchPersistence(NewsPersistenceMixin, OpportunityPersistenceMixin, CycleRunPersistenceMixin, MacroPersistenceMixin, MacroEventPersistenceMixin, EtfPersistenceMixin):
    def __init__(self, database_path: str | Path = ":memory:") -> None:
        self.database_path = str(database_path)
        self._connection = sqlite3.connect(self.database_path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self.migrate()

    def migrate(self) -> None:
        self._connection.executescript(_sqlite_schema())
        self._connection.commit()
        news_sqlite_schema(self._connection)
        self._connection.executescript(OPPORTUNITY_SCHEMA)
        self._connection.commit()
        self._connection.executescript(SUGGESTION_SCHEMA)
        self._connection.commit()
        self._connection.executescript(CYCLE_RUN_SCHEMA)
        self._connection.commit()
        self._connection.executescript(MACRO_SCHEMA)
        self._connection.commit()
        self._connection.executescript(MACRO_EVENT_SCHEMA)
        self._connection.commit()
        self._connection.executescript(ETF_SQLITE_SCHEMA)
        self._connection.commit()

    def upsert_daily_market_bar(self, bar: DailyMarketBar) -> None:
        self.upsert_daily_market_bars([bar])

    def upsert_daily_market_bars(self, bars: list[DailyMarketBar]) -> int:
        """Atomic correction of provider/day rows; last duplicate input wins.

        Validate before writing (including model_copy/construct bypasses). Batch
        50 rows/750 parameters to stay below older SQLite's 999-parameter limit.
        Returns the number of distinct identities supplied, not a change count.
        """
        selected = {}
        for bar in bars:
            validated = DailyMarketBar.model_validate(bar.model_dump())
            key = (str(validated.global_instrument_id), validated.trading_date, validated.provider)
            selected[key] = validated
        ordered = [selected[key] for key in sorted(selected)]
        if not ordered:
            return 0
        with self._connection:
            for offset in range(0, len(ordered), 50):
                batch = ordered[offset:offset + 50]
                values = ",".join("(" + ",".join("?" for _ in range(15)) + ")" for _ in batch)
                params = []
                for bar in batch:
                    params.extend((str(bar.global_instrument_id), bar.trading_date.isoformat(),
                        _decimal(bar.open), _decimal(bar.high), _decimal(bar.low), _decimal(bar.close),
                        _decimal(bar.previous_close), bar.volume, _decimal(bar.turnover), bar.currency,
                        bar.provider, bar.provider_symbol, str(bar.source_mode), bar.source_url, _dt(bar.retrieved_at)))
                self._connection.execute(f"""INSERT INTO global_daily_market_bars (
                    global_instrument_id,trading_date,open_price,high_price,low_price,close_price,
                    previous_close,volume,turnover,currency,provider,provider_symbol,source_mode,source_url,retrieved_at
                ) VALUES {values}
                ON CONFLICT(global_instrument_id,trading_date,provider) DO UPDATE SET
                    open_price=excluded.open_price,high_price=excluded.high_price,low_price=excluded.low_price,
                    close_price=excluded.close_price,previous_close=excluded.previous_close,volume=excluded.volume,
                    turnover=excluded.turnover,currency=excluded.currency,provider_symbol=excluded.provider_symbol,
                    source_mode=excluded.source_mode,source_url=excluded.source_url,retrieved_at=excluded.retrieved_at""", params)
        return len(ordered)

    def load_daily_market_bars(self, instrument_ids: set[UUID], *, start_date: date | None = None,
                              end_date: date | None = None, provider: str | None = None) -> list[DailyMarketBar]:
        """Inclusive date bounds, explicit IDs only, stable global/date/provider order."""
        if instrument_ids is None:
            raise ValueError("Daily bars require an explicit instrument ID set")
        if not instrument_ids:
            return []
        if any(value is not None and type(value) is not date for value in (start_date, end_date)):
            raise ValueError("Daily bar range bounds must be DATE values")
        if start_date is not None and end_date is not None and start_date > end_date:
            raise ValueError("Daily bar start_date must be <= end_date")
        if provider is not None and not provider.strip():
            raise ValueError("Provider filter must be nonblank")
        ordered = sorted({str(UUID(str(value))) for value in instrument_ids})
        result = []
        with self._connection:
            for offset in range(0, len(ordered), 500):
                batch = ordered[offset:offset + 500]
                params = list(batch)
                sql = "SELECT * FROM global_daily_market_bars WHERE global_instrument_id IN (" + ",".join("?" for _ in batch) + ")"
                if start_date is not None:
                    sql += " AND trading_date >= ?"
                    params.append(start_date.isoformat())
                if end_date is not None:
                    sql += " AND trading_date <= ?"
                    params.append(end_date.isoformat())
                if provider is not None:
                    sql += " AND provider = ?"
                    params.append(provider.strip())
                sql += " ORDER BY global_instrument_id, trading_date, provider"
                result.extend(_daily_market_bar_from_row(row) for row in self._connection.execute(sql, params).fetchall())
        # Sort once more for identical ordering across database collations.
        return sorted(result, key=lambda bar: (str(bar.global_instrument_id), bar.trading_date, bar.provider))

    def _filtered_rows(self, table, ids, *, column="instrument_id", order=None):
        """Internal identifiers only; values are parameterized in bounded batches.

        Read-only: wrapped in ``with self._connection`` so the implicit psycopg
        transaction (autocommit=False) opened by the first SELECT is committed on
        return -- otherwise the long-lived connection is left ``idle in
        transaction`` (DI-16B). Reuses the commit/rollback boundary the write
        methods already use via ``with self._connection``.
        """
        with self._connection:
            if ids is not None:
                ordered = sorted({str(value) for value in ids})
                rows = []
                for offset in range(0, len(ordered), 500):
                    batch = ordered[offset:offset + 500]
                    sql = f"SELECT * FROM {table} WHERE {column} IN ({','.join('?' for _ in batch)})"
                    if order:
                        sql += f" ORDER BY {order}"
                    rows.extend(self._connection.execute(sql, batch).fetchall())
                return rows
            sql = f"SELECT * FROM {table}"
            if order:
                sql += f" ORDER BY {order}"
            return self._connection.execute(sql).fetchall()

    def load_financial_facts(self, instrument_ids: set[UUID] | None = None) -> list[FinancialFact]:
        return [_financial_fact_from_row(row) for row in self._filtered_rows("global_financial_facts", instrument_ids)]

    def upsert_financial_fact(self, fact: FinancialFact, *, allow_same_tier_correction: bool = False) -> bool:
        key = fact.key
        basis = key.reporting_basis or ""
        row = self._connection.execute("SELECT * FROM global_financial_facts WHERE instrument_id=? AND metric=? AND period_end=? AND period_type=? AND reporting_basis=?", (str(key.instrument_id), key.metric, key.period_end or "", key.period_type, basis)).fetchone()
        existing = _financial_fact_from_row(row) if row else None
        accepted = merge_fact(existing, fact, allow_same_tier_correction=allow_same_tier_correction)
        if accepted is existing: return False
        with self._connection:
            self._write_financial_fact(accepted)
        return True

    def reconcile_financial_facts_for_source(
        self,
        instrument_id: UUID,
        source_identity: str,
        facts: list[FinancialFact],
        *,
        allow_official_revision: bool = False,
        complete_scopes: tuple = (),
    ) -> int:
        """Upsert validated output; delete only inside explicit complete scopes.

        The source identity is the durable ``research_documents.document_id``.
        Facts owned by other documents can share a semantic key but are never
        replaced or deleted merely because this document is reconciled. An
        explicitly authorized newer official revision may replace comparable
        keys; obsolete-key deletion remains restricted to this source.
        """
        if not source_identity:
            raise ValueError("A source-owned financial fact set is required")
        if not facts:
            return 0
        if any(fact.key.instrument_id != instrument_id or fact.source_identity != source_identity for fact in facts):
            raise ValueError("Document reconciliation facts must share instrument and source identity")
        incoming = {fact.key: fact for fact in facts}
        if len(incoming) != len(facts):
            raise ValueError("Duplicate financial fact identities must be resolved before persistence")
        from app.financial_projection import FinancialReconciliationScope
        if any(not isinstance(scope, FinancialReconciliationScope) for scope in complete_scopes):
            raise ValueError("Explicit financial reconciliation scopes are required")
        # A scope cannot authorize deleting stronger evidence or use a wholly
        # empty family as evidence of completeness.
        authorized_scopes = tuple(scope for scope in complete_scopes if any(
            scope.contains(fact.key) and fact.source_tier == FactSourceTier.OFFICIAL_NSE
            and fact.source_provider == "NSE" and fact.source_mode == SourceMode.REAL for fact in facts))
        written = 0
        with self._connection:
            owned_rows = self._connection.execute(
                "SELECT * FROM global_financial_facts WHERE instrument_id=? AND source_identity=?",
                (str(instrument_id), source_identity),
            ).fetchall()
            owned = {_financial_fact_from_row(row).key: _financial_fact_from_row(row) for row in owned_rows}
            for key, fact in incoming.items():
                basis = key.reporting_basis or ""
                row = self._connection.execute(
                    "SELECT * FROM global_financial_facts WHERE instrument_id=? AND metric=? AND period_end=? AND period_type=? AND reporting_basis=?",
                    (str(key.instrument_id), key.metric, key.period_end or "", key.period_type, basis),
                ).fetchone()
                existing = _financial_fact_from_row(row) if row else None
                accepted = merge_fact(
                    existing,
                    fact,
                    allow_same_tier_correction=(
                        existing is not None
                        and existing.source_tier == FactSourceTier.OFFICIAL_NSE
                        and (existing.source_identity == source_identity
                             or (allow_official_revision and newer_official_disclosure(existing, fact)))
                    ),
                )
                if accepted is fact:
                    transient = {"as_of_date", "period", "calculation_basis"}
                    if existing is not None and (existing.key, existing.value.model_dump(exclude=transient),
                            existing.source_identity, existing.source_tier, existing.source_provider, existing.source_mode) == (
                            fact.key, fact.value.model_dump(exclude=transient), fact.source_identity, fact.source_tier,
                            fact.source_provider, fact.source_mode):
                        continue
                    self._write_financial_fact(fact)
                    written += 1
            for key in set(owned) - set(incoming):
                if (owned[key].source_tier != FactSourceTier.OFFICIAL_NSE or owned[key].source_provider != "NSE"
                        or owned[key].source_mode != SourceMode.REAL
                        or not any(scope.contains(key) for scope in authorized_scopes)):
                    continue
                self._connection.execute(
                    "DELETE FROM global_financial_facts WHERE instrument_id=? AND metric=? AND period_end=? AND period_type=? AND reporting_basis=? AND source_identity=?",
                    (str(key.instrument_id), key.metric, key.period_end or "", key.period_type, key.reporting_basis or "", source_identity),
                )
                written += 1
        return written

    def _write_financial_fact(self, fact: FinancialFact) -> None:
        key = fact.key
        self._connection.execute("""INSERT INTO global_financial_facts (instrument_id,metric,period_end,period_type,reporting_basis,fact_value,unit,source_provider,source_identity,source_url,source_name,source_type,published_at,retrieved_at,confidence,source_mode,source_tier)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(instrument_id,metric,period_end,period_type,reporting_basis) DO UPDATE SET fact_value=excluded.fact_value,unit=excluded.unit,source_provider=excluded.source_provider,source_identity=excluded.source_identity,source_url=excluded.source_url,source_name=excluded.source_name,source_type=excluded.source_type,published_at=excluded.published_at,retrieved_at=excluded.retrieved_at,confidence=excluded.confidence,source_mode=excluded.source_mode,source_tier=excluded.source_tier""",
        (str(key.instrument_id), key.metric, key.period_end or "", key.period_type, key.reporting_basis or "", str(fact.value.value), fact.value.unit, fact.source_provider, fact.source_identity, fact.value.source_url, fact.value.source_name, fact.value.source_type, _dt(fact.value.published_at), _dt(fact.value.retrieved_at), fact.value.confidence, str(fact.source_mode), int(fact.source_tier)))

    def load_structured_market_snapshots(self, instrument_ids: set[UUID] | None = None) -> list[StructuredMarketSnapshotRecord]:
        if instrument_ids is not None and not instrument_ids:
            return []
        params: list[str] = []
        sql = "SELECT * FROM global_structured_market_snapshots"
        if instrument_ids:
            sql += " WHERE instrument_id IN (" + ",".join("?" for _ in instrument_ids) + ")"
            params = [str(value) for value in instrument_ids]
        with self._connection:
            rows = self._connection.execute(sql, params).fetchall()
        return [_structured_snapshot_from_row(row) for row in rows]

    def upsert_structured_market_snapshot(self, record: StructuredMarketSnapshotRecord) -> None:
        payload = record.snapshot.model_dump(mode="json")
        with self._connection:
            self._connection.execute("""INSERT INTO global_structured_market_snapshots (
                instrument_id, provider, provider_instrument_id, exchange, mic, currency, quote_type, source_url, source_name, source_type, source_identity,
                market_as_of, retrieved_at, persisted_at, last_price_at, last_valuation_at, last_fundamentals_at, last_analyst_at,
                last_success_at, last_provider_attempt_at, acquisition_status, last_failure_code, last_failure_message, facts_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(instrument_id,provider) DO UPDATE SET
              provider_instrument_id=excluded.provider_instrument_id, exchange=excluded.exchange, mic=excluded.mic, currency=excluded.currency, quote_type=excluded.quote_type,
              source_url=excluded.source_url, source_name=excluded.source_name, source_type=excluded.source_type, source_identity=excluded.source_identity,
              market_as_of=excluded.market_as_of, retrieved_at=excluded.retrieved_at, persisted_at=excluded.persisted_at,
              last_price_at=excluded.last_price_at,last_valuation_at=excluded.last_valuation_at,last_fundamentals_at=excluded.last_fundamentals_at,last_analyst_at=excluded.last_analyst_at,
              last_success_at=excluded.last_success_at,last_provider_attempt_at=excluded.last_provider_attempt_at,acquisition_status=excluded.acquisition_status,
              last_failure_code=excluded.last_failure_code,last_failure_message=excluded.last_failure_message,facts_json=excluded.facts_json""",
              (str(record.instrument_id), record.provider, record.provider_instrument_id, record.exchange, record.mic, record.currency, record.quote_type,
               record.source_url, record.source_name, record.source_type, record.source_identity, _dt(record.market_as_of), _dt(record.retrieved_at), _dt(record.persisted_at),
               _dt(record.last_price_at), _dt(record.last_valuation_at), _dt(record.last_fundamentals_at), _dt(record.last_analyst_at), _dt(record.last_success_at),
               _dt(record.last_provider_attempt_at), record.acquisition_status, record.last_failure_code, record.last_failure_message, json.dumps(payload)))
            observation = _price_observation_from_snapshot(record)
            if observation:
                self._connection.execute("""INSERT OR IGNORE INTO global_market_price_observations
                    (instrument_id,observed_at,price,currency,provider,source_url,retrieved_at)
                    VALUES (?,?,?,?,?,?,?)""", (str(observation.instrument_id), _dt(observation.observed_at), str(observation.price),
                    observation.currency, observation.provider, observation.source_url, _dt(observation.retrieved_at)))

    def load_market_price_observations(self, instrument_ids: set[UUID] | None = None) -> list[MarketPriceObservation]:
        if instrument_ids is not None and not instrument_ids:
            return []
        params: list[str] = []
        sql = "SELECT * FROM global_market_price_observations"
        if instrument_ids:
            sql += " WHERE instrument_id IN (" + ",".join("?" for _ in instrument_ids) + ")"
            params = [str(value) for value in instrument_ids]
        sql += " ORDER BY observed_at"
        with self._connection:
            rows = self._connection.execute(sql, params).fetchall()
        return [MarketPriceObservation(instrument_id=_required_uuid(row["instrument_id"], "global_market_price_observations.instrument_id"),
            observed_at=_parse_dt(row["observed_at"]) or datetime.now(timezone.utc), price=Decimal(str(row["price"])), currency=row["currency"],
            provider=row["provider"], source_url=row["source_url"], retrieved_at=_parse_dt(row["retrieved_at"]) or datetime.now(timezone.utc))
            for row in rows]

    def load_market_price_coverage(self, instrument_ids: set[UUID]) -> dict[UUID, tuple[datetime, datetime, int]]:
        if not instrument_ids:
            return {}
        placeholders = ",".join("?" for _ in instrument_ids)
        with self._connection:
            rows = self._connection.execute(
                f"""SELECT instrument_id, MIN(observed_at) AS first_observed_at,
                    MAX(observed_at) AS latest_observed_at, COUNT(*) AS observation_count
                    FROM global_market_price_observations
                    WHERE instrument_id IN ({placeholders}) AND CAST(price AS NUMERIC) > 0
                    GROUP BY instrument_id""",
                [str(value) for value in instrument_ids],
            ).fetchall()
        coverage: dict[UUID, tuple[datetime, datetime, int]] = {}
        for row in rows:
            first = _parse_dt(row["first_observed_at"])
            latest = _parse_dt(row["latest_observed_at"])
            if first is not None and latest is not None:
                coverage[_required_uuid(row["instrument_id"], "global_market_price_observations.instrument_id")] = (
                    first, latest, int(row["observation_count"]),
                )
        return coverage

    def upsert_market_price_observation(self, observation: MarketPriceObservation) -> None:
        with self._connection:
            self._connection.execute("""INSERT INTO global_market_price_observations
                (instrument_id,observed_at,price,currency,provider,source_url,retrieved_at)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(instrument_id,provider,observed_at) DO UPDATE SET
                  price=excluded.price,currency=excluded.currency,source_url=excluded.source_url,retrieved_at=excluded.retrieved_at""",
                (str(observation.instrument_id), _dt(observation.observed_at), str(observation.price),
                 observation.currency, observation.provider, observation.source_url, _dt(observation.retrieved_at)))

    def record_structured_market_failure(self, instrument_id: UUID, provider: str, attempted_at: datetime, code: str, message: str) -> None:
        with self._connection:
            self._connection.execute("""UPDATE global_structured_market_snapshots SET last_provider_attempt_at=?, acquisition_status='PROVIDER_UNAVAILABLE',
                last_failure_code=?, last_failure_message=? WHERE instrument_id=? AND provider=?""",
                (_dt(attempted_at), code, message[:500], str(instrument_id), provider))

    def load_stock_rule_engine_result(
        self,
        global_instrument_id: UUID,
        rule_engine_version: str,
        input_fingerprint: str,
    ) -> dict[str, Any] | None:
        with self._connection:
            row = self._connection.execute(
                """SELECT result_json FROM global_stock_rule_engine_results
                   WHERE global_instrument_id=? AND rule_engine_version=? AND input_fingerprint=?""",
                (str(global_instrument_id), rule_engine_version, input_fingerprint),
            ).fetchone()
        if row is None:
            return None
        payload = row["result_json"]
        return payload if isinstance(payload, dict) else json.loads(payload)

    def upsert_stock_rule_engine_result(self, result: dict[str, Any]) -> None:
        _assert_global_score_public(result)
        with self._connection:
            self._connection.execute(
                """INSERT INTO global_stock_rule_engine_results (
                    global_instrument_id, rule_engine_version, input_fingerprint,
                    calculated_at, input_as_of, overall_score, quality_score,
                    opportunity_score, risk_score, confidence_score,
                    decision_signal, partial, result_json, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(global_instrument_id,rule_engine_version,input_fingerprint)
                DO NOTHING""",
                (
                    str(result["global_instrument_id"]),
                    str(result["rule_engine_version"]),
                    str(result["input_fingerprint"]),
                    _dt(_parse_dt(result.get("calculated_at"))),
                    _dt(_parse_dt(result.get("input_as_of"))),
                    result.get("overall_score"),
                    result.get("quality_score"),
                    result.get("opportunity_score"),
                    result.get("risk_score"),
                    result.get("confidence_score"),
                    str(result["decision_signal"]),
                    bool(result.get("partial")),
                    json.dumps(result, sort_keys=True),
                    _dt(datetime.now(timezone.utc)),
                ),
            )

    def load_market_schedules(self, markets: set[str] | None = None):
        from app.market_sessions import MarketTradingSchedule
        with self._connection:
            rows = self._connection.execute("SELECT * FROM market_trading_schedules").fetchall()
        values = [MarketTradingSchedule(row["market_code"], row["mic"], row["country_code"], row["timezone"], int(row["trading_day"]),
            _parse_sql_time(row["regular_open_time"]), _parse_sql_time(row["regular_close_time"]), bool(row["enabled"])) for row in rows]
        return [value for value in values if not markets or value.market_code in markets or value.mic in markets]

    def load_market_calendar_exceptions(self, markets: set[str] | None = None):
        from app.market_sessions import MarketCalendarException
        with self._connection:
            rows = self._connection.execute("SELECT * FROM market_trading_calendar_exceptions").fetchall()
        values = [MarketCalendarException(row["market_code"], date.fromisoformat(row["trading_date"]), row["exception_type"],
                  _parse_sql_time(row["open_time"]) if row["open_time"] else None, _parse_sql_time(row["close_time"]) if row["close_time"] else None, row["reason"]) for row in rows]
        return [value for value in values if not markets or value.market_code in markets]

    def load_documents(self) -> list[ResearchDocument]:
        with self._connection:
            rows = self._connection.execute("SELECT * FROM research_documents ORDER BY retrieved_at").fetchall()
        return [_document_from_row(row) for row in rows]

    def load_document(self, document_id: UUID) -> ResearchDocument | None:
        """Reload a single document from durable storage by id. Used by
        ResearchRepository's bounded in-memory document cache to recover a
        document that was evicted to bound process memory -- durable
        storage remains the source of truth, so eviction never loses data,
        it only trades a rare reload for a bounded memory footprint.
        """
        with self._connection:
            row = self._connection.execute(
                "SELECT * FROM research_documents WHERE document_id = ? LIMIT 1", (str(document_id),)
            ).fetchone()
        return _document_from_row(row) if row is not None else None

    def load_documents_by_ids(self, document_ids: list[UUID]) -> list[ResearchDocument]:
        """Batched durable reload of evicted document bodies (bounded
        parameter batches, one round trip per 500 ids)."""
        return [_document_from_row(row) for row in self._filtered_rows(
            "research_documents", set(document_ids), column="document_id")]

    def load_document_identities_for_instrument(self, instrument_id: UUID):
        return self.load_document_identities(instrument_id=instrument_id)

    def load_document_identities(self, *, instrument_id: UUID | None = None) -> list[tuple[UUID, UUID | None, str, str, str, datetime | None]]:
        """Cheap startup/dedup-seeding read: compact identity columns only,
        never the (potentially large) normalized_text body -- rehydration
        cost is independent of historical document size.
        """
        with self._connection:
            rows = self._connection.execute(
                "SELECT document_id, instrument_id, canonical_url, content_hash, source_mode, published_at, retrieved_at "
                "FROM research_documents "
                + ("WHERE instrument_id = ? " if instrument_id is not None else "")
                + "ORDER BY retrieved_at",
                (str(instrument_id),) if instrument_id is not None else (),
            ).fetchall()
        return [(_parse_uuid(row["document_id"]), _parse_uuid(row["instrument_id"]), row["canonical_url"], row["content_hash"],
                 SourceMode(row["source_mode"]), _parse_dt(row["published_at"]) or _parse_dt(row["retrieved_at"]))
                for row in rows]

    def load_event_refresh_times(self) -> list[tuple[UUID | None, datetime | None]]:
        """Compact startup read: latest event detection per instrument."""
        with self._connection:
            rows = self._connection.execute(
                "SELECT instrument_id, MAX(detected_at) AS detected_at FROM research_events GROUP BY instrument_id"
            ).fetchall()
        return [(_parse_uuid(row["instrument_id"]),
                 row["detected_at"] if isinstance(row["detected_at"], datetime) else _parse_dt(row["detected_at"]))
                for row in rows]

    def load_events(self, instrument_ids: set[UUID] | None = None) -> list[ResearchEvent]:
        rows = self._filtered_rows("research_events", instrument_ids, order="detected_at")
        events = [_event_from_row(row) for row in rows]
        sources_by_event: dict[UUID, list[ResearchEvidenceSource]] = {}
        for row in self._filtered_rows("research_event_sources", {row["event_id"] for row in rows}, column="event_id", order="created_at"):
            event_id = _parse_uuid(row["event_id"])
            if event_id is None:
                continue
            sources_by_event.setdefault(event_id, []).append(_evidence_source_from_row(row))
        for event in events:
            event.supporting_sources = sources_by_event.get(event.event_id, [])
        return events

    def load_shareholding_snapshots(self, instrument_ids: set[UUID] | None = None) -> list[ShareholdingSnapshot]:
        rows = self._filtered_rows("global_shareholding_snapshots", instrument_ids, order="period_end DESC, retrieved_at DESC")
        values_by_snapshot: dict[UUID, list[ShareholdingSnapshotValue]] = {}
        for row in self._filtered_rows("global_shareholding_snapshot_values", {row["id"] for row in rows}, column="snapshot_id", order="created_at"):
            snapshot_id = _required_uuid(row["snapshot_id"], "global_shareholding_snapshot_values.snapshot_id")
            values_by_snapshot.setdefault(snapshot_id, []).append(ShareholdingSnapshotValue(
                id=_required_uuid(row["id"], "global_shareholding_snapshot_values.id"), category=ShareholdingCategory(row["category"]),
                percentage=Decimal(row["percentage"]), metric_basis=row["metric_basis"],
                raw_source_label=row["raw_source_label"], source_locator=row["source_locator"],
                evidence_text=row["evidence_text"], created_at=_parse_dt(row["created_at"]) or datetime.now(timezone.utc),
            ))
        return [ShareholdingSnapshot(
            id=_required_uuid(row["id"], "global_shareholding_snapshots.id"),
            instrument_id=_required_uuid(row["instrument_id"], "global_shareholding_snapshots.instrument_id"),
            period_end=_parse_dt(row["period_end"]) or datetime.now(timezone.utc),
            filing_basis=row["filing_basis"], source_provider=row["source_provider"], source_type=row["source_type"],
            source_identity_key=row["source_identity_key"], source_url=row["source_url"],
            research_document_id=_parse_uuid(row["research_document_id"]), published_at=_parse_dt(row["published_at"]),
            retrieved_at=_parse_dt(row["retrieved_at"]) or datetime.now(timezone.utc), confidence=Decimal(row["confidence"]),
            reliability_level=ReliabilityLevel(row["reliability_level"]), source_mode=SourceMode(row["source_mode"]),
            created_at=_parse_dt(row["created_at"]) or datetime.now(timezone.utc),
            updated_at=_parse_dt(row["updated_at"]) or datetime.now(timezone.utc),
            values=values_by_snapshot.get(_required_uuid(row["id"], "global_shareholding_snapshots.id"), []),
        ) for row in rows]

    def upsert_shareholding_snapshot(self, snapshot: ShareholdingSnapshot) -> bool:
        with self._connection:
            existing = self._connection.execute(
                "SELECT id FROM global_shareholding_snapshots WHERE instrument_id = ? AND source_provider = ? AND source_identity_key = ? LIMIT 1",
                (str(snapshot.instrument_id), snapshot.source_provider, snapshot.source_identity_key),
            ).fetchone()
            if existing:
                snapshot.id = _required_uuid(existing["id"], "global_shareholding_snapshots.id")
                values_added = False
                for value in snapshot.values:
                    cursor = self._connection.execute(
                        """INSERT OR IGNORE INTO global_shareholding_snapshot_values (
                           id, snapshot_id, category, percentage, metric_basis, raw_source_label, source_locator, evidence_text, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (str(value.id), str(snapshot.id), str(value.category), _decimal(value.percentage), value.metric_basis,
                         value.raw_source_label, value.source_locator, value.evidence_text, _dt(value.created_at)),
                    )
                    values_added = values_added or bool(getattr(cursor, "rowcount", 0))
                return values_added
            self._connection.execute(
                """INSERT INTO global_shareholding_snapshots (
                    id, instrument_id, period_end, filing_basis, source_provider, source_type, source_identity_key, source_url,
                    research_document_id, published_at, retrieved_at, confidence, reliability_level, source_mode, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(snapshot.id), str(snapshot.instrument_id), _dt(snapshot.period_end), snapshot.filing_basis,
                 snapshot.source_provider, snapshot.source_type, snapshot.source_identity_key, snapshot.source_url,
                 _uuid(snapshot.research_document_id), _dt(snapshot.published_at), _dt(snapshot.retrieved_at),
                 _decimal(snapshot.confidence), str(snapshot.reliability_level), str(snapshot.source_mode),
                 _dt(snapshot.created_at), _dt(snapshot.updated_at)),
            )
            for value in snapshot.values:
                self._connection.execute(
                    """INSERT INTO global_shareholding_snapshot_values (
                       id, snapshot_id, category, percentage, metric_basis, raw_source_label, source_locator, evidence_text, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (str(value.id), str(snapshot.id), str(value.category), _decimal(value.percentage), value.metric_basis,
                     value.raw_source_label, value.source_locator, value.evidence_text, _dt(value.created_at)),
                )
        return True

    def upsert_manual_evidence_draft(
        self,
        *,
        draft_id: UUID,
        evidence_type: str,
        content_hash: str,
        original_filename: str,
        content_type: str,
        instrument_id: UUID | None,
        reporting_period: datetime | None,
        extraction_method: str,
        extraction_results: str,
        validation_results: str,
        corrections: str,
        accepted_at: datetime,
    ) -> bool:
        """Persist an accepted manual-evidence draft's provenance record.

        Dedup by (content_hash, evidence_type): an identical upload for the
        same evidence type is recorded once; the UNIQUE constraint enforces
        this at the DB level.  Returns True if a new row was inserted.
        """
        with self._connection:
            existing = self._connection.execute(
                "SELECT draft_id FROM global_manual_evidence WHERE content_hash = ? AND evidence_type = ? LIMIT 1",
                (content_hash, evidence_type),
            ).fetchone()
            if existing:
                return False
            self._connection.execute(
                """INSERT INTO global_manual_evidence (
                    draft_id, evidence_type, content_hash, original_filename, content_type,
                    instrument_id, reporting_period, extraction_method, extraction_results,
                    validation_results, corrections, accepted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(draft_id), evidence_type, content_hash, original_filename, content_type,
                 str(instrument_id) if instrument_id else None,
                 _dt(reporting_period) if reporting_period else None,
                 extraction_method, extraction_results, validation_results, corrections,
                 _dt(accepted_at)),
            )
            return True

    def load_manual_evidence_drafts(self, evidence_type: str | None = None) -> list[Any]:
        """Load accepted manual-evidence provenance records as dicts."""
        sql = "SELECT * FROM global_manual_evidence"
        params: list[Any] = []
        if evidence_type is not None:
            sql += " WHERE evidence_type = ?"
            params.append(evidence_type)
        sql += " ORDER BY accepted_at DESC"
        rows = self._connection.execute(sql, params).fetchall()
        return [
            {
                "draft_id": row["draft_id"],
                "evidence_type": row["evidence_type"],
                "content_hash": row["content_hash"],
                "original_filename": row["original_filename"],
                "content_type": row["content_type"],
                "instrument_id": row["instrument_id"],
                "reporting_period": row["reporting_period"],
                "extraction_method": row["extraction_method"],
                "extraction_results": row["extraction_results"],
                "validation_results": row["validation_results"],
                "corrections": row["corrections"],
                "accepted_at": row["accepted_at"],
            }
            for row in rows
        ]

    def repair_official_document(self, document: ResearchDocument) -> bool:
        """Repair a failed/legacy NSE row or a republished locator in place.

        Retain its durable id and fact provenance. Ordinary duplicate filings
        and documents for a different issuer cannot overwrite an existing row.
        """
        from app.financial_projection import nse_authority_rejection
        if nse_authority_rejection(document) or not (document.normalized_text or "").strip():
            return False
        with self._connection:
            row = self._connection.execute(
                "SELECT * FROM research_documents WHERE canonical_url=?", (document.canonical_url,),
            ).fetchone()
            if row is None:
                return False
            prior = _document_from_row(row)
            if prior.instrument_id != document.instrument_id or prior.company_id != document.company_id:
                return False
            revised = bool(document.published_at and prior.published_at and document.published_at > prior.published_at)
            changed = document.content_hash != prior.content_hash and document.retrieved_at >= prior.retrieved_at
            if nse_authority_rejection(prior) is None and prior.normalized_text and not revised and not changed:
                return False
            document.published_at = document.published_at or prior.published_at
            document.document_subtype = document.document_subtype or prior.document_subtype
            self._connection.execute(
                """UPDATE research_documents SET normalized_text=?, content_hash=?, status=?,
                   title=?, published_at=?, retrieved_at=?, content_type=?, source_type=?, source_classification=?,
                   discovery_provider=?, reliability_level=?, entity_resolution_confidence=?, original_url=?,
                   source_url=?, source_name=?, document_type=?, document_subtype=?, source_independence_key=?,
                   updated_at=? WHERE document_id=?""",
                (document.normalized_text, document.content_hash, str(DocumentStatus.PROCESSED),
                 _bounded(document.title, RESEARCH_DOCUMENT_COLUMN_LIMITS["title"]), _dt(document.published_at),
                 _dt(document.retrieved_at), document.content_type, str(document.source_type), str(document.source_classification),
                 document.discovery_provider, str(document.reliability_level), document.entity_resolution_confidence,
                 document.original_url, document.original_url, _bounded(document.source_name, RESEARCH_DOCUMENT_COLUMN_LIMITS["source_name"]),
                 str(document.document_type), document.document_subtype, document.source_independence_key,
                 _dt(datetime.now(timezone.utc)), str(prior.document_id)),
            )
        document.document_id = prior.document_id
        document.status = DocumentStatus.PROCESSED
        document.duplicate_of_document_id = None
        return True

    def upsert_document(self, document: ResearchDocument) -> bool:
        inserted = False
        with self._connection:
            existing = self._connection.execute(
                "SELECT document_id FROM research_documents WHERE canonical_url = ? OR content_hash = ? LIMIT 1",
                (document.canonical_url, document.content_hash),
            ).fetchone()
            if existing:
                # existing["document_id"] is a native uuid.UUID on PostgreSQL
                # (psycopg3 adapts the `uuid` column type automatically) but a
                # plain str on the SQLite test store -- UUID(...) unconditionally
                # only handles the latter and raises AttributeError against the
                # former. _required_uuid (the file's existing UUID-normalization
                # helper, used throughout this module) accepts both without an
                # unnecessary stringify/reparse round trip for the already-UUID case.
                document.duplicate_of_document_id = _required_uuid(existing["document_id"], "research_documents.document_id")
                # A later official discovery may know a high-value subtype for
                # an attachment already stored as generic.  Enrich only the
                # optional metadata; identity/deduplication remain URL/hash
                # based and an existing subtype is never erased.
                if document.document_subtype:
                    self._connection.execute(
                        "UPDATE research_documents SET document_subtype = COALESCE(document_subtype, ?) WHERE document_id = ?",
                        (str(document.document_subtype), str(document.duplicate_of_document_id)),
                    )
                return False
            self._connection.execute(
                """
                INSERT INTO research_documents (
                    document_id, company_id, instrument_id, source_type, source_classification,
                    source_name, source_url, canonical_url, original_url, document_type, document_subtype, title,
                    published_at, retrieved_at, content_type, content_hash, source_mode, freshness,
                    reliability_level, status, entity_resolution_confidence, discovered_at,
                    discovery_provider, source_independence_key, duplicate_of_document_id,
                    normalized_text, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(document.document_id),
                    _uuid(document.company_id),
                    _uuid(document.instrument_id),
                    str(document.source_type),
                    str(document.source_classification),
                    _bounded(document.source_name, RESEARCH_DOCUMENT_COLUMN_LIMITS["source_name"]),
                    document.original_url,
                    document.canonical_url,
                    document.original_url,
                    str(document.document_type), document.document_subtype,
                    _bounded(document.title, RESEARCH_DOCUMENT_COLUMN_LIMITS["title"]),
                    _dt(document.published_at),
                    _dt(document.retrieved_at),
                    _bounded(document.content_type, RESEARCH_DOCUMENT_COLUMN_LIMITS["content_type"]),
                    document.content_hash,
                    str(document.source_mode),
                    document.freshness,
                    str(document.reliability_level),
                    str(document.status),
                    document.entity_resolution_confidence,
                    _dt(document.discovered_at),
                    _bounded(document.discovery_provider, RESEARCH_DOCUMENT_COLUMN_LIMITS["discovery_provider"]),
                    document.source_independence_key,
                    _uuid(document.duplicate_of_document_id),
                    document.normalized_text,
                    _dt(datetime.now(timezone.utc)),
                    _dt(datetime.now(timezone.utc)),
                ),
            )
            inserted = True
        return inserted

    def upsert_event(self, event: ResearchEvent) -> bool:
        fingerprint = _event_fingerprint(event)
        with self._connection:
            existing = self._connection.execute(
                "SELECT event_id FROM research_events WHERE event_fingerprint = ? LIMIT 1",
                (fingerprint,),
            ).fetchone()
            if existing:
                event.event_id = UUID(existing["event_id"])
                self._upsert_event_sources(event)
                return False
            self._connection.execute(
                """
                INSERT INTO research_events (
                    event_id, instrument_id, company_id, event_fingerprint, event_type, event_date,
                    detected_at, title, summary, impact, time_horizon, confidence, status,
                    source_document_id, source_url, source_type, source_classification, reliability,
                    source_mode, currency, monetary_value, monetary_original, percentage_value,
                    percentage_original, customer, counterparty, location, capacity_value,
                    capacity_unit, raw_evidence_reference, published_at, retrieved_at,
                    independence_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(event.event_id),
                    str(event.instrument_id),
                    str(event.company_id),
                    fingerprint,
                    str(event.event_type),
                    _dt(event.event_date),
                    _dt(event.detected_at),
                    event.title,
                    event.summary,
                    str(event.impact),
                    str(event.time_horizon),
                    event.confidence,
                    str(event.status),
                    str(event.source_document_id),
                    event.source_url,
                    str(event.source_type),
                    str(event.source_classification),
                    str(event.reliability),
                    str(event.source_mode),
                    event.currency,
                    _decimal(event.monetary_value),
                    event.monetary_original,
                    _decimal(event.percentage_value),
                    event.percentage_original,
                    event.customer,
                    event.counterparty,
                    event.location,
                    _decimal(event.capacity_value),
                    event.capacity_unit,
                    event.raw_evidence_reference,
                    _dt(event.published_at),
                    _dt(event.retrieved_at),
                    event.independence_key,
                    _dt(datetime.now(timezone.utc)),
                    _dt(datetime.now(timezone.utc)),
                ),
            )
            self._upsert_event_sources(event)
        return True

    def upsert_acquisition_observation(self, instrument_id, requirement_id, provider, outcome, observed_at, source_url, failure_reason=None, evidence_count=0):
        self._connection.execute("""INSERT INTO research_acquisition_observations
            (instrument_id, requirement_id, provider, outcome, observed_at, source_url, failure_reason, evidence_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (instrument_id, requirement_id, provider) DO UPDATE SET
            outcome=excluded.outcome, observed_at=excluded.observed_at, source_url=excluded.source_url,
            failure_reason=excluded.failure_reason, evidence_count=excluded.evidence_count""",
            (str(instrument_id), requirement_id, provider, outcome, observed_at.isoformat(), source_url, failure_reason, evidence_count))
        self._connection.commit()

    def load_acquisition_observations(self, instrument_id):
        with self._connection:
            rows = self._connection.execute("SELECT * FROM research_acquisition_observations WHERE instrument_id=? ORDER BY observed_at", (str(instrument_id),)).fetchall()
        return [dict(row) for row in rows]

    def start_refresh_run(
        self,
        *,
        instrument_id: UUID,
        company_id: UUID,
        correlation_id: str | None,
        mode: str,
    ) -> RefreshRun:
        run = RefreshRun(uuid4(), instrument_id, company_id, datetime.now(timezone.utc), correlation_id, mode)
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO research_refresh_runs (
                    refresh_run_id, instrument_id, company_id, started_at, status, mode, correlation_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (str(run.refresh_run_id), str(instrument_id), str(company_id), _dt(run.started_at), "RUNNING", mode, correlation_id),
            )
        return run

    def complete_refresh_run(
        self,
        run: RefreshRun,
        *,
        status: str,
        documents_discovered: int,
        documents_accepted: int,
        events_extracted: int,
        events_created: int,
        events_updated: int,
        deduplicated_count: int,
        safe_error_code: str | None = None,
        safe_error_message: str | None = None,
    ) -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE research_refresh_runs
                SET completed_at = ?, status = ?, documents_discovered = ?, documents_accepted = ?,
                    events_extracted = ?, events_created = ?, events_updated = ?, deduplicated_count = ?,
                    safe_error_code = ?, safe_error_message = ?, updated_at = ?
                WHERE refresh_run_id = ?
                """,
                (
                    _dt(datetime.now(timezone.utc)),
                    status,
                    documents_discovered,
                    documents_accepted,
                    events_extracted,
                    events_created,
                    events_updated,
                    deduplicated_count,
                    safe_error_code,
                    safe_error_message,
                    _dt(datetime.now(timezone.utc)),
                    str(run.refresh_run_id),
                ),
            )

    def _upsert_event_sources(self, event: ResearchEvent) -> None:
        sources = event.supporting_sources or [
            ResearchEvidenceSource(
                publisher=None,
                url=event.source_url,
                source_type=event.source_classification,
                published_at=event.published_at,
                retrieved_at=event.retrieved_at or event.detected_at,
                reliability=event.reliability,
                source_mode=event.source_mode,
                document_id=event.source_document_id,
                source_name=str(event.source_type),
                canonical_url=event.source_url,
                independent=True,
            )
        ]
        for source in sources:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO research_event_sources (
                    event_source_id, event_id, document_id, source_url, canonical_url, source_name,
                    publisher, source_classification, evidence_excerpt, reliability, published_at,
                    retrieved_at, source_mode, independent, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    str(event.event_id),
                    str(source.document_id),
                    source.url,
                    source.canonical_url,
                    source.source_name,
                    source.publisher,
                    str(source.source_type),
                    event.raw_evidence_reference,
                    str(source.reliability),
                    _dt(source.published_at),
                    _dt(source.retrieved_at),
                    str(source.source_mode),
                    bool(source.independent),
                    _dt(datetime.now(timezone.utc)),
                ),
            )


# PostgreSQL (research-service Flyway V1) VARCHAR limits for display metadata
# of research_documents. SQLite TEXT has no limit, which hid DOCUMENT_PERSIST_FAILED
# for long official NSE announcement titles (desc + attchmntText) in production.
# Identity columns (URLs, hashes) are never truncated -- only descriptive text.
# tests/test_slice5_failure_regressions.py asserts these match the migrations.
RESEARCH_DOCUMENT_COLUMN_LIMITS = {"title": 500, "source_name": 240, "content_type": 120, "discovery_provider": 120}


def _bounded(value: str | None, limit: int) -> str | None:
    if value is None or len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "\u2026"


def persistence_from_settings(settings: Settings) -> ResearchPersistence:
    if not settings.research_persistence_enabled:
        return DisabledResearchPersistence()
    if settings.research_database_backend == "sqlite":
        return SqliteResearchPersistence(settings.research_database_name)
    try:
        from app.postgres_persistence import PostgresResearchPersistence
    except ImportError as exc:
        raise RuntimeError("PostgreSQL research persistence requires psycopg") from exc
    return PostgresResearchPersistence(settings)


def _sqlite_schema() -> str:
    return """
    CREATE TABLE IF NOT EXISTS global_daily_market_bars (
        global_instrument_id TEXT NOT NULL,
        trading_date TEXT NOT NULL CHECK (length(trading_date) = 10 AND date(trading_date) IS NOT NULL AND date(trading_date) = trading_date),
        open_price TEXT CHECK (CAST(open_price AS NUMERIC) > 0),
        high_price TEXT CHECK (CAST(high_price AS NUMERIC) > 0),
        low_price TEXT CHECK (CAST(low_price AS NUMERIC) > 0),
        close_price TEXT CHECK (CAST(close_price AS NUMERIC) > 0),
        previous_close TEXT CHECK (CAST(previous_close AS NUMERIC) > 0),
        volume INTEGER CHECK (typeof(volume) = 'null' OR (typeof(volume) = 'integer' AND volume >= 0)),
        turnover TEXT CHECK (CAST(turnover AS NUMERIC) >= 0),
        currency TEXT NOT NULL CHECK (length(trim(currency)) > 0 AND length(currency) <= 16),
        provider TEXT NOT NULL CHECK (length(trim(provider)) > 0 AND length(provider) <= 120),
        provider_symbol TEXT CHECK (length(provider_symbol) <= 240),
        source_mode TEXT NOT NULL CHECK (source_mode IN ('REAL', 'DEMO')),
        source_url TEXT NOT NULL CHECK (length(trim(source_url)) > 0 AND length(source_url) <= 1000),
        retrieved_at TEXT NOT NULL,
        CONSTRAINT pk_global_daily_market_bars PRIMARY KEY (global_instrument_id, trading_date, provider),
        CONSTRAINT ck_daily_bar_range CHECK (CAST(high_price AS NUMERIC) >= CAST(low_price AS NUMERIC))
    );
    CREATE INDEX IF NOT EXISTS idx_daily_market_bars_date_instrument
        ON global_daily_market_bars (trading_date, global_instrument_id);
    CREATE TABLE IF NOT EXISTS research_acquisition_observations (
        instrument_id TEXT NOT NULL, requirement_id TEXT NOT NULL, provider TEXT NOT NULL,
        outcome TEXT NOT NULL, observed_at TEXT NOT NULL, source_url TEXT,
        failure_reason TEXT, evidence_count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (instrument_id, requirement_id, provider)
    );
    CREATE TABLE IF NOT EXISTS research_documents (
        document_id TEXT PRIMARY KEY,
        company_id TEXT,
        instrument_id TEXT,
        source_type TEXT NOT NULL,
        source_classification TEXT NOT NULL,
        source_name TEXT NOT NULL,
        source_url TEXT NOT NULL,
        canonical_url TEXT NOT NULL,
        original_url TEXT NOT NULL,
        document_type TEXT NOT NULL,
        document_subtype TEXT,
        title TEXT,
        published_at TEXT,
        retrieved_at TEXT NOT NULL,
        content_type TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        source_mode TEXT NOT NULL,
        freshness TEXT NOT NULL,
        reliability_level TEXT NOT NULL,
        status TEXT NOT NULL,
        entity_resolution_confidence REAL NOT NULL,
        discovered_at TEXT,
        discovery_provider TEXT,
        source_independence_key TEXT,
        duplicate_of_document_id TEXT,
        normalized_text TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS ux_research_documents_canonical_url ON research_documents (canonical_url);
    CREATE UNIQUE INDEX IF NOT EXISTS ux_research_documents_content_hash ON research_documents (content_hash);

    CREATE TABLE IF NOT EXISTS research_events (
        event_id TEXT PRIMARY KEY,
        instrument_id TEXT NOT NULL,
        company_id TEXT NOT NULL,
        event_fingerprint TEXT NOT NULL UNIQUE,
        event_type TEXT NOT NULL,
        event_date TEXT,
        detected_at TEXT NOT NULL,
        title TEXT NOT NULL,
        summary TEXT NOT NULL,
        impact TEXT NOT NULL,
        time_horizon TEXT NOT NULL,
        confidence REAL NOT NULL,
        status TEXT NOT NULL,
        source_document_id TEXT NOT NULL,
        source_url TEXT NOT NULL,
        source_type TEXT NOT NULL,
        source_classification TEXT NOT NULL,
        reliability TEXT NOT NULL,
        source_mode TEXT NOT NULL,
        currency TEXT,
        monetary_value TEXT,
        monetary_original TEXT,
        percentage_value TEXT,
        percentage_original TEXT,
        customer TEXT,
        counterparty TEXT,
        location TEXT,
        capacity_value TEXT,
        capacity_unit TEXT,
        raw_evidence_reference TEXT NOT NULL,
        published_at TEXT,
        retrieved_at TEXT,
        independence_key TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (source_document_id) REFERENCES research_documents (document_id)
    );

    CREATE TABLE IF NOT EXISTS research_event_sources (
        event_source_id TEXT PRIMARY KEY,
        event_id TEXT NOT NULL,
        document_id TEXT NOT NULL,
        source_url TEXT NOT NULL,
        canonical_url TEXT NOT NULL,
        source_name TEXT NOT NULL,
        publisher TEXT,
        source_classification TEXT NOT NULL,
        evidence_excerpt TEXT NOT NULL,
        reliability TEXT NOT NULL,
        published_at TEXT,
        retrieved_at TEXT NOT NULL,
        source_mode TEXT NOT NULL,
        independent INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (event_id, document_id, evidence_excerpt),
        FOREIGN KEY (event_id) REFERENCES research_events (event_id),
        FOREIGN KEY (document_id) REFERENCES research_documents (document_id)
    );

    CREATE TABLE IF NOT EXISTS research_refresh_runs (
        refresh_run_id TEXT PRIMARY KEY,
        instrument_id TEXT NOT NULL,
        company_id TEXT NOT NULL,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        status TEXT NOT NULL,
        mode TEXT NOT NULL,
        documents_discovered INTEGER NOT NULL DEFAULT 0,
        documents_accepted INTEGER NOT NULL DEFAULT 0,
        events_extracted INTEGER NOT NULL DEFAULT 0,
        events_created INTEGER NOT NULL DEFAULT 0,
        events_updated INTEGER NOT NULL DEFAULT 0,
        deduplicated_count INTEGER NOT NULL DEFAULT 0,
        correlation_id TEXT,
        safe_error_code TEXT,
        safe_error_message TEXT,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS global_shareholding_snapshots (
        id TEXT PRIMARY KEY,
        instrument_id TEXT NOT NULL,
        period_end TEXT NOT NULL,
        filing_basis TEXT,
        source_provider TEXT NOT NULL,
        source_type TEXT NOT NULL,
        source_identity_key TEXT NOT NULL,
        source_url TEXT NOT NULL,
        research_document_id TEXT,
        published_at TEXT,
        retrieved_at TEXT NOT NULL,
        confidence TEXT NOT NULL,
        reliability_level TEXT NOT NULL,
        source_mode TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (instrument_id, source_provider, source_identity_key),
        FOREIGN KEY (research_document_id) REFERENCES research_documents (document_id)
    );
    CREATE INDEX IF NOT EXISTS idx_global_shareholding_snapshots_instrument_period
        ON global_shareholding_snapshots (instrument_id, period_end);
    CREATE INDEX IF NOT EXISTS idx_global_shareholding_snapshots_instrument_period_provider
        ON global_shareholding_snapshots (instrument_id, period_end, source_provider);

    CREATE TABLE IF NOT EXISTS global_shareholding_snapshot_values (
        id TEXT PRIMARY KEY,
        snapshot_id TEXT NOT NULL,
        category TEXT NOT NULL,
        percentage TEXT NOT NULL,
        metric_basis TEXT,
        raw_source_label TEXT,
        source_locator TEXT,
        evidence_text TEXT,
        created_at TEXT NOT NULL,
        UNIQUE (snapshot_id, category),
        CHECK (category <> 'PROMOTER_PLEDGE' OR metric_basis IS NOT NULL),
        FOREIGN KEY (snapshot_id) REFERENCES global_shareholding_snapshots (id)
    );
    CREATE TABLE IF NOT EXISTS global_manual_evidence (
        draft_id TEXT PRIMARY KEY,
        evidence_type TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        original_filename TEXT NOT NULL,
        content_type TEXT NOT NULL,
        instrument_id TEXT,
        reporting_period TEXT,
        extraction_method TEXT NOT NULL,
        extraction_results TEXT NOT NULL,
        validation_results TEXT NOT NULL,
        corrections TEXT NOT NULL,
        accepted_at TEXT NOT NULL,
        UNIQUE (content_hash, evidence_type)
    );
    CREATE INDEX IF NOT EXISTS idx_global_manual_evidence_instrument
        ON global_manual_evidence (instrument_id);
    CREATE INDEX IF NOT EXISTS idx_global_manual_evidence_content_hash
        ON global_manual_evidence (content_hash);
    CREATE TABLE IF NOT EXISTS global_financial_facts (
        instrument_id TEXT NOT NULL, metric TEXT NOT NULL, period_end TEXT NOT NULL, period_type TEXT NOT NULL, reporting_basis TEXT NOT NULL DEFAULT '', fact_value TEXT NOT NULL, unit TEXT,
        source_provider TEXT NOT NULL, source_identity TEXT NOT NULL, source_url TEXT NOT NULL, source_name TEXT NOT NULL, source_type TEXT, published_at TEXT, retrieved_at TEXT NOT NULL, confidence REAL, source_mode TEXT NOT NULL, source_tier INTEGER NOT NULL,
        PRIMARY KEY (instrument_id, metric, period_end, period_type, reporting_basis)
    );
    CREATE TABLE IF NOT EXISTS global_structured_market_snapshots (
        instrument_id TEXT NOT NULL, provider TEXT NOT NULL, provider_instrument_id TEXT, exchange TEXT, mic TEXT, currency TEXT, quote_type TEXT,
        source_url TEXT, source_name TEXT, source_type TEXT, source_identity TEXT, market_as_of TEXT, retrieved_at TEXT NOT NULL, persisted_at TEXT NOT NULL,
        last_price_at TEXT, last_valuation_at TEXT, last_fundamentals_at TEXT, last_analyst_at TEXT, last_success_at TEXT, last_provider_attempt_at TEXT,
        acquisition_status TEXT NOT NULL, last_failure_code TEXT, last_failure_message TEXT, facts_json TEXT NOT NULL,
        PRIMARY KEY (instrument_id, provider)
    );
    CREATE INDEX IF NOT EXISTS idx_structured_market_instrument ON global_structured_market_snapshots (instrument_id);
    CREATE TABLE IF NOT EXISTS global_market_price_observations (
        instrument_id TEXT NOT NULL, observed_at TEXT NOT NULL, price TEXT NOT NULL, currency TEXT, provider TEXT NOT NULL,
        source_url TEXT NOT NULL, retrieved_at TEXT NOT NULL, PRIMARY KEY (instrument_id, provider, observed_at)
    );
    CREATE INDEX IF NOT EXISTS idx_market_price_observations_lookup ON global_market_price_observations (instrument_id, observed_at);
    CREATE TABLE IF NOT EXISTS global_stock_rule_engine_results (
        global_instrument_id TEXT NOT NULL,
        rule_engine_version TEXT NOT NULL,
        input_fingerprint TEXT NOT NULL,
        calculated_at TEXT NOT NULL,
        input_as_of TEXT,
        overall_score REAL,
        quality_score REAL,
        opportunity_score REAL,
        risk_score REAL,
        confidence_score REAL NOT NULL,
        decision_signal TEXT NOT NULL,
        partial INTEGER NOT NULL,
        result_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (global_instrument_id, rule_engine_version, input_fingerprint)
    );
    CREATE INDEX IF NOT EXISTS idx_stock_rule_engine_latest
        ON global_stock_rule_engine_results (global_instrument_id, rule_engine_version, calculated_at);
    CREATE TABLE IF NOT EXISTS market_trading_schedules (
        id INTEGER PRIMARY KEY AUTOINCREMENT, market_code TEXT NOT NULL, mic TEXT, country_code TEXT, timezone TEXT NOT NULL, trading_day INTEGER NOT NULL,
        regular_open_time TEXT NOT NULL, regular_close_time TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, provenance TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (market_code, trading_day, regular_open_time, regular_close_time)
    );
    CREATE INDEX IF NOT EXISTS idx_market_trading_schedules_lookup ON market_trading_schedules (market_code, mic, enabled);
    CREATE TABLE IF NOT EXISTS market_trading_calendar_exceptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, market_code TEXT NOT NULL, trading_date TEXT NOT NULL, exception_type TEXT NOT NULL, open_time TEXT, close_time TEXT,
        reason TEXT, source_reference TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (market_code, trading_date)
    );
    CREATE INDEX IF NOT EXISTS idx_market_calendar_exceptions_lookup ON market_trading_calendar_exceptions (market_code, trading_date);
    INSERT OR IGNORE INTO market_trading_schedules (market_code,mic,country_code,timezone,trading_day,regular_open_time,regular_close_time,enabled,provenance)
    VALUES ('NSE','XNSE','IN','Asia/Kolkata',0,'09:15','15:30',1,'NSE regular session');
    INSERT OR IGNORE INTO market_trading_schedules (market_code,mic,country_code,timezone,trading_day,regular_open_time,regular_close_time,enabled,provenance)
    VALUES ('NSE','XNSE','IN','Asia/Kolkata',1,'09:15','15:30',1,'NSE regular session');
    INSERT OR IGNORE INTO market_trading_schedules (market_code,mic,country_code,timezone,trading_day,regular_open_time,regular_close_time,enabled,provenance)
    VALUES ('NSE','XNSE','IN','Asia/Kolkata',2,'09:15','15:30',1,'NSE regular session');
    INSERT OR IGNORE INTO market_trading_schedules (market_code,mic,country_code,timezone,trading_day,regular_open_time,regular_close_time,enabled,provenance)
    VALUES ('NSE','XNSE','IN','Asia/Kolkata',3,'09:15','15:30',1,'NSE regular session');
    INSERT OR IGNORE INTO market_trading_schedules (market_code,mic,country_code,timezone,trading_day,regular_open_time,regular_close_time,enabled,provenance)
    VALUES ('NSE','XNSE','IN','Asia/Kolkata',4,'09:15','15:30',1,'NSE regular session');
    """


def _daily_market_bar_from_row(row) -> DailyMarketBar:
    return DailyMarketBar(
        global_instrument_id=_required_uuid(row["global_instrument_id"], "global_daily_market_bars.global_instrument_id"),
        trading_date=row["trading_date"], open=_parse_decimal(row["open_price"]), high=_parse_decimal(row["high_price"]),
        low=_parse_decimal(row["low_price"]), close=_parse_decimal(row["close_price"]),
        previous_close=_parse_decimal(row["previous_close"]), volume=row["volume"], turnover=_parse_decimal(row["turnover"]),
        currency=row["currency"], provider=row["provider"], provider_symbol=row["provider_symbol"],
        source_mode=row["source_mode"], source_url=row["source_url"], retrieved_at=_parse_dt(row["retrieved_at"]),
    )


def _document_from_row(row: sqlite3.Row) -> ResearchDocument:
    return ResearchDocument(
        document_id=_parse_uuid(row["document_id"]) or uuid4(),
        canonical_url=row["canonical_url"],
        original_url=row["original_url"],
        title=row["title"],
        source_type=SourceType(row["source_type"]),
        source_classification=SourceClassification(row["source_classification"]),
        source_name=row["source_name"],
        publisher=None,
        published_at=_parse_dt(row["published_at"]),
        retrieved_at=_parse_dt(row["retrieved_at"]) or datetime.now(timezone.utc),
        content_type=row["content_type"],
        document_type=DocumentType(row["document_type"]),
        document_subtype=(row["document_subtype"] if "document_subtype" in row.keys() else None),
        normalized_text=row["normalized_text"],
        content_hash=row["content_hash"],
        instrument_id=_parse_uuid(row["instrument_id"]),
        company_id=_parse_uuid(row["company_id"]),
        status=DocumentStatus(row["status"]),
        reliability_level=ReliabilityLevel(row["reliability_level"]),
        entity_resolution_confidence=float(row["entity_resolution_confidence"]),
        source_mode=SourceMode(row["source_mode"]),
        freshness=row["freshness"],
        discovered_at=_parse_dt(row["discovered_at"]),
        discovery_provider=row["discovery_provider"],
        source_independence_key=row["source_independence_key"],
        duplicate_of_document_id=_parse_uuid(row["duplicate_of_document_id"]),
    )


def _structured_snapshot_from_row(row) -> StructuredMarketSnapshotRecord:
    payload = _decode_json_value(row["facts_json"])
    snapshot = StructuredMarketSnapshot.model_validate(payload)
    # Pydantic's JSON mode intentionally serializes Decimal as text. Restore
    # numeric structured values at this persistence boundary without coercing
    # descriptive facts such as sector or recommendation labels.
    for value in snapshot.facts.values():
        if isinstance(value.value, str):
            try:
                value.value = Decimal(value.value)
            except Exception:
                pass
    return StructuredMarketSnapshotRecord(
        instrument_id=_required_uuid(row["instrument_id"], "global_structured_market_snapshots.instrument_id"), provider=row["provider"],
        provider_instrument_id=row["provider_instrument_id"], exchange=row["exchange"], mic=row["mic"], currency=row["currency"], quote_type=row["quote_type"],
        source_url=row["source_url"], source_name=row["source_name"], source_type=row["source_type"], source_identity=row["source_identity"],
        market_as_of=_parse_dt(row["market_as_of"]), retrieved_at=_parse_dt(row["retrieved_at"]) or datetime.now(timezone.utc),
        persisted_at=_parse_dt(row["persisted_at"]) or datetime.now(timezone.utc), last_price_at=_parse_dt(row["last_price_at"]),
        last_valuation_at=_parse_dt(row["last_valuation_at"]), last_fundamentals_at=_parse_dt(row["last_fundamentals_at"]), last_analyst_at=_parse_dt(row["last_analyst_at"]),
        last_success_at=_parse_dt(row["last_success_at"]), last_provider_attempt_at=_parse_dt(row["last_provider_attempt_at"]), acquisition_status=row["acquisition_status"],
        last_failure_code=row["last_failure_code"], last_failure_message=row["last_failure_message"], snapshot=snapshot,
    )


def _price_observation_from_snapshot(record: StructuredMarketSnapshotRecord) -> MarketPriceObservation | None:
    value = record.snapshot.facts.get("latestPrice")
    if value is None:
        return None
    try:
        price = Decimal(str(value.value))
    except Exception:
        return None
    if price <= 0:
        return None
    observed_at = value.as_of_date or record.market_as_of or record.retrieved_at
    return MarketPriceObservation(
        instrument_id=record.instrument_id,
        observed_at=observed_at,
        price=price,
        currency=value.unit or record.currency,
        provider=record.provider,
        source_url=value.source_url or record.source_url,
        retrieved_at=record.retrieved_at,
    )


def _decode_json_value(value: Any) -> Any:
    """Accept SQLite JSON text and psycopg's already-decoded JSONB values."""
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        return json.loads(value)
    raise TypeError(f"Unsupported JSON representation: {type(value).__name__}")


def _event_from_row(row: sqlite3.Row) -> ResearchEvent:
    return ResearchEvent(
        event_id=_parse_uuid(row["event_id"]) or uuid4(),
        instrument_id=_parse_uuid(row["instrument_id"]) or uuid4(),
        company_id=_parse_uuid(row["company_id"]) or uuid4(),
        event_type=ResearchEventType(row["event_type"]),
        event_date=_parse_dt(row["event_date"]),
        detected_at=_parse_dt(row["detected_at"]) or datetime.now(timezone.utc),
        title=row["title"],
        summary=row["summary"],
        source_document_id=_parse_uuid(row["source_document_id"]) or uuid4(),
        source_url=row["source_url"],
        source_type=SourceType(row["source_type"]),
        source_classification=SourceClassification(row["source_classification"]),
        reliability=ReliabilityLevel(row["reliability"]),
        source_mode=SourceMode(row["source_mode"]),
        confidence=float(row["confidence"]),
        impact=EventImpact(row["impact"]),
        time_horizon=TimeHorizon(row["time_horizon"]),
        currency=row["currency"],
        monetary_value=_parse_decimal(row["monetary_value"]),
        monetary_original=row["monetary_original"],
        percentage_value=_parse_decimal(row["percentage_value"]),
        percentage_original=row["percentage_original"],
        customer=row["customer"],
        counterparty=row["counterparty"],
        location=row["location"],
        capacity_value=_parse_decimal(row["capacity_value"]),
        capacity_unit=row["capacity_unit"],
        status=ResearchLifecycleStatus(row["status"]),
        raw_evidence_reference=row["raw_evidence_reference"],
        published_at=_parse_dt(row["published_at"]),
        retrieved_at=_parse_dt(row["retrieved_at"]),
        independence_key=row["independence_key"],
    )


def _evidence_source_from_row(row: sqlite3.Row) -> ResearchEvidenceSource:
    return ResearchEvidenceSource(
        publisher=row["publisher"],
        url=row["source_url"],
        source_type=SourceClassification(row["source_classification"]),
        published_at=_parse_dt(row["published_at"]),
        retrieved_at=_parse_dt(row["retrieved_at"]) or datetime.now(timezone.utc),
        reliability=ReliabilityLevel(row["reliability"]),
        source_mode=SourceMode(row["source_mode"]),
        document_id=_parse_uuid(row["document_id"]) or uuid4(),
        source_name=row["source_name"],
        canonical_url=row["canonical_url"],
        independent=bool(row["independent"]),
    )


def _event_fingerprint(event: ResearchEvent) -> str:
    return "|".join(
        [
            str(event.instrument_id),
            str(event.event_type),
            event.raw_evidence_reference.strip().lower(),
            event.monetary_original or "",
            event.customer or "",
            event.counterparty or "",
        ]
    )


def _assert_global_score_public(result: Mapping[str, Any]) -> None:
    """Fail closed if private portfolio context reaches a global score record."""
    forbidden = {
        "portfolioid", "portfolio_id", "quantity", "averagecost", "average_cost",
        "costbasis", "cost_basis", "investedamount", "invested_amount", "pnl",
        "allocation", "positionid", "position_id",
    }

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                if str(key).casefold() in forbidden:
                    raise ValueError(f"PRIVATE_PORTFOLIO_FIELD_IN_GLOBAL_SCORE:{key}")
                visit(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                visit(nested)

    visit(result)


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse_sql_time(value: Any) -> time:
    """Accept SQLite text and psycopg PostgreSQL TIME values without coercion."""
    if isinstance(value, time):
        return value
    if isinstance(value, str):
        return time.fromisoformat(value)
    raise TypeError(f"Unsupported SQL TIME representation: {type(value).__name__}")


def _uuid(value: UUID | None) -> str | None:
    return str(value) if value else None


def _decimal(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    # V2 shareholding columns are PostgreSQL TIMESTAMP values, while the
    # research domain convention (and V1) is UTC-aware datetimes. PostgreSQL
    # therefore hydrates those V2 values as naïve even though the application
    # wrote them as UTC. Attach UTC at this persistence boundary; never strip
    # timezone information from aware timestamps.
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_uuid(value: Any) -> UUID | None:
    if isinstance(value, UUID):
        return value
    return UUID(str(value)) if value else None


def _required_uuid(value: Any, field: str) -> UUID:
    parsed = _parse_uuid(value)
    if parsed is None:
        raise ValueError(f"Missing required UUID: {field}")
    return parsed


def _parse_decimal(value: Any) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


def _financial_fact_from_row(row: Any) -> FinancialFact:
    return FinancialFact(FinancialFactKey(_required_uuid(row["instrument_id"], "global_financial_facts.instrument_id"), row["metric"], row["period_end"] or None, row["period_type"], row["reporting_basis"] or None), ProvenancedValue(value=Decimal(str(row["fact_value"])), unit=row["unit"], source_url=row["source_url"], source_name=row["source_name"], source_type=row["source_type"], published_at=_parse_dt(row["published_at"]), retrieved_at=_parse_dt(row["retrieved_at"]) or datetime.now(timezone.utc), confidence=row["confidence"]), FactSourceTier(int(row["source_tier"])), row["source_provider"], row["source_identity"], SourceMode(row["source_mode"]))
