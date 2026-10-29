package com.aiinvestment.portfolio.application;

import org.springframework.stereotype.Service;
import java.util.Optional;
import java.util.UUID;
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

    /**
     * Explicit, bounded single-ISIN provisioning -- distinct from {@link #apply()}: fetches both
     * official sources fresh on every call (no caching across retries), validates and provisions
     * ONLY the selected ISIN, and never falls back to {@link NseEtfUniverseBootstrapService#bootstrapMissingEtfs()}.
     * Shares the same admission guard as {@link #apply()}, so a bulk pass and a single-ISIN
     * provisioning attempt can never run concurrently; whichever call does not win the guard
     * returns {@link Optional#empty()} immediately rather than waiting.
     *
     * @param selector the exact, uppercase, check-digit-valid ISIN to provision. Anything else
     *                 (null, blank, wrong case, extra whitespace, multiple ISINs, a malformed or
     *                 checksum-invalid ISIN) throws {@link IllegalArgumentException} before any
     *                 source is read or the admission guard is touched.
     */
    public Optional<SingleResult> applySingle(String selector) {
        String isin = validateSingleSelector(selector);
        if (!running.compareAndSet(false, true)) return Optional.empty();
        try {
            NseEtfUniverseBootstrapService.SingleEtfOutcome outcome = bootstrap.provisionSingleEtf(isin);
            Status status = "VERIFIED".equals(outcome.outcome()) ? Status.COMPLETED : Status.ABORTED;
            return Optional.of(new SingleResult(status, outcome.instrumentId(), outcome.outcome()));
        } finally {
            running.set(false);
        }
    }

    /** Exact-ISIN selector: case-sensitive, no whitespace tolerance, and a real ISO 6166 check-digit
     * validation (not just the 2-letter + 9-alphanumeric + 1-digit shape) -- deliberately stricter
     * than {@code InstrumentMasterEntity.normalizeIsin}, which normalizes case/whitespace for other
     * callers. A selector that merely looks shaped like an ISIN but carries the wrong check digit
     * must never reach a source lookup or a write. */
    private static String validateSingleSelector(String selector) {
        if (selector == null || !selector.matches("[A-Z]{2}[A-Z0-9]{9}[0-9]"))
            throw new IllegalArgumentException("ISIN_SELECTOR_MALFORMED");
        if (!hasValidIsinCheckDigit(selector))
            throw new IllegalArgumentException("ISIN_SELECTOR_CHECKSUM_INVALID");
        return selector;
    }

    /** ISO 6166 Annex A check digit: letters are their base-36 digit value (A=10..Z=35), then a
     * Luhn mod-10 sum over all resulting digits of the 11-character base, doubling every other
     * digit starting from the rightmost, is compared against the 12th (check) character. */
    private static boolean hasValidIsinCheckDigit(String isin) {
        String base = isin.substring(0, 11);
        int actual = isin.charAt(11) - '0';
        StringBuilder digits = new StringBuilder();
        for (int i = 0; i < base.length(); i++) digits.append(Character.getNumericValue(base.charAt(i)));
        int sum = 0;
        boolean doubleDigit = true;
        for (int i = digits.length() - 1; i >= 0; i--) {
            int d = digits.charAt(i) - '0';
            if (doubleDigit) { d *= 2; if (d > 9) d -= 9; }
            sum += d;
            doubleDigit = !doubleDigit;
        }
        int expected = (10 - (sum % 10)) % 10;
        return actual == expected;
    }

    public enum Status { COMPLETED, PARTIAL_FAILURE, ABORTED }
    public record Result(Status status, AuthoritativeAssetTypeReclassificationService.Summary reclassification,
                         NseEtfUniverseBootstrapService.Summary bootstrap) {}
    /** Outcome of a single-ISIN provisioning attempt; {@code globalInstrumentId} is null unless
     * {@code status == COMPLETED}. */
    public record SingleResult(Status status, UUID globalInstrumentId, String outcome) {}
}
