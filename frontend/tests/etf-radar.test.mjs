import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import {
  React,
  create,
  act,
  renderToStaticMarkup,
  loadEtfModule,
  candidate,
  cycle,
  textOf,
  button,
} from "./etf-test-helpers.mjs";
const { EtfRadarCard, EtfRadarContent } = loadEtfModule();
const settle = async (fn) => {
  await act(async () => {
    fn?.();
    await Promise.resolve();
  });
};

test("card shows unavailable factors without fabricating zero or a successful risk assessment", () => {
  const html = renderToStaticMarkup(
    React.createElement(EtfRadarCard, {
      item: candidate({ risk_gates: null, rule_engine_result: null }),
    }),
  );
  assert.match(html, /Analysis not recorded/);
  assert.match(html, /Risk assessment not recorded/);
  const factors = html.match(/etf-radar-factors">([\s\S]*?)<\/dl>/)[1];
  assert.equal((factors.match(/\u2014/g) || []).length, 7);
  assert.doesNotMatch(factors, />0</);
});
test("card preserves raw factor metric, score basis and actual source dates with a safe external link", () => {
  const row = candidate({
    score_basis: { explanation: "Available weights are normalized." },
    rule_engine_result: {
      factor_results: [
        {
          factor: "PERFORMANCE_MOMENTUM",
          status: "SCORED",
          normalized_score: 60,
          raw_metric: "RETURN_1M=2.123",
          freshness: "2026-11-04",
        },
      ],
      overall_score: 60,
      data_completeness: 0.4,
      confidence: "LOW",
    },
    source_references: [
      {
        metric: "NAV",
        value: "100",
        as_of: "2026-11-04",
        provider: "SYNTHETIC",
        url: "https://example.test/official.pdf",
      },
    ],
  });
  const html = renderToStaticMarkup(
    React.createElement(EtfRadarCard, { item: row }),
  );
  assert.match(html, /RETURN_1M=2.123/);
  assert.match(html, /Available weights/);
  assert.match(html, /2026-11-04/);
  assert.match(html, /href="https:\/\/example.test\/official.pdf"/);
  assert.match(html, /rel="noopener noreferrer"/);
});
test("card renders only triggered gates and never imports company valuation concepts", () => {
  const html = renderToStaticMarkup(
    React.createElement(EtfRadarCard, {
      item: candidate({
        risk_gates: [
          {
            gate_id: "TRIGGERED",
            triggered: true,
            reason: "Synthetic missing evidence",
          },
          { gate_id: "NOT_TRIGGERED", triggered: false, reason: "Hidden" },
        ],
      }),
    }),
  );
  assert.match(html, /TRIGGERED/);
  assert.doesNotMatch(html, /NOT TRIGGERED/);
  assert.doesNotMatch(html, /ROE|promoter holding|order book/i);
});
test("legacy favorable entry remains visibly unverified, separate from preliminary score", () => {
  const html = renderToStaticMarkup(
    React.createElement(EtfRadarCard, {
      item: candidate({
        entry_assessment: {
          classification: "FAVORABLE_ENTRY_ZONE",
          conditions: ["Synthetic old session"],
          methodology: "OLD",
        },
      }),
    }),
  );
  assert.match(html, /Preliminary research/);
  assert.match(html, /Entry evidence incomplete/);
  assert.match(html, /Historical entry assessment/);
  assert.doesNotMatch(html, /Verified favorable entry conditions/);
});
test("card retains backend readiness reasons, missing fund metrics and source diagnostics", () => {
  const html = renderToStaticMarkup(
    React.createElement(EtfRadarCard, {
      item: candidate({
        research_readiness: {
          observed: true,
          status: "NOT_READY",
          requirements: [
            {
              requirement: "MARKET_PRICE",
              status: "MISSING",
              reason: "ETF_MARKET_PAIR_SESSION_MISMATCH",
              provider: "SYNTHETIC",
            },
          ],
        },
        missing_data: ["TRACKING_QUALITY"],
        limitations: ["Adjustment coverage unavailable"],
      }),
    }),
  );
  assert.match(html, /ETF_MARKET_PAIR_SESSION_MISMATCH/);
  assert.match(html, /TRACKING_QUALITY/);
  assert.match(html, /Adjustment coverage unavailable/);
  assert.match(html, /Verified, current source evidence unavailable/);
});
test("unsafe source URLs never become clickable", () => {
  const html = renderToStaticMarkup(
    React.createElement(EtfRadarCard, {
      item: candidate({
        source_references: [
          {
            metric: "NAV",
            value: "1",
            provider: "SYNTHETIC",
            as_of: "2026-11-04",
            url: "javascript:alert(1)",
          },
        ],
      }),
    }),
  );
  assert.doesNotMatch(html, /href="javascript:/);
  assert.match(html, /Source link unavailable/);
});
test("GET envelope failure has a recoverable retry and cannot start acquisition", async () => {
  const calls = [];
  let attempt = 0;
  const { EtfRadar } = loadEtfModule(
    "components/etf-radar",
    async (url, init) => {
      calls.push({ url, init });
      return ++attempt === 1 ? { candidates: null } : cycle();
    },
  );
  let tree;
  await settle(() => {
    tree = create(React.createElement(EtfRadar));
  });
  assert.match(textOf(tree), /ETF radar unavailable/);
  assert.match(textOf(tree), /incomplete or malformed/);
  await settle(() =>
    button(tree, "Retry loading ETF research").props.onClick(),
  );
  assert.match(textOf(tree), /ETF research/);
  assert.ok(
    calls.every(
      (c) => c.url === "/api/v1/etf-radar/current" && !c.init?.method,
    ),
  );
  await settle(() => tree.unmount());
});
test("404 is an empty publication state and ordinary users have no acquisition forms or run action", async () => {
  const calls = [];
  const { EtfRadar } = loadEtfModule("components/etf-radar", async (url) => {
    calls.push(url);
    throw { status: 404 };
  });
  let tree;
  await settle(() => {
    tree = create(React.createElement(EtfRadar, { roles: ["USER"] }));
  });
  assert.match(textOf(tree), /No ETF radar cycle yet/);
  assert.equal(button(tree, "Run full NSE ETF Radar"), undefined);
  assert.equal(tree.root.findAllByType("textarea").length, 0);
  assert.doesNotMatch(textOf(tree), /Check preflight|Run Bounded/);
  assert.equal(calls.length, 1);
  await settle(() => tree.unmount());
});
test("administrator retains explicit full action, no bounded controls and published data survives action failure", async () => {
  const calls = [];
  const { EtfRadar } = loadEtfModule(
    "components/etf-radar",
    async (url, init) => {
      calls.push({ url, init });
      if (init?.method === "POST") throw new Error("synthetic failure");
      return cycle();
    },
  );
  let tree;
  await settle(() => {
    tree = create(React.createElement(EtfRadar, { roles: ["ADMIN"] }));
  });
  assert.equal(tree.root.findAllByType("textarea").length, 0);
  await settle(() => button(tree, "Run full NSE ETF Radar").props.onClick());
  assert.match(textOf(tree), /last published research is preserved/);
  assert.match(textOf(tree), /ETF research/);
  assert.equal(calls.at(-1).url, "/api/v1/etf-radar/full-universe-cycles");
  assert.equal(calls.at(-1).init.method, "POST");
  assert.ok(
    !calls.some((c) => /preflight|controlled-cycles|\/evidence/.test(c.url)),
  );
  await settle(() => tree.unmount());
});
test("dashboard stays lightweight and dedicated radar routing stays isolated", () => {
  const workspace = fs.readFileSync(
      new URL("../app/components/investment-workspace.tsx", import.meta.url),
      "utf8",
    ),
    tabs = fs.readFileSync(
      new URL("../app/components/radar-tabs.tsx", import.meta.url),
      "utf8",
    ),
    source = fs.readFileSync(
      new URL("../app/components/etf-radar.tsx", import.meta.url),
      "utf8",
    );
  assert.match(workspace, /view === "etf-radar" \? <EtfRadar/);
  assert.match(workspace, /<RadarTabs/);
  assert.doesNotMatch(tabs, /import \{ EtfRadar \}/);
  assert.doesNotMatch(source, /from ["']\.\/opportunity-radar/);
  assert.doesNotMatch(source, /\/api\/v1\/research\/opportunities/);
});
test("partial publication discloses the actual returned count", () => {
  const html = renderToStaticMarkup(
    React.createElement(EtfRadarContent, {
      data: cycle([candidate()], { universe_count: 359 }),
    }),
  );
  assert.match(html, /Partial published result: 1 of 359/);
  assert.doesNotMatch(html, /Complete official NSE snapshot assessed/);
});
