"""Trusted identity (issue #41): parsing, matching, migration and background checks.

All data is synthetic; the identity endpoint is faked and no real file, cookie
or network is touched.
"""

import asyncio
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import login_desktop_server
from core import identity
from core import login as login_module
from core import streak_state
from webui import app as app_module
from webui import ops as ops_module
from webui import users as users_module

UID = "2718281828459045"
OTHER_UID = "9999999999999999"
COOKIE_SECRET = "COOKIE-SECRET-VALUE"


def trusted(uid=UID):
    return {"uid": uid, "source": identity.IDENTITY_SOURCE, "version": identity.IDENTITY_VERSION}


def bound(uid=UID):
    return {"platform_uid": uid, "identity_binding": {"source": identity.IDENTITY_SOURCE, "version": 1, "method": "same_uid"}}


def cookies(value=COOKIE_SECRET):
    return [{"name": "sessionid", "value": value, "domain": ".douyin.com", "path": "/"}]


def legacy_row(**extra):
    row = {
        "account_ref": "acc-1",
        "unique_id": "31415926535",
        "username": "火花号",
        "cookies": cookies("old-login"),
        "targets": ["阿杰"],
        "target_states": {"阿杰": {"status": "sent"}},
        "enabled": True,
    }
    row.update(extra)
    return row


class FakeStore:
    def __init__(self, accounts):
        self.accounts = copy.deepcopy(accounts)
        self.writes = 0

    def update(self, mutator, **_kwargs):
        working = copy.deepcopy(self.accounts)
        result = mutator(working)
        changed = True
        if isinstance(result, tuple) and len(result) == 2:
            result, changed = result
        if changed:
            self.accounts = working
            self.writes += 1
        return copy.deepcopy(result)


class ParseUserTokenTests(unittest.TestCase):
    def test_long_numeric_uid_is_kept_exactly(self):
        body = '{"status_code":0,"user_id":1234567890123456789,"token":"t"}'
        self.assertEqual("1234567890123456789", identity.parse_user_token_response(body))

    def test_string_uid_is_accepted(self):
        self.assertEqual(UID, identity.parse_user_token_response('{"status_code":0,"user_id":"%s"}' % UID))

    def test_invalid_answers_are_rejected(self):
        for body in (
            "",
            "not json",
            '{"status_code":8,"user_id":123}',
            '{"status_code":0}',
            '{"status_code":0,"user_id":0}',
            '{"status_code":0,"user_id":1.5e15}',
            '{"status_code":0,"user_id":true}',
            '{"status_code":0,"user_id":"abc123"}',
            '{"status_code":0,"user_id":"-12"}',
            "[1,2]",
        ):
            with self.subTest(body=body), self.assertRaises(identity.IdentityUnavailable):
                identity.parse_user_token_response(body)


class FetchTrustedIdentityTests(unittest.TestCase):
    def test_falls_back_to_the_next_route_and_reports_source(self):
        calls = []

        def opener(request, proxy, timeout):
            calls.append(proxy)
            if proxy is None:
                return 503, ""
            return 200, '{"status_code":0,"user_id":%s}' % UID

        result = identity.fetch_trusted_identity(
            cookies(), routes=[("direct", None), ("mihomo", "http://proxy:7890")], opener=opener
        )
        self.assertEqual(UID, result["uid"])
        self.assertEqual(identity.IDENTITY_SOURCE, result["source"])
        self.assertEqual(identity.IDENTITY_VERSION, result["version"])
        self.assertEqual([None, "http://proxy:7890"], calls)

    def test_failure_never_echoes_cookies_or_body(self):
        def opener(request, proxy, timeout):
            self.assertIn(COOKIE_SECRET, request.get_header("Cookie"))
            return 200, '{"status_code":7,"message":"SECRET-BODY","user_id":"%s"}' % COOKIE_SECRET

        with self.assertRaises(identity.IdentityUnavailable) as caught:
            identity.fetch_trusted_identity(cookies(), routes=[("direct", None)], opener=opener)
        self.assertNotIn(COOKIE_SECRET, str(caught.exception))
        self.assertNotIn("SECRET-BODY", str(caught.exception))
        self.assertEqual(identity.CATEGORY_IDENTITY_UNVERIFIED, caught.exception.category)

    def test_no_cookies_is_unavailable_without_a_request(self):
        def opener(*_args):
            raise AssertionError("must not call the endpoint")

        with self.assertRaises(identity.IdentityUnavailable):
            identity.fetch_trusted_identity([], routes=[("direct", None)], opener=opener)


class IdentityHelpersTests(unittest.TestCase):
    def test_display_id_keeps_letters_and_strips_the_label(self):
        self.assertEqual("abc_123", identity.display_id_from_text("抖音号：abc_123"))
        self.assertEqual("8940433898798", identity.display_id_from_text(" 8940433898798 "))

    def test_bound_uid_requires_a_trusted_binding(self):
        self.assertEqual(UID, identity.account_uid(bound()))
        self.assertEqual("", identity.account_uid({"platform_uid": UID}))
        self.assertEqual("", identity.account_uid({"unique_id": UID}))

    def test_states_and_labels(self):
        self.assertEqual("legacy", identity.identity_state({"unique_id": "1"}))
        self.assertEqual("verified", identity.identity_state(bound()))
        self.assertEqual("conflict", identity.identity_state(dict(bound(), identity_mismatch={"actual": "x"})))
        self.assertEqual(
            "unverified",
            identity.identity_state(dict(bound(), login_verification={"category": identity.CATEGORY_IDENTITY_UNVERIFIED})),
        )
        view = ops_module.identity_view(dict(bound(), display_id="abc_123"))
        self.assertEqual("身份已核实", view["identity_label"])
        self.assertEqual(UID, view["platform_uid"])
        self.assertEqual("abc_123", view["display_id"])

    def test_a_legacy_account_without_new_fields_is_not_paused(self):
        row = legacy_row()
        self.assertTrue(streak_state.preflight_account(row)["healthy"])


class SaveMatchingTests(unittest.TestCase):
    def _save(self, store, **kwargs):
        exported = kwargs.pop("exported", {"display_id": "abc_123", "username": "火花号", "cookies": cookies("new-login")})
        with (
            patch.object(app_module, "update_user_data", side_effect=store.update),
            patch.object(app_module, "remove_account_refs_from_users"),
        ):
            return app_module.save_exported_login_result(exported, **kwargs)

    def test_no_trusted_uid_writes_nothing(self):
        store = FakeStore([legacy_row()])
        for bad in (None, {"uid": ""}, {"uid": "uid_tt-fragment"}):
            with self.subTest(identity=bad), self.assertRaises(identity.IdentityUnavailable):
                self._save(store, relogin_account_ref="acc-1", identity=bad)
        self.assertEqual(0, store.writes)

    def test_page_digits_or_cookie_fragments_cannot_stand_in_for_the_uid(self):
        store = FakeStore([])
        exported = {"unique_id": "31415926535", "display_id": "31415926535", "username": "x", "cookies": cookies()}
        with self.assertRaises(identity.IdentityUnavailable):
            self._save(store, exported=exported)
        self.assertEqual(0, store.writes)

    def test_same_bound_uid_updates_in_place_whatever_the_page_shows(self):
        store = FakeStore([legacy_row(**bound())])
        for shown in ("31415926535", UID, "abc_123"):
            account, action = self._save(
                store,
                relogin_account_ref="acc-1",
                identity=trusted(),
                exported={"display_id": shown, "username": "改名了", "cookies": cookies("login-" + shown)},
            )
            self.assertEqual("updated", action)
        row = store.accounts[0]
        self.assertEqual(1, len(store.accounts))
        self.assertEqual("31415926535", row["unique_id"])
        self.assertEqual("abc_123", row["display_id"])
        self.assertEqual("改名了", row["username"])
        self.assertEqual(["阿杰"], row["targets"])

    def test_plain_login_without_target_finds_the_bound_row(self):
        store = FakeStore([legacy_row(**bound()), legacy_row(account_ref="acc-2", unique_id="555", **bound("7777777777777777"))])
        account, action = self._save(store, identity=trusted())
        self.assertEqual(("acc-1", "updated"), (account["account_ref"], action))
        self.assertEqual(2, len(store.accounts))

    def test_new_account_is_keyed_by_the_uid_and_bound(self):
        store = FakeStore([])
        account, action = self._save(store, identity=trusted())
        self.assertEqual("created", action)
        self.assertEqual(UID, account["unique_id"])
        self.assertEqual(UID, identity.account_uid(account))
        self.assertEqual(identity.BINDING_NEW, account["identity_binding"]["method"])

    def test_different_bound_uid_is_refused_before_any_write(self):
        store = FakeStore([legacy_row(**bound())])
        with self.assertRaises(app_module.IdentityConflict):
            self._save(store, relogin_account_ref="acc-1", identity=trusted(OTHER_UID))
        self.assertEqual(0, store.writes)

    def test_uid_already_bound_elsewhere_is_refused_for_a_legacy_target(self):
        store = FakeStore([legacy_row(), legacy_row(account_ref="acc-2", unique_id="555", **bound())])
        with self.assertRaises(app_module.IdentityConflict):
            self._save(
                store,
                relogin_account_ref="acc-1",
                identity=trusted(),
                legacy_check={"account_ref": "acc-1", "fingerprint": identity.record_fingerprint(store.accounts[0]), "uid": UID},
            )
        self.assertEqual(0, store.writes)

    def test_legacy_row_is_bound_when_its_own_login_proves_the_uid(self):
        store = FakeStore([legacy_row(identity_mismatch={"expected": "1", "actual": "2"})])
        check = {"account_ref": "acc-1", "fingerprint": identity.record_fingerprint(store.accounts[0]), "uid": UID}
        self._save(store, relogin_account_ref="acc-1", identity=trusted(), legacy_check=check)
        row = store.accounts[0]
        self.assertEqual(identity.BINDING_LEGACY_COOKIE, row["identity_binding"]["method"])
        self.assertEqual("31415926535", row["unique_id"])
        self.assertNotIn("identity_mismatch", row)

    def test_legacy_row_with_a_different_old_uid_is_refused(self):
        store = FakeStore([legacy_row()])
        check = {"account_ref": "acc-1", "fingerprint": identity.record_fingerprint(store.accounts[0]), "uid": OTHER_UID}
        with self.assertRaises(app_module.IdentityConflict):
            self._save(store, relogin_account_ref="acc-1", identity=trusted(), legacy_check=check)
        self.assertEqual(0, store.writes)

    def test_guessed_legacy_row_proven_to_be_someone_else_becomes_a_new_account(self):
        store = FakeStore([legacy_row()])
        check = {"account_ref": "acc-1", "fingerprint": identity.record_fingerprint(store.accounts[0]), "uid": OTHER_UID}
        account, action = self._save(
            store, relogin_account_ref="acc-1", identity=trusted(), legacy_check=check, auto_matched=True
        )
        self.assertEqual("created", action)
        self.assertEqual(2, len(store.accounts))
        self.assertEqual("old-login", store.accounts[0]["cookies"][0]["value"])

    def test_unprovable_legacy_row_asks_for_confirmation_and_writes_nothing(self):
        store = FakeStore([legacy_row()])
        check = {"account_ref": "acc-1", "fingerprint": identity.record_fingerprint(store.accounts[0]), "uid": "", "reason": "expired"}
        with self.assertRaises(app_module.NeedsIdentityConfirmation) as caught:
            self._save(store, relogin_account_ref="acc-1", identity=trusted(), legacy_check=check)
        self.assertEqual("31415926535", caught.exception.history_id)
        self.assertEqual(UID, caught.exception.uid)
        self.assertEqual(0, store.writes)

    def test_confirmation_binds_only_for_the_same_record_and_candidate(self):
        row = legacy_row()
        fingerprint = identity.record_fingerprint(row)
        # The confirmation is bound to the exact candidate it was issued for:
        # the same UID, nickname, displayed ID and cookie list the save carries.
        candidate = identity.candidate_fingerprint(
            uid=UID,
            username="火花号",
            display_id="abc_123",
            display_name="",
            cookies=cookies("new-login"),
        )
        confirm = {
            "account_ref": "acc-1",
            "fingerprint": fingerprint,
            "uid": UID,
            "candidate_fingerprint": candidate,
            "confirmed_by": "admin",
        }

        changed = FakeStore([dict(row, cookies=cookies("changed-meanwhile"))])
        with self.assertRaises(app_module.AccountChangedConcurrently):
            self._save(changed, relogin_account_ref="acc-1", identity=trusted(), confirmation=confirm)
        other_candidate = FakeStore([row])
        with self.assertRaises(app_module.AccountChangedConcurrently):
            self._save(other_candidate, relogin_account_ref="acc-1", identity=trusted(OTHER_UID), confirmation=confirm)
        self.assertEqual(0, changed.writes + other_candidate.writes)

        store = FakeStore([row])
        self._save(store, relogin_account_ref="acc-1", identity=trusted(), confirmation=confirm)
        binding = store.accounts[0]["identity_binding"]
        self.assertEqual(identity.BINDING_CONFIRMED, binding["method"])
        self.assertEqual("admin", binding["confirmedBy"])

    def test_confirmation_is_refused_when_the_candidate_cookies_changed(self):
        # Same UID, same record, but a different cookie candidate: the old
        # confirmation must not write (issue #41 A5).
        row = legacy_row()
        confirm = {
            "account_ref": "acc-1",
            "fingerprint": identity.record_fingerprint(row),
            "uid": UID,
            "candidate_fingerprint": identity.candidate_fingerprint(
                uid=UID, username="火花号", display_id="abc_123", display_name="", cookies=cookies("first-login")
            ),
            "confirmed_by": "admin",
        }
        store = FakeStore([row])
        with self.assertRaises(app_module.AccountChangedConcurrently):
            self._save(
                store,
                relogin_account_ref="acc-1",
                identity=trusted(),
                confirmation=confirm,
                exported={"display_id": "abc_123", "username": "火花号", "cookies": cookies("second-login")},
            )
        self.assertEqual(0, store.writes)

    def test_confirmation_is_refused_when_the_nickname_or_target_changed(self):
        row = legacy_row()
        confirm = {
            "account_ref": "acc-1",
            "fingerprint": identity.record_fingerprint(row),
            "uid": UID,
            "candidate_fingerprint": identity.candidate_fingerprint(
                uid=UID, username="火花号", display_id="abc_123", display_name="", cookies=cookies("new-login")
            ),
            "confirmed_by": "admin",
        }
        # Nickname differs from the confirmed candidate.
        renamed = FakeStore([row])
        with self.assertRaises(app_module.AccountChangedConcurrently):
            self._save(
                renamed,
                relogin_account_ref="acc-1",
                identity=trusted(),
                confirmation=confirm,
                exported={"display_id": "abc_123", "username": "改名了", "cookies": cookies("new-login")},
            )
        # The displayed ID differs (handle vs UID), same nickname and cookies.
        other_display = FakeStore([row])
        with self.assertRaises(app_module.AccountChangedConcurrently):
            self._save(
                other_display,
                relogin_account_ref="acc-1",
                identity=trusted(),
                confirmation=confirm,
                exported={"display_id": "999", "username": "火花号", "cookies": cookies("new-login")},
            )
        self.assertEqual(0, renamed.writes + other_display.writes)

    def test_confirmation_is_refused_when_a_cookie_metadata_field_changed(self):
        row = legacy_row()
        confirm = {
            "account_ref": "acc-1",
            "fingerprint": identity.record_fingerprint(row),
            "uid": UID,
            "candidate_fingerprint": identity.candidate_fingerprint(
                uid=UID, username="火花号", display_id="abc_123", display_name="", cookies=cookies("new-login")
            ),
            "confirmed_by": "admin",
        }
        # Same name/value, different path: the full cookie metadata is bound.
        different_meta = [
            {"name": "sessionid", "value": "new-login", "domain": ".douyin.com", "path": "/creator"}
        ]
        store = FakeStore([row])
        with self.assertRaises(app_module.AccountChangedConcurrently):
            self._save(
                store,
                relogin_account_ref="acc-1",
                identity=trusted(),
                confirmation=confirm,
                exported={"display_id": "abc_123", "username": "火花号", "cookies": different_meta},
            )
        self.assertEqual(0, store.writes)

    def test_confirmation_cannot_override_a_known_different_uid(self):
        store = FakeStore([legacy_row(**bound())])
        confirm = {"account_ref": "acc-1", "fingerprint": identity.record_fingerprint(store.accounts[0]), "uid": OTHER_UID}
        with self.assertRaises(app_module.IdentityConflict):
            self._save(store, relogin_account_ref="acc-1", identity=trusted(OTHER_UID), confirmation=confirm)
        self.assertEqual(0, store.writes)

    def test_only_login_related_failures_are_cleared(self):
        for category, cleared in (("login_required", True), ("account_identity_mismatch", True), ("protocol_rate_limited", False)):
            store = FakeStore([legacy_row(account_failure={"category": category}, identity_mismatch={"x": 1}, **bound())])
            self._save(store, relogin_account_ref="acc-1", identity=trusted())
            with self.subTest(category=category):
                self.assertEqual(cleared, "account_failure" not in store.accounts[0])
                self.assertNotIn("identity_mismatch", store.accounts[0])

    def test_dedupe_never_removes_a_row_bound_to_another_uid(self):
        store = FakeStore([legacy_row(**bound()), legacy_row(account_ref="acc-2", **bound(OTHER_UID))])
        self._save(store, relogin_account_ref="acc-1", identity=trusted())
        self.assertEqual(["acc-1", "acc-2"], [row["account_ref"] for row in store.accounts])

    def test_identity_metadata_holds_no_secret(self):
        store = FakeStore([])
        self._save(store, identity=dict(trusted(), token="im-token"), exported={"username": "x", "cookies": cookies()})
        row = store.accounts[0]
        material = json.dumps({key: value for key, value in row.items() if key != "cookies"}, ensure_ascii=False)
        self.assertNotIn(COOKIE_SECRET, material)
        self.assertNotIn("im-token", material)


class BackgroundVerificationTests(unittest.TestCase):
    def _write(self, store, *args, **kwargs):
        with patch.object(app_module, "update_user_data", side_effect=store.update):
            return app_module.write_login_verification(*args, **kwargs)

    def test_conflict_is_written_only_for_the_current_generation(self):
        row = legacy_row(login_verification={"state": "verifying", "token": "t2"}, **bound())
        stale = FakeStore([row])
        self._write(stale, "acc-1", "t1", "identity_conflict", expected_uid=UID, actual_uid=OTHER_UID)
        self.assertEqual(0, stale.writes)

        current = FakeStore([row])
        self._write(current, "acc-1", "t2", "identity_conflict", expected_uid=UID, actual_uid=OTHER_UID)
        saved = current.accounts[0]
        self.assertEqual(OTHER_UID, saved["identity_mismatch"]["actual"])
        self.assertFalse(streak_state.preflight_account(saved)["healthy"])

    def test_conflict_is_ignored_when_the_binding_changed_meanwhile(self):
        store = FakeStore([legacy_row(login_verification={"state": "verifying", "token": "t2"}, **bound(OTHER_UID))])
        self._write(store, "acc-1", "t2", "identity_conflict", expected_uid=UID, actual_uid="1")
        self.assertEqual(0, store.writes)

    def test_check_classifies_same_other_and_unreadable(self):
        account = legacy_row(**bound())
        with patch.object(app_module.account_identity, "fetch_trusted_identity", return_value=trusted()):
            self.assertEqual("verified", asyncio.run(app_module.check_saved_login_identity(account))[0])
        with patch.object(app_module.account_identity, "fetch_trusted_identity", return_value=trusted(OTHER_UID)):
            outcome = asyncio.run(app_module.check_saved_login_identity(account))
        self.assertEqual(("identity_conflict", UID, OTHER_UID), (outcome[0], outcome[3], outcome[4]))
        with patch.object(
            app_module.account_identity, "fetch_trusted_identity", side_effect=identity.IdentityUnavailable("x")
        ):
            outcome = asyncio.run(app_module.check_saved_login_identity(account))
        self.assertEqual(("unverified", identity.CATEGORY_IDENTITY_UNVERIFIED), outcome[:2])
        # A legacy record is not judged by the background check.
        with patch.object(app_module.account_identity, "fetch_trusted_identity") as fetch:
            self.assertEqual("verified", asyncio.run(app_module.check_saved_login_identity(legacy_row()))[0])
        fetch.assert_not_called()

    def test_unverified_identity_does_not_pause_sending(self):
        store = FakeStore([legacy_row(login_verification={"state": "verifying", "token": "t1"}, **bound())])
        self._write(store, "acc-1", "t1", "unverified", category=identity.CATEGORY_IDENTITY_UNVERIFIED, reason="x")
        saved = store.accounts[0]
        self.assertTrue(streak_state.preflight_account(saved)["healthy"])
        self.assertEqual("unverified", identity.identity_state(saved))


class _CookieRouteBase(unittest.TestCase):
    """Isolated stores and helpers for the /accounts/cookies route tests."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.accounts_path = root / "usersData.json"
        self.users_path = root / "webui_users.json"
        self.users_path.write_text(
            json.dumps({"auth_epoch": 1, "users": [
                {"username": "zxb", "role": "user", "password_hash": "x", "enabled": True, "session_version": 1, "account_refs": ["acc-1"]},
                {"username": "zcf", "role": "user", "password_hash": "x", "enabled": True, "session_version": 1, "account_refs": []},
            ]}),
            encoding="utf-8",
        )
        self.accounts_path.write_text(json.dumps([legacy_row()], ensure_ascii=False), encoding="utf-8")
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
        app_module._identity_confirmations.clear()
        self.addCleanup(app_module._identity_confirmations.clear)
        self.client = TestClient(app_module.app)

    def _principal(self, username="zxb", refs=("acc-1",), session="s-1"):
        return {"username": username, "role": "user", "account_refs": list(refs), "session_id": session}

    def _post(self, principal, *, uid=UID, extra=None, old_login_uid=None, cookie_input=None):
        verified = (True, "", {"unique_id": uid, "display_id": "abc_123", "username": "火花号", "trusted": trusted(uid)}, "")

        def fetch(cookie_list, **_kwargs):
            if old_login_uid is None:
                raise identity.IdentityUnavailable("old login expired")
            return trusted(old_login_uid)

        data = {"csrf_token": "t", "cookie_input": cookie_input or f"sessionid={COOKIE_SECRET}; sid_guard=g", "relogin_unique_id": "31415926535"}
        data.update(extra or {})
        with (
            patch.object(app_module, "current_user", return_value=principal["username"]),
            patch.object(app_module, "current_principal", return_value=principal),
            patch.object(app_module, "validate_csrf", return_value=True),
            patch.object(app_module, "verify_login_result", new=AsyncMock(return_value=verified)),
            patch.object(app_module.account_identity, "fetch_trusted_identity", side_effect=fetch),
            patch.object(app_module, "call_login_desktop", return_value={"ok": True}),
        ):
            return self.client.post("/accounts/cookies", data=data)

    def _row(self):
        return json.loads(self.accounts_path.read_text(encoding="utf-8"))[0]

    def _ask(self):
        response = self._post(self._principal())
        self.assertEqual(409, response.status_code, response.text)
        body = response.json()
        self.assertEqual("pending_confirmation", body["identityState"])
        info = body["identity_confirmation"]
        self.assertEqual("31415926535", info["history_id"])
        self.assertEqual(UID, info["platform_uid"])
        self.assertNotIn(COOKIE_SECRET, response.text)
        self.assertNotIn("old-login", response.text)
        self.assertNotIn("fingerprint", json.dumps(info))
        return info["token"]



class CookieRouteConfirmationTests(_CookieRouteBase):
    """The one-time migration confirmation, driven through /accounts/cookies."""

    def test_confirmed_migration_binds_once_and_cannot_be_replayed(self):
        token = self._ask()
        before = self._row()
        self.assertNotIn("platform_uid", before)

        confirmed = self._post(self._principal(), extra={"confirm_identity": token})
        self.assertEqual(200, confirmed.status_code, confirmed.text)
        row = self._row()
        self.assertEqual(UID, identity.account_uid(row))
        self.assertEqual(identity.BINDING_CONFIRMED, row["identity_binding"]["method"])
        self.assertEqual("31415926535", row["unique_id"])
        self.assertEqual(["阿杰"], row["targets"])

        replay = self._post(self._principal(), extra={"confirm_identity": token})
        self.assertEqual(409, replay.status_code)
        self.assertEqual("identity_confirmation_invalid", replay.json()["category"])

        # Bound now: the next re-login of the same UID needs no confirmation.
        again = self._post(self._principal())
        self.assertEqual(200, again.status_code, again.text)

    def test_old_login_that_proves_the_uid_needs_no_confirmation(self):
        response = self._post(self._principal(), old_login_uid=UID)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(identity.BINDING_LEGACY_COOKIE, self._row()["identity_binding"]["method"])

    def test_old_login_of_another_uid_is_a_conflict(self):
        before = self._row()
        response = self._post(self._principal(), old_login_uid=OTHER_UID)
        self.assertEqual(409, response.status_code)
        self.assertEqual("conflict", response.json()["identityState"])
        self.assertNotIn("identity_confirmation", response.json())
        self.assertEqual(before, self._row())

    def test_expired_other_session_and_changed_candidate_are_refused(self):
        before = self._row()
        token = self._ask()
        app_module._identity_confirmations[token]["expires"] = time.monotonic() - 1
        self.assertEqual(409, self._post(self._principal(), extra={"confirm_identity": token}).status_code)

        token = self._ask()
        other_session = self._post(self._principal(session="s-2"), extra={"confirm_identity": token})
        self.assertEqual(409, other_session.status_code)

        token = self._ask()
        changed = self._post(self._principal(), uid=OTHER_UID, extra={"confirm_identity": token})
        self.assertEqual(409, changed.status_code)
        self.assertEqual(before, self._row())

    def test_a_user_without_access_cannot_use_a_confirmation(self):
        token = self._ask()
        app_module._identity_confirmations[token]["username"] = "zcf"
        response = self._post(self._principal("zcf", refs=()), extra={"confirm_identity": token, "relogin_unique_id": ""})
        self.assertIn(response.status_code, (403, 409))
        self.assertNotIn("platform_uid", self._row())

    def test_unreadable_new_identity_is_retryable_and_writes_nothing(self):
        before = self._row()
        unverified = (False, "暂时无法核实账号身份", {"unique_id": "", "username": "", "display_id": "", "trusted": {}}, identity.CATEGORY_IDENTITY_UNVERIFIED)
        with (
            patch.object(app_module, "current_user", return_value="zxb"),
            patch.object(app_module, "current_principal", return_value=self._principal()),
            patch.object(app_module, "validate_csrf", return_value=True),
            patch.object(app_module, "verify_login_result", new=AsyncMock(return_value=unverified)),
        ):
            response = self.client.post(
                "/accounts/cookies",
                data={"csrf_token": "t", "cookie_input": f"sessionid={COOKIE_SECRET}; sid_guard=g"},
            )
        body = response.json()
        self.assertEqual(400, response.status_code)
        self.assertTrue(body["retryable"])
        self.assertEqual("身份暂时无法核实", body["categoryLabel"])
        self.assertNotEqual(app_module.CATEGORY_LOGIN_REQUIRED, body["category"])
        self.assertEqual(before, self._row())


class ReviewFollowUpTests(_CookieRouteBase):
    """Cases found in the independent review of the fix candidate."""

    def _post_plain(self, principal, *, display_id, old_login_uid):
        verified = (True, "", {"unique_id": UID, "display_id": display_id, "username": "火花号", "trusted": trusted()}, "")

        def fetch(cookie_list, **_kwargs):
            if old_login_uid is None:
                raise identity.IdentityUnavailable("old login expired")
            return trusted(old_login_uid)

        def post(extra=None):
            data = {"csrf_token": "t", "cookie_input": f"sessionid={COOKIE_SECRET}; sid_guard=g"}
            data.update(extra or {})
            with (
                patch.object(app_module, "current_user", return_value=principal["username"]),
                patch.object(app_module, "current_principal", return_value=principal),
                patch.object(app_module, "validate_csrf", return_value=True),
                patch.object(app_module, "verify_login_result", new=AsyncMock(return_value=verified)),
                patch.object(app_module.account_identity, "fetch_trusted_identity", side_effect=fetch),
                patch.object(app_module, "call_login_desktop", return_value={"ok": True}),
            ):
                return self.client.post("/accounts/cookies", data=data)

        return post

    def _rows(self):
        return json.loads(self.accounts_path.read_text(encoding="utf-8"))

    def _refs(self, username):
        users = json.loads(self.users_path.read_text(encoding="utf-8"))["users"]
        return next(u["account_refs"] for u in users if u["username"] == username)

    def test_legacy_key_saved_from_a_handle_with_letters_is_found(self):
        # The old save stored "abc123" as "123".
        self.accounts_path.write_text(json.dumps([legacy_row(unique_id="123")]), encoding="utf-8")
        post = self._post_plain(self._principal(), display_id="abc123", old_login_uid=UID)
        response = post()
        self.assertEqual(200, response.status_code, response.text)
        rows = self._rows()
        self.assertEqual(1, len(rows))
        self.assertEqual(("acc-1", "123", UID), (rows[0]["account_ref"], rows[0]["unique_id"], rows[0]["platform_uid"]))
        self.assertEqual(["阿杰"], rows[0]["targets"])

    def test_a_user_can_confirm_and_claim_an_unassigned_legacy_row(self):
        users = json.loads(self.users_path.read_text(encoding="utf-8"))
        for user in users["users"]:
            user["account_refs"] = []
        self.users_path.write_text(json.dumps(users), encoding="utf-8")
        principal = self._principal("zcf", refs=())
        post = self._post_plain(principal, display_id="31415926535", old_login_uid=None)
        asked = post()
        self.assertEqual(409, asked.status_code, asked.text)
        token = asked.json()["identity_confirmation"]["token"]
        confirmed = post({"confirm_identity": token})
        self.assertEqual(200, confirmed.status_code, confirmed.text)
        self.assertEqual(identity.BINDING_CONFIRMED, self._rows()[0]["identity_binding"]["method"])
        self.assertEqual(["acc-1"], self._refs("zcf"))

    def test_a_row_owned_by_someone_else_cannot_be_confirmed(self):
        token = self._ask()  # issued for zxb's own row
        app_module._identity_confirmations[token]["username"] = "zcf"
        principal = self._principal("zcf", refs=())
        post = self._post_plain(principal, display_id="", old_login_uid=None)
        response = post({"confirm_identity": token})
        self.assertEqual(403, response.status_code, response.text)
        self.assertNotIn("platform_uid", self._rows()[0])

    def test_same_uid_relogin_lifts_a_conflict_found_in_the_background(self):
        self.accounts_path.write_text(json.dumps([legacy_row(
            identity_mismatch={"kind": "platform_uid", "expected": UID, "actual": OTHER_UID},
            login_verification={"state": "identity_conflict", "token": "t1"},
            **bound(),
        )]), encoding="utf-8")
        self.assertFalse(streak_state.preflight_account(self._rows()[0])["healthy"])
        response = self._post(self._principal())
        self.assertEqual(200, response.status_code, response.text)
        row = self._rows()[0]
        self.assertNotIn("identity_mismatch", row)
        self.assertNotIn("login_verification", row)
        self.assertTrue(streak_state.preflight_account(row)["healthy"])


class ConfirmationCandidateTests(_CookieRouteBase):
    """A confirmation must be bound to the whole candidate, not only the UID."""

    def test_a_same_uid_but_different_cookie_candidate_is_refused(self):
        token = self._ask()
        before = self._row()
        changed = self._post(
            self._principal(),
            extra={"confirm_identity": token},
            cookie_input=f"sessionid=DIFFERENT-SECRET; sid_guard=g2",
        )
        self.assertEqual(409, changed.status_code, changed.text)
        self.assertNotIn("platform_uid", self._row())
        self.assertEqual(before, self._row())

    def test_the_same_candidate_is_accepted(self):
        token = self._ask()
        confirmed = self._post(self._principal(), extra={"confirm_identity": token})
        self.assertEqual(200, confirmed.status_code, confirmed.text)
        self.assertEqual(UID, identity.account_uid(self._row()))


class SaveConcurrencyTests(_CookieRouteBase):
    """A record changed during the identity check must not be half-written."""

    def test_a_bound_target_changed_during_verification_is_refused(self):
        # acc-1 is bound to UID; while this request is verifying the pasted
        # cookie, another login writes new cookies to the same record.
        self.accounts_path.write_text(
            json.dumps([legacy_row(**bound())]), encoding="utf-8"
        )

        def concurrent_write(*_args, **_kwargs):
            rows = json.loads(self.accounts_path.read_text(encoding="utf-8"))
            rows[0]["cookies"] = cookies("concurrent-login")
            self.accounts_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            return (
                True,
                "",
                {"unique_id": UID, "display_id": "abc_123", "username": "火花号", "trusted": trusted()},
                "",
            )

        with (
            patch.object(app_module, "current_user", return_value="zxb"),
            patch.object(app_module, "current_principal", return_value=self._principal()),
            patch.object(app_module, "validate_csrf", return_value=True),
            patch.object(app_module, "verify_login_result", new=AsyncMock(side_effect=concurrent_write)),
            patch.object(app_module.account_identity, "fetch_trusted_identity", side_effect=lambda *a, **k: trusted()),
            patch.object(app_module, "call_login_desktop", return_value={"ok": True}),
        ):
            response = self.client.post(
                "/accounts/cookies",
                data={
                    "csrf_token": "t",
                    "cookie_input": f"sessionid={COOKIE_SECRET}; sid_guard=g",
                    "relogin_unique_id": "31415926535",
                },
            )
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual("account_changed", response.json()["category"])
        # The concurrent login's cookies survive; this request wrote nothing.
        self.assertEqual("concurrent-login", self._row()["cookies"][0]["value"])

    def test_a_target_deleted_during_verification_is_refused(self):
        self.accounts_path.write_text(
            json.dumps([legacy_row(**bound())]), encoding="utf-8"
        )

        def concurrent_delete(*_args, **_kwargs):
            self.accounts_path.write_text("[]", encoding="utf-8")
            return (
                True,
                "",
                {"unique_id": UID, "display_id": "abc_123", "username": "火花号", "trusted": trusted()},
                "",
            )

        with (
            patch.object(app_module, "current_user", return_value="zxb"),
            patch.object(app_module, "current_principal", return_value=self._principal()),
            patch.object(app_module, "validate_csrf", return_value=True),
            patch.object(app_module, "verify_login_result", new=AsyncMock(side_effect=concurrent_delete)),
            patch.object(app_module.account_identity, "fetch_trusted_identity", side_effect=lambda *a, **k: trusted()),
            patch.object(app_module, "call_login_desktop", return_value={"ok": True}),
        ):
            response = self.client.post(
                "/accounts/cookies",
                data={
                    "csrf_token": "t",
                    "cookie_input": f"sessionid={COOKIE_SECRET}; sid_guard=g",
                    "relogin_unique_id": "31415926535",
                },
            )
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual("account_changed", response.json()["category"])
        self.assertEqual([], json.loads(self.accounts_path.read_text(encoding="utf-8")))


class CliLoginTests(unittest.TestCase):
    def _save(self, store, uid, display_id=""):
        with patch.object(login_module, "update_user_data", side_effect=store.update):
            return login_module.save_cli_login(
                trusted(uid), {"username": "火花号", "display_id": display_id, "cookies": cookies("cli")}, ["阿杰"]
            )

    def test_bound_row_is_updated_and_a_new_uid_creates_a_bound_row(self):
        store = FakeStore([legacy_row(**bound())])
        account = self._save(store, UID, display_id="abc_123")
        self.assertEqual(("acc-1", "31415926535"), (account["account_ref"], account["unique_id"]))
        created = self._save(store, OTHER_UID)
        self.assertEqual(OTHER_UID, created["unique_id"])
        self.assertEqual(OTHER_UID, identity.account_uid(created))
        self.assertEqual(2, len(store.accounts))

    def test_unverified_legacy_match_is_not_overwritten(self):
        store = FakeStore([legacy_row(unique_id="123")])
        with self.assertRaises(RuntimeError):
            self._save(store, UID, display_id="abc123")
        self.assertEqual(0, store.writes)

    def test_a_new_key_occupied_by_another_bound_uid_is_refused(self):
        # A stored key is occupied even when the row is bound to a different
        # platform UID: reusing it would collide two identities on one key.
        store = FakeStore([legacy_row(unique_id="123", **bound(OTHER_UID))])
        with self.assertRaises(RuntimeError):
            self._save(store, "123")
        self.assertEqual(0, store.writes)
        self.assertEqual(["acc-1"], [row["account_ref"] for row in store.accounts])

    def test_same_uid_recovery_clears_login_failure_and_keeps_send_state(self):
        store = FakeStore([
            legacy_row(
                **bound(),
                login_required=True,
                pending_login_verification=True,
                identity_mismatch={"kind": "platform_uid"},
                account_health={"healthy": False, "category": "login_required"},
                account_failure={"category": "login_required", "reason": "login required"},
                failure_queue={"阿杰": {"category": "protocol_user_blocked", "attempts": 1}},
                login_verification={"state": "verifying", "token": "old-token"},
            )
        ])
        account = self._save(store, UID, display_id="abc_123")
        row = store.accounts[0]
        # The proven same-UID login lifts the old login/identity state.
        self.assertNotIn("identity_mismatch", row)
        self.assertNotIn("login_required", row)
        self.assertNotIn("pending_login_verification", row)
        self.assertNotIn("account_health", row)
        self.assertNotIn("account_failure", row)
        # A background check queued for the old cookies is dropped (A7).
        self.assertNotIn("login_verification", row)
        # The existing targets and the non-login send failure survive.
        self.assertEqual(["阿杰"], row["targets"])
        self.assertEqual("protocol_user_blocked", row["failure_queue"]["阿杰"]["category"])
        self.assertEqual("acc-1", account["account_ref"])
        self.assertTrue(streak_state.preflight_account(row)["healthy"])

    def test_same_uid_recovery_keeps_a_non_login_failure(self):
        store = FakeStore([
            legacy_row(
                **bound(),
                account_failure={"category": "protocol_rate_limited", "reason": "rate limited"},
            )
        ])
        self._save(store, UID)
        self.assertEqual(
            "protocol_rate_limited", store.accounts[0]["account_failure"]["category"]
        )

    def test_an_old_background_token_cannot_write_after_a_cli_save(self):
        store = FakeStore([
            legacy_row(**bound(), login_verification={"state": "verifying", "token": "old-token"})
        ])
        self._save(store, UID, display_id="abc_123")
        self.assertNotIn("login_verification", store.accounts[0])
        writes_before = store.writes
        with patch.object(app_module, "update_user_data", side_effect=store.update):
            result = app_module.write_login_verification(
                "acc-1", "old-token", "identity_conflict", expected_uid=UID, actual_uid=OTHER_UID
            )
        self.assertIsNone(result)
        self.assertEqual(writes_before, store.writes)
        self.assertNotIn("identity_mismatch", store.accounts[0])

    def test_cli_update_keeps_existing_targets(self):
        store = FakeStore([legacy_row(**bound(), targets=["阿杰", "小明"])])
        self._save(store, UID, display_id="abc_123")
        self.assertEqual(["阿杰", "小明"], store.accounts[0]["targets"])


class PageIdentityTests(unittest.TestCase):
    def test_collected_display_id_keeps_letters(self):
        class Locator:
            def __init__(self, texts):
                self.texts = texts

            @property
            def first(self):
                return self

            async def count(self):
                return len(self.texts)

            async def is_visible(self):
                return bool(self.texts)

            async def inner_text(self):
                return self.texts[0]

        class Page:
            url = "https://creator.douyin.com/"

            def locator(self, selector):
                return Locator(
                    {
                        login_module.READY_SELECTOR: ["火花号抖音号：abc_123"],
                        login_module.XPATHS["unique_id"]: ["抖音号：abc_123"],
                        login_module.XPATHS["name"]: ["火花号"],
                    }.get(selector, [])
                )

        class Context:
            async def cookies(self):
                return cookies()

        result = asyncio.run(login_module.collect_login_result(Page(), Context(), timeout_ms=1000))
        self.assertEqual("abc_123", result["display_id"])
        self.assertEqual("火花号", result["username"])

    def test_www_fallback_derives_no_account_id_from_cookies(self):
        class Page:
            async def goto(self, *_args, **_kwargs):
                return None

            async def wait_for_load_state(self, *_args, **_kwargs):
                return None

        class Context:
            async def cookies(self):
                return [{"name": "uid_tt", "value": "fragment123"}, {"name": "sessionid", "value": "v"}]

        with patch.object(
            login_desktop_server,
            "collect_www_identity_from_page",
            new=AsyncMock(return_value={"candidates": [{"text": "火花号"}]}),
        ):
            result = asyncio.run(login_desktop_server.collect_www_login_result(Page(), Context()))
        self.assertEqual("", result["unique_id"])
        self.assertNotIn("fragment123", json.dumps({k: v for k, v in result.items() if k != "cookies"}))


class TemplateIdentityTests(unittest.TestCase):
    def test_console_templates_render_identity_state(self):
        root = Path(app_module.TEMPLATES_DIR)
        self.assertIn("identity_view(account)", (root / "accounts.html").read_text(encoding="utf-8"))
        self.assertIn("account.identity_state", (root / "send_console.html").read_text(encoding="utf-8"))
        self.assertIn("row.identity_state", (root / "dashboard.html").read_text(encoding="utf-8"))
        script = (Path(app_module.STATIC_DIR) / "app.js").read_text(encoding="utf-8")
        self.assertIn("confirm_identity: info.token", script)
        self.assertNotIn("clear_identity", script)


if __name__ == "__main__":
    unittest.main()
