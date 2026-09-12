package com.aiinvestment.portfolio.application;

import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentProviderMappingEntity;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.UUID;

/** Owns public provider reconciliation for a global instrument. */
@Service
public class GlobalInstrumentReconciliationService {
    private static final Logger log = LoggerFactory.getLogger(GlobalInstrumentReconciliationService.class);
    private final InstrumentMasterService instruments;
    private final NseMappingReconciliationService nse;
    private final StructuredMarketClient structuredMarket;

    public GlobalInstrumentReconciliationService(InstrumentMasterService instruments, NseMappingReconciliationService nse,
            StructuredMarketClient structuredMarket) {
        this.instruments = instruments;
        this.nse = nse;
        this.structuredMarket = structuredMarket;
    }

    public Outcome reconcile(UUID globalInstrumentId) {
        if (instruments.globalInstrument(globalInstrumentId).isEmpty()) throw new IllegalArgumentException("GLOBAL_INSTRUMENT_NOT_FOUND");
        log.info("global_provider_reconciliation_start globalInstrumentId={}", globalInstrumentId);
        var nseOutcome = nse.reconcile(globalInstrumentId);
        log.info("global_provider_reconciliation_nse globalInstrumentId={} outcome={} reason={}",
                globalInstrumentId, nseOutcome.status(), nseOutcome.reason());
        if (instruments.reusableMapping(globalInstrumentId, "YAHOO_FINANCE").isPresent()) {
            log.info("global_provider_reconciliation_yahoo globalInstrumentId={} outcome=REUSED reason=VERIFIED_MAPPING_EXISTS", globalInstrumentId);
            return new Outcome("SKIPPED", "VERIFIED_MAPPING_EXISTS");
        }
        if (instruments.mappings(globalInstrumentId).stream().anyMatch(mapping -> "YAHOO_FINANCE".equals(mapping.getProvider()) && "INVALID".equals(mapping.getStatus())))
            return new Outcome("REJECTED", "INVALID_MAPPING_REQUIRES_REVIEW");
        boolean trustedNse = instruments.mappings(globalInstrumentId).stream().anyMatch(this::trustedNse);
        if (!trustedNse) {
            log.info("global_provider_reconciliation_yahoo globalInstrumentId={} outcome=REJECTED reason=NO_TRUSTED_NSE_MAPPING", globalInstrumentId);
            return new Outcome("REJECTED", "NSE_MAPPING_MISSING");
        }
        try {
            structuredMarket.resolveGlobalIdentity(globalInstrumentId);
            log.info("global_provider_reconciliation_yahoo globalInstrumentId={} candidateSource=VERIFIED_NSE outcome=PERSISTED reason=VALIDATED", globalInstrumentId);
            return new Outcome("VALIDATED", "YAHOO_MAPPING_VALIDATED");
        } catch (StructuredMarketClient.IdentityRejectedException exception) {
            return new Outcome("REJECTED", exception.getMessage());
        } catch (RuntimeException exception) {
            log.info("global_provider_reconciliation_yahoo globalInstrumentId={} candidateSource=VERIFIED_NSE outcome=UNAVAILABLE reason={}",
                    globalInstrumentId, exception.getMessage());
            return new Outcome("UNAVAILABLE", "PROVIDER_TEMPORARILY_UNAVAILABLE");
        }
    }

    public record Outcome(String status, String reason) {}

    private boolean trustedNse(InstrumentProviderMappingEntity mapping) {
        return "NSE".equalsIgnoreCase(mapping.getProvider()) && "VERIFIED".equalsIgnoreCase(mapping.getStatus())
                && mapping.getProviderSymbol() != null && !mapping.getProviderSymbol().isBlank()
                && !"BROKER_IMPORT_IDENTITY".equalsIgnoreCase(mapping.getResolutionSource());
    }
}
