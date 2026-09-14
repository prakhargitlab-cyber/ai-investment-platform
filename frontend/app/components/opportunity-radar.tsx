"use client";

import { useEffect, useState } from "react";
import { request } from "../lib/portfolio-api";

export type Opportunity = {
  global_instrument_id: string; company_name?: string; symbol?: string; current_price: number | null;
  opportunity_score: number | null; opportunity_confidence: number; score_coverage: number;
  new_investor_action: string; existing_holder_action: string; current_short_action: string; current_long_action: string;
  short_entry_low: number | null; short_entry_high: number | null; short_target_1: number | null;
  short_target_2: number | null; short_invalidation: number | null; long_entry_low: number | null;
  long_entry_high: number | null; long_fair_value: number | null; long_target: number | null; long_invalidation: number | null;
  top_positive_reasons: string[]; top_negative_reasons: string[]; data_state: string;
  missing_areas: string[]; stale_areas: string[]; short_horizon: string; long_horizon: string;
  short_term_state: string; long_term_state: string; lifecycle_reasons?: string[]; evaluation_status?: string;
};
export type Radar = { generated_at: string | null; best_buy_today: Opportunity | null;
  top_short_term: Opportunity[]; top_long_term: Opportunity[]; previous_recommendations: Opportunity[] };

export function price(value: number | null | undefined): string {
  return value == null ? "Insufficient evidence" : `₹${value.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}
function range(low: number | null, high: number | null): string {
  return low == null || high == null ? "Insufficient evidence" : `${price(low)} – ${price(high)}`;
}
function label(value: string | undefined) { return value?.replaceAll("_", " ") ?? "Unavailable"; }

export function OpportunityCard({ item, horizon, held, watchlisted }: {
  item: Opportunity; horizon: "short" | "long"; held: boolean; watchlisted: boolean;
}) {
  return <article className="opportunity-card">
    <h4>{item.company_name ?? item.symbol} <small>{item.symbol}</small></h4>
    <p>{price(item.current_price)} {held ? " · Held" : ""}{watchlisted ? " · Watchlisted" : ""}</p>
    <p>Opportunity {item.opportunity_score?.toFixed(1) ?? "Unavailable"} · Confidence {item.opportunity_confidence.toFixed(1)}% · Coverage {item.score_coverage.toFixed(1)}%</p>
    <p>New investor: {label(item.new_investor_action)}<br />Existing holder: {label(item.existing_holder_action)}</p>
    <strong>{label(horizon === "short" ? item.current_short_action : item.current_long_action)}</strong>
    <dl>{horizon === "short" ? <>
      <dt>Entry range</dt><dd>{range(item.short_entry_low, item.short_entry_high)}</dd>
      <dt>Target 1</dt><dd>{price(item.short_target_1)}</dd><dt>Target 2</dt><dd>{price(item.short_target_2)}</dd>
      <dt>Invalidation</dt><dd>{price(item.short_invalidation)}</dd><dt>Horizon</dt><dd>{item.short_horizon}</dd>
    </> : <>
      <dt>Accumulation range</dt><dd>{range(item.long_entry_low, item.long_entry_high)}</dd>
      <dt>Fair value</dt><dd>{price(item.long_fair_value)}</dd><dt>Target</dt><dd>{price(item.long_target)}</dd>
      <dt>Invalidation</dt><dd>{price(item.long_invalidation)}</dd><dt>Horizon</dt><dd>{item.long_horizon}</dd>
    </>}</dl>
    <p><strong>Why</strong></p><ul>{item.top_positive_reasons.slice(0, 4).map(r => <li key={r}>{label(r)}</li>)}</ul>
    <p><strong>Risks</strong></p><ul>{item.top_negative_reasons.slice(0, 3).map(r => <li key={r}>{label(r)}</li>)}</ul>
    <details><summary>Data: {label(item.data_state)}</summary>
      <p>Missing: {item.missing_areas.join(", ") || "None reported"}</p><p>Stale: {item.stale_areas.join(", ") || "None reported"}</p>
    </details>
  </article>;
}

export function RadarContent({ data, heldIds = [], watchlistedIds = [] }: { data: Radar; heldIds?: string[]; watchlistedIds?: string[] }) {
  const card = (item: Opportunity, horizon: "short" | "long") => <OpportunityCard key={item.global_instrument_id} item={item} horizon={horizon}
    held={heldIds.includes(item.global_instrument_id)} watchlisted={watchlistedIds.includes(item.global_instrument_id)} />;
  return <>
    <p>{data.generated_at ? `Last cycle: ${new Date(data.generated_at).toLocaleString()}` : "No persisted opportunity cycle yet."}</p>
    <h3>BEST BUY TODAY</h3>
    {data.best_buy_today ? <p>{data.best_buy_today.company_name ?? data.best_buy_today.symbol} · {price(data.best_buy_today.current_price)} · {label(data.best_buy_today.new_investor_action)}</p> : <p>No qualifying buy candidate.</p>}
    <h3>TOP SHORT-TERM OPPORTUNITIES</h3>
    <div className="opportunity-cards">{data.top_short_term.map(item => card(item, "short"))}</div>
    {!data.top_short_term.length && <p>No qualifying short-term opportunities.</p>}
    <h3>TOP LONG-TERM OPPORTUNITIES</h3>
    <div className="opportunity-cards">{data.top_long_term.map(item => card(item, "long"))}</div>
    {!data.top_long_term.length && <p>No qualifying long-term opportunities.</p>}
    <h3>PREVIOUS RECOMMENDATIONS</h3>
    {!data.previous_recommendations.length ? <p>No previous recommendations.</p> : <ul>{data.previous_recommendations.map(item =>
      <li key={item.global_instrument_id}>{item.company_name ?? item.symbol} · Short: {label(item.current_short_action)} ({label(item.short_term_state)}) · Long: {label(item.current_long_action)} ({label(item.long_term_state)})
        <p>{item.lifecycle_reasons?.map(label).join(" · ")}{item.evaluation_status ? ` · ${label(item.evaluation_status)}` : ""}</p>
      </li>)}</ul>}
  </>;
}

export function OpportunityRadar({ heldIds, watchlistedIds }: { heldIds: string[]; watchlistedIds: string[] }) {
  const [data, setData] = useState<Radar | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    let active = true;
    request<Radar>("/api/v1/research/opportunities/current").then(value => { if (active) setData(value); }).catch(() => { if (active) setError(true); });
    return () => { active = false; };
  }, []);
  return <section className="opportunity-radar" aria-label="Global opportunity radar">
    <h2>GLOBAL OPPORTUNITY RADAR</h2>
    {error ? <p role="alert">Opportunity radar unavailable.</p> : data ? <RadarContent data={data} heldIds={heldIds} watchlistedIds={watchlistedIds} /> : <p>Loading persisted opportunities…</p>}
  </section>;
}
