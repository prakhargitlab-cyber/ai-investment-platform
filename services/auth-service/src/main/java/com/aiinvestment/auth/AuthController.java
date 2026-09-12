package com.aiinvestment.auth;

import com.aiinvestment.shared.web.auth.AuthenticatedUserResolver;
import com.aiinvestment.shared.web.auth.HmacJwtService;
import com.aiinvestment.shared.web.auth.JwtClaims;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.server.ResponseStatusException;

import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.UUID;

@RestController
@RequestMapping("/api/v1/auth")
public class AuthController {
    private static final Map<String, DevIdentity> DEV_IDENTITIES = Map.of(
            "user-a", new DevIdentity("dev-user-a", "user.a@example.invalid", "User A", List.of("USER")),
            "user-b", new DevIdentity("dev-user-b", "user.b@example.invalid", "User B", List.of("USER"))
    );

    private final AuthProperties properties;
    private final AppUserRepository users;
    private final AuthLifecycleService lifecycle;

    public AuthController(AuthProperties properties, AppUserRepository users, AuthLifecycleService lifecycle) {
        this.properties = properties;
        this.users = users;
        this.lifecycle = lifecycle;
    }

    @PostMapping("/register")
    @ResponseStatus(HttpStatus.CREATED)
    public RegistrationResponse register(@Valid @RequestBody RegisterRequest request) { return lifecycle.register(request); }

    @PostMapping("/verify-email")
    public VerificationResponse verifyEmail(@Valid @RequestBody VerifyRequest request) { return lifecycle.verify(request.token()); }
    @PostMapping("/resend-verification")
    public GenericEmailResponse resendVerification(@Valid @RequestBody PasswordResetRequest request) { return lifecycle.resendVerification(request.email()); }

    @PostMapping("/login")
    public TokenResponse login(@Valid @RequestBody LoginRequest request) { return lifecycle.login(request); }
    @PostMapping("/password-reset/request")
    public PasswordResetRequestResponse passwordResetRequest(@Valid @RequestBody PasswordResetRequest request) { return lifecycle.requestPasswordReset(request.email()); }
    @PostMapping("/password-reset/confirm")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    public void passwordResetConfirm(@Valid @RequestBody PasswordResetConfirmRequest request) { lifecycle.confirmPasswordReset(request.token(), request.newPassword()); }

    @GetMapping("/dev/users")
    public List<DevUserResponse> devUsers() {
        if (!properties.devLoginEnabled()) {
            throw new ResponseStatusException(HttpStatus.NOT_FOUND);
        }
        return DEV_IDENTITIES.entrySet().stream()
                .map(entry -> new DevUserResponse(entry.getKey(), entry.getValue().email(), entry.getValue().displayName()))
                .toList();
    }

    @PostMapping("/dev/login")
    public TokenResponse devLogin(@Valid @RequestBody DevLoginRequest request) {
        if (!properties.devLoginEnabled()) {
            throw new ResponseStatusException(HttpStatus.NOT_FOUND);
        }
        DevIdentity identity = DEV_IDENTITIES.get(request.userKey());
        if (identity == null) {
            throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, "Unknown DEV user");
        }
        AppUserEntity user = upsertUser(identity);
        Instant expiresAt = Instant.now().plusSeconds(properties.tokenTtlSeconds());
        String token = new HmacJwtService(properties.jwtSecret()).issue(new JwtClaims(
                properties.issuer(),
                identity.subject(),
                identity.email(),
                identity.displayName(),
                identity.roles(),
                expiresAt
        ));
        return new TokenResponse(token, "Bearer", expiresAt, UserResponse.from(user, identity.roles()));
    }

    private AppUserEntity upsertUser(DevIdentity identity) {
        Instant now = Instant.now();
        UUID userId = AuthenticatedUserResolver.stableUserId(properties.issuer(), identity.subject());
        return users.findByIssuerAndExternalSubject(properties.issuer(), identity.subject())
                .map(existing -> {
                    existing.setEmail(identity.email());
                    existing.setDisplayName(identity.displayName());
                    existing.setUpdatedAt(now);
                    return existing;
                })
                .orElseGet(() -> users.save(new AppUserEntity(
                        userId,
                        properties.issuer(),
                        identity.subject(),
                        identity.email(),
                        identity.displayName(),
                        now,
                        now
                )));
    }

    public record DevLoginRequest(@NotBlank String userKey) {
    }
    public record RegisterRequest(@NotBlank String email, @NotBlank @Size(min = 12, max = 200) String password,
                                  @NotBlank @Size(max = 120) String firstName, @NotBlank @Size(max = 120) String lastName) {}
    public record LoginRequest(@NotBlank String email, @NotBlank String password) {}
    public record VerifyRequest(@NotBlank String token) {}
    public record PasswordResetRequest(@NotBlank String email) {}
    public record PasswordResetConfirmRequest(@NotBlank String token, @NotBlank @Size(min = 12, max = 200) String newPassword) {}
    public record RegistrationResponse(UUID userId, String status) {}
    public record VerificationResponse(UUID userId, String status) {}
    public record PasswordResetRequestResponse(String message) {}
    public record GenericEmailResponse(String message) {}

    public record DevUserResponse(String userKey, String email, String displayName) {
    }

    public record TokenResponse(String accessToken, String tokenType, Instant expiresAt, UserResponse user) {
    }

    public record UserResponse(UUID userId, String issuer, String subject, String email, String displayName, List<String> roles) {
        static UserResponse from(AppUserEntity user, List<String> roles) {
            return new UserResponse(user.getId(), user.getIssuer(), user.getExternalSubject(), user.getEmail(), user.getDisplayName(), roles);
        }
    }

    private record DevIdentity(String subject, String email, String displayName, List<String> roles) {
    }
}
