package com.aiinvestment.broker.application;

import com.aiinvestment.broker.persistence.BrokerConnectionEntity;
import com.aiinvestment.shared.domain.broker.BrokerConnectionState;
import com.aiinvestment.shared.domain.broker.BrokerType;
import org.springframework.stereotype.Component;

import java.util.Collection;
import java.util.Comparator;
import java.util.EnumMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

@Component
public class CanonicalBrokerConnectionResolver {
    private static final Comparator<BrokerConnectionEntity> QUALITY = Comparator
            .comparingInt(CanonicalBrokerConnectionResolver::qualityTier)
            .thenComparing(BrokerConnectionEntity::getUpdatedAt,
                    Comparator.nullsFirst(Comparator.naturalOrder()))
            .thenComparing(BrokerConnectionEntity::getCreatedAt,
                    Comparator.nullsFirst(Comparator.naturalOrder()));

    public Optional<BrokerConnectionEntity> resolve(Collection<BrokerConnectionEntity> connections,
                                                     BrokerType brokerType) {
        return connections.stream()
                .filter(connection -> connection.getBrokerType() == brokerType)
                .max(QUALITY);
    }

    public List<BrokerConnectionEntity> resolveAll(Collection<BrokerConnectionEntity> connections) {
        Map<BrokerType, BrokerConnectionEntity> canonical = new EnumMap<>(BrokerType.class);
        for (BrokerConnectionEntity connection : connections) {
            canonical.merge(connection.getBrokerType(), connection,
                    (left, right) -> QUALITY.compare(left, right) >= 0 ? left : right);
        }
        return canonical.values().stream()
                .sorted(Comparator.comparing(BrokerConnectionEntity::getBrokerType))
                .toList();
    }

    private static int qualityTier(BrokerConnectionEntity connection) {
        boolean successfulHistory = connection.getLastSuccessfulSyncAt() != null;
        boolean establishedAccount = connection.getExternalAccountReference() != null
                && !connection.getExternalAccountReference().isBlank();
        if (connection.getStatus() == BrokerConnectionState.CONNECTED && successfulHistory && establishedAccount) return 5;
        if (connection.getStatus() == BrokerConnectionState.CONNECTED && successfulHistory) return 4;
        if (connection.getStatus() == BrokerConnectionState.AUTHENTICATION_REQUIRED
                && (successfulHistory || establishedAccount)) return 3;
        if (connection.getStatus() == BrokerConnectionState.CONNECTED
                || connection.getStatus() == BrokerConnectionState.AUTHENTICATION_REQUIRED) return 2;
        if (successfulHistory || establishedAccount) return 1;
        return 0;
    }
}
