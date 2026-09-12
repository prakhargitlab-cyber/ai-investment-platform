import { frontendConfig } from "../config";

export type Money = {
  amount: number;
  currency: string;
};

export type Portfolio = {
  portfolioId: string;
  name: string;
  baseCurrency: string;
  createdAt: string;
  updatedAt: string;
  provider?: string | null;
  brokerConnectionId?: string | null;
  lastBrokerSyncAt?: string | null;
  lastBrokerSyncAttemptAt?: string | null;
  lastBrokerSyncErrorCode?: string | null;
  acquisitionSource?: "BROKER_API" | "MANUAL_CSV_IMPORT" | null;
  sourceAccountReference?: string | null;
  lastImportedAt?: string | null;
  lastImportedFilename?: string | null;
};

export type PortfolioListItem = {
  portfolioId: string;
  name: string;
  baseCurrency: string;
  totalMarketValue: Money | null;
  unrealizedProfitLoss: Money | null;
  unrealizedProfitLossPercent: number | null;
  positions: number;
  updatedAt: string;
  provider?: string | null;
  brokerConnectionId?: string | null;
  lastBrokerSyncAt?: string | null;
  lastBrokerSyncAttemptAt?: string | null;
  lastBrokerSyncErrorCode?: string | null;
  valuationComplete: boolean;
  acquisitionSource?: "BROKER_API" | "MANUAL_CSV_IMPORT" | null;
  sourceAccountReference?: string | null;
  lastImportedAt?: string | null;
  lastImportedFilename?: string | null;
};

export type PortfolioDashboard = {
  currencyTotals: Record<string, number>;
  portfolios: PortfolioListItem[];
  incompleteValuationPortfolioIds: string[];
};

export type SectorPerformanceStock = { globalInstrumentId: string; companyName: string; ticker: string; exchange: string; currency: string; latestPrice: number; referencePrice: number; performancePct: number | null; };
export type SectorPerformance = { region: "USA" | "EUROPE" | "INDIA"; sector: string; period: "DAY" | "WEEK" | "MONTH" | "YEAR"; asOf?: string | null; bestPerformers: SectorPerformanceStock[]; worstPerformers: SectorPerformanceStock[] };
export type MarketUniverseSector = { name: string; instrumentCount: number };
export type MarketUniverseSectors = { region: SectorPerformance["region"]; sectors: MarketUniverseSector[] };

export type ResearchInstrumentMatch = {
  canonicalSymbol: string;
  globalInstrumentId: string;
  companyName: string;
  symbol: string;
  exchange: string;
  mic?: string;
  country: string;
  currency?: string;
  isin?: string;
  sector?: string | null;
  industry?: string | null;
  assetType: string;
  score?: number | null;
  region: SectorPerformance["region"];
};
export type ResearchRequirementStatus = "READY_FRESH" | "READY_STALE" | "PARTIAL" | "MISSING"
  | "CONFLICTING" | "UNSUPPORTED" | "REFRESHING" | "FAILED" | "NOT_APPLICABLE";
export type ResearchSupportedAction = "FIND_DATA" | "UPLOAD_EVIDENCE" | "RUN_PARTIAL_ANALYSIS";
export type ResearchReadinessRequirement = {
  requirementId: string;
  area: string;
  areaWeightPct: number;
  importance: "MANDATORY" | "IMPORTANT" | "SUPPORTING";
  mandatory: boolean;
  status: ResearchRequirementStatus;
  applicability?: "APPLICABLE" | "PARTIALLY_APPLICABLE" | "NOT_APPLICABLE" | "UNKNOWN";
  applicabilityReason?: string | null;
  businessClassification?: string | null;
  classificationSource?: string | null;
  acquisitionObservation?: { outcome: string; provider: string; observed_at: string; failure_reason?: string | null; history?: { outcome: string; provider: string; observed_at: string }[] } | null;
  sourceProvider?: string | null;
  sourceTier?: string | null;
  sourceUrl?: string | null;
  asOf?: string | null;
  retrievedAt?: string | null;
  ageSeconds?: number | null;
  freshnessPolicy: {
    policyId: string;
    mode: string;
    maximumAgeSeconds?: number | null;
    scoringWindowDays?: number | null;
  };
  evidenceIds: string[];
  coveredInputIds: string[];
  missingInputIds: string[];
  concreteRequirements: Array<{ inputId: string; importance: "MANDATORY" | "IMPORTANT" | "SUPPORTING"; covered: boolean; applicability?: string; applicabilityReason?: string | null }>;
  coveragePct: number;
  criticalCoveragePct: number;
  missingReason?: string | null;
  conflictReason?: string | null;
  supportedActions: ResearchSupportedAction[];
};
export type ResearchReadiness = {
  globalInstrumentId: string;
  overallStatus: ResearchRequirementStatus;
  overallCompletenessPct: number;
  criticalCompletenessPct: number;
  confidence: "HIGH" | "MEDIUM" | "LOW";
  confidencePct: number;
  generatedAt: string;
  requirements: ResearchReadinessRequirement[];
  analysisEligibility?: {
    fullAnalysisAllowed: boolean;
    partialAnalysisAllowed: boolean;
    blockingRequirements: string[];
    reason: string;
  };
  refreshState?: {
    plannedRequirements: string[];
    executedCapabilities: string[];
    reusedSingleFlight: boolean;
    failureReasons: Record<string, string>;
  };
};
export type StockRuleEngineMetric = {
  metric: string;
  value: unknown;
  unit?: string | null;
  score: number;
  rule: string;
  configuredSubruleWeight: number;
  appliedWeightPct: number;
  source: string;
  sourceUrl?: string | null;
  asOf?: string | null;
  evidenceReferences: string[];
};
export type StockRuleEngineAreaScore = {
  area: string;
  weight: number;
  rawScore?: number | null;
  weightedContribution: number;
  status: "READY_FRESH" | "READY_STALE" | "PARTIAL" | "CONFLICTING" | "UNSCORABLE" | "UNSUPPORTED" | "NOT_APPLICABLE";
  applicable: boolean;
  metrics: StockRuleEngineMetric[];
  positiveFactors: string[];
  negativeFactors: string[];
  evidenceReferences: string[];
  sourceReferences: Array<{
    sourceProvider?: string | null;
    sourceTier?: string | null;
    sourceUrl: string;
    asOf?: string | null;
    retrievedAt?: string | null;
    evidenceReferences: string[];
  }>;
  missingInputs: string[];
};
export type StockRuleEngineAnalysis = {
  ruleEngineVersion: string;
  calculatedAt: string;
  globalInstrumentId: string;
  inputAsOf?: string | null;
  inputFingerprint: string;
  overallScore?: number | null;
  qualityScore?: number | null;
  opportunityScore?: number | null;
  riskScore?: number | null;
  confidenceScore: number;
  confidence: "HIGH" | "MEDIUM" | "LOW";
  decisionSignal: "STRONG_BUY" | "BUY" | "ACCUMULATE" | "HOLD" | "REDUCE" | "AVOID" | "EXIT_REVIEW" | "INSUFFICIENT_DATA";
  partial: boolean;
  cacheHit: boolean;
  eligibility: NonNullable<ResearchReadiness["analysisEligibility"]>;
  areaScores: StockRuleEngineAreaScore[];
  riskOverrides: Array<{ code: string; severity: "HIGH" | "CRITICAL"; effect: "BLOCK_BUY"; evidenceIds: string[] }>;
  missingInputs: string[];
  evidenceReferences: string[];
};
export type ResearchWatchlist = {
  watchlistId: string;
  name: "IND-WATCHLIST" | "EU-WATCHLIST" | "USA-WATCHLIST" | string;
  region: SectorPerformance["region"];
  systemDefault: boolean;
  instrumentCount: number;
  createdAt: string;
  updatedAt: string;
};
export type WatchlistResearchInstrument = {
  globalInstrumentId: string;
  companyName?: string | null;
  ticker?: string | null;
  exchange?: string | null;
  country?: string | null;
  currency?: string | null;
  assetType?: string | null;
  held: false;
  sourcePeriod?: SectorPerformance["period"] | null;
  sourcePerformancePct?: number | null;
  addedAt?: string | null;
  company: PortfolioResearchCompany;
};
export type WatchlistResearchPresentation = {
  watchlist: ResearchWatchlist;
  instruments: WatchlistResearchInstrument[];
};
export type MarketDataEnsureStatus = {
  region: "INDIA";
  universe: { status: "FRESH" | "STALE" | "REFRESH_STARTED" | "UNAVAILABLE"; lastUpdatedAt?: string | null };
  historicalPrices: {
    status: "FRESH" | "STALE" | "POPULATION_STARTED" | "RUNNING" | "UNAVAILABLE";
    jobId?: string | null;
    lastObservedAt?: string | null;
    eligibleInstruments?: number;
    coveredInstruments?: number;
  };
};

export type Instrument = {
  instrumentId: string;
  globalInstrumentId?: string | null;
  provider?: string | null;
  providerInstrumentId?: string | null;
  isin?: string | null;
  ticker: string;
  exchange: string;
  mic?: string | null;
  companyName: string;
  assetType: string;
  country?: string | null;
  tradingCurrency: string;
  sector?: string | null;
  industry?: string | null;
  brokerSymbol?: string | null;
  brokerDescription?: string | null;
  brokerExchange?: string | null;
  canonicalSymbol?: string | null;
  canonicalName?: string | null;
  canonicalExchange?: string | null;
  canonicalMic?: string | null;
  securityType?: string | null;
  providerMappings?: Array<{
    provider: string;
    providerSymbol?: string | null;
    providerInstrumentId?: string | null;
    exchange?: string | null;
     currency?: string | null;
     status: string;
     resolutionSource?: string | null;
     failureReason?: string | null;
     confidence?: number | null;
   }>;
};

export type Quote = {
  bid?: Money;
  ask?: Money;
  last?: Money;
  previousClose?: Money;
  currency?: string;
  timestamp?: string;
  source?: string;
  freshness?: "REAL_TIME" | "DELAYED" | "END_OF_DAY" | "STALE" | "MOCK" | "UNAVAILABLE";
  marketStatus?: string;
  sourceTimestamp?: string;
  receivedAt?: string;
};

export type PortfolioPosition = {
  positionId: string;
  portfolioId: string;
  instrument: Instrument;
  quantity: number;
  averageCost: Money;
  currentPrice: Money | null;
  importedPrice?: Money | null;
  marketValue: Money | null;
  costBasis: Money;
  unrealizedProfitLoss: Money | null;
  unrealizedProfitLossPercent: number | null;
  brokerType: string;
  sourceType: string;
  displayName: string;
  customDisplayName?: string | null;
  dataFreshness: string;
  lastUpdated: string;
  quote?: Quote | null;
};

export type Allocation = {
  country: Record<string, number>;
  currency: Record<string, number>;
  sector: Record<string, number>;
  assetType: Record<string, number>;
  broker: Record<string, number>;
};

export type PortfolioSummary = {
  portfolioId: string;
  baseCurrency: string;
  totalMarketValue: Money | null;
  totalCostBasis: Money;
  unrealizedProfitLoss: Money | null;
  unrealizedProfitLossPercent: number | null;
  cash: Money;
  positions: number;
  allocation: Allocation;
};

export type PortfolioHistoryRange = "1D" | "5D" | "1W" | "1M" | "1Y" | "2Y" | "3Y" | "4Y" | "5Y" | "MAX";

export type PortfolioHistoryPoint = {
  timestamp: string;
  marketValue: Money;
  investedCapital?: Money | null;
  investedCapitalStatus: string;
  cash: Money;
  positionsMarketValue: Money;
  unrealizedPnl: Money;
  realizedPnl?: Money | null;
  broker: string;
  source: string;
  dataFreshness: string;
};

export type PortfolioHistory = {
  portfolioId: string;
  baseCurrency: string;
  range: PortfolioHistoryRange;
  from?: string | null;
  to: string;
  investedCapitalStatus: string;
  backfillAvailable: boolean;
  points: PortfolioHistoryPoint[];
};

export type BrokerProviderInfo = {
  brokerType: string;
  displayName: string;
  connectable: boolean;
  unavailableReason?: string | null;
  status: string;
  providerStatus: string;
  code: string;
  message: string;
  dataFreshness: string;
  readOnly: boolean;
  officialProviderSetupRequired: boolean;
  consumerAuthMode: "BROKER_REDIRECT" | "PARTNER_OAUTH" | "INDIVIDUAL_API_CREDENTIALS" | "PARTNER_UNAVAILABLE" | "NONE";
  individualApiSupported: boolean;
  advancedIndividualMode: boolean;
  manualImportSupported: boolean;
  manualImportParserStatus: "SUPPORTED" | "UNCONFIGURED" | "UNAVAILABLE";
};

export type PortfolioImportPreview = {
  portfolioName: string;
  broker: string;
  sourceType: "MANUAL_CSV_IMPORT";
  rowsDetected: number;
  validHoldings: number;
  rejectedRows: number;
  columnsMapped: string[];
  issues: string[];
  updatesExistingPortfolio: boolean;
  parserSupported: boolean;
  statementAt?: string | null;
  currency: string;
  holdings: Array<{ companyName: string; symbol: string; quantity: number; averageCost: number;
    importedPrice: number; marketValue: number; unrealizedPnl: number }>;
};

export type PortfolioImportResult = {
  portfolioId: string;
  portfolioName: string;
  broker: string;
  sourceType: "MANUAL_CSV_IMPORT";
  importedAt: string;
  rowCount: number;
  acceptedCount: number;
  rejectedCount: number;
  updatedExistingPortfolio: boolean;
};

export type BrokerAuthenticationAction = {
  connectionId: string;
  provider: string;
  status: string;
  action: "NONE" | "REDIRECT_REQUIRED" | "POPUP_REQUIRED" | "CALLBACK_PENDING"
    | "CONSENT_REQUIRED" | "AUTHENTICATION_REQUIRED" | "PARTNER_AUTH_UNAVAILABLE"
    | "UNAVAILABLE" | "UNSUPPORTED";
  authenticationUrl?: string | null;
  message: string;
};

export type BrokerAuthStatus = {
  connectionId: string;
  state: string;
  authenticated: boolean;
};

export type BrokerCredentialStatus = {
  connectionId: string;
  provider: string;
  configured: boolean;
  requiredFields: string[];
  message: string;
};

export type BrokerPortfolioSync = {
  connectionId: string;
  provider?: string | null;
  status: string;
  action: string;
  message: string;
  completedAt: string;
  portfolios: PortfolioListItem[];
};

export type BrokerConnection = {
  connectionId: string;
  brokerType: string;
  displayName: string;
  status: string;
  connectedAt?: string | null;
  lastSuccessfulSyncAt?: string | null;
  lastSyncAttemptAt?: string | null;
  lastErrorCode?: string | null;
  createdAt: string;
  updatedAt: string;
  capabilities: string[];
  providerStatus: string;
  dataFreshness: string;
  readOnly: boolean;
};

export type ApiFailure = {
  message: string;
  correlationId?: string;
  status?: number;
};

export type AuthenticatedUser = {
  userId: string;
  issuer: string;
  subject: string;
  email?: string | null;
  displayName?: string | null;
  roles: string[];
};

export type LoginResponse = {
  accessToken: string;
  tokenType: "Bearer";
  expiresAt: string;
  user: AuthenticatedUser;
};
export type RegistrationResponse = { userId: string; status: "EMAIL_VERIFICATION_PENDING" };

export type ResearchProfile = {
  instrumentId: string;
  companyId: string;
  companyName: string;
  aliases: string[];
  isin?: string | null;
  ticker: string;
  exchange: string;
  mic: string;
  country: string;
  currency: string;
};

export type ResearchEvent = {
  eventId: string;
  instrumentId: string;
  companyId: string;
  eventType: string;
  eventDate?: string | null;
  detectedAt: string;
  title: string;
  summary: string;
  sourceDocumentId: string;
  sourceUrl: string;
  sourceType: string;
  sourceClassification?: string;
  reliability: string;
  sourceMode: "DEMO" | "REAL";
  confidence: number;
  impact: string;
  timeHorizon: string;
  currency?: string | null;
  monetaryValue?: number | null;
  monetaryOriginal?: string | null;
  percentageValue?: number | null;
  percentageOriginal?: string | null;
  customer?: string | null;
  counterparty?: string | null;
  location?: string | null;
  capacityValue?: number | null;
  capacityUnit?: string | null;
  status: string;
  rawEvidenceReference: string;
  publishedAt?: string | null;
  retrievedAt?: string | null;
  supportingSources?: ResearchEvidenceSource[];
  independenceKey?: string | null;
};

export type ResearchEvidenceSource = {
  publisher?: string | null;
  url: string;
  sourceType: string;
  publishedAt?: string | null;
  retrievedAt: string;
  reliability: string;
  sourceMode: "DEMO" | "REAL";
  documentId: string;
  sourceName: string;
  canonicalUrl: string;
  independent: boolean;
};

export type ResearchDocument = {
  documentId: string;
  canonicalUrl: string;
  originalUrl: string;
  title?: string | null;
  sourceType: string;
  sourceClassification?: string;
  sourceName: string;
  publisher?: string | null;
  publishedAt?: string | null;
  retrievedAt: string;
  language?: string | null;
  contentType: string;
  documentType: string;
  contentHash: string;
  instrumentId?: string | null;
  companyId?: string | null;
  country?: string | null;
  exchange?: string | null;
  status: string;
  reliabilityLevel: string;
  sourceMode: "DEMO" | "REAL";
  freshness: string;
  entityResolutionConfidence: number;
  discoveredAt?: string | null;
  discoveryProvider?: string | null;
  sourceIndependenceKey?: string | null;
  duplicateOfDocumentId?: string | null;
};

export type CatalystScore = {
  instrumentId: string;
  overallScore: number;
  buckets: Record<string, number | null>;
  categoryEvidence?: Record<
    string,
    {
      category: string;
      status: "POSITIVE_EVIDENCE" | "NEUTRAL_EVIDENCE" | "NEGATIVE_EVIDENCE" | "MIXED_EVIDENCE" | "NO_EVIDENCE";
      score?: number | null;
      eventCount: number;
      sourceCount: number;
      independentSourceCount?: number;
      hasConflict?: boolean;
      supportingEvents?: ResearchEvent[];
    }
  >;
  aggregationRule?: string;
  researchConfidence: number;
  generatedAt: string;
};

export type ResearchSummary = {
  profile: ResearchProfile;
  catalystScore: CatalystScore;
  recentEvents: ResearchEvent[];
  documents: ResearchDocument[];
  lastRefreshAt?: string | null;
  dataFreshness: string;
  demo: boolean;
  sourceMix: Record<string, number>;
};

export type ProvenancedValue = {
  value: unknown;
  unit?: string | null;
  asOfDate?: string | null;
  period?: string | null;
  sourceUrl: string;
  sourceName: string;
  sourceType?: string | null;
  publishedAt?: string | null;
  retrievedAt: string;
    confidence?: number | null;
    calculationBasis?: string | null;
};

export type QuarterlyResult = {
    period: string; resultDate?: string | null;
    documentTitle?: string | null; extractionStatus?: string | null; reportingBasis?: "CONSOLIDATED" | "STANDALONE" | null;
  revenue?: ProvenancedValue | null; revenueYoYPercent?: ProvenancedValue | null; revenueQoQPercent?: ProvenancedValue | null;
  ebitda?: ProvenancedValue | null; ebitdaMargin?: ProvenancedValue | null; ebitdaYoYPercent?: ProvenancedValue | null;
  pat?: ProvenancedValue | null; patYoYPercent?: ProvenancedValue | null; patQoQPercent?: ProvenancedValue | null;
    eps?: ProvenancedValue | null; sourceName: string; sourceUrl: string; sourceType: string;
    debtOrBorrowings?: ProvenancedValue | null; exceptionalItems?: string | null; segmentInformation?: string | null;
    managementCommentary?: string[]; yoySummary?: string | null;
    nim?: ProvenancedValue | null; roa?: ProvenancedValue | null; roe?: ProvenancedValue | null;
    grossNpa?: ProvenancedValue | null; netNpa?: ProvenancedValue | null; deposits?: ProvenancedValue | null;
    advances?: ProvenancedValue | null; capitalAdequacy?: ProvenancedValue | null; creditCost?: ProvenancedValue | null;
  publishedAt?: string | null; retrievedAt: string; confidence: number;
};

export type FinancialResultPeriod = {
  period: string;
  periodType: string;
  reportingBasis?: string | null;
  revenue?: ProvenancedValue | null;
  operatingIncome?: ProvenancedValue | null;
  ebit?: ProvenancedValue | null;
  ebitda?: ProvenancedValue | null;
  pat?: ProvenancedValue | null;
  eps?: ProvenancedValue | null;
  sourceName: string;
  sourceUrl: string;
  sourceType: string;
  publishedAt?: string | null;
  retrievedAt: string;
  confidence: number;
};

export type FinancialStatementPeriod = {
  period: string;
  periodType: string;
  reportingBasis?: string | null;
  metrics: Record<string, ProvenancedValue>;
};

export type ShareholdingChange = {
  category: string; current: ProvenancedValue; previous: ProvenancedValue;
  currentPeriod: string; previousPeriod: string; changePercentagePoints: string; sourceDate?: string | null;
};

export type ShareholdingSnapshot = {
  id: string; instrumentId: string; periodEnd: string; filingBasis?: string | null;
  sourceProvider: string; sourceType: string; sourceUrl: string; publishedAt?: string | null;
  retrievedAt: string; confidence: string | number; reliabilityLevel: string; sourceMode: string;
  values: Array<{ category: string; percentage: string; metricBasis?: string | null; rawSourceLabel?: string | null; sourceLocator?: string | null; evidenceText?: string | null }>;
};

export type ValuationAssessment = {
  state: "CHEAP" | "FAIR" | "EXPENSIVE" | "UNKNOWN"; reason: string;
  currentPe?: ProvenancedValue | null; sectorPe?: ProvenancedValue | null; peerPe?: ProvenancedValue | null;
  historicalPe?: ProvenancedValue | null; roe?: ProvenancedValue | null; roce?: ProvenancedValue | null;
};

export type StructuredMarketSnapshot = {
  resolution: { providerTicker: string; companyName: string; exchange?: string | null; currency?: string | null; quoteType?: string | null; status: string };
  status: string;
  retrievedAt: string;
  marketAsOf?: string | null;
  sourceName: string;
  sourceType: string;
  sourceUrl: string;
  facts: Record<string, ProvenancedValue>;
  news: Array<{ headline: string; publisher: string; url: string; publishedAt?: string | null; retrievedAt: string }>;
  acceptedFieldsCount: number;
  safeErrorCode?: string | null;
};

export type EtfResearchProfile = {
  instrumentId: string;
  fundId: string;
  fundName: string;
  ticker: string;
  exchange: string;
  mic: string;
  provider?: string | null;
  providerInstrumentId?: string | null;
  isin?: string | null;
  currency?: string | null;
  fundProvider?: string | null;
  underlyingIndex?: string | null;
  knownDomains: string[];
  facts: Record<string, ProvenancedValue>;
};

export type PortfolioResearchCompany = {
  instrumentId?: string | null;
  companyId?: string | null;
  companyName: string;
  ticker?: string | null;
  exchange?: string | null;
  isin?: string | null;
  provider?: string | null;
  providerInstrumentId?: string | null;
  assetType?: string | null;
  status: string;
  catalystScore?: number | null;
  confidence?: number | null;
  evidenceCoverage: Record<string, string>;
  latestEvent?: ResearchEvent | null;
  positiveEventsCount: number;
  negativeEventsCount: number;
  neutralEventsCount: number;
  documentCount: number;
  eventCount: number;
  sourceCount: number;
  lastRefresh?: string | null;
  freshness: string;
  mode: string;
  missingCategories: string[];
  etfProfile?: EtfResearchProfile | null;
  currentPrice?: string | null;
  entryZoneLow?: string | null;
  entryZoneHigh?: string | null;
  target1?: string | null;
  target2?: string | null;
  riskInvalidationLevel?: string | null;
  potentialUpsidePct?: string | null;
  potentialDownsidePct?: string | null;
  riskRewardRatio?: string | null;
  safeErrorCode?: string | null;
  safeErrorMessage?: string | null;
  latestQuarterlyResult?: QuarterlyResult | null;
  financialResultHistory: FinancialResultPeriod[];
  balanceSheetHistory: FinancialStatementPeriod[];
  cashFlowHistory: FinancialStatementPeriod[];
  quarterlyResultStatus?: string;
  structuredMarket?: StructuredMarketSnapshot | null;
  structuredProviderStatus?: string | null;
  priceFreshness?: string;
  shareholdingChanges: ShareholdingChange[];
  shareholdingSnapshots?: ShareholdingSnapshot[];
  shareholdingFreshness?: string;
  ownershipIncreases: Array<"PROMOTER" | "FII_FPI" | "DII">;
  valuation: ValuationAssessment;
  currentQuarterCatalysts: ResearchEvent[];
  durableCategoryEvidence?: CatalystScore["categoryEvidence"];
  sourceDiversity: { sourcesFound: number; domainsFound: number; officialSources: number; exchangeSources: number; companySources: number; secondarySources: number };
};

export type PortfolioResearchSummary = {
  portfolioId: string;
  generatedAt: string;
  companiesRequested: number;
  companiesResolved: number;
  companiesSucceeded: number;
  companiesDegraded: number;
  companiesFailed: number;
  documentsCreated: number;
  eventsCreated: number;
  deduplicatedCount: number;
  companies: PortfolioResearchCompany[];
  totalCompanies: number; completed: number; partial: number; failed: number; unsupported: number; inProgress: number;
};

type ApiErrorBody = {
  message?: string;
  correlationId?: string;
};

function currentAuthenticatedApiToken(): string | null {
  return typeof window === "undefined" ? null : window.localStorage.getItem("aip.accessToken");
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = currentAuthenticatedApiToken();
  const response = await fetch(`${frontendConfig.apiBaseUrl}${path}`, {
    ...init,
    headers: {
      ...(init?.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      "X-Correlation-Id": crypto.randomUUID(),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...init?.headers
    }
  });

  if (!response.ok) {
    let body: ApiErrorBody = {};
    try {
      body = (await response.json()) as ApiErrorBody;
    } catch {
      body = {};
    }

    const failure: ApiFailure = {
      message: body.message ?? "The request could not be completed.",
      correlationId: body.correlationId ?? response.headers.get("X-Correlation-Id") ?? undefined,
      status: response.status
    };
    if (response.status === 401 && typeof window !== "undefined") {
      window.localStorage.removeItem("aip.accessToken");
      window.localStorage.removeItem("aip.user");
      window.dispatchEvent(new Event("aip:unauthorized"));
    }
    throw failure;
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

export const authApi = {
  register: (payload: { email: string; password: string; firstName: string; lastName: string }) =>
    request<RegistrationResponse>("/api/v1/auth/register", { method: "POST", body: JSON.stringify(payload) }),
  login: (email: string, password: string) =>
    request<LoginResponse>("/api/v1/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }),
  verifyEmail: (token: string) => request<{ userId: string; status: "ACTIVE" }>("/api/v1/auth/verify-email", { method: "POST", body: JSON.stringify({ token }) }),
  resendVerification: (email: string) => request<{ message: string }>("/api/v1/auth/resend-verification", { method: "POST", body: JSON.stringify({ email }) }),
  requestPasswordReset: (email: string) => request<{ message: string }>("/api/v1/auth/password-reset/request", { method: "POST", body: JSON.stringify({ email }) }),
  confirmPasswordReset: (token: string, newPassword: string) => request<void>("/api/v1/auth/password-reset/confirm", { method: "POST", body: JSON.stringify({ token, newPassword }) }),
  loginDev: (userKey: "user-a" | "user-b") =>
    request<LoginResponse>("/api/v1/auth/dev/login", {
      method: "POST",
      body: JSON.stringify({ userKey })
    })
};

export const portfolioApi = {
  listPortfolios: () => request<PortfolioListItem[]>("/api/v1/portfolios"),
  getDashboard: () => request<PortfolioDashboard>("/api/v1/portfolios/dashboard"),
  ensureMarketData: (region: "INDIA") =>
    request<MarketDataEnsureStatus>(`/api/v1/research/market-data/ensure?region=${region}`, { method: "POST" }),
  getMarketUniverseSectors: (region: SectorPerformance["region"]) => {
    const params = new URLSearchParams({ region });
    return request<MarketUniverseSectors>(`/api/v1/research/market-universe/sectors?${params.toString()}`);
  },
  getSectorPerformance: (region: SectorPerformance["region"], sector: string, period: SectorPerformance["period"]) => {
    // URLSearchParams represents spaces as '+'. This remains one decoded
    // query value when the API gateway forwards the request.
    const params = new URLSearchParams({ region, sector, period, limit: "5" });
    return request<SectorPerformance>(`/api/v1/research/sector-performance?${params.toString()}`);
  },
  createPortfolio: (payload: { name: string; baseCurrency: string }) =>
    request<Portfolio>("/api/v1/portfolios", {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  getPortfolio: (portfolioId: string) => request<Portfolio>(`/api/v1/portfolios/${portfolioId}`),
  getPositions: (portfolioId: string) =>
    request<PortfolioPosition[]>(`/api/v1/portfolios/${portfolioId}/positions`),
  refreshPrices: (portfolioId: string) =>
    request<{ portfolioId: string; refreshed: number; failed: number; skipped: number; completedAt: string }>(
      `/api/v1/portfolios/${portfolioId}/prices/refresh`, { method: "POST" }
    ),
  updateHoldingDisplayName: (portfolioId: string, positionId: string, customDisplayName: string | null) =>
    request<PortfolioPosition>(`/api/v1/portfolios/${portfolioId}/positions/${positionId}/display-name`, {
      method: "PUT",
      body: JSON.stringify({ customDisplayName })
    }),
  getSummary: (portfolioId: string) =>
    request<PortfolioSummary>(`/api/v1/portfolios/${portfolioId}/summary`),
  getHistory: (portfolioId: string, range: PortfolioHistoryRange) =>
    request<PortfolioHistory>(`/api/v1/portfolios/${portfolioId}/history?range=${encodeURIComponent(range)}`),
  syncPortfolio: (portfolioId: string) =>
    request<PortfolioSummary>(`/api/v1/portfolios/${portfolioId}/sync`, { method: "POST" }),
  importBrokerConnection: (portfolioId: string, connectionId: string) =>
    request<PortfolioSummary>(`/api/v1/portfolios/${portfolioId}/broker-connections/${connectionId}/import`, { method: "POST" }),
  syncBrokerConnection: (connectionId: string) =>
    request<BrokerPortfolioSync>(`/api/v1/portfolios/broker-connections/${connectionId}/sync`, { method: "POST" }),
  previewImport: (brokerType: string, file: File, portfolioName?: string) => {
    const body = new FormData();
    body.append("file", file);
    if (portfolioName?.trim()) body.append("portfolioName", portfolioName.trim());
    return request<PortfolioImportPreview>(`/api/v1/portfolios/imports/${brokerType.toLowerCase()}/preview`, { method: "POST", body });
  },
  confirmImport: (brokerType: string, file: File, portfolioName?: string) => {
    const body = new FormData();
    body.append("file", file);
    if (portfolioName?.trim()) body.append("portfolioName", portfolioName.trim());
    return request<PortfolioImportResult>(`/api/v1/portfolios/imports/${brokerType.toLowerCase()}/confirm`, { method: "POST", body });
  }
};

export const brokerApi = {
  listBrokers: () => request<BrokerProviderInfo[]>("/api/v1/brokers"),
  listConnections: () => request<BrokerConnection[]>("/api/v1/broker-connections"),
  getConnectionStatus: (connectionId: string) =>
    request<BrokerConnection>(`/api/v1/broker-connections/${connectionId}/status`),
  getAuthStatus: (connectionId: string) =>
    request<BrokerAuthStatus>(`/api/v1/broker-connections/${connectionId}/auth-status`),
  connectMock: () => request<BrokerConnection>("/api/v1/broker-connections/mock", { method: "POST" }),
  connectBroker: (brokerType: string) =>
    request<BrokerConnection>(`/api/v1/broker-connections/${brokerType.toLowerCase()}/connect`, { method: "POST" }),
  authenticationAction: (connectionId: string) =>
    request<BrokerAuthenticationAction>(`/api/v1/broker-connections/${connectionId}/authentication-action`),
  configureCredentials: (connectionId: string, clientKey: string, clientSecret: string) =>
    request<BrokerCredentialStatus>(`/api/v1/broker-connections/${connectionId}/credentials`, {
      method: "PUT", body: JSON.stringify({ clientKey, clientSecret })
    }),
  attachIciciSession: (connectionId: string, apiSession: string) =>
    request<BrokerConnection>(`/api/v1/broker-connections/${connectionId}/icici-session`, {
      method: "POST", body: JSON.stringify({ apiSession })
    }),
  attachHdfcRequestToken: (connectionId: string, requestToken: string) =>
    request<BrokerConnection>(`/api/v1/broker-connections/${connectionId}/hdfc-request-token`, {
      method: "POST", body: JSON.stringify({ requestToken })
    }),
  syncConnection: (connectionId: string) =>
    request<BrokerConnection>(`/api/v1/broker-connections/${connectionId}/sync`, { method: "POST" }),
  connectorLogin: (connectorId: string) =>
    request<{ connectorId: string; loginUrl?: string | null; authStatus: string }>(`/api/v1/broker-connectors/${connectorId}/login`),
  disconnectConnection: (connectionId: string) =>
    request<void>(`/api/v1/broker-connections/${connectionId}`, { method: "DELETE" })
};

export const researchApi = {
  listCompanies: () => request<ResearchProfile[]>("/api/v1/research/companies"),
  getSummary: (instrumentId: string) => request<ResearchSummary>(`/api/v1/research/companies/${instrumentId}/summary`),
  getCompanyPresentation: (instrumentId: string, region: SectorPerformance["region"]) => {
    const params = new URLSearchParams({ region });
    return request<PortfolioResearchCompany>(
      `/api/v1/research/companies/${instrumentId}/presentation?${params.toString()}`
    );
  },
  getEvents: (instrumentId: string, filters?: { eventType?: string; impact?: string; reliability?: string }) => {
    const params = new URLSearchParams();
    if (filters?.eventType) params.set("eventType", filters.eventType);
    if (filters?.impact) params.set("impact", filters.impact);
    if (filters?.reliability) params.set("reliability", filters.reliability);
    const suffix = params.toString() ? `?${params}` : "";
    return request<ResearchEvent[]>(`/api/v1/research/companies/${instrumentId}/events${suffix}`);
  },
  getDocuments: (instrumentId: string) =>
    request<ResearchDocument[]>(`/api/v1/research/companies/${instrumentId}/documents`),
  getReadiness: (globalInstrumentId: string) =>
    request<ResearchReadiness>(`/api/v1/research/readiness/${globalInstrumentId}`),
  ensureReadiness: (globalInstrumentId: string, requirements?: string[]) =>
    request<ResearchReadiness>(`/api/v1/research/readiness/${globalInstrumentId}/ensure`, {
      method: "POST",
      body: JSON.stringify(requirements?.length ? { requirements } : {})
    }),
  analyze: (globalInstrumentId: string, allowPartial: boolean) =>
    request<StockRuleEngineAnalysis>(`/api/v1/research/analysis/${globalInstrumentId}`, {
      method: "POST",
      body: JSON.stringify({ allowPartial })
    }),
  listWatchlists: () => request<ResearchWatchlist[]>("/api/v1/research/watchlists"),
  ensureDefaultWatchlist: (region: SectorPerformance["region"]) =>
    request<ResearchWatchlist>("/api/v1/research/watchlists/default/ensure", {
      method: "POST", body: JSON.stringify({ region })
    }),
  addWatchlistInstrument: (
    watchlistId: string,
    value: { globalInstrumentId: string; sourcePeriod?: SectorPerformance["period"] | null; sourcePerformancePct?: number | null }
  ) => request<{ globalInstrumentId: string }>(`/api/v1/research/watchlists/${watchlistId}/instruments`, {
    method: "POST", body: JSON.stringify(value)
  }),
  removeWatchlistInstrument: (watchlistId: string, globalInstrumentId: string) =>
    request<void>(`/api/v1/research/watchlists/${watchlistId}/instruments/${globalInstrumentId}`, { method: "DELETE" }),
  getWatchlistResearch: (watchlistId: string) =>
    request<WatchlistResearchPresentation>(`/api/v1/research/watchlists/${watchlistId}/research`),
  getPortfolioSummary: (portfolioId: string) =>
    request<PortfolioResearchSummary>(`/api/v1/research/portfolios/${portfolioId}/summary`),
  searchInstruments: (region: SectorPerformance["region"], query: string, limit = 20) => {
    const params = new URLSearchParams({ region, q: query, limit: String(limit) });
    return request<ResearchInstrumentMatch[]>(
      `/api/v1/research/instruments/search?${params.toString()}`
    ).then((results) => results.map((result) => ({ ...result, region })));
  },
};
