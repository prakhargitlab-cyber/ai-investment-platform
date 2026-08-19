package com.aiinvestment.portfolio.infrastructure.persistence;

import com.aiinvestment.shared.domain.broker.BrokerAccountStatus;
import com.aiinvestment.shared.domain.broker.BrokerType;
import jakarta.persistence.*;

import java.util.UUID;

@Entity
@Table(name = "broker_accounts")
public class BrokerAccountEntity {
    @Id
    @Column(name = "broker_account_id", nullable = false)
    private String brokerAccountId;
    @Column(name = "user_id", nullable = false)
    private UUID userId;
    @Enumerated(EnumType.STRING)
    @Column(name = "broker_type", nullable = false)
    private BrokerType brokerType;
    @Column(name = "external_account_reference")
    private String externalAccountReference;
    @Column(name = "display_name", nullable = false)
    private String displayName;
    @Column(name = "base_currency", nullable = false)
    private String baseCurrency;
    @Enumerated(EnumType.STRING)
    @Column(nullable = false)
    private BrokerAccountStatus status;

    protected BrokerAccountEntity() {
    }

    public BrokerAccountEntity(String brokerAccountId, UUID userId, BrokerType brokerType, String externalAccountReference,
                               String displayName, String baseCurrency, BrokerAccountStatus status) {
        this.brokerAccountId = brokerAccountId;
        this.userId = userId;
        this.brokerType = brokerType;
        this.externalAccountReference = externalAccountReference;
        this.displayName = displayName;
        this.baseCurrency = baseCurrency;
        this.status = status;
    }

    public String getBrokerAccountId() { return brokerAccountId; }
    public UUID getUserId() { return userId; }
    public BrokerType getBrokerType() { return brokerType; }
    public String getExternalAccountReference() { return externalAccountReference; }
    public String getDisplayName() { return displayName; }
    public String getBaseCurrency() { return baseCurrency; }
    public BrokerAccountStatus getStatus() { return status; }
}
