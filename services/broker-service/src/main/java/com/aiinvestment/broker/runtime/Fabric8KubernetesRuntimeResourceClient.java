package com.aiinvestment.broker.runtime;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.fabric8.kubernetes.api.model.*;
import io.fabric8.kubernetes.client.KubernetesClient;
import io.fabric8.kubernetes.client.KubernetesClientException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.*;

/** Fabric8-only adapter; lifecycle ownership is verified before this client is called. */
final class Fabric8KubernetesRuntimeResourceClient implements KubernetesRuntimeResourceClient {
    private static final Logger logger = LoggerFactory.getLogger(Fabric8KubernetesRuntimeResourceClient.class);
    static final String CONNECTOR_ID_LABEL = "ai-investment/connector-id";
    private static final Map<String, String> REQUIRED_LABELS = Map.of(
            "app.kubernetes.io/name", "ibkr-connector-runtime",
            "app.kubernetes.io/managed-by", "ai-investment",
            "ai-investment/broker", "ibkr");
    private static final Duration STOP_RECONCILIATION_TIMEOUT = Duration.ofSeconds(5);
    private static final Duration STOP_RECONCILIATION_POLL_INTERVAL = Duration.ofMillis(100);
    private final Fabric8RuntimeOperations operations;
    private final ObjectMapper mapper;
    private final Clock clock;
    private final RuntimeWaitStrategy waitStrategy;
    private final Map<String, String> allocationNamespaces = new java.util.concurrent.ConcurrentHashMap<>();

    Fabric8KubernetesRuntimeResourceClient(KubernetesClient client) { this(new Fabric8RuntimeOperations.ClientBacked(client), new ObjectMapper()); }
    Fabric8KubernetesRuntimeResourceClient(Fabric8RuntimeOperations operations) { this(operations, new ObjectMapper()); }
    Fabric8KubernetesRuntimeResourceClient(Fabric8RuntimeOperations operations, ObjectMapper mapper) {
        this(operations, mapper, Clock.systemUTC(), duration -> Thread.sleep(duration.toMillis()));
    }
    Fabric8KubernetesRuntimeResourceClient(Fabric8RuntimeOperations operations, ObjectMapper mapper, Clock clock,
                                            RuntimeWaitStrategy waitStrategy) {
        this.operations = operations; this.mapper = mapper; this.clock = clock; this.waitStrategy = waitStrategy;
    }

    @Override public RuntimeResources get(String namespace, UUID connectorId) {
        String name = KubernetesIBKRRuntimeOrchestrator.name(connectorId);
        Pod pod = operations.getPod(namespace, name);
        Service service = operations.getService(namespace, name);
        boolean podPresent = pod != null, servicePresent = service != null;
        boolean labels = (!podPresent || hasIdentity(pod.getMetadata(), connectorId))
                && (!servicePresent || hasIdentity(service.getMetadata(), connectorId));
        boolean selector = !servicePresent || selectorMatches(service, connectorId);
        String endpoint = "http://" + name + ":" + servicePort(service, 80);
        return new RuntimeResources(connectorId, "service/" + name, podPresent, servicePresent, labels, selector, endpoint);
    }

    @Override public RuntimeAllocationResult reconcileOrCreate(String namespace, KubernetesRuntimeResource resource) {
        RuntimeResources before;
        try { before = get(namespace, resource.connectorId()); }
        catch (RuntimeException failure) { logFailure("STATUS_READ", resource, "RuntimeResources", failure); throw failure; }
        if (before.state() == RuntimeReconciliationState.IDENTITY_MISMATCH) throw new IllegalStateException("Runtime identity mismatch");
        boolean podCreated = false, serviceCreated = false;
        try {
            if (!before.podPresent()) {
                try { podCreated = createPod(namespace, resource); }
                catch (RuntimeException failure) { logFailure("POD_RECONCILE", resource, "Pod", failure); throw failure; }
            }
            if (!before.servicePresent()) {
                try { serviceCreated = createService(namespace, resource); }
                catch (RuntimeException failure) { logFailure("SERVICE_RECONCILE", resource, "Service", failure); throw failure; }
            }
        } catch (RuntimeException failure) {
            if (serviceCreated) compensateService(namespace, resource, failure);
            if (podCreated) compensatePod(namespace, resource, failure);
            throw failure;
        }
        RuntimeResources after;
        try { after = get(namespace, resource.connectorId()); }
        catch (RuntimeException failure) { logFailure("STATUS_READ", resource, "RuntimeResources", failure); throw failure; }
        if (after.state() == RuntimeReconciliationState.IDENTITY_MISMATCH || !after.podPresent() || !after.servicePresent()) {
            throw new IllegalStateException("Runtime reconciliation did not produce matching resources");
        }
        allocationNamespaces.put(after.runtimeIdentity(), namespace);
        return new RuntimeAllocationResult(resource.connectorId(), after.runtimeIdentity(), after.endpoint(), podCreated, serviceCreated,
                true, true, inspect(namespace, resource.connectorId()) == RuntimeTechnicalStatus.READY, !podCreated && !serviceCreated);
    }

    @Override public RuntimeTechnicalStatus inspect(String namespace, UUID connectorId) {
        RuntimeResources resources = get(namespace, connectorId);
        if (resources.state() == RuntimeReconciliationState.IDENTITY_MISMATCH || resources.state() == RuntimeReconciliationState.SERVICE_ONLY) return RuntimeTechnicalStatus.UNKNOWN;
        if (resources.state() == RuntimeReconciliationState.ABSENT_BOTH) return RuntimeTechnicalStatus.ABSENT;
        Pod pod = operations.getPod(namespace, KubernetesIBKRRuntimeOrchestrator.name(connectorId));
        if (pod == null || pod.getStatus() == null) return RuntimeTechnicalStatus.UNKNOWN;
        String phase = pod.getStatus().getPhase();
        if ("Failed".equalsIgnoreCase(phase) || "Succeeded".equalsIgnoreCase(phase)) return RuntimeTechnicalStatus.FAILED;
        if (!"Running".equalsIgnoreCase(phase)) return RuntimeTechnicalStatus.STARTING;
        return ready(pod) ? RuntimeTechnicalStatus.READY : RuntimeTechnicalStatus.NOT_READY;
    }

    @Override public RuntimeStopResult stop(String namespace, UUID connectorId) {
        RuntimeResources resources = get(namespace, connectorId);
        if (resources.state() == RuntimeReconciliationState.IDENTITY_MISMATCH) return new RuntimeStopResult(connectorId, resources.runtimeIdentity(), false, false, false, false, false);
        boolean podAbsent = !resources.podPresent(), serviceAbsent = !resources.servicePresent();
        boolean podDeleted = !podAbsent && deletePodIfOwned(namespace, connectorId);
        boolean serviceDeleted = !serviceAbsent && deleteServiceIfOwned(namespace, connectorId);
        boolean stopped = awaitAbsent(namespace, connectorId);
        return new RuntimeStopResult(connectorId, resources.runtimeIdentity(), podDeleted, serviceDeleted, podAbsent, serviceAbsent, stopped);
    }

    @Override public void compensate(RuntimeAllocationResult result) {
        String namespace = allocationNamespaces.get(result.runtimeIdentity());
        // No resource can have been created through this client before it records its allocation namespace.
        // A pre-construction failure therefore has nothing connector-scoped to compensate.
        if (namespace == null) return;
        if (result.serviceCreated()) deleteServiceIfOwned(namespace, result.connectorId());
        if (result.podCreated()) deletePodIfOwned(namespace, result.connectorId());
    }

    private boolean createPod(String namespace, KubernetesRuntimeResource resource) {
        try { operations.createPod(namespace, pod(resource)); return true; }
        catch (KubernetesClientException e) { if (e.getCode() == 409 && podMatches(namespace, resource.connectorId())) return false; throw e; }
    }
    private boolean createService(String namespace, KubernetesRuntimeResource resource) {
        try { operations.createService(namespace, service(resource)); return true; }
        catch (KubernetesClientException e) { if (e.getCode() == 409 && serviceMatches(namespace, resource.connectorId())) return false; throw e; }
    }
    private void compensatePod(String namespace, KubernetesRuntimeResource resource, RuntimeException originalFailure) {
        try { deletePodIfOwned(namespace, resource.connectorId()); }
        catch (RuntimeException cleanupFailure) { logFailure("COMPENSATION", resource, "Pod", cleanupFailure); originalFailure.addSuppressed(cleanupFailure); }
    }
    private void compensateService(String namespace, KubernetesRuntimeResource resource, RuntimeException originalFailure) {
        try { deleteServiceIfOwned(namespace, resource.connectorId()); }
        catch (RuntimeException cleanupFailure) { logFailure("COMPENSATION", resource, "Service", cleanupFailure); originalFailure.addSuppressed(cleanupFailure); }
    }
    private static void logFailure(String stage, KubernetesRuntimeResource resource, String resourceKind, RuntimeException failure) {
        if (failure instanceof KubernetesClientException kubernetesFailure) {
            Status status = kubernetesFailure.getStatus();
            String reason = status == null ? null : status.getReason();
            String message = status == null ? null : bounded(status.getMessage());
            if (status != null && status.getDetails() != null && status.getDetails().getCauses() != null && !status.getDetails().getCauses().isEmpty()) {
                status.getDetails().getCauses().stream().limit(10).forEach(cause ->
                        logger.warn("ibkr_runtime_allocation_failure stage={} connector={} resource_kind={} resource_name={} exception={} kubernetes_status={} kubernetes_reason={} kubernetes_message={} validation_field={} validation_reason={} validation_message={}",
                                stage, resource.connectorId(), resourceKind, resource.name(), failure.getClass().getSimpleName(), kubernetesFailure.getCode(), reason, message,
                                bounded(cause.getField()), bounded(cause.getReason()), bounded(cause.getMessage())));
            } else {
                logger.warn("ibkr_runtime_allocation_failure stage={} connector={} resource_kind={} resource_name={} exception={} kubernetes_status={} kubernetes_reason={} kubernetes_message={}",
                        stage, resource.connectorId(), resourceKind, resource.name(), failure.getClass().getSimpleName(), kubernetesFailure.getCode(), reason, message);
            }
            return;
        }
        logger.warn("ibkr_runtime_allocation_failure stage={} connector={} resource_kind={} resource_name={} exception={}",
                stage, resource.connectorId(), resourceKind, resource.name(), failure.getClass().getSimpleName());
    }
    private static String bounded(String value){ return value == null ? null : value.length() <= 512 ? value : value.substring(0, 512); }
    private boolean podMatches(String ns, UUID id) { Pod p=operations.getPod(ns,KubernetesIBKRRuntimeOrchestrator.name(id)); return p!=null && hasIdentity(p.getMetadata(), id); }
    private boolean serviceMatches(String ns, UUID id) { Service s=operations.getService(ns,KubernetesIBKRRuntimeOrchestrator.name(id)); return s!=null && hasIdentity(s.getMetadata(), id) && selectorMatches(s,id); }
    private boolean deletePodIfOwned(String ns, UUID id) { String name=KubernetesIBKRRuntimeOrchestrator.name(id); Pod p=operations.getPod(ns,name); if(p==null) return false; if(!hasIdentity(p.getMetadata(),id)) throw new IllegalStateException("Runtime identity mismatch"); operations.deletePod(ns,name); return true; }
    private boolean deleteServiceIfOwned(String ns, UUID id) { String name=KubernetesIBKRRuntimeOrchestrator.name(id); Service s=operations.getService(ns,name); if(s==null) return false; if(!hasIdentity(s.getMetadata(),id)||!selectorMatches(s,id)) throw new IllegalStateException("Runtime identity mismatch"); operations.deleteService(ns,name); return true; }

    private boolean awaitAbsent(String namespace, UUID connectorId) {
        Instant deadline = Instant.now(clock).plus(STOP_RECONCILIATION_TIMEOUT);
        while (true) {
            RuntimeResources current = get(namespace, connectorId);
            if (current.state() == RuntimeReconciliationState.IDENTITY_MISMATCH) {
                throw new IllegalStateException("Runtime identity mismatch");
            }
            if (!current.podPresent() && !current.servicePresent()) return true;
            if (!Instant.now(clock).isBefore(deadline)) return false;
            try {
                waitStrategy.await(STOP_RECONCILIATION_POLL_INTERVAL);
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
                return false;
            }
        }
    }

    Pod pod(KubernetesRuntimeResource resource) {
        IBKRKubernetesRuntimeSpecProperties spec=resource.spec(); Map<String,String> labels=labels(spec.getPodLabels(),resource.connectorId());
        List<VolumeMount> mounts=List.of(new VolumeMountBuilder().withName("gateway-package").withMountPath(spec.getGatewayPackage().getMountPath()).withReadOnly(true).build(), new VolumeMountBuilder().withName("runtime-workspace").withMountPath(spec.getWorkspace().getMountPath()).build());
        Container init=new ContainerBuilder().withName(spec.getInitContainer().getName()).withImage(spec.getInitContainer().getImage()).withImagePullPolicy(spec.getInitContainer().getImagePullPolicy()).withCommand(spec.getInitContainer().getCommand()).withArgs(spec.getInitContainer().getArgs()).withVolumeMounts(mounts).withSecurityContext(new SecurityContextBuilder().withRunAsUser(0L).withRunAsNonRoot(false).withAllowPrivilegeEscalation(false).withCapabilities(new CapabilitiesBuilder().withDrop("ALL").withAdd("CHOWN").build()).build()).build();
        Container main=new ContainerBuilder().withName("ibkr-connector").withImage(spec.getRuntimeImage()).withImagePullPolicy(spec.getImagePullPolicy())
                .withPorts(new ContainerPortBuilder().withContainerPort(spec.getPorts().getContainerPort()).build()).withVolumeMounts(mounts)
                .withEnv(env(spec)).withResources(resources(spec)).withSecurityContext(containerSecurity(spec)).withStartupProbe(probe(spec.getStartupProbe(),spec)).withReadinessProbe(probe(spec.getReadinessProbe(),spec)).withLivenessProbe(probe(spec.getLivenessProbe(),spec)).build();
        PodSpecBuilder ps=new PodSpecBuilder().withRestartPolicy("Never").withServiceAccountName(spec.getServiceAccountName()).withInitContainers(init).withContainers(main)
                .withVolumes(new VolumeBuilder().withName("gateway-package").withNewPersistentVolumeClaim().withClaimName(spec.getGatewayPackage().getClaimName()).endPersistentVolumeClaim().build(), workspace(spec))
                .withNodeSelector(spec.getNodeSelector()).withTolerations(spec.getTolerations().stream().map(this::toleration).toList()).withSecurityContext(new PodSecurityContextBuilder().withRunAsNonRoot(spec.getSecurity().isRunAsNonRoot()).withRunAsUser(spec.getSecurity().getRunAsUser()).build());
        if(!spec.getImagePullSecrets().isEmpty()) ps.withImagePullSecrets(spec.getImagePullSecrets().stream().map(n->new LocalObjectReferenceBuilder().withName(n).build()).toList());
        if(!spec.getAffinity().isEmpty()) ps.withAffinity(mapper.convertValue(spec.getAffinity(), Affinity.class));
        if(!spec.getTopologySpreadConstraints().isEmpty()) ps.withTopologySpreadConstraints(mapper.convertValue(spec.getTopologySpreadConstraints(), new TypeReference<List<TopologySpreadConstraint>>(){}));
        return new PodBuilder().withNewMetadata().withName(resource.name()).withNamespace(resource.namespace()).withLabels(labels).withAnnotations(spec.getPodAnnotations()).endMetadata().withSpec(ps.build()).build();
    }
    Service service(KubernetesRuntimeResource resource) { IBKRKubernetesRuntimeSpecProperties spec=resource.spec(); return new ServiceBuilder().withNewMetadata().withName(resource.name()).withNamespace(resource.namespace()).withLabels(labels(spec.getServiceLabels(),resource.connectorId())).withAnnotations(spec.getServiceAnnotations()).endMetadata().withNewSpec().withType("ClusterIP").withSelector(labels(Map.of(),resource.connectorId())).withPorts(new ServicePortBuilder().withName("http").withPort(spec.getPorts().getServicePort()).withTargetPort(new IntOrString(spec.getPorts().getServiceTargetPort())).build()).endSpec().build(); }
    private List<EnvVar> env(IBKRKubernetesRuntimeSpecProperties s){ return List.of(new EnvVarBuilder().withName("AIP_IBKR_GATEWAY_PACKAGE_PATH").withValue(s.getWorkspace().getMountPath()).build(),new EnvVarBuilder().withName("AIP_IBKR_GATEWAY_BASE_URL").withValue(s.getGatewayBaseUrl()).build(),new EnvVarBuilder().withName("AIP_IBKR_GATEWAY_TLS_VERIFY").withValue(Boolean.toString(s.isGatewayTlsVerify())).build(),new EnvVarBuilder().withName("AIP_IBKR_LOGIN_PUBLIC_BASE_URL").withValue(s.getLoginPublicBaseUrl()).build(),new EnvVarBuilder().withName("AIP_INTERNAL_TOKEN").withNewValueFrom().withNewSecretKeyRef().withName(s.getInternalToken().getSecretName()).withKey(s.getInternalToken().getSecretKey()).endSecretKeyRef().endValueFrom().build()); }
    private ResourceRequirements resources(IBKRKubernetesRuntimeSpecProperties s){
        ResourceRequirementsBuilder requirements = new ResourceRequirementsBuilder();
        addQuantity(requirements::addToRequests, "cpu", s.getResources().getRequests().getCpu());
        addQuantity(requirements::addToRequests, "memory", s.getResources().getRequests().getMemory());
        addQuantity(requirements::addToLimits, "cpu", s.getResources().getLimits().getCpu());
        addQuantity(requirements::addToLimits, "memory", s.getResources().getLimits().getMemory());
        return requirements.build();
    }
    private static void addQuantity(java.util.function.BiConsumer<String, Quantity> target, String name, String value){ if(value != null && !value.isBlank()) target.accept(name, new Quantity(value)); }
    private SecurityContext containerSecurity(IBKRKubernetesRuntimeSpecProperties s){ return new SecurityContextBuilder().withRunAsNonRoot(s.getSecurity().isRunAsNonRoot()).withRunAsUser(s.getSecurity().getRunAsUser()).withAllowPrivilegeEscalation(s.getSecurity().isAllowPrivilegeEscalation()).withCapabilities(new CapabilitiesBuilder().withDrop(s.getSecurity().getDropCapabilities()).build()).build(); }
    private Probe probe(IBKRKubernetesRuntimeSpecProperties.Probe p,IBKRKubernetesRuntimeSpecProperties s){ return new ProbeBuilder().withNewHttpGet().withPath(p.getPath()).withNewPort(s.getPorts().getContainerPort()).endHttpGet().withInitialDelaySeconds(p.getInitialDelaySeconds()).withPeriodSeconds(p.getPeriodSeconds()).withTimeoutSeconds(p.getTimeoutSeconds()).withFailureThreshold(p.getFailureThreshold()).withSuccessThreshold(p.getSuccessThreshold()).build(); }
    private Volume workspace(IBKRKubernetesRuntimeSpecProperties s){ EmptyDirVolumeSourceBuilder e=new EmptyDirVolumeSourceBuilder(); if(!s.getWorkspace().getMedium().isBlank())e.withMedium(s.getWorkspace().getMedium()); if(!s.getWorkspace().getSizeLimit().isBlank())e.withSizeLimit(new Quantity(s.getWorkspace().getSizeLimit())); return new VolumeBuilder().withName("runtime-workspace").withEmptyDir(e.build()).build(); }
    private Toleration toleration(IBKRKubernetesRuntimeSpecProperties.TolerationSpec t){ return new TolerationBuilder().withKey(t.getKey()).withOperator(t.getOperator()).withValue(t.getValue()).withEffect(t.getEffect()).withTolerationSeconds(t.getTolerationSeconds()).build(); }
    private Map<String,String> labels(Map<String,String> configured,UUID id){ Map<String,String> out=new LinkedHashMap<>(configured); out.putAll(REQUIRED_LABELS); out.put(CONNECTOR_ID_LABEL,id.toString()); return out; }
    private boolean hasIdentity(ObjectMeta m,UUID id){ if(m==null||m.getLabels()==null)return false; Map<String,String> l=m.getLabels(); return id.toString().equals(l.get(CONNECTOR_ID_LABEL))&&REQUIRED_LABELS.entrySet().stream().allMatch(e->e.getValue().equals(l.get(e.getKey()))); }
    private boolean selectorMatches(Service s,UUID id){ return s.getSpec()!=null&&s.getSpec().getSelector()!=null&&labels(Map.of(),id).equals(s.getSpec().getSelector()); }
    private static int servicePort(Service s,int fallback){ return s!=null&&s.getSpec()!=null&&s.getSpec().getPorts()!=null&&!s.getSpec().getPorts().isEmpty()?s.getSpec().getPorts().get(0).getPort():fallback; }
    private static boolean ready(Pod p){ return p.getStatus().getConditions()!=null&&p.getStatus().getConditions().stream().anyMatch(c->"Ready".equals(c.getType())&&"True".equals(c.getStatus())); }
}
