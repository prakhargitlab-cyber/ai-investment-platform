import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const workspace = readFileSync(new URL("../app/components/investment-workspace.tsx", import.meta.url), "utf8");
const api = readFileSync(new URL("../app/lib/portfolio-api.ts", import.meta.url), "utf8");
const styles = readFileSync(new URL("../app/styles.css", import.meta.url), "utf8");

test("renders persisted multi-portfolio totals while keeping holdings out of the dashboard", () => {
  assert.match(workspace, />All<\/button>/);
  assert.match(workspace, /Object\.entries\(dashboard\.currencyTotals\)/);
  assert.match(workspace, /dashboard\.portfolios\.map/);
  assert.doesNotMatch(workspace, /dashboard\.combinedHoldings\.map/);
});

test("manual-import display-name editing lives inside the holding drawer", () => {
  assert.match(workspace, /position\.sourceType === "MANUAL_CSV_IMPORT"/);
  assert.match(workspace, />Edit<\/button>/);
  assert.match(workspace, />Save<\/Button>/);
  assert.match(workspace, />Cancel<\/Button>/);
  assert.doesNotMatch(workspace, />Edit name<\/button>/);
  assert.match(workspace, /maxLength=\{160\}/);
  assert.match(api, /positions\/\$\{positionId\}\/display-name/);
  assert.doesNotMatch(workspace, /dangerouslySetInnerHTML/);
  assert.match(workspace, /<strong>\{position\.displayName\}<\/strong>/);
});

test("research selection uses global instrument identity rather than a display ticker alias", () => {
  assert.match(api, /globalInstrumentId\?: string \| null/);
  assert.match(workspace, /company\.instrumentId === globalInstrumentId/);
  assert.match(workspace, /value: globalInstrumentId \?\? resolved\?\.instrumentId \?\? instrument\.instrumentId/);
  assert.match(workspace, /globalInstrumentId,/);
  assert.match(workspace, /canRefreshResearch\(company\)/);
  assert.doesNotMatch(workspace, /KPITE\s*[-=]>\s*KPITTECH/);
});

test("refresh eligibility is based on canonical global identity, not a portfolio-summary row or existing research", () => {
  const eligibility = workspace.match(/function canRefreshResearch[\s\S]*?\n}\n\nfunction researchEmptyTitle/)?.[0] ?? "";
  assert.match(eligibility, /Boolean\(company\?\.instrumentId\)/);
  assert.doesNotMatch(eligibility, /documentCount|eventCount|confidence|freshness|lastRefresh/);
  assert.match(eligibility, /"COMPANY_NOT_RESOLVED"/);
  assert.match(eligibility, /"RESEARCH_NOT_APPLICABLE"/);
  assert.match(eligibility, /"ETF_UNSUPPORTED"/);
  assert.match(workspace, /function canRefreshResearchIdentity\([\s\S]*?Boolean\(globalInstrumentId\?\.trim\(\)\)/);
  assert.match(workspace, /selectedContextResearchCompany\?\.status\s*\?\? \(selectedContextResearchCompany \? undefined : selectedResearchOption\?\.status\)/);
  assert.match(workspace, /disabled=\{loading \|\| !refreshEligible\}/);
  assert.match(workspace, /selectedResearchInstrumentId=\{selectedResearchInstrumentId\}/);
  assert.match(workspace, /openResearchReadiness\([\s\S]*?selectedResearchInstrumentId/);
});

test("Talbros/Ujjivan-shaped global selections are refreshable while unresolved and unsupported selections are not", () => {
  const eligibility = workspace.match(/function canRefreshResearchIdentity[\s\S]*?\n}\n\nfunction researchEmptyTitle/)?.[0] ?? "";
  // The helper deliberately ignores documents/events/confidence/freshness and
  // gates only canonical identity, active loading, explicit status, and asset type.
  assert.doesNotMatch(eligibility, /documentCount|eventCount|confidence|freshness|lastRefresh/);
  assert.match(workspace, /globalInstrumentId \? "GLOBAL_INSTRUMENT_RESOLVED" : "COMPANY_NOT_RESOLVED"/);
  assert.match(eligibility, /"COMPANY_NOT_RESOLVED"/);
  assert.match(eligibility, /"ETF_UNSUPPORTED"/);
  assert.match(eligibility, /\["ETF", "FUND", "BOND", "CASH", "CRYPTO"\]/);
  assert.match(workspace, /value: globalInstrumentId \?\? resolved\?\.instrumentId/);
  assert.match(workspace, /openResearchReadiness\([\s\S]*?selectedResearchInstrumentId/);
});

test("portfolio detail and research-table drawers join global research to a different local holding ID", () => {
  const holdingsView = workspace.match(/function HoldingsTable\([\s\S]*?\n}\n\nfunction metricText/)?.[0] ?? "";
  assert.match(holdingsView, /company\.instrumentId === position\.instrument\.globalInstrumentId/);
  assert.match(holdingsView, /company\.instrumentId === position\.instrument\.instrumentId/);
  assert.match(holdingsView, /<StockResearchDrawer position=\{detail\.position\} research=\{detail\.researchInstrumentId \? portfolioResearch\?\.companies\.find/);
  assert.match(workspace, /position\.instrument\.globalInstrumentId === company\.instrumentId/);
  assert.match(workspace, /market\?\.resolution\.providerTicker/);
  assert.match(workspace, /research\?\.structuredMarket\?\.facts\[key\]/);
  assert.match(workspace, /latestQuarterlyResult|shareholdingChanges|currentQuarterCatalysts|sourceDiversity/);
});

test("renders public broker cards and hides the demo provider in normal mode", () => {
  assert.match(workspace, /visibleProviders\.map/);
  assert.match(workspace, /provider\.brokerType !== "MOCK"/);
  assert.match(workspace, /provider\.connectable/);
  assert.match(workspace, /provider\.unavailableReason \?\? "Connection not available yet\."/);
  assert.match(api, /\/api\/v1\/brokers/);
});

test("uses generic sync and authentication continuation contracts", () => {
  assert.match(api, /portfolios\/broker-connections\/\$\{connectionId\}\/sync/);
  assert.match(workspace, /refreshAuthenticatedBroker/);
  assert.match(workspace, /REDIRECT_REQUIRED/);
});

test("consumer broker flow never asks for retail API credentials", () => {
  assert.match(api, /PARTNER_AUTH_UNAVAILABLE/);
  assert.doesNotMatch(workspace, /USER_CREDENTIALS_REQUIRED|broker password|OTP|MFA/);
  assert.match(workspace, /provider\.individualApiSupported && provider\.advancedIndividualMode/);
  assert.match(workspace, /activeConnection\.brokerType === "IBKR" \? "Connect \/ Re-authenticate" : "Connect"/);
});

test("opens a generic broker authentication popup synchronously before API work", () => {
  assert.match(workspace, /window\.open\("about:blank", "aip-ibkr-auth", features\)/);
  assert.match(workspace, /popup=yes,width=\$\{width\},height=\$\{height\},left=\$\{left\},top=\$\{top\},resizable=yes,scrollbars=yes/);
  assert.match(workspace, /const authWindow = openBrokerAuthenticationWindow\(provider\.brokerType\);[\s\S]*?await brokerApi\.connectBroker/);
  assert.match(workspace, /authWindow\.location\.assign\(action\.authenticationUrl\)/);
  assert.match(workspace, /action\.action === "REDIRECT_REQUIRED" \|\| action\.action === "POPUP_REQUIRED"/);
});

test("popup flow handles blocked windows, NONE, failures, and missing URLs safely", () => {
  assert.match(workspace, /Your browser blocked the IBKR sign-in window\. Allow pop-ups for this site and try again\./);
  assert.match(workspace, /if \(action\.action === "NONE"\)/);
  assert.match(workspace, /if \(!action\.authenticationUrl\)/);
  assert.match(workspace, /authWindow\.close\(\)/);
  assert.match(workspace, /Your saved portfolio remains available/);
});

test("popup handling is provider-neutral and unsupported providers never receive a URL", () => {
  assert.doesNotMatch(workspace, /authenticationUrl.*IBKR|authenticationUrl.*ICICI|authenticationUrl.*HDFC/);
  assert.match(api, /"UNAVAILABLE" \| "UNSUPPORTED"/);
});

test("keeps market-price and broker-sync freshness visibly separate", () => {
  assert.match(workspace, /Broker holdings synced:/);
  assert.match(workspace, /Market price updated/);
});

test("authentication-required IBKR uses re-authentication and broker cards do not offer Sync", () => {
  const brokerView = workspace.match(/function BrokerView\([\s\S]*?\nfunction ResearchView/)?.[0] ?? "";
  assert.match(brokerView, /Connect \/ Re-authenticate/);
  assert.doesNotMatch(brokerView, />Sync</);
  assert.match(brokerView, /Scalable broker authentication|unavailableReason/);
});

test("connected IBKR maps to Manage and Disconnect", () => {
  const brokerView = workspace.match(/function BrokerView\([\s\S]*?\nfunction ResearchView/)?.[0] ?? "";
  assert.match(brokerView, /activeConnection && connected \? <Button variant="secondary" disabled>Manage<\/Button>/);
  assert.match(brokerView, />Disconnect<\/Button>/);
});

test("disconnected and error IBKR remain recoverable instead of Disconnect-only", () => {
  const brokerView = workspace.match(/function BrokerView\([\s\S]*?\nfunction ResearchView/)?.[0] ?? "";
  assert.match(brokerView, /const recoverable = Boolean\(activeConnection\) && !connected/);
  assert.match(brokerView, /activeConnection && recoverable/);
  assert.match(brokerView, /Connect \/ Re-authenticate/);
});

test("authentication-required IBKR exposes the reconnect action", () => {
  assert.match(workspace, /authenticationRequired[\s\S]*?recoverable/);
  assert.match(workspace, /activeConnection\.brokerType === "IBKR" \? "Connect \/ Re-authenticate" : "Connect"/);
});

test("HDFC and ICICI partner-unavailable Connect does not open a popup", () => {
  assert.match(workspace, /provider\.consumerAuthMode === "PARTNER_UNAVAILABLE"[\s\S]*?Direct customer account connection is not available yet\.[\s\S]*?return;[\s\S]*?openBrokerAuthenticationWindow/);
  assert.match(api, /consumerAuthMode: "BROKER_REDIRECT" \| "PARTNER_OAUTH" \| "INDIVIDUAL_API_CREDENTIALS" \| "PARTNER_UNAVAILABLE" \| "NONE"/);
});

test("partner-unavailable and authentication errors are broker-card-local", () => {
  const brokerView = workspace.match(/function BrokerView\([\s\S]*?\nfunction ResearchView/)?.[0] ?? "";
  assert.match(brokerView, /errors\[provider\.brokerType\]/);
  assert.doesNotMatch(brokerView, /\{error \? <div className="broker-inline-error"/);
});

test("broker authentication failure preserves the Brokers page", () => {
  assert.match(workspace, /setBrokerCardError\(brokerType, getApiFailure\(err\)\.message\)/);
  assert.match(workspace, /visibleProviders\.map/);
  assert.doesNotMatch(workspace, /setError\(getApiFailure\(err\)\)[\s\S]{0,120}completeBrokerAuthentication/);
});

test("normal broker rendering consumes the backend canonical connection list", () => {
  const brokerView = workspace.match(/function BrokerView\([\s\S]*?\nfunction ResearchView/)?.[0] ?? "";
  assert.match(api, /listConnections: \(\) => request<BrokerConnection\[]>\("\/api\/v1\/broker-connections"\)/);
  assert.match(brokerView, /connections\.find\(\(connection\) => connection\.brokerType === provider\.brokerType\)/);
  assert.match(brokerView, /linkedPortfolio[\s\S]*?portfolio\.brokerConnectionId === activeConnection\.connectionId/);
});

test("partner unavailable has a dedicated badge and no meaningless disconnect", () => {
  const brokerView = workspace.match(/function BrokerView\([\s\S]*?\nfunction ResearchView/)?.[0] ?? "";
  assert.match(brokerView, /partnerUnavailable \? "Direct connection unavailable"/);
  assert.match(brokerView, /Direct customer account connection is not available yet\./);
  assert.match(brokerView, /meaningfulPersistedLinkage/);
  assert.match(brokerView, /!partnerUnavailable \|\| meaningfulPersistedLinkage/);
  assert.match(brokerView, /!partnerUnavailable && !developerIndividualMode && !activeConnection && provider\.connectable/);
  assert.match(brokerView, /!partnerUnavailable && !developerIndividualMode && activeConnection && recoverable/);
  assert.match(brokerView, /!partnerUnavailable && errors\[provider\.brokerType\]/);
});

test("provider metadata drives partner, unavailable, and advanced individual modes", () => {
  assert.match(api, /"PARTNER_OAUTH" \| "INDIVIDUAL_API_CREDENTIALS" \| "PARTNER_UNAVAILABLE"/);
  assert.match(api, /individualApiSupported: boolean/);
  assert.match(api, /advancedIndividualMode: boolean/);
  assert.match(workspace, /developerIndividualMode \? \(/);
  assert.match(workspace, /Configure DEV API/);
  assert.match(workspace, /Credentials are stored write-only/);
  assert.match(workspace, /setDeveloperSecret\(""\)/);
  assert.doesNotMatch(workspace, /provider\.brokerType === "HDFC_SECURITIES".*developer|provider\.brokerType === "ICICI_DIRECT".*developer/);
});

test("IBKR redirect keeps its popup open and NONE closes it", () => {
  assert.match(workspace, /action\.action === "REDIRECT_REQUIRED" \|\| action\.action === "POPUP_REQUIRED"/);
  assert.match(workspace, /authWindow\.location\.assign\(action\.authenticationUrl\)[\s\S]*?return true/);
  assert.match(workspace, /authWindow\.close\(\);[\s\S]*?if \(action\.action === "NONE"\)/);
});

test("saved IBKR portfolio remains rendered while authentication is required", () => {
  const brokerView = workspace.match(/function BrokerView\([\s\S]*?\nfunction ResearchView/)?.[0] ?? "";
  assert.match(brokerView, /Authentication is required to refresh holdings\. Your saved portfolio remains available\./);
  assert.match(brokerView, /linkedPortfolio \? <p className="broker-linked-portfolio">/);
});

test("Sync is rendered only when a linked broker portfolio supplies an action", () => {
  assert.match(workspace, /selectedPortfolio\?\.brokerConnectionId \? syncSelectedPortfolio : undefined/);
  assert.match(workspace, /\{onSync \? <Button/);
});

test("broker status badges and actions cannot wrap character by character", () => {
  assert.match(styles, /\.broker-card \.badge,[\s\S]*?white-space: nowrap/);
  assert.match(styles, /word-break: normal/);
});

test("persisted broker portfolios remain visible with unknown historical sync time", () => {
  assert.match(workspace, /Imported previously; sync time unknown/);
  assert.match(workspace, /Your saved portfolio remains available/);
});

test("does not expose connector or session internals in frontend connection types", () => {
  const publicConnectionType = api.match(/export type BrokerConnection = \{[\s\S]*?\n\};/)?.[0] ?? "";
  assert.doesNotMatch(publicConnectionType, /connectorId/);
  assert.doesNotMatch(publicConnectionType, /sessionReference/);
  assert.doesNotMatch(publicConnectionType, /externalAccountReference/);
  const descriptorType = api.match(/export type BrokerProviderInfo = \{[\s\S]*?\n\};/)?.[0] ?? "";
  assert.doesNotMatch(descriptorType, /authenticationModel|capabilities|connectionMethod/);
});

test("manual import uses the exact multipart preview contract and derives browser-suffixed accounts", () => {
  assert.match(api, /body\.append\("file", file\)/);
  assert.match(api, /init\?\.body instanceof FormData \? \{\} : \{ "Content-Type": "application\/json" \}/);
  assert.doesNotMatch(api, /previewImport[\s\S]{0,500}"Content-Type": "multipart\/form-data"/);
  assert.match(api, /imports\/\$\{brokerType\.toLowerCase\(\)\}\/preview/);
  assert.match(workspace, /detectedImportAccount/);
  assert.match(workspace, /PortFolioEqtSummary/);
  assert.match(workspace, /Invest Right Equity Portfolio_/);
  assert.match(workspace, /Preview portfolio/);
});

test("manual import is a polished dialog with statement prices and useful backend errors", () => {
  assert.match(workspace, /role="dialog"/);
  assert.match(workspace, /import-modal-backdrop/);
  assert.match(workspace, /Statement price/);
  assert.match(workspace, /getApiFailure\(error\)\.message/);
  assert.match(styles, /\.import-dropzone/);
  assert.match(workspace, /Portfolio imported successfully/);
});

test("manual portfolios expose explicit price refresh without a selection-triggered sweep", () => {
  assert.match(api, /refreshPrices: \(portfolioId: string\)/);
  assert.match(api, /\/prices\/refresh/);
  assert.match(api, /importedPrice\?: Money \| null/);
  assert.match(workspace, /acquisitionSource === "MANUAL_CSV_IMPORT" \? refreshSelectedPrices/);
  assert.doesNotMatch(workspace, /automaticallyRefreshedPricesRef/);
  assert.doesNotMatch(workspace, /void refreshSelectedPrices\(\)/);
  assert.match(workspace, /Refresh Prices/);
  assert.match(workspace, /Latest Price/);
  assert.match(workspace, /Imported Price/);
  assert.match(workspace, /Provider/);
  assert.match(workspace, /Market As Of/);
  assert.match(workspace, /Retrieved At/);
});

test("IBKR authentication completion message resumes the canonical connection", () => {
  assert.match(workspace, /aip:ibkr-authenticated/);
  assert.match(workspace, /event\.origin !== window\.location\.origin/);
  assert.match(workspace, /finishBrokerAuthentication\(pending\.connectionId\)/);
});

test("IBKR popup lifecycle retains one popup and polls the canonical connection", () => {
  assert.match(workspace, /brokerAuthPopupRef = useRef<Window \| null>/);
  assert.match(workspace, /brokerAuthPopupRef\.current\.focus\(\)/);
  assert.match(workspace, /pendingBrokerAuthRef\.current = \{ connectionId, provider \}/);
  assert.match(workspace, /window\.setInterval\(\(\) => void poll\(\), brokerAuthenticationPollMs\)/);
  assert.match(workspace, /brokerApi\.getAuthStatus\(connectionId\)/);
  assert.match(workspace, /if \(!authStatus\.authenticated \|\| authStatus\.state !== "CONNECTED"\) return false/);
  assert.match(workspace, /brokerApi\.authenticationAction\(connectionId\)/);
  assert.match(workspace, /brokerApi\.listConnections\(\)/);
  assert.match(workspace, /portfolioApi\.getDashboard\(\)/);
  assert.match(workspace, /portfolioApi\.getPositions\(linkedPortfolio\.portfolioId\)/);
  assert.match(workspace, /stopBrokerAuthenticationMonitoring\(true\)/);
  assert.match(workspace, /if \(authWindow\.closed\)[\s\S]*?refreshAuthenticatedBroker\(connectionId\)[\s\S]*?stopBrokerAuthenticationMonitoring\(false\)/);
  assert.match(workspace, /Waiting for IBKR authentication…/);
  assert.match(workspace, /!authenticationPending[\s\S]*?onAuthenticate/);
});

test("research uses a clean company name while preserving structured identity", () => {
  assert.match(workspace, /function legacyCompositeCompanyName/);
  assert.match(workspace, /segments\.length >= 3 && segments\[2\] \? segments\[2\]/);
  assert.match(workspace, /position\.customDisplayName\?\.trim\(\)/);
  assert.match(workspace, /instrument\.companyName\?\.trim\(\)/);
  assert.match(workspace, /instrument\.canonicalName\?\.trim\(\) \|\| resolved\?\.companyName\?\.trim\(\)/);
  assert.match(workspace, /ticker: displayTicker,[\s\S]*?exchange: displayExchange/);
  assert.match(workspace, /\{option\.companyName\}/);
  assert.doesNotMatch(workspace, /label: `\$\{displayTicker\} \/ \$\{displayExchange\} \/ /);
  assert.match(workspace, /title=\{option\.companyName\}/);
  assert.match(workspace, /join\(" · "\)/);
  assert.match(styles, /\.research-company-select option[\s\S]*?background: var\(--research-option-bg\)[\s\S]*?color: var\(--research-option-text\)/);
  assert.match(styles, /\.research-company-select option:hover,[\s\S]*?\.research-company-select option:checked/);
  assert.match(styles, /\.research-company-select:focus-visible/);
  assert.match(styles, /text-overflow: ellipsis/);
});
