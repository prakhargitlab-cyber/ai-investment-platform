"use client";
import { useEffect, useMemo, useState } from "react";
import { request } from "../lib/portfolio-api";
import { Badge, Button, Card, EmptyState, ErrorState, Field, MetricCard, Skeleton } from "./ui";
import {
  backtestHeading, backtestStatusLabel, backtestStatusTone, horizonBucketLabel,
  normalizeBacktest, recommendationTypeLabel,
  type Backtest, type BacktestPayload, type BacktestSample, type HorizonBucket
} from "../lib/backtest-status";

const PAGE_SIZE = 10;

function readableDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleDateString();
}

function pct(value: number | null): string {
  return value == null ? "N/A" : `${value.toFixed(2)}%`;
}

function valueTone(value: number | null): "positive" | "negative" | "neutral" {
  if (value == null || value === 0) return "neutral";
  return value > 0 ? "positive" : "negative";
}

function returnCell(sample: BacktestSample): string {
  if (sample.status === "EVALUATED") {
    const signalReturn = pct(sample.signal_return_pct);
    return sample.direction === "SELL"
      ? `${signalReturn} signal (${pct(sample.return_pct)} stock)`
      : signalReturn;
  }
  if (sample.status === "NOT_MATURED") {
    return sample.required_evaluation_date
      ? `Evaluation due ${readableDate(sample.required_evaluation_date)}`
      : "Not matured";
  }
  if (sample.status === "MISSING_MARKET_DATA") return "Required market price unavailable";
  if (sample.status === "INSUFFICIENT_EVIDENCE") return "Evidence unavailable at recommendation time";
  if (sample.status === "BACKEND_FAILURE") return "Technical evaluation failure";
  if (sample.status === "OUTSIDE_EVALUATION_WINDOW") return "Outside selected period";
  return "—";
}

function instrumentName(sample: BacktestSample): string {
  return sample.company_name ?? sample.symbol ?? "Company unavailable";
}

function BucketTabs({ bucket, buckets, onChange }: {
  bucket: HorizonBucket;
  buckets: HorizonBucket[];
  onChange: (b: HorizonBucket) => void;
}) {
  return (
    <div className="sort-control" role="tablist" aria-label="Evaluation horizon">
      {buckets.map(b => (
        <Button key={b} type="button" variant={b === bucket ? "primary" : "ghost"}
          aria-selected={b === bucket} role="tab" onClick={() => onChange(b)}>
          {horizonBucketLabel(b)}
        </Button>
      ))}
    </div>
  );
}

function ResultsTable({ samples }: { samples: BacktestSample[] }) {
  const [page, setPage] = useState(1);
  if (samples.length === 0) {
    return <EmptyState title="No recommendations" message="No recommendations fall into this horizon for the selected period." />;
  }
  const start = (page - 1) * PAGE_SIZE;
  const visible = samples.slice(start, start + PAGE_SIZE);
  const totalPages = Math.max(1, Math.ceil(samples.length / PAGE_SIZE));
  return (
    <>
      <div className="table-frame">
        <table className="backtest-table">
          <thead>
            <tr>
              <th>Instrument</th><th>Direction</th><th>Recommendation date</th><th>Evaluation due</th>
              <th>Status</th><th>Signal return</th>
            </tr>
          </thead>
          <tbody>
            {visible.map(s => (
              <tr key={`${s.recommendation_id}`}>
                <td>{instrumentName(s)}</td>
                <td>{s.direction ?? "—"}</td>
                <td>{readableDate(s.evaluation_date)}</td>
                <td>{readableDate(s.required_evaluation_date)}</td>
                <td><Badge tone={backtestStatusTone(s.status)}>{backtestStatusLabel(s.status)}</Badge></td>
                <td>{returnCell(s)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="pagination-controls" role="navigation" aria-label="Results pages">
        <button type="button" className="pagination-prev" disabled={page <= 1} onClick={() => setPage(p => Math.max(1, p - 1))}>Previous</button>
        <span aria-live="polite">Page {page} of {totalPages}</span>
        <button type="button" className="pagination-next" disabled={page >= totalPages} onClick={() => setPage(p => Math.min(totalPages, p + 1))}>Next</button>
      </div>
    </>
  );
}

export function BacktestResults({ run }: { run: BacktestPayload }) {
  const normalizedRun = useMemo(() => normalizeBacktest(run), [run]);
  const [bucket, setBucket] = useState<HorizonBucket>(normalizedRun.available_horizons[0]);
  const activeBucket = normalizedRun.available_horizons.includes(bucket)
    ? bucket
    : normalizedRun.available_horizons[0];
  const metric = normalizedRun.metrics[activeBucket];
  const samples = normalizedRun.samples[activeBucket] ?? [];
  const exampleBucket = normalizedRun.example_horizon ?? normalizedRun.available_horizons[0];
  const maturedCount = metric.recommendation_count
    - metric.not_matured_count
    - metric.outside_evaluation_window_count;

  return (
    <>
      <Card className="wide-panel">
        <div className="panel-header compact">
          <h2>{backtestHeading(normalizedRun.horizon, activeBucket)}</h2>
        </div>
        <p>
          Selection period: {readableDate(normalizedRun.start)} to {readableDate(normalizedRun.end)}. Evaluation date: {readableDate(normalizedRun.as_of)}.
        </p>
        <div className="panel-header compact">
          <h3>{recommendationTypeLabel(normalizedRun.horizon)} RADAR ACCURACY</h3>
        </div>
        <div className="table-frame">
          <table className="backtest-table">
            <thead><tr><th>Horizon</th><th>Evaluated</th><th>Success rate</th><th>Avg return</th></tr></thead>
            <tbody>
              {normalizedRun.available_horizons.map(horizon => {
                const horizonMetric = normalizedRun.metrics[horizon];
                return <tr key={horizon}>
                  <th>{horizonBucketLabel(horizon)}</th>
                  <td>{horizonMetric.evaluated_count}</td>
                  <td>{pct(horizonMetric.success_rate)}</td>
                  <td>{pct(horizonMetric.average_return)}</td>
                </tr>;
              })}
            </tbody>
          </table>
        </div>
        <BucketTabs bucket={activeBucket} buckets={normalizedRun.available_horizons} onChange={setBucket} />
        <div className="metrics-grid backtest-status-grid">
          <MetricCard label="Total recommendations" value={String(metric.recommendation_count)}
            meta={metric.outside_evaluation_window_count ? `${metric.outside_evaluation_window_count} outside selected window` : undefined} />
          <MetricCard label="Matured" value={String(maturedCount)} />
          <MetricCard label="Evaluated" value={String(metric.evaluated_count)} tone={metric.evaluated_count ? "positive" : "neutral"} />
          <MetricCard label="Not matured" value={String(metric.not_matured_count)} tone="info" />
          <MetricCard label="Missing market data" value={String(metric.missing_market_data_count)} tone={metric.missing_market_data_count ? "warning" : "neutral"} />
          <MetricCard label="Insufficient evidence" value={String(metric.insufficient_evidence_count)} tone={metric.insufficient_evidence_count ? "warning" : "neutral"} />
          <MetricCard label="Failed" value={String(metric.backend_failure_count)} tone={metric.backend_failure_count ? "negative" : "neutral"} />
          <MetricCard label="Outside selected window" value={String(metric.outside_evaluation_window_count)} />
        </div>
        <p className="backtest-accounting-note">
          Every recommendation has one status. Evaluated + not matured + missing market data + insufficient evidence + failed + outside window = {metric.recommendation_count}.
        </p>
        <details className="backtest-methodology">
          <summary>How this evaluation works</summary>
          <p>{normalizedRun.methodology}</p>
        </details>
        <div className="panel-header compact"><h3>Aggregate performance — {horizonBucketLabel(activeBucket)}</h3></div>
        {metric.evaluated_count === 0 ? (
          <EmptyState title="Not enough evaluated recommendations"
            message="No recommendation in this horizon has a completed return calculation. The status counts above explain why." />
        ) : (
          <div className="metrics-grid">
            <MetricCard label="Success rate" value={pct(metric.success_rate)} tone={valueTone(metric.success_rate)} />
            <MetricCard label="Average return" value={pct(metric.average_return)} tone={valueTone(metric.average_return)} />
            <MetricCard label="Median return" value={pct(metric.median_return)} tone={valueTone(metric.median_return)} />
            <MetricCard label="Best return" value={pct(metric.best_return)} tone="positive" />
            <MetricCard label="Worst return" value={pct(metric.worst_return)} tone="negative" />
            <MetricCard label="Benchmark excess" value={pct(metric.benchmark_excess_return)} />
          </div>
        )}
      </Card>

      <Card className="wide-panel">
        <div className="panel-header compact"><h2>Recommendations — {horizonBucketLabel(activeBucket)}</h2></div>
        <p className="backtest-accounting-note">Recommendation date is when Radar recorded the recommendation. Evaluation due is that date plus the selected calendar horizon.</p>
        <ResultsTable key={`${normalizedRun.backtest_id}:${activeBucket}`} samples={samples} />
      </Card>

      <Card className="wide-panel">
        <div className="panel-header compact"><h2>Winners &amp; losers — {horizonBucketLabel(exampleBucket)}</h2></div>
        <div className="backtest-examples">
          <div>
            <h3>Winners</h3>
            {normalizedRun.winners.length
              ? <ul>{normalizedRun.winners.map(e => <li key={e.recommendation_id}>{instrumentName(e)}: {returnCell(e)}</li>)}</ul>
              : <p>No winning examples yet.</p>}
          </div>
          <div>
            <h3>Losers</h3>
            {normalizedRun.losers.length
              ? <ul>{normalizedRun.losers.map(e => <li key={e.recommendation_id}>{instrumentName(e)}: {returnCell(e)}</li>)}</ul>
              : <p>No losing examples yet.</p>}
          </div>
        </div>
      </Card>
    </>
  );
}

function defaultDateRange(): { start: string; end: string } {
  const end = new Date();
  const start = new Date(end);
  start.setDate(start.getDate() - 90);
  const iso = (d: Date) => d.toISOString().slice(0, 10);
  return { start: iso(start), end: iso(end) };
}

export function Backtesting() {
  const defaults = useMemo(() => defaultDateRange(), []);
  const [start, setStart] = useState(defaults.start);
  const [end, setEnd] = useState(defaults.end);
  const [asOf, setAsOf] = useState("");
  const [horizon, setHorizon] = useState("SHORT_TERM");
  const [runs, setRuns] = useState<Backtest[]>([]);
  const [runsLoading, setRunsLoading] = useState(true);
  const [runsError, setRunsError] = useState("");
  const [selected, setSelected] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    request<BacktestPayload[]>("/api/v1/research/backtesting/runs")
      .then((response) => {
        if (!active) return;
        const normalized = response.map(normalizeBacktest);
        setRuns(normalized);
        setSelected(normalized[0]?.backtest_id ?? "");
      })
      .catch(() => { if (active) setRunsError("Unable to load persisted backtests."); })
      .finally(() => { if (active) setRunsLoading(false); });
    return () => { active = false; };
  }, []);

  async function runBacktest() {
    setBusy(true); setError("");
    try {
      const response = await request<BacktestPayload>("/api/v1/research/backtesting/runs", {
        method: "POST",
        body: JSON.stringify({
          start: `${start}T00:00:00Z`,
          end: new Date(Math.min(new Date(`${end}T23:59:59Z`).getTime(), Date.now())).toISOString(),
          market: "NSE", horizon, ...(asOf ? { as_of: `${asOf}T23:59:59Z` } : {})
        })
      });
      const result = normalizeBacktest(response);
      setRuns(old => [result, ...old]); setSelected(result.backtest_id);
    } catch (err) {
      const message = (err as { message?: string } | null)?.message;
      setError(message || "Backtest could not run. Check the date range and try again.");
    } finally { setBusy(false); }
  }

  const result = runs.find(r => r.backtest_id === selected);

  return (
    <section className="backtesting-page" aria-label="Backtesting">
      <div className="page-header">
        <div>
          <h1>RADAR BACKTEST</h1>
          <p>Evaluate each original Radar recommendation at the horizons defined by its persisted investment type.</p>
        </div>
      </div>

      <Card className="wide-panel">
        <div className="panel-header compact"><h2>Run a backtest</h2></div>
        <form className="backtest-controls" onSubmit={e => { e.preventDefault(); void runBacktest(); }}>
          <Field label="Selection period — start" hint="Recommendations generated on or after this date">
            <input type="date" required value={start} onChange={e => setStart(e.target.value)} />
          </Field>
          <Field label="Selection period — end" hint="Recommendations generated on or before this date">
            <input type="date" required min={start} value={end} onChange={e => setEnd(e.target.value)} />
          </Field>
          <Field label="Evaluation date (as-of)" hint="Leave blank to evaluate as of today">
            <input type="date" value={asOf} max={defaults.end} onChange={e => setAsOf(e.target.value)} />
          </Field>
          <Field label="Market">
            <select value="NSE" disabled><option>NSE</option></select>
          </Field>
          <Field label="Signal type" hint="Which persisted investment-horizon class is being tested">
            <select value={horizon} onChange={e => setHorizon(e.target.value)}>
              <option value="SHORT_TERM">Short-term</option>
              <option value="LONG_TERM">Long-term</option>
            </select>
          </Field>
          <Button disabled={busy}>{busy ? "Running…" : "Run backtest"}</Button>
        </form>
        {error && <ErrorState message={error} />}
      </Card>

      <Card className="wide-panel">
        <div className="panel-header compact"><h2>Persisted runs</h2></div>
        {runsLoading ? <Skeleton rows={2} /> : runsError ? <ErrorState message={runsError} /> : runs.length === 0 ? (
          <EmptyState title="No backtests yet" message="Run a backtest above to see evaluated results here." />
        ) : (
          <Field label="Select a run">
            <select value={selected} onChange={e => setSelected(e.target.value)}>
              {runs.map(r => (
                <option key={r.backtest_id} value={r.backtest_id}>
                  {new Date(r.generated_at).toLocaleString()} · {r.recommendation_count} recommendations · {r.horizon === "SHORT_TERM" ? "Short-term" : "Long-term"}
                </option>
              ))}
            </select>
          </Field>
        )}
      </Card>

      {result ? <BacktestResults key={result.backtest_id} run={result} /> : !runsLoading && !runsError && runs.length > 0 ? (
        <Card className="wide-panel"><EmptyState title="No run selected" message="Select a persisted run above, or run a new backtest." /></Card>
      ) : null}
    </section>
  );
}
