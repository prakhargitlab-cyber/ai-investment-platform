package com.aiinvestment.portfolio.api;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

public record CreatePortfolioRequest(
        @NotBlank @Size(max = 160) String name,
        @NotBlank @Pattern(regexp = "[A-Z]{3}") String baseCurrency
) {
}
