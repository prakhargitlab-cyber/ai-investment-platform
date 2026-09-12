package com.aiinvestment.broker.connector;

import com.aiinvestment.broker.application.BrokerConnectionNotFoundException;
import com.aiinvestment.broker.application.BrokerProviderException;
import com.aiinvestment.broker.config.IBKRProviderProperties;
import com.aiinvestment.broker.persistence.BrokerConnectorInstanceEntity;
import com.aiinvestment.broker.persistence.BrokerConnectorInstanceRepository;
import org.springframework.stereotype.Component;

import java.net.URI;
import java.net.URISyntaxException;
import java.util.UUID;

@Component
public class ConnectorRuntimeLocationResolver {
    private final BrokerConnectorInstanceRepository repository;
    private final IBKRProviderProperties properties;

    public ConnectorRuntimeLocationResolver(BrokerConnectorInstanceRepository repository,
                                            IBKRProviderProperties properties) {
        this.repository = repository;
        this.properties = properties;
    }

    public ConnectorRuntimeLocation resolve(UUID userId, UUID connectorId) {
        BrokerConnectorInstanceEntity entity = repository.findById(connectorId).orElse(null);
        if (entity != null && !entity.getUserId().equals(userId)) {
            throw new BrokerConnectionNotFoundException(connectorId);
        }
        if (entity != null && !isBlank(entity.getRuntimeEndpoint())) {
            return new ConnectorRuntimeLocation(connectorId, validatedEndpoint(entity.getRuntimeEndpoint()),
                    entity.getRuntimeIdentity(), entity.getRuntimeProvider());
        }
        return new ConnectorRuntimeLocation(connectorId, validatedEndpoint(properties.connectorBaseUrl()), null, "STATIC_DEV");
    }

    private static URI validatedEndpoint(String value) {
        if (isBlank(value)) {
            throw BrokerProviderException.unavailable();
        }
        try {
            URI endpoint = new URI(value.trim());
            if (!endpoint.isAbsolute()
                    || !("http".equalsIgnoreCase(endpoint.getScheme()) || "https".equalsIgnoreCase(endpoint.getScheme()))
                    || endpoint.getHost() == null
                    || endpoint.getUserInfo() != null
                    || endpoint.getQuery() != null
                    || endpoint.getFragment() != null) {
                throw BrokerProviderException.unavailable();
            }
            return endpoint;
        } catch (URISyntaxException exception) {
            throw BrokerProviderException.unavailable();
        }
    }

    private static boolean isBlank(String value) {
        return value == null || value.isBlank();
    }
}
