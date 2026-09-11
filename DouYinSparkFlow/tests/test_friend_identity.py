import unittest
from datetime import datetime, timezone

from core import friends, tasks
from core.friends import FriendIdentityCollector, build_friend_records, normalize_friend_name
from webui import ops


# Shape copied from a real /aweme/v1/creator/im/user_detail/ reply; every value is
# invented, so the fixture can live in a public repository.
USER_DETAIL_REPLY = {
    "status_code": 0,
    "status_msg": "",
    "user_list": [
        {
            "user_id": "sec-uid-aaaa",
            "user": {
                "SecretUseId": "sec-uid-aaaa",
                "ShareQrcodeUri": "share-code-aaaa",
                "ShortId": "10000001",
                "nickname": "示例好友甲",
                "signature": "",
            },
        },
        {
            "user_id": "sec-uid-bbbb",
            "user": {
                "SecretUseId": "sec-uid-bbbb",
                "ShortId": "10000002",
                "nickname": "全角（括号）好友",
                "signature": "签名示例",
            },
        },
    ],
}


class FriendIdentityCollectorTests(unittest.TestCase):
    def test_short_id_is_read_next_to_the_current_nickname(self):
        collector = FriendIdentityCollector()
        collector.absorb(USER_DETAIL_REPLY)

        self.assertEqual(
            collector.id_to_name,
            {"10000001": "示例好友甲", "10000002": "全角（括号）好友"},
        )
        self.assertEqual(collector.name_to_id["示例好友甲"], "10000001")

    def test_entries_without_an_id_or_a_nickname_are_ignored(self):
        collector = FriendIdentityCollector()
        collector.absorb(
            {
                "user_list": [
                    {"user_id": "x", "user": {"nickname": "无抖音号"}},
                    {"user_id": "y", "user": {"ShortId": "111"}},
                    {"user_id": "z", "user": {"ShortId": "222", "nickname": "   "}},
                    "not-a-dict",
                ]
            }
        )
        self.assertEqual(collector.id_to_name, {})

    def test_a_nickname_beside_user_id_is_also_accepted(self):
        collector = FriendIdentityCollector()
        collector.absorb(
            {"user_list": [{"user_id": "x", "user": {"ShortId": "999"}, "nickname": "另一种摆放"}]}
        )
        self.assertEqual(collector.id_to_name, {"999": "另一种摆放"})

    def test_unreadable_payloads_are_ignored(self):
        collector = FriendIdentityCollector()
        collector.absorb(None)
        collector.absorb([])
        self.assertEqual(collector.records(), [])

    def test_a_rename_changes_only_the_nickname(self):
        original = FriendIdentityCollector()
        original.absorb(USER_DETAIL_REPLY)
        renamed = FriendIdentityCollector()
        renamed.absorb(
            {
                "user_list": [
                    {
                        "user_id": "sec-uid-aaaa",
                        "user": {"ShortId": "10000001", "nickname": "改了名字"},
                    }
                ]
            }
        )
        self.assertIn("10000001", original.id_to_name)
        self.assertIn("10000001", renamed.id_to_name)
        self.assertEqual(original.id_to_name["10000001"], "示例好友甲")
        self.assertEqual(renamed.id_to_name["10000001"], "改了名字")


class NameNormalizationTests(unittest.TestCase):
    def test_zero_width_characters_and_full_width_spaces_are_folded(self):
        self.assertEqual(normalize_friend_name("零宽\u200b好友"), "零宽好友")
        self.assertEqual(normalize_friend_name("ＡＢ"), "AB")
        self.assertEqual(normalize_friend_name("  a  b "), "a b")

    def test_tasks_uses_the_same_normalization_as_the_refresh(self):
        self.assertEqual(
            tasks._normalize_target_name("零宽\u200b好友"),
            friends.normalize_friend_name("零宽好友"),
        )


class BuildFriendRecordsTests(unittest.TestCase):
    def test_records_prefer_the_api_and_keep_scraped_names_as_a_fallback(self):
        collector = FriendIdentityCollector()
        collector.absorb(USER_DETAIL_REPLY)

        records = build_friend_records(["示例好友甲", "只在DOM里的好友"], collector)

        self.assertEqual(
            records,
            [
                {"id": "10000001", "name": "示例好友甲"},
                {"id": "10000002", "name": "全角（括号）好友"},
                {"id": "", "name": "只在DOM里的好友"},
            ],
        )

    def test_a_friend_the_api_did_not_answer_for_still_gets_one_row(self):
        records = build_friend_records(["同一个人", "同一个人"], None)
        self.assertEqual(records, [{"id": "", "name": "同一个人"}])


class TargetResolutionTests(unittest.TestCase):
    """The point of the change: a rename must not orphan a stored target."""

    def test_a_stored_douyin_id_resolves_to_the_current_nickname(self):
        mapping = tasks._build_normalized_target_map(["10000001"], {"10000001": "示例好友甲"})
        self.assertEqual(mapping, {"示例好友甲": "10000001"})

    def test_the_send_state_key_stays_the_stored_douyin_id(self):
        mapping = tasks._build_normalized_target_map(["10000001"], {"10000001": "改了名字"})
        self.assertEqual(list(mapping.values()), ["10000001"])

    def test_the_same_id_resolves_to_the_new_name_after_a_rename(self):
        before = tasks._build_normalized_target_map(["10000001"], {"10000001": "示例好友甲"})
        after = tasks._build_normalized_target_map(["10000001"], {"10000001": "改了名字"})
        self.assertIn("示例好友甲", before)
        self.assertIn("改了名字", after)
        self.assertEqual(before["示例好友甲"], after["改了名字"])

    def test_an_unresolved_id_falls_back_to_name_matching(self):
        mapping = tasks._build_normalized_target_map(["10000001"], {})
        self.assertEqual(mapping, {"10000001": "10000001"})

    def test_a_legacy_name_target_is_untouched(self):
        mapping = tasks._build_normalized_target_map(["旧名字"], {"10000001": "示例好友甲"})
        self.assertEqual(mapping, {"旧名字": "旧名字"})


class ConsoleTargetLabelTests(unittest.TestCase):
    """The console shows the nickname; the stored target stays the 抖音号."""

    def setUp(self):
        self.now = datetime(2026, 7, 10, 14, 0, tzinfo=timezone.utc)

    def test_an_id_target_shows_the_name_the_index_learned(self):
        account = {
            "friend_index": {
                "示例好友甲": {
                    "visibleName": "示例好友甲",
                    "normalizedName": "示例好友甲",
                    "stableKeys": ["douyin_id:10000001"],
                }
            }
        }
        status = ops._base_target_status(account, "10000001", self.now)
        self.assertEqual(status["target"], "10000001")
        self.assertEqual(status["targetLabel"], "示例好友甲")

    def test_the_last_friend_refresh_is_used_before_any_scan(self):
        account = {"friends_cache": [{"id": "10000001", "name": "示例好友甲"}]}
        status = ops._base_target_status(account, "10000001", self.now)
        self.assertEqual(status["targetLabel"], "示例好友甲")

    def test_an_unknown_target_still_shows_something(self):
        status = ops._base_target_status({}, "10000001", self.now)
        self.assertEqual(status["targetLabel"], "10000001")

    def test_a_legacy_name_target_keeps_showing_its_name(self):
        account = {"friend_index": {"旧名字": {"visibleName": "旧名字", "stableKeys": []}}}
        status = ops._base_target_status(account, "旧名字", self.now)
        self.assertEqual(status["targetLabel"], "旧名字")

    def test_an_untouched_cache_entry_does_not_break_the_label(self):
        account = {"friends_cache": ["旧版缓存里的名字"]}
        status = ops._base_target_status(account, "10000001", self.now)
        self.assertEqual(status["targetLabel"], "10000001")


if __name__ == "__main__":
    unittest.main()
