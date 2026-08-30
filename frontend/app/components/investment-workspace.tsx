"use client";

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
  WalletCards,
  X
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
  type PortfolioListItem,
  type PortfolioImportPreview,
  type PortfolioPosition,
  type PortfolioResearchCompany,
  type PortfolioResearchSummary,
  type PortfolioSummary,
  type ResearchDocument,
  type ResearchEvent,
  type ResearchSummary,
  brokerApi,
  authApi,
  portfolioApi,
  researchApi
} from "../lib/portfolio-api";
import { Badge, Button, Card, EmptyState, ErrorState, Field, MetricCard, Skeleton } from "./ui";

type View = "dashboard" | "portfolio" | "research" | "brokers" | "settings";
type Theme = "system" | "light" | "dark";
type SortKey = "company" | "ticker" | "marketValue" | "profitLoss" | "allocation";
type ResearchSectionId = "overview" | "growth" | "orders" | "capex" | "customers" | "guidance" | "news" | "sources";
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
  return formatMoney(money?.amount, money?.currency);
}

function formatPercent(value?: number) {
  if (value === undefined || Number.isNaN(value)) {
    return "--";
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
  if (!summary || summary.totalMarketValue.amount === 0) {
    return 0;
  }

  return (position.marketValue.amount / summary.totalMarketValue.amount) * 100;
}

function getApiFailure(error: unknown): ApiFailure {
  if (typeof error === "object" && error !== null && "message" in error) {
    return error as ApiFailure;
  }

  return { message: "The portfolio API is not reachable. Confirm the gateway or portfolio service is running." };
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
  const [view, setView] = useState<View>("dashboard");
  const [theme, setTheme] = useState<Theme>("system");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [portfolios, setPortfolios] = useState<PortfolioListItem[]>([]);
  const [portfolioDashboard, setPortfolioDashboard] = useState<PortfolioDashboard | null>(null);
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
  const [selectedResearchInstrumentId, setSelectedResearchInstrumentId] = useState("");
  const [researchSummary, setResearchSummary] = useState<ResearchSummary | null>(null);
  const [researchLoading, setResearchLoading] = useState(false);
  const [portfolioResearchSummary, setPortfolioResearchSummary] = useState<PortfolioResearchSummary | null>(null);
  const [portfolioResearchLoading, setPortfolioResearchLoading] = useState(false);
  const [researchEventType, setResearchEventType] = useState("");
  const [researchImpact, setResearchImpact] = useState("");

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

    if (accessToken) {
      void loadPortfolios();
    }
    return () => {
      cancelled = true;
    };
  }, [accessToken, authenticatedUser?.userId]);

  useEffect(() => {
    let cancelled = false;

    async function loadResearchSummary() {
      if (!selectedResearchInstrumentId) {
        setResearchSummary(null);
        return;
      }
      const selectedPortfolioCompany = portfolioResearchSummary?.companies.find(
        (company) => company.instrumentId === selectedResearchInstrumentId
      );
      if (selectedPortfolioCompany && !isResearchSummaryLoadable(selectedPortfolioCompany.status)) {
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
  }, [portfolioResearchSummary, selectedResearchInstrumentId]);

  useEffect(() => {
    let cancelled = false;

    async function loadPortfolioResearchSummary() {
      if (!selectedPortfolioId) {
        setPortfolioResearchSummary(null);
        setSelectedResearchInstrumentId("");
        return;
      }
      setPortfolioResearchLoading(true);
      try {
        const loaded = await researchApi.getPortfolioSummary(selectedPortfolioId);
        if (cancelled) {
          return;
        }
        setPortfolioResearchSummary(loaded);
        setSelectedResearchInstrumentId((current) => {
          if (current && loaded.companies.some((company) => company.instrumentId === current)) {
            return current;
          }
          return loaded.companies.find((company) => company.instrumentId && isResearchSummaryLoadable(company.status))?.instrumentId ?? "";
        });
      } catch {
        if (!cancelled) {
          setPortfolioResearchSummary(null);
          setSelectedResearchInstrumentId("");
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

    if (accessToken) {
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

    if (accessToken) {
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
            return b.unrealizedProfitLoss.amount - a.unrealizedProfitLoss.amount;
          case "allocation":
            return getAllocationValue(summary, b) - getAllocationValue(summary, a);
          case "marketValue":
          default:
            return b.marketValue.amount - a.marketValue.amount;
        }
      });
  }, [positions, searchText, sortKey, summary]);

  async function updateHoldingDisplayName(position: PortfolioPosition, customDisplayName: string | null) {
    const updated = await portfolioApi.updateHoldingDisplayName(position.portfolioId, position.positionId, customDisplayName);
    setPositions((current) => current.map((item) => item.positionId === updated.positionId ? updated : item));
    setPortfolioDashboard(await portfolioApi.getDashboard());
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
      const [loadedSummary, loadedPositions] = await Promise.all([
        portfolioApi.getSummary(selectedPortfolioId), portfolioApi.getPositions(selectedPortfolioId)
      ]);
      setPortfolioDashboard(dashboard);
      setPortfolios(dashboard.portfolios);
      setSummary(loadedSummary);
      setPositions(loadedPositions);
    } catch (err) {
      authWindow?.close();
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
    const [connections, dashboard] = await Promise.all([
      brokerApi.listConnections(),
      portfolioApi.getDashboard()
    ]);
    setBrokerConnections(connections);
    setPortfolioDashboard(dashboard);
    setPortfolios(dashboard.portfolios);
    return true;
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
      clearUserScopedState();
      setAccessToken(session.accessToken);
      setAuthenticatedUser(session.user);
    } catch (err) {
      setError(getApiFailure(err));
    } finally {
      setAuthLoading(false);
    }
  }

  function logout() {
    window.localStorage.removeItem("aip.accessToken");
    window.localStorage.removeItem("aip.user");
    setAccessToken(null);
    setAuthenticatedUser(null);
    clearUserScopedState();
  }

  if (authLoading) {
    return <LoadingView />;
  }

  if (!accessToken || !authenticatedUser) {
    return <SignInView error={error} onLogin={login} />;
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
                Backend: <code>{frontendConfig.apiBaseUrl}</code>
              </p>
            </div>
            <PortfolioSelector
              portfolios={portfolios}
              selectedPortfolioId={selectedPortfolioId}
              onChange={(portfolioId) => {
                setPortfolioResearchSummary(null);
                setSelectedResearchInstrumentId("");
                setResearchSummary(null);
                setPortfolioHistory(null);
                rememberSelectedPortfolioId(authenticatedUser?.userId, portfolioId);
                setSelectedPortfolioId(portfolioId);
              }}
            />
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
                    <MultiPortfolioDashboard dashboard={portfolioDashboard} onCreate={createPortfolio} creating={creating} />
                  ) : (
                    <DashboardView
                      portfolio={selectedPortfolio}
                      summary={summary}
                      positions={positions}
                      onCreate={createPortfolio}
                      onSync={selectedPortfolio?.brokerConnectionId ? syncSelectedPortfolio : undefined}
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
                  creating={creating}
                  syncing={syncing}
                  newPortfolioName={newPortfolioName}
                  newPortfolioCurrency={newPortfolioCurrency}
                  setNewPortfolioName={setNewPortfolioName}
                  setNewPortfolioCurrency={setNewPortfolioCurrency}
                  onUpdateDisplayName={updateHoldingDisplayName}
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
                <ResearchView
                  positions={positions}
                  selectedPortfolioId={selectedPortfolioId}
                  portfolioResearchSummary={portfolioResearchSummary}
                  portfolioResearchLoading={portfolioResearchLoading}
                  selectedInstrumentId={selectedResearchInstrumentId}
                  onSelectInstrument={setSelectedResearchInstrumentId}
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
                    setResearchLoading(true);
                    try {
                      setResearchSummary(await researchApi.refresh(selectedResearchInstrumentId));
                    } finally {
                      setResearchLoading(false);
                    }
                  }}
                  onPortfolioRefresh={async () => {
                    if (!selectedPortfolioId) {
                      return;
                    }
                    setPortfolioResearchLoading(true);
                    try {
                      const refreshed = await researchApi.refreshPortfolio(selectedPortfolioId);
                      setPortfolioResearchSummary(refreshed);
                      const firstLoadable = refreshed.companies.find((company) =>
                        company.instrumentId && isResearchSummaryLoadable(company.status)
                      );
                      if (firstLoadable?.instrumentId) {
                        setSelectedResearchInstrumentId(firstLoadable.instrumentId);
                      }
                    } finally {
                      setPortfolioResearchLoading(false);
                    }
                  }}
                />
              ) : null}
              {view === "settings" ? <SettingsView /> : null}
            </>
          ) : null}
        </div>
      </section>
    </main>
  );
}

function SignInView({
  error,
  onLogin
}: {
  error: ApiFailure | null;
  onLogin: (userKey: "user-a" | "user-b") => void;
}) {
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
        <h1>Sign in</h1>
        <p>Choose a DEV identity to validate authentication and data isolation.</p>
        {error ? <ErrorState message={error.message} correlationId={error.correlationId} /> : null}
        <div className="signin-actions">
          <Button onClick={() => onLogin("user-a")}>Sign in as User A</Button>
          <Button variant="secondary" onClick={() => onLogin("user-b")}>Sign in as User B</Button>
        </div>
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
  onCreate,
  creating
}: {
  dashboard: PortfolioDashboard | null;
  onCreate: () => void;
  creating: boolean;
}) {
  if (!dashboard || dashboard.portfolios.length === 0) {
    return (
      <Card className="wide-panel">
        <EmptyState title="No portfolios" message="Connect a broker or create a portfolio to begin." />
        <Button onClick={onCreate} disabled={creating}>{creating ? "Creating..." : "Create portfolio"}</Button>
      </Card>
    );
  }
  return (
    <div className="dashboard-grid">
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
      <Card className="wide-panel">
        <div className="panel-header"><div><h2>All holdings</h2><p>Matching ISINs are combined with quantity-weighted average cost. Imported prices remain snapshots.</p></div></div>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Instrument</th><th>ISIN</th><th>Quantity</th><th>Weighted average cost</th><th>Value</th><th>Provenance</th></tr></thead>
            <tbody>
              {dashboard.combinedHoldings.map((holding) => (
                  <tr key={holding.securityKey + holding.averageCost.currency}>
                    <td>{holding.companyName}<small>{holding.symbol}</small></td>
                    <td>{holding.isin}</td>
                    <td>{holding.quantity}</td>
                    <td>{formatBackendMoney(holding.averageCost)}</td>
                    <td>{formatBackendMoney(holding.marketValue)}</td>
                    <td>{holding.dataFreshness === "IMPORTED_SNAPSHOT" ? "Imported snapshot" : "Mixed sources"}</td>
                  </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
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

  const topGainers = [...positions].sort((a, b) => b.unrealizedProfitLossPercent - a.unrealizedProfitLossPercent).slice(0, 3);
  const topLosers = [...positions].sort((a, b) => a.unrealizedProfitLossPercent - b.unrealizedProfitLossPercent).slice(0, 3);
  const sourceLabels = portfolioSourceLabels(positions);
  const syncAction = onSync ? (
    <Button onClick={onSync} disabled={syncing} variant="secondary">
      <RefreshCw size={16} />
      {syncing ? "Syncing..." : sourceLabels.syncButton}
    </Button>
  ) : null;

  return (
    <div className="dashboard-grid">
      <section className="metrics-grid" aria-label="Portfolio summary">
        <MetricCard
          label="Portfolio value"
          value={formatBackendMoney(summary?.totalMarketValue)}
          meta={sourceLabels.syncMeta}
        />
        <MetricCard label={sourceLabels.totalProfitLoss} value={formatBackendMoney(summary?.unrealizedProfitLoss)} tone={(summary?.unrealizedProfitLoss.amount ?? 0) >= 0 ? "positive" : "negative"} />
        <MetricCard label={sourceLabels.returnLabel} value={formatPercent(summary?.unrealizedProfitLossPercent)} tone={(summary?.unrealizedProfitLossPercent ?? 0) >= 0 ? "positive" : "negative"} />
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
          {syncAction}
        </div>
        {summary ? <AllocationCharts summary={summary} /> : <EmptyState title="No positions" message="This portfolio currently has no positions." />}
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
              <strong className={position.unrealizedProfitLossPercent >= 0 ? "positive-text" : "negative-text"}>
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
  creating,
  syncing,
  newPortfolioName,
  newPortfolioCurrency,
  setNewPortfolioName,
  setNewPortfolioCurrency,
  onUpdateDisplayName
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
  creating: boolean;
  syncing: boolean;
  newPortfolioName: string;
  newPortfolioCurrency: string;
  setNewPortfolioName: (value: string) => void;
  setNewPortfolioCurrency: (value: string) => void;
  onUpdateDisplayName: (position: PortfolioPosition, customDisplayName: string | null) => Promise<void>;
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

  return (
    <div className="portfolio-layout">
      <Card className="wide-panel">
        <div className="panel-header">
          <div>
            <p className="eyebrow">{portfolio.provider ? brokerDisplayName(portfolio.provider) : "Manual portfolio"}</p>
            <h2>{portfolio.name}</h2>
            <p>Broker holdings synced: {portfolio.lastBrokerSyncAt ? new Date(portfolio.lastBrokerSyncAt).toLocaleString() : portfolio.brokerConnectionId ? "Imported previously; sync time unknown" : "Not applicable"}</p>
            <p>Market price updated: {rawPositions.map((position) => position.quote?.sourceTimestamp).filter(Boolean).sort().at(-1) ? new Date(rawPositions.map((position) => position.quote?.sourceTimestamp).filter(Boolean).sort().at(-1)!).toLocaleString() : "Last-known valuation"}</p>
          </div>
          {onSync ? <Button onClick={onSync} disabled={syncing} variant="secondary"><RefreshCw size={16} />{syncing ? "Syncing..." : "Sync"}</Button> : null}
        </div>
        <p>Base currency: {portfolio.baseCurrency} · Connection: {portfolio.lastBrokerSyncErrorCode ? "Re-authentication or retry required" : portfolio.brokerConnectionId ? "Connected or previously connected" : "Not broker-backed"}</p>
      </Card>
      <section className="metrics-grid" aria-label="Portfolio totals">
        <MetricCard label={sourceLabels.marketValue} value={formatBackendMoney(summary?.totalMarketValue)} />
        <MetricCard label={sourceLabels.costBasis} value={formatBackendMoney(summary?.totalCostBasis)} />
        <MetricCard label={sourceLabels.unrealizedProfitLoss} value={formatBackendMoney(summary?.unrealizedProfitLoss)} tone={(summary?.unrealizedProfitLoss.amount ?? 0) >= 0 ? "positive" : "negative"} />
        <MetricCard label={sourceLabels.returnLabel} value={formatPercent(summary?.unrealizedProfitLossPercent)} />
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
          </div>
          {onSync ? (
            <Button onClick={onSync} disabled={syncing} variant="secondary">
              <RefreshCw size={16} />
              {syncing ? "Syncing..." : sourceLabels.syncButton}
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
          <HoldingsTable positions={positions} summary={summary} onUpdateDisplayName={onUpdateDisplayName} />
        )}
      </Card>

      {summary ? (
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

function HoldingsTable({ positions, summary, onUpdateDisplayName }: {
  positions: PortfolioPosition[];
  summary: PortfolioSummary | null;
  onUpdateDisplayName: (position: PortfolioPosition, customDisplayName: string | null) => Promise<void>;
}) {
  const [editingPositionId, setEditingPositionId] = useState<string | null>(null);
  const [editingName, setEditingName] = useState("");
  const [savingPositionId, setSavingPositionId] = useState<string | null>(null);
  const [renameError, setRenameError] = useState<string | null>(null);

  async function saveName(position: PortfolioPosition, reset = false) {
    const nextName = reset ? null : editingName.trim();
    if (!reset && !nextName) {
      setRenameError("Enter a name, or use Reset to remove the custom name.");
      return;
    }
    setSavingPositionId(position.positionId);
    setRenameError(null);
    try {
      await onUpdateDisplayName(position, nextName);
      setEditingPositionId(null);
    } catch (error) {
      setRenameError(getApiFailure(error).message);
    } finally {
      setSavingPositionId(null);
    }
  }

  return (
    <div className="table-frame">
      <table>
        <thead>
          <tr>
            <th>Company</th>
            <th>Ticker</th>
            <th>Exchange</th>
            <th>Quantity</th>
            <th>Average cost</th>
            <th>Broker current price</th>
            <th>Quote last price</th>
            <th>Bid</th>
            <th>Ask</th>
            <th>Market value</th>
            <th>Unrealized P/L</th>
            <th>Unrealized P/L %</th>
            <th>Allocation</th>
            <th>Currency</th>
            <th>Data status</th>
            <th>Last updated</th>
            <th>Source</th>
            <th>Broker</th>
            <th>AI rating</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((position) => {
            const allocation = getAllocationValue(summary, position);
            return (
              <tr key={position.positionId}>
                <td>
                  <button className="security-button" type="button">
                    <strong>{position.displayName}</strong>
                    <span>{position.instrument.isin ?? "No ISIN"}</span>
                  </button>
                  {position.sourceType === "MANUAL_CSV_IMPORT" ? editingPositionId === position.positionId ? (
                    <div className="holding-name-editor">
                      <label>
                        Display name
                        <input maxLength={160} value={editingName} onChange={(event) => setEditingName(event.target.value)} />
                      </label>
                      <Button disabled={savingPositionId === position.positionId} onClick={() => void saveName(position)}>Save</Button>
                      <Button variant="secondary" disabled={savingPositionId === position.positionId} onClick={() => { setEditingPositionId(null); setRenameError(null); }}>Cancel</Button>
                      {position.customDisplayName ? <Button variant="secondary" disabled={savingPositionId === position.positionId} onClick={() => void saveName(position, true)}>Reset</Button> : null}
                      {renameError ? <small role="alert">{renameError}</small> : null}
                    </div>
                  ) : (
                    <button className="text-action" type="button" onClick={() => { setEditingPositionId(position.positionId); setEditingName(position.customDisplayName ?? position.displayName); setRenameError(null); }}>
                      Edit name
                    </button>
                  ) : null}
                </td>
                <td>{position.instrument.ticker}</td>
                <td>{position.instrument.exchange}</td>
                <td>{position.quantity.toLocaleString("en")}</td>
                <td>{formatMoney(position.averageCost.amount, position.averageCost.currency)}</td>
                <td>{formatMoney(position.currentPrice.amount, position.currentPrice.currency)}</td>
                <td>{formatMoney(position.quote?.last?.amount, position.quote?.last?.currency)}</td>
                <td>{formatMoney(position.quote?.bid?.amount, position.quote?.bid?.currency)}</td>
                <td>{formatMoney(position.quote?.ask?.amount, position.quote?.ask?.currency)}</td>
                <td>{formatMoney(position.marketValue.amount, position.marketValue.currency)}</td>
                <td className={position.unrealizedProfitLoss.amount >= 0 ? "positive-text" : "negative-text"}>
                  {formatMoney(position.unrealizedProfitLoss.amount, position.unrealizedProfitLoss.currency)}
                </td>
                <td className={position.unrealizedProfitLossPercent >= 0 ? "positive-text" : "negative-text"}>
                  {formatPercent(position.unrealizedProfitLossPercent)}
                </td>
                <td>{formatPercent(allocation)}</td>
                <td>{position.instrument.tradingCurrency}</td>
                <td>
                  {position.dataFreshness === "REAL_BROKER" ? (
                    <FreshnessBadge freshness="REAL_BROKER" />
                  ) : position.quote?.freshness ? (
                    <FreshnessBadge freshness={position.quote.freshness} />
                  ) : (
                    <FreshnessBadge freshness="UNAVAILABLE" />
                  )}
                </td>
                <td>{position.lastUpdated ? new Date(position.lastUpdated).toLocaleString() : position.quote?.receivedAt ? new Date(position.quote.receivedAt).toLocaleString() : position.quote?.timestamp ? new Date(position.quote.timestamp).toLocaleString() : "Unavailable"}</td>
                <td>{position.dataFreshness === "REAL_BROKER" ? `${brokerDisplayName(position.brokerType)} / REAL_BROKER` : position.quote?.source ?? "--"}</td>
                <td>{brokerDisplayName(position.brokerType)}</td>
                <td>
                  <Badge tone="neutral">Not rated</Badge>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
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
  selectedPortfolioId,
  portfolioResearchSummary,
  portfolioResearchLoading,
  selectedInstrumentId,
  onSelectInstrument,
  summary,
  loading,
  eventType,
  impact,
  onEventType,
  onImpact,
  onRefresh,
  onPortfolioRefresh
}: {
  positions: PortfolioPosition[];
  selectedPortfolioId: string;
  portfolioResearchSummary: PortfolioResearchSummary | null;
  portfolioResearchLoading: boolean;
  selectedInstrumentId: string;
  onSelectInstrument: (value: string) => void;
  summary: ResearchSummary | null;
  loading: boolean;
  eventType: string;
  impact: string;
  onEventType: (value: string) => void;
  onImpact: (value: string) => void;
  onRefresh: () => Promise<void>;
  onPortfolioRefresh: () => Promise<void>;
}) {
  const [openSection, setOpenSection] = useState<ResearchSectionId | null>(null);
  const [openEventId, setOpenEventId] = useState<string | null>(null);
  const filteredEvents = (summary?.recentEvents ?? []).filter((event) => {
    return (!eventType || event.eventType === eventType) && (!impact || event.impact === impact);
  });
  const eventTypes = [...new Set((summary?.recentEvents ?? []).map((event) => event.eventType))].sort();
  const impacts = [...new Set((summary?.recentEvents ?? []).map((event) => event.impact))].sort();
  const documentsById = new Map((summary?.documents ?? []).map((document) => [document.documentId, document]));
  const researchSections = summary ? getResearchSections(summary, filteredEvents) : [];
  const selectedPortfolioResearchCompany =
    portfolioResearchSummary?.companies.find((company) => company.instrumentId === selectedInstrumentId) ?? null;
  const researchOptions = useMemo(() => {
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

    return positions.map((position) => {
      const instrument = position.instrument;
      const keys = [
        instrument.provider && instrument.providerInstrumentId ? `${instrument.provider}:${instrument.providerInstrumentId}` : "",
        instrument.isin ? `isin:${instrument.isin}` : "",
        `${instrument.ticker}:${instrument.exchange}`,
        `ticker:${instrument.ticker}`
      ].filter(Boolean);
      const resolved = keys.map((key) => resolvedByHoldingKey.get(key.toUpperCase())).find(Boolean);
      const status = resolved?.status ?? "COMPANY_NOT_RESOLVED";
      const displayTicker = resolved?.ticker ?? instrument.ticker;
      const displayExchange = resolved?.exchange ?? instrument.exchange;
      return {
        value: resolved?.instrumentId ?? instrument.instrumentId,
        companyName: researchCompanyName(position, resolved),
        ticker: displayTicker,
        exchange: displayExchange,
        status,
        loadable: Boolean(resolved?.instrumentId) && isResearchSummaryLoadable(status)
      };
    });
  }, [portfolioResearchSummary, positions]);
  const selectedResearchOption = researchOptions.find((option) => option.value === selectedInstrumentId);

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
            <h2>Portfolio research</h2>
            <p>Company-level research status across current holdings.</p>
          </div>
          <Button variant="secondary" onClick={onPortfolioRefresh} disabled={portfolioResearchLoading || !selectedPortfolioId}>
            <RefreshCw size={16} />
            {portfolioResearchLoading ? "Refreshing..." : "Refresh portfolio research"}
          </Button>
        </div>
        {portfolioResearchSummary ? (
          <div className="portfolio-research-table" role="table" aria-label="Portfolio research summary">
            <div className="portfolio-research-row portfolio-research-head" role="row">
              <span role="columnheader">Company</span>
              <span role="columnheader">Catalyst</span>
              <span role="columnheader">Confidence</span>
              <span role="columnheader">Evidence</span>
              <span role="columnheader">Sources</span>
              <span role="columnheader">Status</span>
            </div>
            {portfolioResearchSummary.companies.map((company) => {
              const evidenceTotal = Object.keys(company.evidenceCoverage).length || 5;
              const evidenceCount = Object.values(company.evidenceCoverage).filter((value) => value !== "NO_EVIDENCE").length;
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
                  }}
                >
                  <span role="cell">
                    <strong>{company.companyName}</strong>
                    <small>{[company.ticker, company.exchange, company.isin].filter(Boolean).join(" / ")}</small>
                  </span>
                  <span role="cell">{company.catalystScore ?? "N/A"}</span>
                  <span role="cell">{company.confidence != null ? `${company.confidence}%` : "N/A"}</span>
                  <span role="cell">
                    {evidenceCount}/{evidenceTotal}
                  </span>
                  <span role="cell">
                    {company.sourceCount} sources / {company.documentCount} docs
                  </span>
                  <span role="cell">
                    <Badge tone={researchStatusTone(company.status)}>{company.status.replaceAll("_", " ")}</Badge>
                    <small>{company.lastRefresh ? new Date(company.lastRefresh).toLocaleString() : company.mode}</small>
                  </span>
                </button>
              );
            })}
          </div>
        ) : (
          <EmptyState title="No portfolio research run" message="Refresh portfolio research to summarize current holdings." />
        )}
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
              disabled={loading || !selectedInstrumentId || !canRefreshResearch(selectedPortfolioResearchCompany?.status)}
            >
              <RefreshCw size={16} />
              {loading ? "Refreshing..." : "Refresh research"}
            </Button>
          </div>
        </div>
        <div className="research-controls">
          <label className="sort-control research-company-control">
            <Search size={16} aria-hidden="true" />
            <span>Company</span>
            <select
              className="research-company-select"
              value={selectedInstrumentId}
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
                {[selectedResearchOption.ticker, selectedResearchOption.exchange].filter(Boolean).join(" · ")}
              </small>
            ) : null}
          </label>
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
            title={researchEmptyTitle(selectedPortfolioResearchCompany?.status)}
            message={researchEmptyMessage(selectedPortfolioResearchCompany?.status)}
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
          <span>{label}</span>
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

function getResearchSections(summary: ResearchSummary, filteredEvents: ResearchEvent[]) {
  const orders = eventsMatching(filteredEvents, ["NEW_ORDER", "ORDER_BACKLOG_CHANGE", "MAJOR_CONTRACT", "GOVERNMENT_CONTRACT"]);
  const capex = eventsMatching(filteredEvents, ["CAPEX", "CAPACITY_EXPANSION", "NEW_FACILITY", "FACTORY_EXPANSION", "PROJECT_DELAY"]);
  const customers = eventsMatching(filteredEvents, ["NEW_CUSTOMER", "CUSTOMER_EXPANSION", "MAJOR_CUSTOMER", "CUSTOMER_LOSS"]);
  const guidance = eventsMatching(filteredEvents, ["GUIDANCE_RAISED", "GUIDANCE_LOWERED", "GUIDANCE_CUT", "GUIDANCE_MAINTAINED", "REVENUE_GUIDANCE", "MARGIN_GUIDANCE"]);
  const growth = filteredEvents.filter((event) =>
    ["GEOGRAPHIC_EXPANSION", "PARTNERSHIP", "PRODUCT_LAUNCH"].includes(event.eventType)
  );

  return [
    { id: "overview" as const, title: "Overview", metricLabel: `Score ${summary.catalystScore.overallScore}`, events: filteredEvents },
    { id: "growth" as const, title: "Growth", metricLabel: categoryMetricLabel(summary, "Growth"), events: growth },
    { id: "orders" as const, title: "Orders & Backlog", metricLabel: categoryMetricLabel(summary, "Orders & Backlog"), events: orders },
    { id: "capex" as const, title: "CAPEX & Capacity", metricLabel: categoryMetricLabel(summary, "CAPEX & Capacity"), events: capex },
    { id: "customers" as const, title: "Customers", metricLabel: categoryMetricLabel(summary, "Customers"), events: customers },
    { id: "guidance" as const, title: "Guidance", metricLabel: categoryMetricLabel(summary, "Guidance"), events: guidance },
    { id: "news" as const, title: "News / Events", metricLabel: `Events ${filteredEvents.length}`, events: filteredEvents },
    { id: "sources" as const, title: "Sources", metricLabel: `Documents ${summary.documents.length}`, events: [] }
  ];
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
  if (status === "AVAILABLE") {
    return "positive";
  }
  if (status === "DEGRADED" || status === "RESEARCH_NOT_REFRESHED" || status === "NO_EVIDENCE") {
    return "warning";
  }
  if (status === "COMPANY_NOT_RESOLVED" || status === "RESEARCH_PROVIDER_UNAVAILABLE") {
    return "negative";
  }
  if (status === "RESEARCH_NOT_APPLICABLE") {
    return "info";
  }
  if (status?.startsWith("ETF_RESEARCH_")) {
    return status === "ETF_RESEARCH_AVAILABLE" ? "positive" : "info";
  }
  return "neutral";
}

function isResearchSummaryLoadable(status?: string | null) {
  return status === "AVAILABLE" || status === "DEGRADED";
}

function canRefreshResearch(status?: string | null) {
  return Boolean(status) && !status?.startsWith("ETF_RESEARCH_") && !["COMPANY_NOT_RESOLVED", "RESEARCH_NOT_APPLICABLE"].includes(status ?? "");
}

function researchEmptyTitle(status?: string | null) {
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
