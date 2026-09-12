package com.aiinvestment.portfolio.application;

import com.aiinvestment.portfolio.infrastructure.persistence.AppUserProvisioner;
import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterEntity;
import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterRepository;
import com.aiinvestment.portfolio.infrastructure.persistence.PortfolioPositionRepository;
import com.aiinvestment.portfolio.infrastructure.persistence.WatchlistMembershipRepository;
import com.aiinvestment.portfolio.infrastructure.persistence.WatchlistRepository;
import com.aiinvestment.shared.domain.AssetType;
import com.aiinvestment.shared.web.auth.AuthenticatedUser;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.List;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

@SpringBootTest
@ActiveProfiles("test")
class WatchlistServiceIntegrationTest {
    @Autowired private WatchlistService service;
    @Autowired private WatchlistRepository watchlists;
    @Autowired private WatchlistMembershipRepository memberships;
    @Autowired private InstrumentMasterRepository instruments;
    @Autowired private PortfolioPositionRepository positions;
    @Autowired private AppUserProvisioner users;

    @Test
    void createsOneLazyDefaultPerUserAndRegionWithCanonicalNames() {
        UUID userId = user();

        var india = service.ensureDefault(userId, MarketRegion.INDIA);
        var indiaAgain = service.ensureDefault(userId, MarketRegion.INDIA);
        var europe = service.ensureDefault(userId, MarketRegion.EUROPE);
        var usa = service.ensureDefault(userId, MarketRegion.USA);

        assertThat(indiaAgain.getWatchlistId()).isEqualTo(india.getWatchlistId());
        assertThat(service.list(userId)).extracting(value -> value.getName())
                .containsExactly("WATCHLIST-EU", "WATCHLIST-IND", "WATCHLIST-USA");
        assertThat(watchlists.findByUserIdOrderByRegionAscNameAsc(userId)).hasSize(3);
        assertThat(europe.getRegion()).isEqualTo(MarketRegion.EUROPE);
        assertThat(usa.getRegion()).isEqualTo(MarketRegion.USA);
    }

    @Test
    void membershipIsCanonicalIdempotentRemovableAndNeverCreatesAPosition() {
        UUID userId = user();
        UUID instrumentId = instrument("IN", "NSE", "INR", "PFOCUS");
        UUID watchlistId = service.ensureDefault(userId, MarketRegion.INDIA).getWatchlistId();
        long positionCount = positions.count();

        var first = service.add(userId, watchlistId, instrumentId, "WEEK", new BigDecimal("13.43"));
        var duplicate = service.add(userId, watchlistId, instrumentId, "MONTH", new BigDecimal("7.10"));

        assertThat(duplicate.getMembershipId()).isEqualTo(first.getMembershipId());
        assertThat(memberships.findByWatchlistIdOrderByAddedAtAscGlobalInstrumentIdAsc(watchlistId))
                .singleElement().satisfies(value -> {
                    assertThat(value.getGlobalInstrumentId()).isEqualTo(instrumentId);
                    assertThat(value.getSourcePeriod()).isEqualTo("MONTH");
                    assertThat(value.getSourcePerformancePct()).isEqualByComparingTo("7.10");
                });
        assertThat(positions.count()).isEqualTo(positionCount);

        service.remove(userId, watchlistId, instrumentId);
        assertThat(memberships.countByWatchlistId(watchlistId)).isZero();
        assertThat(positions.count()).isEqualTo(positionCount);
    }

    @Test
    void ownershipAndAllCrossRegionMembershipsFailClosed() {
        UUID owner = user();
        UUID otherUser = user();
        var byRegion = java.util.Map.of(
                MarketRegion.INDIA, instrument("IN", "NSE", "INR", "IND"),
                MarketRegion.EUROPE, instrument("DE", "XETR", "EUR", "EU"),
                MarketRegion.USA, instrument("US", "XNAS", "USD", "USA")
        );
        var watchlistByRegion = java.util.Arrays.stream(MarketRegion.values()).collect(
                java.util.stream.Collectors.toMap(region -> region,
                        region -> service.ensureDefault(owner, region).getWatchlistId()));

        assertThatThrownBy(() -> service.get(otherUser, watchlistByRegion.get(MarketRegion.INDIA)))
                .isInstanceOf(WatchlistNotFoundException.class);

        for (var instrument : byRegion.entrySet()) {
            for (MarketRegion destination : MarketRegion.values()) {
                if (destination == instrument.getKey()) continue;
                assertThatThrownBy(() -> service.add(owner, watchlistByRegion.get(destination),
                        instrument.getValue(), "WEEK", BigDecimal.ONE))
                        .isInstanceOf(WatchlistRegionMismatchException.class)
                        .hasMessage("WATCHLIST_REGION_MISMATCH");
            }
        }
        assertThat(watchlistByRegion.values())
                .allSatisfy(watchlistId -> assertThat(memberships.countByWatchlistId(watchlistId)).isZero());
    }

    private UUID user() {
        UUID id = UUID.randomUUID();
        users.upsert(new AuthenticatedUser(id, "watchlist-test", "subject-" + id,
                null, "Watchlist Test", List.of("USER")));
        return id;
    }

    private UUID instrument(String country, String exchange, String currency, String symbol) {
        UUID id = UUID.randomUUID();
        instruments.saveAndFlush(new InstrumentMasterEntity(id, null, symbol + " Limited", AssetType.EQUITY,
                currency, country, exchange, symbol + id.toString().substring(0, 4), "ACTIVE", Instant.now()));
        return id;
    }
}
