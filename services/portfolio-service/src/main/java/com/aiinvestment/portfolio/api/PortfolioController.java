package com.aiinvestment.portfolio.api;

import com.aiinvestment.portfolio.application.PortfolioService;
import com.aiinvestment.portfolio.application.PortfolioImportService;
import com.aiinvestment.portfolio.application.ManualMarketPriceService;
import com.aiinvestment.portfolio.application.InstrumentMasterService;
import com.aiinvestment.portfolio.infrastructure.persistence.AppUserProvisioner;
import com.aiinvestment.shared.web.auth.AuthenticatedUserResolver;
import jakarta.servlet.http.HttpServletRequest;
import com.aiinvestment.shared.domain.market.MarketDataProvider;
import io.swagger.v3.oas.annotations.Operation;
import jakarta.validation.Valid;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.UUID;
import java.time.Instant;
import com.aiinvestment.portfolio.application.BrokerSyncAuthenticationRequiredException;

@RestController
@RequestMapping("/api/v1/portfolios")
public class PortfolioController {
    private final PortfolioService portfolioService;
    private final MarketDataProvider marketDataProvider;
    private final AppUserProvisioner appUserProvisioner;
    private final PortfolioImportService portfolioImportService;
    private final ManualMarketPriceService manualMarketPriceService;
    private final InstrumentMasterService instrumentMasterService;

    public PortfolioController(PortfolioService portfolioService, MarketDataProvider marketDataProvider,
                               AppUserProvisioner appUserProvisioner, PortfolioImportService portfolioImportService,
                               ManualMarketPriceService manualMarketPriceService, InstrumentMasterService instrumentMasterService) {
        this.portfolioService = portfolioService;
        this.marketDataProvider = marketDataProvider;
        this.appUserProvisioner = appUserProvisioner;
        this.portfolioImportService = portfolioImportService;
        this.manualMarketPriceService = manualMarketPriceService;
        this.instrumentMasterService = instrumentMasterService;
    }

    @PostMapping(value = "/imports/{broker}/preview", consumes = org.springframework.http.MediaType.MULTIPART_FORM_DATA_VALUE)
    @Operation(summary = "Preview a private broker portfolio CSV without persisting it")
    public PortfolioImportPreviewResponse previewImport(@PathVariable("broker") String broker,
            @RequestPart("file") org.springframework.web.multipart.MultipartFile file,
            @RequestParam(name = "portfolioName", required = false) String portfolioName,
            HttpServletRequest servletRequest) {
        var user = AuthenticatedUserResolver.require(servletRequest);
        appUserProvisioner.upsert(user);
        return portfolioImportService.preview(user.userId(), brokerType(broker), file, portfolioName);
    }

    @PostMapping(value = "/imports/{broker}/confirm", consumes = org.springframework.http.MediaType.MULTIPART_FORM_DATA_VALUE)
    @Operation(summary = "Transactionally import or update a manual broker portfolio")
    public PortfolioImportResultResponse confirmImport(@PathVariable("broker") String broker,
            @RequestPart("file") org.springframework.web.multipart.MultipartFile file,
            @RequestParam(name = "portfolioName", required = false) String portfolioName,
            HttpServletRequest servletRequest) {
        var user = AuthenticatedUserResolver.require(servletRequest);
        appUserProvisioner.upsert(user);
        return portfolioImportService.confirm(user.userId(), brokerType(broker), file, portfolioName);
    }

    private com.aiinvestment.shared.domain.broker.BrokerType brokerType(String broker) {
        try {
            return com.aiinvestment.shared.domain.broker.BrokerType.valueOf(
                    broker.toUpperCase(java.util.Locale.ROOT).replace('-', '_'));
        } catch (RuntimeException exception) {
            throw new IllegalArgumentException("Unsupported broker import type");
        }
    }

    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    @Operation(summary = "Create a portfolio")
    public PortfolioResponse create(@Valid @RequestBody CreatePortfolioRequest request, HttpServletRequest servletRequest) {
        var user = AuthenticatedUserResolver.require(servletRequest);
        appUserProvisioner.upsert(user);
        return PortfolioResponse.from(portfolioService.createPortfolio(user.userId(), request.name(), request.baseCurrency()));
    }

    @GetMapping
    @Operation(summary = "List portfolios for the authenticated user")
    public List<PortfolioListItemResponse> list(HttpServletRequest servletRequest) {
        var user = AuthenticatedUserResolver.require(servletRequest);
        appUserProvisioner.upsert(user);
        return portfolioService.listPortfolioSummaries(user.userId()).stream()
                .filter(summary -> PortfolioPresentationName.visibleInManagedNavigation(
                        portfolioService.getPortfolio(user.userId(), summary.portfolioId())))
                .map(summary -> PortfolioListItemResponse.from(portfolioService.getPortfolio(user.userId(), summary.portfolioId()), summary))
                .toList();
    }

    @GetMapping("/dashboard")
    @Operation(summary = "Load persisted multi-portfolio dashboard without broker authentication")
    public PortfolioDashboardResponse dashboard(HttpServletRequest servletRequest) {
        var user = AuthenticatedUserResolver.require(servletRequest);
        appUserProvisioner.upsert(user);
        List<PortfolioListItemResponse> portfolios = list(servletRequest);
        List<com.aiinvestment.portfolio.domain.PortfolioPosition> rawHoldings = portfolios.stream()
                .flatMap(portfolio -> portfolioService.getPositions(user.userId(), portfolio.portfolioId()).stream())
                .filter(com.aiinvestment.portfolio.domain.PortfolioPosition::active).toList();
        List<PortfolioPositionResponse> holdings = rawHoldings.stream()
                .map(position -> withMaster(PortfolioPositionResponse.from(position,
                        QuoteResponse.from(marketDataProvider.getQuote(position.instrument())))))
                .toList();
        return new PortfolioDashboardResponse(portfolioService.currencyTotals(user.userId()), portfolios, holdings,
                CombinedPortfolioHoldingResponse.aggregate(rawHoldings),
                portfolios.stream().filter(portfolio -> !portfolio.valuationComplete())
                        .map(portfolio -> portfolio.portfolioId().toString()).toList());
    }

    @GetMapping("/{portfolioId}")
    @Operation(summary = "Get a portfolio")
    public PortfolioResponse get(@PathVariable("portfolioId") UUID portfolioId, HttpServletRequest servletRequest) {
        var user = AuthenticatedUserResolver.require(servletRequest);
        appUserProvisioner.upsert(user);
        return PortfolioResponse.from(portfolioService.getPortfolio(user.userId(), portfolioId));
    }

    @GetMapping("/{portfolioId}/positions")
    @Operation(summary = "Get portfolio positions")
    public List<PortfolioPositionResponse> positions(@PathVariable("portfolioId") UUID portfolioId, HttpServletRequest servletRequest) {
        var user = AuthenticatedUserResolver.require(servletRequest);
        appUserProvisioner.upsert(user);
        return portfolioService.getPositions(user.userId(), portfolioId).stream()
                .map(position -> withMaster(PortfolioPositionResponse.from(position,
                        QuoteResponse.from(marketDataProvider.getQuote(position.instrument())))))
                .toList();
    }

    @PostMapping("/{portfolioId}/prices/refresh")
    @Operation(summary = "Refresh cached public market prices for a manual Indian portfolio without changing positions")
    public ManualMarketPriceService.RefreshResult refreshPrices(@PathVariable("portfolioId") UUID portfolioId,
                                                                 HttpServletRequest servletRequest) {
        var user = AuthenticatedUserResolver.require(servletRequest);
        appUserProvisioner.upsert(user);
        return manualMarketPriceService.refresh(user.userId(), portfolioId);
    }

    private PortfolioPositionResponse withMaster(PortfolioPositionResponse response) {
        UUID id=instrumentMasterService.ensureMaster(response.instrument().instrumentId());
        var global = instrumentMasterService.globalInstrument(id)
                .orElseThrow(() -> new IllegalStateException("Linked instrument master was not found"));
        var mappings=global.providerMappings().stream().map(InstrumentProviderMappingResponse::from).toList();
        return response.withMaster(global.master(),mappings);
    }

    @PutMapping("/{portfolioId}/positions/{positionId}/display-name")
    @Operation(summary = "Set or clear a display-name override for a manually imported holding")
    public PortfolioPositionResponse updateHoldingDisplayName(@PathVariable("portfolioId") UUID portfolioId,
            @PathVariable("positionId") UUID positionId,
            @RequestBody UpdateHoldingDisplayNameRequest request,
            HttpServletRequest servletRequest) {
        var user = AuthenticatedUserResolver.require(servletRequest);
        appUserProvisioner.upsert(user);
        return PortfolioPositionResponse.from(portfolioService.updateCustomDisplayName(
                user.userId(), portfolioId, positionId, request.customDisplayName()));
    }

    @GetMapping("/{portfolioId}/summary")
    @Operation(summary = "Get portfolio summary")
    public PortfolioSummaryResponse summary(@PathVariable("portfolioId") UUID portfolioId, HttpServletRequest servletRequest) {
        var user = AuthenticatedUserResolver.require(servletRequest);
        appUserProvisioner.upsert(user);
        return PortfolioSummaryResponse.from(portfolioService.getSummary(user.userId(), portfolioId));
    }

    @GetMapping("/{portfolioId}/history")
    @Operation(summary = "Get portfolio valuation history")
    public PortfolioHistoryResponse history(@PathVariable("portfolioId") UUID portfolioId,
                                            @RequestParam(name = "range", defaultValue = "1M") String range,
                                            HttpServletRequest servletRequest) {
        var user = AuthenticatedUserResolver.require(servletRequest);
        appUserProvisioner.upsert(user);
        return PortfolioHistoryResponse.from(portfolioService.getHistory(user.userId(), portfolioId, range));
    }

    @PostMapping("/{portfolioId}/sync")
    @Operation(summary = "Sync portfolio from the Phase 2B mock broker provider")
    public PortfolioSummaryResponse sync(@PathVariable("portfolioId") UUID portfolioId, HttpServletRequest servletRequest) {
        var user = AuthenticatedUserResolver.require(servletRequest);
        appUserProvisioner.upsert(user);
        return PortfolioSummaryResponse.from(portfolioService.sync(user.userId(), portfolioId));
    }

    @PostMapping("/{portfolioId}/broker-connections/{connectionId}/import")
    @Operation(summary = "Import read-only broker positions into an owned portfolio")
    public PortfolioSummaryResponse importBrokerConnection(@PathVariable("portfolioId") UUID portfolioId,
                                                           @PathVariable("connectionId") UUID connectionId,
                                                           HttpServletRequest servletRequest) {
        var user = AuthenticatedUserResolver.require(servletRequest);
        appUserProvisioner.upsert(user);
        return PortfolioSummaryResponse.from(portfolioService.importBrokerConnection(user, portfolioId, connectionId));
    }

    @PostMapping("/broker-connections/{connectionId}/sync")
    @Operation(summary = "Synchronize a broker connection and create or reuse account-scoped portfolios")
    public BrokerPortfolioSyncResponse syncBrokerConnection(@PathVariable("connectionId") UUID connectionId,
                                                            HttpServletRequest servletRequest) {
        var user = AuthenticatedUserResolver.require(servletRequest);
        appUserProvisioner.upsert(user);
        try {
            var summaries = portfolioService.syncBrokerConnection(user, connectionId);
            var portfolios = summaries.stream()
                    .map(summary -> PortfolioListItemResponse.from(
                            portfolioService.getPortfolio(user.userId(), summary.portfolioId()), summary))
                    .toList();
            String provider = portfolios.stream().map(PortfolioListItemResponse::provider)
                    .filter(java.util.Objects::nonNull).findFirst().orElse("UNKNOWN");
            return new BrokerPortfolioSyncResponse(connectionId, provider, "CONNECTED", "NONE",
                    "Broker synchronization completed", Instant.now(), portfolios);
        } catch (BrokerSyncAuthenticationRequiredException exception) {
            return new BrokerPortfolioSyncResponse(connectionId, null, "AUTHENTICATION_REQUIRED",
                    "REDIRECT_REQUIRED", exception.getMessage(), Instant.now(), List.of());
        }
    }
}
