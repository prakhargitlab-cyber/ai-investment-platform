package com.aiinvestment.auth;

import com.aiinvestment.shared.web.auth.HmacJwtService;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.persistence.EntityManager;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;

import java.time.Instant;
import java.util.List;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class RoleAdminServiceIntegrationTest {
    private static final String SECRET = "test-auth-jwt-secret-at-least-32-characters";

    @Autowired MockMvc mvc;
    @Autowired AppUserRepository users;
    @Autowired AppUserRoleRepository userRoles;
    @Autowired AppUserRoleAuditRepository roleAudit;
    @Autowired EmailVerificationTokenRepository verificationTokens;
    @Autowired PasswordResetTokenRepository resetTokens;
    @Autowired RoleAdminService roleAdminService;
    @Autowired EntityManager entityManager;

    @BeforeEach
    void clearData() {
        roleAudit.deleteAll();
        userRoles.deleteAll();
        verificationTokens.deleteAll();
        resetTokens.deleteAll();
        users.deleteAll();
    }

    @Test
    void newUserReceivesUserRoleOnRegistration() throws Exception {
        mvc.perform(post("/api/v1/auth/register").contentType(MediaType.APPLICATION_JSON)
                        .content("{\"email\":\"fresh@example.test\",\"password\":\"correct-password-123\",\"firstName\":\"Fresh\",\"lastName\":\"User\"}"))
                .andExpect(status().isCreated());
        AppUserEntity user = users.findByNormalizedEmail("fresh@example.test").orElseThrow();
        assertThat(userRoles.existsByUserIdAndRole(user.getId(), "USER")).isTrue();
    }

    @Test
    void existingAccountWithNoPersistedRoleDefaultsToUser() throws Exception {
        AppUserEntity user = registerAndActivate("legacy@example.test");
        // Simulate an account that predates the roles migration: no persisted role row at all.
        userRoles.deleteAll();
        assertThat(userRoles.findByUserId(user.getId())).isEmpty();

        List<String> roles = loginAndDecodeRoles("legacy@example.test");
        assertThat(roles).containsExactly("USER");
    }

    @Test
    void explicitlyGrantedAdminAppearsInNewlyIssuedJwt() throws Exception {
        AppUserEntity user = registerAndActivate("future-admin@example.test");

        roleAdminService.grantRole("future-admin@example.test", "ADMIN", "ops.lead@example.test", "approved via ticket OPS-1");

        List<String> roles = loginAndDecodeRoles("future-admin@example.test");
        assertThat(roles).contains("ADMIN");

        List<AppUserRoleAuditEntity> audit = roleAudit.findByUserIdOrderByCreatedAtDesc(user.getId());
        assertThat(audit).hasSize(1);
        assertThat(audit.get(0).getAction()).isEqualTo("GRANT");
        assertThat(audit.get(0).getRole()).isEqualTo("ADMIN");
        assertThat(audit.get(0).getOperator()).isEqualTo("ops.lead@example.test");
        assertThat(audit.get(0).getTargetEmail()).isEqualTo(user.getEmail());
        assertThat(audit.get(0).getCreatedAt()).isNotNull();
    }

    @Test
    void revokedAdminIsReflectedInNewlyIssuedJwt() throws Exception {
        registerAndActivate("retained-admin@example.test");
        roleAdminService.grantRole("retained-admin@example.test", "ADMIN", "ops.lead@example.test", "retain an active administrator");
        registerAndActivate("temporary-admin@example.test");
        roleAdminService.grantRole("temporary-admin@example.test", "ADMIN", "ops.lead@example.test", "temporary elevation");
        assertThat(loginAndDecodeRoles("temporary-admin@example.test")).contains("ADMIN");

        roleAdminService.revokeRole("temporary-admin@example.test", "ADMIN", "ops.lead@example.test", "elevation window ended");

        List<String> roles = loginAndDecodeRoles("temporary-admin@example.test");
        assertThat(roles).doesNotContain("ADMIN");
        assertThat(roles).contains("USER");
    }

    @Test
    void operatorCannotGrantRoleToSelf() throws Exception {
        AppUserEntity user = registerAndActivate("self-service@example.test");

        assertThatThrownBy(() -> roleAdminService.grantRole("self-service@example.test", "ADMIN", "self-service@example.test", "trying to self-promote"))
                .isInstanceOf(RoleAdminException.class);

        assertThat(userRoles.existsByUserIdAndRole(user.getId(), "ADMIN")).isFalse();
        assertThat(roleAudit.findByUserIdOrderByCreatedAtDesc(user.getId())).isEmpty();
    }

    @Test
    void unknownAccountCannotBeProvisioned() {
        assertThatThrownBy(() -> roleAdminService.grantRole("ghost@example.test", "ADMIN", "ops.lead@example.test", "no such account"))
                .isInstanceOf(RoleAdminException.class);
        assertThat(roleAudit.count()).isZero();
    }

    private AppUserEntity registerAndActivate(String email) throws Exception {
        mvc.perform(post("/api/v1/auth/register").contentType(MediaType.APPLICATION_JSON)
                        .content("{\"email\":\"" + email + "\",\"password\":\"correct-password-123\",\"firstName\":\"Test\",\"lastName\":\"User\"}"))
                .andExpect(status().isCreated());
        AppUserEntity user = users.findByNormalizedEmail(email).orElseThrow();
        user.activate(Instant.now());
        return users.save(user);
    }

    private List<String> loginAndDecodeRoles(String email) throws Exception {
        String response = mvc.perform(post("/api/v1/auth/login").contentType(MediaType.APPLICATION_JSON)
                        .content("{\"email\":\"" + email + "\",\"password\":\"correct-password-123\"}"))
                .andExpect(status().isOk())
                .andReturn().getResponse().getContentAsString();
        JsonNode node = new ObjectMapper().readTree(response);
        String accessToken = node.get("accessToken").asText();
        return new HmacJwtService(SECRET).verify(accessToken, "test-auth-issuer").roles();
    }
}
