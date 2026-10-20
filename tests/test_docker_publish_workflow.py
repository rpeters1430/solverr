import unittest
from pathlib import Path

import yaml


class TestDockerPublishWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        workflow = Path(__file__).resolve().parents[1] / ".github/workflows/docker-publish.yml"
        cls.workflow = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        cls.steps = cls.workflow["jobs"]["build-and-push"]["steps"]

    def test_native_build_tools_are_installed_only_in_dependency_stage(self):
        dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
        base, stages = dockerfile.split("FROM base AS deps", 1)
        deps, runtime = stages.split("FROM base AS runtime", 1)
        self.assertIn("apt-get install -y --no-install-recommends build-essential libyaml-dev", deps)
        for package in ("build-essential", "libyaml-dev"):
            self.assertLess(deps.index(package), deps.index("uv pip install"))
            self.assertNotIn(package, base)
            self.assertNotIn(package, runtime)
        self.assertIn("COPY --from=deps /opt/venv /opt/venv", runtime)
        self.assertNotIn("COPY --from=deps /usr", runtime)
        self.assertIn("libyaml-0-2", runtime)
        self.assertLess(runtime.index("libyaml-0-2"), runtime.index("ensure_fpgen_model"))

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
            "${{ steps.build.outcome == 'success' && steps.build.outputs.digest || steps.build-retry.outputs.digest }}",
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

    def test_release_notes_are_embedded_before_build_and_release_waits_for_validation(self):
        build_info = next(step for step in self.steps if step.get("name") == "Embed changelog and image revision")
        candidate = next(step for step in self.steps if step.get("name") == "Build candidate image")
        self.assertLess(self.steps.index(build_info), self.steps.index(candidate))
        self.assertIn("EVENT_BEFORE", build_info["env"])
        self.assertIn("RELEASE_CHANGELOG", build_info["env"])
        self.assertIn("build-info.json", (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8"))
        release = self.workflow["jobs"]["create-release"]
        self.assertEqual(release["needs"], "build-and-push")
        self.assertEqual(release["permissions"]["contents"], "write")
        builder = next(step for step in release["steps"] if step.get("id") == "changelog")
        self.assertIn("release-changelog-builder-action@", builder["uses"])


if __name__ == "__main__":
    unittest.main()
