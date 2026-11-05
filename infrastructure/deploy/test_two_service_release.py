"""Offline tests for explicit two-Deployment CAS patch contract."""
import copy
import unittest

from target_patch_plan import Blocked
from test_target_patch_plan import make_deployment
from two_service_release import patch_operations


class GuardedPatchTests(unittest.TestCase):
    def test_gateway_patch_only_image_and_one_env(self):
        name = "api-gateway"
        obj = make_deployment(name, "gateway:old", [
            {"name": "SAFE_OTHER_ENV", "valueFrom": {
                "secretKeyRef": {"name": "do-not-touch", "key": "key"}}},
            {"name": "GATEWAY_CORS_ALLOWED_ORIGINS", "value": "old"}
        ])
        original = copy.deepcopy(obj)
        patch = patch_operations(obj, name,
                                 "localhost:5001/ai-investment/api-gateway:new",
                                 "http://localhost:18080")
        self.assertEqual(obj, original)
        self.assertEqual(patch[0], {
            "op": "test", "path": "/metadata/resourceVersion", "value": "42"})
        self.assertEqual([p["op"] for p in patch],
                         ["test", "test", "replace", "test", "replace"])
        self.assertNotIn("do-not-touch", repr(patch))
        self.assertEqual(patch[-1]["path"],
                         "/spec/template/spec/containers/0/env/1")
        self.assertEqual(patch[-1]["value"],
                         {"name": "GATEWAY_CORS_ALLOWED_ORIGINS",
                          "value": "http://localhost:18080"})

    def test_auth_missing_env_appended_without_touching_existing(self):
        name = "auth-service"
        obj = make_deployment(name, "auth:old", [
            {"name": "APP_PUBLIC_URL", "value": "http://localhost:18080"}])
        patch = patch_operations(obj, name,
                                 "localhost:5001/ai-investment/auth-service:new",
                                 "http://localhost:18080")
        self.assertEqual(patch[-1]["op"], "add")
        self.assertEqual(patch[-1]["path"],
                         "/spec/template/spec/containers/0/env/-")
        self.assertEqual(patch[-1]["value"]["name"],
                         "AUTH_ALLOWED_PUBLIC_ORIGINS")

    def test_unchanged_has_only_resourceversion_test(self):
        name = "auth-service"
        image = "localhost:5001/ai-investment/auth-service:new"
        obj = make_deployment(name, image, [
            {"name": "AUTH_ALLOWED_PUBLIC_ORIGINS", "value": "allowed"}])
        patch = patch_operations(obj, name, image, "allowed")
        self.assertEqual(len(patch), 1)

    def test_prohibit_non_helm_identity(self):
        obj = make_deployment("auth-service", "old")
        obj["metadata"]["labels"]["app.kubernetes.io/managed-by"] = "manual"
        with self.assertRaises(Blocked):
            patch_operations(obj, "auth-service",
                             "localhost:5001/ai-investment/auth-service:new",
                             "allowed")

    def test_prohibit_secret_sourced_origin(self):
        obj = make_deployment("auth-service", "old", [{
            "name": "AUTH_ALLOWED_PUBLIC_ORIGINS",
            "valueFrom": {"secretKeyRef": {"name": "foo", "key": "bar"}}
        }])
        with self.assertRaises(Blocked):
            patch_operations(obj, "auth-service",
                             "localhost:5001/ai-investment/auth-service:new",
                             "allowed")


if __name__ == "__main__":
    unittest.main()
