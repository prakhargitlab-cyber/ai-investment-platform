"use client";
import { useEffect, useState } from "react";
import { request } from "../lib/portfolio-api";

type Metric = { recommendation_count: number; evaluated_count: number; missing_count: number;
  hit_rate: number | null; average_return: number | null; median_return: number | null };
type Example = { recommendation_id: string; global_instrument_id: string; symbol?: string; company_name?: string; return_pct: number };
export type Backtest = { backtest_id: string; generated_at: string; recommendation_count: number;
  metrics: Record<string, Metric>; winners: Example[]; losers: Example[]; methodology: string };
const pct = (v: number | null) => v == null ? "Insufficient evidence" : `${v.toFixed(2)}%`;

export function BacktestResults({ run }: { run: Backtest }) {
  return <section aria-label="Backtest results"><h3>Recommendations tested: {run.recommendation_count}</h3>
    <p>{run.methodology}</p><div style={{ overflowX: "auto" }}><table><thead><tr><th>Horizon</th><th>Evaluated</th><th>Missing</th><th>Hit rate</th><th>Average return</th><th>Median return</th></tr></thead>
      <tbody>{["1W", "1M", "3M", "6M", "1Y"].map(h => <tr key={h}><th>{h}</th><td>{run.metrics[h].evaluated_count}</td><td>{run.metrics[h].missing_count}</td>
        <td>{pct(run.metrics[h].hit_rate)}</td><td>{pct(run.metrics[h].average_return)}</td><td>{pct(run.metrics[h].median_return)}</td></tr>)}</tbody></table></div>
    <h4>Winner examples</h4>{run.winners.length ? <ul>{run.winners.map(e => <li key={e.recommendation_id}>{e.company_name ?? e.symbol ?? "Company unavailable"}: {pct(e.return_pct)}</li>)}</ul> : <p>No winner evidence yet.</p>}
    <h4>Loser examples</h4>{run.losers.length ? <ul>{run.losers.map(e => <li key={e.recommendation_id}>{e.company_name ?? e.symbol ?? "Company unavailable"}: {pct(e.return_pct)}</li>)}</ul> : <p>No loser evidence yet.</p>}
  </section>;
}

export function Backtesting() {
  const [start, setStart] = useState(""); const [end, setEnd] = useState("");
  const [horizon, setHorizon] = useState("SHORT_TERM"); const [runs, setRuns] = useState<Backtest[]>([]);
  const [selected, setSelected] = useState(""); const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  useEffect(() => { let active = true;
    request<Backtest[]>("/api/v1/research/backtesting/runs").then(r => { if (active) { setRuns(r); setSelected(r[0]?.backtest_id ?? ""); } }).catch(() => { if (active) setError("Unable to load backtests."); });
    return () => { active = false; };
  }, []);
  async function run() {
    setBusy(true); setError("");
    try {
      const result = await request<Backtest>("/api/v1/research/backtesting/runs", { method: "POST", body: JSON.stringify({
        start: `${start}T00:00:00Z`, end: new Date(Math.min(new Date(`${end}T23:59:59Z`).getTime(), Date.now())).toISOString(), market: "NSE", horizon }) });
      setRuns(old => [result, ...old]); setSelected(result.backtest_id);
    } catch { setError("Backtest could not run. Check the date range and try again."); }
    finally { setBusy(false); }
  }
  const result = runs.find(r => r.backtest_id === selected);
  return <section className="opportunity-radar"><h2>BACKTESTING</h2>
    <p>Evaluate persisted recommendations using recorded evidence and subsequent prices.</p>
    <form onSubmit={e => { e.preventDefault(); void run(); }}>
      <label>Start date <input type="date" required value={start} onChange={e => setStart(e.target.value)} /></label>{" "}
      <label>End date <input type="date" required min={start} value={end} onChange={e => setEnd(e.target.value)} /></label>{" "}
      <label>Market <select><option>NSE</option></select></label>{" "}
      <label>Horizon <select value={horizon} onChange={e => setHorizon(e.target.value)}><option value="SHORT_TERM">Short-term</option><option value="LONG_TERM">Long-term</option></select></label>{" "}
      <button disabled={busy}>{busy ? "Running…" : "Run backtest"}</button>
    </form>
    {error && <p role="alert">{error}</p>}
    <label>Persisted backtest <select value={selected} onChange={e => setSelected(e.target.value)}><option value="">Select a run</option>
      {runs.map(r => <option key={r.backtest_id} value={r.backtest_id}>{r.generated_at} · {r.recommendation_count} recommendations</option>)}</select></label>
    {result ? <BacktestResults run={result} /> : <p>No backtest selected.</p>}
  </section>;
}
