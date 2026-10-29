"use client";

import { useEffect, useRef, useState } from "react";
import { request } from "../lib/portfolio-api";
import { isTerminalRadarStatus } from "./radar-progress";
import { normalizeRadar, type Radar } from "./opportunity-radar";
import type { EtfCycleResult } from "./etf-radar";
import { Card, Badge, Button } from "./ui";

const EQUITY_ACTIVE_CYCLE_STORAGE_KEY = "aip.opportunityRadar.activeCycleId";
const ETF_ACTIVE_CYCLE_STORAGE_KEY = "aip.etfRadar.activeCycleId";

// Deliberately slower than the dedicated Equity/ETF Radar page's own 2.5s
// poll, so the two never collide on exactly the same tick when a person has
// both the Dashboard and a dedicated Radar page open at once -- a residual,
// documented limitation: a genuinely shared status cache (per Priority 4's
// "existing shared status state" alternative) would remove this gap
// entirely, but is a larger change than this pass makes.
const DASHBOARD_RADAR_SUMMARY_POLL_MS = 6000;

type CycleStatusSnapshot = { status: string; updated_at?: string } | null;

/**
 * Compact, read-only Dashboard summary for both Radars.
 *
 * This deliberately does NOT mount OpportunityRadar/EtfRadar -- their full
 * tabbed research terminals, search/filter state, and own polling loops --
 * inside the Dashboard. Each card makes its own small, independent, bounded
 * poll of the Radar's existing lightweight status endpoint only while a
 * cycle is genuinely running (never once it reaches a terminal status),
 * and always clears its timer on unmount. "Open Equity/ETF Radar" navigates
 * to the dedicated, lazily-mounted page for the full live research terminal.
 */
export function RadarTabs({ heldIds: _heldIds, watchlistedIds: _watchlistedIds, roles: _roles = [], onOpenEquityRadar, onOpenEtfRadar }: {
  heldIds: string[];
  watchlistedIds: string[];
  roles?: string[];
  onOpenEquityRadar?: () => void;
  onOpenEtfRadar?: () => void;
}) {
  return (
    <div className="radar-summary-panels">
      <EquityRadarSummaryCard onOpen={onOpenEquityRadar} />
      <EtfRadarSummaryCard onOpen={onOpenEtfRadar} />
    </div>
  );
}

function EquityRadarSummaryCard({ onOpen }: { onOpen?: () => void }) {
  const [radar, setRadar] = useState<Radar | null>(null);
  const [cycle, setCycle] = useState<CycleStatusSnapshot>(null);
  const [error, setError] = useState(false);
  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    let active = true;
    request<Radar>("/api/v1/research/opportunities/current")
      .then(normalizeRadar)
      .then((value) => { if (active) setRadar(value); })
      .catch(() => { if (active) setError(true); });

    let cycleId: string | null = null;
    try { cycleId = window.sessionStorage.getItem(EQUITY_ACTIVE_CYCLE_STORAGE_KEY); } catch { /* sessionStorage unavailable */ }

    const poll = () => {
      if (!cycleId) return;
      request<{ status: string; updated_at?: string }>(`/api/v1/research/opportunities/cycles/${cycleId}/status`)
        .then((value) => {
          if (!active) return;
          setCycle(value);
          if (isTerminalRadarStatus(value.status)) {
            // Stop polling once terminal; refresh the published-results
            // summary one last time so the card reflects the finished cycle.
            request<Radar>("/api/v1/research/opportunities/current").then(normalizeRadar)
              .then((result) => { if (active) setRadar(result); }).catch(() => { /* keep last known summary */ });
            return;
          }
          pollTimer.current = setTimeout(poll, DASHBOARD_RADAR_SUMMARY_POLL_MS);
        })
        .catch(() => { /* a stale/expired cycle id, or a transient error, is not worth surfacing here */ });
    };
    if (cycleId) poll();

    return () => { active = false; if (pollTimer.current) clearTimeout(pollTimer.current); };
  }, []);

  const publishedCount = (radar?.top_short_term_count ?? 0) + (radar?.top_long_term_count ?? 0);
  const running = Boolean(cycle) && !isTerminalRadarStatus(cycle?.status);

  return (
    <Card className="radar-summary-card">
      <div className="panel-header compact">
        <h3>Equity Radar</h3>
        {cycle ? <Badge tone={running ? "info" : cycle?.status === "FAILED" ? "negative" : "positive"}>{(cycle?.status ?? "").replaceAll("_", " ")}</Badge> : null}
      </div>
      {error ? <p className="broker-note">Radar summary is not available right now.</p>
        : !radar ? <p className="broker-note">Loading summary…</p>
        : (
        <dl className="radar-summary-stats">
          <div><dt>Published opportunities</dt><dd>{publishedCount}</dd></div>
          <div><dt>Last published</dt><dd>{radar.generated_at ? new Date(radar.generated_at).toLocaleDateString() : "No persisted cycle yet"}</dd></div>
        </dl>
      )}
      <Button variant="secondary" onClick={onOpen}>Open Equity Radar</Button>
    </Card>
  );
}

function EtfRadarSummaryCard({ onOpen }: { onOpen?: () => void }) {
  const [radar, setRadar] = useState<EtfCycleResult | null>(null);
  const [cycle, setCycle] = useState<CycleStatusSnapshot>(null);
  const [error, setError] = useState(false);
  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    let active = true;
    request<EtfCycleResult>("/api/v1/etf-radar/current")
      .then((value) => { if (active) setRadar(value); })
      .catch(() => { if (active) setError(true); });

    let cycleId: string | null = null;
    try { cycleId = window.sessionStorage.getItem(ETF_ACTIVE_CYCLE_STORAGE_KEY); } catch { /* sessionStorage unavailable */ }

    const poll = () => {
      if (!cycleId) return;
      request<{ status: string; updated_at?: string }>(`/api/v1/etf-radar/cycles/${cycleId}/status`)
        .then((value) => {
          if (!active) return;
          setCycle(value);
          if (isTerminalRadarStatus(value.status)) {
            request<EtfCycleResult>("/api/v1/etf-radar/current")
              .then((result) => { if (active) setRadar(result); }).catch(() => { /* keep last known summary */ });
            return;
          }
          pollTimer.current = setTimeout(poll, DASHBOARD_RADAR_SUMMARY_POLL_MS);
        })
        .catch(() => { /* a stale/expired cycle id, or a transient error, is not worth surfacing here */ });
    };
    if (cycleId) poll();

    return () => { active = false; if (pollTimer.current) clearTimeout(pollTimer.current); };
  }, []);

  const publishedCount = radar?.top_opportunities?.length ?? radar?.ranked?.length ?? 0;
  const running = Boolean(cycle) && !isTerminalRadarStatus(cycle?.status);

  return (
    <Card className="radar-summary-card">
      <div className="panel-header compact">
        <h3>ETF Radar</h3>
        {cycle ? <Badge tone={running ? "info" : cycle?.status === "FAILED" ? "negative" : "positive"}>{(cycle?.status ?? "").replaceAll("_", " ")}</Badge> : null}
      </div>
      {error ? <p className="broker-note">Radar summary is not available right now.</p>
        : !radar ? <p className="broker-note">Loading summary…</p>
        : (
        <dl className="radar-summary-stats">
          <div><dt>Universe coverage</dt><dd>{radar.universe_count ?? "—"}</dd></div>
          <div><dt>Published opportunities</dt><dd>{publishedCount}</dd></div>
          <div><dt>Last published</dt><dd>{radar.as_of ? new Date(radar.as_of).toLocaleDateString() : "No persisted cycle yet"}</dd></div>
        </dl>
      )}
      <Button variant="secondary" onClick={onOpen}>Open ETF Radar</Button>
    </Card>
  );
}
