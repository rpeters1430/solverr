import unittest
from pathlib import Path

import yaml


class TestDockerPublishWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        workflow = Path(__file__).resolve().parents[1] / ".github/workflows/docker-publish.yml"
        cls.workflow = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        cls.steps = cls.workflow["jobs"]["build-and-push"]["steps"]

    def test_candidate_retry_is_bounded_and_reuses_build_inputs(self):
        builds = [step for step in self.steps if step.get("id") in {"build", "build-retry"}]
        self.assertEqual(len(builds), 2)
        first, retry = builds
        self.assertTrue(first["continue-on-error"])
        self.assertEqual(retry["if"], "steps.build.outcome == 'failure'")
        self.assertFalse(retry.get("continue-on-error", False))
        self.assertEqual(first["uses"], retry["uses"])
        self.assertEqual(first["with"], retry["with"])
        self.assertEqual(first["with"]["sbom"], "${{ env.PUBLISH == 'true' }}")
        self.assertEqual(first["with"]["provenance"], "${{ env.PUBLISH == 'true' }}")
        self.assertEqual(first["with"]["push"], "${{ env.PUBLISH == 'true' }}")
        self.assertEqual(first["with"]["load"], "${{ env.PUBLISH != 'true' }}")
        wait = self.steps[self.steps.index(retry) - 1]
        self.assertEqual(wait["if"], retry["if"])
        self.assertEqual(wait["run"], "sleep 15")

    def test_validation_uses_successful_build_digest_and_still_gates_promotion(self):
        candidate = next(step for step in self.steps if step.get("id") == "candidate")
        self.assertEqual(
            candidate["env"]["DIGEST"],
            "${{ steps.build.outputs.digest || steps.build-retry.outputs.digest }}",
        )
        self.assertNotIn("if", candidate)
        self.assertIn('test -n "$DIGEST"', candidate["run"])
        checks = [
            "Smoke test the built image",
            "Smoke test Camoufox as UGREEN UID/GID",
            "Scan image for vulnerabilities (trivy)",
            "Promote validated image to release tags",
        ]
        indices = []
        for name in checks:
            step = next(step for step in self.steps if step["name"] == name)
            indices.append(self.steps.index(step))
            self.assertFalse(step.get("continue-on-error", False))
            self.assertNotIn("always()", step.get("if", ""))
            self.assertIn("${{ steps.candidate.outputs.ref }}", str(step))
        self.assertEqual(indices, sorted(indices))
        self.assertGreater(indices[0], self.steps.index(candidate))


if __name__ == "__main__":
    unittest.main()
