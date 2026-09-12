package com.aiinvestment.broker.api;

import com.aiinvestment.broker.application.BrokerConnectionNotFoundException;
import com.aiinvestment.broker.application.BrokerProviderException;
import org.slf4j.MDC;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

import java.time.Instant;

@RestControllerAdvice
public class GlobalExceptionHandler {
    @ExceptionHandler(BrokerConnectionNotFoundException.class)
    public ResponseEntity<ApiErrorResponse> notFound(BrokerConnectionNotFoundException exception) {
        return error(HttpStatus.NOT_FOUND, "BROKER_CONNECTION_NOT_FOUND", exception.getMessage());
    }

    @ExceptionHandler(IllegalArgumentException.class)
    public ResponseEntity<ApiErrorResponse> badRequest(IllegalArgumentException exception) {
        return error(HttpStatus.BAD_REQUEST, "INVALID_BROKER_REQUEST", exception.getMessage());
    }

    @ExceptionHandler(IllegalStateException.class)
    public ResponseEntity<ApiErrorResponse> unavailable(IllegalStateException exception) {
        return error(HttpStatus.BAD_REQUEST, "BROKER_PROVIDER_UNAVAILABLE", exception.getMessage());
    }

    @ExceptionHandler(BrokerProviderException.class)
    public ResponseEntity<ApiErrorResponse> brokerProvider(BrokerProviderException exception) {
        return error(exception.status(), exception.code(), exception.getMessage());
    }

    private ResponseEntity<ApiErrorResponse> error(HttpStatus status, String code, String message) {
        String correlationId = MDC.get("correlationId");
        return ResponseEntity.status(status)
                .body(new ApiErrorResponse(Instant.now(), status.value(), code, message, correlationId));
    }
}
