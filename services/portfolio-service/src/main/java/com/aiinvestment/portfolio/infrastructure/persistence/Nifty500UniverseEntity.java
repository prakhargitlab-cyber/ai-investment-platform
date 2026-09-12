package com.aiinvestment.portfolio.infrastructure.persistence;
import jakarta.persistence.*; import java.time.Instant; import java.util.UUID;
@Entity @Table(name="nifty500_universe") public class Nifty500UniverseEntity {
 @Id @Column(name="instrument_id") private UUID instrumentId; @Column(nullable=false) private String symbol; @Column(nullable=false) private String isin;
 @Column(name="company_name",nullable=false) private String companyName; @Column(nullable=false) private String industry; @Column(name="canonical_sector") private String canonicalSector; @Column(nullable=false) private String source; @Column(name="retrieved_at",nullable=false) private Instant retrievedAt;
 protected Nifty500UniverseEntity(){} public Nifty500UniverseEntity(UUID id,String s,String i,String c,String industry,String sector,Instant at){instrumentId=id;symbol=s;isin=i;companyName=c;this.industry=industry;canonicalSector=sector;source="NSE_INDICES_NIFTY500";retrievedAt=at;}
 public UUID getInstrumentId(){return instrumentId;} public String getSymbol(){return symbol;} public String getIsin(){return isin;} public String getCompanyName(){return companyName;} public String getIndustry(){return industry;} public String getCanonicalSector(){return canonicalSector;} public String getSource(){return source;} public Instant getRetrievedAt(){return retrievedAt;}
}
