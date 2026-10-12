package com.aiinvestment.auth;

import org.junit.jupiter.api.Test;
import org.springframework.boot.DefaultApplicationArguments;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.*;

class FirstAdminBootstrapRunnerTest {
    @Test void createdOutcomeReportsSuccess() {
        FirstAdminBootstrapService service = mock(FirstAdminBootstrapService.class);
        when(service.bootstrap(eq("admin@example.test"), eq("correct-password-123")))
                .thenReturn(FirstAdminBootstrapOutcome.CREATED);
        FirstAdminBootstrapRunner runner = new FirstAdminBootstrapRunner(service,
                new FirstAdminBootstrapProperties("admin@example.test", "correct-password-123"));
        runner.run(new DefaultApplicationArguments());
        assertThat(runner.getExitCode()).isZero();
    }

    @Test void adminAlreadyPresentIsNotTreatedAsFailure() {
        FirstAdminBootstrapService service = mock(FirstAdminBootstrapService.class);
        when(service.bootstrap(any(), any())).thenReturn(FirstAdminBootstrapOutcome.ADMIN_ALREADY_PRESENT);
        FirstAdminBootstrapRunner runner = new FirstAdminBootstrapRunner(service,
                new FirstAdminBootstrapProperties("admin@example.test", "correct-password-123"));
        runner.run(new DefaultApplicationArguments());
        assertThat(runner.getExitCode()).isZero();
    }

    @Test void existingUsersWithoutAdminFailsClosedWithDistinctExitCode() {
        FirstAdminBootstrapService service = mock(FirstAdminBootstrapService.class);
        when(service.bootstrap(any(), any())).thenReturn(FirstAdminBootstrapOutcome.EXISTING_USERS_WITHOUT_ADMIN);
        FirstAdminBootstrapRunner runner = new FirstAdminBootstrapRunner(service,
                new FirstAdminBootstrapProperties("admin@example.test", "correct-password-123"));
        runner.run(new DefaultApplicationArguments());
        assertThat(runner.getExitCode()).isEqualTo(3);
    }

    @Test void missingEmailFailsWithoutCallingService() {
        FirstAdminBootstrapService service = mock(FirstAdminBootstrapService.class);
        FirstAdminBootstrapRunner runner = new FirstAdminBootstrapRunner(service,
                new FirstAdminBootstrapProperties("", "correct-password-123"));
        runner.run(new DefaultApplicationArguments());
        assertThat(runner.getExitCode()).isEqualTo(1);
        verifyNoInteractions(service);
    }

    @Test void businessRuleFailureReportsExitCodeOneWithoutLeakingInfrastructureDetail() {
        FirstAdminBootstrapService service = mock(FirstAdminBootstrapService.class);
        when(service.bootstrap(any(), any())).thenThrow(new IllegalArgumentException("bad input"));
        FirstAdminBootstrapRunner runner = new FirstAdminBootstrapRunner(service,
                new FirstAdminBootstrapProperties("admin@example.test", "correct-password-123"));
        runner.run(new DefaultApplicationArguments());
        assertThat(runner.getExitCode()).isEqualTo(1);
    }

    @Test void unexpectedInfrastructureFailureReportsExitCodeTwo() {
        FirstAdminBootstrapService service = mock(FirstAdminBootstrapService.class);
        when(service.bootstrap(any(), any())).thenThrow(new IllegalStateException("db unreachable"));
        FirstAdminBootstrapRunner runner = new FirstAdminBootstrapRunner(service,
                new FirstAdminBootstrapProperties("admin@example.test", "correct-password-123"));
        runner.run(new DefaultApplicationArguments());
        assertThat(runner.getExitCode()).isEqualTo(2);
    }
}
