"""Bounded, in-process Stage-2 timing instrumentation.

Added for the FINAL CONTROLLED VALIDATION follow-up's Issue 3 ("explain the
real 7-minute critical path"). Purely additive observability: every
recording call is a side effect guarded by a no-op default (an unset
contextvar), so this module changes no return value, no branch, and no
persisted research/eligibility decision anywhere in the pipeline --
tests/test_cycle_timing_instrumentation.py's "does not change decisions"
regression runs the same readiness/eligibility fixtures with and without an
active recorder and asserts byte-identical outcomes (test requirement #10).

Bounded by construction, not by discipline:
 - one CycleTimingRecorder is created per Stage-2 run() invocation (see
   app.global_opportunity_orchestration) and discarded at the end of that
   cycle -- nothing here is a process-lifetime singleton, and nothing here
   is written to durable storage;
 - per (candidate, requirement) records are capped at
   _MAX_TRACKED_OPERATIONS, a hard ceiling independent of universe size;
   once reached, further operations are simply not tracked (start_requirement
   returns None for them, and every record_* call silently no-ops for a
   None span) rather than growing the record set further;
 - the "slowest N" cycle-level report is computed with heapq.nlargest over
   the already-capped record set, never a separately retained unbounded
   event log;
 - nothing here accumulates across cycles: nothing is stored at module
   scope except two ContextVars, which hold at most one recorder/span
   reference each, reset at the end of their `with` block regardless of
   exceptions.
"""
from __future__ import annotations

import heapq
import logging
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

_MAX_TRACKED_OPERATIONS = 2000
_SLOWEST_N = 10


def _now_ms() -> float:
    return time.monotonic() * 1000.0


@dataclass
class RequirementTimingRecord:
    """One candidate+requirement(/group) unit's timing breakdown. Elapsed
    sub-fields are accumulators (a requirement's acquisition may touch the
    network, PDF extraction, and persistence more than once -- e.g. across
    retries or several discovered documents -- so each record_* call adds
    to the running total rather than overwriting it)."""

    candidate_id: str
    requirement_id: str
    queued_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    elapsed_ms: float | None = None
    provider_elapsed_ms: float = 0.0
    network_elapsed_ms: float = 0.0
    # Guardian Review Slice 6: two stages proven (by the aggregate/slowest-N
    # report showing every fine-grained field at 0.0 while
    # runtime_ensure_elapsed_ms absorbed almost all wall time) to have had NO
    # instrumentation at all -- both structured_market.py's Yahoo
    # search/quote calls (MAPPING_RESOLUTION) and source_discovery.py's NSE
    # announcement calls (DISCOVERY) use their own raw httpx clients outside
    # research_fetching.py's instrumented fetch path, so neither
    # provider_elapsed_ms nor network_elapsed_ms above ever captured them.
    mapping_resolution_elapsed_ms: float = 0.0
    discovery_elapsed_ms: float = 0.0
    # READINESS_LOAD: the initial runtime.read() at the top of
    # deep_investigation.investigate(), which runs BEFORE any per-requirement
    # span exists (see cycle-level _cycle_readiness_load_ms below for the
    # aggregate this always reaches even then).
    readiness_load_elapsed_ms: float = 0.0
    # FINAL_READINESS_RELOAD: the evidence_only=True reload
    # (deep_investigation._requirement_sufficient) used both mid-acquisition
    # ("is this requirement already satisfied?") and inside _finalize's
    # truthful classification -- a genuinely distinct step from the initial
    # READINESS_LOAD above, run possibly several times per requirement.
    final_readiness_reload_elapsed_ms: float = 0.0
    pdf_queue_wait_ms: float = 0.0
    pdf_parse_elapsed_ms: float = 0.0
    persistence_wait_ms: float = 0.0
    persistence_elapsed_ms: float = 0.0
    retry_backoff_elapsed_ms: float = 0.0
    runtime_ensure_elapsed_ms: float = 0.0
    finalization_elapsed_ms: float = 0.0
    # PLANNING: planner.plan() + the authority-upgrade gate inside
    # ResearchReadinessRuntime.ensure() (INDIA QUARTERLY_FINANCIALS check that
    # may re-load facts). Distinct from provider/refresh work below.
    planning_elapsed_ms: float = 0.0
    # SINGLE_FLIGHT_WAIT: time an ensure() call blocks on an in-flight shared
    # acquisition task (asyncio.shield of an existing _EnsureFlight) before
    # its own plan executes. Zero for the owning (first) call.
    single_flight_wait_elapsed_ms: float = 0.0
    # OBSERVATION_RECORDING: the per-target
    # record_acquisition_observation persistence loop at the tail of
    # ensure(), each call serialized through the global persistence lock.
    observation_recording_elapsed_ms: float = 0.0
    _start_monotonic_ms: float = field(default=0.0, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _add(self, attr: str, ms: float | None) -> None:
        if not ms:
            return
        with self._lock:
            setattr(self, attr, round(getattr(self, attr) + ms, 3))

    def to_report_dict(self) -> dict:
        # Built field-by-field rather than dataclasses.asdict(self): asdict()
        # deep-copies every field, including the non-data _lock (a
        # threading.Lock, which cannot be pickled/deepcopied).
        return {
            "candidate_id": self.candidate_id,
            "requirement_id": self.requirement_id,
            "queued_at": self.queued_at.isoformat() if self.queued_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "elapsed_ms": self.elapsed_ms,
            "provider_elapsed_ms": self.provider_elapsed_ms,
            "network_elapsed_ms": self.network_elapsed_ms,
            "mapping_resolution_elapsed_ms": self.mapping_resolution_elapsed_ms,
            "discovery_elapsed_ms": self.discovery_elapsed_ms,
            "readiness_load_elapsed_ms": self.readiness_load_elapsed_ms,
            "final_readiness_reload_elapsed_ms": self.final_readiness_reload_elapsed_ms,
            "pdf_queue_wait_ms": self.pdf_queue_wait_ms,
            "pdf_parse_elapsed_ms": self.pdf_parse_elapsed_ms,
            "persistence_wait_ms": self.persistence_wait_ms,
            "persistence_elapsed_ms": self.persistence_elapsed_ms,
            "retry_backoff_elapsed_ms": self.retry_backoff_elapsed_ms,
            "runtime_ensure_elapsed_ms": self.runtime_ensure_elapsed_ms,
            "finalization_elapsed_ms": self.finalization_elapsed_ms,
            "planning_elapsed_ms": self.planning_elapsed_ms,
            "single_flight_wait_elapsed_ms": self.single_flight_wait_elapsed_ms,
            "observation_recording_elapsed_ms": self.observation_recording_elapsed_ms,
            "other_unattributed_ms": self.other_unattributed_ms,
        }

    @property
    def other_unattributed_ms(self) -> float | None:
        """OTHER_UNATTRIBUTED: elapsed_ms minus every attributed sub-stage
        below it, clamped at 0 so double-counted/overlapping sub-measurements
        (e.g. retries that re-enter an already-measured stage) never go
        negative. None until this operation has completed (elapsed_ms unset),
        matching every other derived field's convention here."""
        if self.elapsed_ms is None:
            return None
        attributed = (
            self.provider_elapsed_ms + self.network_elapsed_ms
            + self.mapping_resolution_elapsed_ms + self.discovery_elapsed_ms
            + self.readiness_load_elapsed_ms + self.final_readiness_reload_elapsed_ms
            + self.pdf_queue_wait_ms
            + self.pdf_parse_elapsed_ms + self.persistence_wait_ms
            + self.persistence_elapsed_ms + self.retry_backoff_elapsed_ms
            + self.finalization_elapsed_ms
            + self.planning_elapsed_ms
            + self.single_flight_wait_elapsed_ms
            + self.observation_recording_elapsed_ms
        )
        return round(max(0.0, self.elapsed_ms - attributed), 3)


class CycleTimingRecorder:
    """One instance per Stage-2 cycle. Created and discarded entirely
    within app.global_opportunity_orchestration.run(); never a singleton,
    never durable."""

    def __init__(self, cycle_id: str | None = None):
        self.cycle_id = cycle_id
        self._records: dict[tuple[str, str], RequirementTimingRecord] = {}
        self._lock = threading.Lock()
        self._stage2_started_monotonic_ms: float | None = None
        self._stage2_completed_monotonic_ms: float | None = None
        self.max_concurrent_mandatory_investigations = 0
        self._dropped_count = 0
        # Cycle-wide aggregates independent of per-requirement attribution,
        # so work that happens outside any active requirement span (e.g. a
        # cycle-level persistence write) still counts toward the
        # cycle-level totals the task asked for.
        self._cycle_pdf_queue_wait_ms = 0.0
        self._cycle_provider_wait_ms = 0.0
        self._cycle_persistence_wait_ms = 0.0
        self._cycle_mapping_resolution_ms = 0.0
        self._cycle_discovery_ms = 0.0
        self._cycle_readiness_load_ms = 0.0
        self._cycle_final_readiness_reload_ms = 0.0
        self._cycle_planning_ms = 0.0
        self._cycle_single_flight_wait_ms = 0.0
        self._cycle_observation_recording_ms = 0.0
        # Guardian Review Slice 7: measure-first DB instrumentation -- a
        # bounded per-OPERATION-NAME counter (never per-call), so this is
        # bounded by the number of distinct persistence method names in the
        # codebase (a small, fixed set), not by call volume across a
        # 2585-instrument cycle. Identifies the highest-frequency
        # persistence calls without any pool/architecture change.
        self._persistence_operation_counts: dict[str, int] = {}
        # Guardian Review Slice 7 (measure-first): per-OPERATION-NAME timing
        # breakdown (call count + total wait + total execution), in addition to
        # the already-present per-name COUNT. Bounded by the small, fixed set of
        # distinct persistence method names, not by call volume. Identifies
        # which persistence operation consumes wall time, so a 26s
        # persistence_elapsed_ms aggregate is attributable to a specific
        # load_* / upsert_* / commit call rather than being a flat aggregate.
        self._persistence_operation_timing: dict[str, dict[str, float]] = {}

    # -- cycle-level bookkeeping -------------------------------------------------
    def mark_stage2_start(self) -> None:
        self._stage2_started_monotonic_ms = _now_ms()

    def mark_stage2_complete(self) -> None:
        self._stage2_completed_monotonic_ms = _now_ms()

    def note_concurrency(self, in_flight: int) -> None:
        with self._lock:
            if in_flight > self.max_concurrent_mandatory_investigations:
                self.max_concurrent_mandatory_investigations = in_flight

    def _add_cycle_aggregate(self, attr: str, ms: float | None) -> None:
        if not ms:
            return
        with self._lock:
            setattr(self, attr, round(getattr(self, attr) + ms, 3))

    def note_persistence_operation(self, name: str) -> None:
        with self._lock:
            self._persistence_operation_counts[name] = self._persistence_operation_counts.get(name, 0) + 1

    def note_persistence_operation_timing(self, name: str, wait_ms: float, exec_ms: float) -> None:
        """Accumulate per-operation-name persistence timing (wait + execution).
        A no-op with no active recorder. Bounded by distinct method names."""
        with self._lock:
            slot = self._persistence_operation_timing.get(name)
            if slot is None:
                slot = {"calls": 0, "total_wait_ms": 0.0, "total_exec_ms": 0.0,
                        "max_wait_ms": 0.0, "max_exec_ms": 0.0}
                self._persistence_operation_timing[name] = slot
            slot["calls"] += 1
            slot["total_wait_ms"] = round(slot["total_wait_ms"] + (wait_ms or 0.0), 3)
            slot["total_exec_ms"] = round(slot["total_exec_ms"] + (exec_ms or 0.0), 3)
            slot["max_wait_ms"] = round(max(slot["max_wait_ms"], wait_ms or 0.0), 3)
            slot["max_exec_ms"] = round(max(slot["max_exec_ms"], exec_ms or 0.0), 3)

    # -- per (candidate, requirement) bookkeeping --------------------------------
    def start_requirement(self, candidate_id, requirement_id: str) -> RequirementTimingRecord | None:
        key = (str(candidate_id), requirement_id)
        with self._lock:
            record = self._records.get(key)
            if record is None:
                if len(self._records) >= _MAX_TRACKED_OPERATIONS:
                    self._dropped_count += 1
                    return None
                record = RequirementTimingRecord(candidate_id=str(candidate_id), requirement_id=requirement_id,
                                                  queued_at=datetime.now(timezone.utc))
                self._records[key] = record
        record.started_at = datetime.now(timezone.utc)
        record._start_monotonic_ms = _now_ms()
        return record

    def complete_requirement(self, record: RequirementTimingRecord | None) -> None:
        if record is None:
            return
        record.completed_at = datetime.now(timezone.utc)
        if record._start_monotonic_ms:
            record.elapsed_ms = round(_now_ms() - record._start_monotonic_ms, 3)

    # -- reporting -----------------------------------------------------------
    @property
    def stage2_wall_clock_ms(self) -> float | None:
        if self._stage2_started_monotonic_ms is None:
            return None
        end = self._stage2_completed_monotonic_ms or _now_ms()
        return round(end - self._stage2_started_monotonic_ms, 3)

    def _aggregate(self, attr: str) -> float:
        with self._lock:
            records = list(self._records.values())
        return round(sum(getattr(r, attr) for r in records), 3)

    def slowest_operations(self, n: int = _SLOWEST_N) -> list[dict]:
        with self._lock:
            records = list(self._records.values())
        ranked = heapq.nlargest(n, records, key=lambda r: r.elapsed_ms or 0.0)
        return [r.to_report_dict() for r in ranked]

    def report(self) -> dict:
        with self._lock:
            record_count = len(self._records)
            dropped = self._dropped_count
            operations = [r.to_report_dict() for r in self._records.values()]
        return {
            "cycle_id": self.cycle_id,
            "stage2_wall_clock_ms": self.stage2_wall_clock_ms,
            "max_concurrent_mandatory_investigations": self.max_concurrent_mandatory_investigations,
            "tracked_operation_count": record_count,
            "dropped_operation_count": dropped,
            "aggregate_pdf_queue_wait_ms": round(self._cycle_pdf_queue_wait_ms, 3),
            "aggregate_provider_wait_ms": round(self._cycle_provider_wait_ms, 3),
            "aggregate_persistence_wait_ms": round(self._cycle_persistence_wait_ms, 3),
            "aggregate_mapping_resolution_ms": round(self._cycle_mapping_resolution_ms, 3),
            "aggregate_discovery_ms": round(self._cycle_discovery_ms, 3),
            "aggregate_readiness_load_ms": round(self._cycle_readiness_load_ms, 3),
            "aggregate_final_readiness_reload_ms": round(self._cycle_final_readiness_reload_ms, 3),
            "aggregate_planning_ms": round(self._cycle_planning_ms, 3),
            "aggregate_single_flight_wait_ms": round(self._cycle_single_flight_wait_ms, 3),
            "aggregate_observation_recording_ms": round(self._cycle_observation_recording_ms, 3),
            "persistence_operation_counts": dict(
                sorted(self._persistence_operation_counts.items(), key=lambda item: item[1], reverse=True)
            ),
            "persistence_operation_timing": {
                name: {
                    "calls": slot["calls"],
                    "total_wait_ms": slot["total_wait_ms"],
                    "total_exec_ms": slot["total_exec_ms"],
                    "max_wait_ms": slot["max_wait_ms"],
                    "max_exec_ms": slot["max_exec_ms"],
                }
                for name, slot in sorted(
                    self._persistence_operation_timing.items(),
                    key=lambda item: item[1]["total_wait_ms"] + item[1]["total_exec_ms"],
                    reverse=True,
                )
            },
            "slowest_operations": self.slowest_operations(),
            "operations": operations,
        }


# ---------------------------------------------------------------------------
# Context propagation. Stage-2's run() binds one CycleTimingRecorder for the
# duration of the deep-investigation phase (cycle_scope); each
# candidate+requirement's own acquisition task binds its own
# RequirementTimingRecord for the duration of its runtime.ensure() call
# (track_requirement). asyncio.to_thread() copies the current contextvars
# context into its worker thread, so code that runs off-loop (persistence)
# still resolves the correct active span/recorder.
# ---------------------------------------------------------------------------
_current_recorder: ContextVar[Optional["CycleTimingRecorder"]] = ContextVar("_current_recorder", default=None)
_current_span: ContextVar[Optional["RequirementTimingRecord"]] = ContextVar("_current_span", default=None)


def active_recorder() -> CycleTimingRecorder | None:
    return _current_recorder.get()


def active_span() -> RequirementTimingRecord | None:
    return _current_span.get()


def bind_span(record: Optional["RequirementTimingRecord"]):
    """Designate `record` as the active per-requirement span for the current
    async context. Returns a token for unbind_span().

    Used where ONE contextvar span slot must be shared across several
    requirement records that execute a single shared capability call -- e.g.
    app.deep_investigation._acquire_group, where a grouped runtime.ensure()
    is not wrapped by track_requirement() (which owns one span). Binding the
    FIRST member's record as the active span while the shared ensure() runs
    routes descendant provider/network/discovery/PDF/persistence sub-timing
    to that representative member, matching the intent stated in
    _acquire_group's own comment ("members beyond the first ... only receive
    the two elapsed totals measured here directly"). Non-members still receive
    the group's totals later via record_elapsed_on(). This changes no decision
    semantics: it only repairs attribution that was silently dropped."""
    return _current_span.set(record)


def unbind_span(token) -> None:
    _current_span.reset(token)


def bind_recorder(recorder: CycleTimingRecorder | None):
    """Non-context-manager equivalent of cycle_scope(), for a caller (Stage-2's
    run()) whose existing try/finally boundary already brackets the phase --
    avoids re-indenting a large existing method body just to add a `with`.
    Returns a token; pass it to unbind_recorder() to restore the previous
    value. Prefer cycle_scope() wherever a `with` block is natural."""
    return _current_recorder.set(recorder)


def unbind_recorder(token) -> None:
    _current_recorder.reset(token)


@contextmanager
def cycle_scope(recorder: CycleTimingRecorder | None) -> Iterator[Optional[CycleTimingRecorder]]:
    """Bind `recorder` as the active cycle-level recorder. Pass None to run
    with instrumentation fully disabled -- every record_* helper below is
    then a guaranteed no-op, identical to behavior before this module
    existed."""
    token = _current_recorder.set(recorder)
    try:
        yield recorder
    finally:
        _current_recorder.reset(token)


@contextmanager
def track_requirement(candidate_id, requirement_id: str) -> Iterator[Optional[RequirementTimingRecord]]:
    """Bind a per-(candidate, requirement) timing record as the active
    span. No-ops entirely when no cycle_scope() is active (recorder is
    None) or once _MAX_TRACKED_OPERATIONS has been reached."""
    recorder = _current_recorder.get()
    if recorder is None:
        yield None
        return
    record = recorder.start_requirement(candidate_id, requirement_id)
    token = _current_span.set(record)
    try:
        yield record
    finally:
        recorder.complete_requirement(record)
        _current_span.reset(token)


def _record(attr: str, ms: float | None) -> None:
    span = _current_span.get()
    if span is not None:
        span._add(attr, ms)


def record_provider_elapsed(ms: float | None) -> None:
    _record("provider_elapsed_ms", ms)
    recorder = _current_recorder.get()
    if recorder is not None:
        recorder._add_cycle_aggregate("_cycle_provider_wait_ms", ms)


def record_network_elapsed(ms: float | None) -> None:
    _record("network_elapsed_ms", ms)


def record_mapping_resolution_elapsed(ms: float | None) -> None:
    _record("mapping_resolution_elapsed_ms", ms)
    recorder = _current_recorder.get()
    if recorder is not None:
        recorder._add_cycle_aggregate("_cycle_mapping_resolution_ms", ms)


def record_discovery_elapsed(ms: float | None) -> None:
    _record("discovery_elapsed_ms", ms)
    recorder = _current_recorder.get()
    if recorder is not None:
        recorder._add_cycle_aggregate("_cycle_discovery_ms", ms)


def record_readiness_load_elapsed(ms: float | None) -> None:
    """READINESS_LOAD may fire before any per-requirement span exists (the
    initial runtime.read() at the top of deep_investigation.investigate());
    _record() is a safe no-op against a None span, and the cycle-level
    aggregate below still captures it either way."""
    _record("readiness_load_elapsed_ms", ms)
    recorder = _current_recorder.get()
    if recorder is not None:
        recorder._add_cycle_aggregate("_cycle_readiness_load_ms", ms)


def record_final_readiness_reload_elapsed(ms: float | None) -> None:
    _record("final_readiness_reload_elapsed_ms", ms)
    recorder = _current_recorder.get()
    if recorder is not None:
        recorder._add_cycle_aggregate("_cycle_final_readiness_reload_ms", ms)


def record_persistence_operation(name: str) -> None:
    """Count one dispatched persistence call by its operation name (never by
    call, so this stays bounded by the small number of distinct persistence
    method names, not by cycle volume). A no-op with no active recorder."""
    recorder = _current_recorder.get()
    if recorder is not None:
        recorder.note_persistence_operation(name)


def record_persistence_operation_timing(name: str, wait_ms: float, exec_ms: float) -> None:
    """Record per-operation-name persistence timing (lock wait + execution).
    A no-op with no active recorder. Complements record_persistence_operation,
    which only counts calls -- this is what makes a flat
    persistence_wait/persistence_elapsed aggregate attributable to a specific
    load_*/upsert_*/commit method."""
    recorder = _current_recorder.get()
    if recorder is not None:
        recorder.note_persistence_operation_timing(name, wait_ms, exec_ms)


def record_pdf_queue_wait(ms: float | None) -> None:
    _record("pdf_queue_wait_ms", ms)
    recorder = _current_recorder.get()
    if recorder is not None:
        recorder._add_cycle_aggregate("_cycle_pdf_queue_wait_ms", ms)


def record_pdf_parse_elapsed(ms: float | None) -> None:
    _record("pdf_parse_elapsed_ms", ms)


def record_persistence_wait(ms: float | None) -> None:
    _record("persistence_wait_ms", ms)
    recorder = _current_recorder.get()
    if recorder is not None:
        recorder._add_cycle_aggregate("_cycle_persistence_wait_ms", ms)


def record_persistence_elapsed(ms: float | None) -> None:
    _record("persistence_elapsed_ms", ms)


def record_retry_backoff(ms: float | None) -> None:
    _record("retry_backoff_elapsed_ms", ms)


def record_runtime_ensure_elapsed(ms: float | None) -> None:
    _record("runtime_ensure_elapsed_ms", ms)


def record_finalization_elapsed(ms: float | None) -> None:
    _record("finalization_elapsed_ms", ms)


def record_planning_elapsed(ms: float | None) -> None:
    _record("planning_elapsed_ms", ms)
    recorder = _current_recorder.get()
    if recorder is not None:
        recorder._add_cycle_aggregate("_cycle_planning_ms", ms)


def record_single_flight_wait_elapsed(ms: float | None) -> None:
    _record("single_flight_wait_elapsed_ms", ms)
    recorder = _current_recorder.get()
    if recorder is not None:
        recorder._add_cycle_aggregate("_cycle_single_flight_wait_ms", ms)


def record_observation_recording_elapsed(ms: float | None) -> None:
    _record("observation_recording_elapsed_ms", ms)
    recorder = _current_recorder.get()
    if recorder is not None:
        recorder._add_cycle_aggregate("_cycle_observation_recording_ms", ms)


def record_elapsed_on(record: Optional["RequirementTimingRecord"], attr: str, ms: float | None) -> None:
    """Add `ms` to one explicit record's `attr`, bypassing the
    single-slot _current_span contextvar. Used where ONE measured elapsed
    value must be attributed to SEVERAL records at once -- e.g. a grouped
    acquisition's single shared runtime.ensure() call, whose elapsed time
    is attributed to every member requirement's own record, mirroring how
    the group's document/query budget is already shared across members
    (see app.deep_investigation._acquire_group)."""
    if record is not None:
        record._add(attr, ms)
