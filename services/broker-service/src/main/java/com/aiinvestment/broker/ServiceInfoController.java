package com.aiinvestment.broker;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;

@RestController
@RequestMapping("/api/broker-service")
public class ServiceInfoController {
    @GetMapping("/info")
    public Map<String, String> info() {
        return Map.of("service", "broker-service", "status", "starting-foundation");
    }
}
