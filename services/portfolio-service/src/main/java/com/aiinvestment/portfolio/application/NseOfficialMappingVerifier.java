package com.aiinvestment.portfolio.application;

public interface NseOfficialMappingVerifier {
    Verification verify(String candidateSymbol);

    record Verification(boolean recognized, String symbol, String isin, String companyName, String reason) {
        static Verification rejected(String reason) {
            return new Verification(false, null, null, null, reason);
        }
    }
}
