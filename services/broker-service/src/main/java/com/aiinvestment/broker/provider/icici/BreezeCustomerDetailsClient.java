package com.aiinvestment.broker.provider.icici;

interface BreezeCustomerDetailsClient {
    BreezeCustomerDetails exchangeApiSession(String appKey, String apiSession);
}
