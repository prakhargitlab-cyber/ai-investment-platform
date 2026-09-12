package com.aiinvestment.broker.runtime;

import org.springframework.boot.context.properties.ConfigurationProperties;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Trusted infrastructure input for a future dynamic IBKR runtime Pod/Service builder.
 * This deliberately contains no Kubernetes client types or secret values. Mandatory
 * connector identity labels are added by the future builder and take precedence over
 * configurable metadata below.
 */
@ConfigurationProperties(prefix = "broker.ibkr.runtime.kubernetes.spec")
public class IBKRKubernetesRuntimeSpecProperties {
    private String runtimeImage = "";
    private String imagePullPolicy = "IfNotPresent";
    private InitContainer initContainer = new InitContainer();
    private Resources resources = new Resources();
    private String serviceAccountName = "ibkr-runtime-orchestrator";
    private List<String> imagePullSecrets = new ArrayList<>();
    private Map<String, String> podLabels = new LinkedHashMap<>();
    private Map<String, String> podAnnotations = new LinkedHashMap<>();
    private Map<String, String> serviceLabels = new LinkedHashMap<>();
    private Map<String, String> serviceAnnotations = new LinkedHashMap<>();
    private Probe startupProbe = Probe.startup();
    private Probe readinessProbe = Probe.readiness();
    private Probe livenessProbe = Probe.liveness();
    private Ports ports = new Ports();
    private GatewayPackage gatewayPackage = new GatewayPackage();
    private Workspace workspace = new Workspace();
    private InternalTokenSecret internalToken = new InternalTokenSecret();
    private String gatewayBaseUrl = "https://127.0.0.1:5000/v1/api";
    private boolean gatewayTlsVerify = true;
    private String loginPublicBaseUrl = "";
    private Security security = new Security();
    private Map<String, String> nodeSelector = new LinkedHashMap<>();
    private List<TolerationSpec> tolerations = new ArrayList<>();
    private Map<String, Object> affinity = new LinkedHashMap<>();
    private List<Map<String, Object>> topologySpreadConstraints = new ArrayList<>();

    public String getRuntimeImage() { return runtimeImage; }
    public void setRuntimeImage(String runtimeImage) { this.runtimeImage = runtimeImage; }
    public String getImagePullPolicy() { return imagePullPolicy; }
    public void setImagePullPolicy(String imagePullPolicy) { this.imagePullPolicy = imagePullPolicy; }
    public InitContainer getInitContainer() { return initContainer; }
    public void setInitContainer(InitContainer initContainer) { this.initContainer = initContainer; }
    public Resources getResources() { return resources; }
    public void setResources(Resources resources) { this.resources = resources; }
    public String getServiceAccountName() { return serviceAccountName; }
    public void setServiceAccountName(String serviceAccountName) { this.serviceAccountName = serviceAccountName; }
    public List<String> getImagePullSecrets() { return imagePullSecrets; }
    public void setImagePullSecrets(List<String> imagePullSecrets) { this.imagePullSecrets = imagePullSecrets; }
    public Map<String, String> getPodLabels() { return podLabels; }
    public void setPodLabels(Map<String, String> podLabels) { this.podLabels = podLabels; }
    public Map<String, String> getPodAnnotations() { return podAnnotations; }
    public void setPodAnnotations(Map<String, String> podAnnotations) { this.podAnnotations = podAnnotations; }
    public Map<String, String> getServiceLabels() { return serviceLabels; }
    public void setServiceLabels(Map<String, String> serviceLabels) { this.serviceLabels = serviceLabels; }
    public Map<String, String> getServiceAnnotations() { return serviceAnnotations; }
    public void setServiceAnnotations(Map<String, String> serviceAnnotations) { this.serviceAnnotations = serviceAnnotations; }
    public Probe getStartupProbe() { return startupProbe; }
    public void setStartupProbe(Probe startupProbe) { this.startupProbe = startupProbe; }
    public Probe getReadinessProbe() { return readinessProbe; }
    public void setReadinessProbe(Probe readinessProbe) { this.readinessProbe = readinessProbe; }
    public Probe getLivenessProbe() { return livenessProbe; }
    public void setLivenessProbe(Probe livenessProbe) { this.livenessProbe = livenessProbe; }
    public Ports getPorts() { return ports; }
    public void setPorts(Ports ports) { this.ports = ports; }
    public GatewayPackage getGatewayPackage() { return gatewayPackage; }
    public void setGatewayPackage(GatewayPackage gatewayPackage) { this.gatewayPackage = gatewayPackage; }
    public Workspace getWorkspace() { return workspace; }
    public void setWorkspace(Workspace workspace) { this.workspace = workspace; }
    public InternalTokenSecret getInternalToken() { return internalToken; }
    public void setInternalToken(InternalTokenSecret internalToken) { this.internalToken = internalToken; }
    public String getGatewayBaseUrl() { return gatewayBaseUrl; }
    public void setGatewayBaseUrl(String gatewayBaseUrl) { this.gatewayBaseUrl = gatewayBaseUrl; }
    public boolean isGatewayTlsVerify() { return gatewayTlsVerify; }
    public void setGatewayTlsVerify(boolean gatewayTlsVerify) { this.gatewayTlsVerify = gatewayTlsVerify; }
    public String getLoginPublicBaseUrl() { return loginPublicBaseUrl; }
    public void setLoginPublicBaseUrl(String loginPublicBaseUrl) { this.loginPublicBaseUrl = loginPublicBaseUrl; }
    public Security getSecurity() { return security; }
    public void setSecurity(Security security) { this.security = security; }
    public Map<String, String> getNodeSelector() { return nodeSelector; }
    public void setNodeSelector(Map<String, String> nodeSelector) { this.nodeSelector = nodeSelector; }
    public List<TolerationSpec> getTolerations() { return tolerations; }
    public void setTolerations(List<TolerationSpec> tolerations) { this.tolerations = tolerations; }
    public Map<String, Object> getAffinity() { return affinity; }
    public void setAffinity(Map<String, Object> affinity) { this.affinity = affinity; }
    public List<Map<String, Object>> getTopologySpreadConstraints() { return topologySpreadConstraints; }
    public void setTopologySpreadConstraints(List<Map<String, Object>> topologySpreadConstraints) { this.topologySpreadConstraints = topologySpreadConstraints; }

    public static class Resources { private Quantities requests = new Quantities(); private Quantities limits = new Quantities(); public Quantities getRequests(){return requests;} public void setRequests(Quantities requests){this.requests=requests;} public Quantities getLimits(){return limits;} public void setLimits(Quantities limits){this.limits=limits;} }
    public static class Quantities { private String cpu = ""; private String memory = ""; public String getCpu(){return cpu;} public void setCpu(String cpu){this.cpu=cpu;} public String getMemory(){return memory;} public void setMemory(String memory){this.memory=memory;} }
    public static class InitContainer { private String name = "copy-gateway-package"; private String image = ""; private String imagePullPolicy = "IfNotPresent"; private List<String> command = new ArrayList<>(List.of("/bin/sh", "-c")); private List<String> args = new ArrayList<>(List.of(
        "cp -a /opt/ibkr/clientportal.gw-source/. /opt/ibkr/runtime/ && " +
            "chmod u+x /opt/ibkr/runtime/bin/run.sh && " +
            "chown -R 1000:1000 /opt/ibkr/runtime"
    )); public String getName(){return name;} public void setName(String name){this.name=name;} public String getImage(){return image;} public void setImage(String image){this.image=image;} public String getImagePullPolicy(){return imagePullPolicy;} public void setImagePullPolicy(String imagePullPolicy){this.imagePullPolicy=imagePullPolicy;} public List<String> getCommand(){return command;} public void setCommand(List<String> command){this.command=command;} public List<String> getArgs(){return args;} public void setArgs(List<String> args){this.args=args;} }
    public static class Probe { private String path; private int initialDelaySeconds; private int periodSeconds = 10; private int timeoutSeconds = 3; private int failureThreshold = 30; private int successThreshold = 1; static Probe startup(){ Probe p=new Probe(); p.path="/actuator/health/readiness"; return p; } static Probe readiness(){ Probe p=new Probe(); p.path="/actuator/health/readiness"; p.failureThreshold=3; return p; } static Probe liveness(){ Probe p=new Probe(); p.path="/actuator/health/liveness"; p.failureThreshold=3; return p; } public String getPath(){return path;} public void setPath(String path){this.path=path;} public int getInitialDelaySeconds(){return initialDelaySeconds;} public void setInitialDelaySeconds(int v){initialDelaySeconds=v;} public int getPeriodSeconds(){return periodSeconds;} public void setPeriodSeconds(int v){periodSeconds=v;} public int getTimeoutSeconds(){return timeoutSeconds;} public void setTimeoutSeconds(int v){timeoutSeconds=v;} public int getFailureThreshold(){return failureThreshold;} public void setFailureThreshold(int v){failureThreshold=v;} public int getSuccessThreshold(){return successThreshold;} public void setSuccessThreshold(int v){successThreshold=v;} }
    public static class Ports { private int containerPort=8080; private int servicePort=80; private int serviceTargetPort=8080; private int gatewayPort=5000; public int getContainerPort(){return containerPort;} public void setContainerPort(int v){containerPort=v;} public int getServicePort(){return servicePort;} public void setServicePort(int v){servicePort=v;} public int getServiceTargetPort(){return serviceTargetPort;} public void setServiceTargetPort(int v){serviceTargetPort=v;} public int getGatewayPort(){return gatewayPort;} public void setGatewayPort(int v){gatewayPort=v;} }
    public static class GatewayPackage { private String claimName=""; private String mountPath="/opt/ibkr/clientportal.gw-source"; private boolean readOnly=true; public String getClaimName(){return claimName;} public void setClaimName(String v){claimName=v;} public String getMountPath(){return mountPath;} public void setMountPath(String v){mountPath=v;} public boolean isReadOnly(){return readOnly;} public void setReadOnly(boolean v){readOnly=v;} }
    public static class Workspace { private String mountPath="/opt/ibkr/runtime"; private String medium=""; private String sizeLimit=""; public String getMountPath(){return mountPath;} public void setMountPath(String v){mountPath=v;} public String getMedium(){return medium;} public void setMedium(String v){medium=v;} public String getSizeLimit(){return sizeLimit;} public void setSizeLimit(String v){sizeLimit=v;} }
    public static class InternalTokenSecret { private String secretName=""; private String secretKey=""; public String getSecretName(){return secretName;} public void setSecretName(String v){secretName=v;} public String getSecretKey(){return secretKey;} public void setSecretKey(String v){secretKey=v;} }
    public static class Security { private boolean runAsNonRoot=true; private long runAsUser=1000; private boolean allowPrivilegeEscalation=false; private List<String> dropCapabilities=new ArrayList<>(List.of("ALL")); public boolean isRunAsNonRoot(){return runAsNonRoot;} public void setRunAsNonRoot(boolean v){runAsNonRoot=v;} public long getRunAsUser(){return runAsUser;} public void setRunAsUser(long v){runAsUser=v;} public boolean isAllowPrivilegeEscalation(){return allowPrivilegeEscalation;} public void setAllowPrivilegeEscalation(boolean v){allowPrivilegeEscalation=v;} public List<String> getDropCapabilities(){return dropCapabilities;} public void setDropCapabilities(List<String> v){dropCapabilities=v;} }
    public static class TolerationSpec { private String key=""; private String operator=""; private String value=""; private String effect=""; private Long tolerationSeconds; public String getKey(){return key;} public void setKey(String v){key=v;} public String getOperator(){return operator;} public void setOperator(String v){operator=v;} public String getValue(){return value;} public void setValue(String v){value=v;} public String getEffect(){return effect;} public void setEffect(String v){effect=v;} public Long getTolerationSeconds(){return tolerationSeconds;} public void setTolerationSeconds(Long v){tolerationSeconds=v;} }
}
