package com.aiinvestment.portfolio.application;

import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterEntity;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Shared prevalidation of the official NSE ETF securities list, used IDENTICALLY by both
 * {@link NseEtfUniversePreviewService} and {@link NseEtfUniverseBootstrapService} so the two can
 * never disagree about which listings are malformed or duplicated -- they call this exact same
 * method rather than each rolling their own equivalent-but-possibly-divergent logic.
 *
 * <p>The ENTIRE list is validated up front, before either caller acts on any single row: a
 * listing with a missing/non-normalizable ISIN or a blank symbol/security name is rejected as
 * malformed. Separately, if the SAME normalized ISIN appears more than once anywhere in the
 * official list, EVERY occurrence of that ISIN -- including the first -- is rejected as a
 * duplicate; no occurrence is ever arbitrarily treated as "the real one," because nothing in the
 * official source itself says which one that would be.
 */
final class NseEtfListingValidation {
    private NseEtfListingValidation() {
    }

    static Result validate(List<NseOfficialEtfSecurityList.Listing> listings) {
        List<Candidate> candidates = new ArrayList<>();
        List<Rejected> malformed = new ArrayList<>();
        for (NseOfficialEtfSecurityList.Listing listing : listings) {
            String isin = InstrumentMasterEntity.normalizeIsin(listing.isin());
            String symbol = listing.symbol();
            String securityName = listing.securityName();
            if (isin == null || symbol == null || symbol.isBlank() || securityName == null || securityName.isBlank()) {
                malformed.add(new Rejected(listing.isin(), symbol, "INVALID_IDENTITY_FIELDS"));
                continue;
            }
            candidates.add(new Candidate(isin, symbol, securityName));
        }

        // Group by normalized ISIN (first-seen order preserved) across the WHOLE list before
        // accepting any single row, so a duplicate appearing later still disqualifies the first.
        Map<String, List<Candidate>> byIsin = new LinkedHashMap<>();
        for (Candidate candidate : candidates) {
            byIsin.computeIfAbsent(candidate.isin(), key -> new ArrayList<>()).add(candidate);
        }

        List<Candidate> accepted = new ArrayList<>();
        List<Rejected> duplicates = new ArrayList<>();
        for (Map.Entry<String, List<Candidate>> entry : byIsin.entrySet()) {
            List<Candidate> group = entry.getValue();
            if (group.size() > 1) {
                for (Candidate candidate : group) {
                    duplicates.add(new Rejected(candidate.isin(), candidate.symbol(), "DUPLICATE_ISIN_IN_OFFICIAL_ETF_LIST"));
                }
            } else {
                accepted.add(group.get(0));
            }
        }

        return new Result(accepted, malformed, duplicates);
    }

    /** A validated, normalized, non-duplicate official ETF listing eligible for comparison/creation. */
    record Candidate(String isin, String symbol, String securityName) {
    }

    /** A listing rejected before any comparison/creation, with the reason it was rejected. */
    record Rejected(String isin, String symbol, String reason) {
    }

    record Result(List<Candidate> accepted, List<Rejected> malformed, List<Rejected> duplicates) {
    }
}
