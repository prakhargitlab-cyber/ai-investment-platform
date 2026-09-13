package com.aiinvestment.portfolio.application;

import com.aiinvestment.portfolio.infrastructure.persistence.*;
import org.slf4j.Logger; import org.slf4j.LoggerFactory;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.ConnectionCallback;
import org.springframework.context.ApplicationEventPublisher;
import java.math.BigDecimal; import java.time.Instant; import java.util.*;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;

@Service
public class InstrumentMasterService {
    private static final Logger log=LoggerFactory.getLogger(InstrumentMasterService.class);
    private static final List<String> REUSABLE=List.of("VERIFIED");
    private final InstrumentMasterRepository masters; private final InstrumentProviderMappingRepository mappings;
    private final InstrumentRepository legacy;
    private final JdbcTemplate jdbc;
    private final ApplicationEventPublisher events;
    public InstrumentMasterService(InstrumentMasterRepository masters,InstrumentProviderMappingRepository mappings,
            InstrumentRepository legacy,JdbcTemplate jdbc, ApplicationEventPublisher events){
        this.masters=masters;this.mappings=mappings;this.legacy=legacy;this.jdbc=jdbc;this.events=events;
    }

    @Transactional
    public InstrumentMasterEntity resolveAndAttach(InstrumentEntity row,String provider,String providerId,String symbol,
            String exchange,String source,BigDecimal confidence){
        String isin=InstrumentMasterEntity.normalizeIsin(row.getIsin());
        lockIdentity(isin!=null?"ISIN:"+isin:providerId!=null&&!providerId.isBlank()
                ?"PROVIDER:"+upper(provider)+":"+upper(providerId)
                :"LISTING:"+upper(provider)+":"+normalizeExchange(exchange)+":"+upper(symbol));
        Optional<InstrumentMasterEntity> found=isin==null?Optional.empty():masters.findByNormalizedIsin(isin);
        if(found.isEmpty() && providerId!=null && !providerId.isBlank())
            found=mappings.findByProviderAndProviderInstrumentId(provider,providerId).flatMap(m->masters.findById(m.getInstrumentId()));
        if(found.isEmpty() && symbol!=null && !symbol.isBlank() && exchange!=null && !exchange.isBlank())
            found=mappings.findByProviderAndExchangeIgnoreCaseAndProviderSymbolIgnoreCase(provider,exchange,symbol)
                    .flatMap(m->masters.findById(m.getInstrumentId()));
        InstrumentMasterEntity master=found.orElseGet(()->createConcurrentlySafe(row,isin,symbol,exchange));
        persistMapping(master.getInstrumentId(),provider,symbol,providerId,exchange,row.getTradingCurrency(),
                confidence.compareTo(new BigDecimal("0.90"))>=0?"VERIFIED":"RESOLVED",source,confidence);
        row.attachMaster(master.getInstrumentId()); legacy.save(row);
        events.publishEvent(new GlobalInstrumentAttachedEvent(master.getInstrumentId()));
        log.info("instrument_master_lookup keyType={} found={} instrumentId={} provider={} mappingStatus={} resolutionAttempted=false confidence={}",
                isin!=null?"ISIN":providerId!=null?"PROVIDER_ID":"EXCHANGE_SYMBOL",found.isPresent(),master.getInstrumentId(),provider,
                confidence.compareTo(new BigDecimal("0.90"))>=0?"VERIFIED":"RESOLVED",confidence);
        return master;
    }

    @Transactional(readOnly=true)
    public Optional<InstrumentProviderMappingEntity> reusableMapping(UUID instrumentId,String provider){
        return mappings.findFirstByInstrumentIdAndProviderAndStatusIn(instrumentId,provider,REUSABLE);
    }
    @Transactional
    public UUID ensureMaster(UUID presentedInstrumentId) {
        if (masters.existsById(presentedInstrumentId)) return presentedInstrumentId;
        InstrumentEntity row=legacy.findById(presentedInstrumentId).orElseThrow(()->new IllegalArgumentException("Instrument not found"));
        if(row.getMasterInstrumentId()!=null)return row.getMasterInstrumentId();
        return resolveAndAttach(row,row.getProvider(),row.getProviderInstrumentId(),row.getBrokerSymbol(),
                row.getBrokerExchange()==null||row.getBrokerExchange().isBlank()?row.getExchange():row.getBrokerExchange(),
                "LEGACY_ADOPTION",new BigDecimal(row.getIsin()==null?"0.85":"0.99")).getInstrumentId();
    }
    @Transactional(readOnly=true) public List<InstrumentProviderMappingEntity> mappings(UUID id){return mappings.findByInstrumentId(id);}

    @Transactional(readOnly=true)
    public Optional<GlobalInstrument> globalInstrument(UUID instrumentId) {
        return masters.findById(instrumentId)
                .map(master -> new GlobalInstrument(master, mappings.findByInstrumentId(instrumentId)));
    }

    @Transactional(readOnly=true)
    public Map<UUID, List<InstrumentProviderMappingEntity>> searchMappings(Collection<UUID> instrumentIds) {
        if (instrumentIds.isEmpty()) return Map.of();
        return mappings.findByInstrumentIdIn(instrumentIds).stream()
                .filter(mapping -> "VERIFIED".equalsIgnoreCase(mapping.getStatus()))
                .collect(java.util.stream.Collectors.groupingBy(InstrumentProviderMappingEntity::getInstrumentId));
    }

    public record GlobalInstrument(InstrumentMasterEntity master, List<InstrumentProviderMappingEntity> providerMappings) {}

    /** Platform keys are independent of provider symbols; catalog evidence is versioned. */
    public static final String BENCHMARK_CATALOG_VERSION = "NSE_BENCHMARK_CATALOG_V1";
    public static final Map<String, String> BENCHMARKS = Map.of(
            "INDIA_BROAD_PRICE", "NIFTY 500", "INDIA_TECHNOLOGY_PRICE", "NIFTY IT",
            "INDIA_FINANCIALS_PRICE", "NIFTY FINANCIAL SERVICES", "INDIA_HEALTHCARE_PRICE", "NIFTY HEALTHCARE INDEX");

    public static UUID benchmarkId(String key) {
        if (!BENCHMARKS.containsKey(key)) throw new IllegalArgumentException("UNKNOWN_BENCHMARK_KEY");
        return UUID.nameUUIDFromBytes(("aip:benchmark:" + key).getBytes(java.nio.charset.StandardCharsets.UTF_8));
    }

    @Transactional
    public GlobalInstrument registerBenchmark(String key) {
        UUID id = benchmarkId(key);
        String symbol = BENCHMARKS.get(key);
        lockIdentity("BENCHMARK:" + key);
        var bySymbol = mappings.findByProviderAndExchangeIgnoreCaseAndProviderSymbolIgnoreCase("NSE", "NSE", symbol);
        if (bySymbol.isPresent() && !id.equals(bySymbol.get().getInstrumentId()))
            throw new IllegalStateException("BENCHMARK_IDENTITY_CONFLICT");
        var existing = masters.findById(id);
        var currentNse = mappings.findByInstrumentId(id).stream().filter(m -> "NSE".equals(m.getProvider())).toList();
        if (currentNse.size() > 1 || currentNse.stream().anyMatch(m -> !symbol.equals(m.getProviderSymbol())))
            throw new IllegalStateException("BENCHMARK_IDENTITY_CONFLICT");
        if (existing.isPresent() && (existing.get().getAssetType() != com.aiinvestment.shared.domain.AssetType.INDEX
                || !"ACTIVE".equals(existing.get().getStatus()) || !"INR".equals(existing.get().getCurrency())
                || !"NSE".equals(existing.get().getPrimaryExchange()) || !"IN".equals(existing.get().getCountry())
                || !symbol.equals(existing.get().getPrimarySymbol())))
            throw new IllegalStateException("BENCHMARK_IDENTITY_CONFLICT");
        InstrumentMasterEntity master = existing.orElseGet(() -> masters.saveAndFlush(new InstrumentMasterEntity(id, null,
                symbol, com.aiinvestment.shared.domain.AssetType.INDEX, "INR", "IN", "NSE", symbol, "ACTIVE", Instant.now())));
        persistMapping(id, "NSE", symbol, null, "NSE", "INR", "VERIFIED", BENCHMARK_CATALOG_VERSION, new BigDecimal("0.99"));
        return new GlobalInstrument(master, mappings.findByInstrumentId(id));
    }

    @Transactional(readOnly=true)
    public List<GlobalInstrument> registeredBenchmarks() {
        var ids = BENCHMARKS.keySet().stream().sorted().map(InstrumentMasterService::benchmarkId).toList();
        var rows = masters.findAllById(ids);
        var byId = mappings.findByInstrumentIdIn(ids).stream()
                .collect(java.util.stream.Collectors.groupingBy(InstrumentProviderMappingEntity::getInstrumentId));
        return rows.stream().sorted(Comparator.comparing(row -> row.getInstrumentId().toString()))
                .map(row -> new GlobalInstrument(row, byId.getOrDefault(row.getInstrumentId(), List.of()))).toList();
    }

    @Transactional(readOnly=true)
    public Page<InstrumentMasterEntity> enumerate(String status, com.aiinvestment.shared.domain.AssetType assetType, Pageable pageable) {
        if (status == null || assetType == null) throw new IllegalArgumentException("STATUS_AND_ASSET_TYPE_REQUIRED");
        return masters.findByStatusIgnoreCaseAndAssetType(status, assetType, pageable);
    }

    /** Canonicalize an exact official NSE identity; never uses a name-only match. */
    @Transactional
    public InstrumentMasterEntity canonicalizeOfficialNse(String isin, String symbol, String companyName) {
        return canonicalizeOfficialNse(isin, symbol, companyName, "OFFICIAL_NSE_NIFTY500");
    }

    @Transactional
    public InstrumentMasterEntity canonicalizeOfficialNse(String isin, String symbol, String companyName, String source) {
        String normalized = InstrumentMasterEntity.normalizeIsin(isin);
        if (normalized == null || symbol == null || symbol.isBlank()) throw new IllegalArgumentException("OFFICIAL_NSE_IDENTITY_REQUIRED");
        lockIdentity("ISIN:" + normalized);
        Optional<InstrumentMasterEntity> existing = masters.findByNormalizedIsin(normalized);
        if (existing.isEmpty()) existing = mappings.findByProviderAndExchangeIgnoreCaseAndProviderSymbolIgnoreCase("NSE", "NSE", symbol)
                .flatMap(mapping -> masters.findById(mapping.getInstrumentId()));
        if (existing.isPresent() && existing.get().getIsin() != null && !normalized.equals(existing.get().getIsin()))
            throw new IllegalStateException("NSE_IDENTITY_MISMATCH");
        InstrumentMasterEntity master = existing.orElseGet(() -> masters.saveAndFlush(new InstrumentMasterEntity(UUID.randomUUID(), normalized,
                firstText(companyName, symbol), com.aiinvestment.shared.domain.AssetType.EQUITY, "INR", "IN", "NSE", symbol, "ACTIVE", Instant.now())));
        master.applyVerifiedPrimaryListing("NSE", symbol, companyName);
        var verified = reusableMapping(master.getInstrumentId(), "NSE");
        if (verified.isPresent() && !same(verified.get().getProviderSymbol(), symbol))
            throw new IllegalStateException("NSE_IDENTITY_MISMATCH");
        if (verified.isEmpty()) persistMapping(master.getInstrumentId(), "NSE", symbol, normalized, "NSE", "INR", "VERIFIED", source, new BigDecimal("0.99"));
        return master;
    }

    @Transactional
    public void applyValidatedAssetType(UUID instrumentId, String providerQuoteType) {
        com.aiinvestment.shared.domain.AssetType type = switch (String.valueOf(providerQuoteType).toUpperCase()) {
            case "ETF" -> com.aiinvestment.shared.domain.AssetType.ETF;
            case "EQUITY", "STOCK" -> com.aiinvestment.shared.domain.AssetType.EQUITY;
            default -> null;
        };
        if (type != null) masters.findById(instrumentId).ifPresent(master -> master.applyValidatedAssetType(type));
    }

    @Transactional
    public InstrumentProviderMappingEntity saveResolvedMapping(UUID instrumentId,String provider,String symbol,String providerId,
            String exchange,String currency,String status,String source,BigDecimal confidence){
        if(!masters.existsById(instrumentId)) throw new IllegalArgumentException("Canonical instrument not found");
        Optional<InstrumentProviderMappingEntity> durable=reusableMapping(instrumentId,provider);
        if(durable.isPresent()) {
            if (!same(durable.get().getProviderSymbol(),symbol)
                    || !same(durable.get().getExchange(),normalizeExchange(exchange))
                    || !same(durable.get().getCurrency(),currency))
                throw new IllegalStateException("PROVIDER_MAPPING_REVALIDATION_REQUIRED");
            if ("VERIFIED".equalsIgnoreCase(status)) {
                durable.get().markVerified(upper(symbol), providerId, normalizeExchange(exchange), currency, source, confidence,
                        Instant.now());
            }
            return durable.get();
        }
        return persistMapping(instrumentId,provider,symbol,providerId,exchange,currency,status,source,confidence);
    }

    @Transactional
    public InstrumentProviderMappingEntity saveVerifiedExternalMapping(UUID instrumentId, String provider, String symbol,
            String providerId, String exchange, String currency, String source, BigDecimal confidence) {
        if (provider == null || provider.isBlank() || providerId == null || providerId.isBlank()
                || source == null || source.isBlank() || confidence == null || confidence.compareTo(new BigDecimal("0.90")) < 0)
            throw new IllegalArgumentException("VERIFIED_MAPPING_REQUIRED");
        return saveResolvedMapping(instrumentId, provider, symbol, providerId, exchange, currency,
                "VERIFIED", source, confidence);
    }

    @Transactional
    public void rejectMapping(UUID instrumentId,String provider,String reason){
        reusableMapping(instrumentId,provider).ifPresent(mapping->mapping.markInvalid(reason,Instant.now()));
    }

    /** Records a failed authoritative validation without making the mapping reusable. */
    @Transactional
    public InstrumentProviderMappingEntity recordMappingFailure(UUID instrumentId, String provider, String symbol,
            String providerId, String exchange, String currency, String source, BigDecimal confidence, String reason) {
        if (!masters.existsById(instrumentId)) throw new IllegalArgumentException("Canonical instrument not found");
        Optional<InstrumentProviderMappingEntity> existing = providerId == null || providerId.isBlank() ? Optional.empty()
                : mappings.findByProviderAndProviderInstrumentId(provider, providerId);
        if (existing.isEmpty() && symbol != null && !symbol.isBlank() && exchange != null && !exchange.isBlank())
            existing = mappings.findByProviderAndExchangeIgnoreCaseAndProviderSymbolIgnoreCase(provider, normalizeExchange(exchange), symbol);
        Instant now = Instant.now();
        if (existing.isPresent()) {
            if (!existing.get().getInstrumentId().equals(instrumentId)) throw new IllegalStateException("AMBIGUOUS_PROVIDER_MAPPING");
            existing.get().markInvalid(reason, now);
            return existing.get();
        }
        InstrumentProviderMappingEntity created = new InstrumentProviderMappingEntity(UUID.randomUUID(), instrumentId,
                provider, upper(symbol), providerId, normalizeExchange(exchange), currency, "INVALID", source, confidence, now);
        created.markInvalid(reason, now);
        return mappings.save(created);
    }

    private InstrumentMasterEntity createConcurrentlySafe(InstrumentEntity row,String isin,String symbol,String exchange){
        Instant now=Instant.now();
        try{return masters.saveAndFlush(new InstrumentMasterEntity(UUID.randomUUID(),isin,
                firstText(row.getCanonicalName(),row.getCompanyName(),symbol),row.getAssetType(),row.getTradingCurrency(),
                row.getCountry(),normalizeExchange(exchange),symbol,"ACTIVE",now));}
        catch(DataIntegrityViolationException conflict){
            if(isin!=null)return masters.findByNormalizedIsin(isin).orElseThrow(()->conflict);
            throw conflict;
        }
    }
    private InstrumentProviderMappingEntity persistMapping(UUID id,String provider,String symbol,String providerId,String exchange,
            String currency,String status,String source,BigDecimal confidence){
        Optional<InstrumentProviderMappingEntity> existing=providerId==null||providerId.isBlank()?Optional.empty():mappings.findByProviderAndProviderInstrumentId(provider,providerId);
        if(existing.isEmpty() && symbol!=null && !symbol.isBlank() && exchange!=null && !exchange.isBlank())
            existing=mappings.findByProviderAndExchangeIgnoreCaseAndProviderSymbolIgnoreCase(provider,normalizeExchange(exchange),symbol);
        if(existing.isPresent()){
            if(!existing.get().getInstrumentId().equals(id)) throw new IllegalStateException("AMBIGUOUS_PROVIDER_MAPPING");
            if ("VERIFIED".equalsIgnoreCase(status)) {
                existing.get().markVerified(upper(symbol), providerId, normalizeExchange(exchange), currency, source, confidence,
                        Instant.now());
            }
            return existing.get();
        }
        return mappings.save(new InstrumentProviderMappingEntity(UUID.randomUUID(),id,provider,upper(symbol),providerId,
                normalizeExchange(exchange),currency,status,source,confidence,Instant.now()));
    }
    private static String normalizeExchange(String value){return value==null?null:value.trim().toUpperCase();}
    private static String upper(String value){return value==null?null:value.trim().toUpperCase();}
    private static boolean same(String left,String right){return Objects.equals(upper(left),upper(right));}
    private static String firstText(String... values){return Arrays.stream(values).filter(v->v!=null&&!v.isBlank()).findFirst().orElse("Unresolved instrument");}
    private void lockIdentity(String key){
        Boolean postgres=jdbc.execute((ConnectionCallback<Boolean>) connection ->
                connection.getMetaData().getDatabaseProductName().equalsIgnoreCase("PostgreSQL"));
        if(Boolean.TRUE.equals(postgres)) jdbc.queryForObject("SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",Object.class,key);
    }
}
