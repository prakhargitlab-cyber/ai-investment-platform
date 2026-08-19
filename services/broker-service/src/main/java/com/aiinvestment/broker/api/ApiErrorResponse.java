package com.aiinvestment.broker.api;

import java.time.Instant;

public record ApiErrorResponse(Instant timestamp, int status, String code, String message, String correlationId) {
}
