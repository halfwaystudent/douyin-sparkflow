import asyncio
from copy import deepcopy
import unittest
import inspect
from unittest.mock import patch

from core import friend_identity, friends, identity, tasks
from tests import test_shared_security as fixtures
from utils import config
from webui import app as app_module


class RefreshGenerationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SharedConsoleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client = self.fixture.login()
        self.csrf = self.fixture.token(self.client)
        self.accounts = config.get_userData(force_reload=True)
        self.accounts[0].update(login_generation="old-generation", friends_cache=["原好友"], group_cache=[{"name": "原群", "conversationId": "1000000001"}])
        config.save_userData(self.accounts)
        for registry in (app_module._friend_refresh_active, app_module._group_refresh_active, app_module._patrol_active):
            registry.discard("111")
            self.addCleanup(registry.discard, "111")

    def renew(self, *, replacement=False):
        def write(accounts):
            accounts[0].update(login_generation="new-generation", friends_cache=["新登录好友"], group_cache=[{"name": "新登录群", "conversationId": "1000000002"}])
            if replacement:
                accounts[0]["account_ref"] = "replacement"
        config.update_user_data(write, force_reload=True)

    def account(self):
        return config.get_userData(force_reload=True)[0]

    def test_sync_friend_refresh_cannot_overwrite_new_login(self):
        async def fetch(_):
            self.renew()
            return friends.FriendScanResult(["旧浏览器好友"], complete=True)
        with patch.object(app_module, "fetch_account_friends", side_effect=fetch):
            response = self.client.post("/accounts/111/friends/refresh", data={"csrf_token": self.csrf})
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual("account_changed", response.json()["category"])
        self.assertEqual(["新登录好友"], self.account()["friends_cache"])
        self.assertNotIn("friend_index", self.account())

    def test_async_group_refresh_cannot_write_into_recreated_record(self):
        captured = []
        with patch.object(app_module, "_start_background", side_effect=captured.append):
            response = self.client.post("/accounts/111/groups/refresh/async", data={"csrf_token": self.csrf})
        self.assertEqual(202, response.status_code)
        async def fetch(_):
            self.renew(replacement=True)
            return [{"name": "旧浏览器群", "conversationId": "1000000003"}]
        with patch.object(app_module, "fetch_account_groups", side_effect=fetch):
            asyncio.run(captured[0])
        self.assertEqual([{"name": "新登录群", "conversationId": "1000000002"}], self.account()["group_cache"])
        self.assertEqual("replacement", self.account()["account_ref"])

    def test_old_group_job_status_is_hidden_after_relogin(self):
        captured = []
        with patch.object(app_module, "_start_background", side_effect=captured.append):
            self.client.post("/accounts/111/groups/refresh/async", data={"csrf_token": self.csrf})
        for coroutine in captured:
            coroutine.close()
        self.renew()
        response = self.client.get("/accounts/111/groups/refresh/status")
        self.assertEqual("idle", response.json()["state"])
        self.assertNotIn("groups", response.json())

    def test_async_friend_refresh_and_its_status_discard_old_generation(self):
        endpoint = next(route.endpoint for route in self.fixture.application.routes if getattr(route, "path", "") == "/accounts/{unique_id}/friends/refresh/async")
        locals_ = inspect.getclosurevars(endpoint).nonlocals
        old = deepcopy(self.account())
        locals_["friend_refresh_jobs"]["111"] = {**identity.login_snapshot(old), "state": "running"}
        app_module._friend_refresh_active.add("111")
        async def fetch(*args, **kwargs):
            self.renew()
            return friends.FriendScanResult(["旧浏览器好友"], complete=True)
        with patch.object(app_module, "fetch_account_friends", side_effect=fetch):
            asyncio.run(locals_["_run_friend_refresh_job"]("111", old, "alice"))
        self.assertEqual(["新登录好友"], self.account()["friends_cache"])
        self.assertNotIn("friend_index", self.account())
        status = self.client.get("/accounts/111/friends/refresh/status").json()
        self.assertEqual("idle", status["state"])
        self.assertNotIn("friends", status)

    def test_relogin_between_cache_and_index_does_not_write_old_index(self):
        async def fetch(*args, **kwargs):
            return friends.FriendScanResult(["旧浏览器好友"], complete=True)
        def index(account, names, **kwargs):
            self.renew()
            return tasks.record_friend_scan(account, names, **kwargs)
        with patch.object(app_module, "fetch_account_friends", side_effect=fetch), patch.object(app_module, "record_friend_scan", side_effect=index):
            response = self.client.post("/accounts/111/friends/refresh", data={"csrf_token": self.csrf})
        self.assertEqual(409, response.status_code)
        self.assertEqual(["新登录好友"], self.account()["friends_cache"])
        self.assertNotIn("friend_index", self.account())

    def test_friend_index_and_uid_scan_each_check_generation_at_their_write(self):
        old = deepcopy(self.account())
        old["_refresh_snapshot"] = identity.login_snapshot(old)
        self.renew()
        self.assertIs(False, tasks.record_friend_scan(old, ["旧浏览器好友"], scan_complete=True))
        result = friend_identity.persist_identity_scan(old, [{"uid": "123", "nickname": "旧浏览器好友", "secUid": "x"}], scan_complete=True, scanned_names=["旧浏览器好友"])
        self.assertIsNone(result)
        self.assertNotIn("friend_index", self.account())
        self.assertNotIn("friend_uids", self.account())
