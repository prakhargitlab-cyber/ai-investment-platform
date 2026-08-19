package com.aiinvestment.portfolio.application;

import com.aiinvestment.portfolio.domain.Portfolio;
import com.aiinvestment.portfolio.domain.PortfolioCalculator;
import com.aiinvestment.portfolio.domain.PortfolioPosition;
import com.aiinvestment.portfolio.domain.PortfolioSummary;
import com.aiinvestment.portfolio.infrastructure.persistence.*;
import com.aiinvestment.shared.domain.broker.*;
import com.aiinvestment.shared.domain.event.BrokerSyncEvent;
import com.aiinvestment.shared.domain.event.PortfolioUpdatedEvent;
import org.slf4j.MDC;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

@Service
public class PortfolioService {
    private static final UUID DEFAULT_PHASE1_USER = UUID.fromString("00000000-0000-0000-0000-000000000001");
    private static final UUID MOCK_CONNECTION_ID = UUID.nameUUIDFromBytes("mock-broker-connection".getBytes());
    private static final Set<UUID> ACTIVE_SYNCS = ConcurrentHashMap.newKeySet();

    private final PortfolioRepository portfolioRepository;
    private final BrokerAccountRepository brokerAccountRepository;
    private final InstrumentRepository instrumentRepository;
    private final PortfolioPositionRepository positionRepository;
    private final PortfolioCalculator calculator;
    private final BrokerProvider brokerProvider;
    private final PlatformEventPublisher eventPublisher;

    public PortfolioService(PortfolioRepository portfolioRepository,
                            BrokerAccountRepository brokerAccountRepository,
                            InstrumentRepository instrumentRepository,
                            PortfolioPositionRepository positionRepository,
                            PortfolioCalculator calculator,
                            PlatformEventPublisher eventPublisher,
                            List<BrokerProvider> brokerProviders) {
        this.portfolioRepository = portfolioRepository;
        this.brokerAccountRepository = brokerAccountRepository;
        this.instrumentRepository = instrumentRepository;
        this.positionRepository = positionRepository;
        this.calculator = calculator;
        this.eventPublisher = eventPublisher;
        this.brokerProvider = brokerProviders.stream()
                .filter(provider -> provider.supportedBroker() == BrokerType.MOCK)
                .findFirst()
                .orElseThrow(() -> new IllegalStateException("MockBrokerProvider is required for Phase 2B sync validation"));
    }

    @Transactional
    public Portfolio createPortfolio(String name, String baseCurrency) {
        Instant now = Instant.now();
        PortfolioEntity entity = new PortfolioEntity(UUID.randomUUID(), DEFAULT_PHASE1_USER, name, baseCurrency, now, now);
        return PortfolioMapper.toDomain(portfolioRepository.save(entity));
    }

    @Transactional(readOnly = true)
    public Portfolio getPortfolio(UUID portfolioId) {
        return portfolioRepository.findById(portfolioId)
                .map(PortfolioMapper::toDomain)
                .orElseThrow(() -> new PortfolioNotFoundException(portfolioId));
    }

    @Transactional(readOnly = true)
    public List<PortfolioSummary> listPortfolioSummaries() {
        return portfolioRepository.findByUserId(DEFAULT_PHASE1_USER).stream()
                .map(PortfolioMapper::toDomain)
                .map(portfolio -> calculator.summarize(portfolio, getPositions(portfolio.portfolioId()), cashBalancesFor(portfolio)))
                .toList();
    }

    @Transactional(readOnly = true)
    public List<PortfolioPosition> getPositions(UUID portfolioId) {
        ensurePortfolioExists(portfolioId);
        return positionRepository.findByPortfolioPortfolioId(portfolioId).stream()
                .map(PortfolioMapper::toDomain)
                .toList();
    }

    @Transactional(readOnly = true)
    public PortfolioSummary getSummary(UUID portfolioId) {
        Portfolio portfolio = getPortfolio(portfolioId);
        List<PortfolioPosition> positions = getPositions(portfolioId);
        return calculator.summarize(portfolio, positions, cashBalancesFor(portfolio));
    }

    @Transactional
    public PortfolioSummary sync(UUID portfolioId) {
        if (!ACTIVE_SYNCS.add(portfolioId)) {
            throw new IllegalStateException("Portfolio sync already in progress: " + portfolioId);
        }
        String correlationId = MDC.get("correlationId");
        eventPublisher.publish(BrokerSyncEvent.started(MOCK_CONNECTION_ID, portfolioId, correlationId));
        try {
            PortfolioSummary summary = doSync(portfolioId, correlationId);
            eventPublisher.publish(BrokerSyncEvent.completed(MOCK_CONNECTION_ID, portfolioId, correlationId));
            return summary;
        } catch (RuntimeException exception) {
            eventPublisher.publish(BrokerSyncEvent.failed(MOCK_CONNECTION_ID, portfolioId, correlationId, exception.getClass().getSimpleName()));
            throw exception;
        } finally {
            ACTIVE_SYNCS.remove(portfolioId);
        }
    }

    private PortfolioSummary doSync(UUID portfolioId, String correlationId) {
        PortfolioEntity portfolio = portfolioRepository.findById(portfolioId)
                .orElseThrow(() -> new PortfolioNotFoundException(portfolioId));
        positionRepository.deleteByPortfolioPortfolioId(portfolioId);
        List<BrokerCashBalance> cashBalances = new ArrayList<>();
        for (BrokerAccount account : brokerProvider.fetchAccounts(portfolio.getUserId())) {
            BrokerAccountEntity accountEntity = brokerAccountRepository.save(PortfolioMapper.toEntity(account));
            cashBalances.addAll(brokerProvider.fetchCashBalances(account));
            for (BrokerPosition brokerPosition : brokerProvider.fetchPositions(account)) {
                InstrumentEntity instrument = instrumentRepository.save(PortfolioMapper.toEntity(brokerPosition.instrument()));
                PortfolioPositionEntity position = new PortfolioPositionEntity(
                        UUID.nameUUIDFromBytes((portfolioId + "|" + account.brokerAccountId() + "|" + instrument.getInstrumentId()).getBytes()),
                        portfolio,
                        instrument,
                        brokerPosition.quantity(),
                        brokerPosition.averageCost().amount(),
                        brokerPosition.averageCost().currency(),
                        brokerPosition.currentPrice().amount(),
                        brokerPosition.currentPrice().currency(),
                        accountEntity,
                        brokerPosition.observedAt()
                );
                positionRepository.save(position);
            }
        }
        List<PortfolioPosition> positions = getPositions(portfolioId);
        PortfolioSummary summary = calculator.summarize(PortfolioMapper.toDomain(portfolio), positions, cashBalances);
        eventPublisher.publish(new PortfolioUpdatedEvent(UUID.randomUUID(), correlationId, Instant.now(), portfolioId, portfolio.getUserId()));
        return summary;
    }

    private List<BrokerCashBalance> cashBalancesFor(Portfolio portfolio) {
        return brokerAccountRepository.findAll().stream()
                .map(PortfolioMapper::toDomain)
                .filter(account -> account.userId().equals(portfolio.userId()))
                .flatMap(account -> brokerProvider.fetchCashBalances(account).stream())
                .toList();
    }

    private void ensurePortfolioExists(UUID portfolioId) {
        if (!portfolioRepository.existsById(portfolioId)) {
            throw new PortfolioNotFoundException(portfolioId);
        }
    }
}
