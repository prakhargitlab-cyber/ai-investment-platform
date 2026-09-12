package com.aiinvestment.broker.provider.icici;

import java.util.List;

interface BreezeReadClient {
    List<BreezeDematHolding> fetchDematHoldings(BreezeSignedHeaders headers);

    BreezeFunds fetchFunds(BreezeSignedHeaders headers);
}
