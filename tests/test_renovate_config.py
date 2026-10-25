import json
import unittest
from pathlib import Path


class TestRenovateConfig(unittest.TestCase):
    def test_solverr_image_updates_are_disabled(self):
        renovate_config = Path(__file__).resolve().parents[1] / "renovate.json"
        data = json.loads(renovate_config.read_text(encoding="utf-8"))

        self.assertIn("packageRules", data)

        matching_rules = [
            rule
            for rule in data["packageRules"]
            if "ghcr.io/rpeters1430/solverr" in rule.get("matchPackageNames", [])
        ]
        self.assertTrue(matching_rules, "Missing Renovate rule for solverr image")
        self.assertTrue(
            any(rule.get("enabled") is False for rule in matching_rules),
            "Renovate should ignore updates to ghcr.io/rpeters1430/solverr",
        )

    def test_python_version_is_restricted_below_3_15(self):
        renovate_config = Path(__file__).resolve().parents[1] / "renovate.json"
        data = json.loads(renovate_config.read_text(encoding="utf-8"))

        matching_rules = [
            rule
            for rule in data.get("packageRules", [])
            if "python" in rule.get("matchPackageNames", [])
        ]
        self.assertTrue(matching_rules, "Missing Renovate rule for python package")
        self.assertTrue(
            any(rule.get("allowedVersions") == "<3.15" for rule in matching_rules),
            "Renovate should restrict python to <3.15 to prevent Camoufox Base85 decode errors",
        )


if __name__ == "__main__":
    unittest.main()
