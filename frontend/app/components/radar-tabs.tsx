"use client";

import { useState } from "react";
import { OpportunityRadar } from "./opportunity-radar";
import { EtfRadar } from "./etf-radar";

/**
 * Dashboard-level [Equity Radar][ETF Radar] tab switch.
 *
 * Equity Radar behavior is unchanged: OpportunityRadar (opportunity-radar.tsx)
 * is rendered exactly as it always was, un-modified, as one of the two tabs.
 * ETF Radar is the new, separate EtfRadar component. Only the active tab is
 * mounted, so loading/error/empty/result state, and the refresh action, for
 * one radar can never leak into or be mixed with the other's.
 */
export function RadarTabs({ heldIds, watchlistedIds }: { heldIds: string[]; watchlistedIds: string[] }) {
  const [tab, setTab] = useState<"equity" | "etf">("equity");
  return (
    <div className="radar-tabs">
      <div className="radar-tab-switch" role="tablist" aria-label="Radar type">
        <button
          type="button"
          role="tab"
          aria-selected={tab === "equity"}
          className={tab === "equity" ? "radar-tab radar-tab-active" : "radar-tab"}
          onClick={() => setTab("equity")}
        >
          Equity Radar
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "etf"}
          className={tab === "etf" ? "radar-tab radar-tab-active" : "radar-tab"}
          onClick={() => setTab("etf")}
        >
          ETF Radar
        </button>
      </div>
      {tab === "equity" ? <OpportunityRadar heldIds={heldIds} watchlistedIds={watchlistedIds} /> : <EtfRadar />}
    </div>
  );
}
