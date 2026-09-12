package com.aiinvestment.portfolio.application;

import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterEntity;
import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterRepository;
import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentProviderMappingEntity;
import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentProviderMappingRepository;
import com.aiinvestment.shared.domain.AssetType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.math.BigDecimal;
import java.util.Comparator;
import java.util.List;
import java.util.Locale;
import java.util.Optional;
import java.util.UUID;

/** Reconciles NSE identities through NSE's official symbol verifier or exact-ISIN security master. */
@Service
public class NseMappingReconciliationService {
    private static final Logger log = LoggerFactory.getLogger(NseMappingReconciliationService.class);
    private static final String NSE = "NSE";
    private static final String YAHOO = "YAHOO_FINANCE";
    private final InstrumentMasterRepository masters;
    private final InstrumentProviderMappingRepository mappings;
    private final InstrumentMasterService instrumentMaster;
    private final NseOfficialMappingVerifier verifier;
    private final NseOfficialSecurityMaster securityMaster;
    private final NseOfficialEtfSecurityList etfSecurityList;

    public NseMappingReconciliationService(InstrumentMasterRepository masters, InstrumentProviderMappingRepository mappings,
            InstrumentMasterService instrumentMaster, NseOfficialMappingVerifier verifier, NseOfficialSecurityMaster securityMaster,
            NseOfficialEtfSecurityList etfSecurityList) {
        this.masters = masters; this.mappings = mappings; this.instrumentMaster = instrumentMaster; this.verifier = verifier;
        this.securityMaster = securityMaster; this.etfSecurityList = etfSecurityList;
    }

    @Transactional
    public Outcome reconcile(UUID instrumentId) {
        log.info("nse_reconciliation_start globalInstrumentId={}", instrumentId);
        Optional<InstrumentMasterEntity> master = masters.findById(instrumentId);
        if (master.isEmpty()) return skipped(instrumentId, "MASTER_NOT_FOUND");
        if (!eligible(master.get())) return skipped(instrumentId, "NOT_NSE_EQUITY");
        List<InstrumentProviderMappingEntity> existing = mappings.findByInstrumentId(instrumentId);
        if (existing.stream().anyMatch(this::isTrustworthyNse)) return skipped(instrumentId, "TRUSTED_NSE_MAPPING_EXISTS");
        if (existing.stream().anyMatch(this::isLegacyBrokerNse)) return skipped(instrumentId, "LEGACY_NSE_MAPPING_REPAIR_REQUIRED");
        if (master.get().getAssetType() == AssetType.ETF) return bootstrapFromOfficialEtfList(instrumentId, master.get());
        Outcome officialMaster = bootstrapFromOfficialIsin(instrumentId, master.get());
        if ("PERSISTED".equals(officialMaster.status()) || !canFallbackFromOfficialMaster(officialMaster)) return officialMaster;

        String candidate = existing.stream().filter(this::isVerifiedYahooNse).map(InstrumentProviderMappingEntity::getProviderSymbol)
                .map(symbol -> symbol.substring(0, symbol.length() - 3)).map(value -> value.toUpperCase(Locale.ROOT))
                .sorted(Comparator.naturalOrder()).findFirst().orElse(null);
        if (candidate == null) return officialMaster;
        log.info("nse_reconciliation_candidate globalInstrumentId={} candidateSymbol={}", instrumentId, candidate);

        NseOfficialMappingVerifier.Verification verification = verifier.verify(candidate);
        log.info("nse_reconciliation_official_verification globalInstrumentId={} candidateSymbol={} recognized={} reason={}",
                instrumentId, candidate, verification.recognized(), verification.reason());
        if (!verification.recognized()) {
            if ("NSE_EMPTY_RESPONSE".equals(verification.reason())) {
                Outcome bootstrap = bootstrapFromOfficialIsin(instrumentId, master.get());
                if ("PERSISTED".equals(bootstrap.status())) return bootstrap;
            }
            return rejected(instrumentId, master.get(), candidate, verification.reason());
        }
        String expectedIsin = InstrumentMasterEntity.normalizeIsin(master.get().getIsin());
        String verifiedIsin = InstrumentMasterEntity.normalizeIsin(verification.isin());
        ValidationEvidence evidence = validationEvidence(master.get(), verification, expectedIsin, verifiedIsin);
        log.info("nse_reconciliation_identity_evidence globalInstrumentId={} candidateSymbol={} masterIsin={} officialIsin={} "
                        + "masterCanonicalName={} officialCompanyName={} normalizedMasterName={} normalizedOfficialName={} "
                        + "exactIsinMatch={} companyNameMatch={}",
                instrumentId, candidate, expectedIsin, verifiedIsin, master.get().getCanonicalName(), verification.companyName(),
                evidence.normalizedMasterName(), evidence.normalizedOfficialName(), evidence.exactIsinMatch(), evidence.companyNameMatch());
        if (!candidate.equalsIgnoreCase(verification.symbol())) return rejected(instrumentId, master.get(), candidate, "NSE_SYMBOL_MISMATCH");
        if (expectedIsin == null || verifiedIsin == null) return rejected(instrumentId, master.get(), candidate, "NSE_ISIN_MISSING");
        if (evidence.exactIsinMatch()) {
            log.info("nse_reconciliation_isin globalInstrumentId={} candidateSymbol={} match=true verificationMode=EXACT_ISIN", instrumentId, candidate);
            return persist(instrumentId, master.get(), verification, "EXACT_ISIN");
        }
        if (!evidence.companyNameMatch())
            return rejected(instrumentId, master.get(), candidate, "NSE_COMPANY_NAME_MISMATCH");
        boolean trustedCurrentIsin = hasTrustedCurrentIsin(existing, expectedIsin);
        boolean successorCompatible = compatibleIndianSuccessorIsin(expectedIsin, verifiedIsin);
        log.info("nse_reconciliation_successor_evidence globalInstrumentId={} candidateSymbol={} trustedCurrentIsin={} "
                        + "successorCompatibilityResult={} verificationMode=HISTORICAL_ISIN_SUCCESSOR",
                instrumentId, candidate, trustedCurrentIsin, successorCompatible);
        if (!trustedCurrentIsin)
            return rejected(instrumentId, master.get(), candidate, "CURRENT_ISIN_NOT_TRUSTED");
        if (!successorCompatible)
            return rejected(instrumentId, master.get(), candidate, "NSE_ISIN_NOT_COMPATIBLE");
        log.info("nse_reconciliation_isin globalInstrumentId={} candidateSymbol={} match=false verificationMode=HISTORICAL_ISIN_SUCCESSOR", instrumentId, candidate);
        return persist(instrumentId, master.get(), verification, "HISTORICAL_ISIN_SUCCESSOR");
    }

    private Outcome bootstrapFromOfficialIsin(UUID instrumentId, InstrumentMasterEntity master) {
        String expectedIsin = InstrumentMasterEntity.normalizeIsin(master.getIsin());
        if (expectedIsin == null) return skipped(instrumentId, "INVALID_ISIN");
        NseOfficialSecurityMaster.Lookup lookup = securityMaster.lookupByIsin(expectedIsin);
        if (!"MATCHED".equals(lookup.status()))
            return rejectedOfficialLookup(instrumentId, master, expectedIsin, "OFFICIAL_MASTER_" + lookup.status());
        if (lookup.symbol() == null || lookup.symbol().isBlank())
            return rejected(instrumentId, master, failureCandidate(master), "OFFICIAL_MASTER_SYMBOL_MISSING");
        if (!expectedIsin.equals(InstrumentMasterEntity.normalizeIsin(lookup.isin())))
            return rejected(instrumentId, master, lookup.symbol(), "OFFICIAL_MASTER_ISIN_MISMATCH");
        if (!officialListingCompatible(master, lookup.series(), lookup.companyName()))
            return rejected(instrumentId, master, lookup.symbol(), master.getAssetType() == AssetType.EQUITY
                    ? "OFFICIAL_MASTER_NOT_EQUITY_COMPATIBLE" : "OFFICIAL_MASTER_NOT_ETF_COMPATIBLE");
        // A single compatible official security-master record for the current
        // normalized ISIN is stronger identity evidence than a legal-name
        // variation.  Keep sameCompanyName mandatory in candidate/successor
        // verification below, where the ISIN is not exact.
        master.applyVerifiedPrimaryListing(NSE, lookup.symbol(), lookup.companyName());
        instrumentMaster.saveResolvedMapping(instrumentId, NSE, lookup.symbol().trim().toUpperCase(Locale.ROOT), null, NSE,
                master.getCurrency(), "VERIFIED", "NSE_OFFICIAL_ISIN_BOOTSTRAP", new BigDecimal("0.99"));
        log.info("nse_reconciliation_persist globalInstrumentId={} candidateSymbol={} result=PERSISTED verificationMode=OFFICIAL_ISIN_BOOTSTRAP",
                instrumentId, lookup.symbol());
        return new Outcome("PERSISTED", lookup.symbol(), "OFFICIAL_MASTER_MATCHED");
    }

    private Outcome bootstrapFromOfficialEtfList(UUID instrumentId, InstrumentMasterEntity master) {
        String expectedIsin = InstrumentMasterEntity.normalizeIsin(master.getIsin());
        if (expectedIsin == null) return skipped(instrumentId, "INVALID_ISIN");
        NseOfficialEtfSecurityList.Lookup lookup = etfSecurityList.lookupByIsin(expectedIsin);
        if (!"MATCHED".equals(lookup.status())) return skipped(instrumentId, "OFFICIAL_ETF_LIST_" + lookup.status());
        if (!expectedIsin.equals(InstrumentMasterEntity.normalizeIsin(lookup.isin())))
            return Outcome.rejected(lookup.symbol(), "OFFICIAL_ETF_LIST_ISIN_MISMATCH");
        master.applyVerifiedPrimaryListing(NSE, lookup.symbol(), lookup.securityName());
        instrumentMaster.saveResolvedMapping(instrumentId, NSE, lookup.symbol().trim().toUpperCase(Locale.ROOT), null, NSE,
                master.getCurrency(), "VERIFIED", "NSE_OFFICIAL_ETF_SECURITY_LIST", new BigDecimal("0.99"));
        log.info("nse_reconciliation_persist globalInstrumentId={} candidateSymbol={} result=PERSISTED verificationMode=OFFICIAL_ETF_SECURITY_LIST",
                instrumentId, lookup.symbol());
        return new Outcome("PERSISTED", lookup.symbol(), "OFFICIAL_ETF_LIST_MATCHED");
    }

    private Outcome persist(UUID instrumentId, InstrumentMasterEntity master, NseOfficialMappingVerifier.Verification verification,
            String verificationMode) {
        master.applyVerifiedPrimaryListing(NSE, verification.symbol(), verification.companyName());
        instrumentMaster.saveResolvedMapping(instrumentId, NSE, verification.symbol(), null, NSE, master.getCurrency(),
                "VERIFIED", "NSE_VALIDATED_RESOLUTION", new BigDecimal("0.99"));
        log.info("nse_reconciliation_persist globalInstrumentId={} candidateSymbol={} result=PERSISTED verificationMode={}", instrumentId,
                verification.symbol(), verificationMode);
        return Outcome.persisted(verification.symbol());
    }

    private Outcome rejected(UUID instrumentId, InstrumentMasterEntity master, String candidate, String reason) {
        instrumentMaster.recordMappingFailure(instrumentId, NSE, candidate, null, NSE, master.getCurrency(),
                "NSE_VALIDATION_FAILED", BigDecimal.ZERO, reason);
        return Outcome.rejected(candidate, reason);
    }

    private Outcome rejectedOfficialLookup(UUID instrumentId, InstrumentMasterEntity master,
                                           String expectedIsin, String reason) {
        String candidate = failureCandidate(master);
        instrumentMaster.recordMappingFailure(instrumentId, NSE, candidate, "ISIN:" + expectedIsin, NSE,
                master.getCurrency(), "NSE_OFFICIAL_ISIN_LOOKUP", BigDecimal.ZERO, reason);
        log.info("nse_reconciliation_skip globalInstrumentId={} reason={}", instrumentId, reason);
        return Outcome.skipped(reason);
    }

    private static String failureCandidate(InstrumentMasterEntity master) {
        String symbol = master.getPrimarySymbol();
        return symbol == null || symbol.isBlank() ? "ISIN_LOOKUP" : symbol.trim().toUpperCase(Locale.ROOT);
    }

    private static Outcome skipped(UUID instrumentId, String reason) {
        log.info("nse_reconciliation_skip globalInstrumentId={} reason={}", instrumentId, reason);
        return Outcome.skipped(reason);
    }
    private static boolean canFallbackFromOfficialMaster(Outcome outcome) {
        return "SKIPPED".equals(outcome.status()) && ("OFFICIAL_MASTER_NO_ISIN_MATCH".equals(outcome.reason())
                || "OFFICIAL_MASTER_UNAVAILABLE".equals(outcome.reason()));
    }

    static boolean eligible(InstrumentMasterEntity master) {
        if (master == null || (master.getAssetType() != AssetType.EQUITY && master.getAssetType() != AssetType.ETF)
                || !"IN".equalsIgnoreCase(master.getCountry())) return false;
        String exchange = master.getPrimaryExchange() == null ? "" : master.getPrimaryExchange().trim().toUpperCase(Locale.ROOT);
        if (exchange.contains(NSE)) return true;
        if (!exchange.isEmpty() && !"UNKNOWN".equals(exchange)) return false;
        String isin = InstrumentMasterEntity.normalizeIsin(master.getIsin());
        return isin != null && isin.startsWith("IN");
    }
    private boolean isTrustworthyNse(InstrumentProviderMappingEntity mapping) {
        return NSE.equalsIgnoreCase(mapping.getProvider()) && "VERIFIED".equalsIgnoreCase(mapping.getStatus())
                && !"BROKER_IMPORT_IDENTITY".equalsIgnoreCase(mapping.getResolutionSource());
    }
    private boolean isLegacyBrokerNse(InstrumentProviderMappingEntity mapping) {
        return NSE.equalsIgnoreCase(mapping.getProvider()) && "VERIFIED".equalsIgnoreCase(mapping.getStatus())
                && "BROKER_IMPORT_IDENTITY".equalsIgnoreCase(mapping.getResolutionSource());
    }
    private boolean isVerifiedYahooNse(InstrumentProviderMappingEntity mapping) {
        return YAHOO.equalsIgnoreCase(mapping.getProvider()) && "VERIFIED".equalsIgnoreCase(mapping.getStatus())
                && mapping.getProviderSymbol() != null && mapping.getProviderSymbol().toUpperCase(Locale.ROOT).endsWith(".NS");
    }
    private boolean hasTrustedCurrentIsin(List<InstrumentProviderMappingEntity> mappings, String expectedIsin) {
        return mappings.stream().anyMatch(mapping -> "VERIFIED".equalsIgnoreCase(mapping.getStatus())
                && !NSE.equalsIgnoreCase(mapping.getProvider()) && !YAHOO.equalsIgnoreCase(mapping.getProvider())
                && expectedIsin.equals(explicitIsin(mapping.getProviderInstrumentId())));
    }
    private static String explicitIsin(String providerInstrumentId) {
        if (providerInstrumentId == null || !providerInstrumentId.regionMatches(true, 0, "ISIN:", 0, 5)) return null;
        return InstrumentMasterEntity.normalizeIsin(providerInstrumentId.substring(5));
    }
    private static boolean compatibleIndianSuccessorIsin(String currentIsin, String historicalIsin) {
        return currentIsin.startsWith("IN") && historicalIsin.startsWith("IN")
                && currentIsin.substring(0, 10).equals(historicalIsin.substring(0, 10));
    }
    private static boolean sameCompanyName(String canonicalName, String officialName) {
        return normalizeCompanyName(canonicalName).equals(normalizeCompanyName(officialName));
    }
    static ValidationEvidence validationEvidence(InstrumentMasterEntity master, NseOfficialMappingVerifier.Verification verification,
            String expectedIsin, String verifiedIsin) {
        String normalizedMasterName = normalizeCompanyName(master == null ? null : master.getCanonicalName());
        String normalizedOfficialName = normalizeCompanyName(verification == null ? null : verification.companyName());
        return new ValidationEvidence(
                expectedIsin != null && expectedIsin.equals(verifiedIsin),
                normalizedMasterName.equals(normalizedOfficialName), normalizedMasterName, normalizedOfficialName);
    }
    private static boolean equityCompatibleSeries(String series) {
        if (series == null) return false;
        return switch (series.trim().toUpperCase(Locale.ROOT)) {
            case "EQ", "BE", "BZ", "SM", "ST", "IV" -> true;
            default -> false;
        };
    }
    private static boolean officialListingCompatible(InstrumentMasterEntity master, String series, String companyName) {
        if (master.getAssetType() == AssetType.EQUITY) return equityCompatibleSeries(series);
        if (master.getAssetType() != AssetType.ETF || series == null || !"EQ".equalsIgnoreCase(series.trim())) return false;
        String name = String.valueOf(companyName).toUpperCase(Locale.ROOT);
        return name.contains("ETF") || name.contains("EXCHANGE TRADED FUND") || name.contains("BEES");
    }
    private static String normalizeCompanyName(String value) {
        if (value == null) return "";
        String normalized = value.toUpperCase(Locale.ROOT).replace("&", " AND ").replaceAll("[^A-Z0-9]+", " ")
                .trim().replaceAll("\\s+", " ");
        if (normalized.startsWith("THE ")) normalized = normalized.substring(4);
        if (normalized.endsWith(" LIMITED")) normalized = normalized.substring(0, normalized.length() - 8).trim();
        if (normalized.endsWith(" LTD")) normalized = normalized.substring(0, normalized.length() - 4).trim();
        StringBuilder canonical = new StringBuilder();
        for (String token : normalized.split(" ")) {
            String equivalent = switch (token) {
                case "FIN" -> "FINANCE";
                case "CORP" -> "CORPORATION";
                default -> token;
            };
            if (!canonical.isEmpty()) canonical.append(' ');
            canonical.append(equivalent);
        }
        return canonical.toString();
    }

    public record Outcome(String status, String candidate, String reason) {
        static Outcome skipped(String reason) { return new Outcome("SKIPPED", null, reason); }
        static Outcome rejected(String candidate, String reason) { return new Outcome("REJECTED", candidate, reason); }
        static Outcome persisted(String candidate) { return new Outcome("PERSISTED", candidate, null); }
    }
    record ValidationEvidence(boolean exactIsinMatch, boolean companyNameMatch, String normalizedMasterName,
                              String normalizedOfficialName) {}
}
