package com.aiinvestment.auth;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

class AuthPublicOriginPolicyTest {
    private static final String ALLOWED =
            "http://localhost:13000,http://localhost:18080,http://localhost:18081";
    private static final String FALLBACK = "http://localhost:18080";

    @Test
    void resetLinkUsesTheExactApprovedRequestOrigin() {
        for (String origin : ALLOWED.split(",")) {
            assertThat(AuthPublicOriginPolicy.resolve(origin, ALLOWED, FALLBACK))
                    .isEqualTo(origin);
        }
    }

    @Test
    void missingOrUntrustedOriginUsesConfiguredPublicUrl() {
        assertThat(AuthPublicOriginPolicy.resolve(null, ALLOWED, FALLBACK)).isEqualTo(FALLBACK);
        assertThat(AuthPublicOriginPolicy.resolve("http://evil.example", ALLOWED, FALLBACK))
                .isEqualTo(FALLBACK);
        assertThat(AuthPublicOriginPolicy.resolve("http://localhost:9999", ALLOWED, FALLBACK))
                .isEqualTo(FALLBACK);
        assertThat(AuthPublicOriginPolicy.resolve("http://localhost:13000.evil.example", ALLOWED, FALLBACK))
                .isEqualTo(FALLBACK);
    }

    @Test
    void refusesCredentialsPathsQueriesFragmentsAndMalformedOrigins() {
        for (String origin : new String[]{
                "http://localhost:13000/path",
                "http://localhost:13000?token=abc",
                "http://localhost:13000/#fragment",
                "http://user@localhost:13000",
                "http://localhost:13000@evil.example",
                "http://localhost:13000\n",
                "javascript:alert(1)",
                "https://localhost:13000",
                "http://example.org:13000",
                "http://localhost:13000/",
        }) {
            assertThat(AuthPublicOriginPolicy.resolve(origin, ALLOWED, FALLBACK))
                    .as("Reject untrusted origin shape")
                    .isEqualTo(FALLBACK);
        }
    }
}
