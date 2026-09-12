package com.aiinvestment.broker.application;

import com.aiinvestment.broker.domain.BrokerConnection;
import com.aiinvestment.broker.audit.BrokerOperationAuditor;
import com.aiinvestment.broker.api.BrokerConnectionResponse;
import com.aiinvestment.broker.api.BrokerConnectorLoginResponse;
import com.aiinvestment.broker.api.BrokerConnectorResponse;
import com.aiinvestment.broker.api.BrokerSnapshotResponse;
import com.aiinvestment.broker.api.BrokerAuthenticationActionResponse;
import com.aiinvestment.broker.api.BrokerProviderResponse;
import com.aiinvestment.broker.connector.BrokerConnector;
import com.aiinvestment.broker.connector.BrokerConnectorState;
import com.aiinvestment.broker.connector.ConnectorRuntimeMode;
import com.aiinvestment.broker.config.IBKRProviderProperties;
import com.aiinvestment.broker.provider.icici.ICICIDirectBrokerProvider;
import com.aiinvestment.broker.provider.hdfc.HDFCSecuritiesBrokerProvider;
import com.aiinvestment.broker.persistence.*;
import com.aiinvestment.broker.partner.PartnerAuthProvider;
import com.aiinvestment.broker.resilience.ProviderCircuitBreaker;
import com.aiinvestment.broker.resilience.ProviderRateLimiter;
import com.aiinvestment.broker.runtime.IBKRRuntimeLifecycleException;
import com.aiinvestment.broker.runtime.IBKRRuntimeLifecycleService;
import com.aiinvestment.broker.runtime.IBKRRuntimeProperties;
import com.aiinvestment.shared.domain.broker.*;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Instant;
import java.time.Duration;
import java.util.Comparator;
import java.util.List;
import java.util.UUID;
import java.util.Optional;
import java.util.stream.Collectors;

@Service
public class BrokerConnectionService {
    private static final Logger logger = LoggerFactory.getLogger(BrokerConnectionService.class);
    private static final Duration IBKR_CONNECTOR_STARTUP_POLL_INTERVAL = Duration.ofMillis(250);
    private final BrokerConnectionRepository repository;
    private final List<BrokerProvider> providers;
    private final ProviderRateLimiter rateLimiter;
    private final ProviderCircuitBreaker circuitBreaker;
    private final BrokerOperationAuditor auditor;
    private final BrokerConnectorInstanceRepository connectorRepository;
    private final List<BrokerConnector> connectors;
    private final IBKRProviderProperties ibkrProperties;
    private final List<PartnerAuthProvider> partnerAuthProviders;
    private final CanonicalBrokerConnectionResolver canonicalConnectionResolver;
    private final IBKRRuntimeLifecycleService ibkrRuntimeLifecycleService;
    private final IBKRRuntimeProperties ibkrRuntimeProperties;

    @Autowired
    public BrokerConnectionService(BrokerConnectionRepository repository, List<BrokerProvider> providers,
                                   ProviderRateLimiter rateLimiter, ProviderCircuitBreaker circuitBreaker,
                                   BrokerOperationAuditor auditor,
                                   BrokerConnectorInstanceRepository connectorRepository,
                                   List<BrokerConnector> connectors,
                                   IBKRProviderProperties ibkrProperties,
                                   List<PartnerAuthProvider> partnerAuthProviders,
                                   CanonicalBrokerConnectionResolver canonicalConnectionResolver,
                                   IBKRRuntimeLifecycleService ibkrRuntimeLifecycleService,
                                   IBKRRuntimeProperties ibkrRuntimeProperties) {
        this.repository = repository;
        this.providers = providers;
        this.rateLimiter = rateLimiter;
        this.circuitBreaker = circuitBreaker;
        this.auditor = auditor;
        this.connectorRepository = connectorRepository;
        this.connectors = connectors;
        this.ibkrProperties = ibkrProperties;
        this.partnerAuthProviders = partnerAuthProviders;
        this.canonicalConnectionResolver = canonicalConnectionResolver;
        this.ibkrRuntimeLifecycleService = ibkrRuntimeLifecycleService;
        this.ibkrRuntimeProperties = ibkrRuntimeProperties;
    }

    public BrokerConnectionService(BrokerConnectionRepository repository, List<BrokerProvider> providers,
                                   ProviderRateLimiter rateLimiter, ProviderCircuitBreaker circuitBreaker,
                                   BrokerOperationAuditor auditor,
                                   BrokerConnectorInstanceRepository connectorRepository,
                                   List<BrokerConnector> connectors,
                                   IBKRProviderProperties ibkrProperties,
                                   List<PartnerAuthProvider> partnerAuthProviders,
                                   CanonicalBrokerConnectionResolver canonicalConnectionResolver,
                                   IBKRRuntimeLifecycleService ibkrRuntimeLifecycleService) {
        this(repository, providers, rateLimiter, circuitBreaker, auditor, connectorRepository, connectors,
                ibkrProperties, partnerAuthProviders, canonicalConnectionResolver, ibkrRuntimeLifecycleService, null);
    }

    // Retained for focused unit tests and non-Spring callers that exercise only LOCAL_AGENT behavior.
    public BrokerConnectionService(BrokerConnectionRepository repository, List<BrokerProvider> providers,
                                   ProviderRateLimiter rateLimiter, ProviderCircuitBreaker circuitBreaker,
                                   BrokerOperationAuditor auditor,
                                   BrokerConnectorInstanceRepository connectorRepository,
                                   List<BrokerConnector> connectors,
                                   IBKRProviderProperties ibkrProperties,
                                   List<PartnerAuthProvider> partnerAuthProviders,
                                   CanonicalBrokerConnectionResolver canonicalConnectionResolver) {
        this(repository, providers, rateLimiter, circuitBreaker, auditor, connectorRepository, connectors,
                ibkrProperties, partnerAuthProviders, canonicalConnectionResolver, null, null);
    }

    @Transactional(readOnly = true)
    public List<BrokerConnection> listConnections(UUID userId) {
        return canonicalConnectionResolver.resolveAll(repository.findByUserId(userId)).stream()
                .map(BrokerConnectionMapper::toDomain).toList();
    }

    @Transactional(readOnly = true)
    public BrokerConnection getConnection(UUID userId, UUID connectionId) {
        return repository.findByConnectionIdAndUserId(connectionId, userId).map(BrokerConnectionMapper::toDomain)
                .orElseThrow(() -> new BrokerConnectionNotFoundException(connectionId));
    }

    @Transactional
    public BrokerConnection connectMock(UUID userId) {
        Instant now = Instant.now();
        BrokerConnectionEntity entity = new BrokerConnectionEntity(UUID.randomUUID(), userId, BrokerType.MOCK,
                "mock-demo", "Demo Broker", BrokerConnectionState.CONNECTED, now, null, null, null, now, now);
        return BrokerConnectionMapper.toDomain(repository.save(entity));
    }

    @Transactional
    public BrokerConnection initiateConnection(UUID userId, BrokerType brokerType) {
        Instant startedAt = Instant.now();
        if (brokerType == BrokerType.MOCK) {
            BrokerConnection connection = connectMock(userId);
            auditor.record(brokerType, "connect", startedAt, true, "LOCAL");
            return connection;
        }
        BrokerConnectionEntity existing = canonicalConnectionResolver
                .resolve(repository.findByUserId(userId), brokerType).orElse(null);
        if (existing != null) {
            return BrokerConnectionMapper.toDomain(existing);
        }
        if (brokerType == BrokerType.IBKR) {
            BrokerConnector connector = connectorFor(BrokerType.IBKR);
            BrokerConnectorInstanceEntity connectorEntity = createConnectorInstance(userId, connector);
            var connectorStatus = connector.status(userId, connectorEntity.getConnectorId());
            updateConnectorStatus(connectorEntity, connectorStatus.runtimeStatus(), connectorStatus.authStatus());
            String loginUrl = connectorStatus.loginUrl() == null
                    ? connectorEntity.getLoginUrl()
                    : connectorStatus.loginUrl();
            if (loginUrl == null) {
                loginUrl = connector.loginUrl(userId, connectorEntity.getConnectorId());
            }
            connectorEntity.setLoginUrl(loginUrl);
            if (!connectorStatus.authenticated()) {
                BrokerConnectionEntity entity = new BrokerConnectionEntity(UUID.randomUUID(), userId, BrokerType.IBKR,
                        connectorEntity.getConnectorId(), null, "Interactive Brokers", BrokerConnectionState.AUTHENTICATION_REQUIRED,
                        null, BrokerProviderStatus.AUTHENTICATION_REQUIRED.name(), "UNAVAILABLE", null,
                        capabilities(providerFor(BrokerType.IBKR)), null, null, null, connectorStatus.code(), startedAt, startedAt);
                auditor.record(brokerType, "connect", startedAt, false, connectorStatus.code());
                return BrokerConnectionMapper.toDomain(repository.save(entity));
            }
            BrokerAccount account = connector.fetchAccounts(userId, connectorEntity.getConnectorId()).stream()
                    .min(Comparator.comparing(BrokerAccount::brokerAccountId))
                    .orElseThrow(BrokerProviderException::accountNotFound);
            BrokerConnectionEntity entity = new BrokerConnectionEntity(UUID.randomUUID(), userId, BrokerType.IBKR,
                    connectorEntity.getConnectorId(), account.externalAccountReference(), account.displayName(), BrokerConnectionState.CONNECTED,
                    account.baseCurrency(), BrokerProviderStatus.CONNECTED.name(), "REAL_BROKER", "client-portal-gateway",
                    capabilities(providerFor(BrokerType.IBKR)), startedAt, null, null, null, startedAt, startedAt);
            auditor.record(brokerType, "connect", startedAt, true, "CONNECTED");
            return BrokerConnectionMapper.toDomain(repository.save(entity));
        }
        if (brokerType == BrokerType.ICICI_DIRECT || brokerType == BrokerType.HDFC_SECURITIES) {
            BrokerConnectionStatus providerStatus = providerFor(brokerType).connectionStatus(userId,
                    new UUID(0L, 0L));
            if (providerStatus.providerStatus() != BrokerProviderStatus.AUTHENTICATION_REQUIRED) {
                auditor.record(brokerType, "connect", startedAt, false, providerStatus.code());
                throw new IllegalStateException(providerStatus.code() + ": " + providerStatus.message());
            }
            UUID connectionId = UUID.randomUUID();
            String displayName = brokerType == BrokerType.ICICI_DIRECT ? "ICICI Direct" : "HDFC Securities";
            BrokerConnectionEntity entity = new BrokerConnectionEntity(connectionId, userId, brokerType,
                    null, displayName, BrokerConnectionState.AUTHENTICATION_REQUIRED,
                    null, BrokerProviderStatus.AUTHENTICATION_REQUIRED.name(), "UNAVAILABLE", null,
                    capabilities(providerFor(brokerType)), null, null, null, providerStatus.code(), startedAt, startedAt);
            auditor.record(brokerType, "connect", startedAt, true, "AUTHENTICATION_REQUIRED");
            return BrokerConnectionMapper.toDomain(repository.save(entity));
        }
        BrokerConnectionStatus status = providerFor(brokerType).connectionStatus();
        auditor.record(brokerType, "connect", startedAt, false, status.code());
        if (brokerType == BrokerType.IBKR && status.providerStatus() == BrokerProviderStatus.AUTHENTICATION_REQUIRED) {
            throw BrokerProviderException.authenticationRequired();
        }
        throw new IllegalStateException(status.code() + ": " + status.message());
    }

    @Transactional
    public BrokerConnection status(UUID userId, UUID connectionId) {
        BrokerConnectionEntity entity = repository.findByConnectionIdAndUserId(connectionId, userId)
                .orElseThrow(() -> new BrokerConnectionNotFoundException(connectionId));
        if (entity.getBrokerType() == BrokerType.IBKR && entity.getConnectorId() != null) {
            BrokerConnectionState brokerStateBefore = entity.getStatus();
            BrokerConnectorInstanceEntity connectorEntity = requireConnector(userId, entity);
            var status = connectorFor(BrokerType.IBKR).status(userId, connectorEntity.getConnectorId());
            updateConnectorStatus(connectorEntity, status.runtimeStatus(), status.authStatus());
            reconcileIbkrConnectionStatus(entity, status);
            logger.info("ibkr_auth_status_reconciliation connection={} connector={} runtimeExists={} "
                            + "runtimeState={} liveAuthState={} authenticated={} authenticatedAt={} "
                            + "persistedConnectorAuthState={} brokerStateBefore={} brokerStateAfter={}",
                    entity.getConnectionId(), connectorEntity.getConnectorId(),
                    status.runtimeStatus() != BrokerConnectorState.NOT_CONFIGURED,
                    status.runtimeStatus(), status.authStatus(), status.authenticated(), status.authenticatedAt(),
                    connectorEntity.getAuthStatus(), brokerStateBefore, entity.getStatus());
        } else if (entity.getBrokerType() == BrokerType.ICICI_DIRECT || entity.getBrokerType() == BrokerType.HDFC_SECURITIES) {
            reconcileProviderStatus(entity, providerFor(entity.getBrokerType())
                    .connectionStatus(userId, connectionId));
        }
        return BrokerConnectionMapper.toDomain(entity);
    }

    @Transactional
    public BrokerAuthenticationActionResponse authenticationAction(UUID userId, UUID connectionId) {
        BrokerConnectionEntity entity = repository.findByConnectionIdAndUserId(connectionId, userId)
                .orElseThrow(() -> new BrokerConnectionNotFoundException(connectionId));
        BrokerProvider provider = providerFor(entity.getBrokerType());
        if (entity.getBrokerType() == BrokerType.IBKR) {
            BrokerConnectorInstanceEntity connectorEntity = requireConnector(userId, entity);
            BrokerConnector connector = connectorFor(BrokerType.IBKR);
            boolean allocatedRuntime = false;
            try {
                ConnectorRuntimeMode runtimeMode = currentIbkrRuntimeMode();
                boolean existingRuntimeReady = !requiresKubernetesRuntimeAllocation(connectorEntity, runtimeMode)
                        && hasReadyKubernetesRuntime(connectorEntity.getConnectorId(), userId, runtimeMode);
                if (!existingRuntimeReady) {
                    allocateIbkrRuntime(connectorEntity.getConnectorId(), userId);
                    allocatedRuntime = true;
                }
                // This endpoint represents an explicit Connect/Re-authenticate action. Unlike ordinary status reads,
                // start() idempotently ensures that an ephemeral runtime exists for the persisted connector ID.
                var connectorStatus = connector.status(userId, connectorEntity.getConnectorId());

                // A Gateway that has completed authentication once cannot be reused for a later interactive
                // re-authentication.  Replace it only for this explicit user action, after a ready runtime reports
                // AUTHENTICATION_REQUIRED, so ordinary status polling and an in-progress first MFA remain untouched.
                if (requiresFreshIbkrRuntimeForAuthentication(entity, connectorEntity, connectorStatus,
                        runtimeMode, existingRuntimeReady)) {
                    ibkrRuntimeLifecycleService.stop(connectorEntity.getConnectorId(), userId);
                    allocateIbkrRuntime(connectorEntity.getConnectorId(), userId);
                    allocatedRuntime = true;
                    connectorStatus = startIbkrConnector(connector, userId, connectorEntity.getConnectorId());
                }

                // Only start the connector if it's not configured or in error state.
                // If already running, preserve the existing session to avoid regenerating login URLs mid-MFA.
                if (connectorStatus.runtimeStatus() == BrokerConnectorState.NOT_CONFIGURED 
                        || connectorStatus.runtimeStatus() == BrokerConnectorState.ERROR) {
                    startIbkrConnector(connector, userId, connectorEntity.getConnectorId());
                    connectorStatus = connector.status(userId, connectorEntity.getConnectorId());
                }
                updateConnectorStatus(connectorEntity, connectorStatus.runtimeStatus(), connectorStatus.authStatus());
                reconcileIbkrConnectionStatus(entity, connectorStatus);
                String url = null;
                if (!connectorStatus.authenticated()) {
                    // This explicit user action is the only place allowed to obtain a login URL. The connector's
                    // login operation renews an expired Gateway runtime (while leaving an in-progress MFA runtime
                    // alone) and returns the URL for that exact persisted connector ID. Status polling must never
                    // reuse a stale URL as a substitute for beginning the requested authentication attempt.
                    url = connector.loginUrl(userId, connectorEntity.getConnectorId());
                    if (url == null || url.isBlank()) {
                        throw BrokerProviderException.unavailable();
                    }
                }
                connectorEntity.setLoginUrl(url);
                return new BrokerAuthenticationActionResponse(connectionId, BrokerType.IBKR.name(),
                        entity.getStatus().name(),
                        connectorStatus.authenticated() ? "NONE" : "REDIRECT_REQUIRED", url,
                        connectorStatus.authenticated() ? "Broker connection is ready" : "Complete authentication with Interactive Brokers");
            } catch (RuntimeException exception) {
                if (allocatedRuntime) {
                    stopAllocatedIbkrRuntime(connectorEntity.getConnectorId(), userId);
                }
                connectorEntity.setRuntimeStatus(BrokerConnectorState.ERROR);
                connectorEntity.setUpdatedAt(Instant.now());
                entity.setStatus(BrokerConnectionState.ERROR);
                entity.setProviderStatus(BrokerProviderStatus.ERROR.name());
                entity.setLastErrorCode(exception instanceof BrokerProviderException providerException
                        ? providerException.code() : "BROKER_UNAVAILABLE");
                entity.setUpdatedAt(Instant.now());
                return new BrokerAuthenticationActionResponse(connectionId, BrokerType.IBKR.name(),
                        BrokerConnectionState.ERROR.name(), "UNAVAILABLE", null,
                        "Interactive Brokers authentication is temporarily unavailable. Your saved portfolio is unchanged.");
            }
        }
        if (entity.getBrokerType() == BrokerType.ICICI_DIRECT) {
            var status = provider.connectionStatus(userId, connectionId);
            reconcileProviderStatus(entity, status);
            if (status.providerStatus() != BrokerProviderStatus.CONNECTED) {
                var partner = partnerAuthProvider(BrokerType.ICICI_DIRECT);
                if (partner.isEmpty()) {
                    return new BrokerAuthenticationActionResponse(connectionId, BrokerType.ICICI_DIRECT.name(),
                            entity.getStatus().name(), "PARTNER_AUTH_UNAVAILABLE", null,
                            "ICICI Direct customer authorization requires broker partner access and is not available yet.");
                }
                var authorization = partner.get().beginAuthorization(userId, connectionId);
                return new BrokerAuthenticationActionResponse(connectionId, BrokerType.ICICI_DIRECT.name(),
                        "CALLBACK_PENDING", "REDIRECT_REQUIRED", authorization.authenticationUrl(),
                        "Complete authentication on the official ICICI Direct page.");
            }
            return new BrokerAuthenticationActionResponse(connectionId, BrokerType.ICICI_DIRECT.name(),
                    entity.getStatus().name(),
                    "NONE", null, "Broker connection is ready");
        }
        if (entity.getBrokerType() == BrokerType.HDFC_SECURITIES) {
            var status = provider.connectionStatus(userId, connectionId);
            reconcileProviderStatus(entity, status);
            if (status.providerStatus() != BrokerProviderStatus.CONNECTED) {
                var partner = partnerAuthProvider(BrokerType.HDFC_SECURITIES);
                if (partner.isEmpty()) {
                    return new BrokerAuthenticationActionResponse(connectionId, BrokerType.HDFC_SECURITIES.name(),
                            entity.getStatus().name(), "PARTNER_AUTH_UNAVAILABLE", null,
                            "HDFC Securities customer authorization requires broker partner access and is not available yet.");
                }
                var authorization = partner.get().beginAuthorization(userId, connectionId);
                return new BrokerAuthenticationActionResponse(connectionId, BrokerType.HDFC_SECURITIES.name(),
                        "CALLBACK_PENDING", "REDIRECT_REQUIRED", authorization.authenticationUrl(),
                        "Complete authentication on the official HDFC Securities page.");
            }
            return new BrokerAuthenticationActionResponse(connectionId, BrokerType.HDFC_SECURITIES.name(),
                    entity.getStatus().name(), "NONE", null, "Broker connection is ready");
        }
        return new BrokerAuthenticationActionResponse(connectionId, entity.getBrokerType().name(),
                entity.getStatus().name(), "NONE", null, "Connection is ready");
    }

    @Transactional
    public com.aiinvestment.broker.api.ICICIDirectLoginResponse iciciLogin(UUID userId, UUID connectionId) {
        BrokerConnectionEntity entity = requireIciciConnection(userId, connectionId);
        ICICIDirectBrokerProvider provider = iciciProvider();
        var login = provider.login(userId, connectionId);
        entity.setStatus(BrokerConnectionState.AUTHENTICATION_REQUIRED);
        entity.setProviderStatus(BrokerProviderStatus.AUTHENTICATION_REQUIRED.name());
        entity.setLastErrorCode("AUTHENTICATION_REQUIRED");
        entity.setUpdatedAt(Instant.now());
        return new com.aiinvestment.broker.api.ICICIDirectLoginResponse(connectionId, login.loginUrl(),
                BrokerProviderStatus.AUTHENTICATION_REQUIRED.name());
    }

    @Transactional
    public BrokerConnection attachIciciSession(UUID userId, UUID connectionId, String apiSession) {
        BrokerConnectionEntity entity = requireIciciConnection(userId, connectionId);
        BrokerConnectionStatus status = iciciProvider().attachApiSession(userId, connectionId, apiSession);
        reconcileProviderStatus(entity, status);
        entity.setSessionReference("breeze-interactive-session");
        entity.setCapabilities(capabilities(iciciProvider(), userId, connectionId));
        return BrokerConnectionMapper.toDomain(entity);
    }

    @Transactional
    public BrokerConnection attachHdfcRequestToken(UUID userId, UUID connectionId, String requestToken) {
        BrokerConnectionEntity entity = requireProviderConnection(userId, connectionId, BrokerType.HDFC_SECURITIES);
        BrokerConnectionStatus status = hdfcProvider().attachRequestToken(userId, connectionId, requestToken);
        reconcileProviderStatus(entity, status);
        entity.setSessionReference("investright-access-token");
        entity.setCapabilities(capabilities(hdfcProvider(), userId, connectionId));
        return BrokerConnectionMapper.toDomain(entity);
    }

    @Transactional
    public BrokerConnectorResponse connectorStatus(UUID userId, UUID connectorId) {
        BrokerConnectorInstanceEntity entity = connectorRepository.findByConnectorIdAndUserId(connectorId, userId)
                .orElseThrow(() -> new BrokerConnectionNotFoundException(connectorId));
        BrokerConnector connector = connectorFor(entity.getBrokerType());
        var status = connector.status(userId, connectorId);
        updateConnectorStatus(entity, status.runtimeStatus(), status.authStatus());
        return BrokerConnectorResponse.from(entity);
    }

    @Transactional
    public BrokerConnectorLoginResponse connectorLogin(UUID userId, UUID connectorId) {
        BrokerConnectorInstanceEntity entity = connectorRepository.findByConnectorIdAndUserId(connectorId, userId)
                .orElseThrow(() -> new BrokerConnectionNotFoundException(connectorId));
        BrokerConnector connector = connectorFor(entity.getBrokerType());
        var status = connector.status(userId, connectorId);
        updateConnectorStatus(entity, status.runtimeStatus(), status.authStatus());
        String loginUrl = connector.loginUrl(userId, connectorId);
        entity.setLoginUrl(loginUrl);
        return new BrokerConnectorLoginResponse(connectorId, loginUrl, entity.getAuthStatus().name());
    }

    @Transactional
    public BrokerConnection refresh(UUID userId, UUID connectionId) {
        BrokerConnectionEntity entity = repository.findByConnectionIdAndUserId(connectionId, userId)
                .orElseThrow(() -> new BrokerConnectionNotFoundException(connectionId));
        if (entity.getBrokerType() == BrokerType.IBKR) {
            BrokerConnectorInstanceEntity connectorEntity = requireConnector(userId, entity);
            var status = connectorFor(BrokerType.IBKR).status(userId, connectorEntity.getConnectorId());
            updateConnectorStatus(connectorEntity, status.runtimeStatus(), status.authStatus());
            if (!status.authenticated()) {
                entity.setStatus(BrokerConnectionState.AUTHENTICATION_REQUIRED);
                entity.setProviderStatus(BrokerProviderStatus.AUTHENTICATION_REQUIRED.name());
                entity.setLastErrorCode(status.code());
            }
        } else if (entity.getBrokerType() != BrokerType.MOCK) {
            providerFor(entity.getBrokerType()).fetchAccounts(userId, connectionId);
        }
        entity.setUpdatedAt(Instant.now());
        return BrokerConnectionMapper.toDomain(entity);
    }

    @Transactional
    public BrokerConnection sync(UUID userId, UUID connectionId) {
        Instant startedAt = Instant.now();
        BrokerConnectionEntity entity = repository.findForUpdateByConnectionIdAndUserId(connectionId, userId)
                .orElseThrow(() -> new BrokerConnectionNotFoundException(connectionId));
        Instant now = Instant.now();
        entity.setStatus(BrokerConnectionState.SYNCING);
        entity.setLastSyncAttemptAt(now);
        entity.setUpdatedAt(now);
        BrokerProvider provider = providerFor(entity.getBrokerType());
        if (!circuitBreaker.allowRequest(entity.getBrokerType())) {
            entity.setStatus(BrokerConnectionState.DEGRADED);
            entity.setLastErrorCode("CIRCUIT_OPEN");
            auditor.record(entity.getBrokerType(), "sync", startedAt, false, "CIRCUIT_OPEN");
            return BrokerConnectionMapper.toDomain(entity);
        }
        if (!rateLimiter.tryAcquire(entity.getBrokerType(), "sync")) {
            entity.setStatus(BrokerConnectionState.DEGRADED);
            entity.setLastErrorCode("RATE_LIMITED");
            auditor.record(entity.getBrokerType(), "sync", startedAt, false, "RATE_LIMITED");
            return BrokerConnectionMapper.toDomain(entity);
        }
        if (entity.getBrokerType() == BrokerType.IBKR) {
            try {
                BrokerConnectorInstanceEntity connectorEntity = requireConnector(userId, entity);
                BrokerConnector connector = connectorFor(BrokerType.IBKR);
                var connectorStatus = connector.status(userId, connectorEntity.getConnectorId());
                updateConnectorStatus(connectorEntity, connectorStatus.runtimeStatus(), connectorStatus.authStatus());
                if (!connectorStatus.authenticated()) {
                    entity.setStatus(BrokerConnectionState.AUTHENTICATION_REQUIRED);
                    entity.setProviderStatus(BrokerProviderStatus.AUTHENTICATION_REQUIRED.name());
                    entity.setLastErrorCode(connectorStatus.code());
                    throw BrokerProviderException.sessionExpired();
                }
                List<BrokerAccount> accounts = connector.fetchAccounts(userId, connectorEntity.getConnectorId());
                BrokerAccount account = accounts.stream()
                        .filter(candidate -> candidate.externalAccountReference().equals(entity.getExternalAccountReference()))
                        .findFirst()
                        .orElse(accounts.get(0));
                connector.fetchPositions(userId, connectorEntity.getConnectorId(), account);
                connector.fetchCashBalances(userId, connectorEntity.getConnectorId(), account);
                entity.setStatus(BrokerConnectionState.CONNECTED);
                entity.setExternalAccountReference(account.externalAccountReference());
                entity.setDisplayName(account.displayName());
                entity.setAccountCurrency(account.baseCurrency());
                entity.setProviderStatus(BrokerProviderStatus.CONNECTED.name());
                entity.setDataFreshness("REAL_BROKER");
                entity.setSessionReference("client-portal-gateway");
                entity.setCapabilities(capabilities(provider));
                entity.setLastSuccessfulSyncAt(now);
                entity.setLastErrorCode(null);
                auditor.record(entity.getBrokerType(), "sync", startedAt, true, "OK");
                return BrokerConnectionMapper.toDomain(entity);
            } catch (BrokerProviderException exception) {
                if ("BROKER_SESSION_EXPIRED".equals(exception.code())) {
                    entity.setStatus(BrokerConnectionState.AUTHENTICATION_REQUIRED);
                    entity.setProviderStatus(BrokerProviderStatus.AUTHENTICATION_REQUIRED.name());
                } else {
                    entity.setStatus(BrokerConnectionState.ERROR);
                    entity.setProviderStatus(BrokerProviderStatus.ERROR.name());
                }
                entity.setLastErrorCode(exception.code());
                auditor.record(entity.getBrokerType(), "sync", startedAt, false, exception.code());
                if ("BROKER_SESSION_EXPIRED".equals(exception.code())) {
                    return BrokerConnectionMapper.toDomain(entity);
                }
                throw exception;
            }
        }
        if (provider.connectionStatus(userId, connectionId).state() == BrokerConnectionState.DISCONNECTED && entity.getBrokerType() != BrokerType.MOCK) {
            entity.setStatus(BrokerConnectionState.ERROR);
            entity.setLastErrorCode(provider.connectionStatus().code());
            circuitBreaker.recordFailure(entity.getBrokerType());
            auditor.record(entity.getBrokerType(), "sync", startedAt, false, provider.connectionStatus().code());
            return BrokerConnectionMapper.toDomain(entity);
        }
        entity.setStatus(BrokerConnectionState.CONNECTED);
        entity.setLastSuccessfulSyncAt(now);
        entity.setLastErrorCode(null);
        circuitBreaker.recordSuccess(entity.getBrokerType());
        auditor.record(entity.getBrokerType(), "sync", startedAt, true, "OK");
        return BrokerConnectionMapper.toDomain(entity);
    }

    @Transactional(readOnly = true)
    public BrokerSnapshotResponse snapshot(UUID userId, UUID connectionId) {
        BrokerConnectionEntity entity = repository.findByConnectionIdAndUserId(connectionId, userId)
                .orElseThrow(() -> new BrokerConnectionNotFoundException(connectionId));
        BrokerProvider provider = providerFor(entity.getBrokerType());
        List<BrokerAccount> accounts;
        List<BrokerPosition> positions;
        List<BrokerCashBalance> cashBalances;
        if (entity.getBrokerType() == BrokerType.IBKR) {
            BrokerConnectorInstanceEntity connectorEntity = requireConnector(userId, entity);
            BrokerConnector connector = connectorFor(BrokerType.IBKR);
            accounts = connector.fetchAccounts(userId, connectorEntity.getConnectorId());
            if (entity.getExternalAccountReference() != null) {
                accounts = accounts.stream()
                        .filter(account -> entity.getExternalAccountReference().equals(account.externalAccountReference()))
                        .toList();
            }
            if (accounts.isEmpty()) {
                throw BrokerProviderException.accountNotFound();
            }
            positions = accounts.stream()
                    .flatMap(account -> connector.fetchPositions(userId, connectorEntity.getConnectorId(), account).stream())
                    .toList();
            cashBalances = accounts.stream()
                    .flatMap(account -> connector.fetchCashBalances(userId, connectorEntity.getConnectorId(), account).stream())
                    .toList();
        } else {
            accounts = provider.fetchAccounts(userId, connectionId);
            accounts = accounts.stream()
                    .toList();
            if (accounts.isEmpty()) {
                throw BrokerProviderException.accountNotFound();
            }
            positions = accounts.stream().flatMap(account -> provider.fetchPositions(userId, connectionId, account).stream()).toList();
            cashBalances = accounts.stream().flatMap(account -> provider.fetchCashBalances(userId, connectionId, account).stream()).toList();
        }
        String freshness = entity.getBrokerType() == BrokerType.MOCK ? "DEMO" : "REAL_BROKER";
        return new BrokerSnapshotResponse(
                BrokerConnectionResponse.from(BrokerConnectionMapper.toDomain(entity), provider),
                accounts.stream().map(BrokerSnapshotResponse.AccountResponse::from).toList(),
                positions.stream().map(position -> BrokerSnapshotResponse.PositionResponse.from(position, freshness)).toList(),
                cashBalances.stream().map(BrokerSnapshotResponse.CashResponse::from).toList()
        );
    }

    @Transactional(readOnly = true)
    public List<BrokerSnapshotResponse.AccountResponse> inspectAccounts(UUID userId, UUID connectionId) {
        BrokerProvider provider = requireIciciReadProvider(userId, connectionId);
        return provider.fetchAccounts(userId, connectionId).stream()
                .map(BrokerSnapshotResponse.AccountResponse::from)
                .toList();
    }

    @Transactional(readOnly = true)
    public List<BrokerSnapshotResponse.PositionResponse> inspectDematHoldings(UUID userId, UUID connectionId) {
        BrokerProvider provider = requireIciciReadProvider(userId, connectionId);
        return provider.fetchAccounts(userId, connectionId).stream()
                .flatMap(account -> provider.fetchPositions(userId, connectionId, account).stream())
                .map(position -> BrokerSnapshotResponse.PositionResponse.from(position, "REAL_BROKER"))
                .toList();
    }

    @Transactional(readOnly = true)
    public List<BrokerSnapshotResponse.CashResponse> inspectFunds(UUID userId, UUID connectionId) {
        BrokerProvider provider = requireIciciReadProvider(userId, connectionId);
        return provider.fetchAccounts(userId, connectionId).stream()
                .flatMap(account -> provider.fetchCashBalances(userId, connectionId, account).stream())
                .map(BrokerSnapshotResponse.CashResponse::from)
                .toList();
    }

    @Transactional
    public void disconnect(UUID userId, UUID connectionId) {
        BrokerConnectionEntity entity = repository.findByConnectionIdAndUserId(connectionId, userId)
                .orElseThrow(() -> new BrokerConnectionNotFoundException(connectionId));
        if (entity.getBrokerType() == BrokerType.IBKR && entity.getConnectorId() != null
                && ibkrRuntimeLifecycleService != null && currentIbkrRuntimeMode() == ConnectorRuntimeMode.KUBERNETES) {
            BrokerConnectorInstanceEntity connectorEntity = requireConnector(userId, entity);
            // An IBKR runtime represents an active connection.  Removing it on
            // disconnect guarantees that a later Connect starts a fresh Gateway.
            ibkrRuntimeLifecycleService.stop(connectorEntity.getConnectorId(), userId);
            connectorEntity.setRuntimeStatus(BrokerConnectorState.STOPPED);
            connectorEntity.setAuthStatus(BrokerConnectorState.STOPPED);
            connectorEntity.setLoginUrl(null);
        }
        providerFor(entity.getBrokerType()).disconnect(userId, connectionId);
        entity.setStatus(BrokerConnectionState.DISCONNECTED);
        entity.setProviderStatus(BrokerProviderStatus.UNAVAILABLE.name());
        entity.setLastErrorCode(null);
        entity.setUpdatedAt(Instant.now());
    }

    public List<BrokerProvider> providers() {
        return providers;
    }

    public String consumerAuthMode(BrokerType brokerType) {
        return consumerAuthMode(brokerType, false);
    }

    public String consumerAuthMode(BrokerType brokerType, boolean advancedIndividualMode) {
        if (brokerType == BrokerType.IBKR) return "BROKER_REDIRECT";
        if (brokerType == BrokerType.ICICI_DIRECT || brokerType == BrokerType.HDFC_SECURITIES) {
            if (partnerAuthProvider(brokerType).isPresent()) return "PARTNER_OAUTH";
            return advancedIndividualMode ? "INDIVIDUAL_API_CREDENTIALS" : "PARTNER_UNAVAILABLE";
        }
        return "NONE";
    }

    private BrokerProvider providerFor(BrokerType brokerType) {
        return providers.stream().filter(provider -> provider.supportedBroker() == brokerType).findFirst()
                .orElseThrow(() -> new IllegalStateException("No provider for " + brokerType));
    }

    private ICICIDirectBrokerProvider iciciProvider() {
        return (ICICIDirectBrokerProvider) providerFor(BrokerType.ICICI_DIRECT);
    }

    private HDFCSecuritiesBrokerProvider hdfcProvider() {
        return (HDFCSecuritiesBrokerProvider) providerFor(BrokerType.HDFC_SECURITIES);
    }

    private Optional<PartnerAuthProvider> partnerAuthProvider(BrokerType brokerType) {
        return partnerAuthProviders.stream().filter(provider -> provider.brokerType() == brokerType).findFirst();
    }

    private BrokerConnectionEntity requireProviderConnection(UUID userId, UUID connectionId, BrokerType type) {
        BrokerConnectionEntity entity = repository.findByConnectionIdAndUserId(connectionId, userId)
                .orElseThrow(() -> new BrokerConnectionNotFoundException(connectionId));
        if (entity.getBrokerType() != type) throw new IllegalArgumentException("Connection provider does not match");
        return entity;
    }

    private BrokerProvider requireIciciReadProvider(UUID userId, UUID connectionId) {
        requireIciciConnection(userId, connectionId);
        return providerFor(BrokerType.ICICI_DIRECT);
    }

    private BrokerConnectionEntity requireIciciConnection(UUID userId, UUID connectionId) {
        BrokerConnectionEntity entity = repository.findByConnectionIdAndUserId(connectionId, userId)
                .orElseThrow(() -> new BrokerConnectionNotFoundException(connectionId));
        if (entity.getBrokerType() != BrokerType.ICICI_DIRECT) {
            throw new IllegalArgumentException("Connection is not an ICICI Direct connection");
        }
        return entity;
    }

    private BrokerConnector connectorFor(BrokerType brokerType) {
        return connectors.stream().filter(connector -> connector.brokerType() == brokerType).findFirst()
                .orElseThrow(() -> new IllegalStateException("No connector for " + brokerType));
    }

    private BrokerConnectorInstanceEntity createConnectorInstance(UUID userId, BrokerConnector connector) {
        Instant now = Instant.now();
        ConnectorRuntimeMode runtimeMode = connector.runtimeMode();
        UUID connectorId = UUID.randomUUID();
        BrokerConnectorInstanceEntity entity = new BrokerConnectorInstanceEntity(connectorId, userId,
                connector.brokerType(), runtimeMode, BrokerConnectorState.STARTING,
                BrokerConnectorState.AUTHENTICATION_REQUIRED, null,
                ibkrProperties.connectorIdleTimeoutSeconds(), ibkrProperties.connectorSessionTimeoutSeconds(), now, now);
        BrokerConnectorInstanceEntity saved = connectorRepository.save(entity);
        boolean dedicatedKubernetesRuntime = runtimeMode == ConnectorRuntimeMode.KUBERNETES;
        if (dedicatedKubernetesRuntime) {
            allocateIbkrRuntime(connectorId, userId);
        }
        com.aiinvestment.broker.connector.BrokerConnectorStatus status;
        try {
            status = startIbkrConnector(connector, userId, connectorId);
        } catch (RuntimeException startFailure) {
            if (dedicatedKubernetesRuntime) {
                stopAllocatedIbkrRuntime(connectorId, userId);
            }
            throw startFailure;
        }
        updateConnectorStatus(saved, status.runtimeStatus(), status.authStatus());
        saved.setLoginUrl(status.loginUrl());
        return saved;
    }

    private void allocateIbkrRuntime(UUID connectorId, UUID userId) {
        if (ibkrRuntimeLifecycleService == null) {
            throw BrokerProviderException.unavailable();
        }
        try {
            ibkrRuntimeLifecycleService.allocate(connectorId, userId);
        } catch (IBKRRuntimeLifecycleException exception) {
            throw new BrokerProviderException("IBKR_RUNTIME_ALLOCATION_FAILED", org.springframework.http.HttpStatus.BAD_GATEWAY,
                    "Interactive Brokers runtime is temporarily unavailable.");
        }
    }

    /**
     * Kubernetes resource readiness precedes the connector HTTP server becoming reachable.  Keep this
     * initialization retry within the same configured runtime startup budget used by allocation.
     */
    private com.aiinvestment.broker.connector.BrokerConnectorStatus startIbkrConnector(BrokerConnector connector,
                                                                                          UUID userId, UUID connectorId) {
        if (currentIbkrRuntimeMode() != ConnectorRuntimeMode.KUBERNETES || ibkrRuntimeProperties == null) {
            return connector.start(userId, connectorId);
        }
        Instant deadline = Instant.now().plusSeconds(Math.max(0, ibkrRuntimeProperties.startupTimeoutSeconds()));
        int attempts = 0;
        while (true) {
            try {
                return connector.start(userId, connectorId);
            } catch (BrokerProviderException exception) {
                if (!"IBKR_CONNECTOR_STARTING".equals(exception.code()) || !Instant.now().isBefore(deadline)) {
                    throw exception;
                }
                attempts++;
                logger.info("ibkr_connector_startup_wait connector={} attempts={}", connectorId, attempts);
                try {
                    Thread.sleep(IBKR_CONNECTOR_STARTUP_POLL_INTERVAL.toMillis());
                } catch (InterruptedException interrupted) {
                    Thread.currentThread().interrupt();
                    throw BrokerProviderException.unavailable();
                }
            }
        }
    }

    private void stopAllocatedIbkrRuntime(UUID connectorId, UUID userId) {
        try {
            ibkrRuntimeLifecycleService.stop(connectorId, userId);
        } catch (RuntimeException cleanupFailure) {
            logger.warn("ibkr_runtime_start_cleanup_failed connector={}", connectorId);
        }
    }

    private ConnectorRuntimeMode currentIbkrRuntimeMode() {
        return ibkrRuntimeProperties != null && ibkrRuntimeProperties.kubernetes()
                ? ConnectorRuntimeMode.KUBERNETES : ConnectorRuntimeMode.LOCAL_AGENT;
    }

    private static boolean requiresKubernetesRuntimeAllocation(BrokerConnectorInstanceEntity connector,
                                                               ConnectorRuntimeMode configuredRuntimeMode) {
        return configuredRuntimeMode == ConnectorRuntimeMode.KUBERNETES
                && (isBlank(connector.getRuntimeEndpoint())
                || isBlank(connector.getRuntimeIdentity())
                || !"KUBERNETES".equals(connector.getRuntimeProvider())
                || connector.getRuntimeCreatedAt() == null
                || connector.getRuntimeStoppedAt() != null);
    }

    private boolean hasReadyKubernetesRuntime(UUID connectorId, UUID userId, ConnectorRuntimeMode configuredRuntimeMode) {
        if (configuredRuntimeMode != ConnectorRuntimeMode.KUBERNETES) {
            return true;
        }
        return ibkrRuntimeLifecycleService != null
                && ibkrRuntimeLifecycleService.isReady(connectorId, userId);
    }

    private static boolean requiresFreshIbkrRuntimeForAuthentication(BrokerConnectionEntity connection,
                                                                      BrokerConnectorInstanceEntity connector,
                                                                      com.aiinvestment.broker.connector.BrokerConnectorStatus status,
                                                                      ConnectorRuntimeMode runtimeMode,
                                                                      boolean existingRuntimeReady) {
        boolean previouslyAuthenticated = connection.getConnectedAt() != null || connector.getLastAuthenticatedAt() != null;
        return runtimeMode == ConnectorRuntimeMode.KUBERNETES
                && existingRuntimeReady
                && previouslyAuthenticated
                && status.authStatus() == BrokerConnectorState.AUTHENTICATION_REQUIRED;
    }

    private static boolean isBlank(String value) {
        return value == null || value.isBlank();
    }

    private BrokerConnectorInstanceEntity requireConnector(UUID userId, BrokerConnectionEntity connection) {
        if (connection.getConnectorId() == null) {
            throw BrokerProviderException.authenticationRequired();
        }
        return connectorRepository.findByConnectorIdAndUserId(connection.getConnectorId(), userId)
                .orElseThrow(() -> new BrokerConnectionNotFoundException(connection.getConnectionId()));
    }

    private static void updateConnectorStatus(BrokerConnectorInstanceEntity entity,
                                              BrokerConnectorState runtimeStatus,
                                              BrokerConnectorState authStatus) {
        Instant now = Instant.now();
        entity.setRuntimeStatus(runtimeStatus);
        entity.setAuthStatus(authStatus);
        entity.setLastHeartbeatAt(now);
        if (authStatus == BrokerConnectorState.CONNECTED) {
            entity.setLastAuthenticatedAt(now);
        }
        entity.setUpdatedAt(now);
    }

    private static void reconcileIbkrConnectionStatus(BrokerConnectionEntity entity,
                                                      com.aiinvestment.broker.connector.BrokerConnectorStatus connectorStatus) {
        if (connectorStatus.authenticated()) {
            entity.setStatus(BrokerConnectionState.CONNECTED);
            entity.setProviderStatus(BrokerProviderStatus.CONNECTED.name());
            entity.setLastErrorCode(null);
        } else if (connectorStatus.authStatus() == BrokerConnectorState.AUTHENTICATION_REQUIRED
                || connectorStatus.authStatus() == BrokerConnectorState.SESSION_EXPIRED
                || connectorStatus.runtimeStatus() == BrokerConnectorState.AUTHENTICATION_REQUIRED
                || connectorStatus.runtimeStatus() == BrokerConnectorState.SESSION_EXPIRED) {
            entity.setStatus(BrokerConnectionState.AUTHENTICATION_REQUIRED);
            entity.setProviderStatus(BrokerProviderStatus.AUTHENTICATION_REQUIRED.name());
            entity.setLastErrorCode(connectorStatus.code());
        } else {
            entity.setStatus(BrokerConnectionState.ERROR);
            entity.setProviderStatus(BrokerProviderStatus.ERROR.name());
            entity.setLastErrorCode(connectorStatus.code());
        }
        entity.setUpdatedAt(Instant.now());
    }

    private static void reconcileProviderStatus(BrokerConnectionEntity entity, BrokerConnectionStatus status) {
        entity.setStatus(status.state());
        entity.setProviderStatus(status.providerStatus().name());
        entity.setLastErrorCode(status.providerStatus() == BrokerProviderStatus.CONNECTED ? null : status.code());
        entity.setUpdatedAt(Instant.now());
    }

    private static String capabilities(BrokerProvider provider) {
        return provider.connectionCapabilities().capabilities().stream()
                .map(Enum::name)
                .sorted()
                .collect(Collectors.joining(","));
    }

    private static String capabilities(BrokerProvider provider, UUID userId, UUID connectionId) {
        return provider.connectionCapabilities(userId, connectionId).capabilities().stream()
                .map(Enum::name)
                .sorted()
                .collect(Collectors.joining(","));
    }
}
