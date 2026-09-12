package com.aiinvestment.shared.web;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.slf4j.MDC;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;
import java.util.UUID;

@Component
public class CorrelationIdFilter extends OncePerRequestFilter {
    public static final String HEADER_NAME = "X-Correlation-Id";
    public static final String REQUEST_ID_HEADER_NAME = "X-Request-ID";
    private static final String MDC_KEY = "correlationId";
    private static final int MAX_ID_LENGTH = 120;

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response, FilterChain filterChain)
            throws ServletException, IOException {
        String correlationId = firstValid(request.getHeader(REQUEST_ID_HEADER_NAME), request.getHeader(HEADER_NAME));
        if (correlationId == null) {
            correlationId = UUID.randomUUID().toString();
        }
        MDC.put(MDC_KEY, correlationId);
        response.setHeader(HEADER_NAME, correlationId);
        response.setHeader(REQUEST_ID_HEADER_NAME, correlationId);
        try {
            filterChain.doFilter(request, response);
        } finally {
            MDC.remove(MDC_KEY);
        }
    }

    public static String currentId() {
        return MDC.get(MDC_KEY);
    }

    private static String firstValid(String... candidates) {
        for (String candidate : candidates) {
            if (candidate == null) {
                continue;
            }
            String value = candidate.trim();
            if (!value.isEmpty() && value.length() <= MAX_ID_LENGTH
                    && value.chars().allMatch(character -> Character.isLetterOrDigit(character)
                    || character == '-' || character == '_' || character == '.' || character == ':' || character == '/')) {
                return value;
            }
        }
        return null;
    }
}
