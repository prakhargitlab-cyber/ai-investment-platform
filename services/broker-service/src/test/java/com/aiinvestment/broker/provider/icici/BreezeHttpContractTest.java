package com.aiinvestment.broker.provider.icici;

import org.junit.jupiter.api.Test;
import org.springframework.http.HttpMethod;
import org.springframework.http.MediaType;
import org.springframework.test.web.client.MockRestServiceServer;
import org.springframework.web.client.RestClient;

import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.client.ExpectedCount.once;
import static org.springframework.test.web.client.match.MockRestRequestMatchers.*;
import static org.springframework.test.web.client.response.MockRestResponseCreators.withServerError;
import static org.springframework.test.web.client.response.MockRestResponseCreators.withSuccess;

class BreezeHttpContractTest {
    @Test
    void customerDetailsUsesVerifiedGetJsonBodyWithoutSignedHeaders() {
        RestClient.Builder builder = RestClient.builder();
        MockRestServiceServer server = MockRestServiceServer.bindTo(builder).build();
        server.expect(once(), requestTo("https://api.icicidirect.com/breezeapi/api/v1/customerdetails"))
                .andExpect(method(HttpMethod.GET))
                .andExpect(content().contentType(MediaType.APPLICATION_JSON))
                .andExpect(content().json("{\"SessionToken\":\"dummy-api-session\",\"AppKey\":\"dummy-app-key\"}"))
                .andExpect(headerDoesNotExist("X-Checksum"))
                .andRespond(withSuccess("""
                        {"Status":200,"Success":{"session_token":"dummy-session-token",
                        "idirect_userid":"USER-A","idirect_user_name":"User A"}}
                        """, MediaType.APPLICATION_JSON));

        BreezeCustomerDetails details = new BreezeHttpCustomerDetailsClient(
                BreezeICICIDirectConnectorTest.properties(), builder.build())
                .exchangeApiSession("dummy-app-key", "dummy-api-session");

        assertThat(details.providerUserId()).isEqualTo("USER-A");
        server.verify();
    }

    @Test
    void dematAndFundsUseOnlyVerifiedGetPathsEmptyBodyAndSignedHeaders() {
        RestClient.Builder builder = RestClient.builder();
        MockRestServiceServer server = MockRestServiceServer.bindTo(builder).build();
        BreezeSignedHeaders headers = new BreezeSignedHeaders(Map.of(
                "Content-Type", "application/json", "X-Checksum", "token dummy-checksum",
                "X-Timestamp", "2026-08-29T10:15:30.000Z", "X-AppKey", "dummy-app-key",
                "X-SessionToken", "dummy-session-token"));
        server.expect(once(), requestTo("https://api.icicidirect.com/breezeapi/api/v1/dematholdings"))
                .andExpect(method(HttpMethod.GET)).andExpect(content().json("{}"))
                .andExpect(header("X-Checksum", "token dummy-checksum"))
                .andRespond(withSuccess("""
                        {"Status":200,"Success":[{"stock_code":"TCS","stock_ISIN":"INE467B01029",
                        "quantity":"1.125","demat_avail_quantity":"1.000"}]}
                        """, MediaType.APPLICATION_JSON));
        server.expect(once(), requestTo("https://api.icicidirect.com/breezeapi/api/v1/funds"))
                .andExpect(method(HttpMethod.GET)).andExpect(content().json("{}"))
                .andExpect(header("X-SessionToken", "dummy-session-token"))
                .andRespond(withSuccess("""
                        {"Status":200,"Success":{"total_bank_balance":"500.25",
                        "unallocated_balance":"9999.99","block_by_trade_balance":"7.00"}}
                        """, MediaType.APPLICATION_JSON));
        BreezeHttpReadClient client = new BreezeHttpReadClient(BreezeICICIDirectConnectorTest.properties(), builder.build());

        assertThat(client.fetchDematHoldings(headers).get(0).quantity()).isEqualByComparingTo("1.125");
        assertThat(client.fetchFunds(headers).totalBankBalance()).isEqualByComparingTo("500.25");
        server.verify();
    }

    @Test
    void providerFailureIsNotConvertedToEmptyHoldingsWhileVerifiedEmptyIsEmpty() {
        RestClient.Builder failingBuilder = RestClient.builder();
        MockRestServiceServer failing = MockRestServiceServer.bindTo(failingBuilder).build();
        failing.expect(requestTo("https://api.icicidirect.com/breezeapi/api/v1/dematholdings"))
                .andRespond(withServerError());
        BreezeSignedHeaders headers = new BreezeSignedHeaders(Map.of("Content-Type", "application/json"));
        BreezeHttpReadClient failingClient = new BreezeHttpReadClient(
                BreezeICICIDirectConnectorTest.properties(), failingBuilder.build());
        org.assertj.core.api.Assertions.assertThatThrownBy(() -> failingClient.fetchDematHoldings(headers))
                .isInstanceOf(com.aiinvestment.broker.application.BrokerProviderException.class);

        RestClient.Builder emptyBuilder = RestClient.builder();
        MockRestServiceServer empty = MockRestServiceServer.bindTo(emptyBuilder).build();
        empty.expect(requestTo("https://api.icicidirect.com/breezeapi/api/v1/dematholdings"))
                .andRespond(withSuccess("{\"Status\":200,\"Success\":[]}", MediaType.APPLICATION_JSON));
        assertThat(new BreezeHttpReadClient(BreezeICICIDirectConnectorTest.properties(), emptyBuilder.build())
                .fetchDematHoldings(headers)).isEmpty();
    }
}
