package com.aiinvestment.shared.domain.broker;

import com.aiinvestment.shared.domain.Instrument;

public interface BrokerInstrumentNormalizer {
    BrokerType supportedBroker();

    Instrument normalize(BrokerInstrumentIdentity identity);
}
