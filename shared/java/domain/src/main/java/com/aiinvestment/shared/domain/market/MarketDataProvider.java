package com.aiinvestment.shared.domain.market;

import com.aiinvestment.shared.domain.Instrument;

import java.util.List;

public interface MarketDataProvider {
    Quote getQuote(Instrument instrument);

    List<Quote> getQuotes(List<Instrument> instruments);

    MarketDataStatus getMarketDataStatus(Instrument instrument);
}
