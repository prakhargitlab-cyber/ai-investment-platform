package com.aiinvestment.portfolio.api;

import com.aiinvestment.portfolio.application.Nifty500ReferenceService;
import com.aiinvestment.portfolio.infrastructure.persistence.AppUserProvisioner;
import com.aiinvestment.portfolio.infrastructure.persistence.Nifty500UniverseEntity;
import com.aiinvestment.portfolio.infrastructure.persistence.Nifty500UniverseRepository;
import jakarta.servlet.http.HttpServletRequest;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.springframework.data.domain.PageImpl;
import org.springframework.data.domain.Pageable;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

import java.time.Instant;
import java.util.List;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

class IndiaMarketUniverseControllerTest {
    @Test
    void get_is_cache_only_bounded_and_preserves_classification() {
        var refresh = mock(Nifty500ReferenceService.class);
        var rows = mock(Nifty500UniverseRepository.class);
        var users = mock(AppUserProvisioner.class);
        var request = request("USER");
        var entity = entity();
        when(rows.findAllByOrderBySymbolAscInstrumentIdAsc(any()))
                .thenAnswer(invocation -> new PageImpl<>(
                        List.of(entity), invocation.getArgument(0), 1));

        var result = new IndiaMarketUniverseController(refresh, rows, users).list(-2, 900, request);

        var pageable = ArgumentCaptor.forClass(Pageable.class);
        verify(rows).findAllByOrderBySymbolAscInstrumentIdAsc(pageable.capture());
        assertThat(pageable.getValue().getPageNumber()).isZero();
        assertThat(pageable.getValue().getPageSize()).isEqualTo(500);
        assertThat(result.instruments().get(0).officialIndustry()).isEqualTo("Financial Services");
        assertThat(result.instruments().get(0).canonicalSector()).isEqualTo("Financials");
        verifyNoInteractions(refresh);
    }

    @Test
    void authenticated_get_binds_explicit_page_and_size_and_preserves_response_shape() throws Exception {
        var refresh = mock(Nifty500ReferenceService.class);
        var rows = mock(Nifty500UniverseRepository.class);
        var users = mock(AppUserProvisioner.class);
        var entity = entity();
        when(rows.findAllByOrderBySymbolAscInstrumentIdAsc(any()))
                .thenAnswer(invocation -> {
                    Pageable pageable = invocation.getArgument(0);
                    return new PageImpl<>(List.of(entity), pageable, 7);
                });
        MockMvc mockMvc = mockMvc(refresh, rows, users);

        mockMvc.perform(authenticatedGet().queryParam("page", "2").queryParam("size", "3"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.instruments.length()").value(1))
                .andExpect(jsonPath("$.instruments[0].globalInstrumentId").value(entity.getInstrumentId().toString()))
                .andExpect(jsonPath("$.instruments[0].symbol").value("AAA"))
                .andExpect(jsonPath("$.page").value(2))
                .andExpect(jsonPath("$.size").value(3))
                .andExpect(jsonPath("$.totalElements").value(7));

        var pageable = ArgumentCaptor.forClass(Pageable.class);
        verify(rows).findAllByOrderBySymbolAscInstrumentIdAsc(pageable.capture());
        assertThat(pageable.getValue().getPageNumber()).isEqualTo(2);
        assertThat(pageable.getValue().getPageSize()).isEqualTo(3);
        verify(users).upsert(any());
        verifyNoInteractions(refresh);
    }

    @Test
    void authenticated_get_uses_existing_page_and_size_defaults() throws Exception {
        var refresh = mock(Nifty500ReferenceService.class);
        var rows = mock(Nifty500UniverseRepository.class);
        var users = mock(AppUserProvisioner.class);
        when(rows.findAllByOrderBySymbolAscInstrumentIdAsc(any()))
                .thenAnswer(invocation -> new PageImpl<>(
                        List.of(entity()), invocation.getArgument(0), 1));
        MockMvc mockMvc = mockMvc(refresh, rows, users);

        mockMvc.perform(authenticatedGet())
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.instruments").isArray())
                .andExpect(jsonPath("$.page").value(0))
                .andExpect(jsonPath("$.size").value(100))
                .andExpect(jsonPath("$.totalElements").value(1));

        var pageable = ArgumentCaptor.forClass(Pageable.class);
        verify(rows).findAllByOrderBySymbolAscInstrumentIdAsc(pageable.capture());
        assertThat(pageable.getValue().getPageNumber()).isZero();
        assertThat(pageable.getValue().getPageSize()).isEqualTo(100);
    }

    @Test
    void get_remains_authenticated() throws Exception {
        var refresh = mock(Nifty500ReferenceService.class);
        var rows = mock(Nifty500UniverseRepository.class);
        var users = mock(AppUserProvisioner.class);
        MockMvc mockMvc = mockMvc(refresh, rows, users);

        mockMvc.perform(get("/api/v1/market-universe/india/nifty500"))
                .andExpect(status().isUnauthorized());

        verifyNoInteractions(rows, users, refresh);
    }

    private static MockMvc mockMvc(
            Nifty500ReferenceService refresh,
            Nifty500UniverseRepository rows,
            AppUserProvisioner users
    ) {
        return MockMvcBuilders.standaloneSetup(new IndiaMarketUniverseController(refresh, rows, users)).build();
    }

    private static MockHttpServletRequestBuilder authenticatedGet() {
        return get("/api/v1/market-universe/india/nifty500")
                .header("X-AIP-User-Id", "00000000-0000-0000-0000-000000000777")
                .header("X-AIP-User-Issuer", "test")
                .header("X-AIP-User-Subject", "research-engine-test")
                .header("X-AIP-User-Roles", "ADMIN");
    }

    private static HttpServletRequest request(String role) {
        var request = mock(HttpServletRequest.class);
        when(request.getHeader("X-AIP-User-Id")).thenReturn(UUID.randomUUID().toString());
        when(request.getHeader("X-AIP-User-Issuer")).thenReturn("test");
        when(request.getHeader("X-AIP-User-Subject")).thenReturn("subject");
        when(request.getHeader("X-AIP-User-Roles")).thenReturn(role);
        return request;
    }

    private static Nifty500UniverseEntity entity() {
        return new Nifty500UniverseEntity(
                UUID.randomUUID(),
                "AAA",
                "INE123A01016",
                "Alpha",
                "Financial Services",
                "Financials",
                Instant.parse("2026-09-07T00:00:00Z")
        );
    }
}
