package com.aiinvestment.broker.api;

import java.util.UUID;

public record ICICIDirectLoginResponse(UUID connectionId, String loginUrl, String status) {
}
