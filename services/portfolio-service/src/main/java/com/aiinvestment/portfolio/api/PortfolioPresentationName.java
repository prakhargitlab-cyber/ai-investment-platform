package com.aiinvestment.portfolio.api;

import com.aiinvestment.portfolio.domain.Portfolio;
import com.aiinvestment.shared.domain.broker.BrokerType;

import java.util.regex.Pattern;

final class PortfolioPresentationName {
    private static final Pattern INTERNAL_PHASE_NAME = Pattern.compile(
            "(?i)^\\s*phase(?:[-_ ]?\\d+[a-z0-9]*)?(?:[-_ ].*)?$");

    private PortfolioPresentationName() {
    }

    static String forPortfolio(Portfolio portfolio) {
        String persistedName = portfolio.name();
        if (portfolio.brokerProvider() == null || !INTERNAL_PHASE_NAME.matcher(persistedName).matches()) {
            return persistedName;
        }
        return switch (portfolio.brokerProvider()) {
            case IBKR -> "Interactive Brokers";
            case ICICI_DIRECT -> "ICICI Direct";
            case HDFC_SECURITIES -> "HDFC Securities";
            default -> persistedName;
        };
    }

    static boolean visibleInManagedNavigation(Portfolio portfolio) {
        return portfolio.brokerProvider() != null || !INTERNAL_PHASE_NAME.matcher(portfolio.name()).matches();
    }
}
