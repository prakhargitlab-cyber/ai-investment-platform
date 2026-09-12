package com.aiinvestment.broker.provider.hdfc;

import com.aiinvestment.broker.application.BrokerProviderException;
import com.aiinvestment.broker.config.HDFCSecuritiesProviderProperties;
import com.aiinvestment.shared.domain.broker.BrokerType;
import com.fasterxml.jackson.databind.JsonNode;
import org.springframework.http.HttpMethod;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientResponseException;
import java.math.BigDecimal;
import java.net.URI;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;

@Component
class HDFCHttpReadClient implements HDFCReadClient {
    private static final String HOST = "developer.hdfcsec.com";
    private static final String ACCESS = "/oapi/v1/access-token";
    private static final String PROFILE = "/oapi/v3/user/profile";
    private static final String HOLDINGS = "/oapi/v1/portfolio/holdings";
    private static final String FUNDS = "/oapi/v1/user/margins";
    private final RestClient client;

    HDFCHttpReadClient(RestClient.Builder builder) {
        var factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(Duration.ofSeconds(5)); factory.setReadTimeout(Duration.ofSeconds(10));
        client = builder.clone().requestFactory(factory).build();
    }

    public String exchangeAccessToken(String key, String secret, String requestToken) {
        JsonNode response = execute(HttpMethod.POST, uri(ACCESS, key, requestToken), null,
                java.util.Map.of("apiSecret", secret));
        String token = text(response, "accessToken");
        if (token == null) token = text(response.path("data"), "accessToken");
        if (token == null) throw BrokerProviderException.authenticationRequired(BrokerType.HDFC_SECURITIES);
        return token;
    }
    public Profile profile(String key, String token) {
        JsonNode data = execute(HttpMethod.POST, uri(PROFILE, key, null), token, null).path("data");
        JsonNode item = data.isArray() && !data.isEmpty() ? data.get(0) : data;
        String id = text(item, "user_id"), name = text(item, "user_name");
        if (id == null) throw BrokerProviderException.unavailable(BrokerType.HDFC_SECURITIES);
        return new Profile(id, name == null ? "HDFC Securities" : name);
    }
    public List<Holding> holdings(String key, String token) {
        JsonNode data = execute(HttpMethod.GET, uri(HOLDINGS, key, null), token, null).path("data");
        if (!data.isArray()) throw BrokerProviderException.unavailable(BrokerType.HDFC_SECURITIES);
        List<Holding> result = new ArrayList<>();
        for (JsonNode item : data) result.add(new Holding(text(item,"security_id"), text(item,"exchange"),
                text(item,"company_name"), text(item,"isin"), decimal(item,"quantity"),
                decimal(item,"average_price"), decimal(item,"close_price")));
        return List.copyOf(result);
    }
    public Funds funds(String key, String token) {
        JsonNode equity = execute(HttpMethod.GET, uri(FUNDS, key, null), token, null).path("data").path("equity");
        // The official contract distinguishes cash from overall buying/margin limits.
        BigDecimal cash = decimal(equity.path("totalAvailableLimitDetails"), "cash");
        if (cash == null) throw BrokerProviderException.unavailable(BrokerType.HDFC_SECURITIES);
        return new Funds(cash);
    }
    private JsonNode execute(HttpMethod method, URI uri, String token, Object body) {
        try {
            var request = client.method(method).uri(uri).header("User-Agent", "AI-Investment-Platform/1.0");
            if (token != null) request.header("Authorization", token);
            var spec = body == null ? request : request.contentType(org.springframework.http.MediaType.APPLICATION_JSON).body(body);
            JsonNode response = spec.retrieve().body(JsonNode.class);
            if (response == null || "error".equalsIgnoreCase(response.path("status").asText()))
                throw BrokerProviderException.unavailable(BrokerType.HDFC_SECURITIES);
            return response;
        } catch (RestClientResponseException e) {
            if (e.getStatusCode().value() == 401 || e.getStatusCode().value() == 403)
                throw BrokerProviderException.authenticationRequired(BrokerType.HDFC_SECURITIES);
            throw BrokerProviderException.unavailable(BrokerType.HDFC_SECURITIES);
        } catch (BrokerProviderException e) { throw e; }
        catch (RuntimeException e) { throw BrokerProviderException.unavailable(BrokerType.HDFC_SECURITIES); }
    }
    private static URI uri(String path, String key, String requestToken) {
        String value = "https://" + HOST + path + "?api_key=" + enc(key);
        if (requestToken != null) value += "&request_token=" + enc(requestToken);
        URI uri = URI.create(value);
        if (!HOST.equalsIgnoreCase(uri.getHost())) throw BrokerProviderException.documentationRequired(BrokerType.HDFC_SECURITIES);
        return uri;
    }
    private static String enc(String value) { return java.net.URLEncoder.encode(value, java.nio.charset.StandardCharsets.UTF_8); }
    private static String text(JsonNode n, String f) { String v=n.path(f).asText(null); return v==null||v.isBlank()?null:v; }
    private static BigDecimal decimal(JsonNode n, String f) { try { JsonNode v=n.path(f); return v.isMissingNode()||v.isNull()||v.asText().isBlank()?null:new BigDecimal(v.asText()); } catch(NumberFormatException e){ return null; } }
}
