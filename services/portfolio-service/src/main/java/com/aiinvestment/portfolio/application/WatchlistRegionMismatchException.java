package com.aiinvestment.portfolio.application;

public class WatchlistRegionMismatchException extends RuntimeException {
    public WatchlistRegionMismatchException() {
        super("WATCHLIST_REGION_MISMATCH");
    }
}
