import test from "node:test";
import assert from "node:assert/strict";
import { loadEtfModule, candidate, cycle } from "./etf-test-helpers.mjs";
const m = loadEtfModule("lib/etf-radar-model");
const metric = (value, unit = "PERCENT") => ({ value, unit, quality: "VALID" });

for (const disposition of [
  "TECHNICAL_FAILURE",
  "INELIGIBLE",
  "INSUFFICIENT_DATA",
  "CANCELLED",
  "UNSUPPORTED",
]) {
  test(`actual-shaped ${disposition} without analytical fields remains unassessed`, () => {
    const row = m.normalizeEtfCycle(
      cycle([
        {
          global_instrument_id: null,
          symbol: "SYNTHETIC",
          disposition,
          reason: "SYNTHETIC_REASON",
          recommendation: null,
        },
      ]),
    ).candidates[0];
    assert.equal(row.rule_engine_result, null);
    assert.deepEqual(row.risk_gates, []);
    assert.equal(row.analysis_recorded, false);
    assert.equal(row.rank, null);
    assert.notEqual(m.entryLabel(row), "Verified favorable entry conditions");
  });
}
for (const raw of [
  null,
  [],
  {},
  { cycle_id: "x", as_of: "bad", candidates: [], ranked: [] },
  { cycle_id: "x", as_of: "2026-11-05", candidates: {}, ranked: [] },
  cycle([candidate({ disposition: "UNKNOWN" })]),
  cycle([candidate(), candidate()]),
]) {
  test(`reject malformed envelope ${JSON.stringify(raw).slice(0, 65)}`, () =>
    assert.throws(() => m.normalizeEtfCycle(raw)));
}
test("malformed optional analysis becomes unavailable with diagnostic; preserves outcome", () => {
  const row = m.normalizeEtfCandidate(
    candidate({
      risk_gates: null,
      rule_engine_result: {
        overall_score: "99",
        factor_results: null,
        data_completeness: null,
      },
      missing_data: null,
      source_references: null,
    }),
  );
  assert.equal(row.rule_engine_result.overall_score, null);
  assert.equal(row.analysis_recorded, false);
  assert.equal(row.disposition, "EVALUATED");
  assert.ok(row.contract_issues.length);
  assert.deepEqual(row.source_references, []);
});
for (const value of ["", " ", "NaN", "Infinity", null])
  test(`invalid metric ${String(value)} never becomes zero`, () => {
    const row = candidate({
      screening_metrics: { expense_ratio: metric(value, "PERCENT_PA") },
    });
    assert.equal(m.metricRaw(row, "expense_ratio"), null);
    assert.equal(m.metricDisplay(row, "expense_ratio"), "Unavailable");
  });
test("unit validation never relabels USD AUM or fractions as INR / percent", () => {
  assert.equal(
    m.metricDisplay(
      candidate({ screening_metrics: { aum: metric("123", "USD") } }),
      "aum",
    ),
    "Unavailable",
  );
  assert.equal(
    m.metricRaw(
      candidate({
        screening_metrics: { expense_ratio: metric("0.2", "FRACTION") },
      }),
      "expense_ratio",
    ),
    null,
  );
  assert.match(
    m.metricDisplay(
      candidate({
        screening_metrics: { aum: metric("1234567.891234", "INR") },
      }),
      "aum",
    ),
    /1,234,567\.89 INR/,
  );
});
const rows = [
  candidate({
    global_instrument_id: "b",
    symbol: "B",
    rule_engine_result: null,
    screening_metrics: { expense_ratio: metric(null, "PERCENT_PA") },
  }),
  candidate({
    global_instrument_id: "c",
    symbol: "C",
    rule_engine_result: {
      overall_score: 50,
      data_completeness: 0.5,
      confidence: "MEDIUM",
    },
    screening_metrics: {
      expense_ratio: metric("0.5", "PERCENT_PA"),
      aum: metric("500", "INR"),
    },
  }),
  candidate({
    global_instrument_id: "a",
    symbol: "A",
    rule_engine_result: {
      overall_score: 50,
      data_completeness: 0.5,
      confidence: "HIGH",
    },
    screening_metrics: {
      expense_ratio: metric("0.05", "PERCENT_PA"),
      aum: metric("50", "INR"),
    },
  }),
];
for (const key of ["score", "confidence", "completeness", "expense", "aum"])
  for (const dir of ["asc", "desc"])
    test(`${key} ${dir} keeps missing values last and does not mutate inputs`, () => {
      const before = rows.map((c) => c.symbol);
      assert.equal(m.sortEtfs(rows, key, dir).at(-1).symbol, "B");
      assert.deepEqual(
        rows.map((c) => c.symbol),
        before,
      );
    });
test("confidence has ordinal order and score ties use stable canonical identity", () => {
  assert.deepEqual(
    m.sortEtfs(rows, "confidence", "desc").map((c) => c.symbol),
    ["A", "C", "B"],
  );
  assert.deepEqual(
    m.sortEtfs(rows, "score", "desc").map((c) => c.symbol),
    ["A", "C", "B"],
  );
});
test("all search terms and text/numeric filters combine before pagination", () => {
  const source = [
    candidate({
      fund_name: "Synthetic Alpha Gold",
      fund_house: "Synthetic AMC",
      peer_group: "GOLD",
      history_observations: 120,
      screening_metrics: {
        expense_ratio: metric("0.2", "PERCENT_PA"),
        aum: metric("1000", "INR"),
      },
      source_references: [
        {
          provider: "OFFICIAL_TEST",
          as_of: "2026-11-04",
          metric: "NAV",
          url: "https://example.test",
        },
      ],
    }),
    candidate({
      global_instrument_id: "other",
      symbol: "OTHER",
      fund_name: "Another Gold",
      peer_group: "GOLD",
    }),
  ];
  assert.equal(
    m.filterEtfs(source, "alpha official_test 2026-11-04", {
      ...m.EMPTY_FILTERS,
      category: "GOLD",
      fundHouse: "amc",
      minScore: "60",
      maxExpense: ".3",
      minAum: "900",
      minHistory: "100",
    }).length,
    1,
  );
  assert.equal(
    m.filterEtfs(source, "", { ...m.EMPTY_FILTERS, minScore: "not numeric" })
      .length,
    0,
  );
  assert.equal(
    m.filterEtfs(source, "", { ...m.EMPTY_FILTERS, minAum: "0" }).length,
    1,
  );
});
test("outcomes partition every assessed ETF once, including readiness-excluded records", () => {
  const input = [
    candidate(),
    candidate({ disposition: "INSUFFICIENT_DATA" }),
    candidate({ disposition: "TECHNICAL_FAILURE" }),
    candidate({ disposition: "INELIGIBLE" }),
    candidate({ recommendation: "WATCH" }),
  ];
  const counts = m.outcomePartition(input);
  assert.equal(
    Object.values(counts).reduce((a, b) => a + b, 0),
    input.length,
  );
  assert.equal(counts["Preliminary research"], 1);
  assert.equal(counts["Insufficient data"], 1);
});
test("legacy favorable assessment and self-attested actionable flag cannot establish verified entry", () => {
  const old = candidate({
    entry_assessment: {
      classification: "FAVORABLE_ENTRY_ZONE",
      conditions: [],
    },
  });
  assert.equal(m.verifiedEntry(old), false);
  assert.equal(m.entryLabel(old), "Entry evidence incomplete");
  assert.equal(
    m.verifiedEntry({
      ...old,
      actionable_recommendation: {
        status: "FAVORABLE_ENTRY_ZONE",
        is_actionable: true,
      },
    }),
    false,
  );
});
test("verified entry requires new readiness, aligned session gate and five unit-valid core metrics", () => {
  const screening_metrics = Object.fromEntries(
    m.FUND_EVIDENCE.map((k) => [
      k,
      metric(
        "1",
        k === "aum" ? "INR" : k === "expense_ratio" ? "PERCENT_PA" : "PERCENT",
      ),
    ]),
  );
  const good = candidate({
    screening_metrics,
    research_readiness: {
      observed: true,
      status: "READY",
      summary: { ready_for_analysis: true },
    },
    entry_inputs: { verified: true, current: true },
    actionable_recommendation: {
      status: "FAVORABLE_ENTRY_ZONE",
      is_actionable: true,
      explanation: "Synthetic verified gate",
    },
  });
  assert.equal(m.verifiedEntry(good), true);
  assert.equal(
    m.verifiedEntry({
      ...good,
      entry_inputs: { verified: false, current: true },
    }),
    false,
  );
  assert.equal(
    m.verifiedEntry({
      ...good,
      research_readiness: { observed: true, status: "NOT_READY" },
    }),
    false,
  );
  assert.equal(m.verifiedEntry({ ...good, screening_metrics: {} }), false);
});
