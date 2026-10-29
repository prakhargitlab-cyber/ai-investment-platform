"""Explicit bounded ETF ingestion using the existing acquisition and bar writers."""
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from uuid import UUID

from app.etf_acquisition import EtfAcquisitionService
from app.etf_evidence import EtfMetric
from app.etf_readiness import evaluate_etf_readiness
from app.nse_historical_daily import verified_identity

logger = logging.getLogger(__name__)
MAX_ETF_INGESTION_CANDIDATES = 5


async def canonical_etfs(orchestrator, instrument_ids, *, identity_headers, correlation_id=None):
    if not 1 <= len(instrument_ids) <= MAX_ETF_INGESTION_CANDIDATES or len(set(instrument_ids)) != len(instrument_ids):
        raise ValueError("ETF_CANDIDATE_LIMIT_OR_DUPLICATES")
    rows = []
    for key in instrument_ids:
        row = await orchestrator.global_instrument_metadata(key, identity_headers=identity_headers,
                                                            correlation_id=correlation_id)
        if row.get("assetType") != "ETF":
            raise ValueError("CANONICAL_ACTIVE_NSE_ETF_REQUIRED")
        symbol, _ = verified_identity(row, key)
        if symbol != row.get("primarySymbol") or not row.get("isin"):
            raise ValueError("ETF_CANONICAL_LISTING_CONFLICT")
        rows.append(row)
    return rows


async def ingest_quotes(repository, orchestrator, rows, client, *, clock=None, fence=None):
    """Bounded NSE-first batch: list + market snapshot + at most five metadata GETs.

    Existing Yahoo acquisition remains separately available, never an implicit
    fallback. Only canonical IDs are persisted; no cycles or publication here.
    """
    if (not 1 <= len(rows) <= MAX_ETF_INGESTION_CANDIDATES
            or len({row["globalInstrumentId"] for row in rows}) != len(rows)):
        raise ValueError("ETF_CANDIDATE_LIMIT_OR_DUPLICATES")
    async def persisted(operation):
        def locked():
            with repository._persistence_worker_lock:
                return operation()
        return await repository._run_blocking_persistence(locked)

    service = EtfAcquisitionService(repository.persistence, run_blocking=persisted, clock=clock, fence=fence)
    ids = [UUID(str(row["globalInstrumentId"])) for row in rows]
    discovery = await service.discover_nse(client, master_rows=rows, instrument_ids=ids)
    if discovery.outcome != "SUCCESS_WITH_DATA":
        return {"status": "BLOCKED", "discovery": discovery.model_dump(mode="json"), "instruments": []}
    from app.nse_etf_market import MARKET_URL, META_URL, get_json, market_snapshot, verify_metadata, evidence, failure_code
    from app.models import EtfResearchProfile
    from app.etf_evidence import EtfAcquisitionOutcome as Outcome
    results = []
    market_error = None
    requested_symbols = {str(row["primarySymbol"]).strip().upper() for row in rows}
    try:
        payload = await get_json(client, MARKET_URL)
        stamp, market_rows, envelope = market_snapshot(payload, service.discovered_listings, service.clock(),
                                                       requested_symbols=requested_symbols)
    except Exception as exc:
        # Never persist arbitrary exception text (HTTP errors can contain secrets).
        market_error = failure_code(exc)
    for key, row in zip(ids, rows):
        attempts = []
        failure = market_error
        if failure is None:
            try:
                meta = await get_json(client, META_URL, params={"functionName": "getMetaData", "symbol": row["primarySymbol"]})
                verify_metadata(meta, row)
                listing = next(item for item in service.discovered_listings if item.instrument_id == key)
                attempts.append(await service._record_async(key, EtfMetric.IDENTITY, "NSE", [listing],
                    reason="NSE_METADATA_IDENTITY_VERIFIED"))
                quote = market_rows[row["primarySymbol"]]
            except Exception as exc:
                failure = failure_code(exc)
                if failure == "NSE_METADATA_IDENTITY_CONFLICT":
                    attempts.append(await service._record_async(key, EtfMetric.IDENTITY, "NSE",
                        outcome=Outcome.TECHNICAL_FAILURE, reason=failure))
        profile = EtfResearchProfile(instrument_id=key, fund_id=key, fund_name=row["canonicalName"],
            ticker=row["primarySymbol"], exchange="NSE", mic="XNSE", isin=row["isin"], currency=row["currency"])
        for metric in (EtfMetric.MARKET_PRICE, EtfMetric.TRADING_VOLUME, EtfMetric.NAV):
            try:
                if failure:
                    raise ValueError(failure)
                fact = evidence(profile, metric, quote, stamp, envelope, meta, service.clock())
                attempt = await service._record_async(key, metric, "NSE", [fact] if fact else [],
                    reason="NSE_SESSION_OBSERVATION" if fact else "FIELD_ABSENT_OR_NO_SESSION_TRADE")
            except Exception as exc:
                reason = "NSE_ACQUISITION_FAILED:" + (failure or failure_code(exc))
                if metric == EtfMetric.NAV and failure is None:
                    # Diagnostic only, no behavior change: numeric() in
                    # nse_etf_market.py already treats None/""/"-" as a
                    # legitimately absent value (returns None, no raise),
                    # so an exception here means the live field held some
                    # OTHER representation. Logs the symbol (public ticker,
                    # not a secret) and a length-capped repr of only the
                    # "nav" field itself -- never the full row/quote, no
                    # price/volume payload, no credentials -- so the actual
                    # marker can be identified on a future approved run
                    # without guessing a parser change now.
                    logger.warning("etf_nav_parse_rejected symbol=%s raw_nav=%s",
                                    row.get("primarySymbol"), repr(quote.get("nav"))[:80] if quote else None)
                attempt = await service._record_async(key, metric, "NSE", outcome=Outcome.TECHNICAL_FAILURE,
                    reason=reason)
            attempts.append(attempt)
        statuses, summary = await persisted(lambda: evaluate_etf_readiness(repository.persistence, key, now=service.clock()))
        success = summary.ready_for_analysis and all(a.outcome == "SUCCESS_WITH_DATA" for a in attempts if a.metric in {"MARKET_PRICE", "TRADING_VOLUME"})
        results.append({"instrument_id": str(key), "status": "READY" if success else "BLOCKED",
                        "attempts": [a.model_dump(mode="json") for a in attempts],
                        "readiness": [asdict(s) for s in statuses], "summary": asdict(summary)})
    return {"status": "COMPLETED" if all(r["status"] == "READY" and all(a["outcome"] != "TECHNICAL_FAILURE" for a in r["attempts"]) for r in results) else "PARTIAL_FAILURE",
            "discovery": discovery.model_dump(mode="json"), "instruments": results}


async def ingest_history(jobs, rows, *, start, end, identity_headers, correlation_id=None):
    if start > end or end > datetime.now(timezone.utc).date():
        raise ValueError("INVALID_DATE_RANGE")
    if (end - start).days + 1 > min(31, jobs.settings.nse_historical_request_window_days):
        raise ValueError("REQUEST_WINDOW_EXCEEDED")
    results = []
    for row in rows:
        result = await jobs.populate_daily_bars(UUID(str(row["globalInstrumentId"])), start=start, end=end,
                                              identity_headers=identity_headers, correlation_id=correlation_id)
        results.append({"instrument_id": str(result.global_instrument_id), "status": result.status,
                        "failure_reason": result.failure_reason, "persisted_rows": result.persisted_rows,
                        "first_trading_date": result.first_trading_date, "last_trading_date": result.last_trading_date,
                        "source_url": result.source_url, "rejection_reasons": result.rejection_reasons})
    return {"status": "COMPLETED" if all(r["status"] == "SUCCESS" and not r["rejection_reasons"] for r in results) else "PARTIAL_FAILURE",
            "instruments": results}


def etf_preflight(store, rows, *, now=None):
    """Read persisted evidence only; no provider, acquisition, or lifecycle writes."""
    now = now or datetime.now(timezone.utc)
    if not 1 <= len(rows) <= MAX_ETF_INGESTION_CANDIDATES:
        raise ValueError("ETF_CANDIDATE_LIMIT_OR_DUPLICATES")
    listings = store.etf_listings()
    instruments = []
    for row in rows:
        key = UUID(str(row["globalInstrumentId"]))
        statuses, summary = evaluate_etf_readiness(store, key, now=now)
        identities = {(item.isin, item.symbol, item.exchange) for item in listings if item.instrument_id == key}
        identity_matches = identities == {(row["isin"], row["primarySymbol"], "NSE")}
        ready = identity_matches and summary.ready_for_analysis
        instruments.append({"instrument_id": str(key), "symbol": row["primarySymbol"], "isin": row["isin"],
            "canonical_identity_matches": identity_matches, "mandatory_evidence_ready": ready,
            "blocking_reasons": (["CANONICAL_LISTING_MISMATCH"] if not identity_matches else []) +
                [s.reason or s.status.value for s in statuses if s.role == "MANDATORY" and s.status != "READY_FRESH"],
            "readiness": [asdict(s) for s in statuses], "summary": asdict(summary)})
    return {"status": "READY" if all(x["mandatory_evidence_ready"] for x in instruments) else "BLOCKED",
            "scope": "BOUNDED_CANONICAL_ETF", "evaluated_at": now.isoformat(), "candidate_count": len(rows),
            "ready_count": sum(x["mandatory_evidence_ready"] for x in instruments), "instruments": instruments}
