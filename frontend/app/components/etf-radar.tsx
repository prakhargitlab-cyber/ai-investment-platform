"use client";

import { useEffect, useRef, useState } from "react";
import { request } from "../lib/portfolio-api";
import { RadarProgressPanel, isTerminalRadarStatus, type RadarProgressCounter } from "./radar-progress";

/**
 * ETF Radar's dedicated frontend surface. Deliberately separate from
 * opportunity-radar.tsx (the Equity Radar component), which this file does
 * not import or modify -- the two radars never share loading/error/result
 * state, matching ai/research-engine/app/etf_opportunity_cycle.py's own
 * separation from app/global_opportunity_cycle.py.
 *
 * Field names below are snake_case because the backend persists/returns
 * app.etf_opportunity_cycle.etf_cycle_result_to_dict's output verbatim (via
 * GET/POST /api/v1/etf-radar/*), with no camelCase response_model layer --
 * unlike several other endpoints in this app. This is a deliberate,
 * documented choice, not an oversight.
 */

export type EtfFactorResult = {
  factor: string;
  weight: number;
  status: "SCORED" | "UNAVAILABLE";
  raw_metric: string | null;
  normalized_score: number | null;
  weighted_contribution: number | null;
  freshness: string | null;
  source_provenance: string | null;
  confidence: number | null;
  reason: string | null;
};

export type EtfRiskGate = {
  gate_id: string;
  category: "TECHNICAL_FAILURE" | "INSUFFICIENT_DATA" | "POOR_CHARACTERISTICS";
  severity: "BLOCKING" | "WARNING";
  triggered: boolean;
  reason: string;
};

export type EtfRuleEngineResult = {
  rule_engine_version: string;
  instrument_id: string;
  as_of: string;
  factor_results: EtfFactorResult[];
  overall_score: number | null;
  data_completeness: number;
  confidence: "HIGH" | "MEDIUM" | "LOW";
};

export type EtfCandidate = {
  global_instrument_id: string | null;
  symbol: string | null;
  disposition: "EVALUATED" | "INSUFFICIENT_DATA" | "INELIGIBLE" | "UNSUPPORTED" | "TECHNICAL_FAILURE" | "CANCELLED";
  reason: string;
  subtype: string | null;
  rule_engine_result: EtfRuleEngineResult | null;
  risk_gates: EtfRiskGate[];
  recommendation: string | null;
  rank: number | null;
};

export type EtfCycleResult = {
  radar_version: string;
  cycle_id: string;
  correlation_id: string | null;
  as_of: string;
  candidates: EtfCandidate[];
  ranked: EtfCandidate[];
  excluded_by_reason: Record<string, number>;
};

function factorByName(result: EtfRuleEngineResult | null, name: string): EtfFactorResult | undefined {
  return result?.factor_results.find((f) => f.factor === name);
}

/** Never fakes a zero: an UNAVAILABLE or missing factor renders as "—". */
function factorDisplay(result: EtfRuleEngineResult | null, name: string): string {
  const f = factorByName(result, name);
  if (!f || f.status === "UNAVAILABLE" || f.normalized_score == null) return "—";
  return f.normalized_score.toFixed(0);
}

function pct(value: number | null | undefined): string {
  return value == null ? "—" : `${value.toFixed(0)}%`;
}

const FACTOR_LABELS: [string, string][] = [
  ["PERFORMANCE_MOMENTUM", "Performance / momentum"],
  ["RISK_DRAWDOWN_VOLATILITY", "Risk (drawdown / volatility)"],
  ["LIQUIDITY", "Liquidity"],
  ["TRACKING_QUALITY", "Tracking quality"],
  ["COST", "Cost (expense ratio)"],
  ["NAV_PREMIUM_DISCOUNT", "NAV premium / discount"],
  ["DIVERSIFICATION", "Diversification"],
];

export function EtfRadarCard({ item }: { item: EtfCandidate }) {
  const result = item.rule_engine_result;
  const triggeredGates = item.risk_gates.filter((gate) => gate.triggered);
  return (
    <article className="opportunity-card etf-radar-card">
      <header className="opportunity-card-header">
        <div>
          <h4>{item.symbol ?? "Unknown ETF"}</h4>
          <small>{item.subtype ?? "Unclassified"}</small>
        </div>
        {item.rank != null ? <div className="opportunity-card-flags"><span>Rank {item.rank}</span></div> : null}
      </header>
      <p className="opportunity-action">{item.recommendation ?? item.disposition}</p>
      <dl className="opportunity-primary-metrics">
        <div><dt>Score</dt><dd>{result?.overall_score != null ? result.overall_score.toFixed(1) : "—"}</dd></div>
        <div><dt>Confidence</dt><dd>{result?.confidence ?? "—"}</dd></div>
        <div><dt>Data completeness</dt><dd>{result ? pct(result.data_completeness * 100) : "—"}</dd></div>
      </dl>
      <dl className="opportunity-primary-metrics etf-radar-factors">
        {FACTOR_LABELS.map(([factorName, label]) => (
          <div key={factorName}><dt>{label}</dt><dd>{factorDisplay(result, factorName)}</dd></div>
        ))}
      </dl>
      {triggeredGates.length ? (
        <details>
          <summary>Risk flags · {triggeredGates.length}</summary>
          <ul>{triggeredGates.map((gate) => <li key={gate.gate_id}>{gate.gate_id.replaceAll("_", " ")}: {gate.reason}</li>)}</ul>
        </details>
      ) : null}
    </article>
  );
}

export function EtfRadarContent({ data }: { data: EtfCycleResult }) {
  return (
    <>
      <p>
        Last cycle: {new Date(data.as_of).toLocaleString()} · {data.ranked.length} ranked ETF{data.ranked.length === 1 ? "" : "s"}
      </p>
      <h3>Top ETF opportunities</h3>
      <div className="opportunity-cards">{data.ranked.map((item) => <EtfRadarCard key={item.global_instrument_id ?? item.symbol} item={item} />)}</div>
      {!data.ranked.length ? <p>No ETF currently ranks as a qualifying opportunity.</p> : null}
    </>
  );
}

type EtfRadarStatus = "loading" | "empty" | "error" | "ready";

type EtfActiveCycle = {
  cycleId: string;
  status: string;
  startedAt: string | null;
  updatedAt: string | null;
  errorCode: string | null;
  pollError: boolean;
};

// Client-side only (not a backend capability): the current ETF API surface
// has no "discover the active cycle" endpoint, so a page refresh cannot
// recover progress from the server alone. Persisting the cycle_id we were
// already handed, in this tab's sessionStorage, lets a refresh resume
// polling the real status endpoint for that same cycle -- it never
// fabricates state, it only remembers an ID the backend already gave us.
const ETF_ACTIVE_CYCLE_STORAGE_KEY = "aip.etfRadar.activeCycleId";

export function EtfRadar() {
  const [data, setData] = useState<EtfCycleResult | null>(null);
  const [status, setStatus] = useState<EtfRadarStatus>("loading");
  const [activeCycle, setActiveCycle] = useState<EtfActiveCycle | null>(null);
  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    let active = true;
    setStatus("loading");
    request<EtfCycleResult>("/api/v1/etf-radar/current")
      .then((value) => { if (active) { setData(value); setStatus("ready"); } })
      .catch((failure: { status?: number }) => {
        if (!active) return;
        if (failure?.status === 404) { setData(null); setStatus("empty"); } else setStatus("error");
      });
    let storedCycleId: string | null = null;
    try { storedCycleId = window.sessionStorage.getItem(ETF_ACTIVE_CYCLE_STORAGE_KEY); } catch { /* ignore */ }
    if (storedCycleId) {
      setActiveCycle({ cycleId: storedCycleId, status: "RUNNING", startedAt: null, updatedAt: null, errorCode: null, pollError: false });
    }
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (!activeCycle || isTerminalRadarStatus(activeCycle.status)) {
      try { if (!activeCycle) window.sessionStorage.removeItem(ETF_ACTIVE_CYCLE_STORAGE_KEY); } catch { /* ignore */ }
      return;
    }
    let active = true;
    const poll = () => {
      request<{ status: string; updated_at?: string; created_at?: string; error_code?: string }>(
        `/api/v1/etf-radar/cycles/${activeCycle.cycleId}/status`
      )
        .then((value) => {
          if (!active) return;
          setActiveCycle((previous) => previous && previous.cycleId === activeCycle.cycleId
            ? { ...previous, status: value.status, updatedAt: value.updated_at ?? previous.updatedAt,
                startedAt: previous.startedAt ?? value.created_at ?? null,
                errorCode: value.error_code ?? previous.errorCode, pollError: false }
            : previous);
          if (isTerminalRadarStatus(value.status)) {
            try { window.sessionStorage.removeItem(ETF_ACTIVE_CYCLE_STORAGE_KEY); } catch { /* ignore */ }
            if (value.status === "COMPLETED") {
              request<EtfCycleResult>("/api/v1/etf-radar/current")
                .then((result) => { if (active) { setData(result); setStatus("ready"); } })
                .catch(() => { /* keep showing the terminal status panel */ });
            }
          } else {
            pollTimer.current = setTimeout(poll, 2500);
          }
        })
        .catch(() => {
          if (!active) return;
          // A transient poll failure must never leave the panel stuck
          // silently forever: surface it, but keep polling.
          setActiveCycle((previous) => previous && previous.cycleId === activeCycle.cycleId
            ? { ...previous, pollError: true } : previous);
          pollTimer.current = setTimeout(poll, 2500);
        });
    };
    poll();
    return () => { active = false; if (pollTimer.current) clearTimeout(pollTimer.current); };
    // Re-run whenever the tracked cycle_id changes; status itself is
    // updated inside the closure above, not via this dependency.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeCycle?.cycleId]);

  const runCycle = () => {
    request<{ cycle_id: string; status: string; updated_at?: string }>(
      "/api/v1/etf-radar/cycles", { method: "POST", body: JSON.stringify({ top_n: 10 }) }
    )
      .then((value) => {
        setActiveCycle({ cycleId: value.cycle_id, status: value.status, startedAt: value.updated_at ?? null,
                          updatedAt: value.updated_at ?? null, errorCode: null, pollError: false });
        try { window.sessionStorage.setItem(ETF_ACTIVE_CYCLE_STORAGE_KEY, value.cycle_id); } catch { /* ignore */ }
      })
      .catch(() => setStatus("error"));
  };

  const running = !!activeCycle && !isTerminalRadarStatus(activeCycle.status);
  const counters: RadarProgressCounter[] = []; // ETF Radar's cycle is single-pass/bounded and does not expose
  // per-instrument progress counters (see app/etf_opportunity_cycle.py) -- an
  // indeterminate bar plus the lifecycle status is the truthful presentation.

  return (
    <section className="opportunity-radar etf-radar" aria-label="ETF radar">
      <h2>ETF radar</h2>
      <button type="button" className="button button-primary radar-run-action" disabled={running} onClick={runCycle}>
        {running ? "Running…" : "Run ETF Radar"}
      </button>
      {activeCycle ? (
        <RadarProgressPanel radarType="ETF" cycleId={activeCycle.cycleId} status={activeCycle.status}
          startedAt={activeCycle.startedAt} updatedAt={activeCycle.updatedAt} counters={counters}
          percent={null} errorCode={activeCycle.errorCode} pollError={activeCycle.pollError} />
      ) : null}
      {status === "error" ? <p role="alert">ETF radar unavailable.</p> : null}
      {status === "empty" ? <p>No persisted ETF radar cycle yet. Run it to evaluate the current ETF universe.</p> : null}
      {status === "loading" ? <p>Loading persisted ETF opportunities…</p> : null}
      {status === "ready" && data ? <EtfRadarContent data={data} /> : null}
    </section>
  );
}
