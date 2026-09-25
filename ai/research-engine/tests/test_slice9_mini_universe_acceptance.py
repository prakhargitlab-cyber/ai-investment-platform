"""Slice 9: deterministic FRESH-DB mini-universe acceptance on the production
orchestration path (worker -> run_global_opportunity_cycle -> discovery-v2
orchestrator -> fenced publication). Offline; empty research state; a crash and
restart on a new pod; default Stage-2 concurrency.

Per-instrument case plan (instrument n -> CASES[n % len(CASES)]). Each deep
attempt of a case returns one step of its script; failures carry the exact
DI-20H.4 reason. Filings are real synthetic documents ingested through the
production prepare -> apply -> persist path.
"""
from __future__ import annotations

import asyncio
import gc
import weakref
from collections import Counter
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

import app.deep_investigation as deep_investigation
from app import global_opportunity_cycle as cycle
from app.cycle_checkpoint import CandidateState, PHASE_BASELINE, PHASE_DEEP
from app.global_opportunity_orchestration import GlobalOpportunityOrchestrator
from app.global_scanner import GlobalScanner
from app.models import DocumentStatus, ReliabilityLevel, SourceClassification, SourceMode, SourceType
from app.opportunity_worker import OpportunityCycleWorker
from app.persistence import SqliteResearchPersistence
from app.portfolio_orchestration import PortfolioResearchOrchestrator
from app.repository import ResearchRepository
from app.research_fetching import FetchError
from app.research_readiness import ResearchRequirementStatus as S
from app.research_readiness_runtime import BASELINE_REQUIREMENT_IDS, TargetedEnsureResult
from app.settings import Settings
from test_cycle_checkpoint_recovery import SimulatedCrash, _fact
from test_global_opportunity_baseline import _make_runtime
from test_global_opportunity_empty_universe import _Clock
from test_global_opportunity_ranker import inputs
from test_global_scanner import NOW, instrument, persisted
from test_slice1_document_retention import _pdf_body
from test_stock_rule_engine import _readiness

READY = None
# case -> list of per-attempt outcomes: READY, or (blocking requirement, DI-20H.4 reason)
CASES = {
    "STRUCTURED_SUCCESS": [READY],
    "FINANCIAL_PDF": [READY],
    "DUPLICATE_FILING": [READY],
    "IRRELEVANT_FILING": [READY],
    "PARSER_FAILURE_THEN_SUCCESS": [("QUARTERLY_FINANCIALS", "PARSER_FAILED"), READY],
    "EXTRACTION_TIMEOUT_THEN_SUCCESS": [("QUARTERLY_FINANCIALS", "PDF_EXTRACTION_TIMEOUT"), READY],
    "PERSISTENCE_FAILURE_THEN_SUCCESS": [READY],
    "GOVERNANCE_FALLBACK": [("GOVERNANCE_HISTORY", "DISCOVERY_QUERY_BUDGET_EXHAUSTED"), READY],
    "ROBOTS_BLOCKED": [("QUARTERLY_FINANCIALS", "ROBOTS_OR_ACCESS_BLOCKED")],
    "STALE_PRICE": [("LATEST_PRICE", "EXTERNAL_RESULT_STALE"), READY],
    "SLOW_PROVIDER": [READY],
    "GENUINE_UNAVAILABLE": [("QUARTERLY_FINANCIALS", "DISCOVERY_NO_FINANCIAL_RESULTS_CANDIDATES")],
    "REPAIRABLE_TRANSIENT": [("VALUATION_INPUTS", "NETWORK_TIMEOUT"), READY],
    "PERSISTENT_TECHNICAL": [("VALUATION_INPUTS", "NETWORK_TIMEOUT")],
}
NAMES = list(CASES)
COUNT = 84          # 6 instruments per case
CRASH_AT = 50       # deep investigation that dies with the first pod


def case_of(key: UUID) -> str:
    return NAMES[key.int % len(NAMES)]


class Universe:
    def __init__(self):
        self.store = SqliteResearchPersistence()
        self.rows = [instrument(n) for n in range(1, COUNT + 1)]
        for row in self.rows:
            persisted(self.store, row)
        self.pairs = {UUID(int=n): inputs(n, core=55 + n % 40) for n in range(1, COUNT + 1)}
        self.calls = {k: Counter() for k in ("baseline", "deep", "provider", "evaluated", "cache_hits", "ingested", "reused")}
        self.peak_active = 0
        self.active = 0
        self.structures: list[weakref.ref] = []
        self.crashed = False
        self.persist_failed_once: set = set()
        self.repos: list[ResearchRepository] = []


def build_process(u: Universe, monkeypatch):
    repo = ResearchRepository(persistence=u.store)
    u.repos.append(repo)
    original_upsert = u.store.upsert_document

    def flaky_upsert(document):
        key = document.instrument_id
        if key and case_of(key) == "PERSISTENCE_FAILURE_THEN_SUCCESS" and key not in u.persist_failed_once:
            u.persist_failed_once.add(key)
            raise RuntimeError("transient persistence failure")
        return original_upsert(document)
    monkeypatch.setattr(u.store, "upsert_document", flaky_upsert)
    hydrator = PortfolioResearchOrchestrator(repo, Settings(), client=object())
    runtime = _make_runtime(repo)
    service = GlobalOpportunityOrchestrator(repo, u.store, profile_hydrator=hydrator.register_global_profile_metadata,
                                            readiness_runtime=runtime, clock=lambda: NOW)
    monkeypatch.setattr(GlobalScanner, "enrich_candidates", lambda self, scan, **kw: [
        u.pairs[c.global_instrument_id][0] for c in reversed(scan.candidates) if c.eligible_for_deep_analysis])
    service.readiness.read = AsyncMock(return_value=_readiness())

    async def ensure(key, *, requirement_ids=None, **kwargs):
        if requirement_ids is not None and set(requirement_ids) == BASELINE_REQUIREMENT_IDS:
            u.calls["baseline"][key] += 1
        return TargetedEnsureResult(_readiness(), (), ())
    service.readiness.ensure = ensure

    async def analyze(profile, readiness, **kwargs):
        key = profile.instrument_id
        rule = u.pairs[key][1]
        if await repo.stock_rule_engine_result(key, rule.rule_engine_version, rule.input_fingerprint) is not None:
            u.calls["cache_hits"][key] += 1
            return rule.model_copy(update={"cache_hit": True})
        u.calls["evaluated"][key] += 1
        await repo.persist_stock_rule_engine_result(rule.model_dump(mode="json"))
        return rule
    service.rule_engine.analyze = analyze
    monkeypatch.setattr(cycle, "GlobalOpportunityOrchestrator", lambda *a, **kw: service)
    source = SimpleNamespace(active_global_equities=AsyncMock(return_value=u.rows),
                             sector_benchmark_contexts=AsyncMock(return_value={}),
                             register_global_profile_metadata=service.profile_hydrator)
    return repo, partial(cycle.run_global_opportunity_cycle, repo, source, readiness_runtime=service.readiness)


async def _ingest(u, repo, key, suffix, body):
    url = f"https://nsearchives.nseindia.com/corporate/F{key.int}_{suffix}.pdf"
    if url in repo.document_urls_for(key):
        # Production reuse (repository._reusable_official_document): a durable
        # filing is never downloaded or parsed again.
        u.calls["reused"][key] += 1
        return SimpleNamespace(status=DocumentStatus.DUPLICATE)
    document = repo._prepare_ingested_document(
        original_url=url,
        source_type=SourceType.EXCHANGE_ANNOUNCEMENT, source_name="NSE", publisher="NSE", content_type="application/pdf",
        body=body, reliability=ReliabilityLevel.LEVEL_A, published_at=None, source_mode=SourceMode.REAL,
        source_classification=SourceClassification.OTHER, discovered_at=None, discovery_provider=None,
        expected_profile=None, document_status=DocumentStatus.PARSED, allow_empty_content=False,
        trusted_profile_identity=None)
    document.instrument_id = key
    u.structures.append(weakref.ref(document.pdf_structure))
    stored = await repo._apply_prepared_ingested_document_async(document, document_status=DocumentStatus.PARSED)
    u.calls["ingested"][key] += 1
    return stored


@pytest.fixture
def universe(monkeypatch):
    monkeypatch.setattr(cycle, "datetime", _Clock)
    original = deep_investigation.investigate
    u = Universe()
    deep_seen: list = []

    async def investigate(runtime, key, **kwargs):
        repo = u.repos[-1]
        u.calls["deep"][key] += 1
        if key not in deep_seen:
            deep_seen.append(key)
        if not u.crashed and deep_seen.index(key) + 1 == CRASH_AT and u.calls["deep"][key] == 1:
            u.crashed = True
            raise SimulatedCrash("pod died")
        u.active += 1
        u.peak_active = max(u.peak_active, u.active)
        try:
            case = case_of(key)
            await asyncio.sleep(0.05 if case == "SLOW_PROVIDER" else 0.002)
            attempt = u.calls["deep"][key] - 1 - (1 if (u.crashed and deep_seen.index(key) + 1 == CRASH_AT) else 0)
            script = CASES[case]
            step = script[min(attempt, len(script) - 1)]
            if step is READY and not u.store.load_financial_facts({key}):
                u.calls["provider"][key] += 1
                u.store.reconcile_financial_facts_for_source(key, f"fixture-{key}", [_fact(key)])
            if case in {"FINANCIAL_PDF", "PERSISTENCE_FAILURE_THEN_SUCCESS", "DUPLICATE_FILING"} and step is READY:
                try:
                    await _ingest(u, repo, key, "results", _pdf_body(key.int, lines=200))
                except FetchError:
                    step = ("QUARTERLY_FINANCIALS", "DOCUMENT_PERSIST_FAILED")
                if case == "DUPLICATE_FILING" and step is READY:
                    duplicate = await _ingest(u, repo, key, "results_copy", _pdf_body(key.int, lines=200))
                    assert duplicate.status == DocumentStatus.DUPLICATE
            if case == "IRRELEVANT_FILING":
                await _ingest(u, repo, key, "trading_window", "[PDF_PAGE 1]\nClosure of trading window for designated persons.\n" * 3)
            result, plan, matrix = await original(runtime, key, **kwargs)
            if step is READY:
                return result, plan, matrix
            requirement, reason = step
            blocked = _readiness(overrides={requirement: S.MISSING}, critical_pct=50)
            return TargetedEnsureResult(blocked, (), (), failures={requirement: reason}), plan, matrix
        finally:
            u.active -= 1
    monkeypatch.setattr(deep_investigation, "investigate", investigate)
    return u


def _classify(u, cycle_id, selection):
    """Every instrument of the applicable universe -> exactly one category."""
    deep = {k[1]: v for k, v in u.store.cycle_progress(cycle_id).items() if k[0] == PHASE_DEEP}
    base = {k[1]: v for k, v in u.store.cycle_progress(cycle_id).items() if k[0] == PHASE_BASELINE}
    diagnostics = {}
    for d in selection["diagnostics"]:
        diagnostics.setdefault(d["global_instrument_id"], []).append(d)
    categories = Counter()
    per_instrument = {}
    for row in u.rows:
        key = row["globalInstrumentId"]
        if key in deep:
            state, disposition = deep[key]["state"], deep[key]["disposition"]
            label = f"{state}:{disposition}"
        elif key in base and base[key]["state"] != CandidateState.COMPLETED:
            label = f"BASELINE_{base[key]['state']}:{base[key]['disposition']}"
        elif key in diagnostics:
            label = f"DIAGNOSTIC:{diagnostics[key][-1]['disposition']}"
        else:
            label = "UNACCOUNTED"
        per_instrument[key] = label
        categories[label] += 1
    return categories, per_instrument


@pytest.mark.asyncio
async def test_fresh_db_mini_universe_acceptance(universe, monkeypatch, capsys):
    u = universe
    assert u.store.load_documents() == [] and u.store.recommendation_states() == []  # EMPTY research state
    # ---- pod 1 runs until it dies mid-Stage-2 ---------------------------------
    repo1, runner1 = build_process(u, monkeypatch)
    worker1 = OpportunityCycleWorker(repo1, runner1, lease_seconds=0.3, poll_seconds=0.05)
    job = worker1.submit({"top_n": 4, "shortlist_limit": 100})
    with pytest.raises(SimulatedCrash):
        await asyncio.wait_for(worker1.task, 120)
    cycle_id = job["cycle_id"]
    assert u.store.cycle_run(cycle_id)["status"] == "RUNNING"
    completed_before = {k[1] for k, v in u.store.cycle_progress(cycle_id).items()
                        if k[0] == PHASE_DEEP and v["state"] in (CandidateState.COMPLETED, CandidateState.EVIDENCE_UNAVAILABLE)}
    deep_before = Counter(u.calls["deep"])
    await asyncio.sleep(0.4)
    # ---- pod 2 (fresh process state, same DB) resumes the SAME cycle ----------
    repo2, runner2 = build_process(u, monkeypatch)
    worker2 = OpportunityCycleWorker(repo2, runner2, lease_seconds=5, poll_seconds=0.05)
    worker2.start()
    await asyncio.wait_for(worker2.queue.join(), 120)
    await worker2.close()

    run = u.store.cycle_run(cycle_id)
    finished = next(j for j in u.store.opportunity_jobs() if j["cycle_id"] == cycle_id)
    assert run["status"] == "COMPLETED" and run["resume_count"] == 1
    assert finished["status"] == "COMPLETED" and finished["result_cycle_id"] == cycle_id  # same-cycle recovery
    selection = u.store.cycle_published_selection(cycle_id)

    # ---- exact accounting ------------------------------------------------------
    categories, per_instrument = _classify(u, cycle_id, selection)
    assert categories["UNACCOUNTED"] == 0
    assert sum(categories.values()) == len(u.rows) == COUNT

    # ---- per case expectations ---------------------------------------------------
    by_case = {}
    for key, label in per_instrument.items():
        by_case.setdefault(case_of(UUID(key)), Counter())[label] += 1
    analyzed = {"COMPLETED:ANALYZED", "COMPLETED:RANK_FILTERED"}
    for case in ("STRUCTURED_SUCCESS", "FINANCIAL_PDF", "DUPLICATE_FILING", "IRRELEVANT_FILING",
                 "PARSER_FAILURE_THEN_SUCCESS", "EXTRACTION_TIMEOUT_THEN_SUCCESS", "PERSISTENCE_FAILURE_THEN_SUCCESS",
                 "GOVERNANCE_FALLBACK", "STALE_PRICE", "SLOW_PROVIDER", "REPAIRABLE_TRANSIENT"):
        assert set(by_case[case]) <= analyzed, (case, by_case[case])
    assert set(by_case["ROBOTS_BLOCKED"]) == {"EVIDENCE_UNAVAILABLE:DEEP_READINESS_NOT_MET"}
    assert set(by_case["GENUINE_UNAVAILABLE"]) == {"EVIDENCE_UNAVAILABLE:DEEP_READINESS_NOT_MET"}
    assert set(by_case["PERSISTENT_TECHNICAL"]) == {"RETRYABLE_FAILURE:DEEP_READINESS_NOT_MET"}  # explicit, not rejection
    diag = {d["global_instrument_id"]: d for d in selection["diagnostics"] if d.get("disposition")}
    for key, label in per_instrument.items():
        if case_of(UUID(key)) == "PERSISTENT_TECHNICAL":
            assert diag[key]["failure_class"] == "TECHNICAL_RETRYABLE"
            assert diag[key]["acquisition_failures"] == {"VALUATION_INPUTS": "NETWORK_TIMEOUT"}

    # ---- satisfied candidates reach the Rule Engine; nothing evaluated twice ----
    satisfied = [k for k, label in per_instrument.items() if label in analyzed]
    assert all(u.calls["evaluated"][UUID(k)] == 1 for k in satisfied)
    assert sum(u.calls["evaluated"].values()) == len(satisfied)
    # ---- restart: completed candidates not re-investigated ------------------------
    assert all(u.calls["deep"][UUID(k)] == deep_before[UUID(k)] for k in completed_before)
    # ---- Stage-1 baseline acquired once per instrument across both pods ---------
    assert all(n == 1 for n in u.calls["baseline"].values())
    # ---- durable evidence, no duplicates -----------------------------------------
    for case in ("FINANCIAL_PDF", "PERSISTENCE_FAILURE_THEN_SUCCESS", "DUPLICATE_FILING"):
        for key in (k for k in per_instrument if case_of(UUID(k)) == case):
            docs = u.store.load_documents_by_ids(
                [ref.document_id for ref in repo2.documents.refs_for_instrument(UUID(key))]) or \
                [d for d in u.store.load_documents() if str(d.instrument_id) == key]
            assert any(d.canonical_url.endswith("_results.pdf") for d in docs), (case, key)
    rows = u.store._connection.execute("SELECT canonical_url, COUNT(*) FROM research_documents GROUP BY 1").fetchall()
    assert all(n == 1 for _, n in rows)
    facts = u.store._connection.execute("SELECT instrument_id, COUNT(*) FROM global_financial_facts GROUP BY 1").fetchall()
    assert all(n == 1 for _, n in facts)
    assert all(u.calls["provider"][k] <= 1 for k in u.calls["provider"])
    history = u.store._connection.execute(
        "SELECT global_instrument_id, COUNT(*) FROM stock_recommendation_history GROUP BY 1").fetchall()
    assert all(n == 1 for _, n in history)
    snapshots = u.store._connection.execute(
        "SELECT global_instrument_id, COUNT(*) FROM global_opportunity_snapshot WHERE cycle_id=? GROUP BY 1",
        (cycle_id,)).fetchall()
    assert all(n == 1 for _, n in snapshots)
    assert u.store._connection.execute("SELECT COUNT(*) FROM global_opportunity_top_selection WHERE cycle_id=?",
                                       (cycle_id,)).fetchone()[0] == 1  # publication exactly once
    # ---- memory and concurrency bounded --------------------------------------------
    concurrency = repo2.settings.research_stage2_concurrency
    assert u.peak_active <= concurrency
    gc.collect()
    live_structures = sum(ref() is not None for ref in u.structures)
    assert live_structures <= repo2.settings.research_document_cache_max_documents
    stats = repo2.documents.stats()
    assert stats["evictable_bytes"] <= repo2.settings.research_document_cache_max_bytes
    with capsys.disabled():
        print("\nMINI_UNIVERSE_ACCOUNTING total=%d unaccounted=%d" % (sum(categories.values()), categories["UNACCOUNTED"]))
        for label, n in sorted(categories.items()):
            print("MINI_UNIVERSE_ACCOUNTING", label, n)
        print("MINI_UNIVERSE_COUNTS deep_calls=%d provider=%d ingested=%d evaluated=%d cache_hits=%d baseline=%d "
              "peak_active=%d concurrency=%d live_pdf_structures=%d resumed=%s executed=%s" % (
                  sum(u.calls["deep"].values()), sum(u.calls["provider"].values()), sum(u.calls["ingested"].values()),
                  sum(u.calls["evaluated"].values()), sum(u.calls["cache_hits"].values()),
                  sum(u.calls["baseline"].values()), u.peak_active, concurrency, live_structures,
                  finished.get("resumed_candidates"), finished.get("executed_candidates")))
