package com.aiinvestment.portfolio.api;

import com.aiinvestment.portfolio.application.InstrumentMasterService;
import com.aiinvestment.portfolio.application.GlobalInstrumentReconciliationService;
import com.aiinvestment.portfolio.infrastructure.persistence.AppUserProvisioner;
import com.aiinvestment.shared.web.auth.AuthenticatedUserResolver;
import io.swagger.v3.oas.annotations.Operation;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.data.domain.PageRequest;
import org.springframework.data.domain.Sort;
import com.aiinvestment.shared.domain.AssetType;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;

import java.util.UUID;
import java.math.BigDecimal;

@RestController
@RequestMapping("/api/v1/instruments")
public class GlobalInstrumentController {
    private final InstrumentMasterService instrumentMasterService;
    private final GlobalInstrumentReconciliationService reconciliationService;
    private final AppUserProvisioner appUserProvisioner;

    public GlobalInstrumentController(InstrumentMasterService instrumentMasterService,
            GlobalInstrumentReconciliationService reconciliationService, AppUserProvisioner appUserProvisioner) {
        this.instrumentMasterService = instrumentMasterService;
        this.reconciliationService = reconciliationService;
        this.appUserProvisioner = appUserProvisioner;
    }

    @GetMapping("/{globalInstrumentId}")
    @Operation(summary = "Read public global instrument metadata and provider mappings")
    public GlobalInstrumentResponse get(@PathVariable("globalInstrumentId") UUID globalInstrumentId, HttpServletRequest servletRequest) {
        // Follow the service-wide authenticated-request convention, without
        // applying portfolio ownership because this resource is global/public.
        appUserProvisioner.upsert(AuthenticatedUserResolver.require(servletRequest));
        return instrumentMasterService.globalInstrument(globalInstrumentId)
                .map(GlobalInstrumentResponse::from)
                .orElseThrow(() -> new ResponseStatusException(HttpStatus.NOT_FOUND, "Global instrument not found"));
    }

    @GetMapping("/benchmarks")
    public java.util.List<GlobalInstrumentResponse> benchmarks(HttpServletRequest request) {
        appUserProvisioner.upsert(AuthenticatedUserResolver.require(request));
        return instrumentMasterService.registeredBenchmarks().stream().map(GlobalInstrumentResponse::from).toList();
    }

    @PostMapping("/benchmarks/{benchmarkKey}/register")
    public GlobalInstrumentResponse registerBenchmark(@PathVariable String benchmarkKey, HttpServletRequest request) {
        appUserProvisioner.upsert(AuthenticatedUserResolver.require(request));
        try {
            return GlobalInstrumentResponse.from(instrumentMasterService.registerBenchmark(benchmarkKey));
        } catch (IllegalArgumentException e) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "UNKNOWN_BENCHMARK_KEY");
        } catch (IllegalStateException e) {
            throw new ResponseStatusException(HttpStatus.CONFLICT, "BENCHMARK_IDENTITY_CONFLICT");
        }
    }

    @GetMapping
    public InstrumentUniverseResponse enumerate(@RequestParam(name = "status", defaultValue = "ACTIVE") String status,
            @RequestParam(name = "assetType", defaultValue = "EQUITY") AssetType assetType,
            @RequestParam(name = "page", defaultValue = "0") int page,
            @RequestParam(name = "size", defaultValue = "100") int size, HttpServletRequest servletRequest) {
        appUserProvisioner.upsert(AuthenticatedUserResolver.require(servletRequest));
        var values = instrumentMasterService.enumerate(status, assetType, PageRequest.of(Math.max(0, page), Math.min(500, Math.max(1, size)), Sort.by("primarySymbol").ascending().and(Sort.by("instrumentId"))));
        var mappings = instrumentMasterService.searchMappings(values.getContent().stream().map(value -> value.getInstrumentId()).toList());
        return new InstrumentUniverseResponse(values.getContent().stream().map(value -> InstrumentUniverseItem.from(value,
                mappings.getOrDefault(value.getInstrumentId(), java.util.List.of()))).toList(), values.getNumber(), values.getSize(), values.getTotalElements());
    }

    public record InstrumentUniverseResponse(java.util.List<InstrumentUniverseItem> instruments, int page, int size, long totalElements) {}
    public record InstrumentUniverseItem(UUID globalInstrumentId, String canonicalName, String ticker, String exchange,
            String country, String currency, String status, AssetType assetType, String isin,
            java.util.List<GlobalInstrumentResponse.GlobalInstrumentProviderMappingResponse> providerMappings) {
        static InstrumentUniverseItem from(com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterEntity value,
                java.util.List<com.aiinvestment.portfolio.infrastructure.persistence.InstrumentProviderMappingEntity> mappings) {
            return new InstrumentUniverseItem(value.getInstrumentId(), value.getCanonicalName(), value.getPrimarySymbol(),
                    value.getPrimaryExchange(), value.getCountry(), value.getCurrency(), value.getStatus(), value.getAssetType(),
                    value.getIsin(), mappings.stream().map(GlobalInstrumentResponse.GlobalInstrumentProviderMappingResponse::from).toList());
        }
    }

    @PostMapping("/{globalInstrumentId}/reconcile")
    @Operation(summary = "Reconcile public global instrument provider mappings")
    public GlobalInstrumentResponse reconcile(@PathVariable("globalInstrumentId") UUID globalInstrumentId,
            HttpServletRequest servletRequest) {
        appUserProvisioner.upsert(AuthenticatedUserResolver.require(servletRequest));
        if (instrumentMasterService.globalInstrument(globalInstrumentId).isEmpty())
            throw new ResponseStatusException(HttpStatus.NOT_FOUND, "Global instrument not found");
        reconciliationService.reconcile(globalInstrumentId);
        return instrumentMasterService.globalInstrument(globalInstrumentId)
                .map(GlobalInstrumentResponse::from)
                .orElseThrow(() -> new ResponseStatusException(HttpStatus.NOT_FOUND, "Global instrument not found"));
    }

    @PutMapping("/{globalInstrumentId}/provider-mappings/verified")
    public GlobalInstrumentResponse saveVerifiedMapping(@PathVariable UUID globalInstrumentId,
            @RequestBody VerifiedProviderMappingRequest request, HttpServletRequest servletRequest) {
        appUserProvisioner.upsert(AuthenticatedUserResolver.require(servletRequest));
        try {
            instrumentMasterService.saveVerifiedExternalMapping(globalInstrumentId, request.provider(), request.providerSymbol(),
                    request.providerInstrumentId(), request.exchange(), request.currency(), request.resolutionSource(), request.confidence());
        } catch (IllegalArgumentException ex) {
            throw new ResponseStatusException(HttpStatus.NOT_FOUND, "Global instrument not found");
        }
        return instrumentMasterService.globalInstrument(globalInstrumentId).map(GlobalInstrumentResponse::from)
                .orElseThrow(() -> new ResponseStatusException(HttpStatus.NOT_FOUND, "Global instrument not found"));
    }

    public record VerifiedProviderMappingRequest(String provider, String providerSymbol, String providerInstrumentId,
            String exchange, String currency, String resolutionSource, BigDecimal confidence) {}
}
