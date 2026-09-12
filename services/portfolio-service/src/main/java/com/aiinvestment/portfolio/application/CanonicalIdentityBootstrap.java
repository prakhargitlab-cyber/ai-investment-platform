package com.aiinvestment.portfolio.application;

import jakarta.annotation.PreDestroy;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.scheduling.annotation.EnableScheduling;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import java.time.Instant;
import java.util.*;
import java.util.concurrent.*;
import java.util.stream.Collectors;

/** Explicit, leased, retryable reference/identity bootstrap. No research acquisition. */
@Component
@EnableScheduling
@ConditionalOnProperty(name="market.identity-bootstrap.enabled", havingValue="true", matchIfMissing=true)
public class CanonicalIdentityBootstrap {
    private static final Logger log = LoggerFactory.getLogger(CanonicalIdentityBootstrap.class);
    private final NseOfficialSecurityMaster official;
    private final Nifty500ReferenceService nifty;
    private final InstrumentMasterService instruments;
    private final GlobalInstrumentReconciliationService reconciliation;
    private final CanonicalIdentityBootstrapStore store;
    private final String owner = UUID.randomUUID().toString();
    private final ExecutorService workers = Executors.newFixedThreadPool(4);
    private Instant providerRetryAfter = Instant.EPOCH;
    @Value("${market.identity-bootstrap.batch-size:40}") private int batchSize = 40;

    public CanonicalIdentityBootstrap(NseOfficialSecurityMaster official, Nifty500ReferenceService nifty,
            InstrumentMasterService instruments, GlobalInstrumentReconciliationService reconciliation, CanonicalIdentityBootstrapStore store) {
        this.official=official; this.nifty=nifty; this.instruments=instruments; this.reconciliation=reconciliation; this.store=store;
    }

    @Scheduled(initialDelayString="${market.identity-bootstrap.initial-delay-ms:15000}", fixedDelayString="${market.identity-bootstrap.delay-ms:5000}")
    public void tick() {
        Instant now = Instant.now();
        if (now.isBefore(providerRetryAfter) || !store.claim(owner, now)) return;
        try {
            if (store.universeDue(now)) {
                List<NseOfficialSecurityMaster.Listing> rows = official.listedEquities();
                if (rows.isEmpty() || rows.size()>10000) throw new IllegalStateException("CANONICAL_BOOTSTRAP_FAILED");
                var isinCounts = rows.stream().collect(Collectors.groupingBy(NseOfficialSecurityMaster.Listing::isin, Collectors.counting()));
                var symbolCounts = rows.stream().collect(Collectors.groupingBy(r -> r.symbol().toUpperCase(Locale.ROOT), Collectors.counting()));
                for (var row : rows) {
                    if (!Set.of("EQ","BE","BZ","SM","ST","IV").contains(row.series().toUpperCase(Locale.ROOT))) continue;
                    if (isinCounts.get(row.isin()) != 1 || symbolCounts.get(row.symbol().toUpperCase(Locale.ROOT)) != 1) {
                        log.warn("canonical_identity_bootstrap event=CANONICAL_BOOTSTRAP_FAILED reason=AMBIGUOUS_OFFICIAL_IDENTITY symbol={}", row.symbol());
                        continue;
                    }
                    try { instruments.canonicalizeOfficialNse(row.isin(), row.symbol(), row.companyName(), "NSE_OFFICIAL_ISIN_BOOTSTRAP"); }
                    catch (RuntimeException rejected) { log.warn("canonical_identity_bootstrap event=CANONICAL_BOOTSTRAP_FAILED reason=IDENTITY_REJECTED symbol={}", row.symbol()); }
                }
                store.universeLoaded(now);
            }
            if (store.referenceDue(now)) {
                try { nifty.refresh(); store.referenceLoaded(now); }
                catch (RuntimeException unavailable) { log.warn("canonical_identity_bootstrap event=PROVIDER_TEMPORARILY_UNAVAILABLE provider=NIFTY_REFERENCE"); }
            }
            store.enqueue();
            List<UUID> ids = store.dueMappings(Math.max(1, Math.min(batchSize, 40)), Instant.now());
            List<Callable<GlobalInstrumentReconciliationService.Outcome>> tasks = ids.stream()
                    .<Callable<GlobalInstrumentReconciliationService.Outcome>>map(id -> () -> reconcile(id)).toList();
            var results = workers.invokeAll(tasks, 160, TimeUnit.SECONDS);
            boolean allUnavailable = !results.isEmpty();
            for (int i=0; i<results.size(); i++) {
                var result = results.get(i);
                GlobalInstrumentReconciliationService.Outcome outcome;
                try { outcome = result.get(); }
                catch (ExecutionException | CancellationException error) { outcome = new GlobalInstrumentReconciliationService.Outcome("UNAVAILABLE", "PROVIDER_TEMPORARILY_UNAVAILABLE"); }
                store.completed(ids.get(i), outcome, Instant.now());
                allUnavailable &= "UNAVAILABLE".equals(outcome.status());
            }
            if (allUnavailable) providerRetryAfter = Instant.now().plusSeconds(60);
            log.info("canonical_identity_bootstrap event=BOOTSTRAP_PROGRESS counts={}", store.counts());
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
        } catch (RuntimeException unavailable) {
            providerRetryAfter = Instant.now().plusSeconds(60);
            log.warn("canonical_identity_bootstrap event=CANONICAL_BOOTSTRAP_FAILED reason=PROVIDER_TEMPORARILY_UNAVAILABLE exception={}", unavailable.getClass().getSimpleName());
        } finally { store.release(owner); }
    }

    private GlobalInstrumentReconciliationService.Outcome reconcile(UUID id) {
        var result = reconciliation.reconcile(id);
        String event = switch (result.status()) {
            case "VALIDATED" -> "YAHOO_MAPPING_VALIDATED";
            case "REJECTED" -> "NSE_MAPPING_MISSING".equals(result.reason()) ? "NSE_MAPPING_MISSING" : "YAHOO_MAPPING_REJECTED";
            case "SKIPPED" -> "YAHOO_MAPPING_SKIPPED";
            default -> "PROVIDER_TEMPORARILY_UNAVAILABLE";
        };
        log.info("canonical_identity_bootstrap event={} globalInstrumentId={} reason={}", event, id, result.reason());
        return result;
    }
    @PreDestroy public void stop() { workers.shutdownNow(); }
}
