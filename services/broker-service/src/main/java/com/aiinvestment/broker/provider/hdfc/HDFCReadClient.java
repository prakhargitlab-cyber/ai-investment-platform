package com.aiinvestment.broker.provider.hdfc;

import java.math.BigDecimal;
import java.util.List;

public interface HDFCReadClient {
    String exchangeAccessToken(String apiKey, String apiSecret, String requestToken);
    Profile profile(String apiKey, String accessToken);
    List<Holding> holdings(String apiKey, String accessToken);
    Funds funds(String apiKey, String accessToken);
    record Profile(String userId, String userName) {}
    record Holding(String securityId, String exchange, String companyName, String isin,
                   BigDecimal quantity, BigDecimal averagePrice, BigDecimal closePrice) {}
    record Funds(BigDecimal cash) {}
}
