package com.aiinvestment.portfolio.application;

/** Read-only exact-ISIN lookup in NSE's public ETF securities available-for-trading list. */
public interface NseOfficialEtfSecurityList {
    Lookup lookupByIsin(String isin);

    record Lookup(String status, String symbol, String isin, String securityName, String underlying) {
        static Lookup matched(String symbol, String isin, String securityName, String underlying) {
            return new Lookup("MATCHED", symbol, isin, securityName, underlying);
        }
        static Lookup noIsinMatch() { return new Lookup("NO_ISIN_MATCH", null, null, null, null); }
        static Lookup ambiguous() { return new Lookup("AMBIGUOUS", null, null, null, null); }
        static Lookup invalid() { return new Lookup("INVALID", null, null, null, null); }
        static Lookup unavailable() { return new Lookup("UNAVAILABLE", null, null, null, null); }
    }
}
