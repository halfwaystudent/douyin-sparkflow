import asyncio
import errno
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from core import tasks
from webui import app as app_module
from webui import login_lock
from webui import ops


class WebUiSafetyTests(unittest.TestCase):
    def setUp(self):
        try:
            login_lock.LOCK_PATH.unlink()
        except FileNotFoundError:
            pass

    def tearDown(self):
        try:
            login_lock.LOCK_PATH.unlink()
        except FileNotFoundError:
            pass

    def test_windows_invalid_pid_probe_is_treated_as_dead(self):
        error = OSError(errno.EINVAL, "invalid pid")
        error.winerror = 87
        with patch.object(ops.os, "kill", side_effect=error):
            self.assertFalse(ops._pid_is_alive(999999))
        with patch.object(tasks.os, "kill", side_effect=error):
            self.assertFalse(tasks._pid_is_alive(999999))

    def test_missing_optional_runtime_tools_do_not_log_warnings(self):
        with (
            patch.object(ops.subprocess, "run", side_effect=FileNotFoundError("missing")),
            patch.object(ops.logger, "warning") as warning,
            patch.object(ops.logger, "debug") as debug,
        ):
            result = ops.run_command(["docker", "ps"])
            self.assertEqual(1, result.returncode)
            ops.read_crontab()

        warning.assert_not_called()
        self.assertGreaterEqual(debug.call_count, 2)

    def test_stale_lock_inspection_does_not_delete_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lock_path = root / "logs" / "task.run.lock"
            lock_path.parent.mkdir(parents=True)
            lock_path.write_text("99999999\n", encoding="utf-8")
            old = time.time() - 10800
            os.utime(lock_path, (old, old))

            with patch.object(ops, "repo_root", return_value=root):
                status = ops.task_run_lock_status()

            self.assertTrue(lock_path.exists())
            self.assertTrue(status["stale"])
            self.assertFalse(status["running"])
            self.assertEqual("owner_pid_missing", status["staleReason"])

    def test_overview_snapshot_excludes_sensitive_payloads(self):
        send_console = {
            "now": "2026-07-10T22:00:00+08:00",
            "summary": {
                "enabled_accounts": 1,
                "total_targets": 2,
                "today_confirmed_targets": 1,
                "today_unconfirmed_targets": 1,
                "today_failed_targets": 0,
                "today_account_blocked_targets": 0,
                "today_attention_targets": 1,
                "today_pending_targets": 0,
                "today_unprocessed_targets": 0,
                "today_remaining_targets": 1,
                "today_warning_count": 0,
                "last_confirmed_at": "2026-07-10T21:00:00+08:00",
                "all_confirmed": False,
            },
            "accounts": [
                {
                    "unique_id": "account-1",
                    "username": "Account",
                    "state": "attention",
                    "total_targets": 2,
                    "confirmed_targets": [{"message": "secret message"}],
                    "attention_count": 1,
                    "pending_count": 0,
                    "last_confirmed_at": "2026-07-10T21:00:00+08:00",
                }
            ],
        }

        with (
            patch.object(ops, "get_send_console_snapshot", return_value=send_console),
            patch.object(
                ops,
                "get_schedule_snapshot",
                return_value={"label": "10:00-18:00/20m", "nextTriggerAt": ""},
            ),
            patch.object(
                ops,
                "task_run_lock_status",
                return_value={"running": False, "stale": False, "ageSeconds": 0},
            ),
        ):
            payload = ops.get_overview_snapshot()

        serialized = repr(payload)
        self.assertNotIn("secret message", serialized)
        self.assertNotIn("cookies", serialized)
        self.assertNotIn("serverReceipt", serialized)
        self.assertNotIn("reason", serialized)
        self.assertEqual(1, payload["summary"]["attention"])

    def test_primary_pages_and_local_icons_render(self):
        client = TestClient(app_module.app, raise_server_exceptions=False)
        self.assertEqual(200, client.get("/login").status_code)
        self.assertEqual(200, client.get("/static/lucide.min.js").status_code)

        with patch.object(app_module, "current_user", return_value="admin"):
            for path in ("/", "/ops/send-console", "/ops/logs"):
                response = client.get(path)
                self.assertEqual(200, response.status_code, path)
                self.assertEqual("no-store", response.headers["cache-control"])

    def test_login_desktop_timeout_is_wrapped_as_runtime_error(self):
        with patch.object(app_module.urllib.request, "urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaisesRegex(RuntimeError, "login-desktop unavailable: timed out"):
                app_module.call_login_desktop("/open-login", method="POST", payload={})

    def test_login_desktop_open_uses_extended_startup_timeout(self):
        client = TestClient(app_module.app)
        with (
            patch.object(app_module, "current_user", return_value="admin"),
            patch.object(app_module, "validate_csrf", return_value=True),
            patch.object(app_module, "call_login_desktop", return_value={}) as call_login,
        ):
            response = client.post("/login-desktop/open", data={"csrf_token": "test"})

        self.assertEqual(200, response.status_code)
        call_login.assert_called_once_with("/open-login", method="POST", payload={}, timeout=90)

    def test_websocket_relay_cleans_up_pending_tasks(self):
        cleaned_up = []

        async def completes():
            return None

        async def waits_forever():
            try:
                await asyncio.Event().wait()
            finally:
                cleaned_up.append(True)

        asyncio.run(app_module._run_websocket_relays(completes(), waits_forever()))
        self.assertEqual([True], cleaned_up)

    def test_login_desktop_urls_honor_container_environment(self):
        with (
            patch.dict(
                os.environ,
                {
                    "SPARKFLOW_LOGIN_DESKTOP_API_URL": "http://login-desktop:18090",
                    "SPARKFLOW_LOGIN_DESKTOP_PUBLIC_URL": "http://127.0.0.1:8788/vnc.html",
                },
            ),
            patch.object(app_module, "get_app_settings", return_value={}),
        ):
            self.assertEqual("http://login-desktop:18090", app_module.login_desktop_api_url())
            request = type("Request", (), {"url": type("Url", (), {"hostname": "example", "scheme": "http"})()})()
            self.assertEqual(
                "http://127.0.0.1:8788/vnc.html",
                app_module.login_desktop_public_url(request),
            )

    def test_login_desktop_defaults_to_authenticated_same_origin_proxy(self):
        request = type("Request", (), {"url": type("Url", (), {"hostname": "example", "scheme": "https"})()})()
        with (
            patch.dict(
                os.environ,
                {
                    "SPARKFLOW_LOGIN_DESKTOP_PUBLIC_URL": "",
                    "SPARKFLOW_LOGIN_DESKTOP_MODE": "novnc",
                },
                clear=False,
            ),
            patch.object(app_module, "get_app_settings", return_value={}),
        ):
            url = app_module.login_desktop_public_url(request)

        self.assertTrue(url.startswith("/login-desktop/proxy/vnc.html?"))
        self.assertIn("path=login-desktop/proxy/websockify", url)

    def test_login_desktop_defaults_to_native_mode_on_windows(self):
        request = type("Request", (), {"url": type("Url", (), {"hostname": "example", "scheme": "http"})()})()
        with (
            patch.dict(os.environ, {"SPARKFLOW_LOGIN_DESKTOP_PUBLIC_URL": "", "SPARKFLOW_LOGIN_DESKTOP_MODE": "native"}, clear=False),
            patch.object(app_module, "get_app_settings", return_value={}),
        ):
            url = app_module.login_desktop_public_url(request)
        if os.name == "nt":
            self.assertEqual("", url)
        else:
            self.assertTrue(url.startswith("/login-desktop/proxy/vnc.html?"))

    def test_login_desktop_http_proxy_requires_auth_and_forwards_assets(self):
        client = TestClient(app_module.app)
        unauthenticated = client.get(
            "/login-desktop/proxy/vnc.html",
            follow_redirects=False,
        )
        self.assertEqual(303, unauthenticated.status_code)
        self.assertEqual("/login", unauthenticated.headers["location"])

        with (
            patch.object(app_module, "current_user", return_value="admin"),
            patch.object(app_module, "get_login_lock", return_value={"username": "admin", "session_id": ""}),
            patch.object(app_module, "owns_login_lock", return_value=True),
            patch.object(
                app_module,
                "fetch_login_desktop_asset",
                return_value=(200, {"Content-Type": "text/html"}, b"<html>noVNC</html>"),
            ) as fetch_asset,
        ):
            response = client.get("/login-desktop/proxy/vnc.html?autoconnect=1")

        self.assertEqual(200, response.status_code)
        self.assertEqual("text/html", response.headers["content-type"])
        self.assertIn("noVNC", response.text)
        fetch_asset.assert_called_once_with("vnc.html", "autoconnect=1")

    def test_login_qr_proxy_requires_auth_and_returns_png(self):
        client = TestClient(app_module.app)
        unauthenticated = client.get("/login-desktop/qr", follow_redirects=False)
        self.assertEqual(303, unauthenticated.status_code)

        upstream = Mock()
        upstream.read.return_value = b"fake-png"
        with (
            patch.object(app_module, "current_user", return_value="admin"),
            patch.object(app_module, "get_login_lock", return_value={"username": "admin", "session_id": ""}),
            patch.object(app_module, "owns_login_lock", return_value=True),
            patch.object(app_module.urllib.request, "urlopen", return_value=upstream),
        ):
            response = client.get("/login-desktop/qr")

        self.assertEqual(200, response.status_code)
        self.assertEqual("image/png", response.headers["content-type"])
        self.assertEqual("no-store, max-age=0", response.headers["cache-control"])
        self.assertEqual(b"fake-png", response.content)

    def test_dashboard_contains_mobile_qr_controls(self):
        dashboard = (Path(app_module.TEMPLATES_DIR) / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("data-login-qr", dashboard)
        self.assertIn("data-refresh-login-qr", dashboard)
        self.assertIn("/login-desktop/qr", dashboard)

    def test_login_workspace_loads_inside_dashboard_frame(self):
        script = (Path(app_module.STATIC_DIR) / "app.js").read_text(encoding="utf-8")
        block_start = script.index('document.querySelectorAll(".login-desktop-open")')
        block_end = script.index('document.querySelectorAll(".login-desktop-save")', block_start)
        block = script[block_start:block_end]
        self.assertLess(block.index('postForm("/login-desktop/open"'), block.index("loadFrame(true)"))
        self.assertNotIn('window.open("about:blank"', block)
        self.assertNotIn("popup.location", block)
        self.assertIn("new URL(configuredPublicUrl, window.location.href).href", script)
        self.assertIn("refreshLoginQr(500)", block)
        self.assertIn('data.state === "queued"', block)
        self.assertIn("renderWorkspace(data.workspace)", block)
        self.assertIn("retries - 1", script)
        self.assertIn('/login-desktop/qr/refresh', script)

    def test_schedule_sync_writes_configured_window_to_shared_spool(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cron_path = Path(temp_dir) / "root"
            with (
                patch.object(ops, "HOST_CRONTAB_PATH", cron_path),
                patch.object(ops, "running_in_container", return_value=True),
                patch.object(ops, "read_crontab", return_value=""),
                patch.object(
                    ops,
                    "get_config",
                    return_value={
                        "dailySendWindow": {
                            "enabled": True,
                            "startHour": 10,
                            "endHour": 18,
                            "scheduleIntervalMinutes": 20,
                        }
                    },
                ),
            ):
                result = ops.sync_daily_schedule_from_config()

            self.assertEqual(0, result.returncode)
            text = cron_path.read_text(encoding="utf-8")
            self.assertIn("*/20 10-17 * * *", text)
            self.assertIn("0 18 * * *", text)
            self.assertIn("20 18 * * *", text)
            self.assertIn("run_scheduled_task.sh", text)
            self.assertNotIn("docker exec", text)

    def test_overview_api_requires_authentication_and_disables_cache(self):
        client = TestClient(app_module.app)
        response = client.get("/api/ops/overview")

        self.assertEqual(401, response.status_code)
        self.assertEqual("no-store", response.headers["cache-control"])

        with (
            patch.object(app_module, "current_user", return_value="admin"),
            patch.object(
                app_module,
                "get_overview_snapshot",
                return_value={
                    "now": "2026-07-10T22:00:00+08:00",
                    "schedule": {},
                    "task": {},
                    "summary": {},
                    "accounts": [],
                },
            ),
        ):
            response = client.get("/api/ops/overview")

        self.assertEqual(200, response.status_code)
        self.assertEqual("no-store", response.headers["cache-control"])

    def test_public_settings_and_template_do_not_expose_server_password(self):
        with patch.object(
            app_module,
            "get_app_settings",
            return_value={
                "server_host": "example",
                "server_username": "root",
                "server_password": "secret",
                "session_secret": "secret",
                "admin_password_hash": "hash",
                "compose_root": "/opt/app",
                "ui_port": 8787,
                "login_desktop_api_url": "http://127.0.0.1:18090",
            },
        ):
            public = app_module.public_app_settings()

        self.assertNotIn("server_password", public)
        self.assertNotIn("session_secret", public)
        dashboard = (
            Path(app_module.TEMPLATES_DIR) / "dashboard.html"
        ).read_text(encoding="utf-8")
        self.assertNotIn("server_password", dashboard)
        self.assertNotIn("server_username", dashboard)
        self.assertNotIn("server_host", dashboard)


if __name__ == "__main__":
    unittest.main()
