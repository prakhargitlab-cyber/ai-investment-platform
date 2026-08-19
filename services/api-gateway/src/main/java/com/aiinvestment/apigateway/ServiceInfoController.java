package com.aiinvestment.apigateway;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;

@RestController
@RequestMapping("/api/api-gateway")
public class ServiceInfoController {
    @GetMapping("/info")
    public Map<String, String> info() {
        return Map.of("service", "api-gateway", "status", "starting-foundation");
    }
}
