package com.aiinvestment.broker.provider.icici;

import com.aiinvestment.broker.application.BrokerProviderException;
import com.aiinvestment.broker.config.ICICIDirectProviderProperties;
import com.aiinvestment.broker.security.SecretProvider;
import com.aiinvestment.shared.domain.broker.BrokerType;
import org.springframework.stereotype.Component;
import org.springframework.beans.factory.annotation.Autowired;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.time.temporal.ChronoUnit;
import java.util.HexFormat;
import java.util.Map;

@Component
public class BreezeRequestSigner {
    private static final DateTimeFormatter TIMESTAMP = DateTimeFormatter
            .ofPattern("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'").withZone(ZoneOffset.UTC);
    private final ICICIDirectProviderProperties properties;
    private final SecretProvider secretProvider;
    private final Clock clock;

    @Autowired
    public BreezeRequestSigner(ICICIDirectProviderProperties properties, SecretProvider secretProvider) {
        this(properties, secretProvider, Clock.systemUTC());
    }

    BreezeRequestSigner(ICICIDirectProviderProperties properties, SecretProvider secretProvider, Clock clock) {
        this.properties = properties;
        this.secretProvider = secretProvider;
        this.clock = clock;
    }

    public BreezeSignedHeaders sign(String exactJsonBody, String sessionToken) {
        if (exactJsonBody == null || sessionToken == null || sessionToken.isBlank()) {
            throw new IllegalArgumentException("Request body and session token are required");
        }
        String secret = secretProvider.getSecret(properties.secretKeyReference())
                .filter(value -> !value.isBlank())
                .orElseThrow(() -> BrokerProviderException.secretUnavailable(BrokerType.ICICI_DIRECT));
        return sign(exactJsonBody, sessionToken, properties.appKey(), secret);
    }

    public BreezeSignedHeaders sign(String exactJsonBody, String sessionToken, String appKey, String secret) {
        if (exactJsonBody == null || sessionToken == null || sessionToken.isBlank()
                || appKey == null || appKey.isBlank() || secret == null || secret.isBlank()) {
            throw new IllegalArgumentException("Breeze signing inputs are required");
        }
        String timestamp = TIMESTAMP.format(Instant.now(clock).truncatedTo(ChronoUnit.MILLIS));
        String checksum = sha256(timestamp + exactJsonBody + secret);
        return new BreezeSignedHeaders(Map.of(
                "Content-Type", "application/json",
                "X-Checksum", "token " + checksum,
                "X-Timestamp", timestamp,
                "X-AppKey", appKey,
                "X-SessionToken", sessionToken));
    }

    private static String sha256(String value) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                    .digest(value.getBytes(StandardCharsets.UTF_8)));
        } catch (Exception exception) {
            throw new IllegalStateException("SHA-256 is unavailable");
        }
    }
}
