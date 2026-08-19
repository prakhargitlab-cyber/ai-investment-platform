package com.aiinvestment.research;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.annotation.ComponentScan;

@SpringBootApplication
@ComponentScan("com.aiinvestment")
public class ResearchServiceApplication {
    public static void main(String[] args) {
        SpringApplication.run(ResearchServiceApplication.class, args);
    }
}
