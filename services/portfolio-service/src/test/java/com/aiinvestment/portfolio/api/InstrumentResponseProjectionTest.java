package com.aiinvestment.portfolio.api;

import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterEntity;
import com.aiinvestment.shared.domain.AssetType;
import com.aiinvestment.shared.domain.Instrument;
import org.junit.jupiter.api.Test;

import java.time.Instant;
import java.util.List;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;

class InstrumentResponseProjectionTest {

    @Test
    void linkedMasterOverridesUnknownCanonicalIdentityButRetainsImportProvenance() {
        UUID masterId = UUID.randomUUID();
        InstrumentResponse projected = InstrumentResponse.from(importedInstrument())
                .withMaster(master(masterId, "FEDBAN", "NSE", "FEDERAL BANK LTD", "INE171A01029", "IN", "INR"),
                        List.of(new InstrumentProviderMappingResponse("NSE", "FEDBAN", null, "NSE", "INR", "VERIFIED",
                                "NSE_OFFICIAL_ISIN_BOOTSTRAP", null, null)));

        assertThat(projected.globalInstrumentId()).isEqualTo(masterId);
        assertThat(projected.canonicalSymbol()).isEqualTo("FEDBAN");
        assertThat(projected.canonicalExchange()).isEqualTo("NSE");
        assertThat(projected.canonicalName()).isEqualTo("FEDERAL BANK LTD");
        assertThat(projected.companyName()).isEqualTo("FEDERAL BANK LTD");
        assertThat(projected.isin()).isEqualTo("INE171A01029");
        assertThat(projected.country()).isEqualTo("IN");
        assertThat(projected.tradingCurrency()).isEqualTo("INR");
        assertThat(projected.provider()).isEqualTo("ICICI_DIRECT");
        assertThat(projected.providerInstrumentId()).isEqualTo("ISIN:INE171A01029");
        assertThat(projected.ticker()).isEqualTo("FEDBAN");
        assertThat(projected.exchange()).isEqualTo("UNKNOWN");
        assertThat(projected.brokerSymbol()).isEqualTo("FEDBAN");
        assertThat(projected.brokerExchange()).isBlank();
        assertThat(projected.providerMappings()).singleElement().extracting(InstrumentProviderMappingResponse::provider)
                .isEqualTo("NSE");
        assertThat(projected.providerMappings()).singleElement().extracting(InstrumentProviderMappingResponse::resolutionSource)
                .isEqualTo("NSE_OFFICIAL_ISIN_BOOTSTRAP");
    }

    @Test
    void unlinkedInstrumentRetainsUnknownWithoutGuessingNse() {
        InstrumentResponse projected = InstrumentResponse.from(importedInstrument());

        assertThat(projected.globalInstrumentId()).isNull();
        assertThat(projected.exchange()).isEqualTo("UNKNOWN");
        assertThat(projected.canonicalExchange()).isBlank();
    }

    @Test
    void partialMasterFallsBackOnlyToExistingLocalCanonicalFields() {
        UUID masterId = UUID.randomUUID();
        Instrument local = new Instrument(UUID.randomUUID(), "ICICI_DIRECT", "ISIN:INE171A01029", "INE171A01029",
                "FEDBAN", "UNKNOWN", null, "Federal Bank import name", AssetType.EQUITY, "IN", "INR", null, null,
                "FEDBAN", "Federal Bank import name", "", "LOCAL_CANONICAL", "Local canonical name", "XETR", null, "EQUITY");
        InstrumentMasterEntity partial = master(masterId, null, null, null, null, null, null);

        InstrumentResponse projected = InstrumentResponse.from(local).withMaster(partial, List.of());

        assertThat(projected.globalInstrumentId()).isEqualTo(masterId);
        assertThat(projected.canonicalSymbol()).isEqualTo("LOCAL_CANONICAL");
        assertThat(projected.canonicalExchange()).isEqualTo("XETR");
        assertThat(projected.canonicalName()).isEqualTo("Fallback master name");
        assertThat(projected.isin()).isEqualTo("INE171A01029");
    }

    private static Instrument importedInstrument() {
        return new Instrument(UUID.randomUUID(), "ICICI_DIRECT", "ISIN:INE171A01029", "INE171A01029",
                "FEDBAN", "UNKNOWN", null, "Federal Bank import name", AssetType.EQUITY, "IN", "INR", null, null,
                "FEDBAN", "Federal Bank import name", "", "FEDBAN", "", "", null, "EQUITY");
    }

    private static InstrumentMasterEntity master(UUID id, String symbol, String exchange, String name, String isin,
                                                   String country, String currency) {
        return new InstrumentMasterEntity(id, isin, name == null ? "Fallback master name" : name, AssetType.EQUITY,
                currency == null ? "INR" : currency, country, exchange, symbol, "ACTIVE", Instant.now());
    }
}
