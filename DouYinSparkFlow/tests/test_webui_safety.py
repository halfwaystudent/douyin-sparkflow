import asyncio
import errno
import os
import re
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

    def test_windows_bad_format_pid_probe_is_treated_as_dead(self):
        # Windows raises ERROR_BAD_FORMAT (11) instead of ERROR_INVALID_PARAMETER
        # (87) for some out-of-range pids; both mean "no live process".
        error = OSError(errno.EINVAL, "bad format pid")
        error.winerror = 11
        with patch.object(ops.os, "kill", side_effect=error):
            self.assertFalse(ops._pid_is_alive(99999999))
        with patch.object(tasks.os, "kill", side_effect=error):
            self.assertFalse(tasks._pid_is_alive(99999999))

    def test_schedule_alignment_flags_window_lines_without_configured_window(self):
        window_line = (
            "*/20 10-17 * * * env SPARKFLOW_TRIGGER_LABEL='scheduled send' "
            "bash /app/scripts/run_scheduled_task.sh"
        )
        with (
            patch.object(
                ops,
                "get_config",
                return_value={"dailySendWindow": {"enabled": False}},
            ),
            patch.object(ops, "read_crontab", return_value=window_line + "\n"),
            patch.object(ops, "current_daily_schedule", return_value="18:20"),
        ):
            alignment = ops.get_schedule_alignment()

        self.assertFalse(alignment["aligned"])
        self.assertEqual(["window"], alignment["kinds"])
        self.assertIn("窗口式任务行", alignment["detail"])

    def test_schedule_alignment_accepts_a_fixed_single_run(self):
        fixed_line = (
            "20 18 * * * env SPARKFLOW_TRIGGER_LABEL='scheduled send' "
            "bash /app/scripts/run_scheduled_task.sh\n"
            # The fixed run's daily account patrol, 30 minutes earlier.
            "50 17 * * * env SPARKFLOW_TASK_MODE=patrol PYTHONUNBUFFERED=1 "
            "SPARKFLOW_TRIGGER_LABEL='account patrol' bash /app/scripts/run_scheduled_task.sh"
        )
        with (
            patch.object(
                ops,
                "get_config",
                return_value={"dailySendWindow": {"enabled": False}},
            ),
            patch.object(ops, "read_crontab", return_value=fixed_line + "\n"),
            patch.object(ops, "current_daily_schedule", return_value="18:20"),
        ):
            alignment = ops.get_schedule_alignment()

        self.assertTrue(alignment["aligned"])
        self.assertEqual(["fixed"], alignment["kinds"])

    def test_window_interval_is_pinned_to_twenty_minutes(self):
        # The console no longer offers an interval input, so a stale or hand
        # written value must not be able to change the real cadence.
        for typed in ("10:00-18:00/5m", "10:00-18:00/20m", "10:00-18:00/45m"):
            parsed = ops.parse_schedule_string(typed)
            self.assertEqual(20, parsed["scheduleIntervalMinutes"])
            self.assertEqual("10:00-18:00/20m", ops._format_window_schedule(parsed))

    def test_window_state_reports_whether_now_is_inside(self):
        import datetime as datetime_module

        with patch.object(ops, "current_daily_schedule", return_value="10:00-18:00/20m"):
            self.assertTrue(
                ops.schedule_window_state(
                    now=datetime_module.datetime(2026, 9, 18, 11, 0)
                )["inside"]
            )
            self.assertFalse(
                ops.schedule_window_state(
                    now=datetime_module.datetime(2026, 9, 18, 20, 0)
                )["inside"]
            )
            # The end hour is exclusive: 18:00 is already outside the window.
            self.assertFalse(
                ops.schedule_window_state(
                    now=datetime_module.datetime(2026, 9, 18, 18, 0)
                )["inside"]
            )
        with patch.object(ops, "current_daily_schedule", return_value="18:20"):
            self.assertFalse(ops.schedule_window_state()["enabled"])

    def test_preview_daily_schedule_validates_and_estimates(self):
        with (
            patch.object(
                ops,
                "get_config",
                return_value={
                    "sendStrategy": {
                        "messageIntervalSecondsMin": 10,
                        "messageIntervalSecondsMax": 10,
                    }
                },
            ),
            patch.object(
                ops,
                "get_send_console_snapshot",
                return_value={"summary": {"total_targets": 17, "enabled_accounts": 1}},
            ),
        ):
            preview = ops.preview_daily_schedule("10:00-18:00/20m")

        self.assertTrue(preview["ok"])
        self.assertEqual("10:00-18:00/20m", preview["label"])
        self.assertTrue(preview["nextTriggerAt"])
        self.assertEqual(17, preview["targetCount"])
        self.assertGreater(preview["estimatedRunSeconds"], 0)
        self.assertEqual(24, preview["slotsPerDay"])

        bad = ops.preview_daily_schedule("10:00-18:00/0m")
        self.assertFalse(bad["ok"])
        self.assertTrue(bad["error"])

    def test_fixed_time_preview_explains_the_mode(self):
        with (
            patch.object(ops, "get_config", return_value={}),
            patch.object(
                ops,
                "get_send_console_snapshot",
                return_value={"summary": {"total_targets": 0, "enabled_accounts": 0}},
            ),
        ):
            preview = ops.preview_daily_schedule("18:20")

        self.assertTrue(preview["ok"])
        self.assertEqual("18:20", preview["label"])
        self.assertEqual(1, preview["slotsPerDay"])
        self.assertTrue(any("单次固定时间" in text for text in preview["warnings"]))

    def test_log_summary_counts_levels_and_categories(self):
        # read_log_tail returns the tail as one newline-joined string, which is
        # what the summary has to split; stubbing it with a list used to hide the
        # fact that the caller iterated characters instead of lines.
        tail = "\n".join(
            [
                "2026-09-18 10:00:00 - app - ERROR - tasks.py:1 - friend_index_stale blocked the run",
                "2026-09-18 10:00:01 - app - WARNING - tasks.py:2 - protocol_sender_failed",
                "2026-09-18 10:00:02 - app - INFO - tasks.py:3 - nothing notable",
                "plain line without a level",
            ]
        )
        with patch.object(ops, "read_log_tail", return_value=tail):
            summary = ops.summarize_log_tail()

        self.assertEqual(4, summary["lines"])
        self.assertEqual(1, summary["levels"]["ERROR"])
        self.assertEqual(1, summary["levels"]["WARNING"])
        self.assertEqual(1, summary["levels"]["INFO"])
        counts = {item["category"]: item["count"] for item in summary["categories"]}
        self.assertEqual(1, counts["friend_index_stale"])
        self.assertEqual(1, counts["protocol_sender_failed"])

    def test_missing_optional_runtime_tools_do_not_log_warnings(self):
        with (
            patch.object(ops.subprocess, "run", side_effect=FileNotFoundError("missing")),
            patch.object(ops, "running_in_container", return_value=False),
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
                "today_page_echo_targets": 1,
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
                    "page_echo_targets": [{"message": "secret page echo"}],
                    "page_echo_count": 1,
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
            patch.object(
                app_module,
                "current_principal",
                return_value={"username": "admin", "role": "admin", "account_refs": [], "session_id": "session-1"},
            ),
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
            patch.object(
                app_module,
                "current_principal",
                return_value={"username": "admin", "role": "admin", "account_refs": [], "session_id": "session-1"},
            ),
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
        self.assertTrue(response.headers["content-type"].startswith("text/html"))
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
            patch.object(
                app_module,
                "current_principal",
                return_value={"username": "admin", "role": "admin", "account_refs": [], "session_id": "session-1"},
            ),
            patch.object(app_module, "get_login_lock", return_value={"username": "admin", "session_id": ""}),
            patch.object(app_module, "owns_login_lock", return_value=True),
            patch.object(app_module.urllib.request, "urlopen", return_value=upstream),
        ):
            response = client.get("/login-desktop/qr")

        self.assertEqual(200, response.status_code)
        self.assertEqual("image/png", response.headers["content-type"])
        self.assertEqual("no-store, max-age=0", response.headers["cache-control"])
        self.assertEqual(b"fake-png", response.content)

    def test_login_qr_upstream_failures_are_classified(self):
        # The frontend can only show "Douyin unreachable" vs "service down" if
        # the 502 carries a machine-readable reason instead of plain text.
        client = TestClient(app_module.app)
        http_error = app_module.urllib.error.HTTPError(
            "http://login-desktop/qr", 500, "boom", {}, None
        )
        cases = (
            (http_error, 502, "douyin_unreachable"),
            (app_module.urllib.error.URLError("refused"), 502, "service_unavailable"),
            # A slow page holding the lock is "busy" and must stay retryable.
            (TimeoutError("timed out"), 503, "page_busy"),
            (app_module.urllib.error.URLError(TimeoutError("timed out")), 503, "page_busy"),
        )
        for error, status, category in cases:
            with self.subTest(category=category), (
                patch.object(app_module, "current_user", return_value="admin")
            ), patch.object(
                app_module,
                "current_principal",
                return_value={"username": "admin", "role": "admin", "account_refs": [], "session_id": "session-1"},
            ), patch.object(
                app_module, "get_login_lock", return_value={"username": "admin", "session_id": ""}
            ), patch.object(app_module, "owns_login_lock", return_value=True), patch.object(
                app_module.urllib.request, "urlopen", side_effect=error
            ):
                response = client.get("/login-desktop/qr")
            self.assertEqual(status, response.status_code)
            payload = response.json()
            self.assertEqual(category, payload["category"])
            self.assertTrue(payload["message"])

    def test_login_workspace_contains_mobile_qr_controls(self):
        page = (Path(app_module.TEMPLATES_DIR) / "login_workspace.html").read_text(encoding="utf-8")
        self.assertIn("data-login-qr", page)
        self.assertIn("data-refresh-login-qr", page)
        self.assertIn("/login-desktop/qr", page)

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

    def test_saving_a_login_shows_progress_and_blocks_a_second_click(self):
        # A second click while the first save was still running raced its
        # release and failed with a 400; the save must look busy until it ends.
        script = (Path(app_module.STATIC_DIR) / "app.js").read_text(encoding="utf-8")
        start = script.index('document.querySelectorAll(".login-desktop-save").forEach((button) =>')
        block = script[start:script.index('document.querySelectorAll(".login-desktop-close")', start)]
        self.assertIn("if (button.disabled) return;", block)
        self.assertLess(block.index("setSaving(true);"), block.index("try {"))
        catch = block[block.rindex("} catch (error) {"):]
        self.assertIn("setSaving(false);", catch)
        # Every early return before the result must end the busy state too.
        duplicates = block[block.index("if (candidates.length > 1) {"):block.index("const update = window.confirm(")]
        self.assertGreaterEqual(duplicates.count("setSaving(false);"), 2)
        saving = script[script.index("const setSaving = "):start]
        self.assertIn("item.disabled = saving || keepDisabled;", saving)
        self.assertIn("已等待", saving)
        # The lease countdown and the status poll must not overwrite the save
        # progress or its error: they flickered with it every second.
        self.assertIn("statusHeldUntil = saving ? Infinity : Date.now() + 8000;", saving)
        countdown = script[script.index("countdownTimer = window.setInterval"):]
        countdown = countdown[: countdown.index("}, 1000);")]
        self.assertLess(countdown.index("if (statusHeld()) return;"), countdown.index("setStatus("))
        poll = script[script.index("const pollStatus = async"):script.index("const heartbeat = async")]
        self.assertLess(poll.index("if (statusHeld())"), poll.index("当前浏览器已登录"))
        # After a successful save the page reloads; the buttons stay disabled.
        self.assertIn("setSaving(false, { keepDisabled: true });", block)

    def test_save_and_close_do_not_wait_for_the_next_workspace(self):
        source = Path(app_module.__file__).read_text(encoding="utf-8")
        save = source[source.index('@app.post("/login-desktop/save")'):]
        self.assertIn('await asyncio.to_thread(call_login_desktop, "/export"', save)
        self.assertIn("_reset_and_promote_later()", save)
        self.assertNotIn("await _reset_and_promote()", save)
        close = source[source.index('@app.post("/login-desktop/close")'):source.index('@app.post("/login-desktop/release")')]
        self.assertIn("_reset_and_promote_later()", close)
        later = source[source.index("def _reset_and_promote_later"):]
        later = later[: later.index("release_tasks.discard") + len("release_tasks.discard")]
        # Keep a reference, or the event loop may drop the running task.
        self.assertIn("release_tasks.add(task)", later)

    def test_add_account_shows_progress_from_the_click(self):
        # Opening a cold login browser takes tens of seconds; the click must
        # answer at once and the button must not invite a second open.
        script = (Path(app_module.STATIC_DIR) / "app.js").read_text(encoding="utf-8")
        # The click handler, not startOpening(), which also walks these buttons.
        block_start = script.index('button.addEventListener("click", async () => {\n      if (section) section.open = true;')
        block = script[block_start:script.index('document.querySelectorAll("[data-focus-native-browser]")', block_start)]
        self.assertLess(block.index("startOpening()"), block.index('postForm("/login-desktop/open"'))
        self.assertGreaterEqual(block.count("stopOpening()"), 2)  # queued and failure
        opening = script[script.index("const renderOpening = "):script.index("function stopOpening()")]
        self.assertIn("button.disabled = true", opening)
        self.assertIn("已等待", opening)
        self.assertIn("frameWrap.dataset.progress", script[script.index("const renderOpening = "):])
        buttons = script[script.index("const setQrButtons = "):script.index("const qrPlaceholder = ")]
        self.assertIn("if (!busy) stopOpening();", buttons)
        css = (Path(app_module.STATIC_DIR) / "app.css").read_text(encoding="utf-8")
        self.assertIn("content: attr(data-progress)", css)

    def test_a_fresh_grant_loads_the_remote_browser_once(self):
        # renderWorkspace already loads the frame for a new ticket; loading it
        # again in the click handler reconnected noVNC mid-handshake.
        script = (Path(app_module.STATIC_DIR) / "app.js").read_text(encoding="utf-8")
        # The click handler, not startOpening(), which also walks these buttons.
        block_start = script.index('button.addEventListener("click", async () => {\n      if (section) section.open = true;')
        block = script[block_start:script.index('document.querySelectorAll("[data-focus-native-browser]")', block_start)]
        guard = block.index("if (lastPromotedTicket === promotedBefore) {")
        self.assertLess(guard, block.index("loadFrame(true)"))
        self.assertLess(block.index("const promotedBefore = lastPromotedTicket;"), block.index("renderWorkspace(data.workspace)"))

    def test_qr_proxy_waits_upstream_and_open_does_not_block(self):
        source = Path(app_module.__file__).read_text(encoding="utf-8")
        self.assertIn('/qr?wait={LOGIN_QR_WAIT_SECONDS}', source)
        # Upstream may add ~3s to the wait; it must stay inside the 20s proxy timeout.
        self.assertLessEqual(app_module.LOGIN_QR_WAIT_SECONDS + 3, 20)
        self.assertIn('await asyncio.to_thread(call_login_desktop, "/open-login"', source)
        self.assertNotIn('            call_login_desktop("/open-login"', source)

    def test_runtime_pill_follows_the_workspace_state(self):
        # A held workspace is warning-toned close to its lease renewal; the
        # label must still say it is in use, not that the operator is queued.
        script = (Path(app_module.STATIC_DIR) / "app.js").read_text(encoding="utf-8")
        start = script.index("const setStatus = ")
        block = script[start:script.index("const postForm = ", start)]
        self.assertNotIn('tone === "warning" ? "排队中"', block)
        self.assertIn('state === "active"', block)
        self.assertIn('state === "queued"', block)

    def test_qr_upstream_failure_leaves_the_refresh_button_usable(self):
        # A 502 ends the QR poll; the button the grant set to "读取中…" must be
        # re-enabled, or the copy's "点击刷新二维码" cannot be acted on.
        script = (Path(app_module.STATIC_DIR) / "app.js").read_text(encoding="utf-8")
        start = script.index("if (response.status === 502) {")
        end = script.index("if (response.status === 401) {", start)
        self.assertIn('setQrButtons("已停止，点此重试", { stopped: true });', script[start:end])

    def test_login_workspace_recovers_when_the_lease_is_gone(self):
        """A 423 means "this session does not hold the lease", not "the page is slow".

        The old frontend retried 423 as if the login page were still loading, so
        the QR code spun forever and the noVNC frame never loaded, while the copy
        blamed the healthy login-desktop container.
        """
        script = (Path(app_module.STATIC_DIR) / "app.js").read_text(encoding="utf-8")
        page = (Path(app_module.TEMPLATES_DIR) / "login_workspace.html").read_text(encoding="utf-8")

        self.assertIn("data-recover-login-workspace", page)
        self.assertIn("recoverLease", script)
        # The lease-loss branch must clear the stale local workspace view. The
        # whole reset is required, not just the literal that also exists in the
        # initial declaration.
        self.assertIn('workspace = { state: "closed", active: false, position: 0, ticket: "" };', script)
        recover_start = script.index("const recoverLease")
        reset_block = script[recover_start:script.index('querySelectorAll("[data-recover-login-workspace]")', recover_start)]
        for reset in ("qrPollStartedAt = 0", 'lastPromotedTicket = ""', "closeFrame()", "setRecoverVisible(true)"):
            self.assertIn(reset, reset_block)
        # Inside the QR poll, 423 must be classified and recovered BEFORE the
        # "page is busy" fallback, otherwise the retry loop swallows it again.
        start = script.index("const refreshLoginQr = async")
        end = script.index("const pollStatus = async")
        qr_poll = script[start:end]
        self.assertIn("response.status === 423", qr_poll)
        self.assertIn("recoverLease()", qr_poll)
        self.assertLess(qr_poll.index("response.status === 423"), qr_poll.index("recoverLease()"))
        self.assertLess(qr_poll.index("recoverLease()"), qr_poll.index('retryLater("登录页正在加载")'))
        # The other lease-gated calls must recover instead of printing a bare error.
        for handler in (".login-desktop-save", "data-focus-native-browser"):
            self.assertIn(handler, script)
        self.assertGreaterEqual(script.count("payload.leaseLost"), 4)
        self.assertIn("if (error.payload && error.payload.leaseLost)", script)
        # A never-ready QR code must stop polling and hand control back to the
        # operator; it must not keep POSTing /qr/refresh, because every refresh
        # reloads the shared Douyin login page under the operator's cursor.
        # The wait is bounded by time since the workspace opened (each GET /qr
        # already waits upstream), and a not-ready answer is re-asked after ~1s.
        self.assertIn("const QR_WAIT_LIMIT_MS = 60000;", script)
        self.assertIn("const QR_RETRY_MS = 1000;", script)
        self.assertIn("if (Date.now() - qrPollStartedAt >= QR_WAIT_LIMIT_MS) {", script)
        self.assertIn('retryLater(data.message || "浏览器正在生成二维码", QR_RETRY_MS);', script)
        self.assertNotIn("qrNotReadyAttempts", script)
        self.assertIn("const refreshLoginQr = async (delay = 0, retries = 40) => {", script)
        self.assertIn('setQrButtons("已停止，点此重试", { stopped: true })', script)
        self.assertNotIn("qrAutoRefreshes", script)
        auto_calls = [line for line in script.splitlines() if "requestQrRefresh(" in line and "await requestQrRefresh()" not in line]
        self.assertEqual([], auto_calls, f"automatic refresh must not be wired back in: {auto_calls}")
        # Recovery must not be able to start a second loop, and the number of
        # automatic re-acquisitions must be capped by a budget that only a user
        # action renews -- a grant must NOT renew it, or a flapping lease turns
        # the ladder into an endless cycle of POST /login-desktop/open.
        self.assertIn("if (recoveringLease) return;", script)
        self.assertIn("const RECOVER_BUDGET = 2;", script)
        # Pin the exact comparison: a trailing `|| true` would neuter the cap
        # while still containing the substring.
        self.assertIn("if (recoverAttempts <= RECOVER_BUDGET) {", script)
        self.assertNotIn("RECOVER_BUDGET ||", script)
        # The ladder must only be as long as the budget can reach.
        self.assertIn("[5000, 15000]", script)
        self.assertNotIn("30000, 60000", script)
        # Exactly one place may renew the budget, and it must not be the
        # automatic path. Line-start matching is not enough: an inlined
        # `if (active) recoverAttempts = 0;` would slip through, so match the
        # assignment anywhere and require the single hit to sit inside
        # resetRecoveryBudget.
        reset_sites = [
            match.group(0)
            for match in re.finditer(r"^[^\n]*recoverAttempts\s*=\s*0", script, flags=re.MULTILINE)
            if not re.search(r"\b(let|const|var)\b[^\n]*recoverAttempts\s*=\s*0", match.group(0))
        ]
        self.assertEqual(1, len(reset_sites), f"only resetRecoveryBudget may renew the budget: {reset_sites}")
        budget_fn = script[script.index("const resetRecoveryBudget"):script.index("const recoverLease")]
        self.assertRegex(budget_fn, r"recoverAttempts\s*=\s*0")
        self.assertIn("window.clearTimeout(recoveryTimer)", budget_fn)
        # The automatic path must never renew the budget or cancel the ladder.
        recover_start = script.index("const recoverLease")
        recover_fn = script[recover_start:script.index("document.querySelectorAll", recover_start)]
        self.assertNotIn("resetRecoveryBudget", recover_fn)
        # Pin the value: `recoverAttempts += 0;` would make the cap permanently
        # true and revive the unbounded loop while still matching `+=`.
        self.assertIn("recoverAttempts += 1;", recover_fn)
        self.assertIn("recoverAttempts += 1;", script)
        # The pending re-acquisition must be cancellable and must not run after a
        # grant, otherwise it reloads the shared login page for nothing.
        self.assertIn("recoveryTimer", script)
        acquire_fn = script[script.index("const acquireWorkspace"):script.index("const resetRecoveryBudget")]
        self.assertIn("window.clearTimeout(recoveryTimer)", acquire_fn)
        # The timer callback must re-check that no workspace is held. Assert it
        # inside the timer body so deleting the guard cannot pass by matching the
        # same string in renderWorkspace or pollStatus.
        timer_body = recover_fn[recover_fn.index("window.setTimeout("):]
        self.assertIn('workspace.state === "active" && workspace.active', timer_body)
        self.assertIn("acquireWorkspace()", timer_body)
        # The automatic pollers must never re-acquire: acquireWorkspace may only
        # be called from the budgeted timer, so pin the call-site count.
        self.assertEqual(1, script.count("acquireWorkspace()"), "acquireWorkspace must have exactly one call site")
        # A newly granted workspace starts a fresh QR wait on either path.
        promote_block = script[script.index("if (promoted) {"):script.index("if (displayMode === \"native\") {")]
        self.assertIn("qrPollStartedAt = Date.now()", promote_block)
        apply_fn = script[script.index("const applyWorkspace"):script.index("const acquireWorkspace")]
        self.assertIn("qrPollStartedAt = Date.now()", apply_fn)
        # Every deliberate user action renews the budget.
        self.assertGreaterEqual(script.count("resetRecoveryBudget()"), 4)
        self.assertNotIn("requestQrRefresh({", script)
        # The lease problem must not be reported as a login-desktop outage, and
        # the native-only focus button must not be offered in noVNC mode.
        self.assertNotIn("请检查 login-desktop 服务", script)
        self.assertIn('if (displayMode !== "native")', script)

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
                "current_principal",
                return_value={"username": "admin", "role": "admin", "account_refs": [], "session_id": "session-1"},
            ),
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
        for template in Path(app_module.TEMPLATES_DIR).glob("*.html"):
            text = template.read_text(encoding="utf-8")
            with self.subTest(template=template.name):
                self.assertNotIn("server_password", text)
                self.assertNotIn("server_username", text)
                self.assertNotIn("server_host", text)


if __name__ == "__main__":
    unittest.main()
