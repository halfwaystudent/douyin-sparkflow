import copy
import unittest
from pathlib import Path
from unittest.mock import patch

from core import streak_state
from webui import app as app_module


ACCOUNT = {
    "account_ref": "acc-1",
    "unique_id": "1001",
    "username": "主号",
    "cookies": [{"name": "sessionid", "value": "x", "domain": ".douyin.com"}],
    "targets": ["阿杰"],
    "enabled": True,
}


class FakeStore:
    """In-memory stand-in for usersData so no test touches the real file."""

    def __init__(self, accounts):
        self.accounts = copy.deepcopy(accounts)

    def update(self, mutator, **_kwargs):
        result = mutator(self.accounts)
        if isinstance(result, tuple) and len(result) == 2:
            result = result[0]
        return copy.deepcopy(result)

    def read(self, **_kwargs):
        return copy.deepcopy(self.accounts)


class SaveFirstVerifyLaterTests(unittest.TestCase):
    def _save(self, store, **kwargs):
        exported = {"unique_id": "1001", "username": "主号", "cookies": ACCOUNT["cookies"]}
        # The row is already bound to the trusted UID this login reports, so the
        # save is a same-account update (issue #41).
        for row in store.accounts:
            row.setdefault("platform_uid", "1001")
            row.setdefault("identity_binding", {"source": "creator_im_user_token", "version": 1, "method": "same_uid"})
        identity = {"uid": "1001", "source": "creator_im_user_token", "version": 1}
        with (
            patch.object(app_module, "update_user_data", side_effect=store.update),
            patch.object(app_module, "remove_account_refs_from_users"),
        ):
            return app_module.save_exported_login_result(
                exported, relogin_account_ref="acc-1", identity=identity, **kwargs
            )

    def test_a_pending_save_is_not_paused(self):
        stale = dict(ACCOUNT, login_required=True, pending_login_verification=True,
                     account_health={"healthy": False, "category": "login_required"})
        store = FakeStore([stale])
        account, _ = self._save(store, is_healthy=None, verification_token="t1")
        saved = store.accounts[0]
        self.assertEqual({"state": "verifying", "token": "t1"},
                         {k: saved["login_verification"][k] for k in ("state", "token")})
        for key in app_module.HEALTH_CLEARING_KEYS:
            self.assertNotIn(key, saved)
        # Sending treats an account under verification as usable.
        self.assertTrue(streak_state.preflight_account(saved)["healthy"])
        self.assertEqual("verifying", account["login_verification"]["state"])

    def test_a_verified_save_drops_any_old_verification(self):
        store = FakeStore([dict(ACCOUNT, login_verification={"state": "unverified", "token": "old"})])
        self._save(store, is_healthy=True)
        self.assertNotIn("login_verification", store.accounts[0])

    def _write(self, store, *args, **kwargs):
        with patch.object(app_module, "update_user_data", side_effect=store.update):
            return app_module.write_login_verification(*args, **kwargs)

    def test_verified_clears_the_flag(self):
        store = FakeStore([dict(ACCOUNT, login_verification={"state": "verifying", "token": "t1"})])
        saved = self._write(store, "acc-1", "t1", "verified")
        self.assertNotIn("login_verification", store.accounts[0])
        self.assertEqual("1001", saved["unique_id"])
        self.assertTrue(streak_state.preflight_account(store.accounts[0])["healthy"])

    def test_a_passed_check_keeps_newer_problems(self):
        # A relogin into a different Douyin id, or a real send failure written
        # after the save, must survive the background check passing.
        failure = {"category": "login_required", "reason": "发送时登录失效"}
        store = FakeStore([dict(ACCOUNT, login_verification={"state": "verifying", "token": "t1"},
                                identity_mismatch={"expected": "1001", "actual": "2002"},
                                account_failure=failure)])
        self._write(store, "acc-1", "t1", "verified")
        account = store.accounts[0]
        self.assertNotIn("login_verification", account)
        self.assertEqual("2002", account["identity_mismatch"]["actual"])
        self.assertEqual(failure, account["account_failure"])

    def test_a_dead_login_pauses_the_account(self):
        store = FakeStore([dict(ACCOUNT, login_verification={"state": "verifying", "token": "t1"})])
        self._write(store, "acc-1", "t1", "login_required", category="login_required", reason="登录已失效")
        account = store.accounts[0]
        self.assertEqual("login_required", account["login_verification"]["state"])
        self.assertTrue(account["login_required"])
        self.assertIs(False, account["account_health"]["healthy"])
        self.assertFalse(streak_state.preflight_account(account)["healthy"])

    def test_an_unconfirmed_check_does_not_pause_sending(self):
        # D12 / Q4: a network or page problem is shown, not enforced.
        store = FakeStore([dict(ACCOUNT, login_verification={"state": "verifying", "token": "t1"})])
        self._write(store, "acc-1", "t1", "unverified", category="network_unavailable", reason="超时")
        account = store.accounts[0]
        self.assertEqual("unverified", account["login_verification"]["state"])
        self.assertNotIn("login_required", account)
        self.assertNotIn("account_health", account)
        self.assertTrue(streak_state.preflight_account(account)["healthy"])

    def test_a_newer_login_or_a_deleted_account_drops_the_result(self):
        store = FakeStore([dict(ACCOUNT, login_verification={"state": "verifying", "token": "newer"})])
        self.assertIsNone(self._write(store, "acc-1", "older", "login_required"))
        self.assertEqual("verifying", store.accounts[0]["login_verification"]["state"])
        self.assertIsNone(self._write(FakeStore([]), "acc-1", "older", "verified"))


class SaveRouteSourceTests(unittest.TestCase):
    """The save must not wait for a second browser; the check is queued after release."""

    def setUp(self):
        self.source = Path(app_module.__file__).read_text(encoding="utf-8")
        start = self.source.index('@app.post("/login-desktop/save")')
        self.save = self.source[start:]

    def test_save_does_not_verify_inline(self):
        self.assertNotIn("await verify_login_result(", self.save)
        self.assertIn("require_auth_cookies(", self.save)
        self.assertIn('"verification": "pending" if verified is None', self.save)

    def test_check_is_queued_after_the_workspace_is_released(self):
        release = self.save.index("begin_login_release(")
        queued = self.save.index("_queue_login_verification(")
        self.assertLess(release, queued)

    def test_checks_run_one_at_a_time_when_the_browser_is_free(self):
        body = self.source[self.source.index("async def _verify_saved_login"):self.source.index("def _queue_login_verification")]
        self.assertIn("async with login_verification_lock:", body)
        # The wait limit starts on this account's turn, not when it was queued.
        self.assertLess(body.index("async with login_verification_lock:"),
                        body.index("deadline = loop.time() + LOGIN_VERIFICATION_MAX_WAIT_SECONDS"))
        # Only a page that shows the login is gone may pause the account (Q4):
        # timeouts and other errors are "unverified".
        self.assertNotIn("verify_login_result(", body)
        self.assertIn("verify_account_session(account, auth_only=True)", body)
        login_branch = body[body.index("except FriendRefreshError as exc:"):body.index("except asyncio.TimeoutError:")]
        self.assertIn("streak_state.LOGIN_FAILURE_CATEGORIES", login_branch)
        rest = body[body.index("except asyncio.TimeoutError:"):body.index("logger.info(")]
        self.assertNotIn('"login_required"', rest)
        free = self.source[self.source.index("async def _wait_until_browser_is_free"):]
        free = free[: free.index("return False")]
        self.assertIn('task_run_lock_status().get("running")', free)
        self.assertIn("get_login_lock()", free)
        self.assertIn("LOGIN_VERIFICATION_TIMEOUT_SECONDS", body)
        # A verified login refreshes the friend list right away.
        self.assertIn("await _run_friend_refresh_job(", body)

    def test_restart_requeues_unfinished_checks(self):
        lifespan = self.source[self.source.index("async def lifespan(_app):"):]
        lifespan = lifespan[: lifespan.index("yield")]
        self.assertIn("_requeue_pending_login_verifications()", lifespan)
        requeue = self.source[self.source.index("def _requeue_pending_login_verifications"):]
        requeue = requeue[: requeue.index("delay=LOGIN_VERIFICATION_RESTART_DELAY_SECONDS")]
        self.assertIn('check.get("state") == "verifying"', requeue)


class VerificationShownOnPagesTests(unittest.TestCase):
    def test_a_failed_load_is_not_a_logout(self):
        from core import friends

        class Page:
            def __init__(self, url):
                self.url = url

        self.assertFalse(friends._left_creator_host(Page("chrome-error://chromewebdata/")))
        self.assertFalse(friends._left_creator_host(Page("about:blank")))
        self.assertTrue(friends._left_creator_host(Page("https://www.douyin.com/login")))
        self.assertFalse(friends._left_creator_host(Page("https://creator.douyin.com/creator-micro/home")))
    def test_pages_name_the_verification_state(self):
        templates = Path(app_module.TEMPLATES_DIR)
        accounts = (templates / "accounts.html").read_text(encoding="utf-8")
        for label in ("登录态验证中", "登录态未能验证", "需重新登录"):
            self.assertIn(label, accounts)
        for name in ("dashboard.html", "send_console.html"):
            page = (templates / name).read_text(encoding="utf-8")
            self.assertIn("login_verification_state", page)
            self.assertIn("登录态验证中", page)
        ops = (Path(app_module.__file__).parent / "ops.py").read_text(encoding="utf-8")
        self.assertIn('"login_verification_state":', ops)


if __name__ == "__main__":
    unittest.main()
