import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which("bash"), "Bash is required")
class LocalInstallerTests(unittest.TestCase):
    def run_installer(self, failure=""):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "deploy").mkdir()
            (root / "bin").mkdir()
            (root / "proxy").mkdir()
            shutil.copyfile(REPO_ROOT / "deploy/install-local.sh", root / "deploy/install-local.sh")
            (root / ".env.example").write_text("WEB_PORT=8787\n", encoding="utf-8")
            (root / "proxy/config.example.yaml").write_text("mode: direct\n", encoding="utf-8")
            (root / "refresh_proxy.sh").write_text("touch refreshed\n", encoding="utf-8")
            docker = root / "bin/docker"
            docker.write_text(
                '#!/bin/bash\n'
                'printf "%s\\n" "$*" >> "$TEST_COMMAND_LOG"\n'
                'if [ "$*" = "$TEST_FAILURE" ]; then exit 23; fi\n'
                'exit 0\n', encoding="utf-8",
            )
            docker.chmod(0o755)
            for name in ("open", "xdg-open"):
                opener = root / "bin" / name
                opener.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
                opener.chmod(0o755)
            env = dict(os.environ)
            env.update(
                PATH=str(root / "bin") + os.pathsep + env.get("PATH", ""),
                TEST_COMMAND_LOG=str(root / "commands"), TEST_FAILURE=failure,
            )
            result = subprocess.run(
                ["bash", str(root / "deploy/install-local.sh")],
                env=env, capture_output=True, text=True,
            )
            return result, (root / "commands").read_text(), (root / "refreshed").exists()

    def test_preflight_failure_stops_before_proxy_refresh_or_start(self):
        for failure in ("compose config --quiet", "info"):
            with self.subTest(failure=failure):
                result, commands, refreshed = self.run_installer(failure)
                self.assertEqual(result.returncode, 23)
                self.assertFalse(refreshed)
                self.assertNotIn("compose up", commands)
                self.assertNotIn("is running", result.stdout)

    def test_start_failure_does_not_report_success(self):
        result, _, refreshed = self.run_installer("compose up -d --build")
        self.assertEqual(result.returncode, 23)
        self.assertTrue(refreshed)
        self.assertNotIn("is running", result.stdout)

    def test_success_checks_configuration_and_daemon_before_start(self):
        result, commands, refreshed = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(refreshed)
        self.assertEqual(commands.splitlines(), [
            "compose version", "compose config --quiet", "info", "compose up -d --build",
        ])
        self.assertIn("is running", result.stdout)

    def test_login_cpu_template_matches_compose_default(self):
        template = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        cpu = next(line.split("=", 1)[1] for line in template.splitlines()
                   if line.startswith("LOGIN_DESKTOP_CPUS="))
        self.assertIn("${LOGIN_DESKTOP_CPUS:-" + cpu + "}", compose)


if __name__ == "__main__":
    unittest.main()
