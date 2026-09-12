package com.aiinvestment.portfolio.infrastructure.persistence;

import jakarta.persistence.*;
import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "portfolio_import_history")
public class PortfolioImportHistoryEntity {
    @Id @Column(name = "import_id") private UUID importId;
    @Column(name = "user_id", nullable = false) private UUID userId;
    @Column(name = "portfolio_id", nullable = false) private UUID portfolioId;
    @Column(name = "broker_provider", nullable = false) private String brokerProvider;
    @Column(name = "source_account_reference", nullable = false) private String sourceAccountReference;
    @Column(name = "uploaded_filename", nullable = false) private String uploadedFilename;
    @Column(name = "imported_at", nullable = false) private Instant importedAt;
    @Column(name = "row_count", nullable = false) private int rowCount;
    @Column(name = "accepted_count", nullable = false) private int acceptedCount;
    @Column(name = "rejected_count", nullable = false) private int rejectedCount;
    @Column(name = "parser_type", nullable = false) private String parserType;
    @Column(name = "file_sha256") private String fileSha256;
    protected PortfolioImportHistoryEntity() {}
    public PortfolioImportHistoryEntity(UUID importId, UUID userId, UUID portfolioId, String brokerProvider,
                                        String sourceAccountReference, String uploadedFilename, Instant importedAt,
                                        int rowCount, int acceptedCount, int rejectedCount, String parserType,
                                        String fileSha256) {
        this.importId=importId; this.userId=userId; this.portfolioId=portfolioId; this.brokerProvider=brokerProvider;
        this.sourceAccountReference=sourceAccountReference; this.uploadedFilename=uploadedFilename;
        this.importedAt=importedAt; this.rowCount=rowCount; this.acceptedCount=acceptedCount;
        this.rejectedCount=rejectedCount; this.parserType=parserType; this.fileSha256=fileSha256;
    }
}
