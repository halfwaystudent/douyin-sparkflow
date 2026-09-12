import asyncio
import json
import multiprocessing
import os
import tempfile
import time
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import login_desktop_server
from core import browser
from core import friends
from core import protocol_dispatch
from core import tasks
from utils.config import Environment
from webui import app as app_module
from webui import auth
from webui import ops
from webui import users as user_store


class WebSessionHardeningTests(unittest.TestCase):
    def test_overview_rejects_legacy_session_without_live_principal(self):
        client = TestClient(app_module.app)
        snapshot = Mock(return_value={"summary": {}, "accounts": []})

        with (
            patch.object(app_module, "current_user", return_value="ghost"),
            patch.object(app_module, "current_principal", return_value=None),
            patch.object(app_module, "get_overview_snapshot", snapshot),
        ):
            response = client.get("/api/ops/overview")

        self.assertEqual(401, response.status_code)
        snapshot.assert_not_called()

    def test_overview_does_not_promote_admin_name_without_live_principal(self):
        client = TestClient(app_module.app)
        snapshot = Mock(return_value={"summary": {}, "accounts": []})

        with (
            patch.object(app_module, "current_user", return_value="admin"),
            patch.object(app_module, "current_principal", return_value=None),
            patch.object(app_module, "get_overview_snapshot", snapshot),
        ):
            response = client.get("/api/ops/overview")

        self.assertEqual(401, response.status_code)
        snapshot.assert_not_called()

    def test_login_page_does_not_redirect_stale_legacy_session(self):
        client = TestClient(app_module.app)

        with (
            patch.object(app_module, "current_user", return_value="ghost"),
            patch.object(app_module, "current_principal", return_value=None),
        ):
            response = client.get("/login", follow_redirects=False)

        self.assertEqual(200, response.status_code)

    def test_admin_password_change_invalidates_issued_session(self):
        settings = {}

        def get_settings(force_reload=False):
            del force_reload
            return dict(settings)

        def save_settings(new_settings):
            settings.clear()
            settings.update(new_settings)
            return dict(settings)

        class Request:
            session = {}

        with (
            patch.object(auth, "get_app_settings", side_effect=get_settings),
            patch.object(auth, "save_app_settings", side_effect=save_settings),
            patch.object(user_store, "get_app_settings", side_effect=get_settings),
        ):
            auth.bootstrap_admin_password("old-password")
            identity = user_store.authenticate("admin", "old-password")
            self.assertIsNotNone(identity)
            request = Request()
            auth.issue_session(
                request,
                identity["username"],
                role=identity["role"],
                account_refs=identity.get("account_refs", []),
                auth_version=identity["auth_version"],
            )
            self.assertIsNotNone(auth.current_principal(request))

            auth.update_admin_password("new-password")

            self.assertIsNone(auth.current_principal(request))

    def test_web_user_password_change_invalidates_issued_session(self):
        class Request:
            session = {}

        with tempfile.TemporaryDirectory() as temp_dir:
            users_file = Path(temp_dir) / "webui_users.json"
            with (
                patch.object(user_store, "USERS_FILE", users_file),
                patch.object(user_store, "get_userData", return_value=[]),
            ):
                user_store.create_web_user("alice", "old-password")
                identity = user_store.authenticate("alice", "old-password")
                request = Request()
                auth.issue_session(
                    request,
                    identity["username"],
                    role=identity["role"],
                    account_refs=identity.get("account_refs", []),
                    auth_version=identity["auth_version"],
                )
                self.assertIsNotNone(auth.current_principal(request))

                user_store.update_web_user("alice", password="new-password")

                self.assertIsNone(auth.current_principal(request))

    def test_disabled_then_reenabled_user_does_not_revive_session(self):
        class Request:
            session = {}

        with tempfile.TemporaryDirectory() as temp_dir:
            users_file = Path(temp_dir) / "webui_users.json"
            with (
                patch.object(user_store, "USERS_FILE", users_file),
                patch.object(user_store, "get_userData", return_value=[]),
            ):
                user_store.create_web_user("alice", "password")
                identity = user_store.authenticate("alice", "password")
                request = Request()
                auth.issue_session(
                    request,
                    identity["username"],
                    role=identity["role"],
                    account_refs=[],
                    auth_version=identity["auth_version"],
                )

                user_store.update_web_user("alice", enabled=False)
                user_store.update_web_user("alice", enabled=True)

                self.assertIsNone(auth.current_principal(request))

    def test_deleted_then_recreated_user_does_not_revive_session(self):
        class Request:
            session = {}

        with tempfile.TemporaryDirectory() as temp_dir:
            users_file = Path(temp_dir) / "webui_users.json"
            with (
                patch.object(user_store, "USERS_FILE", users_file),
                patch.object(user_store, "get_userData", return_value=[]),
            ):
                user_store.create_web_user("alice", "password")
                identity = user_store.authenticate("alice", "password")
                request = Request()
                auth.issue_session(
                    request,
                    identity["username"],
                    role=identity["role"],
                    account_refs=[],
                    auth_version=identity["auth_version"],
                )

                user_store.delete_web_user("alice")
                user_store.create_web_user("alice", "new-password")

                self.assertIsNone(auth.current_principal(request))

    def test_public_web_user_save_does_not_revive_legacy_session(self):
        class Request:
            session = {}

        with tempfile.TemporaryDirectory() as temp_dir:
            users_file = Path(temp_dir) / "webui_users.json"
            legacy_user = {
                "username": "alice",
                "password_hash": auth.hash_password("password"),
                "enabled": True,
                "account_refs": [],
            }
            with (
                patch.object(user_store, "USERS_FILE", users_file),
                patch.object(user_store, "get_userData", return_value=[]),
            ):
                users_file.write_text(
                    json.dumps({"auth_epoch": 0, "users": [legacy_user]}),
                    encoding="utf-8",
                )
                identity = user_store.authenticate("alice", "password")
                request = Request()
                auth.issue_session(
                    request,
                    identity["username"],
                    role=identity["role"],
                    account_refs=[],
                    auth_version=identity["auth_version"],
                )
                self.assertIsNotNone(auth.current_principal(request))

                user_store.delete_web_user("alice")
                user_store.save_web_users([legacy_user])

                self.assertIsNone(auth.current_principal(request))

    def test_renamed_user_does_not_revive_old_session(self):
        class Request:
            session = {}

        with tempfile.TemporaryDirectory() as temp_dir:
            users_file = Path(temp_dir) / "webui_users.json"
            with (
                patch.object(user_store, "USERS_FILE", users_file),
                patch.object(user_store, "get_userData", return_value=[]),
            ):
                user_store.create_web_user("alice", "password")
                identity = user_store.authenticate("alice", "password")
                request = Request()
                auth.issue_session(
                    request,
                    identity["username"],
                    role=identity["role"],
                    account_refs=[],
                    auth_version=identity["auth_version"],
                )

                user_store.update_web_user("alice", new_username="alice-renamed")

                self.assertIsNone(auth.current_principal(request))


class LoginDesktopApiHardeningTests(unittest.TestCase):
    def test_login_desktop_api_requires_configured_token(self):
        client = TestClient(login_desktop_server.app)

        with patch.dict(
            "os.environ",
            {
                "LOGIN_DESKTOP_API_BIND_ADDRESS": "127.0.0.1",
                "LOGIN_DESKTOP_API_TOKEN": "shared-secret",
                "LOGIN_DESKTOP_API_TOKEN_FILE": "",
            },
            clear=False,
        ):
            unauthorized = client.get("/debug/net_log")
            authorized = client.get(
                "/debug/net_log",
                headers={"Authorization": "Bearer shared-secret"},
            )

        self.assertEqual(401, unauthorized.status_code)
        self.assertEqual(200, authorized.status_code)

    def test_login_desktop_api_fails_closed_without_token_on_non_loopback(self):
        client = TestClient(login_desktop_server.app)

        with patch.dict(
            "os.environ",
            {
                "LOGIN_DESKTOP_API_BIND_ADDRESS": "0.0.0.0",
                "LOGIN_DESKTOP_API_TOKEN": "",
                "LOGIN_DESKTOP_API_TOKEN_FILE": "",
            },
            clear=False,
        ):
            response = client.get("/debug/net_log")

        self.assertEqual(503, response.status_code)

    def test_login_desktop_status_requires_current_workspace(self):
        client = TestClient(app_module.app)
        principal = {
            "username": "alice",
            "role": "user",
            "account_refs": ["acc-1"],
            "session_id": "session-1",
        }

        with (
            patch.object(app_module, "current_principal", return_value=principal),
            patch.object(app_module, "get_login_lock", return_value={"username": "bob", "session_id": "session-2"}),
            patch.object(app_module, "call_login_desktop", return_value={}) as call_login,
        ):
            response = client.get("/login-desktop/status")

        self.assertEqual(423, response.status_code)
        call_login.assert_not_called()

    def test_web_login_desktop_client_sends_bearer_token(self):
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                del exc_type, exc, traceback
                return False

            def read(self):
                return b"{}"

        def open_request(request, timeout):
            del timeout
            captured["headers"] = dict(request.header_items())
            return Response()

        with tempfile.TemporaryDirectory() as temp_dir:
            token_path = Path(temp_dir) / "token"
            token_path.write_text("shared-secret", encoding="utf-8")
            with (
                patch.dict(
                    os.environ,
                    {
                        "SPARKFLOW_LOGIN_DESKTOP_API_TOKEN": "",
                        "SPARKFLOW_LOGIN_DESKTOP_API_TOKEN_FILE": str(token_path),
                    },
                    clear=False,
                ),
                patch.object(app_module, "login_desktop_api_url", return_value="http://login-desktop:18090"),
                patch.object(app_module.urllib.request, "urlopen", side_effect=open_request),
            ):
                app_module.call_login_desktop("/status")

        self.assertEqual(
            "Bearer shared-secret",
            captured["headers"]["Authorization"],
        )


class ProtocolHardeningTests(unittest.TestCase):
    def test_protocol_dry_run_does_not_write_failure_queue(self):
        accounts = [
            {
                "username": "demo",
                "unique_id": "1001",
                "targets": ["target"],
            }
        ]
        result = {
            "demo": {
                "protocol_targets_cache": [],
                "sent": [
                    {
                        "target": "target",
                        "message": "hello",
                        "dryRun": True,
                        "sentAt": "2026-09-12T12:00:00+00:00",
                    }
                ],
            }
        }
        saved = []

        def update_accounts(mutator, **kwargs):
            del kwargs
            current = deepcopy(accounts)
            value = mutator(current)
            saved.append(current)
            return value

        with patch.object(
            protocol_dispatch,
            "update_user_data",
            side_effect=update_accounts,
        ):
            protocol_dispatch._merge_protocol_runtime_state(accounts, result)

        account = saved[0][0]
        self.assertNotIn("target", account.get("message_history") or {})
        self.assertNotIn("target", account.get("failure_queue") or {})

    def test_protocol_missing_success_field_is_not_strong_confirmed(self):
        accounts = [
            {
                "username": "demo",
                "unique_id": "1001",
                "targets": ["target"],
            }
        ]
        result = {
            "demo": {
                "protocol_targets_cache": [],
                "sent": [
                    {
                        "target": "target",
                        "message": "hello",
                        "statusCode": 0,
                        "statusName": "Succeeded",
                        "sentAt": "2026-09-12T12:00:00+00:00",
                    }
                ],
            }
        }
        saved = []

        def update_accounts(mutator, **kwargs):
            del kwargs
            current = deepcopy(accounts)
            value = mutator(current)
            saved.append(current)
            return value

        with patch.object(
            protocol_dispatch,
            "update_user_data",
            side_effect=update_accounts,
        ):
            protocol_dispatch._merge_protocol_runtime_state(accounts, result)

        account = saved[0][0]
        self.assertNotIn("target", account.get("message_history") or {})
        self.assertEqual(
            "protocol_send_failed",
            account["failure_queue"]["target"]["category"],
        )

    def test_protocol_nonzero_status_cannot_be_strong_confirmed(self):
        now = "2026-09-12T12:00:00+00:00"
        accounts = [
            {
                "username": "demo",
                "unique_id": "1001",
                "targets": ["target"],
            }
        ]
        result = {
            "demo": {
                "protocol_targets_cache": [],
                "sent": [
                    {
                        "target": "target",
                        "message": "hello",
                        "success": True,
                        "statusCode": 1,
                        "statusName": "UserNotInConversation",
                        "sentAt": now,
                    }
                ],
            }
        }
        saved = []

        def update_accounts(mutator, **kwargs):
            del kwargs
            current = deepcopy(accounts)
            value = mutator(current)
            saved.append(current)
            return value

        with patch.object(
            protocol_dispatch,
            "update_user_data",
            side_effect=update_accounts,
        ):
            protocol_dispatch._merge_protocol_runtime_state(accounts, result)

        account = saved[0][0]
        self.assertNotIn("target", account.get("message_history") or {})
        self.assertEqual(
            "protocol_user_not_in_conversation",
            account["failure_queue"]["target"]["category"],
        )

    def test_protocol_success_records_strong_confirmation_and_clears_failures(self):
        now = "2026-09-12T12:00:00+00:00"
        accounts = [
            {
                "username": "demo",
                "unique_id": "1001",
                "targets": ["target"],
                "failure_queue": {
                    "target": {
                        "category": "timeout",
                        "attemptCount": 1,
                        "lastAttemptAt": now,
                    }
                },
                "account_failure": {
                    "category": "timeout",
                    "attemptCount": 1,
                    "lastAttemptAt": now,
                    "affectedTargets": ["target"],
                },
            }
        ]
        result = {
            "demo": {
                "protocol_targets_cache": [],
                "sent": [
                    {
                        "target": "target",
                        "message": "hello",
                        "success": True,
                        "statusCode": 0,
                        "statusName": "Succeeded",
                        "sentAt": now,
                    }
                ],
            }
        }
        saved = []

        def update_accounts(mutator, **kwargs):
            del kwargs
            current = deepcopy(accounts)
            result = mutator(current)
            saved.append(current)
            return result

        with (
            patch.object(
                protocol_dispatch,
                "update_user_data",
                side_effect=update_accounts,
            ),
        ):
            protocol_dispatch._merge_protocol_runtime_state(accounts, result)

        account = saved[0][0]
        entry = account["message_history"]["target"]
        self.assertEqual("confirmed", entry["status"])
        self.assertEqual("strong", entry["confirmationLevel"])
        self.assertEqual("protocol_send_receipt", entry["confirmationSource"])
        self.assertFalse(entry["needsVerification"])
        self.assertNotIn("target", account.get("failure_queue") or {})
        self.assertNotIn("account_failure", account)

    def test_protocol_subprocess_uses_configured_timeout(self):
        process = SimpleNamespace(
            stdout='{"ok": true, "sent": [], "unresolved": []}',
            stderr="",
            returncode=0,
        )

        with (
            patch.object(
                protocol_dispatch,
                "_build_protocol_command",
                return_value=(["node"], Path.cwd(), "local-node", str(Path.cwd()), None),
            ),
            patch.object(protocol_dispatch.subprocess, "run", return_value=process) as run,
            patch.dict(os.environ, {"SPARKFLOW_PROTOCOL_TIMEOUT_SECONDS": "123"}, clear=False),
        ):
            protocol_dispatch._run_protocol_for_user(
                {"username": "demo", "cookies": []},
                {"target": "hello"},
                False,
                {},
            )

        self.assertEqual(123, run.call_args.kwargs["timeout"])

    def test_protocol_timeout_is_reported_as_runtime_error(self):
        with (
            patch.object(
                protocol_dispatch,
                "_build_protocol_command",
                return_value=(["node"], Path.cwd(), "local-node", str(Path.cwd()), None),
            ),
            patch.object(
                protocol_dispatch.subprocess,
                "run",
                side_effect=protocol_dispatch.subprocess.TimeoutExpired(["node"], 30),
            ),
            patch.dict(os.environ, {"SPARKFLOW_PROTOCOL_TIMEOUT_SECONDS": "30"}, clear=False),
        ):
            with self.assertRaisesRegex(RuntimeError, "timed out after 30s"):
                protocol_dispatch._run_protocol_for_user(
                    {"username": "demo", "cookies": []},
                    {"target": "hello"},
                    False,
                    {},
                )

    def test_protocol_docker_timeout_stops_named_container(self):
        process = SimpleNamespace(stdout="", stderr="", returncode=0)
        with (
            patch.object(
                protocol_dispatch,
                "_build_protocol_command",
                return_value=(
                    ["docker", "run", "--name", "sparkflow-protocol-test"],
                    Path.cwd(),
                    "docker-node-helper",
                    "/workspace",
                    "sparkflow-protocol-test",
                ),
            ),
            patch.object(
                protocol_dispatch.subprocess,
                "run",
                side_effect=[
                    protocol_dispatch.subprocess.TimeoutExpired(["docker"], 30),
                    process,
                ],
            ) as run,
            patch.dict(os.environ, {"SPARKFLOW_PROTOCOL_TIMEOUT_SECONDS": "30"}, clear=False),
        ):
            with self.assertRaisesRegex(RuntimeError, "timed out after 30s"):
                protocol_dispatch._run_protocol_for_user(
                    {"username": "demo", "cookies": []},
                    {"target": "hello"},
                    False,
                    {},
                )

        self.assertEqual(
            ["docker", "rm", "-f", "sparkflow-protocol-test"],
            run.call_args_list[1].args[0],
        )

    def test_protocol_payload_uses_unique_stable_identity(self):
        process = SimpleNamespace(
            stdout='{"ok": true, "sent": [], "unresolved": []}',
            stderr="",
            returncode=0,
        )
        user = {
            "username": "demo",
            "unique_id": "1001",
            "cookies": [],
            "protocol_targets_cache": [
                {"nickname": "Alice", "secUid": "sec-1", "peerUserId": "2001"},
            ],
        }

        with (
            patch.object(
                protocol_dispatch,
                "_build_protocol_command",
                return_value=(["node"], Path.cwd(), "local-node", str(Path.cwd()), None),
            ),
            patch.object(protocol_dispatch.subprocess, "run", return_value=process) as run,
        ):
            protocol_dispatch._run_protocol_for_user(
                user,
                {"Alice": "hello"},
                False,
                {},
            )

        payload = json.loads(run.call_args.kwargs["input"])
        self.assertEqual("sec-1", payload["targetIdentities"]["Alice"]["secUid"])

    def test_protocol_payload_marks_ambiguous_nickname(self):
        process = SimpleNamespace(
            stdout='{"ok": true, "sent": [], "unresolved": []}',
            stderr="",
            returncode=0,
        )
        user = {
            "username": "demo",
            "unique_id": "1001",
            "cookies": [],
            "protocol_targets_cache": [
                {"nickname": "Alice", "secUid": "sec-1", "peerUserId": "2001"},
                {"nickname": "Alice", "secUid": "sec-2", "peerUserId": "2002"},
            ],
        }

        with (
            patch.object(
                protocol_dispatch,
                "_build_protocol_command",
                return_value=(["node"], Path.cwd(), "local-node", str(Path.cwd()), None),
            ),
            patch.object(protocol_dispatch.subprocess, "run", return_value=process) as run,
        ):
            protocol_dispatch._run_protocol_for_user(
                user,
                {"Alice": "hello"},
                False,
                {},
            )

        payload = json.loads(run.call_args.kwargs["input"])
        self.assertTrue(payload["targetIdentities"]["Alice"]["ambiguous"])


class RuntimeHardeningTests(unittest.TestCase):
    def test_concurrent_stale_task_lock_has_single_owner(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "task.run.lock"
            lock_path.write_text("99999999\n", encoding="utf-8")
            context = multiprocessing.get_context("spawn")
            start_event = context.Event()
            queue = context.Queue()
            processes = [
                context.Process(
                    target=_try_task_lock_worker,
                    args=(str(lock_path), start_event, queue),
                )
                for _ in range(2)
            ]
            for process in processes:
                process.start()
            start_event.set()
            for process in processes:
                process.join(timeout=10)

            self.assertEqual([0, 0], [process.exitcode for process in processes])
            outcomes = sorted(queue.get(timeout=2) for _ in processes)

        self.assertEqual(["acquired", "blocked"], outcomes)

    def test_due_targets_accept_midnight_end_hour(self):
        now = datetime(2026, 9, 12, 23, 30, tzinfo=timezone.utc)
        window = {
            "enabled": True,
            "startHour": 10,
            "endHour": 24,
            "scheduleIntervalMinutes": 20,
        }
        user = {"username": "demo", "unique_id": "1001", "targets": ["target"]}

        due, already_sent, pending, queued_failures = tasks._select_due_targets(
            user,
            window,
            now,
        )

        self.assertEqual(["target"], due)
        self.assertEqual([], already_sent)
        self.assertEqual([], pending)
        self.assertEqual([], queued_failures)

    def test_dashboard_schedule_accepts_midnight_end_hour(self):
        now = datetime(2026, 9, 12, 23, 30, tzinfo=timezone.utc)
        window = {
            "enabled": True,
            "startHour": 10,
            "endHour": 24,
            "scheduleIntervalMinutes": 20,
        }

        with (
            patch.object(ops, "_normalize_send_window", return_value=window),
            patch.object(ops, "current_daily_schedule", return_value="10:00-24:00/20m"),
        ):
            snapshot = ops.get_schedule_snapshot(now=now)

        self.assertEqual("2026-09-12T23:40:00+00:00", snapshot["nextTriggerAt"])

    def test_cron_sync_accepts_midnight_end_hour(self):
        with (
            patch.object(ops, "build_scheduled_task_command", return_value="scheduled"),
            patch.object(ops, "build_unsent_fallback_task_command", return_value="fallback"),
        ):
            crontab = ops.replace_douyin_cron_schedule("", "10:00-24:00/20m")

        self.assertIn("*/20 10-23 * * * scheduled", crontab)
        self.assertIn("59 23 * * * fallback", crontab)

    def test_midnight_window_does_not_resend_after_date_rollover(self):
        now = datetime(2026, 9, 13, 0, 5, tzinfo=timezone.utc)
        window = {
            "enabled": True,
            "startHour": 10,
            "endHour": 24,
            "scheduleIntervalMinutes": 20,
        }
        user = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["target"],
            "message_history": {
                "target": {
                    "sentAt": "2026-09-12T23:50:00+00:00",
                    "status": "confirmed",
                    "confirmationLevel": "strong",
                    "needsVerification": False,
                }
            },
        }

        due, already_sent, pending, queued_failures = tasks._select_due_targets(
            user,
            window,
            now,
        )

        self.assertEqual([], due)
        self.assertEqual([], already_sent)
        self.assertEqual([], queued_failures)
        self.assertEqual(1, len(pending))

    def test_missing_playwright_browser_raises_runtime_error(self):
        class FailingPlaywright:
            async def start(self):
                raise RuntimeError("Executable doesn't exist")

        with (
            patch.object(browser, "async_playwright", return_value=FailingPlaywright()),
            patch.object(browser, "install_browser", new=AsyncMock()),
            patch.object(browser, "get_environment", return_value=Environment.LOCAL),
        ):
            with self.assertRaisesRegex(RuntimeError, "browser is missing"):
                asyncio.run(browser.get_browser())

    def test_browser_target_order_honors_shuffle_setting(self):
        targets = ["alpha", "beta", "gamma"]
        shuffled_strategy = tasks._normalize_send_strategy(
            {"sendStrategy": {"shuffleTargets": True}}
        )
        ordered_strategy = tasks._normalize_send_strategy(
            {"sendStrategy": {"shuffleTargets": False}}
        )

        with patch.object(
            tasks.random,
            "shuffle",
            side_effect=lambda items: items.reverse(),
        ):
            shuffled = tasks._ordered_targets_for_run(targets, shuffled_strategy)

        self.assertEqual(["gamma", "beta", "alpha"], shuffled)
        self.assertEqual(["alpha", "beta", "gamma"], targets)
        self.assertEqual(
            ["alpha", "beta", "gamma"],
            tasks._ordered_targets_for_run(targets, ordered_strategy),
        )

    def test_browser_send_uses_requested_target_order(self):
        class Element:
            def __init__(self, name):
                self.name = name

            async def click(self):
                return None

        alpha = Element("alpha")
        beta = Element("beta")

        class Locator:
            async def all(self):
                return [alpha, beta]

        async def extract_record(element):
            return {
                "visibleName": element.name,
                "normalizedName": element.name,
                "stableKeys": [],
            }

        async def run():
            generator = tasks.scroll_and_select_user(
                Mock(evaluate=AsyncMock()),
                {"username": "demo", "unique_id": "1001"},
                "demo",
                ["beta", "alpha"],
                {"maxScanSeconds": 60, "idleScanSeconds": 10, "scrollStepPx": 100, "scrollDelaySeconds": 0},
            )
            try:
                return await anext(generator), await anext(generator)
            finally:
                await generator.aclose()

        with (
            patch.object(tasks, "_open_friends_tab", new=AsyncMock()),
            patch.object(
                tasks,
                "_wait_for_friend_list_ready",
                new=AsyncMock(return_value=("selector", Locator())),
            ),
            patch.object(tasks, "_dismiss_non_login_dialogs", new=AsyncMock(return_value=0)),
            patch.object(
                tasks,
                "_first_non_empty_locator",
                new=AsyncMock(return_value=("selector", Locator())),
            ),
            patch.object(tasks, "_extract_friend_record", side_effect=extract_record),
            patch.object(tasks, "_selector_visible", new=AsyncMock(return_value=False)),
            patch.object(
                tasks,
                "_first_scrollable_friends_element",
                new=AsyncMock(return_value=("scroll", object())),
            ),
            patch.object(tasks, "_persist_friend_index"),
            patch.object(tasks.asyncio, "sleep", new=AsyncMock()),
        ):
            first, second = asyncio.run(run())

        self.assertEqual(("beta", "alpha"), (first, second))

    def test_missing_target_does_not_block_later_target(self):
        class Element:
            def __init__(self, name):
                self.name = name

            async def click(self):
                return None

        beta = Element("beta")

        class Locator:
            async def all(self):
                return [beta]

        async def extract_record(element):
            return {
                "visibleName": element.name,
                "normalizedName": element.name,
                "stableKeys": [],
            }

        async def selector_visible(page, selectors):
            del page
            return "没有更多" in " ".join(selectors)

        async def run():
            generator = tasks.scroll_and_select_user(
                Mock(evaluate=AsyncMock()),
                {"username": "demo", "unique_id": "1001"},
                "demo",
                ["alpha", "beta"],
                {"maxScanSeconds": 60, "idleScanSeconds": 10, "scrollStepPx": 100, "scrollDelaySeconds": 0},
            )
            try:
                return await anext(generator)
            finally:
                await generator.aclose()

        with (
            patch.object(tasks, "_open_friends_tab", new=AsyncMock()),
            patch.object(
                tasks,
                "_wait_for_friend_list_ready",
                new=AsyncMock(return_value=("selector", Locator())),
            ),
            patch.object(tasks, "_dismiss_non_login_dialogs", new=AsyncMock(return_value=0)),
            patch.object(
                tasks,
                "_first_non_empty_locator",
                new=AsyncMock(return_value=("selector", Locator())),
            ),
            patch.object(tasks, "_extract_friend_record", side_effect=extract_record),
            patch.object(tasks, "_selector_visible", side_effect=selector_visible),
            patch.object(
                tasks,
                "_first_scrollable_friends_element",
                new=AsyncMock(return_value=("scroll", object())),
            ),
            patch.object(tasks, "_persist_friend_index"),
            patch.object(tasks.asyncio, "sleep", new=AsyncMock()),
        ):
            result = asyncio.run(run())

        self.assertEqual("beta", result)


    def test_friend_refresh_cleanup_continues_after_page_close_error(self):
        closed = []

        class FakePage:
            async def goto(self, *args, **kwargs):
                del args, kwargs

            async def close(self):
                closed.append("page")
                raise RuntimeError("page close failed")

        class FakeContext:
            def __init__(self):
                self.page = FakePage()

            def set_default_navigation_timeout(self, value):
                del value

            def set_default_timeout(self, value):
                del value

            async def add_cookies(self, cookies):
                del cookies

            async def new_page(self):
                return self.page

            async def close(self):
                closed.append("context")

        class FakeBrowser:
            def __init__(self):
                self.context = FakeContext()

            async def new_context(self):
                return self.context

            async def close(self):
                closed.append("browser")

        class FakePlaywright:
            async def stop(self):
                closed.append("playwright")

        async def fake_get_browser(**kwargs):
            del kwargs
            return FakePlaywright(), FakeBrowser()

        with (
            patch.object(friends, "get_browser", side_effect=fake_get_browser),
            patch.object(friends, "collect_friend_names", new=AsyncMock(return_value=["Alice"])),
        ):
            result = asyncio.run(
                friends._fetch_account_friends_once(
                    {"cookies": [{"name": "sessionid", "value": "x"}]},
                    "direct",
                )
            )

        self.assertEqual(["Alice"], result)
        self.assertEqual(["page", "context", "browser", "playwright"], closed)

    def test_persistent_browser_setup_failure_cleans_resources(self):
        playwright = SimpleNamespace(stop=AsyncMock())
        context = SimpleNamespace(
            close=AsyncMock(),
            set_default_navigation_timeout=Mock(),
            set_default_timeout=Mock(),
        )

        with (
            patch.object(
                tasks,
                "get_persistent_browser_context",
                new=AsyncMock(return_value=(playwright, context, "profile")),
            ),
            patch.object(
                tasks,
                "apply_stored_cookies_to_profile",
                new=AsyncMock(side_effect=RuntimeError("cookie injection failed")),
            ),
            patch.object(tasks, "_random_delay_seconds", return_value=0),
        ):
            with self.assertRaisesRegex(RuntimeError, "cookie injection failed"):
                asyncio.run(
                    tasks._do_user_task_locked(
                        None,
                        {"username": "demo", "unique_id": "1001", "targets": ["target"], "cookies": []},
                        {
                            "shuffleTargets": False,
                            "accountStartDelaySecondsMin": 0,
                            "accountStartDelaySecondsMax": 0,
                            "messageIntervalSecondsMin": 0,
                            "messageIntervalSecondsMax": 0,
                        },
                        {
                            "enabled": True,
                            "root": "profile-root",
                            "seedCookiesWhenEmpty": True,
                            "syncStoredCookiesBeforeRun": True,
                            "refreshStoredCookiesAfterLogin": True,
                        },
                        {"maxScanSeconds": 60, "idleScanSeconds": 10, "scrollStepPx": 100, "scrollDelaySeconds": 0},
                        "demo",
                        "direct",
                    )
                )

        context.close.assert_awaited_once()
        playwright.stop.assert_awaited_once()


def _append_account_worker(path, unique_id, delay):
    from utils import config

    def append_account(accounts):
        time.sleep(delay)
        accounts.append({"unique_id": unique_id, "username": unique_id})
        return None, True

    config.update_user_data(append_account, path=Path(path))


def _set_config_worker(path, key, value, delay):
    from utils import config

    def set_value(current):
        time.sleep(delay)
        current[key] = value
        return None, True

    config.update_config(set_value, path=Path(path))


def _create_web_user_worker(path, username, delay):
    from unittest.mock import patch

    from webui import users

    users.USERS_FILE = Path(path)
    with patch.object(users, "get_userData", return_value=[]):
        time.sleep(delay)
        users.create_web_user(username, "password")


def _try_task_lock_worker(path, start_event, queue):
    start_event.wait()
    try:
        with tasks.task_run_lock(Path(path)):
            queue.put("acquired")
            time.sleep(0.4)
    except tasks.TaskRunAlreadyInProgress:
        queue.put("blocked")


class StateLockingHardeningTests(unittest.TestCase):
    def test_ensure_account_refs_does_not_overwrite_stale_snapshot(self):
        stale = [{"unique_id": "1001", "username": "one", "account_ref": "acc-1"}]
        latest = [
            {"unique_id": "1001", "username": "one", "account_ref": "acc-1"},
            {"unique_id": "1002", "username": "two"},
        ]
        saved = []

        with (
            patch.object(user_store, "get_userData", return_value=latest),
            patch.object(
                user_store,
                "save_userData",
                side_effect=lambda accounts: saved.append(deepcopy(accounts)),
            ),
        ):
            accounts, changed = user_store.ensure_account_refs(stale)

        self.assertTrue(changed)
        self.assertEqual(["1001", "1002"], [item["unique_id"] for item in accounts])
        self.assertEqual(["1001", "1002"], [item["unique_id"] for item in saved[0]])

    def test_concurrent_user_data_updates_preserve_both_records(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_path = Path(temp_dir) / "usersData.json"
            data_path.write_text("[]\n", encoding="utf-8")
            context = multiprocessing.get_context("spawn")
            first = context.Process(
                target=_append_account_worker,
                args=(str(data_path), "1001", 0.2),
            )
            second = context.Process(
                target=_append_account_worker,
                args=(str(data_path), "1002", 0.0),
            )

            first.start()
            time.sleep(0.05)
            second.start()
            first.join(timeout=10)
            second.join(timeout=10)

            self.assertEqual(0, first.exitcode)
            self.assertEqual(0, second.exitcode)
            records = json.loads(data_path.read_text(encoding="utf-8"))

        self.assertEqual(["1001", "1002"], sorted(item["unique_id"] for item in records))

    def test_concurrent_config_updates_preserve_both_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text("{}\n", encoding="utf-8")
            context = multiprocessing.get_context("spawn")
            first = context.Process(
                target=_set_config_worker,
                args=(str(config_path), "firstField", "first", 0.2),
            )
            second = context.Process(
                target=_set_config_worker,
                args=(str(config_path), "secondField", "second", 0.0),
            )

            first.start()
            time.sleep(0.05)
            second.start()
            first.join(timeout=10)
            second.join(timeout=10)

            self.assertEqual(0, first.exitcode)
            self.assertEqual(0, second.exitcode)
            payload = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual("first", payload["firstField"])
        self.assertEqual("second", payload["secondField"])

    def test_concurrent_web_user_creates_preserve_both_users(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            users_path = Path(temp_dir) / "webui_users.json"
            context = multiprocessing.get_context("spawn")
            first = context.Process(
                target=_create_web_user_worker,
                args=(str(users_path), "alice", 0.2),
            )
            second = context.Process(
                target=_create_web_user_worker,
                args=(str(users_path), "bob", 0.0),
            )

            first.start()
            time.sleep(0.05)
            second.start()
            first.join(timeout=10)
            second.join(timeout=10)

            self.assertEqual(0, first.exitcode)
            self.assertEqual(0, second.exitcode)
            payload = json.loads(users_path.read_text(encoding="utf-8"))

        self.assertEqual(
            ["alice", "bob"],
            sorted(item["username"] for item in payload["users"]),
        )


class DependencyHardeningTests(unittest.TestCase):
    def test_compose_wires_login_desktop_api_token(self):
        compose = (
            Path(__file__).resolve().parents[2] / "docker-compose.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("SPARKFLOW_LOGIN_DESKTOP_API_TOKEN_FILE: /run/login-api/token", compose)
        self.assertIn('LOGIN_DESKTOP_API_BIND_ADDRESS: "0.0.0.0"', compose)
        self.assertIn("LOGIN_DESKTOP_API_TOKEN_FILE: /run/login-api/token", compose)
        self.assertIn("./state/login-api:/run/login-api", compose)

    def test_requirements_pin_vulnerability_fix_versions(self):
        root = Path(__file__).resolve().parents[1]
        main_requirements = (root / "requirements.txt").read_text(encoding="utf-8")
        web_requirements = (root / "requirements-web.txt").read_text(encoding="utf-8")

        for requirement in (
            "fastapi==0.141.1",
            "filelock==3.32.6",
            "idna==3.19",
            "Pygments==2.21.0",
            "python-multipart==0.0.32",
            "requests==2.34.2",
            "starlette==1.6.0",
            "urllib3==2.7.0",
            "uvicorn==0.52.4",
        ):
            self.assertIn(requirement, main_requirements)

        for requirement in (
            "fastapi==0.141.1",
            "filelock==3.32.6",
            "python-multipart==0.0.32",
            "starlette==1.6.0",
            "uvicorn==0.52.4",
        ):
            self.assertIn(requirement, web_requirements)


if __name__ == "__main__":
    unittest.main()
