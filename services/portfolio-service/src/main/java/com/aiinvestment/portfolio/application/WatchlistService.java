package com.aiinvestment.portfolio.application;

import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterEntity;
import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterRepository;
import com.aiinvestment.portfolio.infrastructure.persistence.WatchlistEntity;
import com.aiinvestment.portfolio.infrastructure.persistence.WatchlistMembershipEntity;
import com.aiinvestment.portfolio.infrastructure.persistence.WatchlistMembershipRepository;
import com.aiinvestment.portfolio.infrastructure.persistence.WatchlistRepository;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.List;
import java.util.Set;
import java.util.UUID;

@Service
public class WatchlistService {
    private static final Set<String> PERIODS = Set.of("DAY", "WEEK", "MONTH", "YEAR");

    private final WatchlistRepository watchlists;
    private final WatchlistMembershipRepository memberships;
    private final InstrumentMasterRepository instruments;

    public WatchlistService(WatchlistRepository watchlists, WatchlistMembershipRepository memberships,
                            InstrumentMasterRepository instruments) {
        this.watchlists = watchlists;
        this.memberships = memberships;
        this.instruments = instruments;
    }

    @Transactional(readOnly = true)
    public List<WatchlistEntity> list(UUID userId) {
        return watchlists.findByUserIdOrderByRegionAscNameAsc(userId);
    }

    @Transactional(readOnly = true)
    public WatchlistEntity defaultWatchlist(UUID userId, MarketRegion region) {
        return watchlists.findByUserIdAndRegionAndSystemDefaultTrue(userId, region)
                .orElseThrow(WatchlistNotFoundException::new);
    }

    /**
     * Lazy, idempotent creation. The database unique constraint is the final
     * cross-pod guard; retrying after a concurrent insert returns that row.
     */
    public WatchlistEntity ensureDefault(UUID userId, MarketRegion region) {
        var existing = watchlists.findByUserIdAndRegionAndSystemDefaultTrue(userId, region);
        if (existing.isPresent()) return existing.get();
        try {
            return watchlists.saveAndFlush(new WatchlistEntity(
                    UUID.randomUUID(), userId, region.defaultWatchlistName(), region, true, Instant.now()));
        } catch (DataIntegrityViolationException conflict) {
            return watchlists.findByUserIdAndRegionAndSystemDefaultTrue(userId, region)
                    .orElseThrow(() -> conflict);
        }
    }

    @Transactional(readOnly = true)
    public WatchlistView get(UUID userId, UUID watchlistId) {
        var watchlist = owned(userId, watchlistId);
        return new WatchlistView(watchlist,
                memberships.findByWatchlistIdOrderByAddedAtAscGlobalInstrumentIdAsc(watchlistId));
    }

    @Transactional
    public WatchlistMembershipEntity add(UUID userId, UUID watchlistId, UUID globalInstrumentId,
                                         String sourcePeriod, BigDecimal sourcePerformancePct) {
        var watchlist = owned(userId, watchlistId);
        InstrumentMasterEntity instrument = instruments.findById(globalInstrumentId)
                .orElseThrow(() -> new IllegalArgumentException("GLOBAL_INSTRUMENT_NOT_FOUND"));
        if (MarketRegion.from(instrument) != watchlist.getRegion()) {
            throw new WatchlistRegionMismatchException();
        }
        String normalizedPeriod = normalizePeriod(sourcePeriod);
        Instant now = Instant.now();
        var existing = memberships.findByWatchlistIdAndGlobalInstrumentId(watchlistId, globalInstrumentId);
        if (existing.isPresent()) {
            existing.get().updateSourceContext(normalizedPeriod, sourcePerformancePct, now);
            watchlist.touch(now);
            return existing.get();
        }
        var created = memberships.save(new WatchlistMembershipEntity(
                UUID.randomUUID(), watchlistId, globalInstrumentId,
                normalizedPeriod, sourcePerformancePct, now));
        watchlist.touch(now);
        return created;
    }

    @Transactional
    public void remove(UUID userId, UUID watchlistId, UUID globalInstrumentId) {
        var watchlist = owned(userId, watchlistId);
        if (memberships.deleteByWatchlistIdAndGlobalInstrumentId(watchlistId, globalInstrumentId) > 0) {
            watchlist.touch(Instant.now());
        }
    }

    @Transactional(readOnly = true)
    public long instrumentCount(UUID watchlistId) {
        return memberships.countByWatchlistId(watchlistId);
    }

    private WatchlistEntity owned(UUID userId, UUID watchlistId) {
        return watchlists.findByWatchlistIdAndUserId(watchlistId, userId)
                .orElseThrow(WatchlistNotFoundException::new);
    }

    private static String normalizePeriod(String sourcePeriod) {
        if (sourcePeriod == null || sourcePeriod.isBlank()) return null;
        String normalized = sourcePeriod.trim().toUpperCase();
        if (!PERIODS.contains(normalized)) throw new IllegalArgumentException("WATCHLIST_SOURCE_PERIOD_INVALID");
        return normalized;
    }

    public record WatchlistView(WatchlistEntity watchlist, List<WatchlistMembershipEntity> memberships) {}
}
