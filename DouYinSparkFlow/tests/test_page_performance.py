import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests import test_shared_security as fixtures
from webui import app as app_module, ops


class PagePerformanceTests(unittest.TestCase):
    def test_public_assets_are_compressed_cached_and_revalidated(self):
        client = TestClient(app_module.app)
        for path in ("app.css", "app.js", "lucide.min.js"):
            with self.subTest(path=path):
                response = client.get("/static/" + path, headers={"accept-encoding": "gzip"})
                self.assertEqual(200, response.status_code)
                self.assertEqual("gzip", response.headers["content-encoding"])
                self.assertIn("public", response.headers["cache-control"])
                self.assertIn("Accept-Encoding", response.headers["vary"])
                unchanged = client.get("/static/" + path, headers={"if-none-match": response.headers["etag"]})
                self.assertEqual(304, unchanged.status_code)
        private = client.get("/login")
        self.assertEqual("no-store", private.headers["cache-control"])
        self.assertNotIn("content-encoding", private.headers)

    def test_normal_user_snapshot_never_collects_host_logs_or_containers(self):
        with patch.object(ops, "get_send_console_snapshot", return_value={}), patch.object(ops, "get_schedule_snapshot", return_value={}), patch.object(ops, "current_daily_schedule", return_value=""), patch.object(ops, "task_run_lock_status", return_value={}), patch.object(ops, "_cached") as host:
            snapshot = ops.get_ops_snapshot(account_refs=["only-own"])
            host.assert_not_called()
            self.assertEqual([], snapshot["log_tail"])
            self.assertEqual([], snapshot["containers"])

    def test_own_schedule_is_used_by_overview_instead_of_global_cron(self):
        fixture = fixtures.SharedConsoleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        from core.personal_streak import save_preferences
        save_preferences({"username": "alice", "role": "user"}, enabled=True, at_time="09:15", timezone_name="Asia/Shanghai", messages=["hello"])
        with patch.object(app_module, "get_overview_snapshot", return_value={"schedule": {"label": "global"}}):
            response = fixture.login().get("/api/ops/overview")
        self.assertEqual(200, response.status_code)
        self.assertEqual("每天 09:15 · Asia/Shanghai", response.json()["schedule"]["label"])
