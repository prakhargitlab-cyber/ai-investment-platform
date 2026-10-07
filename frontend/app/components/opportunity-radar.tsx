"use client";

import { useEffect, useState } from "react";
import { request } from "../lib/portfolio-api";
import { investorLabel } from "../lib/investor-labels";

export { investorLabel } from "../lib/investor-labels";

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

export const INITIAL_DISPLAY = 4;

export type Radar = {
  generated_at: string | null;
  best_buy_today: Opportunity | null;
  top_short_term: Opportunity[];
  top_long_term: Opportunity[];
  top_exit?: Opportunity[];
  top_short_term_count?: number;
  top_long_term_count?: number;
  top_exit_count?: number;
  previous_recommendations?: Opportunity[];
};

/**
 * Normalize a Radar response so a persistence backend that legitimately omits
 * `previous_recommendations` (or the new count fields) cannot crash rendering.
 *
 * Root cause (traced from the browser TypeError "Cannot read properties of
 * undefined (reading 'length')" at RadarContent's
 * `!data.previous_recommendations.length` below): GET
 * /api/v1/research/opportunities/current (ai/research-engine/app/main.py
 * `opportunity_radar`) returns `repository.persistence.global_opportunity_radar()`
 * verbatim, with no response_model to enforce the shape. Both
 * `OpportunityPersistenceMixin.global_opportunity_radar()`
 * (ai/research-engine/app/opportunity_persistence.py) and
 * `DisabledResearchPersistence.global_opportunity_radar()`
 * (ai/research-engine/app/persistence.py) build their return dict without a
 * `previous_recommendations` key at all, so the frontend never receives it --
 * every response to this endpoint omits the field, not just a degraded one.
 * research-engine is out of scope for this fix, so the boundary is
 * normalized here, matching the existing normalizeCompany /
 * normalizeResearchSummary pattern in lib/portfolio-api.ts.
 */
export function normalizeRadar(raw: Radar): Radar {
  const top_short_term = raw.top_short_term ?? [];
  const top_long_term = raw.top_long_term ?? [];
  const top_exit = raw.top_exit ?? [];
  return {
    ...raw,
    top_short_term,
    top_long_term,
    top_exit,
    previous_recommendations: Array.isArray(raw.previous_recommendations) ? raw.previous_recommendations : [],
    top_short_term_count: typeof raw.top_short_term_count === "number" ? raw.top_short_term_count : top_short_term.length,
    top_long_term_count: typeof raw.top_long_term_count === "number" ? raw.top_long_term_count : top_long_term.length,
    top_exit_count: typeof raw.top_exit_count === "number" ? raw.top_exit_count : top_exit.length,
  };
}

export function price(value: number | null | undefined): string {
  return value == null ? "Insufficient evidence" : `₹${value.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}
function range(low: number | null, high: number | null): string {
  return low == null || high == null ? "Insufficient evidence" : `${price(low)} – ${price(high)}`;
}

function compactPrice(value: number | null | undefined): string {
  return value == null ? "—" : price(value);
}

function compactRange(low: number | null, high: number | null): string {
  return low == null || high == null ? "—" : range(low, high);
}

export function OpportunityCard({ item, horizon, held, watchlisted }: {
  item: Opportunity; horizon: "short" | "long"; held: boolean; watchlisted: boolean;
}) {
  const action = horizon === "short" ? item.current_short_action : item.current_long_action;
  const entryRange = horizon === "short"
    ? compactRange(item.short_entry_low, item.short_entry_high)
    : compactRange(item.long_entry_low, item.long_entry_high);
  const target = horizon === "short" ? compactPrice(item.short_target_1) : compactPrice(item.long_target);
  const timeHorizon = horizon === "short" ? item.short_horizon : item.long_horizon;
  const missingPriceLevels = horizon === "short"
    ? [item.current_price, item.short_entry_low, item.short_entry_high, item.short_target_1].some((value) => value == null)
    : [item.current_price, item.long_entry_low, item.long_entry_high, item.long_target].some((value) => value == null);
  return <article className="opportunity-card">
    <header className="opportunity-card-header">
      <div><h4>{item.company_name ?? item.symbol}</h4><small>{item.symbol}</small></div>
      <div className="opportunity-card-flags">{held ? <span>Held</span> : null}{watchlisted ? <span>Watchlisted</span> : null}</div>
    </header>
    <p className="opportunity-action">{investorLabel(action)}</p>
    <dl className="opportunity-primary-metrics">
      <div><dt>Current price</dt><dd>{compactPrice(item.current_price)}</dd></div>
      <div><dt>{horizon === "short" ? "Entry range" : "Accumulation range"}</dt><dd>{entryRange}</dd></div>
      <div><dt>Target</dt><dd>{target}</dd></div>
      <div><dt>Horizon</dt><dd>{timeHorizon}</dd></div>
    </dl>
    <p className="opportunity-quality">
      Opportunity {item.opportunity_score?.toFixed(1) ?? "Unavailable"} · Confidence {item.opportunity_confidence.toFixed(1)}% · Evidence quality {item.score_coverage.toFixed(1)}%
      {missingPriceLevels ? " · Insufficient evidence for some price levels" : ""}
    </p>
    <p className="opportunity-investor-actions">New investor: {investorLabel(item.new_investor_action)} · Existing holder: {investorLabel(item.existing_holder_action)}</p>
    <div className="opportunity-rationale">
      <section><h5>Why</h5>{item.top_positive_reasons.length ? <ul>{item.top_positive_reasons.slice(0, 4).map(r => <li key={r}>{investorLabel(r)}</li>)}</ul> : <p>No verified positive factor.</p>}</section>
      <section><h5>Risks</h5>{item.top_negative_reasons.length ? <ul>{item.top_negative_reasons.slice(0, 3).map(r => <li key={r}>{investorLabel(r)}</li>)}</ul> : <p>No specific risk factor reported.</p>}</section>
    </div>
    <details><summary>Evidence details · {investorLabel(item.data_state)}</summary>
      {horizon === "short" ? <p>Second target: {compactPrice(item.short_target_2)} · Invalidation: {compactPrice(item.short_invalidation)}</p>
        : <p>Fair value: {compactPrice(item.long_fair_value)} · Invalidation: {compactPrice(item.long_invalidation)}</p>}
      <p>Missing: {item.missing_areas.map(investorLabel).join(", ") || "None reported"}</p><p>Stale: {item.stale_areas.map(investorLabel).join(", ") || "None reported"}</p>
    </details>
  </article>;
}

/**
 * Render a section of opportunity cards with a "More / Show less" affordance.
 *
 * Shows the first `INITIAL_DISPLAY` cards initially. When more than
 * `INITIAL_DISPLAY` cards exist, a "More" button reveals the next batch
 * (next `INITIAL_DISPLAY` per click). A "Show less" button collapses back to
 * the initial window. Expansion state is managed internally so that
 * short-term and long-term sections are independent.
 */
function ExpandableSection({ title, items, horizon, heldIds, watchlistedIds }: {
  title: string; items: Opportunity[]; horizon: "short" | "long";
  heldIds: string[]; watchlistedIds: string[];
}) {
  const [visible, setVisible] = useState(INITIAL_DISPLAY);
  const expanded = items.length > visible;
  const card = (item: Opportunity) => <OpportunityCard key={item.global_instrument_id} item={item} horizon={horizon}
    held={heldIds.includes(item.global_instrument_id)} watchlisted={watchlistedIds.includes(item.global_instrument_id)} />;
  return (
    <>
      <h3>{title}</h3>
      <div className="opportunity-cards">{items.slice(0, visible).map(card)}</div>
      {!items.length && <p>No qualifying {title === "Top short-term opportunities" ? "short-term" : "long-term"} opportunities.</p>}
      {items.length > INITIAL_DISPLAY && expanded && (
        <button type="button" className="opportunity-show-more"
          onClick={() => setVisible(v => v + INITIAL_DISPLAY)}>
          More
        </button>
      )}
      {items.length > INITIAL_DISPLAY && !expanded && (
        <button type="button" className="opportunity-show-less" onClick={() => setVisible(INITIAL_DISPLAY)}>
          Show less
        </button>
      )}
    </>
  );
}

/**
 * Paginated list of previous recommendations.
 *
 * Renders at most PAGE_SIZE items at a time with deterministic ordering
 * (by global_instrument_id), previous/next controls, and disabled boundary
 * states. The page is reset to 1 whenever the underlying item list changes
 * identity (same-length different-content or different length).
 */
const RECOMMENDATIONS_PAGE_SIZE = 10;

export function PreviousRecommendationsList({ items }: { items: Opportunity[] }) {
  const [page, setPage] = useState(1);
  const total = items.length;
  // Deterministic ordering: sort by global_instrument_id so page boundaries
  // are stable across re-renders and refreshes.
  const sorted = [...items].sort((a, b) =>
    a.global_instrument_id.localeCompare(b.global_instrument_id)
  );
  const start = (page - 1) * RECOMMENDATIONS_PAGE_SIZE;
  const end = start + RECOMMENDATIONS_PAGE_SIZE;
  const visible = sorted.slice(start, end);
  const hasNext = end < total;
  const hasPrev = page > 1;

  // Reset to page 1 when the list identity (length or content) changes.
  const listKey = `${total}:${items.map(i => i.global_instrument_id).join(",")}`;
  useEffect(() => {
    if (page !== 1) setPage(1);
  }, [listKey]);

  return (
    <>
      <ul>{visible.map(item =>
        <li key={item.global_instrument_id}>{item.company_name ?? item.symbol} · Short: {investorLabel(item.current_short_action)} ({investorLabel(item.short_term_state)}) · Long: {investorLabel(item.current_long_action)} ({investorLabel(item.long_term_state)})
          <p>{item.lifecycle_reasons?.map(investorLabel).join(" · ")}{item.evaluation_status ? ` · ${investorLabel(item.evaluation_status)}` : ""}</p>
        </li>
      )}</ul>
      <div className="pagination-controls" role="navigation" aria-label="Previous recommendations pages">
        <button type="button" className="pagination-prev"
          disabled={!hasPrev}
          onClick={() => setPage(p => Math.max(1, p - 1))}>
          Previous
        </button>
        <span aria-live="polite">Page {page} of {Math.ceil(total / RECOMMENDATIONS_PAGE_SIZE)}</span>
        <button type="button" className="pagination-next"
          disabled={!hasNext}
          onClick={() => setPage(p => p + 1)}>
          Next
        </button>
      </div>
    </>
  );
}

export function RadarContent({ data, heldIds = [], watchlistedIds = [] }: { data: Radar; heldIds?: string[]; watchlistedIds?: string[] }) {
  return <>
    <p>{data.generated_at ? `Last cycle: ${new Date(data.generated_at).toLocaleString()}` : "No persisted opportunity cycle yet."}</p>
    <h3>Best buy today</h3>
    {data.best_buy_today ? <p>{data.best_buy_today.company_name ?? data.best_buy_today.symbol} · {price(data.best_buy_today.current_price)} · {investorLabel(data.best_buy_today.new_investor_action)}</p> : <p>No qualifying buy candidate.</p>}
    <ExpandableSection
      title="Top short-term opportunities" items={data.top_short_term} horizon="short"
      heldIds={heldIds} watchlistedIds={watchlistedIds} />
    <ExpandableSection
      title="Top long-term opportunities" items={data.top_long_term} horizon="long"
      heldIds={heldIds} watchlistedIds={watchlistedIds} />
    <h3>Previous recommendations</h3>
    {!data.previous_recommendations || !data.previous_recommendations.length ? <p>No previous recommendations.</p> :
      <PreviousRecommendationsList items={data.previous_recommendations} />}
  </>;
}

export function OpportunityRadar({ heldIds, watchlistedIds }: { heldIds: string[]; watchlistedIds: string[] }) {
  const [data, setData] = useState<Radar | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    let active = true;
    request<Radar>("/api/v1/research/opportunities/current").then(normalizeRadar).then(value => { if (active) setData(value); }).catch(() => { if (active) setError(true); });
    return () => { active = false; };
  }, []);
  return <section className="opportunity-radar" aria-label="Global opportunity radar">
    <h2>Global opportunity radar</h2>
    {error ? <p role="alert">Opportunity radar unavailable.</p> : data ? <RadarContent data={data} heldIds={heldIds} watchlistedIds={watchlistedIds} /> : <p>Loading persisted opportunities…</p>}
  </section>;
}
