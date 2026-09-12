package com.aiinvestment.portfolio.api;

import com.aiinvestment.portfolio.application.InstrumentMasterService;
import com.aiinvestment.portfolio.application.MarketRegion;
import com.aiinvestment.portfolio.application.WatchlistService;
import com.aiinvestment.portfolio.infrastructure.persistence.AppUserProvisioner;
import com.aiinvestment.portfolio.infrastructure.persistence.WatchlistEntity;
import com.aiinvestment.portfolio.infrastructure.persistence.WatchlistMembershipEntity;
import com.aiinvestment.shared.web.auth.AuthenticatedUserResolver;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotNull;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.List;
import java.util.UUID;

@RestController
@RequestMapping("/api/v1/watchlists")
public class WatchlistController {
    private final WatchlistService watchlists;
    private final InstrumentMasterService instruments;
    private final AppUserProvisioner users;

    public WatchlistController(WatchlistService watchlists, InstrumentMasterService instruments,
                               AppUserProvisioner users) {
        this.watchlists = watchlists;
        this.instruments = instruments;
        this.users = users;
    }

    @GetMapping
    public List<WatchlistResponse> list(HttpServletRequest request) {
        var user = authenticated(request);
        return watchlists.list(user.userId()).stream().map(this::response).toList();
    }

    @GetMapping("/default")
    public WatchlistResponse defaultWatchlist(@RequestParam(name = "region") String region,
                                              HttpServletRequest request) {
        var user = authenticated(request);
        return response(watchlists.defaultWatchlist(user.userId(), MarketRegion.parse(region)));
    }

    @PostMapping("/default/ensure")
    public WatchlistResponse ensureDefault(@Valid @RequestBody EnsureDefaultWatchlistRequest payload,
                                           HttpServletRequest request) {
        var user = authenticated(request);
        return response(watchlists.ensureDefault(user.userId(), MarketRegion.parse(payload.region())));
    }

    @GetMapping("/{watchlistId}")
    public WatchlistDetailResponse get(@PathVariable("watchlistId") UUID watchlistId,
                                       HttpServletRequest request) {
        var user = authenticated(request);
        var value = watchlists.get(user.userId(), watchlistId);
        return new WatchlistDetailResponse(
                response(value.watchlist()),
                value.memberships().stream().map(this::membershipResponse).toList());
    }

    @PostMapping("/{watchlistId}/instruments")
    public WatchlistMembershipResponse add(@PathVariable("watchlistId") UUID watchlistId,
                                           @Valid @RequestBody AddWatchlistInstrumentRequest payload,
                                           HttpServletRequest request) {
        var user = authenticated(request);
        return membershipResponse(watchlists.add(
                user.userId(), watchlistId, payload.globalInstrumentId(),
                payload.sourcePeriod(), payload.sourcePerformancePct()));
    }

    @DeleteMapping("/{watchlistId}/instruments/{globalInstrumentId}")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    public void remove(@PathVariable("watchlistId") UUID watchlistId,
                       @PathVariable("globalInstrumentId") UUID globalInstrumentId,
                       HttpServletRequest request) {
        var user = authenticated(request);
        watchlists.remove(user.userId(), watchlistId, globalInstrumentId);
    }

    private com.aiinvestment.shared.web.auth.AuthenticatedUser authenticated(HttpServletRequest request) {
        var user = AuthenticatedUserResolver.require(request);
        users.upsert(user);
        return user;
    }

    private WatchlistResponse response(WatchlistEntity value) {
        return new WatchlistResponse(value.getWatchlistId(), value.getName(), value.getRegion().name(),
                value.isSystemDefault(), watchlists.instrumentCount(value.getWatchlistId()),
                value.getCreatedAt(), value.getUpdatedAt());
    }

    private WatchlistMembershipResponse membershipResponse(WatchlistMembershipEntity value) {
        GlobalInstrumentResponse instrument = instruments.globalInstrument(value.getGlobalInstrumentId())
                .map(GlobalInstrumentResponse::from)
                .orElseThrow(() -> new IllegalArgumentException("GLOBAL_INSTRUMENT_NOT_FOUND"));
        return new WatchlistMembershipResponse(
                value.getGlobalInstrumentId(), instrument, value.getSourcePeriod(),
                value.getSourcePerformancePct(), value.getAddedAt(), value.getUpdatedAt());
    }

    public record EnsureDefaultWatchlistRequest(@NotNull String region) {}
    public record AddWatchlistInstrumentRequest(
            @NotNull UUID globalInstrumentId,
            String sourcePeriod,
            BigDecimal sourcePerformancePct
    ) {}
    public record WatchlistResponse(
            UUID watchlistId,
            String name,
            String region,
            boolean systemDefault,
            long instrumentCount,
            Instant createdAt,
            Instant updatedAt
    ) {}
    public record WatchlistMembershipResponse(
            UUID globalInstrumentId,
            GlobalInstrumentResponse instrument,
            String sourcePeriod,
            BigDecimal sourcePerformancePct,
            Instant addedAt,
            Instant updatedAt
    ) {}
    public record WatchlistDetailResponse(WatchlistResponse watchlist, List<WatchlistMembershipResponse> instruments) {}
}
