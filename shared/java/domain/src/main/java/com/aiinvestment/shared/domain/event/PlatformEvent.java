package com.aiinvestment.shared.domain.event;

import java.time.Instant;
import java.util.UUID;

public interface PlatformEvent {
    String eventType();

    int version();

    UUID eventId();

    String correlationId();

    Instant occurredAt();
}
