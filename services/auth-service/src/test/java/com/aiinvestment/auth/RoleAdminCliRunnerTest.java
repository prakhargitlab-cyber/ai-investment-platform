package com.aiinvestment.auth;

import org.junit.jupiter.api.Test;
import org.springframework.boot.DefaultApplicationArguments;
import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.*;

class RoleAdminCliRunnerTest {
    @Test void reportsFailureExitCodeWithoutLeakingInfrastructureException() {
        RoleAdminService service = mock(RoleAdminService.class);
        doThrow(new IllegalStateException("sensitive infrastructure detail")).when(service).grantRole(any(), any(), any(), any());
        RoleAdminCliRunner runner = new RoleAdminCliRunner(service, new RoleAdminProperties("GRANT", "target@example.test", "ADMIN", "operator", "approved"));
        runner.run(new DefaultApplicationArguments());
        assertThat(runner.getExitCode()).isEqualTo(1);
    }
    @Test void usesAuditedServiceAndReportsSuccess() {
        RoleAdminService service = mock(RoleAdminService.class);
        RoleAdminCliRunner runner = new RoleAdminCliRunner(service, new RoleAdminProperties("GRANT", "target@example.test", "ADMIN", "operator", "approved"));
        runner.run(new DefaultApplicationArguments());
        verify(service).grantRole("target@example.test", "ADMIN", "operator", "approved");
        assertThat(runner.getExitCode()).isZero();
    }
}
