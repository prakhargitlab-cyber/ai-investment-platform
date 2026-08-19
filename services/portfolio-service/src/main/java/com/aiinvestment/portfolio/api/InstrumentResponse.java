package com.aiinvestment.portfolio.api;

import com.aiinvestment.shared.domain.AssetType;
import com.aiinvestment.shared.domain.Instrument;

import java.util.UUID;

public record InstrumentResponse(
        UUID instrumentId,
        String isin,
        String ticker,
        String exchange,
        String mic,
        String companyName,
        AssetType assetType,
        String country,
        String tradingCurrency,
        String sector,
        String industry
) {
    public static InstrumentResponse from(Instrument instrument) {
        return new InstrumentResponse(instrument.instrumentId(), instrument.isin(), instrument.ticker(), instrument.exchange(),
                instrument.mic(), instrument.companyName(), instrument.assetType(), instrument.country(),
                instrument.tradingCurrency(), instrument.sector(), instrument.industry());
    }
}
