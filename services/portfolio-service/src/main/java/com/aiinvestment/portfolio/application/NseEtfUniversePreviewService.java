package com.aiinvestment.portfolio.application;

import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterEntity;
import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterRepository;
import com.aiinvestment.shared.domain.AssetType;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Optional;
import java.util.Set;

/**
 * STRICTLY READ-ONLY comparison of the official NSE ETF list against existing canonical
 * {@code instrument_master} rows by normalized ISIN. Never calls
 * {@link InstrumentMasterService#canonicalizeOfficialEtf}, never calls
 * {@link AuthoritativeAssetTypeReclassificationService}, never triggers any reconciliation path --
 * only {@link InstrumentMasterRepository#findByNormalizedIsin}, {@link NseOfficialEtfSecurityList#listAll()}
 * and {@link NseOfficialSecurityMaster#listedEquities()} are read.
 *
 * <p>Fails CLOSED: if the official ETF list cannot be fetched at all, {@link #preview()} returns
 * a {@link Preview} with {@code sourceAvailable=false} and every list empty -- it does not fall
 * back to a partial or cached comparison.
 *
 * <p>The ENTIRE official ETF list is prevalidated via {@link NseEtfListingValidation} before any
 * row is classified: a malformed listing is rejected, and if the SAME ISIN occurs more than once
 * anywhere in the source, EVERY occurrence -- including the first -- is rejected as a duplicate,
 * reported in {@link Preview#authoritativeConflicts()}. {@link NseEtfUniverseBootstrapService} uses
 * this exact same validation, so preview and bootstrap can never disagree about which listings are
 * malformed or duplicated.
 *
 * <p>The official IV-series equity/security master is read separately, purely to detect an
 * authoritative conflict (an ISIN appearing in BOTH the official ETF list and the official IV
 * (Investment Vehicle / ETF-like) series of the general security master) -- this mirrors
 * {@link AuthoritativeAssetTypeReclassificationService}'s own existing conservative convention of
 * treating an empty {@code listedEquities()} result as "security master unavailable" (that
 * provider fails closed to an empty list on failure, not an exception, so emptiness is the only
 * unavailability signal it exposes).
 *
 * <p>This conflict check runs BEFORE an ISIN is ever classified as missing or as an equity-to-ETF
 * candidate -- never after. A brand-new ISIN that the IV series also claims is reported as a
 * conflict, never as "missing" (which would otherwise read as safe to bootstrap). If the IV-series
 * source itself is unavailable, that ISIN's conflict status is UNKNOWN, not "no conflict": it is
 * reported separately via {@link Preview#conflictVerificationIncomplete()} rather than being
 * silently folded into {@code missingEtfs} or {@code equityToEtfCandidates} as though verified safe.
 */
@Service
public class NseEtfUniversePreviewService {
    private final InstrumentMasterRepository masters;
    private final NseOfficialEtfSecurityList etfSecurityList;
    private final NseOfficialSecurityMaster securityMaster;

    public NseEtfUniversePreviewService(InstrumentMasterRepository masters, NseOfficialEtfSecurityList etfSecurityList,
            NseOfficialSecurityMaster securityMaster) {
        this.masters = masters;
        this.etfSecurityList = etfSecurityList;
        this.securityMaster = securityMaster;
    }

    @Transactional(readOnly = true)
    public Preview preview() {
        List<NseOfficialEtfSecurityList.Listing> etfListings;
        try {
            etfListings = etfSecurityList.listAll();
        } catch (RuntimeException unavailable) {
            return Preview.unavailable();
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

        NseEtfListingValidation.Result validation = NseEtfListingValidation.validate(etfListings);

        List<Entry> malformed = toEntries(validation.malformed());
        List<Entry> authoritativeConflicts = new ArrayList<>(toEntries(validation.duplicates()));
        List<Entry> conflictVerificationIncomplete = new ArrayList<>();
        List<Entry> missingEtfs = new ArrayList<>();
        List<Entry> equityToEtfCandidates = new ArrayList<>();
        List<Entry> alreadyClassifiedEtfs = new ArrayList<>();

        for (NseEtfListingValidation.Candidate candidate : validation.accepted()) {
            String isin = candidate.isin();
            String symbol = candidate.symbol();

            Optional<InstrumentMasterEntity> existing = masters.findByNormalizedIsin(isin);
            if (existing.isEmpty()) {
                // Conflict status must be resolved BEFORE this ISIN is ever reported as missing.
                if (!ivSeriesSourceAvailable) {
                    conflictVerificationIncomplete.add(
                            new Entry(isin, symbol, "IV_SERIES_SOURCE_UNAVAILABLE_CANNOT_VERIFY_CONFLICT"));
                } else if (ivIsins.contains(isin)) {
                    authoritativeConflicts.add(new Entry(isin, symbol, "ISIN_IN_BOTH_ETF_LIST_AND_IV_SERIES"));
                } else {
                    missingEtfs.add(new Entry(isin, symbol, "MISSING"));
                }
                continue;
            }

            AssetType currentType = existing.get().getAssetType();
            if (currentType == AssetType.ETF) {
                alreadyClassifiedEtfs.add(new Entry(isin, symbol, "ALREADY_ETF"));
            } else if (currentType == AssetType.EQUITY) {
                // Likewise resolved BEFORE this ISIN is ever reported as an eligible candidate.
                if (!ivSeriesSourceAvailable) {
                    conflictVerificationIncomplete.add(
                            new Entry(isin, symbol, "IV_SERIES_SOURCE_UNAVAILABLE_CANNOT_VERIFY_CONFLICT"));
                } else if (ivIsins.contains(isin)) {
                    authoritativeConflicts.add(new Entry(isin, symbol, "ISIN_IN_BOTH_ETF_LIST_AND_IV_SERIES"));
                } else {
                    equityToEtfCandidates.add(new Entry(isin, symbol, "EQUITY_TO_ETF_CANDIDATE"));
                }
            } else {
                authoritativeConflicts.add(new Entry(isin, symbol, "EXISTING_NON_EQUITY_NON_ETF_ASSET_TYPE:" + currentType));
            }
        }

        return new Preview(true, ivSeriesSourceAvailable, missingEtfs, equityToEtfCandidates, alreadyClassifiedEtfs,
                malformed, authoritativeConflicts, conflictVerificationIncomplete);
    }

    private static List<Entry> toEntries(List<NseEtfListingValidation.Rejected> rejected) {
        List<Entry> entries = new ArrayList<>();
        for (NseEtfListingValidation.Rejected r : rejected) {
            entries.add(new Entry(r.isin(), r.symbol(), r.reason()));
        }
        return entries;
    }

    /** One ISIN's comparison outcome against existing {@code instrument_master} rows. */
    public record Entry(String isin, String symbol, String reason) {
    }

    /**
     * @param sourceAvailable               whether the official NSE ETF list itself was fetched successfully;
     *                                       false means every list below is empty and nothing was compared.
     * @param ivSeriesSourceAvailable        whether the official IV-series security master was available for
     *                                       conflict detection; false means {@code conflictVerificationIncomplete}
     *                                       holds every ISIN whose conflict status could not be checked, rather
     *                                       than those ISINs being silently treated as conflict-free.
     * @param conflictVerificationIncomplete ISINs that would otherwise be reported as missing or as an
     *                                       equity-to-ETF candidate, but whose IV-series conflict status is
     *                                       UNKNOWN because that source was unavailable at preview time.
     */
    public record Preview(
            boolean sourceAvailable,
            boolean ivSeriesSourceAvailable,
            List<Entry> missingEtfs,
            List<Entry> equityToEtfCandidates,
            List<Entry> alreadyClassifiedEtfs,
            List<Entry> malformed,
            List<Entry> authoritativeConflicts,
            List<Entry> conflictVerificationIncomplete
    ) {
        static Preview unavailable() {
            return new Preview(false, false, List.of(), List.of(), List.of(), List.of(), List.of(), List.of());
        }
    }
}
