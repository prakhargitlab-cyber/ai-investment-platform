package com.aiinvestment.portfolio.application;

import org.junit.jupiter.api.Test;
import java.util.concurrent.*;
import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.*;

class NseEtfUniverseApplyServiceTest {
    private final NseEtfUniverseBootstrapService bootstrap = mock(NseEtfUniverseBootstrapService.class);
    private final NseEtfUniverseApplyService service = new NseEtfUniverseApplyService(bootstrap);

    private NseEtfUniverseBootstrapService.Summary valid() {
        var s = new NseEtfUniverseBootstrapService.Summary();
        s.examined = 2; s.ivSeriesSourceAvailable = true;
        return s;
    }

    @Test void retriesPreserveExistingAndNeverReclassify() {
        var first = valid(); first.created = 2;
        var retry = valid(); retry.existing = 2;
        when(bootstrap.bootstrapMissingEtfs()).thenReturn(first, retry);
        assertThat(service.apply().orElseThrow().bootstrap().created).isEqualTo(2);
        var result = service.apply().orElseThrow();
        assertThat(result.status()).isEqualTo(NseEtfUniverseApplyService.Status.COMPLETED);
        assertThat(result.bootstrap().existing).isEqualTo(2);
        assertThat(result.reclassification().examined).isZero();
        verify(bootstrap, times(2)).bootstrapMissingEtfs();
    }

    @Test void unavailableSourcesOrEmptyListAbort() {
        var etfUnavailable = valid(); etfUnavailable.providerUnavailable = true;
        var equityUnavailable = valid(); equityUnavailable.ivSeriesSourceAvailable = false;
        var empty = valid(); empty.examined = 0;
        when(bootstrap.bootstrapMissingEtfs()).thenReturn(etfUnavailable, equityUnavailable, empty);
        for (int i = 0; i < 3; i++)
            assertThat(service.apply().orElseThrow().status()).isEqualTo(NseEtfUniverseApplyService.Status.ABORTED);
    }

    @Test void freshConflictsAndPartialWritesAreExplicit() {
        var conflict = valid(); conflict.rejectedConflict = 1;
        var failed = valid(); failed.created = 1; failed.failed = 1;
        when(bootstrap.bootstrapMissingEtfs()).thenReturn(conflict, failed);
        assertThat(service.apply().orElseThrow().status()).isEqualTo(NseEtfUniverseApplyService.Status.PARTIAL_FAILURE);
        var result = service.apply().orElseThrow();
        assertThat(result.status()).isEqualTo(NseEtfUniverseApplyService.Status.PARTIAL_FAILURE);
        assertThat(result.bootstrap().created).isEqualTo(1);
    }

    @Test void concurrentApplyRejectedAndGuardReleased() throws Exception {
        var entered = new CountDownLatch(1); var release = new CountDownLatch(1);
        when(bootstrap.bootstrapMissingEtfs()).thenAnswer(call -> {
            entered.countDown();
            if (!release.await(5, TimeUnit.SECONDS)) throw new IllegalStateException("timeout");
            return valid();
        });
        var executor = Executors.newSingleThreadExecutor();
        try {
            var pending = executor.submit(service::apply);
            assertThat(entered.await(5, TimeUnit.SECONDS)).isTrue();
            assertThat(service.apply()).isEmpty();
            release.countDown();
            assertThat(pending.get(5, TimeUnit.SECONDS)).isPresent();
            assertThat(service.apply()).isPresent();
        } finally { release.countDown(); executor.shutdownNow(); }
    }

    @Test void unexpectedBootstrapFailureReleasesGuardForNextExplicitCall() {
        when(bootstrap.bootstrapMissingEtfs()).thenThrow(new IllegalStateException("source failed"))
                .thenReturn(valid());
        org.assertj.core.api.Assertions.assertThatThrownBy(service::apply)
                .isInstanceOf(IllegalStateException.class).hasMessage("source failed");
        assertThat(service.apply().orElseThrow().status()).isEqualTo(NseEtfUniverseApplyService.Status.COMPLETED);
    }

    @Test void onlyOneOfManyOverlappingAppliesRuns() throws Exception {
        var entered = new CountDownLatch(1);
        var release = new CountDownLatch(1);
        when(bootstrap.bootstrapMissingEtfs()).thenAnswer(call -> {
            entered.countDown();
            if (!release.await(5, TimeUnit.SECONDS)) throw new IllegalStateException("timeout");
            return valid();
        });
        var executor = Executors.newFixedThreadPool(8);
        try {
            var first = executor.submit(service::apply);
            assertThat(entered.await(5, TimeUnit.SECONDS)).isTrue();
            var overlapping = new java.util.ArrayList<Future<java.util.Optional<NseEtfUniverseApplyService.Result>>>();
            for (int i = 0; i < 7; i++) overlapping.add(executor.submit(service::apply));
            for (var result : overlapping) assertThat(result.get(5, TimeUnit.SECONDS)).isEmpty();
            verify(bootstrap, times(1)).bootstrapMissingEtfs();
            release.countDown();
            assertThat(first.get(5, TimeUnit.SECONDS)).isPresent();
        } finally { release.countDown(); executor.shutdownNow(); }
    }
}
