package com.aiinvestment.portfolio.application;

import com.aiinvestment.portfolio.api.GlobalInstrumentResponse;
import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterRepository;
import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentProviderMappingRepository;
import com.aiinvestment.shared.domain.AssetType;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;

import java.nio.file.Path;
import static org.assertj.core.api.Assertions.assertThat;

@SpringBootTest
@ActiveProfiles("test")
@org.springframework.transaction.annotation.Transactional
class BenchmarkRegistrationIntegrationTest {
    @Autowired InstrumentMasterService service;
    @Autowired InstrumentMasterRepository masters;
    @Autowired InstrumentProviderMappingRepository mappings;
    @Autowired ObjectMapper mapper;

    @Test
    void registersAndReadsCanonicalIndicesUsingExistingSchema() throws Exception {
        for (String key : InstrumentMasterService.BENCHMARKS.keySet()) {
            var first = service.registerBenchmark(key);
            var second = service.registerBenchmark(key);
            var id = InstrumentMasterService.benchmarkId(key);
            assertThat(first.master().getInstrumentId()).isEqualTo(second.master().getInstrumentId()).isEqualTo(id);
            assertThat(masters.findById(id).orElseThrow().getAssetType()).isEqualTo(AssetType.INDEX);
            assertThat(mappings.findByInstrumentId(id)).singleElement().satisfies(mapping -> {
                assertThat(mapping.getProvider()).isEqualTo("NSE");
                assertThat(mapping.getStatus()).isEqualTo("VERIFIED");
                assertThat(mapping.getResolutionSource()).isEqualTo(InstrumentMasterService.BENCHMARK_CATALOG_VERSION);
            });
        }
        var registered = service.registeredBenchmarks();
        assertThat(registered).hasSize(4);
        // Public canonical metadata exported for the captured-response local runtime smoke.
        mapper.writeValue(Path.of("target", "benchmark-registered-runtime.json").toFile(),
                registered.stream().map(GlobalInstrumentResponse::from).toList());
    }
}
