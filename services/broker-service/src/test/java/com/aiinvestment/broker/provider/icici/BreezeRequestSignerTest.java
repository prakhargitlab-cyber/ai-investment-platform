package com.aiinvestment.broker.provider.icici;

import com.aiinvestment.broker.security.SecretProvider;
import org.junit.jupiter.api.Test;

import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.Optional;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;

class BreezeRequestSignerTest {
    @Test
    void producesDeterministicOfficialHeadersUsingResolvedDummySecret() {
        AtomicReference<String> resolvedReference = new AtomicReference<>();
        SecretProvider secrets = reference -> {
            resolvedReference.set(reference);
            return Optional.of("dummy-secret");
        };
        BreezeRequestSigner signer = new BreezeRequestSigner(BreezeICICIDirectConnectorTest.properties(), secrets,
                Clock.fixed(Instant.parse("2026-08-29T10:15:30Z"), ZoneOffset.UTC));

        BreezeSignedHeaders signed = signer.sign("{}", "dummy-session-token");

        assertThat(resolvedReference.get()).isEqualTo("ICICI_DUMMY_SECRET_REFERENCE");
        assertThat(signed.values()).containsEntry("Content-Type", "application/json")
                .containsEntry("X-Timestamp", "2026-08-29T10:15:30.000Z")
                .containsEntry("X-AppKey", "dummy+app=key")
                .containsEntry("X-SessionToken", "dummy-session-token")
                .containsEntry("X-Checksum", "token 4d78011e3335ea37eaf9df1b609179f4e09b40498498f0f5e3010e50434df4f9");
    }
}
