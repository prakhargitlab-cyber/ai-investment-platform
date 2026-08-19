package com.aiinvestment.portfolio.infrastructure.events;

import com.aiinvestment.portfolio.application.PlatformEventPublisher;
import com.aiinvestment.shared.domain.event.PlatformEvent;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

@Component
public class LoggingPlatformEventPublisher implements PlatformEventPublisher {
    private static final Logger log = LoggerFactory.getLogger(LoggingPlatformEventPublisher.class);

    @Override
    public void publish(PlatformEvent event) {
        log.info("eventType={} version={} eventId={} correlationId={}",
                event.eventType(), event.version(), event.eventId(), event.correlationId());
    }
}
