"""Safety properties for exact-record duplicate account merges."""

from copy import deepcopy
import unittest

from tests import test_shared_security as fixtures
from utils import config
from webui import users


class RecordMergeSafetyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SharedConsoleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.rows = [
            {
                "account_ref": "keep",
                "unique_id": "111",
                "username": "same account",
                "cookies": [],
                "targets": ["target-only"],
                "login_generation": "keep-gen",
            },
            {
                "account_ref": "source",
                "unique_id": "111",
                "username": "same account",
                "cookies": [],
                "targets": ["source-only"],
                "login_generation": "source-gen",
            },
        ]
        config.save_userData(deepcopy(self.rows))
        people = users.get_web_users()
        next(item for item in people if item["username"] == "alice")["account_refs"] = ["keep", "source"]
        next(item for item in people if item["username"] == "bob")["account_refs"] = []
        users.save_web_users(people)
        self.client = self.fixture.login()
        self.token = self.fixture.token(self.client)

    def stored(self):
        return {row["account_ref"]: row for row in config.get_userData(force_reload=True)}

    def save(self, **source):
        rows = self.stored()
        rows["source"].update(source)
        config.save_userData(list(rows.values()))

    def post_merge(self):
        return self.client.post(
            "/accounts/111/merge-into",
            data={
                "csrf_token": self.token,
                "account_ref": "source",
                "login_generation": "source-gen",
                "target_account_ref": "keep",
                "target_login_generation": "keep-gen",
            },
            follow_redirects=False,
        )

    def test_merge_preserves_source_group_binding_cache_and_removes_ownership_ref(self):
        self.save(
            targets=["source-only", "群聊：source-group"],
            group_target_bindings={"群聊：source-group": "1000000002"},
            group_target_notices={"群聊：source-group": {"kind": "renamed", "currentName": "Demo renamed group"}},
            group_cache=[{"name": "source group", "conversationId": "1000000002"}],
        )

        response = self.post_merge()

        self.assertEqual(303, response.status_code, response.text)
        kept = self.stored()["keep"]
        self.assertEqual("1000000002", kept["group_target_bindings"]["群聊：source-group"])
        self.assertEqual("renamed", kept.get("group_target_notices", {}).get("群聊：source-group", {}).get("kind"))
        self.assertEqual({"1000000002"}, {item["conversationId"] for item in kept["group_cache"]})
        self.assertEqual(["keep"], users.find_web_user("alice")["account_refs"])

    def test_merge_rejects_conflicting_group_identity_binding_without_deleting_source(self):
        rows = self.stored()
        rows["keep"]["group_target_bindings"] = {"群聊：shared": "1000000001"}
        rows["source"]["group_target_bindings"] = {"群聊：shared": "1000000002"}
        config.save_userData(list(rows.values()))

        response = self.post_merge()

        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual({"keep", "source"}, set(self.stored()))

    def test_merge_preserves_nonconflicting_refusal_retry_reservations(self):
        rows = self.stored()
        rows["keep"]["refusal_retry_reservations"] = {"keep-key": {"identityRefs": ["keep-key"], "runId": "keep"}}
        rows["source"]["refusal_retry_reservations"] = {"source-key": {"identityRefs": ["source-key", "peer:2"], "runId": "source"}}
        rows["source"]["target_states"] = {"unknown-only": {"status": "unconfirmed", "detail": "unknown barrier"}}
        config.save_userData(list(rows.values()))

        response = self.post_merge()

        self.assertEqual(303, response.status_code, response.text)
        reservations = self.stored()["keep"]["refusal_retry_reservations"]
        self.assertEqual({"keep-key", "source-key"}, set(reservations))
        self.assertEqual(["source-key", "peer:2"], reservations["source-key"]["identityRefs"])
        self.assertEqual("unconfirmed", self.stored()["keep"]["target_states"]["unknown-only"]["status"])

    def test_merge_rejects_conflicting_retry_or_unknown_safety_state(self):
        rows = self.stored()
        rows["keep"]["refusal_retry_reservations"] = {"same-key": {"identityRefs": ["same-key"], "runId": "keep"}}
        rows["source"]["refusal_retry_reservations"] = {"same-key": {"identityRefs": ["same-key"], "runId": "source"}}
        config.save_userData(list(rows.values()))
        self.assertEqual(409, self.post_merge().status_code)
        self.assertEqual({"keep", "source"}, set(self.stored()))

        rows = self.stored()
        rows["keep"].pop("refusal_retry_reservations")
        rows["source"].pop("refusal_retry_reservations")
        rows["keep"]["target_states"] = {"shared": {"status": "sent"}}
        rows["source"]["target_states"] = {"shared": {"status": "unconfirmed"}}
        config.save_userData(list(rows.values()))
        self.assertEqual(409, self.post_merge().status_code)
        self.assertEqual({"keep", "source"}, set(self.stored()))

    def test_merge_rejects_cleanup_block_without_dropping_the_barrier(self):
        self.save(browser_cleanup_blocked={"category": "browser_cleanup_blocked"})

        response = self.post_merge()

        self.assertEqual(409, response.status_code, response.text)
        self.assertIn("browser_cleanup_blocked", self.stored()["source"])


if __name__ == "__main__":
    unittest.main()
