package com.aiinvestment.multiparttest;

import com.aiinvestment.apigateway.ApiGatewayApplication;
import com.aiinvestment.shared.web.auth.HmacJwtService;
import com.aiinvestment.shared.web.auth.JwtClaims;
import jakarta.servlet.http.HttpServletRequest;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.springframework.boot.WebApplicationType;
import org.springframework.boot.autoconfigure.EnableAutoConfiguration;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.boot.builder.SpringApplicationBuilder;
import org.springframework.boot.web.context.WebServerApplicationContext;
import org.springframework.context.ConfigurableApplicationContext;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Import;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestPart;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.multipart.MultipartFile;

import java.nio.charset.StandardCharsets;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Instant;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

class MultipartProxyHttpIntegrationTest {
    private static final String ISSUER = "multipart-test";
    private static final String SECRET = "multipart-test-jwt-secret-32-characters";
    private static final String FILENAME = "8510744020_PortFolioEqtSummary (1).csv";
    private static final byte[] CSV = ("Stock Symbol,Company Name,ISIN Code,Qty,Average Cost Price,Current Market Price,"
            + "% Change over prev close,Value At Cost,Value At Market Price,Realized Profit / Loss,"
            + "Unrealized Profit/Loss,Unrealized Profit/Loss %\n"
            + "ABC,ABC Limited,INE000A01001,10,200,250,+1,2000,2500,0,500,25\n")
            .getBytes(StandardCharsets.UTF_8);

    private static ConfigurableApplicationContext downstream;
    private static ConfigurableApplicationContext gateway;
    private static String downstreamBaseUrl;
    private static String gatewayBaseUrl;

    @BeforeAll
    static void startRealHttpServers() {
        downstream = new SpringApplicationBuilder(DownstreamConfiguration.class)
                .web(WebApplicationType.SERVLET).run("--server.port=0", "--spring.servlet.multipart.enabled=true",
                        "--test.multipart-downstream=true");
        downstreamBaseUrl = baseUrl(downstream);
        gateway = new SpringApplicationBuilder(ApiGatewayApplication.class)
                .run("--server.port=0", "--portfolio.service.base-url=" + downstreamBaseUrl,
                        "--auth.issuer=" + ISSUER, "--auth.jwt-secret=" + SECRET,
                        "--spring.servlet.multipart.enabled=false");
        gatewayBaseUrl = baseUrl(gateway);
    }

    @AfterAll
    static void stopRealHttpServers() {
        if (gateway != null) gateway.close();
        if (downstream != null) downstream.close();
    }

    @Test
    void directPortfolioStyleControllerReceivesRealMultipartFile() throws Exception {
        String response = sendMultipart(downstreamBaseUrl, false);
        assertMultipartBound(response);
    }

    @Test
    void gatewayPreservesRealMultipartUntilDownstreamRequestPartBinding() throws Exception {
        String response = sendMultipart(gatewayBaseUrl, true);
        assertMultipartBound(response);
    }

    private static String sendMultipart(String baseUrl, boolean authenticated) throws Exception {
        String boundary = "----AipRealMultipartBoundary7MA4YWxk";
        byte[] prefix = ("--" + boundary + "\r\nContent-Disposition: form-data; name=\"file\"; filename=\""
                + FILENAME + "\"\r\nContent-Type: text/csv\r\n\r\n").getBytes(StandardCharsets.UTF_8);
        byte[] suffix = ("\r\n--" + boundary + "--\r\n").getBytes(StandardCharsets.UTF_8);
        byte[] body = new byte[prefix.length + CSV.length + suffix.length];
        System.arraycopy(prefix, 0, body, 0, prefix.length);
        System.arraycopy(CSV, 0, body, prefix.length, CSV.length);
        System.arraycopy(suffix, 0, body, prefix.length + CSV.length, suffix.length);
        HttpRequest.Builder request = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl + "/api/v1/portfolios/imports/icici_direct/preview"))
                .header(HttpHeaders.CONTENT_TYPE, "multipart/form-data; boundary=" + boundary)
                .POST(HttpRequest.BodyPublishers.ofByteArray(body));
        if (authenticated) request.header(HttpHeaders.AUTHORIZATION, "Bearer " + token());
        HttpResponse<String> response = HttpClient.newHttpClient().send(
                request.build(), HttpResponse.BodyHandlers.ofString());
        assertThat(response.statusCode()).isEqualTo(200);
        return response.body();
    }

    private static void assertMultipartBound(String response) {
        assertThat(response).contains("\"multipart\":true");
        assertThat(response).contains("\"partNames\":[\"file\"]");
        assertThat(response).contains("\"filename\":\"" + FILENAME + "\"");
        assertThat(response).contains("\"size\":" + CSV.length);
        assertThat(response).contains("multipart/form-data; boundary=");
    }

    private static String token() {
        return new HmacJwtService(SECRET).issue(new JwtClaims(
                ISSUER, "user-a", null, null, List.of("USER"), Instant.now().plusSeconds(300)));
    }

    private static String baseUrl(ConfigurableApplicationContext context) {
        int port = ((WebServerApplicationContext) context).getWebServer().getPort();
        return "http://127.0.0.1:" + port;
    }

    @Configuration(proxyBeanMethods = false)
    @EnableAutoConfiguration
    @Import(DownstreamController.class)
    static class DownstreamConfiguration {}

    @RestController
    @ConditionalOnProperty(name = "test.multipart-downstream", havingValue = "true")
    static class DownstreamController {
        @PostMapping(value = "/api/v1/portfolios/imports/icici_direct/preview",
                consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
        Map<String, Object> preview(@RequestPart("file") MultipartFile file, HttpServletRequest request)
                throws Exception {
            return Map.of(
                    "multipart", request.getContentType().startsWith("multipart/form-data"),
                    "contentType", request.getContentType(),
                    "partNames", request.getParts().stream().map(part -> part.getName()).toList(),
                    "filename", file.getOriginalFilename(),
                    "size", file.getSize()
            );
        }
    }
}
