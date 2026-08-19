package com.aiinvestment.broker.api;

import com.aiinvestment.broker.application.BrokerConnectionService;
import com.aiinvestment.shared.domain.broker.BrokerType;
import com.aiinvestment.shared.domain.broker.BrokerProvider;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.*;

import java.util.Comparator;
import java.util.List;
import java.util.UUID;

@RestController
public class BrokerController {
    private final BrokerConnectionService service;

    public BrokerController(BrokerConnectionService service) {
        this.service = service;
    }

    @GetMapping("/api/v1/brokers")
    public List<BrokerProviderResponse> brokers() {
        return service.providers().stream()
                .sorted(Comparator.comparing(BrokerProvider::supportedBroker))
                .map(BrokerProviderResponse::from)
                .toList();
    }

    @GetMapping("/api/v1/broker-connections")
    public List<BrokerConnectionResponse> connections() {
        return service.listConnections().stream()
                .map(connection -> BrokerConnectionResponse.from(connection, providerFor(connection.brokerType().name())))
                .toList();
    }

    @GetMapping("/api/v1/broker-connections/{id}")
    public BrokerConnectionResponse connection(@PathVariable("id") UUID id) {
        var connection = service.getConnection(id);
        return BrokerConnectionResponse.from(connection, providerFor(connection.brokerType().name()));
    }

    @PostMapping("/api/v1/broker-connections/mock")
    @ResponseStatus(HttpStatus.CREATED)
    public BrokerConnectionResponse connectMock() {
        var connection = service.connectMock();
        return BrokerConnectionResponse.from(connection, providerFor(connection.brokerType().name()));
    }

    @PostMapping("/api/v1/broker-connections/{broker}/connect")
    @ResponseStatus(HttpStatus.CREATED)
    public BrokerConnectionResponse connect(@PathVariable("broker") String broker) {
        var connection = service.initiateConnection(BrokerType.valueOf(broker.toUpperCase().replace("-", "_")));
        return BrokerConnectionResponse.from(connection, providerFor(connection.brokerType().name()));
    }

    @GetMapping("/api/v1/broker-connections/{id}/status")
    public BrokerConnectionResponse status(@PathVariable("id") UUID id) {
        var connection = service.status(id);
        return BrokerConnectionResponse.from(connection, providerFor(connection.brokerType().name()));
    }

    @PostMapping("/api/v1/broker-connections/{id}/refresh")
    public BrokerConnectionResponse refresh(@PathVariable("id") UUID id) {
        var connection = service.refresh(id);
        return BrokerConnectionResponse.from(connection, providerFor(connection.brokerType().name()));
    }

    @GetMapping("/api/v1/broker-connections/{broker}/callback")
    public void callback(@PathVariable("broker") String broker) {
        throw new IllegalStateException("Callback flow is unavailable until official " + broker + " authentication documentation is configured.");
    }

    @PostMapping("/api/v1/broker-connections/{id}/sync")
    public BrokerConnectionResponse sync(@PathVariable("id") UUID id) {
        var connection = service.sync(id);
        return BrokerConnectionResponse.from(connection, providerFor(connection.brokerType().name()));
    }

    @DeleteMapping("/api/v1/broker-connections/{id}")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    public void disconnect(@PathVariable("id") UUID id) {
        service.disconnect(id);
    }

    private BrokerProvider providerFor(String brokerType) {
        return service.providers().stream()
                .filter(provider -> provider.supportedBroker().name().equals(brokerType))
                .findFirst()
                .orElseThrow(() -> new IllegalStateException("No provider for " + brokerType));
    }
}
