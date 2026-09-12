package com.aiinvestment.portfolio.application;

import com.aiinvestment.portfolio.domain.Portfolio;
import com.aiinvestment.portfolio.domain.PortfolioCalculator;
import com.aiinvestment.portfolio.domain.PortfolioHistory;
import com.aiinvestment.portfolio.domain.PortfolioHistoryRange;
import com.aiinvestment.portfolio.domain.PortfolioPosition;
import com.aiinvestment.portfolio.domain.PortfolioSummary;
import com.aiinvestment.portfolio.domain.PortfolioValuationPoint;
import com.aiinvestment.shared.domain.Money;
import com.aiinvestment.portfolio.infrastructure.persistence.*;
import com.aiinvestment.shared.domain.broker.*;
import com.aiinvestment.shared.domain.event.BrokerSyncEvent;
import com.aiinvestment.shared.domain.event.PortfolioUpdatedEvent;
import com.aiinvestment.shared.web.auth.AuthenticatedUser;
import com.aiinvestment.shared.web.auth.AuthenticationHeaders;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpHeaders;
import org.slf4j.MDC;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.client.RestClient;

import java.time.Instant;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import com.aiinvestment.shared.web.CorrelationIdFilter;

@Service
public class PortfolioService {
    private static final Logger logger = LoggerFactory.getLogger(PortfolioService.class);
    private static final UUID MOCK_CONNECTION_ID = UUID.nameUUIDFromBytes("mock-broker-connection".getBytes());
    private static final Set<UUID> ACTIVE_SYNCS = ConcurrentHashMap.newKeySet();

    private final PortfolioRepository portfolioRepository;
    private final BrokerAccountRepository brokerAccountRepository;
    private final BrokerAccountCashBalanceRepository brokerAccountCashBalanceRepository;
    private final BrokerAccountCashBalanceEntryRepository brokerAccountCashBalanceEntryRepository;
    private final PortfolioValuationSnapshotRepository valuationSnapshotRepository;
    private final BrokerPositionSnapshotRepository brokerPositionSnapshotRepository;
    private final InstrumentRepository instrumentRepository;
    private final PortfolioPositionRepository positionRepository;
    private final BrokerHoldingRepository holdingRepository;
    private final PortfolioCalculator calculator;
    private final BrokerProvider brokerProvider;
    private final RestClient restClient;
    private final String brokerServiceBaseUrl;
    private final PlatformEventPublisher eventPublisher;
    private final InstrumentMasterService instrumentMaster;

    public PortfolioService(PortfolioRepository portfolioRepository,
                            BrokerAccountRepository brokerAccountRepository,
                            BrokerAccountCashBalanceRepository brokerAccountCashBalanceRepository,
                            BrokerAccountCashBalanceEntryRepository brokerAccountCashBalanceEntryRepository,
                            PortfolioValuationSnapshotRepository valuationSnapshotRepository,
                            BrokerPositionSnapshotRepository brokerPositionSnapshotRepository,
                            InstrumentRepository instrumentRepository,
                            PortfolioPositionRepository positionRepository,
                            BrokerHoldingRepository holdingRepository,
                            PortfolioCalculator calculator,
                            PlatformEventPublisher eventPublisher,
                            InstrumentMasterService instrumentMaster,
                            List<BrokerProvider> brokerProviders,
                            @Value("${broker.service.base-url:http://broker-service}") String brokerServiceBaseUrl) {
        this.portfolioRepository = portfolioRepository;
        this.brokerAccountRepository = brokerAccountRepository;
        this.brokerAccountCashBalanceRepository = brokerAccountCashBalanceRepository;
        this.brokerAccountCashBalanceEntryRepository = brokerAccountCashBalanceEntryRepository;
        this.valuationSnapshotRepository = valuationSnapshotRepository;
        this.brokerPositionSnapshotRepository = brokerPositionSnapshotRepository;
        this.instrumentRepository = instrumentRepository;
        this.positionRepository = positionRepository;
        this.holdingRepository = holdingRepository;
        this.calculator = calculator;
        this.eventPublisher = eventPublisher;
        this.instrumentMaster = instrumentMaster;
        this.brokerServiceBaseUrl = brokerServiceBaseUrl;
        this.restClient = RestClient.builder().build();
        this.brokerProvider = brokerProviders.stream()
                .filter(provider -> provider.supportedBroker() == BrokerType.MOCK)
                .findFirst()
                .orElseThrow(() -> new IllegalStateException("MockBrokerProvider is required for Phase 2B sync validation"));
    }

    @Transactional
    public Portfolio createPortfolio(UUID userId, String name, String baseCurrency) {
        Instant now = Instant.now();
        PortfolioEntity entity = new PortfolioEntity(UUID.randomUUID(), userId, name, baseCurrency, now, now);
        return PortfolioMapper.toDomain(portfolioRepository.save(entity));
    }

    @Transactional(readOnly = true)
    public Portfolio getPortfolio(UUID userId, UUID portfolioId) {
        return portfolioRepository.findByPortfolioIdAndUserId(portfolioId, userId)
                .map(PortfolioMapper::toDomain)
                .orElseThrow(() -> new PortfolioNotFoundException(portfolioId));
    }

    @Transactional
    public List<PortfolioSummary> listPortfolioSummaries(UUID userId) {
        adoptUnambiguousLegacyBrokerPortfolios(userId);
        return portfolioRepository.findByUserId(userId).stream()
                .map(PortfolioMapper::toDomain)
                .map(portfolio -> calculator.summarize(portfolio, activePositions(userId, portfolio.portfolioId()), cashBalancesFor(portfolio)))
                .toList();
    }

    /**
     * Attaches Phase 5C portfolios whose persisted position provenance already
     * contains one complete, unambiguous broker source. No live broker session
     * is required and no position is rewritten.
     */
    private void adoptUnambiguousLegacyBrokerPortfolios(UUID userId) {
        for (PortfolioEntity portfolio : portfolioRepository.findByUserId(userId)) {
            if (portfolio.getBrokerConnectionId() != null) {
                continue;
            }
            List<PortfolioPositionEntity> positions = positionRepository
                    .findByPortfolioPortfolioId(portfolio.getPortfolioId());
            if (positions.isEmpty() || positions.stream().noneMatch(position ->
                    "REAL_BROKER".equals(position.getDataFreshness()))) {
                continue;
            }
            Set<LegacyBrokerSource> sources = positions.stream()
                    .filter(position -> "BROKER".equals(position.getSourceType()))
                    .filter(position -> position.getSourceConnectionId() != null)
                    .filter(position -> position.getSourceBrokerType() != null && !position.getSourceBrokerType().isBlank())
                    .filter(position -> position.getSourceBrokerAccountId() != null && !position.getSourceBrokerAccountId().isBlank())
                    .map(position -> new LegacyBrokerSource(position.getSourceConnectionId(),
                            BrokerType.valueOf(position.getSourceBrokerType()), position.getSourceBrokerAccountId()))
                    .collect(java.util.stream.Collectors.toSet());
            boolean everyPositionHasSameSource = sources.size() == 1 && positions.stream().allMatch(position ->
                    "BROKER".equals(position.getSourceType()) && position.getSourceConnectionId() != null
                            && position.getSourceBrokerType() != null && position.getSourceBrokerAccountId() != null);
            if (!everyPositionHasSameSource) {
                if (sources.size() > 1) {
                    logger.warn("legacyBrokerPortfolioAdoption portfolioId={} result=AMBIGUOUS sourceCount={}",
                            portfolio.getPortfolioId(), sources.size());
                }
                continue;
            }
            LegacyBrokerSource source = sources.iterator().next();
            var existing = portfolioRepository.findByUserIdAndBrokerConnectionIdAndBrokerAccountId(
                    userId, source.connectionId(), source.accountId());
            if (existing.isPresent() && !existing.get().getPortfolioId().equals(portfolio.getPortfolioId())) {
                throw new IllegalStateException("Broker source is already attached to another owned portfolio");
            }
            portfolio.attachBrokerSource(source.connectionId(), source.accountId(), source.provider());
            logger.info("legacyBrokerPortfolioAdoption provider={} connectionId={} portfolioId={} result=ADOPTED positionCount={}",
                    source.provider(), source.connectionId(), portfolio.getPortfolioId(), positions.size());
        }
    }

    private record LegacyBrokerSource(UUID connectionId, BrokerType provider, String accountId) { }

    @Transactional(readOnly = true)
    public Map<String, java.math.BigDecimal> currencyTotals(UUID userId) {
        Map<String, java.math.BigDecimal> totals = new java.util.TreeMap<>();
        for (PortfolioSummary summary : listPortfolioSummaries(userId)) {
            if (summary.totalMarketValue() == null || summary.totalMarketValue().currency() == null) {
                continue;
            }
            totals.merge(summary.totalMarketValue().currency(), summary.totalMarketValue().amount(),
                    java.math.BigDecimal::add);
        }
        return Map.copyOf(totals);
    }

    @Transactional(readOnly = true)
    public List<PortfolioPosition> getPositions(UUID userId, UUID portfolioId) {
        ensurePortfolioExists(userId, portfolioId);
        return positionRepository.findByPortfolioPortfolioId(portfolioId).stream()
                .map(PortfolioMapper::toDomain)
                .toList();
    }

    @Transactional
    public PortfolioPosition updateCustomDisplayName(UUID userId, UUID portfolioId, UUID positionId, String requestedName) {
        ensurePortfolioExists(userId, portfolioId);
        PortfolioPositionEntity position = positionRepository
                .findByPositionIdAndPortfolioPortfolioIdAndPortfolioUserId(positionId, portfolioId, userId)
                .orElseThrow(() -> new PortfolioNotFoundException(portfolioId));
        if (!"MANUAL_CSV_IMPORT".equals(position.getSourceType())) {
            throw new IllegalArgumentException("Display names can only be changed for manually imported holdings");
        }
        String customName = requestedName == null ? null : requestedName.trim();
        if (customName != null && customName.isEmpty()) customName = null;
        if (customName != null && customName.length() > 160) {
            throw new IllegalArgumentException("Display name must be 160 characters or fewer");
        }
        position.setCustomDisplayName(customName);
        return PortfolioMapper.toDomain(positionRepository.save(position));
    }

    @Transactional(readOnly = true)
    public PortfolioSummary getSummary(UUID userId, UUID portfolioId) {
        Portfolio portfolio = getPortfolio(userId, portfolioId);
        List<PortfolioPosition> positions = activePositions(userId, portfolioId);
        return calculator.summarize(portfolio, positions, cashBalancesFor(portfolio));
    }

    @Transactional
    public PortfolioSummary sync(UUID userId, UUID portfolioId) {
        if (!ACTIVE_SYNCS.add(portfolioId)) {
            throw new IllegalStateException("Portfolio sync already in progress: " + portfolioId);
        }
        String correlationId = MDC.get("correlationId");
        eventPublisher.publish(BrokerSyncEvent.started(MOCK_CONNECTION_ID, portfolioId, correlationId));
        try {
            PortfolioSummary summary = doSync(userId, portfolioId, correlationId);
            eventPublisher.publish(BrokerSyncEvent.completed(MOCK_CONNECTION_ID, portfolioId, correlationId));
            return summary;
        } catch (RuntimeException exception) {
            eventPublisher.publish(BrokerSyncEvent.failed(MOCK_CONNECTION_ID, portfolioId, correlationId, exception.getClass().getSimpleName()));
            throw exception;
        } finally {
            ACTIVE_SYNCS.remove(portfolioId);
        }
    }

    @Transactional
    public PortfolioSummary importBrokerConnection(AuthenticatedUser user, UUID portfolioId, UUID connectionId) {
        if (!ACTIVE_SYNCS.add(portfolioId)) {
            throw new IllegalStateException("Portfolio sync already in progress: " + portfolioId);
        }
        String correlationId = MDC.get("correlationId");
        eventPublisher.publish(BrokerSyncEvent.started(connectionId, portfolioId, correlationId));
        try {
            PortfolioEntity portfolio = portfolioRepository.findByPortfolioIdAndUserId(portfolioId, user.userId())
                    .orElseThrow(() -> new PortfolioNotFoundException(portfolioId));
            BrokerSnapshotClientResponse snapshot = restClient.get()
                    .uri(brokerServiceBaseUrl + "/api/v1/broker-connections/" + connectionId + "/snapshot")
                    .headers(headers -> addIdentity(headers, user))
                    .retrieve()
                    .body(BrokerSnapshotClientResponse.class);
            if (snapshot == null) {
                throw new IllegalStateException("Broker snapshot was empty");
            }
            if (isRealBrokerSnapshot(snapshot)) {
                resolveImportedBaseCurrency(snapshot).ifPresent(portfolio::setBaseCurrency);
                portfolio.setUpdatedAt(Instant.now());
            }
            UUID syncGenerationId = UUID.randomUUID();
            for (BrokerSnapshotClientResponse.Account account : snapshot.accounts()) {
                BrokerType brokerType = BrokerType.valueOf(account.brokerType());
                BrokerAccountEntity accountEntity = resolveBrokerAccountEntity(user.userId(), connectionId, brokerType,
                        account.brokerAccountId(), account.externalAccountReference(), account.displayName(),
                        account.baseCurrency(), BrokerAccountStatus.valueOf(account.status()));
                List<BrokerSnapshotClientResponse.Position> accountPositions = snapshot.positions().stream()
                        .filter(position -> position.brokerAccountId().equals(account.brokerAccountId()))
                        .toList();
                for (BrokerSnapshotClientResponse.Position brokerPosition : accountPositions) {
                    upsertBrokerPosition(user.userId(), portfolio, connectionId, brokerType, account, accountEntity,
                            brokerPosition, syncGenerationId);
                }
                markMissingPositionsStale(user.userId(), portfolioId, connectionId, account.brokerAccountId(),
                        accountPositions, syncGenerationId);
            }
            for (BrokerSnapshotClientResponse.CashBalance cash : snapshot.cashBalances()) {
                brokerAccountCashBalanceEntryRepository.save(cashEntity(user.userId(), connectionId, cash));
            }
            List<BrokerCashBalance> cashBalances = snapshot.cashBalances().stream()
                    .map(PortfolioService::cashBalance)
                    .toList();
            PortfolioSummary summary = calculator.summarize(PortfolioMapper.toDomain(portfolio), activePositions(user.userId(), portfolioId), cashBalances);
            recordRealBrokerSnapshotIfNeeded(user.userId(), portfolioId, connectionId, snapshot, summary, Instant.now());
            eventPublisher.publish(BrokerSyncEvent.completed(connectionId, portfolioId, correlationId));
            eventPublisher.publish(new PortfolioUpdatedEvent(UUID.randomUUID(), correlationId, Instant.now(), portfolioId, user.userId()));
            return summary;
        } catch (RuntimeException exception) {
            eventPublisher.publish(BrokerSyncEvent.failed(connectionId, portfolioId, correlationId, exception.getClass().getSimpleName()));
            throw exception;
        } finally {
            ACTIVE_SYNCS.remove(portfolioId);
        }
    }

    /**
     * Performs the provider-neutral, account-partitioned import. The complete
     * broker snapshot is obtained before any database mutation; consequently a
     * provider failure cannot deactivate the last known-good positions.
     */
    @Transactional
    public List<PortfolioSummary> syncBrokerConnection(AuthenticatedUser user, UUID connectionId) {
        String syncKey = "broker:" + user.userId() + ":" + connectionId;
        UUID lockId = UUID.nameUUIDFromBytes(syncKey.getBytes(java.nio.charset.StandardCharsets.UTF_8));
        if (!ACTIVE_SYNCS.add(lockId)) {
            throw new IllegalStateException("Broker connection sync already in progress");
        }
        Instant startedAt = Instant.now();
        UUID syncGenerationId = UUID.randomUUID();
        try {
            BrokerConnectionClientResponse connection = restClient.post()
                    .uri(brokerServiceBaseUrl + "/api/v1/broker-connections/" + connectionId + "/sync")
                    .headers(headers -> addIdentity(headers, user))
                    .retrieve()
                    .body(BrokerConnectionClientResponse.class);
            if (connection == null) {
                throw new IllegalStateException("Broker connection status was empty");
            }
            if (!connection.connected()) {
                throw new BrokerSyncAuthenticationRequiredException(connection.status(), connection.lastErrorCode());
            }
            if (connection.capabilities() == null || !connection.capabilities().contains(BrokerCapability.PORTFOLIO_READ.name())) {
                throw new UnsupportedOperationException("Provider is not ready for safe portfolio synchronization");
            }
            BrokerSnapshotClientResponse snapshot = restClient.get()
                    .uri(brokerServiceBaseUrl + "/api/v1/broker-connections/" + connectionId + "/snapshot")
                    .headers(headers -> addIdentity(headers, user))
                    .retrieve()
                    .body(BrokerSnapshotClientResponse.class);
            validateCompleteSnapshot(snapshot);

            List<PortfolioSummary> summaries = new ArrayList<>();
            for (BrokerSnapshotClientResponse.Account account : snapshot.accounts()) {
                BrokerType brokerType = BrokerType.valueOf(account.brokerType());
                PortfolioEntity portfolio = resolveBrokerPortfolio(user.userId(), connectionId, brokerType, account,
                        connection.displayName());
                portfolio.recordBrokerSyncAttempt(startedAt);
                BrokerAccountEntity accountEntity = resolveBrokerAccountEntity(user.userId(), connectionId, brokerType,
                        account.brokerAccountId(), account.externalAccountReference(), account.displayName(),
                        normalizedCurrency(account.baseCurrency()), BrokerAccountStatus.valueOf(account.status()));
                List<BrokerSnapshotClientResponse.Position> accountPositions = snapshot.positions().stream()
                        .filter(position -> account.brokerAccountId().equals(position.brokerAccountId()))
                        .toList();
                for (BrokerSnapshotClientResponse.Position position : accountPositions) {
                    upsertBrokerPosition(user.userId(), portfolio, connectionId, brokerType, account, accountEntity,
                            position, syncGenerationId);
                }
                markMissingPositionsStale(user.userId(), portfolio.getPortfolioId(), connectionId,
                        account.brokerAccountId(), accountPositions, syncGenerationId);
                List<BrokerSnapshotClientResponse.CashBalance> accountCash = snapshot.cashBalances().stream()
                        .filter(cash -> account.brokerAccountId().equals(cash.brokerAccountId()))
                        .toList();
                accountCash.forEach(cash -> brokerAccountCashBalanceEntryRepository.save(
                        cashEntity(user.userId(), connectionId, cash)));
                PortfolioSummary summary = calculator.summarize(PortfolioMapper.toDomain(portfolio),
                        activePositions(user.userId(), portfolio.getPortfolioId()),
                        accountCash.stream().map(PortfolioService::cashBalance).toList());
                Instant completedAt = Instant.now();
                recordRealBrokerSnapshotIfNeeded(user.userId(), portfolio.getPortfolioId(), connectionId,
                        accountSnapshot(snapshot, account, accountPositions, accountCash), summary, completedAt);
                recordBrokerPositionHistory(user.userId(), portfolio.getPortfolioId(), connectionId,
                        account.brokerAccountId(), accountPositions, syncGenerationId, completedAt);
                portfolio.recordBrokerSyncSuccess(completedAt);
                summaries.add(summary);
                eventPublisher.publish(new PortfolioUpdatedEvent(UUID.randomUUID(), MDC.get("correlationId"),
                        Instant.now(), portfolio.getPortfolioId(), user.userId()));
            }
            int positionCount = summaries.stream().mapToInt(PortfolioSummary::numberOfPositions).sum();
            logger.info("brokerSync provider={} connectionId={} syncGenerationId={} syncResult={} portfolioCount={} positionCount={} durationMs={}",
                    connection.brokerType(), connectionId, syncGenerationId, "SUCCESS", summaries.size(), positionCount,
                    java.time.Duration.between(startedAt, Instant.now()).toMillis());
            return List.copyOf(summaries);
        } catch (RuntimeException exception) {
            logger.warn("brokerSync connectionId={} syncGenerationId={} syncResult={} errorCategory={} durationMs={}", connectionId,
                    syncGenerationId, "FAILED", exception.getClass().getSimpleName(),
                    java.time.Duration.between(startedAt, Instant.now()).toMillis());
            throw exception;
        } finally {
            ACTIVE_SYNCS.remove(lockId);
        }
    }

    @Transactional(readOnly = true)
    public PortfolioHistory getHistory(UUID userId, UUID portfolioId, String rangeCode) {
        Portfolio portfolio = getPortfolio(userId, portfolioId);
        PortfolioHistoryRange range = PortfolioHistoryRange.fromCode(rangeCode == null || rangeCode.isBlank() ? "1M" : rangeCode);
        Instant to = Instant.now();
        Instant from = range.from(to);
        List<PortfolioValuationPoint> raw = (from == null
                ? valuationSnapshotRepository.findByPortfolioIdAndUserIdOrderBySnapshotTimestampAsc(portfolioId, userId)
                : valuationSnapshotRepository.findByPortfolioIdAndUserIdAndSnapshotTimestampGreaterThanEqualOrderBySnapshotTimestampAsc(portfolioId, userId, from))
                .stream()
                .map(PortfolioMapper::toDomain)
                .sorted(Comparator.comparing(PortfolioValuationPoint::timestamp))
                .toList();
        List<PortfolioValuationPoint> sampled = downsample(raw, range.maxPoints());
        String investedCapitalStatus = sampled.stream()
                .anyMatch(point -> point.investedCapital() != null)
                ? "AVAILABLE"
                : "INVESTED_CAPITAL_HISTORY_UNAVAILABLE";
        Instant effectiveFrom = from == null
                ? sampled.stream().findFirst().map(PortfolioValuationPoint::timestamp).orElse(null)
                : from;
        return new PortfolioHistory(portfolioId, portfolio.baseCurrency(), range, effectiveFrom, to,
                investedCapitalStatus, false, sampled);
    }

    private PortfolioSummary doSync(UUID userId, UUID portfolioId, String correlationId) {
        PortfolioEntity portfolio = portfolioRepository.findByPortfolioIdAndUserId(portfolioId, userId)
                .orElseThrow(() -> new PortfolioNotFoundException(portfolioId));
        List<BrokerCashBalance> cashBalances = new ArrayList<>();
        UUID syncGenerationId = UUID.randomUUID();
        for (BrokerAccount account : brokerProvider.fetchAccounts(portfolio.getUserId())) {
            BrokerAccountEntity accountEntity = resolveBrokerAccountEntity(userId, MOCK_CONNECTION_ID, account.brokerType(),
                    account.brokerAccountId(), account.externalAccountReference(), account.displayName(),
                    account.baseCurrency(), account.status());
            List<BrokerCashBalance> accountCashBalances = brokerProvider.fetchCashBalances(account);
            cashBalances.addAll(accountCashBalances);
            for (BrokerCashBalance cashBalance : accountCashBalances) {
                brokerAccountCashBalanceEntryRepository.save(cashEntity(userId, MOCK_CONNECTION_ID, cashBalance));
            }
            List<BrokerPosition> accountPositions = brokerProvider.fetchPositions(account);
            for (BrokerPosition brokerPosition : accountPositions) {
                upsertBrokerPosition(userId, portfolio, MOCK_CONNECTION_ID, account.brokerType(), account, accountEntity,
                        brokerPosition, syncGenerationId, "DEMO");
            }
            markMissingBrokerPositionsStale(userId, portfolioId, MOCK_CONNECTION_ID, account.brokerAccountId(),
                    accountPositions.stream()
                            .map(BrokerPosition::instrument)
                            .map(com.aiinvestment.shared.domain.Instrument::providerInstrumentId)
                            .filter(value -> value != null && !value.isBlank())
                            .distinct()
                            .toList(),
                    syncGenerationId);
        }
        List<PortfolioPosition> positions = activePositions(userId, portfolioId);
        PortfolioSummary summary = calculator.summarize(PortfolioMapper.toDomain(portfolio), positions, cashBalances);
        eventPublisher.publish(new PortfolioUpdatedEvent(UUID.randomUUID(), correlationId, Instant.now(), portfolioId, portfolio.getUserId()));
        return summary;
    }

    private List<BrokerCashBalance> cashBalancesFor(Portfolio portfolio) {
        Set<String> portfolioBrokerAccountIds = positionRepository.findByPortfolioPortfolioId(portfolio.portfolioId()).stream()
                .filter(PortfolioPositionEntity::isActive)
                .map(position -> sourceScopeKey(position.getSourceConnectionId(),
                        position.getSourceBrokerAccountId() == null || position.getSourceBrokerAccountId().isBlank()
                                ? position.getBrokerAccount().getBrokerAccountId()
                                : position.getSourceBrokerAccountId()))
                .collect(java.util.stream.Collectors.toSet());
        List<BrokerCashBalance> persistedRealBrokerCash = brokerAccountCashBalanceEntryRepository.findByUserId(portfolio.userId()).stream()
                .filter(cash -> portfolioBrokerAccountIds.contains(sourceScopeKey(cash.getConnectionId(), cash.getBrokerAccountId())))
                .map(PortfolioMapper::toDomain)
                .toList();
        if (!persistedRealBrokerCash.isEmpty()) {
            return persistedRealBrokerCash;
        }
        return brokerAccountRepository.findAll().stream()
                .map(PortfolioMapper::toDomain)
                .filter(account -> account.userId().equals(portfolio.userId()))
                .filter(account -> account.brokerType() == BrokerType.MOCK)
                .flatMap(account -> brokerProvider.fetchCashBalances(account).stream())
                .toList();
    }

    private List<PortfolioPosition> activePositions(UUID userId, UUID portfolioId) {
        return getPositions(userId, portfolioId).stream()
                .filter(PortfolioPosition::active)
                .toList();
    }

    private void ensurePortfolioExists(UUID userId, UUID portfolioId) {
        if (!portfolioRepository.existsByPortfolioIdAndUserId(portfolioId, userId)) {
            throw new PortfolioNotFoundException(portfolioId);
        }
    }

    private static void addIdentity(HttpHeaders headers, AuthenticatedUser user) {
        headers.set(AuthenticationHeaders.USER_ID, user.userId().toString());
        headers.set(AuthenticationHeaders.ISSUER, user.issuer());
        headers.set(AuthenticationHeaders.SUBJECT, user.subject());
        if (user.email() != null) {
            headers.set(AuthenticationHeaders.EMAIL, user.email());
        }
        if (user.displayName() != null) {
            headers.set(AuthenticationHeaders.DISPLAY_NAME, user.displayName());
        }
        headers.set(AuthenticationHeaders.ROLES, String.join(",", user.roles()));
        String correlationId = CorrelationIdFilter.currentId();
        if (correlationId != null && !correlationId.isBlank()) {
            headers.set(CorrelationIdFilter.HEADER_NAME, correlationId);
            headers.set(CorrelationIdFilter.REQUEST_ID_HEADER_NAME, correlationId);
        }
    }

    private static boolean isRealBrokerSnapshot(BrokerSnapshotClientResponse snapshot) {
        return snapshot.positions().stream().anyMatch(position -> "REAL_BROKER".equals(position.dataFreshness()))
                || snapshot.cashBalances().stream().anyMatch(cash -> "REAL_BROKER".equals(cash.source()));
    }

    private static java.util.Optional<String> resolveImportedBaseCurrency(BrokerSnapshotClientResponse snapshot) {
        List<String> cashCurrencies = snapshot.cashBalances().stream()
                .filter(cash -> "REAL_BROKER".equals(cash.source()))
                .map(cash -> cash.cash().currency())
                .filter(currency -> currency != null && currency.matches("[A-Z]{3}"))
                .distinct()
                .toList();
        if (cashCurrencies.size() == 1) {
            return java.util.Optional.of(cashCurrencies.get(0));
        }
        List<String> accountCurrencies = snapshot.accounts().stream()
                .map(BrokerSnapshotClientResponse.Account::baseCurrency)
                .filter(currency -> currency != null && currency.matches("[A-Z]{3}"))
                .distinct()
                .toList();
        return accountCurrencies.size() == 1 ? java.util.Optional.of(accountCurrencies.get(0)) : java.util.Optional.empty();
    }

    private static BrokerCashBalance cashBalance(BrokerSnapshotClientResponse.CashBalance cash) {
        return new BrokerCashBalance(
                cash.brokerAccountId(),
                money(cash.cash()),
                money(cash.settledCash()),
                money(cash.netLiquidationValue()),
                money(cash.stockMarketValue()),
                money(cash.unrealizedPnl()),
                money(cash.realizedPnl()),
                cash.source());
    }

    private void upsertBrokerPosition(UUID userId, PortfolioEntity portfolio, UUID connectionId, BrokerType brokerType,
                                      BrokerSnapshotClientResponse.Account account, BrokerAccountEntity accountEntity,
                                      BrokerSnapshotClientResponse.Position brokerPosition, UUID syncGenerationId) {
        upsertBrokerPosition(userId, portfolio, connectionId, brokerType, account, accountEntity, brokerPosition,
                syncGenerationId, brokerPosition.dataFreshness());
    }

    private void upsertBrokerPosition(UUID userId, PortfolioEntity portfolio, UUID connectionId, BrokerType brokerType,
                                      BrokerSnapshotClientResponse.Account account, BrokerAccountEntity accountEntity,
                                      BrokerSnapshotClientResponse.Position brokerPosition, UUID syncGenerationId,
                                      String dataFreshness) {
        String externalProvider = requiredInstrumentIdentity(brokerPosition.instrument().provider(), "instrument.provider");
        String externalInstrumentId = requiredInstrumentIdentity(brokerPosition.instrument().providerInstrumentId(), "instrument.providerInstrumentId");
        if (brokerPosition.averageCost() == null || brokerPosition.currentPrice() == null) {
            upsertIncompleteHolding(userId, portfolio, connectionId, account, brokerPosition, syncGenerationId,
                    externalProvider, externalInstrumentId);
            return;
        }
        InstrumentEntity instrument = instrumentRepository
                .findByProviderAndProviderInstrumentId(externalProvider, externalInstrumentId)
                .map(existing -> {
                    existing.refreshBrokerMetadata(PortfolioMapper.toEntity(brokerPosition.instrument()));
                    return existing;
                })
                .orElseGet(() -> instrumentRepository.save(PortfolioMapper.toEntity(brokerPosition.instrument())));
        instrumentMaster.resolveAndAttach(instrument, externalProvider, externalInstrumentId,
                brokerPosition.instrument().brokerSymbol(), brokerPosition.instrument().brokerExchange(),
                "BROKER_API", new java.math.BigDecimal("0.99"));
        Instant observedAt = brokerPosition.observedAt() == null ? Instant.now() : brokerPosition.observedAt();
        PortfolioPositionEntity existing = positionRepository
                .findByPortfolioPortfolioIdAndPortfolioUserIdAndSourceTypeAndSourceConnectionIdAndSourceBrokerAccountIdAndExternalInstrumentProviderAndExternalInstrumentId(
                        portfolio.getPortfolioId(), userId, "BROKER", connectionId, account.brokerAccountId(),
                        externalProvider, externalInstrumentId)
                .orElseGet(() -> adoptableLegacyPosition(userId, portfolio, connectionId, brokerType, account,
                        accountEntity, externalProvider, externalInstrumentId));
        if (existing == null) {
            positionRepository.save(new PortfolioPositionEntity(
                    brokerPositionId(portfolio.getPortfolioId(), connectionId, account.brokerAccountId(), externalProvider, externalInstrumentId),
                    portfolio,
                    instrument,
                    brokerPosition.quantity(),
                    brokerPosition.averageCost().amount(),
                    brokerPosition.averageCost().currency(),
                    brokerPosition.currentPrice().amount(),
                    brokerPosition.currentPrice().currency(),
                    amount(brokerPosition.marketValue()),
                    currency(brokerPosition.marketValue()),
                    amount(brokerPosition.unrealizedProfitLoss()),
                    currency(brokerPosition.unrealizedProfitLoss()),
                    accountEntity,
                    "BROKER",
                    connectionId,
                    brokerType.name(),
                    account.brokerAccountId(),
                    externalProvider,
                    externalInstrumentId,
                    observedAt,
                    syncGenerationId,
                    true,
                    dataFreshness,
                    observedAt
            ));
            return;
        }
        existing.replaceBrokerValues(
                instrument,
                brokerPosition.quantity(),
                brokerPosition.averageCost().amount(),
                brokerPosition.averageCost().currency(),
                brokerPosition.currentPrice().amount(),
                brokerPosition.currentPrice().currency(),
                amount(brokerPosition.marketValue()),
                currency(brokerPosition.marketValue()),
                amount(brokerPosition.unrealizedProfitLoss()),
                currency(brokerPosition.unrealizedProfitLoss()),
                syncGenerationId,
                observedAt,
                dataFreshness);
    }

    private void upsertIncompleteHolding(UUID userId, PortfolioEntity portfolio, UUID connectionId,
                                         BrokerSnapshotClientResponse.Account account,
                                         BrokerSnapshotClientResponse.Position position, UUID generation,
                                         String provider, String externalId) {
        Instant observed = position.observedAt() == null ? Instant.now() : position.observedAt();
        var instrument = position.instrument();
        BrokerHoldingEntity holding = holdingRepository
                .findByUserIdAndConnectionIdAndBrokerAccountIdAndProviderAndProviderInstrumentId(
                        userId, connectionId, account.brokerAccountId(), provider, externalId)
                .orElse(null);
        if (holding == null) {
            holdingRepository.save(new BrokerHoldingEntity(
                    UUID.nameUUIDFromBytes((userId+"|"+connectionId+"|"+account.brokerAccountId()+"|"+provider+"|"+externalId).getBytes()),
                    userId, portfolio, connectionId, account.brokerAccountId(), provider, externalId,
                    instrument.isin(), instrument.ticker(), position.quantity(), amount(position.averageCost()),
                    amount(position.currentPrice()), amount(position.marketValue()),
                    currency(position.marketValue()) != null ? currency(position.marketValue()) : instrument.tradingCurrency(),
                    instrument.exchange(), observed, generation));
        } else {
            holding.replace(instrument.isin(), instrument.ticker(), position.quantity(), amount(position.averageCost()),
                    amount(position.currentPrice()), amount(position.marketValue()),
                    currency(position.marketValue()) != null ? currency(position.marketValue()) : instrument.tradingCurrency(),
                    instrument.exchange(), observed, generation);
        }
    }

    private BrokerAccountEntity resolveBrokerAccountEntity(UUID userId, UUID connectionId, BrokerType brokerType,
                                                           String sourceBrokerAccountId,
                                                           String externalAccountReference, String displayName,
                                                           String baseCurrency, BrokerAccountStatus status) {
        return brokerAccountRepository
                .findByUserIdAndConnectionIdAndBrokerTypeAndSourceBrokerAccountId(
                        userId, connectionId, brokerType, sourceBrokerAccountId)
                .map(existing -> {
                    existing.refreshBrokerMetadata(brokerType, sourceBrokerAccountId, externalAccountReference,
                            displayName, baseCurrency, status);
                    return existing;
                })
                .orElseGet(() -> adoptOrCreateBrokerAccount(userId, connectionId, brokerType, sourceBrokerAccountId,
                        externalAccountReference, displayName, baseCurrency, status));
    }

    private PortfolioEntity resolveBrokerPortfolio(UUID userId, UUID connectionId, BrokerType brokerType,
                                                   BrokerSnapshotClientResponse.Account account,
                                                   String connectionDisplayName) {
        return portfolioRepository.findByUserIdAndBrokerConnectionIdAndBrokerAccountId(
                        userId, connectionId, account.brokerAccountId())
                .orElseGet(() -> adoptOrCreateBrokerPortfolio(userId, connectionId, brokerType, account,
                        connectionDisplayName));
    }

    private PortfolioEntity adoptOrCreateBrokerPortfolio(UUID userId, UUID connectionId, BrokerType brokerType,
                                                         BrokerSnapshotClientResponse.Account account,
                                                         String connectionDisplayName) {
        List<PortfolioEntity> candidates = portfolioRepository.findByUserId(userId).stream()
                .filter(portfolio -> portfolio.getBrokerConnectionId() == null)
                .filter(portfolio -> {
                    List<PortfolioPositionEntity> positions = positionRepository
                            .findByPortfolioPortfolioId(portfolio.getPortfolioId());
                    return !positions.isEmpty() && positions.stream().allMatch(position ->
                            connectionId.equals(position.getSourceConnectionId())
                                    && account.brokerAccountId().equals(position.getSourceBrokerAccountId())
                                    && brokerType.name().equals(position.getExternalInstrumentProvider()));
                })
                .toList();
        if (candidates.size() > 1) {
            throw new IllegalStateException("Ambiguous legacy portfolio ownership for broker account");
        }
        if (candidates.size() == 1) {
            PortfolioEntity candidate = candidates.get(0);
            candidate.attachBrokerSource(connectionId, account.brokerAccountId(), brokerType);
            return candidate;
        }
        Instant now = Instant.now();
        String brokerName = connectionDisplayName == null || connectionDisplayName.isBlank()
                ? brokerDisplayName(brokerType) : connectionDisplayName;
        String accountName = account.displayName() == null || account.displayName().isBlank()
                ? "Account" : account.displayName();
        PortfolioEntity created = new PortfolioEntity(UUID.randomUUID(), userId,
                brokerName + " - " + accountName, normalizedCurrency(account.baseCurrency()), now, now);
        created.attachBrokerSource(connectionId, account.brokerAccountId(), brokerType);
        return portfolioRepository.save(created);
    }

    private static void validateCompleteSnapshot(BrokerSnapshotClientResponse snapshot) {
        if (snapshot == null || snapshot.accounts() == null || snapshot.positions() == null
                || snapshot.cashBalances() == null) {
            throw new IllegalStateException("Broker returned an incomplete authoritative snapshot");
        }
        for (BrokerSnapshotClientResponse.Account account : snapshot.accounts()) {
            if (account == null || account.brokerAccountId() == null || account.brokerAccountId().isBlank()
                    || account.brokerType() == null || account.brokerType().isBlank()) {
                throw new IllegalStateException("Broker returned malformed account metadata");
            }
        }
    }

    private static BrokerSnapshotClientResponse accountSnapshot(BrokerSnapshotClientResponse snapshot,
                                                                 BrokerSnapshotClientResponse.Account account,
                                                                 List<BrokerSnapshotClientResponse.Position> positions,
                                                                 List<BrokerSnapshotClientResponse.CashBalance> cash) {
        return new BrokerSnapshotClientResponse(snapshot.connection(), List.of(account), positions, cash);
    }

    private static String normalizedCurrency(String currency) {
        return currency != null && currency.matches("[A-Z]{3}") ? currency : "XXX";
    }

    private static String brokerDisplayName(BrokerType brokerType) {
        return switch (brokerType) {
            case IBKR -> "Interactive Brokers";
            case ICICI_DIRECT -> "ICICI Direct";
            case HDFC_SECURITIES -> "HDFC Securities";
            case MOCK -> "Demo Broker";
        };
    }

    private BrokerAccountEntity adoptOrCreateBrokerAccount(UUID userId, UUID connectionId, BrokerType brokerType,
                                                           String sourceBrokerAccountId,
                                                           String externalAccountReference, String displayName,
                                                           String baseCurrency, BrokerAccountStatus status) {
        List<BrokerAccountEntity> legacyCandidates = brokerAccountRepository
                .findByUserIdAndConnectionIdIsNullAndBrokerTypeAndSourceBrokerAccountId(
                        userId, brokerType, sourceBrokerAccountId);
        if (legacyCandidates.size() > 1) {
            throw new IllegalStateException("Ambiguous legacy broker account for " + brokerType + " account "
                    + sourceBrokerAccountId);
        }
        if (legacyCandidates.size() == 1) {
            BrokerAccountEntity legacy = legacyCandidates.get(0);
            legacy.adoptConnection(connectionId, brokerType, sourceBrokerAccountId, externalAccountReference,
                    displayName, baseCurrency, status);
            return legacy;
        }
        return brokerAccountRepository.save(new BrokerAccountEntity(
                brokerAccountKey(userId, connectionId, brokerType, sourceBrokerAccountId),
                userId, connectionId, brokerType, sourceBrokerAccountId, externalAccountReference, displayName,
                baseCurrency, status));
    }

    private PortfolioPositionEntity adoptableLegacyPosition(UUID userId, PortfolioEntity portfolio, UUID connectionId,
                                                            BrokerType brokerType,
                                                            BrokerSnapshotClientResponse.Account account,
                                                            BrokerAccountEntity accountEntity,
                                                            String externalProvider, String externalInstrumentId) {
        List<PortfolioPositionEntity> legacyCandidates = positionRepository
                .findByPortfolioPortfolioIdAndPortfolioUserIdAndSourceTypeAndSourceConnectionIdIsNullAndSourceBrokerAccountIdAndExternalInstrumentProviderAndExternalInstrumentId(
                        portfolio.getPortfolioId(), userId, "BROKER", account.brokerAccountId(), externalProvider,
                        externalInstrumentId);
        if (legacyCandidates.size() > 1) {
            throw new IllegalStateException("Ambiguous legacy broker position for portfolio "
                    + portfolio.getPortfolioId() + ", account " + account.brokerAccountId() + ", instrument "
                    + externalProvider + ":" + externalInstrumentId);
        }
        if (legacyCandidates.isEmpty()) {
            return null;
        }
        PortfolioPositionEntity legacy = legacyCandidates.get(0);
        legacy.adoptBrokerSource(connectionId, brokerType.name(), account.brokerAccountId(), externalProvider,
                externalInstrumentId, accountEntity);
        return legacy;
    }

    private void upsertBrokerPosition(UUID userId, PortfolioEntity portfolio, UUID connectionId, BrokerType brokerType,
                                      BrokerAccount account, BrokerAccountEntity accountEntity,
                                      BrokerPosition brokerPosition, UUID syncGenerationId, String dataFreshness) {
        BrokerSnapshotClientResponse.Account snapshotAccount = new BrokerSnapshotClientResponse.Account(
                account.brokerAccountId(), account.brokerType().name(), account.externalAccountReference(),
                account.displayName(), account.baseCurrency(), account.status().name());
        BrokerSnapshotClientResponse.Position snapshotPosition = new BrokerSnapshotClientResponse.Position(
                brokerPosition.brokerAccountId(), brokerPosition.instrument(), brokerPosition.quantity(),
                moneyValue(brokerPosition.averageCost()), moneyValue(brokerPosition.currentPrice()),
                moneyValue(brokerPosition.marketValue()), moneyValue(brokerPosition.unrealizedProfitLoss()),
                dataFreshness, brokerPosition.observedAt());
        upsertBrokerPosition(userId, portfolio, connectionId, brokerType, snapshotAccount, accountEntity,
                snapshotPosition, syncGenerationId, dataFreshness);
    }

    private void markMissingPositionsStale(UUID userId, UUID portfolioId, UUID connectionId, String brokerAccountId,
                                           List<BrokerSnapshotClientResponse.Position> accountPositions,
                                           UUID syncGenerationId) {
        List<String> activeExternalInstrumentIds = accountPositions.stream()
                .map(BrokerSnapshotClientResponse.Position::instrument)
                .map(com.aiinvestment.shared.domain.Instrument::providerInstrumentId)
                .filter(value -> value != null && !value.isBlank())
                .distinct()
                .toList();
        if (activeExternalInstrumentIds.isEmpty()) {
            markMissingBrokerPositionsStale(userId, portfolioId, connectionId, brokerAccountId, List.of(), syncGenerationId);
            return;
        }
        markMissingBrokerPositionsStale(userId, portfolioId, connectionId, brokerAccountId, activeExternalInstrumentIds,
                syncGenerationId);
    }

    private void markMissingBrokerPositionsStale(UUID userId, UUID portfolioId, UUID connectionId, String brokerAccountId,
                                                 List<String> activeExternalInstrumentIds, UUID syncGenerationId) {
        Set<String> activeIds = Set.copyOf(activeExternalInstrumentIds);
        positionRepository
                .findByPortfolioPortfolioIdAndPortfolioUserIdAndSourceTypeAndSourceConnectionIdAndSourceBrokerAccountIdAndActiveTrue(
                        portfolioId, userId, "BROKER", connectionId, brokerAccountId)
                .stream()
                .filter(position -> activeIds.isEmpty() || !activeIds.contains(position.getExternalInstrumentId()))
                .forEach(position -> position.markStale(syncGenerationId));
        holdingRepository.findByUserIdAndConnectionIdAndBrokerAccountIdAndActiveTrue(userId, connectionId, brokerAccountId)
                .stream().filter(holding -> activeIds.isEmpty() || !activeIds.contains(holding.getProviderInstrumentId()))
                .forEach(holding -> holding.markStale(syncGenerationId));
    }

    private void recordRealBrokerSnapshotIfNeeded(UUID userId, UUID portfolioId, UUID connectionId,
                                                  BrokerSnapshotClientResponse snapshot, PortfolioSummary summary,
                                                  Instant completedAt) {
        if (!isRealBrokerSnapshot(snapshot)) {
            return;
        }
        Instant snapshotTimestamp = snapshot.positions().stream()
                .map(BrokerSnapshotClientResponse.Position::observedAt)
                .filter(java.util.Objects::nonNull)
                .max(Instant::compareTo)
                .orElse(completedAt);
        String sourceSyncId = sourceSyncId(connectionId, snapshotTimestamp, summary);
        if (valuationSnapshotRepository.existsByPortfolioIdAndSourceSyncId(portfolioId, sourceSyncId)) {
            return;
        }
        Money positionsMarketValue = summary.totalMarketValue();
        Money portfolioMarketValue = summary.totalMarketValue().add(summary.cash());
        String broker = snapshot.accounts().stream()
                .map(BrokerSnapshotClientResponse.Account::brokerType)
                .filter(value -> value != null && !value.isBlank())
                .distinct()
                .reduce((left, right) -> left.equals(right) ? left : "MULTI_BROKER")
                .orElse("UNKNOWN");
        valuationSnapshotRepository.save(new PortfolioValuationSnapshotEntity(
                UUID.nameUUIDFromBytes((portfolioId + "|" + sourceSyncId).getBytes()),
                portfolioId,
                userId,
                snapshotTimestamp,
                summary.baseCurrency(),
                null,
                null,
                "INVESTED_CAPITAL_HISTORY_UNAVAILABLE",
                summary.cash().amount(),
                summary.cash().currency(),
                positionsMarketValue.amount(),
                positionsMarketValue.currency(),
                portfolioMarketValue.amount(),
                portfolioMarketValue.currency(),
                summary.unrealizedProfitLoss().amount(),
                summary.unrealizedProfitLoss().currency(),
                realizedPnlAmount(snapshot),
                summary.baseCurrency(),
                broker,
                "REAL_BROKER",
                "REAL_BROKER",
                sourceSyncId,
                completedAt));
    }

    private void recordBrokerPositionHistory(UUID userId, UUID portfolioId, UUID connectionId,
                                             String brokerAccountId,
                                             List<BrokerSnapshotClientResponse.Position> positions,
                                             UUID syncGenerationId, Instant completedAt) {
        for (BrokerSnapshotClientResponse.Position position : positions) {
            String identity = syncGenerationId + "|" + brokerAccountId + "|"
                    + position.instrument().provider() + "|" + position.instrument().providerInstrumentId();
            brokerPositionSnapshotRepository.save(new BrokerPositionSnapshotEntity(
                    UUID.nameUUIDFromBytes(identity.getBytes(java.nio.charset.StandardCharsets.UTF_8)),
                    syncGenerationId, userId, portfolioId, connectionId, brokerAccountId, position,
                    position.observedAt() == null ? completedAt : position.observedAt(), completedAt));
        }
    }

    private static String sourceSyncId(UUID connectionId, Instant snapshotTimestamp, PortfolioSummary summary) {
        return connectionId + "|" + snapshotTimestamp + "|" + summary.totalMarketValue().amount().toPlainString()
                + "|" + summary.cash().amount().toPlainString() + "|" + summary.baseCurrency();
    }

    private static java.math.BigDecimal realizedPnlAmount(BrokerSnapshotClientResponse snapshot) {
        return snapshot.cashBalances().stream()
                .map(BrokerSnapshotClientResponse.CashBalance::realizedPnl)
                .filter(java.util.Objects::nonNull)
                .map(BrokerSnapshotClientResponse.MoneyValue::amount)
                .reduce(java.math.BigDecimal.ZERO, java.math.BigDecimal::add);
    }

    private static List<PortfolioValuationPoint> downsample(List<PortfolioValuationPoint> raw, int maxPoints) {
        if (raw.size() <= maxPoints || maxPoints < 2) {
            return raw;
        }
        List<PortfolioValuationPoint> sampled = new ArrayList<>();
        double step = (double) (raw.size() - 1) / (double) (maxPoints - 1);
        int lastIndex = -1;
        for (int i = 0; i < maxPoints; i++) {
            int index = (int) Math.round(i * step);
            if (index != lastIndex) {
                sampled.add(raw.get(index));
                lastIndex = index;
            }
        }
        return sampled;
    }

    private static BrokerAccountCashBalanceEntryEntity cashEntity(UUID userId, UUID connectionId,
                                                                  BrokerSnapshotClientResponse.CashBalance cash) {
        return new BrokerAccountCashBalanceEntryEntity(
                cashBalanceId(userId, connectionId, cash.brokerAccountId(), cash.cash().currency()),
                userId,
                connectionId,
                cash.brokerAccountId(),
                cash.cash().amount(),
                cash.cash().currency(),
                amount(cash.settledCash()),
                currency(cash.settledCash()),
                amount(cash.netLiquidationValue()),
                currency(cash.netLiquidationValue()),
                amount(cash.stockMarketValue()),
                currency(cash.stockMarketValue()),
                amount(cash.unrealizedPnl()),
                currency(cash.unrealizedPnl()),
                amount(cash.realizedPnl()),
                currency(cash.realizedPnl()),
                cash.source() == null || cash.source().isBlank() ? "UNKNOWN" : cash.source(),
                Instant.now());
    }

    private static BrokerAccountCashBalanceEntryEntity cashEntity(UUID userId, UUID connectionId,
                                                                  BrokerCashBalance cash) {
        return new BrokerAccountCashBalanceEntryEntity(
                cashBalanceId(userId, connectionId, cash.brokerAccountId(), cash.cash().currency()),
                userId,
                connectionId,
                cash.brokerAccountId(),
                cash.cash().amount(),
                cash.cash().currency(),
                amount(cash.settledCash()),
                currency(cash.settledCash()),
                amount(cash.netLiquidationValue()),
                currency(cash.netLiquidationValue()),
                amount(cash.stockMarketValue()),
                currency(cash.stockMarketValue()),
                amount(cash.unrealizedPnl()),
                currency(cash.unrealizedPnl()),
                amount(cash.realizedPnl()),
                currency(cash.realizedPnl()),
                cash.source() == null || cash.source().isBlank() ? "UNKNOWN" : cash.source(),
                Instant.now());
    }

    private static BrokerSnapshotClientResponse.MoneyValue moneyValue(Money money) {
        return money == null ? null : new BrokerSnapshotClientResponse.MoneyValue(money.amount(), money.currency());
    }

    private static UUID brokerPositionId(UUID portfolioId, UUID connectionId, String brokerAccountId,
                                         String provider, String externalInstrumentId) {
        return UUID.nameUUIDFromBytes((portfolioId + "|" + connectionId + "|" + brokerAccountId + "|"
                + provider + "|" + externalInstrumentId).getBytes());
    }

    private static String brokerAccountKey(UUID userId, UUID connectionId, BrokerType brokerType, String brokerAccountId) {
        return brokerType.name() + "_" + UUID.nameUUIDFromBytes((userId + "|" + connectionId + "|"
                + brokerType + "|" + brokerAccountId).getBytes());
    }

    private static UUID cashBalanceId(UUID userId, UUID connectionId, String brokerAccountId, String currency) {
        return UUID.nameUUIDFromBytes((userId + "|" + connectionId + "|" + brokerAccountId + "|" + currency).getBytes());
    }

    private static String sourceScopeKey(UUID connectionId, String brokerAccountId) {
        return connectionId + "|" + brokerAccountId;
    }

    private static String requiredInstrumentIdentity(String value, String fieldName) {
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException(fieldName + " is required for broker position import");
        }
        return value;
    }

    private static com.aiinvestment.shared.domain.Money money(BrokerSnapshotClientResponse.MoneyValue value) {
        return value == null ? null : new com.aiinvestment.shared.domain.Money(value.amount(), value.currency());
    }

    private static java.math.BigDecimal amount(BrokerSnapshotClientResponse.MoneyValue value) {
        return value == null ? null : value.amount();
    }

    private static String currency(BrokerSnapshotClientResponse.MoneyValue value) {
        return value == null ? null : value.currency();
    }

    private static java.math.BigDecimal amount(Money value) {
        return value == null ? null : value.amount();
    }

    private static String currency(Money value) {
        return value == null ? null : value.currency();
    }
}
