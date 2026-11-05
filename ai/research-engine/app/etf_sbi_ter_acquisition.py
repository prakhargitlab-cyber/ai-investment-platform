"""Bounded, fail-closed SBI Mutual Fund TER acquisition for the normal ETF Radar cycle.

One workbook GET + one AMFI scheme-master GET per cycle (NAVAll is reused from the fund-identity step). Identity is the
conservative exact-name rule in ``etf_identity_resolution`` (verified AMC, ETF category, unique AMFI name, unique
canonical ETF, ISIN confirmed in both AMFI files). Only Regular Plan Total TER is persisted, as official-fund evidence
with source URL, SHA-256, TER date, unit and retrieval time. Everything else (other AMCs, AUM, unmatched or withheld
rows) stays unavailable and is recorded as an explicit attempt, never as a value.
Any source problem is bounded to this source and never fails the cycle.
"""
from __future__ import annotations

import asyncio
import tempfile
from collections import Counter
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from uuid import UUID

from app.cycle_checkpoint import CycleOwnershipLost
from app.etf_amfi_navall import AmfiNavAllIndex
from app.etf_amfi_scheme_master import parse_amfi_scheme_master
from app.etf_evidence import EtfAcquisitionAttempt, EtfAcquisitionOutcome, EtfMetric
from app.etf_phase_progress import record_etf_phase
from app.etf_identity_resolution import build_amc_pairing, build_name_based_code_resolver, reconcile_isin
from app.sbi_ter_workbook_adapter import AdapterOutcome, BoundedSbiTerAdapter, build_staged_observations

SBI_TER_URL = "https://www.sbimf.com/docs/default-source/ter_allschemes/current-year-ter.xlsx"
AMFI_SCHEME_MASTER_URL = "https://portal.amfiindia.com/DownloadSchemeData_Po.aspx?mf=0"
SBI_AMC = "SBI Funds Management Limited"      # AMFI scheme-master AMC name; pairing to NAVAll is verified from data
MAX_WORKBOOK_BYTES = 8 * 1024 * 1024
MAX_MASTER_BYTES = 16 * 1024 * 1024
PROVIDER = "SBI_MUTUAL_FUND"


class _Recorder:
    """Collects the adapter's writes so they can be persisted through the cycle's fenced, blocking-safe path."""

    def __init__(self, existing):
        self._existing = existing    # preloaded on the caller's thread: store connections are thread-bound
        self.saved: list[tuple[list, EtfAcquisitionAttempt]] = []

    def etf_facts(self, instrument_id, metric):
        return self._existing.get(instrument_id, [])

    def save_etf_acquisition(self, evidence, attempt):
        self.saved.append((list(evidence), attempt))


async def _get(client, url, *, limit, accept):
    response = await client.get(url, headers={"Accept": accept}, timeout=60.0)
    response.raise_for_status()
    if len(response.content) > limit:
        raise ValueError("RESPONSE_TOO_LARGE")
    modified = response.headers.get("last-modified")
    published = None
    if modified:
        from email.utils import parsedate_to_datetime
        published = parsedate_to_datetime(modified)
    return response.content, published


def _unavailable(instrument: UUID, at: datetime, reason: str) -> EtfAcquisitionAttempt:
    return EtfAcquisitionAttempt(instrument_id=instrument, metric=EtfMetric.EXPENSE_RATIO.value, provider=PROVIDER,
                                 attempted_at=at, outcome=EtfAcquisitionOutcome.SUCCESS_EMPTY, reason=reason)


async def acquire_sbi_ter(repository, selection, checkpoint, client, now, navall: AmfiNavAllIndex | None, *,
                          workbook_fetcher=None, master_fetcher=None) -> dict:
    """Returns a diagnostic dict (never raises for source/identity problems)."""
    import httpx
    retrieved = now()
    fence = (checkpoint.cycle_id, checkpoint.owner_id)
    items = [i for i in selection["universe"] if i["canonical"]]
    diagnostic: dict = {"source": PROVIDER, "status": "FAILED", "ingested": 0, "unresolved": 0, "withheld": 0}
    try:
        if navall is None:
            raise ValueError("AMFI_NAVALL_UNAVAILABLE")
        owned = None
        if workbook_fetcher is None or master_fetcher is None:
            owned = client or httpx.AsyncClient(timeout=60.0, follow_redirects=False)
        try:
            workbook, published = await (workbook_fetcher() if workbook_fetcher else
                                         _get(owned, SBI_TER_URL, limit=MAX_WORKBOOK_BYTES, accept="*/*"))
            master_bytes, _ = await (master_fetcher() if master_fetcher else
                                     _get(owned, AMFI_SCHEME_MASTER_URL, limit=MAX_MASTER_BYTES, accept="text/csv"))
        finally:
            if owned is not None and client is None:
                await owned.aclose()
        if not workbook.startswith(b"PK\x03\x04"):
            raise ValueError("SBI_TER_WORKBOOK_SIGNATURE_INVALID")
        master = parse_amfi_scheme_master(master_bytes.decode("utf-8") if isinstance(master_bytes, bytes) else master_bytes)
        digest = sha256(workbook).hexdigest()

        pairing = build_amc_pairing(navall, master)
        isin_counts = Counter(i["canonical"]["isin"] for i in items)
        duplicates = frozenset(isin for isin, n in isin_counts.items() if n > 1)
        # canonical ETFs whose ISIN is verified in BOTH AMFI files; nothing else can receive AMFI-derived identity
        canonical = {i["canonical"]["isin"]: UUID(str(i["canonical"]["globalInstrumentId"])) for i in items
                     if reconcile_isin(i["canonical"]["isin"], navall, master, pairing).accepted}

        existing = {instrument: await repository._run_blocking_persistence(
            repository.persistence.etf_facts, instrument, EtfMetric.EXPENSE_RATIO) for instrument in canonical.values()}

        def stage_and_acquire():
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "current-year-ter.xlsx"
                path.write_bytes(workbook)
                names = {o.scheme_code: o.scheme_name for o in build_staged_observations(path)}
                resolver = build_name_based_code_resolver(names, amc=SBI_AMC, navall=navall, master=master,
                                                          canonical_by_isin=canonical, canonical_duplicate_isins=duplicates)
                extra = {}
                for code, decision in resolver.decisions.items():
                    if decision.accepted:
                        extra[code] = {"identity_confidence": str(decision.confidence), "amfi_scheme_code": decision.scheme_code,
                                       "isin": decision.isin, "amfi_amc": decision.amc, "workbook_sha256": digest,
                                       "amfi_category": decision.category}
                recorder = _Recorder(existing)
                adapter = BoundedSbiTerAdapter(resolver, max_instruments=max(len(canonical), 1))
                results = adapter.acquire(recorder, path, source_identity=f"sbi-ter-sha256:{digest}",
                                          retrieved_at=retrieved, published_at=published, extra_metadata=extra)
                return results, recorder.saved, resolver.decisions
        results, saved, decisions = await asyncio.to_thread(stage_and_acquire)

        ingested: set[UUID] = set()
        for evidence, attempt in saved:
            await repository._run_blocking_persistence(repository.persistence.save_etf_acquisition, evidence, attempt, fence=fence)
            ingested.add(attempt.instrument_id)
        withheld = {r.instrument_id: r.detail for r in results if r.outcome is AdapterOutcome.WITHHELD_IMPLAUSIBLE_VALUE}
        already = {r.instrument_id for r in results if r.outcome in (AdapterOutcome.ALREADY_INGESTED,)}
        conflict = {r.instrument_id for r in results if r.outcome is AdapterOutcome.CONFLICTING_OFFICIAL_METRIC_FOR_PERIOD}
        # explicit "unavailable" for canonical SBI ETFs that received nothing, with the reason
        sbi_isins = {isin for isin, scheme in master.by_isin.items() if scheme.amc == SBI_AMC}
        for item in items:
            row = item["canonical"]
            instrument = UUID(str(row["globalInstrumentId"]))
            if row["isin"] not in sbi_isins:
                continue
            if instrument in ingested or instrument in already:
                outcome = "SUCCEEDED"
            else:
                reason = (f"SOURCE_VALUE_WITHHELD:{withheld[instrument]}" if instrument in withheld else
                          "CONFLICTING_OFFICIAL_METRIC_FOR_PERIOD" if instrument in conflict else
                          "NOT_IN_SBI_TER_WORKBOOK_UNDER_AN_EXACT_VERIFIED_NAME")
                await repository._run_blocking_persistence(repository.persistence.save_etf_acquisition, [],
                                                           _unavailable(instrument, retrieved, reason), fence=fence)
                outcome = "SKIPPED"
            await record_etf_phase(checkpoint, selection, "FUNDAMENTAL_ACQUISITION", events=[(row["isin"], outcome)])
        diagnostic.update(status="OK", ingested=len(ingested), already_ingested=len(already), withheld=len(withheld),
                          unresolved=sum(1 for r in results if r.outcome is AdapterOutcome.IDENTITY_UNRESOLVED_STAGED_ONLY),
                          sha256=digest)
    except (CycleOwnershipLost, asyncio.CancelledError):
        raise
    except Exception as error:  # bounded to this source; the cycle continues without TER
        diagnostic.update(status="FAILED", failure=f"SBI_TER_{type(error).__name__}:{str(error)[:120]}")
    selection.setdefault("sourceDiagnostics", {})[PROVIDER] = diagnostic
    return diagnostic
