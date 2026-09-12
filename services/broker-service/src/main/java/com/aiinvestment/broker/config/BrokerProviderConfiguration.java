package com.aiinvestment.broker.config;

import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Configuration;
import com.aiinvestment.broker.runtime.IBKRRuntimeProperties;
import com.aiinvestment.broker.runtime.IBKRKubernetesRuntimeSpecProperties;

@Configuration
@EnableConfigurationProperties({IBKRProviderProperties.class, ICICIDirectProviderProperties.class,
        HDFCSecuritiesProviderProperties.class, IBKRRuntimeProperties.class,
        IBKRKubernetesRuntimeSpecProperties.class})
public class BrokerProviderConfiguration {
}
