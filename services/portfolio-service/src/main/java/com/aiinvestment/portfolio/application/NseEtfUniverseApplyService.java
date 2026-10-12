package com.aiinvestment.portfolio.application;

import org.springframework.stereotype.Service;
import java.util.Optional;
import java.util.concurrent.atomic.AtomicBoolean;

/** Explicit missing-ETF synchronization. Bootstrap freshly fetches both official
 * sources, validates the whole ETF list and checks conflicts before each creation.
 * Preview is advisory; apply never treats an earlier preview as authorization to
 * overwrite identity. Existing instruments are preserved, not reclassified.
 * Each creation commits independently; returned counts describe partial writes.
 */
@Service
public class NseEtfUniverseApplyService {
    private final NseEtfUniverseBootstrapService bootstrap;
    private final AtomicBoolean running = new AtomicBoolean(false);

    public NseEtfUniverseApplyService(NseEtfUniverseBootstrapService bootstrap) {
        this.bootstrap = bootstrap;
    }

    public Optional<Result> apply() {
        if (!running.compareAndSet(false, true)) return Optional.empty();
        try {
            var summary = bootstrap.bootstrapMissingEtfs();
            Status status = summary.providerUnavailable || !summary.ivSeriesSourceAvailable || summary.examined == 0
                    ? Status.ABORTED
                    : summary.failed > 0 || summary.rejectedConflict > 0 || summary.rejectedMalformed > 0
                      || summary.rejectedDuplicateIsin > 0 || summary.skippedUnverifiableConflict > 0
                        ? Status.PARTIAL_FAILURE : Status.COMPLETED;
            // Retain the response field for clients; this operation never reclassifies.
            return Optional.of(new Result(status, new AuthoritativeAssetTypeReclassificationService.Summary(), summary));
        } finally {
            running.set(false);
        }
    }

    public enum Status { COMPLETED, PARTIAL_FAILURE, ABORTED }
    public record Result(Status status, AuthoritativeAssetTypeReclassificationService.Summary reclassification,
                         NseEtfUniverseBootstrapService.Summary bootstrap) {}
}
