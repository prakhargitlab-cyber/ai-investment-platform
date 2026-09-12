package com.aiinvestment.portfolio.application;

import com.aiinvestment.portfolio.infrastructure.persistence.*;
import com.aiinvestment.shared.domain.AssetType;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.math.BigDecimal;
import java.util.*;
import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

class ProviderIdentityClientTest {
    @ParameterizedTest @ValueSource(ints={200,422,503})
    void identityOnlyWireContractNeverFetchesResearchAndPersistsOnlyValidatedResponses(int status) throws Exception {
        UUID id=UUID.randomUUID();
        var instruments=mock(InstrumentMasterService.class);
        var nse=mock(NseMappingReconciliationService.class);
        var master=new InstrumentMasterEntity(id,"INE111A01010","Alpha Components Limited",AssetType.EQUITY,"INR","IN","NSE","ALPHA","ACTIVE",Instant.now());
        var mapping=new InstrumentProviderMappingEntity(UUID.randomUUID(),id,"NSE","ALPHA","INE111A01010","NSE","INR","VERIFIED","NSE_OFFICIAL_ISIN_BOOTSTRAP",new BigDecimal(".99"),Instant.now());
        when(instruments.globalInstrument(id)).thenReturn(Optional.of(new InstrumentMasterService.GlobalInstrument(master,List.of(mapping))));
        List<String> paths=new ArrayList<>();
        HttpServer server=HttpServer.create(new InetSocketAddress("127.0.0.1",0),0);
        server.createContext("/", exchange -> {
            paths.add(exchange.getRequestURI().getPath());
            assertThat(exchange.getRequestHeaders().getFirst("X-AIP-Service-Identity")).isEqualTo("portfolio-service");
            String body=new String(exchange.getRequestBody().readAllBytes(),StandardCharsets.UTF_8);
            assertThat(body).contains("VERIFIED_NSE","ALPHA.NS","INE111A01010");
            String response=status==200?"{\"instrument_id\":\""+id+"\",\"provider_ticker\":\"ALPHA.NS\",\"exchange\":\"NSI\",\"quote_type\":\"EQUITY\",\"status\":\"VERIFIED_NSE_CANDIDATE\"}":"{\"detail\":\"ISIN_MISMATCH\"}";
            byte[] bytes=response.getBytes(StandardCharsets.UTF_8);exchange.sendResponseHeaders(status,bytes.length);
            exchange.getResponseBody().write(bytes);exchange.close();
        });
        server.start();
        try {
            var client=new StructuredMarketClient(new ObjectMapper(),instruments,nse,"http://127.0.0.1:"+server.getAddress().getPort());
            if(status==200) {
                assertThat(client.resolveGlobalIdentity(id).providerTicker()).isEqualTo("ALPHA.NS");
                verify(instruments).saveResolvedMapping(eq(id),eq("YAHOO_FINANCE"),eq("ALPHA.NS"),isNull(),eq("NSE"),eq("INR"),eq("VERIFIED"),eq("YAHOO_FROM_VERIFIED_NSE"),eq(new BigDecimal(".95")));
            } else {
                assertThatThrownBy(()->client.resolveGlobalIdentity(id)).isInstanceOf(RuntimeException.class);
                verify(instruments,never()).saveResolvedMapping(any(),any(),any(),any(),any(),any(),any(),any(),any());
            }
            assertThat(paths).containsExactly("/internal/v1/research/instruments/resolve-provider");
            verifyNoInteractions(nse);
        } finally {server.stop(0);}
    }
}
