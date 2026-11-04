"""Regression contract for the PR62/live sender-protection merge.

This test intentionally parses the three core modules instead of importing them:
the regression starts at unresolved-conflict state, where imports are impossible.
It proves the merged source contains each independently required guard before any
browser or network-capable test is allowed to run.
"""

import ast
import asyncio
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core"


def source(name):
    return (CORE / name).read_text(encoding="utf-8")


def function_source(tree, text, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(text, node) or ""
    raise AssertionError(f"missing function {name}")


class PR62LiveGuardIntegrationTests(unittest.TestCase):
    def test_identity_refresh_and_sender_guards_are_composed(self):
        identity_text = source("friend_identity.py")
        friends_text = source("friends.py")
        tasks_text = source("tasks.py")
        # A parse failure is the RED state for this merge: it means a conflict
        # still separates one side's protection from the other.
        identity_tree = ast.parse(identity_text)
        friends_tree = ast.parse(friends_text)
        tasks_tree = ast.parse(tasks_text)

        self.assertNotIn("<<<<<<<", identity_text + friends_text + tasks_text)
        imported = {
            alias.name
            for node in ast.walk(identity_tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        self.assertTrue(
            {"same_login_snapshot", "normalize_unique_id", "repo_root"}.issubset(imported)
        )

        refresh = function_source(friends_tree, friends_text, "_fetch_account_friends_once")
        # Fresh pasted-cookie verification gets a disposable context, but cannot
        # bypass the same account lock/cleanup ownership as a normal refresh.
        self.assertIn('fresh_login = auth_only and account.get("_fresh_login_verification") is True', refresh)
        self.assertIn('context = await browser.new_context(service_workers="block", accept_downloads=False)', refresh)
        self.assertIn("await acquire_browser_account_lock(", refresh)
        self.assertIn("retain_browser_account_lock_for_cleanup(", refresh)

        sender = function_source(tasks_tree, tasks_text, "_do_user_task_locked")
        scan = function_source(tasks_tree, tasks_text, "_bounded_target_scan")
        self.assertIn('max(30, float(friend_scan_config.get("maxScanSeconds") or 300))', sender)
        self.assertIn("if not personal_streak.authorized(user):", sender)
        # Both kinds receive a durable unknown-outcome record before Enter;
        # friend delivery additionally re-arms the old identity guard.
        self.assertGreaterEqual(sender.count("_persist_browser_pre_enter_unknown("), 2)
        self.assertIn("await _require_friend_chat(page, user, target_name, arm=True)", sender)
        self.assertIn("Stopping before send because personal authorization changed", sender)
        self.assertIn("_dispatch_browser_enter_if_current(user, chat_input)", sender)
        self.assertIn("generator.__anext__()", scan)

        dispatch = function_source(tasks_tree, tasks_text, "_dispatch_browser_enter_if_current")
        self.assertLess(
            dispatch.index("_browser_login_generation_current(user)"),
            dispatch.index('chat_input.press("Enter")'),
        )
        collector = function_source(identity_tree, identity_text, "attach")
        self.assertIn("self.disabled = not uid_collection_enabled(self.purpose)", collector)
        self.assertIn("return self", collector)

    def test_queued_dispatch_copy_checks_generation_before_press(self):
        """Exercise the exact source function with a no-browser fake input."""
        tasks_text = source("tasks.py")
        tree = ast.parse(tasks_text)
        dispatch = function_source(tree, tasks_text, "_dispatch_browser_enter_if_current")
        from types import SimpleNamespace
        namespace = {
            "_browser_login_generation_current": lambda user: user["current"],
            "send_pause": SimpleNamespace(should_pause_sending=lambda: False),
            "personal_streak": SimpleNamespace(authorized=lambda user: True, within_send_window=lambda user: True),
        }
        exec(compile(dispatch, "<dispatch-copy>", "exec"), namespace)

        class Input:
            def __init__(self):
                self.presses = []

            async def press(self, key):
                self.presses.append(key)

        stale, current = Input(), Input()
        self.assertFalse(asyncio.run(namespace["_dispatch_browser_enter_if_current"]({"current": False}, stale)))
        self.assertEqual([], stale.presses)
        self.assertTrue(asyncio.run(namespace["_dispatch_browser_enter_if_current"]({"current": True}, current)))
        self.assertEqual(["Enter"], current.presses)


class FreshLoginLockIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_cookie_verification_is_disposable_but_lock_owned(self):
        """No browser/network is started: every browser handle is a fake."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock, patch
        from core import friends

        page = SimpleNamespace(goto=AsyncMock(), close=AsyncMock(), url=friends.CREATOR_HOME_URL)
        context = SimpleNamespace(
            set_default_navigation_timeout=Mock(),
            set_default_timeout=Mock(),
            add_cookies=AsyncMock(),
            new_page=AsyncMock(return_value=page),
            close=AsyncMock(),
        )
        browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
        playwright = SimpleNamespace(stop=AsyncMock())
        account = {
            "cookies": [{"name": "sessionid", "value": "test-only", "domain": ".douyin.com", "path": "/"}],
            "_fresh_login_verification": True,
        }
        identity = {"unique_id": "test-handle", "display_id": "test-handle", "username": "test"}
        with patch.object(friends, "get_browser", new_callable=AsyncMock, return_value=(playwright, browser)), \
             patch.object(friends, "get_persistent_browser_context", new_callable=AsyncMock) as persistent, \
             patch.object(friends, "acquire_browser_account_lock", new_callable=AsyncMock, return_value=("guard", "path", "token")) as lock, \
             patch.object(friends, "release_browser_account_lock") as release, \
             patch.object(friends, "get_config") as config, \
             patch.object(friends, "collect_login_result", new_callable=AsyncMock, return_value=identity), \
             patch.object(friends, "_wait_for_chat_or_login", new_callable=AsyncMock):
            result = await friends._fetch_account_friends_once(account, "direct", auth_only=True)

        self.assertEqual(identity, result)
        persistent.assert_not_awaited()
        config.assert_not_called()
        lock.assert_awaited_once()
        release.assert_called_once_with("guard", "path", "token", "")
        browser.new_context.assert_awaited_once_with(service_workers="block", accept_downloads=False)
        context.add_cookies.assert_awaited_once_with(account["cookies"])
        context.close.assert_awaited_once()
        browser.close.assert_awaited_once()
        playwright.stop.assert_awaited_once()
