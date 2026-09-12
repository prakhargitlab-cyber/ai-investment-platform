package com.aiinvestment.portfolio.application;

/** Read-only lookup in NSE's public listed-equity security master. */
public interface NseOfficialSecurityMaster {
    Lookup lookupByIsin(String isin);
    default java.util.List<Listing> listedEquities() { throw new UnsupportedOperationException("LISTING_VIEW_UNAVAILABLE"); }

    record Lookup(String status, String symbol, String isin, String companyName, String series) {
        static Lookup matched(String symbol, String isin, String companyName, String series) {
            return new Lookup("MATCHED", symbol, isin, companyName, series);
        }
        static Lookup noIsinMatch() { return new Lookup("NO_ISIN_MATCH", null, null, null, null); }
        static Lookup ambiguous() { return new Lookup("AMBIGUOUS", null, null, null, null); }
        static Lookup unavailable() { return new Lookup("UNAVAILABLE", null, null, null, null); }
    }
    record Listing(String symbol, String isin, String companyName, String series) {}
}
