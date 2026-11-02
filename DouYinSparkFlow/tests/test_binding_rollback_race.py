"""A failed ownership claim must preserve health written before the login save."""

from copy import deepcopy
import unittest

from core import identity
from tests import test_shared_security as fixtures
from utils import config
from utils.secure_storage import StorageError
from webui import app as app_module
from webui.account_binding import BindingCommitError, commit_login_binding


class BindingRollbackRaceTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.SharedConsoleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture

    def test_health_updated_after_request_snapshot_survives_failed_claim_rollback(self):
        trusted = {"uid": "333", "source": identity.IDENTITY_SOURCE, "version": 1}
        original = {
            "account_ref": "unassigned",
            "unique_id": "333",
            "username": "Original account",
            "cookies": [{"name": "sessionid", "value": "old-private-cookie"}],
            "targets": ["friend"],
            "enabled": True,
            "login_generation": "original-generation",
            "account_health": {"healthy": False, "category": "protocol_rate_limited", "reason": "older-health"},
            "account_failure": {"category": "protocol_rate_limited", "attempts": 1},
        }
        identity.bind_identity(original, trusted, identity.BINDING_NEW)
        config.save_userData(deepcopy(self.fixture.accounts) + [deepcopy(original)])
        request_snapshot = {
            row["account_ref"]: identity.record_fingerprint(row)
            for row in config.get_userData(force_reload=True)
        }
        newer_health = {"healthy": False, "category": "protocol_rate_limited", "reason": "newer-background-health"}
        newer_failure = {"category": "protocol_rate_limited", "attempts": 3}
        operator = {"username": "alice", "role": "user", "session_id": "rollback-race"}

        def save_after_background_update():
            # This update arrives after the request snapshot and immediately
            # before the login mutator takes the account-data lock.
            def background(accounts):
                target = next(row for row in accounts if row["account_ref"] == "unassigned")
                target["account_health"] = deepcopy(newer_health)
                target["account_failure"] = deepcopy(newer_failure)
            config.update_user_data(background, force_reload=True)
            current = next(row for row in config.get_userData(force_reload=True) if row["account_ref"] == "unassigned")
            self.assertEqual(request_snapshot["unassigned"], identity.record_fingerprint(current))
            return app_module.save_exported_login_result(
                {"username": "Updated login", "cookies": [{"name": "sessionid", "value": "new-private-cookie"}]},
                relogin_account_ref="unassigned", identity=trusted,
                expected_records=request_snapshot,
                principal_resolver=lambda: operator, expected_principal=operator,
            )

        def failed_claim(_account):
            raise StorageError("synthetic ownership storage failure")

        with self.assertRaises(BindingCommitError) as caught:
            commit_login_binding(
                save_after_background_update,
                update_accounts=config.update_user_data,
                claim_callback=failed_claim,
                owner_username="alice",
            )
        self.assertFalse(caught.exception.uncertain)
        expected = deepcopy(original)
        expected.update(account_health=newer_health, account_failure=newer_failure)
        restored = config.get_userData(force_reload=True)
        self.assertEqual(expected, next(row for row in restored if row["account_ref"] == "unassigned"))
        self.assertEqual(self.fixture.accounts, [row for row in restored if row["account_ref"] != "unassigned"])
        self.assertNotIn("private-cookie", config.users_data_path().read_text())
