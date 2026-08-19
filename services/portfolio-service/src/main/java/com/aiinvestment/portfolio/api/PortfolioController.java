package com.aiinvestment.portfolio.api;

import com.aiinvestment.portfolio.application.PortfolioService;
import com.aiinvestment.shared.domain.market.MarketDataProvider;
import io.swagger.v3.oas.annotations.Operation;
import jakarta.validation.Valid;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.UUID;

@RestController
@RequestMapping("/api/v1/portfolios")
public class PortfolioController {
    private final PortfolioService portfolioService;
    private final MarketDataProvider marketDataProvider;

    public PortfolioController(PortfolioService portfolioService, MarketDataProvider marketDataProvider) {
        this.portfolioService = portfolioService;
        this.marketDataProvider = marketDataProvider;
    }

    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    @Operation(summary = "Create a portfolio")
    public PortfolioResponse create(@Valid @RequestBody CreatePortfolioRequest request) {
        return PortfolioResponse.from(portfolioService.createPortfolio(request.name(), request.baseCurrency()));
    }

    @GetMapping
    @Operation(summary = "List portfolios for the current Phase 2B validation user")
    public List<PortfolioListItemResponse> list() {
        return portfolioService.listPortfolioSummaries().stream()
                .map(summary -> PortfolioListItemResponse.from(portfolioService.getPortfolio(summary.portfolioId()), summary))
                .toList();
    }

    @GetMapping("/{portfolioId}")
    @Operation(summary = "Get a portfolio")
    public PortfolioResponse get(@PathVariable("portfolioId") UUID portfolioId) {
        return PortfolioResponse.from(portfolioService.getPortfolio(portfolioId));
    }

    @GetMapping("/{portfolioId}/positions")
    @Operation(summary = "Get portfolio positions")
    public List<PortfolioPositionResponse> positions(@PathVariable("portfolioId") UUID portfolioId) {
        return portfolioService.getPositions(portfolioId).stream()
                .map(position -> PortfolioPositionResponse.from(position, QuoteResponse.from(marketDataProvider.getQuote(position.instrument()))))
                .toList();
    }

    @GetMapping("/{portfolioId}/summary")
    @Operation(summary = "Get portfolio summary")
    public PortfolioSummaryResponse summary(@PathVariable("portfolioId") UUID portfolioId) {
        return PortfolioSummaryResponse.from(portfolioService.getSummary(portfolioId));
    }

    @PostMapping("/{portfolioId}/sync")
    @Operation(summary = "Sync portfolio from the Phase 2B mock broker provider")
    public PortfolioSummaryResponse sync(@PathVariable("portfolioId") UUID portfolioId) {
        return PortfolioSummaryResponse.from(portfolioService.sync(portfolioId));
    }
}
