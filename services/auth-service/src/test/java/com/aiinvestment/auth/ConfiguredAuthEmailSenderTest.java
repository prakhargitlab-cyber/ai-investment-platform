package com.aiinvestment.auth;

import org.junit.jupiter.api.Test;
import java.util.Properties;
import static org.assertj.core.api.Assertions.assertThat;

class ConfiguredAuthEmailSenderTest {
    @Test void genericSmtpUsesAuthenticationAndRequiredStartTlsWithoutImplicitSsl() {
        Properties properties = new Properties();
        ConfiguredAuthEmailSender.configureStartTls(properties);
        assertThat(properties.getProperty("mail.smtp.auth")).isEqualTo("true");
        assertThat(properties.getProperty("mail.smtp.starttls.enable")).isEqualTo("true");
        assertThat(properties.getProperty("mail.smtp.starttls.required")).isEqualTo("true");
        assertThat(properties.getProperty("mail.smtp.ssl.enable")).isNotEqualTo("true");
    }
}
