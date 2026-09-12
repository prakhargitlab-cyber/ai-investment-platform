package com.aiinvestment.broker.provider.icici;

import java.util.Map;

public record BreezeSignedHeaders(Map<String, String> values) {
    public BreezeSignedHeaders {
        values = Map.copyOf(values);
    }
}
