package com.aiinvestment.portfolio.application;

/** Read-only exact-ISIN lookup in NSE's public ETF securities available-for-trading list. */
public interface NseOfficialEtfSecurityList {
    Lookup lookupByIsin(String isin);

    /**
     * Full ETF universe from the same authoritative source as {@link #lookupByIsin}, fetched ONCE
     * rather than once per instrument -- for bounded bulk passes (e.g. historical asset-type
     * reclassification) that need to check many instruments against this list without issuing one
     * remote call per instrument. Unlike {@link #lookupByIsin}, which fails closed to a status value,
     * an unavailable or invalid source is signaled by throwing (see
     * OfficialNseEtfSecurityListClient#listAll), so a caller can distinguish "fetched, genuinely no
     * rows" from "could not fetch" -- important for callers that must not treat a failed fetch as
     * authoritative absence of a match. Default implementation throws for any caller (e.g. a test
     * double) that has not implemented bulk listing, mirroring NseOfficialSecurityMaster#listedEquities.
     */
    default java.util.List<Listing> listAll() { throw new UnsupportedOperationException("LISTING_VIEW_UNAVAILABLE"); }

    record Listing(String symbol, String isin, String securityName, String underlying) {}

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
