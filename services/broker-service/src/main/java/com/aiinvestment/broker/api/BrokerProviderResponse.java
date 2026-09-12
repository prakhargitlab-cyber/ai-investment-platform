package com.aiinvestment.broker.api;

import com.aiinvestment.broker.domain.BrokerConnection;
import com.aiinvestment.shared.domain.broker.BrokerCapability;
import com.aiinvestment.shared.domain.broker.BrokerConnectionCapabilities;
import com.aiinvestment.shared.domain.broker.BrokerProvider;
import com.aiinvestment.shared.domain.broker.BrokerType;

import java.util.List;
import java.util.Set;
import java.util.stream.Collectors;

public record BrokerProviderResponse(
        BrokerType brokerType,
        String displayName,
        boolean connectable,
        String unavailableReason,
        String status,
        String providerStatus,
        String code,
        String message,
        String dataFreshness,
        boolean readOnly,
        boolean officialProviderSetupRequired,
        String consumerAuthMode,
        boolean individualApiSupported,
        boolean advancedIndividualMode,
        boolean manualImportSupported,
        String manualImportParserStatus
) {
    public static BrokerProviderResponse from(BrokerProvider provider) {
        return from(provider, List.of(), defaultConsumerAuthMode(provider.supportedBroker()), false);
    }

    public static BrokerProviderResponse from(BrokerProvider provider, List<BrokerConnection> connections) {
        return from(provider, connections, defaultConsumerAuthMode(provider.supportedBroker()), false);
    }

    public static BrokerProviderResponse from(BrokerProvider provider, List<BrokerConnection> connections,
                                              String consumerAuthMode) {
        return from(provider, connections, consumerAuthMode, false);
    }

    public static BrokerProviderResponse from(BrokerProvider provider, List<BrokerConnection> connections,
                                              String consumerAuthMode, boolean advancedIndividualMode) {
        boolean mock = provider.supportedBroker() == BrokerType.MOCK;
        var status = provider.connectionStatus();
        BrokerConnection activeConnection = connections.stream()
                .filter(connection -> connection.brokerType() == provider.supportedBroker())
                .findFirst()
                .orElse(null);
        boolean configured = activeConnection != null;
        String providerStatus = activeConnection != null && activeConnection.providerStatus() != null && !activeConnection.providerStatus().isBlank()
                ? activeConnection.providerStatus()
                : status.providerStatus().name();
        String code = configured && "NOT_CONFIGURED".equals(status.code()) ? providerStatus : status.code();
        String message = configured && status.providerStatus().name().equals("NOT_CONFIGURED")
                ? "Broker connection is configured for this user."
                : status.message();
        boolean setupRequired = !mock && !configured && status.providerStatus().name().matches("NOT_CONFIGURED|DOCUMENTATION_REQUIRED");
        String freshness = activeConnection != null && activeConnection.dataFreshness() != null && !activeConnection.dataFreshness().isBlank()
                ? activeConnection.dataFreshness()
                : mock ? "DEMO" : status.providerStatus().name().equals("CONNECTED") ? "REAL_BROKER" : "UNAVAILABLE";
        boolean connectable = mock || provider.supportedBroker() == BrokerType.IBKR
                || provider.supportedBroker() == BrokerType.ICICI_DIRECT
                || provider.supportedBroker() == BrokerType.HDFC_SECURITIES;
        String unavailableReason = connectable ? null : message;
        return new BrokerProviderResponse(provider.supportedBroker(), displayName(provider.supportedBroker()),
                connectable, unavailableReason,
                activeConnection == null ? status.state().name() : activeConnection.status().name(),
                providerStatus, code, message,
                freshness,
                !capabilities(provider, activeConnection).contains(BrokerCapability.ORDER_EXECUTION.name()),
                setupRequired, consumerAuthMode, individualApiSupported(provider.supportedBroker()),
                advancedIndividualMode, manualImportSupported(provider.supportedBroker()),
                manualImportParserStatus(provider.supportedBroker()));
    }

    public static BrokerProviderResponse unsupported(BrokerType brokerType, String displayName, String reason) {
        return new BrokerProviderResponse(brokerType, displayName,
                false, reason, "DISCONNECTED", "UNAVAILABLE", "UNSUPPORTED", reason,
                "UNAVAILABLE", true, true, "NONE", false, false, false, "UNAVAILABLE");
    }

    private static String defaultConsumerAuthMode(BrokerType brokerType) {
        return brokerType == BrokerType.IBKR ? "BROKER_REDIRECT"
                : brokerType == BrokerType.ICICI_DIRECT || brokerType == BrokerType.HDFC_SECURITIES
                ? "PARTNER_UNAVAILABLE" : "NONE";
    }

    private static boolean individualApiSupported(BrokerType brokerType) {
        return brokerType == BrokerType.ICICI_DIRECT || brokerType == BrokerType.HDFC_SECURITIES;
    }

    private static boolean manualImportSupported(BrokerType brokerType) {
        return brokerType == BrokerType.ICICI_DIRECT || brokerType == BrokerType.HDFC_SECURITIES;
    }

    private static String manualImportParserStatus(BrokerType brokerType) {
        return brokerType == BrokerType.ICICI_DIRECT || brokerType == BrokerType.HDFC_SECURITIES
                ? "SUPPORTED" : "UNAVAILABLE";
    }

    private static String displayName(BrokerType brokerType) {
        return switch (brokerType) {
            case IBKR -> "Interactive Brokers";
            case ICICI_DIRECT -> "ICICI Direct";
            case HDFC_SECURITIES -> "HDFC Securities";
            case MOCK -> "Demo Broker";
        };
    }

    public static Set<String> capabilities(BrokerProvider provider, BrokerConnection connection) {
        if (connection != null && connection.capabilities() != null && !connection.capabilities().isBlank()) {
            return Set.of(connection.capabilities().split(",")).stream()
                    .map(String::trim)
                    .filter(value -> !value.isBlank())
                    .filter(value -> !BrokerCapability.ORDER_EXECUTION.name().equals(value))
                    .collect(Collectors.toUnmodifiableSet());
        }
        Set<BrokerCapability> providerCapabilities = provider.connectionCapabilities().capabilities();
        if (providerCapabilities.isEmpty() && connection != null && connection.brokerType() == BrokerType.IBKR) {
            providerCapabilities = BrokerConnectionCapabilities.ibkrReadOnly(false).capabilities();
        }
        return providerCapabilities.stream()
                .filter(capability -> capability != BrokerCapability.ORDER_EXECUTION)
                .map(Enum::name)
                .collect(Collectors.toUnmodifiableSet());
    }
}
