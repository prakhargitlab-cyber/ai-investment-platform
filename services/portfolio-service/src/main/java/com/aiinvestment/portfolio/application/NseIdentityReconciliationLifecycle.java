package com.aiinvestment.portfolio.application;

import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentProviderMappingEntity;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import org.springframework.transaction.event.TransactionPhase;
import org.springframework.transaction.event.TransactionalEventListener;

/** Performs non-blocking global NSE identity recovery after an instrument is durably attached. */
@Component
public class NseIdentityReconciliationLifecycle {
    private static final Logger log = LoggerFactory.getLogger(NseIdentityReconciliationLifecycle.class);
    private final InstrumentMasterService instruments;
    private final NseMappingReconciliationService nse;

    public NseIdentityReconciliationLifecycle(InstrumentMasterService instruments, NseMappingReconciliationService nse) {
        this.instruments = instruments;
        this.nse = nse;
    }

    @TransactionalEventListener(phase = TransactionPhase.AFTER_COMMIT)
    public void reconcileAfterAttachment(GlobalInstrumentAttachedEvent event) {
        instruments.globalInstrument(event.globalInstrumentId()).ifPresent(global -> {
            var master = global.master();
            if (!NseMappingReconciliationService.eligible(master)
                    || global.providerMappings().stream().anyMatch(this::blocksReconciliation)) return;
            try {
                nse.reconcile(event.globalInstrumentId());
            } catch (RuntimeException exception) {
                log.info("nse_identity_lifecycle globalInstrumentId={} outcome=UNAVAILABLE reason={}",
                        event.globalInstrumentId(), exception.getMessage());
            }
        });
    }

    private boolean blocksReconciliation(InstrumentProviderMappingEntity mapping) {
        return "NSE".equalsIgnoreCase(mapping.getProvider()) && "VERIFIED".equalsIgnoreCase(mapping.getStatus());
    }
}
