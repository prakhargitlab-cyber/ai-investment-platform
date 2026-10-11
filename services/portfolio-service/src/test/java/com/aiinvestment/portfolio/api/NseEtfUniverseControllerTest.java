package com.aiinvestment.portfolio.api;

import com.aiinvestment.portfolio.application.AuthoritativeAssetTypeReclassificationService;
import com.aiinvestment.portfolio.application.NseEtfUniverseApplyService;
import com.aiinvestment.portfolio.application.NseEtfUniverseBootstrapService;
import com.aiinvestment.portfolio.application.NseEtfUniversePreviewService;
import com.aiinvestment.portfolio.infrastructure.persistence.AppUserProvisioner;
import org.junit.jupiter.api.Test;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

import java.util.List;
import java.util.Optional;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/**
 * MockMvc standalone unit tests (no Spring context, no real DB) for the ADMIN-gated NSE ETF
 * universe preview/apply endpoints -- mirrors {@code IndiaMarketUniverseControllerTest}'s style.
 */
class NseEtfUniverseControllerTest {
    private static final String PREVIEW_PATH = "/api/v1/market-universe/india/nse-etf/preview";
    private static final String APPLY_PATH = "/api/v1/market-universe/india/nse-etf/apply";

    // 1. Unauthenticated preview (no identity headers at all) is rejected before any service is invoked.
    @Test void unauthenticatedPreviewIsRejectedWithoutInvokingServices() throws Exception {
        var preview = mock(NseEtfUniversePreviewService.class);
        var apply = mock(NseEtfUniverseApplyService.class);
        var users = mock(AppUserProvisioner.class);
        MockMvc mockMvc = mockMvc(preview, apply, users);

        mockMvc.perform(get(PREVIEW_PATH)).andExpect(status().isUnauthorized());

        verifyNoInteractions(preview, apply, users);
    }

    // 2. Authenticated but non-ADMIN preview is rejected (403) without invoking the preview service.
    @Test void nonAdminPreviewIsForbiddenWithoutInvokingPreviewService() throws Exception {
        var preview = mock(NseEtfUniversePreviewService.class);
        var apply = mock(NseEtfUniverseApplyService.class);
        var users = mock(AppUserProvisioner.class);
        MockMvc mockMvc = mockMvc(preview, apply, users);

        mockMvc.perform(authenticatedRequest(get(PREVIEW_PATH), "ANALYST")).andExpect(status().isForbidden());

        verify(preview, never()).preview();
        verifyNoInteractions(users);
    }

    // 3. Authenticated ADMIN preview succeeds and invokes the preview service exactly once.
    @Test void adminPreviewSucceedsAndInvokesPreviewServiceOnce() throws Exception {
        var preview = mock(NseEtfUniversePreviewService.class);
        var apply = mock(NseEtfUniverseApplyService.class);
        var users = mock(AppUserProvisioner.class);
        when(preview.preview()).thenReturn(new NseEtfUniversePreviewService.Preview(
                true, true, List.of(), List.of(), List.of(), List.of(), List.of(), List.of()));
        MockMvc mockMvc = mockMvc(preview, apply, users);

        mockMvc.perform(authenticatedRequest(get(PREVIEW_PATH), "ADMIN")).andExpect(status().isOk());

        verify(preview).preview();
        verify(users).upsert(any());
        verifyNoInteractions(apply);
    }

    // 4. Unauthenticated apply is rejected before any service is invoked.
    @Test void unauthenticatedApplyIsRejectedWithoutInvokingServices() throws Exception {
        var preview = mock(NseEtfUniversePreviewService.class);
        var apply = mock(NseEtfUniverseApplyService.class);
        var users = mock(AppUserProvisioner.class);
        MockMvc mockMvc = mockMvc(preview, apply, users);

        mockMvc.perform(post(APPLY_PATH)).andExpect(status().isUnauthorized());

        verifyNoInteractions(preview, apply, users);
    }

    // 5. Authenticated but non-ADMIN apply is rejected (403) without invoking the apply service.
    @Test void nonAdminApplyIsForbiddenWithoutInvokingApplyService() throws Exception {
        var preview = mock(NseEtfUniversePreviewService.class);
        var apply = mock(NseEtfUniverseApplyService.class);
        var users = mock(AppUserProvisioner.class);
        MockMvc mockMvc = mockMvc(preview, apply, users);

        mockMvc.perform(authenticatedRequest(post(APPLY_PATH), "ANALYST")).andExpect(status().isForbidden());

        verify(apply, never()).apply();
        verifyNoInteractions(users);
    }

    // 6. Authenticated ADMIN apply succeeds and invokes the apply service exactly once.
    @Test void adminApplySucceedsAndInvokesApplyServiceOnce() throws Exception {
        var preview = mock(NseEtfUniversePreviewService.class);
        var apply = mock(NseEtfUniverseApplyService.class);
        var users = mock(AppUserProvisioner.class);
        when(apply.apply()).thenReturn(Optional.of(new NseEtfUniverseApplyService.Result(
                NseEtfUniverseApplyService.Status.COMPLETED,
                new AuthoritativeAssetTypeReclassificationService.Summary(),
                new NseEtfUniverseBootstrapService.Summary())));
        MockMvc mockMvc = mockMvc(preview, apply, users);

        mockMvc.perform(authenticatedRequest(post(APPLY_PATH), "ADMIN")).andExpect(status().isOk());

        verify(apply).apply();
        verify(users).upsert(any());
    }

    // 7. A concurrently-rejected apply (service returns empty) surfaces as 409 Conflict, not a 200 or 500.
    @Test void applyAlreadyRunningSurfacesAsConflict() throws Exception {
        var preview = mock(NseEtfUniversePreviewService.class);
        var apply = mock(NseEtfUniverseApplyService.class);
        var users = mock(AppUserProvisioner.class);
        when(apply.apply()).thenReturn(Optional.empty());
        MockMvc mockMvc = mockMvc(preview, apply, users);

        mockMvc.perform(authenticatedRequest(post(APPLY_PATH), "ADMIN")).andExpect(status().isConflict());

        verify(apply).apply();
    }

    private static MockMvc mockMvc(NseEtfUniversePreviewService preview, NseEtfUniverseApplyService apply,
            AppUserProvisioner users) {
        return MockMvcBuilders.standaloneSetup(new NseEtfUniverseController(preview, apply, users)).build();
    }

    private static MockHttpServletRequestBuilder authenticatedRequest(MockHttpServletRequestBuilder builder, String role) {
        return builder
                .header("X-AIP-User-Id", "00000000-0000-0000-0000-000000000777")
                .header("X-AIP-User-Issuer", "test")
                .header("X-AIP-User-Subject", "nse-etf-universe-test")
                .header("X-AIP-User-Roles", role);
    }
}
