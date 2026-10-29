"use client";

import { Fragment, useEffect, useRef, useState } from "react";
import { request } from "../lib/portfolio-api";
import { RadarProgressPanel, isTerminalRadarStatus, type RadarProgressCounter } from "./radar-progress";
import { Card, EmptyState, Skeleton } from "./ui";

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

export type EtfScreeningMetric = { value: string | null; unit: string; quality: "VALID" | "UNAVAILABLE" };

export type EtfEntryAssessment = {
  classification: string;
  price_band: unknown;
  conditions: string[];
  calculated_at: string;
  methodology: string;
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
  peer_group?: string;
  missing_data?: string[];
  limitations?: string[];
  source_references?: { metric: string; value: string; as_of: string; provider: string; url: string }[];
  // Diagnostics-only: the ETF_ONBOARD history-acquisition checkpoint
  // outcome for this instrument (see
  // app.etf_full_universe._classify_historical_acquisition). Never
  // affects scoring/eligibility; purely informational.
  historical_acquisition?: { status: string; reason: string | null; observations: number | null } | null;
  // Fund quality / liquidity / cost / risk / tracking evidence, when
  // app.etf_screening.screening_evidence found fresh backing facts; a
  // metric present with quality UNAVAILABLE is shown as "--", never 0.
  screening_metrics?: Record<string, EtfScreeningMetric>;
  history_observations?: number;
  holdings_coverage?: string | null;
  fund_house?: string;
  domicile?: string;
  distribution_type?: string;
  replication_method?: string;
  positive_signals?: string[];
  negative_signals?: string[];
  entry_assessment?: EtfEntryAssessment;
};

export type EtfCycleResult = {
  universe_count?: number;
  scope?: string;
  coverage?: Record<string, number>;
  radar_version: string;
  cycle_id: string;
  correlation_id: string | null;
  as_of: string;
  candidates: EtfCandidate[];
  ranked: EtfCandidate[];
  // app.etf_screening.research_overview's top-10 overview queue, ordered by
  // peer rank then score -- a research queue across peer groups, not a
  // claim that every entry is an interchangeable, single "best" pick.
  top_opportunities?: EtfCandidate[];
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

/** The factor's own raw_metric string (e.g. "RETURN_1M=-6.31,...") -- already
 * computed and persisted by the rule engine, just not previously rendered.
 * Shown as-is; never reformatted into a number that could misrepresent it. */
function factorRawMetric(result: EtfRuleEngineResult | null, name: string): string | null {
  const f = factorByName(result, name);
  return f && f.status !== "UNAVAILABLE" ? f.raw_metric : null;
}

function pct(value: number | null | undefined): string {
  return value == null ? "—" : `${value.toFixed(0)}%`;
}

/** Never fakes a value: an UNAVAILABLE or absent screening metric renders as "—". */
function metricDisplay(metrics: Record<string, EtfScreeningMetric> | undefined, name: string, suffix = ""): string {
  const m = metrics?.[name];
  if (!m || m.quality === "UNAVAILABLE" || m.value == null) return "—";
  const asNumber = Number(m.value);
  // maximumFractionDigits guards against raw floating-point display
  // artifacts (e.g. "1234567.891234") while never altering the
  // underlying API value (m.value, used for sorting/export) -- this is
  // display-only rounding, the original evidence is untouched.
  const formatted = Number.isFinite(asNumber) ? (Math.abs(asNumber) >= 1000 ? asNumber.toLocaleString(undefined, { maximumFractionDigits: 2 }) : asNumber.toFixed(2)) : m.value;
  return `${formatted}${suffix}`;
}

/** Raw numeric value for sorting; never fabricates a number for an UNAVAILABLE/missing metric. */
function metricRaw(metrics: Record<string, EtfScreeningMetric> | undefined, name: string): number | null {
  const m = metrics?.[name];
  if (!m || m.quality === "UNAVAILABLE" || m.value == null) return null;
  const asNumber = Number(m.value);
  return Number.isFinite(asNumber) ? asNumber : null;
}

/**
 * An honest, non-fabricated research-status label for a candidate --
 * distinguishes a genuinely qualified, ranked opportunity from a merely
 * evaluated or insufficient-data one. "Qualified opportunity" requires
 * BOTH an OPPORTUNITY/STRONG_OPPORTUNITY recommendation AND rank != null
 * (meaning the candidate actually survived peer_results() into
 * data.ranked) -- never inferred from recommendation alone.
 */
function researchStatusLabel(item: EtfCandidate): string {
  if (item.disposition === "INSUFFICIENT_DATA") return "Insufficient data";
  if (item.disposition === "TECHNICAL_FAILURE") return "Technical failure";
  if (item.disposition === "INELIGIBLE") return "Ineligible";
  if (item.disposition === "UNSUPPORTED") return "Unsupported";
  if (item.disposition === "CANCELLED") return "Cancelled";
  if (item.recommendation === "OPPORTUNITY" || item.recommendation === "STRONG_OPPORTUNITY") {
    return item.rank != null ? "Qualified opportunity" : "Recommended, not yet ranked";
  }
  if (item.recommendation === "INSUFFICIENT_DATA") return "Insufficient data";
  if (item.disposition === "EVALUATED") return "Evaluated \u2014 not recommended";
  return item.recommendation ?? item.disposition;
}

/** Latest (max) source-reference as-of date for a candidate, or "\u2014" when none is recorded. Never fabricates a date. */
function freshestEvidenceDate(item: EtfCandidate): string {
  const dates = (item.source_references ?? []).map((ref) => ref.as_of).filter((value): value is string => !!value);
  if (!dates.length) return "\u2014";
  return dates.slice().sort().slice(-1)[0];
}

/** Historical-acquisition diagnostics as one honest line; never claims a status that was not actually recorded. */
function historicalAcquisitionLabel(item: EtfCandidate): string {
  const h = item.historical_acquisition;
  if (!h) return "\u2014";
  return h.reason ? `${h.status} (${h.reason})` : h.status;
}

const FUND_METRIC_LABELS: [string, string][] = [
  ["expense_ratio", "Expense ratio (% p.a.)"],
  ["aum", "AUM"],
  ["trading_volume", "Trading volume"],
  ["bid_ask_spread", "Bid/ask spread (%)"],
  ["tracking_error", "Tracking error (%)"],
  ["tracking_difference", "Tracking difference (%)"],
  ["fund_age", "Fund age (yrs)"],
  ["volatility", "Annualized volatility (%)"],
  ["drawdown", "Max drawdown (%)"],
  ["concentration", "Top-10 concentration (%)"],
];

/** Minutes/hours-ago freshness label; never claims freshness it cannot verify. */
function freshnessLabel(asOf: string): string {
  const ms = Date.now() - new Date(asOf).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "unknown";
  const minutes = Math.round(ms / 60000);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
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
          {/* Persisted classification (subtype) is shown separately from
              the CURRENT, cycle-time verified peer group used for ranking
              -- the two can differ, and conflating them would misstate
              which classification actually drove this candidate's rank. */}
          <small>Subtype: {item.subtype ?? "Unclassified"} &middot; Peer group: {item.peer_group ?? "Unclassified"}</small>
        </div>
        {item.rank != null ? <div className="opportunity-card-flags"><span>{item.peer_group ? `${item.peer_group}: peer rank ` : "Rank "}{item.rank}</span></div> : null}
      </header>
      <p className="opportunity-action">{item.recommendation ?? item.disposition}</p>
      <dl className="opportunity-primary-metrics">
        <div><dt>Score</dt><dd>{result?.overall_score != null ? result.overall_score.toFixed(1) : "—"}</dd></div>
        <div><dt>Confidence</dt><dd>{result?.confidence ?? "—"}</dd></div>
        <div><dt>Data completeness</dt><dd>{result ? pct(result.data_completeness * 100) : "—"}</dd></div>
      </dl>
      <dl className="opportunity-primary-metrics etf-radar-factors">
        {FACTOR_LABELS.map(([factorName, label]) => {
          const raw = factorRawMetric(result, factorName);
          return (
            <div key={factorName}>
              <dt>{label}</dt>
              <dd>{factorDisplay(result, factorName)}{raw ? <small className="etf-factor-raw-metric"> ({raw})</small> : null}</dd>
            </div>
          );
        })}
      </dl>
      {item.entry_assessment ? (
        <p className="etf-entry-assessment">
          Entry: {item.entry_assessment.classification.replaceAll("_", " ").toLowerCase()}
        </p>
      ) : null}
      {item.positive_signals?.length || item.negative_signals?.length ? (
        <p className="etf-signals">
          {item.positive_signals?.length ? <span className="signal-positive">+ {item.positive_signals.join(", ")}</span> : null}
          {item.negative_signals?.length ? <span className="signal-negative"> − {item.negative_signals.join(", ")}</span> : null}
        </p>
      ) : null}
      {item.missing_data?.length ? <p>Missing data: {item.missing_data.join(", ")}</p> : null}
      {item.limitations?.length ? <ul>{item.limitations.map(text => <li key={text}>{text}</li>)}</ul> : null}
      <details className="etf-fund-quality">
        <summary>Fund quality, liquidity, cost &amp; risk</summary>
        <dl className="opportunity-primary-metrics">
          {FUND_METRIC_LABELS.map(([name, label]) => (
            <div key={name}><dt>{label}</dt><dd>{metricDisplay(item.screening_metrics, name)}</dd></div>
          ))}
          <div><dt>Price history depth</dt><dd>{item.history_observations != null ? `${item.history_observations} sessions` : "—"}</dd></div>
          <div><dt>Fund house</dt><dd>{item.fund_house ?? "—"}</dd></div>
          <div><dt>Replication</dt><dd>{item.replication_method ?? "—"}</dd></div>
          <div><dt>Distribution</dt><dd>{item.distribution_type ?? "—"}</dd></div>
          <div><dt>Holdings top-10 weight</dt><dd>{item.holdings_coverage ?? "—"}</dd></div>
        </dl>
      </details>
      {item.source_references?.length ? <details><summary>Evidence dates and sources</summary><ul>
        {item.source_references.map(source => <li key={source.metric}>{source.metric}: {source.value}; as of {source.as_of}; {source.provider}; {source.url}</li>)}
      </ul></details> : null}
      {triggeredGates.length ? (
        <details>
          <summary>Risk flags · {triggeredGates.length}</summary>
          <ul>{triggeredGates.map((gate) => <li key={gate.gate_id}>{gate.gate_id.replaceAll("_", " ")}: {gate.reason}</li>)}</ul>
        </details>
      ) : null}
    </article>
  );
}

type EtfSortKey = "rank" | "score" | "symbol" | "category" | "recommendation" | "confidence" | "completeness" | "expense" | "aum";
type EtfTab = "overview" | "top" | "leaders" | "universe" | "screener" | "compare" | "quality";

const ETF_TABS: [EtfTab, string][] = [
  ["overview", "Overview"],
  ["top", "Top Opportunities"],
  ["leaders", "Category Leaders"],
  ["universe", "Research Universe"],
  ["screener", "Advanced Screener"],
  ["compare", "ETF Comparison"],
  ["quality", "Data Quality"],
];

// Research Universe tab's own sort keys -- a superset is unnecessary since
// this tab operates over data.candidates (every assessed ETF), not just
// data.ranked, and several screener-only sort keys (peer rank, confidence)
// are not meaningful across unranked candidates.
type EtfUniverseSortKey = "symbol" | "category" | "status" | "score" | "completeness" | "expense" | "aum";

/** One ranked leader per peer_group (lowest `rank` wins), for the Category Leaders tab. */
function categoryLeaders(ranked: EtfCandidate[]): { category: string; leader: EtfCandidate }[] {
  const byCategory = new Map<string, EtfCandidate>();
  for (const item of ranked) {
    const category = item.peer_group ?? "Unclassified";
    const current = byCategory.get(category);
    if (!current || (item.rank ?? 1e9) < (current.rank ?? 1e9)) byCategory.set(category, item);
  }
  return Array.from(byCategory.entries())
    .map(([category, leader]) => ({ category, leader }))
    .sort((a, b) => a.category.localeCompare(b.category));
}

function EtfSectionTabs({ active, onChange }: { active: EtfTab; onChange: (tab: EtfTab) => void }) {
  return (
    <div className="radar-section-tabs" role="tablist" aria-label="ETF radar sections">
      {ETF_TABS.map(([id, label]) => (
        <button key={id} type="button" role="tab" aria-selected={active === id}
          className={active === id ? "radar-section-tab radar-section-tab-active" : "radar-section-tab"}
          onClick={() => onChange(id)}>
          {label}
        </button>
      ))}
    </div>
  );
}

const SCREENER_METRIC_ROWS: [string, (c: EtfCandidate) => string][] = [
  // Shown first and deliberately distinct from "Recommendation" below --
  // "Research status" is the honest, rank-aware label (see
  // researchStatusLabel) that never presents an insufficient-data or
  // unranked candidate as a qualified opportunity.
  ["Research status", (c) => researchStatusLabel(c)],
  ["Recommendation", (c) => c.recommendation ?? c.disposition],
  ["Category / peer group", (c) => c.peer_group ?? "Unclassified"],
  ["Peer rank", (c) => (c.rank != null ? String(c.rank) : "—")],
  ["Score", (c) => (c.rule_engine_result?.overall_score != null ? c.rule_engine_result.overall_score.toFixed(1) : "—")],
  ["Confidence", (c) => c.rule_engine_result?.confidence ?? "—"],
  ["Data completeness", (c) => pct(c.rule_engine_result ? c.rule_engine_result.data_completeness * 100 : null)],
  ["Returns / momentum", (c) => factorDisplay(c.rule_engine_result, "PERFORMANCE_MOMENTUM")],
  ["Risk (drawdown / volatility)", (c) => factorDisplay(c.rule_engine_result, "RISK_DRAWDOWN_VOLATILITY")],
  ["Liquidity", (c) => factorDisplay(c.rule_engine_result, "LIQUIDITY")],
  ["NAV premium / discount", (c) => factorDisplay(c.rule_engine_result, "NAV_PREMIUM_DISCOUNT")],
  ["Expense ratio", (c) => metricDisplay(c.screening_metrics, "expense_ratio")],
  ["AUM", (c) => metricDisplay(c.screening_metrics, "aum")],
  ["Tracking error", (c) => metricDisplay(c.screening_metrics, "tracking_error")],
  ["Tracking difference", (c) => metricDisplay(c.screening_metrics, "tracking_difference")],
  ["Bid/ask spread", (c) => metricDisplay(c.screening_metrics, "bid_ask_spread")],
  ["Fund age (yrs)", (c) => metricDisplay(c.screening_metrics, "fund_age")],
  ["Volatility", (c) => metricDisplay(c.screening_metrics, "volatility")],
  ["Max drawdown", (c) => metricDisplay(c.screening_metrics, "drawdown")],
  ["Holdings concentration", (c) => metricDisplay(c.screening_metrics, "concentration")],
  ["Domicile", (c) => c.domicile ?? "—"],
  ["Distribution", (c) => c.distribution_type ?? "—"],
  ["Replication", (c) => c.replication_method ?? "—"],
  ["Evidence freshness (latest as-of)", (c) => freshestEvidenceDate(c)],
  ["Historical acquisition", (c) => historicalAcquisitionLabel(c)],
  ["Missing evidence", (c) => (c.missing_data?.length ? c.missing_data.join(", ") : "None reported")],
  ["Limitations", (c) => (c.limitations?.length ? c.limitations.join("; ") : "None reported")],
];

type EtfDonutSegment = { label: string; value: number; color: string };

/**
 * Small local donut renderer, deliberately duplicated rather than imported
 * from opportunity-radar.tsx (that file's RadarDonut) -- this component
 * must never import Equity Radar code, per the "never copy Equity-specific
 * pipeline semantics into ETF Radar" requirement. Renders exact proportions
 * of whatever segments the caller computed; never infers or fabricates a
 * split itself.
 */
function EtfDonut({ segments, size = 128, thickness = 20 }: { segments: EtfDonutSegment[]; size?: number; thickness?: number }) {
  const total = segments.reduce((sum, segment) => sum + segment.value, 0);
  const radius = (size - thickness) / 2;
  const circumference = 2 * Math.PI * radius;
  let offsetAcc = 0;
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img" aria-label="ETF readiness donut chart">
      <circle cx={size / 2} cy={size / 2} r={radius} fill="none" stroke="var(--line)" strokeWidth={thickness} />
      <g transform={`rotate(-90 ${size / 2} ${size / 2})`}>
        {total > 0 ? segments.filter((segment) => segment.value > 0).map((segment, index) => {
          const dash = (segment.value / total) * circumference;
          const node = (
            <circle key={`${segment.label}-${index}`} cx={size / 2} cy={size / 2} r={radius} fill="none"
              stroke={segment.color} strokeWidth={thickness}
              strokeDasharray={`${dash} ${Math.max(circumference - dash, 0)}`} strokeDashoffset={-offsetAcc}>
              <title>{`${segment.label}: ${segment.value}`}</title>
            </circle>
          );
          offsetAcc += dash;
          return node;
        }) : null}
      </g>
      <text x="50%" y="50%" textAnchor="middle" dominantBaseline="central" className="donut-center-label">
        {total > 0 ? total : "N/A"}
      </text>
    </svg>
  );
}

function EtfDonutLegend({ segments }: { segments: EtfDonutSegment[] }) {
  return (
    <ul className="allocation-legend">
      {segments.filter((segment) => segment.value > 0).map((segment) => (
        <li key={segment.label}>
          <span className="allocation-legend-swatch" style={{ background: segment.color }} aria-hidden="true" />
          <span className="allocation-legend-label">{segment.label}</span>
          <span className="allocation-legend-value">{segment.value}</span>
        </li>
      ))}
      {segments.every((segment) => segment.value === 0) ? <li className="broker-note">No data yet for this cycle.</li> : null}
    </ul>
  );
}

/**
 * ETF Radar's own three-column analysis area: fund universe & coverage,
 * research outcomes (a genuine, non-overlapping partition of
 * data.candidates -- EVALUATED-with-recommendation / INSUFFICIENT_DATA /
 * TECHNICAL_FAILURE / excluded-before-scoring, each candidate counted in
 * exactly one bucket), category-leader/screening coverage, failure-reason
 * distribution (straight from data.excluded_by_reason, never fabricated),
 * and a publication/results status panel. Deliberately uses only ETF's own
 * fields (candidates/ranked/coverage/excluded_by_reason) -- never
 * the two-stage evaluation vocabulary, which belongs to Equity Radar's
 * own two-stage pipeline and have no ETF Radar equivalent.
 */
function EtfAnalysisPanels({ data, evaluated, eligible, dataGaps, failures, excluded, coveragePct, leaders, categories }: {
  data: EtfCycleResult; evaluated: number; eligible: number; dataGaps: number; failures: number;
  excluded: number; coveragePct: number; leaders: { category: string; leader: EtfCandidate }[]; categories: string[];
}) {
  const universeTotal = data.universe_count ?? data.candidates.length;
  const notYetEvaluated = Math.max(universeTotal - evaluated - excluded, 0);

  const coverageDonutSegments: EtfDonutSegment[] = [
    { label: "Evaluated", value: evaluated, color: "var(--positive)" },
    { label: "Excluded before scoring", value: excluded, color: "var(--line-strong)" },
  ];
  const outcomeDonutSegments: EtfDonutSegment[] = [
    { label: "Opportunity recommendations", value: eligible, color: "var(--positive)" },
    { label: "Insufficient data", value: dataGaps, color: "var(--warning)" },
    { label: "Technical failure", value: failures, color: "var(--negative)" },
    { label: "Evaluated, not recommended", value: Math.max(evaluated - eligible - dataGaps - failures, 0), color: "var(--info)" },
  ];

  const failureReasons = Object.entries(data.excluded_by_reason ?? {});

  return (
    <div className="etf-analysis-panels">
      <div className="etf-analysis-panel" aria-label="Fund universe and coverage">
        <h3>Fund universe &amp; coverage</h3>
        <dl className="radar-progress-counters">
          <div><dt>Universe size</dt><dd>{universeTotal}</dd></div>
          <div><dt>Evaluated</dt><dd>{evaluated}</dd></div>
          <div><dt>Not yet evaluated</dt><dd>{notYetEvaluated}</dd></div>
          <div><dt>Coverage</dt><dd>{coveragePct}%</dd></div>
        </dl>
        {data.coverage ? (
          <dl className="radar-progress-counters" aria-label="Official NSE coverage">
            {Object.entries(data.coverage).map(([key, value]) => (
              <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{value}</dd></div>
            ))}
          </dl>
        ) : null}
        <div className="allocation-donut-body">
          <EtfDonut segments={coverageDonutSegments} />
          <EtfDonutLegend segments={coverageDonutSegments} />
        </div>
      </div>

      <div className="etf-analysis-panel" aria-label="Research outcomes">
        <h3>Research outcomes</h3>
        <div className="allocation-donut-body">
          <EtfDonut segments={outcomeDonutSegments} />
          <EtfDonutLegend segments={outcomeDonutSegments} />
        </div>
        <p className="allocation-methodology">
          A genuine partition of {evaluated} evaluated funds: every evaluated fund is in exactly one of these
          buckets, never a combined or double-counted figure.
        </p>
      </div>

      <div className="etf-analysis-panel" aria-label="Category coverage">
        <h3>Category coverage</h3>
        <dl className="radar-progress-counters">
          <div><dt>Ranked ETFs</dt><dd>{data.ranked.length}</dd></div>
          <div><dt>Peer categories</dt><dd>{categories.length}</dd></div>
          <div><dt>Category leaders</dt><dd>{leaders.length}</dd></div>
        </dl>
        {leaders.length ? (
          <ul className="allocation-legend">
            {leaders.slice(0, 6).map(({ category, leader }) => (
              <li key={category}>
                <span className="allocation-legend-label">{category}</span>
                <span className="allocation-legend-value">{leader.symbol ?? "Unknown ETF"}</span>
              </li>
            ))}
          </ul>
        ) : <p className="broker-note">No category leaders yet.</p>}
      </div>

      <div className="etf-analysis-panel" aria-label="Failure reason distribution">
        <h3>Failure Reasons</h3>
        {failureReasons.length ? (
          <dl className="radar-progress-counters">
            {failureReasons.map(([reason, count]) => <div key={reason}><dt>{reason.replaceAll("_", " ")}</dt><dd>{count}</dd></div>)}
          </dl>
        ) : <p className="broker-note">No failure data available for this cycle yet.</p>}
      </div>

      <div className="etf-analysis-panel" aria-label="Research results and publication status">
        <h3>Research results</h3>
        <dl className="radar-progress-counters">
          <div><dt>Ranked ETFs</dt><dd>{data.ranked.length}</dd></div>
          <div><dt>Top opportunities published</dt><dd>{data.top_opportunities?.length ?? 0}</dd></div>
          <div><dt>Last published</dt><dd>{new Date(data.as_of).toLocaleString()}</dd></div>
        </dl>
      </div>
    </div>
  );
}

export function EtfRadarContent({ data, initialTab }: { data: EtfCycleResult; initialTab?: EtfTab }) {
  const [tab, setTab] = useState<EtfTab>(initialTab ?? "overview");
  const [search, setSearch] = useState("");
  const [categoryFilter, setCategoryFilter] = useState("ALL");
  const [sortKey, setSortKey] = useState<EtfSortKey>("rank");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");
  // Research Universe tab's own filter/sort state -- deliberately
  // separate from the Advanced Screener's (above), which stays scoped to
  // data.ranked and must keep behaving exactly as it did before.
  const [universeSearch, setUniverseSearch] = useState("");
  const [universeCategoryFilter, setUniverseCategoryFilter] = useState("ALL");
  const [universeStatusFilter, setUniverseStatusFilter] = useState("ALL");
  const [universeSortKey, setUniverseSortKey] = useState<EtfUniverseSortKey>("symbol");
  const [universeSortDir, setUniverseSortDir] = useState<"asc" | "desc">("asc");
  const toggleUniverseSort = (key: EtfUniverseSortKey) => {
    if (universeSortKey === key) { setUniverseSortDir((d) => (d === "asc" ? "desc" : "asc")); }
    else { setUniverseSortKey(key); setUniverseSortDir(key === "symbol" || key === "category" || key === "status" ? "asc" : "desc"); }
  };
  const universeSortArrow = (key: EtfUniverseSortKey) => (universeSortKey !== key ? "" : universeSortDir === "asc" ? " \u25b2" : " \u25bc");
  const universeSortAria = (key: EtfUniverseSortKey): "none" | "ascending" | "descending" => (universeSortKey !== key ? "none" : universeSortDir === "asc" ? "ascending" : "descending");
  // Comparison selection resolves against data.candidates (see `compared`
  // below), never data.ranked -- the fix for Task 2: any researched ETF,
  // including an insufficient-data/unranked one, can be compared.
  // Supports 2-5 selected ETFs.
  const [compareIds, setCompareIds] = useState<string[]>([]);
  const toggleCompare = (id: string) => setCompareIds((prev) => prev.includes(id) ? prev.filter((x) => x !== id) : prev.length < 5 ? [...prev, id] : prev);
  const toggleSort = (key: EtfSortKey) => {
    if (sortKey === key) { setSortDir((d) => (d === "asc" ? "desc" : "asc")); }
    else { setSortKey(key); setSortDir(key === "symbol" || key === "category" || key === "recommendation" ? "asc" : "desc"); }
  };
  const sortArrow = (key: EtfSortKey) => (sortKey !== key ? "" : sortDir === "asc" ? " ▲" : " ▼");
  const sortAria = (key: EtfSortKey): "none" | "ascending" | "descending" => (sortKey !== key ? "none" : sortDir === "asc" ? "ascending" : "descending");

  const evaluated = data.candidates.filter(c => c.disposition === "EVALUATED").length;
  const eligible = data.candidates.filter(c => c.recommendation === "OPPORTUNITY" || c.recommendation === "STRONG_OPPORTUNITY").length;
  const dataGaps = data.candidates.filter(c => c.disposition === "INSUFFICIENT_DATA" || c.recommendation === "INSUFFICIENT_DATA").length;
  const failures = data.candidates.filter(c => c.disposition === "TECHNICAL_FAILURE").length;
  const excluded = data.candidates.length - evaluated;
  const categories = Array.from(new Set(data.ranked.map(c => c.peer_group).filter((v): v is string => !!v))).sort();
  const visible = data.ranked
    .filter(c => categoryFilter === "ALL" || c.peer_group === categoryFilter)
    .filter(c => !search.trim() || (c.symbol ?? "").toLowerCase().includes(search.trim().toLowerCase())
      || (c.peer_group ?? "").toLowerCase().includes(search.trim().toLowerCase()))
    .sort((a, b) => {
      const dirMul = sortDir === "asc" ? 1 : -1;
      if (sortKey === "symbol") return dirMul * (a.symbol ?? "").localeCompare(b.symbol ?? "");
      if (sortKey === "category") return dirMul * (a.peer_group ?? "Unclassified").localeCompare(b.peer_group ?? "Unclassified");
      if (sortKey === "recommendation") return dirMul * (a.recommendation ?? a.disposition ?? "").localeCompare(b.recommendation ?? b.disposition ?? "");
      if (sortKey === "score") return dirMul * ((a.rule_engine_result?.overall_score ?? -1) - (b.rule_engine_result?.overall_score ?? -1));
      if (sortKey === "confidence") return dirMul * (a.rule_engine_result?.confidence ?? "").localeCompare(b.rule_engine_result?.confidence ?? "");
      if (sortKey === "completeness") return dirMul * ((a.rule_engine_result?.data_completeness ?? -1) - (b.rule_engine_result?.data_completeness ?? -1));
      if (sortKey === "expense") return dirMul * ((metricRaw(a.screening_metrics, "expense_ratio") ?? Number.POSITIVE_INFINITY) - (metricRaw(b.screening_metrics, "expense_ratio") ?? Number.POSITIVE_INFINITY));
      if (sortKey === "aum") return dirMul * ((metricRaw(a.screening_metrics, "aum") ?? -1) - (metricRaw(b.screening_metrics, "aum") ?? -1));
      return dirMul * ((a.rank ?? 1e9) - (b.rank ?? 1e9));
    });
  const leaders = categoryLeaders(data.ranked);
  const candidateId = (c: EtfCandidate) => c.global_instrument_id ?? c.symbol ?? "";
  // Full-universe category/status lists -- built from ALL candidates, not
  // just ranked ones, plus an explicit "Unclassified" bucket so a missing
  // peer_group is handled clearly rather than silently dropped.
  const universeCategories = Array.from(new Set(data.candidates.map(c => c.peer_group ?? "Unclassified"))).sort();
  const universeStatuses = Array.from(new Set(data.candidates.map(c => researchStatusLabel(c)))).sort();
  const visibleUniverse = data.candidates
    .filter(c => universeCategoryFilter === "ALL" || (c.peer_group ?? "Unclassified") === universeCategoryFilter)
    .filter(c => universeStatusFilter === "ALL" || researchStatusLabel(c) === universeStatusFilter)
    .filter(c => !universeSearch.trim() || (c.symbol ?? "").toLowerCase().includes(universeSearch.trim().toLowerCase())
      || (c.peer_group ?? "").toLowerCase().includes(universeSearch.trim().toLowerCase()))
    .sort((a, b) => {
      const dirMul = universeSortDir === "asc" ? 1 : -1;
      if (universeSortKey === "symbol") return dirMul * (a.symbol ?? "").localeCompare(b.symbol ?? "");
      if (universeSortKey === "category") return dirMul * (a.peer_group ?? "Unclassified").localeCompare(b.peer_group ?? "Unclassified");
      if (universeSortKey === "status") return dirMul * researchStatusLabel(a).localeCompare(researchStatusLabel(b));
      if (universeSortKey === "score") return dirMul * ((a.rule_engine_result?.overall_score ?? -1) - (b.rule_engine_result?.overall_score ?? -1));
      if (universeSortKey === "completeness") return dirMul * ((a.rule_engine_result?.data_completeness ?? -1) - (b.rule_engine_result?.data_completeness ?? -1));
      if (universeSortKey === "expense") return dirMul * ((metricRaw(a.screening_metrics, "expense_ratio") ?? Number.POSITIVE_INFINITY) - (metricRaw(b.screening_metrics, "expense_ratio") ?? Number.POSITIVE_INFINITY));
      return dirMul * ((metricRaw(a.screening_metrics, "aum") ?? -1) - (metricRaw(b.screening_metrics, "aum") ?? -1));
    });
  // Resolves against the FULL candidate universe (Task 2's fix): any
  // researched ETF can be compared, not only the handful that cleared
  // peer_results() into data.ranked.
  const compared = data.candidates.filter((c) => compareIds.includes(candidateId(c)));
  const coveragePct = data.candidates.length ? Math.round((evaluated / data.candidates.length) * 100) : 0;

  return (
    <Card className="wide-panel">
      <header className="etf-radar-header">
        <p>
          Last cycle: {new Date(data.as_of).toLocaleString()} ({freshnessLabel(data.as_of)}) · {data.ranked.length} ranked ETF{data.ranked.length === 1 ? "" : "s"}
          {" · "}{coveragePct}% of the universe evaluated
        </p>
        <p className="etf-coverage-caveat">
          Evaluation coverage counts how many ETFs the pipeline assessed; it is not a claim that each one has
          complete evidence. See Research Universe for per-ETF evidence gaps, or Data Quality for exclusion reasons.
        </p>
      </header>
      <EtfSectionTabs active={tab} onChange={setTab} />

      {tab === "overview" ? (<div className="radar-tab-panel">
        <dl className="opportunity-primary-metrics radar-summary-tiles">
          <div><dt>Universe size</dt><dd>{data.universe_count ?? data.candidates.length}</dd></div>
          <div><dt>Evaluated ETFs</dt><dd>{evaluated}</dd></div>
          <div><dt>Opportunity recommendations</dt><dd>{eligible}</dd></div>
          <div><dt>Excluded before scoring</dt><dd>{excluded}</dd></div>
          <div><dt>Insufficient data</dt><dd>{dataGaps}</dd></div>
          <div><dt>Technical failures</dt><dd>{failures}</dd></div>
          <div><dt>Ranked ETFs</dt><dd>{data.ranked.length}</dd></div>
        </dl>
        {data.scope === "FULL_OFFICIAL_NSE_ETF" ? <p>Complete official NSE snapshot assessed. Rankings compare verified peers only; missing peer classification cannot produce a buy shortlist.</p> : null}
        <EtfAnalysisPanels data={data} evaluated={evaluated} eligible={eligible} dataGaps={dataGaps} failures={failures} excluded={excluded} coveragePct={coveragePct} leaders={leaders} categories={categories} />
        {(data.universe_count ?? data.candidates.length) === 0
          ? <p role="alert">No ETF instruments were evaluated. ETF universe setup or discovery needs attention.</p>
          : !data.ranked.length ? <p>{failures > 0
            ? "ETF evaluation encountered technical failures. Review the failure reasons in Data Quality before interpreting the empty ranking."
            : dataGaps > 0 ? "ETF data prerequisites are missing for the universe evaluated. Review Data Quality and synchronize verified ETF evidence."
            : "No ETFs were ranked yet. Review the evaluation and exclusion counts in Data Quality."}</p> : null}
      </div>) : null}

      {tab === "top" ? (<div className="radar-tab-panel">
        {data.top_opportunities?.length ? (
          <section className="etf-top-opportunities" aria-label="Top ETF opportunities">
            <h3>Top opportunities</h3>
            <p>Research queue ordered by peer rank, then score -- not a claim of interchangeable exposures across categories.</p>
            <div className="opportunity-cards">
              {data.top_opportunities.map((item) => <EtfRadarCard key={`top-${item.global_instrument_id ?? item.symbol}`} item={item} />)}
            </div>
          </section>
        ) : (
          <div className="radar-empty-state">
            <h3>No shortlist yet</h3>
            <p>{failures > 0
              ? `${failures} ETF${failures === 1 ? "" : "s"} hit a technical failure during evaluation, and no ETF currently clears the ranking bar.`
              : dataGaps > 0
                ? `${dataGaps} ETF${dataGaps === 1 ? "" : "s"} lack sufficient verified history, holdings, or classification evidence to be ranked.`
                : "No ETF currently clears the eligibility and evidence bar required for ranking."}</p>
            <p>The Advanced Screener and ETF Comparison tabs remain useful even without a ranked shortlist.</p>
          </div>
        )}
      </div>) : null}

      {tab === "leaders" ? (<div className="radar-tab-panel">
        <h3>Category leaders</h3>
        <p>The top-ranked ETF within each verified peer category.</p>
        {leaders.length ? (
          <table className="radar-dense-table">
            <thead><tr><th>Category</th><th>Leader</th><th>Rank</th><th>Score</th><th>Recommendation</th></tr></thead>
            <tbody>
              {leaders.map(({ category, leader }) => (
                <tr key={category}>
                  <td>{category}</td>
                  <td>{leader.symbol ?? "Unknown ETF"}</td>
                  <td>{leader.rank ?? "—"}</td>
                  <td>{leader.rule_engine_result?.overall_score != null ? leader.rule_engine_result.overall_score.toFixed(1) : "—"}</td>
                  <td>{leader.recommendation ?? leader.disposition}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <p>No ranked ETF exists yet in any category.</p>}
      </div>) : null}

      {tab === "universe" ? (<div className="radar-tab-panel">
        <h3>Research universe</h3>
        <p>
          Every assessed ETF from this cycle ({data.candidates.length} total), including candidates with
          insufficient evidence to qualify as a recommendation. A row here is NOT a buy recommendation unless
          its Research status reads "Qualified opportunity".
        </p>
        <div className="etf-radar-controls radar-filter-rail" role="search">
          <input type="search" aria-label="Search research universe by symbol or category" placeholder="Search symbol or category…"
            value={universeSearch} onChange={(e) => setUniverseSearch(e.target.value)} />
          <select aria-label="Filter research universe by category" value={universeCategoryFilter} onChange={(e) => setUniverseCategoryFilter(e.target.value)}>
            <option value="ALL">All categories</option>
            {universeCategories.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
          <select aria-label="Filter research universe by research status" value={universeStatusFilter} onChange={(e) => setUniverseStatusFilter(e.target.value)}>
            <option value="ALL">All statuses</option>
            {universeStatuses.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
          <span className="radar-result-count">{visibleUniverse.length} of {data.candidates.length} result{data.candidates.length === 1 ? "" : "s"}</span>
        </div>
        {visibleUniverse.length ? (
        <div className="radar-table-scroll">
          <table className="radar-dense-table radar-screener-table">
            <thead>
              <tr>
                <th aria-sort={universeSortAria("symbol")}><button type="button" className="radar-sort-button" onClick={() => toggleUniverseSort("symbol")}>Symbol{universeSortArrow("symbol")}</button></th>
                <th aria-sort={universeSortAria("category")}><button type="button" className="radar-sort-button" onClick={() => toggleUniverseSort("category")}>Category{universeSortArrow("category")}</button></th>
                <th aria-sort={universeSortAria("status")}><button type="button" className="radar-sort-button" onClick={() => toggleUniverseSort("status")}>Research status{universeSortArrow("status")}</button></th>
                <th aria-sort={universeSortAria("score")}><button type="button" className="radar-sort-button" onClick={() => toggleUniverseSort("score")}>Score{universeSortArrow("score")}</button></th>
                <th aria-sort={universeSortAria("completeness")}><button type="button" className="radar-sort-button" onClick={() => toggleUniverseSort("completeness")}>Data completeness{universeSortArrow("completeness")}</button></th>
                <th aria-sort={universeSortAria("expense")}><button type="button" className="radar-sort-button" onClick={() => toggleUniverseSort("expense")}>Expense ratio{universeSortArrow("expense")}</button></th>
                <th aria-sort={universeSortAria("aum")}><button type="button" className="radar-sort-button" onClick={() => toggleUniverseSort("aum")}>AUM{universeSortArrow("aum")}</button></th>
                <th scope="col">Confidence</th>
                <th scope="col">Missing evidence</th>
                <th scope="col">Historical acquisition</th>
                <th scope="col">Compare</th>
                <th scope="col">Details</th>
              </tr>
            </thead>
            <tbody>{visibleUniverse.map((item) => {
              const id = candidateId(item);
              const rowKey = id || item.symbol || "";
              const result = item.rule_engine_result;
              return (
                <Fragment key={rowKey}>
                  <tr className="etf-screener-row">
                    <td>{item.symbol ?? "Unknown ETF"}</td>
                    <td>{item.peer_group ?? "Unclassified"}</td>
                    <td>{researchStatusLabel(item)}</td>
                    <td>{result?.overall_score != null ? result.overall_score.toFixed(1) : "—"}</td>
                    <td>{result ? pct(result.data_completeness * 100) : "—"}</td>
                    <td>{metricDisplay(item.screening_metrics, "expense_ratio")}</td>
                    <td>{metricDisplay(item.screening_metrics, "aum")}</td>
                    <td>{result?.confidence ?? "—"}</td>
                    <td>{item.missing_data?.length ? item.missing_data.join(", ") : "None reported"}</td>
                    <td>{historicalAcquisitionLabel(item)}</td>
                    <td>{id ? <label className="etf-compare-select">
                      <input type="checkbox" aria-label={`Compare ${item.symbol ?? id}`} checked={compareIds.includes(id)} onChange={() => toggleCompare(id)} /> Compare
                    </label> : "—"}</td>
                    <td><span className="radar-table-hint">See row below</span></td>
                  </tr>
                  <tr className="etf-screener-detail-row">
                    <td colSpan={12}>
                      <details>
                        <summary>View full research detail for {item.symbol ?? "this ETF"}</summary>
                        <EtfRadarCard item={item} />
                        {item.limitations?.length ? <p className="etf-universe-limitations">Limitations: {item.limitations.join("; ")}</p> : null}
                        {item.source_references?.length ? (
                          <ul className="etf-universe-source-references">
                            {item.source_references.map((ref) => (
                              <li key={`${rowKey}-${ref.metric}`}>{ref.metric}: {ref.value} (as of {ref.as_of}, {ref.provider})</li>
                            ))}
                          </ul>
                        ) : null}
                      </details>
                    </td>
                  </tr>
                </Fragment>
              );
            })}</tbody>
          </table>
        </div>
        ) : <p>No ETF matches this search/filter.</p>}
      </div>) : null}

      {tab === "screener" ? (<div className="radar-tab-panel">
        <h3>Advanced screener</h3>
        {data.ranked.length ? (
          <div className="etf-radar-controls radar-filter-rail" role="search">
            <input type="search" aria-label="Search ETF rankings by symbol or category" placeholder="Search symbol or category…"
              value={search} onChange={(e) => setSearch(e.target.value)} />
            <select aria-label="Filter by peer category" value={categoryFilter} onChange={(e) => setCategoryFilter(e.target.value)}>
              <option value="ALL">All categories</option>
              {categories.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
            <span className="radar-result-count">{visible.length} of {data.ranked.length} result{data.ranked.length === 1 ? "" : "s"}</span>
          </div>
        ) : null}
        {visible.length ? (
        <div className="radar-table-scroll">
          <table className="radar-dense-table radar-screener-table">
            <thead>
              <tr>
                <th aria-sort={sortAria("symbol")}><button type="button" className="radar-sort-button" onClick={() => toggleSort("symbol")}>Symbol{sortArrow("symbol")}</button></th>
                <th aria-sort={sortAria("category")}><button type="button" className="radar-sort-button" onClick={() => toggleSort("category")}>Category{sortArrow("category")}</button></th>
                <th aria-sort={sortAria("recommendation")}><button type="button" className="radar-sort-button" onClick={() => toggleSort("recommendation")}>Recommendation{sortArrow("recommendation")}</button></th>
                <th aria-sort={sortAria("score")}><button type="button" className="radar-sort-button" onClick={() => toggleSort("score")}>Score{sortArrow("score")}</button></th>
                <th aria-sort={sortAria("confidence")}><button type="button" className="radar-sort-button" onClick={() => toggleSort("confidence")}>Confidence{sortArrow("confidence")}</button></th>
                <th aria-sort={sortAria("completeness")}><button type="button" className="radar-sort-button" onClick={() => toggleSort("completeness")}>Data completeness{sortArrow("completeness")}</button></th>
                <th aria-sort={sortAria("expense")}><button type="button" className="radar-sort-button" onClick={() => toggleSort("expense")}>Expense ratio{sortArrow("expense")}</button></th>
                <th aria-sort={sortAria("aum")}><button type="button" className="radar-sort-button" onClick={() => toggleSort("aum")}>AUM{sortArrow("aum")}</button></th>
                <th scope="col">Compare</th>
                <th scope="col">Details</th>
              </tr>
            </thead>
            <tbody>{visible.map((item) => {
              const id = item.global_instrument_id ?? item.symbol ?? "";
              const rowKey = id || item.symbol || "";
              const result = item.rule_engine_result;
              return (
                <Fragment key={rowKey}>
                  <tr className="etf-screener-row">
                    <td>{item.symbol ?? "Unknown ETF"}</td>
                    <td>{item.peer_group ?? "Unclassified"}</td>
                    <td>{item.recommendation ?? item.disposition}</td>
                    <td>{result?.overall_score != null ? result.overall_score.toFixed(1) : "—"}</td>
                    <td>{result?.confidence ?? "—"}</td>
                    <td>{result ? pct(result.data_completeness * 100) : "—"}</td>
                    <td>{metricDisplay(item.screening_metrics, "expense_ratio")}</td>
                    <td>{metricDisplay(item.screening_metrics, "aum")}</td>
                    <td>{id ? <label className="etf-compare-select">
                      <input type="checkbox" aria-label={`Compare ${item.symbol ?? id}`} checked={compareIds.includes(id)} onChange={() => toggleCompare(id)} /> Compare
                    </label> : "—"}</td>
                    <td><span className="radar-table-hint">See row below</span></td>
                  </tr>
                  <tr className="etf-screener-detail-row">
                    <td colSpan={10}>
                      <details>
                        <summary>View full detail for {item.symbol ?? "this ETF"}</summary>
                        <EtfRadarCard item={item} />
                      </details>
                    </td>
                  </tr>
                </Fragment>
              );
            })}</tbody>
          </table>
        </div>
        ) : null}
        {data.ranked.length > 0 && !visible.length ? <p>No ranked ETFs match this search/filter.</p> : null}
        {!data.ranked.length ? <p>No ranked ETFs are available to screen yet.</p> : null}
      </div>) : null}

      {tab === "compare" ? (<div className="radar-tab-panel">
        <h3>ETF comparison</h3>
        {compared.length ? (
          <div className="radar-table-scroll">
            <table className="radar-dense-table radar-compare-table">
              <thead><tr><th>Metric</th>{compared.map((c) => <th key={c.global_instrument_id ?? c.symbol}>{c.symbol ?? "Unknown ETF"}</th>)}</tr></thead>
              <tbody>
                {SCREENER_METRIC_ROWS.map(([label, accessor]) => (
                  <tr key={label}><td>{label}</td>{compared.map((c) => <td key={(c.global_instrument_id ?? c.symbol ?? "") + label}>{accessor(c)}</td>)}</tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <p>Select 2–5 ETFs using the Compare checkbox in the Research Universe or Advanced Screener tab to see them side-by-side here.</p>}
      </div>) : null}

      {tab === "quality" ? (<div className="radar-tab-panel">
        <h3>Data quality &amp; diagnostics</h3>
        {Object.keys(data.excluded_by_reason ?? {}).length ? <>
          <p>Exclusion reasons</p>
          <ul>{Object.entries(data.excluded_by_reason ?? {}).map(([reason, count]) => <li key={reason}>{reason}: {count}</li>)}</ul>
        </> : null}
        {data.coverage ? <dl aria-label="Official NSE coverage">{Object.entries(data.coverage).map(([key, value]) =>
          <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{value}</dd></div>)}</dl> : null}
        {data.scope === "FULL_OFFICIAL_NSE_ETF" ? <details><summary>All assessed ETFs ({data.candidates.length})</summary><ul>
          {data.candidates.map(item => <li key={item.global_instrument_id ?? item.symbol}>{item.symbol ?? "Unknown ETF"}: {item.disposition}; {item.recommendation ?? item.reason}{item.missing_data?.length ? `; missing: ${item.missing_data.join(", ")}` : ""}</li>)}
        </ul></details> : null}
      </div>) : null}
    </Card>
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
  coverage?: { official: number | null; completed: number };
};

// Response shape of GET /api/v1/etf-radar/preflight (app/etf_ingestion.py's
// etf_preflight, plus the recovery/authorization fields main.py's
// _controlled_etf_preflight adds on top) -- read-only, never triggers
// acquisition or a cycle.
type EtfBoundedPreflightInstrument = {
  instrument_id: string;
  symbol: string;
  isin: string;
  canonical_identity_matches: boolean;
  mandatory_evidence_ready: boolean;
  blocking_reasons: string[];
};

type EtfBoundedPreflight = {
  status: "READY" | "BLOCKED";
  scope: string;
  candidate_count: number;
  ready_count: number;
  instruments: EtfBoundedPreflightInstrument[];
  recovery_paused: boolean;
  active_cycle_id: string | null;
  manual_execution_authorized: boolean;
  can_submit: boolean;
};

const ETF_UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// Client-side only (not a backend capability): the current ETF API surface
// has no "discover the active cycle" endpoint, so a page refresh cannot
// recover progress from the server alone. Persisting the cycle_id we were
// already handed, in this tab's sessionStorage, lets a refresh resume
// polling the real status endpoint for that same cycle -- it never
// fabricates state, it only remembers an ID the backend already gave us.
const ETF_ACTIVE_CYCLE_STORAGE_KEY = "aip.etfRadar.activeCycleId";

export function EtfRadar({ roles = [] }: { roles?: string[] } = {}) {
  const [data, setData] = useState<EtfCycleResult | null>(null);
  const [status, setStatus] = useState<EtfRadarStatus>("loading");
  const [activeCycle, setActiveCycle] = useState<EtfActiveCycle | null>(null);
  // Bounded/controlled ETF admission -- deliberately separate state from the
  // full-universe run above: this never touches full-universe's own flag,
  // button, or endpoint. Submitting a bounded cycle still lands in the same
  // `activeCycle`/polling state as a full-universe cycle, because the
  // backend only ever has one active ETF cycle at a time
  // (active_cycle_run('ETF')) and the progress/terminal-result rendering
  // below is already correct for either scope's cycle_id.
  const [boundedIdsText, setBoundedIdsText] = useState("");
  const [boundedPreflight, setBoundedPreflight] = useState<EtfBoundedPreflight | null>(null);
  const [boundedPreflightError, setBoundedPreflightError] = useState<string | null>(null);
  const [boundedPreflightLoading, setBoundedPreflightLoading] = useState(false);
  const [boundedConfirmed, setBoundedConfirmed] = useState(false);
  const [boundedSubmitting, setBoundedSubmitting] = useState(false);
  const [boundedSubmitError, setBoundedSubmitError] = useState<string | null>(null);
  // Bounded market-evidence refresh -- the ONLY way to move a BLOCKED
  // (stale MARKET_PRICE/TRADING_VOLUME) preflight forward: preflight
  // itself is read-only (app/etf_ingestion.py's etf_preflight docstring:
  // "no provider, acquisition, or lifecycle writes") and admission into a
  // controlled cycle requires can_submit, which requires READY_FRESH for
  // every mandatory requirement (app/etf_readiness.py: "only READY_FRESH
  // counts as ready"), so without this a stale ETF could never become
  // submittable through this UI at all. This reuses the existing,
  // already-approved, admin-gated, bounded (1-5 instrument) acquisition
  // endpoint POST /api/v1/etf-radar/evidence (app/etf_ingestion.py's
  // ingest_quotes, already wired in app/main.py's _ingest_etf) -- no new
  // acquisition system, no scheduler, no full-universe call. Never run
  // automatically; always an explicit click, same as preflight/submit.
  const [boundedRefreshing, setBoundedRefreshing] = useState(false);
  const [boundedRefreshError, setBoundedRefreshError] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    // Read the browser-owned store after hydration; cancel recovery on unmount.
    Promise.resolve().then(() => {
      if (!active) return;
      try {
        const storedCycleId = window.sessionStorage.getItem(ETF_ACTIVE_CYCLE_STORAGE_KEY);
        if (storedCycleId) setActiveCycle(previous => previous ?? { cycleId: storedCycleId, status: "RUNNING", startedAt: null, updatedAt: null, errorCode: null, pollError: false });
      } catch { /* Storage may be disabled. */ }
    });
    return () => { active = false; };
  }, []);
  const cycleId = activeCycle?.cycleId;
  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    let active = true;
    request<EtfCycleResult>("/api/v1/etf-radar/current")
      .then((value) => { if (active) { setData(value); setStatus("ready"); } })
      .catch((failure: { status?: number }) => {
        if (!active) return;
        if (failure?.status === 404) { setData(null); setStatus("empty"); } else setStatus("error");
      });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (!cycleId) return;
    let active = true;
    const poll = () => {
      request<{ status: string; updated_at?: string; created_at?: string; error_code?: string; coverage?: { official: number | null; completed: number } }>(
        `/api/v1/etf-radar/cycles/${cycleId}/status`
      )
        .then((value) => {
          if (!active) return;
          setActiveCycle((previous) => previous && previous.cycleId === cycleId
            ? { ...previous, status: value.status, updatedAt: value.updated_at ?? previous.updatedAt,
                startedAt: previous.startedAt ?? value.created_at ?? null,
                errorCode: value.error_code ?? previous.errorCode, pollError: false, coverage: value.coverage }
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
          setActiveCycle((previous) => previous && previous.cycleId === cycleId
            ? { ...previous, pollError: true } : previous);
          pollTimer.current = setTimeout(poll, 2500);
        });
    };
    poll();
    return () => { active = false; if (pollTimer.current) clearTimeout(pollTimer.current); };
    // Re-run whenever the tracked cycle_id changes; status itself is
    // updated inside the closure above, not via this dependency.
  }, [cycleId]);

  const runCycle = () => {
    request<{ cycle_id: string; status: string; updated_at?: string }>(
      "/api/v1/etf-radar/full-universe-cycles", { method: "POST", body: JSON.stringify({ max_universe: 2000, max_requests: 1500, max_seconds: 3600 }) }
    )
      .then((value) => {
        setActiveCycle({ cycleId: value.cycle_id, status: value.status, startedAt: value.updated_at ?? null,
                          updatedAt: value.updated_at ?? null, errorCode: null, pollError: false });
        try { window.sessionStorage.setItem(ETF_ACTIVE_CYCLE_STORAGE_KEY, value.cycle_id); } catch { /* ignore */ }
      })
      .catch(() => setStatus("error"));
  };

  const running = !!activeCycle && !isTerminalRadarStatus(activeCycle.status);

  // Parses the textarea into 1-5 distinct, syntactically-valid instrument
  // UUIDs, or returns an error string. Purely client-side input hygiene --
  // the backend (EtfEvidenceRequest, min_length=1/max_length=5) is the real
  // authority and is re-checked by its own 422 response regardless.
  function parseBoundedInstrumentIds(): { ids: string[] } | { error: string } {
    const ids = boundedIdsText.split(/[\s,]+/).map((value) => value.trim()).filter(Boolean);
    if (ids.length < 1 || ids.length > 5) return { error: "Enter between 1 and 5 ETF instrument IDs." };
    const invalid = ids.find((id) => !ETF_UUID_PATTERN.test(id));
    if (invalid) return { error: `Not a valid instrument ID: ${invalid}` };
    const unique = new Set(ids.map((id) => id.toLowerCase()));
    if (unique.size !== ids.length) return { error: "Instrument IDs must be distinct." };
    return { ids };
  }

  const checkBoundedPreflight = () => {
    setBoundedPreflightError(null);
    setBoundedSubmitError(null);
    setBoundedConfirmed(false);
    const parsed = parseBoundedInstrumentIds();
    if ("error" in parsed) { setBoundedPreflight(null); setBoundedPreflightError(parsed.error); return; }
    setBoundedPreflightLoading(true);
    const query = parsed.ids.map((id) => `instrument_ids=${encodeURIComponent(id)}`).join("&");
    request<EtfBoundedPreflight>(`/api/v1/etf-radar/preflight?${query}`)
      .then((value) => { setBoundedPreflight(value); })
      .catch((failure: { message?: string }) => {
        setBoundedPreflight(null);
        setBoundedPreflightError(failure?.message ?? "Preflight check failed.");
      })
      .finally(() => setBoundedPreflightLoading(false));
  };

  // Refreshes MARKET_PRICE/TRADING_VOLUME evidence for exactly the
  // currently-entered, validated instrument IDs via the existing bounded
  // ingestion endpoint, then re-checks preflight so the panel reflects
  // the refreshed freshness immediately. Does not submit a cycle.
  const refreshBoundedEvidence = () => {
    const parsed = parseBoundedInstrumentIds();
    if ("error" in parsed || boundedRefreshing || boundedPreflightLoading) return;
    setBoundedRefreshing(true);
    setBoundedRefreshError(null);
    request<{ status: string }>("/api/v1/etf-radar/evidence", { method: "POST", body: JSON.stringify({ instrument_ids: parsed.ids }) })
      .then(() => { checkBoundedPreflight(); })
      .catch((failure: { message?: string }) => setBoundedRefreshError(failure?.message ?? "Could not refresh market evidence."))
      .finally(() => setBoundedRefreshing(false));
  };

  // POST /controlled-cycles only -- this bounded action never calls, and
  // never falls back to, the OTHER (full-universe) ETF endpoint above.
  // Requires a prior READY, submittable preflight result for these exact
  // IDs plus the explicit confirmation checkbox below; never submitted
  // automatically.
  const submitBoundedCycle = () => {
    const parsed = parseBoundedInstrumentIds();
    if ("error" in parsed || !boundedPreflight || !boundedPreflight.can_submit || !boundedConfirmed || running) return;
    setBoundedSubmitting(true);
    setBoundedSubmitError(null);
    request<{ cycle_id: string; status: string; updated_at?: string }>(
      "/api/v1/etf-radar/controlled-cycles", { method: "POST", body: JSON.stringify({ instrument_ids: parsed.ids }) }
    )
      .then((value) => {
        setActiveCycle({ cycleId: value.cycle_id, status: value.status, startedAt: value.updated_at ?? null,
                          updatedAt: value.updated_at ?? null, errorCode: null, pollError: false });
        try { window.sessionStorage.setItem(ETF_ACTIVE_CYCLE_STORAGE_KEY, value.cycle_id); } catch { /* ignore */ }
        setBoundedIdsText(""); setBoundedPreflight(null); setBoundedConfirmed(false);
      })
      .catch((failure: { message?: string }) => setBoundedSubmitError(failure?.message ?? "Could not start the bounded ETF cycle."))
      .finally(() => setBoundedSubmitting(false));
  };
  // Live counts come only from this cycle's durable official-snapshot checkpoints.
  const terminalData = activeCycle?.status === "COMPLETED" && data?.cycle_id === activeCycle.cycleId ? data : null;
  const counters: RadarProgressCounter[] = !terminalData ? (activeCycle?.coverage ? [
    { label: "Official NSE universe", value: activeCycle.coverage.official },
    { label: "First-pass outcomes", value: activeCycle.coverage.completed },
  ] : []) : [
    { label: "Total universe", value: terminalData.candidates.length },
    { label: "Evaluated candidates", value: terminalData.candidates.filter((c) => c.disposition === "EVALUATED").length },
    { label: "Technical failures", value: terminalData.candidates.filter((c) => c.disposition === "TECHNICAL_FAILURE").length },
    // Readiness exclusions (insufficient evidence) kept separate from
    // eligibility filtering (ineligible/unsupported fund type), per
    // app/etf_opportunity_cycle.py's own admission-outcome vocabulary.
    { label: "Readiness excluded", value: terminalData.candidates.filter((c) => c.disposition === "INSUFFICIENT_DATA").length },
    { label: "Eligibility filtered", value: terminalData.candidates.filter((c) => c.disposition === "INELIGIBLE" || c.disposition === "UNSUPPORTED").length },
    { label: "Cancelled", value: terminalData.candidates.filter((c) => c.disposition === "CANCELLED").length },
    { label: "Pod restarts", value: null },
    // AVOID is a real category in EtfRecommendation (see app/etf_rule_engine.py)
    // -- "where supported" from the RADAR UI + API ROUTING spec, and ETF is
    // the radar that supports it.
    { label: "AVOID count", value: terminalData.candidates.filter((c) => c.recommendation === "AVOID").length },
    // ETF's recommendation vocabulary (STRONG_OPPORTUNITY/OPPORTUNITY/WATCH/
    // AVOID/INSUFFICIENT_DATA) has no literal BUY/SELL/HOLD category --
    // mapping WATCH to "HOLD" or OPPORTUNITY to "BUY" would be inventing a
    // correspondence the backend never states, so these stay Unavailable.
    { label: "BUY count", value: null },
    { label: "SELL count", value: null },
    { label: "HOLD/neutral count", value: null },
  ];

  return (
    <section className="opportunity-radar etf-radar" aria-label="ETF radar">
      {/* Clear, self-contained execution/status area -- separated from the
         results below so a running or terminal cycle's own state is never
         visually mixed with the last persisted ranking. */}
      <Card className="wide-panel etf-radar-execution">
        <div className="panel-header compact">
          <h2>ETF radar</h2>
          <button type="button" className="button button-primary radar-run-action" disabled={running} onClick={runCycle}>
            {running ? "Running…" : "Run full NSE ETF Radar"}
          </button>
        </div>
        {activeCycle ? (
          <RadarProgressPanel radarType="ETF" cycleId={activeCycle.cycleId} status={activeCycle.status}
            startedAt={activeCycle.startedAt} updatedAt={activeCycle.updatedAt} counters={counters}
            percent={null} errorCode={activeCycle.errorCode} pollError={activeCycle.pollError} />
        ) : null}
      </Card>
      {roles.includes("ADMIN") ? (
        <Card className="wide-panel etf-radar-bounded" aria-label="Bounded ETF radar">
          {/* Separate, admin-only, LOCAL-only workflow -- never calls or
             falls back to /full-universe-cycles, never touches the
             full-universe flag/button above, and never submits without an
             explicit preflight check and confirmation. Server-side
             authorization (environment==LOCAL, scheduler disabled, the
             explicit AIP_RESEARCH_ETF_MANUAL_EXECUTION_ENABLED flag) is the
             real gate -- see app/main.py's _manual_etf_enabled(); this
             section only decides what to show, never what to allow. */}
          <div className="panel-header compact">
            <h2>Run Bounded ETF Radar</h2>
          </div>
          <p>
            Run a controlled, preflight-checked cycle for 1-5 specific, canonical ETF instrument IDs --
            never the full NSE ETF universe. Available only when this environment is explicitly
            authorized for bounded manual execution.
          </p>
          <label htmlFor="etf-bounded-ids">ETF instrument IDs (1-5, comma or newline separated)</label>
          <textarea id="etf-bounded-ids" rows={3} value={boundedIdsText}
            disabled={boundedPreflightLoading || boundedSubmitting}
            onChange={(event) => { setBoundedIdsText(event.target.value); setBoundedPreflight(null); setBoundedConfirmed(false); setBoundedPreflightError(null); }} />
          <button type="button" className="button" disabled={boundedPreflightLoading || boundedSubmitting || !boundedIdsText.trim()}
            onClick={checkBoundedPreflight}>
            {boundedPreflightLoading ? "Checking preflight…" : "Check preflight"}
          </button>
          {boundedPreflightError ? <p role="alert">{boundedPreflightError}</p> : null}
          {boundedPreflight ? (
            <div className="etf-radar-bounded-preflight">
              <p>
                Preflight: status={boundedPreflight.status}, ready={boundedPreflight.ready_count}/{boundedPreflight.candidate_count},
                recovery_paused={String(boundedPreflight.recovery_paused)}, manual_execution_authorized={String(boundedPreflight.manual_execution_authorized)}
              </p>
              {boundedPreflight.active_cycle_id ? <p role="alert">Another ETF cycle is already active ({boundedPreflight.active_cycle_id}); it must finish before a bounded cycle can be submitted.</p> : null}
              {!boundedPreflight.can_submit && !boundedPreflight.active_cycle_id ? (
                <>
                  <p role="alert">
                    {boundedPreflight.status !== "READY"
                      ? "Not all selected ETFs have mandatory evidence ready -- see blocking reasons below."
                      : boundedPreflight.recovery_paused && !boundedPreflight.manual_execution_authorized
                        ? "Recovery is paused and bounded manual execution is not authorized in this environment (requires LOCAL, scheduler disabled, and the explicit manual-execution flag)."
                        : "Submission is currently unavailable."}
                  </p>
                  {boundedPreflight.status !== "READY" ? (
                    <>
                      <button type="button" className="button" disabled={boundedRefreshing || boundedPreflightLoading}
                        onClick={refreshBoundedEvidence}>
                        {boundedRefreshing ? "Refreshing market evidence…" : "Refresh market price & volume evidence"}
                      </button>
                      <p>
                        Fetches current MARKET_PRICE/TRADING_VOLUME for these exact ETF(s) only -- not the
                        scheduler, not the full universe -- then re-checks preflight. Stale data alone never
                        fabricates readiness; this only re-acquires it.
                      </p>
                      {boundedRefreshError ? <p role="alert">{boundedRefreshError}</p> : null}
                    </>
                  ) : null}
                </>
              ) : null}
              <ul>
                {boundedPreflight.instruments.map((item) => (
                  <li key={item.instrument_id}>
                    {item.symbol} ({item.instrument_id}): {item.mandatory_evidence_ready ? "ready" : "not ready"}
                    {item.blocking_reasons.length ? ` -- ${item.blocking_reasons.join(", ")}` : ""}
                  </li>
                ))}
              </ul>
              {boundedPreflight.can_submit ? (
                <>
                  <label>
                    <input type="checkbox" checked={boundedConfirmed} disabled={boundedSubmitting || running}
                      onChange={(event) => setBoundedConfirmed(event.target.checked)} />
                    {` I confirm I want to run a bounded ETF Radar cycle for these ${boundedPreflight.candidate_count} ETF(s).`}
                  </label>
                  <button type="button" className="button button-primary" disabled={!boundedConfirmed || boundedSubmitting || running}
                    onClick={submitBoundedCycle}>
                    {boundedSubmitting ? "Starting…" : running ? "Another ETF cycle is running…" : "Run Bounded ETF Radar"}
                  </button>
                </>
              ) : null}
            </div>
          ) : null}
          {boundedSubmitError ? <p role="alert">{boundedSubmitError}</p> : null}
        </Card>
      ) : null}
      {status === "error" ? (
        <Card className="wide-panel">
          <EmptyState title="ETF radar unavailable" message="The last persisted ETF radar result could not be loaded. Try again shortly." />
        </Card>
      ) : null}
      {status === "empty" ? (
        <Card className="wide-panel">
          <EmptyState title="No ETF radar cycle yet" message="No ETF radar cycle has been run and persisted yet. Run the full NSE ETF Radar above to evaluate the current ETF universe." />
        </Card>
      ) : null}
      {status === "loading" ? (
        <Card className="wide-panel"><Skeleton rows={4} /></Card>
      ) : null}
      {status === "ready" && data ? <EtfRadarContent data={data} /> : null}
    </section>
  );
}
