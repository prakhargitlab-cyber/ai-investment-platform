package com.aiinvestment.portfolio.api;

import com.aiinvestment.portfolio.application.PortfolioNotFoundException;
import com.aiinvestment.portfolio.application.WatchlistNotFoundException;
import com.aiinvestment.portfolio.application.WatchlistRegionMismatchException;
import com.aiinvestment.shared.web.CorrelationIdFilter;
import jakarta.servlet.http.HttpServletRequest;
import org.slf4j.MDC;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

import java.time.Instant;
import java.util.List;
import org.springframework.web.multipart.MultipartException;
import org.springframework.web.multipart.support.MissingServletRequestPartException;

@RestControllerAdvice
public class GlobalExceptionHandler {
    @ExceptionHandler(MethodArgumentNotValidException.class)
    public ResponseEntity<ApiErrorResponse> validation(MethodArgumentNotValidException ex, HttpServletRequest request) {
        return error(HttpStatus.BAD_REQUEST, "INVALID_PORTFOLIO_REQUEST", "Invalid portfolio request", request);
    }

    @ExceptionHandler({IllegalArgumentException.class})
    public ResponseEntity<ApiErrorResponse> illegalArgument(IllegalArgumentException ex, HttpServletRequest request) {
        String code = request.getRequestURI().contains("/imports/")
                ? "INVALID_PORTFOLIO_FILE" : "INVALID_PORTFOLIO_REQUEST";
        return error(HttpStatus.BAD_REQUEST, code, ex.getMessage(), request);
    }

    @ExceptionHandler({MultipartException.class, MissingServletRequestPartException.class})
    public ResponseEntity<ApiErrorResponse> invalidMultipart(Exception ex, HttpServletRequest request) {
        return error(HttpStatus.BAD_REQUEST, "INVALID_PORTFOLIO_FILE",
                "A CSV file is required. Please choose the broker portfolio statement and try again.", request);
    }

    @ExceptionHandler(PortfolioNotFoundException.class)
    public ResponseEntity<ApiErrorResponse> notFound(PortfolioNotFoundException ex, HttpServletRequest request) {
        return error(HttpStatus.NOT_FOUND, "PORTFOLIO_NOT_FOUND", ex.getMessage(), request);
    }

    @ExceptionHandler(WatchlistNotFoundException.class)
    public ResponseEntity<ApiErrorResponse> watchlistNotFound(WatchlistNotFoundException ex, HttpServletRequest request) {
        return error(HttpStatus.NOT_FOUND, "WATCHLIST_NOT_FOUND", ex.getMessage(), request);
    }

    @ExceptionHandler(WatchlistRegionMismatchException.class)
    public ResponseEntity<ApiErrorResponse> watchlistRegionMismatch(WatchlistRegionMismatchException ex,
                                                                     HttpServletRequest request) {
        return error(HttpStatus.CONFLICT, "WATCHLIST_REGION_MISMATCH", ex.getMessage(), request);
    }

    private ResponseEntity<ApiErrorResponse> error(HttpStatus status, String code, String message, HttpServletRequest request) {
        String correlationId = MDC.get("correlationId");
        if (correlationId == null || correlationId.isBlank()) {
            correlationId = request.getHeader(CorrelationIdFilter.HEADER_NAME);
        }
        return ResponseEntity.status(status).body(new ApiErrorResponse(
                Instant.now(), status.value(), code, message, correlationId, List.of()));
    }
}
