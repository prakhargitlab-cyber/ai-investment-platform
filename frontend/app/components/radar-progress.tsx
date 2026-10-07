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
  errorCode?: string | null;
  /** Set when the most recent status poll itself failed (transient network
   * error) -- the panel keeps showing the last known good state rather than
   * going blank or stuck. */
  pollError?: boolean;
}) {
  const terminal = isTerminalRadarStatus(status);
  const tone = status === "FAILED" ? "failed" : status === "CANCELLED" ? "cancelled" : status === "COMPLETED" ? "completed" : "active";
  return (
    <div className={`radar-progress-panel radar-progress-${tone}`} role="status" aria-live="polite">
      <div className="radar-progress-heading">
        <strong>{radarType} radar · {statusLabel(status)}</strong>
        <small>Cycle {cycleId}</small>
      </div>
      {!terminal ? (
        <div className={`bar-track${percent == null ? " bar-track-indeterminate" : ""}`} aria-hidden="true">
          <span style={percent == null ? undefined : { width: `${Math.min(Math.max(percent, 0), 100)}%` }} />
        </div>
      ) : null}
      {counters.length ? (
        <dl className="radar-progress-counters">
          {counters.filter((c) => c.value != null).map((c) => (
            <div key={c.label}><dt>{c.label}</dt><dd>{c.value}</dd></div>
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
