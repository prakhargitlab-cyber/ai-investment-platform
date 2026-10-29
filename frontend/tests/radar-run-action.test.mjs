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

test('Run full NSE ETF Radar posts only to the full-universe ETF endpoint, never the Equity one', () => {
  assert.match(etfRadarSource, /"\/api\/v1\/etf-radar\/full-universe-cycles"/);
  assert.doesNotMatch(etfRadarSource, /\/api\/v1\/research\/opportunities\/cycles"/);
});

// Superseded: the Equity run action now explicitly requests the FULL
// analysis scope on every run (see opportunity-radar.tsx's runCycle,
// which documents why: the server-side OpportunityCycleRequest default
// must never silently decide this request's scope). This is a confirmed,
// intentional product requirement -- verified against the real
// diagnostic cycle 1029d5e7-3797-4567-852e-deb869ed88da, which recorded a
// FULL-universe scan of the entire 2,605-instrument NSE equity universe --
// not a regression of the original Defect 3/4 fix below, which remains
// intact (each radar still posts only to its own endpoint, with its own
// storage key and status poll).
test('the Equity run action explicitly requests the FULL analysis scope on every run', () => {
  const runCycleBody = opportunityRadarSource.slice(
    opportunityRadarSource.indexOf('const runCycle = () => {'),
    opportunityRadarSource.indexOf('const running = !!activeCycle')
  );
  assert.match(runCycleBody, /JSON\.stringify\(\{\s*analysis_scope:\s*"FULL"\s*\}\)/);
});

test('both run buttons are disabled while their own cycle is non-terminal (no double submit)', () => {
  assert.match(opportunityRadarSource, /disabled=\{running \|\| discoveryLoading\}/);
  assert.match(etfRadarSource, /disabled=\{running\}/);
  assert.match(opportunityRadarSource, /const running = !!activeCycle && !isTerminalRadarStatus\(activeCycle\.status\);/);
  assert.match(etfRadarSource, /const running = !!activeCycle && !isTerminalRadarStatus\(activeCycle\.status\);/);
});

test('button labels are consistently cased: "Run Equity Radar" / "Run full NSE ETF Radar"', () => {
  assert.match(opportunityRadarSource, /Run Equity Radar/);
  assert.match(etfRadarSource, /Run full NSE ETF Radar/);
  assert.doesNotMatch(opportunityRadarSource, /Run full NSE ETF Radar/);
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
  assert.match(opportunityRadarSource, /`\/api\/v1\/research\/opportunities\/cycles\/\$\{encodeURIComponent\(id\)\}\/status`/);
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

test('ETF live counters require durable coverage; legacy cycles stay indeterminate', () => {
  assert.match(etfRadarSource, /const terminalData = activeCycle\?\.status === "COMPLETED" && data\?\.cycle_id === activeCycle\.cycleId \? data : null;/);
  assert.match(etfRadarSource, /activeCycle\?\.coverage \? \[/);
  assert.match(etfRadarSource, /value: activeCycle\.coverage\.official/);
  assert.match(etfRadarSource, /value: activeCycle\.coverage\.completed/);
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

test('active keys are cleared on terminal status; Equity has no persisted history selection', () => {
  assert.match(opportunityRadarSource, /window\.sessionStorage\.removeItem\(EQUITY_ACTIVE_CYCLE_STORAGE_KEY\)/);
  assert.match(etfRadarSource, /window\.sessionStorage\.removeItem\(ETF_ACTIVE_CYCLE_STORAGE_KEY\)/);
  assert.doesNotMatch(opportunityRadarSource, /selectedCycleId|Cycle history|<select/);
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

const equitySelectedKey = 'aip.opportunityRadar.selectedCycleId';
const equityLegacyKey = 'aip.opportunityRadar.activeCycleId';
const cycleSummary = (id, status) => ({ cycle_id: id, status,
  created_at: '2026-10-17T11:00:00Z', updated_at: '2026-10-17T16:52:29Z' });
const savedProgress = {
  phase: 'CANCELLED', universe_count: 2605, baseline_evaluated: 2605,
  baseline_completed: 2003, baseline_failed: 0, baseline_awaiting_retry: 595,
  baseline_in_progress: 7, baseline_not_started: 0,
  deep_attempted: 234, deep_completed: 119, deep_ready: 72,
  deep_readiness_failed: 47, deep_technical_failure: 0, technical: 113, deep_denominator: 2605,
};
const browserStorage = initial => {
  const values = new Map(Object.entries(initial ?? {}));
  return { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key) };
};

async function equityFixture(t, { history, statuses, selected, legacy, noStorage = false, onRequest } = {}) {
  const originalWindow = globalThis.window;
  const localStorage = browserStorage(selected ? { [equitySelectedKey]: selected } : {});
  const sessionStorage = browserStorage(legacy ? { [equityLegacyKey]: legacy } : {});
  const blocked = { getItem() { throw new Error('Storage disabled'); }, setItem() { throw new Error('Storage disabled'); }, removeItem() { throw new Error('Storage disabled'); } };
  globalThis.window = { localStorage: noStorage ? blocked : localStorage,
    sessionStorage: noStorage ? blocked : sessionStorage };
  const calls = [];
  const api = { request: async (url, options) => {
    calls.push({ url, method: options?.method ?? 'GET' });
    const custom = onRequest?.(url, options);
    if (custom !== undefined) return custom;
    if (url.endsWith('/current')) return { generated_at: null, top_short_term: [], top_long_term: [], previous_recommendations: [] };
    if (url === '/api/v1/research/opportunities/cycles?limit=1') return history;
    const match = url.match(/\/cycles\/([^/]+)\/status$/);
    if (match) {
      const value = statuses[decodeURIComponent(match[1])];
      if (value instanceof Error) throw value;
      if (value) return value;
      throw Object.assign(new Error('Missing cycle'), { status: 404 });
    }
    throw new Error(`Unexpected request: ${options?.method ?? 'GET'} ${url}`);
  } };
  const { OpportunityRadar } = loadComponent('opportunity-radar.tsx', api);
  let tree;
  const mount = async () => { await settle(() => { tree = create(React.createElement(OpportunityRadar,
    { heldIds: [], watchlistedIds: [], roles: ['ADMIN'] })); }); return tree; };
  await mount();
  t.after(async () => {
    await settle(() => tree.unmount());
    if (originalWindow === undefined) delete globalThis.window;
    else globalThis.window = originalWindow;
  });
  return { get tree() { return tree; }, calls, localStorage, sessionStorage,
    remount: async () => { await settle(() => tree.unmount()); await mount(); } };
}

test('bounded action is ADMIN-only and explains the deep limit and shared publication', () => {
  const { OpportunityRadar } = loadComponent('opportunity-radar.tsx');
  for (const roles of [[], ['USER'], ['ADMIN']]) {
    const html = renderToString(React.createElement(OpportunityRadar, { heldIds: [], watchlistedIds: [], roles }));
    assert.equal(html.includes('Run Bounded Test'), roles.includes('ADMIN'));
    assert.match(html, /Run Equity Radar/);
    if (roles.includes('ADMIN')) {
      assert.match(html, /limited to 1 candidate/);
      assert.match(html, /Baseline screening still covers the NSE universe/);
      assert.match(html, /Completed results update the shared Equity Radar/);
    }
  }
});

for (const terminalStatus of ['COMPLETED', 'FAILED']) {
  test(`bounded submission tracks ${terminalStatus} and refreshes only completed persisted results`, async t => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    const pending = deferred();
    const submissions = [];
    const statuses = { bounded: { status: 'RUNNING' } };
    const result = { generated_at: '2026-10-21T00:00:00Z', top_short_term: [], top_long_term: [] };
    const previousResult = { ...result, generated_at: '2026-10-20T00:00:00Z' };
    let currentReads = 0;
    const fixture = await equityFixture(t, {
      history: { cycles: [], active_cycle: null }, statuses,
      onRequest: (url, options) => {
        if (options?.method === 'POST') {
          submissions.push({ url, ...options });
          return pending.promise;
        }
        if (url.endsWith('/current')) { currentReads++; return currentReads === 1 ? previousResult : result; }
      },
    });
    const start = button(fixture.tree, 'Run Bounded Test').props.onClick;
    await settle(start);
    assert.equal(button(fixture.tree, 'Submitting bounded test…').props.disabled, true);
    await settle(start);
    assert.equal(submissions.length, 1);
    assert.equal(submissions[0].url, '/api/v1/research/opportunities/cycles');
    assert.deepEqual(JSON.parse(submissions[0].body), { analysis_scope: 'BOUNDED', shortlist_limit: 1 });
    assert.equal(submissions[0].headers, undefined);
    await settle(() => pending.resolve({ cycle_id: 'bounded', status: 'ACCEPTED' }));
    assert.equal(fixture.sessionStorage.getItem(equityLegacyKey), 'bounded');
    assert.equal(button(fixture.tree, 'Run Bounded Test').props.disabled, true);
    assert.equal(button(fixture.tree, 'Running…').props.disabled, true);
    assert.ok(fixture.calls.some(call => call.url.endsWith('/cycles/bounded/status')));

    statuses.bounded = { status: terminalStatus, error_code: terminalStatus === 'FAILED' ? 'BOUNDED_FIXTURE_FAILURE' : null };
    await settle(() => t.mock.timers.tick(2500));
    assert.equal(button(fixture.tree, 'Run Bounded Test').props.disabled, false);
    assert.equal(fixture.sessionStorage.getItem(equityLegacyKey), null);
    assert.equal(currentReads, terminalStatus === 'COMPLETED' ? 2 : 1);
    const content = fixture.tree.root.find(node => typeof node.type === 'function' && node.type.name === 'RadarContent');
    assert.equal(content.props.data.generated_at, terminalStatus === 'COMPLETED' ? result.generated_at : previousResult.generated_at);
    if (terminalStatus === 'FAILED') assert.match(textOf(fixture.tree), /BOUNDED FIXTURE FAILURE/);
    const callsAtCompletion = fixture.calls.length;
    await settle(() => t.mock.timers.tick(10000));
    assert.equal(fixture.calls.length, callsAtCompletion);
  });
}

for (const [status, message, explanation] of [
  [403, 'Forbidden', /Administrator access is required/],
  [409, 'ETF_ACTIVE_CYCLE_REQUIRES_REVIEW', /already in flight/],
  [503, 'RECOVERY_PAUSED', /manual execution is not authorized/],
]) {
  test(`bounded admission preserves the server's ${status} refusal`, async t => {
    const fixture = await equityFixture(t, {
      history: { cycles: [], active_cycle: null }, statuses: {},
      onRequest: (_url, options) => options?.method === 'POST' ? Promise.reject({ status, message }) : undefined,
    });
    await settle(button(fixture.tree, 'Run Bounded Test').props.onClick);
    assert.match(textOf(fixture.tree), explanation);
    assert.equal(button(fixture.tree, 'Run Bounded Test').props.disabled, false);
    assert.equal(fixture.sessionStorage.getItem(equityLegacyKey), null);
    assert.equal(fixture.calls.filter(call => call.method === 'POST').length, 1);
  });
}

test('bounded action stays disabled while discovery is pending or fails', async t => {
  const pending = deferred();
  const fixture = await equityFixture(t, {
    onRequest: url => url.endsWith('/cycles?limit=1') ? pending.promise : undefined,
  });
  assert.equal(button(fixture.tree, 'Run Bounded Test').props.disabled, true);
  await settle(button(fixture.tree, 'Run Bounded Test').props.onClick);
  await settle(() => pending.reject({ status: 503 }));
  assert.equal(button(fixture.tree, 'Run Bounded Test').props.disabled, true);
  await settle(button(fixture.tree, 'Run Bounded Test').props.onClick);
  assert.equal(fixture.calls.filter(call => call.method === 'POST').length, 0);
});

test('the existing FULL button retains its exact request after adding the bounded action', async t => {
  let submission;
  const fixture = await equityFixture(t, {
    history: { cycles: [], active_cycle: null }, statuses: {},
    onRequest: (url, options) => {
      if (options?.method === 'POST') {
        submission = { url, ...options };
        return Promise.reject({ status: 503, message: 'RECOVERY_PAUSED' });
      }
    },
  });
  await settle(button(fixture.tree, 'Run Equity Radar').props.onClick);
  assert.equal(submission.url, '/api/v1/research/opportunities/cycles');
  assert.deepEqual(JSON.parse(submission.body), { analysis_scope: 'FULL' });
});

test('cancelled Equity progress survives reload and terminal cycles stop polling', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const id = 'retained-execution-id-must-stay-internal';
  const cancelled = cycleSummary(id, 'CANCELLED');
  const fixture = await equityFixture(t, { history: { cycles: [cancelled], active_cycle: null },
    statuses: { [id]: { status: 'CANCELLED', authoritative_progress: savedProgress } } });
  assert.equal(button(fixture.tree, 'Run Equity Radar').props.disabled, false);
  assert.equal(fixture.tree.root.findAllByType('select').length, 0);
  assert.match(textOf(fixture.tree), /Retained historical research/);
  assert.doesNotMatch(textOf(fixture.tree), /Cycle cancelled|Historical cycle|Cycle history/);
  assert.ok(!textOf(fixture.tree).includes(id));
  assert.match(textOf(fixture.tree), /2003/);
  assert.match(textOf(fixture.tree), /119/);
  assert.equal(button(fixture.tree, 'Cancel Equity Radar'), undefined);
  const before = fixture.calls.length;
  await settle(() => t.mock.timers.tick(30000));
  assert.equal(fixture.calls.length, before);
  await fixture.remount();
  assert.match(textOf(fixture.tree), /2003/);
  assert.equal(fixture.tree.root.findAllByType('select').length, 0);
  assert.ok(fixture.calls.every(call => call.method === 'GET'));
});

test('server discovery restores terminal progress with empty or disabled storage', async t => {
  const failed = cycleSummary('failed', 'FAILED');
  const fixture = await equityFixture(t, { noStorage: true,
    history: { cycles: [failed], active_cycle: null },
    statuses: { failed: { status: 'FAILED', authoritative_progress: { ...savedProgress, phase: 'FAILED' } } } });
  assert.equal(fixture.tree.root.findAllByType('select').length, 0);
  assert.equal(button(fixture.tree, 'Run Equity Radar').props.disabled, false);
  assert.match(textOf(fixture.tree), /2003/);
});

test('obsolete saved selection is ignored in favor of the latest retained work', async t => {
  const fixture = await equityFixture(t, { selected: 'older',
    history: { cycles: [cycleSummary('newest', 'COMPLETED')], active_cycle: null },
    statuses: { newest: { status: 'COMPLETED', authoritative_progress: savedProgress } } });
  assert.equal(fixture.tree.root.findAllByType('select').length, 0);
  assert.match(textOf(fixture.tree), /2003/);
  assert.ok(!fixture.calls.some(call => call.url.includes('/older/')));
  assert.doesNotMatch(textOf(fixture.tree), /newest|older/);
});

test('server active execution takes priority over retained work and obsolete selection', async t => {
  const active = cycleSummary('active-old', 'RUNNING');
  const fixture = await equityFixture(t, { selected: 'deleted',
    history: { cycles: [cycleSummary('newest', 'COMPLETED')], active_cycle: active },
    statuses: { 'active-old': { status: 'RUNNING', authoritative_progress: { ...savedProgress, phase: 'DEEP' } } } });
  assert.equal(fixture.tree.root.findAllByType('select').length, 0);
  assert.ok(fixture.tree.root.findAllByType('button').some(node => node.props.disabled && node.children.join('').startsWith('Running')));
  assert.ok(!fixture.calls.some(call => /\/(newest|deleted)\/status$/.test(call.url)));
  assert.doesNotMatch(textOf(fixture.tree), /Retained historical|active-old|newest|deleted/);
});

test('progress and Cancel target only the active execution despite an old saved selection', async t => {
  const active = cycleSummary('active', 'RUNNING');
  const fixture = await equityFixture(t, { selected: 'cancelled',
    history: { cycles: [active, cycleSummary('cancelled', 'CANCELLED')], active_cycle: active },
    statuses: { active: { status: 'RUNNING', authoritative_progress: { ...savedProgress, baseline_completed: 42 } }, cancelled: { status: 'CANCELLED', authoritative_progress: savedProgress } },
    onRequest: (url, options) => options?.method === 'DELETE' ? { status: 'CANCEL_REQUESTED' } : undefined });
  assert.equal(fixture.tree.root.findAllByType('select').length, 0);
  assert.match(textOf(fixture.tree), /42/);
  assert.doesNotMatch(textOf(fixture.tree), /2003|Retained historical/);
  assert.ok(fixture.tree.root.findAllByType('button').some(node => node.props.disabled && node.children.join('').startsWith('Running')));
  await settle(() => button(fixture.tree, 'Cancel Equity Radar').props.onClick());
  assert.deepEqual(fixture.calls.filter(call => call.method === 'DELETE'),
    [{ url: '/api/v1/research/opportunities/cycles/active/cancel', method: 'DELETE' }]);
  assert.ok(!fixture.calls.some(call => call.url.endsWith('/cancelled/status')));
});

test('cancellation retains final progress and server discovery restores it on reload', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const active = cycleSummary('legacy', 'RUNNING');
  const statuses = { legacy: { status: 'RUNNING', authoritative_progress: { ...savedProgress, phase: 'DEEP' } } };
  const history = { cycles: [active], active_cycle: active };
  const fixture = await equityFixture(t, { legacy: 'legacy', history, statuses,
    onRequest: (url, options) => {
      if (options?.method !== 'DELETE') return undefined;
      statuses.legacy = { status: 'CANCELLED', authoritative_progress: savedProgress };
      history.active_cycle = null;
      history.cycles = [cycleSummary('legacy', 'CANCELLED')];
      return { status: 'CANCEL_REQUESTED' };
    } });
  await settle(() => button(fixture.tree, 'Cancel Equity Radar').props.onClick());
  await settle(() => t.mock.timers.tick(2500));
  assert.equal(fixture.sessionStorage.getItem(equityLegacyKey), null);
  assert.equal(button(fixture.tree, 'Run Equity Radar').props.disabled, false);
  await fixture.remount();
  assert.match(textOf(fixture.tree), /2003/);
  assert.match(textOf(fixture.tree), /Retained historical research/);
  assert.doesNotMatch(textOf(fixture.tree), /Cycle legacy|Cycle history/);
});

test('late retained status responses cannot replace a newly started execution', async t => {
  const slow = deferred();
  const fixture = await equityFixture(t, { selected: 'first',
    history: { cycles: [cycleSummary('first', 'CANCELLED')], active_cycle: null },
    statuses: { second: { status: 'RUNNING', authoritative_progress: { ...savedProgress, baseline_completed: 42 } } },
    onRequest: (url, options) => options?.method === 'POST' ? { cycle_id: 'second', status: 'ACCEPTED' }
      : url.endsWith('/first/status') ? slow.promise : undefined });
  await settle(() => button(fixture.tree, 'Run Equity Radar').props.onClick());
  await settle(() => slow.resolve({ status: 'RUNNING', authoritative_progress: savedProgress }));
  assert.ok(fixture.tree.root.findAllByType('button').some(node => node.props.disabled && node.children.join('').startsWith('Running')));
  assert.match(textOf(fixture.tree), /42/);
  assert.doesNotMatch(textOf(fixture.tree), /2003|Retained historical/);
  assert.equal(fixture.sessionStorage.getItem(equityLegacyKey), 'second');
});

test('a transient active status failure retains the last known counters', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const active = cycleSummary('active', 'RUNNING');
  const statuses = { active: { status: 'RUNNING', authoritative_progress: { ...savedProgress, phase: 'DEEP' } } };
  const fixture = await equityFixture(t, { history: { cycles: [active], active_cycle: active }, statuses });
  statuses.active = Object.assign(new Error('Temporarily unavailable'), { status: 503 });
  await settle(() => t.mock.timers.tick(2500));
  assert.match(textOf(fixture.tree), /Could not refresh status just now/);
  assert.match(textOf(fixture.tree), /2003/);
  assert.equal(fixture.tree.root.findAllByType('select').length, 0);
});

test('failed discovery restores the known active execution without a history selection', async t => {
  const fixture = await equityFixture(t, { legacy: 'known-active',
    statuses: { 'known-active': { status: 'RUNNING', authoritative_progress: { ...savedProgress, phase: 'DEEP' } } },
    onRequest: url => url.endsWith('/cycles?limit=1')
      ? Promise.reject(Object.assign(new Error('Unavailable'), { status: 503 })) : undefined });
  assert.ok(fixture.tree.root.findAllByType('button').some(node => node.props.disabled && node.children.join('').startsWith('Running')));
  assert.ok(button(fixture.tree, 'Cancel Equity Radar'));
  assert.match(textOf(fixture.tree), /2003/);
  assert.doesNotMatch(textOf(fixture.tree), /known-active|Retained historical/);
  assert.equal(fixture.tree.root.findAllByType('select').length, 0);
});

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
          if (url.endsWith('/cycles?limit=1')) return Promise.resolve({
            cycles: [cycleSummary('terminal-cycle', terminalStatus)], active_cycle: null });
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
      if (url.endsWith('/cycles?limit=1')) return Promise.resolve({
        cycles: [cycleSummary('restored', 'RUNNING')], active_cycle: cycleSummary('restored', 'RUNNING') });
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


test('phase descriptions are explicit and historical recommendations never claim today freshness', () => {
  const { RadarContent } = loadComponent('opportunity-radar.tsx');
  const { RadarProgressPanel } = loadComponent('radar-progress.tsx');
  const data = { generated_at: '2026-10-05T00:00:00Z', best_buy_today: null, top_short_term: [], top_long_term: [] };
  const html = renderToString(React.createElement(RadarContent, { data }));
  assert.match(html, /Previous completed cycle/);
  assert.doesNotMatch(html, /Best buy today/);
  for (const [phase, description] of [['BASELINE', 'Baseline scanning'], ['CANDIDATE_SELECTION', 'Candidate selection'], ['DEEP', 'Deep investigation']]) {
    const panel = renderToString(React.createElement(RadarProgressPanel, {
      radarType: 'Equity', cycleId: 'c', status: 'RUNNING', phase, counters: [], percent: null
    }));
    assert.match(panel, new RegExp(description));
    assert.match(panel, /bar-track-indeterminate/);
    assert.doesNotMatch(panel, /80%/);
  }
});

test('ADMIN cancellation uses DELETE request helper and exposes permission failures without deleting historical data', async () => {
  const originalWindow = globalThis.window;
  const calls = [];
  let tree;
  try {
    globalThis.window = { sessionStorage: { getItem: () => 'c', removeItem() {} } };
    const { OpportunityRadar } = loadComponent('opportunity-radar.tsx', { request: (url, init) => {
      if (url.endsWith('/cycles?limit=1')) return Promise.resolve({
        cycles: [cycleSummary('c', 'RUNNING')], active_cycle: cycleSummary('c', 'RUNNING') });
      const pending = deferred(); calls.push({ url, init, ...pending }); return pending.promise;
    } });
    await settle(() => { tree = create(React.createElement(OpportunityRadar, { heldIds: [], watchlistedIds: [], roles: ['ADMIN'] })); });
    await settle(() => calls[0].resolve({ generated_at: '2026-10-05', best_buy_today: null, top_short_term: [], top_long_term: [] }));
    await settle(() => button(tree, 'Cancel Equity Radar').props.onClick());
    assert.equal(calls[2].url, '/api/v1/research/opportunities/cycles/c/cancel');
    assert.equal(calls[2].init.method, 'DELETE');
    assert.equal(calls[2].init.headers, undefined);
    await settle(() => calls[2].reject({ status: 403 }));
    assert.match(textOf(tree), /Administrator access is required/);
    assert.match(textOf(tree), /Previous completed cycle/);
    await settle(() => button(tree, 'Cancel Equity Radar').props.onClick());
    await settle(() => calls[3].resolve({ status: 'CANCEL_REQUESTED' }));
    assert.equal(button(tree, 'Cancel Equity Radar').props.disabled, true);
    assert.match(textOf(tree), /waiting for in-flight work to drain/);
    await settle(() => tree.update(React.createElement(OpportunityRadar, { heldIds: [], watchlistedIds: [], roles: ['USER'] })));
    assert.equal(button(tree, 'Cancel Equity Radar'), undefined);
  } finally {
    if (tree) await settle(() => tree.unmount());
    globalThis.window = originalWindow;
  }
});


const deepProgress = (completed, attempted = completed) => ({
  phase: 'DEEP', deep_completed: completed, deep_attempted: attempted,
  deep_denominator: 25, deep_ready: 0, deep_readiness_failed: completed,
  deep_technical_failure: 0
});
for (const scenario of [
  { name: '25 terminal and zero ready remains RUNNING at 100% processed', progress: deepProgress(25), status: 'RUNNING', percent: 100 },
  { name: '25 terminal remains PUBLISHED at 100% processed', progress: deepProgress(25), status: 'PUBLISHED', percent: 100 },
  { name: '4 terminal and zero ready out of 25 admitted is 16%', progress: deepProgress(4), percent: 16 },
  { name: '25 attempts with only 4 terminal is still 16%', progress: deepProgress(4, 25), percent: 16 },
  { name: 'legacy completion without granular readiness still measures work', progress: { phase: 'DEEP', deep_completed: 4, deep_denominator: 25 }, percent: 16 },
  { name: 'readiness without completion cannot manufacture work progress', progress: { phase: 'DEEP', deep_ready: 25, deep_denominator: 25 } },
  { name: 'missing legacy fields remain indeterminate', progress: {} },
  ...['COMPLETED', 'CANCELLED', 'FAILED'].map(status => ({ name: `${status} preserves counters and hides active bars`, status, progress: deepProgress(4, 25), terminal: true })),
  // baseline_evaluated is deliberately decoyed to a DIFFERENT value than
  // baseline_completed in every scenario below that has a defined
  // acquisitionPercent -- the displayed/percentaged "scanned" count must
  // come from baseline_completed (live progress), never baseline_evaluated
  // (a static admission-time snapshot, see app/cycle_checkpoint.py's
  // cycle_status_snapshot); a regression back to reading baseline_evaluated
  // would compute a visibly wrong percentage/count against these fixtures.
  { name: 'acquisition phase with authoritative scan counters', progress: { phase: 'BASELINE', baseline_evaluated: 2621, baseline_completed: 700, universe_count: 2621, deep_denominator: 25 }, acquisitionPercent: 700 / 2621 * 100 },
  { name: 'acquisition phase without counters never uses the shortlist', progress: { phase: 'BASELINE', deep_denominator: 25 } },
  { name: 'acquisition phase with missing total is indeterminate', progress: { phase: 'BASELINE', baseline_evaluated: 2621, baseline_completed: 700, deep_denominator: 25 } },
  { name: 'durable full-market scan and deep work have independent percentages', progress: { ...deepProgress(4), baseline_evaluated: 1, baseline_completed: 2621, universe_count: 2621 }, percent: 16, acquisitionPercent: 100 },
]) {
  test(`Equity progress: ${scenario.name}`, async () => {
    const originalWindow = globalThis.window, originalTimeout = globalThis.setTimeout, originalClear = globalThis.clearTimeout;
    const status = scenario.status ?? 'RUNNING';
    let tree;
    try {
      globalThis.window = { sessionStorage: { getItem: () => 'progress-cycle', removeItem() {} } };
      globalThis.setTimeout = () => 1;
      globalThis.clearTimeout = () => {};
      const { OpportunityRadar } = loadComponent('opportunity-radar.tsx', { request: async url =>
        url.endsWith('/cycles?limit=1') ? { cycles: [cycleSummary('progress-cycle', status)],
          active_cycle: scenario.terminal ? null : cycleSummary('progress-cycle', status) }
          : url.endsWith('/status') ? { status, authoritative_progress: scenario.progress }
          : { generated_at: null, best_buy_today: null, top_short_term: [], top_long_term: [] }
      });
      await settle(() => { tree = create(React.createElement(OpportunityRadar, { heldIds: [], watchlistedIds: [] })); });
      const bars = tree.root.findAll(node => node.props.role === 'progressbar');
      const counter = label => tree.root.findAllByType('dt').find(node => node.children.join('') === label).parent.findByType('dd').children.join('');
      // "Deep completed"/"Deep attempted" counters were removed from the shared
      // progress-counters dl (duplicate-counters cleanup); the same deep_completed/
      // deep_attempted/deep_ready values are still rendered, now only inside
      // EquityAnalysisPanels under the shorter "Completed"/"Attempted"/"Ready for
      // ranking" labels -- assert there instead of in the counters dl.
      assert.equal(counter('Completed'), String(scenario.progress.deep_completed ?? 'Unavailable'));
      assert.equal(counter('Ready for ranking'), String(scenario.progress.deep_ready ?? 'Unavailable'));
      assert.equal(counter('Attempted'), String(scenario.progress.deep_attempted ?? 'Unavailable'));
      assert.equal(button(tree, scenario.terminal ? 'Run Equity Radar' : 'Running\u2026').props.disabled, !scenario.terminal);
      if (scenario.terminal) {
        assert.equal(bars.length, 0);
        assert.match(textOf(tree), new RegExp(status[0] + status.slice(1).toLowerCase()));
      } else {
        const market = bars.find(node => node.props['aria-label'] === 'Full-market baseline scan');
        assert.equal(market.props['aria-valuenow'], scenario.acquisitionPercent);
        // Explicit check that the rendered scanned/total text itself comes from
        // baseline_completed, not baseline_evaluated (see decoy values above).
        const scanSection = tree.root.findAll(node => node.type === 'section' && node.props['aria-label'] === 'Full-market baseline scan')[0];
        assert.equal(scanSection.findByType('p').children.join(''),
          `Full-market baseline scan: ${scenario.progress.baseline_completed ?? 'Unavailable'} of ${scenario.progress.universe_count ?? 'Unavailable'} instruments scanned.`);
        const deep = bars.find(node => node.props['aria-label'] === 'Deep processing');
        if (scenario.progress.phase === 'DEEP') assert.equal(deep.props['aria-valuenow'], scenario.percent);
        else assert.equal(deep, undefined);
        if (scenario.progress.phase === 'BASELINE') {
          assert.equal(bars.length, 1);
          assert.match(textOf(tree), /Baseline scanning/);
          if (scenario.acquisitionPercent === undefined) assert.match(textOf(tree), /live full-market scan progress is unavailable/);
        }
        if (scenario.percent === 100) assert.match(textOf(tree), new RegExp(status === 'PUBLISHED' ? 'Published' : 'Running'));
      }
    } finally {
      if (tree) await settle(() => tree.unmount());
      globalThis.window = originalWindow;
      globalThis.setTimeout = originalTimeout;
      globalThis.clearTimeout = originalClear;
    }
  });
}

// Bounded/controlled ETF Radar admin action -----------------------------

const boundedPreflightResponse = (overrides = {}) => ({
  status: 'READY', scope: 'BOUNDED_CANONICAL_ETF', candidate_count: 1, ready_count: 1,
  instruments: [{ instrument_id: '11111111-1111-1111-1111-111111111111', symbol: 'NIFTYBEES', isin: 'INF1', canonical_identity_matches: true, mandatory_evidence_ready: true, blocking_reasons: [] }],
  recovery_paused: true, active_cycle_id: null, manual_execution_authorized: true, can_submit: true,
  ...overrides,
});

test('Bounded ETF Radar section is hidden without the ADMIN role', async () => {
  const calls = [];
  const Component = loadComponent('etf-radar.tsx', { request: url => { calls.push(url); return Promise.reject({ status: 404 }); } }).EtfRadar;
  let tree;
  await settle(() => { tree = create(React.createElement(Component, {})); });
  assert.doesNotMatch(textOf(tree), /Run Bounded ETF Radar/);
  await settle(() => tree.unmount());
});

test('Bounded ETF Radar: happy path checks preflight, requires confirmation, and submits only to /controlled-cycles', async () => {
  const calls = [];
  const originalWindow = globalThis.window;
  const store = new Map();
  try {
    globalThis.window = { sessionStorage: { getItem: k => store.get(k) ?? null, setItem: (k, v) => store.set(k, v), removeItem: k => store.delete(k) } };
    const Component = loadComponent('etf-radar.tsx', { request: (url, init) => {
      calls.push({ url, init });
      if (url === '/api/v1/etf-radar/current') return Promise.reject({ status: 404 });
      if (url.startsWith('/api/v1/etf-radar/full-universe-cycles')) return Promise.reject(new Error('bounded action must never call full-universe-cycles'));
      if (url.startsWith('/api/v1/etf-radar/preflight')) return Promise.resolve(boundedPreflightResponse());
      if (url === '/api/v1/etf-radar/controlled-cycles') return Promise.resolve({ cycle_id: 'bounded-1', status: 'ACCEPTED' });
      return Promise.reject(new Error('unexpected url ' + url));
    } }).EtfRadar;
    let tree;
    await settle(() => { tree = create(React.createElement(Component, { roles: ['ADMIN'] })); });
    assert.match(textOf(tree), /Run Bounded ETF Radar/);
    await settle(() => tree.root.findByType('textarea').props.onChange({ target: { value: '11111111-1111-1111-1111-111111111111' } }));
    await settle(() => button(tree, 'Check preflight').props.onClick());
    const preflightCall = calls.find(c => c.url.startsWith('/api/v1/etf-radar/preflight'));
    assert.match(preflightCall.url, /instrument_ids=11111111-1111-1111-1111-111111111111/);
    assert.match(textOf(tree), /status=/);
    assert.match(textOf(tree), /READY/);
    // Submit stays disabled until the explicit confirmation checkbox is checked.
    assert.equal(button(tree, 'Run Bounded ETF Radar').props.disabled, true);
    await settle(() => tree.root.findByType('input').props.onChange({ target: { checked: true } }));
    assert.equal(button(tree, 'Run Bounded ETF Radar').props.disabled, false);
    await settle(() => button(tree, 'Run Bounded ETF Radar').props.onClick());
    const submitCall = calls.find(c => c.url === '/api/v1/etf-radar/controlled-cycles');
    assert.ok(submitCall, 'controlled-cycles was called');
    assert.deepEqual(JSON.parse(submitCall.init.body), { instrument_ids: ['11111111-1111-1111-1111-111111111111'] });
    assert.equal(calls.some(c => c.url.startsWith('/api/v1/etf-radar/full-universe-cycles')), false);
    assert.equal(store.get('aip.etfRadar.activeCycleId'), 'bounded-1');
    await settle(() => tree.unmount());
  } finally { globalThis.window = originalWindow; }
});

test('Bounded ETF Radar: invalid instrument ID input is rejected client-side without any network call', async () => {
  const calls = [];
  const Component = loadComponent('etf-radar.tsx', { request: url => { calls.push(url); return Promise.reject({ status: 404 }); } }).EtfRadar;
  let tree;
  await settle(() => { tree = create(React.createElement(Component, { roles: ['ADMIN'] })); });
  await settle(() => tree.root.findByType('textarea').props.onChange({ target: { value: 'not-a-uuid' } }));
  await settle(() => button(tree, 'Check preflight').props.onClick());
  assert.match(textOf(tree), /Not a valid instrument ID/);
  assert.equal(calls.filter(u => u.includes('preflight')).length, 0);
  await settle(() => tree.unmount());
});

test('Bounded ETF Radar: a BLOCKED preflight explains why and never offers a confirm/submit control', async () => {
  const Component = loadComponent('etf-radar.tsx', { request: url => {
    if (url === '/api/v1/etf-radar/current') return Promise.reject({ status: 404 });
    if (url.startsWith('/api/v1/etf-radar/preflight')) return Promise.resolve(boundedPreflightResponse({
      status: 'BLOCKED', can_submit: false, ready_count: 0,
      instruments: [{ instrument_id: '11111111-1111-1111-1111-111111111111', symbol: 'NIFTYBEES', isin: 'INF1', canonical_identity_matches: true, mandatory_evidence_ready: false, blocking_reasons: ['EVIDENCE_INSUFFICIENT_WITHIN_PLAN'] }],
    }));
    return Promise.reject(new Error('unexpected url ' + url));
  } }).EtfRadar;
  let tree;
  await settle(() => { tree = create(React.createElement(Component, { roles: ['ADMIN'] })); });
  await settle(() => tree.root.findByType('textarea').props.onChange({ target: { value: '11111111-1111-1111-1111-111111111111' } }));
  await settle(() => button(tree, 'Check preflight').props.onClick());
  assert.match(textOf(tree), /mandatory evidence ready/);
  assert.match(textOf(tree), /EVIDENCE_INSUFFICIENT_WITHIN_PLAN/);
  assert.equal(tree.root.findAllByType('input').length, 0);
  assert.equal(button(tree, 'Run Bounded ETF Radar'), undefined);
  await settle(() => tree.unmount());
});

test('Bounded ETF Radar: recovery-paused-without-authorization preflight is explained, not silently blocked', async () => {
  const Component = loadComponent('etf-radar.tsx', { request: url => {
    if (url === '/api/v1/etf-radar/current') return Promise.reject({ status: 404 });
    if (url.startsWith('/api/v1/etf-radar/preflight')) return Promise.resolve(boundedPreflightResponse({
      recovery_paused: true, manual_execution_authorized: false, can_submit: false,
    }));
    return Promise.reject(new Error('unexpected url ' + url));
  } }).EtfRadar;
  let tree;
  await settle(() => { tree = create(React.createElement(Component, { roles: ['ADMIN'] })); });
  await settle(() => tree.root.findByType('textarea').props.onChange({ target: { value: '11111111-1111-1111-1111-111111111111' } }));
  await settle(() => button(tree, 'Check preflight').props.onClick());
  assert.match(textOf(tree), /not authorized in this environment/);
  await settle(() => tree.unmount());
});

test('Bounded ETF Radar: submission is disabled while another ETF cycle is active, even if preflight says READY', async () => {
  const originalWindow = globalThis.window;
  const store = new Map([['aip.etfRadar.activeCycleId', 'already-running']]);
  try {
    globalThis.window = { sessionStorage: { getItem: k => store.get(k) ?? null, setItem: (k, v) => store.set(k, v), removeItem: k => store.delete(k) } };
    const calls = [];
    const Component = loadComponent('etf-radar.tsx', { request: url => {
      calls.push(url);
      if (url === '/api/v1/etf-radar/current') return Promise.reject({ status: 404 });
      if (url.endsWith('/cycles/already-running/status')) return Promise.resolve({ status: 'RUNNING' });
      if (url.startsWith('/api/v1/etf-radar/preflight')) return Promise.resolve(boundedPreflightResponse());
      if (url === '/api/v1/etf-radar/controlled-cycles') return Promise.reject(new Error('must never submit while a cycle is active'));
      return Promise.reject(new Error('unexpected url ' + url));
    } }).EtfRadar;
    let tree;
    await settle(() => { tree = create(React.createElement(Component, { roles: ['ADMIN'] })); });
    await settle(() => tree.root.findByType('textarea').props.onChange({ target: { value: '11111111-1111-1111-1111-111111111111' } }));
    await settle(() => button(tree, 'Check preflight').props.onClick());
    await settle(() => tree.root.findByType('input').props.onChange({ target: { checked: true } }));
    assert.equal(button(tree, 'Another ETF cycle is running…').props.disabled, true);
    await settle(() => tree.unmount());
  } finally { globalThis.window = originalWindow; }
});
