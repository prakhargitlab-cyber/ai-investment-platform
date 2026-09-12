package com.aiinvestment.broker.runtime;

import io.fabric8.kubernetes.api.model.Pod;
import io.fabric8.kubernetes.api.model.Service;
import io.fabric8.kubernetes.client.KubernetesClient;

interface Fabric8RuntimeOperations {
    Pod getPod(String namespace, String name);
    Pod createPod(String namespace, Pod pod);
    boolean deletePod(String namespace, String name);
    Service getService(String namespace, String name);
    Service createService(String namespace, Service service);
    boolean deleteService(String namespace, String name);

    final class ClientBacked implements Fabric8RuntimeOperations {
        private final KubernetesClient client;
        ClientBacked(KubernetesClient client) { this.client = client; }
        public Pod getPod(String ns, String name) { return client.pods().inNamespace(ns).withName(name).get(); }
        public Pod createPod(String ns, Pod pod) { return client.pods().inNamespace(ns).resource(pod).create(); }
        public boolean deletePod(String ns, String name) { return Boolean.TRUE.equals(client.pods().inNamespace(ns).withName(name).delete()); }
        public Service getService(String ns, String name) { return client.services().inNamespace(ns).withName(name).get(); }
        public Service createService(String ns, Service service) { return client.services().inNamespace(ns).resource(service).create(); }
        public boolean deleteService(String ns, String name) { return Boolean.TRUE.equals(client.services().inNamespace(ns).withName(name).delete()); }
    }
}
