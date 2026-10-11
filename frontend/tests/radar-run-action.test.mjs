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
  assert.match(opportunityRadarSource, /`\/api\/v1\/research\/opportunities\/cycles\/\$\{cycleId\}\/status`/);
  assert.match(etfRadarSource, /`\/api\/v1\/etf-radar\/cycles\/\$\{cycleId\}\/status`/);
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

test('ETF progress never fabricates counters while RUNNING (single-pass cycle has no live per-instrument progress)', () => {
  assert.match(etfRadarSource, /const terminalData = activeCycle\?\.status === "COMPLETED" && data\?\.cycle_id === activeCycle\.cycleId \? data : null;/);
  assert.match(etfRadarSource, /const counters: RadarProgressCounter\[\] = !terminalData \? \[\] : \[/);
  assert.match(etfRadarSource, /percent=\{null\}/);
});

test('ETF progress counters, once COMPLETED, come only from the full persisted candidates set, never the truncated top-N ranked list', () => {
  assert.match(etfRadarSource, /terminalData\.candidates\.length/);
  assert.match(etfRadarSource, /terminalData\.candidates\.filter\(\(c\) => c\.disposition === "EVALUATED"\)\.length/);
  assert.match(etfRadarSource, /terminalData\.candidates\.filter\(\(c\) => c\.disposition === "TECHNICAL_FAILURE"\)\.length/);
  assert.match(etfRadarSource, /terminalData\.candidates\.filter\(\(c\) => c\.disposition === "INSUFFICIENT_DATA"\)\.length/);
  assert.match(etfRadarSource, /terminalData\.candidates\.filter\(\(c\) => c\.disposition === "INELIGIBLE" \|\| c\.disposition === "UNSUPPORTED"\)\.length/);
  assert.match(etfRadarSource, /terminalData\.candidates\.filter\(\(c\) => c\.recommendation === "AVOID"\)\.length/);
  // ETF's recommendation vocabulary has no literal BUY/SELL/HOLD -- never invented.
  assert.match(etfRadarSource, /\{ label: "BUY count", value: null \}/);
  assert.match(etfRadarSource, /\{ label: "SELL count", value: null \}/);
  assert.match(etfRadarSource, /\{ label: "HOLD\/neutral count", value: null \}/);
  assert.match(etfRadarSource, /\{ label: "Pod restarts", value: null \}/);
});

test('Equity progress counters and percentage come only from authoritative_progress, filtered to real values', () => {
  assert.match(opportunityRadarSource, /authoritative_progress\?: EquityAuthoritativeProgress/);
  // radar-progress.tsx now renders every listed counter (never hides one),
  // substituting the literal "Unavailable" for a null value instead of
  // filtering it out of the list.
  assert.match(progressSource, /\{c\.value != null \? c\.value : "Unavailable"\}/);
});

test('Equity action counts remain unavailable without cycle-associated authoritative totals', () => {
  assert.match(opportunityRadarSource, /\{ label: "BUY count", value: null \}/);
  assert.match(opportunityRadarSource, /\{ label: "Exit \(Sell\/Partial\) count", value: null \}/);
});

test('a technical/FAILED cycle is never presented as a poor-investment recommendation', () => {
  assert.match(progressSource, /technical\s+failure, not an investment assessment/);
});

test('OpportunityRadar and EtfRadar each own independent activeCycle state -- no shared module-level state', () => {
  assert.doesNotMatch(opportunityRadarSource, /from ["']\.\/etf-radar["']/);
  assert.doesNotMatch(etfRadarSource, /from ["']\.\/opportunity-radar["']/);
});

// RADAR UI + API ROUTING -- item F.5, page-refresh recovery ---------------

test('a page refresh recovers an in-progress Equity cycle from sessionStorage and resumes polling its real status endpoint', () => {
  assert.match(
    opportunityRadarSource,
    /storedCycleId = window\.sessionStorage\.getItem\(EQUITY_ACTIVE_CYCLE_STORAGE_KEY\);/
  );
  assert.match(
    opportunityRadarSource,
    /if \(storedCycleId\) setActiveCycle\(previous => previous \?\? \{ cycleId: storedCycleId, status: "RUNNING",/
  );
  // The restored cycle re-enters the SAME poll effect (keyed on
  // activeCycle?.cycleId), so it is never a dead/static placeholder -- it
  // immediately starts polling the real backend status for that cycle_id.
  assert.match(opportunityRadarSource, /\}, \[cycleId\]\);/);
});

test('a page refresh recovers an in-progress ETF cycle from sessionStorage and resumes polling its real status endpoint', () => {
  assert.match(
    etfRadarSource,
    /storedCycleId = window\.sessionStorage\.getItem\(ETF_ACTIVE_CYCLE_STORAGE_KEY\);/
  );
  assert.match(
    etfRadarSource,
    /if \(storedCycleId\) setActiveCycle\(previous => previous \?\? \{ cycleId: storedCycleId, status: "RUNNING",/
  );
  assert.match(etfRadarSource, /\}, \[cycleId\]\);/);
});

test('the recovered cycle_id is cleared from sessionStorage once the cycle reaches a terminal state, so a later refresh does not resurrect a finished cycle', () => {
  assert.match(opportunityRadarSource, /window\.sessionStorage\.removeItem\(EQUITY_ACTIVE_CYCLE_STORAGE_KEY\)/);
  assert.match(etfRadarSource, /window\.sessionStorage\.removeItem\(ETF_ACTIVE_CYCLE_STORAGE_KEY\)/);
});

// Interaction regressions: execute React effects and handlers with controlled API promises.
import React from 'react';
import { create, act } from 'react-test-renderer';
import { renderToString } from 'react-dom/server';
import ts from 'typescript';
import { createRequire } from 'node:module';
const nodeRequire = createRequire(import.meta.url);
function loadComponent(filename, api = {}) {
  const cache = new Map();
  function load(file) {
    if (cache.has(file)) return cache.get(file);
    const mod = { exports: {} };
    cache.set(file, mod.exports);
    const output = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
      compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
    }).outputText;
    const require = spec => {
      if (spec.endsWith('.css')) return {};
      if (spec.includes('portfolio-api')) return { request: api.request };
      if (spec.includes('admin-api')) return { adminApi: api };
      if (spec.startsWith('.')) {
        const base = path.resolve(path.dirname(file), spec);
        return load(['.tsx', '.ts'].map(ext => base + ext).find(candidate => fs.existsSync(candidate)));
      }
      return nodeRequire(spec);
    };
    new Function('require', 'module', 'exports', output)(require, mod, mod.exports);
    return mod.exports;
  }
  return load(path.join(dirname, '../app/components', filename));
}
const textOf = tree => JSON.stringify(tree.toJSON());
const button = (tree, label) => tree.root.findAllByType('button').find(node => node.children.join('') === label);
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const settle = async fn => { await act(async () => { fn?.(); await Promise.resolve(); }); };

test('recommendation page 2 survives rerenders and resets on changed list membership', async () => {
  const { PreviousRecommendationsList } = loadComponent('opportunity-radar.tsx');
  const items = Array.from({ length: 21 }, (_, i) => ({ global_instrument_id: String(i).padStart(2, '0'), company_name: `Company ${i}`, current_short_action: 'BUY', current_long_action: 'BUY' }));
  let tree;
  await settle(() => { tree = create(React.createElement(PreviousRecommendationsList, { items })); });
  await settle(() => button(tree, 'Next').props.onClick());
  assert.match(textOf(tree), /Company 10/);
  assert.ok(!tree.root.findAllByType('li').some(node => node.children[0] === 'Company 0'));
  await settle(() => tree.update(React.createElement(PreviousRecommendationsList, { items: [...items].reverse() })));
  assert.match(textOf(tree), /Company 10/);
  await settle(() => tree.update(React.createElement(PreviousRecommendationsList, { items: items.map((item, i) => i === 0 ? { ...item, global_instrument_id: 'new' } : item) })));
  assert.equal(tree.root.findAllByType('li')[0].children[0], 'Company 1');
  await settle(() => tree.unmount());
});

test('ADMIN hides stale users and audit immediately, cancels obsolete requests, and handles denied access', async () => {
  const usersRequests = [], auditRequests = [];
  const user = id => ({ userId: id, email: `${id}@example.test`, displayName: id, roles: [], status: 'ACTIVE', createdAt: '2026-01-01', emailVerifiedAt: '2026-01-01' });
  const page = content => ({ content, totalElements: 4, totalPages: 2 });
  const { AdminUsers } = loadComponent('admin-users.tsx', {
    users: () => { const pending = deferred(); usersRequests.push(pending); return pending.promise; },
    user: id => Promise.resolve(user(id)),
    audit: () => { const pending = deferred(); auditRequests.push(pending); return pending.promise; }
  });
  let tree;
  await settle(() => { tree = create(React.createElement(AdminUsers, { operatorId: 'operator', roles: ['ADMIN'] })); });
  await settle(() => usersRequests[0].resolve(page([user('alice')])));
  await settle(() => button(tree, 'Details & roles').props.onClick());
  await settle(() => auditRequests[0].resolve(page([{ id: '1', action: 'GRANT', role: 'ADMIN', reason: 'old audit', createdAt: '2026-01-01' }])));
  assert.match(textOf(tree), /old audit/);
  await settle(() => button(tree, 'Next audit records').props.onClick());
  assert.doesNotMatch(textOf(tree), /old audit/);
  assert.match(textOf(tree), /Loading history/);
  await settle(() => button(tree, 'Next users').props.onClick());
  assert.match(textOf(tree), /Loading users/);
  await settle(() => tree.root.findByType('input').props.onChange({ target: { value: 'bob' } }));
  await settle(() => tree.root.findAllByType('form')[0].props.onSubmit({ preventDefault() {} }));
  await settle(() => usersRequests[1].resolve(page([user('obsolete')])));
  assert.doesNotMatch(textOf(tree), /obsolete/);
  await settle(() => usersRequests[2].resolve(page([user('bob')])));
  await settle(() => button(tree, 'Details & roles').props.onClick());
  await settle(() => auditRequests[1].resolve(page([{ id: 'late', reason: 'obsolete audit' }])));
  assert.doesNotMatch(textOf(tree), /obsolete audit/);
  await settle(() => auditRequests[2].reject({ status: 403, message: 'denied' }));
  assert.match(textOf(tree), /Administrator access is no longer available/);
  assert.doesNotMatch(textOf(tree), /bob@example/);
  await settle(() => tree.unmount());
});

for (const [name, filename, key, endpoint] of [
  ['EtfRadar', 'etf-radar.tsx', 'aip.etfRadar.activeCycleId', '/api/v1/etf-radar'],
  ['OpportunityRadar', 'opportunity-radar.tsx', 'aip.opportunityRadar.activeCycleId', '/api/v1/research/opportunities']
]) {
  for (const terminalStatus of ['FAILED', 'CANCELLED']) {
    test(`${name}: ${terminalStatus} stops polling without refetching results`, async () => {
      const originalWindow = globalThis.window;
      const store = new Map([[key, 'terminal-cycle']]);
      const calls = [];
      let tree;
      try {
        globalThis.window = { sessionStorage: { getItem: k => store.get(k), removeItem: k => store.delete(k) } };
        const Component = loadComponent(filename, { request: url => {
          const pending = deferred(); calls.push({ url, ...pending }); return pending.promise;
        } })[name];
        await settle(() => { tree = create(React.createElement(Component, { heldIds: [], watchlistedIds: [] })); });
        await settle(() => calls[1].resolve({ status: terminalStatus }));
        assert.equal(store.has(key), false);
        assert.equal(calls.length, 2);
        assert.equal(tree.root.findAllByType('button')[0].props.disabled, false);
        assert.doesNotMatch(textOf(tree), /Total universe.*candidates/);
        await settle(() => tree.unmount());
      } finally { globalThis.window = originalWindow; }
    });
  }
  test(`${name}: SSR parity, refresh recovery, retry polling, terminal cleanup and cancellation`, async () => {
    const originalWindow = globalThis.window, originalTimeout = globalThis.setTimeout, originalClear = globalThis.clearTimeout;
    const store = new Map([[key, 'restored'], ['unrelated', 'keep']]);
    const storage = { getItem: key => store.get(key), setItem: (key, value) => store.set(key, value), removeItem: key => store.delete(key) };
    const calls = [], timers = new Map(); let sequence = 0;
    const data = name === 'EtfRadar' ? { cycle_id: 'older', as_of: '2026-01-01', candidates: [], ranked: [] } : { generated_at: null, best_buy_today: null, top_short_term: [], top_long_term: [] };
    const Component = loadComponent(filename, { request: url => {
      const pending = deferred(); calls.push({ url, ...pending }); return pending.promise;
    } })[name];
    const props = { heldIds: [], watchlistedIds: [] };
    let tree;
    try {
      delete globalThis.window;
      const server = renderToString(React.createElement(Component, props));
      globalThis.window = { sessionStorage: storage };
      assert.equal(renderToString(React.createElement(Component, props)), server);
      globalThis.setTimeout = fn => { const id = ++sequence; timers.set(id, fn); return id; };
      globalThis.clearTimeout = id => timers.delete(id);
      await settle(() => { tree = create(React.createElement(Component, props)); });
      assert.equal(calls[1].url, `${endpoint}/cycles/restored/status`);
      await settle(() => calls[0].resolve(data));
      await settle(() => calls[1].reject(new Error('offline')));
      assert.match(textOf(tree), /Could not refresh/);
      assert.equal(timers.size, 1);
      const tick = async () => { const [id, fn] = [...timers][0]; timers.delete(id); await settle(fn); };
      await tick();
      await settle(() => calls[2].resolve({ status: 'RUNNING', authoritative_progress: { deep_completed: 2, deep_denominator: 4 } }));
      assert.equal(timers.size, 1);
      await tick();
      await settle(() => calls[3].resolve({ status: 'COMPLETED' }));
      assert.equal(timers.size, 0);
      assert.equal(store.has(key), false);
      assert.equal(store.get('unrelated'), 'keep');
      if (name === 'EtfRadar') assert.doesNotMatch(textOf(tree), /Total universe/);
      await settle(() => calls[4].resolve({ ...data, cycle_id: 'restored' }));
      if (name === 'EtfRadar') assert.match(textOf(tree), /Total universe/);
      await settle(() => tree.unmount());
      store.set(key, 'second');
      await settle(() => { tree = create(React.createElement(Component, props)); });
      const late = calls.at(-1);
      await settle(() => tree.unmount());
      await settle(() => late.resolve({ status: 'RUNNING' }));
      assert.equal(timers.size, 0);
    } finally {
      globalThis.window = originalWindow;
      globalThis.setTimeout = originalTimeout;
      globalThis.clearTimeout = originalClear;
    }
  });
}
