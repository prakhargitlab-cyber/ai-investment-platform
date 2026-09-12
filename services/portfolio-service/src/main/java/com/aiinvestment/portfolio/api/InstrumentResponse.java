package com.aiinvestment.portfolio.api;

import com.aiinvestment.shared.domain.AssetType;
import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterEntity;
import com.aiinvestment.shared.domain.Instrument;

import java.util.UUID;
import java.util.List;

public record InstrumentResponse(
        UUID instrumentId,
        UUID globalInstrumentId,
        String provider,
        String providerInstrumentId,
        String isin,
        String ticker,
        String exchange,
        String mic,
        String companyName,
        AssetType assetType,
        String country,
        String tradingCurrency,
        String sector,
        String industry,
        String brokerSymbol,
        String brokerDescription,
        String brokerExchange,
        String canonicalSymbol,
        String canonicalName,
        String canonicalExchange,
        String canonicalMic,
        String securityType,
        List<InstrumentProviderMappingResponse> providerMappings
) {
    public static InstrumentResponse from(Instrument instrument) {
        return new InstrumentResponse(instrument.instrumentId(), null, instrument.provider(), instrument.providerInstrumentId(),
                instrument.isin(), instrument.ticker(), instrument.exchange(),
                instrument.mic(), instrument.companyName(), instrument.assetType(), instrument.country(),
                instrument.tradingCurrency(), instrument.sector(), instrument.industry(), instrument.brokerSymbol(),
                instrument.brokerDescription(), instrument.brokerExchange(), instrument.canonicalSymbol(),
                instrument.canonicalName(), instrument.canonicalExchange(), instrument.canonicalMic(),
                instrument.securityType(), List.of());
    }
    public static InstrumentResponse from(Instrument instrument, List<InstrumentProviderMappingResponse> mappings) {
        InstrumentResponse base=from(instrument);
        return new InstrumentResponse(base.instrumentId,base.globalInstrumentId,base.provider,base.providerInstrumentId,base.isin,base.ticker,
                base.exchange,base.mic,base.companyName,base.assetType,base.country,base.tradingCurrency,base.sector,
                base.industry,base.brokerSymbol,base.brokerDescription,base.brokerExchange,base.canonicalSymbol,
                base.canonicalName,base.canonicalExchange,base.canonicalMic,base.securityType,List.copyOf(mappings));
    }
    public InstrumentResponse withMappings(List<InstrumentProviderMappingResponse> mappings) {
        return new InstrumentResponse(instrumentId,globalInstrumentId,provider,providerInstrumentId,isin,ticker,exchange,mic,companyName,
                assetType,country,tradingCurrency,sector,industry,brokerSymbol,brokerDescription,brokerExchange,
                canonicalSymbol,canonicalName,canonicalExchange,canonicalMic,securityType,List.copyOf(mappings));
    }
    public InstrumentResponse withMaster(UUID masterId, List<InstrumentProviderMappingResponse> mappings) {
        return new InstrumentResponse(instrumentId,masterId,provider,providerInstrumentId,isin,ticker,exchange,mic,companyName,
                assetType,country,tradingCurrency,sector,industry,brokerSymbol,brokerDescription,brokerExchange,
                canonicalSymbol,canonicalName,canonicalExchange,canonicalMic,securityType,List.copyOf(mappings));
    }

    /**
     * Adds the authoritative global identity without overwriting the local broker/import provenance fields.
     * A linked master is the only source allowed to replace canonical identity placeholders such as UNKNOWN.
     */
    public InstrumentResponse withMaster(InstrumentMasterEntity master, List<InstrumentProviderMappingResponse> mappings) {
        return new InstrumentResponse(
                instrumentId,
                master.getInstrumentId(),
                provider,
                providerInstrumentId,
                preferred(master.getIsin(), isin),
                ticker,
                exchange,
                mic,
                preferred(master.getCanonicalName(), companyName),
                assetType,
                preferred(master.getCountry(), country),
                preferred(master.getCurrency(), tradingCurrency),
                sector,
                industry,
                brokerSymbol,
                brokerDescription,
                brokerExchange,
                preferred(master.getPrimarySymbol(), canonicalSymbol),
                preferred(master.getCanonicalName(), canonicalName),
                preferred(master.getPrimaryExchange(), canonicalExchange),
                canonicalMic,
                securityType,
                List.copyOf(mappings)
        );
    }

    private static String preferred(String authoritative, String fallback) {
        return meaningful(authoritative) ? authoritative : fallback;
    }

    private static boolean meaningful(String value) {
        return value != null && !value.isBlank()
                && !"UNKNOWN".equalsIgnoreCase(value.trim())
                && !"N/A".equalsIgnoreCase(value.trim());
    }
}
