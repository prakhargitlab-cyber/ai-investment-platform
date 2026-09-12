
package com.aiinvestment.broker.connector.ibkr;

import com.aiinvestment.broker.application.BrokerProviderException;
import com.aiinvestment.broker.config.IBKRProviderProperties;
import com.aiinvestment.broker.connector.*;
import com.aiinvestment.broker.provider.ibkr.IBKRInstrumentNormalizer;
import com.aiinvestment.shared.domain.Money;
import com.aiinvestment.shared.domain.broker.*;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatusCode;
import org.springframework.http.MediaType;
import org.springframework.http.client.JdkClientHttpRequestFactory;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientResponseException;
import org.springframework.web.client.ResourceAccessException;

import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;
import java.math.BigDecimal;
import java.net.URLEncoder;
import java.net.URI;
import java.net.http.HttpClient;
import com.aiinvestment.shared.web.CorrelationIdFilter;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.security.cert.X509Certificate;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;

@Component
public class IBKRIndividualConnector implements BrokerConnector {
    private static final Logger logger = LoggerFactory.getLogger(IBKRIndividualConnector.class);
    private static final ObjectMapper OBJECT_MAPPER = new ObjectMapper();
    private static final int PAGE_SIZE = 100;
    private final IBKRProviderProperties properties;
    private final IBKRInstrumentNormalizer instrumentNormalizer;
    private final RestClient restClient;
    private final ConnectorRuntimeLocationResolver runtimeLocations;

    @Autowired
    public IBKRIndividualConnector(IBKRProviderProperties properties, IBKRInstrumentNormalizer instrumentNormalizer,
                                   ConnectorRuntimeLocationResolver runtimeLocations) {
        this(properties, instrumentNormalizer, restClient(properties), runtimeLocations);
    }

    public IBKRIndividualConnector(IBKRProviderProperties properties, IBKRInstrumentNormalizer instrumentNormalizer, RestClient restClient) {
        this(properties, instrumentNormalizer, restClient, null);
    }

    private IBKRIndividualConnector(IBKRProviderProperties properties, IBKRInstrumentNormalizer instrumentNormalizer,
                                    RestClient restClient, ConnectorRuntimeLocationResolver runtimeLocations) {
        this.properties = properties;
        this.instrumentNormalizer = instrumentNormalizer;
        this.restClient = restClient;
        this.runtimeLocations = runtimeLocations;
    }

    @Override
    public BrokerType brokerType() {
        return BrokerType.IBKR;
    }

    @Override
    public ConnectorRuntimeMode runtimeMode() {
        try {
            return ConnectorRuntimeMode.valueOf(properties.connectorRuntimeMode().toUpperCase());
        } catch (RuntimeException exception) {
            return ConnectorRuntimeMode.LOCAL_AGENT;
        }
    }

    @Override
    public BrokerConnectorStatus start(UUID userId, UUID connectorId) {
        if (!configured()) {
            return new BrokerConnectorStatus(BrokerConnectorState.NOT_CONFIGURED, BrokerConnectorState.NOT_CONFIGURED,
                    null, null, "NOT_CONFIGURED", "IBKR connector is not configured.");
        }
        JsonNode status = post("/internal/connectors", userId, connectorId,
                Map.of("connectorId", connectorId.toString(), "userId", userId.toString()));
        return connectorStatus(status);
    }

    @Override
    public BrokerConnectorStatus status(UUID userId, UUID connectorId) {
        if (!configured()) {
            return new BrokerConnectorStatus(BrokerConnectorState.NOT_CONFIGURED, BrokerConnectorState.NOT_CONFIGURED,
                    null, null, "NOT_CONFIGURED", "IBKR connector is not configured.");
        }
        try {
            return connectorStatus(get("/internal/connectors/" + connectorId + "/status", userId, connectorId));
        } catch (BrokerProviderException exception) {
            return new BrokerConnectorStatus(BrokerConnectorState.ERROR, BrokerConnectorState.SESSION_EXPIRED,
                    null, null, exception.code(), exception.getMessage());
        }
    }

    @Override
    public String loginUrl(UUID userId, UUID connectorId) {
        if (!configured()) {
            return null;
        }
        JsonNode response = get("/internal/connectors/" + connectorId + "/login", userId, connectorId);
        return text(response, "loginUrl", null);
    }

    @Override
    public List<BrokerAccount> fetchAccounts(UUID userId, UUID connectorId) {
        requireAuthenticated(userId, connectorId);
        JsonNode accounts = data(get("/internal/connectors/" + connectorId + "/accounts", userId, connectorId));
        if (!accounts.isArray()) {
            throw BrokerProviderException.malformedResponse();
        }
        List<BrokerAccount> result = new ArrayList<>();
        for (JsonNode account : accounts) {
            String accountId = text(account, "id", text(account, "accountId", text(account, "account_id", null)));
            if (isBlank(accountId)) {
                throw BrokerProviderException.malformedResponse();
            }
            String currency = upperCurrency(text(account, "currency", "USD"));
            String displayName = text(account, "accountAlias", text(account, "accountTitle", "IBKR " + mask(accountId)));
            result.add(new BrokerAccount("IBKR_" + accountId, userId, BrokerType.IBKR, mask(accountId), displayName,
                    currency, BrokerAccountStatus.ACTIVE));
        }
        if (result.isEmpty()) {
            throw BrokerProviderException.accountNotFound();
        }
        return result;
    }

    @Override
    public List<BrokerPosition> fetchPositions(UUID userId, UUID connectorId, BrokerAccount account) {
        requireAuthenticated(userId, connectorId);
        requireIbkrAccount(account);
        String accountId = rawAccountId(account);
        List<BrokerPosition> result = new ArrayList<>();
        for (int page = 0; page < 100; page++) {
            JsonNode positions = data(get("/internal/connectors/" + connectorId + "/positions?account_id="
                    + encode(accountId) + "&page=" + page, userId, connectorId));
            if (!positions.isArray()) {
                throw BrokerProviderException.malformedResponse();
            }
            if (positions.isEmpty()) {
                break;
            }
            for (JsonNode position : positions) {
                result.add(position(userId, connectorId, account, position));
            }
            if (positions.size() < PAGE_SIZE) {
                break;
            }
        }
        return result;
    }

    @Override
    public List<BrokerCashBalance> fetchCashBalances(UUID userId, UUID connectorId, BrokerAccount account) {
        requireAuthenticated(userId, connectorId);
        requireIbkrAccount(account);
        JsonNode ledger = data(get("/internal/connectors/" + connectorId + "/ledger?account_id="
                + encode(rawAccountId(account)), userId, connectorId));
        if (!ledger.isObject()) {
            throw BrokerProviderException.malformedResponse();
        }
        List<BrokerCashBalance> balances = new ArrayList<>();
        ledger.fields().forEachRemaining(entry -> {
            JsonNode node = entry.getValue();
            BigDecimal cash = decimalAny(node, "cashbalance", "cashBalance", "cash");
            String currency = upperCurrency(text(node, "currency", entry.getKey()));
            if (cash != null && currency.matches("[A-Z]{3}")) {
                balances.add(new BrokerCashBalance(
                        account.brokerAccountId(),
                        new Money(cash, currency),
                        money(node, currency, "settledcash", "settledCash"),
                        money(node, currency, "netliquidationvalue", "netLiquidationValue", "netLiquidation"),
                        money(node, currency, "stockmarketvalue", "stockMarketValue"),
                        money(node, currency, "unrealizedpnl", "unrealizedPnl", "unrealizedPNL"),
                        money(node, currency, "realizedpnl", "realizedPnl", "realizedPNL"),
                        "REAL_BROKER"));
            }
        });
        return balances;
    }

    private BrokerPosition position(UUID userId, UUID connectorId, BrokerAccount account, JsonNode node) {
        String contractId = text(node, "conid", text(node, "contractId", null));
        JsonNode metadata = isBlank(contractId) ? OBJECT_MAPPER.createObjectNode() : instrumentMetadata(userId, connectorId, contractId);
        String brokerSymbol = text(node, "ticker", text(node, "symbol", text(node, "contractDesc", null)));
        String brokerDescription = text(node, "contractDesc", text(node, "description", null));
        String brokerExchange = text(node, "listingExchange", text(node, "exchange", null));
        String ticker = textAny(metadata, textAny(node, null, "ticker", "symbol", "contractDesc"),
                "symbol", "ticker", "localSymbol");
        String exchange = textAny(metadata, textAny(node, "UNKNOWN", "listingExchange", "exchange"),
                "listingExchange", "primaryExchange", "exchange", "routingExchange");
        if ("UNKNOWN".equals(exchange)) {
            exchange = firstExchange(text(metadata, "validExchanges", null), exchange);
        }
        String currency = upperCurrency(text(metadata, "currency", text(node, "currency", account.baseCurrency())));
        String isin = text(metadata, "isin", text(node, "isin", null));
        String name = textAny(metadata, textAny(node, brokerSymbol, "name", "companyName", "description", "contractDesc", "fullName"),
                "companyName", "description", "longName", "name");
        String securityType = textAny(metadata, textAny(node, null, "secType", "type", "assetClass"),
                "secType", "securityType", "assetClass", "contractType");
        BigDecimal quantity = decimal(node, "position", decimal(node, "quantity", null));
        BigDecimal averageCost = decimal(node, "avgCost", decimal(node, "averageCost", BigDecimal.ZERO));
        BigDecimal marketPrice = decimal(node, "mktPrice", decimal(node, "marketPrice", averageCost));
        BigDecimal marketValue = decimal(node, "mktValue", decimal(node, "marketValue", null));
        BigDecimal unrealizedProfitLoss = decimal(node, "unrealizedPnl",
                decimal(node, "unrealizedProfitLoss", decimal(node, "unrealizedPNL", null)));
        if (isBlank(contractId) || isBlank(ticker) || quantity == null) {
            throw BrokerProviderException.malformedResponse();
        }
        var instrument = instrumentNormalizer.normalize(new BrokerInstrumentIdentity(BrokerType.IBKR, null, contractId,
                isin, ticker, exchange, text(metadata, "mic", text(node, "mic", null)), name,
                currency, text(metadata, "country", text(node, "country", null)), assetType(securityType, metadata),
                brokerSymbol, brokerDescription, brokerExchange, securityType));
        return new BrokerPosition(account.brokerAccountId(), instrument, quantity,
                new Money(averageCost == null ? BigDecimal.ZERO : averageCost, currency),
                new Money(marketPrice == null ? BigDecimal.ZERO : marketPrice, currency),
                marketValue == null ? null : new Money(marketValue, currency),
                unrealizedProfitLoss == null ? null : new Money(unrealizedProfitLoss, currency),
                Instant.now());
    }

    private JsonNode instrumentMetadata(UUID userId, UUID connectorId, String contractId) {
        try {
            return data(get("/internal/connectors/" + connectorId + "/instruments/" + encode(contractId), userId, connectorId));
        } catch (BrokerProviderException exception) {
            logger.warn("ibkr_contract_metadata_unavailable conid={} code={}", contractId, exception.code());
            return OBJECT_MAPPER.createObjectNode();
        }
    }

    private void requireAuthenticated(UUID userId, UUID connectorId) {
        if (!status(userId, connectorId).authenticated()) {
            throw BrokerProviderException.sessionExpired();
        }
    }

    private boolean configured() {
        return properties.enabled()
                && properties.officialDocumentationVerified()
                && "client-portal-gateway".equalsIgnoreCase(properties.authMethod())
                && !isBlank(properties.connectorInternalToken());
    }

    private JsonNode get(String path, UUID userId, UUID connectorId) {
        try {
            return restClient.get().uri(endpointUri(userId, connectorId, path))
                    .header("X-Internal-Token", properties.connectorInternalToken())
                    .header("X-AIP-User-Id", userId.toString())
                    .header(CorrelationIdFilter.HEADER_NAME, safeCorrelationId())
                    .retrieve().body(JsonNode.class);
        } catch (RestClientResponseException exception) {
            logger.warn("ibkr_connector_get_failed path={} status={}", path, exception.getStatusCode().value());
            throw mapStatus(exception.getStatusCode());
        } catch (RuntimeException exception) {
            logger.warn("ibkr_connector_get_failed path={} exception={}", path, exception.getClass().getSimpleName());
            throw BrokerProviderException.unavailable();
        }
    }

    private JsonNode post(String path, UUID userId, UUID connectorId, Map<String, String> body) {
        try {
            return restClient.post().uri(endpointUri(userId, connectorId, path))
                    .header("X-Internal-Token", properties.connectorInternalToken())
                    .header("X-AIP-User-Id", userId.toString())
                    .header(CorrelationIdFilter.HEADER_NAME, safeCorrelationId())
                    .contentType(MediaType.APPLICATION_JSON)
                    .accept(MediaType.APPLICATION_JSON)
                    .body(body)
                    .retrieve().body(JsonNode.class);
        } catch (RestClientResponseException exception) {
            logger.warn("ibkr_connector_post_failed path={} status={}", path, exception.getStatusCode().value());
            logValidationDetails(path, exception);
            throw mapPostStatus(exception);
        } catch (ResourceAccessException exception) {
            logger.warn("ibkr_connector_post_failed path={} exception={}", path, exception.getClass().getSimpleName());
            throw new BrokerProviderException("IBKR_CONNECTOR_STARTING", org.springframework.http.HttpStatus.BAD_GATEWAY,
                    "Interactive Brokers connector is still starting.");
        } catch (RuntimeException exception) {
            logger.warn("ibkr_connector_post_failed path={} exception={}", path, exception.getClass().getSimpleName());
            throw BrokerProviderException.unavailable();
        }
    }

    private static String safeCorrelationId() {
        String correlationId = CorrelationIdFilter.currentId();
        return correlationId == null || correlationId.isBlank() ? "internal" : correlationId;
    }

    private static void logValidationDetails(String path, RestClientResponseException exception) {
        if (exception.getStatusCode().value() != 422) {
            return;
        }
        try {
            JsonNode details = OBJECT_MAPPER.readTree(exception.getResponseBodyAsString()).path("detail");
            if (!details.isArray()) {
                return;
            }
            for (JsonNode detail : details) {
                logger.warn("ibkr_connector_validation_failed path={} loc={} type={} msg={}",
                        path, detail.path("loc"), text(detail, "type", ""), text(detail, "msg", ""));
            }
        } catch (Exception ignored) {
            logger.warn("ibkr_connector_validation_failed path={} detail_unparseable=true", path);
        }
    }

    private static BrokerProviderException mapStatus(HttpStatusCode status) {
        if (status.value() == 401) {
            return BrokerProviderException.sessionExpired();
        }
        if (status.value() == 403) {
            return BrokerProviderException.permissionDenied();
        }
        if (status.value() == 404) {
            return BrokerProviderException.accountNotFound();
        }
        if (status.value() == 429) {
            return BrokerProviderException.rateLimited();
        }
        return BrokerProviderException.unavailable();
    }

    private static BrokerProviderException mapPostStatus(RestClientResponseException exception) {
        if (exception.getStatusCode().value() != 409) {
            return mapStatus(exception.getStatusCode());
        }
        try {
            String code = text(OBJECT_MAPPER.readTree(exception.getResponseBodyAsString()), "code", "");
            if ("DEV_SINGLE_CONNECTOR_LIMIT".equals(code)) {
                return BrokerProviderException.connectorCapacityUnavailable();
            }
        } catch (Exception ignored) {
            // Treat malformed conflict responses as a generic conflict without exposing upstream details.
        }
        return BrokerProviderException.connectorConflict();
    }

    private static RestClient restClient(IBKRProviderProperties properties) {
        RestClient.Builder builder = RestClient.builder().baseUrl(trimTrailingSlash(properties.connectorBaseUrl()));
        if (properties.insecureTls()) {
            builder.requestFactory(new JdkClientHttpRequestFactory(insecureHttpClient()));
        } else {
            builder.requestFactory(new SimpleClientHttpRequestFactory());
        }
        return builder.build();
    }

    private URI endpointUri(UUID userId, UUID connectorId, String path) {
        if (runtimeLocations == null) {
            return URI.create(trimTrailingSlash(properties.connectorBaseUrl()) + path);
        }
        String base = trimTrailingSlash(runtimeLocations.resolve(userId, connectorId).runtimeEndpoint().toString());
        return URI.create(base + path);
    }

    private static HttpClient insecureHttpClient() {
        try {
            TrustManager[] trustAll = new TrustManager[]{new X509TrustManager() {
                @Override public void checkClientTrusted(X509Certificate[] chain, String authType) { }
                @Override public void checkServerTrusted(X509Certificate[] chain, String authType) { }
                @Override public X509Certificate[] getAcceptedIssuers() { return new X509Certificate[0]; }
            }};
            SSLContext context = SSLContext.getInstance("TLS");
            context.init(null, trustAll, new SecureRandom());
            return HttpClient.newBuilder().sslContext(context).build();
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to configure IBKR connector TLS client", exception);
        }
    }

    private static void requireIbkrAccount(BrokerAccount account) {
        if (account == null || account.brokerType() != BrokerType.IBKR) {
            throw BrokerProviderException.accountNotFound();
        }
    }

    private static String rawAccountId(BrokerAccount account) {
        return account.brokerAccountId().startsWith("IBKR_")
                ? account.brokerAccountId().substring("IBKR_".length())
                : account.brokerAccountId();
    }

    private static String encode(String value) {
        return URLEncoder.encode(value, StandardCharsets.UTF_8);
    }

    private static String text(JsonNode node, String field, String fallback) {
        JsonNode value = node.path(field);
        return value.isMissingNode() || value.isNull() || value.asText().isBlank() ? fallback : value.asText();
    }

    private static String textAny(JsonNode node, String fallback, String... fields) {
        for (String field : fields) {
            String value = text(node, field, null);
            if (!isBlank(value)) {
                return value;
            }
        }
        return fallback;
    }

    private static String firstExchange(String values, String fallback) {
        if (isBlank(values)) {
            return fallback;
        }
        for (String value : values.split(",")) {
            if (!isBlank(value)) {
                return value.trim();
            }
        }
        return fallback;
    }

    private static BigDecimal decimal(JsonNode node, String field, BigDecimal fallback) {
        JsonNode value = node.path(field);
        if (value.isMissingNode() || value.isNull() || value.asText().isBlank()) {
            return fallback;
        }
        try {
            return new BigDecimal(value.asText());
        } catch (NumberFormatException exception) {
            throw BrokerProviderException.malformedResponse();
        }
    }

    private static BigDecimal decimalAny(JsonNode node, String... fields) {
        for (String field : fields) {
            BigDecimal value = decimal(node, field, null);
            if (value != null) {
                return value;
            }
        }
        return null;
    }

    private static Money money(JsonNode node, String currency, String... fields) {
        BigDecimal amount = decimalAny(node, fields);
        return amount == null ? null : new Money(amount, currency);
    }

    private static String upperCurrency(String value) {
        return isBlank(value) ? "USD" : value.toUpperCase();
    }

    private static com.aiinvestment.shared.domain.AssetType assetType(String value, JsonNode metadata) {
        String normalized = value == null ? "" : value.toUpperCase();
        String searchable = (normalized + " "
                + text(metadata, "description", "") + " "
                + text(metadata, "longName", "") + " "
                + text(metadata, "name", "") + " "
                + text(metadata, "category", "") + " "
                + text(metadata, "subCategory", "")).toUpperCase();
        if (searchable.contains("EXCHANGE TRADED FUND") || searchable.contains(" ETF")
                || searchable.contains("UCITS ETF") || "ETF".equals(normalized)) {
            return com.aiinvestment.shared.domain.AssetType.ETF;
        }
        if (searchable.contains("MUTUAL FUND") || searchable.contains(" FUND")
                || "FUND".equals(normalized) || "MUTUALFUND".equals(normalized) || "MF".equals(normalized)) {
            return com.aiinvestment.shared.domain.AssetType.FUND;
        }
        if (isBlank(value)) {
            return com.aiinvestment.shared.domain.AssetType.EQUITY;
        }
        return switch (normalized) {
            case "STK", "EQUITY", "STOCK", "COMMON" -> com.aiinvestment.shared.domain.AssetType.EQUITY;
            case "BOND" -> com.aiinvestment.shared.domain.AssetType.BOND;
            case "CRYPTO" -> com.aiinvestment.shared.domain.AssetType.CRYPTO;
            default -> com.aiinvestment.shared.domain.AssetType.OTHER;
        };
    }

    private static String mask(String accountId) {
        if (accountId == null || accountId.length() < 4) {
            return "****";
        }
        return "****" + accountId.substring(accountId.length() - 4);
    }

    private static String trimTrailingSlash(String value) {
        if (value == null) {
            return "";
        }
        return value.endsWith("/") ? value.substring(0, value.length() - 1) : value;
    }

    private static JsonNode data(JsonNode response) {
        JsonNode data = response == null ? null : response.path("data");
        if (data == null || data.isMissingNode() || data.isNull()) {
            throw BrokerProviderException.malformedResponse();
        }
        return data;
    }

    private static BrokerConnectorStatus connectorStatus(JsonNode response) {
        Instant heartbeatAt = instant(response, "heartbeatAt");
        Instant authenticatedAt = instant(response, "authenticatedAt");
        return new BrokerConnectorStatus(
                connectorState(text(response, "runtimeStatus", "ERROR")),
                connectorState(text(response, "authStatus", "ERROR")),
                heartbeatAt,
                authenticatedAt,
                text(response, "loginUrl", null),
                text(response, "code", "ERROR"),
                text(response, "message", "IBKR connector returned an error."));
    }

    private static BrokerConnectorState connectorState(String value) {
        try {
            return BrokerConnectorState.valueOf(value);
        } catch (RuntimeException exception) {
            return BrokerConnectorState.ERROR;
        }
    }

    private static Instant instant(JsonNode response, String field) {
        String value = text(response, field, null);
        if (isBlank(value)) {
            return null;
        }
        try {
            return Instant.parse(value);
        } catch (RuntimeException exception) {
            return null;
        }
    }

    private static boolean isBlank(String value) {
        return value == null || value.isBlank();
    }
}
