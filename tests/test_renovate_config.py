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


if __name__ == "__main__":
    unittest.main()
