"""Issue #41 regression: re-login must not flip identity and block the schedule.

These scenarios drive the real ``/login-desktop/save`` route with synthetic
accounts, cookies and IDs. The creator self-identity endpoint is faked at the
network layer (``urllib.request.build_opener``), so the same file runs against
the implementation before the fix (where the scenarios fail: the page-read ID
is compared after overwriting and ``identity_mismatch`` is written) and after
it. Nothing here imports fix-only names.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from core import streak_state
from webui import app as app_module
from webui import users as users_module

STORED_KEY = "31415926535"            # 11-digit ID the account was first saved with
PLATFORM_UID = "2718281828459045"     # 16-digit platform UID of the same person
OTHER_UID = "9999999999999999"        # a different Douyin account
IDENTITY_MARKERS = ("identity_mismatch", "account_identity_mismatch")


class _FakeResponse:
    def __init__(self, body, status=200):
        self._body = body.encode("utf-8")
        self.status = status

    def read(self, *_args):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _FakeOpener:
    """Answers the creator self-identity request with a fixed platform UID."""

    def __init__(self, uid_for_cookie):
        self.uid_for_cookie = uid_for_cookie
        self.urls = []

    def open(self, request, timeout=None):
        del timeout
        self.urls.append(request.full_url)
        cookie = request.get_header("Cookie") or ""
        uid = self.uid_for_cookie(cookie)
        # A JSON number on purpose: the UID must survive without float rounding.
        return _FakeResponse('{"status_code":0,"user_id":%s,"token":"im-token"}' % uid)


def _account(**extra):
    base = {
        "account_ref": "acc-1",
        "unique_id": STORED_KEY,
        "username": "火花号",
        "cookies": [{"name": "sessionid", "value": "old-login", "domain": ".douyin.com", "path": "/"}],
        "targets": ["阿杰"],
        "target_states": {"阿杰": {"status": "sent"}},
        "failure_queue": {"阿杰": {"category": "protocol_user_blocked", "attempts": 1}},
        "enabled": True,
    }
    base.update(extra)
    return base


def _export(displayed_id, cookie_value):
    return {
        "unique_id": displayed_id,
        "display_id": displayed_id,
        "username": "火花号",
        "cookies": [
            {"name": "sessionid", "value": cookie_value, "domain": ".douyin.com", "path": "/"},
            {"name": "sid_guard", "value": cookie_value + "-guard", "domain": ".douyin.com", "path": "/"},
        ],
    }


class Issue41ReloginRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.accounts_path = root / "usersData.json"
        self.users_path = root / "webui_users.json"
        self.users_path.write_text(json.dumps({"auth_epoch": 1, "users": []}), encoding="utf-8")

        from utils import config as config_module

        for patcher in (
            patch.object(config_module, "get_environment", return_value=config_module.Environment.LOCAL),
            patch.object(config_module, "users_data_path", return_value=self.accounts_path),
            patch.object(users_module, "USERS_FILE", self.users_path),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        cached = config_module.userData
        config_module.userData = None
        self.addCleanup(lambda: setattr(config_module, "userData", cached))

        self.client = TestClient(app_module.app)
        self.principal = {"username": "admin", "role": "admin", "account_refs": [], "session_id": "s-1"}

    def _write(self, accounts):
        self.accounts_path.write_text(json.dumps(accounts, ensure_ascii=False), encoding="utf-8")

    def _rows(self):
        return json.loads(self.accounts_path.read_text(encoding="utf-8"))

    def _relogin(self, exported, uid_for_cookie):
        """Save a scan re-login into acc-1, as the operator's re-login button does."""
        row = next(item for item in self._rows() if item["account_ref"] == "acc-1")
        active = {"mode": "relogin", "account_ref": "acc-1", "ticket": "t1", "username": "admin", "session_id": "s-1"}
        opener = _FakeOpener(uid_for_cookie)
        with (
            patch.object(app_module, "current_user", return_value="admin"),
            patch.object(app_module, "current_principal", return_value=self.principal),
            patch.object(app_module, "validate_csrf", return_value=True),
            patch.object(app_module, "get_login_lock", return_value=active),
            patch.object(app_module, "owns_login_lock", return_value=True),
            patch.object(app_module, "call_login_desktop", return_value={"ok": True, "result": exported}),
            patch.object(app_module, "begin_login_release"),
            patch.object(app_module, "begin_login_expiration", return_value=None),
            patch.object(app_module, "get_workspace_state", return_value={}),
            patch("urllib.request.build_opener", return_value=opener),
        ):
            response = self.client.post(
                "/login-desktop/save",
                data={"csrf_token": "t", "relogin_unique_id": row["unique_id"]},
            )
        return response

    def _assert_schedulable(self, row, now=None):
        for key in IDENTITY_MARKERS:
            self.assertNotIn(key, row, f"{key} must not be written for the same person")
        verdict = streak_state.preflight_account(row, now=now)
        self.assertTrue(verdict["healthy"], verdict)

    def test_relogin_alternating_handle_and_uid_does_not_flag_or_block(self):
        # Source timeline: saved with the 11-digit ID, the next login shows the
        # 16-digit UID, the one after shows the 11-digit ID again.
        self._write([_account()])
        same_person = lambda _cookie: PLATFORM_UID  # noqa: E731

        first = self._relogin(_export(PLATFORM_UID, "login-2"), same_person)
        self.assertEqual(200, first.status_code, first.text)
        self._assert_schedulable(self._rows()[0])

        second = self._relogin(_export(STORED_KEY, "login-3"), same_person)
        self.assertEqual(200, second.status_code, second.text)

        rows = self._rows()
        self.assertEqual(1, len(rows))
        row = rows[0]
        self._assert_schedulable(row)
        # The record keeps its key, targets, ledger and unrelated failures.
        self.assertEqual("acc-1", row["account_ref"])
        self.assertEqual(STORED_KEY, row["unique_id"])
        self.assertEqual(["阿杰"], row["targets"])
        self.assertEqual({"阿杰": {"status": "sent"}}, row["target_states"])
        self.assertEqual("protocol_user_blocked", row["failure_queue"]["阿杰"]["category"])
        self.assertEqual("login-3", row["cookies"][0]["value"])
        # The next scheduled run (tomorrow) is not blocked by an identity marker.
        self._assert_schedulable(row, now=datetime.now(timezone.utc) + timedelta(days=1))

    def test_account_left_blocked_by_the_bug_recovers_on_relogin(self):
        # The state the source report ended in: the stored key flipped to the
        # UID, an identity_mismatch marker and an identity account_failure.
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
        self._write(
            [
                _account(
                    unique_id=PLATFORM_UID,
                    identity_mismatch={
                        "expected": STORED_KEY,
                        "actual": PLATFORM_UID,
                        "detectedAt": yesterday,
                        "reason": "relogin exported a different Douyin account",
                    },
                    account_failure={
                        "category": "account_identity_mismatch",
                        "reason": "login identity does not match the configured account",
                        "lastAttemptAt": yesterday,
                    },
                )
            ]
        )
        self.assertFalse(streak_state.preflight_account(self._rows()[0])["healthy"])

        response = self._relogin(_export(STORED_KEY, "login-4"), lambda _cookie: PLATFORM_UID)

        self.assertEqual(200, response.status_code, response.text)
        row = self._rows()[0]
        self._assert_schedulable(row)
        self.assertNotIn("account_failure", row)
        self.assertEqual("protocol_user_blocked", row["failure_queue"]["阿杰"]["category"])

    def test_relogin_into_a_different_account_is_refused_and_nothing_changes(self):
        bound = _account(
            platform_uid=PLATFORM_UID,
            identity_binding={"source": "creator_im_user_token", "version": 1, "method": "same_uid"},
        )
        self._write([bound])
        before = self._rows()

        response = self._relogin(_export("88888888888", "someone-else"), lambda _cookie: OTHER_UID)

        self.assertEqual(409, response.status_code, response.text)
        self.assertNotIn("someone-else", response.text)
        self.assertEqual(before, self._rows())


if __name__ == "__main__":
    unittest.main()
