package com.aiinvestment.broker.runtime;

import java.time.Duration;

@FunctionalInterface
interface RuntimeWaitStrategy {
    void await(Duration duration) throws InterruptedException;
}
