"use client";

import { useEffect, useId, useState } from "react";
import {
  EMPTY_FILTERS,
  candidateId,
  entryLabel,
  filterEtfs,
  metricDisplay,
  researchStatusLabel,
  sortEtfs,
  type EtfCandidate,
  type EtfFilters,
  type EtfSortKey,
} from "../lib/etf-radar-model";

const COLUMNS: [EtfSortKey, string][] = [
  ["symbol", "ETF"],
  ["category", "Peer category"],
  ["status", "Research status"],
  ["rank", "Peer rank"],
  ["score", "Score / 100"],
  ["confidence", "Confidence"],
  ["completeness", "Score coverage"],
  ["expense", "Expense ratio"],
  ["aum", "AUM"],
  ["history", "History"],
  ["entry", "Entry assessment"],
];

/** Filter the entire scoped dataset before stable sorting and pagination. */
export function EtfOpportunityTable({
  rows,
  label,
  compareIds,
  onCompare,
  onAnalyze,
  initialSort = "symbol",
}: {
  rows: EtfCandidate[];
  label: string;
  compareIds: string[];
  onCompare: (id: string) => void;
  onAnalyze: (item: EtfCandidate) => void;
  initialSort?: EtfSortKey;
}) {
  const id = useId();
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [filters, setFilters] = useState<EtfFilters>({ ...EMPTY_FILTERS });
  const [sort, setSort] = useState<EtfSortKey>(initialSort);
  const [direction, setDirection] = useState<"asc" | "desc">("asc");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  useEffect(() => {
    const timer = setTimeout(() => {
      setQuery(search);
      setPage(1);
    }, 200);
    return () => clearTimeout(timer);
  }, [search]);
  const filtered = sortEtfs(filterEtfs(rows, query, filters), sort, direction);
  const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize)),
    safePage = Math.min(page, totalPages);
  const visible = filtered.slice(
    (safePage - 1) * pageSize,
    safePage * pageSize,
  );
  const categories = Array.from(
    new Set(rows.map((c) => c.peer_group ?? "Unclassified")),
  ).sort();
  const statuses = Array.from(new Set(rows.map(researchStatusLabel))).sort();
  const changeFilter = (key: keyof EtfFilters, value: string) => {
    setFilters((old) => ({ ...old, [key]: value }));
    setPage(1);
  };
  const changeSort = (key: EtfSortKey) => {
    setSort(key);
    setDirection(
      sort === key
        ? direction === "asc"
          ? "desc"
          : "asc"
        : key === "score" ||
            key === "aum" ||
            key === "confidence" ||
            key === "history" ||
            key === "completeness"
          ? "desc"
          : "asc",
    );
    setPage(1);
  };
  const reset = () => {
    setSearch("");
    setQuery("");
    setFilters({ ...EMPTY_FILTERS });
    setSort(initialSort);
    setDirection("asc");
    setPage(1);
  };
  return (
    <section className="etf-results-table" aria-label={label}>
      <div className="etf-table-toolbar">
        <label className="etf-search">
          <span>Search all research fields</span>
          <input
            type="search"
            value={search}
            placeholder="Symbol, fund, category, status, source…"
            onChange={(e) => {
              setSearch(e.target.value);
              setPage(1);
            }}
            aria-controls={`${id}-table`}
          />
        </label>
        <label>
          <span>Peer category</span>
          <select
            value={filters.category}
            onChange={(e) => changeFilter("category", e.target.value)}
          >
            <option value="ALL">All categories</option>
            {categories.map((c) => (
              <option key={c}>{c}</option>
            ))}
          </select>
        </label>
        <label>
          <span>Research status</span>
          <select
            value={filters.status}
            onChange={(e) => changeFilter("status", e.target.value)}
          >
            <option value="ALL">All statuses</option>
            {statuses.map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
        </label>
        <button
          type="button"
          className="button button-secondary"
          onClick={reset}
        >
          Reset filters
        </button>
      </div>
      <details className="etf-advanced-filters">
        <summary>More filters · cost, size, evidence and confidence</summary>
        <div className="etf-filter-grid">
          <label>
            Confidence
            <select
              value={filters.confidence}
              onChange={(e) => changeFilter("confidence", e.target.value)}
            >
              <option value="ALL">All confidence levels</option>
              {["HIGH", "MEDIUM", "LOW"].map((v) => (
                <option key={v}>{v}</option>
              ))}
            </select>
          </label>
          <label>
            Fund house
            <input
              value={filters.fundHouse}
              onChange={(e) => changeFilter("fundHouse", e.target.value)}
            />
          </label>
          {(
            [
              ["minScore", "Minimum score / 100"],
              ["minCompleteness", "Minimum score coverage (%)"],
              ["maxExpense", "Maximum expense (% p.a.)"],
              ["minAum", "Minimum AUM (INR)"],
              ["minHistory", "Minimum history sessions"],
            ] as [keyof EtfFilters, string][]
          ).map(([key, text]) => (
            <label key={key}>
              {text}
              <input
                type="number"
                min="0"
                step="any"
                value={filters[key]}
                onChange={(e) => changeFilter(key, e.target.value)}
              />
            </label>
          ))}
        </div>
        <p>
          Unavailable values do not satisfy numeric filters. Multiple filters
          apply together.
        </p>
      </details>
      <div className="etf-table-meta">
        <p role="status" aria-live="polite">
          {filtered.length} of {rows.length} results
          {search !== query ? " · Updating search…" : ""}
        </p>
        <label>
          Rows per page
          <select
            value={pageSize}
            onChange={(e) => {
              setPageSize(Number(e.target.value));
              setPage(1);
            }}
          >
            {[10, 25, 50, 100].map((size) => (
              <option key={size}>{size}</option>
            ))}
          </select>
        </label>
      </div>
      {visible.length ? (
        <div
          className="radar-table-scroll etf-table-scroll"
          tabIndex={0}
          role="region"
          aria-label={`${label} table, scroll horizontally for all columns`}
        >
          <table
            id={`${id}-table`}
            className="radar-dense-table radar-screener-table etf-data-table"
          >
            <caption>{label} · ranks compare verified peers only</caption>
            <thead>
              <tr>
                {COLUMNS.map(([key, text]) => (
                  <th
                    scope="col"
                    key={key}
                    aria-sort={
                      sort === key
                        ? direction === "asc"
                          ? "ascending"
                          : "descending"
                        : "none"
                    }
                  >
                    <button
                      type="button"
                      className="radar-sort-button"
                      onClick={() => changeSort(key)}
                    >
                      {text}
                      {sort === key ? (direction === "asc" ? " ▲" : " ▼") : ""}
                    </button>
                  </th>
                ))}
                <th scope="col">Evidence gaps</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((item) => {
                const key = candidateId(item),
                  selected = compareIds.includes(key),
                  r = item.rule_engine_result;
                return (
                  <tr className="etf-screener-row" key={key}>
                    <th scope="row">
                      <button
                        type="button"
                        className="etf-symbol-button"
                        onClick={() => onAnalyze(item)}
                        aria-label={`Analyze ${item.symbol ?? key}`}
                      >
                        {item.symbol ?? "Unknown ETF"}
                      </button>
                      <small>
                        {item.fund_name ??
                          item.canonical_name ??
                          "Fund name unavailable"}
                      </small>
                    </th>
                    <td>{item.peer_group ?? "Unclassified"}</td>
                    <td>
                      <span
                        className={`etf-status etf-status-${item.disposition.toLowerCase()}`}
                      >
                        {researchStatusLabel(item)}
                      </span>
                    </td>
                    <td>
                      {item.rank == null ? (
                        "Unavailable"
                      ) : (
                        <span
                          title={`${item.peer_group ?? "Unclassified"}: peer rank ${item.rank}`}
                        >
                          {item.rank}
                          <small>within peer</small>
                        </span>
                      )}
                    </td>
                    <td>
                      {r?.overall_score == null
                        ? "Unavailable"
                        : r.overall_score.toFixed(1)}
                    </td>
                    <td>{r?.confidence ?? "Unavailable"}</td>
                    <td>
                      {r?.data_completeness == null
                        ? "Unavailable"
                        : `${(r.data_completeness * 100).toFixed(0)}%`}
                    </td>
                    <td>{metricDisplay(item, "expense_ratio")}</td>
                    <td>{metricDisplay(item, "aum")}</td>
                    <td>
                      {item.history_observations == null
                        ? "Unavailable"
                        : `${item.history_observations} sessions`}
                      {item.historical_acquisition ? (
                        <small
                          title={
                            item.historical_acquisition.reason ?? undefined
                          }
                        >
                          {item.historical_acquisition.status
                            .replaceAll("_", " ")
                            .toLowerCase()}
                        </small>
                      ) : null}
                    </td>
                    <td>{entryLabel(item)}</td>
                    <td className="etf-gap-cell">
                      {item.missing_data?.length
                        ? item.missing_data.join(", ")
                        : item.rule_engine_result
                          ? "None reported; inspect fund evidence"
                          : "Analysis not recorded"}
                    </td>
                    <td>
                      <div className="etf-table-actions">
                        <button
                          type="button"
                          className="button button-secondary"
                          onClick={() => onAnalyze(item)}
                        >
                          Analysis
                        </button>
                        <label className="etf-compare-select">
                          <input
                            type="checkbox"
                            aria-label={`Compare ${item.symbol ?? key}`}
                            checked={selected}
                            disabled={!selected && compareIds.length >= 5}
                            onChange={() => onCompare(key)}
                          />
                          Compare
                        </label>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="radar-empty-state">
          <h3>No matching ETFs</h3>
          <p>
            {rows.length
              ? "Adjust or reset the filters. Missing evidence is never treated as zero."
              : "This cycle has no records in this research group. Explore the Research Universe for other outcomes."}
          </p>
          <button
            type="button"
            className="button button-secondary"
            onClick={reset}
          >
            Clear search and filters
          </button>
        </div>
      )}
      <nav className="etf-pagination" aria-label={`${label} pagination`}>
        <span>
          {filtered.length
            ? `${(safePage - 1) * pageSize + 1}–${Math.min(safePage * pageSize, filtered.length)} of ${filtered.length}`
            : "0 results"}
        </span>
        <button
          type="button"
          className="button button-secondary"
          disabled={safePage === 1}
          onClick={() => setPage((p) => Math.max(1, p - 1))}
        >
          Previous page
        </button>
        <span>
          Page {safePage} of {totalPages}
        </span>
        <button
          type="button"
          className="button button-secondary"
          disabled={safePage === totalPages}
          onClick={() => setPage((p) => p + 1)}
        >
          Next page
        </button>
      </nav>
    </section>
  );
}
