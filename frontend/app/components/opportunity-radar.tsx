"use client";

import { useEffect, useRef, useState } from "react";
import { request } from "../lib/portfolio-api";
import { investorLabel } from "../lib/investor-labels";
import { RadarProgressPanel, isTerminalRadarStatus, type RadarProgressCounter } from "./radar-progress";

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
  // Real evidence from app.sector_relative_strength.SectorRelativeStrengthEngine, spread
  // verbatim into every persisted/returned opportunity card (see
  // app/global_opportunity_cycle.py's `card = {**s, ...}`) -- optional here only because
  // older persisted snapshots may predate the feature, never fabricated when absent.
  sector?: string | null; sector_score?: number | null; sector_state?: string;
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
  const listKey = JSON.stringify(items.map(item => item.global_instrument_id).sort());
  return <PreviousRecommendationsPage key={listKey} items={items} />;
}

function PreviousRecommendationsPage({ items }: { items: Opportunity[] }) {
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  // Deterministic ordering: sort by global_instrument_id so page boundaries
  // are stable across re-renders and refreshes.
  const sorted = [...items].sort((a, b) =>
    a.global_instrument_id.localeCompare(b.global_instrument_id)
  );
  const filtered = search.trim()
    ? sorted.filter(item => (item.company_name ?? "").toLowerCase().includes(search.trim().toLowerCase())
        || (item.symbol ?? "").toLowerCase().includes(search.trim().toLowerCase()))
    : sorted;
  const total = filtered.length;
  const effectivePage = Math.min(page, Math.max(1, Math.ceil(total / RECOMMENDATIONS_PAGE_SIZE)));
  const start = (effectivePage - 1) * RECOMMENDATIONS_PAGE_SIZE;
  const end = start + RECOMMENDATIONS_PAGE_SIZE;
  const visible = filtered.slice(start, end);
  const hasNext = end < total;
  const hasPrev = effectivePage > 1;

  return (
    <>
      <input type="search" aria-label="Search previous recommendations by company or symbol" placeholder="Search company or symbol…"
        value={search} onChange={(e) => { setSearch(e.target.value); setPage(1); }} />
      <ul>{visible.map(item =>
        <li key={item.global_instrument_id}>{item.company_name ?? item.symbol} · Short: {investorLabel(item.current_short_action)} ({investorLabel(item.short_term_state)}) · Long: {investorLabel(item.current_long_action)} ({investorLabel(item.long_term_state)})
          <p>{item.lifecycle_reasons?.map(investorLabel).join(" · ")}{item.evaluation_status ? ` · ${investorLabel(item.evaluation_status)}` : ""}</p>
        </li>
      )}</ul>
      {!visible.length ? <p>No previous recommendations match this search.</p> : null}
      <div className="pagination-controls" role="navigation" aria-label="Previous recommendations pages">
        <button type="button" className="pagination-prev"
          disabled={!hasPrev}
          onClick={() => setPage(p => Math.max(1, p - 1))}>
          Previous
        </button>
        <span aria-live="polite">Page {effectivePage} of {Math.max(1, Math.ceil(total / RECOMMENDATIONS_PAGE_SIZE))}</span>
        <button type="button" className="pagination-next"
          disabled={!hasNext}
          onClick={() => setPage(p => p + 1)}>
          Next
        </button>
      </div>
    </>
  );
}

function equityFreshnessLabel(generatedAt: string): string {
  const ms = Date.now() - new Date(generatedAt).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "unknown";
  const minutes = Math.round(ms / 60000);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

type EquityTab = "overview" | "top" | "screener" | "sector" | "compare" | "quality";

const EQUITY_TABS: [EquityTab, string][] = [
  ["overview", "Overview"],
  ["top", "Top Opportunities"],
  ["screener", "Advanced Screener"],
  ["sector", "Sector Analysis"],
  ["compare", "Stock Comparison"],
  ["quality", "Data Quality"],
];

function EquitySectionTabs({ active, onChange }: { active: EquityTab; onChange: (tab: EquityTab) => void }) {
  return (
    <div className="radar-section-tabs" role="tablist" aria-label="Equity radar sections">
      {EQUITY_TABS.map(([id, label]) => (
        <button key={id} type="button" role="tab" aria-selected={active === id}
          className={active === id ? "radar-section-tab radar-section-tab-active" : "radar-section-tab"}
          onClick={() => onChange(id)}>
          {label}
        </button>
      ))}
    </div>
  );
}

/** Short+long+previous, deduped by instrument id -- the full known universe for this cycle. */
function dedupeOpportunities(data: Radar): Opportunity[] {
  const byId = new Map<string, Opportunity>();
  for (const candidate of [...data.top_short_term, ...data.top_long_term, ...(data.previous_recommendations ?? [])]) {
    if (!byId.has(candidate.global_instrument_id)) byId.set(candidate.global_instrument_id, candidate);
  }
  return Array.from(byId.values());
}

const COMPARE_METRIC_ROWS: [string, (o: Opportunity) => string][] = [
  ["Current price", (o) => compactPrice(o.current_price)],
  ["Opportunity score", (o) => (o.opportunity_score != null ? o.opportunity_score.toFixed(1) : "Unavailable")],
  ["Confidence", (o) => `${o.opportunity_confidence.toFixed(1)}%`],
  ["Evidence quality", (o) => `${o.score_coverage.toFixed(1)}%`],
  ["New investor action", (o) => investorLabel(o.new_investor_action)],
  ["Existing holder action", (o) => investorLabel(o.existing_holder_action)],
  ["Short-term action", (o) => investorLabel(o.current_short_action)],
  ["Long-term action", (o) => investorLabel(o.current_long_action)],
  ["Sector", (o) => o.sector ?? "—"],
  ["Data state", (o) => investorLabel(o.data_state)],
  ["Missing evidence", (o) => (o.missing_areas.length ? o.missing_areas.map(investorLabel).join(", ") : "None reported")],
];

export function RadarContent({ data, heldIds = [], watchlistedIds = [], initialTab }: { data: Radar; heldIds?: string[]; watchlistedIds?: string[]; initialTab?: EquityTab }) {
  const [tab, setTab] = useState<EquityTab>(initialTab ?? "overview");
  const [search, setSearch] = useState("");
  const [compareIds, setCompareIds] = useState<string[]>([]);
  const toggleCompare = (id: string) => setCompareIds((prev) => prev.includes(id) ? prev.filter((x) => x !== id) : prev.length < 6 ? [...prev, id] : prev);
  type EquitySortKey = "company" | "price" | "score" | "confidence";
  const [sortKey, setSortKey] = useState<EquitySortKey>("score");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const toggleSort = (key: EquitySortKey) => {
    if (sortKey === key) { setSortDir((d) => (d === "asc" ? "desc" : "asc")); }
    else { setSortKey(key); setSortDir(key === "company" ? "asc" : "desc"); }
  };
  const sortArrow = (key: EquitySortKey) => (sortKey !== key ? "" : sortDir === "asc" ? " ▲" : " ▼");
  const sortAria = (key: EquitySortKey): "none" | "ascending" | "descending" => (sortKey !== key ? "none" : sortDir === "asc" ? "ascending" : "descending");

  const universe = dedupeOpportunities(data);
  const bySector = new Map<string, Opportunity[]>();
  for (const candidate of universe) {
    const key = candidate.sector || "Unclassified / unavailable";
    const existing = bySector.get(key);
    if (existing) existing.push(candidate); else bySector.set(key, [candidate]);
  }
  const sectorRows = Array.from(bySector.entries()).sort((a, b) => a[0].localeCompare(b[0]));
  const haveSectorEvidence = sectorRows.some(([key]) => key !== "Unclassified / unavailable");
  const filteredScreener = universe.filter(o => !search.trim()
    || (o.company_name ?? "").toLowerCase().includes(search.trim().toLowerCase())
    || (o.symbol ?? "").toLowerCase().includes(search.trim().toLowerCase()))
    .sort((a, b) => {
      const dirMul = sortDir === "asc" ? 1 : -1;
      if (sortKey === "company") return dirMul * (a.company_name ?? a.symbol ?? "").localeCompare(b.company_name ?? b.symbol ?? "");
      if (sortKey === "price") return dirMul * ((a.current_price ?? -1) - (b.current_price ?? -1));
      if (sortKey === "confidence") return dirMul * (a.opportunity_confidence - b.opportunity_confidence);
      return dirMul * ((a.opportunity_score ?? -1) - (b.opportunity_score ?? -1));
    });
  const compared = universe.filter(o => compareIds.includes(o.global_instrument_id));
  const dataStateCounts = (["FRESH", "PARTIAL", "STALE"] as const).map(state => [state, universe.filter(o => o.data_state === state).length] as const);
  const missingAreaCounts = new Map<string, number>();
  for (const candidate of universe) for (const area of candidate.missing_areas) missingAreaCounts.set(area, (missingAreaCounts.get(area) ?? 0) + 1);
  const missingAreaRows = Array.from(missingAreaCounts.entries()).sort((a, b) => b[1] - a[1]);

  return <>
    <header className="etf-radar-header">
      <p>{data.generated_at
        ? `Previous completed cycle: ${new Date(data.generated_at).toLocaleString()} (${equityFreshnessLabel(data.generated_at)})`
        : "No persisted opportunity cycle yet."}</p>
    </header>
    <EquitySectionTabs active={tab} onChange={setTab} />

    {tab === "overview" ? (<div className="radar-tab-panel">
      <dl className="opportunity-primary-metrics radar-summary-tiles">
        <div><dt>Short-term candidates</dt><dd>{data.top_short_term.length}</dd></div>
        <div><dt>Long-term candidates</dt><dd>{data.top_long_term.length}</dd></div>
        <div><dt>Previous recommendations</dt><dd>{data.previous_recommendations?.length ?? 0}</dd></div>
      </dl>
      <h3>Leading buy candidate from that cycle</h3>
      {data.best_buy_today ? <p>{data.best_buy_today.company_name ?? data.best_buy_today.symbol} · {price(data.best_buy_today.current_price)} · {investorLabel(data.best_buy_today.new_investor_action)}</p> : <p>No qualifying buy candidate.</p>}
    </div>) : null}

    {tab === "top" ? (<div className="radar-tab-panel">
      <ExpandableSection
        title="Top short-term opportunities" items={data.top_short_term} horizon="short"
        heldIds={heldIds} watchlistedIds={watchlistedIds} />
      <ExpandableSection
        title="Top long-term opportunities" items={data.top_long_term} horizon="long"
        heldIds={heldIds} watchlistedIds={watchlistedIds} />
    </div>) : null}

    {tab === "screener" ? (<div className="radar-tab-panel">
      <h3>Advanced screener</h3>
      {universe.length ? <>
        <div className="etf-radar-controls radar-filter-rail" role="search">
          <input type="search" aria-label="Search opportunities by company or symbol" placeholder="Search company or symbol…"
            value={search} onChange={(e) => setSearch(e.target.value)} />
          <span className="radar-result-count">{filteredScreener.length} of {universe.length} result{universe.length === 1 ? "" : "s"}</span>
        </div>
        <div className="radar-table-scroll">
          <table className="radar-dense-table radar-screener-table">
            <thead><tr>
              <th scope="col">Compare</th>
              <th aria-sort={sortAria("company")}><button type="button" className="radar-sort-button" onClick={() => toggleSort("company")}>Company{sortArrow("company")}</button></th>
              <th aria-sort={sortAria("price")}><button type="button" className="radar-sort-button" onClick={() => toggleSort("price")}>Price{sortArrow("price")}</button></th>
              <th aria-sort={sortAria("score")}><button type="button" className="radar-sort-button" onClick={() => toggleSort("score")}>Score{sortArrow("score")}</button></th>
              <th aria-sort={sortAria("confidence")}><button type="button" className="radar-sort-button" onClick={() => toggleSort("confidence")}>Confidence{sortArrow("confidence")}</button></th>
              <th scope="col">New investor</th>
              <th scope="col">Short-term</th>
              <th scope="col">Long-term</th>
            </tr></thead>
            <tbody>
              {filteredScreener.map((o) => (
                <tr key={o.global_instrument_id}>
                  <td><input type="checkbox" aria-label={`Compare ${o.company_name ?? o.symbol}`} checked={compareIds.includes(o.global_instrument_id)} onChange={() => toggleCompare(o.global_instrument_id)} /></td>
                  <td>{o.company_name ?? o.symbol}</td>
                  <td>{compactPrice(o.current_price)}</td>
                  <td>{o.opportunity_score != null ? o.opportunity_score.toFixed(1) : "—"}</td>
                  <td>{o.opportunity_confidence.toFixed(1)}%</td>
                  <td>{investorLabel(o.new_investor_action)}</td>
                  <td>{investorLabel(o.current_short_action)}</td>
                  <td>{investorLabel(o.current_long_action)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!filteredScreener.length ? <p>No opportunities match this search.</p> : null}
      </> : <p>No opportunities are available to screen yet.</p>}
      <h3>Previous recommendations</h3>
      {!data.previous_recommendations || !data.previous_recommendations.length ? <p>No previous recommendations.</p> :
        <PreviousRecommendationsList items={data.previous_recommendations} />}
    </div>) : null}

    {tab === "sector" ? (<div className="radar-tab-panel">
      <h3>Sector analysis</h3>
      {haveSectorEvidence ? (
        <div className="radar-table-scroll">
          <table className="radar-dense-table">
            <thead><tr><th>Sector</th><th>Candidates</th><th>Representative state</th><th>Relative strength</th></tr></thead>
            <tbody>
              {sectorRows.map(([sector, items]) => {
                const withScore = items.find(o => o.sector_score != null) ?? items[0];
                return (
                  <tr key={sector}>
                    <td>{sector}</td>
                    <td>{items.length}</td>
                    <td>{withScore?.sector_state ?? "—"}</td>
                    <td>{withScore?.sector_score != null ? withScore.sector_score.toFixed(1) : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : <p>Sector evidence is not available for the candidates in this cycle.</p>}
    </div>) : null}

    {tab === "compare" ? (<div className="radar-tab-panel">
      <h3>Stock comparison</h3>
      {compared.length ? (
        <div className="radar-table-scroll">
          <table className="radar-dense-table radar-compare-table">
            <thead><tr><th>Metric</th>{compared.map(o => <th key={o.global_instrument_id}>{o.company_name ?? o.symbol}</th>)}</tr></thead>
            <tbody>
              {COMPARE_METRIC_ROWS.map(([label, accessor]) => (
                <tr key={label}><td>{label}</td>{compared.map(o => <td key={o.global_instrument_id + label}>{accessor(o)}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <p>Select stocks in the Advanced Screener tab using the Compare checkbox to see them side-by-side here.</p>}
    </div>) : null}

    {tab === "quality" ? (<div className="radar-tab-panel">
      <h3>Data quality &amp; diagnostics</h3>
      <dl aria-label="Data state breakdown">
        {dataStateCounts.map(([state, count]) => <div key={state}><dt>{investorLabel(state)}</dt><dd>{count}</dd></div>)}
      </dl>
      {missingAreaRows.length ? <>
        <p>Most common missing evidence areas</p>
        <ul>{missingAreaRows.map(([area, count]) => <li key={area}>{investorLabel(area)}: {count}</li>)}</ul>
      </> : <p>No missing evidence areas reported.</p>}
    </div>) : null}
  </>;
}

type EquityAuthoritativeProgress = {
  phase?: string; baseline_evaluated?: number; universe_count?: number; deep_attempted?: number;
  deep_completed?: number; deep_denominator?: number;
  /** Readiness outcomes are separate from terminal work completion. */
  deep_ready?: number; deep_readiness_failed?: number; deep_technical_failure?: number;
  ready?: number; failed?: number; technical?: number; eligible?: number;
  // `technical` is EVERY current RETRYABLE_FAILURE row in the admitted DEEP
  // set, exhausted or not; `deep_technical_failure` is only the exhausted
  // subset (terminal, no further retry will happen). A cycle freshly admitted
  // to DEEP can genuinely have `technical` > 0 while `deep_technical_failure`
  // is still 0 -- every retryable row is still within its repair budget. This
  // is NOT a stale snapshot or an API mapping defect; both fields were already
  // correct, just not both consumed by the frontend (see deepAwaitingRetry
  // below).
  /**
   * analysis_scope/requested_shortlist_limit/shortlist_limit_applied
   * disambiguate deep_denominator from the shortlist_limit a caller
   * requested: a FULL-scope cycle deliberately admits the whole
   * eligible universe to deep investigation (deep_denominator can
   * legitimately equal universe_count), so shortlist_limit_applied
   * tells the UI whether the requested cap actually bounded this
   * cycle's admission, rather than the UI inferring it from the
   * admitted count alone.
   */
  analysis_scope?: string | null; requested_shortlist_limit?: number | null;
  shortlist_limit_applied?: boolean | null;
  // Already present on every authoritative_progress payload the backend
  // sends (app.cycle_checkpoint's cycle_status_snapshot spreads its whole
  // dict verbatim into the response -- see opportunity_persistence.py's
  // job["authoritative_progress"] = snapshot), just not previously
  // declared here. baseline_completed is the live, durable-row-derived
  // BASELINE completion count (distinct from baseline_evaluated, a static
  // admitted/scheduled count set once at admission time); baseline_failed
  // is the subset of baseline_completed whose own payload did not record
  // readiness. No new endpoint or backend change needed for these.
  baseline_completed?: number; baseline_failed?: number;
  // BASELINE state breakdown (Equity Radar progress-accounting follow-up,
  // Part 2): baseline_completed/baseline_failed above are both terminal-only
  // (CandidateState in FINAL_STATES, or RETRYABLE_FAILURE with attempts
  // exhausted). A BASELINE_INCOMPLETE row that has NOT yet exhausted its
  // repair budget is neither "completed" nor "failed" by those definitions,
  // so it needs its own explicit bucket -- baseline_awaiting_retry -- rather
  // than silently falling into an inferred "in progress" catch-all on the
  // frontend (the root cause of the "607 shown as IN PROGRESS" defect: those
  // rows genuinely are RETRYABLE_FAILURE, just not yet exhausted, so neither
  // baseline_completed nor baseline_failed counted them). baseline_in_progress
  // (state=IN_PROGRESS right now) and baseline_not_started (no BASELINE row at
  // all yet) complete the exact, non-overlapping partition of universe_count.
  // Same cycle_checkpoint.cycle_status_snapshot change as deep_technical_failure
  // vs technical below -- additive only, baseline_completed/baseline_failed keep
  // their exact prior values.
  baseline_in_progress?: number; baseline_awaiting_retry?: number; baseline_not_started?: number;
};

type EquityActiveCycle = {
  cycleId: string;
  status: string;
  startedAt: string | null;
  updatedAt: string | null;
  errorCode: string | null;
  progress: EquityAuthoritativeProgress | null;
  pollError: boolean;
};

type EquityCycleSummary = {
  cycle_id: string; status: string; created_at: string; updated_at: string;
  error_code?: string | null; analysis_scope?: string | null;
};
type EquityCycleHistory = { cycles: EquityCycleSummary[]; active_cycle: EquityCycleSummary | null };
type EquityCycleStatus = {
  status: string; updated_at?: string; error_code?: string | null;
  authoritative_progress?: EquityAuthoritativeProgress;
};

function equityCycle(summary: EquityCycleSummary): EquityActiveCycle {
  return { cycleId: summary.cycle_id, status: summary.status, startedAt: summary.created_at,
    updatedAt: summary.updated_at, errorCode: summary.error_code ?? null, progress: null, pollError: false };
}

// Active execution identity is internal; server discovery restores retained work.
const EQUITY_ACTIVE_CYCLE_STORAGE_KEY = "aip.opportunityRadar.activeCycleId";

function equityCounters(progress: EquityAuthoritativeProgress | null): RadarProgressCounter[] {
  // RADAR UI + API ROUTING closure, item C/D -- every field the spec asks
  // for is listed, even when this backend genuinely has no trustworthy
  // value for it: the panel renders "Unavailable" for a null value rather
  // than hiding the row (see radar-progress.tsx).
  //
  // Universe and phase counts come from the durable cycle selection.
  // Pod restart counts are not exposed by this API.
  // The current-results API has no cycle identity. Its action totals cannot
  // be attributed to this tracked cycle, even after a successful refetch.
  // Trimmed to ONLY the counters genuinely absent from EquityAnalysisPanels
  // (the 3-column Universe/Baseline/Deep area below, which already shows
  // baseline_evaluated/ready/failed and every deep_* count) -- this list
  // used to repeat nearly every one of those numbers a second time under a
  // slightly different label (confirmed duplicate-counters defect). What
  // remains here is either informational context not shown elsewhere
  // (shortlist_limit, the true deep_denominator admission count) or a
  // deliberate, tested "Unavailable" placeholder for an action-count total
  // this API genuinely does not expose per cycle -- never a fabricated
  // zero (see "Equity action counts remain unavailable..." test).
  return [
    { label: "Shortlist limit (requested)", value: progress?.requested_shortlist_limit ?? null },
    { label: "Deep admitted (actual)", value: progress?.deep_denominator ?? null },
    { label: "BUY count", value: null },
    { label: "Exit (Sell/Partial) count", value: null },
  ];
}

function equityPercent(progress: EquityAuthoritativeProgress | null): number | null {
  if (!progress || progress.phase !== "DEEP" || !progress.deep_denominator) return null;
  // Terminal outcomes measure work, including failures. Readiness and the
  // overall lifecycle are independent; 100% processed can still be RUNNING.
  if (progress.deep_completed == null) return null;
  return (progress.deep_completed / progress.deep_denominator) * 100;
}

// Admission-refusal reasons the server now distinguishes (app/main.py's
// opportunity_cycle endpoint / app/opportunity_worker.ManualEtfAdmissionError)
// -- each needs its own actionable explanation, not a single generic
// "unavailable" message (Workstream 2 item 7).
const EQUITY_ADMISSION_EXPLANATIONS: Record<string, string> = {
  RECOVERY_PAUSED: "Equity Radar's automatic scheduler is paused and manual execution is not authorized for this environment. An administrator can enable manual execution or resume the scheduler.",
  ETF_MANUAL_EXECUTION_DISABLED: "Manual execution is not authorized in this environment. An administrator must enable it for a controlled run here.",
  ETF_ACTIVE_CYCLE_REQUIRES_REVIEW: "A cycle is already in flight for this radar and must be reviewed or cancelled before a new one can start.",
  RECOMMENDATION_PERSISTENCE_REQUIRED: "This environment cannot persist cycle results, so the Equity Radar cannot run here.",
};

function equityAdmissionExplanation(failure: { status?: number; message?: string }): string {
  if (failure.status === 401) return "Sign in again to run the Equity Radar.";
  if (failure.status === 403) return "Administrator access is required to run the Equity Radar.";
  const known = failure.message ? EQUITY_ADMISSION_EXPLANATIONS[failure.message] : undefined;
  if (known) return known;
  if (failure.status === 503) return "The Equity Radar is temporarily unavailable (server-side failure). Try again shortly.";
  if (failure.status === 409) return failure.message ?? "The Equity Radar run conflicts with current state.";
  return "The Equity Radar run could not be started.";
}

type RadarDonutSegment = { label: string; value: number; color: string };

/**
 * Small local donut renderer (deliberately not shared with
 * investment-workspace.tsx's Donut, to avoid a cross-file import into that
 * already very large component). Renders exact proportions of whatever
 * segments the caller computed; never infers or fabricates a split itself.
 */
function RadarDonut({ segments, size = 128, thickness = 20 }: { segments: RadarDonutSegment[]; size?: number; thickness?: number }) {
  const total = segments.reduce((sum, segment) => sum + segment.value, 0);
  const radius = (size - thickness) / 2;
  const circumference = 2 * Math.PI * radius;
  let offsetAcc = 0;
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img" aria-label="Readiness donut chart">
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

function RadarDonutLegend({ segments }: { segments: RadarDonutSegment[] }) {
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
 * Ranked horizontal bars for a short, already-exclusive breakdown (e.g. deep
 * failure reasons). Widths are relative to the largest bar in the list, not
 * to a global total, so a single-reason breakdown still reads clearly.
 */
function RadarBarList({ bars }: { bars: { label: string; value: number; color: string }[] }) {
  const max = Math.max(1, ...bars.map((bar) => bar.value));
  return (
    <ul className="radar-bar-list">
      {bars.map((bar) => (
        <li key={bar.label} className="radar-bar-row">
          <span className="radar-bar-label">{bar.label}</span>
          <span className="radar-bar-track">
            <span className="radar-bar-fill" style={{ width: `${(bar.value / max) * 100}%`, background: bar.color }} />
          </span>
          <span className="radar-bar-value">{bar.value}</span>
        </li>
      ))}
    </ul>
  );
}

/**
 * The Image-1-style three-column Universe / Baseline / Deep analysis area,
 * plus a Deep-outcome donut, a failure-reason distribution, and a
 * publication/results status panel. Additive to (never a replacement for)
 * RadarProgressPanel's existing lifecycle timeline and flat counters list,
 * which stay exactly as they were (same tests, same denominators).
 *
 * Every number here is read straight off EquityAuthoritativeProgress --
 * nothing is derived except plain, verified-exclusive subtractions:
 *   - universe_count - baseline_completed = baseline incomplete
 *   - deep_denominator - deep_attempted = not yet attempted
 *   - deep_attempted - deep_completed = in progress
 *   - baseline_completed - baseline_failed = baseline succeeded
 * and deep_ready + deep_readiness_failed + deep_technical_failure ==
 * deep_completed exactly, by construction in
 * app.cycle_checkpoint.cycle_status_snapshot (each terminal DEEP row
 * increments exactly one of the three), so the Deep-outcome donut is a
 * genuine, non-overlapping partition -- never a combined/misleading one.
 */
function EquityAnalysisPanels({ progress, data }: { progress: EquityAuthoritativeProgress | null; data: Radar | null }) {
  const universeTotal = progress?.universe_count ?? null;
  const baselineReady = progress?.baseline_completed ?? null;
  const baselineFailed = progress?.baseline_failed ?? null;
  const baselineSucceeded = baselineReady != null && baselineFailed != null ? Math.max(baselineReady - baselineFailed, 0) : null;
  // Each read directly off its own authoritative backend field (never
  // inferred): a RETRYABLE_FAILURE row awaiting a bounded repair retry is
  // its own state, distinct from both "failed" (exhausted) and "in progress"
  // (claimed right now by a worker) and from "not yet scanned" (no BASELINE
  // row at all). This is exactly the previously-collapsed 607-row bucket.
  const baselineAwaitingRetry = progress?.baseline_awaiting_retry ?? null;
  const baselineActive = progress?.baseline_in_progress ?? null;
  const baselineNotStarted = progress?.baseline_not_started ?? null;

  const deepDenominator = progress?.deep_denominator ?? null;
  const deepAttempted = progress?.deep_attempted ?? null;
  const deepCompleted = progress?.deep_completed ?? null;
  const deepReady = progress?.deep_ready ?? null;
  const deepReadinessFailed = progress?.deep_readiness_failed ?? null;
  const deepTechnicalFailure = progress?.deep_technical_failure ?? null;
  // `technical` is every CURRENT RETRYABLE_FAILURE row (exhausted or not);
  // deep_technical_failure is only the exhausted subset. The difference is
  // exactly the awaiting-retry population that must not be counted as
  // "in progress" -- subtracting deep_technical_failure alone (the previous
  // local fix) left every not-yet-exhausted retry row miscounted as
  // in-flight, which is the exact defect this follow-up corrects.
  const deepTechnical = progress?.technical ?? null;
  const deepAwaitingRetry = deepTechnical != null && deepTechnicalFailure != null
    ? Math.max(deepTechnical - deepTechnicalFailure, 0) : null;
  const deepInProgress = deepAttempted != null && deepCompleted != null && deepAwaitingRetry != null
    ? Math.max(deepAttempted - deepCompleted - deepAwaitingRetry, 0) : null;
  const deepNotYetAttempted = deepDenominator != null && deepAttempted != null ? Math.max(deepDenominator - deepAttempted, 0) : null;

  const universeDonutSegments: RadarDonutSegment[] = [
    { label: "Baseline ready", value: baselineSucceeded ?? 0, color: "var(--positive)" },
    { label: "Awaiting retry", value: baselineAwaitingRetry ?? 0, color: "var(--warning)" },
    { label: "In progress", value: baselineActive ?? 0, color: "var(--info)" },
    { label: "Not yet scanned", value: baselineNotStarted ?? 0, color: "var(--line-strong)" }
  ];
  const deepDonutSegments: RadarDonutSegment[] = [
    { label: "Ready for ranking", value: deepReady ?? 0, color: "var(--positive)" },
    { label: "Evidence unavailable", value: deepReadinessFailed ?? 0, color: "var(--warning)" },
    { label: "Technical retry pending", value: deepAwaitingRetry ?? 0, color: "var(--warning)" },
    { label: "Technical failure", value: deepTechnicalFailure ?? 0, color: "var(--negative)" },
    { label: "In progress", value: deepInProgress ?? 0, color: "var(--info)" },
    { label: "Not yet attempted", value: deepNotYetAttempted ?? 0, color: "var(--line-strong)" }
  ];

  const publishedCount = (data?.top_short_term_count ?? 0) + (data?.top_long_term_count ?? 0);

  return (
    <div className="equity-analysis-panels">
      <div className="equity-analysis-panel" aria-label="Universe and readiness">
        <h3>Universe Progress</h3>
        <dl className="radar-progress-counters">
          <div><dt>Total universe</dt><dd>{universeTotal ?? "Unavailable"}</dd></div>
          <div><dt>Baseline ready</dt><dd>{baselineSucceeded ?? "Unavailable"}</dd></div>
          <div><dt>Awaiting retry</dt><dd>{baselineAwaitingRetry ?? "Unavailable"}</dd></div>
          <div><dt>In progress</dt><dd>{baselineActive ?? "Unavailable"}</dd></div>
          <div><dt>Not yet scanned</dt><dd>{baselineNotStarted ?? "Unavailable"}</dd></div>
        </dl>
        <div className="allocation-donut-body">
          <RadarDonut segments={universeDonutSegments} />
          <RadarDonutLegend segments={universeDonutSegments} />
        </div>
      </div>

      <div className="equity-analysis-panel" aria-label="Baseline analysis">
        <h3>Baseline Analysis</h3>
        <dl className="radar-progress-counters">
          <div><dt>Evaluated (admitted)</dt><dd>{progress?.baseline_evaluated ?? "Unavailable"}</dd></div>
          <div><dt>Ready</dt><dd>{baselineSucceeded ?? "Unavailable"}</dd></div>
          <div><dt>Failed (exhausted retries)</dt><dd>{baselineFailed ?? "Unavailable"}</dd></div>
          <div><dt>Awaiting retry</dt><dd>{baselineAwaitingRetry ?? "Unavailable"}</dd></div>
          <div><dt>In progress</dt><dd>{baselineActive ?? "Unavailable"}</dd></div>
          <div><dt>Not yet scanned</dt><dd>{baselineNotStarted ?? "Unavailable"}</dd></div>
        </dl>
      </div>

      <div className="equity-analysis-panel" aria-label="Deep analysis">
        <h3>Deep Analysis</h3>
        <dl className="radar-progress-counters">
          <div><dt>Eligible</dt><dd>{progress?.eligible ?? "Unavailable"}</dd></div>
          <div><dt>Attempted</dt><dd>{deepAttempted ?? "Unavailable"}</dd></div>
          <div><dt>Completed</dt><dd>{deepCompleted ?? "Unavailable"}</dd></div>
          <div><dt>In progress</dt><dd>{deepInProgress ?? "Unavailable"}</dd></div>
          <div><dt>Technical retry pending</dt><dd>{deepAwaitingRetry ?? "Unavailable"}</dd></div>
          <div><dt>Technical failures (exhausted)</dt><dd>{deepTechnicalFailure ?? "Unavailable"}</dd></div>
          <div><dt>Evidence unavailable</dt><dd>{deepReadinessFailed ?? "Unavailable"}</dd></div>
        </dl>
      </div>

      <div className="equity-analysis-panel" aria-label="Deep analysis outcomes">
        <h3>Deep Analysis Breakdown</h3>
        <div className="allocation-donut-body">
          <RadarDonut segments={deepDonutSegments} />
          <RadarDonutLegend segments={deepDonutSegments} />
        </div>
        <p className="allocation-methodology">
          A genuine partition of {deepDenominator ?? "the admitted"} admitted instruments: every admitted instrument is in
          exactly one of these buckets, by construction in the backend checkpoint, never a combined or double-counted figure.
        </p>
      </div>

      <div className="equity-analysis-panel" aria-label="Failure reasons">
        <h3>Failure Reasons (Deep)</h3>
        {deepReadinessFailed == null && deepTechnicalFailure == null ? (
          <p className="broker-note">No failure data available for this cycle yet.</p>
        ) : (
          <RadarBarList
            bars={[
              { label: "Evidence unavailable", value: deepReadinessFailed ?? 0, color: "var(--warning)" },
              { label: "Technical failure", value: deepTechnicalFailure ?? 0, color: "var(--negative)" }
            ]}
          />
        )}
      </div>

      <div className="equity-analysis-panel" aria-label="Research results and publication status">
        <h3>Research Results</h3>
        <dl className="radar-progress-counters">
          <div><dt>Ready for ranking</dt><dd>{deepReady ?? "Unavailable"}</dd></div>
          <div><dt>Published opportunities</dt><dd>{data ? publishedCount : "Unavailable"}</dd></div>
          <div><dt>Last published</dt><dd>{data?.generated_at ? new Date(data.generated_at).toLocaleString() : "No persisted cycle"}</dd></div>
        </dl>
      </div>
    </div>
  );
}

export function OpportunityRadar({ heldIds, watchlistedIds, roles = [] }: { heldIds: string[]; watchlistedIds: string[]; roles?: string[] }) {
  const [data, setData] = useState<Radar | null>(null);
  const [error, setError] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const [cancelError, setCancelError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const [activeCycle, setActiveCycle] = useState<EquityActiveCycle | null>(null);
  const [progressCycle, setProgressCycle] = useState<EquityActiveCycle | null>(null);
  const [discoveryError, setDiscoveryError] = useState(false);
  const [discoveryLoading, setDiscoveryLoading] = useState(true);
  const [discoveryVersion, setDiscoveryVersion] = useState(0);
  const [boundedSubmitting, setBoundedSubmitting] = useState(false);
  const boundedRequestPending = useRef(false);
  useEffect(() => {
    let active = true;
    const restore = async () => {
      setDiscoveryLoading(true);
      try {
        const history = await request<EquityCycleHistory>("/api/v1/research/opportunities/cycles?limit=1");
        if (!active) return;
        setActiveCycle(history.active_cycle ? equityCycle(history.active_cycle) : null);
        const summary = history.active_cycle ?? history.cycles.find(value => isTerminalRadarStatus(value.status));
        setProgressCycle(summary ? equityCycle(summary) : null);
        setDiscoveryError(false);
      } catch {
        if (!active) return;
        setDiscoveryError(true);
        // A failed discovery must not enable Run over a known active execution.
        let storedCycleId: string | null = null;
        try { storedCycleId = window.sessionStorage.getItem(EQUITY_ACTIVE_CYCLE_STORAGE_KEY); } catch { /* ignore */ }
        if (storedCycleId) {
          const restored = { cycleId: storedCycleId, status: "UNKNOWN", startedAt: null,
            updatedAt: null, errorCode: null, progress: null, pollError: false };
          setActiveCycle(previous => previous ?? restored);
          setProgressCycle(previous => previous ?? restored);
        }
      } finally {
        if (active) setDiscoveryLoading(false);
      }
    };
    void restore();
    return () => { active = false; };
  }, [discoveryVersion]);
  const cycleId = progressCycle?.cycleId;

  useEffect(() => {
    let active = true;
    request<Radar>("/api/v1/research/opportunities/current").then(normalizeRadar).then(value => { if (active) setData(value); }).catch(() => { if (active) setError(true); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (discoveryLoading || !cycleId) return;
    let active = true;
    let pollTimer: ReturnType<typeof setTimeout> | null = null;
    const poll = async () => {
      const id = cycleId;
      let terminal = false;
      try {
        const value = await request<EquityCycleStatus>(
          `/api/v1/research/opportunities/cycles/${encodeURIComponent(id)}/status`
        );
        if (!active) return;
        const update = (previous: EquityActiveCycle | null) => previous && previous.cycleId === id
          ? { ...previous, status: value.status, updatedAt: value.updated_at ?? previous.updatedAt,
              errorCode: value.error_code ?? null,
              progress: value.authoritative_progress ?? previous.progress, pollError: false }
          : previous;
        setProgressCycle(update);
        if (isTerminalRadarStatus(value.status)) {
          terminal = true;
          setActiveCycle(previous => previous?.cycleId === id ? null : previous);
          try {
            if (window.sessionStorage.getItem(EQUITY_ACTIVE_CYCLE_STORAGE_KEY) === id)
              window.sessionStorage.removeItem(EQUITY_ACTIVE_CYCLE_STORAGE_KEY);
          } catch { /* ignore */ }
          if (value.status === "COMPLETED") {
            request<Radar>("/api/v1/research/opportunities/current").then(normalizeRadar)
              .then((result) => { if (active) setData(result); })
              .catch(() => { /* keep showing the terminal status panel */ });
          }
        } else {
          setActiveCycle(update);
        }
      } catch (failure) {
        if (!active) return;
        const status = (failure as { status?: number }).status;
        if (status === 404 || status === 422) {
          terminal = true;
          setProgressCycle(previous => previous?.cycleId === id ? null : previous);
          setActiveCycle(previous => previous?.cycleId === id ? null : previous);
          setDiscoveryError(true);
        } else {
          const failed = (previous: EquityActiveCycle | null) => previous?.cycleId === id ? { ...previous, pollError: true } : previous;
          setProgressCycle(failed);
          setActiveCycle(failed);
        }
      }
      if (active && !terminal) pollTimer = setTimeout(poll, 2500);
    };
    void poll();
    return () => { active = false; if (pollTimer) clearTimeout(pollTimer); };
  }, [cycleId, discoveryLoading]);

  const runCycle = () => {
    // Explicitly requests the FULL analysis scope: every eligible instrument
    // is deep-analysed, with no artificial 25-stock shortlist cap. Submitted
    // explicitly (not left to the server default) so this request's scope is
    // never silently dependent on app/main.py's OpportunityCycleRequest default
    // changing underneath it. The server-side BOUNDED contract and its
    // shortlist_limit=25 default remain available for other callers.
    setRunError(null);
    request<{ cycle_id: string; status: string; updated_at?: string }>(
      "/api/v1/research/opportunities/cycles", { method: "POST", body: JSON.stringify({ analysis_scope: "FULL" }) }
    )
      .then((value) => {
        const cycle = { cycleId: value.cycle_id, status: value.status, startedAt: value.updated_at ?? null,
          updatedAt: value.updated_at ?? null, errorCode: null, progress: null, pollError: false };
        setActiveCycle(cycle);
        setProgressCycle(cycle);
        try { window.sessionStorage.setItem(EQUITY_ACTIVE_CYCLE_STORAGE_KEY, value.cycle_id); } catch { /* ignore */ }
      })
      .catch((failure: { status?: number; message?: string }) => setRunError(equityAdmissionExplanation(failure)));
  };

  const runBoundedTest = async () => {
    if (!roles.includes("ADMIN") || running || discoveryLoading || discoveryError || boundedRequestPending.current) return;
    boundedRequestPending.current = true;
    setBoundedSubmitting(true);
    setRunError(null);
    try {
      const value = await request<{ cycle_id: string; status: string; updated_at?: string }>(
        "/api/v1/research/opportunities/cycles", {
          method: "POST", body: JSON.stringify({ analysis_scope: "BOUNDED", shortlist_limit: 1 })
        });
      const cycle = { cycleId: value.cycle_id, status: value.status, startedAt: value.updated_at ?? null,
        updatedAt: value.updated_at ?? null, errorCode: null, progress: null, pollError: false };
      setActiveCycle(cycle);
      setProgressCycle(cycle);
      try { window.sessionStorage.setItem(EQUITY_ACTIVE_CYCLE_STORAGE_KEY, value.cycle_id); } catch { /* ignore */ }
    } catch (failure) {
      setRunError(equityAdmissionExplanation(failure as { status?: number; message?: string }));
    } finally {
      boundedRequestPending.current = false;
      setBoundedSubmitting(false);
    }
  };

  const cancelCycle = async () => {
    if (!activeCycle || cancelling) return;
    const id = activeCycle.cycleId;
    setCancelling(true);
    setCancelError(null);
    try {
      const result = await request<{ status: string }>(
        `/api/v1/research/opportunities/cycles/${id}/cancel`, { method: "DELETE" });
      setActiveCycle(previous => previous?.cycleId === id
        ? isTerminalRadarStatus(result.status) ? null : { ...previous, status: result.status } : previous);
      // The status poll loads final durable progress before stopping.
      setProgressCycle(previous => previous?.cycleId === id ? { ...previous, status: result.status } : previous);
    } catch (failure) {
      const status = (failure as { status?: number }).status;
      setCancelError(status === 401 ? "Sign in again to cancel this cycle."
        : status === 403 ? "Administrator access is required to cancel this cycle." : "Cancellation could not be requested. Try again.");
    } finally { setCancelling(false); }
  };

  const running = !!activeCycle && !isTerminalRadarStatus(activeCycle.status);

  return <section className="opportunity-radar" aria-label="Global opportunity radar">
    <h2>Global opportunity radar</h2>
    <button type="button" className="button button-primary radar-run-action" disabled={running || discoveryLoading} onClick={runCycle}>
      {running ? "Running…" : "Run Equity Radar"}
    </button>
    {roles.includes("ADMIN") ? <>
      <button type="button" className="button button-secondary" aria-describedby="bounded-equity-scope"
        disabled={running || discoveryLoading || discoveryError || boundedSubmitting} onClick={() => void runBoundedTest()}>
        {boundedSubmitting ? "Submitting bounded test…" : "Run Bounded Test"}
      </button>
      <p id="bounded-equity-scope" className="broker-note">
        Deep investigation is limited to 1 candidate. Baseline screening still covers the NSE universe.
        Completed results update the shared Equity Radar.
      </p>
    </> : null}
    {running && roles.includes("ADMIN") ? <button type="button" disabled={cancelling || activeCycle?.status === "CANCEL_REQUESTED"}
      onClick={() => void cancelCycle()}>{cancelling ? "Requesting cancellation…" : "Cancel Equity Radar"}</button> : null}
    {runError ? <p role="alert">{runError}</p> : null}
    {cancelError ? <p role="alert">{cancelError}</p> : null}
    {discoveryLoading ? <p>Loading research progress…</p> : null}
    {discoveryError ? <p role="alert">Could not load research progress. <button type="button" onClick={() => setDiscoveryVersion(value => value + 1)}>Retry research progress</button></p> : null}
    {progressCycle && isTerminalRadarStatus(progressCycle.status) ? <p>Retained historical research from the last execution. No execution is active.</p> : null}
    {progressCycle ? (
      <RadarProgressPanel radarType="Equity" cycleId={progressCycle.cycleId} showCycleId={false} status={progressCycle.status}
        startedAt={progressCycle.startedAt} updatedAt={progressCycle.updatedAt}
        counters={equityCounters(progressCycle.progress)}
        /* scanned must be the live baseline_completed count, not baseline_evaluated --
           baseline_evaluated is a static admission-time snapshot (always equal to
           universe_count for Equity cycles) and reporting it as "instruments scanned"
           claims a fully-complete baseline scan the instant the universe is admitted,
           even when almost none of it has actually been processed yet. See the
           baseline_evaluated vs baseline_completed comment in
           app/cycle_checkpoint.py's cycle_status_snapshot. */
        acquisition={{ scanned: progressCycle.progress?.baseline_completed ?? null, total: progressCycle.progress?.universe_count ?? null }}
        phase={progressCycle.progress?.phase} percent={equityPercent(progressCycle.progress)} errorCode={progressCycle.errorCode} pollError={progressCycle.pollError} />
    ) : null}
    {progressCycle || data ? <EquityAnalysisPanels progress={progressCycle?.progress ?? null} data={data} /> : null}
    {error ? <p role="alert">Opportunity radar unavailable.</p> : null}
    {data ? <RadarContent data={data} heldIds={heldIds} watchlistedIds={watchlistedIds} /> : <p>Loading persisted opportunities…</p>}
  </section>;
}
