package com.aiinvestment.broker.persistence;

import com.aiinvestment.broker.connector.BrokerConnectorState;
import com.aiinvestment.broker.connector.ConnectorRuntimeMode;
import com.aiinvestment.shared.domain.broker.BrokerType;
import jakarta.persistence.*;

import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "broker_connector_instances")
public class BrokerConnectorInstanceEntity {
    @Id
    @Column(name = "connector_id", nullable = false)
    private UUID connectorId;
    @Column(name = "user_id", nullable = false)
    private UUID userId;
    @Enumerated(EnumType.STRING)
    @Column(name = "broker_type", nullable = false)
    private BrokerType brokerType;
    @Enumerated(EnumType.STRING)
    @Column(name = "runtime_mode", nullable = false)
    private ConnectorRuntimeMode runtimeMode;
    @Enumerated(EnumType.STRING)
    @Column(name = "runtime_status", nullable = false)
    private BrokerConnectorState runtimeStatus;
    @Enumerated(EnumType.STRING)
    @Column(name = "auth_status", nullable = false)
    private BrokerConnectorState authStatus;
    @Column(name = "login_url")
    private String loginUrl;
    @Column(name = "runtime_endpoint")
    private String runtimeEndpoint;
    @Column(name = "runtime_identity")
    private String runtimeIdentity;
    @Column(name = "runtime_provider")
    private String runtimeProvider;
    @Column(name = "runtime_created_at")
    private Instant runtimeCreatedAt;
    @Column(name = "runtime_stopped_at")
    private Instant runtimeStoppedAt;
    @Column(name = "last_heartbeat_at")
    private Instant lastHeartbeatAt;
    @Column(name = "last_authenticated_at")
    private Instant lastAuthenticatedAt;
    @Column(name = "idle_timeout_seconds", nullable = false)
    private int idleTimeoutSeconds;
    @Column(name = "session_timeout_seconds", nullable = false)
    private int sessionTimeoutSeconds;
    @Column(name = "created_at", nullable = false)
    private Instant createdAt;
    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;

    protected BrokerConnectorInstanceEntity() {
    }

    public BrokerConnectorInstanceEntity(UUID connectorId, UUID userId, BrokerType brokerType, ConnectorRuntimeMode runtimeMode,
                                         BrokerConnectorState runtimeStatus, BrokerConnectorState authStatus,
                                         String loginUrl, int idleTimeoutSeconds, int sessionTimeoutSeconds,
                                         Instant createdAt, Instant updatedAt) {
        this.connectorId = connectorId;
        this.userId = userId;
        this.brokerType = brokerType;
        this.runtimeMode = runtimeMode;
        this.runtimeStatus = runtimeStatus;
        this.authStatus = authStatus;
        this.loginUrl = loginUrl;
        this.idleTimeoutSeconds = idleTimeoutSeconds;
        this.sessionTimeoutSeconds = sessionTimeoutSeconds;
        this.createdAt = createdAt;
        this.updatedAt = updatedAt;
    }

    public UUID getConnectorId() { return connectorId; }
    public UUID getUserId() { return userId; }
    public BrokerType getBrokerType() { return brokerType; }
    public ConnectorRuntimeMode getRuntimeMode() { return runtimeMode; }
    public BrokerConnectorState getRuntimeStatus() { return runtimeStatus; }
    public BrokerConnectorState getAuthStatus() { return authStatus; }
    public String getLoginUrl() { return loginUrl; }
    public String getRuntimeEndpoint() { return runtimeEndpoint; }
    public String getRuntimeIdentity() { return runtimeIdentity; }
    public String getRuntimeProvider() { return runtimeProvider; }
    public Instant getRuntimeCreatedAt() { return runtimeCreatedAt; }
    public Instant getRuntimeStoppedAt() { return runtimeStoppedAt; }
    public Instant getLastHeartbeatAt() { return lastHeartbeatAt; }
    public Instant getLastAuthenticatedAt() { return lastAuthenticatedAt; }
    public int getIdleTimeoutSeconds() { return idleTimeoutSeconds; }
    public int getSessionTimeoutSeconds() { return sessionTimeoutSeconds; }
    public Instant getCreatedAt() { return createdAt; }
    public Instant getUpdatedAt() { return updatedAt; }
    public void setRuntimeStatus(BrokerConnectorState runtimeStatus) { this.runtimeStatus = runtimeStatus; }
    public void setAuthStatus(BrokerConnectorState authStatus) { this.authStatus = authStatus; }
    public void setLoginUrl(String loginUrl) { this.loginUrl = loginUrl; }
    public void markRuntimeAllocated(String runtimeEndpoint, String runtimeIdentity, String runtimeProvider, Instant allocatedAt) {
        this.runtimeMode = ConnectorRuntimeMode.KUBERNETES;
        this.runtimeEndpoint = runtimeEndpoint;
        this.runtimeIdentity = runtimeIdentity;
        this.runtimeProvider = runtimeProvider;
        this.runtimeCreatedAt = allocatedAt;
        this.runtimeStoppedAt = null;
        this.updatedAt = allocatedAt;
    }
    public void markRuntimeStopped(Instant stoppedAt) {
        this.runtimeStoppedAt = stoppedAt;
        this.updatedAt = stoppedAt;
    }
    public void markAuthenticated(Instant authenticatedAt) {
        this.lastAuthenticatedAt = authenticatedAt;
        this.updatedAt = authenticatedAt;
    }
    public void setLastHeartbeatAt(Instant lastHeartbeatAt) { this.lastHeartbeatAt = lastHeartbeatAt; }
    public void setLastAuthenticatedAt(Instant lastAuthenticatedAt) { this.lastAuthenticatedAt = lastAuthenticatedAt; }
    public void setUpdatedAt(Instant updatedAt) { this.updatedAt = updatedAt; }
}
