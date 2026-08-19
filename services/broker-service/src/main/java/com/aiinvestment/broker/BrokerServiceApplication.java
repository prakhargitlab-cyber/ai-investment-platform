package com.aiinvestment.broker;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.annotation.ComponentScan;

@SpringBootApplication
@ComponentScan("com.aiinvestment")
public class BrokerServiceApplication {
    public static void main(String[] args) {
        SpringApplication.run(BrokerServiceApplication.class, args);
    }
}
