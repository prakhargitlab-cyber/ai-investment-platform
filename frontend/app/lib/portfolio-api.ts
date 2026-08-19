import { frontendConfig } from "../config";

export type Money = {
  amount: number;
  currency: string;
};

export type Portfolio = {
  portfolioId: string;
  userId: string;
  name: string;
  baseCurrency: string;
  createdAt: string;
  updatedAt: string;
};

export type PortfolioListItem = {
  portfolioId: string;
  name: string;
  baseCurrency: string;
  totalMarketValue: Money;
  unrealizedProfitLoss: Money;
  unrealizedProfitLossPercent: number;
  positions: number;
  updatedAt: string;
};

export type Instrument = {
  instrumentId: string;
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
  currentPrice: Money;
  marketValue: Money;
  costBasis: Money;
  unrealizedProfitLoss: Money;
  unrealizedProfitLossPercent: number;
  brokerAccountId: string;
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
  totalMarketValue: Money;
  totalCostBasis: Money;
  unrealizedProfitLoss: Money;
  unrealizedProfitLossPercent: number;
  cash: Money;
  positions: number;
  allocation: Allocation;
};

export type BrokerProviderInfo = {
  brokerType: string;
  status: string;
  providerStatus: string;
  code: string;
  message: string;
  capabilities: string[];
  connectionMethod: string;
  dataFreshness: string;
  readOnly: boolean;
  officialProviderSetupRequired: boolean;
};

export type BrokerConnection = {
  connectionId: string;
  userId: string;
  brokerType: string;
  externalAccountReference?: string | null;
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
  reliability: string;
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
};

export type ResearchDocument = {
  documentId: string;
  canonicalUrl: string;
  originalUrl: string;
  title?: string | null;
  sourceType: string;
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
  entityResolutionConfidence: number;
};

export type CatalystScore = {
  instrumentId: string;
  overallScore: number;
  buckets: Record<string, number>;
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

type ApiErrorBody = {
  message?: string;
  correlationId?: string;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${frontendConfig.apiBaseUrl}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      "X-Correlation-Id": crypto.randomUUID(),
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
    throw failure;
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

export const portfolioApi = {
  listPortfolios: () => request<PortfolioListItem[]>("/api/v1/portfolios"),
  createPortfolio: (payload: { name: string; baseCurrency: string }) =>
    request<Portfolio>("/api/v1/portfolios", {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  getPortfolio: (portfolioId: string) => request<Portfolio>(`/api/v1/portfolios/${portfolioId}`),
  getPositions: (portfolioId: string) =>
    request<PortfolioPosition[]>(`/api/v1/portfolios/${portfolioId}/positions`),
  getSummary: (portfolioId: string) =>
    request<PortfolioSummary>(`/api/v1/portfolios/${portfolioId}/summary`),
  syncPortfolio: (portfolioId: string) =>
    request<PortfolioSummary>(`/api/v1/portfolios/${portfolioId}/sync`, { method: "POST" })
};

export const brokerApi = {
  listBrokers: () => request<BrokerProviderInfo[]>("/api/v1/brokers"),
  listConnections: () => request<BrokerConnection[]>("/api/v1/broker-connections"),
  connectMock: () => request<BrokerConnection>("/api/v1/broker-connections/mock", { method: "POST" }),
  syncConnection: (connectionId: string) =>
    request<BrokerConnection>(`/api/v1/broker-connections/${connectionId}/sync`, { method: "POST" }),
  disconnectConnection: (connectionId: string) =>
    request<void>(`/api/v1/broker-connections/${connectionId}`, { method: "DELETE" })
};

export const researchApi = {
  listCompanies: () => request<ResearchProfile[]>("/api/v1/research/companies"),
  getSummary: (instrumentId: string) => request<ResearchSummary>(`/api/v1/research/companies/${instrumentId}/summary`),
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
  refresh: (instrumentId: string) =>
    request<ResearchSummary>(`/api/v1/research/companies/${instrumentId}/refresh`, { method: "POST" })
};
