import type { SectorPerformance, SectorPerformanceStock } from "./portfolio-api";
export type PerformanceRowTone = "positive" | "negative" | "neutral";
export type MarketIntelligenceSelection = {
  stock: SectorPerformanceStock;
  region: SectorPerformance["region"];
  period: SectorPerformance["period"];
};

export function marketIntelligenceSelection(
  performance: SectorPerformance,
  stock: SectorPerformanceStock,
): MarketIntelligenceSelection {
  return { stock, region: performance.region, period: performance.period };
}

export function regionalWatchlistName(region: SectorPerformance["region"]): string {
  return ({ INDIA: "WATCHLIST-IND", EUROPE: "WATCHLIST-EU", USA: "WATCHLIST-USA" })[region];
}

export function performanceRowTone(performancePct: number | null | undefined): PerformanceRowTone {
  if (typeof performancePct !== "number" || !Number.isFinite(performancePct) || performancePct === 0) {
    return "neutral";
  }
  return performancePct > 0 ? "positive" : "negative";
}

export function formatSignedPerformancePct(performancePct: number | null | undefined): string {
  if (typeof performancePct !== "number" || !Number.isFinite(performancePct)) return "--";
  if (performancePct > 0) return `+${performancePct.toFixed(2)}%`;
  return `${performancePct.toFixed(2)}%`;
}

export function performanceDirectionLabel(stock: SectorPerformanceStock): string {
  const tone = performanceRowTone(stock.performancePct);
  return tone === "positive" ? "positive return" : tone === "negative" ? "negative return" : "neutral return";
}
