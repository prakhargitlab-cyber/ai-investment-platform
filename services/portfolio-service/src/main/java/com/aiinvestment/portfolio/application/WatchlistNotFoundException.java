package com.aiinvestment.portfolio.application;

public class WatchlistNotFoundException extends RuntimeException {
    public WatchlistNotFoundException() {
        super("Watchlist not found");
    }
}
