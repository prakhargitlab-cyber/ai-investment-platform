/** Presentation only: no acquisition, scoring or inferred investment evidence. */
export type EtfFactorResult = {
  factor: string;
  weight: number | null;
  status: string;
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
  category: string;
  severity: string;
  triggered: boolean;
  reason: string;
};
export type EtfRuleEngineResult = {
  rule_engine_version: string;
  instrument_id: string;
  as_of: string;
  factor_results: EtfFactorResult[];
  overall_score: number | null;
  data_completeness: number | null;
  confidence: string | null;
};
export type EtfScreeningMetric = {
  value: string | null;
  unit: string;
  quality: string;
  reason?: string | null;
};
export type EtfEntryAssessment = {
  classification: string;
  price_band: unknown;
  conditions: string[];
  calculated_at: string;
  methodology: string;
  [key: string]: unknown;
};
export type EtfCandidate = {
  global_instrument_id: string | null;
  symbol: string | null;
  disposition: string;
  reason: string;
  subtype: string | null;
  rule_engine_result: EtfRuleEngineResult | null;
  risk_gates: EtfRiskGate[];
  recommendation: string | null;
  rank: number | null;
  peer_group?: string;
  fund_name?: string;
  canonical_name?: string;
  benchmark?: string;
  isin?: string;
  missing_data?: string[];
  limitations?: string[];
  source_references?: Array<{
    metric: string;
    value: string;
    as_of: string;
    provider: string;
    url: string;
    unit?: string;
    retrieved_at?: string;
    quality?: string;
    reason?: string;
  }>;
  historical_acquisition?: {
    status: string;
    reason: string | null;
    observations: number | null;
  } | null;
  document_acquisition?: {
    status: string;
    reason: string | null;
    url: string;
    facts?: Record<string, { outcome: string | null; reason: string | null }>;
  } | null;
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
  evidence_diagnostics?: Record<string, unknown>;
  evidence_completeness?: Record<string, unknown>;
  research_readiness?: Record<string, unknown>;
  score_basis?: Record<string, unknown>;
  risk_assessment?: Record<string, unknown>;
  actionable_recommendation?: {
    status: string;
    is_actionable: boolean;
    explanation: string;
  };
  entry_checks?: unknown;
  entry_inputs?: Record<string, unknown>;
  presentation?: Record<string, unknown>;
  contract_issues?: string[];
  analysis_recorded?: boolean;
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
  top_opportunities?: EtfCandidate[];
  excluded_by_reason: Record<string, number>;
  recommendation_evidence?: {
    screening_opportunities: number;
    missing_verified_metrics: Record<string, number>;
    entry_assessed: number;
    entry_insufficient_evidence: number;
  };
};
export const METRICS = [
  ["expense_ratio", "Expense ratio", "% p.a."],
  ["aum", "AUM", "INR"],
  ["trading_volume", "Executed volume", "units"],
  ["bid_ask_spread", "Bid/ask spread", "%"],
  ["tracking_error", "Tracking error", "%"],
  ["tracking_difference", "Tracking difference", "%"],
  ["fund_age", "Fund age", "years"],
  ["volatility", "Annualized volatility", "%"],
  ["drawdown", "Maximum drawdown", "%"],
  ["concentration", "Top-10 concentration", "%"],
] as const;
const obj = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === "object" && !Array.isArray(v);
const text = (v: unknown): string | null =>
  typeof v === "string" && v.trim() ? v : null;
export const finite = (v: unknown): number | null =>
  typeof v === "number" && Number.isFinite(v) ? v : null;
const strings = (v: unknown): string[] =>
  Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
export function normalizeEtfCandidate(raw: unknown): EtfCandidate {
  if (
    !obj(raw) ||
    ![
      "EVALUATED",
      "INSUFFICIENT_DATA",
      "TECHNICAL_FAILURE",
      "INELIGIBLE",
      "UNSUPPORTED",
      "CANCELLED",
    ].includes(String(raw.disposition)) ||
    (!text(raw.symbol) && !text(raw.global_instrument_id))
  )
    throw new Error(
      "ETF result contains an invalid candidate identity or outcome.",
    );
  const issues = strings(raw.contract_issues);
  let result: EtfRuleEngineResult | null = null;
  if (raw.rule_engine_result != null) {
    if (!obj(raw.rule_engine_result))
      issues.push("Scoring record is malformed; analysis is unavailable.");
    else {
      const r = raw.rule_engine_result;
      if (!Array.isArray(r.factor_results))
        issues.push("Factor evidence was not recorded.");
      if (r.overall_score != null && finite(r.overall_score) == null)
        issues.push("Score is not a finite number.");
      result = {
        rule_engine_version: text(r.rule_engine_version) ?? "Unavailable",
        instrument_id: text(r.instrument_id) ?? "",
        as_of: text(r.as_of) ?? "",
        overall_score: finite(r.overall_score),
        data_completeness: finite(r.data_completeness),
        confidence: ["HIGH", "MEDIUM", "LOW"].includes(String(r.confidence))
          ? String(r.confidence)
          : null,
        factor_results: (Array.isArray(r.factor_results)
          ? r.factor_results
          : []
        )
          .filter(obj)
          .filter((f) => text(f.factor))
          .map((f) => ({
            factor: String(f.factor),
            weight: finite(f.weight),
            status: f.status === "SCORED" ? "SCORED" : "UNAVAILABLE",
            raw_metric: text(f.raw_metric),
            normalized_score: finite(f.normalized_score),
            weighted_contribution: finite(f.weighted_contribution),
            freshness: text(f.freshness),
            source_provenance: text(f.source_provenance),
            confidence: finite(f.confidence),
            reason: text(f.reason),
          })),
      };
    }
  }
  const metrics: Record<string, EtfScreeningMetric> = {};
  if (obj(raw.screening_metrics))
    for (const [key, value] of Object.entries(raw.screening_metrics))
      if (obj(value))
        metrics[key] = {
          value: typeof value.value === "string" ? value.value : null,
          unit: text(value.unit) ?? "",
          quality: text(value.quality) ?? "UNAVAILABLE",
          reason: text(value.reason),
        };
  const h = obj(raw.historical_acquisition) ? raw.historical_acquisition : null;
  const d =
    obj(raw.document_acquisition) && text(raw.document_acquisition.status)
      ? raw.document_acquisition
      : null;
  const entry =
    obj(raw.entry_assessment) && text(raw.entry_assessment.classification)
      ? raw.entry_assessment
      : null;
  const extraText = Object.fromEntries(
    [
      "peer_group",
      "fund_name",
      "canonical_name",
      "benchmark",
      "isin",
      "fund_house",
      "domicile",
      "distribution_type",
      "replication_method",
      "holdings_coverage",
    ].map((key) => [
      key,
      text(raw[key]) === "UNKNOWN" ? undefined : (text(raw[key]) ?? undefined),
    ]),
  );
  return {
    ...extraText,
    global_instrument_id: text(raw.global_instrument_id),
    symbol: text(raw.symbol),
    disposition: String(raw.disposition),
    reason: text(raw.reason) ?? "No reason recorded",
    subtype: text(raw.subtype),
    recommendation: text(raw.recommendation),
    rank: finite(raw.rank),
    rule_engine_result: result,
    risk_gates: (Array.isArray(raw.risk_gates) ? raw.risk_gates : [])
      .filter(obj)
      .filter((g) => typeof g.triggered === "boolean" && text(g.gate_id))
      .map((g) => ({
        gate_id: String(g.gate_id),
        category: text(g.category) ?? "",
        severity: text(g.severity) ?? "",
        triggered: g.triggered === true,
        reason: text(g.reason) ?? "No reason recorded",
      })),
    analysis_recorded:
      raw.analysis_recorded !== false &&
      (!obj(raw.risk_assessment) ||
        raw.risk_assessment.status !== "NOT_EVALUATED") &&
      Array.isArray(raw.risk_gates) &&
      raw.risk_gates.every(
        (g) => obj(g) && typeof g.triggered === "boolean" && text(g.gate_id),
      ),
    contract_issues: issues,
    missing_data: strings(raw.missing_data),
    limitations: strings(raw.limitations),
    positive_signals: strings(raw.positive_signals),
    negative_signals: strings(raw.negative_signals),
    screening_metrics: metrics,
    history_observations: finite(raw.history_observations) ?? undefined,
    historical_acquisition:
      h && text(h.status)
        ? {
            status: String(h.status),
            reason: text(h.reason),
            observations: finite(h.observations),
          }
        : null,
    document_acquisition: d
      ? {
          status: String(d.status),
          reason: text(d.reason),
          url: text(d.url) ?? "",
          facts: obj(d.facts)
            ? Object.fromEntries(
                Object.entries(d.facts)
                  .filter(([, v]) => obj(v))
                  .map(([k, v]) => [
                    k,
                    {
                      outcome: text((v as Record<string, unknown>).outcome),
                      reason: text((v as Record<string, unknown>).reason),
                    },
                  ]),
              )
            : undefined,
        }
      : null,
    entry_assessment: entry
      ? {
          ...entry,
          classification: String(entry.classification),
          price_band: entry.price_band ?? null,
          conditions: strings(entry.conditions),
          calculated_at: text(entry.calculated_at) ?? "",
          methodology: text(entry.methodology) ?? "",
        }
      : undefined,
    source_references: (Array.isArray(raw.source_references)
      ? raw.source_references
      : []
    )
      .filter(obj)
      .filter((r) => text(r.metric))
      .map((r) => ({
        metric: String(r.metric),
        value: text(r.value) ?? "Unavailable",
        as_of: text(r.as_of) ?? "Unavailable",
        provider: text(r.provider) ?? "Unavailable",
        url: text(r.url) ?? "",
        unit: text(r.unit) ?? undefined,
        retrieved_at: text(r.retrieved_at) ?? undefined,
        quality: text(r.quality) ?? undefined,
        reason: text(r.reason) ?? undefined,
      })),
    evidence_diagnostics: obj(raw.evidence_diagnostics)
      ? raw.evidence_diagnostics
      : undefined,
    evidence_completeness: obj(raw.evidence_completeness)
      ? raw.evidence_completeness
      : undefined,
    entry_checks: raw.entry_checks,
    entry_inputs: obj(raw.entry_inputs) ? raw.entry_inputs : undefined,
    research_readiness: obj(raw.research_readiness)
      ? raw.research_readiness
      : undefined,
    score_basis: obj(raw.score_basis) ? raw.score_basis : undefined,
    risk_assessment: obj(raw.risk_assessment) ? raw.risk_assessment : undefined,
    actionable_recommendation:
      obj(raw.actionable_recommendation) &&
      text(raw.actionable_recommendation.status)
        ? {
            status: String(raw.actionable_recommendation.status),
            is_actionable: raw.actionable_recommendation.is_actionable === true,
            explanation:
              text(raw.actionable_recommendation.explanation) ??
              "No explanation recorded",
          }
        : undefined,
    presentation: obj(raw.presentation) ? raw.presentation : undefined,
  };
}
export function normalizeEtfCycle(raw: unknown): EtfCycleResult {
  if (
    !obj(raw) ||
    !text(raw.cycle_id) ||
    !text(raw.as_of) ||
    !Number.isFinite(Date.parse(String(raw.as_of))) ||
    !Array.isArray(raw.candidates) ||
    !Array.isArray(raw.ranked)
  )
    throw new Error(
      "ETF result is incomplete or malformed. Retry loading the persisted result.",
    );
  const rows = (values: unknown[]) => values.map(normalizeEtfCandidate);
  const candidates = rows(raw.candidates),
    seen = new Set<string>();
  for (const item of candidates) {
    const id = candidateId(item);
    if (seen.has(id))
      throw new Error("ETF result contains duplicate candidate identities.");
    seen.add(id);
  }
  const ranked = rows(raw.ranked);
  const rankedIds = new Set<string>();
  for (const item of ranked) {
    const id = candidateId(item);
    if (!seen.has(id) || rankedIds.has(id))
      throw new Error(
        "ETF rankings contain an unknown or duplicate candidate identity.",
      );
    rankedIds.add(id);
  }
  const counts = (v: unknown) =>
    obj(v)
      ? (Object.fromEntries(
          Object.entries(v).filter(
            ([, n]) => finite(n) != null && Number(n) >= 0,
          ),
        ) as Record<string, number>)
      : {};
  const re = obj(raw.recommendation_evidence)
    ? raw.recommendation_evidence
    : null;
  return {
    cycle_id: String(raw.cycle_id),
    as_of: String(raw.as_of),
    radar_version: text(raw.radar_version) ?? "Unavailable",
    correlation_id: text(raw.correlation_id),
    scope: text(raw.scope) ?? undefined,
    universe_count: finite(raw.universe_count) ?? undefined,
    candidates,
    ranked,
    top_opportunities: Array.isArray(raw.top_opportunities)
      ? rows(raw.top_opportunities)
      : [],
    excluded_by_reason: counts(raw.excluded_by_reason),
    coverage: obj(raw.coverage) ? counts(raw.coverage) : undefined,
    recommendation_evidence: re
      ? {
          screening_opportunities: finite(re.screening_opportunities) ?? 0,
          missing_verified_metrics: counts(re.missing_verified_metrics),
          entry_assessed: finite(re.entry_assessed) ?? 0,
          entry_insufficient_evidence:
            finite(re.entry_insufficient_evidence) ?? 0,
        }
      : undefined,
  };
}
export const candidateId = (c: EtfCandidate) =>
  c.global_instrument_id ?? c.symbol ?? "";
export const humanize = (value: string) => value.replaceAll("_", " ");
export function researchStatusLabel(c: EtfCandidate): string {
  if (c.disposition === "TECHNICAL_FAILURE") return "Technical failure";
  if (
    c.disposition === "INSUFFICIENT_DATA" ||
    c.recommendation === "INSUFFICIENT_DATA"
  )
    return "Insufficient data";
  if (c.disposition !== "EVALUATED")
    return humanize(c.disposition)
      .toLowerCase()
      .replace(/^./, (x) => x.toUpperCase());
  if (["OPPORTUNITY", "STRONG_OPPORTUNITY"].includes(c.recommendation ?? ""))
    return c.rank != null
      ? "Preliminary research opportunity"
      : "Preliminary, unranked";
  return c.recommendation
    ? humanize(c.recommendation).toLowerCase()
    : "Evaluated — not recommended";
}
export function metricRaw(c: EtfCandidate, name: string): number | null {
  const m = c.screening_metrics?.[name];
  if (m?.quality !== "VALID" || m.value == null || !m.value.trim()) return null;
  const units: Record<string, string[]> = {
    expense_ratio: ["PERCENT_PA", "PERCENT", "percent"],
    aum: ["INR"],
    trading_volume: ["UNITS", "units", "shares", "SHARES"],
    fund_age: ["YEARS"],
  };
  const expected = units[name] ?? ["PERCENT", "percent"];
  if (!expected.includes(m.unit)) return null;
  const value = Number(m.value);
  return Number.isFinite(value) ? value : null;
}
export function metricDisplay(c: EtfCandidate, name: string): string {
  const value = metricRaw(c, name);
  if (value == null) return "Unavailable";
  const unit =
    METRICS.find(([key]) => key === name)?.[2] ??
    c.screening_metrics?.[name]?.unit ??
    "";
  return `${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}${unit ? ` ${unit}` : ""}`;
}
export function entryLabel(c: EtfCandidate): string {
  const label = c.actionable_recommendation?.status;
  return label === "FAVORABLE_ENTRY_ZONE" && verifiedEntry(c)
    ? "Verified favorable entry conditions"
    : label === "NEUTRAL_WAIT"
      ? "Wait / neutral"
      : label === "EXTENDED_AVOID_CHASING"
        ? "Extended — avoid chasing"
        : "Entry evidence incomplete";
}
export function verifiedEntry(c: EtfCandidate): boolean {
  const summary = c.research_readiness?.summary;
  return (
    c.disposition === "EVALUATED" &&
    c.actionable_recommendation?.status === "FAVORABLE_ENTRY_ZONE" &&
    c.actionable_recommendation.is_actionable === true &&
    c.research_readiness?.observed === true &&
    c.research_readiness.status === "READY" &&
    obj(summary) &&
    summary.ready_for_analysis === true &&
    c.entry_inputs?.verified === true &&
    c.entry_inputs.current === true &&
    completeFundEvidence(c)
  );
}
export const FUND_EVIDENCE = [
  "expense_ratio",
  "aum",
  "tracking_error",
  "tracking_difference",
  "bid_ask_spread",
];
export const completeFundEvidence = (c: EtfCandidate) =>
  FUND_EVIDENCE.every((key) => metricRaw(c, key) != null);
export function outcomePartition(candidates: EtfCandidate[]) {
  const counts = {
    "Preliminary research": 0,
    "Evaluated, other outcome": 0,
    "Insufficient data": 0,
    "Technical failure": 0,
    "Ineligible / unsupported / cancelled": 0,
  };
  for (const c of candidates) {
    if (c.disposition === "TECHNICAL_FAILURE") counts["Technical failure"]++;
    else if (
      c.disposition === "INSUFFICIENT_DATA" ||
      c.recommendation === "INSUFFICIENT_DATA"
    )
      counts["Insufficient data"]++;
    else if (c.disposition !== "EVALUATED")
      counts["Ineligible / unsupported / cancelled"]++;
    else if (
      ["OPPORTUNITY", "STRONG_OPPORTUNITY"].includes(c.recommendation ?? "")
    )
      counts["Preliminary research"]++;
    else counts["Evaluated, other outcome"]++;
  }
  return counts;
}
export type EtfSortKey =
  | "symbol"
  | "fund"
  | "category"
  | "status"
  | "rank"
  | "score"
  | "confidence"
  | "completeness"
  | "expense"
  | "aum"
  | "history"
  | "entry";
export type EtfFilters = {
  category: string;
  status: string;
  confidence: string;
  fundHouse: string;
  minScore: string;
  minCompleteness: string;
  maxExpense: string;
  minAum: string;
  minHistory: string;
};
export const EMPTY_FILTERS: EtfFilters = {
  category: "ALL",
  status: "ALL",
  confidence: "ALL",
  fundHouse: "",
  minScore: "",
  minCompleteness: "",
  maxExpense: "",
  minAum: "",
  minHistory: "",
};
export function candidateSearch(c: EtfCandidate) {
  return [
    c.symbol,
    c.fund_name,
    c.canonical_name,
    c.isin,
    c.peer_group,
    c.subtype,
    c.fund_house,
    c.benchmark,
    researchStatusLabel(c),
    c.reason,
    c.recommendation,
    c.rule_engine_result?.overall_score,
    c.rule_engine_result?.confidence,
    c.rule_engine_result?.data_completeness == null
      ? null
      : `${(c.rule_engine_result.data_completeness * 100).toFixed(0)}%`,
    c.rank,
    entryLabel(c),
    c.history_observations,
    c.historical_acquisition?.status,
    c.historical_acquisition?.reason,
    ...(c.missing_data ?? []),
    ...METRICS.map(([key]) => metricDisplay(c, key)),
    ...(c.source_references ?? []).flatMap((r) => [
      r.provider,
      r.metric,
      r.as_of,
      r.url,
    ]),
  ]
    .filter((v) => v != null)
    .join(" ")
    .toLowerCase();
}
export function filterEtfs(
  rows: EtfCandidate[],
  search: string,
  filters: EtfFilters,
): EtfCandidate[] {
  const terms = search.toLowerCase().trim().split(/\s+/).filter(Boolean);
  const bound = (input: string, value: number | null, max = false) =>
    !input.trim() ||
    (Number.isFinite(Number(input)) &&
      value != null &&
      (max ? value <= Number(input) : value >= Number(input)));
  return rows.filter(
    (c) =>
      terms.every((term) => candidateSearch(c).includes(term)) &&
      (filters.category === "ALL" ||
        (c.peer_group ?? "Unclassified") === filters.category) &&
      (filters.status === "ALL" || researchStatusLabel(c) === filters.status) &&
      (filters.confidence === "ALL" ||
        c.rule_engine_result?.confidence === filters.confidence) &&
      (!filters.fundHouse.trim() ||
        (c.fund_house ?? "")
          .toLowerCase()
          .includes(filters.fundHouse.trim().toLowerCase())) &&
      bound(filters.minScore, finite(c.rule_engine_result?.overall_score)) &&
      bound(
        filters.minCompleteness,
        c.rule_engine_result?.data_completeness == null
          ? null
          : c.rule_engine_result.data_completeness * 100,
      ) &&
      bound(filters.maxExpense, metricRaw(c, "expense_ratio"), true) &&
      bound(filters.minAum, metricRaw(c, "aum")) &&
      bound(filters.minHistory, finite(c.history_observations)),
  );
}
export function sortEtfs(
  rows: EtfCandidate[],
  key: EtfSortKey,
  direction: "asc" | "desc",
) {
  const value = (c: EtfCandidate): number | string | null =>
    key === "symbol"
      ? c.symbol
      : key === "fund"
        ? (c.fund_name ?? c.canonical_name ?? null)
        : key === "category"
          ? (c.peer_group ?? null)
          : key === "status"
            ? researchStatusLabel(c)
            : key === "entry"
              ? entryLabel(c)
              : key === "rank"
                ? finite(c.rank)
                : key === "score"
                  ? finite(c.rule_engine_result?.overall_score)
                  : key === "confidence"
                    ? ({ HIGH: 3, MEDIUM: 2, LOW: 1 }[
                        c.rule_engine_result?.confidence ?? ""
                      ] ?? null)
                    : key === "completeness"
                      ? finite(c.rule_engine_result?.data_completeness)
                      : key === "expense"
                        ? metricRaw(c, "expense_ratio")
                        : key === "aum"
                          ? metricRaw(c, "aum")
                          : finite(c.history_observations);
  return rows.slice().sort((a, b) => {
    const av = value(a),
      bv = value(b);
    if (av == null && bv != null) return 1;
    if (av != null && bv == null) return -1;
    const compared =
      av == null || bv == null
        ? 0
        : typeof av === "number" && typeof bv === "number"
          ? av - bv
          : String(av).localeCompare(String(bv));
    return (
      (direction === "asc" ? compared : -compared) ||
      candidateId(a).localeCompare(candidateId(b))
    );
  });
}
