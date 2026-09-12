package com.aiinvestment.auth;

import com.aiinvestment.shared.web.auth.HmacJwtService;
import jakarta.persistence.EntityManager;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.transaction.annotation.Transactional;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.Base64;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class AuthLifecycleIntegrationTest {
    private static final String SECRET = "test-auth-jwt-secret-at-least-32-characters";
    @Autowired MockMvc mvc;
    @Autowired AppUserRepository users;
    @Autowired EmailVerificationTokenRepository tokens;
    @Autowired PasswordResetTokenRepository resetTokens;
    @Autowired EntityManager entityManager;

    @BeforeEach
    void clearData() {
        tokens.deleteAll();
        resetTokens.deleteAll();
        users.deleteAll();
    }

    @Test
    void registerVerifyAndLoginUsesDurableUuidSubjectAndHashedCredentials() throws Exception {
        mvc.perform(post("/api/v1/auth/register").contentType(MediaType.APPLICATION_JSON)
                        .content("{\"email\":\"  Real.User@Example.Test \",\"password\":\"correct-password-123\",\"firstName\":\"Real\",\"lastName\":\"User\"}"))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.status").value("EMAIL_VERIFICATION_PENDING"));
        AppUserEntity user = users.findByNormalizedEmail("real.user@example.test").orElseThrow();
        assertThat(user.getPasswordHash()).startsWith("$2");
        assertThat(user.getPasswordHash()).doesNotContain("correct-password-123");
        assertThat(user.getAccountStatus()).isEqualTo("EMAIL_VERIFICATION_PENDING");

        mvc.perform(post("/api/v1/auth/login").contentType(MediaType.APPLICATION_JSON)
                        .content("{\"email\":\"real.user@example.test\",\"password\":\"correct-password-123\"}"))
                .andExpect(status().isForbidden());

        String rawToken = "test-verification-token";
        tokens.save(new EmailVerificationTokenEntity(UUID.randomUUID(), user.getId(), tokenHash(rawToken), Instant.now().plusSeconds(60), Instant.now()));
        mvc.perform(post("/api/v1/auth/verify-email").contentType(MediaType.APPLICATION_JSON)
                        .content("{\"token\":\"" + rawToken + "\"}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("ACTIVE"));
        mvc.perform(post("/api/v1/auth/verify-email").contentType(MediaType.APPLICATION_JSON)
                        .content("{\"token\":\"" + rawToken + "\"}"))
                .andExpect(status().isBadRequest());

        String response = mvc.perform(post("/api/v1/auth/login").contentType(MediaType.APPLICATION_JSON)
                        .content("{\"email\":\"REAL.USER@example.test\",\"password\":\"correct-password-123\"}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.tokenType").value("Bearer"))
                .andExpect(jsonPath("$.user.userId").value(user.getId().toString()))
                .andReturn().getResponse().getContentAsString();
        String accessToken = new com.fasterxml.jackson.databind.ObjectMapper().readTree(response).get("accessToken").asText();
        assertThat(new HmacJwtService(SECRET).verify(accessToken, "test-auth-issuer").subject()).isEqualTo(user.getId().toString());
        assertThat(users.findById(user.getId()).orElseThrow().getLastLoginAt()).isNotNull();

        mvc.perform(post("/api/v1/auth/login").contentType(MediaType.APPLICATION_JSON)
                        .content("{\"email\":\"real.user@example.test\",\"password\":\"wrong-password-123\"}"))
                .andExpect(status().isUnauthorized());
    }

    @Test
    void expiredVerificationAndDevLoginDisabledAreRejected() throws Exception {
        AppUserEntity user = users.save(AppUserEntity.local(UUID.randomUUID(), "test-auth-issuer", "pending@example.test", "pending@example.test", "$2a$12$abcdefghijklmnopqrstuvabcdefghijklmnopqrstuvabcdefghijklmnop", "Pending", Instant.now()));
        tokens.save(new EmailVerificationTokenEntity(UUID.randomUUID(), user.getId(), tokenHash("expired-token"), Instant.now().minusSeconds(1), Instant.now().minusSeconds(2)));
        mvc.perform(post("/api/v1/auth/verify-email").contentType(MediaType.APPLICATION_JSON).content("{\"token\":\"expired-token\"}"))
                .andExpect(status().isBadRequest());
        mvc.perform(post("/api/v1/auth/dev/login").contentType(MediaType.APPLICATION_JSON).content("{\"userKey\":\"user-a\"}"))
                .andExpect(status().isNotFound());
    }

    @Test
    @Transactional
    void disabledAccountCannotLogin() throws Exception {
        mvc.perform(post("/api/v1/auth/register").contentType(MediaType.APPLICATION_JSON)
                        .content("{\"email\":\"disabled@example.test\",\"password\":\"correct-password-123\",\"firstName\":\"Disabled\",\"lastName\":\"User\"}"))
                .andExpect(status().isCreated());
        entityManager.createNativeQuery("UPDATE auth.app_users SET account_status = 'DISABLED' WHERE normalized_email = 'disabled@example.test'").executeUpdate();
        mvc.perform(post("/api/v1/auth/login").contentType(MediaType.APPLICATION_JSON)
                        .content("{\"email\":\"disabled@example.test\",\"password\":\"correct-password-123\"}"))
                .andExpect(status().isForbidden());
    }

    @Test
    void passwordResetConsumesHashedOneTimeTokenAndReplacesPassword() throws Exception {
        mvc.perform(post("/api/v1/auth/register").contentType(MediaType.APPLICATION_JSON)
                        .content("{\"email\":\"reset@example.test\",\"password\":\"correct-password-123\",\"firstName\":\"Reset\",\"lastName\":\"User\"}"))
                .andExpect(status().isCreated());
        AppUserEntity user = users.findByNormalizedEmail("reset@example.test").orElseThrow();
        user.activate(Instant.now()); users.save(user);
        String genericExisting = mvc.perform(post("/api/v1/auth/password-reset/request").contentType(MediaType.APPLICATION_JSON).content("{\"email\":\"reset@example.test\"}"))
                .andExpect(status().isOk()).andReturn().getResponse().getContentAsString();
        String genericUnknown = mvc.perform(post("/api/v1/auth/password-reset/request").contentType(MediaType.APPLICATION_JSON).content("{\"email\":\"unknown@example.test\"}"))
                .andExpect(status().isOk()).andReturn().getResponse().getContentAsString();
        assertThat(genericExisting).isEqualTo(genericUnknown);

        String raw = "known-reset-token";
        resetTokens.save(new PasswordResetTokenEntity(UUID.randomUUID(), user.getId(), tokenHash(raw), Instant.now().plusSeconds(60), Instant.now()));
        mvc.perform(post("/api/v1/auth/password-reset/confirm").contentType(MediaType.APPLICATION_JSON)
                        .content("{\"token\":\"" + raw + "\",\"newPassword\":\"new-password-456\"}"))
                .andExpect(status().isNoContent());
        mvc.perform(post("/api/v1/auth/login").contentType(MediaType.APPLICATION_JSON).content("{\"email\":\"reset@example.test\",\"password\":\"correct-password-123\"}"))
                .andExpect(status().isUnauthorized());
        mvc.perform(post("/api/v1/auth/login").contentType(MediaType.APPLICATION_JSON).content("{\"email\":\"reset@example.test\",\"password\":\"new-password-456\"}"))
                .andExpect(status().isOk());
        mvc.perform(post("/api/v1/auth/password-reset/confirm").contentType(MediaType.APPLICATION_JSON)
                        .content("{\"token\":\"" + raw + "\",\"newPassword\":\"another-password-789\"}"))
                .andExpect(status().isBadRequest());
    }

    @Test
    void pendingReregistrationReusesOneUserAndSupersedesOldVerificationTokens() throws Exception {
        mvc.perform(post("/api/v1/auth/register").contentType(MediaType.APPLICATION_JSON)
                        .content("{\"email\":\"Pending.User@Example.Test\",\"password\":\"first-password-123\",\"firstName\":\"Pending\",\"lastName\":\"User\"}"))
                .andExpect(status().isCreated());
        AppUserEntity user = users.findByNormalizedEmail("pending.user@example.test").orElseThrow();
        String oldToken = "superseded-verification-token";
        tokens.save(new EmailVerificationTokenEntity(UUID.randomUUID(), user.getId(), tokenHash(oldToken), Instant.now().plusSeconds(60), Instant.now()));

        mvc.perform(post("/api/v1/auth/register").contentType(MediaType.APPLICATION_JSON)
                        .content("{\"email\":\" pending.user@example.test \",\"password\":\"second-password-456\",\"firstName\":\"Updated\",\"lastName\":\"User\"}"))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.userId").value(user.getId().toString()))
                .andExpect(jsonPath("$.status").value("EMAIL_VERIFICATION_PENDING"));

        assertThat(users.count()).isEqualTo(1);
        assertThat(tokens.count()).isEqualTo(1);
        assertThat(users.findById(user.getId()).orElseThrow().getPasswordHash()).doesNotContain("second-password-456");
        mvc.perform(post("/api/v1/auth/verify-email").contentType(MediaType.APPLICATION_JSON).content("{\"token\":\"" + oldToken + "\"}"))
                .andExpect(status().isBadRequest());
        mvc.perform(post("/api/v1/auth/login").contentType(MediaType.APPLICATION_JSON)
                        .content("{\"email\":\"pending.user@example.test\",\"password\":\"second-password-456\"}"))
                .andExpect(status().isForbidden());

        String freshToken = "fresh-verification-token";
        tokens.save(new EmailVerificationTokenEntity(UUID.randomUUID(), user.getId(), tokenHash(freshToken), Instant.now().plusSeconds(60), Instant.now()));
        mvc.perform(post("/api/v1/auth/verify-email").contentType(MediaType.APPLICATION_JSON).content("{\"token\":\"" + freshToken + "\"}"))
                .andExpect(status().isOk());
        mvc.perform(post("/api/v1/auth/login").contentType(MediaType.APPLICATION_JSON)
                        .content("{\"email\":\"pending.user@example.test\",\"password\":\"second-password-456\"}"))
                .andExpect(status().isOk());
    }

    @Test
    void activeEmailCannotBeRegisteredAgain() throws Exception {
        mvc.perform(post("/api/v1/auth/register").contentType(MediaType.APPLICATION_JSON)
                        .content("{\"email\":\"active@example.test\",\"password\":\"correct-password-123\",\"firstName\":\"Active\",\"lastName\":\"User\"}"))
                .andExpect(status().isCreated());
        AppUserEntity user = users.findByNormalizedEmail("active@example.test").orElseThrow();
        user.activate(Instant.now());
        users.save(user);
        mvc.perform(post("/api/v1/auth/register").contentType(MediaType.APPLICATION_JSON)
                        .content("{\"email\":\"ACTIVE@example.test\",\"password\":\"another-password-456\",\"firstName\":\"Active\",\"lastName\":\"User\"}"))
                .andExpect(status().isConflict())
                .andExpect(status().reason("An account already exists for this email. Sign in or reset your password."));
        assertThat(users.count()).isEqualTo(1);
    }

    private static String tokenHash(String raw) throws Exception {
        return Base64.getUrlEncoder().withoutPadding().encodeToString(MessageDigest.getInstance("SHA-256").digest(raw.getBytes(StandardCharsets.UTF_8)));
    }
}
