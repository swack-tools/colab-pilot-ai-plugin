from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DockerfileTests(unittest.TestCase):
    def test_devcontainer_dockerfile_best_practices(self):
        text = (ROOT / ".devcontainer" / "Dockerfile").read_text(encoding="utf-8")
        lines = text.splitlines()
        self.assertTrue(lines[0].strip() == "# syntax=docker/dockerfile:1")
        self.assertIn("@sha256:", lines[1])
        self.assertNotIn("COPY --from=node", text)
        self.assertIn('SHELL ["/bin/bash", "-euo", "pipefail", "-c"]', text)
        self.assertIn("ARG TARGETARCH", text)
        self.assertIn("amd64)", text)
        self.assertIn("arm64)", text)
        self.assertIn("sha256sum --check", text)
        self.assertIn("--no-same-owner", text)
        self.assertIn("--no-install-recommends", text)
        self.assertIn("rm -rf /var/lib/apt/lists/*", text)


if __name__ == "__main__":
    unittest.main()
