"""Portable compose contract for account-profile login and local builds."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "DouYinSparkFlow"


def service(name):
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    block = text.split(f"  {name}:", 1)[1]
    return re.split(r"\n  [a-z][a-z-]*:\n", block, maxsplit=1)[0]


class LoginProfileDeploymentTests(unittest.TestCase):
    def test_login_mounts_the_same_profile_root_as_senders(self):
        mount = "./state/browser-profiles:/opt/douyin-sparkflow/state/browser-profiles"
        for role in ["web", "scheduler", "task", "login-desktop"]:
            with self.subTest(role=role):
                self.assertIn(mount, service(role))
        self.assertIn(
            "LOGIN_ACCOUNT_PROFILE_ROOT: /opt/douyin-sparkflow/state/browser-profiles",
            service("login-desktop"),
        )

    def test_login_uses_existing_physical_account_guard_directory(self):
        login = service("login-desktop")
        self.assertIn(
            "./DouYinSparkFlow/logs/browser-account-locks:/app/logs/browser-account-locks",
            login,
        )
        self.assertIn("LOGIN_ACCOUNT_LOCK_ROOT: /app/logs/browser-account-locks", login)
        for role in ["web", "scheduler", "task"]:
            self.assertIn("./DouYinSparkFlow/logs:/app/logs", service(role))
        # The narrow lock mount must not expose unrelated account data/logs.
        self.assertNotRegex(login, r"(?m)^\s+- \./DouYinSparkFlow:/app\s*$")
        self.assertNotRegex(login, r"(?m)^\s+- \./DouYinSparkFlow/logs:/app/logs\s*$")

    def test_login_uses_the_explicit_public_local_build_contract(self):
        login = service("login-desktop")
        web = service("web")
        expected_build = "build:\n      context: ./DouYinSparkFlow\n      dockerfile: Dockerfile.server"
        self.assertIn("image: douyin-sparkflow:local", login)
        self.assertIn("image: douyin-sparkflow:local", web)
        self.assertIn(expected_build, login)
        self.assertIn(expected_build, web)
        self.assertNotIn("login-reliability-20261106", (ROOT / "docker-compose.yml").read_text())

    def test_local_build_contains_login_source_and_its_runtime_dependencies(self):
        dockerfile = (SOURCE / "Dockerfile.server").read_text(encoding="utf-8")
        self.assertIn("COPY requirements.txt .", dockerfile)
        self.assertIn("pip install --no-cache-dir -r requirements.txt", dockerfile)
        self.assertIn("COPY . .", dockerfile)
        for package in ("fluxbox", "novnc", "websockify", "x11vnc"):
            with self.subTest(package=package):
                self.assertIn(package, dockerfile)
        self.assertTrue((SOURCE / "login_desktop_server.py").is_file())
        self.assertTrue((SOURCE / "scripts" / "start_login_desktop.sh").is_file())

    def test_account_continuity_is_explicitly_enabled_for_the_web_service(self):
        self.assertIn('SPARKFLOW_LOGIN_PROFILE_CONTINUITY: "1"', service("web"))

    def test_default_browser_engine_is_explicitly_disabled(self):
        for role in ["web", "scheduler", "task", "login-desktop"]:
            with self.subTest(role=role):
                self.assertIn('SPARKFLOW_FINGERPRINT_BROWSER_ENABLED: "0"', service(role))

    def test_host_status_mount_is_relative_and_readonly(self):
        web = service("web")
        self.assertIn("SPARKFLOW_HOST_STATUS_FILE: /run/service-status/status.json", web)
        self.assertIn("./state/service-status:/run/service-status:ro", web)
        self.assertNotIn("/opt/douyin-sparkflow/state/service-status", web)

    def test_sender_mode_and_source_mounts_are_preserved(self):
        for role in ["web", "scheduler", "task"]:
            text = service(role)
            self.assertIn("./DouYinSparkFlow:/app", text)
            self.assertNotIn("SPARKFLOW_BROWSER_HEADFUL", text)
        self.assertIn("./state/login-profile:/data/login-profile", service("login-desktop"))


if __name__ == "__main__":
    unittest.main()
