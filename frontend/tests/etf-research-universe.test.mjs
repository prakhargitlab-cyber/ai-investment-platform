import test from "node:test";
import assert from "node:assert/strict";
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
  change,
} from "./etf-test-helpers.mjs";
const { EtfRadarContent } = loadEtfModule();
const settle = async (fn) => {
  await act(async () => {
    fn?.();
    await Promise.resolve();
  });
};
const tab = (tree, label) =>
  tree.root
    .findAll((n) => n.props.role === "tab")
    .find((n) => n.children.join("") === label);
const rowNodes = (tree) =>
  tree.root.findAll((n) => n.props.className === "etf-screener-row");
const names = (tree) =>
  rowNodes(tree).map((n) =>
    n
      .findAll((n) => n.props.className === "etf-symbol-button")[0]
      .children.join(""),
  );
const many = (count) =>
  Array.from({ length: count }, (_, i) =>
    candidate({
      global_instrument_id: `synthetic-${String(i).padStart(3, "0")}`,
      symbol: `ETF${String(i).padStart(3, "0")}`,
      fund_name: `Synthetic Fund ${i}`,
      rank: i + 1,
    }),
  );

for (const branch of [
  {
    global_instrument_id: "synthetic-failed",
    symbol: "FAILED",
    disposition: "TECHNICAL_FAILURE",
    reason: "BASELINE_ACQUISITION_TECHNICAL_FAILURE",
    recommendation: null,
    missing_data: [],
    limitations: [],
    historical_acquisition: null,
  },
  {
    global_instrument_id: null,
    symbol: "UNRESOLVED",
    disposition: "INELIGIBLE",
    reason: "CANONICAL_IDENTITY_NOT_FOUND",
    recommendation: null,
    historical_acquisition: null,
  },
])
  test(`production branch ${branch.disposition} renders Universe and lazy analysis without missing-array crash`, async () => {
    let tree;
    await settle(() => {
      tree = create(
        React.createElement(EtfRadarContent, {
          data: cycle([branch]),
          initialTab: "universe",
        }),
      );
    });
    assert.equal(
      tree.root.findAll(
        (n) => n.props.className === "opportunity-card etf-radar-card",
      ).length,
      0,
    );
    assert.match(textOf(tree), new RegExp(branch.symbol));
    await settle(() => button(tree, "Analysis").props.onClick());
    assert.match(textOf(tree), /Analysis not recorded/);
    assert.match(textOf(tree), /Risk assessment not recorded/);
    await settle(() => tree.unmount());
  });
test("Universe and Advanced Screener include unranked candidates, while Opportunities remains the full ranked set", () => {
  const ranked = candidate(),
    excluded = candidate({
      global_instrument_id: "excluded",
      symbol: "EXCLUDED",
      disposition: "INSUFFICIENT_DATA",
      rank: null,
      rule_engine_result: null,
    });
  for (const initialTab of ["universe", "screener"]) {
    const html = renderToStaticMarkup(
      React.createElement(EtfRadarContent, {
        data: cycle([ranked, excluded]),
        initialTab,
      }),
    );
    assert.match(html, /EXCLUDED/);
    assert.match(html, /2 of 2 results/);
    assert.doesNotMatch(
      html,
      /Qualified opportunity|buy recommendation unless/,
    );
  }
  const top = renderToStaticMarkup(
    React.createElement(EtfRadarContent, {
      data: cycle([ranked, excluded]),
      initialTab: "top",
    }),
  );
  assert.doesNotMatch(top, /EXCLUDED/);
  assert.match(top, /Preliminary research opportunity/);
});
test("pagination covers the whole ranked set, resets after filters and never eagerly mounts detail cards", async () => {
  let tree;
  await settle(() => {
    tree = create(
      React.createElement(EtfRadarContent, {
        data: cycle(many(61)),
        initialTab: "top",
      }),
    );
  });
  assert.equal(rowNodes(tree).length, 25);
  assert.match(textOf(tree), /61 of 61 results/);
  assert.equal(
    tree.root.findAll(
      (n) => n.props.className === "opportunity-card etf-radar-card",
    ).length,
    0,
  );
  await settle(() => button(tree, "Next page").props.onClick());
  assert.match(textOf(tree), /Page 2 of 3/);
  assert.equal(names(tree)[0], "ETF025");
  await settle(() => change(tree, "Minimum score / 100", "99"));
  assert.match(textOf(tree), /0 of 61 results/);
  assert.match(textOf(tree), /Page 1 of 1/);
  await settle(() => button(tree, "Reset filters").props.onClick());
  assert.equal(names(tree)[0], "ETF000");
  await settle(() => tree.unmount());
});
test("debounced search matches fund names, evidence sources and other displayed fields before pagination", async () => {
  const data = cycle(many(40));
  data.candidates[37] = {
    ...data.candidates[37],
    fund_name: "Unique synthetic long fund",
    source_references: [
      {
        metric: "NAV",
        value: "100",
        as_of: "2026-11-04",
        provider: "OFFICIAL_SYNTHETIC",
        url: "https://example.test",
      },
    ],
  };
  let tree;
  await settle(() => {
    tree = create(
      React.createElement(EtfRadarContent, { data, initialTab: "universe" }),
    );
  });
  const search = tree.root
    .findAllByType("input")
    .find((n) => n.props.type === "search");
  await settle(() =>
    search.props.onChange({ target: { value: "unique official_synthetic" } }),
  );
  assert.match(textOf(tree), /Updating search/);
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 240));
  });
  assert.deepEqual(names(tree), ["ETF037"]);
  assert.match(textOf(tree), /1 of 40 results/);
  await settle(() => tree.unmount());
});
test("combined category, status, confidence and numeric filters operate on all assessed candidates", async () => {
  const a = candidate({
      peer_group: "GOLD",
      screening_metrics: {
        aum: { value: "1000", unit: "INR", quality: "VALID" },
      },
    }),
    b = candidate({
      global_instrument_id: "b",
      symbol: "OTHER",
      peer_group: "DEBT",
      rank: null,
    });
  let tree;
  await settle(() => {
    tree = create(
      React.createElement(EtfRadarContent, {
        data: cycle([a, b]),
        initialTab: "screener",
      }),
    );
  });
  await settle(() => change(tree, "Peer category", "GOLD"));
  await settle(() => change(tree, "Confidence", "LOW"));
  await settle(() => change(tree, "Minimum AUM (INR)", "900"));
  assert.deepEqual(names(tree), ["SYNTHETICETF"]);
  await settle(() => change(tree, "Research status", "Insufficient data"));
  assert.equal(rowNodes(tree).length, 0);
  await settle(() => button(tree, "Reset filters").props.onClick());
  assert.equal(rowNodes(tree).length, 2);
  await settle(() => tree.unmount());
});
test("table sort toggles both directions and missing expense remains last", async () => {
  const data = cycle([
    candidate({
      global_instrument_id: "a",
      symbol: "LOW",
      screening_metrics: {
        expense_ratio: { value: ".1", unit: "PERCENT_PA", quality: "VALID" },
      },
    }),
    candidate({
      global_instrument_id: "b",
      symbol: "HIGH",
      screening_metrics: {
        expense_ratio: { value: ".5", unit: "PERCENT_PA", quality: "VALID" },
      },
    }),
    candidate({ global_instrument_id: "c", symbol: "MISSING" }),
  ]);
  let tree;
  await settle(() => {
    tree = create(
      React.createElement(EtfRadarContent, { data, initialTab: "universe" }),
    );
  });
  await settle(() => button(tree, "Expense ratio").props.onClick());
  assert.deepEqual(names(tree), ["LOW", "HIGH", "MISSING"]);
  await settle(() => button(tree, "Expense ratio \u25b2").props.onClick());
  assert.deepEqual(names(tree), ["HIGH", "LOW", "MISSING"]);
  await settle(() => tree.unmount());
});
test("analysis and chart mount only for selected canonical ETF; closing removes both", async () => {
  let tree;
  await settle(() => {
    tree = create(
      React.createElement(EtfRadarContent, {
        data: cycle(many(40)),
        initialTab: "universe",
      }),
    );
  });
  assert.equal(
    tree.root.findAll((n) => n.props["data-chart-instrument"]).length,
    0,
  );
  await settle(() =>
    tree.root
      .findAll((n) => n.props["aria-label"] === "Analyze ETF002")[0]
      .props.onClick(),
  );
  assert.equal(
    tree.root.findAll((n) => n.props["data-chart-instrument"])[0].props[
      "data-chart-instrument"
    ],
    "synthetic-002",
  );
  assert.equal(
    tree.root.findAll(
      (n) => n.props.className === "opportunity-card etf-radar-card",
    ).length,
    1,
  );
  await settle(() => button(tree, "Close analysis").props.onClick());
  assert.equal(
    tree.root.findAll((n) => n.props["data-chart-instrument"]).length,
    0,
  );
  await settle(() => tree.unmount());
});
test("comparison preserves full-candidate identities across pages, caps at five, and requires two columns", async () => {
  let tree;
  const data = cycle(many(8).map((c) => ({ ...c, rank: null })));
  await settle(() => {
    tree = create(
      React.createElement(EtfRadarContent, { data, initialTab: "universe" }),
    );
  });
  for (let i = 0; i < 5; i++)
    await settle(() =>
      tree.root
        .findAllByType("input")
        .find((n) => n.props["aria-label"] === `Compare ETF00${i}`)
        .props.onChange(),
    );
  assert.equal(
    tree.root
      .findAllByType("input")
      .find((n) => n.props["aria-label"] === "Compare ETF005").props.disabled,
    true,
  );
  await settle(() => tab(tree, "ETF Comparison (5)").props.onClick());
  assert.match(textOf(tree), /ETF004/);
  assert.match(textOf(tree), /Peer rank \(within category\)/);
  assert.equal(tree.root.findAllByType("table").length, 1);
  for (let i = 1; i < 5; i++)
    await settle(() => button(tree, `Remove ETF00${i}`).props.onClick());
  assert.equal(tree.root.findAllByType("table").length, 0);
  assert.match(textOf(tree), /Select one more ETF/);
  await settle(() => tree.unmount());
});
test("Category Leaders is filterable and paginated across every peer group, preserving category ranks", async () => {
  const rows = many(37).map((c, i) => ({
    ...c,
    peer_group: `PEER${i}`,
    rank: 1,
  }));
  let tree;
  await settle(() => {
    tree = create(
      React.createElement(EtfRadarContent, {
        data: cycle(rows),
        initialTab: "leaders",
      }),
    );
  });
  assert.match(textOf(tree), /37 of 37 results/);
  assert.equal(rowNodes(tree).length, 25);
  assert.match(textOf(tree), /within peer/);
  await settle(() => button(tree, "Next page").props.onClick());
  assert.equal(rowNodes(tree).length, 12);
  await settle(() => tree.unmount());
});
test("active tab is conditionally mounted, preserving selection without rendering every table", async () => {
  let tree;
  await settle(() => {
    tree = create(React.createElement(EtfRadarContent, { data: cycle() }));
  });
  assert.equal(tree.root.findAllByType("table").length, 0);
  await settle(() => tab(tree, "Research Universe").props.onClick());
  assert.equal(tree.root.findAllByType("table").length, 1);
  await settle(() => tab(tree, "Overview").props.onClick());
  assert.equal(tree.root.findAllByType("table").length, 0);
  await settle(() => tree.unmount());
});

test("numeric sort headers alternate descending and ascending on repeated clicks", async () => {
  const rows = [
    candidate({
      global_instrument_id: "a",
      symbol: "LOW",
      rule_engine_result: {
        overall_score: 10,
        data_completeness: 0.1,
        confidence: "LOW",
      },
    }),
    candidate({
      global_instrument_id: "b",
      symbol: "HIGH",
      rule_engine_result: {
        overall_score: 90,
        data_completeness: 0.9,
        confidence: "HIGH",
      },
    }),
  ];
  let tree;
  await settle(() => {
    tree = create(
      React.createElement(EtfRadarContent, {
        data: cycle(rows),
        initialTab: "universe",
      }),
    );
  });
  await settle(() => button(tree, "Score / 100").props.onClick());
  assert.deepEqual(names(tree), ["HIGH", "LOW"]);
  await settle(() => button(tree, "Score / 100 \u25bc").props.onClick());
  assert.deepEqual(names(tree), ["LOW", "HIGH"]);
  await settle(() => button(tree, "Score / 100 \u25b2").props.onClick());
  assert.deepEqual(names(tree), ["HIGH", "LOW"]);
  await settle(() => tree.unmount());
});
