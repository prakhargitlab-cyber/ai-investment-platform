"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { request } from "../lib/portfolio-api";
import {
  historyExtremes,
  historyIndicators,
  historyWindow,
  loadEtfHistory,
  type HistoryPoint,
  type HistoryRange,
  type HistorySelection,
  type HistorySeries,
} from "../lib/etf-price-history";
import "./etf-price-history.css";

const price = (value: number | null) =>
  value === null
    ? "Unavailable"
    : value.toLocaleString(undefined, {
        maximumFractionDigits: 2,
        minimumFractionDigits: 2,
      });
const percent = (value: number | null) =>
  value === null ? "Unavailable" : `${value.toFixed(2)}%`;
const volume = (value: string | null) =>
  value === null ? "Not recorded" : BigInt(value).toLocaleString();
const WARNING_LABELS: Record<string, string> = {
  CORPORATE_ACTION_ADJUSTMENT_UNVERIFIED:
    "Corporate-action adjustment coverage has not been verified.",
  UNADJUSTED_PRICE_NOT_TOTAL_RETURN:
    "These are unadjusted price observations, not total returns or dividend-adjusted performance.",
  MULTIPLE_PROVIDERS_KEPT_SEPARATE:
    "Other providers are stored separately. This chart uses one selected source.",
  HISTORY_SESSION_CALENDAR_UNAVAILABLE:
    "The exchange session calendar is unavailable; observations cannot be verified for plotting.",
  CONFLICTING_PROVIDER_SESSIONS_EXCLUDED:
    "Conflicting observations were excluded.",
  WARMUP_OBSERVATIONS_EXCLUDED:
    "Some earlier observations could not be validated; indicators may need more history.",
  MISSING_COMPLETED_SESSIONS_NO_CANDLES_FILLED:
    "Completed trading sessions are missing. Lines break at gaps and moving-average windows restart.",
  NO_VALIDATED_CLOSES_IN_RETURNED_PAGE:
    "Stored observations in this page failed validation.",
  NO_PERSISTED_HISTORY_IN_RANGE:
    "No historical observations are stored for this range. Opening this chart does not acquire data.",
};

export function EtfHistoryPlot({
  points,
  indicators = [50, 150, 200],
  width = 900,
  hoverIndex,
  onHover,
  onPan,
  ohlc = false,
}: {
  points: HistoryPoint[];
  indicators?: number[];
  width?: number;
  hoverIndex?: number;
  onHover?: (index: number) => void;
  onPan?: (fraction: number) => void;
  ohlc?: boolean;
}) {
  const drag = useRef<number | null>(null);
  const w = Math.max(300, width),
    left = 62,
    right = w - 16,
    top = 22,
    bottom = 222,
    volumeTop = 252,
    volumeBottom = 306;
  const prices = points
    .flatMap((p) => [
      p.price,
      ...(ohlc
        ? [
            p.open === null ? null : Number(p.open),
            p.high === null ? null : Number(p.high),
            p.low === null ? null : Number(p.low),
          ]
        : []),
      ...indicators.map((n) => p[`sma${n}` as "sma50" | "sma150" | "sma200"]),
    ])
    .filter((v): v is number => v !== null && Number.isFinite(v));
  if (!points.length) return null;
  const min = Math.min(...prices),
    max = Math.max(...prices),
    span = Math.max(max - min, max * 0.005, 0.01);
  const x = (i: number) =>
    left +
    (points.length < 2
      ? (right - left) / 2
      : (i / (points.length - 1)) * (right - left));
  const y = (v: number) =>
    bottom - ((v - min) / span) * (bottom - top - 12) - 6;
  const maxVolume = Math.max(
    1,
    ...points.map((p) => (p.volume === null ? 0 : Number(p.volume))),
  );
  const path = (key: "price" | "sma50" | "sma150" | "sma200") => {
    let drawing = false;
    return points
      .map((p, i) => {
        const value = p[key];
        if (value === null || p.gap_before) drawing = false;
        if (value === null) return "";
        const command = `${drawing ? "L" : "M"}${x(i).toFixed(2)},${y(value).toFixed(2)}`;
        drawing = true;
        return command;
      })
      .join(" ");
  };
  const nearest = (event: React.PointerEvent<SVGSVGElement>) => {
    const bounds = event.currentTarget.getBoundingClientRect();
    const position = ((event.clientX - bounds.left) / bounds.width) * w;
    onHover?.(
      Math.max(
        0,
        Math.min(
          points.length - 1,
          Math.round(
            ((position - left) / (right - left)) * (points.length - 1),
          ),
        ),
      ),
    );
  };
  const hovered = hoverIndex !== undefined ? points[hoverIndex] : null;
  return (
    <svg
      className="etf-history-svg"
      viewBox={`0 0 ${w} 334`}
      role="img"
      aria-label="Unadjusted ETF close prices and recorded daily volume"
      onPointerMove={(event) => {
        if (drag.current !== null && onPan) {
          const delta = event.clientX - drag.current;
          if (Math.abs(delta) >= 12) {
            onPan(-delta / (right - left));
            drag.current = event.clientX;
          }
        } else nearest(event);
      }}
      onPointerDown={(event) => {
        drag.current = event.clientX;
        event.currentTarget.setPointerCapture?.(event.pointerId);
      }}
      onPointerUp={() => {
        drag.current = null;
      }}
      onPointerCancel={() => {
        drag.current = null;
      }}
    >
      <title>
        Validated persisted ETF closes. Missing sessions have no invented
        candles.
      </title>
      {[0, 0.5, 1].map((t) => (
        <g key={t}>
          <line
            x1={left}
            x2={right}
            y1={y(min + t * span)}
            y2={y(min + t * span)}
            className="etf-history-gridline"
          />
          <text x={left - 8} y={y(min + t * span) + 4} textAnchor="end">
            {(min + t * span).toLocaleString(undefined, {
              maximumFractionDigits: 2,
              notation: "compact",
            })}
          </text>
        </g>
      ))}
      <text x={left} y={14}>
        INR · close
      </text>
      <path d={path("price")} className="etf-history-line" />
      {points.length === 1 ? (
        <circle
          cx={x(0)}
          cy={y(points[0].price)}
          r={3}
          className="etf-history-point"
        />
      ) : null}
      {indicators.map((n) => (
        <path
          key={n}
          d={path(`sma${n}` as "sma50" | "sma150" | "sma200")}
          className={`etf-history-line etf-history-sma-${n}`}
        />
      ))}
      {ohlc
        ? points.map((p, i) =>
            p.high !== null && p.low !== null && p.open !== null ? (
              <g key={p.date} className="etf-history-ohlc">
                <line
                  x1={x(i)}
                  x2={x(i)}
                  y1={y(Number(p.high))}
                  y2={y(Number(p.low))}
                />
                <line
                  x1={x(i) - 3}
                  x2={x(i)}
                  y1={y(Number(p.open))}
                  y2={y(Number(p.open))}
                />
                <line x1={x(i)} x2={x(i) + 3} y1={y(p.price)} y2={y(p.price)} />
              </g>
            ) : null,
          )
        : null}
      <text x={left} y={245}>
        Volume · units (when recorded)
      </text>
      {points.map((p, i) =>
        p.volume === null ? null : (
          <rect
            key={p.date}
            x={x(i) - 1.5}
            y={
              volumeBottom -
              (Number(p.volume) / maxVolume) * (volumeBottom - volumeTop)
            }
            width={Math.max(1, Math.min(8, (right - left) / points.length - 1))}
            height={(Number(p.volume) / maxVolume) * (volumeBottom - volumeTop)}
            className="etf-history-volume"
          />
        ),
      )}
      <line
        x1={left}
        x2={right}
        y1={volumeBottom}
        y2={volumeBottom}
        className="etf-history-gridline"
      />
      {[0, Math.floor((points.length - 1) / 2), points.length - 1]
        .filter((v, i, a) => a.indexOf(v) === i)
        .map((i, k, a) => (
          <text
            key={i}
            x={x(i)}
            y={327}
            textAnchor={
              k === 0 ? "start" : k === a.length - 1 ? "end" : "middle"
            }
          >
            {points[i].date}
          </text>
        ))}
      {hovered ? (
        <g>
          <line
            x1={x(hoverIndex!)}
            x2={x(hoverIndex!)}
            y1={top}
            y2={volumeBottom}
            className="etf-history-crosshair"
          />
          <circle
            cx={x(hoverIndex!)}
            cy={y(hovered.price)}
            r={4}
            className="etf-history-point"
          />
        </g>
      ) : null}
    </svg>
  );
}

export function EtfPriceHistory({
  instrumentId,
  symbol,
}: {
  instrumentId: string;
  symbol?: string | null;
}) {
  const [selection, setSelection] = useState<HistorySelection>({ range: "1Y" });
  const [customStart, setCustomStart] = useState(""),
    [customEnd, setCustomEnd] = useState("");
  const [data, setData] = useState<HistorySeries | null>(null),
    [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true),
    [retry, setRetry] = useState(0);
  const [indicators, setIndicators] = useState<number[]>([50]),
    [ohlc, setOhlc] = useState(false);
  const [view, setView] = useState({ start: 0, size: 0 }),
    [hover, setHover] = useState<number | undefined>();
  const parent = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(900);
  useEffect(() => {
    if (!parent.current || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) =>
      setWidth(Math.max(300, entries[0]?.contentRect.width ?? 900)),
    );
    observer.observe(parent.current);
    return () => observer.disconnect();
  }, [data]);
  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 60000);
    setLoading(true);
    setError(null);
    setData(null);
    setHover(undefined);
    loadEtfHistory(instrumentId, selection, request, controller.signal)
      .then((result) => {
        if (active) {
          setData(result);
          setView({ start: 0, size: result.bars.length });
          setLoading(false);
        }
      })
      .catch((failure: unknown) => {
        if (!active) return;
        const message =
          failure &&
          typeof failure === "object" &&
          "message" in failure &&
          typeof failure.message === "string"
            ? failure.message
            : "Historical prices could not be loaded. Retry the persisted history request.";
        setError(
          controller.signal.aborted
            ? "The history request timed out. Retry or select a shorter range."
            : message,
        );
        setLoading(false);
      })
      .finally(() => clearTimeout(timeout));
    return () => {
      active = false;
      clearTimeout(timeout);
      controller.abort();
    };
  }, [instrumentId, selection, retry]);
  const all = useMemo(
    () => (data ? historyIndicators(data.bars, data.warmup) : []),
    [data],
  );
  const bounds = historyWindow(all.length, view.start, view.size || all.length);
  const points = all.slice(bounds.start, bounds.start + bounds.size);
  const extremes = historyExtremes(points),
    current = points[hover ?? points.length - 1];
  const pan = (fraction: number) => {
    setView(
      historyWindow(
        all.length,
        bounds.start + Math.round(fraction * bounds.size),
        bounds.size,
      ),
    );
    setHover(undefined);
  };
  const zoom = (factor: number) => {
    const size = Math.max(10, Math.round(bounds.size * factor));
    setView(
      historyWindow(all.length, bounds.start + (bounds.size - size) / 2, size),
    );
    setHover(undefined);
  };
  const hasOhlc = points.some(
    (p) => p.open !== null && p.high !== null && p.low !== null,
  );
  const changeRange = (range: HistoryRange) =>
    setSelection((old) => ({ range, provider: old.provider }));
  return (
    <section
      className="etf-price-history"
      aria-label={`${symbol ?? "ETF"} historical market data`}
    >
      <header>
        <div>
          <h3>Price history</h3>
          <p>
            Persisted daily observations · one verified source · no automatic
            backfill
          </p>
        </div>
        <span className="etf-history-basis">Unadjusted price</span>
      </header>
      <div
        className="etf-history-toolbar"
        aria-label="Historical range selection"
      >
        <div role="group" aria-label="Price history range">
          {(["1Y", "3Y", "5Y", "MAX"] as HistoryRange[]).map((range) => (
            <button
              key={range}
              type="button"
              aria-pressed={selection.range === range}
              onClick={() => changeRange(range)}
            >
              {range === "MAX" ? "Max" : range}
            </button>
          ))}
        </div>
        <label>
          Source
          <select
            aria-label="Historical price source"
            value={selection.provider ?? data?.provider ?? ""}
            onChange={(event) =>
              setSelection((old) => ({
                ...old,
                provider: event.target.value || undefined,
              }))
            }
          >
            {!data?.available_providers.length ? (
              <option value="">Recorded source</option>
            ) : (
              data.available_providers.map((p) => (
                <option key={p.provider} value={p.provider}>
                  {p.provider.replaceAll("_", " ")}
                </option>
              ))
            )}
          </select>
        </label>
        <details className="etf-history-custom">
          <summary>Custom dates</summary>
          <div>
            <label>
              From
              <input
                aria-label="History start date"
                type="date"
                value={customStart}
                onChange={(e) => setCustomStart(e.target.value)}
              />
            </label>
            <label>
              To
              <input
                aria-label="History end date"
                type="date"
                value={customEnd}
                onChange={(e) => setCustomEnd(e.target.value)}
              />
            </label>
            <button
              type="button"
              disabled={!customStart || !customEnd || customStart > customEnd}
              onClick={() =>
                setSelection((old) => ({
                  range: "CUSTOM",
                  start: customStart,
                  end: customEnd,
                  provider: old.provider,
                }))
              }
            >
              Apply dates
            </button>
          </div>
        </details>
      </div>
      {loading ? (
        <p role="status">Loading and validating stored price observations…</p>
      ) : null}
      {error ? (
        <div role="alert" className="error-state">
          <p>{error}</p>
          <button type="button" onClick={() => setRetry((v) => v + 1)}>
            Retry price history
          </button>
        </div>
      ) : null}
      {!loading && data ? (
        <>
          <p className="etf-history-coverage">
            Stored source coverage:{" "}
            {data.available.first_date ?? "not available"} to{" "}
            {data.available.last_date ?? "not available"}.{" "}
            {data.bars.length.toLocaleString()} validated closes loaded. Each
            page is validated; stored bounds alone do not certify complete
            history.
          </p>
          {data.truncated ? (
            <p role="status">
              The bounded chart limit was reached. Choose a shorter custom
              range; older observations have not been invented.
            </p>
          ) : null}
          <div
            className="etf-history-indicators"
            role="group"
            aria-label="Moving averages"
          >
            {[50, 150, 200].map((n) => (
              <label key={n}>
                <input
                  type="checkbox"
                  checked={indicators.includes(n)}
                  onChange={() =>
                    setIndicators((old) =>
                      old.includes(n)
                        ? old.filter((v) => v !== n)
                        : [...old, n],
                    )
                  }
                />
                SMA {n}
              </label>
            ))}
            <label>
              <input
                type="checkbox"
                checked={ohlc}
                disabled={!hasOhlc}
                onChange={(event) => setOhlc(event.target.checked)}
              />
              OHLC {hasOhlc ? "when recorded" : "unavailable"}
            </label>
          </div>
          {points.length ? (
            <>
              <div
                className="etf-history-navigation"
                role="group"
                aria-label="Chart zoom and pan"
              >
                <button
                  type="button"
                  aria-label="Pan history earlier"
                  disabled={bounds.start === 0}
                  onClick={() => pan(-0.25)}
                >
                  ← Earlier
                </button>
                <button
                  type="button"
                  aria-label="Zoom into price history"
                  disabled={bounds.size <= 10}
                  onClick={() => zoom(0.5)}
                >
                  Zoom in
                </button>
                <button
                  type="button"
                  aria-label="Zoom out of price history"
                  disabled={bounds.size >= all.length}
                  onClick={() => zoom(2)}
                >
                  Zoom out
                </button>
                <button
                  type="button"
                  aria-label="Reset price history zoom"
                  onClick={() => setView({ start: 0, size: all.length })}
                >
                  Reset view
                </button>
                <button
                  type="button"
                  aria-label="Pan history later"
                  disabled={bounds.start + bounds.size >= all.length}
                  onClick={() => pan(0.25)}
                >
                  Later →
                </button>
              </div>
              <div
                ref={parent}
                className="etf-history-plot"
                tabIndex={0}
                aria-label="Interactive price chart; arrow keys move the observation cursor"
                onKeyDown={(event) => {
                  if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
                    event.preventDefault();
                    setHover((old) =>
                      Math.max(
                        0,
                        Math.min(
                          points.length - 1,
                          (old ?? points.length - 1) +
                            (event.key === "ArrowLeft" ? -1 : 1),
                        ),
                      ),
                    );
                  }
                }}
              >
                <EtfHistoryPlot
                  points={points}
                  indicators={indicators}
                  width={width}
                  hoverIndex={hover}
                  onHover={setHover}
                  onPan={pan}
                  ohlc={ohlc}
                />
              </div>
              {current ? (
                <dl
                  className="etf-history-hover"
                  aria-label="Price chart observation"
                >
                  <div>
                    <dt>Date</dt>
                    <dd>{current.date}</dd>
                  </div>
                  <div>
                    <dt>Close · INR</dt>
                    <dd title={current.close}>{price(current.price)}</dd>
                  </div>
                  <div>
                    <dt>Change since prior observation</dt>
                    <dd>{percent(current.change)}</dd>
                  </div>
                  <div>
                    <dt>Volume · units</dt>
                    <dd>{volume(current.volume)}</dd>
                  </div>
                  {indicators.map((n) => (
                    <div key={n}>
                      <dt>SMA {n} · INR</dt>
                      <dd>
                        {price(
                          current[`sma${n}` as "sma50" | "sma150" | "sma200"],
                        )}
                      </dd>
                    </div>
                  ))}
                  {ohlc
                    ? (["open", "high", "low"] as const).map((key) => (
                        <div key={key}>
                          <dt>{key} · INR</dt>
                          <dd>
                            {current[key] === null
                              ? "Not recorded"
                              : price(Number(current[key]))}
                          </dd>
                        </div>
                      ))
                    : null}
                </dl>
              ) : null}
              <dl className="etf-history-extremes">
                <div>
                  <dt>Peak observed close in view</dt>
                  <dd>
                    INR {price(extremes.peak?.price ?? null)} ·{" "}
                    {extremes.peak?.date}
                  </dd>
                </div>
                <div>
                  <dt>Drawdown from that peak</dt>
                  <dd>{percent(extremes.currentDrawdown)}</dd>
                </div>
                <div>
                  <dt>Maximum observed drawdown in view</dt>
                  <dd>{percent(extremes.maxDrawdown)}</dd>
                </div>
              </dl>
              <p className="etf-history-method">
                Drag a zoomed chart to pan, or use the buttons. SMA windows need
                50, 150 or 200 consecutive validated exchange-session
                observations. Unavailable indicators remain blank; weekends and
                recorded exchange closures are not filled.
              </p>
            </>
          ) : (
            <p className="empty-state">
              No validated price history is available for this selection. Choose
              another recorded source or range.
            </p>
          )}
          <p className="etf-history-benchmark">
            Benchmark comparison unavailable: no independently verified
            ETF-to-benchmark mapping with a compatible persisted series is
            available.
          </p>
          <details className="etf-history-warnings">
            <summary>Price basis, data gaps and source limitations</summary>
            <ul>
              {data.diagnostics.warnings.map((code) => (
                <li key={code}>
                  {WARNING_LABELS[code] ??
                    code.replaceAll("_", " ").toLowerCase()}
                </li>
              ))}
              {data.diagnostics.rejected_rows ? (
                <li>
                  {data.diagnostics.rejected_rows} stored rows were excluded by
                  validation.
                </li>
              ) : null}
              {data.diagnostics.missing_sessions.length ? (
                <li>
                  Missing configured sessions include:{" "}
                  {data.diagnostics.missing_sessions.slice(0, 8).join(", ")}
                  {data.diagnostics.missing_sessions.length > 8 ? "…" : ""}.
                </li>
              ) : null}
            </ul>
            <p>
              Provider: {data.provider ?? "not available"}. Session definitions
              use persisted NSE schedules and exceptions. Close-only source
              records cannot supply OHLC or executed volume.
            </p>
          </details>
        </>
      ) : null}
    </section>
  );
}
