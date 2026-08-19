package com.aiinvestment.portfolio.application;

import com.aiinvestment.shared.domain.event.PlatformEvent;

public interface PlatformEventPublisher {
    void publish(PlatformEvent event);
}
