package com.aiinvestment.broker.partner;

public record PartnerAuthorization(String authenticationUrl, String state) {
    public PartnerAuthorization {
        if (authenticationUrl == null || authenticationUrl.isBlank()) {
            throw new IllegalArgumentException("Partner authentication URL is required");
        }
    }
}
