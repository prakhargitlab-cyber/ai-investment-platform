package com.aiinvestment.portfolio.application;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.*;

/**
 * Pure Mockito unit tests (no Spring context, no real DB) for the apply orchestrator -- kept off
 * the shared H2 test database for the same reason documented on
 * {@link AuthoritativeAssetTypeReclassificationServiceTest}.
 */
class NseEtfUniverseApplyServiceTest {
    private final NseOfficialEtfSecurityList etfSecurityList = mock(NseOfficialEtfSecurityList.class);
    private final AuthoritativeAssetTypeReclassificationService reclassification =
            mock(AuthoritativeAssetTypeReclassificationService.class);
    private final NseEtfUniverseBootstrapService bootstrap = mock(NseEtfUniverseBootstrapService.class);
    private final NseEtfUniverseApplyService service =
            new NseEtfUniverseApplyService(etfSecurityList, reclassification, bootstrap);

    private void mandatorySourceAvailable() {
        when(etfSecurityList.listAll()).thenReturn(List.of());
    }

    // 1. A normal apply call validates the mandatory source first, then runs both passes fresh and
    // returns COMPLETED with both summaries.
    @Test void applyValidatesSourceThenRunsReclassificationThenBootstrapAndReturnsCompleted() {
        mandatorySourceAvailable();
        var reclassificationSummary = new AuthoritativeAssetTypeReclassificationService.Summary();
        var bootstrapSummary = new NseEtfUniverseBootstrapService.Summary();
        when(reclassification.reclassifyExistingNseInstruments()).thenReturn(reclassificationSummary);
        when(bootstrap.bootstrapMissingEtfs()).thenReturn(bootstrapSummary);

        var result = service.apply();

        assertThat(result).isPresent();
        assertThat(result.get().status()).isEqualTo(NseEtfUniverseApplyService.Status.COMPLETED);
        assertThat(result.get().reclassification()).isSameAs(reclassificationSummary);
        assertThat(result.get().bootstrap()).isSameAs(bootstrapSummary);
        var inOrder = inOrder(etfSecurityList, reclassification, bootstrap);
        inOrder.verify(etfSecurityList).listAll();
        inOrder.verify(reclassification).reclassifyExistingNseInstruments();
        inOrder.verify(bootstrap).bootstrapMissingEtfs();
    }

    // 2. If the mandatory official ETF list source is unavailable, apply is ABORTED entirely --
    // neither reclassification nor bootstrap is ever invoked, and nothing is mutated.
    @Test void mandatorySourceUnavailableAbortsWithoutInvokingEitherPass() {
        when(etfSecurityList.listAll()).thenThrow(new IllegalStateException("NSE_ETF_LIST_UNAVAILABLE"));

        var result = service.apply();

        assertThat(result).isPresent();
        assertThat(result.get().status()).isEqualTo(NseEtfUniverseApplyService.Status.ABORTED);
        assertThat(result.get().reclassification().examined).isZero();
        assertThat(result.get().bootstrap().examined).isZero();
        verifyNoInteractions(reclassification, bootstrap);
    }

    // 3. If an individual ETF creation fails within bootstrap (while others may have succeeded),
    // apply reports PARTIAL_FAILURE -- not a misleading COMPLETED, and not an implication that
    // the whole operation rolled back.
    @Test void individualBootstrapCreationFailureReportsPartialFailure() {
        mandatorySourceAvailable();
        var reclassificationSummary = new AuthoritativeAssetTypeReclassificationService.Summary();
        var bootstrapSummary = new NseEtfUniverseBootstrapService.Summary();
        bootstrapSummary.created = 1;
        bootstrapSummary.failed = 1;
        when(reclassification.reclassifyExistingNseInstruments()).thenReturn(reclassificationSummary);
        when(bootstrap.bootstrapMissingEtfs()).thenReturn(bootstrapSummary);

        var result = service.apply();

        assertThat(result).isPresent();
        assertThat(result.get().status()).isEqualTo(NseEtfUniverseApplyService.Status.PARTIAL_FAILURE);
        assertThat(result.get().bootstrap().created).isEqualTo(1);
        assertThat(result.get().bootstrap().failed).isEqualTo(1);
    }

    // 4. If bootstrap's own source became unavailable between the preflight check and its own call
    // (a narrow race), apply still reports PARTIAL_FAILURE rather than COMPLETED, and still reports
    // whatever reclassification achieved.
    @Test void bootstrapSourceUnavailableAfterPreflightReportsPartialFailure() {
        mandatorySourceAvailable();
        var reclassificationSummary = new AuthoritativeAssetTypeReclassificationService.Summary();
        reclassificationSummary.correctedToEtf = 1;
        var bootstrapSummary = new NseEtfUniverseBootstrapService.Summary();
        bootstrapSummary.providerUnavailable = true;
        when(reclassification.reclassifyExistingNseInstruments()).thenReturn(reclassificationSummary);
        when(bootstrap.bootstrapMissingEtfs()).thenReturn(bootstrapSummary);

        var result = service.apply();

        assertThat(result.get().status()).isEqualTo(NseEtfUniverseApplyService.Status.PARTIAL_FAILURE);
        assertThat(result.get().reclassification().correctedToEtf).isEqualTo(1);
    }

    // 5. Sequential repeated calls are each allowed (the guard only blocks genuinely overlapping calls).
    @Test void sequentialApplyCallsAreEachAllowed() {
        mandatorySourceAvailable();
        when(reclassification.reclassifyExistingNseInstruments())
                .thenReturn(new AuthoritativeAssetTypeReclassificationService.Summary());
        when(bootstrap.bootstrapMissingEtfs()).thenReturn(new NseEtfUniverseBootstrapService.Summary());

        var first = service.apply();
        var second = service.apply();

        assertThat(first).isPresent();
        assertThat(second).isPresent();
        verify(reclassification, times(2)).reclassifyExistingNseInstruments();
        verify(bootstrap, times(2)).bootstrapMissingEtfs();
    }

    // 6. A genuinely concurrent, overlapping apply call is rejected (returns empty) rather than
    // running both passes twice at once; the guard is released afterwards so a later call succeeds.
    @Test void concurrentOverlappingApplyIsRejected() throws InterruptedException {
        mandatorySourceAvailable();
        CountDownLatch insideFirstCall = new CountDownLatch(1);
        CountDownLatch releaseFirstCall = new CountDownLatch(1);
        when(reclassification.reclassifyExistingNseInstruments()).thenAnswer(invocation -> {
            insideFirstCall.countDown();
            assertThat(releaseFirstCall.await(5, TimeUnit.SECONDS)).isTrue();
            return new AuthoritativeAssetTypeReclassificationService.Summary();
        });
        when(bootstrap.bootstrapMissingEtfs()).thenReturn(new NseEtfUniverseBootstrapService.Summary());

        ExecutorService executor = Executors.newFixedThreadPool(2);
        try {
            var firstCall = executor.submit(service::apply);
            assertThat(insideFirstCall.await(5, TimeUnit.SECONDS)).isTrue();

            var secondResult = service.apply();
            assertThat(secondResult).isEmpty();

            releaseFirstCall.countDown();
            var firstResult = firstCall.get(5, TimeUnit.SECONDS);
            assertThat(firstResult).isPresent();
        } catch (Exception e) {
            throw new RuntimeException(e);
        } finally {
            executor.shutdownNow();
        }

        // Guard is released after the first call completes, so a subsequent call succeeds.
        var thirdResult = service.apply();
        assertThat(thirdResult).isPresent();
    }

    // 7. Under true concurrency, at most one of N overlapping attempts succeeds at the same instant
    // when each pass takes measurable time -- the AtomicBoolean guard genuinely serializes overlap,
    // it does not merely reduce its likelihood.
    @Test void onlyOneOfManyTrulyConcurrentApplyAttemptsRunsAtATime() throws InterruptedException {
        mandatorySourceAvailable();
        AtomicInteger concurrentRunners = new AtomicInteger(0);
        AtomicInteger maxObservedConcurrency = new AtomicInteger(0);
        when(reclassification.reclassifyExistingNseInstruments()).thenAnswer(invocation -> {
            int current = concurrentRunners.incrementAndGet();
            maxObservedConcurrency.updateAndGet(max -> Math.max(max, current));
            try {
                Thread.sleep(50);
            } finally {
                concurrentRunners.decrementAndGet();
            }
            return new AuthoritativeAssetTypeReclassificationService.Summary();
        });
        when(bootstrap.bootstrapMissingEtfs()).thenReturn(new NseEtfUniverseBootstrapService.Summary());

        int attempts = 8;
        ExecutorService executor = Executors.newFixedThreadPool(attempts);
        try {
            CountDownLatch start = new CountDownLatch(1);
            List<java.util.concurrent.Future<java.util.Optional<NseEtfUniverseApplyService.Result>>> futures =
                    new java.util.ArrayList<>();
            for (int i = 0; i < attempts; i++) {
                futures.add(executor.submit(() -> {
                    start.await(5, TimeUnit.SECONDS);
                    return service.apply();
                }));
            }
            start.countDown();
            long succeeded = futures.stream().map(f -> {
                try {
                    return f.get(5, TimeUnit.SECONDS);
                } catch (Exception e) {
                    throw new RuntimeException(e);
                }
            }).filter(java.util.Optional::isPresent).count();

            assertThat(maxObservedConcurrency.get()).isEqualTo(1);
            assertThat(succeeded).isGreaterThanOrEqualTo(1);
        } finally {
            executor.shutdownNow();
        }
    }
}
