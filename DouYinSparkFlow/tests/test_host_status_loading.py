from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tests import test_shared_security as fixtures
from utils import config
from webui import app as app_module, ops


class HostStatusLoadingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SharedConsoleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.admin = self.fixture.login("owner", "Owner-secret-2026!")
        self.user = self.fixture.login()
        cache = patch.object(ops, "_host_status_cache", None)
        cache.start()
        self.addCleanup(cache.stop)
        cron = patch.object(ops, "_display_crontab", None)
        cron.start()
        self.addCleanup(cron.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = patch.object(ops, "HOST_CRONTAB_PATH", Path(directory.name) / "missing-spool")
        path.start()
        self.addCleanup(path.stop)
        config.save_config({**config.get_config(force_reload=True), "dailySendWindow": {"enabled": False}})

    def test_business_html_and_overview_never_execute_host_probes(self):
        with patch.object(ops, "get_container_status", side_effect=AssertionError("Docker must be deferred")), patch.object(ops, "_check_image_present", side_effect=AssertionError("image probe must be deferred")), patch.object(ops, "recent_trigger_lines", side_effect=AssertionError("logs must be deferred")), patch.object(ops, "read_crontab", side_effect=AssertionError("native crontab must be deferred")):
            for client in (self.admin, self.user):
                for path in ("/", "/accounts", "/settings", "/login-workspace", "/api/ops/overview"):
                    with self.subTest(path=path, admin=client is self.admin):
                        response = client.get(path)
                        self.assertEqual(200, response.status_code, response.text[:200])
        page = self.admin.get("/")
        self.assertIn("状态待查询", page.text)

    def test_slow_docker_does_not_delay_html_and_duplicate_probes_are_refused(self):
        started, release = threading.Event(), threading.Event()
        def blocked():
            started.set()
            release.wait(3)
            return [{"Names": "douyin-web", "State": "running", "Status": "Up", "Labels": "internal-secret"}, {"Names": "other-service", "State": "running", "Status": "Up"}]
        with patch.object(ops, "get_container_status", side_effect=blocked) as probe, ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.admin.get, "/api/ops/host-status")
            try:
                self.assertTrue(started.wait(2))
                before = time.monotonic()
                response = self.admin.get("/")
                self.assertEqual(200, response.status_code)
                self.assertLess(time.monotonic() - before, 0.5)
                busy = self.admin.get("/api/ops/host-status")
                self.assertEqual(429, busy.status_code)
            finally:
                release.set()
            loaded = pending.result(timeout=5)
            self.assertEqual(200, loaded.status_code)
            self.assertEqual([{"Names": "douyin-web", "State": "running", "Status": "Up"}], loaded.json()["containers"])
            self.assertNotIn("internal-secret", loaded.text)
            self.assertNotIn("other-service", loaded.text)
            cached = self.admin.get("/api/ops/host-status")
            self.assertEqual(200, cached.status_code)
            self.assertEqual(1, probe.call_count)

    def test_normal_users_and_signed_out_clients_cannot_read_host_state(self):
        with patch.object(ops, "get_container_status") as probe:
            self.assertEqual(403, self.user.get("/api/ops/host-status").status_code)
            self.assertEqual(401, self.fixture.client.get("/api/ops/host-status").status_code)
            probe.assert_not_called()

    def test_revocation_while_host_probe_runs_drops_the_result(self):
        current = {"username": "owner", "role": "admin", "session_id": "first"}
        with patch.object(app_module, "current_principal", side_effect=[current, None]), patch.object(ops, "get_container_status", return_value=[{"Names": "douyin-web", "State": "running", "Status": "Up"}]):
            response = self.admin.get("/api/ops/host-status")
        self.assertEqual(401, response.status_code)
        self.assertNotIn("douyin-web", response.text)

    def test_display_schedule_reads_spool_without_calling_native_commands(self):
        ops.HOST_CRONTAB_PATH.write_text("20 18 * * * bash /app/scripts/run_scheduled_task.sh\n")
        with patch.object(ops, "read_crontab", side_effect=AssertionError("must not execute crontab")):
            self.assertEqual("18:20", ops.current_daily_schedule(allow_host_commands=False))

    def test_display_schedule_uses_last_explicit_read_without_changing_legacy_default(self):
        result = type("Completed", (), {"returncode": 0, "stdout": "15 19 * * * bash /app/scripts/run_scheduled_task.sh\n"})()
        with patch.object(ops.subprocess, "run", return_value=result) as command:
            self.assertEqual("19:15", ops.current_daily_schedule())
            self.assertEqual("19:15", ops.current_daily_schedule(allow_host_commands=False))
            self.assertEqual(1, command.call_count)
