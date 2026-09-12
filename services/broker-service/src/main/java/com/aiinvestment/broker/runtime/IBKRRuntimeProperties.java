package com.aiinvestment.broker.runtime;

import org.springframework.boot.context.properties.ConfigurationProperties;

@ConfigurationProperties(prefix = "broker.ibkr.runtime")
public record IBKRRuntimeProperties(
        String mode,
        String namespace,
        String image,
        String imagePullPolicy,
        String serviceAccountName,
        int port,
        int startupTimeoutSeconds,
        String apiServer,
        String serviceAccountTokenFile,
        String gatewayPackageClaimName,
        String gatewayPackageMountPath,
        boolean gatewayPackageReadOnly,
        String workspaceMountPath,
        String workspaceMedium,
        String workspaceSizeLimit,
        String internalTokenSecretName,
        String internalTokenSecretKey,
        String gatewayBaseUrl,
        boolean gatewayTlsVerify,
        String loginPublicBaseUrl
) {
    public boolean kubernetes() { return "KUBERNETES".equalsIgnoreCase(mode); }
}
