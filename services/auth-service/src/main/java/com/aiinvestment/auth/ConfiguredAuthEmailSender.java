package com.aiinvestment.auth;

import org.springframework.stereotype.Component;
import org.springframework.mail.SimpleMailMessage;
import org.springframework.mail.javamail.JavaMailSenderImpl;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import java.util.UUID;
import java.util.Properties;

@Component
public class ConfiguredAuthEmailSender implements AuthEmailSender {
    private static final Logger logger = LoggerFactory.getLogger(ConfiguredAuthEmailSender.class);
    private final AuthProperties properties;
    public ConfiguredAuthEmailSender(AuthProperties properties) { this.properties = properties; }
    public boolean isConfigured() { return "DEV_LOG".equals(properties.emailMode()) ? properties.devLoginEnabled() : "SMTP".equals(properties.emailMode()) && smtpConfigured(); }
    public void sendVerification(UUID userId, String email, String rawToken) { send("VERIFICATION", userId, email, rawToken, "/verify-email?token="); }
    public void sendPasswordReset(UUID userId, String email, String rawToken) { send("PASSWORD_RESET", userId, email, rawToken, "/reset-password?token="); }
    private void send(String kind, UUID userId, String email, String token, String path) { if ("DEV_LOG".equals(properties.emailMode()) && properties.devLoginEnabled()) { logger.info("auth_dev_email_suppressed kind={} userId={} tokenPresent={}", kind, userId, token != null && !token.isBlank()); return; } if (!"SMTP".equals(properties.emailMode()) || !smtpConfigured()) throw new IllegalStateException("Auth email delivery is not configured"); JavaMailSenderImpl sender = new JavaMailSenderImpl(); sender.setHost(properties.smtpHost()); sender.setPort(properties.smtpPort()); sender.setUsername(properties.smtpUsername()); sender.setPassword(properties.smtpPassword()); configureStartTls(sender.getJavaMailProperties()); SimpleMailMessage message = new SimpleMailMessage(); message.setTo(email); message.setFrom(properties.smtpFrom()); message.setSubject(kind.equals("VERIFICATION") ? "Verify your AI Investment account" : "Reset your AI Investment password"); message.setText("Open this one-time link: " + properties.appPublicUrl().replaceAll("/$", "") + path + token); sender.send(message); }
    static void configureStartTls(Properties mail) { mail.put("mail.smtp.auth", "true"); mail.put("mail.smtp.starttls.enable", "true"); mail.put("mail.smtp.starttls.required", "true"); }
    private boolean smtpConfigured() { return nonBlank(properties.smtpHost()) && nonBlank(properties.smtpFrom()) && nonBlank(properties.appPublicUrl()) && nonBlank(properties.smtpUsername()) && nonBlank(properties.smtpPassword()); }
    private static boolean nonBlank(String value) { return value != null && !value.isBlank(); }
}
