package com.aiinvestment.auth;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.ComponentScan;

@SpringBootApplication
@ComponentScan("com.aiinvestment")
@EnableConfigurationProperties({AuthProperties.class, RoleAdminProperties.class, FirstAdminBootstrapProperties.class})
public class AuthServiceApplication {
    public static void main(String[] args) {
        var context = SpringApplication.run(AuthServiceApplication.class, args);
        if (context.getEnvironment().acceptsProfiles(org.springframework.core.env.Profiles.of("role-admin-cli", "first-admin-bootstrap"))) {
            System.exit(SpringApplication.exit(context));
        }
    }
}
