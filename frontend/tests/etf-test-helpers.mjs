import fs from "node:fs";
import { createRequire } from "node:module";
import ts from "typescript";
import React from "react";
export { React };
export { create, act } from "react-test-renderer";
export { renderToStaticMarkup } from "react-dom/server";
const require = createRequire(import.meta.url);
export function loadEtfModule(
  name = "components/etf-radar",
  request = async () => {
    throw new Error("Unexpected request");
  },
) {
  const cache = new Map();
  function load(url) {
    if (cache.has(url.href)) return cache.get(url.href);
    const mod = { exports: {} };
    cache.set(url.href, mod.exports);
    const output = ts.transpileModule(fs.readFileSync(url, "utf8"), {
      compilerOptions: {
        jsx: ts.JsxEmit.ReactJSX,
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
      },
    }).outputText;
    const localRequire = (spec) => {
      if (spec.endsWith(".css")) return {};
      if (spec.includes("portfolio-api")) return { request };
      // Historical-series behaviour has its own dedicated chart tests. Here we
      // verify canonical selection and lazy mounting without issuing chart I/O.
      if (spec === "./etf-price-history")
        return {
          EtfPriceHistory: (props) =>
            React.createElement(
              "div",
              { "data-chart-instrument": props.instrumentId },
              "Price history",
            ),
        };
      if (spec.startsWith("."))
        for (const ext of [".tsx", ".ts"]) {
          const target = new URL(spec + ext, url);
          if (fs.existsSync(target)) return load(target);
        }
      return require(spec);
    };
    new Function("require", "module", "exports", output)(
      localRequire,
      mod,
      mod.exports,
    );
    return mod.exports;
  }
  return load(
    new URL(
      `../app/${name}.${name.startsWith("lib/") ? "ts" : "tsx"}`,
      import.meta.url,
    ),
  );
}
export function candidate(overrides = {}) {
  return {
    global_instrument_id: "synthetic-id-1",
    symbol: "SYNTHETICETF",
    fund_name: "Synthetic fund fixture",
    disposition: "EVALUATED",
    reason: "EVALUATED",
    subtype: "EQUITY_INDEX",
    peer_group: "EQUITY_INDEX",
    recommendation: "OPPORTUNITY",
    rank: 1,
    risk_gates: [],
    rule_engine_result: {
      rule_engine_version: "ETF_RULE_ENGINE_V1",
      instrument_id: "synthetic-id-1",
      as_of: "2026-11-05T10:00:00Z",
      factor_results: [],
      overall_score: 62.5,
      data_completeness: 0.375,
      confidence: "LOW",
    },
    ...overrides,
  };
}
export function cycle(candidates = [candidate()], overrides = {}) {
  return {
    radar_version: "ETF_RADAR_V1",
    cycle_id: "synthetic-cycle",
    correlation_id: null,
    as_of: "2026-11-05T10:00:00Z",
    universe_count: candidates.length,
    candidates,
    ranked: candidates.filter((c) => c.rank != null),
    top_opportunities: [],
    excluded_by_reason: {},
    ...overrides,
  };
}
export const textOf = (tree) => {
  const text = (value) =>
    value == null
      ? ""
      : typeof value === "string" || typeof value === "number"
        ? String(value)
        : Array.isArray(value)
          ? value.map(text).join("")
          : text(value.children);
  return text(tree.toJSON());
};
export const button = (tree, label) =>
  tree.root.findAllByType("button").find((n) => n.children.join("") === label);
export const change = (tree, label, value) =>
  tree.root
    .findAllByType("label")
    .find((n) =>
      n.children.some(
        (c) =>
          c === label ||
          (typeof c === "object" && c.children?.join("") === label),
      ),
    )
    .findAll((n) => n.type === "input" || n.type === "select")[0]
    .props.onChange({ target: { value } });
