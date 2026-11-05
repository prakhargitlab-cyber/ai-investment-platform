"""Offline contract tests for non-applicable two-service scope simulation."""
import copy
import unittest

from scoped_candidate import ScopeViolation, simulate


def deployment(name, image, with_env):
    return {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": name},
        "spec": {"template": {"spec": {
            "containers": [{
                "name": name, "image": image,
                "env": [{"name": "SAFE_OTHER_ENV", "value": "untouched"}] + with_env
            }]
        }}},
    }


class ScopedCandidateTests(unittest.TestCase):
    def setUp(self):
        self.spec = {
            "images": {name: {"image": "registry/" + name + ":approved"}
                       for name in ("api-gateway", "auth-service")},
            "origins": ["http://localhost:3000", "http://localhost:18080"],
        }
        self.old = {}
        self.new = {}
        for name in self.spec["images"]:
            key = ("apps/v1", "Deployment", name)
            self.old[key] = deployment(name, "registry/" + name + ":old", [])
            env_name = ("GATEWAY_CORS_ALLOWED_ORIGINS" if name == "api-gateway"
                        else "AUTH_ALLOWED_PUBLIC_ORIGINS")
            self.new[key] = deployment(name, self.spec["images"][name]["image"],
                                       [{"name": env_name,
                                         "value": ",".join(self.spec["origins"])}])
        secret_key = ("v1", "Secret", "dev-postgres")
        research_key = ("apps/v1", "Deployment", "research-engine")
        self.old[secret_key] = {"apiVersion": "v1", "kind": "Secret",
                                "metadata": {"name": "dev-postgres"},
                                "data": {"POSTGRES_PASSWORD": "original"}}
        self.old[research_key] = deployment("research-engine", "research:pinned", [])
        self.new[secret_key] = copy.deepcopy(self.old[secret_key])
        self.new[secret_key]["data"]["POSTGRES_PASSWORD"] = "unexpected-drift"
        self.new[research_key] = deployment("research-engine", "research:rollback", [])

    def test_surgical_only_changes_and_source_unmodified(self):
        old = copy.deepcopy(self.old)
        new = copy.deepcopy(self.new)
        out = simulate(self.old, self.new, self.spec)
        self.assertEqual(self.old, old)
        self.assertEqual(self.new, new)
        for key in self.old:
            if key[2] not in ("auth-service", "api-gateway"):
                self.assertEqual(out[key], old[key])
        for name in ("auth-service", "api-gateway"):
            key = ("apps/v1", "Deployment", name)
            self.assertEqual(out[key]["spec"]["template"]["spec"]["containers"][0]["image"],
                             self.spec["images"][name]["image"])

    def test_wrong_image_is_rejected(self):
        self.new[("apps/v1", "Deployment", "auth-service")]["spec"]["template"]["spec"]["containers"][0]["image"] = "malicious"
        with self.assertRaises(ScopeViolation):
            simulate(self.old, self.new, self.spec)

    def test_wrong_origins_are_rejected(self):
        c = self.new[("apps/v1", "Deployment", "api-gateway")]["spec"]["template"]["spec"]["containers"][0]
        c["env"][-1]["value"] = "*"
        with self.assertRaises(ScopeViolation):
            simulate(self.old, self.new, self.spec)

    def test_resource_added_or_removed_is_rejected(self):
        del self.new[("v1", "Secret", "dev-postgres")]
        with self.assertRaises(ScopeViolation):
            simulate(self.old, self.new, self.spec)


if __name__ == "__main__":
    unittest.main()
