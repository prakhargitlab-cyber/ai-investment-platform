export type HistoryRange = "1Y" | "3Y" | "5Y" | "MAX" | "CUSTOM";
export type HistorySelection = {
  range: HistoryRange;
  start?: string;
  end?: string;
  provider?: string;
};
export type HistoryBar = {
  date: string;
  close: string;
  open: string | null;
  high: string | null;
  low: string | null;
  volume: string | null;
  gap_before: boolean;
  provider: string;
  provider_symbol: string;
  currency: string;
  source_url: string;
  retrieved_at: string;
};
export type HistoryPage = {
  instrument_id: string;
  symbol: string;
  isin: string;
  provider: string | null;
  currency: "INR";
  exchange: "NSE";
  mic: "XNSE";
  price_basis: "UNADJUSTED_CLOSE";
  range: { name: HistoryRange; start: string | null; end: string };
  available: {
    first_date: string | null;
    last_date: string | null;
    stored_rows: number;
    observations: number | null;
    validation_scope: string;
  };
  available_providers: {
    provider: string;
    first_date: string;
    last_date: string;
    stored_rows: number;
  }[];
  bars: HistoryBar[];
  warmup: HistoryBar[];
  next_cursor: string | null;
  as_of: string;
  diagnostics: {
    warnings: string[];
    rejected_rows: number;
    calendar_state: string;
    missing_sessions: string[];
    missing_session_count?: number;
  };
  benchmark: { status: "UNAVAILABLE"; reason: string };
};
export type HistorySeries = HistoryPage & { truncated: boolean; pages: number };
export type HistoryPoint = HistoryBar & {
  price: number;
  sma50: number | null;
  sma150: number | null;
  sma200: number | null;
  change: number | null;
};

const PROVIDERS = new Set(["NSE", "YAHOO_FINANCE", "YAHOO_FINANCE_MCP"]);
const record = (v: unknown): v is Record<string, unknown> =>
  v !== null && typeof v === "object" && !Array.isArray(v);
export function validHistoryDate(v: unknown): v is string {
  return (
    typeof v === "string" &&
    /^\d{4}-\d{2}-\d{2}$/.test(v) &&
    Number.isFinite(Date.parse(v)) &&
    new Date(v).toISOString().slice(0, 10) === v
  );
}
const positive = (v: unknown) =>
  typeof v === "string" &&
  /^\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$/.test(v) &&
  Number.isFinite(Number(v)) &&
  Number(v) > 0;
const awareDate = (v: unknown): v is string =>
  typeof v === "string" &&
  /(?:Z|[+-]\d\d:\d\d)$/.test(v) &&
  Number.isFinite(Date.parse(v));
function invalid(): never {
  throw new Error(
    "The persisted price-history response could not be validated. Retry or choose another recorded source.",
  );
}

export function parseHistoryPage(
  raw: unknown,
  instrumentId: string,
): HistoryPage {
  if (
    !record(raw) ||
    raw.instrument_id !== instrumentId ||
    typeof raw.symbol !== "string" ||
    typeof raw.isin !== "string" ||
    raw.currency !== "INR" ||
    raw.exchange !== "NSE" ||
    raw.mic !== "XNSE" ||
    raw.price_basis !== "UNADJUSTED_CLOSE" ||
    !(
      raw.provider === null ||
      (typeof raw.provider === "string" && PROVIDERS.has(raw.provider))
    ) ||
    !awareDate(raw.as_of) ||
    !record(raw.range) ||
    !validHistoryDate(raw.range.end) ||
    !(raw.range.start === null || validHistoryDate(raw.range.start)) ||
    !["1Y", "3Y", "5Y", "MAX", "CUSTOM"].includes(String(raw.range.name)) ||
    !record(raw.available) ||
    !Array.isArray(raw.available_providers) ||
    !Array.isArray(raw.bars) ||
    !Array.isArray(raw.warmup) ||
    raw.bars.length > 500 ||
    raw.warmup.length > 199 ||
    !(raw.next_cursor === null || validHistoryDate(raw.next_cursor)) ||
    !record(raw.diagnostics) ||
    !Array.isArray(raw.diagnostics.warnings) ||
    raw.diagnostics.warnings.some((v) => typeof v !== "string") ||
    !Number.isInteger(raw.diagnostics.rejected_rows) ||
    Number(raw.diagnostics.rejected_rows) < 0 ||
    !Array.isArray(raw.diagnostics.missing_sessions) ||
    raw.diagnostics.missing_sessions.some((v) => !validHistoryDate(v)) ||
    !record(raw.benchmark) ||
    raw.benchmark.status !== "UNAVAILABLE" ||
    typeof raw.benchmark.reason !== "string"
  )
    invalid();
  const page = raw as unknown as HistoryPage;
  if (
    (page.bars.length || page.warmup.length) &&
    page.diagnostics.calendar_state !== "PERSISTED_SESSION_SCHEDULE"
  )
    invalid();
  for (const key of ["first_date", "last_date"] as const)
    if (
      !(page.available[key] === null || validHistoryDate(page.available[key]))
    )
      invalid();
  if (
    !Number.isSafeInteger(page.available.stored_rows) ||
    page.available.stored_rows < 0
  )
    invalid();
  for (const source of page.available_providers) {
    if (
      !record(source) ||
      !PROVIDERS.has(String(source.provider)) ||
      !validHistoryDate(source.first_date) ||
      !validHistoryDate(source.last_date) ||
      !Number.isSafeInteger(source.stored_rows) ||
      source.stored_rows < 0
    )
      invalid();
  }
  for (const collection of [page.bars, page.warmup]) {
    let previous = "";
    for (const bar of collection) {
      if (
        !record(bar) ||
        !validHistoryDate(bar.date) ||
        bar.date <= previous ||
        !positive(bar.close) ||
        bar.provider !== page.provider ||
        bar.currency !== page.currency ||
        typeof bar.gap_before !== "boolean" ||
        bar.provider_symbol !==
          (page.provider === "NSE" ? page.symbol : page.symbol + ".NS") ||
        !awareDate(bar.retrieved_at) ||
        Date.parse(bar.retrieved_at) > Date.parse(page.as_of) ||
        bar.date > page.range.end ||
        bar.date > page.as_of.slice(0, 10) ||
        bar.date > bar.retrieved_at.slice(0, 10) ||
        typeof bar.source_url !== "string" ||
        !(
          bar.volume === null ||
          (typeof bar.volume === "string" &&
            /^(0|[1-9]\d{0,18})$/.test(bar.volume) &&
            BigInt(bar.volume) <= 9223372036854775807n)
        )
      )
        invalid();
      for (const key of ["open", "high", "low"] as const)
        if (!(bar[key] === null || positive(bar[key]))) invalid();
      if (
        bar.high !== null &&
        bar.low !== null &&
        Number(bar.high) < Number(bar.low)
      )
        invalid();
      try {
        const source = new URL(bar.source_url);
        const hosts =
          page.provider === "NSE"
            ? [
                "www.nseindia.com",
                "nseindia.com",
                "nsearchives.nseindia.com",
                "archives.nseindia.com",
              ]
            : [
                "finance.yahoo.com",
                "www.finance.yahoo.com",
                "query1.finance.yahoo.com",
                "query2.finance.yahoo.com",
              ];
        if (
          source.protocol !== "https:" ||
          source.username ||
          source.password ||
          !hosts.includes(source.hostname) ||
          (source.port && source.port !== "443")
        )
          invalid();
      } catch {
        invalid();
      }
      previous = bar.date;
    }
  }
  if (
    page.bars.some(
      (b) => page.range.start !== null && b.date < page.range.start,
    ) ||
    page.warmup.some(
      (b) => page.range.start === null || b.date >= page.range.start,
    ) ||
    (page.next_cursor !== null &&
      (page.next_cursor > page.range.end ||
        page.bars.some((b) => b.date > page.next_cursor!)))
  )
    invalid();
  return page;
}

/** Fixed page and observation ceilings; no GET ever initiates acquisition. */
export async function loadEtfHistory(
  instrumentId: string,
  selection: HistorySelection,
  fetchPage: (path: string, init?: RequestInit) => Promise<unknown>,
  signal?: AbortSignal,
  maxPages = 40,
): Promise<HistorySeries> {
  if (
    !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
      instrumentId,
    ) ||
    !Number.isInteger(maxPages) ||
    maxPages < 1 ||
    maxPages > 40
  )
    invalid();
  if (
    selection.range === "CUSTOM" &&
    (!validHistoryDate(selection.start) ||
      !validHistoryDate(selection.end) ||
      selection.start > selection.end!)
  )
    invalid();
  const params = new URLSearchParams({ range: selection.range, limit: "500" });
  if (selection.provider) params.set("provider", selection.provider);
  if (selection.range === "CUSTOM") {
    params.set("start", selection.start!);
    params.set("end", selection.end!);
  }
  let first: HistoryPage | null = null,
    cursor: string | null = null,
    pages = 0;
  const bars: HistoryBar[] = [],
    warnings = new Set<string>(),
    missing = new Set<string>();
  let rejected = 0;
  do {
    if (signal?.aborted)
      throw new DOMException("History request cancelled", "AbortError");
    if (cursor) params.set("after", cursor);
    const page = parseHistoryPage(
      await fetchPage(
        `/api/v1/etf-radar/instruments/${encodeURIComponent(instrumentId)}/history?${params}`,
        { signal },
      ),
      instrumentId,
    );
    if (
      first &&
      (page.provider !== first.provider ||
        page.symbol !== first.symbol ||
        page.isin !== first.isin ||
        page.range.name !== first.range.name || page.range.start !== first.range.start || page.range.end !== first.range.end ||
        page.warmup.length)
    )
      invalid();
    if (
      page.range.name !== selection.range ||
      (selection.range === "CUSTOM" &&
        (page.range.start !== selection.start ||
          page.range.end !== selection.end))
    )
      invalid();
    first ??= page;
    if (first.provider) params.set("provider", first.provider);
    if (
      page.next_cursor !== null &&
      cursor !== null &&
      page.next_cursor <= cursor
    )
      invalid();
    for (const bar of page.bars) {
      if (bars.length && bar.date <= bars[bars.length - 1].date) invalid();
      bars.push(bar);
    }
    page.diagnostics.warnings.forEach((v) => warnings.add(v));
    page.diagnostics.missing_sessions.forEach((v) => missing.add(v));
    rejected += page.diagnostics.rejected_rows;
    cursor = page.next_cursor;
    pages++;
  } while (cursor && pages < maxPages);
  return {
    ...first!,
    bars,
    next_cursor: cursor,
    pages,
    truncated: cursor !== null,
    diagnostics: {
      ...first!.diagnostics,
      warnings: [...warnings],
      rejected_rows: rejected,
      missing_sessions: [...missing].sort(),
    },
  };
}

/** SMAs count consecutive, validated configured exchange-session observations.
 * A missing session resets the window. No placeholders or partial averages. */
export function historyIndicators(
  bars: HistoryBar[],
  warmup: HistoryBar[] = [],
): HistoryPoint[] {
  const window: number[] = [],
    output: HistoryPoint[] = [];
  let previous: number | null = null;
  for (const [index, bar] of [...warmup, ...bars].entries()) {
    const price = Number(bar.close);
    if (!Number.isFinite(price) || price <= 0)
      throw new Error("Invalid history observation");
    if (bar.gap_before) window.length = 0;
    window.push(price);
    if (window.length > 200) window.shift();
    const average = (size: number) =>
      window.length < size
        ? null
        : window.slice(-size).reduce((sum, v) => sum + v, 0) / size;
    if (index >= warmup.length)
      output.push({
        ...bar,
        price,
        sma50: average(50),
        sma150: average(150),
        sma200: average(200),
        change: previous === null ? null : (price / previous - 1) * 100,
      });
    previous = price;
  }
  return output;
}

export function historyExtremes(points: HistoryPoint[]) {
  let peak: HistoryPoint | null = null,
    maxDrawdown = 0;
  for (const point of points) {
    if (!peak || point.price > peak.price) peak = point;
    maxDrawdown = Math.min(maxDrawdown, (point.price / peak.price - 1) * 100);
  }
  const last = points.at(-1);
  return {
    peak,
    maxDrawdown: points.length ? maxDrawdown : null,
    currentDrawdown: peak && last ? (last.price / peak.price - 1) * 100 : null,
  };
}

export function historyWindow(length: number, start: number, size: number) {
  if (length <= 0) return { start: 0, size: 0 };
  const count = Math.max(1, Math.min(length, Math.round(size)));
  const first = Math.max(
    0,
    Math.min(Math.round(start), Math.max(0, length - count)),
  );
  return { start: first, size: count };
}
