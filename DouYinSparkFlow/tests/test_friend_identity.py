import asyncio
import json
import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from core import friend_identity, friends as friends_module, streak_state, tasks
from utils import config as config_module
from webui import app as app_module


NOW = datetime(2026, 10, 14, 9, 30, tzinfo=timezone(timedelta(hours=8)))
ACCOUNT_ID = "1234567890"
UID_MATCHA = "73001234567891001"
UID_BOB = "73001234567892002"
UID_OTHER = "73001234567893003"

# Shape measured on the production creator chat page (values made up).
USER_DETAIL_FIXTURE = {
    "status_code": 0,
    "status_msg": "",
    "extra": {"now": 1760405400000},
    "user_list": [
        {
            "user_id": UID_MATCHA,
            "user": {
                "nickname": "抹茶",
                "SecretUseId": "MS4wLjABAAAA-matcha",
                "ShortId": "0",
                "signature": "",
                "avatar_thumb": {"url_list": []},
            },
        },
        {
            "user_id": UID_BOB,
            "user": {
                "nickname": "Bob",
                "SecretUseId": "MS4wLjABAAAA-bob",
                "ShortId": "0",
                "signature": "hi",
                "avatar_thumb": {},
            },
        },
    ],
}


def isolate_account_data(test_case, accounts):
    temp_dir = tempfile.TemporaryDirectory()
    test_case.addCleanup(temp_dir.cleanup)
    path = Path(temp_dir.name) / "usersData.json"
    path.write_text(json.dumps(accounts, ensure_ascii=False), encoding="utf-8")
    patches = [
        patch.object(
            config_module,
            "get_environment",
            return_value=config_module.Environment.LOCAL,
        ),
        patch.object(config_module, "users_data_path", return_value=path),
    ]
    for item in patches:
        item.start()
        test_case.addCleanup(item.stop)
    cached = config_module.userData
    config_module.userData = None
    test_case.addCleanup(lambda: setattr(config_module, "userData", cached))
    return path


def stored_accounts(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def entries(*pairs):
    return [
        {"uid": uid, "nickname": nickname, "secUid": sec}
        for uid, nickname, sec in pairs
    ]


def bound_account(name="抹茶叔叔", uid=UID_MATCHA, **extra):
    account = {
        "account_ref": "acc-1",
        "unique_id": ACCOUNT_ID,
        "username": "demo",
        "enabled": True,
        "targets": [name],
        "target_uid_bindings": {
            name: {"uid": uid, "secUid": "", "boundAt": NOW.isoformat(), "source": "web"}
        },
    }
    account.update(extra)
    return account


class UserDetailParsingTests(unittest.TestCase):
    def test_preserves_opaque_friend_id_through_parse_bind_and_rename(self):
        opaque = "u_" + "A" * 51 + "-Z"
        payload = {"status_code": 0, "user_list": [
            {"user_id": opaque, "user": {"nickname": "Before", "SecretUseId": "sec-example"}}
        ]}
        parsed, error = friend_identity.parse_user_detail_payload(payload)
        self.assertEqual("", error)
        self.assertEqual([{"uid": opaque, "nickname": "Before", "secUid": "sec-example"}], parsed)
        account = {"username": "test", "targets": ["Before"]}
        result = friend_identity.reconcile_targets(account, parsed, scan_complete=True, scanned_names=["Before"], now=NOW)
        self.assertTrue(result["targets"][0]["bound"])
        self.assertEqual(opaque, account["target_uid_bindings"]["Before"]["uid"])
        renamed = [{"uid": opaque, "nickname": "After", "secUid": "sec-example"}]
        friend_identity.reconcile_targets(account, renamed, scan_complete=True, scanned_names=["After"], now=NOW)
        self.assertEqual(["After"], account["targets"])
        self.assertNotIn(opaque, json.dumps(account.get("identity_events")))

    def test_rejects_unsafe_friend_identifiers(self):
        for value in (True, False, [], {}, "a b", "a\tb", "a\nb", "https://example.test", "é", " x", "x ", "a" * 257):
            with self.subTest(value=value):
                self.assertEqual("", friend_identity._coerce_uid(value))

    def test_reports_nonempty_response_when_all_friend_records_are_unusable(self):
        parsed, error = friend_identity.parse_user_detail_payload({
            "status_code": 0, "user_list": [{"user_id": True, "user": {"nickname": "Invalid"}}]
        })
        self.assertEqual([], parsed)
        self.assertEqual("no_usable_user_entries", error)

    def test_parses_production_shape(self):
        parsed, error = friend_identity.parse_user_detail_payload(USER_DETAIL_FIXTURE)

        self.assertEqual("", error)
        self.assertEqual(
            [
                {"uid": UID_MATCHA, "nickname": "抹茶", "secUid": "MS4wLjABAAAA-matcha"},
                {"uid": UID_BOB, "nickname": "Bob", "secUid": "MS4wLjABAAAA-bob"},
            ],
            parsed,
        )

    def test_parses_json_text_and_bytes(self):
        text = json.dumps(USER_DETAIL_FIXTURE, ensure_ascii=False)
        self.assertEqual(2, len(friend_identity.parse_user_detail_payload(text)[0]))
        self.assertEqual(
            2,
            len(friend_identity.parse_user_detail_payload(text.encode("utf-8"))[0]),
        )

    def test_skips_items_with_missing_or_mistyped_fields(self):
        payload = {
            "status_code": 0,
            "user_list": [
                {"user": {"nickname": "no uid"}},
                {"user_id": "bad id", "user": {"nickname": "invalid whitespace"}},
                {"user_id": True, "user": {"nickname": "bool uid"}},
                {"user_id": "1001", "user": "not a dict"},
                {"user_id": "1002", "user": {"nickname": 42}},
                {"user_id": "1003", "user": {"nickname": "   "}},
                "not a dict",
                {"user_id": 1004, "user": {"nickname": "int uid", "SecretUseId": 7}},
                {"user_id": "1005", "user": {"nickname": "ok"}},
            ],
        }

        parsed, error = friend_identity.parse_user_detail_payload(payload)

        self.assertEqual("", error)
        self.assertEqual(
            [
                {"uid": "1004", "nickname": "int uid", "secUid": ""},
                {"uid": "1005", "nickname": "ok", "secUid": ""},
            ],
            parsed,
        )

    def test_unusable_bodies_return_empty_with_reason(self):
        cases = {
            "<html>blocked</html>": "invalid_json",
            "[1, 2]": "unexpected_payload_type",
            json.dumps({"status_code": 0}): "missing_user_list",
            json.dumps({"status_code": 8, "user_list": []}): "status_code=8",
            b"\xff\xfe": "undecodable_body",
        }
        for body, reason in cases.items():
            with self.subTest(body=body):
                self.assertEqual(
                    ([], reason),
                    friend_identity.parse_user_detail_payload(body),
                )

    def test_duplicate_uid_keeps_the_later_nickname(self):
        payload = {
            "user_list": [
                {"user_id": "1001", "user": {"nickname": "old"}},
                {"user_id": "1001", "user": {"nickname": "new"}},
            ]
        }
        parsed, _ = friend_identity.parse_user_detail_payload(payload)
        self.assertEqual([{"uid": "1001", "nickname": "new", "secUid": ""}], parsed)


class _FakeResponse:
    def __init__(self, url, body, *, json_fails=False):
        self.url = url
        self._body = body
        self._json_fails = json_fails

    async def json(self):
        if self._json_fails:
            raise ValueError("not json")
        return json.loads(self._body)

    async def text(self):
        return self._body


class _FakePage:
    def __init__(self):
        self.handlers = {}

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        self.handlers.get(event, []).remove(handler)

    def emit(self, event, payload):
        for handler in list(self.handlers.get(event, [])):
            handler(payload)


USER_DETAIL_URL = (
    "https://creator.douyin.com/aweme/v1/creator/im/user_detail/?aid=2906&msToken=x"
)


class UserDetailCollectorTests(unittest.TestCase):
    def test_collects_and_merges_multiple_responses(self):
        page = _FakePage()

        async def run():
            collector = friend_identity.UserDetailCollector("demo").attach(page)
            first = {
                "status_code": 0,
                "user_list": [
                    {"user_id": "1001", "user": {"nickname": "Alice", "SecretUseId": "s1"}},
                    {"user_id": "1002", "user": {"nickname": "Bob"}},
                ],
            }
            second = {
                "status_code": 0,
                "user_list": [
                    {"user_id": "1002", "user": {"nickname": "Bobby", "SecretUseId": "s2"}},
                    {"user_id": "1003", "user": {"nickname": "Carol"}},
                ],
            }
            page.emit("response", _FakeResponse(USER_DETAIL_URL, json.dumps(first)))
            page.emit(
                "response",
                _FakeResponse("https://creator.douyin.com/aweme/v1/creator/im/other/", "{}"),
            )
            page.emit("response", _FakeResponse(USER_DETAIL_URL, json.dumps(second)))
            await collector.drain()
            collector.detach()
            return collector

        collector = asyncio.run(run())

        self.assertEqual(
            [
                {"uid": "1001", "nickname": "Alice", "secUid": "s1"},
                {"uid": "1002", "nickname": "Bobby", "secUid": "s2"},
                {"uid": "1003", "nickname": "Carol", "secUid": ""},
            ],
            collector.entries(),
        )
        self.assertEqual(2, collector.responses_seen)
        self.assertEqual(2, collector.version)
        self.assertEqual([], page.handlers["response"])

    def test_falls_back_to_text_and_records_unparsable_bodies(self):
        page = _FakePage()

        async def run():
            collector = friend_identity.UserDetailCollector("demo").attach(page)
            page.emit(
                "response",
                _FakeResponse(
                    USER_DETAIL_URL,
                    json.dumps(USER_DETAIL_FIXTURE),
                    json_fails=True,
                ),
            )
            page.emit(
                "response",
                _FakeResponse(USER_DETAIL_URL, "<html>", json_fails=True),
            )
            await collector.drain()
            return collector

        collector = asyncio.run(run())

        self.assertEqual(2, len(collector.entries()))
        self.assertEqual(["invalid_json"], collector.errors)
        self.assertEqual(1, collector.responses_parsed)

    def test_collector_never_issues_requests(self):
        page = Mock(spec=["on", "remove_listener"])
        collector = friend_identity.UserDetailCollector("demo").attach(page)
        collector.detach()
        self.assertEqual(
            ["on", "remove_listener"],
            [call[0] for call in page.method_calls],
        )


class ReconcileTargetsTests(unittest.TestCase):
    def test_unique_nickname_auto_binds_unbound_target(self):
        account = {"username": "demo", "targets": ["Bob"], "friend_index": {"bob": {"stableKeys": []}}}

        result = friend_identity.reconcile_targets(
            account,
            entries((UID_BOB, "Bob", "sec-bob")),
            scan_complete=True,
            scanned_names=["Bob"],
            now=NOW,
        )

        self.assertEqual("found", result["targets"][0]["status"])
        self.assertTrue(result["targets"][0]["autoBound"])
        self.assertEqual(UID_BOB, account["target_uid_bindings"]["Bob"]["uid"])
        self.assertEqual("auto", account["target_uid_bindings"]["Bob"]["source"])
        self.assertEqual("sec:sec-bob", streak_state.resolve_target_ref(account, "Bob"))
        self.assertEqual(
            ["sec:sec-bob", f"peer:{UID_BOB}"],
            account["friend_index"]["bob"]["stableKeys"],
        )
        self.assertEqual({"nickname": "Bob", "secUid": "sec-bob"}, {
            key: account["friend_uids"][UID_BOB][key] for key in ("nickname", "secUid")
        })

    def test_same_nickname_with_several_uids_is_not_bound(self):
        account = {"username": "demo", "targets": ["Bob"], "friend_index": {"bob": {"stableKeys": [f"peer:{UID_OTHER}"]}}}

        result = friend_identity.reconcile_targets(
            account,
            entries((UID_BOB, "Bob", ""), (UID_OTHER, "bob", "")),
            scan_complete=True,
            scanned_names=["Bob"],
            now=NOW,
        )

        target = result["targets"][0]
        self.assertEqual("unbound", target["status"])
        self.assertEqual("ambiguous_nickname", target["reason"])
        self.assertNotIn("Bob", account.get("target_uid_bindings") or {})
        self.assertEqual({"Bob": "ambiguous_nickname"}, account["friend_uid_meta"]["unbound"])
        # The ambiguous name cannot identify one friend, so it must not resolve
        # the target to a uid through the friend index either.
        self.assertTrue(
            streak_state.resolve_target_ref(account, "Bob").startswith("nickname:")
        )

    def test_bound_target_is_never_rebound_automatically(self):
        account = bound_account(name="Alice", uid=UID_MATCHA)

        result = friend_identity.reconcile_targets(
            account,
            entries((UID_OTHER, "Alice", ""), (UID_BOB, "Bob", "")),
            scan_complete=True,
            scanned_names=["Alice", "Bob"],
            now=NOW,
        )

        self.assertEqual(UID_MATCHA, account["target_uid_bindings"]["Alice"]["uid"])
        self.assertEqual(f"peer:{UID_MATCHA}", streak_state.resolve_target_ref(account, "Alice"))
        self.assertEqual("missing", result["targets"][0]["status"])
        self.assertEqual(["Alice"], account["targets"])

    def test_rename_follow_carries_the_whole_ledger(self):
        today = NOW.isoformat(timespec="seconds")
        earlier = (NOW - timedelta(hours=1)).isoformat(timespec="seconds")
        account = bound_account(
            targets=["抹茶叔叔", "Bob"],
            target_refs={"抹茶叔叔": f"peer:{UID_MATCHA}"},
            target_states={
                f"peer:{UID_MATCHA}": {
                    "targetRef": f"peer:{UID_MATCHA}",
                    "displayName": "抹茶叔叔",
                    "status": "send_confirmed",
                    "sentAt": today,
                    "confirmedAt": today,
                    "attemptCount": 2,
                }
            },
            message_history={
                "抹茶叔叔": {
                    "message": "早",
                    "sentAt": today,
                    "status": "confirmed",
                    "confirmationLevel": "strong",
                }
            },
            failure_queue={
                "抹茶叔叔": {
                    "category": "browser_timeout",
                    "lastAttemptAt": earlier,
                    "attemptCount": 2,
                }
            },
            account_failure={"category": "x", "affectedTargets": ["抹茶叔叔"]},
            friends_cache=["抹茶叔叔", "Bob"],
        )
        account["target_uid_bindings"]["抹茶叔叔"]["uid"] = UID_MATCHA
        state_count = len(account["target_states"])

        result = friend_identity.reconcile_targets(
            account,
            entries((UID_MATCHA, "抹茶", ""), (UID_BOB, "Bob", "")),
            scan_complete=True,
            scanned_names=["抹茶", "Bob"],
            now=NOW,
        )

        self.assertEqual(
            [{"oldName": "抹茶叔叔", "newName": "抹茶", "uidSuffix": UID_MATCHA[-4:]}],
            result["renamed"],
        )
        self.assertEqual(["抹茶", "Bob"], account["targets"])
        self.assertIn("抹茶", account["target_uid_bindings"])
        self.assertNotIn("抹茶叔叔", account["target_uid_bindings"])
        self.assertEqual(f"peer:{UID_MATCHA}", account["target_refs"]["抹茶"])
        self.assertTrue(streak_state.is_send_confirmed(account, "抹茶", NOW))
        self.assertEqual("早", streak_state.history_entry(account, "抹茶", NOW.tzinfo)["message"])
        self.assertEqual(
            2,
            streak_state.failure_entry(account, "抹茶", NOW.tzinfo)["attemptCount"],
        )
        # The ledger rows now live under the stable identity, not the old name.
        self.assertNotIn("抹茶叔叔", account["message_history"])
        self.assertNotIn("抹茶叔叔", account["failure_queue"])
        self.assertIn(f"peer:{UID_MATCHA}", account["message_history"])
        self.assertEqual(["抹茶"], account["account_failure"]["affectedTargets"])
        self.assertEqual(["抹茶", "Bob"], account["friends_cache"])
        # No new unsent target appears and today's sent target is not resent.
        self.assertTrue(tasks._target_sent_today(account, "抹茶", NOW))
        self.assertNotIn("抹茶", tasks._pending_unsent_targets(account, NOW)[0])
        streak_state.reconcile_account(account, NOW)
        matcha_states = [
            ref
            for ref, state in account["target_states"].items()
            if state.get("displayName") in {"抹茶", "抹茶叔叔"}
        ]
        self.assertEqual(state_count, len(matcha_states))
        self.assertNotIn("nickname:抹茶", account["target_states"])
        self.assertEqual(
            "抹茶",
            account["target_states"][f"peer:{UID_MATCHA}"]["displayName"],
        )
        event = account["identity_events"][0]
        self.assertEqual("renamed", event["category"])
        self.assertEqual(("抹茶叔叔", "抹茶"), (event["oldName"], event["newName"]))
        self.assertEqual(UID_MATCHA[-4:], event["uidSuffix"])
        self.assertEqual("2026-10-14", event["date"])

    def test_bound_target_migrates_legacy_nickname_ledger(self):
        account = bound_account(
            target_refs={"抹茶叔叔": "nickname:抹茶叔叔"},
            target_states={
                "nickname:抹茶叔叔": {
                    "targetRef": "nickname:抹茶叔叔",
                    "displayName": "抹茶叔叔",
                    "status": "send_confirmed",
                    "sentAt": NOW.isoformat(timespec="seconds"),
                    "confirmedAt": NOW.isoformat(timespec="seconds"),
                }
            },
        )

        ref = streak_state.resolve_target_ref(account, "抹茶叔叔")

        self.assertEqual(f"peer:{UID_MATCHA}", ref)
        self.assertNotIn("nickname:抹茶叔叔", account["target_states"])
        self.assertTrue(streak_state.is_send_confirmed(account, "抹茶叔叔", NOW))

    def test_missing_requires_complete_and_trusted_uid_data(self):
        def run(scan_complete, scanned, uid_entries):
            account = bound_account(name="Alice", uid=UID_MATCHA)
            account["targets"].append("Ghost")
            result = friend_identity.reconcile_targets(
                account,
                uid_entries,
                scan_complete=scan_complete,
                scanned_names=scanned,
                now=NOW,
            )
            return account, {item["target"]: item for item in result["targets"]}

        # Complete scan with trusted uid data: judged by uid.
        account, outcome = run(True, ["Bob"], entries((UID_BOB, "Bob", "")))
        self.assertEqual("missing", outcome["Alice"]["status"])
        self.assertFalse(outcome["Alice"]["degraded"])
        self.assertEqual("missing", outcome["Ghost"]["status"])
        self.assertEqual(
            {"Alice", "Ghost"},
            {event["targetName"] for event in account["identity_events"]},
        )

        # Incomplete scan: nothing is missing, nothing renamed, no events.
        account, outcome = run(False, ["Bob"], entries((UID_MATCHA, "Alicia", "")))
        self.assertEqual("unchecked", outcome["Alice"]["status"])
        self.assertEqual("unchecked", outcome["Ghost"]["status"])
        self.assertEqual(["Alice", "Ghost"], account["targets"])
        self.assertFalse(account.get("identity_events"))

        # Complete scan, but a scanned friend has no uid: degrade to nicknames.
        account, outcome = run(True, ["Alice", "Bob"], entries((UID_BOB, "Bob", "")))
        self.assertEqual("found", outcome["Alice"]["status"])
        self.assertTrue(outcome["Alice"]["degraded"])
        self.assertIn("uid_data_mismatch", outcome["Alice"]["reason"])

        account, outcome = run(True, ["Carol", "Bob"], entries((UID_BOB, "Bob", "")))
        self.assertEqual("missing", outcome["Alice"]["status"])
        self.assertTrue(outcome["Alice"]["degraded"])
        self.assertFalse(account["friend_uid_meta"]["uidDataTrusted"])

        # No uid data at all: still degrade, never guess a uid.
        account, outcome = run(True, ["Alice"], [])
        self.assertEqual("found", outcome["Alice"]["status"])
        self.assertTrue(outcome["Alice"]["degraded"])
        self.assertEqual(UID_MATCHA, account["target_uid_bindings"]["Alice"]["uid"])

    def test_identity_events_are_recorded_once_per_day(self):
        account = bound_account(name="Alice", uid=UID_MATCHA)
        uid_data = entries((UID_BOB, "Bob", ""))
        for _ in range(2):
            friend_identity.reconcile_targets(
                account, uid_data, scan_complete=True, scanned_names=["Bob"], now=NOW
            )
        self.assertEqual(1, len(account["identity_events"]))

        friend_identity.reconcile_targets(
            account,
            uid_data,
            scan_complete=True,
            scanned_names=["Bob"],
            now=NOW + timedelta(days=1),
        )
        self.assertEqual(
            ["2026-10-14", "2026-10-15"],
            [event["date"] for event in account["identity_events"]],
        )
        # Old events are pruned so the list stays bounded.
        friend_identity.reconcile_targets(
            account,
            uid_data,
            scan_complete=True,
            scanned_names=["Bob"],
            now=NOW + timedelta(days=40),
        )
        self.assertEqual(1, len(account["identity_events"]))

    def test_rename_follow_is_limited_per_day_when_requested(self):
        account = bound_account(name="A", uid=UID_MATCHA)
        friend_identity.reconcile_targets(
            account,
            entries((UID_MATCHA, "B", "")),
            scan_complete=False,
            scanned_names=[],
            now=NOW,
            allow_partial_rename=True,
            max_follow_per_day=1,
        )
        result = friend_identity.reconcile_targets(
            account,
            entries((UID_MATCHA, "C", "")),
            scan_complete=False,
            scanned_names=[],
            now=NOW,
            allow_partial_rename=True,
            max_follow_per_day=1,
        )
        self.assertEqual(["B"], account["targets"])
        self.assertEqual("rename_follow_limit_reached", result["targets"][0]["reason"])

    def test_incomplete_scan_preserves_persisted_complete_index_on_rename_response(self):
        account = bound_account(name="Old", uid=UID_MATCHA)
        index = {"old": {"visibleName": "Old", "normalizedName": "old",
                         "stableKeys": [f"peer:{UID_MATCHA}", "sec:old-sec"],
                         "peerUserId": UID_MATCHA, "secUid": "old-sec"}}
        account["friend_index"] = index
        account["friends_cache"] = ["Old"]
        account["friend_index_meta"] = {"lastScanComplete": True, "scannedCount": 1}
        snapshot = json.loads(json.dumps(index))
        path = isolate_account_data(self, [account])
        result = friend_identity.persist_identity_scan(
            dict(account), entries((UID_MATCHA, "New", "new-sec")),
            scan_complete=False, scanned_names=["New"], now=NOW,
        )
        saved = stored_accounts(path)[0]
        self.assertEqual(snapshot, saved["friend_index"])
        self.assertEqual(["Old"], saved["friends_cache"])
        self.assertEqual(["Old"], saved["targets"])
        self.assertEqual({"lastScanComplete": True, "scannedCount": 1}, saved["friend_index_meta"])
        self.assertEqual("New", saved["friend_uids"][UID_MATCHA]["nickname"])
        self.assertEqual("rename_deferred_scan_incomplete", result["targets"][0]["reason"])
        self.assertEqual([], result["events"])

    def test_rename_is_deferred_without_complete_scan_by_default(self):
        account = bound_account(name="A", uid=UID_MATCHA)
        result = friend_identity.reconcile_targets(
            account,
            entries((UID_MATCHA, "B", "")),
            scan_complete=False,
            scanned_names=["B"],
            now=NOW,
        )
        self.assertEqual(["A"], account["targets"])
        self.assertEqual("rename_deferred_scan_incomplete", result["targets"][0]["reason"])

    def test_rename_is_blocked_when_new_nickname_is_ambiguous_or_old_name_visible(self):
        account = bound_account(name="甲", uid=UID_MATCHA)
        ambiguous = friend_identity.reconcile_targets(
            account,
            entries((UID_MATCHA, "小明", ""), (UID_BOB, "小明", "")),
            scan_complete=False,
            scanned_names=["小明"],
            now=NOW,
            allow_partial_rename=True,
            max_follow_per_day=1,
        )
        self.assertEqual(["甲"], account["targets"])
        self.assertEqual("unchecked", ambiguous["targets"][0]["status"])
        self.assertEqual("rename_ambiguous_nickname", ambiguous["targets"][0]["reason"])
        self.assertEqual("rename_ambiguous", ambiguous["events"][0]["category"])

        old_name_visible = friend_identity.reconcile_targets(
            account,
            entries((UID_MATCHA, "小明", ""), (UID_BOB, "乙", "")),
            scan_complete=False,
            scanned_names=["甲", "小明"],
            now=NOW + timedelta(minutes=1),
            allow_partial_rename=True,
        )
        self.assertEqual(["甲"], account["targets"])
        self.assertEqual("rename_old_name_still_visible", old_name_visible["targets"][0]["reason"])

    def test_automatic_binding_requires_trusted_complete_scan(self):
        account = {"username": "demo", "targets": ["Bob"]}
        partial = friend_identity.reconcile_targets(
            account,
            entries((UID_BOB, "Bob", "")),
            scan_complete=False,
            scanned_names=["Bob"],
            now=NOW,
        )
        self.assertNotIn("Bob", account.get("target_uid_bindings") or {})
        self.assertEqual("unbound", partial["targets"][0]["status"])
        self.assertEqual("scan_incomplete", partial["targets"][0]["reason"])

        account["friend_uids"] = {UID_OTHER: {"nickname": "Bob"}}
        ambiguous = friend_identity.reconcile_targets(
            account,
            entries((UID_BOB, "Bob", "")),
            scan_complete=True,
            scanned_names=["Bob"],
            now=NOW,
        )
        self.assertNotIn("Bob", account.get("target_uid_bindings") or {})
        self.assertEqual("unbound", ambiguous["targets"][0]["status"])
        self.assertEqual("ambiguous_nickname", ambiguous["targets"][0]["reason"])

        trusted = friend_identity.reconcile_targets(
            account,
            entries((UID_BOB, "Bob", "")),
            scan_complete=True,
            scanned_names=["Bob"],
            now=NOW,
        )
        # Existing uid history still contains the former Bob uid, so trusted
        # scans continue refusing to guess even if this response only has one.
        self.assertNotIn("Bob", account.get("target_uid_bindings") or {})
        self.assertEqual("unbound", trusted["targets"][0]["status"])

    def test_ambiguous_auto_binding_is_removed_but_web_binding_is_kept(self):
        account = {"username": "demo", "targets": ["Bob"]}
        friend_identity.bind_target(account, "Bob", UID_BOB, source="auto", now=NOW)
        friend_identity.merge_friend_uids(
            account,
            entries((UID_BOB, "Bob", ""), (UID_OTHER, "Bob", "")),
            NOW,
        )
        auto_result = friend_identity.reconcile_targets(
            account,
            entries((UID_BOB, "Bob", ""), (UID_OTHER, "Bob", "")),
            scan_complete=True,
            scanned_names=["Bob"],
            now=NOW,
        )
        self.assertNotIn("Bob", account["target_uid_bindings"])
        self.assertEqual("unbound", auto_result["targets"][0]["status"])
        self.assertEqual("ambiguous_nickname", auto_result["targets"][0]["reason"])

        web_account = {"username": "demo", "targets": ["Bob"]}
        friend_identity.bind_target(web_account, "Bob", UID_BOB, source="web", now=NOW)
        friend_identity.merge_friend_uids(
            web_account,
            entries((UID_BOB, "Bob", ""), (UID_OTHER, "Bob", "")),
            NOW,
        )
        friend_identity.reconcile_targets(
            web_account,
            entries((UID_BOB, "Bob", ""), (UID_OTHER, "Bob", "")),
            scan_complete=True,
            scanned_names=["Bob"],
            now=NOW,
        )
        self.assertEqual(UID_BOB, web_account["target_uid_bindings"]["Bob"]["uid"])
        self.assertEqual("web", web_account["target_uid_bindings"]["Bob"]["source"])

    def test_rename_ambiguity_event_is_supported_by_notification_formatter(self):
        from core import notifications

        account = bound_account(name="甲", uid=UID_MATCHA)
        result = friend_identity.reconcile_targets(
            account,
            entries((UID_MATCHA, "小明", ""), (UID_BOB, "小明", "")),
            scan_complete=False,
            scanned_names=["小明"],
            now=NOW,
            allow_partial_rename=True,
        )
        event_entries = notifications._identity_event_entries([account], NOW.date())
        self.assertEqual("rename_ambiguous", result["events"][0]["category"])
        self.assertEqual("rename_ambiguous", event_entries[0]["category"])
        self.assertIn("多个好友 uid", notifications._identity_line(event_entries[0]))

    def test_report_ref_uses_hash_and_never_full_uid(self):
        peer = friend_identity.redact_target_ref(f"peer:{UID_MATCHA}")
        sec = friend_identity.redact_target_ref("sec:secret-sec-uid")
        self.assertTrue(peer.startswith("peer:h-"))
        self.assertTrue(sec.startswith("sec:h-"))
        self.assertNotIn(UID_MATCHA, peer)
        self.assertEqual("nickname:alice", friend_identity.redact_target_ref("nickname:alice"))

    def test_logs_and_events_never_contain_the_full_uid(self):
        account = bound_account(name="抹茶叔叔", uid=UID_MATCHA)
        account["targets"].append("Gone")
        with self.assertLogs(friend_identity.logger, level="DEBUG") as captured:
            friend_identity.reconcile_targets(
                account,
                entries((UID_MATCHA, "抹茶", "sec-matcha")),
                scan_complete=True,
                scanned_names=["抹茶"],
                now=NOW,
            )
            friend_identity.bind_target(account, "Other", UID_BOB, source="web", now=NOW)
            friend_identity.UserDetailCollector("demo").ingest("<html>")
        text = "\n".join(captured.output)
        self.assertNotIn(UID_MATCHA, text)
        self.assertNotIn(UID_BOB, text)
        self.assertIn(UID_MATCHA[-4:], text)
        events_text = json.dumps(account["identity_events"], ensure_ascii=False)
        self.assertNotIn(UID_MATCHA, events_text)


class WebSelectionBindingTests(unittest.TestCase):
    def setUp(self):
        self.account = {
            "account_ref": "acc-1",
            "unique_id": ACCOUNT_ID,
            "username": "Tester",
            "enabled": True,
            "cookies": [{"name": "sessionid", "value": "v"}],
            "targets": ["Alice"],
            "friends_cache": ["Alice", "Bob", "Twin"],
            "friend_uids": {
                UID_MATCHA: {"nickname": "Alice", "secUid": ""},
                UID_BOB: {"nickname": "Bob", "secUid": "sec-bob"},
                "1001": {"nickname": "Twin"},
                "1002": {"nickname": "twin"},
            },
            "target_uid_bindings": {
                "Alice": {"uid": UID_OTHER, "secUid": "", "source": "web"},
            },
        }
        self.path = isolate_account_data(self, [self.account])
        self.client = TestClient(app_module.app)
        self.principal = {
            "username": "admin",
            "role": "admin",
            "account_refs": [],
            "session_id": "s",
        }

    def _save(self, targets):
        with (
            patch.object(app_module, "current_user", return_value="admin"),
            patch.object(app_module, "current_principal", return_value=self.principal),
            patch.object(app_module, "validate_csrf", return_value=True),
        ):
            return self.client.post(
                f"/accounts/{ACCOUNT_ID}/update",
                data={
                    "csrf_token": "t",
                    "username": "Tester",
                    "targets": targets,
                    "enabled": "on",
                },
                follow_redirects=False,
            )

    def test_selecting_a_friend_binds_its_uid(self):
        response = self._save(["Alice", "Bob", "Twin"])

        self.assertEqual(303, response.status_code)
        stored = stored_accounts(self.path)[0]
        bindings = stored["target_uid_bindings"]
        self.assertEqual(UID_BOB, bindings["Bob"]["uid"])
        self.assertEqual("web", bindings["Bob"]["source"])
        self.assertEqual("sec:sec-bob", stored["target_refs"]["Bob"])
        # A target that stayed selected keeps its binding.
        self.assertEqual(UID_OTHER, bindings["Alice"]["uid"])
        # Same nickname for two uids: not bound, reason recorded.
        self.assertNotIn("Twin", bindings)
        self.assertEqual("ambiguous_nickname", stored["friend_uid_meta"]["unbound"]["Twin"])

    def test_reselecting_a_target_rebinds_it(self):
        self._save([])
        self.assertNotIn("Alice", stored_accounts(self.path)[0].get("target_uid_bindings") or {})

        self._save(["Alice"])

        stored = stored_accounts(self.path)[0]
        self.assertEqual(UID_MATCHA, stored["target_uid_bindings"]["Alice"]["uid"])


class WebFriendRefreshUidTests(unittest.TestCase):
    def setUp(self):
        self.account = {
            "account_ref": "acc-1",
            "unique_id": ACCOUNT_ID,
            "username": "Tester",
            "enabled": True,
            "cookies": [{"name": "sessionid", "value": "v"}],
            "targets": ["抹茶", "Bob"],
            "friends_cache": ["抹茶", "Bob"],
            "friends_cache_updated_at": "2026-10-13T00:00:00",
            "friend_index": {
                "抹茶": {"visibleName": "抹茶", "stableKeys": [f"peer:{UID_MATCHA}"]},
            },
            "friend_uids": {UID_MATCHA: {"nickname": "抹茶", "secUid": ""}},
        }
        self.path = isolate_account_data(self, [self.account])
        self.client = TestClient(app_module.app)
        self.principal = {"username": "admin", "role": "admin", "account_refs": [], "session_id": "s"}
        self.addCleanup(lambda: app_module._friend_refresh_active.discard(ACCOUNT_ID))

    def _refresh(self, result):
        async def fake_fetch(account, on_progress=None):
            del account, on_progress
            return result

        with (
            patch.object(app_module, "current_user", return_value="admin"),
            patch.object(app_module, "current_principal", return_value=self.principal),
            patch.object(app_module, "validate_csrf", return_value=True),
            patch.object(app_module, "fetch_account_friends", side_effect=fake_fetch),
        ):
            return self.client.post(
                f"/accounts/{ACCOUNT_ID}/friends/refresh",
                data={"csrf_token": "t"},
            )

    def test_refresh_saves_uids_as_stable_keys(self):
        uid_data = friend_identity.parse_user_detail_payload(USER_DETAIL_FIXTURE)[0]
        response = self._refresh(
            friends_module.FriendScanResult(
                ["抹茶", "Bob"],
                complete=True,
                uid_entries=uid_data,
            )
        )

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(2, response.json()["identity"]["uidCount"])
        stored = stored_accounts(self.path)[0]
        self.assertEqual(["抹茶", "Bob"], stored["friends_cache"])
        self.assertEqual(
            ["sec:MS4wLjABAAAA-bob", f"peer:{UID_BOB}"],
            stored["friend_index"]["bob"]["stableKeys"],
        )
        self.assertIn(f"peer:{UID_MATCHA}", stored["friend_index"]["抹茶"]["stableKeys"])
        self.assertEqual("Bob", stored["friend_uids"][UID_BOB]["nickname"])
        self.assertEqual(UID_BOB, stored["target_uid_bindings"]["Bob"]["uid"])
        self.assertEqual(UID_MATCHA, stored["target_uid_bindings"]["抹茶"]["uid"])
        self.assertNotIn(UID_BOB, response.text)

    def test_refresh_without_uid_data_keeps_names_and_saved_uids(self):
        response = self._refresh(
            friends_module.FriendScanResult(["抹茶", "Bob"], complete=True)
        )

        self.assertEqual(200, response.status_code, response.text)
        stored = stored_accounts(self.path)[0]
        self.assertEqual(["抹茶", "Bob"], stored["friends_cache"])
        self.assertEqual(
            [f"peer:{UID_MATCHA}"],
            stored["friend_index"]["抹茶"]["stableKeys"],
        )
        self.assertEqual({UID_MATCHA}, set(stored["friend_uids"]))
        self.assertEqual({"抹茶", "bob"}, set(stored["friend_index"]))

    def test_fetch_account_friends_carries_uid_entries(self):
        page = _FakePage()

        class Context:
            def set_default_navigation_timeout(self, value):
                pass

            def set_default_timeout(self, value):
                pass

            async def add_cookies(self, cookies):
                pass

            async def new_page(self):
                return page

            async def close(self):
                pass

        class Browser:
            async def new_context(self):
                return Context()

            async def close(self):
                pass

        class Playwright:
            async def stop(self):
                pass

        async def goto(*args, **kwargs):
            return None

        page.goto = goto

        async def close():
            return None

        page.close = close

        async def fake_collect(target_page, on_progress=None):
            target_page.emit(
                "response",
                _FakeResponse(USER_DETAIL_URL, json.dumps(USER_DETAIL_FIXTURE)),
            )
            return friends_module.FriendScanResult(["抹茶", "Bob"], complete=True)

        with (
            patch.object(
                friends_module,
                "normalize_persistent_profile_config",
                return_value={"enabled": False, "root": ""},
            ),
            patch.object(friends_module, "get_config", return_value={}),
            patch.object(
                friends_module,
                "get_browser",
                new=AsyncMock(return_value=(Playwright(), Browser())),
            ),
            patch.object(friends_module, "douyin_network_modes", return_value=["direct"]),
            patch.object(friends_module, "collect_friend_names", side_effect=fake_collect),
            patch.object(friends_module.asyncio, "sleep", new=AsyncMock()),
        ):
            result = asyncio.run(
                friends_module.fetch_account_friends(
                    {"username": "demo", "cookies": [{"name": "sessionid", "value": "v"}]}
                )
            )

        self.assertEqual(["抹茶", "Bob"], list(result))
        self.assertTrue(result.complete)
        self.assertEqual({UID_MATCHA, UID_BOB}, {item["uid"] for item in result.uid_entries})
        self.assertEqual([], page.handlers["response"])


class _FakeCollector:
    def __init__(self, uid_entries=None):
        self._entries = list(uid_entries or [])
        self.version = 1 if self._entries else 0

    def entries(self):
        return [dict(item) for item in self._entries]

    async def drain(self, timeout=0):
        return None


class SendScanIdentityTests(unittest.TestCase):
    def _scan(self, user, targets, visible_names, collector, *, end_marker=False):
        class Element:
            def __init__(self, name):
                self.name = name

            async def click(self):
                return None

        elements = [Element(name) for name in visible_names]

        class Locator:
            async def all(self):
                return list(elements)

        async def extract_record(element):
            return {
                "visibleName": element.name,
                "normalizedName": tasks._normalize_target_name(element.name),
                "stableKeys": ["title:" + element.name],
            }

        async def selector_visible(page, selectors):
            del page
            return end_marker and "没有更多" in " ".join(selectors)

        async def run():
            selected = []
            async for name in tasks.scroll_and_select_user(
                Mock(evaluate=AsyncMock()),
                user,
                "demo",
                targets,
                {"maxScanSeconds": 60, "idleScanSeconds": 10, "scrollStepPx": 100, "scrollDelaySeconds": 0},
                uid_collector=collector,
            ):
                selected.append(name)
            return selected

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
            patch.object(tasks.asyncio, "sleep", new=AsyncMock()),
        ):
            return asyncio.run(run())

    def test_renamed_target_is_selected_under_its_new_name_in_the_same_run(self):
        account = bound_account(
            targets=["抹茶叔叔", "Bob"],
            message_history={
                "抹茶叔叔": {
                    "message": "昨天",
                    "sentAt": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(
                        timespec="seconds"
                    ),
                    "status": "sent_unverified",
                }
            },
        )
        path = isolate_account_data(self, [account])
        user = json.loads(json.dumps(account, ensure_ascii=False))
        targets = ["抹茶叔叔", "Bob"]

        selected = self._scan(
            user,
            targets,
            ["抹茶", "Bob"],
            _FakeCollector(entries((UID_MATCHA, "抹茶", ""), (UID_BOB, "Bob", ""))),
        )

        self.assertEqual(["抹茶", "Bob"], selected)
        self.assertEqual(["抹茶", "Bob"], targets)
        self.assertEqual(["抹茶", "Bob"], user["targets"])
        stored = stored_accounts(path)[0]
        self.assertEqual(["抹茶", "Bob"], stored["targets"])
        self.assertEqual(f"peer:{UID_MATCHA}", stored["target_refs"]["抹茶"])
        self.assertEqual(f"peer:{UID_MATCHA}", streak_state.resolve_target_ref(user, "抹茶"))
        self.assertEqual(
            "昨天",
            streak_state.history_entry(stored, "抹茶")["message"],
        )
        self.assertNotIn("抹茶叔叔", stored["message_history"])
        self.assertEqual(
            [("renamed", "抹茶叔叔", "抹茶")],
            [
                (event["category"], event["oldName"], event["newName"])
                for event in stored["identity_events"]
            ],
        )
        # The send scan also writes the uid stable keys into the friend index,
        # next to the DOM keys.
        self.assertEqual(
            [f"peer:{UID_MATCHA}", "title:抹茶"],
            stored["friend_index"]["抹茶"]["stableKeys"],
        )
        # A partial sender scan cannot establish uniqueness, so Bob is not
        # auto-bound until a complete, trusted refresh or patrol.
        self.assertNotIn("Bob", stored.get("target_uid_bindings") or {})

    def test_scan_without_uid_data_keeps_saved_uids_and_names(self):
        account = bound_account(
            targets=["抹茶"],
            friend_index={
                "抹茶": {"visibleName": "抹茶", "stableKeys": [f"peer:{UID_MATCHA}"]},
            },
            friend_uids={UID_MATCHA: {"nickname": "抹茶"}},
        )
        account["target_uid_bindings"] = {"抹茶": {"uid": UID_MATCHA, "source": "web"}}
        path = isolate_account_data(self, [account])
        user = json.loads(json.dumps(account, ensure_ascii=False))

        selected = self._scan(user, ["抹茶"], ["抹茶", "Bob"], _FakeCollector())

        self.assertEqual(["抹茶"], selected)
        stored = stored_accounts(path)[0]
        self.assertEqual(
            [f"peer:{UID_MATCHA}", "title:抹茶"],
            stored["friend_index"]["抹茶"]["stableKeys"],
        )
        self.assertEqual({UID_MATCHA}, set(stored["friend_uids"]))
        self.assertEqual(["抹茶"], stored["targets"])
        self.assertEqual(UID_MATCHA, stored["target_uid_bindings"]["抹茶"]["uid"])

    def test_scan_reaching_the_end_records_missing_bound_target(self):
        account = bound_account(targets=["抹茶叔叔"])
        path = isolate_account_data(self, [account])
        user = json.loads(json.dumps(account, ensure_ascii=False))

        selected = self._scan(
            user,
            ["抹茶叔叔"],
            ["Bob"],
            _FakeCollector(entries((UID_BOB, "Bob", ""))),
            end_marker=True,
        )

        self.assertEqual([], selected)
        stored = stored_accounts(path)[0]
        self.assertEqual(["抹茶叔叔"], stored["targets"])
        self.assertEqual(
            [("missing", "抹茶叔叔")],
            [(event["category"], event["targetName"]) for event in stored["identity_events"]],
        )


if __name__ == "__main__":
    unittest.main()
