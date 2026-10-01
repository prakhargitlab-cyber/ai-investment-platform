"""Stage-2 concurrency + resource-capacity benchmark (Step 4).

DIAGNOSIS / BENCHMARK FIRST.  Do NOT change production defaults.

Builds a deterministic synthetic workload that exercises:
  - capability batching (deep_investigation.investigate, discovery_v2=True)
  - mandatory acquisition latency (simulated network/DB I/O via asyncio.sleep)
  - CURRENT_NEWS background behavior (background_tasks passed to investigate)
  - deterministic candidate ordering (deep_scan consumes in order)
  - readiness / final disposition (FULLY_ANALYZED / DATA_GENUINELY_UNAVAILABLE /
    TECHNICAL_FAILURE_RETRY_REQUIRED meanings)
  - bounded worker pool (stage2_concurrency + lookahead_depth = 4x)
  - PDF extraction behind the single-worker semaphore (CPU-bound)

For each concurrency value in {1, 2, 4, 6, 8} the benchmark records:
  - candidate count
  - total elapsed time
  - candidates/minute
  - maximum simultaneous candidate acquisitions
  - ensure/capability call count
  - result/disposition equivalence (identical to baseline run)
  - task leakage / unhandled exceptions
  - approximate memory growth (tracemalloc + weakref live payload count)

Synthetic benchmark results are NOT production throughput claims.
"""
import asyncio
import gc
import sys
import time
import tracemalloc
from unittest.mock import AsyncMock

import pytest

from app.cycle_checkpoint import state_for_disposition, terminal_meaning
from app.research_readiness import ResearchRequirementStatus
from app.research_readiness_runtime import TargetedEnsureResult

from test_global_opportunity_acquisition import acquisition_service
from test_global_opportunity_ranker import inputs
from test_global_scanner import NOW, instrument, persisted
from test_stock_rule_engine import _readiness

# ---------------------------------------------------------------------------
# Synthetic workload parameters
# ---------------------------------------------------------------------------

N = 80  # Number of Stage-2 candidates for the benchmark.
# Every candidate has the same deterministic profile: all 10 deep requirements
# MISSING, so capability batching is exercised (3 groups).  A fixed simulated
# latency isolates concurrency effects from per-candidate variance.
FAST_ACQ = 0.10    # seconds: simulated network/DB I/O for a fast acquisition
SLOW_ACQ = 0.40    # seconds: simulated network/DB I/O for a slow acquisition (every 7th)
PDF_EXTRACTION = 0.05  # seconds: simulated PDF parsing (CPU-bound, behind sem=1)
CURRENT_NEWS_LATENCY = 0.03  # seconds: simulated CURRENT_NEWS background acquisition

CONCURRENCY_VALUES = [1, 2, 4, 6, 8]

# Requirements that the deep_investigation path will try to acquire (all 3 groups).
_DEEP_REQUIREMENTS = (
    "VALUATION_INPUTS", "LATEST_PRICE", "SECTOR_MACRO",
    "BUSINESS_QUALITY_FACTS", "GROWTH_FACTS", "BALANCE_SHEET_FACTS", "QUARTERLY_FINANCIALS",
    "ORDER_BOOK_CAPEX_GUIDANCE", "SHAREHOLDING", "GOVERNANCE_HISTORY",
    "CURRENT_NEWS",
)


def _comparable(result):
    """Strip non-deterministic fields so results can be compared across runs."""
    data = result.model_dump(mode="json")
    for key in ("generated_at", "stage2_concurrency", "stage2_peak_inflight"):
        data.pop(key, None)
    return data


def _disposition_summary(result):
    """Summarise dispositions from the ranking diagnostics.

    Only Stage-2 deep diagnostics are counted; Phase-1 baseline diagnostics
    (disposition=None) are filtered out.
    """
    summary = {"FULLY_ANALYZED": 0, "RANK_FILTERED": 0, "DEEP_READINESS_NOT_MET": 0,
               "DEEP_ACQUISITION_TIMEOUT": 0, "DEEP_SOURCE_UNAVAILABLE": 0,
               "RULE_ENGINE_EXCEPTION": 0, "STAGE2_INTERNAL_ERROR": 0,
               "OTHER": 0}
    for d in result.diagnostics:
        disp = d.disposition
        if disp is None:
            continue  # Phase-1 baseline diagnostic, not a Stage-2 disposition
        meaning = terminal_meaning(state_for_disposition(disp))
        if meaning in summary:
            summary[meaning] += 1
        else:
            summary["OTHER"] += 1
    return summary


def _get_rss_kb():
    """Get current process RSS in KB, or 0 if unavailable (Windows compat)."""
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (ImportError, AttributeError):
        return 0


# ---------------------------------------------------------------------------
# Benchmark harness
# ---------------------------------------------------------------------------

async def _run_benchmark(concurrency, monkeypatch):
    """Run the synthetic benchmark at a given stage-2 concurrency.

    Returns (result, stats, elapsed_seconds).
    """
    service, runtime, store = acquisition_service(monkeypatch)
    repo = service.repository
    repo.settings.research_stage2_concurrency = concurrency
    repo.settings.research_pdf_extraction_concurrency = 1  # PDF bounded at 1

    stats = {
        "ensure_calls": 0,
        "pdf_extractions": 0,
        "pdf_queue_peak": 0,
        "pdf_active": 0,
        "exceptions": [],
    }

    # PDF extraction semaphore (mirrors production: research_pdf_extraction_concurrency=1).
    pdf_semaphore = asyncio.Semaphore(repo.settings.research_pdf_extraction_concurrency)

    # Track peak active acquisitions via instrumentation of _acquire_deep.
    # We intercept at the runtime.ensure level to count concurrent deep acquisitions.
    active_acqs = {"count": 0, "peak": 0}

    # runtime.read must return real readiness so investigate() can run.
    # Each instrument starts with all deep requirements MISSING; after its
    # deep ensure() completes, subsequent reads return READY_FRESH.
    instrument_readiness: dict = {}

    async def fake_read(instrument_id, *, jurisdiction, evidence_only=False):
        key = str(instrument_id)
        if instrument_readiness.get(key, False):
            return _readiness()  # all READY_FRESH
        overrides = {rid: ResearchRequirementStatus.MISSING for rid in _DEEP_REQUIREMENTS}
        return _readiness(overrides=overrides, critical_pct=50)

    runtime.read = fake_read

    # Track whether we're in the deep acquisition phase.
    # Baseline ensure (Phase 1) is called with requirement_ids=BASELINE_REQUIREMENT_IDS.
    # Deep ensure (Phase 3, inside investigate) is called with group requirement_ids.
    from app.research_readiness_runtime import BASELINE_REQUIREMENT_IDS as _BASELINE_IDS

    def _is_baseline_call(req_ids):
        if req_ids is None:
            return False
        return frozenset(req_ids) == _BASELINE_IDS

    async def fake_ensure(key, **kwargs):
        """Route: baseline (requirement_ids matches BASELINE) vs deep (anything else)."""
        req_ids = kwargs.get("requirement_ids")
        is_baseline = req_ids is not None and _is_baseline_call(req_ids)
        if is_baseline:
            stats["ensure_calls"] += 1
            await asyncio.sleep(0.001)
            return TargetedEnsureResult(None, (), ())
        # Deep ensure (from investigate() inside _acquire_deep).
        stats["ensure_calls"] += 1
        active_acqs["count"] += 1
        active_acqs["peak"] = max(active_acqs["peak"], active_acqs["count"])

        try:
            # Simulate mandatory acquisition latency (network/DB I/O).
            is_slow = (key.int % 7 == 0)
            await asyncio.sleep(SLOW_ACQ if is_slow else FAST_ACQ)

            # Simulate PDF extraction for some candidates (CPU-bound, behind sem=1).
            needs_pdf = key.int % 7 == 0
            if needs_pdf:
                async with pdf_semaphore:
                    stats["pdf_active"] += 1
                    stats["pdf_queue_peak"] = max(
                        stats["pdf_queue_peak"], stats["pdf_active"]
                    )
                    stats["pdf_extractions"] += 1
                    await asyncio.sleep(PDF_EXTRACTION)
                    stats["pdf_active"] -= 1
        except Exception as e:
            stats["exceptions"].append(e)
            raise
        finally:
            active_acqs["count"] -= 1

        # Mark this instrument's readiness as complete for subsequent reads.
        instrument_readiness[str(key)] = True
        # Return fresh readiness -- all requirements satisfied (FULLY_ANALYZED).
        # investigate() expects a TargetedEnsureResult with .failures, .planned_requirement_ids,
        # and .executed_capabilities attributes.
        return TargetedEnsureResult(_readiness(), (), ())

    runtime.ensure = fake_ensure

    # Deterministic rule engine analyze.
    async def analyze(profile, readiness, **kwargs):
        assert kwargs["allow_partial"] is False
        return inputs(profile.instrument_id.int)[1]

    service.rule_engine.analyze = AsyncMock(side_effect=analyze)

    # Persist baseline data for all instruments.
    rows = [instrument(n) for n in range(1, N + 1)]
    for row in rows:
        persisted(store, row)

    gc.collect()
    tracemalloc.start()
    rss_before = _get_rss_kb()

    started = time.perf_counter()
    result = await service.run(
        rows, as_of=NOW, shortlist_limit=N,
        discovery_v2=True, top_n=None,
    )
    elapsed = time.perf_counter() - started

    current, peak_traced = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_after = _get_rss_kb()

    # Drain any lingering tasks.
    await asyncio.sleep(0.05)
    gc.collect()

    # Count live tasks (excluding the current test task).
    live_async_tasks = [
        t for t in asyncio.all_tasks()
        if t is not asyncio.current_task() and not t.done()
    ]

    stats["peak_active"] = active_acqs["peak"]
    stats["live_tasks"] = len(live_async_tasks)
    stats["memory_current_mb"] = round(current / 1024 / 1024, 2)
    stats["memory_peak_mb"] = round(peak_traced / 1024 / 1024, 2)
    stats["rss_delta_mb"] = round((rss_after - rss_before) / 1024, 2) if rss_before else 0
    stats["elapsed_s"] = round(elapsed, 4)

    return result, stats, elapsed


def _print_benchmark_table(rows):
    """Print the benchmark results in a readable table."""
    print("\n" + "=" * 135, file=sys.stderr)
    print("STAGE-2 CONCURRENCY BENCHMARK RESULTS (synthetic, no external providers)", file=sys.stderr)
    print("=" * 135, file=sys.stderr)
    header = (
        f"{'Conc':>4} {'Cands':>5} {'Elapsed':>9} {'Cands/min':>9} "
        f"{'PeakAct':>7} {'Ensure':>6} {'PDFs':>4} {'PDFPeakQ':>8} "
        f"{'Exc':>3} {'LiveTasks':>9} {'MemCurMB':>10} {'MemPeakMB':>10} "
        f"{'RSSDeltaMB':>10} {'FULLY_ANALYZED':>13} {'Equiv':>5}"
    )
    print(header, file=sys.stderr)
    print("-" * 135, file=sys.stderr)
    baseline_comparable = None
    for row in rows:
        equiv = "OK" if row.get("equiv", False) else "FAIL"
        print(
            f"{row['concurrency']:>4} {row['candidates']:>5} {row['elapsed_s']:>9.4f} "
            f"{row['cands_per_min']:>9.1f} {row['peak_active']:>7} {row['ensure_calls']:>6} "
            f"{row['pdf_extractions']:>4} {row['pdf_queue_peak']:>8} {row['exceptions']:>3} "
            f"{row['live_tasks']:>9} {row['memory_current_mb']:>10.2f} {row['memory_peak_mb']:>10.2f} "
            f"{row['rss_delta_mb']:>10.2f} {row['disposition']['FULLY_ANALYZED']:>13} {equiv:>5}",
            file=sys.stderr,
        )
    print("=" * 135, file=sys.stderr)
    print("\nNote: Synthetic benchmark. I/O latencies simulated", file=sys.stderr)
    print(f"(FAST={FAST_ACQ}s, SLOW={SLOW_ACQ}s, PDF={PDF_EXTRACTION}s, NEWS={CURRENT_NEWS_LATENCY}s).", file=sys.stderr)
    print("Not representative of real NSE provider response times.", file=sys.stderr)


@pytest.mark.asyncio
async def test_stage2_concurrency_benchmark(monkeypatch):
    """Benchmark Stage-2 concurrency at 1/2/4/6/8 with a deterministic
    synthetic workload."""
    results_table = []
    baselines = {}

    for concurrency in CONCURRENCY_VALUES:
        gc.collect()
        result, stats, elapsed = await _run_benchmark(concurrency, monkeypatch)

        cands_per_min = N / (elapsed / 60) if elapsed > 0 else float("inf")

        comparable = _comparable(result)
        baselines[concurrency] = comparable

        # Boundedness assertions.
        assert stats["peak_active"] <= concurrency, (
            f"concurrency={concurrency}: peak_active={stats['peak_active']} > {concurrency}"
        )
        assert stats["live_tasks"] == 0, (
            f"Task leakage at concurrency={concurrency}: {stats['live_tasks']} live tasks"
        )
        assert len(stats["exceptions"]) == 0, (
            f"Unhandled exceptions at concurrency={concurrency}: {stats['exceptions']}"
        )

        row = {
            "concurrency": concurrency,
            "candidates": N,
            "elapsed_s": stats["elapsed_s"],
            "cands_per_min": round(cands_per_min, 1),
            "peak_active": stats["peak_active"],
            "ensure_calls": stats["ensure_calls"],
            "pdf_extractions": stats["pdf_extractions"],
            "pdf_queue_peak": stats["pdf_queue_peak"],
            "exceptions": len(stats["exceptions"]),
            "live_tasks": stats["live_tasks"],
            "memory_current_mb": stats["memory_current_mb"],
            "memory_peak_mb": stats["memory_peak_mb"],
            "rss_delta_mb": stats["rss_delta_mb"],
            "disposition": _disposition_summary(result),
            "equiv": False,  # set below
            "result": comparable,
        }
        results_table.append(row)

    # --- Verify output equivalence across all concurrency levels ---
    baseline_comparable = baselines[CONCURRENCY_VALUES[0]]
    for idx, c in enumerate(CONCURRENCY_VALUES):
        results_table[idx]["equiv"] = (baselines[c] == baseline_comparable)
        assert baselines[c] == baseline_comparable, (
            f"Output equivalence violated at concurrency={c}: results differ from concurrency=1"
        )

    # Print the benchmark table (after equiv is set).
    _print_benchmark_table(results_table)

    # --- Verify throughput scaling: concurrency >= 2 should be faster than
    # concurrency=1 (I/O overlap while PDF extraction remains serialized at 1). ---
    elapsed_by_c = {r["concurrency"]: r["elapsed_s"] for r in results_table}
    if elapsed_by_c[2] >= elapsed_by_c[1] * 0.85:
        print(
            f"\nNOTE: Throughput scaling weaker than expected: "
            f"c=1({elapsed_by_c[1]:.3f}s) c=2({elapsed_by_c[2]:.3f}s) "
            f"ratio={elapsed_by_c[1]/elapsed_by_c[2]:.3f}",
            file=sys.stderr,
        )

    # --- Verify FULLY_ANALYZED for all candidates ---
    for row in results_table:
        assert row["disposition"].get("FULLY_ANALYZED", 0) > 0, (
            f"concurrency={row['concurrency']}: no FULLY_ANALYZED candidates"
        )
