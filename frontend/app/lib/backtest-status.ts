import type { Tone } from "../components/ui";

/**
 * Centralized mapping from the backtesting evaluation-status enum (as the
 * backend actually emits it -- app/recommendation_backtesting.py) to a
 * friendly label and a badge tone, so no component scatters its own
 * `.replaceAll("_", " ")` or ad-hoc string handling for these six states.
 */
export const BACKTEST_STATUSES = [
  "EVALUATED",
  "NOT_MATURED",
  "MISSING_MARKET_DATA",
  "OUTSIDE_EVALUATION_WINDOW",
  "INSUFFICIENT_EVIDENCE",
  "BACKEND_FAILURE"
] as const;

export type BacktestStatus = (typeof BACKTEST_STATUSES)[number];

const LABELS: Record<BacktestStatus, string> = {
  EVALUATED: "Evaluated",
  NOT_MATURED: "Not matured",
  MISSING_MARKET_DATA: "Market data unavailable",
  OUTSIDE_EVALUATION_WINDOW: "Outside selected window",
  INSUFFICIENT_EVIDENCE: "Insufficient evidence",
  BACKEND_FAILURE: "Evaluation failed"
};

const TONES: Record<BacktestStatus, Tone> = {
  EVALUATED: "positive",
  NOT_MATURED: "info",
  MISSING_MARKET_DATA: "warning",
  OUTSIDE_EVALUATION_WINDOW: "neutral",
  INSUFFICIENT_EVIDENCE: "warning",
  BACKEND_FAILURE: "negative"
};

export function isBacktestStatus(value: string): value is BacktestStatus {
  return (BACKTEST_STATUSES as readonly string[]).includes(value);
}

/** Friendly label for a backtest evaluation status. Falls back to a
 * de-slugged rendering of any unrecognized value rather than crashing, so a
 * future backend-added status never shows as a raw SCREAMING_SNAKE string
 * without at least basic readability. */
export function backtestStatusLabel(status: string): string {
  if (isBacktestStatus(status)) return LABELS[status];
  return status.replaceAll("_", " ").toLowerCase().replace(/^./, c => c.toUpperCase());
}

export function backtestStatusTone(status: string): Tone {
  return isBacktestStatus(status) ? TONES[status] : "neutral";
}

/** Recommendation type and evaluation horizon are separate persisted dimensions. */
export const SHORT_TERM_HORIZON_BUCKETS = ["1W", "1M", "3M", "6M"] as const;
export const LONG_TERM_HORIZON_BUCKETS = ["1Y", "2Y", "3Y"] as const;
export const HORIZON_BUCKETS = [
  ...SHORT_TERM_HORIZON_BUCKETS,
  ...LONG_TERM_HORIZON_BUCKETS
] as const;
export type HorizonBucket = (typeof HORIZON_BUCKETS)[number];
export type RecommendationType = "SHORT_TERM" | "LONG_TERM";

const BUCKET_LABELS: Record<HorizonBucket, string> = {
  "1W": "1 week", "1M": "1 month", "3M": "3 months", "6M": "6 months",
  "1Y": "1 year", "2Y": "2 years", "3Y": "3 years"
};

export function horizonBucketLabel(bucket: HorizonBucket): string {
  return BUCKET_LABELS[bucket];
}

export function horizonBucketsFor(recommendationType: string): readonly HorizonBucket[] {
  return recommendationType === "LONG_TERM"
    ? LONG_TERM_HORIZON_BUCKETS
    : SHORT_TERM_HORIZON_BUCKETS;
}

export function recommendationTypeLabel(recommendationType: string): string {
  return recommendationType === "LONG_TERM" ? "LONG-TERM" : "SHORT-TERM";
}

export function backtestHeading(recommendationType: string, bucket: HorizonBucket): string {
  return `RADAR BACKTEST — ${recommendationTypeLabel(recommendationType)} — ${horizonBucketLabel(bucket).toUpperCase()}`;
}

export type BacktestMetric = {
  recommendation_count: number;
  evaluated_count: number;
  not_matured_count: number;
  missing_market_data_count: number;
  outside_evaluation_window_count: number;
  insufficient_evidence_count: number;
  backend_failure_count: number;
  success_rate: number | null;
  hit_rate: number | null;
  average_return: number | null;
  average_market_return: number | null;
  median_return: number | null;
  best_return: number | null;
  worst_return: number | null;
  max_adverse_excursion: number | null;
  benchmark_excess_return: number | null;
};

export type BacktestSample = {
  recommendation_id: string;
  global_instrument_id: string;
  symbol?: string;
  company_name?: string;
  action?: string | null;
  direction?: "BUY" | "SELL" | null;
  recommendation_type?: string | null;
  tested_recommendation_type?: string | null;
  status: BacktestStatus;
  evaluation_date: string | null;
  required_evaluation_date: string | null;
  return_pct: number | null;
  signal_return_pct: number | null;
  success: boolean | null;
  entry_price?: number | null;
  exit_price?: number | null;
  max_adverse_excursion_pct: number | null;
  benchmark_excess_return_pct: number | null;
};

export type Backtest = {
  backtest_id: string;
  generated_at: string;
  as_of?: string;
  engine_version?: string;
  start: string;
  end: string;
  horizon: string;
  recommendation_type?: string;
  available_horizons: HorizonBucket[];
  example_horizon?: HorizonBucket;
  recommendation_count: number;
  selected_recommendation_count?: number;
  excluded_unavailable_evidence: number;
  metrics: Record<string, BacktestMetric>;
  samples: Record<string, BacktestSample[]>;
  winners: BacktestSample[];
  losers: BacktestSample[];
  methodology: string;
};

type RawMetric = Partial<BacktestMetric> & { missing_count?: number };
type RawSample = Omit<BacktestSample, "status" | "required_evaluation_date" | "signal_return_pct" | "success"> & {
  status: string;
  required_evaluation_date?: string | null;
  signal_return_pct?: number | null;
  success?: boolean | null;
};

export type BacktestPayload = Omit<Backtest, "available_horizons" | "metrics" | "samples" | "winners" | "losers"> & {
  available_horizons?: string[];
  metrics?: Record<string, RawMetric>;
  samples?: Record<string, RawSample[]>;
  winners?: RawSample[];
  losers?: RawSample[];
};

const HORIZON_DAYS: Record<HorizonBucket, number> = {
  "1W": 7,
  "1M": 30,
  "3M": 91,
  "6M": 182,
  "1Y": 365,
  "2Y": 730,
  "3Y": 1095
};

function isHorizonBucket(value: string): value is HorizonBucket {
  return (HORIZON_BUCKETS as readonly string[]).includes(value);
}

function count(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? Math.floor(value) : 0;
}

function nullableNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function requiredEvaluationDate(evaluationDate: string | null, bucket: HorizonBucket): string | null {
  if (!evaluationDate) return null;
  const date = new Date(evaluationDate);
  if (Number.isNaN(date.getTime())) return null;
  date.setUTCDate(date.getUTCDate() + HORIZON_DAYS[bucket]);
  return date.toISOString();
}

function normalizeStatus(status: string, requiredDate: string | null, asOf: string): BacktestStatus {
  if (isBacktestStatus(status)) return status;
  if (status === "AVAILABLE") return "EVALUATED";
  if (status === "ENTRY_PRICE_UNAVAILABLE" || status === "FUTURE_PRICE_UNAVAILABLE") {
    const due = requiredDate ? new Date(requiredDate).getTime() : Number.NaN;
    const evaluatedAt = new Date(asOf).getTime();
    return Number.isFinite(due) && Number.isFinite(evaluatedAt) && due > evaluatedAt
      ? "NOT_MATURED"
      : "MISSING_MARKET_DATA";
  }
  return "BACKEND_FAILURE";
}

function normalizeSample(sample: RawSample, bucket: HorizonBucket, asOf: string): BacktestSample {
  const due = sample.required_evaluation_date ?? requiredEvaluationDate(sample.evaluation_date, bucket);
  const status = normalizeStatus(sample.status, due, asOf);
  const marketReturn = nullableNumber(sample.return_pct);
  const signalReturn = nullableNumber(sample.signal_return_pct) ?? marketReturn;
  return {
    ...sample,
    status,
    evaluation_date: sample.evaluation_date ?? null,
    required_evaluation_date: due,
    return_pct: marketReturn,
    signal_return_pct: signalReturn,
    success: typeof sample.success === "boolean"
      ? sample.success
      : status === "EVALUATED" && signalReturn != null ? signalReturn > 0 : null,
    max_adverse_excursion_pct: nullableNumber(sample.max_adverse_excursion_pct),
    benchmark_excess_return_pct: nullableNumber(sample.benchmark_excess_return_pct)
  };
}

function metricFromSamples(
  samples: BacktestSample[],
  rawMetric: RawMetric,
  omittedInsufficientEvidence: number
): BacktestMetric {
  const statusCounts: Record<BacktestStatus, number> = {
    EVALUATED: 0,
    NOT_MATURED: 0,
    MISSING_MARKET_DATA: 0,
    OUTSIDE_EVALUATION_WINDOW: 0,
    INSUFFICIENT_EVIDENCE: omittedInsufficientEvidence,
    BACKEND_FAILURE: 0
  };
  for (const sample of samples) statusCounts[sample.status] += 1;

  if (!samples.length) {
    statusCounts.EVALUATED = count(rawMetric.evaluated_count);
    statusCounts.NOT_MATURED = count(rawMetric.not_matured_count);
    statusCounts.MISSING_MARKET_DATA = count(rawMetric.missing_market_data_count ?? rawMetric.missing_count);
    statusCounts.OUTSIDE_EVALUATION_WINDOW = count(rawMetric.outside_evaluation_window_count);
    statusCounts.INSUFFICIENT_EVIDENCE = Math.max(
      omittedInsufficientEvidence,
      count(rawMetric.insufficient_evidence_count)
    );
    statusCounts.BACKEND_FAILURE = count(rawMetric.backend_failure_count);
  }

  const recommendationCount = Object.values(statusCounts).reduce((total, value) => total + value, 0);
  const evaluatedCount = statusCounts.EVALUATED;
  const successRate = evaluatedCount
    ? nullableNumber(rawMetric.success_rate ?? rawMetric.hit_rate)
    : null;
  return {
    recommendation_count: recommendationCount,
    evaluated_count: evaluatedCount,
    not_matured_count: statusCounts.NOT_MATURED,
    missing_market_data_count: statusCounts.MISSING_MARKET_DATA,
    outside_evaluation_window_count: statusCounts.OUTSIDE_EVALUATION_WINDOW,
    insufficient_evidence_count: statusCounts.INSUFFICIENT_EVIDENCE,
    backend_failure_count: statusCounts.BACKEND_FAILURE,
    success_rate: successRate,
    hit_rate: successRate,
    average_return: evaluatedCount ? nullableNumber(rawMetric.average_return) : null,
    average_market_return: evaluatedCount ? nullableNumber(rawMetric.average_market_return) : null,
    median_return: evaluatedCount ? nullableNumber(rawMetric.median_return) : null,
    best_return: evaluatedCount ? nullableNumber(rawMetric.best_return) : null,
    worst_return: evaluatedCount ? nullableNumber(rawMetric.worst_return) : null,
    max_adverse_excursion: evaluatedCount ? nullableNumber(rawMetric.max_adverse_excursion) : null,
    benchmark_excess_return: evaluatedCount ? nullableNumber(rawMetric.benchmark_excess_return) : null
  };
}

/** Normalizes both current V2 runs and persisted V1 runs at the API boundary.
 * V1 used AVAILABLE / *_PRICE_UNAVAILABLE and omitted the detailed counters;
 * persisted samples and their calendar horizon provide the authoritative
 * replacement status without inventing performance values. */
export function normalizeBacktest(raw: BacktestPayload): Backtest {
  const asOf = raw.as_of ?? raw.generated_at;
  const legacyOmittedEvidence = raw.engine_version === "BACKTESTING_V1"
    ? count(raw.excluded_unavailable_evidence)
    : 0;
  const metrics: Record<string, BacktestMetric> = {};
  const samples: Record<string, BacktestSample[]> = {};
  const allowedBuckets = horizonBucketsFor(raw.horizon);
  const advertisedBuckets = (raw.available_horizons ?? [])
    .filter(isHorizonBucket)
    .filter((bucket) => allowedBuckets.includes(bucket));
  const persistedBuckets = allowedBuckets.filter(
    (bucket) => raw.metrics?.[bucket] != null || raw.samples?.[bucket] != null
  );
  // V1 exposed all five historical buckets for every type. Restrict those
  // persisted runs at the client boundary: four short-term buckets, or 1Y for
  // a legacy long-term run. V3 advertises 2Y/3Y explicitly when supported.
  const availableHorizons = advertisedBuckets.length
    ? advertisedBuckets
    : persistedBuckets.length
      ? persistedBuckets
      : raw.horizon === "LONG_TERM" ? ["1Y" as const] : [...SHORT_TERM_HORIZON_BUCKETS];

  for (const bucket of availableHorizons) {
    const rawBucketSamples = raw.samples?.[bucket];
    const bucketSamples = Array.isArray(rawBucketSamples)
      ? rawBucketSamples.map((sample) => normalizeSample(sample, bucket, asOf))
      : [];
    samples[bucket] = bucketSamples;
    metrics[bucket] = metricFromSamples(
      bucketSamples,
      raw.metrics?.[bucket] ?? {},
      bucketSamples.some((sample) => sample.status === "INSUFFICIENT_EVIDENCE") ? 0 : legacyOmittedEvidence
    );
  }

  const requestedExample = raw.example_horizon;
  const exampleBucket = requestedExample && availableHorizons.includes(requestedExample)
    ? requestedExample
    : raw.horizon === "SHORT_TERM" && availableHorizons.includes("1M")
      ? "1M"
      : availableHorizons[0];
  return {
    ...raw,
    as_of: asOf,
    available_horizons: availableHorizons,
    example_horizon: exampleBucket,
    recommendation_count: metrics[exampleBucket]?.recommendation_count ?? count(raw.recommendation_count),
    excluded_unavailable_evidence: count(raw.excluded_unavailable_evidence),
    metrics,
    samples,
    winners: (raw.winners ?? []).map((sample) => normalizeSample(sample, exampleBucket, asOf)),
    losers: (raw.losers ?? []).map((sample) => normalizeSample(sample, exampleBucket, asOf)),
    methodology: raw.methodology || "Recorded recommendations evaluated at fixed calendar horizons."
  };
}
