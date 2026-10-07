import assert from "node:assert/strict";
import test from "node:test";

import {
  BACKTEST_STATUSES,
  LONG_TERM_HORIZON_BUCKETS,
  SHORT_TERM_HORIZON_BUCKETS,
  backtestHeading,
  backtestStatusLabel,
  horizonBucketsFor,
  normalizeBacktest
} from "../app/lib/backtest-status.ts";

const LEGACY_HORIZONS = ["1W", "1M", "3M", "6M", "1Y"];

function rawSample(id, status, evaluationDate, returnPct = null) {
  return {
    recommendation_id: id,
    global_instrument_id: `instrument-${id}`,
    company_name: `Company ${id}`,
    status,
    evaluation_date: evaluationDate,
    return_pct: returnPct,
    max_adverse_excursion_pct: null,
    benchmark_excess_return_pct: null
  };
}

test("all current backend statuses have distinct investor-facing labels", () => {
  assert.deepEqual(BACKTEST_STATUSES.map(backtestStatusLabel), [
    "Evaluated",
    "Not matured",
    "Market data unavailable",
    "Outside selected window",
    "Insufficient evidence",
    "Evaluation failed"
  ]);
});

test("persisted V1 unavailable statuses are separated by their calendar due date", () => {
  const samples = [
    rawSample("evaluated", "AVAILABLE", "2023-01-01T00:00:00Z", 8),
    rawSample("future", "FUTURE_PRICE_UNAVAILABLE", "2024-02-15T00:00:00Z"),
    rawSample("missing", "ENTRY_PRICE_UNAVAILABLE", "2023-01-01T00:00:00Z")
  ];
  const oldMetric = {
    recommendation_count: 3,
    evaluated_count: 1,
    missing_count: 2,
    hit_rate: 100,
    average_return: 8,
    median_return: 8,
    best_return: 8,
    worst_return: 8,
    max_adverse_excursion: null,
    benchmark_excess_return: null
  };
  const normalized = normalizeBacktest({
    backtest_id: "legacy",
    engine_version: "BACKTESTING_V1",
    generated_at: "2024-03-01T00:00:00Z",
    start: "2023-01-01T00:00:00Z",
    end: "2024-02-29T00:00:00Z",
    horizon: "SHORT_TERM",
    recommendation_count: 3,
    excluded_unavailable_evidence: 1,
    methodology: "Recorded evidence",
    metrics: Object.fromEntries(LEGACY_HORIZONS.map((horizon) => [horizon, oldMetric])),
    samples: Object.fromEntries(LEGACY_HORIZONS.map((horizon) => [horizon, samples])),
    winners: [],
    losers: []
  });

  assert.deepEqual(normalized.samples["1M"].map((sample) => sample.status), [
    "EVALUATED",
    "NOT_MATURED",
    "MISSING_MARKET_DATA"
  ]);
  assert.equal(normalized.samples["1M"][1].required_evaluation_date, "2024-03-16T00:00:00.000Z");
  assert.deepEqual(normalized.metrics["1M"], {
    recommendation_count: 4,
    evaluated_count: 1,
    not_matured_count: 1,
    missing_market_data_count: 1,
    outside_evaluation_window_count: 0,
    insufficient_evidence_count: 1,
    backend_failure_count: 0,
    success_rate: 100,
    hit_rate: 100,
    average_return: 8,
    average_market_return: null,
    median_return: 8,
    best_return: 8,
    worst_return: 8,
    max_adverse_excursion: null,
    benchmark_excess_return: null
  });
  assert.doesNotMatch(JSON.stringify(normalized), /undefined/i);
});

test("current status counts reconcile exactly and zero evaluated keeps performance null", () => {
  const samples = BACKTEST_STATUSES.map((status, index) => ({
    ...rawSample(String(index), status, "2024-01-01T00:00:00Z", status === "EVALUATED" ? null : null),
    required_evaluation_date: "2024-02-01T00:00:00Z"
  }));
  const rawMetric = {
    recommendation_count: samples.length,
    evaluated_count: 0,
    not_matured_count: 1,
    missing_market_data_count: 1,
    outside_evaluation_window_count: 1,
    insufficient_evidence_count: 1,
    backend_failure_count: 1,
    hit_rate: 0,
    average_return: 0,
    median_return: 0,
    best_return: 0,
    worst_return: 0,
    max_adverse_excursion: 0,
    benchmark_excess_return: 0
  };
  const normalized = normalizeBacktest({
    backtest_id: "current",
    engine_version: "BACKTESTING_V2",
    generated_at: "2024-03-01T00:00:00Z",
    as_of: "2024-03-01T00:00:00Z",
    start: "2024-01-01T00:00:00Z",
    end: "2024-02-01T00:00:00Z",
    horizon: "SHORT_TERM",
    recommendation_count: samples.length,
    excluded_unavailable_evidence: 0,
    methodology: "Recorded evidence",
    metrics: Object.fromEntries(LEGACY_HORIZONS.map((horizon) => [horizon, rawMetric])),
    samples: Object.fromEntries(LEGACY_HORIZONS.map((horizon) => [horizon, samples])),
    winners: [],
    losers: []
  });
  const metric = normalized.metrics["1M"];
  const statusTotal = metric.evaluated_count + metric.not_matured_count
    + metric.missing_market_data_count + metric.outside_evaluation_window_count
    + metric.insufficient_evidence_count + metric.backend_failure_count;
  assert.equal(statusTotal, metric.recommendation_count);

  const withoutEvaluated = normalizeBacktest({
    ...normalized,
    samples: Object.fromEntries(normalized.available_horizons.map((horizon) => [horizon,
      normalized.samples[horizon].filter((sample) => sample.status !== "EVALUATED")
    ]))
  });
  const zero = withoutEvaluated.metrics["1M"];
  assert.equal(zero.evaluated_count, 0);
  assert.equal(zero.hit_rate, null);
  assert.equal(zero.average_return, null);
  assert.equal(zero.median_return, null);
});

test("recommendation type exposes only its own evaluation horizons", () => {
  assert.deepEqual([...SHORT_TERM_HORIZON_BUCKETS], ["1W", "1M", "3M", "6M"]);
  assert.deepEqual([...LONG_TERM_HORIZON_BUCKETS], ["1Y", "2Y", "3Y"]);
  assert.deepEqual([...horizonBucketsFor("SHORT_TERM")], ["1W", "1M", "3M", "6M"]);
  assert.deepEqual([...horizonBucketsFor("LONG_TERM")], ["1Y", "2Y", "3Y"]);
  assert.equal(backtestHeading("SHORT_TERM", "3M"), "RADAR BACKTEST — SHORT-TERM — 3 MONTHS");
  assert.equal(backtestHeading("LONG_TERM", "1Y"), "RADAR BACKTEST — LONG-TERM — 1 YEAR");
});

test("normalization never mixes short-term and long-term accuracy buckets", () => {
  const metric = {
    recommendation_count: 1,
    evaluated_count: 1,
    success_rate: 75,
    hit_rate: 75,
    average_return: 12,
    median_return: 12,
    best_return: 12,
    worst_return: 12
  };
  const common = {
    generated_at: "2026-01-01T00:00:00Z",
    as_of: "2026-01-01T00:00:00Z",
    start: "2024-01-01T00:00:00Z",
    end: "2024-01-02T00:00:00Z",
    recommendation_count: 1,
    excluded_unavailable_evidence: 0,
    methodology: "Immutable originals",
    winners: [],
    losers: []
  };
  const shortRun = normalizeBacktest({
    ...common,
    backtest_id: "short",
    engine_version: "BACKTESTING_V3",
    horizon: "SHORT_TERM",
    available_horizons: ["1W", "1M", "3M", "6M"],
    metrics: Object.fromEntries(["1W", "1M", "3M", "6M"].map(h => [h, metric])),
    samples: {}
  });
  const longRun = normalizeBacktest({
    ...common,
    backtest_id: "long",
    engine_version: "BACKTESTING_V3",
    horizon: "LONG_TERM",
    available_horizons: ["1Y", "2Y", "3Y"],
    metrics: Object.fromEntries(["1Y", "2Y", "3Y"].map(h => [h, metric])),
    samples: {}
  });
  assert.deepEqual(shortRun.available_horizons, ["1W", "1M", "3M", "6M"]);
  assert.equal(shortRun.metrics["1Y"], undefined);
  assert.deepEqual(longRun.available_horizons, ["1Y", "2Y", "3Y"]);
  assert.equal(longRun.metrics["1W"], undefined);
});

test("legacy long-term runs expose one year and hide legacy short-term buckets", () => {
  const metric = { recommendation_count: 0, evaluated_count: 0, missing_count: 0 };
  const normalized = normalizeBacktest({
    backtest_id: "legacy-long",
    engine_version: "BACKTESTING_V1",
    generated_at: "2026-01-01T00:00:00Z",
    start: "2024-01-01T00:00:00Z",
    end: "2024-01-02T00:00:00Z",
    horizon: "LONG_TERM",
    recommendation_count: 0,
    excluded_unavailable_evidence: 0,
    methodology: "Legacy",
    metrics: Object.fromEntries(LEGACY_HORIZONS.map(horizon => [horizon, metric])),
    samples: {}, winners: [], losers: []
  });
  assert.deepEqual(normalized.available_horizons, ["1Y"]);
  assert.equal(normalized.metrics["1W"], undefined);
});
