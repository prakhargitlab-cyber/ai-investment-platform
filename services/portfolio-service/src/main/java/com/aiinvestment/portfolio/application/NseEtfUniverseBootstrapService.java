package com.aiinvestment.portfolio.application;

import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterEntity;
import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.UUID;

/**
 * Bounded, idempotent, explicit bootstrap of GENUINELY MISSING NSE ETF {@code instrument_master}
 * rows -- distinct from {@link AuthoritativeAssetTypeReclassificationService}, which only
 * reclassifies EXISTING rows and never creates one. This service creates a brand-new
 * {@code AssetType.ETF} row only for an official NSE ETF ISIN that has NO instrument_master row
 * at all yet; an ISIN that already exists (as EQUITY, ETF, or anything else) is left completely
 * untouched here -- that overlap case is reclassification's job, not this one.
 *
 * <p>Fetches NSE's official ETF securities-available-for-trading list ONCE per call
 * ({@link NseOfficialEtfSecurityList#listAll()}, which throws rather than returning a status value
 * on failure -- see that interface's own documentation) and fails CLOSED: if the fetch throws,
 * nothing is read from or written to {@code instrument_master} at all, and the returned
 * {@link Summary} reports {@code providerUnavailable}.
 *
 * <p>The ENTIRE fetched list is prevalidated via {@link NseEtfListingValidation} -- the exact same
 * logic {@link NseEtfUniversePreviewService} uses, so the two can never disagree -- BEFORE any
 * single ETF row is created: a malformed listing is rejected, and if the SAME ISIN occurs more
 * than once anywhere in the source, EVERY occurrence -- including the first -- is rejected rather
 * than one of them being arbitrarily created.
 *
 * <p>The official IV-series security master is ALSO fetched once per call. An ISIN that the
 * official ETF list and the IV series both claim is an authoritative conflict and is NEVER
 * created -- counted as {@code rejectedConflict}. If the IV-series source itself is unavailable,
 * conflict status for every otherwise-missing ISIN is UNKNOWN, not "no conflict": rather than
 * silently treating an unverified ISIN as safe to create, this service skips creating it and
 * counts it as {@code skippedUnverifiableConflict}.
 *
 * <p>Each genuinely-missing, conflict-free row is created through its OWN independent transaction
 * ({@link InstrumentMasterService#canonicalizeOfficialEtf}, itself {@code @Transactional}) -- this
 * service's own loop is deliberately NOT wrapped in a shared transaction, so a failure creating
 * one ISIN (a constraint violation, an identity-mismatch guard, etc.) can only roll back that one
 * row's own transaction. It is caught here, counted, and logged; it never aborts, poisons, or
 * prevents any other row's independent transaction from running or having already committed.
 *
 * <p>Identity join is exact-ISIN only -- never symbol or name heuristics -- mirroring
 * {@link AuthoritativeAssetTypeReclassificationService}'s own stated invariant.
 *
 * <p><b>Not wired into any automatically-triggered path</b> (not {@code @Scheduled}, not called
 * from {@link CanonicalIdentityBootstrap#tick()}). Invoked only via {@link NseEtfUniverseApplyService},
 * itself only reachable through an explicit, ADMIN-gated operator request.
 */
@Service
public class NseEtfUniverseBootstrapService {
    private static final Logger log = LoggerFactory.getLogger(NseEtfUniverseBootstrapService.class);
    private final InstrumentMasterRepository masters;
    private final NseOfficialEtfSecurityList etfSecurityList;
    private final NseOfficialSecurityMaster securityMaster;
    private final InstrumentMasterService instruments;

    public NseEtfUniverseBootstrapService(InstrumentMasterRepository masters, NseOfficialEtfSecurityList etfSecurityList,
            NseOfficialSecurityMaster securityMaster, InstrumentMasterService instruments) {
        this.masters = masters;
        this.etfSecurityList = etfSecurityList;
        this.securityMaster = securityMaster;
        this.instruments = instruments;
    }

    public Summary bootstrapMissingEtfs() {
        List<NseOfficialEtfSecurityList.Listing> listings;
        try {
            listings = etfSecurityList.listAll();
        } catch (RuntimeException unavailable) {
            log.warn("nse_etf_universe_bootstrap event=PROVIDER_TEMPORARILY_UNAVAILABLE exception={}",
                    unavailable.getClass().getSimpleName());
            Summary summary = new Summary();
            summary.providerUnavailable = true;
            return summary;
        }

        boolean ivSeriesSourceAvailable;
        Set<String> ivIsins = new HashSet<>();
        try {
            List<NseOfficialSecurityMaster.Listing> equityListings = securityMaster.listedEquities();
            ivSeriesSourceAvailable = !equityListings.isEmpty();
            for (NseOfficialSecurityMaster.Listing listing : equityListings) {
                if (!"IV".equalsIgnoreCase(listing.series())) {
                    continue;
                }
                String isin = InstrumentMasterEntity.normalizeIsin(listing.isin());
                if (isin != null) {
                    ivIsins.add(isin);
                }
            }
        } catch (RuntimeException unavailable) {
            ivSeriesSourceAvailable = false;
        }

        Summary summary = new Summary();
        summary.ivSeriesSourceAvailable = ivSeriesSourceAvailable;
        summary.examined = listings.size();

        NseEtfListingValidation.Result validation = NseEtfListingValidation.validate(listings);
        summary.rejectedMalformed = validation.malformed().size();
        summary.rejectedDuplicateIsin = validation.duplicates().size();
        for (NseEtfListingValidation.Rejected rejected : validation.malformed()) {
            log.warn("nse_etf_universe_bootstrap event=REJECTED_MALFORMED isin={} symbol={}",
                    rejected.isin(), rejected.symbol());
        }
        for (NseEtfListingValidation.Rejected rejected : validation.duplicates()) {
            log.warn("nse_etf_universe_bootstrap event=REJECTED_DUPLICATE_ISIN_IN_SOURCE isin={} symbol={}",
                    rejected.isin(), rejected.symbol());
        }

        for (NseEtfListingValidation.Candidate candidate : validation.accepted()) {
            String isin = candidate.isin();
            String symbol = candidate.symbol();
            String securityName = candidate.securityName();

            // Read-only pre-check: an existing row at this ISIN (EQUITY, ETF, or anything else)
            // is skipped untouched -- never passed into canonicalizeOfficialEtf, which must only
            // ever be reached for a genuinely new ISIN.
            if (masters.findByNormalizedIsin(isin).isPresent()) {
                summary.existing++;
                continue;
            }

            // Conflict status must be resolved BEFORE this ISIN is ever created. An unverifiable
            // ISIN is never treated as safe: if the IV-series source itself is unavailable, this
            // ISIN is skipped, not created.
            if (!ivSeriesSourceAvailable) {
                summary.skippedUnverifiableConflict++;
                log.warn("nse_etf_universe_bootstrap event=SKIPPED_UNVERIFIABLE_CONFLICT isin={} symbol={} "
                        + "reason=IV_SERIES_SOURCE_UNAVAILABLE", isin, symbol);
                continue;
            }
            if (ivIsins.contains(isin)) {
                summary.rejectedConflict++;
                log.warn("nse_etf_universe_bootstrap event=REJECTED_AUTHORITATIVE_CONFLICT isin={} symbol={} "
                        + "reason=ISIN_IN_BOTH_ETF_LIST_AND_IV_SERIES", isin, symbol);
                continue;
            }

            try {
                instruments.canonicalizeOfficialEtf(isin, symbol, securityName);
                summary.created++;
                log.info("nse_etf_universe_bootstrap event=ETF_CREATED isin={} symbol={}", isin, symbol);
            } catch (RuntimeException failure) {
                summary.failed++;
                log.warn("nse_etf_universe_bootstrap event=ETF_CREATE_FAILED isin={} symbol={} reason={}",
                        isin, symbol, failure.getClass().getSimpleName());
            }
        }
        log.info("nse_etf_universe_bootstrap_complete {}", summary);
        return summary;
    }

    /**
     * Bounded single-ISIN counterpart to {@link #bootstrapMissingEtfs()}, used only by
     * {@link NseEtfUniverseApplyService#applySingle}. Fetches both official sources fresh (never
     * reuses a prior pass's snapshot), applies the SAME prevalidation ({@link NseEtfListingValidation})
     * and the SAME IV-series conflict definition as the bulk path, plus a symbol-collision check
     * across the fetched ETF list that only matters once a single ISIN is pinned down. Never reads
     * or writes {@code instrument_master} directly -- existence, identity and mapping completeness
     * are entirely {@link InstrumentMasterService#canonicalizeSelectedOfficialEtf}'s job, exactly as
     * {@link InstrumentMasterService#canonicalizeOfficialEtf} is the bulk path's.
     */
    public SingleEtfOutcome provisionSingleEtf(String isin) {
        List<NseOfficialEtfSecurityList.Listing> listings;
        try {
            listings = etfSecurityList.listAll();
        } catch (RuntimeException unavailable) {
            log.warn("nse_etf_single_provisioning event=OFFICIAL_SOURCE_UNAVAILABLE isin={} source=ETF_LIST exception={}",
                    isin, unavailable.getClass().getSimpleName());
            return new SingleEtfOutcome("OFFICIAL_SOURCE_UNAVAILABLE", null);
        }

        List<NseOfficialSecurityMaster.Listing> equityListings;
        try {
            equityListings = securityMaster.listedEquities();
        } catch (RuntimeException unavailable) {
            log.warn("nse_etf_single_provisioning event=OFFICIAL_SOURCE_UNAVAILABLE isin={} source=IV_SERIES exception={}",
                    isin, unavailable.getClass().getSimpleName());
            return new SingleEtfOutcome("OFFICIAL_SOURCE_UNAVAILABLE", null);
        }
        if (equityListings.isEmpty()) {
            // Mirrors bootstrapMissingEtfs()'s ivSeriesSourceAvailable: an empty IV-series source is
            // never treated as "no conflict", only as unavailable.
            log.warn("nse_etf_single_provisioning event=OFFICIAL_SOURCE_UNAVAILABLE isin={} source=IV_SERIES reason=EMPTY", isin);
            return new SingleEtfOutcome("OFFICIAL_SOURCE_UNAVAILABLE", null);
        }

        int rawOccurrences = 0;
        for (NseOfficialEtfSecurityList.Listing listing : listings) {
            if (isin.equals(InstrumentMasterEntity.normalizeIsin(listing.isin()))) {
                rawOccurrences++;
            }
        }
        if (rawOccurrences == 0) {
            return new SingleEtfOutcome("ISIN_NOT_IN_OFFICIAL_ETF_UNIVERSE", null);
        }

        // Same whole-list prevalidation bootstrapMissingEtfs() uses: a listing with a missing ISIN
        // is malformed, and a duplicated ISIN anywhere in the source disqualifies every occurrence --
        // including the one that otherwise looks fine. A raw ISIN appearing more than once in the
        // source is itself a conflicting-identity condition even when only one of those occurrences
        // is well-formed (a "malformed duplicate"): NseEtfListingValidation only groups WELL-FORMED
        // candidates by ISIN, so when one of two same-ISIN entries is malformed, the other is left as
        // the sole accepted candidate and would otherwise look like a clean, unique identity. Checking
        // rawOccurrences alongside acceptedMatches catches that case too: the selector must map to
        // exactly one accepted, well-formed candidate AND appear exactly once in the raw source.
        NseEtfListingValidation.Result validation = NseEtfListingValidation.validate(listings);
        NseEtfListingValidation.Candidate selected = null;
        int acceptedMatches = 0;
        for (NseEtfListingValidation.Candidate candidate : validation.accepted()) {
            if (isin.equals(candidate.isin())) {
                selected = candidate;
                acceptedMatches++;
            }
        }
        if (rawOccurrences != 1 || acceptedMatches != 1) {
            return new SingleEtfOutcome("OFFICIAL_ETF_IDENTITY_INVALID_OR_DUPLICATE", null);
        }

        // A different ISIN in the same fetched list claiming the same trading symbol is an
        // authoritative-source inconsistency NseEtfListingValidation does not check (it only groups
        // by ISIN), so it is checked here before this ISIN is ever handed to the canonicalizer.
        for (NseEtfListingValidation.Candidate other : validation.accepted()) {
            if (!other.isin().equals(selected.isin()) && other.symbol().equalsIgnoreCase(selected.symbol())) {
                return new SingleEtfOutcome("OFFICIAL_ETF_SYMBOL_CONFLICT", null);
            }
        }

        for (NseOfficialSecurityMaster.Listing listing : equityListings) {
            if ("IV".equalsIgnoreCase(listing.series()) && isin.equals(InstrumentMasterEntity.normalizeIsin(listing.isin()))) {
                return new SingleEtfOutcome("ISIN_IN_BOTH_ETF_LIST_AND_IV_SERIES", null);
            }
        }

        try {
            InstrumentMasterEntity master = instruments.canonicalizeSelectedOfficialEtf(selected.isin(), selected.symbol(), selected.securityName());
            log.info("nse_etf_single_provisioning event=ETF_PROVISIONED isin={} symbol={}", selected.isin(), selected.symbol());
            return new SingleEtfOutcome("VERIFIED", master.getInstrumentId());
        } catch (IllegalStateException conflict) {
            log.warn("nse_etf_single_provisioning event=CANONICAL_IDENTITY_OR_MAPPING_CONFLICT isin={} symbol={}",
                    selected.isin(), selected.symbol());
            return new SingleEtfOutcome("CANONICAL_IDENTITY_OR_MAPPING_CONFLICT", null);
        } catch (RuntimeException failure) {
            log.warn("nse_etf_single_provisioning event=PROVISIONING_FAILED isin={} symbol={} reason={}",
                    selected.isin(), selected.symbol(), failure.getClass().getSimpleName());
            return new SingleEtfOutcome("SINGLE_ETF_PROVISIONING_FAILED", null);
        }
    }

    /** Outcome of one {@link #provisionSingleEtf} call; {@code instrumentId} is null unless
     * {@code "VERIFIED".equals(outcome)}. */
    public record SingleEtfOutcome(String outcome, UUID instrumentId) {}

    /** Bounded observability counts for one pass; never per-row logging beyond created/rejected/failed above. */
    public static final class Summary {
        public int examined;
        public int created;
        public int existing;
        public int rejectedMalformed;
        public int rejectedDuplicateIsin;
        public int rejectedConflict;
        public int skippedUnverifiableConflict;
        public int failed;
        public boolean providerUnavailable;
        public boolean ivSeriesSourceAvailable;

        @Override
        public String toString() {
            return "examined=" + examined + " created=" + created + " existing=" + existing
                    + " rejectedMalformed=" + rejectedMalformed + " rejectedDuplicateIsin=" + rejectedDuplicateIsin
                    + " rejectedConflict=" + rejectedConflict + " skippedUnverifiableConflict=" + skippedUnverifiableConflict
                    + " failed=" + failed + " providerUnavailable=" + providerUnavailable
                    + " ivSeriesSourceAvailable=" + ivSeriesSourceAvailable;
        }
    }
}
