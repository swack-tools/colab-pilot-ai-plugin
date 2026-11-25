import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SiteTests(unittest.TestCase):
    def test_static_site_builds_and_passes_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "site"
            build_res = subprocess.run(
                [sys.executable, str(ROOT / "site" / "build.py"), str(out)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(build_res.returncode, 0, build_res.stderr or build_res.stdout)
            check_res = subprocess.run(
                [sys.executable, str(ROOT / "site" / "check.py"), str(out)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(check_res.returncode, 0, check_res.stderr or check_res.stdout)

    def test_site_js_syntax(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        res = subprocess.run(
            [node, "--check", str(ROOT / "site" / "assets" / "site.js")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr or res.stdout)


if __name__ == "__main__":
    unittest.main()
