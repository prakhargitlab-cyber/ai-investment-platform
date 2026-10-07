import type {
  PortfolioRadarSignal,
  RadarPortfolioRecommendation
} from "../lib/portfolio-api";

const RECOMMENDATION_LABELS: Record<RadarPortfolioRecommendation, string> = {
  STRONG_BUY: "Strong Buy",
  BUY: "Buy",
  HOLD: "Hold",
  PARTIAL_EXIT: "Partial Exit",
  FULL_EXIT: "Full Exit"
};

const RECOMMENDATION_TONES: Record<RadarPortfolioRecommendation, string> = {
  STRONG_BUY: "strong-buy",
  BUY: "buy",
  HOLD: "hold",
  PARTIAL_EXIT: "partial-exit",
  FULL_EXIT: "full-exit"
};

export type PortfolioRadarPresentation = {
  recommendation: RadarPortfolioRecommendation | null;
  className: string;
  detail: string | null;
  conflicting: boolean;
};

export function portfolioRadarPresentation(
  signal?: PortfolioRadarSignal | null
): PortfolioRadarPresentation {
  if (!signal) {
    return { recommendation: null, className: "", detail: null, conflicting: false };
  }
  const horizons = [
    signal.shortTermRecommendation
      ? `Short-term: ${RECOMMENDATION_LABELS[signal.shortTermRecommendation]}`
      : null,
    signal.longTermRecommendation
      ? `Long-term: ${RECOMMENDATION_LABELS[signal.longTermRecommendation]}`
      : null
  ].filter((value): value is string => Boolean(value));
  const recommendations = [
    signal.shortTermRecommendation,
    signal.longTermRecommendation
  ].filter((value): value is RadarPortfolioRecommendation => Boolean(value));
  const distinct = new Set(recommendations);
  const conflicting = distinct.size > 1;
  const recommendation = distinct.size === 1 ? recommendations[0] : null;
  const audit = [
    signal.recommendationAt ? `Recommendation: ${new Date(signal.recommendationAt).toLocaleString()}` : null,
    signal.evaluatedAt ? `Last completed evaluation: ${new Date(signal.evaluatedAt).toLocaleString()}` : null,
    signal.cycleId ? `Radar cycle: ${signal.cycleId}` : null
  ].filter(Boolean);
  return {
    recommendation,
    className: recommendation ? `portfolio-radar-name portfolio-radar-${RECOMMENDATION_TONES[recommendation]}` : "",
    detail: [...horizons, ...audit].join(" · ") || null,
    conflicting
  };
}

export function PortfolioRadarCompanyName({
  companyName,
  signal
}: {
  companyName: string;
  signal?: PortfolioRadarSignal | null;
}) {
  const presentation = portfolioRadarPresentation(signal);
  return <>
    <strong className={presentation.className || undefined} title={presentation.detail ?? undefined}>
      {companyName}
    </strong>
    {presentation.detail ? <span className="sr-only">. Radar: {presentation.detail}</span> : null}
  </>;
}

// -- ETF Radar counterpart -----------------------------------------------
// Deliberately SEPARATE from the Equity presentation above: ETF Radar's
// recommendation vocabulary (STRONG_OPPORTUNITY/OPPORTUNITY/WATCH/AVOID/
// INSUFFICIENT_DATA) is not the Equity one (STRONG_BUY/BUY/HOLD/
// PARTIAL_EXIT/FULL_EXIT), so this never shares a type, a label map, or a
// tone map with it. Only ever used for a position whose
// instrument.assetType === "ETF".
import type { EtfPortfolioRadarSignal } from "../lib/portfolio-api";

const ETF_RECOMMENDATION_LABELS: Record<string, string> = {
  STRONG_OPPORTUNITY: "Strong Opportunity",
  OPPORTUNITY: "Opportunity",
  WATCH: "Watch",
  AVOID: "Avoid",
  INSUFFICIENT_DATA: "Insufficient Data",
};

const ETF_RECOMMENDATION_TONES: Record<string, string> = {
  STRONG_OPPORTUNITY: "strong-buy",
  OPPORTUNITY: "buy",
  WATCH: "hold",
  AVOID: "full-exit",
  INSUFFICIENT_DATA: "",
};

export type EtfPortfolioRadarPresentation = {
  recommendation: string | null;
  className: string;
  detail: string | null;
};

export function etfPortfolioRadarPresentation(
  signal?: EtfPortfolioRadarSignal | null
): EtfPortfolioRadarPresentation {
  if (!signal || !signal.recommendation) {
    return { recommendation: null, className: "", detail: null };
  }
  const label = ETF_RECOMMENDATION_LABELS[signal.recommendation] ?? signal.recommendation;
  const tone = ETF_RECOMMENDATION_TONES[signal.recommendation];
  const audit = [
    `ETF Radar: ${label}`,
    signal.score != null ? `Score: ${signal.score.toFixed(1)}` : null,
    signal.confidence ? `Confidence: ${signal.confidence}` : null,
    signal.asOf ? `As of: ${new Date(signal.asOf).toLocaleString()}` : null,
    signal.cycleId ? `Radar cycle: ${signal.cycleId}` : null,
  ].filter(Boolean);
  return {
    recommendation: signal.recommendation,
    className: tone ? `portfolio-radar-name portfolio-radar-${tone}` : "",
    detail: audit.join(" · ") || null,
  };
}

export function EtfRadarCompanyName({
  companyName,
  signal
}: {
  companyName: string;
  signal?: EtfPortfolioRadarSignal | null;
}) {
  const presentation = etfPortfolioRadarPresentation(signal);
  return <>
    <strong className={presentation.className || undefined} title={presentation.detail ?? undefined}>
      {companyName}
    </strong>
    {presentation.detail ? <span className="sr-only">. {presentation.detail}</span> : null}
  </>;
}
