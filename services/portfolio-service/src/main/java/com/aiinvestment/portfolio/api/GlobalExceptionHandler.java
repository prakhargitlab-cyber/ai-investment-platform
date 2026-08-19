package com.aiinvestment.portfolio.api;

import com.aiinvestment.portfolio.application.PortfolioNotFoundException;
import com.aiinvestment.shared.web.CorrelationIdFilter;
import jakarta.servlet.http.HttpServletRequest;
import org.slf4j.MDC;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

import java.time.Instant;

@RestControllerAdvice
public class GlobalExceptionHandler {
    @ExceptionHandler(MethodArgumentNotValidException.class)
    public ResponseEntity<ApiErrorResponse> validation(MethodArgumentNotValidException ex, HttpServletRequest request) {
        return error(HttpStatus.BAD_REQUEST, "INVALID_PORTFOLIO_REQUEST", "Invalid portfolio request", request);
    }

    @ExceptionHandler({IllegalArgumentException.class})
    public ResponseEntity<ApiErrorResponse> illegalArgument(IllegalArgumentException ex, HttpServletRequest request) {
        return error(HttpStatus.BAD_REQUEST, "INVALID_PORTFOLIO_REQUEST", ex.getMessage(), request);
    }

    @ExceptionHandler(PortfolioNotFoundException.class)
    public ResponseEntity<ApiErrorResponse> notFound(PortfolioNotFoundException ex, HttpServletRequest request) {
        return error(HttpStatus.NOT_FOUND, "PORTFOLIO_NOT_FOUND", ex.getMessage(), request);
    }

    private ResponseEntity<ApiErrorResponse> error(HttpStatus status, String code, String message, HttpServletRequest request) {
        String correlationId = MDC.get("correlationId");
        if (correlationId == null || correlationId.isBlank()) {
            correlationId = request.getHeader(CorrelationIdFilter.HEADER_NAME);
        }
        return ResponseEntity.status(status).body(new ApiErrorResponse(Instant.now(), status.value(), code, message, correlationId));
    }
}
