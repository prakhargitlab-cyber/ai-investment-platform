"""Offline tests of read-only targeted patch planner."""
import copy
import unittest

from target_patch_plan import (
    Blocked, NAMESPACE, RELEASE, PINNED_RESEARCH_IMAGE,
    target_plan, validate_research,
)


def make_deployment(name, image, env=None):
    return {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {
            "name": name, "namespace": NAMESPACE, "resourceVersion": "42",
            "labels": {"app.kubernetes.io/instance": RELEASE,
                       "app.kubernetes.io/managed-by": "Helm"},
            "annotations": {
                "meta.helm.sh/release-name": RELEASE,
                "meta.helm.sh/release-namespace": NAMESPACE,
            },
        },
        "spec": {"template": {"spec": {"containers": [{
            "name": name, "image": image, "env": env or [],
        }]}}},
        "status": {"readyReplicas": 1},
    }


class PlannerTests(unittest.TestCase):
    def test_scoped_plan_does_not_mutate_original(self):
        obj = make_deployment("auth-service", "registry/auth:old")
        original = copy.deepcopy(obj)
        result = target_plan(obj, "auth-service",
                             "localhost:5001/ai-investment/auth-service:new",
                             "http://localhost:18080")
        self.assertTrue(result["image_change"])
        self.assertTrue(result["origin_change"])
        self.assertEqual(obj, original)

    def test_existing_origin_unchanged(self):
        obj = make_deployment("api-gateway", "localhost:5001/ai-investment/api-gateway:new",
                              [{"name": "GATEWAY_CORS_ALLOWED_ORIGINS",
                                "value": "http://localhost:18080"}])
        result = target_plan(obj, "api-gateway",
                             "localhost:5001/ai-investment/api-gateway:new",
                             "http://localhost:18080")
        self.assertFalse(result["image_change"])
        self.assertFalse(result["origin_change"])

    def test_non_helm_deployment_blocked(self):
        obj = make_deployment("auth-service", "existing")
        del obj["metadata"]["annotations"]["meta.helm.sh/release-name"]
        with self.assertRaises(Blocked):
            target_plan(obj, "auth-service",
                        "localhost:5001/ai-investment/auth-service:new", "origin")

    def test_secret_origin_source_blocked(self):
        obj = make_deployment("auth-service", "existing",
                              [{"name": "AUTH_ALLOWED_PUBLIC_ORIGINS",
                                "valueFrom": {"secretKeyRef": {"name": "secret", "key": "value"}}}])
        with self.assertRaises(Blocked):
            target_plan(obj, "auth-service",
                        "localhost:5001/ai-investment/auth-service:new", "origin")

    def test_duplicate_env_blocked(self):
        obj = make_deployment("api-gateway", "existing",
                              [{"name": "GATEWAY_CORS_ALLOWED_ORIGINS", "value": "x"}] * 2)
        with self.assertRaises(Blocked):
            target_plan(obj, "api-gateway",
                        "localhost:5001/ai-investment/api-gateway:new", "origin")

    def test_research_pin_is_enforced(self):
        validate_research(make_deployment("research-engine", PINNED_RESEARCH_IMAGE))
        with self.assertRaises(Blocked):
            validate_research(make_deployment("research-engine", "research:rolled-back"))


if __name__ == "__main__":
    unittest.main()
