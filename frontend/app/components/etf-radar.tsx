"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { request } from "../lib/portfolio-api";
import {
  candidateId,
  completeFundEvidence,
  entryLabel,
  finite,
  humanize,
  METRICS,
  metricDisplay,
  normalizeEtfCandidate,
  normalizeEtfCycle,
  outcomePartition,
  researchStatusLabel,
  verifiedEntry,
  type EtfCandidate,
  type EtfCycleResult,
} from "../lib/etf-radar-model";
import {
  RadarProgressPanel,
  isTerminalRadarStatus,
  type RadarProgressCounter,
} from "./radar-progress";
import { Card, EmptyState, Skeleton } from "./ui";
import { EtfOpportunityTable } from "./etf-opportunity-table";
import { EtfPriceHistory } from "./etf-price-history";
export type {
  EtfCandidate,
  EtfCycleResult,
  EtfFactorResult,
  EtfRiskGate,
  EtfRuleEngineResult,
  EtfScreeningMetric,
  EtfEntryAssessment,
} from "../lib/etf-radar-model";

const FACTOR_LABELS = [
  ["PERFORMANCE_MOMENTUM", "Performance / momentum"],
  ["RISK_DRAWDOWN_VOLATILITY", "Risk (drawdown / volatility)"],
  ["LIQUIDITY", "Liquidity"],
  ["TRACKING_QUALITY", "Tracking quality"],
  ["COST", "Cost (expense ratio)"],
  ["NAV_PREMIUM_DISCOUNT", "NAV premium / discount"],
  ["DIVERSIFICATION", "Diversification"],
];
const pct = (v: number | null | undefined) =>
  finite(v) == null ? "Unavailable" : `${(v as number).toFixed(0)}%`;
const dateLabel = (v: string) =>
  Number.isFinite(Date.parse(v)) ? new Date(v).toLocaleString() : "Unavailable";
const safeSource = (url: string) => {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" || parsed.protocol === "http:"
      ? url
      : null;
  } catch {
    return null;
  }
};
const historyLabel = (c: EtfCandidate) =>
  c.historical_acquisition
    ? `${humanize(c.historical_acquisition.status)}${c.historical_acquisition.reason ? ` (${c.historical_acquisition.reason})` : ""}`
    : "Acquisition outcome not recorded";

/** Also accepts legacy excluded records, whose absent analysis is not a successful result. */
export function EtfRadarCard({ item: raw }: { item: EtfCandidate }) {
  const item = normalizeEtfCandidate(raw),
    result = item.rule_engine_result;
  const triggeredGates = item.risk_gates.filter((g) => g.triggered);
  const factors = result?.factor_results ?? [];
  return (
    <article className="opportunity-card etf-radar-card">
      <header className="opportunity-card-header">
        <div>
          <h3>{item.symbol ?? "Unknown ETF"}</h3>
          <p>
            {item.fund_name ?? item.canonical_name ?? "Fund name unavailable"}
          </p>
          <small>
            Subtype: {item.subtype ?? "Unclassified"} · Peer group:{" "}
            {item.peer_group ?? "Unclassified"}
          </small>
        </div>
        <span className="etf-status">{researchStatusLabel(item)}</span>
      </header>
      <p className="etf-analysis-caveat">
        Preliminary research uses the available factor weights. A high score
        does not establish complete fund evidence or a verified entry. Peer rank
        compares the stated peer group only.
      </p>
      {typeof item.score_basis?.explanation === "string" ? (
        <p>{item.score_basis.explanation}</p>
      ) : null}
      <dl className="opportunity-primary-metrics">
        <div>
          <dt>Score / 100</dt>
          <dd>
            {result?.overall_score == null
              ? "—"
              : result.overall_score.toFixed(1)}
          </dd>
        </div>
        <div>
          <dt>Confidence</dt>
          <dd>{result?.confidence ?? "Unavailable"}</dd>
        </div>
        <div>
          <dt>Score coverage</dt>
          <dd>
            {result?.data_completeness == null
              ? "Unavailable"
              : pct(result.data_completeness * 100)}
          </dd>
        </div>
        <div>
          <dt>Peer rank</dt>
          <dd>{item.rank ?? "Unavailable"}</dd>
        </div>
      </dl>
      {!result ? (
        <p className="etf-evidence-notice">
          Analysis not recorded. {item.reason}
        </p>
      ) : null}
      {item.contract_issues?.length ? (
        <ul className="etf-evidence-notice">
          {item.contract_issues.map((t, i) => (
            <li key={i}>{t}</li>
          ))}
        </ul>
      ) : null}
      <dl className="opportunity-primary-metrics etf-radar-factors">
        {FACTOR_LABELS.map(([key, label]) => {
          const f = factors.find((x) => x.factor === key);
          return (
            <div key={key}>
              <dt>{label} score / 100</dt>
              <dd>
                {f?.status === "SCORED" && f.normalized_score != null
                  ? f.normalized_score.toFixed(0)
                  : "—"}
              </dd>
              <small>
                {f?.status === "SCORED"
                  ? (f.raw_metric ?? "Raw metric not recorded")
                  : (f?.reason ?? "Evidence unavailable")}
              </small>
              {f?.freshness ? <small>As of {f.freshness}</small> : null}
            </div>
          );
        })}
      </dl>
      <section className="etf-entry-evidence">
        <h4>Entry assessment</h4>
        <strong>{entryLabel(item)}</strong>
        {typeof item.entry_inputs?.reason === "string" ? (
          <p>{item.entry_inputs.reason}</p>
        ) : null}
        {item.actionable_recommendation ? (
          <p>{item.actionable_recommendation.explanation}</p>
        ) : item.entry_assessment ? (
          <p>
            Historical entry assessment:{" "}
            {humanize(item.entry_assessment.classification)}. The current
            verification checks were not recorded for this payload.
          </p>
        ) : null}
        <p>
          Entry conditions are separate from screening rank and never an
          automatic instruction to buy.
        </p>
        {item.entry_assessment?.conditions?.length ? (
          <ul>
            {item.entry_assessment.conditions.map((c, i) => (
              <li key={i}>{c}</li>
            ))}
          </ul>
        ) : (
          <p>No verified current entry assessment was recorded.</p>
        )}
      </section>
      <ReadinessEvidence item={item} />
      <section>
        <h4>Fund evidence</h4>
        <p>
          {completeFundEvidence(item)
            ? "Five core fund metrics are present; review their source dates and remaining risks."
            : "Fund evidence is incomplete. Missing facts do not contribute favorable values."}
        </p>
        <dl className="opportunity-primary-metrics">
          {METRICS.map(([key, label]) => (
            <div key={key}>
              <dt>{label}</dt>
              <dd>{metricDisplay(item, key)}</dd>
              {metricDisplay(item, key) === "Unavailable" ? (
                <small>
                  {item.screening_metrics?.[key]?.reason ??
                    "Verified, current source evidence unavailable"}
                </small>
              ) : null}
            </div>
          ))}
          <div>
            <dt>Price history depth</dt>
            <dd>
              {item.history_observations == null
                ? "Unavailable"
                : `${item.history_observations} sessions`}
            </dd>
          </div>
          <div>
            <dt>Fund house</dt>
            <dd>{item.fund_house ?? "Unavailable"}</dd>
          </div>
          <div>
            <dt>Benchmark</dt>
            <dd>{item.benchmark ?? "Unavailable"}</dd>
          </div>
          <div>
            <dt>Replication</dt>
            <dd>{item.replication_method ?? "Unavailable"}</dd>
          </div>
          <div>
            <dt>Distribution</dt>
            <dd>{item.distribution_type ?? "Unavailable"}</dd>
          </div>
        </dl>
      </section>
      <section>
        <h4>Risks and evidence gaps</h4>
        {!item.analysis_recorded ? (
          <p>
            Risk assessment not recorded; no conclusion about absence of risk is
            available.
          </p>
        ) : triggeredGates.length ? (
          <ul>
            {triggeredGates.map((g) => (
              <li key={g.gate_id}>
                {humanize(g.gate_id)}: {g.reason}
              </li>
            ))}
          </ul>
        ) : (
          <p>
            No triggered gates were reported in the recorded assessment. Missing
            fund evidence still applies.
          </p>
        )}
        {item.missing_data?.length ? (
          <p>Missing data: {item.missing_data.join(", ")}</p>
        ) : null}
        {item.limitations?.length ? (
          <ul>
            {item.limitations.map((t, i) => (
              <li key={i}>{t}</li>
            ))}
          </ul>
        ) : null}
      </section>
      <details>
        <summary>Acquisition diagnostics</summary>
        <p>{historyLabel(item)}</p>
        {item.document_acquisition ? (
          <>
            <p>
              Official document acquisition:{" "}
              {humanize(item.document_acquisition.status)} ·{" "}
              {item.document_acquisition.reason ?? "No detail recorded"}
            </p>
            {Object.entries(item.document_acquisition.facts ?? {}).map(
              ([key, v]) => (
                <p key={key}>
                  {humanize(key)}: {v.outcome ?? "Unavailable"}
                  {v.reason ? ` · ${v.reason}` : ""}
                </p>
              ),
            )}
          </>
        ) : null}
      </details>
      <details>
        <summary>Evidence dates and sources</summary>
        {item.source_references?.length ? (
          <ul>
            {item.source_references.map((r, i) => (
              <li key={`${r.metric}-${i}`}>
                <strong>{humanize(r.metric)}</strong>: {r.value}
                {r.unit ? ` ${r.unit}` : ""}; as of {r.as_of}; {r.provider}
                {r.retrieved_at ? `; retrieved ${r.retrieved_at}` : ""}
                {safeSource(r.url) ? (
                  <>
                    {" "}
                    ·{" "}
                    <a href={r.url} target="_blank" rel="noopener noreferrer">
                      Source document
                    </a>
                  </>
                ) : (
                  " · Source link unavailable"
                )}
                {r.reason ? ` · ${r.reason}` : ""}
              </li>
            ))}
          </ul>
        ) : (
          <p>No source references were published for this record.</p>
        )}
      </details>
    </article>
  );
}

function ReadinessEvidence({ item }: { item: EtfCandidate }) {
  const readiness = item.research_readiness;
  const requirements = Array.isArray(readiness?.requirements)
    ? readiness.requirements
    : [];
  return (
    <section className="etf-readiness-evidence">
      <h4>Research readiness and entry checks</h4>
      <p>
        {readiness?.observed === true && typeof readiness.status === "string"
          ? `Recorded readiness: ${humanize(readiness.status)}`
          : "Readiness assessment not recorded for this published result."}
      </p>
      <dl className="etf-compact-stats">
        <div>
          <dt>Mandatory readiness recorded</dt>
          <dd>{readiness?.observed === true ? "Yes" : "Not recorded"}</dd>
        </div>
        <div>
          <dt>Aligned entry evidence verified</dt>
          <dd>
            {item.entry_inputs?.verified === true &&
            item.entry_inputs.current === true
              ? "Recorded as verified"
              : "Not verified"}
          </dd>
        </div>
      </dl>
      {requirements.length ? (
        <ul>
          {requirements.map((raw, index) => {
            if (!raw || typeof raw !== "object" || Array.isArray(raw))
              return null;
            const r = raw as Record<string, unknown>;
            return (
              <li key={index}>
                {typeof r.requirement === "string"
                  ? humanize(r.requirement)
                  : "Requirement"}
                :{" "}
                {typeof r.status === "string"
                  ? humanize(r.status)
                  : "Not recorded"}
                {typeof r.reason === "string" ? ` · ${r.reason}` : ""}
                {typeof r.provider === "string" ? ` · ${r.provider}` : ""}
                {typeof r.as_of_date === "string" ? ` · ${r.as_of_date}` : ""}
              </li>
            );
          })}
        </ul>
      ) : null}
    </section>
  );
}

type EtfTab =
  | "overview"
  | "top"
  | "leaders"
  | "universe"
  | "screener"
  | "compare"
  | "quality";
const ETF_TABS: [EtfTab, string][] = [
  ["overview", "Overview"],
  ["top", "Research Opportunities"],
  ["leaders", "Category Leaders"],
  ["universe", "Research Universe"],
  ["screener", "Advanced Screener"],
  ["compare", "ETF Comparison"],
  ["quality", "Data Quality"],
];
function categoryLeaders(ranked: EtfCandidate[]) {
  const groups = new Map<string, EtfCandidate>();
  for (const c of ranked) {
    const k = c.peer_group ?? "Unclassified",
      old = groups.get(k);
    if (!old || (c.rank ?? Infinity) < (old.rank ?? Infinity)) groups.set(k, c);
  }
  return [...groups.values()];
}
function EtfDonut({ segments }: { segments: [string, number][] }) {
  const total = segments.reduce((n, [, v]) => n + v, 0);
  let offset = 0;
  const colors = [
    "var(--primary)",
    "var(--info)",
    "var(--warning)",
    "var(--negative)",
    "var(--text-soft)",
  ];
  return (
    <svg
      className="etf-outcome-donut"
      viewBox="0 0 120 120"
      role="img"
      aria-label={`Research outcomes: ${total} assessed ETFs`}
    >
      <circle
        cx="60"
        cy="60"
        r="44"
        fill="none"
        stroke="var(--line)"
        strokeWidth="14"
      />
      {total > 0
        ? segments.map(([label, value], i) => {
            const length = (value / total) * 276.46,
              node = (
                <circle
                  key={label}
                  cx="60"
                  cy="60"
                  r="44"
                  fill="none"
                  stroke={colors[i]}
                  strokeWidth="14"
                  strokeDasharray={`${length} ${276.46 - length}`}
                  strokeDashoffset={-offset}
                  transform="rotate(-90 60 60)"
                >
                  <title>{`${label}: ${value}`}</title>
                </circle>
              );
            offset += length;
            return node;
          })
        : null}
      <text x="60" y="59" textAnchor="middle" className="etf-donut-value">
        {total}
      </text>
      <text x="60" y="76" textAnchor="middle" className="etf-donut-caption">
        assessed
      </text>
    </svg>
  );
}
function EtfAnalysisPanels({ data }: { data: EtfCycleResult }) {
  const evaluated = data.candidates.filter(
      (c) => c.disposition === "EVALUATED",
    ).length,
    leaders = categoryLeaders(data.ranked),
    outcomes = Object.entries(outcomePartition(data.candidates));
  const completed = data.candidates.filter(completeFundEvidence).length,
    favorable = data.candidates.filter(verifiedEntry).length;
  return (
    <div className="etf-overview-grid">
      <section className="etf-overview-card" aria-label="Research outcomes">
        <h3>Research outcomes</h3>
        <div className="etf-outcome-body">
          <EtfDonut segments={outcomes} />
          <ul className="etf-outcome-legend">
            {outcomes.map(([label, n]) => (
              <li key={label}>
                <span>{label}</span>
                <strong>{n}</strong>
              </li>
            ))}
          </ul>
        </div>
        <p>
          Each of {data.candidates.length} assessed ETFs appears once.
          Preliminary research is not an entry recommendation.
        </p>
      </section>
      <section
        className="etf-overview-card"
        aria-label="Fund universe and coverage"
      >
        <h3>Evidence readiness</h3>
        <dl className="etf-compact-stats">
          <div>
            <dt>Universe size</dt>
            <dd>{data.universe_count ?? data.candidates.length}</dd>
          </div>
          <div>
            <dt>Evaluated ETFs</dt>
            <dd>{evaluated}</dd>
          </div>
          <div>
            <dt>Core fund evidence complete</dt>
            <dd>
              {completed} / {data.candidates.length}
            </dd>
          </div>
          <div>
            <dt>Favorable entry conditions recorded</dt>
            <dd>{favorable}</dd>
          </div>
        </dl>
        <p>
          Five core metrics: expense, AUM, tracking error, tracking difference
          and spread. Score coverage measures available factor weights, not fund
          evidence completeness.
        </p>
      </section>
      <section className="etf-overview-card" aria-label="Category coverage">
        <h3>Category coverage</h3>
        <dl className="etf-compact-stats">
          <div>
            <dt>Ranked ETFs</dt>
            <dd>{data.ranked.length}</dd>
          </div>
          <div>
            <dt>Peer categories</dt>
            <dd>{new Set(data.ranked.map((c) => c.peer_group)).size}</dd>
          </div>
          <div>
            <dt>Category leaders</dt>
            <dd>{leaders.length}</dd>
          </div>
          <div>
            <dt>Insufficient data</dt>
            <dd>
              {outcomes.find(([k]) => k === "Insufficient data")?.[1] ?? 0}
            </dd>
          </div>
        </dl>
        <p>
          Rankings compare verified peers only. Different objectives and
          exposures are not a global best-to-worst list.
        </p>
      </section>
    </div>
  );
}

export function EtfRadarContent({
  data: raw,
  initialTab = "overview",
}: {
  data: EtfCycleResult;
  initialTab?: EtfTab;
}) {
  const data = useMemo(() => normalizeEtfCycle(raw), [raw]);
  const [tab, setTab] = useState<EtfTab>(initialTab),
    [compareIds, setCompareIds] = useState<string[]>([]),
    [selectedId, setSelectedId] = useState<string | null>(null);
  const currentIds = new Set(data.candidates.map(candidateId));
  const activeCompareIds = compareIds.filter((id) => currentIds.has(id));
  const selected =
    data.candidates.find((item) => candidateId(item) === selectedId) ?? null;
  const setSelected = (item: EtfCandidate | null) =>
    setSelectedId(item ? candidateId(item) : null);
  const analysisRef = useRef<HTMLElement | null>(null);
  const toggleCompare = (id: string) =>
    setCompareIds((prev) => {
      const valid = prev.filter((key) => currentIds.has(key));
      return valid.includes(id)
        ? valid.filter((key) => key !== id)
        : valid.length < 5
          ? [...valid, id]
          : valid;
    });
  const compared = data.candidates.filter((c) =>
    compareIds.includes(candidateId(c)),
  );
  const evaluated = data.candidates.filter(
    (c) => c.disposition === "EVALUATED",
  ).length;
  const leaders = categoryLeaders(data.ranked);
  const coveragePct =
    (data.universe_count ?? data.candidates.length) > 0
      ? Math.round(
          (evaluated / (data.universe_count ?? data.candidates.length)) * 100,
        )
      : 0;
  const analyze = (item: EtfCandidate) => setSelected(item);
  useEffect(() => {
    if (selected)
      analysisRef.current?.scrollIntoView?.({
        block: "start",
        behavior: "smooth",
      });
  }, [selected]);
  const table = (rows: EtfCandidate[], label: string, rank = false) => (
    <EtfOpportunityTable
      key={tab}
      rows={rows}
      label={label}
      compareIds={activeCompareIds}
      onCompare={toggleCompare}
      onAnalyze={analyze}
      initialSort={rank ? "rank" : "symbol"}
    />
  );
  const comparison: [string, (c: EtfCandidate) => string][] = [
    ["Research status", researchStatusLabel],
    ["Entry assessment", entryLabel],
    ["Peer category", (c) => c.peer_group ?? "Unavailable"],
    [
      "Peer rank (within category)",
      (c) => (c.rank == null ? "Unavailable" : String(c.rank)),
    ],
    [
      "Score / 100",
      (c) => c.rule_engine_result?.overall_score?.toFixed(1) ?? "Unavailable",
    ],
    ["Confidence", (c) => c.rule_engine_result?.confidence ?? "Unavailable"],
    [
      "Score coverage",
      (c) =>
        c.rule_engine_result?.data_completeness == null
          ? "Unavailable"
          : pct(c.rule_engine_result.data_completeness * 100),
    ],
    ...METRICS.map(
      ([key, label]) =>
        [label, (c: EtfCandidate) => metricDisplay(c, key)] as [
          string,
          (c: EtfCandidate) => string,
        ],
    ),
    [
      "History sessions",
      (c) =>
        c.history_observations == null
          ? "Unavailable"
          : String(c.history_observations),
    ],
    ["History acquisition", historyLabel],
    [
      "Missing evidence",
      (c) =>
        c.missing_data?.join(", ") ||
        "None reported; review source completeness",
    ],
  ];
  return (
    <Card className="wide-panel etf-research-results">
      <header className="etf-results-header">
        <div>
          <h2>ETF research</h2>
          <p>
            Published {dateLabel(data.as_of)} · {data.candidates.length}{" "}
            assessed · {data.ranked.length} ranked · {coveragePct}% of the
            universe evaluated
          </p>
        </div>
        <span className="etf-status">Persisted evidence</span>
      </header>
      <p className="etf-coverage-caveat">
        Evaluation coverage counts assessed ETFs. Complete evidence and verified
        entry conditions are separate checks. No screening rank is a buy
        recommendation.
      </p>
      {data.candidates.length <
      (data.universe_count ?? data.candidates.length) ? (
        <p className="etf-evidence-notice" role="status">
          Partial published result: {data.candidates.length} of{" "}
          {data.universe_count} universe records are available.
        </p>
      ) : null}
      <div
        className="radar-section-tabs"
        role="tablist"
        aria-label="ETF radar sections"
      >
        {ETF_TABS.map(([key, label]) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={tab === key}
            className={`radar-section-tab${tab === key ? " radar-section-tab-active" : ""}`}
            onClick={() => setTab(key)}
          >
            {label}
            {key === "compare" && activeCompareIds.length
              ? ` (${activeCompareIds.length})`
              : ""}
          </button>
        ))}
      </div>
      {tab === "overview" ? (
        <div className="radar-tab-panel">
          <EtfAnalysisPanels data={data} />
          {!data.ranked.length ? (
            <div className="radar-empty-state">
              <h3>No ranked research opportunities</h3>
              <p>
                {data.candidates.some(
                  (c) => c.disposition === "TECHNICAL_FAILURE",
                )
                  ? "Some ETFs had technical failures. Review Data Quality for acquisition outcomes."
                  : data.candidates.some(
                        (c) => c.disposition === "INSUFFICIENT_DATA",
                      )
                    ? "Some data prerequisites are missing. Review Research Universe for the evidence required."
                    : "Review the evaluation and exclusion counts, then explore the assessed universe."}
              </p>
              <button
                type="button"
                className="button button-secondary"
                onClick={() => setTab("universe")}
              >
                Explore research universe
              </button>
            </div>
          ) : null}
          <section aria-label="Missing core fund evidence">
            <h3>Evidence gaps across the assessed universe</h3>
            <div className="etf-evidence-counts">
              {METRICS.filter(([k]) =>
                [
                  "expense_ratio",
                  "aum",
                  "tracking_error",
                  "tracking_difference",
                  "bid_ask_spread",
                ].includes(k),
              ).map(([key, label]) => (
                <div key={key}>
                  <span>{label}</span>
                  <strong>
                    {
                      data.candidates.filter(
                        (c) => metricDisplay(c, key) === "Unavailable",
                      ).length
                    }{" "}
                    unavailable
                  </strong>
                </div>
              ))}
            </div>
          </section>
        </div>
      ) : null}
      {tab === "top" ? (
        <div className="radar-tab-panel">
          <h3>Research opportunities</h3>
          <p>
            The complete ranked research set. Scores use available evidence;
            compare within the stated peer category and review entry checks
            independently.
          </p>
          {table(data.ranked, "Ranked research opportunities", true)}
        </div>
      ) : null}
      {tab === "leaders" ? (
        <div className="radar-tab-panel">
          <h3>Category leaders</h3>
          <p>
            One leading ranked ETF per verified peer group. Peer ranks are
            retained; category leaders are not interchangeable investments.
          </p>
          {table(leaders, "Category leaders", true)}
        </div>
      ) : null}
      {tab === "universe" ? (
        <div className="radar-tab-panel">
          <h3>Research universe</h3>
          <p>
            Every assessed ETF, including excluded, incomplete and failed
            records. Open Analysis for evidence, sources and history.
          </p>
          {table(data.candidates, "Research universe")}
        </div>
      ) : null}
      {tab === "screener" ? (
        <div className="radar-tab-panel">
          <h3>Advanced screener</h3>
          <p>
            Screen all assessed ETFs with combined filters. Unavailable values
            never pass a numeric threshold.
          </p>
          {table(data.candidates, "Advanced screener")}
        </div>
      ) : null}
      {tab === "compare" ? (
        <div className="radar-tab-panel">
          <h3>ETF comparison</h3>
          <p>
            Select 2–5 ETFs from any research table. Cross-category comparisons
            describe different exposures; peer ranks are not global ranks.
          </p>
          {compared.length > 0 ? (
            <>
              <div className="etf-selected-chips">
                {compared.map((c) => (
                  <button
                    type="button"
                    className="button button-secondary"
                    key={candidateId(c)}
                    onClick={() => toggleCompare(candidateId(c))}
                  >
                    Remove {c.symbol}
                  </button>
                ))}
              </div>
              {compared.length < 2 ? (
                <p>Select one more ETF for a side-by-side comparison.</p>
              ) : (
                <div className="radar-table-scroll">
                  <table className="radar-dense-table radar-compare-table">
                    <thead>
                      <tr>
                        <th scope="col">Metric</th>
                        {compared.map((c) => (
                          <th scope="col" key={candidateId(c)}>
                            <button
                              type="button"
                              className="etf-symbol-button"
                              onClick={() => analyze(c)}
                            >
                              {c.symbol}
                            </button>
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {comparison.map(([label, get]) => (
                        <tr key={label}>
                          <th scope="row">{label}</th>
                          {compared.map((c) => (
                            <td key={candidateId(c)}>{get(c)}</td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          ) : (
            <p>
              No ETFs selected. Use the Compare checkbox in Research Universe or
              another table.
            </p>
          )}
        </div>
      ) : null}
      {tab === "quality" ? (
        <div className="radar-tab-panel">
          <h3>Data quality &amp; diagnostics</h3>
          <div className="etf-quality-grid">
            <section aria-label="Failure reason distribution">
              <h4>Exclusion reasons</h4>
              {Object.keys(data.excluded_by_reason).length ? (
                <dl className="etf-compact-stats">
                  {Object.entries(data.excluded_by_reason).map(
                    ([reason, n]) => (
                      <div key={reason}>
                        <dt>{reason}</dt>
                        <dd>{n}</dd>
                      </div>
                    ),
                  )}
                </dl>
              ) : (
                <p>No exclusion diagnostics recorded.</p>
              )}
            </section>
            <section>
              <h4>Official NSE coverage</h4>
              {data.coverage ? (
                <dl className="etf-compact-stats">
                  {Object.entries(data.coverage).map(([key, n]) => (
                    <div key={key}>
                      <dt>{key}</dt>
                      <dd>{n}</dd>
                    </div>
                  ))}
                </dl>
              ) : (
                <p>Coverage diagnostics unavailable for this cycle.</p>
              )}
            </section>
          </div>
          <p>
            Missing evidence is unavailable, never a zero value or an assessment
            of investment quality. Historical acquisition success alone does not
            establish adjusted total-return coverage.
          </p>
        </div>
      ) : null}
      {selected ? (
        <section
          className="etf-selected-analysis"
          ref={analysisRef}
          aria-label={`ETF analysis for ${selected.symbol ?? "selected fund"}`}
        >
          <div className="etf-analysis-heading">
            <h2>ETF analysis · {selected.symbol}</h2>
            <button
              type="button"
              className="button button-secondary"
              onClick={() => setSelected(null)}
            >
              Close analysis
            </button>
          </div>
          <EtfRadarCard item={selected} />
          {selected.global_instrument_id ? (
            <EtfPriceHistory
              instrumentId={selected.global_instrument_id}
              symbol={selected.symbol}
            />
          ) : (
            <p>
              Historical chart unavailable: no verified canonical instrument ID
              was recorded.
            </p>
          )}
        </section>
      ) : null}
    </Card>
  );
}

type EtfRadarStatus = "loading" | "empty" | "error" | "ready";

type EtfPhaseProgress = {
  version?: string;
  current_phase?: string | null;
  current_instrument_id?: string | null;
  phases?: Array<Record<string, unknown>>;
};

const ETF_PHASES = [
  "UNIVERSE_DISCOVERY",
  "IDENTITY_VALIDATION",
  "HISTORY_ONBOARDING",
  "FUNDAMENTAL_ACQUISITION",
  "EVIDENCE_VALIDATION",
  "READINESS_EVALUATION",
  "SCORING_RANKING",
  "PUBLICATION",
];

export function EtfPhaseProgressPanel({
  progress,
}: {
  progress?: EtfPhaseProgress | null;
}) {
  const rows = Array.isArray(progress?.phases) ? progress.phases : [];
  const labels = [
    "total",
    "pending",
    "attempted",
    "succeeded",
    "failed",
    "skipped",
    "reused",
    "remaining",
    "in_progress",
  ];
  return (
    <section className="etf-phase-progress" aria-label="ETF research phases">
      <h3>Research phases</h3>
      <p>
        Only recorded work is counted. Skipped, reused and failed work remain
        separate; overall progress is unavailable.
      </p>
      {typeof progress?.current_instrument_id === "string" &&
      progress.current_instrument_id ? (
        <p>Current instrument: {progress.current_instrument_id}</p>
      ) : null}
      <ol className="etf-phase-grid">
        {ETF_PHASES.map((phase, index) => {
          const row = rows.find((r) => r && r.phase === phase),
            observed = row?.observed === true;
          return (
            <li
              key={phase}
              className={
                progress?.current_phase === phase ? "etf-phase-current" : ""
              }
            >
              <h4>
                {index + 1}. {humanize(phase).toLowerCase()}
              </h4>
              {typeof row?.unit === "string" ? (
                <p>Counted units: {humanize(row.unit).toLowerCase()}</p>
              ) : null}
              {!observed ? (
                <p>Not recorded</p>
              ) : (
                <>
                  <dl>
                    {labels.map((label) => (
                      <div key={label}>
                        <dt>{humanize(label)}</dt>
                        <dd>{finite(row?.[label]) ?? "Not recorded"}</dd>
                      </div>
                    ))}
                  </dl>
                  <p>
                    Attempts including retries:{" "}
                    {finite(row?.attempt_count) ?? "Not recorded"} · Failed
                    attempts: {finite(row?.failed_attempts) ?? "Not recorded"}
                  </p>
                </>
              )}
            </li>
          );
        })}
      </ol>
    </section>
  );
}

type EtfActiveCycle = {
  cycleId: string;
  status: string;
  startedAt: string | null;
  updatedAt: string | null;
  errorCode: string | null;
  pollError: boolean;
  phaseProgress?: EtfPhaseProgress | null;
  coverage?: {
    official: number | null;
    completed: number;
    stages?: {
      baseline: { completed: number; total: number | null };
      onboarding: { completed: number; total: number | null };
    };
  };
  authoritativeProgress?: {
    phase?: string | null;
    stages?: Array<{
      phase: string;
      completed: number;
      total: number | null;
      percentage: number | null;
    }>;
    overall_percentage?: number | null;
    baseline_failed?: number | null;
  } | null;
};

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
  const [loadRevision, setLoadRevision] = useState(0);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [activeCycle, setActiveCycle] = useState<EtfActiveCycle | null>(null);
  useEffect(() => {
    let active = true;
    // Read the browser-owned store after hydration; cancel recovery on unmount.
    Promise.resolve().then(() => {
      if (!active) return;
      try {
        const storedCycleId = window.sessionStorage.getItem(
          ETF_ACTIVE_CYCLE_STORAGE_KEY,
        );
        if (storedCycleId)
          setActiveCycle(
            (previous) =>
              previous ?? {
                cycleId: storedCycleId,
                status: "RUNNING",
                startedAt: null,
                updatedAt: null,
                errorCode: null,
                pollError: false,
              },
          );
      } catch {
        /* Storage may be disabled. */
      }
    });
    return () => {
      active = false;
    };
  }, []);
  const cycleId = activeCycle?.cycleId;
  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    let active = true;
    request<unknown>("/api/v1/etf-radar/current")
      .then(normalizeEtfCycle)
      .then((value) => {
        if (active) {
          setData(value);
          setLoadError(null);
          setStatus("ready");
        }
      })
      .catch((failure: { status?: number; message?: string }) => {
        if (!active) return;
        if (failure?.status === 404) {
          setData(null);
          setStatus("empty");
        } else {
          setLoadError(
            typeof failure?.message === "string"
              ? failure.message
              : "Could not load the persisted ETF result.",
          );
          setStatus("error");
        }
      });
    return () => {
      active = false;
    };
  }, [loadRevision]);

  useEffect(() => {
    if (!cycleId) return;
    let active = true;
    const poll = () => {
      request<{
        status: string;
        updated_at?: string;
        created_at?: string;
        error_code?: string;
        coverage?: NonNullable<EtfActiveCycle["coverage"]>;
        authoritative_progress?: EtfActiveCycle["authoritativeProgress"];
        etf_phase_progress?: EtfPhaseProgress;
      }>(`/api/v1/etf-radar/cycles/${cycleId}/status`)
        .then((value) => {
          if (!active) return;
          if (!value || typeof value.status !== "string" || !value.status)
            throw new Error("Invalid ETF cycle status response");
          setActiveCycle((previous) =>
            previous && previous.cycleId === cycleId
              ? {
                  ...previous,
                  status: value.status,
                  updatedAt: value.updated_at ?? previous.updatedAt,
                  startedAt: previous.startedAt ?? value.created_at ?? null,
                  errorCode: value.error_code ?? previous.errorCode,
                  pollError: false,
                  coverage: value.coverage,
                  authoritativeProgress: value.authoritative_progress ?? null,
                  phaseProgress: value.etf_phase_progress ?? null,
                }
              : previous,
          );
          if (isTerminalRadarStatus(value.status)) {
            try {
              window.sessionStorage.removeItem(ETF_ACTIVE_CYCLE_STORAGE_KEY);
            } catch {
              /* ignore */
            }
            if (value.status === "COMPLETED") {
              request<unknown>("/api/v1/etf-radar/current")
                .then(normalizeEtfCycle)
                .then((result) => {
                  if (active) {
                    setData(result);
                    setStatus("ready");
                  }
                })
                .catch(() => {
                  if (active) {
                    setLoadError(
                      "The cycle completed, but its published result could not be loaded. The previous research is preserved.",
                    );
                    setStatus("error");
                  }
                });
            }
          } else {
            pollTimer.current = setTimeout(poll, 2500);
          }
        })
        .catch(() => {
          if (!active) return;
          // A transient poll failure must never leave the panel stuck
          // silently forever: surface it, but keep polling.
          setActiveCycle((previous) =>
            previous && previous.cycleId === cycleId
              ? { ...previous, pollError: true }
              : previous,
          );
          pollTimer.current = setTimeout(poll, 2500);
        });
    };
    poll();
    return () => {
      active = false;
      if (pollTimer.current) clearTimeout(pollTimer.current);
    };
    // Re-run whenever the tracked cycle_id changes; status itself is
    // updated inside the closure above, not via this dependency.
  }, [cycleId]);

  const runCycle = () => {
    if (!roles.includes("ADMIN") || running || starting) return;
    setActionError(null);
    setStarting(true);
    request<{ cycle_id: string; status: string; updated_at?: string }>(
      "/api/v1/etf-radar/full-universe-cycles",
      {
        method: "POST",
        body: JSON.stringify({
          max_universe: 2000,
          max_requests: 1500,
          max_seconds: 3600,
        }),
      },
    )
      .then((value) => {
        if (
          !value ||
          typeof value.cycle_id !== "string" ||
          !value.cycle_id ||
          typeof value.status !== "string" ||
          !value.status
        )
          throw new Error("Invalid ETF cycle admission response");
        setActiveCycle({
          cycleId: value.cycle_id,
          status: value.status,
          startedAt: value.updated_at ?? null,
          updatedAt: value.updated_at ?? null,
          errorCode: null,
          pollError: false,
        });
        try {
          window.sessionStorage.setItem(
            ETF_ACTIVE_CYCLE_STORAGE_KEY,
            value.cycle_id,
          );
        } catch {
          /* ignore */
        }
      })
      .catch(() =>
        setActionError(
          "Could not start the ETF cycle. The last published research is preserved.",
        ),
      )
      .finally(() => setStarting(false));
  };

  const running = !!activeCycle && !isTerminalRadarStatus(activeCycle.status);

  const terminalData =
    activeCycle?.status === "COMPLETED" &&
    data?.cycle_id === activeCycle.cycleId
      ? data
      : null;
  // The live status endpoint reports only BASELINE completion, not ETF_ONBOARD
  // history ingestion, final scoring, or publication. Never pass that fraction
  // as overall progress: 359/359 first-pass outcomes can precede minutes of work.
  const firstPassOfficial = activeCycle?.coverage?.official ?? null;
  const firstPassCompleted =
    activeCycle?.coverage?.stages?.baseline?.completed ??
    activeCycle?.coverage?.completed ??
    null;
  const historyOnboarded =
    activeCycle?.coverage?.stages?.onboarding?.completed ?? null;
  // Stage completion must never be presented as overall cycle completion.
  const authoritative = activeCycle?.authoritativeProgress;
  const currentStage = authoritative?.stages?.find(
    (stage) => stage.phase === authoritative.phase,
  );
  const stageCompleted = currentStage?.completed;
  const stageTotal = currentStage?.total;
  const stagePercent =
    currentStage &&
    typeof currentStage.percentage === "number" &&
    Number.isFinite(currentStage.percentage) &&
    currentStage.percentage >= 0 &&
    currentStage.percentage <= 100 &&
    typeof stageCompleted === "number" &&
    Number.isFinite(stageCompleted) &&
    stageCompleted >= 0 &&
    typeof stageTotal === "number" &&
    Number.isFinite(stageTotal) &&
    stageTotal > 0 &&
    stageCompleted <= stageTotal
      ? currentStage.percentage
      : null;
  const stageName =
    currentStage?.phase === "BASELINE"
      ? "Baseline"
      : currentStage?.phase === "DEEP"
        ? "Deep investigation"
        : currentStage?.phase === "ETF_ONBOARD"
          ? "Historical onboarding"
          : (currentStage?.phase ?? "Current stage");
  const counters: RadarProgressCounter[] = !terminalData
    ? activeCycle?.coverage
      ? [
          { label: "Official NSE ETF universe", value: firstPassOfficial },
          {
            label: "First-pass processed (not final evaluations)",
            value: firstPassCompleted,
          },
          {
            label: "First-pass remaining",
            value:
              firstPassOfficial != null && firstPassCompleted != null
                ? Math.max(firstPassOfficial - firstPassCompleted, 0)
                : null,
          },
          {
            label: "Historical onboarding recorded (including failures)",
            value: historyOnboarded,
          },
          {
            label: "Historical onboarding remaining",
            value:
              firstPassOfficial != null && historyOnboarded != null
                ? Math.max(firstPassOfficial - historyOnboarded, 0)
                : null,
          },
        ]
      : []
    : [
        { label: "Total universe", value: terminalData.candidates.length },
        {
          label: "Evaluated candidates",
          value: terminalData.candidates.filter(
            (c) => c.disposition === "EVALUATED",
          ).length,
        },
        {
          label: "Technical failures",
          value: terminalData.candidates.filter(
            (c) => c.disposition === "TECHNICAL_FAILURE",
          ).length,
        },
        // Readiness exclusions (insufficient evidence) kept separate from
        // eligibility filtering (ineligible/unsupported fund type), per
        // app/etf_opportunity_cycle.py's own admission-outcome vocabulary.
        {
          label: "Readiness excluded",
          value: terminalData.candidates.filter(
            (c) => c.disposition === "INSUFFICIENT_DATA",
          ).length,
        },
        {
          label: "Eligibility filtered",
          value: terminalData.candidates.filter(
            (c) =>
              c.disposition === "INELIGIBLE" || c.disposition === "UNSUPPORTED",
          ).length,
        },
        {
          label: "Cancelled",
          value: terminalData.candidates.filter(
            (c) => c.disposition === "CANCELLED",
          ).length,
        },
        { label: "Pod restarts", value: null },
        // AVOID is a real category in EtfRecommendation (see app/etf_rule_engine.py)
        // -- "where supported" from the RADAR UI + API ROUTING spec, and ETF is
        // the radar that supports it.
        {
          label: "AVOID count",
          value: terminalData.candidates.filter(
            (c) => c.recommendation === "AVOID",
          ).length,
        },
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
          {roles.includes("ADMIN") ? (
            <button
              type="button"
              className="button button-primary radar-run-action"
              disabled={running || starting}
              onClick={runCycle}
            >
              {running
                ? "Running…"
                : starting
                  ? "Starting…"
                  : "Run full NSE ETF Radar"}
            </button>
          ) : (
            <span className="etf-readonly-note">
              Published research · read only
            </span>
          )}
        </div>
        {actionError ? <p role="alert">{actionError}</p> : null}
        {activeCycle ? (
          <>
            <RadarProgressPanel
              radarType="ETF"
              cycleId={activeCycle.cycleId}
              status={activeCycle.status}
              startedAt={activeCycle.startedAt}
              updatedAt={activeCycle.updatedAt}
              counters={counters}
              percent={stagePercent}
              progressLabel={
                stagePercent == null
                  ? "Research phase progress"
                  : stageName + " progress"
              }
              errorCode={activeCycle.errorCode}
              pollError={activeCycle.pollError}
            />
            <EtfPhaseProgressPanel progress={activeCycle.phaseProgress} />
            {running && currentStage ? (
              <p className="etf-coverage-caveat">
                {stageName}: {stageCompleted} / {stageTotal ?? "unknown"}{" "}
                processed
                {stagePercent != null
                  ? " (" + stagePercent + "%)"
                  : " (percentage unavailable)"}
                .
                {currentStage.phase === "BASELINE" &&
                authoritative?.baseline_failed != null
                  ? " " +
                    authoritative.baseline_failed +
                    " baseline evaluations failed."
                  : ""}{" "}
                Overall cycle progress: unavailable until subsequent stages are
                measurable.
              </p>
            ) : null}
            {running ? (
              <p className="etf-coverage-caveat">
                First-pass and historical onboarding are separate persisted
                stages. A recorded onboarding outcome may be successful,
                insufficient, or failed; it does not mean usable history was
                acquired. Final scoring, peer ranking and publication follow
                both stages. Overall progress remains indeterminate because
                those final stages have no trustworthy instrument counters.
              </p>
            ) : null}
          </>
        ) : null}
      </Card>
      {status === "error" ? (
        <Card className="wide-panel">
          <EmptyState
            title="ETF radar unavailable"
            message={
              loadError ??
              "The last persisted ETF radar result could not be loaded."
            }
            action={
              <button
                type="button"
                className="button button-secondary"
                onClick={() => {
                  setStatus("loading");
                  setLoadRevision((v) => v + 1);
                }}
              >
                Retry loading ETF research
              </button>
            }
          />
        </Card>
      ) : null}
      {status === "empty" ? (
        <Card className="wide-panel">
          <EmptyState
            title="No ETF radar cycle yet"
            message="No ETF cycle has been published yet. An authorized administrator can start a research cycle."
          />
        </Card>
      ) : null}
      {status === "loading" ? (
        <Card className="wide-panel">
          <Skeleton rows={4} />
        </Card>
      ) : null}
      {data ? <EtfRadarContent data={data} /> : null}
    </section>
  );
}
