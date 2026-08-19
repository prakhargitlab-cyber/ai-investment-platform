package com.aiinvestment.apigateway;

import jakarta.servlet.http.HttpServletRequest;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.*;
import org.springframework.util.StreamUtils;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.client.RestClient;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.Collections;

@RestController
public class PortfolioRouteController {
    private final String portfolioServiceBaseUrl;
    private final String brokerServiceBaseUrl;
    private final String researchEngineBaseUrl;
    private final RestClient restClient;

    public PortfolioRouteController(@Value("${portfolio.service.base-url}") String portfolioServiceBaseUrl,
                                    @Value("${broker.service.base-url}") String brokerServiceBaseUrl,
                                    @Value("${research.engine.base-url}") String researchEngineBaseUrl) {
        this.portfolioServiceBaseUrl = portfolioServiceBaseUrl;
        this.brokerServiceBaseUrl = brokerServiceBaseUrl;
        this.researchEngineBaseUrl = researchEngineBaseUrl;
        this.restClient = RestClient.builder().build();
    }

    @RequestMapping("/api/v1/portfolios/**")
    public ResponseEntity<String> route(HttpServletRequest request) throws IOException {
        return forward(request, portfolioServiceBaseUrl);
    }

    @RequestMapping({"/api/v1/brokers/**", "/api/v1/brokers", "/api/v1/broker-connections/**", "/api/v1/broker-connections"})
    public ResponseEntity<String> routeBroker(HttpServletRequest request) throws IOException {
        return forward(request, brokerServiceBaseUrl);
    }

    @RequestMapping({"/api/v1/research/**", "/api/v1/research"})
    public ResponseEntity<String> routeResearch(HttpServletRequest request) throws IOException {
        return forward(request, researchEngineBaseUrl);
    }

    private ResponseEntity<String> forward(HttpServletRequest request, String baseUrl) throws IOException {
        String path = request.getRequestURI();
        String query = request.getQueryString();
        String target = baseUrl + path + (query == null ? "" : "?" + query);
        String body = StreamUtils.copyToString(request.getInputStream(), StandardCharsets.UTF_8);
        HttpHeaders headers = new HttpHeaders();
        Collections.list(request.getHeaderNames()).forEach(name -> headers.add(name, request.getHeader(name)));
        return restClient.method(HttpMethod.valueOf(request.getMethod()))
                .uri(target)
                .headers(outbound -> outbound.addAll(headers))
                .body(body)
                .retrieve()
                .toEntity(String.class);
    }
}
