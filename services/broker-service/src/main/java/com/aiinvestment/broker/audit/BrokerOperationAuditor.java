package com.aiinvestment.broker.audit;

import com.aiinvestment.shared.domain.broker.BrokerType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;
import org.springframework.stereotype.Component;

import java.time.Duration;
import java.time.Instant;

@Component
public class BrokerOperationAuditor {
    private static final Logger LOGGER = LoggerFactory.getLogger(BrokerOperationAuditor.class);

    public void record(BrokerType brokerType, String operation, Instant startedAt, boolean success, String statusCode) {
        LOGGER.info("broker_operation provider={} operation={} success={} durationMs={} correlationId={} status={}",
                brokerType, operation, success, Duration.between(startedAt, Instant.now()).toMillis(),
                MDC.get("correlationId"), statusCode);
    }
}
