package com.aiinvestment.broker.provider.icici;

import com.aiinvestment.broker.application.BrokerProviderException;
import com.aiinvestment.broker.config.ICICIDirectProviderProperties;
import com.aiinvestment.broker.connector.BrokerConnectorState;
import com.aiinvestment.broker.connector.BrokerConnectorStatus;
import com.aiinvestment.broker.security.SecretProvider;
import com.aiinvestment.broker.security.BrokerCredentialStore;
import com.aiinvestment.shared.domain.AssetType;
import com.aiinvestment.shared.domain.Instrument;
import com.aiinvestment.shared.domain.Money;
import com.aiinvestment.shared.domain.broker.*;
import org.springframework.stereotype.Component;
import org.springframework.beans.factory.annotation.Autowired;

import java.net.URI;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.math.BigDecimal;
import java.time.*;
import java.util.EnumSet;
import java.util.List;
import java.util.UUID;
import java.util.regex.Pattern;

@Component
public class BreezeICICIDirectConnector implements ICICIDirectConnector {
    public static final String AUTH_METHOD = "breeze-interactive-session";
    private static final String OFFICIAL_HOST = "api.icicidirect.com";
    private static final ZoneId INDIA_ZONE = ZoneId.of("Asia/Kolkata");
    private static final Pattern ISIN = Pattern.compile("[A-Z]{2}[A-Z0-9]{9}[0-9]");
    private static final String EMPTY_BODY = "{}";

    private final ICICIDirectProviderProperties properties;
    private final SecretProvider secretProvider;
    private final BreezeSessionStore sessionStore;
    private final BreezeCustomerDetailsClient customerDetailsClient;
    private final BreezeReadClient readClient;
    private final BreezeRequestSigner requestSigner;
    private final Clock clock;
    private final BrokerCredentialStore credentialStore;

    @Autowired
    public BreezeICICIDirectConnector(ICICIDirectProviderProperties properties, SecretProvider secretProvider,
                                      BreezeSessionStore sessionStore,
                                      BreezeCustomerDetailsClient customerDetailsClient,
                                      BreezeReadClient readClient, BreezeRequestSigner requestSigner,
                                      BrokerCredentialStore credentialStore) {
        this(properties, secretProvider, sessionStore, customerDetailsClient, readClient, requestSigner,
                Clock.systemUTC(), credentialStore);
    }

    BreezeICICIDirectConnector(ICICIDirectProviderProperties properties, SecretProvider secretProvider,
                               BreezeSessionStore sessionStore,
                               BreezeCustomerDetailsClient customerDetailsClient, BreezeReadClient readClient,
                               BreezeRequestSigner requestSigner, Clock clock) {
        this(properties, secretProvider, sessionStore, customerDetailsClient, readClient, requestSigner, clock,
                new com.aiinvestment.broker.security.InMemoryBrokerCredentialStore());
    }

    BreezeICICIDirectConnector(ICICIDirectProviderProperties properties, SecretProvider secretProvider,
                               BreezeSessionStore sessionStore,
                               BreezeCustomerDetailsClient customerDetailsClient, BreezeReadClient readClient,
                               BreezeRequestSigner requestSigner, Clock clock, BrokerCredentialStore credentialStore) {
        this.properties = properties;
        this.secretProvider = secretProvider;
        this.sessionStore = sessionStore;
        this.customerDetailsClient = customerDetailsClient;
        this.readClient = readClient;
        this.requestSigner = requestSigner;
        this.clock = clock;
        this.credentialStore = credentialStore;
    }

    @Override
    public BrokerConnectionCapabilities supportedCapabilities() {
        return new BrokerConnectionCapabilities(EnumSet.of(BrokerCapability.ACCOUNTS_READ,
                BrokerCapability.ACCOUNT_METADATA_READ, BrokerCapability.POSITIONS_READ,
                BrokerCapability.CASH_READ, BrokerCapability.PORTFOLIO_READ));
    }

    @Override
    public BrokerConnectorStatus status(UUID userId, UUID connectionId) {
        var session = sessionStore.find(userId, connectionId);
        if (session.isEmpty()) {
            return status(BrokerConnectorState.AUTHENTICATION_REQUIRED, "AUTHENTICATION_REQUIRED",
                    "Interactive ICICI Direct authentication is required.");
        }
        if (session.get().expired(Instant.now(clock))) {
            return status(BrokerConnectorState.SESSION_EXPIRED, "SESSION_EXPIRED",
                    "The ICICI Direct session has expired; interactive authentication is required.");
        }
        return new BrokerConnectorStatus(BrokerConnectorState.CONNECTED, BrokerConnectorState.CONNECTED,
                Instant.now(clock), session.get().authenticatedAt(), "CONNECTED",
                "ICICI Direct session is connected.");
    }

    @Override
    public ICICIDirectLogin login(UUID userId, UUID connectionId) {
        var credentials = credentials(userId, connectionId);
        URI loginUri = URI.create(properties.loginUrl());
        if (!"https".equalsIgnoreCase(loginUri.getScheme())
                || !OFFICIAL_HOST.equalsIgnoreCase(loginUri.getHost())
                || !"/apiuser/login".equals(loginUri.getPath())) {
            throw BrokerProviderException.documentationRequired(BrokerType.ICICI_DIRECT);
        }
        sessionStore.markLoginInitiated(userId, connectionId);
        String encodedAppKey = URLEncoder.encode(credentials.clientKey(), StandardCharsets.UTF_8);
        return new ICICIDirectLogin(connectionId, properties.loginUrl() + "?api_key=" + encodedAppKey);
    }

    @Override
    public BrokerConnectorStatus attachApiSession(UUID userId, UUID connectionId, String apiSession) {
        var credentials = credentials(userId, connectionId);
        if (apiSession == null || apiSession.isBlank()) {
            throw new IllegalArgumentException("API session is required");
        }
        if (!sessionStore.consumeLoginInitiated(userId, connectionId)) {
            throw BrokerProviderException.authenticationRequired(BrokerType.ICICI_DIRECT);
        }
        BreezeCustomerDetails details = customerDetailsClient.exchangeApiSession(credentials.clientKey(), apiSession);
        Instant authenticatedAt = Instant.now(clock);
        sessionStore.store(new BreezeSession(userId, connectionId, details.sessionToken(),
                details.providerUserId(), details.displayName(), authenticatedAt, expiresAt(authenticatedAt)));
        return status(userId, connectionId);
    }

    @Override
    public List<BrokerAccount> fetchAccounts(UUID userId, UUID connectionId) {
        BreezeSession session = requireAuthenticated(userId, connectionId);
        if (blank(session.providerUserId()) || blank(session.displayName())) {
            throw BrokerProviderException.unavailable(BrokerType.ICICI_DIRECT);
        }
        String accountId = accountId(session.providerUserId());
        return List.of(new BrokerAccount(accountId, userId, BrokerType.ICICI_DIRECT,
                masked(session.providerUserId()), session.displayName(), null, BrokerAccountStatus.ACTIVE));
    }

    @Override
    public List<BrokerPosition> fetchPositions(UUID userId, UUID connectionId, BrokerAccount account) {
        BreezeSession session = requireAccount(userId, connectionId, account);
        var credentials = credentials(userId, connectionId);
        BreezeSignedHeaders headers = requestSigner.sign(EMPTY_BODY, session.sessionToken(),
                credentials.clientKey(), credentials.clientSecret());
        Instant observedAt = Instant.now(clock);
        return readClient.fetchDematHoldings(headers).stream()
                .map(holding -> normalizeHolding(account, holding, observedAt))
                .flatMap(java.util.Optional::stream)
                .toList();
    }

    @Override
    public List<BrokerCashBalance> fetchCashBalances(UUID userId, UUID connectionId, BrokerAccount account) {
        BreezeSession session = requireAccount(userId, connectionId, account);
        var credentials = credentials(userId, connectionId);
        BreezeFunds funds = readClient.fetchFunds(requestSigner.sign(EMPTY_BODY, session.sessionToken(),
                credentials.clientKey(), credentials.clientSecret()));
        return List.of(new BrokerCashBalance(account.brokerAccountId(), new Money(funds.totalBankBalance(), "INR"),
                null, null, null, null, null, "BREEZE_FUNDS_TOTAL_BANK_BALANCE"));
    }

    @Override
    public void disconnect(UUID userId, UUID connectionId) {
        sessionStore.remove(userId, connectionId);
    }

    private BreezeSession requireAuthenticated(UUID userId, UUID connectionId) {
        BrokerConnectorStatus current = status(userId, connectionId);
        if (!current.authenticated()) {
            throw BrokerProviderException.authenticationRequired(BrokerType.ICICI_DIRECT);
        }
        return sessionStore.find(userId, connectionId)
                .orElseThrow(() -> BrokerProviderException.authenticationRequired(BrokerType.ICICI_DIRECT));
    }

    private BreezeSession requireAccount(UUID userId, UUID connectionId, BrokerAccount account) {
        BreezeSession session = requireAuthenticated(userId, connectionId);
        if (account == null || !userId.equals(account.userId()) || account.brokerType() != BrokerType.ICICI_DIRECT
                || !accountId(session.providerUserId()).equals(account.brokerAccountId())) {
            throw BrokerProviderException.authenticationRequired(BrokerType.ICICI_DIRECT);
        }
        return session;
    }

    private static java.util.Optional<BrokerPosition> normalizeHolding(BrokerAccount account,
                                                                       BreezeDematHolding holding,
                                                                       Instant observedAt) {
        String normalizedIsin = normalizeIsin(holding.stockIsin());
        if (normalizedIsin == null || holding.quantity() == null || holding.quantity().signum() < 0
                || blank(holding.stockCode())) {
            return java.util.Optional.empty();
        }
        String externalId = "ISIN:" + normalizedIsin;
        Instrument instrument = new Instrument(UUID.nameUUIDFromBytes(
                (BrokerType.ICICI_DIRECT.name() + "|" + externalId).getBytes(StandardCharsets.UTF_8)),
                BrokerType.ICICI_DIRECT.name(), externalId, normalizedIsin, holding.stockCode().trim(), null,
                null, holding.stockCode().trim(), AssetType.EQUITY, "IN", null, null, null);
        return java.util.Optional.of(new BrokerPosition(account.brokerAccountId(), instrument,
                holding.quantity(), null, null, null, null, observedAt));
    }

    private static String normalizeIsin(String value) {
        if (value == null) return null;
        String normalized = value.replaceAll("\\s+", "").toUpperCase(java.util.Locale.ROOT);
        return ISIN.matcher(normalized).matches() ? normalized : null;
    }

    private static String accountId(String providerUserId) {
        return "ICICI_DIRECT:" + UUID.nameUUIDFromBytes(
                providerUserId.getBytes(StandardCharsets.UTF_8));
    }

    private static String masked(String value) {
        String trimmed = value.trim();
        int visible = Math.min(4, trimmed.length());
        return "***" + trimmed.substring(trimmed.length() - visible);
    }

    private void requireConfigured() {
        if (!properties.enabled() || !properties.officialDocumentationVerified()
                || !AUTH_METHOD.equalsIgnoreCase(properties.authMethod())
                || blank(properties.appKey()) || blank(properties.secretKeyReference())) {
            throw BrokerProviderException.authenticationRequired(BrokerType.ICICI_DIRECT);
        }
    }

    private BrokerCredentialStore.BrokerCredentials credentials(UUID userId, UUID connectionId) {
        return credentialStore.find(userId, connectionId, BrokerType.ICICI_DIRECT)
                .orElseGet(() -> {
                    requireConfigured();
                    String secret = secretProvider.getSecret(properties.secretKeyReference())
                            .orElseThrow(() -> BrokerProviderException.secretUnavailable(BrokerType.ICICI_DIRECT));
                    return new BrokerCredentialStore.BrokerCredentials(properties.appKey(), secret);
                });
    }

    private static Instant expiresAt(Instant authenticatedAt) {
        Instant after24Hours = authenticatedAt.plus(Duration.ofHours(24));
        Instant nextMidnight = authenticatedAt.atZone(INDIA_ZONE).toLocalDate().plusDays(1)
                .atStartOfDay(INDIA_ZONE).toInstant();
        return after24Hours.isBefore(nextMidnight) ? after24Hours : nextMidnight;
    }

    private static BrokerConnectorStatus status(BrokerConnectorState state, String code, String message) {
        return new BrokerConnectorStatus(state, state, null, null, code, message);
    }

    private static boolean blank(String value) {
        return value == null || value.isBlank();
    }
}
