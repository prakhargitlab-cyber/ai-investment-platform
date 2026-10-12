"use client";

/**
 * Shared PRESENTATIONAL progress/status panel for the Equity Radar and ETF
 * Radar "run a cycle" actions (Defect 4 / Defect 5 closure).
 *
 * Deliberately presentational and stateless: each radar component owns its
 * own polling/fetch state and passes it in as props, so Equity and ETF
 * progress stay structurally isolated (this file never fetches anything and
 * never shares state between the two radars -- see opportunity-radar.tsx
 * and etf-radar.tsx, which each have their own independent
 * useActiveRadarCycle-shaped state).
 *
 * Only real backend lifecycle/status fields are ever rendered. There is no
 * fake timer-based percentage: `percent` is either a genuine
 * completed/admitted ratio the caller computed from real counters, or
 * `null`, in which case the bar renders indeterminate.
 */

export type RadarLifecycleStatus =
  | "ACCEPTED"
  | "QUEUED"
  | "RUNNING"
  | "COMPLETED"
  | "FAILED"
  | "CANCEL_REQUESTED"
  | "CANCELLED";

export const TERMINAL_RADAR_STATUSES: ReadonlySet<RadarLifecycleStatus> = new Set([
  "COMPLETED",
  "FAILED",
  "CANCELLED",
]);

export function isTerminalRadarStatus(status: string | null | undefined): boolean {
  return !!status && TERMINAL_RADAR_STATUSES.has(status as RadarLifecycleStatus);
}

/** Display label for a lifecycle status. ACCEPTED reads as "Queued" to the user. */
function statusLabel(status: string): string {
  if (status === "ACCEPTED") return "Queued";
  return status.charAt(0) + status.slice(1).toLowerCase().replaceAll("_", " ");
}

export type RadarProgressCounter = { label: string; value: number | null };

export function RadarProgressPanel({
  radarType,
  cycleId,
  status,
  startedAt,
  updatedAt,
  counters,
  percent,
  phase,
  acquisition,
  errorCode,
  pollError,
}: {
  radarType: "Equity" | "ETF";
  cycleId: string;
  status: string;
  startedAt: string | null;
  updatedAt: string | null;
  counters: RadarProgressCounter[];
  /** A genuine completed/admitted ratio (0-100), or null for indeterminate. */
  percent: number | null;
  phase?: string;
  /** Durable full-universe scan counts, never the deep shortlist. */
  acquisition?: { scanned: number | null; total: number | null };
  errorCode?: string | null;
  /** Set when the most recent status poll itself failed (transient network
   * error) -- the panel keeps showing the last known good state rather than
   * going blank or stuck. */
  pollError?: boolean;
}) {
  const terminal = isTerminalRadarStatus(status);
  const acquisitionPercent = acquisition?.scanned != null && acquisition.total != null && acquisition.total > 0
    ? (acquisition.scanned / acquisition.total) * 100 : null;
  const tone = status === "FAILED" ? "failed" : status === "CANCELLED" ? "cancelled" : status === "COMPLETED" ? "completed" : "active";
  return (
    <div className={`radar-progress-panel radar-progress-${tone}`} role="status" aria-live="polite">
      <div className="radar-progress-heading">
        <strong>{radarType} radar · {statusLabel(status)}</strong>
        <small>Cycle {cycleId}</small>
      </div>
      {!terminal && radarType === "Equity" && status !== "CANCEL_REQUESTED" ? <p>{phase === "DEEP"
        ? "Deep investigation ? progress covers admitted candidates only."
        : phase === "CANDIDATE_SELECTION" ? "Candidate selection ? selecting instruments for deep investigation."
        : phase === "BASELINE" ? acquisitionPercent == null
          ? "Baseline scanning - live full-market scan progress is unavailable."
          : "Baseline scanning - persisted full-market scan counts are shown below."
        : "Waiting for the current research phase."}</p> : null}
      {status === "CANCEL_REQUESTED" ? <p>Cancellation requested ? waiting for in-flight work to drain.</p> : null}
      {radarType === "Equity" && acquisition ? (
        <section aria-label="Full-market baseline scan">
          <p>Full-market baseline scan: {acquisition.scanned ?? "Unavailable"} of {acquisition.total ?? "Unavailable"} instruments scanned.</p>
          {!terminal ? <div className={`bar-track${acquisitionPercent == null ? " bar-track-indeterminate" : ""}`}
            role="progressbar" aria-label="Full-market baseline scan" aria-valuemin={0} aria-valuemax={100}
            aria-valuenow={acquisitionPercent == null ? undefined : Math.min(Math.max(acquisitionPercent, 0), 100)}>
            <span style={acquisitionPercent == null ? undefined : { width: `${Math.min(Math.max(acquisitionPercent, 0), 100)}%` }} />
          </div> : null}
        </section>
      ) : null}
      {!terminal && !(radarType === "Equity" && acquisition && phase === "BASELINE") ? (
        <div className={`bar-track${percent == null ? " bar-track-indeterminate" : ""}`} role="progressbar" aria-label={radarType === "Equity" && phase === "DEEP" ? "Deep processing" : "Research phase progress"}
          aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent == null ? undefined : Math.min(Math.max(percent, 0), 100)}>
          <span style={percent == null ? undefined : { width: `${Math.min(Math.max(percent, 0), 100)}%` }} />
        </div>
      ) : null}
      {counters.length ? (
        <dl className="radar-progress-counters">
          {/* RADAR UI + API ROUTING closure -- every counter the caller
              includes is rendered, never silently dropped: a real number
              when the backend provided one, or the literal word
              "Unavailable" when it genuinely does not (never a fabricated
              zero). Callers decide which counters are even worth listing
              for a given radar/phase; this panel just never hides one of
              them once listed. */}
          {counters.map((c) => (
            <div key={c.label}><dt>{c.label}</dt><dd>{c.value != null ? c.value : "Unavailable"}</dd></div>
          ))}
        </dl>
      ) : null}
      <p className="radar-progress-meta">
        {startedAt ? `Started ${new Date(startedAt).toLocaleTimeString()}` : null}
        {updatedAt ? ` · Updated ${new Date(updatedAt).toLocaleTimeString()}` : null}
      </p>
      {status === "FAILED" ? (
        <p role="alert">
          {radarType} radar cycle failed{errorCode ? ` (${errorCode.replaceAll("_", " ")})` : ""}. This is a technical
          failure, not an investment assessment.
        </p>
      ) : null}
      {status === "CANCELLED" ? <p>{radarType} radar cycle was cancelled.</p> : null}
      {pollError ? <p role="alert">Could not refresh status just now -- showing the last known state.</p> : null}
    </div>
  );
}
