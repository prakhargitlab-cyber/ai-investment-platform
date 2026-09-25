package com.aiinvestment.portfolio.application;

import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterEntity;
import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterRepository;
import com.aiinvestment.shared.domain.AssetType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.HashSet;
import java.util.List;
import java.util.Set;

/**
 * Bounded, idempotent, authoritative reclassification of EXISTING persisted NSE
 * {@code instrument_master} rows -- distinct from {@link NseMappingReconciliationService#reconcile}, which
 * is the per-instrument, incidental identity-reconciliation path and must stay free of bulk/network-heavy
 * work (see that class's trusted/legacy short-circuits). This service instead runs as one explicit,
 * self-contained pass: it loads NSE's two authoritative datasets ONCE EACH -- the full listed-equity
 * security master (for IV-series evidence) and the full ETF securities list (for ETF evidence) -- builds
 * in-memory ISIN lookup sets from them, and then walks the existing persisted NSE instrument rows,
 * applying only the classification changes that authoritative evidence actually supports. It never
 * performs a remote call per instrument.
 *
 * <p>Both corrections funnel through {@link InstrumentMasterEntity#applyValidatedAssetType}, which is
 * already a one-directional, EQUITY-only, sticky transition: a row that is already ETF or OTHER is left
 * untouched by construction, so this pass can never move a row away from ETF/OTHER back to EQUITY, and
 * re-running it against already-corrected data is a pure no-op (idempotent).
 *
 * <p>Identity join is ISIN-only and exact -- never symbol or company-name heuristics -- and a row whose
 * ISIN does not normalize, or whose ISIN is not found in the relevant authoritative dataset, is left
 * unchanged rather than guessed at. If the SAME ISIN is authoritative evidence for both ETF and IV-series
 * classification at once (a genuine conflict between the two NSE sources, which current NSE/domain
 * semantics give no proven precedence for), the row is left unchanged and the conflict is counted rather
 * than silently resolved one way or the other.
 *
 * <p>Availability of each source is tracked independently, so a down/invalid ETF list does not block an
 * IV correction that the (independently available) security master supports, and vice versa; if both are
 * unavailable, nothing is mutated. See {@link NseOfficialEtfSecurityList#listAll()} (throws on failure) and
 * {@link NseOfficialSecurityMaster#listedEquities()} (returns an empty list on failure -- an existing,
 * unchanged client contract that cannot distinguish "fetched, genuinely zero rows" from "failed"; treating
 * empty as unavailable here is the conservative, safe reading of that existing contract).
 *
 * <p><b>Not wired into any automatically-triggered path</b> (not called from
 * {@link CanonicalIdentityBootstrap#tick()} or any {@code @Scheduled} method) as of this change. This pass
 * is implemented and covered by tests against fixtures only; enabling it to run against live/production
 * persisted data is an explicit, separate decision to be made after live rows are inspected and reported.
 */
@Service
public class AuthoritativeAssetTypeReclassificationService {
    private static final Logger log = LoggerFactory.getLogger(AuthoritativeAssetTypeReclassificationService.class);
    private final InstrumentMasterRepository masters;
    private final NseOfficialSecurityMaster securityMaster;
    private final NseOfficialEtfSecurityList etfSecurityList;

    public AuthoritativeAssetTypeReclassificationService(InstrumentMasterRepository masters,
            NseOfficialSecurityMaster securityMaster, NseOfficialEtfSecurityList etfSecurityList) {
        this.masters = masters;
        this.securityMaster = securityMaster;
        this.etfSecurityList = etfSecurityList;
    }

    @Transactional
    public Summary reclassifyExistingNseInstruments() {
        Set<String> etfIsins = new HashSet<>();
        boolean etfListAvailable;
        try {
            List<NseOfficialEtfSecurityList.Listing> etfListings = etfSecurityList.listAll();
            etfListAvailable = true;
            for (NseOfficialEtfSecurityList.Listing listing : etfListings) {
                String isin = InstrumentMasterEntity.normalizeIsin(listing.isin());
                if (isin != null) etfIsins.add(isin);
            }
        } catch (RuntimeException unavailable) {
            etfListAvailable = false;
        }

        Set<String> ivIsins = new HashSet<>();
        boolean securityMasterAvailable;
        try {
            // listedEquities() fails closed to an empty list rather than throwing (existing, unchanged
            // contract) -- so a genuinely empty result is indistinguishable from a failed fetch here.
            // Treating empty as unavailable is the conservative reading: we would rather skip IV
            // correction than risk having silently seen zero rows because the fetch actually failed.
            List<NseOfficialSecurityMaster.Listing> equityListings = securityMaster.listedEquities();
            securityMasterAvailable = !equityListings.isEmpty();
            for (NseOfficialSecurityMaster.Listing listing : equityListings) {
                if (!"IV".equalsIgnoreCase(listing.series())) continue;
                String isin = InstrumentMasterEntity.normalizeIsin(listing.isin());
                if (isin != null) ivIsins.add(isin);
            }
        } catch (RuntimeException unavailable) {
            securityMasterAvailable = false;
        }

        Summary summary = new Summary();
        List<InstrumentMasterEntity> candidates =
                masters.findByCountryIgnoreCaseAndPrimaryExchangeIgnoreCaseAndStatusIgnoreCase("IN", "NSE", "ACTIVE");
        for (InstrumentMasterEntity master : candidates) {
            summary.examined++;
            String isin = InstrumentMasterEntity.normalizeIsin(master.getIsin());
            if (isin == null) {
                summary.missingIsin++;
                continue;
            }

            boolean etfMatch = etfListAvailable && etfIsins.contains(isin);
            boolean ivMatch = securityMasterAvailable && ivIsins.contains(isin);

            if (etfMatch && ivMatch) {
                summary.conflictingAuthoritativeType++;
                log.warn("authoritative_asset_type_conflict globalInstrumentId={} isin={} "
                        + "reason=ISIN_IN_BOTH_ETF_LIST_AND_IV_SERIES", master.getInstrumentId(), isin);
                continue;
            }

            AssetType before = master.getAssetType();
            if (etfMatch) {
                master.applyValidatedAssetType(AssetType.ETF);
            } else if (ivMatch) {
                master.applyValidatedAssetType(AssetType.OTHER);
            }
            AssetType after = master.getAssetType();

            if (before != after) {
                if (after == AssetType.ETF) summary.correctedToEtf++; else summary.correctedToOther++;
                log.info("authoritative_asset_type_corrected globalInstrumentId={} isin={} from={} to={}",
                        master.getInstrumentId(), isin, before, after);
            } else if (etfMatch || ivMatch) {
                summary.alreadyCorrect++;
            } else if (!etfListAvailable && !securityMasterAvailable) {
                summary.providerUnavailable++;
            } else {
                summary.noAuthoritativeMatch++;
            }
        }
        log.info("authoritative_asset_type_reclassification_complete {}", summary);
        return summary;
    }

    /** Bounded observability counts for one pass; never per-row logging beyond the corrected/conflict cases above. */
    public static final class Summary {
        public int examined;
        public int correctedToEtf;
        public int correctedToOther;
        public int alreadyCorrect;
        public int noAuthoritativeMatch;
        public int missingIsin;
        public int conflictingAuthoritativeType;
        public int providerUnavailable;

        @Override
        public String toString() {
            return "examined=" + examined + " correctedToEtf=" + correctedToEtf + " correctedToOther=" + correctedToOther
                    + " alreadyCorrect=" + alreadyCorrect + " noAuthoritativeMatch=" + noAuthoritativeMatch
                    + " missingIsin=" + missingIsin + " conflictingAuthoritativeType=" + conflictingAuthoritativeType
                    + " providerUnavailable=" + providerUnavailable;
        }
    }
}
