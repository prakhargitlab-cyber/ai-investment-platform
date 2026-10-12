package com.aiinvestment.portfolio.api;

import com.aiinvestment.portfolio.application.NseEtfUniverseApplyService;
import com.aiinvestment.portfolio.application.NseEtfUniversePreviewService;
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

/** ADMIN-only preview and explicit missing-ETF synchronization; neither provisions users. */
@RestController
@RequestMapping("/api/v1/market-universe/india/nse-etf")
public class NseEtfUniverseController {
    private final NseEtfUniversePreviewService preview;
    private final NseEtfUniverseApplyService apply;

    public NseEtfUniverseController(NseEtfUniversePreviewService preview, NseEtfUniverseApplyService apply) {
        this.preview = preview;
        this.apply = apply;
    }

    @GetMapping("/preview")
    public NseEtfUniversePreviewService.Preview preview(HttpServletRequest request) {
        AuthenticatedUser user = AuthenticatedUserResolver.require(request);
        requireAdmin(user);
        return preview.preview();
    }

    @PostMapping("/apply")
    public ResponseEntity<?> apply(HttpServletRequest request) {
        AuthenticatedUser user = AuthenticatedUserResolver.require(request);
        requireAdmin(user);
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
