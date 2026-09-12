package com.aiinvestment.broker.provider.icici;

import com.aiinvestment.broker.application.BrokerProviderException;
import com.aiinvestment.broker.config.ICICIDirectProviderProperties;
import com.aiinvestment.shared.domain.broker.BrokerType;
import com.fasterxml.jackson.databind.JsonNode;
import org.springframework.http.HttpMethod;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.stereotype.Component;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientResponseException;

import java.math.BigDecimal;
import java.net.URI;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;

@Component
class BreezeHttpReadClient implements BreezeReadClient {
    static final String DEMAT_HOLDINGS_PATH = "/breezeapi/api/v1/dematholdings";
    static final String FUNDS_PATH = "/breezeapi/api/v1/funds";
    private static final String OFFICIAL_HOST = "api.icicidirect.com";
    private static final String EMPTY_BODY = "{}";
    private final ICICIDirectProviderProperties properties;
    private final RestClient restClient;

    @Autowired
    BreezeHttpReadClient(ICICIDirectProviderProperties properties, RestClient.Builder restClientBuilder) {
        this.properties = properties;
        this.restClient = configured(restClientBuilder);
    }

    BreezeHttpReadClient(ICICIDirectProviderProperties properties, RestClient restClient) {
        this.properties = properties;
        this.restClient = restClient;
    }

    @Override
    public List<BreezeDematHolding> fetchDematHoldings(BreezeSignedHeaders headers) {
        JsonNode success = execute(DEMAT_HOLDINGS_PATH, headers).path("Success");
        if (!success.isArray()) {
            throw BrokerProviderException.unavailable(BrokerType.ICICI_DIRECT);
        }
        List<BreezeDematHolding> holdings = new ArrayList<>();
        for (JsonNode item : success) {
            BigDecimal quantity = decimalOrNull(item, "quantity");
            holdings.add(new BreezeDematHolding(text(item, "stock_code"), text(item, "stock_ISIN"), quantity));
        }
        return List.copyOf(holdings);
    }

    @Override
    public BreezeFunds fetchFunds(BreezeSignedHeaders headers) {
        JsonNode success = execute(FUNDS_PATH, headers).path("Success");
        if (!success.isObject()) {
            throw BrokerProviderException.unavailable(BrokerType.ICICI_DIRECT);
        }
        return new BreezeFunds(decimal(success, "total_bank_balance"));
    }

    private JsonNode execute(String path, BreezeSignedHeaders headers) {
        URI endpoint = officialEndpoint(properties.baseUrl(), path);
        try {
            JsonNode response = restClient.method(HttpMethod.GET).uri(endpoint)
                    .headers(http -> headers.values().forEach(http::set))
                    .body(EMPTY_BODY).retrieve().body(JsonNode.class);
            int status = response == null ? 0 : response.path("Status").asInt(0);
            if (status == 401 || status == 403) {
                throw BrokerProviderException.authenticationRequired(BrokerType.ICICI_DIRECT);
            }
            if (response == null || response.path("Success").isMissingNode()
                    || response.path("Success").isNull()) {
                throw BrokerProviderException.unavailable(BrokerType.ICICI_DIRECT);
            }
            return response;
        } catch (BrokerProviderException exception) {
            throw exception;
        } catch (RestClientResponseException exception) {
            if (exception.getStatusCode().value() == 401 || exception.getStatusCode().value() == 403) {
                throw BrokerProviderException.authenticationRequired(BrokerType.ICICI_DIRECT);
            }
            throw BrokerProviderException.unavailable(BrokerType.ICICI_DIRECT);
        } catch (RuntimeException exception) {
            throw BrokerProviderException.unavailable(BrokerType.ICICI_DIRECT);
        }
    }

    private static RestClient configured(RestClient.Builder builder) {
        SimpleClientHttpRequestFactory factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(Duration.ofSeconds(5));
        factory.setReadTimeout(Duration.ofSeconds(10));
        return builder.clone().requestFactory(factory).build();
    }

    private static URI officialEndpoint(String baseUrl, String path) {
        URI base = URI.create(baseUrl);
        if (!"https".equalsIgnoreCase(base.getScheme()) || !OFFICIAL_HOST.equalsIgnoreCase(base.getHost())
                || (!DEMAT_HOLDINGS_PATH.equals(path) && !FUNDS_PATH.equals(path))) {
            throw BrokerProviderException.documentationRequired(BrokerType.ICICI_DIRECT);
        }
        return URI.create("https://" + OFFICIAL_HOST + path);
    }

    private static String text(JsonNode node, String field) {
        String value = node.path(field).asText(null);
        return value == null || value.isBlank() ? null : value;
    }

    private static BigDecimal decimal(JsonNode node, String field) {
        JsonNode value = node.path(field);
        if (value.isMissingNode() || value.isNull()) {
            throw BrokerProviderException.unavailable(BrokerType.ICICI_DIRECT);
        }
        try {
            return new BigDecimal(value.asText());
        } catch (NumberFormatException exception) {
            throw BrokerProviderException.unavailable(BrokerType.ICICI_DIRECT);
        }
    }

    private static BigDecimal decimalOrNull(JsonNode node, String field) {
        JsonNode value = node.path(field);
        if (value.isMissingNode() || value.isNull()) return null;
        try {
            return new BigDecimal(value.asText());
        } catch (NumberFormatException exception) {
            return null;
        }
    }
}
