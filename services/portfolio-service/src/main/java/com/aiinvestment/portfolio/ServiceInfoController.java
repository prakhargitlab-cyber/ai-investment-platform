package com.aiinvestment.portfolio;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;

@RestController
@RequestMapping("/api/portfolio-service")
public class ServiceInfoController {
    @GetMapping("/info")
    public Map<String, String> info() {
        return Map.of("service", "portfolio-service", "status", "starting-foundation");
    }
}
