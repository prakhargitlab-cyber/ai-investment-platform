package com.aiinvestment.portfolio.api;

import com.aiinvestment.portfolio.application.NseEtfUniverseApplyService;
import com.aiinvestment.portfolio.application.NseEtfUniversePreviewService;
import com.aiinvestment.portfolio.infrastructure.persistence.AppUserProvisioner;
import com.aiinvestment.shared.web.auth.AuthenticatedUser;
import com.aiinvestment.shared.web.auth.AuthenticatedUserResolver;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;

import java.util.Map;

/**
 * ADMIN-gated, explicitly-invoked-only endpoints for the operator-controlled NSE ETF universe
 * population flow. Preview ({@code GET .../preview}) is read-only; Apply ({@code POST .../apply})
 * mutates and is never scheduled or startup-triggered -- it is reachable only through this HTTP path.
 *
 * <p>Authorization check happens BEFORE any other call, including {@code users.upsert(...)} and both
 * services' own preview/apply methods -- unlike {@link IndiaMarketUniverseController#refresh}, which
 * upserts the calling user before its own admin check. That ordering is deliberate here: this change's
 * requirement is "reject unauthorized requests without invoking services," read strictly, so nothing
 * with any side effect (including the otherwise-harmless user-upsert) runs until the ADMIN role has
 * already been confirmed present on the request.
 */
@RestController
@RequestMapping("/api/v1/market-universe/india/nse-etf")
public class NseEtfUniverseController {
    private final NseEtfUniversePreviewService preview;
    private final NseEtfUniverseApplyService apply;
    private final AppUserProvisioner users;

    public NseEtfUniverseController(NseEtfUniversePreviewService preview, NseEtfUniverseApplyService apply,
            AppUserProvisioner users) {
        this.preview = preview;
        this.apply = apply;
        this.users = users;
    }

    @GetMapping("/preview")
    public NseEtfUniversePreviewService.Preview preview(HttpServletRequest request) {
        AuthenticatedUser user = AuthenticatedUserResolver.require(request);
        requireAdmin(user);
        users.upsert(user);
        return preview.preview();
    }

    @PostMapping("/apply")
    public ResponseEntity<?> apply(HttpServletRequest request) {
        AuthenticatedUser user = AuthenticatedUserResolver.require(request);
        requireAdmin(user);
        users.upsert(user);
        return apply.apply()
                .<ResponseEntity<?>>map(ResponseEntity::ok)
                .orElseGet(() -> ResponseEntity.status(HttpStatus.CONFLICT)
                        .body(Map.of("error", "NSE_ETF_UNIVERSE_APPLY_ALREADY_RUNNING")));
    }

    private static void requireAdmin(AuthenticatedUser user) {
        boolean admin = user.roles().stream().anyMatch("ADMIN"::equalsIgnoreCase);
        if (!admin) {
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "Admin role required");
        }
    }
}
