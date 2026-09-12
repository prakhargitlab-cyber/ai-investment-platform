"use client";

/* eslint-disable react-hooks/set-state-in-effect -- these resets delimit authenticated async request lifecycles */

import {
  Bell,
  BriefcaseBusiness,
  Building2,
  ChevronDown,
  Command,
  Gauge,
  LineChart,
  Menu,
  Moon,
  RefreshCw,
  Search,
  Settings,
  ShieldCheck,
  SlidersHorizontal,
  Sun,
  TrendingDown,
  TrendingUp,
  UploadCloud,
  CheckCircle2,
  Eye,
  EyeOff,
  WalletCards,
  X
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { frontendConfig } from "../config";
import {
  type AuthenticatedUser,
  type ApiFailure,
  type BrokerConnection,
  type BrokerProviderInfo,
  type Portfolio,
  type PortfolioHistory,
  type PortfolioHistoryRange,
  type PortfolioDashboard,
  type MarketUniverseSector,
  type SectorPerformance,
  type SectorPerformanceStock,
  type PortfolioListItem,
  type PortfolioImportPreview,
  type PortfolioPosition,
  type PortfolioResearchCompany,
  type PortfolioResearchSummary,
  type FinancialResultPeriod,
  type FinancialStatementPeriod,
  type ProvenancedValue,
  type PortfolioSummary,
  type ResearchDocument,
  type ResearchEvent,
  type ResearchSummary,
  type CatalystScore,
  type ResearchReadiness,
  type ResearchReadinessRequirement,
  type StockRuleEngineAnalysis,
  type ResearchWatchlist,
  type WatchlistResearchInstrument,
  type WatchlistResearchPresentation,
  type ResearchInstrumentMatch,
  brokerApi,
  authApi,
  portfolioApi,
  researchApi
} from "../lib/portfolio-api";
import {
  type MarketIntelligenceSelection,
  formatSignedPerformancePct,
  marketIntelligenceSelection,
  performanceDirectionLabel,
  performanceRowTone,
  regionalWatchlistName
} from "../lib/market-intelligence";
import { Badge, Button, Card, EmptyState, ErrorState, Field, MetricCard, Skeleton } from "./ui";

type View = "dashboard" | "portfolio" | "research" | "brokers" | "settings";
type Theme = "system" | "light" | "dark";
type SortKey = "company" | "ticker" | "marketValue" | "profitLoss" | "allocation";
type ResearchSectionId = "overview" | "growth" | "orders" | "capex" | "customers" | "guidance" | "news" | "sources";
type ResearchContext =
  | { kind: "PORTFOLIO"; portfolioId: string }
  | { kind: "WATCHLIST"; watchlistId: string; name: string; region: SectorPerformance["region"] }
  | { kind: "SEARCH"; region: SectorPerformance["region"]; globalInstrumentId: string }
  | { kind: "WATCHLIST_PENDING"; name: string; region: SectorPerformance["region"] };
const portfolioHistoryRanges: PortfolioHistoryRange[] = ["1D", "5D", "1W", "1M", "1Y", "2Y", "3Y", "4Y", "5Y", "MAX"];
const brokerAuthenticationPollMs = 2000;
const brokerAuthenticationTimeoutMs = 5 * 60 * 1000;

const navItems: Array<{ id: View; label: string; icon: typeof Gauge }> = [
  { id: "dashboard", label: "Dashboard", icon: Gauge },
  { id: "portfolio", label: "Portfolio", icon: BriefcaseBusiness },
  { id: "research", label: "Research", icon: Search },
  { id: "brokers", label: "Brokers", icon: WalletCards },
  { id: "settings", label: "Settings", icon: Settings }
];

function formatMoney(amount?: number, currency?: string) {
  if (amount === undefined || !currency) {
    return "--";
  }

  return new Intl.NumberFormat("en", {
    style: "currency",
    currency,
    maximumFractionDigits: 2
  }).format(amount);
}

function formatBackendMoney(money?: { amount: number; currency: string } | null) {
  return money ? formatMoney(money.amount, money.currency) : "N/A";
}

function formatPercent(value?: number | null) {
  if (value == null || !Number.isFinite(value)) {
    return "N/A";
  }

  return `${value.toFixed(2)}%`;
}

function formatChangePercent(start?: number, end?: number) {
  if (!start || end === undefined) {
    return "--";
  }
  return formatPercent(((end - start) / start) * 100);
}

function getAllocationValue(summary: PortfolioSummary | null, position: PortfolioPosition) {
  if (!summary?.totalMarketValue || !position.marketValue || summary.totalMarketValue.amount === 0) {
    return null;
  }

  return (position.marketValue.amount / summary.totalMarketValue.amount) * 100;
}

function compareNullableDescending(left?: number | null, right?: number | null) {
  if (left == null && right == null) return 0;
  if (left == null) return 1;
  if (right == null) return -1;
  return right - left;
}

function valueTone(value?: number | null): "positive" | "negative" | undefined {
  return value == null ? undefined : value >= 0 ? "positive" : "negative";
}

function getApiFailure(error: unknown): ApiFailure {
  if (typeof error === "object" && error !== null && "message" in error) {
    return error as ApiFailure;
  }

  return { message: "The portfolio API is not reachable. Confirm the gateway or portfolio service is running." };
}

function marketEnsureErrorCategory(error: unknown): string {
  if (typeof error === "object" && error !== null && "status" in error) {
    const status = (error as { status?: unknown }).status;
    if (typeof status === "number") return `HTTP_${status}`;
  }
  return error instanceof TypeError ? "NETWORK_OR_CLIENT" : "UNKNOWN";
}

function hasRealBrokerPositions(positions: PortfolioPosition[]) {
  return positions.some((position) => position.dataFreshness === "REAL_BROKER");
}

function selectedPortfolioStorageKey(userId: string) {
  return `aip.selectedPortfolioId.${userId}`;
}

function rememberSelectedPortfolioId(userId: string | undefined, portfolioId: string) {
  if (!userId || typeof window === "undefined") {
    return;
  }
  if (portfolioId) {
    window.localStorage.setItem(selectedPortfolioStorageKey(userId), portfolioId);
  } else {
    window.localStorage.removeItem(selectedPortfolioStorageKey(userId));
  }
}

function storedSelectedPortfolioId(userId: string | undefined) {
  if (!userId || typeof window === "undefined") {
    return "";
  }
  return window.localStorage.getItem(selectedPortfolioStorageKey(userId)) ?? "";
}

function portfolioSourceLabels(positions: PortfolioPosition[]) {
  if (positions.some((position) => position.sourceType === "MANUAL_CSV_IMPORT")) {
    return {
      badge: "Imported positions", syncMeta: "CSV position snapshot", totalProfitLoss: "Unrealized P/L",
      returnLabel: "Unrealized return", marketValue: "Latest market value", costBasis: "Acquisition cost",
      unrealizedProfitLoss: "Unrealized P/L", lastUpdated: "from latest accepted public quote",
      source: "CSV positions with public market data", syncButton: "Refresh prices",
      emptyMessage: "Import a broker statement to populate this view.", sortMarketValue: "Latest market value",
      sortProfitLoss: "Unrealized P/L"
    };
  }
  if (hasRealBrokerPositions(positions)) {
    return {
      badge: "Real broker data",
      syncMeta: "IBKR broker sync",
      totalProfitLoss: "Total P/L",
      returnLabel: "Return",
      marketValue: "Market value",
      costBasis: "Cost basis",
      unrealizedProfitLoss: "Unrealized P/L",
      lastUpdated: "from latest IBKR sync",
      source: "Interactive Brokers",
      syncButton: "Sync IBKR broker",
      emptyMessage: "Sync a broker account to populate this view.",
      sortMarketValue: "Market value",
      sortProfitLoss: "P/L"
    };
  }

  return {
    badge: "Demo data",
    syncMeta: "Demo broker sync",
    totalProfitLoss: "Demo total P/L",
    returnLabel: "Demo return",
    marketValue: "Demo market value",
    costBasis: "Demo cost basis",
    unrealizedProfitLoss: "Demo unrealized P/L",
    lastUpdated: "from latest mock sync",
    source: "Broker demo data",
    syncButton: "Sync mock broker",
    emptyMessage: "Sync a mock broker account to populate this view.",
    sortMarketValue: "Demo market value",
    sortProfitLoss: "Demo P/L"
  };
}

export function InvestmentWorkspace() {
  const [accessToken, setAccessToken] = useState<string | null>(() =>
    typeof window === "undefined" ? null : window.localStorage.getItem("aip.accessToken")
  );
  const [authenticatedUser, setAuthenticatedUser] = useState<AuthenticatedUser | null>(() => {
    if (typeof window === "undefined") {
      return null;
    }
    const storedUser = window.localStorage.getItem("aip.user");
    return storedUser ? (JSON.parse(storedUser) as AuthenticatedUser) : null;
  });
  const [authLoading, setAuthLoading] = useState(false);
  const marketEnsureAuthReady = authenticatedUser !== null && Boolean(accessToken);
  const [view, setView] = useState<View>("dashboard");
  const [theme, setTheme] = useState<Theme>("system");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [portfolios, setPortfolios] = useState<PortfolioListItem[]>([]);
  const [portfolioDashboard, setPortfolioDashboard] = useState<PortfolioDashboard | null>(null);
  const [sectorPerformance, setSectorPerformance] = useState<SectorPerformance | null>(null);
  const [sectorPerformanceRegion, setSectorPerformanceRegion] = useState<SectorPerformance["region"]>("EUROPE");
  const [sectorPerformanceSector, setSectorPerformanceSector] = useState("");
  const [sectorOptions, setSectorOptions] = useState<MarketUniverseSector[]>([]);
  const [sectorOptionsRegion, setSectorOptionsRegion] = useState<SectorPerformance["region"] | null>(null);
  const [sectorOptionsLoading, setSectorOptionsLoading] = useState(false);
  const [sectorPerformancePeriod, setSectorPerformancePeriod] = useState<SectorPerformance["period"]>("WEEK");
  const [portfolioScope, setPortfolioScope] = useState<"ALL" | string>("ALL");
  const [selectedPortfolio, setSelectedPortfolio] = useState<Portfolio | undefined>();
  const [selectedPortfolioId, setSelectedPortfolioId] = useState<string>("");
  const [summary, setSummary] = useState<PortfolioSummary | null>(null);
  const [positions, setPositions] = useState<PortfolioPosition[]>([]);
  const [portfolioHistory, setPortfolioHistory] = useState<PortfolioHistory | null>(null);
  const [portfolioHistoryRange, setPortfolioHistoryRange] = useState<PortfolioHistoryRange>("1M");
  const [portfolioHistoryLoading, setPortfolioHistoryLoading] = useState(false);
  const [loading, setLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<ApiFailure | null>(null);
  const [newPortfolioName, setNewPortfolioName] = useState("My Global Portfolio");
  const [newPortfolioCurrency, setNewPortfolioCurrency] = useState("EUR");
  const [searchText, setSearchText] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("marketValue");
  const [brokerProviders, setBrokerProviders] = useState<BrokerProviderInfo[]>([]);
  const [brokerConnections, setBrokerConnections] = useState<BrokerConnection[]>([]);
  const [brokerLoading, setBrokerLoading] = useState(false);
  const [brokerErrors, setBrokerErrors] = useState<Record<string, string>>({});
  const [authenticatingBroker, setAuthenticatingBroker] = useState<string | null>(null);
  const [brokerAuthenticationTimedOut, setBrokerAuthenticationTimedOut] = useState(false);
  const brokerAuthPopupRef = useRef<Window | null>(null);
  const brokerAuthPollRef = useRef<number | null>(null);
  const brokerAuthPollBusyRef = useRef(false);
  const pendingBrokerAuthRef = useRef<{ connectionId: string; provider: string } | null>(null);
  const brokerAuthStartedAtRef = useRef(0);
  const ibkrBootstrapSyncsRef = useRef(new Map<string, Promise<boolean>>());
  const [selectedResearchInstrumentId, setSelectedResearchInstrumentId] = useState("");
  const [selectedMarketIntelligenceStock, setSelectedMarketIntelligenceStock] = useState<SectorPerformanceStock | null>(null);
  const [selectedMarketIntelligenceRegion, setSelectedMarketIntelligenceRegion] = useState<SectorPerformance["region"] | null>(null);
  const [researchSummary, setResearchSummary] = useState<ResearchSummary | null>(null);
  const [researchLoading, setResearchLoading] = useState(false);
  const [portfolioResearchSummary, setPortfolioResearchSummary] = useState<PortfolioResearchSummary | null>(null);
  const [portfolioResearchLoading, setPortfolioResearchLoading] = useState(false);
  const [portfolioResearchError, setPortfolioResearchError] = useState<string | null>(null);
  const [researchReadinessDialog, setResearchReadinessDialog] = useState<{
    globalInstrumentId: string;
    companyName: string;
  } | null>(null);
  const [researchReadiness, setResearchReadiness] = useState<ResearchReadiness | null>(null);
  const [researchReadinessLoading, setResearchReadinessLoading] = useState(false);
  const [researchReadinessError, setResearchReadinessError] = useState<string | null>(null);
  const [ensuringResearchRequirements, setEnsuringResearchRequirements] = useState<string[]>([]);
  const [stockRuleEngineAnalysis, setStockRuleEngineAnalysis] = useState<StockRuleEngineAnalysis | null>(null);
  const [stockRuleEngineLoading, setStockRuleEngineLoading] = useState(false);
  const [stockRuleEngineError, setStockRuleEngineError] = useState<string | null>(null);
  const [researchEventType, setResearchEventType] = useState("");
  const [researchImpact, setResearchImpact] = useState("");
  const [watchlists, setWatchlists] = useState<ResearchWatchlist[]>([]);
  const [savedWatchlistIds, setSavedWatchlistIds] = useState<Record<string, string[]>>({});
  const [watchlistMutation, setWatchlistMutation] = useState<string | null>(null);
  const [watchlistActionError, setWatchlistActionError] = useState<string | null>(null);
  const [watchlistRevision, setWatchlistRevision] = useState(0);
  const watchlistMutationRef = useRef(false);
  const [researchContext, setResearchContext] = useState<ResearchContext>({ kind: "PORTFOLIO", portfolioId: "" });
  const [watchlistResearch, setWatchlistResearch] = useState<WatchlistResearchPresentation | null>(null);
  const [watchlistResearchLoading, setWatchlistResearchLoading] = useState(false);
  const [watchlistResearchError, setWatchlistResearchError] = useState<string | null>(null);
  const positionsRef = useRef<PortfolioPosition[]>([]);
  const researchContextRef = useRef<ResearchContext>(researchContext);

  useEffect(() => {
    positionsRef.current = positions;
  }, [positions]);

  useEffect(() => {
    researchContextRef.current = researchContext;
  }, [researchContext]);

  const clearUserScopedState = useCallback(() => {
    rememberSelectedPortfolioId(authenticatedUser?.userId, "");
    setPortfolios([]);
    setPortfolioDashboard(null);
    setPortfolioScope("ALL");
    setSelectedPortfolio(undefined);
    setSelectedPortfolioId("");
    setSummary(null);
    setPositions([]);
    setPortfolioHistory(null);
    setBrokerConnections([]);
    setBrokerProviders([]);
    setSelectedResearchInstrumentId("");
    setSelectedMarketIntelligenceStock(null);
    setSelectedMarketIntelligenceRegion(null);
    setWatchlists([]);
    setSavedWatchlistIds({});
    setWatchlistActionError(null);
    setResearchContext({ kind: "PORTFOLIO", portfolioId: "" });
    setWatchlistResearch(null);
    setWatchlistResearchError(null);
    setResearchReadinessDialog(null);
    setResearchReadiness(null);
    setResearchReadinessError(null);
    setStockRuleEngineAnalysis(null);
    setStockRuleEngineError(null);
    setResearchSummary(null);
    setPortfolioResearchSummary(null);
    setSearchText("");
  }, [authenticatedUser?.userId]);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const apiSession = params.get("API_Session") ?? params.get("api_session");
    const requestToken = params.get("request_token");
    const pending = window.localStorage.getItem("aip.pendingBrokerAuthentication");
    if ((!apiSession && !requestToken) || !pending || !accessToken) return;
    const value = JSON.parse(pending) as { connectionId: string; provider: string };
    const completion = apiSession && value.provider === "ICICI_DIRECT"
      ? brokerApi.attachIciciSession(value.connectionId, apiSession)
      : requestToken && value.provider === "HDFC_SECURITIES"
        ? brokerApi.attachHdfcRequestToken(value.connectionId, requestToken)
        : null;
    if (!completion) return;
    void completion.then(() => {
      window.localStorage.removeItem("aip.pendingBrokerAuthentication");
      if (window.opener) window.close();
      else window.history.replaceState({}, "", window.location.pathname);
    }).catch(() => setBrokerErrors({}));
  }, [accessToken]);

  useEffect(() => {
    function handleUnauthorized() {
      setAccessToken(null);
      setAuthenticatedUser(null);
      clearUserScopedState();
      setError({ message: "Your session expired. Sign in again." });
    }

    window.addEventListener("aip:unauthorized", handleUnauthorized);
    return () => window.removeEventListener("aip:unauthorized", handleUnauthorized);
  }, [clearUserScopedState]);

  useEffect(() => {
    function handleBrokerAuthentication(event: MessageEvent) {
      if (event.origin !== window.location.origin || event.data?.type !== "aip:ibkr-authenticated") return;
      const pending = pendingBrokerAuthRef.current;
      if (!pending || pending.provider !== "IBKR") return;
      void finishBrokerAuthentication(pending.connectionId);
    }
    window.addEventListener("message", handleBrokerAuthentication);
    return () => window.removeEventListener("message", handleBrokerAuthentication);
  });

  useEffect(() => () => stopBrokerAuthenticationMonitoring(false), []);

  useEffect(() => {
    let cancelled = false;

    async function loadPortfolios() {
      setLoading(true);
      setError(null);
      try {
        const dashboard = await portfolioApi.getDashboard();
        const loaded = dashboard.portfolios;
        if (cancelled) {
          return;
        }
        setPortfolios(loaded);
        setPortfolioDashboard(dashboard);
        setSelectedPortfolioId((current) => {
          const stored = storedSelectedPortfolioId(authenticatedUser?.userId);
          const next = current && loaded.some((portfolio) => portfolio.portfolioId === current)
            ? current
            : stored && loaded.some((portfolio) => portfolio.portfolioId === stored)
              ? stored
              : loaded[0]?.portfolioId || "";
          rememberSelectedPortfolioId(authenticatedUser?.userId, next);
          return next;
        });
        if (loaded.length === 0) {
          rememberSelectedPortfolioId(authenticatedUser?.userId, "");
          setSelectedPortfolio(undefined);
          setSummary(null);
          setPositions([]);
        }
      } catch (err) {
        if (!cancelled) {
          setError(getApiFailure(err));
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    if (accessToken && authenticatedUser) {
      void loadPortfolios();
    }
    return () => {
      cancelled = true;
    };
  }, [accessToken, authenticatedUser?.userId]);

  useEffect(() => {
    console.info("[AIP_MARKET_ENSURE]", { event: "EFFECT", authReady: marketEnsureAuthReady });
    if (!marketEnsureAuthReady) return;

    console.info("[AIP_MARKET_ENSURE]", { event: "DISPATCH" });
    void portfolioApi.ensureMarketData("INDIA")
      .then(() => console.info("[AIP_MARKET_ENSURE]", { event: "RESOLVED" }))
      .catch((error: unknown) => console.info(
        "[AIP_MARKET_ENSURE]",
        { event: "REJECTED", category: marketEnsureErrorCategory(error) }
      ));
  }, [marketEnsureAuthReady]);

  useEffect(() => {
    if (!marketEnsureAuthReady) return;
    let cancelled = false;
    void researchApi.listWatchlists()
      .then(async (values) => {
        const memberships = await Promise.all(values.map(async (list) => {
          const detail = await researchApi.getWatchlistResearch(list.watchlistId);
          return [list.watchlistId, detail.instruments.map((item) => item.globalInstrumentId)] as const;
        }));
        if (!cancelled) {
          setWatchlists(values);
          setSavedWatchlistIds(Object.fromEntries(memberships));
        }
      })
      .catch(() => { if (!cancelled) setWatchlistActionError("Saved watchlists could not be loaded. Please retry."); });
    return () => { cancelled = true; };
  }, [marketEnsureAuthReady]);

  useEffect(() => {
    setResearchContext((current) => current.kind === "PORTFOLIO"
      ? { kind: "PORTFOLIO", portfolioId: selectedPortfolioId }
      : current);
  }, [selectedPortfolioId]);

  useEffect(() => {
    if (!accessToken || !authenticatedUser) return;
    let cancelled = false;
    setSectorOptions([]);
    setSectorOptionsRegion(null);
    setSectorPerformanceSector("");
    setSectorPerformance(null);
    setSectorOptionsLoading(true);
    void portfolioApi.getMarketUniverseSectors(sectorPerformanceRegion)
      .then((value) => {
        if (cancelled) return;
        setSectorOptions(value.sectors);
        setSectorOptionsRegion(value.region);
        setSectorPerformanceSector(value.sectors[0]?.name ?? "");
      })
      .catch(() => {
        if (!cancelled) {
          setSectorOptions([]);
          setSectorOptionsRegion(sectorPerformanceRegion);
        }
      })
      .finally(() => { if (!cancelled) setSectorOptionsLoading(false); });
    return () => { cancelled = true; };
  }, [accessToken, authenticatedUser, sectorPerformanceRegion]);

  useEffect(() => {
    const validSector = sectorOptionsRegion === sectorPerformanceRegion
      && sectorOptions.some((option) => option.name === sectorPerformanceSector);
    if (!accessToken || !authenticatedUser || !validSector) {
      setSectorPerformance(null);
      return;
    }
    let cancelled = false;
    setSectorPerformance(null);
    void portfolioApi.getSectorPerformance(sectorPerformanceRegion, sectorPerformanceSector, sectorPerformancePeriod)
      .then((value) => { if (!cancelled) setSectorPerformance(value); })
      .catch(() => { if (!cancelled) setSectorPerformance(null); });
    return () => { cancelled = true; };
  }, [accessToken, authenticatedUser, sectorOptions, sectorOptionsRegion, sectorPerformanceRegion, sectorPerformanceSector, sectorPerformancePeriod]);

  useEffect(() => {
    let cancelled = false;

    async function loadResearchSummary() {
      if (!selectedResearchInstrumentId) {
        setResearchSummary(null);
        return;
      }
      const selectedCompany = researchContext.kind === "WATCHLIST"
        ? watchlistResearch?.instruments.find((value) => value.globalInstrumentId === selectedResearchInstrumentId)?.company
        : portfolioResearchSummary?.companies.find((company) => company.instrumentId === selectedResearchInstrumentId);
      if (selectedCompany && !canRefreshResearch(selectedCompany)) {
        setResearchSummary(null);
        setResearchLoading(false);
        return;
      }
      setResearchLoading(true);
      try {
        const loaded = await researchApi.getSummary(selectedResearchInstrumentId);
        if (!cancelled) {
          setResearchSummary(loaded);
        }
      } catch {
        if (!cancelled) {
          setResearchSummary(null);
        }
      } finally {
        if (!cancelled) {
          setResearchLoading(false);
        }
      }
    }

    void loadResearchSummary();
    return () => {
      cancelled = true;
    };
  }, [portfolioResearchSummary, researchContext, selectedResearchInstrumentId, watchlistResearch]);

  useEffect(() => {
    if (!marketEnsureAuthReady || researchContext.kind !== "WATCHLIST") {
      setWatchlistResearch(null);
      setWatchlistResearchError(null);
      return;
    }
    let cancelled = false;
    setWatchlistResearchLoading(true);
    setWatchlistResearchError(null);
    void researchApi.getWatchlistResearch(researchContext.watchlistId)
      .then((value) => {
        if (cancelled) return;
        setWatchlistResearch(value);
        setSelectedResearchInstrumentId((current) => value.instruments.some(
          (item) => item.globalInstrumentId === current
        ) ? current : value.instruments[0]?.globalInstrumentId ?? "");
      })
      .catch(() => {
        if (!cancelled) {
          setWatchlistResearch(null);
          setWatchlistResearchError("Watchlist research is temporarily unavailable.");
        }
      })
      .finally(() => { if (!cancelled) setWatchlistResearchLoading(false); });
    return () => { cancelled = true; };
  }, [marketEnsureAuthReady, researchContext, watchlistRevision]);

  useEffect(() => {
    let cancelled = false;

    async function loadPortfolioResearchSummary() {
      if (!selectedPortfolioId) {
        setPortfolioResearchSummary(null);
        setSelectedResearchInstrumentId("");
        return;
      }
      setPortfolioResearchLoading(true);
      setPortfolioResearchError(null);
      try {
        const loaded = await researchApi.getPortfolioSummary(selectedPortfolioId);
        if (cancelled) {
          return;
        }
        setPortfolioResearchSummary(loaded);
        if (researchContextRef.current.kind === "PORTFOLIO") {
          setSelectedResearchInstrumentId((current) => current
            && loaded.companies.some((company) => company.instrumentId === current)
            ? current : loaded.companies.find((company) => canRefreshResearch(company))?.instrumentId ?? "");
        }
      } catch {
        if (!cancelled) {
          setPortfolioResearchSummary(null);
          setPortfolioResearchError("Portfolio research summary unavailable. Existing company research remains available.");
          if (researchContextRef.current.kind === "PORTFOLIO") {
            setSelectedResearchInstrumentId((current) => current || positionsRef.current.find(
              (position) => position.instrument.globalInstrumentId
            )?.instrument.globalInstrumentId || "");
          }
        }
      } finally {
        if (!cancelled) {
          setPortfolioResearchLoading(false);
        }
      }
    }

    void loadPortfolioResearchSummary();
    return () => {
      cancelled = true;
    };
  }, [selectedPortfolioId]);

  useEffect(() => {
    let cancelled = false;

    async function loadPortfolioDetail() {
      setLoading(true);
      setError(null);
      try {
        const [loadedPortfolio, loadedSummary, loadedPositions] = await Promise.all([
          portfolioApi.getPortfolio(selectedPortfolioId),
          portfolioApi.getSummary(selectedPortfolioId),
          portfolioApi.getPositions(selectedPortfolioId)
        ]);
        if (!cancelled) {
          setSelectedPortfolio(loadedPortfolio);
          setSummary(loadedSummary);
          setPositions(loadedPositions);
        }
      } catch (err) {
        if (!cancelled) {
          setError(getApiFailure(err));
          setSummary(null);
          setPositions([]);
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    if (!selectedPortfolioId) {
      return;
    }

    void loadPortfolioDetail();
    return () => {
      cancelled = true;
    };
  }, [selectedPortfolioId]);

  useEffect(() => {
    let cancelled = false;

    async function loadPortfolioHistory() {
      if (!selectedPortfolioId) {
        return;
      }
      setPortfolioHistoryLoading(true);
      try {
        const loaded = await portfolioApi.getHistory(selectedPortfolioId, portfolioHistoryRange);
        if (!cancelled) {
          setPortfolioHistory(loaded);
        }
      } catch {
        if (!cancelled) {
          setPortfolioHistory(null);
        }
      } finally {
        if (!cancelled) {
          setPortfolioHistoryLoading(false);
        }
      }
    }

    if (accessToken && authenticatedUser) {
      void loadPortfolioHistory();
    }
    return () => {
      cancelled = true;
    };
  }, [accessToken, selectedPortfolioId, portfolioHistoryRange]);

  useEffect(() => {
    let cancelled = false;

    async function loadBrokers() {
      setBrokerLoading(true);
      try {
        const [providers, connections] = await Promise.all([brokerApi.listBrokers(), brokerApi.listConnections()]);
        if (!cancelled) {
          setBrokerProviders(providers);
          setBrokerConnections(connections);
        }
      } catch {
        if (!cancelled) {
          setBrokerProviders([]);
          setBrokerConnections([]);
        }
      } finally {
        if (!cancelled) {
          setBrokerLoading(false);
        }
      }
    }

    if (accessToken && authenticatedUser) {
      void loadBrokers();
    }
    return () => {
      cancelled = true;
    };
  }, [accessToken]);

  const sortedPositions = useMemo(() => {
    const normalizedSearch = searchText.trim().toLowerCase();
    return positions
      .filter((position) => {
        if (!normalizedSearch) {
          return true;
        }
        return [
          position.displayName,
          position.instrument.companyName,
          position.instrument.ticker,
          position.instrument.isin,
          position.instrument.exchange,
          position.instrument.country
        ]
          .filter(Boolean)
          .some((value) => String(value).toLowerCase().includes(normalizedSearch));
      })
      .sort((a, b) => {
        switch (sortKey) {
          case "company":
            return a.displayName.localeCompare(b.displayName);
          case "ticker":
            return a.instrument.ticker.localeCompare(b.instrument.ticker);
          case "profitLoss":
            return compareNullableDescending(a.unrealizedProfitLoss?.amount, b.unrealizedProfitLoss?.amount);
          case "allocation":
            return compareNullableDescending(getAllocationValue(summary, a), getAllocationValue(summary, b));
          case "marketValue":
          default:
            return compareNullableDescending(a.marketValue?.amount, b.marketValue?.amount);
        }
      });
  }, [positions, searchText, sortKey, summary]);

  async function updateHoldingDisplayName(position: PortfolioPosition, customDisplayName: string | null) {
    const updated = await portfolioApi.updateHoldingDisplayName(position.portfolioId, position.positionId, customDisplayName);
    setPositions((current) => current.map((item) => item.positionId === updated.positionId ? updated : item));
    setPortfolioDashboard(await portfolioApi.getDashboard());
  }

  function clearMarketIntelligenceContext() {
    setSelectedMarketIntelligenceStock(null);
    setSelectedMarketIntelligenceRegion(null);
    setWatchlistResearch(null);
    setWatchlistResearchError(null);
    setResearchContext({ kind: "PORTFOLIO", portfolioId: selectedPortfolioId });
    setSelectedResearchInstrumentId(
      portfolioResearchSummary?.companies.find((company) => canRefreshResearch(company))?.instrumentId ?? ""
    );
    setResearchSummary(null);
  }

  async function openResearchReadiness(
    globalInstrumentId: string,
    companyName: string
  ) {
    setResearchReadinessDialog({ globalInstrumentId, companyName });
    setResearchReadiness(null);
    setResearchReadinessError(null);
    setResearchReadinessLoading(true);
    try {
      setResearchReadiness(await researchApi.getReadiness(globalInstrumentId));
    } catch {
      setResearchReadinessError("Research readiness is temporarily unavailable.");
    } finally {
      setResearchReadinessLoading(false);
    }
  }

  async function findResearchData(requirements: string[]) {
    const dialog = researchReadinessDialog;
    if (!dialog || requirements.length === 0) return;
    setEnsuringResearchRequirements(requirements);
    setResearchReadinessError(null);
    try {
      setResearchReadiness(
        await researchApi.ensureReadiness(dialog.globalInstrumentId, requirements)
      );
      setStockRuleEngineAnalysis(null);
    } catch {
      setResearchReadinessError("Targeted research acquisition could not be completed.");
    } finally {
      setEnsuringResearchRequirements([]);
    }
  }

  async function runStockRuleEngineAnalysis(allowPartial: boolean) {
    const dialog = researchReadinessDialog;
    if (!dialog) return;
    setStockRuleEngineLoading(true);
    setStockRuleEngineError(null);
    try {
      setStockRuleEngineAnalysis(
        await researchApi.analyze(dialog.globalInstrumentId, allowPartial)
      );
    } catch {
      setStockRuleEngineError("Deterministic analysis could not be completed.");
    } finally {
      setStockRuleEngineLoading(false);
    }
  }

  function openMarketIntelligenceResearch(selection: MarketIntelligenceSelection) {
    const { stock, region } = selection;
    setSelectedMarketIntelligenceStock(stock);
    setSelectedMarketIntelligenceRegion(region);
    setSelectedResearchInstrumentId(stock.globalInstrumentId);
    setResearchContext({ kind: "SEARCH", region, globalInstrumentId: stock.globalInstrumentId });
    void openResearchReadiness(stock.globalInstrumentId, stock.companyName);
    void loadSearchPresentation(stock.globalInstrumentId, stock.companyName, region);
  }

  async function openRegionalWatchlist(region: SectorPerformance["region"]) {
    setWatchlistActionError(null);
    try {
      const list = await researchApi.ensureDefaultWatchlist(region);
      setWatchlists((current) => [...current.filter((item) => item.watchlistId !== list.watchlistId), list]);
      setSelectedResearchInstrumentId("");
      setResearchContext({ kind: "WATCHLIST", watchlistId: list.watchlistId, name: list.name, region: list.region });
    } catch (error) {
      setWatchlistActionError(getApiFailure(error).message);
    }
  }

  async function addRankedStockToWatchlist(selection: MarketIntelligenceSelection) {
    if (watchlistMutationRef.current || !selection.stock.globalInstrumentId?.trim()) return;
    watchlistMutationRef.current = true;
    setWatchlistMutation(selection.stock.globalInstrumentId);
    setWatchlistActionError(null);
    try {
      const list = await researchApi.ensureDefaultWatchlist(selection.region);
      await researchApi.addWatchlistInstrument(list.watchlistId, {
        globalInstrumentId: selection.stock.globalInstrumentId,
        sourcePeriod: selection.period,
        sourcePerformancePct: selection.stock.performancePct,
      });
      const detail = await researchApi.getWatchlistResearch(list.watchlistId);
      setWatchlists((current) => [...current.filter((item) => item.watchlistId !== list.watchlistId), detail.watchlist]);
      setSavedWatchlistIds((current) => ({ ...current, [list.watchlistId]: detail.instruments.map((item) => item.globalInstrumentId) }));
      setWatchlistRevision((value) => value + 1);
    } catch (error) {
      setWatchlistActionError(getApiFailure(error).message);
    } finally {
      watchlistMutationRef.current = false;
      setWatchlistMutation(null);
    }
  }

  async function removeWatchlistStock(globalInstrumentId: string) {
    if (researchContext.kind !== "WATCHLIST" || watchlistMutationRef.current) return;
    const { watchlistId } = researchContext;
    watchlistMutationRef.current = true;
    setWatchlistMutation(globalInstrumentId);
    setWatchlistActionError(null);
    try {
      await researchApi.removeWatchlistInstrument(watchlistId, globalInstrumentId);
      setSavedWatchlistIds((current) => ({ ...current, [watchlistId]: (current[watchlistId] ?? []).filter((id) => id !== globalInstrumentId) }));
      setWatchlists((current) => current.map((list) => list.watchlistId === watchlistId ? { ...list, instrumentCount: Math.max(0, list.instrumentCount - 1) } : list));
      setWatchlistRevision((value) => value + 1);
    } catch (error) {
      setWatchlistActionError(getApiFailure(error).message);
    } finally {
      watchlistMutationRef.current = false;
      setWatchlistMutation(null);
    }
  }

  function selectResearchContext(value: string) {
    setSelectedMarketIntelligenceStock(null);
    setSelectedMarketIntelligenceRegion(null);
    setResearchSummary(null);
    setSelectedResearchInstrumentId("");
    if (value.startsWith("portfolio:")) {
      const portfolioId = value.slice("portfolio:".length);
      setResearchContext({ kind: "PORTFOLIO", portfolioId });
      rememberSelectedPortfolioId(authenticatedUser?.userId, portfolioId);
      setSelectedPortfolioId(portfolioId);
      return;
    }
    const watchlistId = value.slice("watchlist:".length);
    const watchlist = watchlists.find((candidate) => candidate.watchlistId === watchlistId);
    if (watchlist) {
      setResearchContext({
        kind: "WATCHLIST", watchlistId: watchlist.watchlistId,
        name: watchlist.name, region: watchlist.region,
      });
    }
  }

  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<ResearchInstrumentMatch[]>([]);
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [searchPresentation, setSearchPresentation] = useState<PortfolioResearchCompany | null>(null);
  const [searchSelectedMatch, setSearchSelectedMatch] = useState<ResearchInstrumentMatch | null>(null);
  const searchWatchlistSaved = Boolean(searchSelectedMatch && watchlists.some((list) =>
    list.systemDefault && list.region === searchSelectedMatch.region
    && (savedWatchlistIds[list.watchlistId] ?? []).includes(searchSelectedMatch.globalInstrumentId)));
  const searchPresentationRequest = useRef(0);
  const [searchWatchlistBusy, setSearchWatchlistBusy] = useState(false);
  const [searchWatchlistError, setSearchWatchlistError] = useState<string | null>(null);

  async function loadSearchPresentation(
    globalInstrumentId: string, _companyName: string, region: SectorPerformance["region"]
  ) {
    const requestId = ++searchPresentationRequest.current;
    setSearchPresentation(null);
    setResearchLoading(true);
    try {
      const presentation = await researchApi.getCompanyPresentation(globalInstrumentId, region);
      if (requestId === searchPresentationRequest.current) setSearchPresentation(presentation);
    } catch {
      if (requestId === searchPresentationRequest.current) setSearchPresentation(null);
    } finally {
      if (requestId === searchPresentationRequest.current) setResearchLoading(false);
    }
  }

  function handleSearchSelect(match: ResearchInstrumentMatch) {
    setSelectedMarketIntelligenceStock(null);
    setSelectedMarketIntelligenceRegion(null);
    setSearchQuery("");
    setSearchResults([]);
    setSearchSelectedMatch(match);
    setSelectedResearchInstrumentId(match.globalInstrumentId);
    setResearchContext({ kind: "SEARCH", region: match.region, globalInstrumentId: match.globalInstrumentId });
    void openResearchReadiness(match.globalInstrumentId, match.companyName);
    void loadSearchPresentation(match.globalInstrumentId, match.companyName, match.region);
  }

  async function toggleSearchWatchlist() {
    const match = searchSelectedMatch;
    if (searchWatchlistBusy || !match?.globalInstrumentId?.trim()) return;
    setSearchWatchlistBusy(true);
    setSearchWatchlistError(null);
    try {
      const list = await researchApi.ensureDefaultWatchlist(match.region);
      const already = (savedWatchlistIds[list.watchlistId] ?? []).includes(match.globalInstrumentId);
      if (already) {
        await researchApi.removeWatchlistInstrument(list.watchlistId, match.globalInstrumentId);
        setSavedWatchlistIds((current) => ({
          ...current,
          [list.watchlistId]: (current[list.watchlistId] ?? []).filter((id) => id !== match.globalInstrumentId),
        }));
      } else {
        await researchApi.addWatchlistInstrument(list.watchlistId, {
          globalInstrumentId: match.globalInstrumentId,
          sourcePeriod: "DAY",
          sourcePerformancePct: null,
        });
        setSavedWatchlistIds((current) => ({
          ...current,
          [list.watchlistId]: [...(current[list.watchlistId] ?? []), match.globalInstrumentId],
        }));
      }
      setWatchlists((current) => current.some((item) => item.watchlistId === list.watchlistId) ? current : [...current, list]);
    } catch (error) {
      setSearchWatchlistError(getApiFailure(error).message);
    } finally {
      setSearchWatchlistBusy(false);
    }
  }

  async function createPortfolio() {
    setCreating(true);
    setError(null);
    try {
      const created = await portfolioApi.createPortfolio({
        name: newPortfolioName.trim(),
        baseCurrency: newPortfolioCurrency.trim().toUpperCase()
      });
      const loaded = await portfolioApi.listPortfolios();
      setPortfolios(loaded);
      rememberSelectedPortfolioId(authenticatedUser?.userId, created.portfolioId);
      setSelectedPortfolioId(created.portfolioId);
      clearMarketIntelligenceContext();
      setView("portfolio");
    } catch (err) {
      setError(getApiFailure(err));
    } finally {
      setCreating(false);
    }
  }

  async function syncPortfolio() {
    if (!selectedPortfolioId) {
      return;
    }

    setSyncing(true);
    setError(null);
    try {
      const syncedSummary = await portfolioApi.syncPortfolio(selectedPortfolioId);
      const loadedPositions = await portfolioApi.getPositions(selectedPortfolioId);
      const loaded = await portfolioApi.listPortfolios();
      setPortfolios(loaded);
      setSummary(syncedSummary);
      setPositions(loadedPositions);
    } catch (err) {
      setError(getApiFailure(err));
    } finally {
      setSyncing(false);
    }
  }

  async function syncSelectedPortfolio() {
    const brokerPortfolio = portfolios.find((portfolio) => portfolio.portfolioId === selectedPortfolioId);
    if (!brokerPortfolio?.brokerConnectionId) {
      return syncPortfolio();
    }
    const connectionId = brokerPortfolio.brokerConnectionId;
    const brokerConnection = brokerConnections.find((connection) => connection.connectionId === connectionId);
    const brokerType = brokerConnection?.brokerType ?? brokerPortfolio.provider ?? "UNKNOWN";
    const provider = brokerProviders.find((candidate) => candidate.brokerType === brokerType);
    setSyncing(true);
    setError(null);
    const authWindow = provider?.consumerAuthMode === "BROKER_REDIRECT"
      ? openBrokerAuthenticationWindow(brokerType)
      : null;
    try {
      const result = await portfolioApi.syncBrokerConnection(connectionId);
      if (result.status === "AUTHENTICATION_REQUIRED") {
        if (provider?.consumerAuthMode === "PARTNER_UNAVAILABLE") {
          setBrokerCardError(brokerType, "Direct customer account connection is not available yet.");
          return;
        }
        await completeBrokerAuthentication(
          authWindow,
          brokerType,
          () => brokerApi.authenticationAction(connectionId)
        );
        return;
      }
      authWindow?.close();
      const dashboard = await portfolioApi.getDashboard();
      const [loadedSummary, loadedPositions, loadedHistory] = await Promise.all([
        portfolioApi.getSummary(selectedPortfolioId), portfolioApi.getPositions(selectedPortfolioId),
        portfolioApi.getHistory(selectedPortfolioId, portfolioHistoryRange)
      ]);
      setPortfolioDashboard(dashboard);
      setPortfolios(dashboard.portfolios);
      setSummary(loadedSummary);
      setPositions(loadedPositions);
      setPortfolioHistory(loadedHistory);
    } catch (err) {
      authWindow?.close();
      setError(getApiFailure(err));
    } finally {
      setSyncing(false);
    }
  }

  async function refreshSelectedPrices() {
    if (!selectedPortfolioId) return;
    setSyncing(true);
    setError(null);
    try {
      await portfolioApi.refreshPrices(selectedPortfolioId);
      setPositions(await portfolioApi.getPositions(selectedPortfolioId));
    } catch (err) {
      setError(getApiFailure(err));
    } finally {
      setSyncing(false);
    }
  }

  function setBrokerCardError(brokerType: string, message: string | null) {
    setBrokerErrors((current) => {
      const next = { ...current };
      if (message) next[brokerType] = message;
      else delete next[brokerType];
      return next;
    });
  }

  function openBrokerAuthenticationWindow(brokerType: string): Window | null {
    if (brokerAuthPopupRef.current && !brokerAuthPopupRef.current.closed) {
      brokerAuthPopupRef.current.focus();
      return null;
    }
    const width = 620;
    const height = 760;
    const left = Math.max(0, Math.round(window.screenX + (window.outerWidth - width) / 2));
    const top = Math.max(0, Math.round(window.screenY + (window.outerHeight - height) / 2));
    const features = `popup=yes,width=${width},height=${height},left=${left},top=${top},resizable=yes,scrollbars=yes`;
    const authWindow = window.open("about:blank", "aip-ibkr-auth", features);
    if (!authWindow) {
      setBrokerCardError(brokerType, "Your browser blocked the IBKR sign-in window. Allow pop-ups for this site and try again.");
      return null;
    }
    brokerAuthPopupRef.current = authWindow;
    return authWindow;
  }

  function stopBrokerAuthenticationMonitoring(closePopup: boolean) {
    if (brokerAuthPollRef.current !== null) {
      window.clearInterval(brokerAuthPollRef.current);
      brokerAuthPollRef.current = null;
    }
    if (closePopup && brokerAuthPopupRef.current && !brokerAuthPopupRef.current.closed) {
      brokerAuthPopupRef.current.close();
    }
    brokerAuthPopupRef.current = null;
    brokerAuthPollBusyRef.current = false;
    pendingBrokerAuthRef.current = null;
    brokerAuthStartedAtRef.current = 0;
    window.localStorage.removeItem("aip.pendingBrokerAuthentication");
    setAuthenticatingBroker(null);
    setBrokerAuthenticationTimedOut(false);
  }

  async function refreshAuthenticatedBroker(connectionId: string): Promise<boolean> {
    const authStatus = await brokerApi.getAuthStatus(connectionId);
    if (!authStatus.authenticated || authStatus.state !== "CONNECTED") return false;
    if (pendingBrokerAuthRef.current?.provider === "IBKR") {
      await bootstrapIbkrPortfolio(connectionId);
    }
    const [connections, dashboard] = await Promise.all([
      brokerApi.listConnections(),
      portfolioApi.getDashboard()
    ]);
    setBrokerConnections(connections);
    setPortfolioDashboard(dashboard);
    setPortfolios(dashboard.portfolios);
    const linkedPortfolio = dashboard.portfolios.find((portfolio) =>
      portfolio.brokerConnectionId === connectionId && portfolio.portfolioId === selectedPortfolioId
    );
    if (linkedPortfolio) {
      const [loadedPortfolio, loadedSummary, loadedPositions, loadedHistory] = await Promise.all([
        portfolioApi.getPortfolio(linkedPortfolio.portfolioId),
        portfolioApi.getSummary(linkedPortfolio.portfolioId),
        portfolioApi.getPositions(linkedPortfolio.portfolioId),
        portfolioApi.getHistory(linkedPortfolio.portfolioId, portfolioHistoryRange)
      ]);
      setSelectedPortfolio(loadedPortfolio);
      setSummary(loadedSummary);
      setPositions(loadedPositions);
      setPortfolioHistory(loadedHistory);
    }
    return true;
  }

  async function bootstrapIbkrPortfolio(connectionId: string): Promise<boolean> {
    const existing = ibkrBootstrapSyncsRef.current.get(connectionId);
    if (existing) return existing;
    const sync = (async () => {
      try {
        const result = await portfolioApi.syncBrokerConnection(connectionId);
        if (result.status !== "CONNECTED") {
          throw new Error("Broker portfolio synchronization did not complete.");
        }
        const dashboard = await portfolioApi.getDashboard();
        setPortfolioDashboard(dashboard);
        setPortfolios(dashboard.portfolios);
        const returnedPortfolioId = result.portfolios.find((portfolio) =>
          portfolio.brokerConnectionId === connectionId
        )?.portfolioId;
        const portfolioId = returnedPortfolioId ?? dashboard.portfolios.find((portfolio) =>
          portfolio.brokerConnectionId === connectionId
        )?.portfolioId;
        if (portfolioId) {
          setSelectedPortfolioId((current) => {
            if (current) return current;
            rememberSelectedPortfolioId(authenticatedUser?.userId, portfolioId);
            return portfolioId;
          });
        }
        setBrokerCardError("IBKR", null);
        return true;
      } catch {
        setBrokerCardError("IBKR", "Interactive Brokers connected, but portfolio sync failed. Try syncing again.");
        return false;
      }
    })();
    ibkrBootstrapSyncsRef.current.set(connectionId, sync);
    return sync;
  }

  async function finishBrokerAuthentication(connectionId: string) {
    if (brokerAuthPollBusyRef.current) return;
    brokerAuthPollBusyRef.current = true;
    try {
      if (await refreshAuthenticatedBroker(connectionId)) {
        stopBrokerAuthenticationMonitoring(true);
      }
    } catch {
      // Authentication may still be propagating from IBKR; the bounded parent poll remains authoritative.
    } finally {
      brokerAuthPollBusyRef.current = false;
    }
  }

  function startBrokerAuthenticationMonitoring(authWindow: Window, connectionId: string, provider: string) {
    if (provider === "IBKR") {
      ibkrBootstrapSyncsRef.current.delete(connectionId);
    }
    pendingBrokerAuthRef.current = { connectionId, provider };
    setAuthenticatingBroker(provider);
    setBrokerAuthenticationTimedOut(false);
    brokerAuthStartedAtRef.current = Date.now();
    startBrokerAuthenticationPolling(authWindow, connectionId);
  }

  function startBrokerAuthenticationPolling(authWindow: Window, connectionId: string) {
    if (brokerAuthPollRef.current !== null) window.clearInterval(brokerAuthPollRef.current);
    const poll = async () => {
      if (brokerAuthPollBusyRef.current) return;
      if (Date.now() - brokerAuthStartedAtRef.current >= brokerAuthenticationTimeoutMs) {
        if (brokerAuthPollRef.current !== null) window.clearInterval(brokerAuthPollRef.current);
        brokerAuthPollRef.current = null;
        setBrokerAuthenticationTimedOut(true);
        return;
      }
      if (authWindow.closed) {
        brokerAuthPollBusyRef.current = true;
        try {
          await refreshAuthenticatedBroker(connectionId);
        } catch {
          // A manually closed pending login remains unauthenticated.
        } finally {
          brokerAuthPollBusyRef.current = false;
          stopBrokerAuthenticationMonitoring(false);
        }
        return;
      }
      await finishBrokerAuthentication(connectionId);
    };
    brokerAuthPollRef.current = window.setInterval(() => void poll(), brokerAuthenticationPollMs);
    void poll();
  }

  function continueBrokerAuthenticationChecking() {
    const pending = pendingBrokerAuthRef.current;
    const authWindow = brokerAuthPopupRef.current;
    if (!pending || !authWindow || authWindow.closed) return;
    setBrokerAuthenticationTimedOut(false);
    brokerAuthStartedAtRef.current = Date.now();
    startBrokerAuthenticationPolling(authWindow, pending.connectionId);
  }

  function cancelBrokerAuthentication() {
    stopBrokerAuthenticationMonitoring(true);
  }

  async function completeBrokerAuthentication(
    authWindow: Window | null,
    brokerType: string,
    requestAction: () => ReturnType<typeof brokerApi.authenticationAction>
  ): Promise<boolean> {
    if (!authWindow) return false;
    try {
      const action = await requestAction();
      if (action.action === "REDIRECT_REQUIRED" || action.action === "POPUP_REQUIRED") {
        if (!action.authenticationUrl) {
          stopBrokerAuthenticationMonitoring(true);
          setBrokerCardError(brokerType, "Broker authentication is temporarily unavailable. Your saved portfolio remains available.");
          return false;
        }
        window.localStorage.setItem("aip.pendingBrokerAuthentication", JSON.stringify({
          connectionId: action.connectionId, provider: action.provider
        }));
        authWindow.location.assign(action.authenticationUrl);
        setBrokerCardError(brokerType, null);
        startBrokerAuthenticationMonitoring(authWindow, action.connectionId, action.provider);
        return true;
      }
      authWindow.close();
      if (action.action === "NONE") {
        if (brokerType === "IBKR" && action.status === "CONNECTED") {
          ibkrBootstrapSyncsRef.current.delete(action.connectionId);
          await bootstrapIbkrPortfolio(action.connectionId);
        }
        setBrokerConnections(await brokerApi.listConnections());
      } else {
        setBrokerCardError(brokerType, action.message);
      }
      stopBrokerAuthenticationMonitoring(false);
      return false;
    } catch (err) {
      stopBrokerAuthenticationMonitoring(true);
      setBrokerCardError(brokerType, getApiFailure(err).message);
      return false;
    }
  }

  async function login(userKey: "user-a" | "user-b") {
    setAuthLoading(true);
    setError(null);
    try {
      const session = await authApi.loginDev(userKey);
      window.localStorage.setItem("aip.accessToken", session.accessToken);
      window.localStorage.setItem("aip.user", JSON.stringify(session.user));
      // Authentication can complete in a tab that was opened before a frontend rollout.
      // Reload the root document so Dashboard effects always mount from the active build.
      window.location.replace("/");
    } catch (err) {
      setError(getApiFailure(err));
    } finally {
      setAuthLoading(false);
    }
  }

  async function loginEmail(email: string, password: string) {
    setAuthLoading(true); setError(null);
    try {
      const session = await authApi.login(email, password);
      window.localStorage.setItem("aip.accessToken", session.accessToken);
      window.localStorage.setItem("aip.user", JSON.stringify(session.user));
      window.location.replace("/");
    } catch (err) { setError(getApiFailure(err)); } finally { setAuthLoading(false); }
  }

  function logout() {
    window.localStorage.removeItem("aip.accessToken");
    window.localStorage.removeItem("aip.user");
    window.history.replaceState({}, "", "/");
    setError(null);
    setAccessToken(null);
    setAuthenticatedUser(null);
    clearUserScopedState();
  }

  if (authLoading) {
    return <LoadingView />;
  }

  if (!accessToken || !authenticatedUser) {
    return <SignInView error={error} onLogin={login} onEmailLogin={loginEmail} />;
  }

  return (
    <main className="app-shell">
      <aside className={`sidebar ${sidebarOpen ? "sidebar-open" : ""}`}>
        <div className="brand-lockup">
          <div className="brand-mark">AI</div>
          <div>
            <strong>AI Investment</strong>
            <span>Intelligence</span>
          </div>
        </div>

        <nav aria-label="Primary">
          {navItems.map((item) => {
            const Icon = item.icon;
            return (
              <button
                className={`nav-item ${view === item.id ? "nav-item-active" : ""}`}
                key={item.id}
                onClick={() => {
                  clearMarketIntelligenceContext();
                  setView(item.id);
                  setSidebarOpen(false);
                }}
                type="button"
              >
                <Icon size={18} aria-hidden="true" />
                {item.label}
              </button>
            );
          })}
        </nav>

        <div className="sidebar-panel">
          <Badge tone={hasRealBrokerPositions(positions) ? "positive" : "info"}>
            {portfolioSourceLabels(positions).badge}
          </Badge>
          <p>{hasRealBrokerPositions(positions) ? "Read-only broker data is sourced from Interactive Brokers." : "Demo portfolios use generated broker data."}</p>
        </div>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <Button className="mobile-menu" variant="ghost" onClick={() => setSidebarOpen(true)} aria-label="Open navigation">
            <Menu size={20} />
          </Button>
          <div className="command-bar">
            <Command size={17} aria-hidden="true" />
            <input
              aria-label="Global security search"
              placeholder="Search ticker, company, or ISIN"
              type="search"
            />
            <kbd>/</kbd>
          </div>
          <div className="topbar-actions">
            <Badge tone={hasRealBrokerPositions(positions) ? "positive" : "warning"}>
              {portfolioSourceLabels(positions).badge}
            </Badge>
            <button className="icon-button" type="button" aria-label="Notifications">
              <Bell size={18} />
            </button>
            <label className="theme-switch">
              <span className="sr-only">Theme</span>
              <Sun size={16} aria-hidden="true" />
              <select value={theme} onChange={(event) => setTheme(event.target.value as Theme)}>
                <option value="system">System</option>
                <option value="light">Light</option>
                <option value="dark">Dark</option>
              </select>
              <Moon size={16} aria-hidden="true" />
            </label>
            <button className="account-button" type="button">
              <span>{authenticatedUser.displayName ?? authenticatedUser.email ?? "Account"}</span>
              <ChevronDown size={16} aria-hidden="true" />
            </button>
            <Button variant="secondary" onClick={logout}>
              Logout
            </Button>
          </div>
        </header>

        {sidebarOpen ? (
          <button className="sidebar-scrim" onClick={() => setSidebarOpen(false)} aria-label="Close navigation" type="button">
            <X size={20} />
          </button>
        ) : null}

        <div className="content">
          <section className="page-header">
            <div>
              <p className="eyebrow">{view === "research" ? "Research intelligence" : "Brokers & Portfolios"}</p>
              <h1>{view === "dashboard" ? "Portfolio command center" : navItems.find((item) => item.id === view)?.label}</h1>
              <p>
                Backend: <code>{frontendConfig.apiBaseUrl || "same-origin gateway"}</code>
              </p>
            </div>
            {view === "research" ? (
              null
            ) : (
              <PortfolioSelector
                portfolios={portfolios}
                selectedPortfolioId={selectedPortfolioId}
                onChange={(portfolioId) => {
                  setPortfolioResearchSummary(null);
                  setSelectedResearchInstrumentId("");
                  clearMarketIntelligenceContext();
                  setResearchSummary(null);
                  setPortfolioHistory(null);
                  rememberSelectedPortfolioId(authenticatedUser?.userId, portfolioId);
                  setSelectedPortfolioId(portfolioId);
                }}
              />
            )}
          </section>

          {error ? (
            <ErrorState
              message={error.message}
              correlationId={error.correlationId}
              action={
                <Button variant="secondary" onClick={() => window.location.reload()}>
                  Retry
                </Button>
              }
            />
          ) : null}

          {view !== "research" && watchlistActionError ? <p role="alert">{watchlistActionError}</p> : null}
          {loading ? <LoadingView /> : null}

          {!loading && !error ? (
            <>
              {view === "dashboard" ? (
                <>
                  <PortfolioTabs
                    portfolios={portfolios}
                    selected={portfolioScope}
                    onSelect={(value) => {
                      setPortfolioScope(value);
                      if (value !== "ALL") setSelectedPortfolioId(value);
                    }}
                  />
                  {portfolioScope === "ALL" ? (
                    <MultiPortfolioDashboard
                      dashboard={portfolioDashboard}
                      onAddWatchlist={(selection) => { void addRankedStockToWatchlist(selection); }}
                      savedInstrumentIds={watchlists.filter((list) => list.region === sectorPerformanceRegion && list.systemDefault).flatMap((list) => savedWatchlistIds[list.watchlistId] ?? [])}
                      watchlistBusy={watchlistMutation !== null}
                      performance={sectorPerformance}
                      performanceRegion={sectorPerformanceRegion}
                      performanceSector={sectorPerformanceSector}
                      performancePeriod={sectorPerformancePeriod}
                      sectorOptions={sectorOptions}
                      sectorOptionsLoading={sectorOptionsLoading}
                      onPerformanceRegion={(region) => {
                        if (selectedMarketIntelligenceRegion && selectedMarketIntelligenceRegion !== region) {
                          clearMarketIntelligenceContext();
                        }
                        setSectorPerformanceRegion(region);
                      }}
                      onPerformanceSector={setSectorPerformanceSector}
                      onPerformancePeriod={setSectorPerformancePeriod}
                      onCreate={createPortfolio}
                      creating={creating}
                      selectedResearchInstrumentId={selectedResearchInstrumentId}
                      onOpenResearch={(selection) => { void openMarketIntelligenceResearch(selection); }}
                    />
                  ) : (
                    <DashboardView
                      portfolio={selectedPortfolio}
                      summary={summary}
                      positions={positions}
                      onCreate={createPortfolio}
                      onSync={selectedPortfolio?.brokerConnectionId ? syncSelectedPortfolio : undefined}
                      onRefreshPrices={selectedPortfolio?.acquisitionSource === "MANUAL_CSV_IMPORT" ? refreshSelectedPrices : undefined}
                      creating={creating}
                      syncing={syncing}
                      newPortfolioName={newPortfolioName}
                      newPortfolioCurrency={newPortfolioCurrency}
                      setNewPortfolioName={setNewPortfolioName}
                      setNewPortfolioCurrency={setNewPortfolioCurrency}
                    />
                  )}
                </>
              ) : null}
              {view === "portfolio" ? (
                <PortfolioView
                  portfolio={selectedPortfolio}
                  summary={summary}
                  positions={sortedPositions}
                  rawPositions={positions}
                  history={portfolioHistory}
                  historyRange={portfolioHistoryRange}
                  historyLoading={portfolioHistoryLoading}
                  onHistoryRange={setPortfolioHistoryRange}
                  searchText={searchText}
                  sortKey={sortKey}
                  onSearch={setSearchText}
                  onSort={setSortKey}
                  onCreate={createPortfolio}
                  onSync={selectedPortfolio?.brokerConnectionId ? syncSelectedPortfolio : undefined}
                  onRefreshPrices={selectedPortfolio?.acquisitionSource === "MANUAL_CSV_IMPORT" ? refreshSelectedPrices : undefined}
                  creating={creating}
                  syncing={syncing}
                  newPortfolioName={newPortfolioName}
                  newPortfolioCurrency={newPortfolioCurrency}
                  setNewPortfolioName={setNewPortfolioName}
                  setNewPortfolioCurrency={setNewPortfolioCurrency}
                  onUpdateDisplayName={updateHoldingDisplayName}
                  portfolioResearch={portfolioResearchSummary}
                />
              ) : null}
              {view === "brokers" ? (
                <BrokerView
                  providers={brokerProviders}
                  connections={brokerConnections}
                  portfolios={portfolios}
                  errors={brokerErrors}
                  loading={brokerLoading}
                  authenticatingBroker={authenticatingBroker}
                  authenticationTimedOut={brokerAuthenticationTimedOut}
                  onContinueAuthentication={continueBrokerAuthenticationChecking}
                  onCancelAuthentication={cancelBrokerAuthentication}
                  onConnectBroker={async (provider) => {
                    if (provider.consumerAuthMode === "PARTNER_UNAVAILABLE") {
                      setBrokerCardError(provider.brokerType, "Direct customer account connection is not available yet.");
                      return;
                    }
                    const authWindow = openBrokerAuthenticationWindow(provider.brokerType);
                    if (!authWindow) return;
                    setBrokerLoading(true);
                    try {
                      const connection = await brokerApi.connectBroker(provider.brokerType);
                      await completeBrokerAuthentication(
                        authWindow,
                        provider.brokerType,
                        () => brokerApi.authenticationAction(connection.connectionId)
                      );
                      setBrokerConnections(await brokerApi.listConnections());
                      setBrokerProviders(await brokerApi.listBrokers());
                    } catch (err) {
                      stopBrokerAuthenticationMonitoring(true);
                      setBrokerCardError(provider.brokerType, getApiFailure(err).message);
                    } finally {
                      setBrokerLoading(false);
                    }
                  }}
                  onAuthenticate={async (connectionId, provider) => {
                    if (provider.consumerAuthMode === "PARTNER_UNAVAILABLE") {
                      setBrokerCardError(provider.brokerType, "Direct customer account connection is not available yet.");
                      return;
                    }
                    const authWindow = openBrokerAuthenticationWindow(provider.brokerType);
                    if (!authWindow) return;
                    setBrokerLoading(true);
                    try {
                      await completeBrokerAuthentication(
                        authWindow,
                        provider.brokerType,
                        () => brokerApi.authenticationAction(connectionId)
                      );
                      setBrokerConnections(await brokerApi.listConnections());
                    } catch (err) {
                      stopBrokerAuthenticationMonitoring(true);
                      setBrokerCardError(provider.brokerType, getApiFailure(err).message);
                    } finally {
                      setBrokerLoading(false);
                    }
                  }}
                  onConfigureIndividual={async (provider, clientKey, clientSecret) => {
                    setBrokerLoading(true);
                    try {
                      const existing = brokerConnections.find((connection) => connection.brokerType === provider.brokerType);
                      const connection = existing ?? await brokerApi.connectBroker(provider.brokerType);
                      await brokerApi.configureCredentials(connection.connectionId, clientKey, clientSecret);
                      setBrokerCardError(provider.brokerType, null);
                      setBrokerConnections(await brokerApi.listConnections());
                    } catch (err) {
                      setBrokerCardError(provider.brokerType, getApiFailure(err).message);
                      throw err;
                    } finally {
                      setBrokerLoading(false);
                    }
                  }}
                  onImportComplete={async (portfolioId) => {
                    const dashboard = await portfolioApi.getDashboard();
                    setPortfolioDashboard(dashboard);
                    setPortfolios(dashboard.portfolios);
                    setSelectedPortfolioId(portfolioId);
                    setPortfolioScope(portfolioId);
                    rememberSelectedPortfolioId(authenticatedUser?.userId, portfolioId);
                    clearMarketIntelligenceContext();
                    setView("portfolio");
                  }}
                  onDisconnect={async (connectionId) => {
                    setBrokerLoading(true);
                    try {
                      await brokerApi.disconnectConnection(connectionId);
                      setBrokerConnections(await brokerApi.listConnections());
                      setBrokerProviders(await brokerApi.listBrokers());
                    } finally {
                      setBrokerLoading(false);
                    }
                  }}
                />
              ) : null}
              {view === "research" ? (
                <>
                <Card className="wide-panel research-discovery">
                  <h2>Research intelligence</h2>
                  <StockSearchField selectedGlobalInstrumentId={selectedResearchInstrumentId} onSelect={handleSearchSelect} />
                </Card>
                <Card className="wide-panel research-saved-lists">
                  <h3>Saved lists</h3>
                  <div className="button-row" role="group" aria-label="Regional watchlists">
                    {(["INDIA", "USA", "EUROPE"] as const).map((region) => <Button key={region} variant="secondary" onClick={() => { void openRegionalWatchlist(region); }}>{regionalWatchlistName(region)}</Button>)}
                  </div>
<ResearchContextSelector
                portfolios={portfolios}
                watchlists={watchlists}
                context={researchContext}
                onChange={selectResearchContext}
              />
                  {watchlistActionError ? <p role="alert">{watchlistActionError}</p> : null}
                  {researchContext.kind === "WATCHLIST" && selectedResearchInstrumentId ? <Button variant="secondary" disabled={watchlistMutation !== null} onClick={() => { void removeWatchlistStock(selectedResearchInstrumentId); }}>Remove selected company from {researchContext.name}</Button> : null}
                </Card>
                <ResearchView
                  positions={positions}
                  portfolioResearchSummary={portfolioResearchSummary}
                  portfolioResearchLoading={portfolioResearchLoading}
                  portfolioResearchError={portfolioResearchError}
                  researchContext={researchContext}
                  watchlistResearch={watchlistResearch}
                  watchlistResearchLoading={watchlistResearchLoading}
                  watchlistResearchError={watchlistResearchError}
                  selectedResearchInstrumentId={selectedResearchInstrumentId}
                  onSelectInstrument={setSelectedResearchInstrumentId}
                  searchPresentation={searchPresentation}
                  onSearchSelect={handleSearchSelect}
                  searchSelectedMatch={searchSelectedMatch}
                  searchWatchlistSaved={searchWatchlistSaved}
                  searchWatchlistBusy={searchWatchlistBusy}
                  searchWatchlistError={searchWatchlistError}
                  onToggleSearchWatchlist={toggleSearchWatchlist}
                  summary={researchSummary}
                  loading={researchLoading}
                  eventType={researchEventType}
                  impact={researchImpact}
                  onEventType={setResearchEventType}
                  onImpact={setResearchImpact}
                  onRefresh={async () => {
                    if (!selectedResearchInstrumentId) {
                      return;
                    }
                    const selectedCompany = portfolioResearchSummary?.companies.find(
                      (company) => company.instrumentId === selectedResearchInstrumentId
                    );
                    await openResearchReadiness(
                      selectedResearchInstrumentId,
                      selectedCompany?.companyName ?? researchSummary?.profile.companyName ?? "Company research"
                    );
                  }}
                />
                </>
              ) : null}
              {view === "settings" ? <SettingsView /> : null}
            </>
          ) : null}
        </div>
      </section>
      {researchReadinessDialog ? (
        <ResearchReadinessDialog
          companyName={researchReadinessDialog.companyName}
          readiness={researchReadiness}
          loading={researchReadinessLoading}
          error={researchReadinessError}
          ensuringRequirementIds={ensuringResearchRequirements}
          analysis={stockRuleEngineAnalysis}
          analysisLoading={stockRuleEngineLoading}
          analysisError={stockRuleEngineError}
          onClose={() => {
            setResearchReadinessDialog(null);
            setResearchReadiness(null);
            setResearchReadinessError(null);
            setStockRuleEngineAnalysis(null);
            setStockRuleEngineError(null);
          }}
          onFindData={(requirements) => { void findResearchData(requirements); }}
          onRunAnalysis={(allowPartial) => { void runStockRuleEngineAnalysis(allowPartial); }}
        />
      ) : null}
    </main>
  );
}

function ResearchReadinessDialog({
  companyName,
  readiness,
  loading,
  error,
  ensuringRequirementIds,
  analysis,
  analysisLoading,
  analysisError,
  onClose,
  onFindData,
  onRunAnalysis
}: {
  companyName: string;
  readiness: ResearchReadiness | null;
  loading: boolean;
  error: string | null;
  ensuringRequirementIds: string[];
  analysis: StockRuleEngineAnalysis | null;
  analysisLoading: boolean;
  analysisError: string | null;
  onClose: () => void;
  onFindData: (requirements: string[]) => void;
  onRunAnalysis: (allowPartial: boolean) => void;
}) {
  const dialogRef = useRef<HTMLElement>(null);
  const findable = readiness?.requirements.filter((item) =>
    item.supportedActions.includes("FIND_DATA") && item.status !== "READY_FRESH"
  ) ?? [];

  useEffect(() => {
    const previouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const appShell = document.querySelector<HTMLElement>(".app-shell");
    const previousBodyOverflow = document.body.style.overflow;
    const previousBodyPaddingRight = document.body.style.paddingRight;
    const previousAppInert = appShell?.inert ?? false;
    const previousAppAriaHidden = appShell?.getAttribute("aria-hidden") ?? null;
    const scrollbarWidth = window.innerWidth - document.documentElement.clientWidth;

    document.body.style.overflow = "hidden";
    if (scrollbarWidth > 0) {
      const currentPadding = Number.parseFloat(window.getComputedStyle(document.body).paddingRight) || 0;
      document.body.style.paddingRight = `${currentPadding + scrollbarWidth}px`;
    }
    if (appShell) {
      appShell.inert = true;
      appShell.setAttribute("aria-hidden", "true");
    }
    dialogRef.current?.querySelector<HTMLElement>("[data-readiness-close]")?.focus();

    return () => {
      document.body.style.overflow = previousBodyOverflow;
      document.body.style.paddingRight = previousBodyPaddingRight;
      if (appShell) {
        appShell.inert = previousAppInert;
        if (previousAppAriaHidden === null) appShell.removeAttribute("aria-hidden");
        else appShell.setAttribute("aria-hidden", previousAppAriaHidden);
      }
      previouslyFocused?.focus();
    };
  }, []);

  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(
        "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])"
      )).filter((element) => !element.hidden && element.getClientRects().length > 0);
      if (!focusable.length) {
        event.preventDefault();
        dialogRef.current.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  if (typeof document === "undefined") return null;

  return createPortal(
    <div className="readiness-popup-backdrop" onMouseDown={(event) => {
      if (event.target === event.currentTarget) onClose();
    }}>
      <section
        ref={dialogRef}
        className="readiness-popup"
        role="dialog"
        aria-modal="true"
        aria-labelledby="research-readiness-title"
        tabIndex={-1}
      >
        <header className="readiness-popup-header">
          <div>
            <p className="eyebrow">Research readiness</p>
            <h2 id="research-readiness-title">{companyName}</h2>
          </div>
          <button data-readiness-close type="button" className="icon-button" aria-label="Close research readiness" onClick={onClose}>
            <X size={20} aria-hidden="true" />
          </button>
        </header>
        <div className="readiness-popup-body">
          {loading ? <Skeleton rows={5} /> : null}
          {error ? <p className="readiness-popup-error" role="alert">{error}</p> : null}
          {readiness ? <>
            <div className="readiness-summary" aria-label="Research data completeness">
              <div><span>Overall status</span><strong>{readiness.overallStatus.replaceAll("_", " ")}</strong></div>
              <div><span>Completeness</span><strong>{readiness.overallCompletenessPct}%</strong></div>
              <div><span>Critical completeness</span><strong>{readiness.criticalCompletenessPct}%</strong></div>
              <div><span>Data confidence</span><strong>{readiness.confidence} · {readiness.confidencePct}%</strong></div>
            </div>
            <div className="readiness-primary-actions">
              {findable.length ? <Button
                onClick={() => onFindData(findable.map((item) => item.requirementId))}
                disabled={ensuringRequirementIds.length > 0}
              >{ensuringRequirementIds.length ? "Finding targeted data…" : "Find required data"}</Button> : null}
              <div className="readiness-analysis-actions">
                {readiness.analysisEligibility?.fullAnalysisAllowed ? (
                  <Button onClick={() => onRunAnalysis(false)} disabled={analysisLoading || ensuringRequirementIds.length > 0}>
                    {analysisLoading ? "Running deterministic analysis…" : "Run Analysis"}
                  </Button>
                ) : readiness.analysisEligibility?.partialAnalysisAllowed ? (
                  <Button variant="secondary" onClick={() => onRunAnalysis(true)} disabled={analysisLoading || ensuringRequirementIds.length > 0}>
                    {analysisLoading ? "Running deterministic analysis…" : "Run Partial Analysis"}
                  </Button>
                ) : (
                  <p className="readiness-refresh-state">Analysis needs more critical data. Use Find Data for the listed requirements.</p>
                )}
              </div>
            </div>
            {analysisError ? <p className="readiness-popup-error" role="alert">{analysisError}</p> : null}
            {analysis ? <StockRuleEngineBreakdown analysis={analysis} /> : null}
            {readiness.refreshState?.executedCapabilities.length ? <p className="readiness-refresh-state" role="status">
              Targeted capabilities: {readiness.refreshState.executedCapabilities.join(", ").replaceAll("_", " ")}
            </p> : null}
            <div className="readiness-requirements">
              {readiness.requirements.map((requirement) => (
                <ResearchReadinessRow
                  key={requirement.requirementId}
                  requirement={requirement}
                  ensuring={ensuringRequirementIds.includes(requirement.requirementId)}
                  onFindData={() => onFindData([requirement.requirementId])}
                />
              ))}
            </div>
          </> : null}
          </div>
      </section>
    </div>,
    document.body
  );
}

function ResearchReadinessRow({
  requirement,
  ensuring,
  onFindData
}: {
  requirement: ResearchReadinessRequirement;
  ensuring: boolean;
  onFindData: () => void;
}) {
  const tone = readinessStatusTone(requirement.status);
  return <article className={`readiness-requirement readiness-requirement-${tone}`}>
    <div className="readiness-requirement-heading">
      <div>
        <strong>{requirement.area.replaceAll("_", " ")}</strong>
        <small>{requirement.requirementId.replaceAll("_", " ")} · {requirement.importance}</small>
      </div>
      <span className={`readiness-status readiness-status-${tone}`}>{requirement.status.replaceAll("_", " ")}</span>
    </div>
    <p>{requirement.asOf ? `As of ${new Date(requirement.asOf).toLocaleString()}` : "No eligible as-of date"}</p>
    <p>{requirement.sourceProvider ? <>Source: {requirement.sourceProvider.replaceAll("_", " ")}{requirement.sourceUrl ? <> · <a href={requirement.sourceUrl} target="_blank" rel="noreferrer">View source ↗</a></> : null}</> : "Source unavailable"}</p>
    {requirement.applicabilityReason ? <p>Applicability: {requirement.applicability?.replaceAll("_", " ")} · {requirement.applicabilityReason.replaceAll("_", " ")}{requirement.businessClassification ? ` (${requirement.businessClassification})` : ""}</p> : null}
    {requirement.status === "NOT_APPLICABLE" ? <p>Excluded from completeness. No score assigned.</p> : null}
    {requirement.requirementId === "CURRENT_NEWS" && requirement.acquisitionObservation?.history?.some((scan) => scan.outcome === "SUCCESS_EMPTY") ? <p>The latest successful provider scan found no qualifying current events. No news score was inferred.</p> : null}
    {requirement.acquisitionObservation ? <p>Last acquisition: {requirement.acquisitionObservation.outcome.replaceAll("_", " ")} · {requirement.acquisitionObservation.provider.replaceAll("_", " ")}{requirement.acquisitionObservation.failure_reason ? `: ${requirement.acquisitionObservation.failure_reason}` : ""}</p> : null}
    {requirement.missingInputIds.length ? <p>Missing inputs: {requirement.missingInputIds.map((id) => id.replaceAll("_", " ")).join(", ")}</p> : null}
    {requirement.concreteRequirements?.some((input) => input.applicability === "NOT_APPLICABLE") ? <p>Not applicable: {requirement.concreteRequirements.filter((input) => input.applicability === "NOT_APPLICABLE").map((input) => input.inputId.replaceAll("_", " ")).join(", ")}</p> : null}
    {requirement.missingReason ? <p className="readiness-reason">{requirement.missingReason.replaceAll("_", " ")}</p> : null}
    {requirement.conflictReason ? <p className="readiness-reason">{requirement.conflictReason.replaceAll("_", " ")}</p> : null}
    <div className="readiness-actions">
      {requirement.supportedActions.includes("FIND_DATA") ? <Button onClick={onFindData} disabled={ensuring}>{ensuring ? "Finding…" : "Find Data"}</Button> : null}
      {requirement.supportedActions.includes("UPLOAD_EVIDENCE") ? <Button variant="secondary" disabled title="Evidence upload arrives in a later iteration">Upload Evidence</Button> : null}
    </div>
  </article>;
}

function StockRuleEngineBreakdown({ analysis }: { analysis: StockRuleEngineAnalysis }) {
  const tone = analysis.riskOverrides.length
    ? "danger"
    : analysis.overallScore != null && analysis.overallScore >= 65
      ? "ready"
      : analysis.overallScore != null && analysis.overallScore < 50
        ? "danger"
        : "neutral";
  return <section className={`rule-engine-result rule-engine-result-${tone}`} aria-label="Deterministic stock analysis">
    <header>
      <div>
        <p className="eyebrow">{analysis.ruleEngineVersion.replaceAll("_", " ")}</p>
        <h3>{analysis.overallScore == null ? "Insufficient data" : `${analysis.overallScore.toFixed(2)}/100`}</h3>
      </div>
      <span className={`readiness-status readiness-status-${tone}`}>{analysis.decisionSignal.replaceAll("_", " ")}</span>
    </header>
    <div className="rule-engine-score-grid">
      <div><span>Quality</span><strong>{scoreText(analysis.qualityScore)}</strong></div>
      <div><span>Opportunity</span><strong>{scoreText(analysis.opportunityScore)}</strong></div>
      <div><span>Risk</span><strong>{scoreText(analysis.riskScore)}</strong></div>
      <div><span>Confidence</span><strong>{analysis.confidence} · {analysis.confidenceScore.toFixed(2)}%</strong></div>
    </div>
    <p className="rule-engine-meta">
      Calculated {new Date(analysis.calculatedAt).toLocaleString()} · input fingerprint {analysis.inputFingerprint.slice(0, 12)} · {analysis.cacheHit ? "cached exact-input result" : "new calculation"}
    </p>
    {analysis.riskOverrides.length ? <div className="rule-engine-overrides" role="alert">
      <strong>Risk override</strong>
      {analysis.riskOverrides.map((override) => <p key={override.code}>{override.severity}: {override.code.replaceAll("_", " ")} · {override.effect.replaceAll("_", " ")}</p>)}
    </div> : null}
    <div className="rule-engine-area-table" role="table" aria-label="Rule Engine area score breakdown">
      <div className="rule-engine-area-row rule-engine-area-header" role="row">
        <span>Area</span><span>Weight</span><span>Score</span><span>Contribution</span><span>Status</span>
      </div>
      {analysis.areaScores.map((area) => <details key={area.area} className="rule-engine-area-row">
        <summary>
          <span>{area.area.replaceAll("_", " ")}</span>
          <span>{area.weight}%</span>
          <span>{scoreText(area.rawScore)}</span>
          <span>{area.rawScore == null ? "—" : area.weightedContribution.toFixed(2)}</span>
          <span>{area.status.replaceAll("_", " ")}</span>
        </summary>
        <div className="rule-engine-area-detail">
          {area.metrics.length ? <div className="rule-engine-metrics">
            {area.metrics.map((metric, index) => <article key={`${metric.rule}-${index}`}>
              <strong>{metric.metric.replaceAll("_", " ")} · {metric.score.toFixed(2)}/100</strong>
              <p>{formatRuleMetricValue(metric.value, metric.unit)} · rule {metric.rule} · applied weight {metric.appliedWeightPct.toFixed(2)}%</p>
              <p>Source: {metric.source.replaceAll("_", " ")}{metric.asOf ? ` · as of ${new Date(metric.asOf).toLocaleDateString()}` : ""}{metric.sourceUrl ? <> · <a href={metric.sourceUrl} target="_blank" rel="noreferrer">View source ↗</a></> : null}</p>
            </article>)}
          </div> : <p>No scoreable metric is available for this area.</p>}
          {area.sourceReferences.length ? <p><strong>Area evidence sources:</strong>{" "}{area.sourceReferences.map((source, index) => <span key={`${source.sourceUrl}-${index}`}>{index ? " · " : ""}<a href={source.sourceUrl} target="_blank" rel="noreferrer">{source.sourceProvider?.replaceAll("_", " ") ?? "Source"} ↗</a>{source.asOf ? ` (${new Date(source.asOf).toLocaleDateString()})` : ""}</span>)}</p> : null}
          {area.positiveFactors.length ? <p><strong>Positive:</strong> {area.positiveFactors.join(" · ")}</p> : null}
          {area.negativeFactors.length ? <p><strong>Negative:</strong> {area.negativeFactors.join(" · ")}</p> : null}
          {area.missingInputs.length ? <p><strong>Missing:</strong> {area.missingInputs.join(", ").replaceAll("_", " ")}</p> : null}
        </div>
      </details>)}
    </div>
  </section>;
}

function scoreText(value?: number | null): string {
  return value == null ? "—" : value.toFixed(2);
}

function formatRuleMetricValue(value: unknown, unit?: string | null): string {
  const rendered = typeof value === "object" && value !== null ? JSON.stringify(value) : String(value ?? "—");
  return unit ? `${rendered} ${unit.replaceAll("_", " ")}` : rendered;
}

function readinessStatusTone(status: ResearchReadinessRequirement["status"]): "ready" | "warning" | "danger" | "neutral" {
  if (status === "READY_FRESH") return "ready";
  if (["READY_STALE", "PARTIAL", "REFRESHING"].includes(status)) return "warning";
  if (status === "UNSUPPORTED" || status === "NOT_APPLICABLE") return "neutral";
  return "danger";
}

function filingSourceLabel(sourceName: string): string {
  return sourceName.trim().split(/\s+/)[0] || "Official";
}

function AuthPasswordInput({ name, placeholder }: { name: string; placeholder: string }) {
  const [visible, setVisible] = useState(false);
  return <span className="password-input"><input name={name} type={visible ? "text" : "password"} required minLength={12} placeholder={placeholder} /><button className="password-toggle" type="button" aria-label={visible ? "Hide password" : "Show password"} onClick={() => setVisible(!visible)}>{visible ? <EyeOff size={18} aria-hidden="true" /> : <Eye size={18} aria-hidden="true" />}</button></span>;
}

function SignInView({
  error,
  onLogin,
  onEmailLogin
}: {
  error: ApiFailure | null;
  onLogin: (userKey: "user-a" | "user-b") => void;
  onEmailLogin: (email: string, password: string) => void;
}) {
  const [mode, setMode] = useState<"LOGIN" | "REGISTER" | "VERIFY" | "FORGOT" | "RESET" | "RESEND">("LOGIN");
  const [message, setMessage] = useState<string | null>(null);
  const [verificationEmail, setVerificationEmail] = useState("");
  useEffect(() => { const token = new URLSearchParams(window.location.search).get("token"); if (window.location.pathname === "/verify-email" && token) { queueMicrotask(() => { setMode("VERIFY"); setMessage("Verifying your email..."); authApi.verifyEmail(token).then(() => setMessage("Email verified successfully. You can now sign in.")).catch(() => setMessage("Verification link is invalid or expired.")); }); } }, []);
  async function submit(form: FormData) {
    const email = String(form.get("email") || ""), password = String(form.get("password") || "");
    if (mode === "LOGIN") return onEmailLogin(email, password);
    if (mode === "VERIFY") { await authApi.verifyEmail(String(form.get("token") || "")); setMessage("Email verified. You can now sign in."); setMode("LOGIN"); return; }
    if (mode === "FORGOT") { const result = await authApi.requestPasswordReset(email); setMessage(result.message); setMode("RESET"); return; }
    if (mode === "RESET") { if (password !== String(form.get("confirmPassword") || "")) { setMessage("Passwords do not match."); return; } await authApi.confirmPasswordReset(String(form.get("token") || ""), password); setMessage("Password reset. You can now sign in."); setMode("LOGIN"); return; }
    if (mode === "RESEND") { await authApi.resendVerification(email); setMessage("If this account is awaiting verification, a new verification email has been sent."); return; }
    if (password !== String(form.get("confirmPassword") || "")) { setMessage("Passwords do not match."); return; }
    await authApi.register({ email, password, firstName: String(form.get("firstName") || ""), lastName: String(form.get("lastName") || "") });
    setVerificationEmail(email); setMessage("We sent a verification link to your email address."); setMode("VERIFY");
  }
  return (
    <main className="signin-shell">
      <section className="signin-panel">
        <div className="brand-lockup">
          <div className="brand-mark">AI</div>
          <div>
            <strong>AI Investment</strong>
            <span>Secure workspace</span>
          </div>
        </div>
        <h1>{mode === "LOGIN" ? "Sign in" : mode === "REGISTER" ? "Create account" : mode === "VERIFY" ? "Verify email" : mode === "FORGOT" ? "Forgot password" : mode === "RESEND" ? "Resend verification email" : "Reset password"}</h1>
        <p>{mode === "LOGIN" ? "Use your verified account." : "Complete the account verification step to continue."}</p>
        {error ? <ErrorState message={error.message} correlationId={error.correlationId} /> : null}
        {message ? <p role="status">{message}</p> : null}
        <form className="signin-actions" action={(form) => void submit(form).catch((err) => setMessage(getApiFailure(err).message))}>
          {mode === "VERIFY" ? (frontendConfig.authDevLoginEnabled ? <input name="token" required placeholder="Verification token" /> : <p>Check your email and open the verification link.</p>) : mode === "RESET" ? <><input name="token" required placeholder="Reset token" /><AuthPasswordInput name="password" placeholder="New password" /><AuthPasswordInput name="confirmPassword" placeholder="Confirm new password" /></> : mode === "FORGOT" || mode === "RESEND" ? <input name="email" type="email" required placeholder="Email" /> : <><input name="email" type="email" required placeholder="Email" />{mode === "REGISTER" ? <><input name="firstName" required placeholder="First name" /><input name="lastName" required placeholder="Last name" /></> : null}<AuthPasswordInput name="password" placeholder="Password (minimum 12 characters)" />{mode === "REGISTER" ? <AuthPasswordInput name="confirmPassword" placeholder="Confirm password" /> : null}</>}
          <Button type="submit">{mode === "LOGIN" ? "Sign in" : mode === "REGISTER" ? "Register" : mode === "RESEND" ? "Resend verification email" : "Verify email"}</Button>
        </form>
        <p><button type="button" onClick={() => setMode(mode === "LOGIN" ? "REGISTER" : "LOGIN")}>{mode === "LOGIN" ? "Create an account" : "Back to sign in"}</button></p>
        {mode === "VERIFY" ? <p><button type="button" onClick={() => authApi.resendVerification(verificationEmail).then((result) => setMessage(result.message))}>Resend verification email</button></p> : null}
        {mode === "LOGIN" ? <><p><button type="button" onClick={() => setMode("FORGOT")}>Forgot password?</button></p><p><button type="button" onClick={() => setMode("RESEND")}>Resend verification email</button></p></> : null}
        {frontendConfig.authDevLoginEnabled ? <div className="signin-actions">
          <Button onClick={() => onLogin("user-a")}>Sign in as User A</Button>
          <Button variant="secondary" onClick={() => onLogin("user-b")}>Sign in as User B</Button>
        </div> : null}
      </section>
    </main>
  );
}

function PortfolioSelector({
  portfolios,
  selectedPortfolioId,
  onChange
}: {
  portfolios: PortfolioListItem[];
  selectedPortfolioId: string;
  onChange: (value: string) => void;
}) {
  if (portfolios.length === 0) {
    return null;
  }

  return (
    <label className="portfolio-select">
      <span>Portfolio</span>
      <select value={selectedPortfolioId} onChange={(event) => onChange(event.target.value)}>
        {portfolios.map((portfolio) => (
          <option value={portfolio.portfolioId} key={portfolio.portfolioId}>
            {portfolio.name}
          </option>
        ))}
      </select>
    </label>
  );
}

function ResearchContextSelector({
  portfolios,
  watchlists,
  context,
  onChange,
}: {
  portfolios: PortfolioListItem[];
  watchlists: ResearchWatchlist[];
  context: ResearchContext;
  onChange: (value: string) => void;
}) {
  const value = context.kind === "PORTFOLIO"
    ? `portfolio:${context.portfolioId}`
    : context.kind === "WATCHLIST"
      ? `watchlist:${context.watchlistId}`
      : `pending:${context.region}`;
  return (
    <label className="portfolio-select research-context-select">
      <span>Research context</span>
      <select value={value} onChange={(event) => onChange(event.target.value)}>
        {context.kind === "WATCHLIST_PENDING" ? (
          <option value={value}>Opening {context.name}…</option>
        ) : null}
        <optgroup label="Portfolios">
          {portfolios.map((portfolio) => (
            <option value={`portfolio:${portfolio.portfolioId}`} key={`portfolio:${portfolio.portfolioId}`}>
              {portfolio.name}
            </option>
          ))}
        </optgroup>
        <optgroup label="Watchlists">
          {watchlists.map((watchlist) => (
            <option value={`watchlist:${watchlist.watchlistId}`} key={`watchlist:${watchlist.watchlistId}`}>
              {watchlist.name}
            </option>
          ))}
        </optgroup>
      </select>
    </label>
  );
}

function LoadingView() {
  return (
    <div className="loading-grid">
      <Skeleton rows={4} />
      <Skeleton rows={4} />
      <Skeleton rows={8} />
    </div>
  );
}

function PortfolioTabs({
  portfolios,
  selected,
  onSelect
}: {
  portfolios: PortfolioListItem[];
  selected: "ALL" | string;
  onSelect: (value: "ALL" | string) => void;
}) {
  return (
    <nav className="portfolio-tabs" aria-label="Portfolio views">
      <button className={selected === "ALL" ? "active" : ""} onClick={() => onSelect("ALL")} type="button">All</button>
      {portfolios.map((portfolio) => (
        <button
          className={selected === portfolio.portfolioId ? "active" : ""}
          onClick={() => onSelect(portfolio.portfolioId)}
          type="button"
          key={portfolio.portfolioId}
        >
          {portfolio.name}
        </button>
      ))}
    </nav>
  );
}

function MultiPortfolioDashboard({
  dashboard,
  performance,
  performanceRegion,
  performanceSector,
  performancePeriod,
  sectorOptions,
  sectorOptionsLoading,
  onPerformanceRegion,
  onPerformanceSector,
  onPerformancePeriod,
  onCreate,
  creating,
  selectedResearchInstrumentId,
  onOpenResearch,
  onAddWatchlist,
  savedInstrumentIds,
  watchlistBusy,
  researchOnly = false,
}: {
  onAddWatchlist: (selection: MarketIntelligenceSelection) => void;
  savedInstrumentIds: string[];
  watchlistBusy: boolean;
  researchOnly?: boolean;
  dashboard: PortfolioDashboard | null;
  performance: SectorPerformance | null;
  performanceRegion: SectorPerformance["region"];
  performanceSector: string;
  performancePeriod: SectorPerformance["period"];
  sectorOptions: MarketUniverseSector[];
  sectorOptionsLoading: boolean;
  onPerformanceRegion: (value: SectorPerformance["region"]) => void;
  onPerformanceSector: (value: string) => void;
  onPerformancePeriod: (value: SectorPerformance["period"]) => void;
  onCreate: () => void;
  creating: boolean;
  selectedResearchInstrumentId: string;
  onOpenResearch: (selection: MarketIntelligenceSelection) => void;
}) {
  if (!dashboard && !researchOnly) return null;
  return (
    <div className="dashboard-grid">
      {!researchOnly && dashboard ? <>
      <Card className="wide-panel">
        <div className="panel-header"><div><p className="eyebrow">My portfolios</p><h2>Total portfolio value</h2></div></div>
        <div className="currency-total-grid">
          {Object.entries(dashboard.currencyTotals).map(([currency, amount]) => (
            <MetricCard label={currency} value={formatMoney(amount, currency)} key={currency} />
          ))}
        </div>
        {dashboard.incompleteValuationPortfolioIds.length > 0 ? (
          <p className="broker-note">Some portfolios have incomplete valuation data and are not combined with another currency.</p>
        ) : null}
      </Card>
      <section className="portfolio-card-grid" aria-label="Persisted portfolios">
        {dashboard.portfolios.map((portfolio) => (
          <Card as="article" key={portfolio.portfolioId}>
            <Badge tone={portfolio.provider ? "positive" : "neutral"}>{portfolio.provider ? brokerDisplayName(portfolio.provider) : "Manual"}</Badge>
            <h2>{portfolio.name}</h2>
            <strong>{formatBackendMoney(portfolio.totalMarketValue)}</strong>
            <p>{portfolio.acquisitionSource === "MANUAL_CSV_IMPORT"
              ? `Imported snapshot: ${portfolio.lastImportedAt ? new Date(portfolio.lastImportedAt).toLocaleString() : "Not available"}`
              : `Broker holdings synced: ${portfolio.lastBrokerSyncAt ? new Date(portfolio.lastBrokerSyncAt).toLocaleString() : portfolio.brokerConnectionId ? "Imported previously; sync time unknown" : "Not applicable"}`}</p>
            <p>Market price updated independently in the holdings view.</p>
          </Card>
        ))}
      </section>
      {dashboard.portfolios.length === 0 ? <Card className="wide-panel"><EmptyState title="No portfolios" message="Connect a broker or create a portfolio to begin." /><Button onClick={onCreate} disabled={creating}>{creating ? "Creating..." : "Create portfolio"}</Button></Card> : null}
      </> : null}
      <Card className="wide-panel">
        <div className="panel-header"><div><p className="eyebrow">Market intelligence</p><h2>Sector Performance</h2><p>Top gainers from durable market-price observations.</p></div></div>
        <div className="form-grid">
          <Field label="Region"><select aria-label="Sector performance region" value={performanceRegion} onChange={(event) => onPerformanceRegion(event.target.value as SectorPerformance["region"])}>{["USA", "EUROPE", "INDIA"].map((value) => <option key={value}>{value}</option>)}</select></Field>
          <Field label="Sector"><select aria-label="Sector performance sector" value={performanceSector} disabled={sectorOptionsLoading || sectorOptions.length === 0} onChange={(event) => onPerformanceSector(event.target.value)}>{sectorOptions.length ? sectorOptions.map((value) => <option key={value.name} value={value.name}>{value.name} ({value.instrumentCount})</option>) : <option value="">{sectorOptionsLoading ? "Loading durable sectors…" : "No durable sectors"}</option>}</select></Field>
          <Field label="Period"><select aria-label="Sector performance period" value={performancePeriod} onChange={(event) => onPerformancePeriod(event.target.value as SectorPerformance["period"])}>{["DAY", "WEEK", "MONTH", "YEAR"].map((value) => <option key={value}>{value}</option>)}</select></Field>
        </div>
        {sectorOptionsLoading ? <p className="broker-note">Loading durable sector universe…</p>
          : sectorOptions.length === 0 ? <p className="broker-note">No durable sector-classified universe is available for this region.</p>
          : performance ? <div className="stack-gap">
            <div><h3>Top 5 Performers</h3>{performance.bestPerformers.length ? <section className="portfolio-card-grid" aria-label="Sector performance top performers">{performance.bestPerformers.map((stock) => <MarketPerformanceRow stock={stock} key={`best-${stock.globalInstrumentId}`} onOpenResearch={(value) => onOpenResearch(marketIntelligenceSelection(performance, value))} selected={selectedResearchInstrumentId === stock.globalInstrumentId} watchlistName={regionalWatchlistName(performance.region)} saved={savedInstrumentIds.includes(stock.globalInstrumentId)} busy={watchlistBusy} onAddWatchlist={() => onAddWatchlist(marketIntelligenceSelection(performance, stock))} />)}</section> : <p className="broker-note">Insufficient historical data for top performers.</p>}</div>
            <div><h3>Worst 5 Performers</h3>{performance.worstPerformers.length ? <section className="portfolio-card-grid" aria-label="Sector performance worst performers">{performance.worstPerformers.map((stock) => <MarketPerformanceRow stock={stock} key={`worst-${stock.globalInstrumentId}`} onOpenResearch={(value) => onOpenResearch(marketIntelligenceSelection(performance, value))} selected={selectedResearchInstrumentId === stock.globalInstrumentId} watchlistName={regionalWatchlistName(performance.region)} saved={savedInstrumentIds.includes(stock.globalInstrumentId)} busy={watchlistBusy} onAddWatchlist={() => onAddWatchlist(marketIntelligenceSelection(performance, stock))} />)}</section> : <p className="broker-note">Insufficient historical data for worst performers.</p>}</div>
          </div> : <p className="broker-note">Loading sector performance…</p>}
      </Card>
    </div>
  );
}

function MarketPerformanceRow({
  stock,
  selected,
  onOpenResearch,
  onAddWatchlist,
  watchlistName,
  saved,
  busy,
}: {
  onAddWatchlist: () => void;
  watchlistName: string;
  saved: boolean;
  busy: boolean;
  stock: SectorPerformanceStock;
  selected: boolean;
  onOpenResearch: (stock: SectorPerformanceStock) => void;
}) {
  const tone = performanceRowTone(stock.performancePct);
  const signedPerformance = formatSignedPerformancePct(stock.performancePct);
  return (
    <article className="stack-gap" data-global-instrument-id={stock.globalInstrumentId}>
    <button
      className={`market-performance-row market-performance-row-${tone}`}
      type="button"
      data-performance-direction={tone}
      aria-current={selected ? "true" : undefined}
      aria-label={`${stock.companyName}, ${signedPerformance}, ${performanceDirectionLabel(stock)}`}
      onClick={() => onOpenResearch(stock)}
    >
      <strong>{stock.companyName}</strong>
      <span className="market-performance-identity">{stock.ticker} · {stock.exchange}</span>
      <span className="market-performance-return">{signedPerformance}</span>
    </button>
    <Button variant="secondary" disabled={saved || busy || !stock.globalInstrumentId?.trim()} onClick={onAddWatchlist}>
      {saved ? `Saved in ${watchlistName}` : `Add to ${watchlistName}`}
    </Button>
    </article>
  );
}

function PortfolioCreatePanel({
  onCreate,
  creating,
  newPortfolioName,
  newPortfolioCurrency,
  setNewPortfolioName,
  setNewPortfolioCurrency
}: {
  onCreate: () => void;
  creating: boolean;
  newPortfolioName: string;
  newPortfolioCurrency: string;
  setNewPortfolioName: (value: string) => void;
  setNewPortfolioCurrency: (value: string) => void;
}) {
  return (
    <Card className="create-panel">
      <div>
        <h2>Create a portfolio</h2>
        <p>Connect a broker or create a portfolio to get started.</p>
      </div>
      <div className="form-grid">
        <Field label="Name">
          <input value={newPortfolioName} onChange={(event) => setNewPortfolioName(event.target.value)} />
        </Field>
        <Field label="Base currency" hint="ISO 4217 code">
          <input
            value={newPortfolioCurrency}
            maxLength={3}
            onChange={(event) => setNewPortfolioCurrency(event.target.value.toUpperCase())}
          />
        </Field>
      </div>
      <Button onClick={onCreate} disabled={creating || newPortfolioName.trim().length === 0}>
        {creating ? "Creating..." : "Create portfolio"}
      </Button>
    </Card>
  );
}

function DashboardView({
  portfolio,
  summary,
  positions,
  onCreate,
  onSync,
  onRefreshPrices,
  creating,
  syncing,
  newPortfolioName,
  newPortfolioCurrency,
  setNewPortfolioName,
  setNewPortfolioCurrency
}: {
  portfolio?: Portfolio;
  summary: PortfolioSummary | null;
  positions: PortfolioPosition[];
  onCreate: () => void;
  onSync?: () => void;
  onRefreshPrices?: () => void;
  creating: boolean;
  syncing: boolean;
  newPortfolioName: string;
  newPortfolioCurrency: string;
  setNewPortfolioName: (value: string) => void;
  setNewPortfolioCurrency: (value: string) => void;
}) {
  if (!portfolio) {
    return (
      <PortfolioCreatePanel
        onCreate={onCreate}
        creating={creating}
        newPortfolioName={newPortfolioName}
        newPortfolioCurrency={newPortfolioCurrency}
        setNewPortfolioName={setNewPortfolioName}
        setNewPortfolioCurrency={setNewPortfolioCurrency}
      />
    );
  }

  const pricedPositions = positions.filter((position) => position.unrealizedProfitLossPercent != null);
  const topGainers = [...pricedPositions].sort((a, b) => (b.unrealizedProfitLossPercent ?? 0) - (a.unrealizedProfitLossPercent ?? 0)).slice(0, 3);
  const topLosers = [...pricedPositions].sort((a, b) => (a.unrealizedProfitLossPercent ?? 0) - (b.unrealizedProfitLossPercent ?? 0)).slice(0, 3);
  const sourceLabels = portfolioSourceLabels(positions);
  const liveTotals = onRefreshPrices && positions.every((position) => position.marketValue && position.unrealizedProfitLoss)
    ? positions.reduce((totals, position) => ({
    marketValue: totals.marketValue + (position.marketValue?.amount ?? 0),
    pnl: totals.pnl + (position.unrealizedProfitLoss?.amount ?? 0),
    cost: totals.cost + position.costBasis.amount,
  }), { marketValue: 0, pnl: 0, cost: 0 }) : null;
  const syncAction = onSync ? (
    <Button onClick={onSync} disabled={syncing} variant="secondary">
      <RefreshCw size={16} />
      {syncing ? "Syncing..." : sourceLabels.syncButton}
    </Button>
  ) : null;
  const refreshAction = onRefreshPrices ? (
    <Button onClick={onRefreshPrices} disabled={syncing} variant="secondary"><RefreshCw size={16} />{syncing ? "Refreshing..." : "Refresh Prices"}</Button>
  ) : null;

  return (
    <div className="dashboard-grid">
      <section className="metrics-grid" aria-label="Portfolio summary">
        <MetricCard
          label="Portfolio value"
          value={liveTotals ? formatMoney(liveTotals.marketValue, portfolio.baseCurrency) : formatBackendMoney(summary?.totalMarketValue)}
          meta={sourceLabels.syncMeta}
        />
        <MetricCard label={sourceLabels.totalProfitLoss} value={liveTotals ? formatMoney(liveTotals.pnl, portfolio.baseCurrency) : formatBackendMoney(summary?.unrealizedProfitLoss)} tone={valueTone(liveTotals?.pnl ?? summary?.unrealizedProfitLoss?.amount)} />
        <MetricCard label={sourceLabels.returnLabel} value={formatPercent(liveTotals && liveTotals.cost ? liveTotals.pnl / liveTotals.cost * 100 : summary?.unrealizedProfitLossPercent)} tone={valueTone(liveTotals?.pnl ?? summary?.unrealizedProfitLossPercent)} />
        <MetricCard label="Cash" value={formatBackendMoney(summary?.cash)} />
        <MetricCard label="Holdings" value={String(summary?.positions ?? 0)} />
        <MetricCard label="Portfolio risk" value="Pending" meta="Risk service not implemented" tone="warning" />
      </section>

      <Card className="wide-panel">
        <div className="panel-header">
          <div>
            <h2>{portfolio.name}</h2>
            <p>Last updated: {summary ? sourceLabels.lastUpdated : "not synced"} - Source: {sourceLabels.source}</p>
          </div>
          {syncAction}{refreshAction}
        </div>
        {summary && !onRefreshPrices ? <AllocationCharts summary={summary} /> : positions.length ? <p>Allocation uses refreshed holding values in the Portfolio view.</p> : <EmptyState title="No positions" message="This portfolio currently has no positions." />}
      </Card>

      <MovementPanel title="Top gainers" icon={TrendingUp} positions={topGainers} sourceLabels={sourceLabels} />
      <MovementPanel title="Top losers" icon={TrendingDown} positions={topLosers} sourceLabels={sourceLabels} />

      <Card className="wide-panel">
        <div className="panel-header">
          <div>
            <h2>AI opportunities</h2>
            <p>Recommendation logic is not yet available.</p>
          </div>
          <Badge tone="neutral">Future ready</Badge>
        </div>
        <EmptyState title="No AI ratings yet" message="BUY, HOLD, SELL, and opportunity scores will appear after recommendation services are approved and implemented." />
      </Card>
    </div>
  );
}

function MovementPanel({
  title,
  icon: Icon,
  positions,
  sourceLabels
}: {
  title: string;
  icon: typeof TrendingUp;
  positions: PortfolioPosition[];
  sourceLabels: ReturnType<typeof portfolioSourceLabels>;
}) {
  return (
    <Card>
      <div className="panel-header compact">
        <h2>{title}</h2>
        <Icon size={18} aria-hidden="true" />
      </div>
      {positions.length === 0 ? (
        <EmptyState title="No synced positions" message={sourceLabels.emptyMessage} />
      ) : (
        <div className="movement-list">
          {positions.map((position) => (
            <div key={position.positionId}>
              <span>{position.instrument.ticker}</span>
              <strong className={position.unrealizedProfitLossPercent == null ? undefined : position.unrealizedProfitLossPercent >= 0 ? "positive-text" : "negative-text"}>
                {formatPercent(position.unrealizedProfitLossPercent)}
              </strong>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

function PortfolioView({
  portfolio,
  summary,
  positions,
  rawPositions,
  history,
  historyRange,
  historyLoading,
  onHistoryRange,
  searchText,
  sortKey,
  onSearch,
  onSort,
  onCreate,
  onSync,
  onRefreshPrices,
  creating,
  syncing,
  newPortfolioName,
  newPortfolioCurrency,
  setNewPortfolioName,
  setNewPortfolioCurrency,
  onUpdateDisplayName,
  portfolioResearch
}: {
  portfolio?: Portfolio;
  summary: PortfolioSummary | null;
  positions: PortfolioPosition[];
  rawPositions: PortfolioPosition[];
  history: PortfolioHistory | null;
  historyRange: PortfolioHistoryRange;
  historyLoading: boolean;
  onHistoryRange: (value: PortfolioHistoryRange) => void;
  searchText: string;
  sortKey: SortKey;
  onSearch: (value: string) => void;
  onSort: (value: SortKey) => void;
  onCreate: () => void;
  onSync?: () => void;
  onRefreshPrices?: () => void;
  creating: boolean;
  syncing: boolean;
  newPortfolioName: string;
  newPortfolioCurrency: string;
  setNewPortfolioName: (value: string) => void;
  setNewPortfolioCurrency: (value: string) => void;
  onUpdateDisplayName: (position: PortfolioPosition, customDisplayName: string | null) => Promise<void>;
  portfolioResearch: PortfolioResearchSummary | null;
}) {
  if (!portfolio) {
    return (
      <PortfolioCreatePanel
        onCreate={onCreate}
        creating={creating}
        newPortfolioName={newPortfolioName}
        newPortfolioCurrency={newPortfolioCurrency}
        setNewPortfolioName={setNewPortfolioName}
        setNewPortfolioCurrency={setNewPortfolioCurrency}
      />
    );
  }

  const sourceLabels = portfolioSourceLabels(rawPositions);
  const manualMarketTotals = onRefreshPrices && rawPositions.every((position) => position.marketValue && position.unrealizedProfitLoss)
    ? rawPositions.reduce((totals, position) => ({
    marketValue: totals.marketValue + (position.marketValue?.amount ?? 0),
    costBasis: totals.costBasis + position.costBasis.amount,
    pnl: totals.pnl + (position.unrealizedProfitLoss?.amount ?? 0)
  }), { marketValue: 0, costBasis: 0, pnl: 0 }) : null;
  const displayCurrency = summary?.baseCurrency ?? portfolio.baseCurrency;
  const displayReturn = manualMarketTotals && manualMarketTotals.costBasis !== 0
    ? manualMarketTotals.pnl / manualMarketTotals.costBasis * 100
    : summary?.unrealizedProfitLossPercent;

  return (
    <div className="portfolio-layout">
      <Card className="wide-panel">
        <div className="panel-header">
          <div>
            <p className="eyebrow">{portfolio.provider ? brokerDisplayName(portfolio.provider) : "Manual portfolio"}</p>
            <h2>{portfolio.name}</h2>
            {portfolio.brokerConnectionId ? <p>Last successful sync: {portfolio.lastBrokerSyncAt ? new Date(portfolio.lastBrokerSyncAt).toLocaleString() : "Not yet available"}</p> : null}
          </div>
          {onSync ? <Button onClick={onSync} disabled={syncing} variant="secondary"><RefreshCw size={16} />{syncing ? "Syncing..." : "Sync"}</Button> : null}
          {onRefreshPrices ? <Button onClick={onRefreshPrices} disabled={syncing} variant="secondary"><RefreshCw size={16} />{syncing ? "Refreshing..." : "Refresh Prices"}</Button> : null}
        </div>
        <p>Base currency: {portfolio.baseCurrency}</p>
      </Card>
      <section className="metrics-grid" aria-label="Portfolio totals">
        <MetricCard label="Securities market value" value={manualMarketTotals ? formatMoney(manualMarketTotals.marketValue, displayCurrency) : formatBackendMoney(summary?.totalMarketValue)} />
        <MetricCard label="Total portfolio value" value={manualMarketTotals ? formatMoney(manualMarketTotals.marketValue + (summary?.cash.amount ?? 0), displayCurrency) : summary?.totalMarketValue ? formatMoney(summary.totalMarketValue.amount + summary.cash.amount, summary.baseCurrency) : "N/A"} />
        <MetricCard label={sourceLabels.costBasis} value={manualMarketTotals ? formatMoney(manualMarketTotals.costBasis, displayCurrency) : formatBackendMoney(summary?.totalCostBasis)} />
        <MetricCard label={sourceLabels.unrealizedProfitLoss} value={manualMarketTotals ? formatMoney(manualMarketTotals.pnl, displayCurrency) : formatBackendMoney(summary?.unrealizedProfitLoss)} tone={valueTone(manualMarketTotals?.pnl ?? summary?.unrealizedProfitLoss?.amount)} />
        <MetricCard label={sourceLabels.returnLabel} value={formatPercent(displayReturn)} />
        <MetricCard label="Cash" value={formatBackendMoney(summary?.cash)} />
        <MetricCard label="Position count" value={String(summary?.positions ?? 0)} />
      </section>

      <PortfolioHistoryPanel
        history={history}
        range={historyRange}
        loading={historyLoading}
        onRange={onHistoryRange}
      />

      <Card className="wide-panel">
          <div className="panel-header">
            <div>
              <h2>Holdings</h2>
              <p>Search respects ticker, company, ISIN, exchange, and country.</p>
              <ResearchCoverage research={portfolioResearch} />
            </div>
          {onSync ? (
            <Button onClick={onSync} disabled={syncing} variant="secondary">
              <RefreshCw size={16} />
              {syncing ? "Syncing..." : sourceLabels.syncButton}
            </Button>
          ) : null}
          {onRefreshPrices ? (
            <Button onClick={onRefreshPrices} disabled={syncing} variant="secondary">
              <RefreshCw size={16} />{syncing ? "Refreshing..." : "Refresh Prices"}
            </Button>
          ) : null}
        </div>
        <div className="table-toolbar">
          <div className="table-search">
            <Search size={16} aria-hidden="true" />
            <input
              aria-label="Filter holdings"
              placeholder="Filter holdings"
              value={searchText}
              onChange={(event) => onSearch(event.target.value)}
            />
          </div>
          <label className="sort-control">
            <SlidersHorizontal size={16} aria-hidden="true" />
            <span>Sort</span>
            <select value={sortKey} onChange={(event) => onSort(event.target.value as SortKey)}>
              <option value="marketValue">{sourceLabels.sortMarketValue}</option>
              <option value="profitLoss">{sourceLabels.sortProfitLoss}</option>
              <option value="allocation">Allocation</option>
              <option value="company">Company</option>
              <option value="ticker">Ticker</option>
            </select>
          </label>
        </div>
        {rawPositions.length === 0 ? (
          <EmptyState
            title="No positions"
            message="This portfolio currently has no positions."
            action={onSync ? <Button onClick={onSync}>{sourceLabels.syncButton}</Button> : undefined}
          />
        ) : (
          <HoldingsTable positions={positions} summary={summary} portfolioResearch={portfolioResearch} onUpdateDisplayName={onUpdateDisplayName} />
        )}
      </Card>

      {summary && !onRefreshPrices ? (
        <Card className="wide-panel">
          <div className="panel-header">
            <div>
              <h2>Allocation</h2>
              <p>Normalized into {summary.baseCurrency}; charts include textual values for accessibility.</p>
            </div>
            <LineChart size={20} aria-hidden="true" />
          </div>
          <AllocationCharts summary={summary} />
        </Card>
      ) : null}
    </div>
  );
}

function PortfolioHistoryPanel({
  history,
  range,
  loading,
  onRange
}: {
  history: PortfolioHistory | null;
  range: PortfolioHistoryRange;
  loading: boolean;
  onRange: (value: PortfolioHistoryRange) => void;
}) {
  const points = history?.points ?? [];
  const first = points[0];
  const last = points[points.length - 1];
  const absoluteChange = first && last ? last.marketValue.amount - first.marketValue.amount : undefined;
  const hasInvestedCapital = points.some((point) => point.investedCapital);

  return (
    <Card className="wide-panel">
      <div className="panel-header">
        <div>
          <h2>Portfolio value history</h2>
          <p>{history?.investedCapitalStatus === "AVAILABLE" ? "Market value and invested capital." : "Portfolio value change; investment return needs cash-flow history."}</p>
        </div>
        <div className="range-control" aria-label="Portfolio history range">
          {portfolioHistoryRanges.map((value) => (
            <button
              className={range === value ? "range-active" : ""}
              key={value}
              onClick={() => onRange(value)}
              type="button"
            >
              {value}
            </button>
          ))}
        </div>
      </div>
      {loading ? (
        <Skeleton rows={4} />
      ) : points.length === 0 ? (
        <EmptyState title="No portfolio history" message="No portfolio history available yet." />
      ) : (
        <>
          <section className="history-metrics" aria-label="Portfolio value change">
            <MetricCard label="Starting value" value={formatBackendMoney(first?.marketValue)} />
            <MetricCard label="Ending value" value={formatBackendMoney(last?.marketValue)} />
            <MetricCard
              label="Portfolio value change"
              value={absoluteChange === undefined ? "--" : formatMoney(absoluteChange, last?.marketValue.currency)}
              tone={(absoluteChange ?? 0) >= 0 ? "positive" : "negative"}
            />
            <MetricCard
              label="Value change %"
              value={formatChangePercent(first?.marketValue.amount, last?.marketValue.amount)}
              tone={(absoluteChange ?? 0) >= 0 ? "positive" : "negative"}
            />
          </section>
          <PortfolioHistoryChart points={points} showInvestedCapital={hasInvestedCapital} />
          {points.length === 1 ? (
            <p className="history-note">Portfolio history will build as real broker snapshots are collected.</p>
          ) : null}
          {history?.investedCapitalStatus !== "AVAILABLE" ? (
            <p className="history-note">INVESTED_CAPITAL_HISTORY_UNAVAILABLE</p>
          ) : null}
        </>
      )}
    </Card>
  );
}

function PortfolioHistoryChart({
  points,
  showInvestedCapital
}: {
  points: PortfolioHistory["points"];
  showInvestedCapital: boolean;
}) {
  const width = 900;
  const height = 260;
  const padding = { top: 18, right: 28, bottom: 34, left: 64 };
  const marketValues = points.map((point) => point.marketValue.amount);
  const investedValues = showInvestedCapital
    ? points.map((point) => point.investedCapital?.amount).filter((value): value is number => value !== undefined)
    : [];
  const values = [...marketValues, ...investedValues];
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const times = points.map((point) => new Date(point.timestamp).getTime());
  const minTime = Math.min(...times);
  const maxTime = Math.max(...times);
  const timeSpan = maxTime - minTime || 1;
  const singlePoint = points.length === 1;
  const x = (timestamp: string) =>
    singlePoint
      ? padding.left + (width - padding.left - padding.right) / 2
      : padding.left + ((new Date(timestamp).getTime() - minTime) / timeSpan) * (width - padding.left - padding.right);
  const y = (value: number) =>
    singlePoint
      ? padding.top + (height - padding.top - padding.bottom) / 2
      : padding.top + (1 - (value - min) / span) * (height - padding.top - padding.bottom);
  const marketPath = points.length > 1 ? linePath(points.map((point) => [x(point.timestamp), y(point.marketValue.amount)])) : "";
  const investedPath = showInvestedCapital && points.length > 1
    ? linePath(points.filter((point) => point.investedCapital).map((point) => [x(point.timestamp), y(point.investedCapital?.amount ?? 0)]))
    : "";
  const latest = points[points.length - 1];

  return (
    <div className="history-chart">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Portfolio value history chart">
        <line x1={padding.left} y1={padding.top} x2={padding.left} y2={height - padding.bottom} />
        <line x1={padding.left} y1={height - padding.bottom} x2={width - padding.right} y2={height - padding.bottom} />
        <text x={padding.left} y={14}>{formatMoney(max, latest?.marketValue.currency)}</text>
        <text x={padding.left} y={height - 8}>{formatMoney(min, latest?.marketValue.currency)}</text>
        {marketPath ? <path className="market-line" d={marketPath} /> : null}
        {investedPath ? <path className="invested-line" d={investedPath} /> : null}
        {points.map((point) => (
          <circle className="market-point" cx={x(point.timestamp)} cy={y(point.marketValue.amount)} r={points.length === 1 ? 5 : 3} key={point.timestamp}>
            <title>
              {new Date(point.timestamp).toLocaleString()} | Portfolio {formatBackendMoney(point.marketValue)}
              {point.investedCapital ? ` | Invested ${formatBackendMoney(point.investedCapital)}` : ""}
              {` | P/L ${formatBackendMoney(point.unrealizedPnl)}`}
            </title>
          </circle>
        ))}
      </svg>
    </div>
  );
}

function linePath(points: number[][]) {
  return points.map(([x, y], index) => `${index === 0 ? "M" : "L"} ${x.toFixed(2)} ${y.toFixed(2)}`).join(" ");
}

function FreshnessBadge({ freshness }: { freshness: string }) {
  const label = freshness === "END_OF_DAY" ? "EOD" : freshness === "MOCK" ? "DEMO" : freshness.replaceAll("_", "-");
  const tone = freshness === "REAL_TIME" || freshness === "REAL_BROKER" ? "positive" : freshness === "STALE" ? "warning" : freshness === "MOCK" ? "info" : freshness === "UNAVAILABLE" ? "negative" : "neutral";
  return <Badge tone={tone}>{label}</Badge>;
}

function ResearchStatusBadge({ status }: { status?: string | null }) {
  const normalized = status ?? "UNAVAILABLE";
  const label = normalized === "RESOLVED_RESEARCH_AVAILABLE" ? "Available"
    : normalized === "RESOLVED_PARTIAL_DATA" ? "Partial"
    : normalized === "ETF_UNSUPPORTED" || normalized === "RESEARCH_NOT_APPLICABLE" ? "ETF unsupported"
    : normalized.replaceAll("_", " ");
  const tone = normalized === "RESOLVED_RESEARCH_AVAILABLE" ? "positive"
    : normalized === "RESOLVED_PARTIAL_DATA" ? "warning"
    : normalized === "ETF_UNSUPPORTED" || normalized === "RESEARCH_NOT_APPLICABLE" ? "neutral" : "negative";
  return <Badge tone={tone}>Research: {label}</Badge>;
}

function ResearchCoverage({ research }: { research: PortfolioResearchSummary | null }) {
  if (!research) return null;
  const companies = research.companies;
  const available = companies.filter((company) => company.status === "RESOLVED_RESEARCH_AVAILABLE").length;
  const partial = companies.filter((company) => company.status === "RESOLVED_PARTIAL_DATA").length;
  const etfUnsupported = companies.filter((company) => company.status === "ETF_UNSUPPORTED" || company.status === "RESEARCH_NOT_APPLICABLE").length;
  const fresh = companies.filter((company) => company.priceFreshness === "FRESH").length;
  const stale = companies.filter((company) => company.priceFreshness === "STALE").length;
  return <p className="research-coverage" aria-label="Portfolio research coverage"><strong>Research coverage:</strong> {companies.length} holdings · {available} available · {partial} partial · {etfUnsupported} ETF unsupported · {fresh} fresh · {stale} stale</p>;
}

function HoldingsTable({ positions, summary, portfolioResearch, onUpdateDisplayName }: {
  positions: PortfolioPosition[];
  summary: PortfolioSummary | null;
  portfolioResearch: PortfolioResearchSummary | null;
  onUpdateDisplayName: (position: PortfolioPosition, customDisplayName: string | null) => Promise<void>;
}) {
  const [detail, setDetail] = useState<{ position: PortfolioPosition; researchInstrumentId?: string | null } | null>(null);
  const manualMarketTotal = positions.some((position) => position.sourceType === "MANUAL_CSV_IMPORT")
    && positions.every((position) => position.marketValue)
    ? positions.reduce((total, position) => total + (position.marketValue?.amount ?? 0), 0) : null;

  function researchFor(position: PortfolioPosition) {
    return portfolioResearch?.companies.find((company) =>
      company.instrumentId === position.instrument.globalInstrumentId
      || company.instrumentId === position.instrument.instrumentId
      || Boolean(position.instrument.isin && company.isin === position.instrument.isin)
      || (company.ticker === position.instrument.ticker && company.exchange === position.instrument.exchange)
    );
  }

  return (
    <div className="table-frame">
      <table>
        <thead>
          <tr>
            <th>Company</th>
            <th>Ticker</th>
            <th>Quantity</th>
            <th>Average cost</th>
            <th>Latest price</th>
            <th>Market value</th>
            <th>Unrealized P/L</th>
            <th>Unrealized P/L %</th>
            <th>Allocation</th>
            <th>Currency</th>
            <th>Research & price status</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((position) => {
            const allocation = manualMarketTotal !== null
              ? (manualMarketTotal === 0 || !position.marketValue ? null : position.marketValue.amount / manualMarketTotal * 100)
              : getAllocationValue(summary, position);
            const research = researchFor(position);
            const ownershipClass = research?.ownershipIncreases?.length
              ? `ownership-${research.ownershipIncreases.map((value) => value.toLowerCase().replace("_fpi", "")).join("-")}`
              : "";
            const valuationClass = `valuation-${(research?.valuation.state ?? "UNKNOWN").toLowerCase()}`;
            return (
              <tr className={`${valuationClass} ${ownershipClass}`.trim()} key={position.positionId} onClick={() => setDetail({ position, researchInstrumentId: research?.instrumentId ?? position.instrument.globalInstrumentId })}>
                <td>
                  <button className="security-button" type="button" onClick={() => setDetail({ position, researchInstrumentId: research?.instrumentId ?? position.instrument.globalInstrumentId })}>
                    <strong>{position.displayName}</strong>
                    <span>{position.instrument.isin ?? "No ISIN"}</span>
                  </button>
                </td>
                <td>{position.instrument.ticker}</td>
                <td>{position.quantity.toLocaleString("en")}</td>
                <td>{formatMoney(position.averageCost.amount, position.averageCost.currency)}</td>
                <td>{formatBackendMoney(position.currentPrice)}</td>
                <td>{formatBackendMoney(position.marketValue)}</td>
                <td className={position.unrealizedProfitLoss == null ? undefined : position.unrealizedProfitLoss.amount >= 0 ? "positive-text" : "negative-text"}>
                  {formatBackendMoney(position.unrealizedProfitLoss)}
                </td>
                <td className={position.unrealizedProfitLossPercent == null ? undefined : position.unrealizedProfitLossPercent >= 0 ? "positive-text" : "negative-text"}>
                  {formatPercent(position.unrealizedProfitLossPercent)}
                </td>
                <td>{formatPercent(allocation)}</td>
                <td>{position.instrument.tradingCurrency}</td>
                <td className="holding-status-cell">
                  <ResearchStatusBadge status={research?.status} />
                  <span>Position price: {position.dataFreshness === "IMPORTED_SNAPSHOT" ? "Imported snapshot" : position.dataFreshness.replaceAll("_", " ")}</span>
                  <span>Live quote: <FreshnessBadge freshness={position.quote?.freshness ?? "UNAVAILABLE"} /></span>
                  <span>Research price: <FreshnessBadge freshness={research?.priceFreshness ?? "UNKNOWN"} /></span>
                  <span>Valuation: {(research?.valuation.state ?? "UNKNOWN").replaceAll("_", " ")}</span>
                  {research?.ownershipIncreases?.length ? <span>Ownership increase: {research.ownershipIncreases.map((value) => value.replaceAll("_", "/")).join(", ")}</span> : <span>Ownership increase: None reported</span>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {detail ? <StockResearchDrawer position={detail.position} research={detail.researchInstrumentId ? portfolioResearch?.companies.find((company) => company.instrumentId === detail.researchInstrumentId) : researchFor(detail.position)} onUpdateDisplayName={onUpdateDisplayName} onClose={() => setDetail(null)} /> : null}
    </div>
  );
}

type MetricDisplay = { text: string; title?: string };

function metricDisplay(metric?: ProvenancedValue | null): MetricDisplay {
  if (!metric || metric.value === null || metric.value === undefined || metric.value === "") return { text: "N/A" };
  const numeric = Number(metric.value);
  const unit = metric.unit?.trim();
  if (unit?.toUpperCase() === "INR" && Number.isFinite(numeric)) {
    const exact = `₹${new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2, minimumFractionDigits: 2 }).format(numeric)}`;
    const absolute = Math.abs(numeric);
    if (absolute >= 10_000_000) return { text: `₹${new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2, minimumFractionDigits: 2 }).format(numeric / 10_000_000)} Cr`, title: exact };
    if (absolute >= 100_000) return { text: `₹${new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2, minimumFractionDigits: 2 }).format(numeric / 100_000)} Lakh`, title: exact };
    return { text: exact };
  }
  const value = Number.isFinite(numeric) ? new Intl.NumberFormat("en", { maximumFractionDigits: 2, minimumFractionDigits: 2 }).format(numeric) : String(metric.value);
  return { text: `${value}${unit === "percent" || unit === "%" ? "%" : unit && unit !== "ratio" ? ` ${unit}` : ""}` };
}

function metricText(metric?: ProvenancedValue | null) {
  return metricDisplay(metric).text;
}

function MetricValue({ metric }: { metric?: ProvenancedValue | null }) {
  const display = metricDisplay(metric);
  return <span title={display.title}>{display.text}</span>;
}

function statementPeriod(value?: string | null) {
  return value ? value.slice(0, 10) : "N/A";
}

function latestResultText(research?: PortfolioResearchCompany) {
  const result = research?.latestQuarterlyResult;
  if (!result) return research ? "Not publicly available" : "Awaiting research";
  const growth = result.patYoYPercent ? ` PAT ${metricText(result.patYoYPercent)} YoY` : result.revenueYoYPercent ? ` Revenue ${metricText(result.revenueYoYPercent)} YoY` : "";
  return `${statementPeriod(result.period)}${growth}`;
}

function valuationTone(state?: string): "neutral" | "positive" | "negative" | "warning" | "info" {
  if (state === "CHEAP") return "info";
  if (state === "FAIR") return "positive";
  if (state === "EXPENSIVE") return "negative";
  return "neutral";
}

function EvidenceMetric({ label, metric }: { label: string; metric?: ProvenancedValue | null }) {
  return (
    <div className="research-metric">
      <span>{label}</span><strong><MetricValue metric={metric} /></strong>
      {metric?.sourceUrl ? <a href={metric.sourceUrl} target="_blank" rel="noreferrer">{metric.sourceName}</a> : <small>Not publicly available</small>}
      {metric?.calculationBasis ? <small>{metric.calculationBasis}{metric.period ? ` · ${metric.period}` : ""}</small> : null}
    </div>
  );
}

function structuredFact(research: PortfolioResearchCompany | undefined, key: string): ProvenancedValue | undefined {
  return research?.structuredMarket?.facts[key];
}

function FinancialHistoryTable({ periods }: { periods: FinancialResultPeriod[] }) {
  if (!periods.length) return <p>Not publicly available.</p>;
  return <table className="shareholding-table financial-history-table"><thead><tr><th>Period</th><th>Basis</th><th>Revenue / Total Income</th><th>Operating Income</th><th>EBIT</th><th>EBITDA</th><th>PAT</th><th>EPS</th></tr></thead><tbody>
    {periods.map((period) => <tr key={`${period.periodType}:${period.period}:${period.reportingBasis ?? "UNKNOWN"}`}><td>{statementPeriod(period.period)}</td><td>{period.reportingBasis ?? "N/A"}</td><td><MetricValue metric={period.revenue} /></td><td><MetricValue metric={period.operatingIncome} /></td><td><MetricValue metric={period.ebit} /></td><td><MetricValue metric={period.ebitda} /></td><td><MetricValue metric={period.pat} /></td><td><MetricValue metric={period.eps} /></td></tr>)}
  </tbody></table>;
}

function StatementHistoryTable({ periods, labels }: { periods: FinancialStatementPeriod[]; labels: Array<[string[], string]> }) {
  if (!periods.length) return <p>Not publicly available.</p>;
  return <table className="shareholding-table financial-history-table"><thead><tr><th>Period</th><th>Basis</th>{labels.map(([, label]) => <th key={label}>{label}</th>)}</tr></thead><tbody>
    {periods.map((period) => <tr key={`${period.periodType}:${period.period}:${period.reportingBasis ?? "UNKNOWN"}`}><td>{statementPeriod(period.period)}</td><td>{period.reportingBasis ?? "N/A"}</td>{labels.map(([keys]) => <td key={keys.join("/")}><MetricValue metric={keys.map((key) => period.metrics[key]).find(Boolean)} /></td>)}</tr>)}
  </tbody></table>;
}

function DurableEvidenceSection({ title, evidence }: { title: string; evidence?: NonNullable<CatalystScore["categoryEvidence"]>[string] }) {
  const events = evidence?.supportingEvents ?? [];
  return <section><h3>{title}</h3>{events.length ? <><p>{events.length} verified item{events.length === 1 ? "" : "s"}</p>{events.map((event) => <article key={event.eventId}><strong>{statementPeriod(event.eventDate ?? event.publishedAt)} · {event.eventType.replaceAll("_", " ")}</strong>{event.summary ? <p>{event.summary}</p> : null}<a href={event.sourceUrl} target="_blank" rel="noreferrer">Source: {event.sourceType}</a></article>)}</> : <p>N/A — No verified evidence.</p>}</section>;
}

const shareholdingCategoryRows = [
  ["PROMOTER", "Promoters"],
  ["PROMOTER_PLEDGE", "Promoter Pledge*"],
  ["FII_FPI", "FII / FPI"],
  ["DII", "DII"],
  ["PUBLIC_RETAIL", "Retail Public"],
  ["MUTUAL_FUNDS", "Mutual Funds"],
  ["INSURANCE", "Insurance"],
  ["GOVERNMENT", "Government"],
  ["OTHERS", "Others"],
] as const;

const coreShareholdingCategories = new Set(["PROMOTER", "FII_FPI", "DII", "PUBLIC_RETAIL"]);

function shareholdingPeriodLabel(periodEnd: string): string {
  return new Intl.DateTimeFormat("en", { month: "short", year: "numeric", timeZone: "UTC" }).format(new Date(periodEnd));
}

function shareholdingPercentage(value: string | undefined): string {
  if (value === undefined) return "—";
  const percentage = Number(value);
  return Number.isFinite(percentage) ? `${percentage.toFixed(2)}%` : "—";
}

function ShareholdingPatternTable({ snapshots }: { snapshots: NonNullable<PortfolioResearchCompany["shareholdingSnapshots"]> }) {
  const periods = [...snapshots].slice(0, 4).sort((left, right) =>
    new Date(left.periodEnd).getTime() - new Date(right.periodEnd).getTime());
  const rows = shareholdingCategoryRows.filter(([category]) =>
    coreShareholdingCategories.has(category) || periods.some((snapshot) => snapshot.values.some((value) => value.category === category)));
  const newest = periods[periods.length - 1];
  const hasPromoterPledge = rows.some(([category]) => category === "PROMOTER_PLEDGE");

  return <>
    <div className="shareholding-table-frame">
      <table className="shareholding-table">
        <thead><tr><th scope="col">Category</th>{periods.map((snapshot) => <th scope="col" key={snapshot.id}>{shareholdingPeriodLabel(snapshot.periodEnd)}</th>)}</tr></thead>
        <tbody>{rows.map(([category, label]) => <tr key={category}>
          <th scope="row" title={category === "PUBLIC_RETAIL" ? "Resident individual shareholders holding nominal share capital up to ₹2 lakh." : undefined}>{label}</th>
          {periods.map((snapshot) => {
            const value = snapshot.values.find((candidate) => candidate.category === category);
            return <td key={snapshot.id}>{shareholdingPercentage(value?.percentage)}</td>;
          })}
        </tr>)}</tbody>
      </table>
    </div>
    {hasPromoterPledge ? <p className="shareholding-note">* % of promoter holding</p> : null}
    {newest?.sourceUrl ? <p className="shareholding-source">Source: <a href={newest.sourceUrl} target="_blank" rel="noreferrer">{newest.sourceProvider} Shareholding XBRL</a></p> : null}
  </>;
}

function isFinancialCompany(research: PortfolioResearchCompany | undefined): boolean {
  const identity = `${structuredFact(research, "sector")?.value ?? ""} ${structuredFact(research, "industry")?.value ?? ""}`.toLowerCase();
  return /financial|bank|insurance|credit|capital market/.test(identity);
}

function StructuredMetric({ label, research, fact }: { label: string; research?: PortfolioResearchCompany; fact: string }) {
  return <EvidenceMetric label={label} metric={structuredFact(research, fact)} />;
}

function StockResearchDrawer({ position, watchlistItem, research, onUpdateDisplayName, onClose }: { position?: PortfolioPosition; watchlistItem?: WatchlistResearchInstrument; research?: PortfolioResearchCompany; onUpdateDisplayName?: (position: PortfolioPosition, customDisplayName: string | null) => Promise<void>; onClose: () => void }) {
  const instrument = position?.instrument;
  const displayName = position?.displayName ?? research?.companyName ?? "Watchlist instrument";
  const ticker = instrument?.ticker ?? research?.ticker ?? "Ticker N/A";
  const exchange = instrument?.exchange ?? research?.exchange ?? "Exchange N/A";
  const isin = instrument?.isin ?? research?.isin;
  const country = instrument?.country ?? watchlistItem?.country ?? undefined;
  const currency = research?.structuredMarket?.resolution.currency ?? instrument?.tradingCurrency ?? watchlistItem?.currency ?? undefined;
  const assetType = instrument?.assetType ?? research?.assetType ?? watchlistItem?.assetType ?? "EQUITY";
  const result = research?.latestQuarterlyResult;
  const financial = isFinancialCompany(research);
  const isEtf = assetType === "ETF" || research?.structuredMarket?.resolution.quoteType === "ETF";
  const market = research?.structuredMarket;
  const latestPriceFact = structuredFact(research, "latestPrice");
  const researchCurrentPrice =
    latestPriceFact?.value != null
      ? Number(latestPriceFact.value)
      : research?.currentPrice == null
        ? null
        : Number(research.currentPrice);
  const currentPrice = researchCurrentPrice != null && Number.isFinite(researchCurrentPrice) && researchCurrentPrice > 0
    ? formatMoney(researchCurrentPrice, currency)
    : position?.currentPrice && position.currentPrice.amount > 0
      ? formatBackendMoney(position.currentPrice)
      : "N/A";
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(position?.customDisplayName ?? displayName);
  const [saving, setSaving] = useState(false);
  const [renameError, setRenameError] = useState<string | null>(null);
  async function saveName() {
    if (!name.trim()) { setRenameError("Enter a display name."); return; }
    setSaving(true); setRenameError(null);
    try { if (onUpdateDisplayName && position) await onUpdateDisplayName(position, name.trim()); setEditing(false); }
    catch (error) { setRenameError(getApiFailure(error).message); }
    finally { setSaving(false); }
  }
  return (
    <div className="research-drawer-backdrop" role="presentation" onClick={onClose}>
      <aside className="research-drawer" role="dialog" aria-modal="true" aria-label={`${displayName} research details`} onClick={(event) => event.stopPropagation()}>
        <div className="panel-header"><div><p className="eyebrow">Stock research</p><h2>{displayName}</h2><p>{ticker} · {exchange} · {isin ?? "ISIN N/A"}</p></div><button className="icon-button" type="button" aria-label="Close stock research" onClick={onClose}><X size={20} /></button></div>
        <section><h3>Overview</h3>{editing && position ? <div className="holding-name-editor"><label>Display name<input maxLength={160} value={name} onChange={(event) => setName(event.target.value)} /></label><Button disabled={saving} onClick={() => void saveName()}>Save</Button><Button variant="secondary" disabled={saving} onClick={() => { setEditing(false); setName(position.customDisplayName ?? position.displayName); setRenameError(null); }}>Cancel</Button>{renameError ? <small role="alert">{renameError}</small> : null}</div> : <p><strong>{displayName}</strong> {position?.sourceType === "MANUAL_CSV_IMPORT" && onUpdateDisplayName ? <button className="text-action" type="button" onClick={() => setEditing(true)}>Edit</button> : null}</p>}<p>{ticker} · {isin ?? "ISIN N/A"} · {market?.resolution.exchange ?? exchange} · {country ?? "Country N/A"} · {currency ?? "Currency N/A"} · {market?.resolution.quoteType ?? assetType}</p><p>Provider ticker: {market?.resolution.providerTicker ?? "N/A"} · Provider identity: {market?.resolution.companyName ?? "N/A"}</p><p>Sector: {metricText(structuredFact(research, "sector"))} · Industry: {metricText(structuredFact(research, "industry"))}</p><p>{position ? <>Broker/source: {brokerDisplayName(position.brokerType)} · {position.sourceType}</> : <>Watchlist status: Not held</>}</p></section>
        {position ? <section><h3>Position</h3><div className="research-metric-grid"><div className="research-metric"><span>Quantity</span><strong>{position.quantity}</strong></div><div className="research-metric"><span>Average Cost</span><strong>{formatBackendMoney(position.averageCost)}</strong></div><div className="research-metric"><span>Cost Basis</span><strong>{formatBackendMoney(position.costBasis)}</strong></div><div className="research-metric"><span>Imported Price</span><strong>{formatBackendMoney(position.importedPrice)}</strong></div><div className="research-metric"><span>Latest Market Price</span><strong>{formatBackendMoney(position.currentPrice)}</strong></div><div className="research-metric"><span>Market Value</span><strong>{formatBackendMoney(position.marketValue)}</strong></div><div className="research-metric"><span>Unrealized P/L</span><strong>{formatBackendMoney(position.unrealizedProfitLoss)}</strong></div></div></section> : <section><h3>Watchlist status</h3><p>Public company research</p><div className="research-metric-grid">{watchlistItem?.sourcePeriod ? <div className="research-metric"><span>Market return ({watchlistItem.sourcePeriod})</span><strong className={`${performanceRowTone(watchlistItem.sourcePerformancePct)}-text`}>{formatSignedPerformancePct(watchlistItem.sourcePerformancePct)}</strong></div> : null}</div></section>}
        <section><h3>Market Data</h3><div className="research-metric-grid"><div className="research-metric"><span>Latest Price</span><strong>{currentPrice}</strong></div><EvidenceMetric label="Previous close" metric={structuredFact(research, "previousClose")} /><EvidenceMetric label="Bid" metric={structuredFact(research, "bid")} /><EvidenceMetric label="Ask" metric={structuredFact(research, "ask")} /><EvidenceMetric label="Volume" metric={structuredFact(research, "volume")} /><EvidenceMetric label="10-day avg volume" metric={structuredFact(research, "averageVolume10Day")} /><EvidenceMetric label="3-month avg volume" metric={structuredFact(research, "averageVolume")} /><EvidenceMetric label="52-week low" metric={structuredFact(research, "fiftyTwoWeekLow")} /><EvidenceMetric label="52-week high" metric={structuredFact(research, "fiftyTwoWeekHigh")} /><div className="research-metric"><span>Price freshness</span><strong>{research?.priceFreshness ?? "UNKNOWN"}</strong></div><div className="research-metric"><span>Market As Of</span><strong>{market?.marketAsOf ? new Date(market.marketAsOf).toLocaleString() : position?.quote?.sourceTimestamp ? new Date(position.quote.sourceTimestamp).toLocaleString() : "N/A"}</strong></div><div className="research-metric"><span>Retrieved At</span><strong>{market?.retrievedAt ? new Date(market.retrievedAt).toLocaleString() : position?.quote?.receivedAt ? new Date(position.quote.receivedAt).toLocaleString() : "N/A"}</strong></div><div className="research-metric"><span>Provider</span><strong>{market?.sourceName ?? position?.quote?.source ?? "N/A"}</strong></div></div></section>
        <section><h3>Valuation</h3><div className="research-metric-grid"><StructuredMetric label="Market cap" research={research} fact="marketCap" /><StructuredMetric label="Enterprise value" research={research} fact="enterpriseValue" /><StructuredMetric label="Trailing P/E" research={research} fact="trailingPE" /><StructuredMetric label="Forward P/E" research={research} fact="forwardPE" /><StructuredMetric label="P/B" research={research} fact="priceToBook" /><StructuredMetric label="P/S" research={research} fact="priceToSales" /><StructuredMetric label="PEG" research={research} fact="pegRatio" />{!financial && !isEtf ? <><StructuredMetric label="EV/revenue" research={research} fact="evToRevenue" /><StructuredMetric label="EV/EBITDA" research={research} fact="evToEbitda" /></> : null}<StructuredMetric label="Trailing EPS" research={research} fact="trailingEps" /><StructuredMetric label="Forward EPS" research={research} fact="forwardEps" /></div><p><Badge tone={valuationTone(research?.valuation.state)}>{research?.valuation.state ?? "UNKNOWN"}</Badge> {research?.valuation.reason ?? "Awaiting contextual public research."}</p></section>
        {!isEtf ? <section><h3>Quality / Fundamentals</h3><div className="research-metric-grid"><EvidenceMetric label="ROE" metric={structuredFact(research, "roe") ?? research?.valuation.roe} /><StructuredMetric label="ROA" research={research} fact="roa" />{!financial ? <EvidenceMetric label="ROCE" metric={structuredFact(research, "roce") ?? research?.valuation.roce} /> : null}<StructuredMetric label="Operating margin" research={research} fact="operatingMargin" /><StructuredMetric label="Net margin" research={research} fact="profitMargin" /><StructuredMetric label="Revenue growth" research={research} fact="revenueGrowth" /><StructuredMetric label="Earnings growth" research={research} fact="earningsGrowth" /><StructuredMetric label="Cash" research={research} fact="totalCash" /><StructuredMetric label="Debt" research={research} fact="totalDebt" /><StructuredMetric label="Debt / equity" research={research} fact="debtToEquity" /><StructuredMetric label="Free cash flow" research={research} fact="freeCashFlow" /><StructuredMetric label="Operating cash flow" research={research} fact="operatingCashFlow" /></div></section> : null}
        <section><h3>Analyst View</h3><p>External public analyst consensus; not an application recommendation.</p><div className="research-metric-grid"><div className="research-metric"><span>Current Price</span><strong>{currentPrice}</strong></div><StructuredMetric label="Target low" research={research} fact="publicAnalystTargetLowPrice" /><StructuredMetric label="Target median" research={research} fact="publicAnalystTargetMedianPrice" /><StructuredMetric label="Target mean" research={research} fact="publicAnalystTargetMeanPrice" /><StructuredMetric label="Target high" research={research} fact="publicAnalystTargetHighPrice" /><StructuredMetric label="Number of analysts" research={research} fact="publicAnalystCount" /><StructuredMetric label="Consensus" research={research} fact="publicAnalystConsensus" /><StructuredMetric label="Consensus score" research={research} fact="publicAnalystRecommendationMean" /></div></section>
        <section><h3>Latest Quarterly Result</h3>{result ? <><p>{result.documentTitle ?? "Quarterly financial result"} · {statementPeriod(result.period)} · {result.reportingBasis ?? "Reporting basis N/A"} · {result.resultDate ? statementPeriod(result.resultDate) : "Result date N/A"}</p><div className="research-metric-grid">{financial ? <><EvidenceMetric label="Total income" metric={result.revenue} /><EvidenceMetric label="PAT / net profit" metric={result.pat} /><EvidenceMetric label="EPS" metric={result.eps} /><EvidenceMetric label="NIM" metric={result.nim} /><EvidenceMetric label="ROA" metric={result.roa} /><EvidenceMetric label="ROE" metric={result.roe} /><EvidenceMetric label="Gross NPA" metric={result.grossNpa} /><EvidenceMetric label="Net NPA" metric={result.netNpa} /><EvidenceMetric label="Deposits" metric={result.deposits} /><EvidenceMetric label="Advances" metric={result.advances} /><EvidenceMetric label="Capital adequacy" metric={result.capitalAdequacy} /><EvidenceMetric label="Credit cost" metric={result.creditCost} /></> : <><EvidenceMetric label="Revenue" metric={result.revenue} /><EvidenceMetric label="Revenue YoY" metric={result.revenueYoYPercent} /><EvidenceMetric label="EBITDA / operating profit" metric={result.ebitda} /><EvidenceMetric label="EBITDA / operating margin" metric={result.ebitdaMargin} /><EvidenceMetric label="PAT / net profit" metric={result.pat} /><EvidenceMetric label="PAT YoY" metric={result.patYoYPercent} /><EvidenceMetric label="EPS" metric={result.eps} /><EvidenceMetric label="Debt / borrowings" metric={result.debtOrBorrowings} /></>}</div>{result.yoySummary ? <p>{result.yoySummary}</p> : null}<p>Source: {filingSourceLabel(result.sourceName)} · Published {result.publishedAt ? new Date(result.publishedAt).toLocaleDateString() : "N/A"} · Retrieved {new Date(result.retrievedAt).toLocaleString()}</p><a href={result.sourceUrl} target="_blank" rel="noreferrer">View {filingSourceLabel(result.sourceName)} Filing ↗</a></> : <p>{research?.quarterlyResultStatus === "PDF_SCANNED_OCR_REQUIRED" ? "PDF scanned; OCR required." : "Not publicly available."}</p>}</section>
        {!isEtf ? <section><h3>Financial History</h3><h4>Quarterly</h4><FinancialHistoryTable periods={(research?.financialResultHistory ?? []).filter((period) => period.periodType === "QUARTERLY").slice(0, 4)} /><h4>Annual</h4><FinancialHistoryTable periods={(research?.financialResultHistory ?? []).filter((period) => period.periodType === "ANNUAL")} /></section> : null}
        {(country === "IN" || exchange === "NSE" || exchange === "XNSE") ? <section><h3>Shareholding Pattern</h3>{research?.shareholdingSnapshots?.length ? <ShareholdingPatternTable snapshots={research.shareholdingSnapshots} /> : research?.shareholdingChanges?.length ? research.shareholdingChanges.map((change) => <p className={Number(change.changePercentagePoints) >= 0.1 ? `ownership-increase ownership-${change.category.toLowerCase()}` : undefined} key={change.category}><strong>{change.category.replaceAll("_", "/")}</strong>: {metricText(change.current)} ({Number(change.changePercentagePoints) >= 0 ? "+" : ""}{change.changePercentagePoints} pp), {change.previousPeriod} → {change.currentPeriod} · <a href={change.current.sourceUrl} target="_blank" rel="noreferrer">Source</a></p>) : <p>Unavailable.</p>}</section> : null}
        {!isEtf ? <section><h3>Debt & Balance Sheet</h3><StatementHistoryTable periods={research?.balanceSheetHistory ?? []} labels={[[["total_assets"], "Total Assets"], [["total_liabilities"], "Total Liabilities"], [["total_equity", "equity"], "Equity / Net Worth"], [["cash_and_cash_equivalents", "cash_and_equivalents"], "Cash / Cash Equivalents"], [["total_debt", "debt_or_borrowings"], "Total Debt"], [["current_assets"], "Current Assets"], [["current_liabilities"], "Current Liabilities"]]} /></section> : null}
        {!isEtf ? <section><h3>Cash Flow</h3><StatementHistoryTable periods={research?.cashFlowHistory ?? []} labels={[[["operating_cash_flow", "cash_flow_from_operating_activities"], "Operating Cash Flow"], [["investing_cash_flow", "cash_flow_from_investing_activities"], "Investing Cash Flow"], [["financing_cash_flow", "cash_flow_from_financing_activities"], "Financing Cash Flow"], [["capex"], "Capital Expenditure / Capex"]]} /></section> : null}
        <section><h3>Current Quarter Catalysts</h3>{research?.currentQuarterCatalysts?.length ? research.currentQuarterCatalysts.map((event) => <article key={event.eventId}><strong>{event.eventDate ?? event.publishedAt?.slice(0, 10) ?? "Date unavailable"} · {event.eventType.replaceAll("_", " ")}</strong><p>{event.summary}</p><a href={event.sourceUrl} target="_blank" rel="noreferrer">Source: {event.sourceType}</a></article>) : <p>No verified current-quarter catalyst.</p>}</section>
        <DurableEvidenceSection title="Orders & Backlog" evidence={research?.durableCategoryEvidence?.ORDERS_BACKLOG} />
        <DurableEvidenceSection title="CAPEX & Capacity" evidence={research?.durableCategoryEvidence?.CAPEX} />
        <DurableEvidenceSection title="Customers" evidence={research?.durableCategoryEvidence?.CLIENTS} />
        <DurableEvidenceSection title="Catalysts" evidence={research?.durableCategoryEvidence?.CATALYSTS} />
        <section><h3>News</h3>{market?.news?.length ? market.news.map((article) => <article key={article.url}><strong>{article.headline}</strong><p>{article.publisher} · {article.publishedAt ? new Date(article.publishedAt).toLocaleString() : "Date unavailable"}</p><a href={article.url} target="_blank" rel="noreferrer">Open source</a></article>) : <p>No public provider news available.</p>}</section>
        <section><h3>Research & Evidence</h3><p>Status: {research?.status?.replaceAll("_", " ") ?? "Awaiting research"}; {research?.sourceDiversity.domainsFound ?? 0} unique domains, {research?.sourceDiversity.exchangeSources ?? 0} exchange sources, {research?.sourceDiversity.companySources ?? 0} company sources, {research?.sourceDiversity.secondarySources ?? 0} secondary sources.</p>{research?.latestEvent ? <a href={research.latestEvent.sourceUrl} target="_blank" rel="noreferrer">Latest evidence source</a> : null}</section>
      </aside>
    </div>
  );
}

function AllocationCharts({ summary }: { summary: PortfolioSummary }) {
  return (
    <div className="allocation-grid">
      <AllocationGroup title="Country" values={summary.allocation.country} />
      <AllocationGroup title="Sector" values={summary.allocation.sector} />
      <AllocationGroup title="Currency" values={summary.allocation.currency} />
      <AllocationGroup title="Asset type" values={summary.allocation.assetType} />
      <AllocationGroup title="Broker" values={summary.allocation.broker} />
    </div>
  );
}

function AllocationGroup({ title, values }: { title: string; values: Record<string, number> }) {
  const entries = Object.entries(values).sort(([, a], [, b]) => b - a).slice(0, 5);

  return (
    <section className="allocation-group" aria-label={`${title} allocation`}>
      <h3>{title}</h3>
      {entries.length === 0 ? (
        <p>No allocation data.</p>
      ) : (
        entries.map(([label, value]) => (
          <div className="allocation-row" key={label}>
            <div>
              <span>{label}</span>
              <strong>{formatPercent(value)}</strong>
            </div>
            <div className="bar-track" aria-hidden="true">
              <span style={{ width: `${Math.min(value, 100)}%` }} />
            </div>
          </div>
        ))
      )}
    </section>
  );
}

function BrokerView({
  providers,
  connections,
  portfolios,
  errors,
  loading,
  authenticatingBroker,
  authenticationTimedOut,
  onContinueAuthentication,
  onCancelAuthentication,
  onConnectBroker,
  onAuthenticate,
  onConfigureIndividual,
  onImportComplete,
  onDisconnect
}: {
  providers: BrokerProviderInfo[];
  connections: BrokerConnection[];
  portfolios: PortfolioListItem[];
  errors: Record<string, string>;
  loading: boolean;
  authenticatingBroker: string | null;
  authenticationTimedOut: boolean;
  onContinueAuthentication: () => void;
  onCancelAuthentication: () => void;
  onConnectBroker: (provider: BrokerProviderInfo) => Promise<void>;
  onAuthenticate: (connectionId: string, provider: BrokerProviderInfo) => Promise<void>;
  onConfigureIndividual: (provider: BrokerProviderInfo, clientKey: string, clientSecret: string) => Promise<void>;
  onImportComplete: (portfolioId: string) => Promise<void>;
  onDisconnect: (connectionId: string) => Promise<void>;
}) {
  const [developerProvider, setDeveloperProvider] = useState<string | null>(null);
  const [developerKey, setDeveloperKey] = useState("");
  const [developerSecret, setDeveloperSecret] = useState("");
  const [importProvider, setImportProvider] = useState<string | null>(null);
  const [importFile, setImportFile] = useState<File | null>(null);
  const [importName, setImportName] = useState("");
  const [importPreview, setImportPreview] = useState<PortfolioImportPreview | null>(null);
  const [importBusy, setImportBusy] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [importSuccess, setImportSuccess] = useState(false);
  const visibleProviders = providers.filter((provider) => provider.brokerType !== "MOCK");
  if (visibleProviders.length === 0) return <EmptyState title="No brokers available" message="Broker discovery is unavailable." />;
  return (
    <div>
    <div className="broker-grid">
      {visibleProviders.map((provider) => {
        const activeConnection = connections.find((connection) => connection.brokerType === provider.brokerType);
        const linkedPortfolio = activeConnection
          ? portfolios.find((portfolio) => portfolio.brokerConnectionId === activeConnection.connectionId)
          : undefined;
        const authenticationRequired = activeConnection?.status === "AUTHENTICATION_REQUIRED"
          || activeConnection?.providerStatus === "AUTHENTICATION_REQUIRED";
        const connected = activeConnection?.status === "CONNECTED"
          && (!activeConnection.providerStatus || activeConnection.providerStatus === "CONNECTED");
        const recoverable = Boolean(activeConnection) && !connected;
        const connectionError = activeConnection?.status === "ERROR"
          || activeConnection?.providerStatus === "ERROR"
          || activeConnection?.lastErrorCode === "BROKER_UNAVAILABLE";
        const partnerUnavailable = provider.consumerAuthMode === "PARTNER_UNAVAILABLE";
        const developerIndividualMode = provider.individualApiSupported && provider.advancedIndividualMode;
        const meaningfulPersistedLinkage = Boolean(linkedPortfolio || activeConnection?.lastSuccessfulSyncAt || connected);
        const authenticationPending = authenticatingBroker === provider.brokerType;
        const statusLabel = partnerUnavailable ? "Direct connection unavailable"
          : !provider.connectable ? "Unavailable"
          : authenticationPending && authenticationTimedOut ? "Authentication pending"
          : authenticationPending ? "Waiting for IBKR authentication…"
          : authenticationRequired ? "Authentication required"
          : connected ? "Connected"
          : connectionError ? "Connection error"
          : activeConnection ? "Not connected" : "Not connected";
        const message = partnerUnavailable
          ? "Direct customer account connection is not available yet."
          : !provider.connectable
          ? provider.unavailableReason ?? "Connection not available yet."
          : authenticationPending && authenticationTimedOut
            ? "IBKR authentication is still pending."
          : authenticationPending
            ? "Waiting for IBKR authentication…"
          : authenticationRequired
            ? "Authentication is required to refresh holdings. Your saved portfolio remains available."
            : connected ? "Your broker connection is ready. Sync holdings from the linked portfolio."
            : connectionError ? "Broker authentication is temporarily unavailable. Your saved portfolio remains available."
            : "Connect this broker to import read-only holdings.";
        return (
          <Card className="broker-card" as="article" key={provider.brokerType}>
          <div className="broker-icon">
            <Building2 size={22} aria-hidden="true" />
          </div>
          <div className="broker-card-content">
            <div className="panel-header compact">
              <h2>{provider.displayName}</h2>
              <Badge tone={connected ? "positive" : "warning"}>{statusLabel}</Badge>
              <Badge tone="positive">Read-only</Badge>
            </div>
            <p className="broker-message">{message}</p>
            {!partnerUnavailable && errors[provider.brokerType] ? <div className="broker-inline-error" role="alert">{errors[provider.brokerType]}</div> : null}
            {linkedPortfolio ? <p className="broker-linked-portfolio">Portfolio: <strong>{linkedPortfolio.name}</strong></p> : null}
            <div className="broker-actions">
            {!authenticationPending && !partnerUnavailable && !developerIndividualMode && !activeConnection && provider.connectable ? (
              <Button onClick={() => onConnectBroker(provider)} disabled={loading}>
                {loading ? "Connecting..." : provider.brokerType === "IBKR" ? "Connect / Re-authenticate" : "Connect"}
              </Button>
            ) : null}
            {!authenticationPending && !partnerUnavailable && !developerIndividualMode && activeConnection && recoverable ? (
              <Button onClick={() => onAuthenticate(activeConnection.connectionId, provider)} disabled={loading}>
                {loading ? "Opening sign-in..." : activeConnection.brokerType === "IBKR" ? "Connect / Re-authenticate" : "Connect"}
              </Button>
            ) : null}
            {authenticationPending && !authenticationTimedOut ? (
              <Button variant="ghost" onClick={onCancelAuthentication}>Cancel authentication</Button>
            ) : null}
            {authenticationPending && authenticationTimedOut ? (
              <>
                <Button variant="secondary" onClick={onContinueAuthentication}>Continue checking</Button>
                {activeConnection ? <Button onClick={() => {
                  onCancelAuthentication();
                  void onAuthenticate(activeConnection.connectionId, provider);
                }}>Try again</Button> : null}
                <Button variant="ghost" onClick={onCancelAuthentication}>Cancel</Button>
              </>
            ) : null}
            {activeConnection && connected ? <Button variant="secondary" disabled>Manage</Button> : null}
            {activeConnection && (!partnerUnavailable || meaningfulPersistedLinkage) ? (
              <Button variant="ghost" onClick={() => onDisconnect(activeConnection.connectionId)} disabled={loading}>Disconnect</Button>
            ) : null}
            {developerIndividualMode ? (
              <Button variant="secondary" onClick={() => setDeveloperProvider(
                developerProvider === provider.brokerType ? null : provider.brokerType
              )} disabled={loading}>Configure DEV API</Button>
            ) : null}
            {provider.manualImportSupported ? (
              <Button variant="secondary" onClick={() => {
                setImportProvider(importProvider === provider.brokerType ? null : provider.brokerType);
                setImportFile(null); setImportPreview(null); setImportError(null); setImportName("");
              }} disabled={loading}>Import portfolio</Button>
            ) : null}
            </div>
            {developerIndividualMode && developerProvider === provider.brokerType ? (
              <form className="broker-developer-form" onSubmit={async (event) => {
                event.preventDefault();
                await onConfigureIndividual(provider, developerKey, developerSecret);
                setDeveloperKey("");
                setDeveloperSecret("");
                setDeveloperProvider(null);
              }}>
                <p>Developer/test individual API setup. Credentials are stored write-only.</p>
                <label>API key<input type="password" autoComplete="off" value={developerKey} onChange={(event) => setDeveloperKey(event.target.value)} required /></label>
                <label>API secret<input type="password" autoComplete="new-password" value={developerSecret} onChange={(event) => setDeveloperSecret(event.target.value)} required /></label>
                <Button type="submit" disabled={loading}>Save DEV credentials</Button>
              </form>
            ) : null}
            {provider.manualImportSupported && importProvider === provider.brokerType ? (
              <div className="import-modal-backdrop" role="presentation">
              <section className="import-modal" role="dialog" aria-modal="true" aria-label={`Import ${provider.displayName} Portfolio`}>
                <header className="import-modal-header"><div><span>Step {importPreview ? "2" : "1"} of 2</span><h2>Import {provider.displayName} Portfolio</h2></div><button className="icon-button" aria-label="Close import" onClick={() => setImportProvider(null)}><X size={20} /></button></header>
                {provider.manualImportParserStatus !== "SUPPORTED" ? (
                  <p role="status">This broker portfolio file format is not yet supported. No columns have been guessed.</p>
                ) : (
                  <>
                    <p className="import-section-label">Select statement</p>
                    <label className="import-dropzone" onDragOver={(event) => event.preventDefault()} onDrop={(event) => {
                      event.preventDefault(); const selected = event.dataTransfer.files[0] ?? null;
                      if (!selected || !selected.name.toLowerCase().endsWith(".csv") || selected.size === 0 || selected.size > 5 * 1024 * 1024) {
                        setImportFile(null); setImportError("Choose a non-empty CSV file no larger than 5 MB."); return;
                      }
                      setImportFile(selected); setImportPreview(null); setImportError(null);
                    }}><UploadCloud size={32} /><strong>Drag &amp; drop your CSV here</strong><span>or choose an ICICI/HDFC equity portfolio CSV</span><span className="button button-secondary">Choose CSV file</span><input type="file" accept=".csv,text/csv" onChange={(event) => {
                      const selected = event.target.files?.[0] ?? null;
                      if (selected && (!selected.name.toLowerCase().endsWith(".csv") || selected.size === 0 || selected.size > 5 * 1024 * 1024)) {
                        setImportFile(null); setImportError("Choose a non-empty CSV file no larger than 5 MB."); return;
                      }
                      setImportFile(selected); setImportPreview(null); setImportError(null);
                    }} /></label>
                    {importFile ? <p className="import-file"><CheckCircle2 size={18} /> <strong>{importFile.name.replace(/[\\/]/g, "")}</strong></p> : null}
                    {importFile && !detectedImportAccount(importFile.name, provider.brokerType) ? <label>Portfolio name<input value={importName} onChange={(event) => setImportName(event.target.value)} /></label> : importFile ? <p>Account detected: <strong>{detectedImportAccount(importFile.name, provider.brokerType)}</strong></p> : null}
                    <Button variant="secondary" disabled={!importFile || importBusy} onClick={async () => {
                      if (!importFile) return;
                      setImportBusy(true); setImportError(null);
                      try { setImportPreview(await portfolioApi.previewImport(provider.brokerType, importFile, importName)); }
                      catch (error) { setImportError(getApiFailure(error).message); }
                      finally { setImportBusy(false); }
                    }}>{importBusy ? "Reading..." : "Preview portfolio"}</Button>
                    {importError ? <p className="import-error" role="alert">{importError}</p> : null}
                    {importPreview ? <div className="import-preview">
                      <p><strong>{importPreview.updatesExistingPortfolio ? `Update existing portfolio ${importPreview.portfolioName}` : `Portfolio: ${importPreview.portfolioName}`}</strong></p>
                      <p>Broker: {provider.displayName} · Currency: {importPreview.currency}</p>
                      {importPreview.statementAt ? <p>Statement date/time: {new Date(importPreview.statementAt).toLocaleString()}</p> : null}
                      <p>Rows detected: {importPreview.rowsDetected} · Valid holdings: {importPreview.validHoldings} · Rejected rows: {importPreview.rejectedRows}</p>
                      <p>Columns mapped: {importPreview.columnsMapped.join(", ")}</p>
                      <div className="import-table-frame"><table className="import-table"><thead><tr><th>Company</th><th>Symbol</th><th>Quantity</th><th>Avg cost</th><th>Statement price</th><th>Market value</th><th>P&amp;L</th></tr></thead><tbody>{importPreview.holdings.map((holding, index) => <tr key={`${holding.symbol}-${index}`}><td>{holding.companyName}</td><td>{holding.symbol}</td><td>{holding.quantity}</td><td>{formatMoney(holding.averageCost, importPreview.currency)}</td><td>{formatMoney(holding.importedPrice, importPreview.currency)}</td><td>{formatMoney(holding.marketValue, importPreview.currency)}</td><td>{formatMoney(holding.unrealizedPnl, importPreview.currency)}</td></tr>)}</tbody></table></div>
                      {importPreview.issues.length ? <ul>{importPreview.issues.map((issue) => <li key={issue}>{issue}</li>)}</ul> : null}
                      <Button disabled={importBusy || importPreview.validHoldings === 0} onClick={async () => {
                        if (!importFile) return;
                        setImportBusy(true); setImportError(null);
                        try {
                          const result = await portfolioApi.confirmImport(provider.brokerType, importFile, importName);
                          await onImportComplete(result.portfolioId);
                          setImportSuccess(true);
                          window.setTimeout(() => setImportSuccess(false), 3500);
                          setImportProvider(null); setImportFile(null); setImportPreview(null); setImportName("");
                        } catch (error) { setImportError(getApiFailure(error).message); }
                        finally { setImportBusy(false); }
                      }}>{importBusy ? "Importing..." : importPreview.updatesExistingPortfolio ? "Update Portfolio" : "Confirm Import"}</Button>
                    </div> : null}
                  </>
                )}
              </section></div>
            ) : null}
          </div>
          </Card>
        );
      })}
    </div>{importSuccess ? <div className="success-toast" role="status"><CheckCircle2 size={18} /> Portfolio imported successfully</div> : null}</div>
  );
}

function detectedImportAccount(filename: string, brokerType: string): string | null {
  const clean = filename.replace(/ \(\d+\)(?=\.csv$)/i, "");
  const match = brokerType === "ICICI_DIRECT" ? clean.match(/^(\d+)_PortFolioEqtSummary\.csv$/i)
    : brokerType === "HDFC_SECURITIES" ? clean.match(/^Invest Right Equity Portfolio_(\d+)\.csv$/i) : null;
  return match?.[1] ?? null;
}

function legacyCompositeCompanyName(value?: string | null): string | null {
  const segments = (value ?? "").split("/").map((segment) => segment.trim());
  return segments.length >= 3 && segments[2] ? segments[2] : null;
}

function researchCompanyName(position: PortfolioPosition, resolved?: PortfolioResearchCompany): string {
  const instrument = position.instrument;
  const customName = position.customDisplayName?.trim();
  if (customName) return customName;
  const ticker = (resolved?.ticker ?? instrument.ticker).trim();
  const structuredName = instrument.companyName?.trim();
  if (structuredName && structuredName.toUpperCase() !== ticker.toUpperCase()
      && !legacyCompositeCompanyName(structuredName)) return structuredName;
  const trustedName = instrument.canonicalName?.trim() || resolved?.companyName?.trim();
  if (trustedName && trustedName.toUpperCase() !== ticker.toUpperCase()
      && !legacyCompositeCompanyName(trustedName)) return trustedName;
  for (const candidate of [position.displayName, structuredName, trustedName, instrument.brokerDescription]) {
    const parsed = legacyCompositeCompanyName(candidate);
    if (parsed) return parsed;
  }
  return ticker || instrument.brokerSymbol?.trim() || "Unknown company";
}

function ResearchView({
  positions,
  portfolioResearchSummary,
  portfolioResearchLoading,
  portfolioResearchError,
  researchContext,
  watchlistResearch,
  watchlistResearchLoading,
  watchlistResearchError,
  selectedResearchInstrumentId,
  onSelectInstrument,
  summary,
  loading,
  eventType,
  impact,
  onEventType,
  onImpact,
  onRefresh,
  searchPresentation,
  onSearchSelect,
  searchSelectedMatch,
  searchWatchlistSaved,
  searchWatchlistBusy,
  searchWatchlistError,
  onToggleSearchWatchlist
}: {
  positions: PortfolioPosition[];
  portfolioResearchSummary: PortfolioResearchSummary | null;
  portfolioResearchLoading: boolean;
  portfolioResearchError: string | null;
  researchContext: ResearchContext;
  watchlistResearch: WatchlistResearchPresentation | null;
  watchlistResearchLoading: boolean;
  watchlistResearchError: string | null;
  selectedResearchInstrumentId: string;
  onSelectInstrument: (value: string) => void;
  summary: ResearchSummary | null;
  loading: boolean;
  eventType: string;
  impact: string;
  onEventType: (value: string) => void;
  onImpact: (value: string) => void;
  onRefresh: () => Promise<void>;
  searchPresentation: PortfolioResearchCompany | null;
  onSearchSelect: (match: ResearchInstrumentMatch) => void;
  searchSelectedMatch: ResearchInstrumentMatch | null;
  searchWatchlistSaved: boolean;
  searchWatchlistBusy: boolean;
  searchWatchlistError: string | null;
  onToggleSearchWatchlist: () => void;
}) {
  const [openSection, setOpenSection] = useState<ResearchSectionId | null>(null);
  const [openEventId, setOpenEventId] = useState<string | null>(null);
  const [researchDetail, setResearchDetail] = useState<{
    position?: PortfolioPosition;
    watchlistInstrumentId?: string;
    researchInstrumentId: string;
  } | null>(null);
  useEffect(() => {
    setResearchDetail(null);
  }, [researchContext.kind, selectedResearchInstrumentId]);
  const filteredEvents = (summary?.recentEvents ?? []).filter((event) => {
    return (!eventType || event.eventType === eventType) && (!impact || event.impact === impact);
  });
  const eventTypes = [...new Set((summary?.recentEvents ?? []).map((event) => event.eventType))].sort();
  const impacts = [...new Set((summary?.recentEvents ?? []).map((event) => event.impact))].sort();
  const documentsById = new Map((summary?.documents ?? []).map((document) => [document.documentId, document]));
  const researchSections = summary ? getResearchSections(summary, filteredEvents, eventType, impact) : [];
  const selectedPortfolioResearchCompany =
    portfolioResearchSummary?.companies.find((company) => company.instrumentId === selectedResearchInstrumentId) ?? null;
  const selectedWatchlistItem = watchlistResearch?.instruments.find(
    (item) => item.globalInstrumentId === selectedResearchInstrumentId
  ) ?? null;
  const selectedContextResearchCompany = researchContext.kind === "WATCHLIST"
    ? selectedWatchlistItem?.company ?? null
    : researchContext.kind === "SEARCH" ? searchPresentation : selectedPortfolioResearchCompany;
  const detailWatchlistItem = researchDetail?.watchlistInstrumentId
    ? watchlistResearch?.instruments.find((item) => item.globalInstrumentId === researchDetail.watchlistInstrumentId)
    : undefined;
  const detailResearch = researchDetail
    ? (detailWatchlistItem?.company
      ?? portfolioResearchSummary?.companies.find((company) => company.instrumentId === researchDetail.researchInstrumentId)
      ?? (researchContext.kind === "SEARCH" ? searchPresentation ?? null : null))
    : undefined;
  const researchOptions = useMemo(() => {
    if (researchContext.kind === "WATCHLIST") {
      return (watchlistResearch?.instruments ?? []).map((item) => ({
        value: item.globalInstrumentId,
        globalInstrumentId: item.globalInstrumentId,
        companyName: item.company.companyName,
        ticker: item.company.ticker,
        exchange: item.company.exchange,
        status: item.company.status,
        assetType: item.company.assetType,
        loadable: isResearchSummaryLoadable(item.company.status),
        held: false,
        quantity: 0,
      }));
    }
    const resolvedByHoldingKey = new Map<string, PortfolioResearchCompany>();
    const tickerCounts = new Map<string, number>();
    for (const company of portfolioResearchSummary?.companies ?? []) {
      const ticker = (company.ticker ?? "").toUpperCase();
      if (ticker) {
        tickerCounts.set(ticker, (tickerCounts.get(ticker) ?? 0) + 1);
      }
    }
    for (const company of portfolioResearchSummary?.companies ?? []) {
      const ticker = (company.ticker ?? "").toUpperCase();
      const keys = [
        company.provider && company.providerInstrumentId ? `${company.provider}:${company.providerInstrumentId}` : "",
        company.isin ? `isin:${company.isin}` : "",
        `${company.ticker ?? ""}:${company.exchange ?? ""}`,
        ticker && tickerCounts.get(ticker) === 1 ? `ticker:${ticker}` : ""
      ].filter(Boolean);
      for (const key of keys) {
        resolvedByHoldingKey.set(key.toUpperCase(), company);
      }
    }

    const options = positions.map((position) => {
      const instrument = position.instrument;
      const globalInstrumentId = instrument.globalInstrumentId?.trim();
      const keys = [
        globalInstrumentId ? `global:${globalInstrumentId}` : "",
        instrument.provider && instrument.providerInstrumentId ? `${instrument.provider}:${instrument.providerInstrumentId}` : "",
        instrument.isin ? `isin:${instrument.isin}` : "",
        `${instrument.ticker}:${instrument.exchange}`,
        `ticker:${instrument.ticker}`
      ].filter(Boolean);
      const resolved = globalInstrumentId
        ? portfolioResearchSummary?.companies.find((company) => company.instrumentId === globalInstrumentId)
        : keys.map((key) => resolvedByHoldingKey.get(key.toUpperCase())).find(Boolean);
      const status = resolved?.status ?? (globalInstrumentId ? "GLOBAL_INSTRUMENT_RESOLVED" : "COMPANY_NOT_RESOLVED");
      const displayTicker = resolved?.ticker ?? instrument.ticker;
      const displayExchange = resolved?.exchange ?? instrument.exchange;
      return {
        // The selected value is always the stable global research identity when
        // one is available; display/provider aliases are presentation metadata.
        value: globalInstrumentId ?? resolved?.instrumentId ?? instrument.instrumentId,
        globalInstrumentId,
        companyName: researchCompanyName(position, resolved),
        ticker: displayTicker,
        exchange: displayExchange,
        status,
        assetType: resolved?.assetType ?? instrument.assetType,
        loadable: Boolean(resolved?.instrumentId) && isResearchSummaryLoadable(status),
        held: true,
        quantity: position.quantity
      };
    });
    return options;
  }, [portfolioResearchSummary, positions, researchContext.kind, watchlistResearch]);
  const selectedResearchOption = researchContext.kind === "SEARCH" && searchSelectedMatch
    ? { value: searchSelectedMatch.globalInstrumentId, globalInstrumentId: searchSelectedMatch.globalInstrumentId,
        companyName: searchSelectedMatch.companyName, status: searchPresentation?.status,
        assetType: searchSelectedMatch.assetType, ticker: searchSelectedMatch.canonicalSymbol,
        exchange: searchSelectedMatch.exchange, held: false, quantity: 0 }
    : researchOptions.find((option) => option.value === selectedResearchInstrumentId);
  const refreshEligible = canRefreshResearchIdentity(
    selectedResearchOption?.globalInstrumentId ?? "",
    selectedContextResearchCompany?.status
      ?? (selectedContextResearchCompany ? undefined : selectedResearchOption?.status),
    selectedContextResearchCompany?.assetType
      ?? (selectedContextResearchCompany ? undefined : selectedResearchOption?.assetType),
  );

  function toggleSection(sectionId: ResearchSectionId) {
    setOpenSection((current) => (current === sectionId ? null : sectionId));
  }

  function toggleEvent(eventId: string) {
    setOpenEventId((current) => (current === eventId ? null : eventId));
  }

  return (
    <div className="research-layout">
      <Card className="wide-panel">
        <div className="panel-header">
          <div>
            <h2>Company research</h2>
            <p>{researchContext.kind === "PORTFOLIO"
              ? "This research context contains actual portfolio holdings only."
              : researchContext.kind === "WATCHLIST"
                ? `${researchContext.name} · ${researchContext.region} · non-held public research`
                : researchContext.kind === "SEARCH"
                  ? `Opening ${searchSelectedMatch?.companyName ?? searchPresentation?.companyName ?? selectedResearchInstrumentId} without attaching it to a portfolio.`
                  : `Opening ${researchContext.name} without attaching it to a portfolio.`}</p>
          </div>
        </div>
        {researchContext.kind === "SEARCH" && searchSelectedMatch ? <section>
          <h3>{searchSelectedMatch.companyName}</h3>
          <p>Public company research · Not held</p>
          <p>{[searchSelectedMatch.canonicalSymbol ?? searchSelectedMatch.symbol, searchSelectedMatch.exchange, searchSelectedMatch.isin].filter(Boolean).join(" · ")}</p>
          <Button variant="secondary" disabled={!searchPresentation} onClick={() => setResearchDetail({ researchInstrumentId: selectedResearchInstrumentId })}>Open company research</Button>
        </section> : null}
        {researchContext.kind === "PORTFOLIO" && portfolioResearchError ? <p className="research-refresh-notice" role="alert">{portfolioResearchError}</p> : null}
        {watchlistResearchError ? <p className="research-refresh-notice" role="alert">{watchlistResearchError}</p> : null}
        {researchContext.kind === "WATCHLIST_PENDING" || watchlistResearchLoading ? <Skeleton rows={3} /> : null}
        {(researchContext.kind === "PORTFOLIO" && portfolioResearchSummary)
          || (researchContext.kind === "WATCHLIST" && watchlistResearch?.instruments.length) ? (
          <div className="portfolio-research-table" role="table" aria-label="Company research summary">
            <div className="portfolio-research-row portfolio-research-head" role="row">
              <span role="columnheader">Company</span>
              <span role="columnheader">Price / P/E</span>
              <span role="columnheader">Valuation</span>
              <span role="columnheader">Latest result</span>
              <span role="columnheader">Ownership</span>
              <span role="columnheader">Catalyst</span>
              <span role="columnheader">Sources</span>
              <span role="columnheader">Status</span>
            </div>
            {researchContext.kind === "WATCHLIST" ? (watchlistResearch?.instruments ?? []).map((item) => {
              const company = item.company;
              const performanceTone = performanceRowTone(item.sourcePerformancePct);
              return (
                <button
                  className={`portfolio-research-row portfolio-research-company market-intelligence-research-row market-intelligence-research-row-${performanceTone}`}
                  key={`watchlist-${item.globalInstrumentId}`}
                  role="row"
                  type="button"
                  data-held="false"
                  data-region={researchContext.region}
                  data-global-instrument-id={item.globalInstrumentId}
                  aria-current={selectedResearchInstrumentId === item.globalInstrumentId ? "true" : undefined}
                  aria-label={`${company.companyName}, public research${item.sourcePeriod ? `, market return ${formatSignedPerformancePct(item.sourcePerformancePct)}` : ""}`}
                  onClick={() => {
                    onSelectInstrument(item.globalInstrumentId);
                    setResearchDetail({
                      watchlistInstrumentId: item.globalInstrumentId,
                      researchInstrumentId: company.instrumentId ?? item.globalInstrumentId,
                    });
                  }}
                >
                  <span role="cell">
                    <strong>{company.companyName} <span className={`research-status-dot research-status-${researchStatusTone(company.status)}`} role="img" aria-label={researchStatusDescription(company.status)} title={researchStatusDescription(company.status)} /></strong>
                    <small>{[company.ticker, company.exchange, company.isin].filter(Boolean).join(" / ")}</small>
                    <small>{researchContext.name} · Public company research · Not held</small>
                  </span>
                  <span role="cell">{company.currentPrice == null ? "—" : formatMoney(Number(company.currentPrice), company.structuredMarket?.resolution.currency ?? item.currency ?? undefined)}<small>P/E {metricText(company.valuation.currentPe)}</small></span>
                  <span role="cell"><Badge tone={valuationTone(company.valuation.state)}>{company.valuation.state}</Badge><small>{company.valuation.reason}</small></span>
                  <span role="cell">{latestResultText(company)}</span>
                  <span role="cell">{company.ownershipIncreases.length ? company.ownershipIncreases.map((value) => value === "FII_FPI" ? "FII/FPI" : value.replaceAll("_", " ")).join(" · ") : "Unavailable"}<small>{company.shareholdingChanges.length ? `${company.shareholdingChanges.length} comparable trends` : "Previous comparable period not found"}</small></span>
                  <span role="cell">{company.currentQuarterCatalysts.length ? `${company.currentQuarterCatalysts.length} current` : company.catalystScore ?? "None verified"}</span>
                  <span role="cell">{company.sourceCount} sources / {company.documentCount} docs</span>
                  <span role="cell">
                    <Badge tone={researchStatusTone(company.status)}>{company.status.replaceAll("_", " ")}</Badge>
                    {item.sourcePeriod ? <strong className={`market-intelligence-table-return ${performanceTone}-text`}>Market return ({item.sourcePeriod}) {formatSignedPerformancePct(item.sourcePerformancePct)}</strong> : null}
                  </span>
                </button>
              );
            }) : null}
            {researchContext.kind === "PORTFOLIO" ? (portfolioResearchSummary?.companies ?? []).map((company) => {
              const holding = positions.find((position) => position.instrument.globalInstrumentId === company.instrumentId
                || position.instrument.instrumentId === company.instrumentId
                || Boolean(position.instrument.isin && position.instrument.isin === company.isin)
                || (position.instrument.ticker === company.ticker && position.instrument.exchange === company.exchange));
              return (
                <button
                  className="portfolio-research-row portfolio-research-company"
                  key={`${company.instrumentId ?? company.companyName}-${company.status}`}
                  role="row"
                  type="button"
                  onClick={() => {
                    if (company.instrumentId) {
                      onSelectInstrument(company.instrumentId);
                    }
                    if (holding && company.assetType === "EQUITY" && company.instrumentId) {
                      setResearchDetail({ position: holding, researchInstrumentId: company.instrumentId });
                    }
                  }}
                >
                  <span role="cell">
                    <strong>{company.companyName} <span className={`research-status-dot research-status-${researchStatusTone(company.status)}`} role="img" aria-label={researchStatusDescription(company.status)} title={researchStatusDescription(company.status)} /></strong>
                    <small>{[company.ticker, company.exchange, company.isin].filter(Boolean).join(" / ")}</small>
                    {holding ? <small>Held · Qty {holding.quantity.toLocaleString("en")}</small> : null}
                  </span>
                  <span role="cell">{metricText(company.valuation.currentPe)}</span>
                  <span role="cell"><Badge tone={valuationTone(company.valuation.state)}>{company.valuation.state}</Badge><small>{company.valuation.reason}</small></span>
                  <span role="cell">{latestResultText(company)}</span>
                  <span role="cell">{company.ownershipIncreases.length ? company.ownershipIncreases.map((value) => value === "FII_FPI" ? "FII/FPI" : value.replaceAll("_", " ")).join(" · ") : "Unavailable"}<small>{company.shareholdingChanges.length ? `${company.shareholdingChanges.length} comparable trends` : "Previous comparable period not found"}</small></span>
                  <span role="cell">{company.currentQuarterCatalysts.length ? `${company.currentQuarterCatalysts.length} current` : company.catalystScore ?? "None verified"}</span>
                  <span role="cell">
                    {company.sourceCount} sources / {company.documentCount} docs
                  </span>
                  <span role="cell">
                    <Badge tone={researchStatusTone(company.status)}>{company.status.replaceAll("_", " ")}</Badge>
                    <small>{company.lastRefresh ? new Date(company.lastRefresh).toLocaleString() : company.mode}</small>
                  </span>
                </button>
              );
            }) : null}
          </div>
        ) : summary || researchContext.kind === "SEARCH" ? null : (
          <EmptyState
            title={researchContext.kind === "PORTFOLIO" ? "No company research available" : "No watchlist instruments"}
            message={researchContext.kind === "PORTFOLIO"
              ? "Select a company and open Research readiness to find only the data it needs."
              : "Choose a Market Intelligence stock to add it to this regional watchlist."}
          />
        )}
        {researchDetail && detailResearch ? <StockResearchDrawer position={researchDetail.position} watchlistItem={detailWatchlistItem} research={detailResearch} onClose={() => setResearchDetail(null)} /> : null}
      </Card>

      <Card className="wide-panel research-sticky-panel">
        <div className="panel-header">
          <div>
            <h2>Research intelligence</h2>
            <p>Structured evidence, catalyst scoring, and source citations.</p>
          </div>
          <div className="research-actions">
            {summary?.demo ? <Badge tone="info">DEMO</Badge> : null}
            {summary && !summary.demo ? <Badge tone="positive">LIVE</Badge> : null}
            <Button
              variant="secondary"
              onClick={onRefresh}
              disabled={loading || !refreshEligible}
            >
              <RefreshCw size={16} />
              Research readiness
            </Button>
          </div>
        </div>
        <div className="research-controls">
          {researchContext.kind === "SEARCH" && searchSelectedMatch && selectedResearchInstrumentId ? (
            <small className="research-search-selected">
              <span className="research-search-identity" style={{ display: "flex", flexDirection: "column" }}>
                <span className="research-search-name">{searchPresentation?.companyName ?? searchSelectedMatch?.companyName ?? selectedResearchInstrumentId}</span>
                <span className="research-search-symbol">{[searchSelectedMatch?.canonicalSymbol ?? searchSelectedMatch?.symbol, searchSelectedMatch?.exchange, searchSelectedMatch?.isin].filter(Boolean).join(" · ") || selectedResearchInstrumentId}</span>
                <span className="research-search-status">Public company research · Not held</span>
              </span>
              <Button variant="secondary" disabled={searchWatchlistBusy || searchWatchlistSaved} onClick={() => onToggleSearchWatchlist()}>
                {searchWatchlistSaved ? `Saved to ${regionalWatchlistName(searchSelectedMatch.region)}` : `Add to ${regionalWatchlistName(searchSelectedMatch.region)}`}
              </Button>
              {searchWatchlistError ? <small role="alert">{searchWatchlistError}</small> : null}
            </small>
          ) : null}
          <div className="research-controls-filters" style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap", alignItems: "center", flexBasis: "100%" }}>
          {researchContext.kind !== "SEARCH" ? (
          <label className="sort-control research-company-control">
            <Search size={16} aria-hidden="true" />
            <span>Company</span>
            <select
              className="research-company-select"
              value={selectedResearchInstrumentId}
              title={selectedResearchOption?.companyName}
              onChange={(event) => onSelectInstrument(event.target.value)}
            >
              {researchOptions.map((option) => (
                <option key={`${option.value}-${option.companyName}`} value={option.value} title={option.companyName}>
                  {option.companyName}
                </option>
              ))}
            </select>
            {selectedResearchOption ? (
              <small className="research-company-meta">
                {[selectedResearchOption.ticker, selectedResearchOption.exchange,
                  selectedResearchOption.held
                    ? `Held · quantity ${selectedResearchOption.quantity.toLocaleString("en")}`
                    : "Public company research · Not held"].filter(Boolean).join(" · ")}
              </small>
            ) : null}
          </label>
          ) : null}
          <label className="sort-control">
            <span>Event</span>
            <select value={eventType} onChange={(event) => onEventType(event.target.value)}>
              <option value="">All events</option>
              {eventTypes.map((value) => (
                <option key={value} value={value}>
                  {value.replaceAll("_", " ")}
                </option>
              ))}
            </select>
          </label>
          <label className="sort-control">
            <span>Impact</span>
            <select value={impact} onChange={(event) => onImpact(event.target.value)}>
              <option value="">All impacts</option>
              {impacts.map((value) => (
                <option key={value} value={value}>
                  {value.replaceAll("_", " ")}
                </option>
              ))}
            </select>
          </label>
        </div>
      </div>
      </Card>

      {loading ? <Skeleton rows={5} /> : null}

      {!loading && summary ? (
        <>
          <section className="metrics-grid" aria-label="Research scores">
            <MetricCard label="Catalyst score" value={String(summary.catalystScore.overallScore)} meta="0-100 deterministic" />
            <MetricCard label="Research confidence" value={`${summary.catalystScore.researchConfidence}%`} />
            <MetricCard label="Recent events" value={String(summary.recentEvents.length)} />
            <MetricCard label="Documents" value={String(summary.documents.length)} />
            <MetricCard label="Freshness" value={summary.dataFreshness} tone={summary.demo ? "warning" : "neutral"} />
            <MetricCard label="Last refresh" value={summary.lastRefreshAt ? new Date(summary.lastRefreshAt).toLocaleDateString() : "Not refreshed"} />
          </section>

          <Card className="wide-panel">
            <div className="panel-header">
              <div>
                <h2>{summary.profile.companyName}</h2>
                <p>
                  {summary.profile.ticker} / {summary.profile.exchange} / {summary.profile.country} / {summary.profile.isin ?? "No ISIN"}
                </p>
              </div>
            </div>
            <section className="research-accordion" aria-label="Research detail sections">
              {researchSections.map((section) => {
                const isOpen = openSection === section.id;
                const panelId = `research-section-${section.id}`;
                const buttonId = `research-section-button-${section.id}`;

                return (
                  <article className="research-accordion-item" key={section.id}>
                    <h3>
                      <button
                        id={buttonId}
                        className="research-accordion-button"
                        type="button"
                        aria-expanded={isOpen}
                        aria-controls={panelId}
                        onClick={() => toggleSection(section.id)}
                      >
                        <span>{section.title}</span>
                        <span className="research-section-meta">{section.metricLabel}</span>
                        <ChevronDown className="accordion-chevron" size={18} aria-hidden="true" />
                      </button>
                    </h3>
                    <div
                      id={panelId}
                      className="research-accordion-panel"
                      role="region"
                      aria-labelledby={buttonId}
                      hidden={!isOpen}
                    >
                      {section.id === "overview" ? (
                        <ResearchOverview summary={summary} />
                      ) : section.id === "news" ? (
                        <ResearchEventRows
                          events={section.events}
                          documentsById={documentsById}
                          openEventId={openEventId}
                          onToggleEvent={toggleEvent}
                        />
                      ) : section.id === "sources" ? (
                        <ResearchSources documents={summary.documents} />
                      ) : section.events.length > 0 ? (
                        <div className="research-detail-list">
                          {section.events.map((event) => (
                            <ResearchEventDetail event={event} document={documentsById.get(event.sourceDocumentId)} key={event.eventId} />
                          ))}
                        </div>
                      ) : (
                        <EmptyState title="No evidence in this section" message="No filtered research events currently map to this topic." />
                      )}
                    </div>
                  </article>
                );
              })}
            </section>
          </Card>
        </>
      ) : null}

      {!loading && !summary ? (
        <Card className="wide-panel">
          <EmptyState
            title={researchEmptyTitle(selectedContextResearchCompany?.status)}
            message={researchContext.kind === "SEARCH" ? "Open Research readiness to check the data available for this company." : researchEmptyMessage(selectedContextResearchCompany?.status)}
          />
        </Card>
      ) : null}
    </div>
  );
}

function ResearchOverview({ summary }: { summary: ResearchSummary }) {
  const categoryRows = summary.catalystScore.categoryEvidence
    ? Object.entries(summary.catalystScore.categoryEvidence).map(([label, evidence]) => [label, evidence.score, evidence.status] as const)
    : Object.entries(summary.catalystScore.buckets).map(([label, value]) => [label, value, value === null ? "NO_EVIDENCE" : "NEUTRAL_EVIDENCE"] as const);

  return (
    <div className="research-overview-grid">
      {categoryRows.map(([label, value, status]) => (
        <div className="score-row compact" key={label}>
          <span>{researchCategoryLabel(label)}</span>
          <strong>{value ?? "N/A"}</strong>
          <div className="bar-track" aria-hidden="true">
            <span style={{ width: `${value ?? 0}%` }} />
          </div>
          <small>{status.replaceAll("_", " ")}</small>
        </div>
      ))}
      <div className="research-summary-note">
        <strong>{summary.demo ? "DEMO research data" : "Live research data"}</strong>
        <span>
          {summary.recentEvents.length} events / {summary.documents.length} documents / generated{" "}
          {new Date(summary.catalystScore.generatedAt).toLocaleDateString()}
        </span>
      </div>
    </div>
  );
}

function researchCategoryLabel(category: string) {
  return ({
    GROWTH: "Growth",
    ORDERS_BACKLOG: "Orders & Backlog",
    CAPEX: "CAPEX & Capacity",
    CLIENTS: "Customers",
    GUIDANCE: "Guidance",
  } as Record<string, string>)[category] ?? category.replaceAll("_", " ");
}

function ResearchEventRows({
  events,
  documentsById,
  openEventId,
  onToggleEvent
}: {
  events: ResearchEvent[];
  documentsById: Map<string, ResearchDocument>;
  openEventId: string | null;
  onToggleEvent: (eventId: string) => void;
}) {
  if (events.length === 0) {
    return <EmptyState title="No matching events" message="Change the filters or refresh research fixtures." />;
  }

  return (
    <div className="research-event-rows">
      {events.map((event) => {
        const isOpen = openEventId === event.eventId;
        const panelId = `research-event-${event.eventId}`;
        const buttonId = `research-event-button-${event.eventId}`;

        return (
          <article className="research-event-row" key={event.eventId}>
            <h4>
              <button
                id={buttonId}
                className="research-event-row-button"
                type="button"
                aria-expanded={isOpen}
                aria-controls={panelId}
                onClick={() => onToggleEvent(event.eventId)}
              >
                <span>
                  <Badge tone={impactTone(event.impact)}>{event.impact.replaceAll("_", " ")}</Badge>
                  <strong>{event.title}</strong>
                </span>
                <span>{event.eventType.replaceAll("_", " ")}</span>
                <ChevronDown className="accordion-chevron" size={16} aria-hidden="true" />
              </button>
            </h4>
            <div id={panelId} role="region" aria-labelledby={buttonId} hidden={!isOpen}>
              <ResearchEventDetail event={event} document={documentsById.get(event.sourceDocumentId)} />
            </div>
          </article>
        );
      })}
    </div>
  );
}

function ResearchEventDetail({ event, document }: { event: ResearchEvent; document?: ResearchDocument }) {
  const supportingSources = event.supportingSources?.length
    ? event.supportingSources
    : [
        {
          publisher: document?.publisher ?? document?.sourceName ?? event.sourceType,
          url: event.sourceUrl,
          sourceType: event.sourceClassification ?? document?.sourceClassification ?? event.sourceType,
          publishedAt: event.publishedAt ?? event.eventDate ?? document?.publishedAt ?? null,
          retrievedAt: event.retrievedAt ?? document?.retrievedAt ?? event.detectedAt,
          reliability: event.reliability,
          sourceMode: event.sourceMode,
          documentId: event.sourceDocumentId,
          sourceName: document?.sourceName ?? event.sourceType,
          canonicalUrl: event.sourceUrl,
          independent: true
        }
      ];
  return (
    <div className="research-event-detail">
      <div className="panel-header compact">
        <div>
          <Badge tone="neutral">{event.eventType.replaceAll("_", " ")}</Badge>
          <h4>{event.title}</h4>
        </div>
        <Badge tone={impactTone(event.impact)}>{event.impact.replaceAll("_", " ")}</Badge>
      </div>
      <div className="event-value-line">
        <strong>{event.monetaryOriginal ?? (event.capacityValue ? `${event.capacityValue} ${event.capacityUnit}` : event.percentageOriginal ?? "Value undisclosed")}</strong>
        <span>{event.customer ?? event.counterparty ?? event.location ?? event.timeHorizon.replaceAll("_", " ")}</span>
      </div>
      <p>{event.summary}</p>
      <dl className="event-facts">
        <div>
          <dt>Confidence</dt>
          <dd>{Math.round(event.confidence * 100)}%</dd>
        </div>
        <div>
          <dt>Reliability</dt>
          <dd>{event.reliability}</dd>
        </div>
        <div>
          <dt>Published</dt>
          <dd>{event.eventDate ? new Date(event.eventDate).toLocaleDateString() : "Unknown"}</dd>
        </div>
        <div>
          <dt>Source</dt>
          <dd>{document?.sourceName ?? event.sourceClassification ?? event.sourceType}</dd>
        </div>
        <div>
          <dt>Horizon</dt>
          <dd>{event.timeHorizon.replaceAll("_", " ")}</dd>
        </div>
      </dl>
      <blockquote>{event.rawEvidenceReference}</blockquote>
      <div className="supporting-sources">
        <strong>Supporting sources</strong>
        {supportingSources.map((source) => (
          <a className="source-link" href={source.url} target="_blank" rel="noreferrer" key={`${event.eventId}-${source.documentId}`}>
            <span>{source.publisher ?? source.sourceName}</span>
            <small>
              {source.sourceMode} / {source.sourceType.replaceAll("_", " ")} / {source.reliability} /{" "}
              {source.publishedAt ? new Date(source.publishedAt).toLocaleDateString() : "No publication date"} /{" "}
              {source.independent ? "Independent" : "Duplicate"}
            </small>
          </a>
        ))}
      </div>
    </div>
  );
}

function ResearchSources({ documents }: { documents: ResearchDocument[] }) {
  if (documents.length === 0) {
    return <EmptyState title="No sources" message="No source documents are available for this research profile." />;
  }

  return (
    <div className="source-list">
      {documents.map((document) => (
        <a href={document.canonicalUrl} target="_blank" rel="noreferrer" key={document.documentId}>
          <strong>{document.title ?? document.publisher ?? document.sourceName}</strong>
          <span>
            {document.sourceMode} / {(document.sourceClassification ?? document.sourceType).replaceAll("_", " ")} / {document.reliabilityLevel} /{" "}
            {document.publishedAt ? new Date(document.publishedAt).toLocaleDateString() : "No publication date"}
          </span>
          <small>
            {document.sourceName} / {document.status} / retrieved {new Date(document.retrievedAt).toLocaleDateString()} / hash{" "}
            {document.contentHash.slice(0, 12)}
          </small>
        </a>
      ))}
    </div>
  );
}

function getResearchSections(summary: ResearchSummary, filteredEvents: ResearchEvent[], eventType: string, impact: string) {
  const growth = categorySupportingEvents(summary, "GROWTH", filteredEvents, eventType, impact, ["GEOGRAPHIC_EXPANSION", "PARTNERSHIP", "PRODUCT_LAUNCH"]);
  const orders = categorySupportingEvents(summary, "ORDERS_BACKLOG", filteredEvents, eventType, impact, ["NEW_ORDER", "ORDER_BACKLOG_CHANGE", "MAJOR_CONTRACT", "GOVERNMENT_CONTRACT"]);
  const capex = categorySupportingEvents(summary, "CAPEX", filteredEvents, eventType, impact, ["CAPEX", "CAPACITY_EXPANSION", "NEW_FACILITY", "FACTORY_EXPANSION", "PROJECT_DELAY"]);
  const customers = categorySupportingEvents(summary, "CLIENTS", filteredEvents, eventType, impact, ["NEW_CUSTOMER", "CUSTOMER_EXPANSION", "MAJOR_CUSTOMER", "CUSTOMER_LOSS"]);
  const guidance = categorySupportingEvents(summary, "GUIDANCE", filteredEvents, eventType, impact, ["GUIDANCE_RAISED", "GUIDANCE_LOWERED", "GUIDANCE_CUT", "GUIDANCE_MAINTAINED", "REVENUE_GUIDANCE", "MARGIN_GUIDANCE"]);

  return [
    { id: "overview" as const, title: "Overview", metricLabel: `Score ${summary.catalystScore.overallScore}`, events: filteredEvents },
    { id: "growth" as const, title: "Growth", metricLabel: categoryMetricLabel(summary, "GROWTH"), events: growth },
    { id: "orders" as const, title: "Orders & Backlog", metricLabel: categoryMetricLabel(summary, "ORDERS_BACKLOG"), events: orders },
    { id: "capex" as const, title: "CAPEX & Capacity", metricLabel: categoryMetricLabel(summary, "CAPEX"), events: capex },
    { id: "customers" as const, title: "Customers", metricLabel: categoryMetricLabel(summary, "CLIENTS"), events: customers },
    { id: "guidance" as const, title: "Guidance", metricLabel: categoryMetricLabel(summary, "GUIDANCE"), events: guidance },
    { id: "news" as const, title: "News / Events", metricLabel: `Events ${filteredEvents.length}`, events: filteredEvents },
    { id: "sources" as const, title: "Sources", metricLabel: `Documents ${summary.documents.length}`, events: [] }
  ];
}

function categorySupportingEvents(summary: ResearchSummary, category: string, filteredRecentEvents: ResearchEvent[], eventType: string, impact: string, fallbackTypes: string[]) {
  const evidence = summary.catalystScore.categoryEvidence?.[category];
  if (evidence?.supportingEvents) {
    return evidence.supportingEvents.filter((event) =>
      (!eventType || event.eventType === eventType) && (!impact || event.impact === impact)
    );
  }
  return eventsMatching(filteredRecentEvents, fallbackTypes);
}

function categoryMetricLabel(summary: ResearchSummary, category: string) {
  const evidence = summary.catalystScore.categoryEvidence?.[category];
  if (evidence?.status === "NO_EVIDENCE" || evidence?.score == null) {
    return "N/A / No evidence";
  }
  const count = evidence.independentSourceCount ?? evidence.sourceCount;
  const sources = count === 1 ? "1 source" : `${count} sources`;
  if (evidence.hasConflict || evidence.status === "MIXED_EVIDENCE") {
    return `Mixed evidence / Score ${evidence.score} / ${sources}`;
  }
  return `Score ${evidence.score} / ${sources}`;
}

function eventsMatching(events: ResearchEvent[], eventTypes: string[]) {
  return events.filter((event) => eventTypes.includes(event.eventType));
}

function impactTone(impact: string): "neutral" | "positive" | "negative" | "warning" | "info" {
  if (impact.includes("NEGATIVE")) {
    return "negative";
  }
  if (impact.includes("POSITIVE")) {
    return "positive";
  }
  if (impact === "UNCERTAIN") {
    return "warning";
  }
  return "neutral";
}

function researchStatusTone(status: string): "neutral" | "positive" | "negative" | "warning" | "info" {
  if (status === "AVAILABLE" || status === "RESOLVED_RESEARCH_AVAILABLE") {
    return "positive";
  }
  if (status === "DEGRADED" || status === "RESOLVED_PARTIAL_DATA" || status === "RESOLVED_NO_SOURCES" || status === "RESEARCH_NOT_REFRESHED" || status === "NO_EVIDENCE") {
    return "warning";
  }
  if (["COMPANY_NOT_RESOLVED", "RESEARCH_PROVIDER_UNAVAILABLE", "SOURCE_DISCOVERY_UNAVAILABLE", "SEARCH_PROVIDER_UNAVAILABLE", "DOCUMENT_FETCH_FAILED"].includes(status)) {
    return "negative";
  }
  if (["SEARCH_RETURNED_ZERO_RESULTS", "RESULTS_REJECTED", "EXTRACTION_EMPTY"].includes(status)) return "warning";
  if (status === "RESEARCH_NOT_APPLICABLE" || status === "ETF_UNSUPPORTED") {
    return "info";
  }
  if (status?.startsWith("ETF_RESEARCH_")) {
    return status === "ETF_RESEARCH_AVAILABLE" ? "positive" : "info";
  }
  return "neutral";
}

function researchStatusDescription(status: string): string {
  if (status === "RESOLVED_RESEARCH_AVAILABLE") return "Research available";
  if (status === "RESOLVED_PARTIAL_DATA") return "Partial research data — some research sections are unavailable.";
  if (status === "ETF_UNSUPPORTED" || status === "RESEARCH_NOT_APPLICABLE") return "ETF research unsupported";
  return status.replaceAll("_", " ");
}

function isResearchSummaryLoadable(status?: string | null) {
  return ["AVAILABLE", "DEGRADED", "RESOLVED_RESEARCH_AVAILABLE", "RESOLVED_PARTIAL_DATA"].includes(status ?? "");
}

function canRefreshResearch(company?: PortfolioResearchCompany | null) {
  const status = company?.status ?? "";
  return Boolean(company?.instrumentId)
    && status !== "ETF_UNSUPPORTED"
    && !status.startsWith("ETF_RESEARCH_")
    && !["COMPANY_NOT_RESOLVED", "RESEARCH_NOT_APPLICABLE"].includes(status);
}

function canRefreshResearchIdentity(
  globalInstrumentId?: string | null,
  status?: string | null,
  assetType?: string | null,
) {
  const normalizedStatus = status ?? "";
  const normalizedAssetType = (assetType ?? "").toUpperCase();
  return Boolean(globalInstrumentId?.trim())
    && !["COMPANY_NOT_RESOLVED", "RESEARCH_NOT_APPLICABLE", "ETF_UNSUPPORTED"].includes(normalizedStatus)
    && !normalizedStatus.startsWith("ETF_RESEARCH_")
    && !["ETF", "FUND", "BOND", "CASH", "CRYPTO"].includes(normalizedAssetType);
}

function researchEmptyTitle(status?: string | null) {
  if (status === "ETF_UNSUPPORTED") {
    return "ETF company research unsupported";
  }
  if (status === "ETF_RESEARCH_NOT_REFRESHED") {
    return "ETF research not refreshed";
  }
  if (status === "ETF_RESEARCH_SOURCE_UNAVAILABLE") {
    return "ETF research unavailable";
  }
  if (status === "RESEARCH_NOT_APPLICABLE") {
    return "Research not applicable";
  }
  if (status === "COMPANY_NOT_RESOLVED") {
    return "Company not resolved";
  }
  if (status === "RESEARCH_NOT_REFRESHED") {
    return "Research not refreshed";
  }
  return "Research unavailable";
}

function researchEmptyMessage(status?: string | null) {
  if (status === "ETF_RESEARCH_NOT_REFRESHED") {
    return "No shared public ETF research has been collected for this fund yet.";
  }
  if (status === "ETF_RESEARCH_SOURCE_UNAVAILABLE") {
    return "No acceptable public ETF source was collected during the last refresh.";
  }
  if (status === "RESEARCH_NOT_APPLICABLE") {
    return "Company-level catalyst research is not applicable for this asset type.";
  }
  if (status === "COMPANY_NOT_RESOLVED") {
    return "This holding did not match a canonical research company.";
  }
  if (status === "RESEARCH_NOT_REFRESHED") {
    return "No shared public research has been collected for this company yet.";
  }
  return "Select a holding with available company research.";
}

function SettingsView() {
  return (
    <Card className="wide-panel">
      <div className="panel-header">
        <div>
          <h2>Platform preferences</h2>
          <p>Light, dark, and system theme support is wired for frontend work.</p>
        </div>
        <ShieldCheck size={20} aria-hidden="true" />
      </div>
      <div className="settings-grid">
        <div>
          <h3>Data freshness vocabulary</h3>
          <p>REAL-TIME, DELAYED, EOD, STALE, DEMO, and UNAVAILABLE labels are supported. DEMO is never presented as live market data.</p>
        </div>
        <div>
          <h3>Error handling</h3>
          <p>API errors show a user-safe message and correlation ID without stack traces or internal secrets.</p>
        </div>
        <div>
          <h3>Risk indicators</h3>
          <p>Portfolio risk scoring is not yet available.</p>
        </div>
        <div>
          <h3>Recommendation states</h3>
          <p>BUY, ADD, HOLD, TRIM, and SELL badges are design-system ready for future recommendation logic.</p>
        </div>
      </div>
    </Card>
  );
}

function brokerDisplayName(brokerType: string) {
  if (brokerType === "ICICI_DIRECT") {
    return "ICICI Direct";
  }
  if (brokerType === "IBKR") {
    return "Interactive Brokers";
  }
  if (brokerType === "MOCK") {
    return "Demo Broker";
  }
  return brokerType;
}

function StockSearchField({
  selectedGlobalInstrumentId,
  onSelect,
}: {
  selectedGlobalInstrumentId?: string;
  onSelect: (match: ResearchInstrumentMatch) => void;
}) {
  const DEBOUNCE_MS = 300;
  const [region, setRegion] = useState<SectorPerformance["region"]>("INDIA");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<ResearchInstrumentMatch[]>([]);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [highlight, setHighlight] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  const cache = useRef(new Map<string, ResearchInstrumentMatch[]>());
  const trimmed = query.trim();
  useEffect(() => {
    let cancelled = false;
    setResults([]);
    setOpen(false);
    setLoading(false);
    setError(null);
    if (trimmed.length < 3) return;
    const key = `${region}:${trimmed.toLowerCase()}`;
    const show = (items: ResearchInstrumentMatch[]) => {
      if (cancelled) return;
      setResults(items);
      setOpen(true);
      setHighlight(0);
    };
    const cached = cache.current.get(key);
    if (cached) { show(cached); return; }
    const active = setTimeout(() => {
      setLoading(true);
      void researchApi.searchInstruments(region, trimmed, 20)
        .then((items) => {
          if (cancelled) return;
          if (cache.current.size >= 50) cache.current.delete(cache.current.keys().next().value!);
          cache.current.set(key, items);
          show(items);
        })
        .catch(() => {
          if (!cancelled) setError("Stock search unavailable. Please try again.");
        })
        .finally(() => { if (!cancelled) setLoading(false); });
    }, DEBOUNCE_MS);
    return () => { cancelled = true; clearTimeout(active); };
  }, [trimmed, region]);

  useEffect(() => {
    if (open) document.getElementById(`research-option-${highlight}`)?.scrollIntoView({ block: "nearest" });
  }, [highlight, open]);

  function commit(highlighted: number) {
    const item = results[highlighted];
    if (!item) return;
    onSelect(item);
    setQuery("");
    setResults([]);
    setOpen(false);
  }

  const rows = results.slice(0, 20);
  const rowCount = Math.max(1, rows.length);

  return (
    <div className="research-search-control">
      <div className="research-region-control" role="group" aria-label="Region">
      <span>Region</span>
      {(["INDIA", "USA", "EUROPE"] as const).map((r) => (
        <button
          key={r}
          type="button"
          aria-pressed={r === region}
          className={r === region ? "active" : ""}
          onClick={() => { setRegion(r); setQuery(""); setResults([]); setOpen(false); }}
        >
          {r}
        </button>
      ))}
      </div>
      <label htmlFor="research-stock-query">Search stocks</label>
      <input
        id="research-stock-query"
        role="combobox"
        aria-activedescendant={open && rows.length ? `research-option-${highlight}` : undefined}
        ref={inputRef}
        type="search"
        className="research-search-input"
        placeholder="Search by company name, symbol or ISIN..."
        value={query}
        autoComplete="off"
        aria-autocomplete="list"
        aria-expanded={open}
        aria-controls={open ? "research-search-listbox" : undefined}
        onBlur={() => setOpen(false)}
        onFocus={() => { if (trimmed.length >= 3 && results.length) setOpen(true); }}
        onChange={(event) => setQuery(event.target.value)}
        onKeyDown={(event) => {
          if (!open) return;
          if (event.key === "ArrowDown") {
            event.preventDefault();
            setHighlight((current) => (current + 1) % rowCount);
          } else if (event.key === "ArrowUp") {
            event.preventDefault();
            setHighlight((current) => (current - 1 + rowCount) % rowCount);
          } else if (event.key === "Enter") {
            event.preventDefault();
            commit(highlight);
          } else if (event.key === "Escape") {
            event.preventDefault();
            setOpen(false);
          }
        }}
      />
      {open && !loading && rows.length === 0 ? <small role="status">No stocks found.</small> : null}
      {loading ? <small role="status">Searching…</small> : null}
      {error ? <small role="alert">{error}</small> : null}
      <ul
        id="research-search-listbox"
        className="research-search-listbox"
        role="listbox"
        hidden={!open || rows.length === 0}
        style={{ position: "absolute", background: "var(--surface-raised)", border: "1px solid var(--border)", maxHeight: "240px", overflowY: "auto", zIndex: 1000 }}
      >
        {rows.map((item, index) => (
          <li
            id={`research-option-${index}`}
            key={item.globalInstrumentId}
            role="option"
            aria-selected={index === highlight}
            className={index === highlight ? "highlighted" : ""}
            onMouseDown={(event) => {
              event.preventDefault();
              commit(index);
            }}
          >
            <strong>{item.companyName}</strong>
            <small>{[item.canonicalSymbol ?? item.symbol, item.exchange, item.isin].filter(Boolean).join(" · ")}</small>
            {item.sector ? <small>{item.sector}</small> : null}
          </li>
        ))}
      </ul>
    </div>
  );
}
