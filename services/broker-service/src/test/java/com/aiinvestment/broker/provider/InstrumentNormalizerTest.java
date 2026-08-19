package com.aiinvestment.broker.provider;

import com.aiinvestment.broker.provider.ibkr.IBKRInstrumentNormalizer;
import com.aiinvestment.broker.provider.icici.ICICIDirectInstrumentNormalizer;
import com.aiinvestment.shared.domain.AssetType;
import com.aiinvestment.shared.domain.broker.BrokerInstrumentIdentity;
import com.aiinvestment.shared.domain.broker.BrokerType;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class InstrumentNormalizerTest {
    @Test
    void ibkrNormalizerUsesContractExchangeAndCurrencyInStableIdentity() {
        IBKRInstrumentNormalizer normalizer = new IBKRInstrumentNormalizer();

        var instrument = normalizer.normalize(new BrokerInstrumentIdentity(BrokerType.IBKR, null, "265598",
                "US67066G1040", "NVDA", "XNAS", "XNAS", "USD", "US", AssetType.EQUITY));

        assertThat(instrument.ticker()).isEqualTo("NVDA");
        assertThat(instrument.exchange()).isEqualTo("XNAS");
        assertThat(instrument.tradingCurrency()).isEqualTo("USD");
        assertThat(instrument.assetType()).isEqualTo(AssetType.EQUITY);
    }

    @Test
    void normalizerDoesNotTreatTickerAsGloballyUnique() {
        IBKRInstrumentNormalizer normalizer = new IBKRInstrumentNormalizer();

        var besi = normalizer.normalize(new BrokerInstrumentIdentity(BrokerType.IBKR, null, "481771",
                "NL0012866412", "BESI", "XAMS", "XAMS", "EUR", "NL", AssetType.EQUITY));
        var aixtron = normalizer.normalize(new BrokerInstrumentIdentity(BrokerType.IBKR, null, "123456",
                "DE000A0WMPJ6", "AIXTRON", "XETR", "XETR", "EUR", "DE", AssetType.EQUITY));
        var sameTickerDifferentVenue = normalizer.normalize(new BrokerInstrumentIdentity(BrokerType.IBKR, null, "999999",
                "US67066G1040", "NVDA", "XETR", "XETR", "EUR", "DE", AssetType.EQUITY));
        var nvda = normalizer.normalize(new BrokerInstrumentIdentity(BrokerType.IBKR, null, "265598",
                "US67066G1040", "NVDA", "XNAS", "XNAS", "USD", "US", AssetType.EQUITY));

        assertThat(besi.exchange()).isEqualTo("XAMS");
        assertThat(aixtron.exchange()).isEqualTo("XETR");
        assertThat(nvda.exchange()).isEqualTo("XNAS");
        assertThat(sameTickerDifferentVenue.instrumentId()).isNotEqualTo(nvda.instrumentId());
    }

    @Test
    void iciciNormalizerRequiresExplicitIndianMarketIdentity() {
        ICICIDirectInstrumentNormalizer normalizer = new ICICIDirectInstrumentNormalizer();

        var instrument = normalizer.normalize(new BrokerInstrumentIdentity(BrokerType.ICICI_DIRECT, "INE002A01018",
                null, "INE002A01018", "RELIANCE", "XNSE", "XNSE", "INR", "IN", AssetType.EQUITY));

        assertThat(instrument.country()).isEqualTo("IN");
        assertThat(instrument.tradingCurrency()).isEqualTo("INR");
        assertThat(instrument.isin()).isEqualTo("INE002A01018");
        assertThatThrownBy(() -> normalizer.normalize(new BrokerInstrumentIdentity(BrokerType.ICICI_DIRECT, "x",
                null, null, "RELIANCE", "XNSE", "XNSE", "USD", "US", AssetType.EQUITY))).isInstanceOf(IllegalArgumentException.class);
    }
}
