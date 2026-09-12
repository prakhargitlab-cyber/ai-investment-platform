package com.aiinvestment.broker.provider.ibkr;

import com.aiinvestment.broker.application.BrokerProviderException;
import com.aiinvestment.broker.config.IBKRProviderProperties;
import com.aiinvestment.broker.connector.ibkr.IBKRIndividualConnector;
import com.aiinvestment.shared.domain.broker.BrokerCapability;
import com.aiinvestment.shared.domain.broker.BrokerProviderStatus;
import com.aiinvestment.shared.domain.broker.BrokerType;
import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;
import org.springframework.http.HttpStatus;
import org.springframework.test.web.client.MockRestServiceServer;
import org.springframework.web.client.RestClient;

import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.springframework.test.web.client.ExpectedCount.once;
import static org.springframework.test.web.client.response.MockRestResponseCreators.withStatus;
import static org.springframework.test.web.client.match.MockRestRequestMatchers.method;
import static org.springframework.test.web.client.match.MockRestRequestMatchers.requestTo;
import static org.springframework.test.web.client.response.MockRestResponseCreators.withSuccess;
import static org.springframework.test.web.client.response.MockRestResponseCreators.withUnauthorizedRequest;

class IBKRBrokerProviderTest {
    private static final String CONNECTOR_BASE_URL = "https://ibkr-connector.test";
    private static final String INTERNAL_TOKEN = "test-internal-token";

    private final RestClient.Builder builder = RestClient.builder().baseUrl(CONNECTOR_BASE_URL);
    private final MockRestServiceServer server = MockRestServiceServer.bindTo(builder).build();
    private final IBKRBrokerProvider provider = provider(validClientPortalGatewayProperties(), builder);

    @Test
    void reportsAuthenticationRequiredWithoutAuthenticatedGatewaySession() {
        authenticated(false);

        assertThat(provider.connectionStatus().code()).isEqualTo("AUTHENTICATION_REQUIRED");
        assertThat(provider.connectionCapabilities().capabilities())
                .contains(BrokerCapability.ACCOUNTS_READ, BrokerCapability.POSITIONS_READ, BrokerCapability.CASH_READ)
                .doesNotContain(BrokerCapability.ORDER_EXECUTION);
    }

    @Test
    void providerDisabledWhenDocsAreUnverified() {
        IBKRBrokerProvider unverified = provider(new IBKRProviderProperties(true, "", "", "", "", "", "",
                "client-portal-gateway", false, false, false, CONNECTOR_BASE_URL, "LOCAL_AGENT", "", INTERNAL_TOKEN, 1800, 86400, "", ""), builder);

        assertThat(unverified.connectionStatus().providerStatus()).isEqualTo(BrokerProviderStatus.DOCUMENTATION_REQUIRED);
        assertThat(unverified.connectionCapabilities().capabilities()).isEmpty();
    }

    @Test
    void oauth2PrivateKeyJwtIsNotIndividualAccountProductionTarget() {
        IBKRBrokerProvider oauth2 = provider(new IBKRProviderProperties(true, "https://api.ibkr.com/v1/api",
                "https://api.ibkr.com/oauth2", "https://api.ibkr.com", "client", "key-id", "",
                "oauth2-private-key-jwt", true, false, false,
                "https://api.ibkr.com/v1/api", "LOCAL_AGENT", "", INTERNAL_TOKEN, 1800, 86400,
                "k8s-secret:ibkr-private-key", "IBKR_SESSION_TOKEN"), builder);

        assertThat(oauth2.connectionStatus().providerStatus()).isEqualTo(BrokerProviderStatus.DOCUMENTATION_REQUIRED);
        assertThat(oauth2.connectionCapabilities().capabilities()).isEmpty();
    }

    @Test
    void missingGatewayBaseUrlIsNotConfigured() {
        IBKRBrokerProvider missingBaseUrl = provider(new IBKRProviderProperties(true, "",
                "", "", "", "", "", "client-portal-gateway", true, false, false,
                "", "LOCAL_AGENT", "", INTERNAL_TOKEN, 1800, 86400, "", ""), builder);

        assertThat(missingBaseUrl.connectionStatus().code()).isEqualTo("NOT_CONFIGURED");
        assertThat(missingBaseUrl.connectionCapabilities().capabilities()).isEmpty();
    }

    @Test
    void missingMarketDataEntitlementKeepsMarketDataCapabilityDisabled() {
        assertThat(provider.connectionCapabilities().capabilities())
                .contains(BrokerCapability.ACCOUNTS_READ, BrokerCapability.ACCOUNT_METADATA_READ,
                        BrokerCapability.PORTFOLIO_READ, BrokerCapability.POSITIONS_READ, BrokerCapability.CASH_READ)
                .doesNotContain(BrokerCapability.MARKET_DATA_READ, BrokerCapability.DELAYED_QUOTES,
                        BrokerCapability.LIVE_QUOTES, BrokerCapability.ORDER_EXECUTION);
    }

    @Test
    void normalizesMultipleAccountsPositionsAndCashThroughAuthenticatedGatewaySession() {
        UUID userId = UUID.randomUUID();
        authenticated(true);
        server.expect(once(), requestTo(CONNECTOR_BASE_URL + "/internal/connectors/00000000-0000-0000-0000-000000000000/accounts"))
                .andRespond(withSuccess("""
                        {"data": [
                          {"id":"U1234567","accountAlias":"Main IBKR","currency":"EUR"},
                          {"accountId":"U7654321","accountTitle":"Secondary IBKR","currency":"USD"}
                        ]}
                        """, MediaType.APPLICATION_JSON));
        authenticated(true);
        server.expect(once(), requestTo(CONNECTOR_BASE_URL + "/internal/connectors/00000000-0000-0000-0000-000000000000/positions?account_id=U1234567&page=0"))
                .andRespond(withSuccess("""
                        {"data": [
                          {"conid": "123456", "ticker": "AIXA", "listingExchange": "AEB", "currency": "EUR",
                           "position": "40", "avgCost": "18.25", "mktPrice": "24.10", "isin": "DE000A0WMPJ6"},
                          {"conid": "789012", "ticker": "IUSA", "listingExchange": "AEB", "currency": "EUR",
                           "position": "3", "avgCost": "48.25", "mktPrice": "50.10", "contractDesc": "IUSA"},
                          {"conid": "345678", "ticker": "R3NK", "listingExchange": "IBIS2", "currency": "EUR",
                           "position": "7", "avgCost": "22.25", "mktPrice": "24.10", "contractDesc": "R3NK"}
                        ]}
                        """, MediaType.APPLICATION_JSON));
        server.expect(once(), requestTo(CONNECTOR_BASE_URL + "/internal/connectors/00000000-0000-0000-0000-000000000000/instruments/123456"))
                .andRespond(withSuccess("""
                        {"data": {"symbol":"AIXA","description":"AIXTRON SE","listingExchange":"IBIS","currency":"EUR",
                          "isin":"DE000A0WMPJ6","secType":"STK"}}
                        """, MediaType.APPLICATION_JSON));
        server.expect(once(), requestTo(CONNECTOR_BASE_URL + "/internal/connectors/00000000-0000-0000-0000-000000000000/instruments/789012"))
                .andRespond(withSuccess("""
                        {"data": {"symbol":"IUSA","description":"iShares Core S&P 500 UCITS ETF","listingExchange":"AEB",
                          "currency":"EUR","isin":"IE0031442068","secType":"ETF"}}
                        """, MediaType.APPLICATION_JSON));
        server.expect(once(), requestTo(CONNECTOR_BASE_URL + "/internal/connectors/00000000-0000-0000-0000-000000000000/instruments/345678"))
                .andRespond(withSuccess("""
                        {"data": {"symbol":"R3NK","description":"RENK Group AG","listingExchange":"IBIS2",
                          "currency":"EUR","isin":"DE000RENK730","secType":"STK","country":"DE"}}
                        """, MediaType.APPLICATION_JSON));
        authenticated(true);
        server.expect(once(), requestTo(CONNECTOR_BASE_URL + "/internal/connectors/00000000-0000-0000-0000-000000000000/ledger?account_id=U1234567"))
                .andRespond(withSuccess("""
                        {"data": {
                          "BASE":{"currency":"BASE","cashbalance":"123.45","netliquidationvalue":"456.78"},
                          "EUR":{"currency":"EUR","cashbalance":"123.45","settledcash":"120.00",
                            "netliquidationvalue":"456.78","stockmarketvalue":"333.33",
                            "unrealizedpnl":"12.34","realizedpnl":"5.67"}
                        }}
                        """, MediaType.APPLICATION_JSON));

        var accounts = provider.fetchAccounts(userId);
        var account = accounts.get(0);
        var positions = provider.fetchPositions(account);
        var position = positions.get(0);
        var etf = positions.get(1);
        var renk = positions.get(2);
        var cashBalances = provider.fetchCashBalances(account);
        var cash = cashBalances.get(0);

        assertThat(accounts).hasSize(2);
        assertThat(account.brokerType()).isEqualTo(BrokerType.IBKR);
        assertThat(account.externalAccountReference()).isEqualTo("****4567");
        assertThat(accounts.get(1).externalAccountReference()).isEqualTo("****4321");
        assertThat(position.instrument().ticker()).isEqualTo("AIXA");
        assertThat(position.instrument().exchange()).isEqualTo("XETR");
        assertThat(position.instrument().mic()).isEqualTo("XETR");
        assertThat(position.instrument().isin()).isEqualTo("DE000A0WMPJ6");
        assertThat(position.instrument().provider()).isEqualTo("IBKR");
        assertThat(position.instrument().providerInstrumentId()).isEqualTo("123456");
        assertThat(position.instrument().companyName()).isEqualTo("AIXTRON SE");
        assertThat(position.instrument().brokerSymbol()).isEqualTo("AIXA");
        assertThat(position.instrument().brokerExchange()).isEqualTo("AEB");
        assertThat(position.instrument().canonicalSymbol()).isEqualTo("AIXA");
        assertThat(position.instrument().canonicalName()).isEqualTo("AIXTRON SE");
        assertThat(position.instrument().canonicalExchange()).isEqualTo("XETR");
        assertThat(position.instrument().securityType()).isEqualTo("STK");
        assertThat(position.instrument().instrumentId()).isNotNull();
        assertThat(etf.instrument().provider()).isEqualTo("IBKR");
        assertThat(etf.instrument().providerInstrumentId()).isEqualTo("789012");
        assertThat(etf.instrument().companyName()).isEqualTo("iShares Core S&P 500 UCITS ETF");
        assertThat(etf.instrument().assetType().name()).isEqualTo("ETF");
        assertThat(etf.instrument().isin()).isEqualTo("IE0031442068");
        assertThat(renk.instrument().providerInstrumentId()).isEqualTo("345678");
        assertThat(renk.instrument().companyName()).isEqualTo("RENK Group AG");
        assertThat(renk.instrument().assetType().name()).isEqualTo("EQUITY");
        assertThat(cashBalances).hasSize(1);
        assertThat(cash.cash().currency()).isEqualTo("EUR");
        assertThat(cash.cash().amount()).isEqualByComparingTo("123.45");
        assertThat(cash.settledCash().amount()).isEqualByComparingTo("120.00");
        assertThat(cash.netLiquidationValue().amount()).isEqualByComparingTo("456.78");
        assertThat(cash.stockMarketValue().amount()).isEqualByComparingTo("333.33");
        assertThat(cash.unrealizedPnl().amount()).isEqualByComparingTo("12.34");
        assertThat(cash.realizedPnl().amount()).isEqualByComparingTo("5.67");
        assertThat(cash.source()).isEqualTo("REAL_BROKER");
    }

    @Test
    void rejectsMalformedPositionResponse() {
        authenticated(true);
        var account = new com.aiinvestment.shared.domain.broker.BrokerAccount("IBKR_U1234567", UUID.randomUUID(),
                BrokerType.IBKR, "****4567", "Main", "EUR", com.aiinvestment.shared.domain.broker.BrokerAccountStatus.ACTIVE);
        server.expect(once(), requestTo(CONNECTOR_BASE_URL + "/internal/connectors/00000000-0000-0000-0000-000000000000/positions?account_id=U1234567&page=0"))
                .andRespond(withSuccess("{\"data\":[{\"ticker\":\"AIXA\",\"exchange\":\"XETR\",\"currency\":\"EUR\"}]}", MediaType.APPLICATION_JSON));

        assertThatThrownBy(() -> provider.fetchPositions(account))
                .isInstanceOf(BrokerProviderException.class)
                .hasMessageContaining("unexpected response");
    }

    @Test
    void fallsBackToRawPositionIdentityWhenContractMetadataIsUnavailable() {
        authenticated(true);
        var account = new com.aiinvestment.shared.domain.broker.BrokerAccount("IBKR_U1234567", UUID.randomUUID(),
                BrokerType.IBKR, "****4567", "Main", "EUR", com.aiinvestment.shared.domain.broker.BrokerAccountStatus.ACTIVE);
        server.expect(once(), requestTo(CONNECTOR_BASE_URL + "/internal/connectors/00000000-0000-0000-0000-000000000000/positions?account_id=U1234567&page=0"))
                .andRespond(withSuccess("""
                        {"data": [
                          {"conid": "789012", "ticker": "IUSA", "listingExchange": "AEB", "currency": "EUR",
                           "position": "3", "avgCost": "48.25", "mktPrice": "50.10", "contractDesc": "IUSA",
                           "name": "ISHARES CORE S&P 500", "type": "ETF", "assetClass": "STK"}
                        ]}
                        """, MediaType.APPLICATION_JSON));
        server.expect(once(), requestTo(CONNECTOR_BASE_URL + "/internal/connectors/00000000-0000-0000-0000-000000000000/instruments/789012"))
                .andRespond(withStatus(org.springframework.http.HttpStatus.METHOD_NOT_ALLOWED));

        var positions = provider.fetchPositions(account);

        assertThat(positions).hasSize(1);
        assertThat(positions.get(0).instrument().provider()).isEqualTo("IBKR");
        assertThat(positions.get(0).instrument().providerInstrumentId()).isEqualTo("789012");
        assertThat(positions.get(0).instrument().exchange()).isEqualTo("XAMS");
        assertThat(positions.get(0).instrument().canonicalName()).isEqualTo("ISHARES CORE S&P 500");
        assertThat(positions.get(0).instrument().assetType().name()).isEqualTo("ETF");
        assertThat(positions.get(0).instrument().brokerDescription()).isEqualTo("IUSA");
    }

    @Test
    void mapsCommonIbkrTypeToEquity() {
        authenticated(true);
        var account = new com.aiinvestment.shared.domain.broker.BrokerAccount("IBKR_U1234567", UUID.randomUUID(),
                BrokerType.IBKR, "****4567", "Main", "EUR", com.aiinvestment.shared.domain.broker.BrokerAccountStatus.ACTIVE);
        server.expect(once(), requestTo(CONNECTOR_BASE_URL + "/internal/connectors/00000000-0000-0000-0000-000000000000/positions?account_id=U1234567&page=0"))
                .andRespond(withSuccess("""
                        {"data": [
                          {"conid": "316537032", "ticker": "BESI", "listingExchange": "AEB", "currency": "EUR",
                           "position": "3", "avgCost": "252.12", "mktPrice": "208.80", "contractDesc": "BESI",
                           "name": "BE SEMICONDUCTOR INDUSTRIES", "type": "COMMON", "assetClass": "STK"}
                        ]}
                        """, MediaType.APPLICATION_JSON));
        server.expect(once(), requestTo(CONNECTOR_BASE_URL + "/internal/connectors/00000000-0000-0000-0000-000000000000/instruments/316537032"))
                .andRespond(withStatus(org.springframework.http.HttpStatus.METHOD_NOT_ALLOWED));

        var positions = provider.fetchPositions(account);

        assertThat(positions).hasSize(1);
        assertThat(positions.get(0).instrument().assetType().name()).isEqualTo("EQUITY");
        assertThat(positions.get(0).instrument().canonicalName()).isEqualTo("BE SEMICONDUCTOR INDUSTRIES");
        assertThat(positions.get(0).instrument().canonicalExchange()).isEqualTo("XAMS");
        assertThat(positions.get(0).instrument().securityType()).isEqualTo("COMMON");
    }

    @Test
    void unauthorizedGatewayResponseIsMappedToExpiredSession() {
        server.expect(once(), requestTo(CONNECTOR_BASE_URL + "/internal/connectors/00000000-0000-0000-0000-000000000000/status"))
                .andExpect(method(org.springframework.http.HttpMethod.GET))
                .andRespond(withUnauthorizedRequest());

        assertThatThrownBy(() -> provider.fetchAccounts(UUID.randomUUID()))
                .isInstanceOf(BrokerProviderException.class)
                .hasMessageContaining("session is expired");
    }

    @Test
    void connectorCapacityConflictIsSanitizedAndMappedToDomainConflict() {
        UUID userId = UUID.randomUUID();
        UUID connectorId = UUID.randomUUID();
        server.expect(once(), requestTo(CONNECTOR_BASE_URL + "/internal/connectors"))
                .andExpect(method(org.springframework.http.HttpMethod.POST))
                .andRespond(withStatus(HttpStatus.CONFLICT).contentType(MediaType.APPLICATION_JSON).body("""
                        {"code":"DEV_SINGLE_CONNECTOR_LIMIT","message":"connector=secret-other-user"}
                        """));

        assertThatThrownBy(() -> connector().start(userId, connectorId))
                .isInstanceOf(BrokerProviderException.class)
                .satisfies(exception -> {
                    BrokerProviderException mapped = (BrokerProviderException) exception;
                    assertThat(mapped.status()).isEqualTo(HttpStatus.CONFLICT);
                    assertThat(mapped.code()).isEqualTo("IBKR_CONNECTOR_CAPACITY_UNAVAILABLE");
                    assertThat(mapped.getMessage()).doesNotContain("secret-other-user", connectorId.toString(), userId.toString());
                });
        server.verify();
    }

    @Test
    void unknownConnectorConflictDoesNotExposeUpstreamBody() {
        server.expect(once(), requestTo(CONNECTOR_BASE_URL + "/internal/connectors"))
                .andRespond(withStatus(HttpStatus.CONFLICT).contentType(MediaType.APPLICATION_JSON)
                        .body("{\"code\":\"UNSAFE_DETAIL\",\"message\":\"account=U1234567\"}"));

        assertThatThrownBy(() -> connector().start(UUID.randomUUID(), UUID.randomUUID()))
                .isInstanceOf(BrokerProviderException.class)
                .satisfies(exception -> {
                    BrokerProviderException mapped = (BrokerProviderException) exception;
                    assertThat(mapped.status()).isEqualTo(HttpStatus.CONFLICT);
                    assertThat(mapped.code()).isEqualTo("IBKR_CONNECTOR_CONFLICT");
                    assertThat(mapped.getMessage()).doesNotContain("UNSAFE_DETAIL", "U1234567");
                });
    }

    @Test
    void connectorServerFailureRemainsUnavailable() {
        server.expect(once(), requestTo(CONNECTOR_BASE_URL + "/internal/connectors"))
                .andRespond(withStatus(HttpStatus.INTERNAL_SERVER_ERROR));

        assertThatThrownBy(() -> connector().start(UUID.randomUUID(), UUID.randomUUID()))
                .isInstanceOf(BrokerProviderException.class)
                .satisfies(exception -> assertThat(((BrokerProviderException) exception).status()).isEqualTo(HttpStatus.BAD_GATEWAY));
    }

    private static IBKRBrokerProvider provider(IBKRProviderProperties properties, RestClient.Builder builder) {
        return new IBKRBrokerProvider(properties,
                new IBKRIndividualConnector(properties, new IBKRInstrumentNormalizer(), builder.build()));
    }

    private IBKRIndividualConnector connector() {
        return new IBKRIndividualConnector(validClientPortalGatewayProperties(), new IBKRInstrumentNormalizer(), builder.build());
    }

    private static IBKRProviderProperties validClientPortalGatewayProperties() {
        return new IBKRProviderProperties(true, CONNECTOR_BASE_URL, "", "", "", "", "",
                "client-portal-gateway", true, false, true,
                CONNECTOR_BASE_URL, "LOCAL_AGENT", "", INTERNAL_TOKEN, 1800, 86400, "", "");
    }

    private void authenticated(boolean authenticated) {
        server.expect(once(), requestTo(CONNECTOR_BASE_URL + "/internal/connectors/00000000-0000-0000-0000-000000000000/status"))
                .andExpect(method(org.springframework.http.HttpMethod.GET))
                .andRespond(withSuccess("""
                        {"runtimeStatus":"%s","authStatus":"%s","heartbeatAt":"2026-08-23T00:00:00Z",
                         "authenticatedAt":%s,"code":"%s","message":"test"}
                        """.formatted(
                        authenticated ? "CONNECTED" : "AUTHENTICATION_REQUIRED",
                        authenticated ? "CONNECTED" : "AUTHENTICATION_REQUIRED",
                        authenticated ? "\"2026-08-23T00:00:00Z\"" : "null",
                        authenticated ? "CONNECTED" : "AUTHENTICATION_REQUIRED"), MediaType.APPLICATION_JSON));
    }
}
