package com.aiinvestment.portfolio.application;

import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterEntity;
import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.util.HashSet;
import java.util.List;
import java.util.Set;

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
