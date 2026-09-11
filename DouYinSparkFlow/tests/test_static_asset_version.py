import re
import unittest
from pathlib import Path

from webui.app import static_asset_version


REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "DouYinSparkFlow"
TEMPLATES_DIR = SOURCE_ROOT / "webui" / "templates"

# "/static/foo.js?v=20260824" — a stamp somebody has to remember to bump.
HAND_WRITTEN_VERSION = re.compile(r"/static/[^\"']+\?v=(?!\{\{)\d")


class StaticAssetVersionTests(unittest.TestCase):
    """A stale app.js against a newer API reply is a hard crash, not a typo."""

    def test_no_template_pins_a_hand_written_version(self):
        offenders = []
        for template in sorted(TEMPLATES_DIR.glob("*.html")):
            for line_no, line in enumerate(template.read_text(encoding="utf-8").splitlines(), 1):
                if HAND_WRITTEN_VERSION.search(line):
                    offenders.append(f"{template.name}:{line_no}: {line.strip()}")
        self.assertEqual(offenders, [])

    def test_every_static_reference_goes_through_the_helper(self):
        offenders = []
        for template in sorted(TEMPLATES_DIR.glob("*.html")):
            for line_no, line in enumerate(template.read_text(encoding="utf-8").splitlines(), 1):
                if "/static/" in line and "static_version(" not in line:
                    offenders.append(f"{template.name}:{line_no}: {line.strip()}")
        self.assertEqual(offenders, [])

    def test_a_bundled_asset_gets_a_real_token(self):
        for name in ("app.js", "app.css", "lucide.min.js"):
            self.assertNotEqual(static_asset_version(name), "0", name)

    def test_a_missing_asset_falls_back_to_a_token(self):
        self.assertEqual(static_asset_version("does-not-exist.js"), "0")


if __name__ == "__main__":
    unittest.main()
