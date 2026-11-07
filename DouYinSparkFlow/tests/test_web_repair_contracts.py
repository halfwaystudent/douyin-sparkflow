"""Regression contracts for Web repair work; no external browser or network."""

import asyncio
from copy import deepcopy
import inspect
import unittest
from unittest.mock import patch

from utils import config
from webui import app as app_module
from tests import test_shared_security as fixtures


async def _blocked_friend_fetch(*_args, **_kwargs):
    await asyncio.Event().wait()


class FriendRefreshCancellationContracts(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SharedConsoleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(app_module._friend_refresh_active.clear)
        self.account = deepcopy(config.get_userData(force_reload=True)[0])
        self.account["login_generation"] = "generation-a"
        config.save_userData([self.account, *config.get_userData(force_reload=True)[1:]])
        self.account = deepcopy(config.get_userData(force_reload=True)[0])
        endpoint = next(
            route.endpoint
            for route in self.fixture.application.routes
            if getattr(route, "path", "") == "/accounts/{unique_id}/friends/refresh/async"
        )
        self.locals = inspect.getclosurevars(endpoint).nonlocals
        self.jobs = self.locals["friend_refresh_jobs"]
        self.normalized_id = self.account["unique_id"]
        self.account_ref = self.account["account_ref"]

    def test_route_prestart_cancel_releases_only_its_registered_job(self):
        from starlette.requests import Request
        endpoint = next(route.endpoint for route in self.fixture.application.routes if getattr(route, "path", "") == "/accounts/{unique_id}/friends/refresh/async")
        body = ("csrf_token=synthetic&account_ref=" + self.account_ref + "&login_generation=generation-a").encode()
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}
        request = Request({"type": "http", "method": "POST", "path": "/accounts/" + self.normalized_id + "/friends/refresh/async", "query_string": b"", "headers": [(b"content-type", b"application/x-www-form-urlencoded")], "session": {}}, receive)
        created = []
        async def exercise():
            original = asyncio.create_task
            def cancel_before_start(coro):
                task = original(coro); created.append(task); task.cancel(); return task
            with patch.object(app_module, "current_user", return_value="owner"), patch.object(app_module, "current_principal", return_value={"username": "owner", "role": "admin", "session_id": "s"}), patch.object(app_module, "validate_csrf", return_value=True), patch.object(app_module, "task_run_lock_status", return_value={}), patch.object(app_module, "get_login_lock", return_value=None), patch.object(asyncio, "create_task", side_effect=cancel_before_start):
                response = await endpoint(request, self.normalized_id)
            self.assertEqual(202, response.status_code)
            for task in created:
                with self.assertRaises(asyncio.CancelledError): await task
            await asyncio.sleep(0)
        asyncio.run(exercise())
        self.assertNotIn(self.normalized_id, app_module._friend_refresh_active)
        self.assertNotIn(self.account_ref, self.jobs)

    def test_cancelled_friend_job_removes_its_running_status_and_active_marker(self):
        self.jobs[self.account_ref] = {
            "account_ref": self.account_ref,
            "login_generation": "generation-a",
            "state": "running",
        }
        app_module._friend_refresh_active.add(self.normalized_id)

        async def cancel_job():
            with patch.object(app_module, "fetch_account_friends", side_effect=_blocked_friend_fetch):
                task = asyncio.create_task(
                    self.locals["_run_friend_refresh_job"](
                        self.normalized_id,
                        self.account,
                        "alice",
                    )
                )
                await asyncio.sleep(0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        asyncio.run(cancel_job())
        self.assertNotIn(self.normalized_id, app_module._friend_refresh_active)
        self.assertNotIn(self.account_ref, self.jobs)

    def test_cancelled_old_generation_never_clears_a_replacement_job(self):
        self.jobs[self.account_ref] = {
            "account_ref": self.account_ref,
            "login_generation": "generation-a",
            "state": "running",
        }
        app_module._friend_refresh_active.add(self.normalized_id)

        async def replace_then_cancel():
            with patch.object(app_module, "fetch_account_friends", side_effect=_blocked_friend_fetch):
                task = asyncio.create_task(
                    self.locals["_run_friend_refresh_job"](
                        self.normalized_id,
                        self.account,
                        "alice",
                    )
                )
                await asyncio.sleep(0)
                self.jobs[self.account_ref] = {
                    "account_ref": self.account_ref,
                    "login_generation": "generation-b",
                    "state": "running",
                }
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        asyncio.run(replace_then_cancel())
        self.assertEqual("generation-b", self.jobs[self.account_ref]["login_generation"])
        self.assertIn(self.normalized_id, app_module._friend_refresh_active)

    def test_cancelled_job_done_callback_keeps_same_generation_replacement(self):
        original_job = {
            "account_ref": self.account_ref,
            "login_generation": "generation-a",
            "state": "running",
        }
        self.jobs[self.account_ref] = original_job
        app_module._friend_refresh_active.add(self.normalized_id)
        runner = self.locals["_run_friend_refresh_job"]
        finish_index = runner.__code__.co_freevars.index("_finish_friend_job")
        finish_cell = runner.__closure__[finish_index]
        original_finish = finish_cell.cell_contents
        replacement = {
            "account_ref": self.account_ref,
            "login_generation": "generation-a",
            "state": "running",
        }
        cleanup_calls = 0

        def replace_after_first_cleanup(*args, **kwargs):
            nonlocal cleanup_calls
            result = original_finish(*args, **kwargs)
            if kwargs.get("remove") and cleanup_calls == 0:
                cleanup_calls += 1
                self.jobs[self.account_ref] = replacement
                app_module._friend_refresh_active.add(self.normalized_id)
            return result

        async def cancel_then_run_done_callback():
            with patch.object(app_module, "fetch_account_friends", side_effect=_blocked_friend_fetch):
                task = asyncio.create_task(runner(self.normalized_id, self.account, "alice"))
                await asyncio.sleep(0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        finish_cell.cell_contents = replace_after_first_cleanup
        try:
            asyncio.run(cancel_then_run_done_callback())
        finally:
            finish_cell.cell_contents = original_finish

        self.assertIs(replacement, self.jobs[self.account_ref])
        self.assertIn(self.normalized_id, app_module._friend_refresh_active)


class PanelSettingsPortContracts(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SharedConsoleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client = self.fixture.login("owner", "Owner-secret-2026!")
        self.csrf = self.fixture.token(self.client)

    def test_non_numeric_and_out_of_range_ports_leave_settings_unchanged(self):
        for invalid in ("not-a-port", "0", "-1", "65536"):
            with self.subTest(invalid=invalid):
                before = deepcopy(config.get_app_settings(force_reload=True))
                response = self.client.post(
                    "/settings",
                    data={"csrf_token": self.csrf, "ui_port": invalid},
                    follow_redirects=False,
                )
                self.assertIn(response.status_code, {400, 303, 422})
                self.assertEqual("/settings?_anchor=system", response.headers.get("location"))
                self.assertEqual(before, config.get_app_settings(force_reload=True))


class EncodedLoginAnchorContracts(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SharedConsoleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client = self.fixture.client

    def test_encoded_numeric_account_login_anchor_redirects_only_to_internal_console_anchor(self):
        response = self.client.get("/login%23account-123456", follow_redirects=False)
        self.assertEqual(303, response.status_code)
        self.assertEqual("/ops/send-console?_anchor=account-123456", response.headers["location"])

    def test_plain_login_route_keeps_its_existing_page_behavior(self):
        response = self.client.get("/login", follow_redirects=False)
        self.assertEqual(200, response.status_code)

    def test_encoded_login_anchor_rejects_non_numeric_and_query_redirects(self):
        for path in (
            "/login%23account-abc",
            "/login%23account-123456?next=https://evil.example/",
            "/login%23account-123456?next=/admin/users",
        ):
            with self.subTest(path=path):
                response = self.client.get(path, follow_redirects=False)
                self.assertEqual(404, response.status_code)


if __name__ == "__main__":
    unittest.main()
