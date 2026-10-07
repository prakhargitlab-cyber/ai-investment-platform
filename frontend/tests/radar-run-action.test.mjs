import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const dirname = path.dirname(fileURLToPath(import.meta.url));
const opportunityRadarSource = fs.readFileSync(path.join(dirname, '../app/components/opportunity-radar.tsx'), 'utf8');
const etfRadarSource = fs.readFileSync(path.join(dirname, '../app/components/etf-radar.tsx'), 'utf8');
const progressSource = fs.readFileSync(path.join(dirname, '../app/components/radar-progress.tsx'), 'utf8');

// Defect 3 -------------------------------------------------------------

test('Run Equity Radar posts only to the Equity cycles endpoint, never the ETF one', () => {
  assert.match(opportunityRadarSource, /"\/api\/v1\/research\/opportunities\/cycles"/);
  assert.doesNotMatch(opportunityRadarSource, /\/api\/v1\/etf-radar\//);
});

test('Run ETF Radar posts only to the ETF cycles endpoint, never the Equity one', () => {
  assert.match(etfRadarSource, /"\/api\/v1\/etf-radar\/cycles"/);
  assert.doesNotMatch(etfRadarSource, /\/api\/v1\/research\/opportunities\/cycles"/);
});

test('the Equity run action never sends analysis_scope, so it can never request FULL', () => {
  const runCycleBody = opportunityRadarSource.slice(
    opportunityRadarSource.indexOf('const runCycle = () => {'),
    opportunityRadarSource.indexOf('const running = !!activeCycle')
  );
  assert.doesNotMatch(runCycleBody, /JSON\.stringify\(\{[^}]*analysis_scope/);
  assert.match(runCycleBody, /JSON\.stringify\(\{\}\)/);
  assert.doesNotMatch(runCycleBody, /['"]FULL['"]/);
});

test('both run buttons are disabled while their own cycle is non-terminal (no double submit)', () => {
  assert.match(opportunityRadarSource, /disabled=\{running\}/);
  assert.match(etfRadarSource, /disabled=\{running\}/);
  assert.match(opportunityRadarSource, /const running = !!activeCycle && !isTerminalRadarStatus\(activeCycle\.status\);/);
  assert.match(etfRadarSource, /const running = !!activeCycle && !isTerminalRadarStatus\(activeCycle\.status\);/);
});

test('button labels are consistently cased: "Run Equity Radar" / "Run ETF Radar"', () => {
  assert.match(opportunityRadarSource, /Run Equity Radar/);
  assert.match(etfRadarSource, /Run ETF Radar/);
  assert.doesNotMatch(opportunityRadarSource, /Run ETF Radar/);
  assert.doesNotMatch(etfRadarSource, /Run Equity Radar/);
});

// Defect 4 -------------------------------------------------------------

test('Equity and ETF active-cycle state use separate sessionStorage keys, never shared', () => {
  assert.match(opportunityRadarSource, /EQUITY_ACTIVE_CYCLE_STORAGE_KEY = "aip\.opportunityRadar\.activeCycleId"/);
  assert.match(etfRadarSource, /ETF_ACTIVE_CYCLE_STORAGE_KEY = "aip\.etfRadar\.activeCycleId"/);
  assert.notEqual(
    opportunityRadarSource.match(/EQUITY_ACTIVE_CYCLE_STORAGE_KEY = "([^"]+)"/)[1],
    etfRadarSource.match(/ETF_ACTIVE_CYCLE_STORAGE_KEY = "([^"]+)"/)[1]
  );
});

test('each radar polls only its own status endpoint, scoped to its own cycle_id', () => {
  assert.match(opportunityRadarSource, /`\/api\/v1\/research\/opportunities\/cycles\/\$\{activeCycle\.cycleId\}\/status`/);
  assert.match(etfRadarSource, /`\/api\/v1\/etf-radar\/cycles\/\$\{activeCycle\.cycleId\}\/status`/);
});

test('polling stops once the lifecycle status is terminal, and never fabricates a percentage', () => {
  assert.match(opportunityRadarSource, /if \(isTerminalRadarStatus\(value\.status\)\) \{/);
  assert.match(etfRadarSource, /if \(isTerminalRadarStatus\(value\.status\)\) \{/);
  assert.match(progressSource, /TERMINAL_RADAR_STATUSES: ReadonlySet<RadarLifecycleStatus> = new Set\(\[\s*"COMPLETED",\s*"FAILED",\s*"CANCELLED",?\s*\]\);/);
  // No setInterval/timer-driven fake percentage anywhere in the shared panel.
  assert.doesNotMatch(progressSource, /setInterval/);
  assert.doesNotMatch(progressSource, /Math\.random/);
});

test('a failed status poll keeps the panel showing the last known state rather than going blank or stuck', () => {
  assert.match(opportunityRadarSource, /pollError: true/);
  assert.match(etfRadarSource, /pollError: true/);
  assert.match(progressSource, /Could not refresh status just now/);
});

test('ETF progress never fabricates counters it does not have (single-pass cycle has none)', () => {
  assert.match(etfRadarSource, /const counters: RadarProgressCounter\[\] = \[\];/);
  assert.match(etfRadarSource, /percent=\{null\}/);
});

test('Equity progress counters and percentage come only from authoritative_progress, filtered to real values', () => {
  assert.match(opportunityRadarSource, /authoritative_progress\?: EquityAuthoritativeProgress/);
  assert.match(progressSource, /counters\.filter\(\(c\) => c\.value != null\)/);
});

test('a technical/FAILED cycle is never presented as a poor-investment recommendation', () => {
  assert.match(progressSource, /technical\s+failure, not an investment assessment/);
});

test('OpportunityRadar and EtfRadar each own independent activeCycle state -- no shared module-level state', () => {
  assert.doesNotMatch(opportunityRadarSource, /from ["']\.\/etf-radar["']/);
  assert.doesNotMatch(etfRadarSource, /from ["']\.\/opportunity-radar["']/);
});
