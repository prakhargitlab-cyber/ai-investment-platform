package com.aiinvestment.broker.api;

import com.aiinvestment.broker.application.BrokerConnectionService;
import com.aiinvestment.broker.application.BrokerCredentialService;
import com.aiinvestment.broker.persistence.AppUserProvisioner;
import com.aiinvestment.shared.web.auth.AuthenticatedUserResolver;
import jakarta.servlet.http.HttpServletRequest;
import com.aiinvestment.shared.domain.broker.BrokerType;
import com.aiinvestment.shared.domain.broker.BrokerProvider;
import org.springframework.http.HttpStatus;
import org.springframework.web.server.ResponseStatusException;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.web.bind.annotation.*;

import java.util.Comparator;
import java.util.List;
import java.util.UUID;

@RestController
public class BrokerController {
    private final BrokerConnectionService service;
    private final AppUserProvisioner appUserProvisioner;
    private final BrokerCredentialService credentialService;

    @Value("${broker.public-demo-enabled:false}")
    private boolean publicDemoEnabled;

    @Value("${broker.advanced-individual-credentials-enabled:false}")
    private boolean advancedIndividualCredentialsEnabled;

    public BrokerController(BrokerConnectionService service, AppUserProvisioner appUserProvisioner,
                            BrokerCredentialService credentialService) {
        this.service = service;
        this.appUserProvisioner = appUserProvisioner;
        this.credentialService = credentialService;
    }

    @PutMapping("/api/v1/broker-connections/{id}/credentials")
    public BrokerCredentialStatusResponse configureCredentials(@PathVariable("id") UUID id,
                                                               @RequestBody BrokerCredentialRequest body,
                                                               HttpServletRequest request) {
        requireAdvancedIndividualCredentials();
        var user = AuthenticatedUserResolver.require(request);
        appUserProvisioner.upsert(user);
        return credentialService.configure(user.userId(), id, body.clientKey(), body.clientSecret());
    }

    @GetMapping("/api/v1/broker-connections/{id}/credentials/status")
    public BrokerCredentialStatusResponse credentialStatus(@PathVariable("id") UUID id, HttpServletRequest request) {
        requireAdvancedIndividualCredentials();
        var user = AuthenticatedUserResolver.require(request);
        appUserProvisioner.upsert(user);
        return credentialService.status(user.userId(), id);
    }

    @GetMapping("/api/v1/brokers")
    public List<BrokerProviderResponse> brokers(HttpServletRequest request) {
        var user = AuthenticatedUserResolver.require(request);
        appUserProvisioner.upsert(user);
        var connections = service.listConnections(user.userId());
        return service.providers().stream()
                .filter(provider -> publicDemoEnabled || provider.supportedBroker() != BrokerType.MOCK)
                .sorted(Comparator.comparing(BrokerProvider::supportedBroker))
                .map(provider -> BrokerProviderResponse.from(provider, connections,
                        service.consumerAuthMode(provider.supportedBroker(), advancedIndividualCredentialsEnabled),
                        advancedIndividualCredentialsEnabled))
                .toList();
    }

    @GetMapping("/api/v1/broker-connections")
    public List<BrokerConnectionResponse> connections(HttpServletRequest request) {
        var user = AuthenticatedUserResolver.require(request);
        appUserProvisioner.upsert(user);
        return service.listConnections(user.userId()).stream()
                .map(connection -> BrokerConnectionResponse.from(connection, providerFor(connection.brokerType().name())))
                .toList();
    }

    @GetMapping("/api/v1/broker-connections/{id}")
    public BrokerConnectionResponse connection(@PathVariable("id") UUID id, HttpServletRequest request) {
        var user = AuthenticatedUserResolver.require(request);
        appUserProvisioner.upsert(user);
        var connection = service.getConnection(user.userId(), id);
        return BrokerConnectionResponse.from(connection, providerFor(connection.brokerType().name()));
    }

    @PostMapping("/api/v1/broker-connections/mock")
    @ResponseStatus(HttpStatus.CREATED)
    public BrokerConnectionResponse connectMock(HttpServletRequest request) {
        var user = AuthenticatedUserResolver.require(request);
        appUserProvisioner.upsert(user);
        var connection = service.connectMock(user.userId());
        return BrokerConnectionResponse.from(connection, providerFor(connection.brokerType().name()));
    }

    @PostMapping("/api/v1/broker-connections/{broker}/connect")
    @ResponseStatus(HttpStatus.CREATED)
    public BrokerConnectionResponse connect(@PathVariable("broker") String broker, HttpServletRequest request) {
        var user = AuthenticatedUserResolver.require(request);
        appUserProvisioner.upsert(user);
        var connection = service.initiateConnection(user.userId(), BrokerType.valueOf(broker.toUpperCase().replace("-", "_")));
        return BrokerConnectionResponse.from(connection, providerFor(connection.brokerType().name()));
    }

    @GetMapping("/api/v1/broker-connections/{id}/status")
    public BrokerConnectionResponse status(@PathVariable("id") UUID id, HttpServletRequest request) {
        var user = AuthenticatedUserResolver.require(request);
        appUserProvisioner.upsert(user);
        var connection = service.status(user.userId(), id);
        return BrokerConnectionResponse.from(connection, providerFor(connection.brokerType().name()));
    }

    @GetMapping("/api/v1/broker-connections/{id}/auth-status")
    public BrokerAuthStatusResponse authStatus(@PathVariable("id") UUID id, HttpServletRequest request) {
        var user = AuthenticatedUserResolver.require(request);
        appUserProvisioner.upsert(user);
        var connection = service.status(user.userId(), id);
        boolean authenticated = connection.status() == com.aiinvestment.shared.domain.broker.BrokerConnectionState.CONNECTED;
        return new BrokerAuthStatusResponse(connection.connectionId(), connection.status().name(), authenticated);
    }

    @GetMapping("/api/v1/broker-connections/{id}/authentication-action")
    public BrokerAuthenticationActionResponse authenticationAction(@PathVariable("id") UUID id,
                                                                   HttpServletRequest request) {
        var user = AuthenticatedUserResolver.require(request);
        appUserProvisioner.upsert(user);
        return service.authenticationAction(user.userId(), id);
    }

    @GetMapping("/api/v1/broker-connections/{id}/icici-login")
    public ICICIDirectLoginResponse iciciLogin(@PathVariable("id") UUID id, HttpServletRequest request) {
        requireAdvancedIndividualCredentials();
        var user = AuthenticatedUserResolver.require(request);
        appUserProvisioner.upsert(user);
        return service.iciciLogin(user.userId(), id);
    }

    @PostMapping("/api/v1/broker-connections/{id}/icici-session")
    public BrokerConnectionResponse attachIciciSession(@PathVariable("id") UUID id,
                                                       @RequestBody ICICIDirectSessionRequest sessionRequest,
                                                       HttpServletRequest request) {
        requireAdvancedIndividualCredentials();
        var user = AuthenticatedUserResolver.require(request);
        appUserProvisioner.upsert(user);
        var connection = service.attachIciciSession(user.userId(), id, sessionRequest.apiSession());
        return BrokerConnectionResponse.from(connection, providerFor(connection.brokerType().name()));
    }

    @PostMapping("/api/v1/broker-connections/{id}/hdfc-request-token")
    public BrokerConnectionResponse attachHdfcRequestToken(@PathVariable("id") UUID id,
                                                            @RequestBody HDFCRequestTokenRequest tokenRequest,
                                                            HttpServletRequest request) {
        requireAdvancedIndividualCredentials();
        var user = AuthenticatedUserResolver.require(request);
        appUserProvisioner.upsert(user);
        var connection = service.attachHdfcRequestToken(user.userId(), id, tokenRequest.requestToken());
        return BrokerConnectionResponse.from(connection, providerFor(connection.brokerType().name()));
    }

    @PostMapping("/api/v1/broker-connections/{id}/refresh")
    public BrokerConnectionResponse refresh(@PathVariable("id") UUID id, HttpServletRequest request) {
        var user = AuthenticatedUserResolver.require(request);
        appUserProvisioner.upsert(user);
        var connection = service.refresh(user.userId(), id);
        return BrokerConnectionResponse.from(connection, providerFor(connection.brokerType().name()));
    }

    @GetMapping("/api/v1/broker-connections/{broker}/callback")
    public void callback(@PathVariable("broker") String broker) {
        throw new IllegalStateException("Callback flow is unavailable until official " + broker + " authentication documentation is configured.");
    }

    @PostMapping("/api/v1/broker-connections/{id}/sync")
    public BrokerConnectionResponse sync(@PathVariable("id") UUID id, HttpServletRequest request) {
        var user = AuthenticatedUserResolver.require(request);
        appUserProvisioner.upsert(user);
        var connection = service.sync(user.userId(), id);
        return BrokerConnectionResponse.from(connection, providerFor(connection.brokerType().name()));
    }

    @GetMapping("/api/v1/broker-connections/{id}/snapshot")
    public BrokerSnapshotResponse snapshot(@PathVariable("id") UUID id, HttpServletRequest request) {
        var user = AuthenticatedUserResolver.require(request);
        appUserProvisioner.upsert(user);
        return service.snapshot(user.userId(), id);
    }

    @GetMapping("/api/v1/broker-connections/{id}/accounts")
    public List<BrokerSnapshotResponse.AccountResponse> inspectAccounts(@PathVariable("id") UUID id,
                                                                        HttpServletRequest request) {
        var user = AuthenticatedUserResolver.require(request);
        appUserProvisioner.upsert(user);
        return service.inspectAccounts(user.userId(), id);
    }

    @GetMapping("/api/v1/broker-connections/{id}/demat-holdings")
    public List<BrokerSnapshotResponse.PositionResponse> inspectDematHoldings(@PathVariable("id") UUID id,
                                                                              HttpServletRequest request) {
        var user = AuthenticatedUserResolver.require(request);
        appUserProvisioner.upsert(user);
        return service.inspectDematHoldings(user.userId(), id);
    }

    @GetMapping("/api/v1/broker-connections/{id}/funds")
    public List<BrokerSnapshotResponse.CashResponse> inspectFunds(@PathVariable("id") UUID id,
                                                                  HttpServletRequest request) {
        var user = AuthenticatedUserResolver.require(request);
        appUserProvisioner.upsert(user);
        return service.inspectFunds(user.userId(), id);
    }

    private void requireAdvancedIndividualCredentials() {
        if (!advancedIndividualCredentialsEnabled) {
            throw new ResponseStatusException(HttpStatus.NOT_FOUND);
        }
    }

    @GetMapping("/api/v1/broker-connectors/{id}/status")
    public BrokerConnectorResponse connectorStatus(@PathVariable("id") UUID id, HttpServletRequest request) {
        var user = AuthenticatedUserResolver.require(request);
        appUserProvisioner.upsert(user);
        return service.connectorStatus(user.userId(), id);
    }

    @GetMapping("/api/v1/broker-connectors/{id}/login")
    public BrokerConnectorLoginResponse connectorLogin(@PathVariable("id") UUID id, HttpServletRequest request) {
        var user = AuthenticatedUserResolver.require(request);
        appUserProvisioner.upsert(user);
        return service.connectorLogin(user.userId(), id);
    }

    @DeleteMapping("/api/v1/broker-connections/{id}")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    public void disconnect(@PathVariable("id") UUID id, HttpServletRequest request) {
        var user = AuthenticatedUserResolver.require(request);
        appUserProvisioner.upsert(user);
        service.disconnect(user.userId(), id);
    }

    private BrokerProvider providerFor(String brokerType) {
        return service.providers().stream()
                .filter(provider -> provider.supportedBroker().name().equals(brokerType))
                .findFirst()
                .orElseThrow(() -> new IllegalStateException("No provider for " + brokerType));
    }
}
