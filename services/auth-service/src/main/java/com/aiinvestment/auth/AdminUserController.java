package com.aiinvestment.auth;

import com.aiinvestment.shared.web.auth.JwtClaims;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.PageRequest;
import org.springframework.data.domain.Sort;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.server.ResponseStatusException;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.UUID;

@RestController
@RequestMapping("/api/v1/auth/admin/users")
public class AdminUserController {
    private final AppUserRepository users;
    private final AppUserRoleAuditRepository audit;
    private final RoleAdminService roles;

    public AdminUserController(AppUserRepository users, AppUserRoleAuditRepository audit, RoleAdminService roles) {
        this.users = users; this.audit = audit; this.roles = roles;
    }

    @GetMapping
    public PageResponse<UserDetails> list(HttpServletRequest request, @RequestParam(name = "search", defaultValue = "") String search,
            @RequestParam(name = "page", defaultValue = "0") int page, @RequestParam(name = "size", defaultValue = "20") int size) {
        roles.requireAdministrator(claims(request));
        if (search.length() > 320) throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "Search too long");
        PageRequest paging = paging(page, size, Sort.by("email", "id"));
        return PageResponse.from(users.findByEmailContainingIgnoreCaseOrDisplayNameContainingIgnoreCase(search.trim(), search.trim(), paging).map(this::details));
    }

    @GetMapping("/{id}")
    public UserDetails user(HttpServletRequest request, @PathVariable("id") UUID id) {
        roles.requireAdministrator(claims(request));
        return details(requireUser(id));
    }

    @PutMapping("/{id}/roles/{role}")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    public void grant(HttpServletRequest request, @PathVariable("id") UUID id, @PathVariable("role") String role, @Valid @RequestBody ChangeRequest change) {
        roles.changeRole(claims(request), id, role, change.reason(), true);
    }

    @DeleteMapping("/{id}/roles/{role}")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    public void revoke(HttpServletRequest request, @PathVariable("id") UUID id, @PathVariable("role") String role, @Valid @RequestBody ChangeRequest change) {
        roles.changeRole(claims(request), id, role, change.reason(), false);
    }

    @GetMapping("/{id}/audit")
    public PageResponse<AppUserRoleAuditEntity> history(HttpServletRequest request, @PathVariable("id") UUID id,
            @RequestParam(name = "page", defaultValue = "0") int page, @RequestParam(name = "size", defaultValue = "20") int size) {
        roles.requireAdministrator(claims(request));
        requireUser(id);
        return PageResponse.from(audit.findByUserId(id, paging(page, size, Sort.by(Sort.Direction.DESC, "createdAt", "id"))));
    }

    private JwtClaims claims(HttpServletRequest request) {
        Object claims = request.getAttribute(AdminAuthenticationFilter.CLAIMS);
        if (!(claims instanceof JwtClaims verified)) throw new ResponseStatusException(HttpStatus.UNAUTHORIZED);
        return verified;
    }

    private AppUserEntity requireUser(UUID id) {
        return users.findById(id).orElseThrow(() -> new ResponseStatusException(HttpStatus.NOT_FOUND, "User not found"));
    }

    private UserDetails details(AppUserEntity user) {
        List<String> assigned = roles.currentRoles(user.getId());
        return new UserDetails(user.getId(), user.getEmail(), user.getDisplayName(), user.getAccountStatus(),
                user.getEmailVerifiedAt(), user.getCreatedAt(), user.getLastLoginAt(), assigned.isEmpty() ? List.of("USER") : assigned);
    }

    private PageRequest paging(int page, int size, Sort sort) {
        if (page < 0 || size < 1 || size > 100) throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "Invalid pagination");
        return PageRequest.of(page, size, sort);
    }

    @ExceptionHandler(RoleAdminException.class)
    ResponseEntity<Map<String, String>> roleFailure(RoleAdminException ex) {
        return ResponseEntity.status(HttpStatus.CONFLICT).body(Map.of("message", ex.getMessage()));
    }

    @ExceptionHandler({org.springframework.dao.ConcurrencyFailureException.class, org.springframework.dao.QueryTimeoutException.class})
    ResponseEntity<Map<String, String>> concurrentFailure(Exception ex) {
        return ResponseEntity.status(HttpStatus.CONFLICT).body(Map.of("message", "Another role change is in progress. Refresh and retry."));
    }

    @ExceptionHandler(org.springframework.web.bind.MethodArgumentNotValidException.class)
    ResponseEntity<Map<String, String>> invalidReason(Exception ex) {
        return ResponseEntity.badRequest().body(Map.of("message", "A reason of 1 to 500 characters is required"));
    }

    @ExceptionHandler(ResponseStatusException.class)
    ResponseEntity<Map<String, String>> requestFailure(ResponseStatusException ex) {
        return ResponseEntity.status(ex.getStatusCode()).body(Map.of("message", ex.getReason() == null ? "Request rejected" : ex.getReason()));
    }

    public record ChangeRequest(@NotBlank @Size(max = 500) String reason) {}
    public record UserDetails(UUID userId, String email, String displayName, String status, Instant emailVerifiedAt,
                              Instant createdAt, Instant lastLoginAt, List<String> roles) {}
    public record PageResponse<T>(List<T> content, int page, int size, long totalElements, int totalPages) {
        static <T> PageResponse<T> from(Page<T> page) {
            return new PageResponse<>(page.getContent(), page.getNumber(), page.getSize(), page.getTotalElements(), page.getTotalPages());
        }
    }
}
