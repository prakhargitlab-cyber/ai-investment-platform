package com.aiinvestment.portfolio.application;

import com.aiinvestment.portfolio.api.PortfolioImportPreviewResponse;
import com.aiinvestment.portfolio.api.PortfolioImportResultResponse;
import com.aiinvestment.portfolio.importing.*;
import com.aiinvestment.portfolio.infrastructure.persistence.*;
import com.aiinvestment.shared.domain.AssetType;
import com.aiinvestment.shared.domain.broker.BrokerAccountStatus;
import com.aiinvestment.shared.domain.broker.BrokerType;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.multipart.MultipartFile;

import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.HexFormat;
import java.util.List;
import java.util.Locale;
import java.util.UUID;

@Service
public class PortfolioImportService {
    static final int MAX_FILE_BYTES = 5 * 1024 * 1024;
    private final List<PortfolioImportParser> parsers;
    private final PortfolioRepository portfolios;
    private final BrokerAccountRepository accounts;
    private final InstrumentRepository instruments;
    private final PortfolioPositionRepository positions;
    private final PortfolioImportHistoryRepository history;
    private final ManualImportHoldingSnapshotRepository snapshots;
    private final InstrumentMasterService instrumentMaster;

    public PortfolioImportService(List<PortfolioImportParser> parsers, PortfolioRepository portfolios,
                                  BrokerAccountRepository accounts, InstrumentRepository instruments,
                                  PortfolioPositionRepository positions, PortfolioImportHistoryRepository history,
                                  ManualImportHoldingSnapshotRepository snapshots, InstrumentMasterService instrumentMaster) {
        this.parsers=parsers; this.portfolios=portfolios; this.accounts=accounts; this.instruments=instruments;
        this.positions=positions; this.history=history; this.snapshots=snapshots; this.instrumentMaster=instrumentMaster;
    }

    @Transactional(readOnly = true)
    public PortfolioImportPreviewResponse preview(UUID userId, BrokerType broker, MultipartFile file, String name) {
        Upload upload = validate(file);
        PortfolioImportParseResult parsed = parser(broker).parse(upload.filename(), upload.content(), name);
        boolean exists = portfolios.findByUserIdAndBrokerProviderAndAcquisitionSourceAndSourceAccountReference(
                userId, broker, "MANUAL_CSV_IMPORT", parsed.accountReference()).isPresent();
        return PortfolioImportPreviewResponse.from(parsed, exists);
    }

    @Transactional
    public PortfolioImportResultResponse confirm(UUID userId, BrokerType broker, MultipartFile file, String name) {
        Upload upload = validate(file);
        PortfolioImportParseResult parsed = parser(broker).parse(upload.filename(), upload.content(), name);
        if (parsed.holdings().isEmpty()) throw new IllegalArgumentException("The CSV contains no valid holdings");
        Instant now = Instant.now();
        var found = portfolios.findByUserIdAndBrokerProviderAndAcquisitionSourceAndSourceAccountReference(
                userId, broker, "MANUAL_CSV_IMPORT", parsed.accountReference());
        boolean existed = found.isPresent();
        PortfolioEntity portfolio = found.orElseGet(() -> portfolios.save(new PortfolioEntity(
                UUID.randomUUID(), userId, parsed.accountReference(), "INR", now, now)));
        String contentHash = sha256(upload.content());
        if (existed && history.existsByUserIdAndPortfolioIdAndBrokerProviderAndSourceAccountReferenceAndFileSha256(
                userId, portfolio.getPortfolioId(), broker.name(), parsed.accountReference(), contentHash)) {
            return new PortfolioImportResultResponse(portfolio.getPortfolioId(), parsed.accountReference(), broker.name(),
                    "MANUAL_CSV_IMPORT", portfolio.getLastImportedAt(), parsed.rowCount(), parsed.acceptedCount(),
                    parsed.rejectedCount(), true);
        }
        portfolio.attachManualImportSource(broker, parsed.accountReference(), now, upload.filename());

        String internalAccountId = "manual-" + UUID.nameUUIDFromBytes(
                (userId + ":" + broker + ":" + parsed.accountReference()).getBytes(StandardCharsets.UTF_8));
        BrokerAccountEntity account = accounts.findById(internalAccountId).orElseGet(() -> accounts.save(
                new BrokerAccountEntity(internalAccountId, userId, null, broker, parsed.accountReference(), null,
                        parsed.accountReference(), "INR", BrokerAccountStatus.ACTIVE)));
        UUID generation = UUID.randomUUID();
        Instant observedAt = parsed.sourceSnapshotAt() == null ? now : parsed.sourceSnapshotAt();
        var active = positions.findByPortfolioPortfolioIdAndPortfolioUserIdAndSourceTypeAndSourceConnectionIdIsNullAndSourceBrokerAccountIdAndActiveTrue(
                portfolio.getPortfolioId(), userId, "MANUAL_CSV_IMPORT", parsed.accountReference());
        for (ImportedHolding holding : parsed.holdings()) {
            InstrumentEntity candidate = new InstrumentEntity(UUID.randomUUID(), broker.name(), holding.securityKey(),
                    holding.isin(), holding.symbol() == null ? holding.isin() : holding.symbol(), "UNKNOWN", null,
                    holding.companyName(), AssetType.EQUITY, "IN", "INR", null, null,
                    holding.symbol(), holding.companyName(), "", holding.symbol(), holding.companyName(), "", null,
                    AssetType.EQUITY.name());
            InstrumentEntity instrument = instruments.findByProviderAndProviderInstrumentId(broker.name(), holding.securityKey())
                    .map(existing -> { existing.refreshBrokerMetadata(candidate); return existing; })
                    .orElseGet(() -> instruments.save(candidate));
            instrumentMaster.resolveAndAttach(instrument, broker.name(), holding.securityKey(), holding.symbol(), "UNKNOWN",
                    "MANUAL_CSV_IMPORT", new BigDecimal(holding.isin() == null ? "0.85" : "0.99"));
            var matching = positions.findByPortfolioPortfolioIdAndPortfolioUserIdAndSourceTypeAndSourceConnectionIdIsNullAndSourceBrokerAccountIdAndExternalInstrumentProviderAndExternalInstrumentId(
                    portfolio.getPortfolioId(), userId, "MANUAL_CSV_IMPORT", parsed.accountReference(), broker.name(), holding.securityKey());
            if (!matching.isEmpty()) {
                matching.get(0).replaceManualImportValues(instrument, holding.quantity(), holding.averageCost(), "INR",
                        holding.importedCurrentPrice(), holding.importedMarketValue(), holding.unrealizedPnl(),
                        holding.unrealizedPnlPercent(), generation, observedAt);
            } else {
                PortfolioPositionEntity created = new PortfolioPositionEntity(UUID.randomUUID(), portfolio, instrument, holding.quantity(),
                        holding.averageCost(), "INR", null, null, null, null,
                        null, null, account, "MANUAL_CSV_IMPORT", null, broker.name(),
                        parsed.accountReference(), broker.name(), holding.securityKey(), observedAt, generation, true,
                        "IMPORTED_SNAPSHOT", now);
                created.replaceManualImportValues(instrument, holding.quantity(), holding.averageCost(), "INR",
                        holding.importedCurrentPrice(), holding.importedMarketValue(), holding.unrealizedPnl(),
                        holding.unrealizedPnlPercent(), generation, observedAt);
                positions.save(created);
            }
        }
        active.stream().filter(position -> !generation.equals(position.getSyncGenerationId()))
                .forEach(position -> position.markStale(generation));

        UUID importId = UUID.randomUUID();
        history.save(new PortfolioImportHistoryEntity(importId, userId, portfolio.getPortfolioId(), broker.name(),
                parsed.accountReference(), upload.filename(), now, parsed.rowCount(), parsed.acceptedCount(),
                parsed.rejectedCount(), parsed.parserType(), contentHash));
        parsed.holdings().forEach(holding -> snapshots.save(new ManualImportHoldingSnapshotEntity(
                UUID.randomUUID(), importId, userId, portfolio.getPortfolioId(), holding, observedAt)));
        return new PortfolioImportResultResponse(portfolio.getPortfolioId(), parsed.accountReference(), broker.name(),
                "MANUAL_CSV_IMPORT", now, parsed.rowCount(), parsed.acceptedCount(), parsed.rejectedCount(), existed);
    }

    private PortfolioImportParser parser(BrokerType broker) {
        PortfolioImportParser parser = parsers.stream().filter(candidate -> candidate.brokerType() == broker)
                .findFirst().orElseThrow(() -> new IllegalArgumentException("Manual import is unavailable for this broker"));
        if (!parser.supported()) throw new IllegalArgumentException("The " + broker.name() + " portfolio file format is not yet supported");
        return parser;
    }

    private Upload validate(MultipartFile file) {
        if (file == null || file.isEmpty()) throw new IllegalArgumentException("A CSV file is required");
        if (file.getSize() > MAX_FILE_BYTES) throw new IllegalArgumentException("CSV file exceeds the 5 MB limit");
        String raw = file.getOriginalFilename() == null ? "" : file.getOriginalFilename().replace('\\', '/');
        String filename = raw.substring(raw.lastIndexOf('/') + 1).trim();
        if (!filename.matches("[A-Za-z0-9][A-Za-z0-9 ._()\\-]{0,254}\\.csv"))
            throw new IllegalArgumentException("A safe .csv filename is required");
        try {
            byte[] content = file.getBytes();
            if (content.length >= 2 && ((content[0]=='M' && content[1]=='Z') || (content[0]=='P' && content[1]=='K')))
                throw new IllegalArgumentException("Executable or archive content is not accepted");
            for (byte value : content) if (value == 0) throw new IllegalArgumentException("Binary content is not accepted");
            return new Upload(filename, content);
        } catch (java.io.IOException exception) {
            throw new IllegalArgumentException("Unable to read uploaded CSV", exception);
        }
    }

    private String sha256(byte[] content) {
        try { return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(content)); }
        catch (Exception exception) { throw new IllegalStateException(exception); }
    }
    private record Upload(String filename, byte[] content) {}
}
