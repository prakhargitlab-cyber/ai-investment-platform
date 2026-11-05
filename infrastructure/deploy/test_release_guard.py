"""Offline release guard contract tests: python -m unittest discover."""
import copy
import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).with_name("release_guard.py")
spec = importlib.util.spec_from_file_location("release_guard", path)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

GOOD = {
    "namespace": "ai-investment",
    "release": "ai-investment-platform",
    "revision": 1,
    "images": {
        "api-gateway": {
            "image": "localhost:5001/ai-investment/api-gateway:auth-reset-pr28-20261105-145100",
            "digest": "sha256:" + "a" * 64,
        },
        "auth-service": {
            "image": "localhost:5001/ai-investment/auth-service:auth-reset-pr28-20261105-145100",
            "digest": "sha256:" + "b" * 64,
        },
    },
    "origins": list(guard.ORIGINS),
}


class ContractTests(unittest.TestCase):
    def test_valid_contract(self):
        self.assertEqual(set(guard.validate_spec(copy.deepcopy(GOOD))), guard.SERVICES)

    def test_mutable_tag_rejected(self):
        invalid = copy.deepcopy(GOOD)
        invalid["images"]["auth-service"]["image"] = "localhost:5001/ai-investment/auth-service:dev"
        with self.assertRaises(guard.Blocked):
            guard.validate_spec(invalid)

    def test_missing_origin_rejected(self):
        invalid = copy.deepcopy(GOOD)
        invalid["origins"] = ["http://localhost:13000"]
        with self.assertRaises(guard.Blocked):
            guard.validate_spec(invalid)

    def test_unexpected_workload_rejected(self):
        invalid = copy.deepcopy(GOOD)
        invalid["images"]["research-engine"] = invalid["images"]["auth-service"]
        with self.assertRaises(guard.Blocked):
            guard.validate_spec(invalid)

    def test_digest_mismatch_rejected(self):
        invalid = copy.deepcopy(GOOD)
        invalid["images"]["auth-service"]["digest"] = "sha256:abc"
        with self.assertRaises(guard.Blocked):
            guard.validate_spec(invalid)

    def test_changed_release_revision_rejected(self):
        invalid = copy.deepcopy(GOOD)
        invalid["revision"] = 2
        with self.assertRaises(guard.Blocked):
            guard.validate_spec(invalid)

    def test_helm_upgrade_keeps_dev_manual_radar_policy(self):
        """Regression: old release values must not defeat newer LOCAL guards."""
        chart = Path(__file__).parents[1] / "helm" / "ai-investment-platform"
        defaults = (chart / "values-dev.yaml").read_text(encoding="utf-8")
        audit = Path(__file__).with_name("helm_impact_audit.py").read_text(encoding="utf-8")
        for flag in (
            "equityManualExecutionEnabled",
            "etfManualExecutionEnabled",
            "etfFullUniverseExecutionEnabled",
        ):
            self.assertIn(flag + ": true", defaults)
            self.assertIn('"research.' + flag + '=true"', audit)
        self.assertIn('"--reuse-values"', audit)
        self.assertIn('"--dry-run=server"', audit)
        self.assertIn("NO_DEPLOYMENT_AUTHORIZATION", audit)



if __name__ == "__main__":
    unittest.main()
