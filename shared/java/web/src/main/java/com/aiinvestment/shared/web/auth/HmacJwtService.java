package com.aiinvestment.shared.web.auth;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public class HmacJwtService {
    private static final ObjectMapper OBJECT_MAPPER = new ObjectMapper();
    private static final Base64.Encoder ENCODER = Base64.getUrlEncoder().withoutPadding();
    private static final Base64.Decoder DECODER = Base64.getUrlDecoder();
    private static final String ALGORITHM = "HmacSHA256";

    private final byte[] secret;

    public HmacJwtService(String secret) {
        if (secret == null || secret.length() < 32) {
            throw new IllegalArgumentException("JWT HMAC secret must be at least 32 characters");
        }
        this.secret = secret.getBytes(StandardCharsets.UTF_8);
    }

    public String issue(JwtClaims claims) {
        try {
            Map<String, Object> header = Map.of("alg", "HS256", "typ", "JWT");
            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("iss", claims.issuer());
            payload.put("sub", claims.subject());
            payload.put("email", claims.email());
            payload.put("name", claims.displayName());
            payload.put("roles", claims.roles());
            payload.put("iat", claims.issuedAt().getEpochSecond());
            payload.put("exp", claims.expiresAt().getEpochSecond());
            String encodedHeader = encode(OBJECT_MAPPER.writeValueAsBytes(header));
            String encodedPayload = encode(OBJECT_MAPPER.writeValueAsBytes(payload));
            String signingInput = encodedHeader + "." + encodedPayload;
            return signingInput + "." + encode(sign(signingInput));
        } catch (Exception exc) {
            throw new IllegalStateException("Unable to issue JWT", exc);
        }
    }

    public JwtClaims verify(String token, String expectedIssuer) {
        try {
            String[] parts = token.split("\\.");
            if (parts.length != 3) {
                throw new IllegalArgumentException("Invalid JWT format");
            }
            String signingInput = parts[0] + "." + parts[1];
            String expectedSignature = encode(sign(signingInput));
            if (!constantTimeEquals(expectedSignature, parts[2])) {
                throw new IllegalArgumentException("Invalid JWT signature");
            }
            Map<String, Object> claims = OBJECT_MAPPER.readValue(DECODER.decode(parts[1]), new TypeReference<>() {});
            String issuer = stringClaim(claims, "iss");
            if (!issuer.equals(expectedIssuer)) {
                throw new IllegalArgumentException("Invalid JWT issuer");
            }
            Instant expiresAt = Instant.ofEpochSecond(numberClaim(claims, "exp").longValue());
            Instant issuedAt = Instant.ofEpochSecond(numberClaim(claims, "iat").longValue());
            if (!expiresAt.isAfter(Instant.now())) {
                throw new IllegalArgumentException("JWT expired");
            }
            return new JwtClaims(
                    issuer,
                    stringClaim(claims, "sub"),
                    nullableStringClaim(claims, "email"),
                    nullableStringClaim(claims, "name"),
                    rolesClaim(claims.get("roles")),
                    issuedAt,
                    expiresAt
            );
        } catch (IllegalArgumentException exc) {
            throw exc;
        } catch (Exception exc) {
            throw new IllegalArgumentException("Invalid JWT", exc);
        }
    }

    private static String encode(byte[] bytes) {
        return ENCODER.encodeToString(bytes);
    }

    private byte[] sign(String value) throws Exception {
        Mac mac = Mac.getInstance(ALGORITHM);
        mac.init(new SecretKeySpec(secret, ALGORITHM));
        return mac.doFinal(value.getBytes(StandardCharsets.UTF_8));
    }

    private static String stringClaim(Map<String, Object> claims, String name) {
        String value = nullableStringClaim(claims, name);
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException("Missing JWT claim: " + name);
        }
        return value;
    }

    private static String nullableStringClaim(Map<String, Object> claims, String name) {
        Object value = claims.get(name);
        return value instanceof String string ? string : null;
    }

    private static Number numberClaim(Map<String, Object> claims, String name) {
        Object value = claims.get(name);
        if (!(value instanceof Number number)) {
            throw new IllegalArgumentException("Missing JWT claim: " + name);
        }
        return number;
    }

    private static List<String> rolesClaim(Object value) {
        if (!(value instanceof List<?> values)) {
            return List.of();
        }
        return values.stream().filter(String.class::isInstance).map(String.class::cast).toList();
    }

    private static boolean constantTimeEquals(String left, String right) {
        if (left.length() != right.length()) {
            return false;
        }
        int result = 0;
        for (int i = 0; i < left.length(); i++) {
            result |= left.charAt(i) ^ right.charAt(i);
        }
        return result == 0;
    }
}
