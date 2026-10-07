"use client";

import { useEffect, useState } from "react";
import { request } from "../lib/portfolio-api";

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

export function EtfRadar() {
  const [data, setData] = useState<EtfCycleResult | null>(null);
  const [status, setStatus] = useState<EtfRadarStatus>("loading");
  const [running, setRunning] = useState(false);

  useEffect(() => {
    let active = true;
    setStatus("loading");
    request<EtfCycleResult>("/api/v1/etf-radar/current")
      .then((value) => { if (active) { setData(value); setStatus("ready"); } })
      .catch((failure: { status?: number }) => {
        if (!active) return;
        if (failure?.status === 404) { setData(null); setStatus("empty"); } else setStatus("error");
      });
    return () => { active = false; };
  }, []);

  const runCycle = () => {
    setRunning(true);
    request<EtfCycleResult>("/api/v1/etf-radar/cycles", { method: "POST", body: JSON.stringify({ top_n: 10 }) })
      .then((value) => { setData(value); setStatus("ready"); })
      .catch(() => setStatus("error"))
      .finally(() => setRunning(false));
  };

  return (
    <section className="opportunity-radar etf-radar" aria-label="ETF radar">
      <h2>ETF radar</h2>
      <button type="button" className="opportunity-show-more" disabled={running} onClick={runCycle}>
        {running ? "Running…" : "Run ETF radar"}
      </button>
      {status === "error" ? <p role="alert">ETF radar unavailable.</p> : null}
      {status === "empty" ? <p>No persisted ETF radar cycle yet. Run it to evaluate the current ETF universe.</p> : null}
      {status === "loading" ? <p>Loading persisted ETF opportunities…</p> : null}
      {status === "ready" && data ? <EtfRadarContent data={data} /> : null}
    </section>
  );
}
