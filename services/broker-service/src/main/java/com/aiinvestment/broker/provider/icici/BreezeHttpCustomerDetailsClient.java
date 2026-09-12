package com.aiinvestment.broker.provider.icici;

import com.aiinvestment.broker.application.BrokerProviderException;
import com.aiinvestment.broker.config.ICICIDirectProviderProperties;
import com.aiinvestment.shared.domain.broker.BrokerType;
import com.fasterxml.jackson.databind.JsonNode;
import org.springframework.http.HttpMethod;
import org.springframework.http.MediaType;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.stereotype.Component;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientResponseException;

import java.net.URI;
import java.time.Duration;
import java.util.Map;

@Component
class BreezeHttpCustomerDetailsClient implements BreezeCustomerDetailsClient {
    private static final String OFFICIAL_HOST = "api.icicidirect.com";
    private static final String CUSTOMER_DETAILS_PATH = "/breezeapi/api/v1/customerdetails";
    private final ICICIDirectProviderProperties properties;
    private final RestClient restClient;

    @Autowired
    BreezeHttpCustomerDetailsClient(ICICIDirectProviderProperties properties, RestClient.Builder restClientBuilder) {
        this.properties = properties;
        this.restClient = configured(restClientBuilder);
    }

    BreezeHttpCustomerDetailsClient(ICICIDirectProviderProperties properties, RestClient restClient) {
        this.properties = properties;
        this.restClient = restClient;
    }

    @Override
    public BreezeCustomerDetails exchangeApiSession(String appKey, String apiSession) {
        URI endpoint = officialEndpoint(properties.baseUrl(), CUSTOMER_DETAILS_PATH);
        try {
            JsonNode response = restClient.method(HttpMethod.GET).uri(endpoint)
                    .contentType(MediaType.APPLICATION_JSON)
                    .body(Map.of("SessionToken", apiSession, "AppKey", appKey))
                    .retrieve().body(JsonNode.class);
            JsonNode success = response == null ? null : response.path("Success");
            int providerStatus = response == null ? 0 : response.path("Status").asInt(0);
            if (providerStatus == 401 || providerStatus == 403) {
                throw BrokerProviderException.authenticationRequired(BrokerType.ICICI_DIRECT);
            }
            String token = text(success, "session_token");
            String providerUserId = text(success, "idirect_userid");
            String displayName = text(success, "idirect_user_name");
            if (token == null || providerUserId == null || displayName == null) {
                throw BrokerProviderException.unavailable(BrokerType.ICICI_DIRECT);
            }
            return new BreezeCustomerDetails(token, providerUserId, displayName);
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
        SimpleClientHttpRequestFactory requestFactory = new SimpleClientHttpRequestFactory();
        requestFactory.setConnectTimeout(Duration.ofSeconds(5));
        requestFactory.setReadTimeout(Duration.ofSeconds(10));
        return builder.clone().requestFactory(requestFactory).build();
    }

    private static URI officialEndpoint(String baseUrl, String path) {
        URI base = URI.create(baseUrl);
        if (!"https".equalsIgnoreCase(base.getScheme()) || !OFFICIAL_HOST.equalsIgnoreCase(base.getHost())) {
            throw BrokerProviderException.documentationRequired(BrokerType.ICICI_DIRECT);
        }
        return URI.create("https://" + OFFICIAL_HOST + path);
    }

    private static String text(JsonNode node, String field) {
        if (node == null || !node.isObject()) return null;
        String value = node.path(field).asText(null);
        return value == null || value.isBlank() ? null : value;
    }
}
