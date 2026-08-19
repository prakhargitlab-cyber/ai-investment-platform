package com.aiinvestment.broker.application;

import com.aiinvestment.broker.domain.BrokerConnection;
import com.aiinvestment.broker.audit.BrokerOperationAuditor;
import com.aiinvestment.broker.persistence.BrokerConnectionEntity;
import com.aiinvestment.broker.persistence.BrokerConnectionMapper;
import com.aiinvestment.broker.persistence.BrokerConnectionRepository;
import com.aiinvestment.broker.resilience.ProviderCircuitBreaker;
import com.aiinvestment.broker.resilience.ProviderRateLimiter;
import com.aiinvestment.shared.domain.broker.*;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Instant;
import java.util.List;
import java.util.UUID;

@Service
public class BrokerConnectionService {
    public static final UUID DEFAULT_PHASE_USER = UUID.fromString("00000000-0000-0000-0000-000000000001");

    private final BrokerConnectionRepository repository;
    private final List<BrokerProvider> providers;
    private final ProviderRateLimiter rateLimiter;
    private final ProviderCircuitBreaker circuitBreaker;
    private final BrokerOperationAuditor auditor;

    public BrokerConnectionService(BrokerConnectionRepository repository, List<BrokerProvider> providers,
                                   ProviderRateLimiter rateLimiter, ProviderCircuitBreaker circuitBreaker,
                                   BrokerOperationAuditor auditor) {
        this.repository = repository;
        this.providers = providers;
        this.rateLimiter = rateLimiter;
        this.circuitBreaker = circuitBreaker;
        this.auditor = auditor;
    }

    @Transactional(readOnly = true)
    public List<BrokerConnection> listConnections() {
        return repository.findByUserId(DEFAULT_PHASE_USER).stream().map(BrokerConnectionMapper::toDomain).toList();
    }

    @Transactional(readOnly = true)
    public BrokerConnection getConnection(UUID connectionId) {
        return repository.findById(connectionId).map(BrokerConnectionMapper::toDomain)
                .orElseThrow(() -> new BrokerConnectionNotFoundException(connectionId));
    }

    @Transactional
    public BrokerConnection connectMock() {
        Instant now = Instant.now();
        BrokerConnectionEntity entity = new BrokerConnectionEntity(UUID.randomUUID(), DEFAULT_PHASE_USER, BrokerType.MOCK,
                "mock-demo", "Demo Broker", BrokerConnectionState.CONNECTED, now, null, null, null, now, now);
        return BrokerConnectionMapper.toDomain(repository.save(entity));
    }

    @Transactional
    public BrokerConnection initiateConnection(BrokerType brokerType) {
        Instant startedAt = Instant.now();
        if (brokerType == BrokerType.MOCK) {
            BrokerConnection connection = connectMock();
            auditor.record(brokerType, "connect", startedAt, true, "LOCAL");
            return connection;
        }
        BrokerConnectionStatus status = providerFor(brokerType).connectionStatus();
        auditor.record(brokerType, "connect", startedAt, false, status.code());
        throw new IllegalStateException(status.code() + ": " + status.message());
    }

    @Transactional(readOnly = true)
    public BrokerConnection status(UUID connectionId) {
        return getConnection(connectionId);
    }

    @Transactional
    public BrokerConnection refresh(UUID connectionId) {
        BrokerConnectionEntity entity = repository.findById(connectionId)
                .orElseThrow(() -> new BrokerConnectionNotFoundException(connectionId));
        if (entity.getBrokerType() != BrokerType.MOCK) {
            throw new IllegalStateException("Refresh is unavailable until official provider authentication is configured.");
        }
        entity.setUpdatedAt(Instant.now());
        return BrokerConnectionMapper.toDomain(entity);
    }

    @Transactional
    public BrokerConnection sync(UUID connectionId) {
        Instant startedAt = Instant.now();
        BrokerConnectionEntity entity = repository.findById(connectionId)
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
        if (provider.connectionStatus().state() == BrokerConnectionState.DISCONNECTED && entity.getBrokerType() != BrokerType.MOCK) {
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

    @Transactional
    public void disconnect(UUID connectionId) {
        BrokerConnectionEntity entity = repository.findById(connectionId)
                .orElseThrow(() -> new BrokerConnectionNotFoundException(connectionId));
        providerFor(entity.getBrokerType()).disconnect(connectionId);
        entity.setStatus(BrokerConnectionState.DISCONNECTED);
        entity.setUpdatedAt(Instant.now());
    }

    public List<BrokerProvider> providers() {
        return providers;
    }

    private BrokerProvider providerFor(BrokerType brokerType) {
        return providers.stream().filter(provider -> provider.supportedBroker() == brokerType).findFirst()
                .orElseThrow(() -> new IllegalStateException("No provider for " + brokerType));
    }
}
