import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const workspace = readFileSync(new URL("../app/components/investment-workspace.tsx", import.meta.url), "utf8");

test("immediate connected IBKR authentication bootstraps its broker portfolio exactly once", () => {
  assert.match(workspace, /ibkrBootstrapSyncsRef = useRef\(new Map<string, Promise<boolean>>\(\)\)/);
  assert.match(workspace, /if \(brokerType === "IBKR" && action\.status === "CONNECTED"\)[\s\S]*?ibkrBootstrapSyncsRef\.current\.delete\(action\.connectionId\);[\s\S]*?await bootstrapIbkrPortfolio\(action\.connectionId\)/);
  assert.match(workspace, /const existing = ibkrBootstrapSyncsRef\.current\.get\(connectionId\);[\s\S]*?if \(existing\) return existing;/);
  assert.match(workspace, /portfolioApi\.syncBrokerConnection\(connectionId\)/);
});

test("IBKR polling bootstraps once on authenticated CONNECTED and never on page load", () => {
  const refresh = workspace.match(/async function refreshAuthenticatedBroker[\s\S]*?\n  }\n\n  async function bootstrapIbkrPortfolio/)?.[0] ?? "";
  assert.match(refresh, /if \(!authStatus\.authenticated \|\| authStatus\.state !== "CONNECTED"\) return false/);
  assert.match(refresh, /pendingBrokerAuthRef\.current\?\.provider === "IBKR"[\s\S]*?await bootstrapIbkrPortfolio\(connectionId\)/);
  assert.match(workspace, /if \(provider === "IBKR"\)[\s\S]*?ibkrBootstrapSyncsRef\.current\.delete\(connectionId\);[\s\S]*?pendingBrokerAuthRef\.current = \{ connectionId, provider \}/);
  const initialLoad = workspace.match(/async function loadPortfolios[\s\S]*?\n    }\n\n    void loadPortfolios/)?.[0] ?? "";
  assert.doesNotMatch(initialLoad, /syncBrokerConnection|bootstrapIbkrPortfolio/);
});

test("bootstrap refreshes portfolios and selects only a returned or connection-linked portfolio", () => {
  const bootstrap = workspace.match(/async function bootstrapIbkrPortfolio[\s\S]*?\n  }\n\n  async function finishBrokerAuthentication/)?.[0] ?? "";
  assert.match(bootstrap, /const dashboard = await portfolioApi\.getDashboard\(\)/);
  assert.match(bootstrap, /setPortfolios\(dashboard\.portfolios\)/);
  assert.match(bootstrap, /result\.portfolios\.find\(\(portfolio\) =>[\s\S]*?portfolio\.brokerConnectionId === connectionId/);
  assert.match(bootstrap, /dashboard\.portfolios\.find\(\(portfolio\) =>[\s\S]*?portfolio\.brokerConnectionId === connectionId/);
  assert.doesNotMatch(bootstrap, /portfolio\.name ===|find\(\(portfolio\) => portfolio\.name/);
  assert.match(bootstrap, /if \(current\) return current;/);
});

test("bootstrap failure is sanitized, leaves authentication state untouched, and does not sync non-IBKR auth outcomes", () => {
  const bootstrap = workspace.match(/async function bootstrapIbkrPortfolio[\s\S]*?\n  }\n\n  async function finishBrokerAuthentication/)?.[0] ?? "";
  assert.match(bootstrap, /Interactive Brokers connected, but portfolio sync failed\. Try syncing again\./);
  assert.doesNotMatch(bootstrap, /setBrokerConnections\(\[\]|DISCONNECTED|authenticationAction/);
  const completion = workspace.match(/async function completeBrokerAuthentication[\s\S]*?\n  }\n\n  async function login/)?.[0] ?? "";
  assert.match(completion, /action\.action === "NONE"/);
  const redirectBranch = completion.match(/if \(action\.action === "REDIRECT_REQUIRED"[\s\S]*?authWindow\.close\(\);/)?.[0] ?? "";
  assert.doesNotMatch(redirectBranch, /bootstrapIbkrPortfolio/);
  assert.match(completion, /if \(action\.action === "NONE"\)[\s\S]*?else \{[\s\S]*?setBrokerCardError\(brokerType, action\.message\)/);
});

test("manual selected-portfolio sync and non-IBKR authentication flows stay separate", () => {
  assert.match(workspace, /async function syncSelectedPortfolio\(\)[\s\S]*?portfolioApi\.syncBrokerConnection\(connectionId\)/);
  assert.match(workspace, /brokerType === "IBKR" && action\.status === "CONNECTED"/);
  assert.doesNotMatch(workspace, /brokerType === "ICICI_DIRECT"[\s\S]{0,100}bootstrapIbkrPortfolio/);
  assert.doesNotMatch(workspace, /brokerType === "HDFC_SECURITIES"[\s\S]{0,100}bootstrapIbkrPortfolio/);
});
