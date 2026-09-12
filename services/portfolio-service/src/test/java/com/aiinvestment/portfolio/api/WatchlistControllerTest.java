package com.aiinvestment.portfolio.api;

import com.aiinvestment.portfolio.application.InstrumentMasterService;
import com.aiinvestment.portfolio.application.MarketRegion;
import com.aiinvestment.portfolio.application.WatchlistRegionMismatchException;
import com.aiinvestment.portfolio.application.WatchlistService;
import com.aiinvestment.portfolio.infrastructure.persistence.AppUserProvisioner;
import com.aiinvestment.portfolio.infrastructure.persistence.WatchlistEntity;
import com.aiinvestment.shared.web.auth.AuthenticatedUser;
import jakarta.servlet.http.HttpServletRequest;
import org.junit.jupiter.api.Test;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

import java.time.Instant;
import java.util.List;
import java.util.UUID;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

class WatchlistControllerTest {
    @Test
    void defaultEnsureIsAuthenticatedIdempotentAndReturnsWatchlistContract() throws Exception {
        var service = mock(WatchlistService.class);
        var users = mock(AppUserProvisioner.class);
        UUID id = UUID.randomUUID();
        UUID userId = UUID.fromString("00000000-0000-0000-0000-000000000111");
        when(service.ensureDefault(userId, MarketRegion.INDIA)).thenReturn(
                new WatchlistEntity(id, userId, "WATCHLIST-IND", MarketRegion.INDIA, true, Instant.now()));
        when(service.instrumentCount(id)).thenReturn(0L);
        MockMvc mvc = mvc(service, users);

        mvc.perform(post("/api/v1/watchlists/default/ensure")
                        .headers(headers(userId))
                        .contentType("application/json")
                        .content("{\"region\":\"INDIA\"}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.watchlistId").value(id.toString()))
                .andExpect(jsonPath("$.name").value("WATCHLIST-IND"))
                .andExpect(jsonPath("$.region").value("INDIA"))
                .andExpect(jsonPath("$.systemDefault").value(true))
                .andExpect(jsonPath("$.instrumentCount").value(0));
        verify(users).upsert(any(AuthenticatedUser.class));
        verify(service).ensureDefault(userId, MarketRegion.INDIA);
    }

    @Test
    void unauthenticatedRequestsCannotReadUserWatchlists() throws Exception {
        var service = mock(WatchlistService.class);
        var users = mock(AppUserProvisioner.class);
        mvc(service, users).perform(get("/api/v1/watchlists"))
                .andExpect(status().isUnauthorized());
        verifyNoInteractions(service, users);
    }

    @Test
    void mismatchHasDeterministicDomainCode() throws Exception {
        var service = mock(WatchlistService.class);
        UUID userId = UUID.fromString("00000000-0000-0000-0000-000000000111");
        UUID watchlistId = UUID.randomUUID();
        UUID instrumentId = UUID.randomUUID();
        when(service.add(userId, watchlistId, instrumentId, "WEEK", new java.math.BigDecimal("1.5")))
                .thenThrow(new WatchlistRegionMismatchException());

        mvc(service, mock(AppUserProvisioner.class)).perform(post("/api/v1/watchlists/{id}/instruments", watchlistId)
                        .headers(headers(userId))
                        .contentType("application/json")
                        .content("{\"globalInstrumentId\":\"" + instrumentId
                                + "\",\"sourcePeriod\":\"WEEK\",\"sourcePerformancePct\":1.5}"))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("WATCHLIST_REGION_MISMATCH"));
    }

    private static MockMvc mvc(WatchlistService service, AppUserProvisioner users) {
        return MockMvcBuilders.standaloneSetup(
                new WatchlistController(service, mock(InstrumentMasterService.class), users))
                .setControllerAdvice(new GlobalExceptionHandler()).build();
    }

    private static org.springframework.http.HttpHeaders headers(UUID userId) {
        var headers = new org.springframework.http.HttpHeaders();
        headers.add("X-AIP-User-Id", userId.toString());
        headers.add("X-AIP-User-Issuer", "test");
        headers.add("X-AIP-User-Subject", "watchlist-test");
        headers.add("X-AIP-User-Roles", "USER");
        return headers;
    }
}
