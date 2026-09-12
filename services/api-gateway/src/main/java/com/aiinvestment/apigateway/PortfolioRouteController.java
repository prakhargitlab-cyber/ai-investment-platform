package com.aiinvestment.apigateway;

import jakarta.servlet.http.HttpServletRequest;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.*;
import org.springframework.http.client.JdkClientHttpRequestFactory;
import org.springframework.util.StreamUtils;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.ResourceAccessException;
import com.aiinvestment.shared.web.auth.AuthenticationHeaders;
import com.aiinvestment.shared.web.CorrelationIdFilter;

import java.io.IOException;
import java.net.http.HttpClient;
import java.net.http.HttpTimeoutException;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Collections;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.TimeoutException;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

@RestController
public class PortfolioRouteController {
    private static final Logger logger = LoggerFactory.getLogger(PortfolioRouteController.class);
    private static final Set<String> HOP_BY_HOP_HEADERS = Set.of(
            "connection",
            "keep-alive",
            "proxy-authenticate",
            "proxy-authorization",
            "te",
            "trailer",
            "transfer-encoding",
            "upgrade",
            "host",
            "content-length"
    );
    private static final Pattern CONNECTOR_SESSION_PATH = Pattern.compile(
            "^/connector-sessions/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?:/|$)"
    );

    private final String portfolioServiceBaseUrl;
    private final String brokerServiceBaseUrl;
    private final String ibkrConnectorBaseUrl;
    private final String companyServiceBaseUrl;
    private final String authServiceBaseUrl;
    private final String researchEngineBaseUrl;
    private final String frontendBaseUrl;
    private final RestClient restClient;
    private final RestClient connectorSessionRestClient;

    @Autowired
    public PortfolioRouteController(@Value("${portfolio.service.base-url}") String portfolioServiceBaseUrl,
                                    @Value("${broker.service.base-url}") String brokerServiceBaseUrl,
                                    @Value("${ibkr.connector.base-url}") String ibkrConnectorBaseUrl,
                                    @Value("${company.service.base-url}") String companyServiceBaseUrl,
                                    @Value("${auth.service.base-url}") String authServiceBaseUrl,
                                    @Value("${research.engine.base-url}") String researchEngineBaseUrl,
                                    @Value("${frontend.base-url}") String frontendBaseUrl,
                                    @Value("${gateway.http.connect-timeout:3s}") Duration connectTimeout,
                                    @Value("${gateway.http.read-timeout:30s}") Duration readTimeout) {
        this.portfolioServiceBaseUrl = portfolioServiceBaseUrl;
        this.brokerServiceBaseUrl = brokerServiceBaseUrl;
        this.ibkrConnectorBaseUrl = ibkrConnectorBaseUrl;
        this.companyServiceBaseUrl = companyServiceBaseUrl;
        this.authServiceBaseUrl = authServiceBaseUrl;
        this.researchEngineBaseUrl = researchEngineBaseUrl;
        this.frontendBaseUrl = frontendBaseUrl;
        this.restClient = RestClient.builder()
            .requestFactory(connectorSessionRequestFactory(connectTimeout, readTimeout))
            .build();
        this.connectorSessionRestClient = connectorSessionRestClient(connectTimeout, readTimeout);
    }

    PortfolioRouteController(String portfolioServiceBaseUrl, String brokerServiceBaseUrl,
                             String ibkrConnectorBaseUrl, String companyServiceBaseUrl,
                             String authServiceBaseUrl, String researchEngineBaseUrl,
                             String frontendBaseUrl) {
        this(portfolioServiceBaseUrl, brokerServiceBaseUrl, ibkrConnectorBaseUrl, companyServiceBaseUrl,
                authServiceBaseUrl, researchEngineBaseUrl, frontendBaseUrl,
                Duration.ofSeconds(3), Duration.ofSeconds(30));
    }

    @RequestMapping({"/api/v1/auth/**", "/api/v1/auth"})
    public ResponseEntity<String> routeAuth(HttpServletRequest request) throws IOException {
        return forward(request, authServiceBaseUrl, false);
    }

    @RequestMapping({
        "/api/v1/portfolios",
        "/api/v1/portfolios/**",
        "/api/v1/instruments",
        "/api/v1/instruments/**"
    })
    public ResponseEntity<byte[]> route(HttpServletRequest request) throws IOException {
        return forwardBytes(request, portfolioServiceBaseUrl, true, restClient);
    }

    @RequestMapping({"/api/v1/brokers/**", "/api/v1/brokers", "/api/v1/broker-connections/**", "/api/v1/broker-connections",
            "/api/v1/broker-connectors/**", "/api/v1/broker-connectors"})
    public ResponseEntity<String> routeBroker(HttpServletRequest request) throws IOException {
        return forward(request, brokerServiceBaseUrl, true);
    }

    @RequestMapping({"/connector-sessions/**", "/connector-sessions"})
    public ResponseEntity<byte[]> routeConnectorLogin(HttpServletRequest request) throws IOException {
        String requestUri = request.getRequestURI();
        String target = CONNECTOR_SESSION_PATH.matcher(requestUri).find()
                ? connectorRuntimeBaseUrl(requestUri)
                : ibkrConnectorBaseUrl;
        return forwardBytes(request, target, false);
    }

    private static String connectorRuntimeBaseUrl(String requestUri) {
        Matcher matcher = CONNECTOR_SESSION_PATH.matcher(requestUri);
        if (!matcher.find()) {
            throw new IllegalArgumentException("Connector session URL must include a connector UUID.");
        }
        String compactId = UUID.fromString(matcher.group(1)).toString().replace("-", "");
        return "http://ibkr-runtime-" + compactId.substring(0, 12);
    }

    @RequestMapping({"/api/v1/companies/**", "/api/v1/companies"})
    public ResponseEntity<String> routeCompany(HttpServletRequest request) throws IOException {
        return forward(request, companyServiceBaseUrl, true);
    }

    @RequestMapping({"/api/v1/research/**", "/api/v1/research"})
    public ResponseEntity<String> routeResearch(HttpServletRequest request) throws IOException {
        try {
            return forward(request, researchEngineBaseUrl, true);
        } catch (ResourceAccessException exception) {
            if (!isTimeoutFailure(exception)) throw exception;
            logger.warn("RESEARCH_ENGINE_TIMEOUT path={} sanitizedMessage={}", request.getRequestURI(),
                    sanitizeDiagnosticMessage(exception.getMessage()));
            return ResponseEntity.status(HttpStatus.GATEWAY_TIMEOUT)
                    .contentType(MediaType.APPLICATION_JSON)
                    .body("{\"error\":\"Gateway Timeout\",\"code\":\"RESEARCH_ENGINE_TIMEOUT\"}");
        }
    }

    @RequestMapping({"/", "/verify-email", "/reset-password", "/_next/**", "/favicon.ico", "/robots.txt"})
    public ResponseEntity<byte[]> routeFrontend(HttpServletRequest request) throws IOException {
        return forwardPublicBytes(request, frontendBaseUrl);
    }

    private ResponseEntity<byte[]> forwardPublicBytes(HttpServletRequest request, String baseUrl) throws IOException {
        String query = request.getQueryString();
        String target = baseUrl + request.getRequestURI() + (query == null ? "" : "?" + query);
        byte[] body = StreamUtils.copyToByteArray(request.getInputStream());
        HttpHeaders headers = new HttpHeaders();
        Collections.list(request.getHeaderNames()).stream()
                .filter(name -> !HOP_BY_HOP_HEADERS.contains(name.toLowerCase()))
                .filter(name -> !GatewayAuthenticationFilter.isInternalIdentityHeader(name))
                .forEach(name -> headers.add(name, request.getHeader(name)));
        addCorrelationHeaders(headers);
        ResponseEntity<byte[]> response = restClient.method(HttpMethod.valueOf(request.getMethod()))
                .uri(target)
                .headers(outbound -> outbound.addAll(headers))
                .body(body)
                .exchange((clientRequest, clientResponse) -> ResponseEntity
                        .status(clientResponse.getStatusCode())
                        .headers(clientResponse.getHeaders())
                        .body(StreamUtils.copyToByteArray(clientResponse.getBody())));
        HttpHeaders responseHeaders = new HttpHeaders();
        response.getHeaders().forEach((name, values) -> {
            if (!HOP_BY_HOP_HEADERS.contains(name.toLowerCase())) responseHeaders.put(name, values);
        });
        return new ResponseEntity<>(response.getBody(), responseHeaders, response.getStatusCode());
    }

    private ResponseEntity<String> forward(HttpServletRequest request, String baseUrl, boolean includeIdentity) throws IOException {
        String path = request.getRequestURI();
        String query = request.getQueryString();
        String target = baseUrl + path + (query == null ? "" : "?" + query);
        String body = StreamUtils.copyToString(request.getInputStream(), StandardCharsets.UTF_8);
        HttpHeaders headers = new HttpHeaders();
        Collections.list(request.getHeaderNames()).stream()
                .filter(name -> !HOP_BY_HOP_HEADERS.contains(name.toLowerCase()))
                .filter(name -> !GatewayAuthenticationFilter.isInternalIdentityHeader(name))
                .forEach(name -> headers.add(name, request.getHeader(name)));
        addCorrelationHeaders(headers);
        if (includeIdentity) {
            addTrustedIdentity(headers, request);
        }
        ResponseEntity<String> response = restClient.method(HttpMethod.valueOf(request.getMethod()))
                .uri(java.net.URI.create(target))
                .headers(outbound -> outbound.addAll(headers))
                .body(body)
                .exchange((clientRequest, clientResponse) -> {
                    String responseBody = StreamUtils.copyToString(clientResponse.getBody(), StandardCharsets.UTF_8);
                    return ResponseEntity.status(clientResponse.getStatusCode())
                            .headers(clientResponse.getHeaders())
                            .body(responseBody);
                });
        HttpHeaders responseHeaders = new HttpHeaders();
        response.getHeaders().forEach((name, values) -> {
            if (!HOP_BY_HOP_HEADERS.contains(name.toLowerCase())) {
                responseHeaders.put(name, values);
            }
        });
        return new ResponseEntity<>(response.getBody(), responseHeaders, response.getStatusCode());
    }

    private ResponseEntity<byte[]> forwardBytes(HttpServletRequest request, String baseUrl, boolean includeIdentity) throws IOException {
        return forwardBytes(request, baseUrl, includeIdentity, connectorSessionRestClient);
    }

    private ResponseEntity<byte[]> forwardBytes(HttpServletRequest request, String baseUrl, boolean includeIdentity,
                                                RestClient client) throws IOException {
        String path = request.getRequestURI();
        String query = request.getQueryString();
        String target = baseUrl + path + (query == null ? "" : "?" + query);
        byte[] body = StreamUtils.copyToByteArray(request.getInputStream());
        logGatewayIncoming(request, path);
        HttpHeaders headers = new HttpHeaders();
        Collections.list(request.getHeaderNames()).stream()
                .filter(name -> !HOP_BY_HOP_HEADERS.contains(name.toLowerCase()))
                .filter(name -> !GatewayAuthenticationFilter.isInternalIdentityHeader(name))
                .forEach(name -> headers.add(name, request.getHeader(name)));
        addCorrelationHeaders(headers);
        if (includeIdentity) {
            addTrustedIdentity(headers, request);
        }
        ResponseEntity<byte[]> response;
        try {
            response = client.method(HttpMethod.valueOf(request.getMethod()))
                    .uri(target)
                    .headers(outbound -> outbound.addAll(headers))
                    .body(body)
                    .exchange((clientRequest, clientResponse) -> {
                        byte[] responseBody = StreamUtils.copyToByteArray(clientResponse.getBody());
                        logConnectorResponseSeenByGateway(clientResponse.getStatusCode(), clientResponse.getHeaders(), responseBody.length);
                        return ResponseEntity.status(clientResponse.getStatusCode())
                                .headers(clientResponse.getHeaders())
                                .body(responseBody);
                    });
        } catch (RuntimeException exception) {
            logger.warn("RESTCLIENT_EXCEPTION exceptionClass={} sanitizedMessage={}",
                    exception.getClass().getName(), sanitizeDiagnosticMessage(exception.getMessage()));
            throw exception;
        }
        HttpHeaders responseHeaders = new HttpHeaders();
        response.getHeaders().forEach((name, values) -> {
            if (!HOP_BY_HOP_HEADERS.contains(name.toLowerCase())) {
                responseHeaders.put(name, values);
            }
        });
        logGatewayIntendedResponse(response.getStatusCode(), responseHeaders, response.getBody());
        return new ResponseEntity<>(response.getBody(), responseHeaders, response.getStatusCode());
    }

    private void logGatewayIncoming(HttpServletRequest request, String path) {
        String contentLength = request.getHeader(HttpHeaders.CONTENT_LENGTH);
        String transferEncoding = request.getHeader(HttpHeaders.TRANSFER_ENCODING);
        logger.info("GATEWAY_INCOMING method={} path={} contentType={} contentLengthPresent={} contentLength={} "
                        + "transferEncodingPresent={} transferEncoding={}",
                request.getMethod(),
                path,
                nullToEmpty(request.getContentType()),
                contentLength != null,
                nullToEmpty(contentLength),
                transferEncoding != null,
                nullToEmpty(transferEncoding));
    }

    private void logConnectorResponseSeenByGateway(HttpStatusCode status, HttpHeaders headers, int bodyByteLength) {
        String contentLength = headers.getFirst(HttpHeaders.CONTENT_LENGTH);
        String transferEncoding = headers.getFirst(HttpHeaders.TRANSFER_ENCODING);
        logger.info("CONNECTOR_RESPONSE_SEEN_BY_GATEWAY callbackReached={} status={} contentType={} "
                        + "contentLengthPresent={} contentLength={} transferEncodingPresent={} transferEncoding={} "
                        + "bodyByteLength={}",
                true,
                status.value(),
                nullToEmpty(headers.getFirst(HttpHeaders.CONTENT_TYPE)),
                contentLength != null,
                nullToEmpty(contentLength),
                transferEncoding != null,
                nullToEmpty(transferEncoding),
                bodyByteLength);
    }

    private void logGatewayIntendedResponse(HttpStatusCode status, HttpHeaders headers, byte[] body) {
        logger.info("GATEWAY_INTENDED_RESPONSE status={} contentType={} bodyByteLength={}",
                status.value(),
                nullToEmpty(headers.getFirst(HttpHeaders.CONTENT_TYPE)),
                body == null ? 0 : body.length);
    }

    private static String sanitizeDiagnosticMessage(String message) {
        if (message == null || message.isBlank()) {
            return "";
        }
        return message
                .replaceAll("(?i)(authorization\\s*[:=]\\s*bearer\\s+)[^\\s,;]+", "$1<redacted>")
                .replaceAll("(?i)((?:cookie|set-cookie|x-internal-token)\\s*[:=]\\s*)[^\\r\\n]+", "$1<redacted>")
                .replaceAll("(?i)([?&](?:loginToken|token|jwt|authorization|cookie|set-cookie|password|mfa|session|x-internal-token)=)[^\\s&]+", "$1<redacted>")
                .replaceAll("(https?://[^\\s?]+)\\?[^\\s]+", "$1?<redacted>");
    }

    static boolean isTimeoutFailure(Throwable failure) {
        Throwable current = failure;
        while (current != null) {
            if (current instanceof HttpTimeoutException
                    || current instanceof SocketTimeoutException
                    || current instanceof TimeoutException) return true;
            current = current.getCause();
        }
        return false;
    }

    private static String nullToEmpty(String value) {
        return value == null ? "" : value;
    }

    static RestClient connectorSessionRestClient() {
        return connectorSessionRestClient(Duration.ofSeconds(3), Duration.ofSeconds(30));
    }

    private static RestClient connectorSessionRestClient(Duration connectTimeout, Duration readTimeout) {
        return RestClient.builder()
                .requestFactory(connectorSessionRequestFactory(connectTimeout, readTimeout))
                .build();
    }

    static JdkClientHttpRequestFactory connectorSessionRequestFactory() {
        return connectorSessionRequestFactory(Duration.ofSeconds(3), Duration.ofSeconds(30));
    }

    private static JdkClientHttpRequestFactory connectorSessionRequestFactory(
            Duration connectTimeout, Duration readTimeout) {
        HttpClient httpClient = HttpClient.newBuilder()
                .version(HttpClient.Version.HTTP_1_1)
                .connectTimeout(connectTimeout)
                .build();
        JdkClientHttpRequestFactory requestFactory = new JdkClientHttpRequestFactory(httpClient);
        requestFactory.setReadTimeout(readTimeout);
        return requestFactory;
    }

    private void addTrustedIdentity(HttpHeaders headers, HttpServletRequest request) {
        headers.set(AuthenticationHeaders.USER_ID, requiredAttribute(request, GatewayAuthenticationFilter.ATTR_USER_ID));
        headers.set(AuthenticationHeaders.ISSUER, requiredAttribute(request, GatewayAuthenticationFilter.ATTR_ISSUER));
        headers.set(AuthenticationHeaders.SUBJECT, requiredAttribute(request, GatewayAuthenticationFilter.ATTR_SUBJECT));
        setIfPresent(headers, AuthenticationHeaders.EMAIL, request.getAttribute(GatewayAuthenticationFilter.ATTR_EMAIL));
        setIfPresent(headers, AuthenticationHeaders.DISPLAY_NAME, request.getAttribute(GatewayAuthenticationFilter.ATTR_DISPLAY_NAME));
        setIfPresent(headers, AuthenticationHeaders.ROLES, request.getAttribute(GatewayAuthenticationFilter.ATTR_ROLES));
    }

    private static void addCorrelationHeaders(HttpHeaders headers) {
        String correlationId = CorrelationIdFilter.currentId();
        if (correlationId != null && !correlationId.isBlank()) {
            headers.set(CorrelationIdFilter.HEADER_NAME, correlationId);
            headers.set(CorrelationIdFilter.REQUEST_ID_HEADER_NAME, correlationId);
        }
    }

    private String requiredAttribute(HttpServletRequest request, String name) {
        Object value = request.getAttribute(name);
        if (value == null || value.toString().isBlank()) {
            throw new IllegalStateException("Missing trusted gateway identity");
        }
        return value.toString();
    }

    private void setIfPresent(HttpHeaders headers, String name, Object value) {
        if (value != null && !value.toString().isBlank()) {
            headers.set(name, value.toString());
        }
    }
}
