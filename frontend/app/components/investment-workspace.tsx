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
  WalletCards,
  X
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { frontendConfig } from "../config";
import {
  type ApiFailure,
  type BrokerConnection,
  type BrokerProviderInfo,
  type Portfolio,
  type PortfolioListItem,
  type PortfolioPosition,
  type PortfolioSummary,
  type ResearchProfile,
  type ResearchSummary,
  brokerApi,
  portfolioApi,
  researchApi
} from "../lib/portfolio-api";
import { Badge, Button, Card, EmptyState, ErrorState, Field, MetricCard, Skeleton } from "./ui";

type View = "dashboard" | "portfolio" | "research" | "brokers" | "settings";
type Theme = "system" | "light" | "dark";
type SortKey = "company" | "ticker" | "marketValue" | "profitLoss" | "allocation";

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

function formatPercent(value?: number) {
  if (value === undefined || Number.isNaN(value)) {
    return "--";
  }

  return `${value.toFixed(2)}%`;
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

export function InvestmentWorkspace() {
  const [view, setView] = useState<View>("dashboard");
  const [theme, setTheme] = useState<Theme>("system");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [portfolios, setPortfolios] = useState<PortfolioListItem[]>([]);
  const [selectedPortfolio, setSelectedPortfolio] = useState<Portfolio | undefined>();
  const [selectedPortfolioId, setSelectedPortfolioId] = useState<string>("");
  const [summary, setSummary] = useState<PortfolioSummary | null>(null);
  const [positions, setPositions] = useState<PortfolioPosition[]>([]);
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
  const [researchCompanies, setResearchCompanies] = useState<ResearchProfile[]>([]);
  const [selectedResearchInstrumentId, setSelectedResearchInstrumentId] = useState("");
  const [researchSummary, setResearchSummary] = useState<ResearchSummary | null>(null);
  const [researchLoading, setResearchLoading] = useState(false);
  const [researchEventType, setResearchEventType] = useState("");
  const [researchImpact, setResearchImpact] = useState("");

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  useEffect(() => {
    let cancelled = false;

    async function loadPortfolios() {
      setLoading(true);
      setError(null);
      try {
        const loaded = await portfolioApi.listPortfolios();
        if (cancelled) {
          return;
        }
        setPortfolios(loaded);
        setSelectedPortfolioId((current) => current || loaded[0]?.portfolioId || "");
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

    void loadPortfolios();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;

    async function loadResearchCompanies() {
      try {
        const companies = await researchApi.listCompanies();
        if (!cancelled) {
          setResearchCompanies(companies);
          setSelectedResearchInstrumentId((current) => current || companies[0]?.instrumentId || "");
        }
      } catch {
        if (!cancelled) {
          setResearchCompanies([]);
        }
      }
    }

    void loadResearchCompanies();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;

    async function loadResearchSummary() {
      if (!selectedResearchInstrumentId) {
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
  }, [selectedResearchInstrumentId]);

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

    void loadBrokers();
    return () => {
      cancelled = true;
    };
  }, []);

  const sortedPositions = useMemo(() => {
    const normalizedSearch = searchText.trim().toLowerCase();
    return positions
      .filter((position) => {
        if (!normalizedSearch) {
          return true;
        }
        return [
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
            return a.instrument.companyName.localeCompare(b.instrument.companyName);
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
          <Badge tone="info">Demo data</Badge>
          <p>Phase 2B validates backend portfolio and broker APIs. Real providers remain not configured.</p>
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
            <Badge tone="warning">Demo data</Badge>
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
              <span>DEV</span>
              <ChevronDown size={16} aria-hidden="true" />
            </button>
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
              <p className="eyebrow">Phase 2B broker readiness</p>
              <h1>{view === "dashboard" ? "Portfolio command center" : navItems.find((item) => item.id === view)?.label}</h1>
              <p>
                Backend: <code>{frontendConfig.apiBaseUrl}</code>
              </p>
            </div>
            <PortfolioSelector
              portfolios={portfolios}
              selectedPortfolioId={selectedPortfolioId}
              onChange={setSelectedPortfolioId}
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
                <DashboardView
                  portfolio={selectedPortfolio}
                  summary={summary}
                  positions={positions}
                  onCreate={createPortfolio}
                  onSync={syncPortfolio}
                  creating={creating}
                  syncing={syncing}
                  newPortfolioName={newPortfolioName}
                  newPortfolioCurrency={newPortfolioCurrency}
                  setNewPortfolioName={setNewPortfolioName}
                  setNewPortfolioCurrency={setNewPortfolioCurrency}
                />
              ) : null}
              {view === "portfolio" ? (
                <PortfolioView
                  portfolio={selectedPortfolio}
                  summary={summary}
                  positions={sortedPositions}
                  rawPositions={positions}
                  searchText={searchText}
                  sortKey={sortKey}
                  onSearch={setSearchText}
                  onSort={setSortKey}
                  onCreate={createPortfolio}
                  onSync={syncPortfolio}
                  creating={creating}
                  syncing={syncing}
                  newPortfolioName={newPortfolioName}
                  newPortfolioCurrency={newPortfolioCurrency}
                  setNewPortfolioName={setNewPortfolioName}
                  setNewPortfolioCurrency={setNewPortfolioCurrency}
                />
              ) : null}
              {view === "brokers" ? (
                <BrokerView
                  providers={brokerProviders}
                  connections={brokerConnections}
                  loading={brokerLoading}
                  onConnectDemo={async () => {
                    setBrokerLoading(true);
                    try {
                      await brokerApi.connectMock();
                      setBrokerConnections(await brokerApi.listConnections());
                    } finally {
                      setBrokerLoading(false);
                    }
                  }}
                  onSyncConnection={async (connectionId) => {
                    setBrokerLoading(true);
                    try {
                      await brokerApi.syncConnection(connectionId);
                      setBrokerConnections(await brokerApi.listConnections());
                    } finally {
                      setBrokerLoading(false);
                    }
                  }}
                />
              ) : null}
              {view === "research" ? (
                <ResearchView
                  companies={researchCompanies}
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
  onSync: () => void;
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

  return (
    <div className="dashboard-grid">
      <section className="metrics-grid" aria-label="Portfolio summary">
        <MetricCard
          label="Portfolio value"
          value={formatMoney(summary?.totalMarketValue.amount, summary?.baseCurrency)}
          meta="Demo broker sync"
        />
        <MetricCard label="Total P/L" value={formatMoney(summary?.unrealizedProfitLoss.amount, summary?.baseCurrency)} tone={(summary?.unrealizedProfitLoss.amount ?? 0) >= 0 ? "positive" : "negative"} />
        <MetricCard label="Return" value={formatPercent(summary?.unrealizedProfitLossPercent)} tone={(summary?.unrealizedProfitLossPercent ?? 0) >= 0 ? "positive" : "negative"} />
        <MetricCard label="Cash" value={formatMoney(summary?.cash.amount, summary?.baseCurrency)} />
        <MetricCard label="Holdings" value={String(summary?.positions ?? 0)} />
        <MetricCard label="Portfolio risk" value="Pending" meta="Risk service not implemented" tone="warning" />
      </section>

      <Card className="wide-panel">
        <div className="panel-header">
          <div>
            <h2>{portfolio.name}</h2>
            <p>Last updated: {summary ? "from latest mock sync" : "not synced"} - Source: Broker demo data</p>
          </div>
          <Button onClick={onSync} disabled={syncing} variant="secondary">
            <RefreshCw size={16} />
            {syncing ? "Syncing..." : "Sync mock broker"}
          </Button>
        </div>
        {summary ? <AllocationCharts summary={summary} /> : <EmptyState title="No positions" message="This portfolio currently has no positions." />}
      </Card>

      <MovementPanel title="Top gainers" icon={TrendingUp} positions={topGainers} />
      <MovementPanel title="Top losers" icon={TrendingDown} positions={topLosers} />

      <Card className="wide-panel">
        <div className="panel-header">
          <div>
            <h2>AI opportunities</h2>
            <p>Recommendation logic is intentionally out of scope for Phase 2B.</p>
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
  positions
}: {
  title: string;
  icon: typeof TrendingUp;
  positions: PortfolioPosition[];
}) {
  return (
    <Card>
      <div className="panel-header compact">
        <h2>{title}</h2>
        <Icon size={18} aria-hidden="true" />
      </div>
      {positions.length === 0 ? (
        <EmptyState title="No synced positions" message="Sync a mock broker account to populate this view." />
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
  setNewPortfolioCurrency
}: {
  portfolio?: Portfolio;
  summary: PortfolioSummary | null;
  positions: PortfolioPosition[];
  rawPositions: PortfolioPosition[];
  searchText: string;
  sortKey: SortKey;
  onSearch: (value: string) => void;
  onSort: (value: SortKey) => void;
  onCreate: () => void;
  onSync: () => void;
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

  return (
    <div className="portfolio-layout">
      <section className="metrics-grid" aria-label="Portfolio totals">
        <MetricCard label="Total market value" value={formatMoney(summary?.totalMarketValue.amount, summary?.baseCurrency)} />
        <MetricCard label="Total cost" value={formatMoney(summary?.totalCostBasis.amount, summary?.baseCurrency)} />
        <MetricCard label="Unrealized P/L" value={formatMoney(summary?.unrealizedProfitLoss.amount, summary?.baseCurrency)} tone={(summary?.unrealizedProfitLoss.amount ?? 0) >= 0 ? "positive" : "negative"} />
        <MetricCard label="Return" value={formatPercent(summary?.unrealizedProfitLossPercent)} />
        <MetricCard label="Cash" value={formatMoney(summary?.cash.amount, summary?.baseCurrency)} />
        <MetricCard label="Position count" value={String(summary?.positions ?? 0)} />
      </section>

      <Card className="wide-panel">
        <div className="panel-header">
          <div>
            <h2>Holdings</h2>
            <p>Search respects ticker, company, ISIN, exchange, and country.</p>
          </div>
          <Button onClick={onSync} disabled={syncing} variant="secondary">
            <RefreshCw size={16} />
            {syncing ? "Syncing..." : "Sync mock broker"}
          </Button>
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
              <option value="marketValue">Market value</option>
              <option value="profitLoss">P/L</option>
              <option value="allocation">Allocation</option>
              <option value="company">Company</option>
              <option value="ticker">Ticker</option>
            </select>
          </label>
        </div>
        {rawPositions.length === 0 ? (
          <EmptyState title="No positions" message="This portfolio currently has no positions." action={<Button onClick={onSync}>Sync mock broker</Button>} />
        ) : (
          <HoldingsTable positions={positions} summary={summary} />
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

function FreshnessBadge({ freshness }: { freshness: string }) {
  const label = freshness === "END_OF_DAY" ? "EOD" : freshness === "MOCK" ? "DEMO" : freshness.replaceAll("_", "-");
  const tone = freshness === "REAL_TIME" ? "positive" : freshness === "STALE" ? "warning" : freshness === "MOCK" ? "info" : freshness === "UNAVAILABLE" ? "negative" : "neutral";
  return <Badge tone={tone}>{label}</Badge>;
}

function HoldingsTable({ positions, summary }: { positions: PortfolioPosition[]; summary: PortfolioSummary | null }) {
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
            <th>Current price</th>
            <th>Last price</th>
            <th>Bid</th>
            <th>Ask</th>
            <th>Market value</th>
            <th>P/L</th>
            <th>P/L %</th>
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
                    <strong>{position.instrument.companyName}</strong>
                    <span>{position.instrument.isin ?? "No ISIN"}</span>
                  </button>
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
                <td>{position.quote?.freshness ? <FreshnessBadge freshness={position.quote.freshness} /> : <FreshnessBadge freshness="UNAVAILABLE" />}</td>
                <td>{position.quote?.receivedAt ? new Date(position.quote.receivedAt).toLocaleString() : position.quote?.timestamp ? new Date(position.quote.timestamp).toLocaleString() : "Unavailable"}</td>
                <td>{position.quote?.source ?? "--"}</td>
                <td>{position.brokerAccountId}</td>
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
  loading,
  onConnectDemo,
  onSyncConnection
}: {
  providers: BrokerProviderInfo[];
  connections: BrokerConnection[];
  loading: boolean;
  onConnectDemo: () => Promise<void>;
  onSyncConnection: (connectionId: string) => Promise<void>;
}) {
  return (
    <div className="broker-grid">
      {providers.map((provider) => {
        const providerConnections = connections.filter((connection) => connection.brokerType === provider.brokerType);
        const isMock = provider.brokerType === "MOCK";
        const activeConnection = providerConnections[0];
        const providerTone = isMock ? "info" : provider.officialProviderSetupRequired ? "warning" : provider.providerStatus === "CONNECTED" ? "positive" : "neutral";
        return (
        <Card className="broker-card" as="article" key={provider.brokerType}>
          <div className="broker-icon">
            <Building2 size={22} aria-hidden="true" />
          </div>
          <div>
            <div className="panel-header compact">
              <h2>{brokerDisplayName(provider.brokerType)}</h2>
              <Badge tone={providerTone}>{isMock ? "DEMO" : provider.providerStatus}</Badge>
              <Badge tone="positive">Read-only</Badge>
            </div>
            <dl className="broker-facts">
              <div>
                <dt>Connection status</dt>
                <dd>{activeConnection?.status ?? provider.code}</dd>
              </div>
              <div>
                <dt>Connection method</dt>
                <dd>{provider.connectionMethod}</dd>
              </div>
              <div>
                <dt>Last sync</dt>
                <dd>{activeConnection?.lastSuccessfulSyncAt ? new Date(activeConnection.lastSuccessfulSyncAt).toLocaleString() : "Not synced"}</dd>
              </div>
              <div>
                <dt>Account reference</dt>
                <dd>{activeConnection?.externalAccountReference ?? "Unavailable"}</dd>
              </div>
              <div>
                <dt>Data freshness</dt>
                <dd>{activeConnection?.dataFreshness ?? provider.dataFreshness}</dd>
              </div>
              <div>
                <dt>Capabilities</dt>
                <dd>{provider.capabilities.length > 0 ? provider.capabilities.join(", ") : "None"}</dd>
              </div>
            </dl>
            {provider.officialProviderSetupRequired ? (
              <p className="broker-note">{provider.providerStatus === "DOCUMENTATION_REQUIRED" ? "Documentation required" : "Not configured"}</p>
            ) : null}
            {isMock && providerConnections.length === 0 ? (
              <Button variant="secondary" onClick={onConnectDemo} disabled={loading}>
                {loading ? "Connecting..." : "Connect Demo Broker"}
              </Button>
            ) : null}
            {isMock && providerConnections[0] ? (
              <Button variant="secondary" onClick={() => onSyncConnection(providerConnections[0].connectionId)} disabled={loading}>
                {loading ? "Syncing..." : "Sync connection"}
              </Button>
            ) : null}
          </div>
        </Card>
      );
      })}
      <Card className="wide-panel">
        <EmptyState title="Real broker connections are not enabled" message="Interactive Brokers and ICICI Direct explicitly report provider not configured. The UI never asks for broker passwords." />
      </Card>
    </div>
  );
}

function ResearchView({
  companies,
  selectedInstrumentId,
  onSelectInstrument,
  summary,
  loading,
  eventType,
  impact,
  onEventType,
  onImpact,
  onRefresh
}: {
  companies: ResearchProfile[];
  selectedInstrumentId: string;
  onSelectInstrument: (value: string) => void;
  summary: ResearchSummary | null;
  loading: boolean;
  eventType: string;
  impact: string;
  onEventType: (value: string) => void;
  onImpact: (value: string) => void;
  onRefresh: () => Promise<void>;
}) {
  const filteredEvents = (summary?.recentEvents ?? []).filter((event) => {
    return (!eventType || event.eventType === eventType) && (!impact || event.impact === impact);
  });
  const eventTypes = [...new Set((summary?.recentEvents ?? []).map((event) => event.eventType))].sort();
  const impacts = [...new Set((summary?.recentEvents ?? []).map((event) => event.impact))].sort();

  return (
    <div className="research-layout">
      <Card className="wide-panel">
        <div className="panel-header">
          <div>
            <h2>Research intelligence</h2>
            <p>Structured evidence, catalyst scoring, and source citations. Final BUY/SELL recommendations are not implemented.</p>
          </div>
          <div className="research-actions">
            {summary?.demo ? <Badge tone="info">DEMO</Badge> : null}
            <Button variant="secondary" onClick={onRefresh} disabled={loading || !selectedInstrumentId}>
              <RefreshCw size={16} />
              {loading ? "Refreshing..." : "Refresh research"}
            </Button>
          </div>
        </div>
        <div className="research-controls">
          <label className="sort-control">
            <Search size={16} aria-hidden="true" />
            <span>Company</span>
            <select value={selectedInstrumentId} onChange={(event) => onSelectInstrument(event.target.value)}>
              {companies.map((company) => (
                <option key={company.instrumentId} value={company.instrumentId}>
                  {company.ticker} · {company.exchange} · {company.companyName}
                </option>
              ))}
            </select>
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
                  {summary.profile.ticker} · {summary.profile.exchange} · {summary.profile.country} · {summary.profile.isin ?? "No ISIN"}
                </p>
              </div>
              <Badge tone="neutral">No BUY/SELL rating</Badge>
            </div>
            <div className="research-tabs" role="tablist" aria-label="Research sections">
              {["Overview", "Growth", "Orders & Backlog", "CAPEX & Capacity", "Customers", "Guidance", "News / Events", "Sources"].map((tab) => (
                <span role="tab" aria-selected={tab === "Overview"} key={tab}>
                  {tab}
                </span>
              ))}
            </div>
            <div className="score-grid">
              {Object.entries(summary.catalystScore.buckets).map(([label, value]) => (
                <div className="score-row" key={label}>
                  <span>{label}</span>
                  <strong>{value}</strong>
                  <div className="bar-track" aria-hidden="true">
                    <span style={{ width: `${value}%` }} />
                  </div>
                </div>
              ))}
            </div>
          </Card>

          <section className="event-grid" aria-label="Research events">
            {filteredEvents.length === 0 ? (
              <Card className="wide-panel">
                <EmptyState title="No matching events" message="Change the filters or refresh research fixtures." />
              </Card>
            ) : (
              filteredEvents.map((event) => <ResearchEventCard event={event} key={event.eventId} />)
            )}
          </section>

          <Card className="wide-panel">
            <div className="panel-header">
              <div>
                <h2>Sources</h2>
                <p>Traceable citations for the extracted facts. Raw page bodies are not displayed.</p>
              </div>
            </div>
            <div className="source-list">
              {summary.documents.map((document) => (
                <a href={document.canonicalUrl} target="_blank" rel="noreferrer" key={document.documentId}>
                  <strong>{document.title ?? document.publisher ?? document.sourceName}</strong>
                  <span>{document.sourceType} · {document.reliabilityLevel} · {document.publishedAt ? new Date(document.publishedAt).toLocaleDateString() : "No publication date"}</span>
                </a>
              ))}
            </div>
          </Card>
        </>
      ) : null}

      {!loading && !summary ? (
        <Card className="wide-panel">
          <EmptyState title="No research profile loaded" message="The research API is unavailable or has no fixture profiles." />
        </Card>
      ) : null}
    </div>
  );
}

function ResearchEventCard({ event }: { event: import("../lib/portfolio-api").ResearchEvent }) {
  const impactTone = event.impact.includes("NEGATIVE") ? "negative" : event.impact.includes("POSITIVE") ? "positive" : event.impact === "UNCERTAIN" ? "warning" : "neutral";
  return (
    <Card className="research-event-card" as="article">
      <div className="panel-header compact">
        <div>
          <Badge tone="neutral">{event.eventType.replaceAll("_", " ")}</Badge>
          <h2>{event.title}</h2>
        </div>
        <Badge tone={impactTone}>{event.impact.replaceAll("_", " ")}</Badge>
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
          <dd>{event.sourceType}</dd>
        </div>
      </dl>
      <blockquote>{event.rawEvidenceReference}</blockquote>
      <a className="source-link" href={event.sourceUrl} target="_blank" rel="noreferrer">
        Open source
      </a>
    </Card>
  );
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
          <p>Portfolio risk presentation is prepared, but risk scoring is not implemented in Phase 2B.</p>
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
