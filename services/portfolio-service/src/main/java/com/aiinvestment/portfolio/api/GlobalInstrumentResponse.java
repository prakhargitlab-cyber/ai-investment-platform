package com.aiinvestment.portfolio.api;

import com.aiinvestment.portfolio.application.InstrumentMasterService;
import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentProviderMappingEntity;
import com.aiinvestment.shared.domain.AssetType;

import java.math.BigDecimal;
import java.util.List;
import java.util.UUID;

public record GlobalInstrumentResponse(
        UUID globalInstrumentId,
        String canonicalName,
        String isin,
        AssetType assetType,
        String currency,
        String country,
        String primaryExchange,
        String primarySymbol,
        String status,
        List<GlobalInstrumentProviderMappingResponse> providerMappings
) {
    public static GlobalInstrumentResponse from(InstrumentMasterService.GlobalInstrument value) {
        var master = value.master();
        return new GlobalInstrumentResponse(
                master.getInstrumentId(), master.getCanonicalName(), master.getIsin(), master.getAssetType(),
                master.getCurrency(), master.getCountry(), master.getPrimaryExchange(), master.getPrimarySymbol(),
                master.getStatus(), value.providerMappings().stream().map(GlobalInstrumentProviderMappingResponse::from).toList()
        );
    }

    public record GlobalInstrumentProviderMappingResponse(
            String provider,
            String providerSymbol,
            String providerInstrumentId,
            String exchange,
            String currency,
            String status,
            String resolutionSource,
            BigDecimal confidence
    ) {
        static GlobalInstrumentProviderMappingResponse from(InstrumentProviderMappingEntity value) {
            return new GlobalInstrumentProviderMappingResponse(
                    value.getProvider(), value.getProviderSymbol(), value.getProviderInstrumentId(), value.getExchange(),
                    value.getCurrency(), value.getStatus(), value.getResolutionSource(), value.getConfidence()
            );
        }
    }
}
