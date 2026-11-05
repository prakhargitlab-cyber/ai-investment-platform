import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import {
  React,
  renderToStaticMarkup,
  loadEtfModule,
  candidate,
  cycle,
} from "./etf-test-helpers.mjs";
const { EtfRadarContent, EtfPhaseProgressPanel } = loadEtfModule();

test("compact overview partitions every assessed outcome once and separates evidence from score coverage", () => {
  const data = cycle([
    candidate(),
    candidate({
      global_instrument_id: "b",
      disposition: "INSUFFICIENT_DATA",
      rank: null,
    }),
    candidate({
      global_instrument_id: "c",
      disposition: "TECHNICAL_FAILURE",
      rank: null,
    }),
  ]);
  const html = renderToStaticMarkup(
    React.createElement(EtfRadarContent, { data }),
  );
  assert.match(html, /Research outcomes: 3 assessed ETFs/);
  assert.match(html, /Each of 3 assessed ETFs appears once/);
  assert.match(html, /Core fund evidence complete/);
  assert.match(html, /not fund evidence completeness/);
  assert.match(html, /Peer categories/);
  assert.doesNotMatch(html, /etf-selected-analysis/);
});
test("old favorable entry does not inflate verified entry overview counts", () => {
  const html = renderToStaticMarkup(
    React.createElement(EtfRadarContent, {
      data: cycle([
        candidate({
          entry_assessment: { classification: "FAVORABLE_ENTRY_ZONE" },
        }),
      ]),
    }),
  );
  assert.match(html, /Favorable entry conditions recorded<\/dt><dd>0/);
});
test("responsive ETF styles have compact desktop columns and mobile reflow without hiding data columns", () => {
  const css = fs.readFileSync(
    new URL("../app/styles.css", import.meta.url),
    "utf8",
  );
  assert.match(
    css,
    /\.etf-overview-grid \{[^}]*grid-template-columns: 1\.25fr 1fr 1fr/,
  );
  assert.match(css, /@media \(max-width: 700px\)/);
  assert.match(
    css,
    /\.etf-overview-grid, \.etf-quality-grid \{ grid-template-columns: minmax\(0,1fr\)/,
  );
  assert.match(css, /\.etf-table-toolbar > label \{ flex: 1 1 100%/);
  assert.match(css, /\.etf-table-scroll:focus-visible/);
  assert.doesNotMatch(css, /\.etf-data-table[^}]*display: none/);
});
test("all eight phases are present for legacy state with unknown counts rather than zero success", () => {
  const html = renderToStaticMarkup(
    React.createElement(EtfPhaseProgressPanel, {}),
  );
  assert.equal((html.match(/Not recorded/g) || []).length, 8);
  assert.match(html, /universe discovery/);
  assert.match(html, /fundamental acquisition/);
  assert.match(html, /publication/);
  assert.doesNotMatch(html, />0</);
});
test("phase counters preserve real zero, attempted/retry/failed/reused/skipped distinctions without synthetic percentages", () => {
  const progress = {
    current_phase: "HISTORY_ONBOARDING",
    phases: [
      {
        phase: "HISTORY_ONBOARDING",
        observed: true,
        total: 5,
        pending: 1,
        attempted: 4,
        attempt_count: 6,
        succeeded: 0,
        failed: 1,
        failed_attempts: 2,
        skipped: 1,
        reused: 2,
        remaining: 1,
        in_progress: 0,
      },
    ],
  };
  const html = renderToStaticMarkup(
    React.createElement(EtfPhaseProgressPanel, { progress }),
  );
  assert.match(html, /succeeded<\/dt><dd>0/);
  assert.match(html, /Attempts including retries: 6/);
  assert.match(html, /Failed attempts: 2/);
  assert.match(html, /reused<\/dt><dd>2/);
  assert.doesNotMatch(html, /aria-valuenow/);
});
