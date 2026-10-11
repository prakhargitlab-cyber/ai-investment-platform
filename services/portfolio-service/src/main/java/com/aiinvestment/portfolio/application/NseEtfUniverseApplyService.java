package com.aiinvestment.portfolio.application;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.util.Optional;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Explicit, operator-invoked orchestration of the two mutating NSE ETF universe passes:
 * {@link AuthoritativeAssetTypeReclassificationService#reclassifyExistingNseInstruments()} (corrects
 * EXISTING rows for which authoritative evidence now supports ETF/OTHER) and
 * {@link NseEtfUniverseBootstrapService#bootstrapMissingEtfs()} (creates genuinely missing ETF rows).
 *
 * <p>Before either pass runs, this service validates the single MANDATORY authoritative input both
 * passes ultimately depend on -- NSE's official ETF securities list -- by fetching it once itself.
 * If that fetch throws, NEITHER pass is invoked at all: no mutation of any kind happens, and the
 * call returns {@link Status#ABORTED} with zeroed summaries, rather than a misleadingly "successful"
 * response whose summaries merely happen to show nothing done. (Re-fetching the list here, on top of
 * {@link NseEtfUniverseBootstrapService}'s and {@link AuthoritativeAssetTypeReclassificationService}'s
 * own independent fetches, is a deliberate, minor redundancy traded for this correctness guarantee;
 * it does not change either downstream service's own contract.)
 *
 * <p>Once the preflight check passes, both passes still run fresh and independently re-validate
 * their own authoritative evidence: neither this class, {@code preview()}, nor either downstream
 * service shares a cached snapshot of a prior fetch across calls.
 *
 * <p>The returned {@link Result} always carries an explicit {@link Status}:
 * <ul>
 *     <li>{@link Status#ABORTED} -- the mandatory ETF list source was unavailable; nothing ran.</li>
 *     <li>{@link Status#PARTIAL_FAILURE} -- both passes ran, but at least one individual ETF
 *         creation failed, or the ETF list became unavailable to bootstrap after the preflight
 *         check succeeded (a narrow race). Rows that succeeded independently beforehand are NOT
 *         rolled back -- {@link NseEtfUniverseBootstrapService} creates each row in its own
 *         transaction precisely so one failure never implies the whole operation was undone.</li>
 *     <li>{@link Status#COMPLETED} -- both passes ran with no individual creation failures.</li>
 * </ul>
 *
 * <p>Concurrent overlapping apply executions within this JVM are rejected via an in-process
 * {@link AtomicBoolean} compare-and-set guard -- a deliberate, minimal, explicitly scoped choice over a
 * database-backed lease/lock table, which would require a new Flyway migration (out of scope per this
 * change's boundaries, which restrict schema changes to cases where an existing constraint demonstrably
 * prevents safe implementation; no such blocker exists here). This guard does NOT protect against two
 * overlapping apply calls landing on two different portfolio-service replicas at the same time -- that
 * remains a known, disclosed risk of this minimal implementation. It does not, however, allow actual data
 * corruption even under that scenario: both downstream passes still go through
 * {@code InstrumentMasterService}'s own per-ISIN {@code pg_advisory_xact_lock} identity locking, which is
 * unrelated to and unaffected by this in-process guard.
 */
@Service
public class NseEtfUniverseApplyService {
    private static final Logger log = LoggerFactory.getLogger(NseEtfUniverseApplyService.class);
    private final NseOfficialEtfSecurityList etfSecurityList;
    private final AuthoritativeAssetTypeReclassificationService reclassification;
    private final NseEtfUniverseBootstrapService bootstrap;
    private final AtomicBoolean running = new AtomicBoolean(false);

    public NseEtfUniverseApplyService(NseOfficialEtfSecurityList etfSecurityList,
            AuthoritativeAssetTypeReclassificationService reclassification,
            NseEtfUniverseBootstrapService bootstrap) {
        this.etfSecurityList = etfSecurityList;
        this.reclassification = reclassification;
        this.bootstrap = bootstrap;
    }

    /**
     * @return empty if another apply execution is already in progress (within this JVM); otherwise a
     *         {@link Result} carrying an explicit {@link Status} and both passes' summaries.
     */
    public Optional<Result> apply() {
        if (!running.compareAndSet(false, true)) {
            log.warn("nse_etf_universe_apply event=REJECTED_CONCURRENT_APPLY");
            return Optional.empty();
        }
        try {
            try {
                etfSecurityList.listAll();
            } catch (RuntimeException unavailable) {
                log.warn("nse_etf_universe_apply event=ABORTED_MANDATORY_SOURCE_UNAVAILABLE exception={}",
                        unavailable.getClass().getSimpleName());
                return Optional.of(new Result(Status.ABORTED,
                        new AuthoritativeAssetTypeReclassificationService.Summary(),
                        new NseEtfUniverseBootstrapService.Summary()));
            }

            AuthoritativeAssetTypeReclassificationService.Summary reclassificationSummary =
                    reclassification.reclassifyExistingNseInstruments();
            NseEtfUniverseBootstrapService.Summary bootstrapSummary = bootstrap.bootstrapMissingEtfs();

            Status status = (bootstrapSummary.failed > 0 || bootstrapSummary.providerUnavailable)
                    ? Status.PARTIAL_FAILURE
                    : Status.COMPLETED;

            log.info("nse_etf_universe_apply_complete status={} reclassification=[{}] bootstrap=[{}]",
                    status, reclassificationSummary, bootstrapSummary);
            return Optional.of(new Result(status, reclassificationSummary, bootstrapSummary));
        } finally {
            running.set(false);
        }
    }

    public enum Status {
        /** Both passes ran; every individual ETF creation (if any) succeeded. */
        COMPLETED,
        /** Both passes ran, but at least one individual ETF creation failed or bootstrap's source
         *  became unavailable after the preflight check -- already-committed rows were preserved. */
        PARTIAL_FAILURE,
        /** The mandatory official ETF list source was unavailable before either pass began; nothing
         *  was mutated. */
        ABORTED
    }

    public record Result(
            Status status,
            AuthoritativeAssetTypeReclassificationService.Summary reclassification,
            NseEtfUniverseBootstrapService.Summary bootstrap
    ) {
    }
}
