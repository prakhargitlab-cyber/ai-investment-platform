package com.aiinvestment.portfolio.api;
import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentProviderMappingEntity;
import java.math.BigDecimal;
public record InstrumentProviderMappingResponse(String provider,String providerSymbol,String providerInstrumentId,
        String exchange,String currency,String status,String resolutionSource,String failureReason,BigDecimal confidence) {
    public static InstrumentProviderMappingResponse from(InstrumentProviderMappingEntity value){return new InstrumentProviderMappingResponse(
            value.getProvider(),value.getProviderSymbol(),value.getProviderInstrumentId(),value.getExchange(),value.getCurrency(),
            value.getStatus(),value.getResolutionSource(),value.getFailureReason(),value.getConfidence());}
}
