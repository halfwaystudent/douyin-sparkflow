import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SOURCE_ROOT = Path(__file__).resolve().parents[1]
ENSURE_SCRIPT = SOURCE_ROOT / "scripts" / "ensure_novnc_metadata.py"


class NoVncMetadataTests(unittest.TestCase):
    def run_ensure(self, webroot, dpkg_query_source):
        bin_dir = webroot.parent / "bin"
        bin_dir.mkdir(exist_ok=True)
        dpkg_query = bin_dir / "dpkg-query"
        dpkg_query.write_text(dpkg_query_source, encoding="utf-8")
        dpkg_query.chmod(0o755)
        return subprocess.run(
            [sys.executable, str(ENSURE_SCRIPT), "--web-root", str(webroot)],
            env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
            capture_output=True,
            text=True,
        )

    def test_missing_metadata_uses_installed_debian_package_version(self):
        with tempfile.TemporaryDirectory() as directory:
            webroot = Path(directory) / "novnc"
            webroot.mkdir()
            (webroot / "vnc.html").write_text("<!doctype html>", encoding="utf-8")

            result = self.run_ensure(
                webroot,
                "#!/bin/sh\n"
                "[ \"$1\" = \"-W\" ] && [ \"$2\" = '-f=${Version}' ] && [ \"$3\" = \"novnc\" ] || exit 64\n"
                "printf '1.5.0-2\\n'\n",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads((webroot / "package.json").read_text(encoding="utf-8")),
                {"version": "1.5.0-2"},
            )

    def test_existing_metadata_is_preserved_without_querying_dpkg(self):
        with tempfile.TemporaryDirectory() as directory:
            webroot = Path(directory) / "novnc"
            webroot.mkdir()
            (webroot / "vnc.html").write_text("<!doctype html>", encoding="utf-8")
            metadata = webroot / "package.json"
            metadata.write_text('{"version":"existing"}\n', encoding="utf-8")

            result = self.run_ensure(webroot, "#!/bin/sh\nexit 99\n")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(metadata.read_text(encoding="utf-8"), '{"version":"existing"}\n')

    def test_version_lookup_failure_does_not_fabricate_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            webroot = Path(directory) / "novnc"
            webroot.mkdir()
            (webroot / "vnc.html").write_text("<!doctype html>", encoding="utf-8")

            result = self.run_ensure(webroot, "#!/bin/sh\nexit 99\n")

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((webroot / "package.json").exists())

    def test_repeated_runs_keep_the_first_generated_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            webroot = Path(directory) / "novnc"
            webroot.mkdir()
            (webroot / "vnc.html").write_text("<!doctype html>", encoding="utf-8")

            first = self.run_ensure(webroot, "#!/bin/sh\nprintf '1:1.3.0-2\\n'\n")
            second = self.run_ensure(webroot, "#!/bin/sh\nexit 99\n")

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(
                json.loads((webroot / "package.json").read_text(encoding="utf-8")),
                {"version": "1:1.3.0-2"},
            )

    def test_non_novnc_directory_is_left_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            webroot = Path(directory) / "not-novnc"
            webroot.mkdir()

            result = self.run_ensure(webroot, "#!/bin/sh\nexit 99\n")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((webroot / "package.json").exists())


if __name__ == "__main__":
    unittest.main()
