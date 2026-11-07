"""Report metadata must read the same canonical history entry as the sender."""
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from core import tasks


class BoundReportMetadataTests(unittest.TestCase):
    def test_report_reads_sent_time_and_confirmation_from_stable_history_ref(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        account = {
            "enabled": True,
            "username": "synthetic",
            "account_ref": "account-offline",
            "targets": ["renamed friend"],
            "target_uid_bindings": {
                "renamed friend": {"uid": "10001", "secUid": "stable-target"},
            },
            "target_refs": {"renamed friend": "sec:stable-target"},
            "target_states": {
                "sec:stable-target": {
                    "targetRef": "sec:stable-target",
                    "displayName": "renamed friend",
                    "status": "send_confirmed",
                    "confirmationSource": "server",
                    "sentAt": now.isoformat(),
                    "updatedAt": now.isoformat(),
                },
            },
            "message_history": {
                "sec:stable-target": {
                    "sentAt": now.isoformat(),
                    "confirmationLevel": "strong",
                    "confirmationDetail": "offline receipt",
                },
            },
        }
        captured = []

        def capture(row, **_kwargs):
            captured.append(row)

        with patch.object(tasks.streak_state, "append_run_report", side_effect=capture), \
             patch.object(tasks.streak_state, "update_weekly_summary"), \
             patch.object(tasks.streak_state, "prune_run_reports"):
            tasks._append_streak_run_report("offline", now, {}, [account])

        self.assertEqual(1, len(captured))
        row = captured[0]
        self.assertEqual(now.isoformat(), row["sentAt"])
        self.assertEqual("strong", row["confirmationLevel"])
        self.assertEqual("offline receipt", row["confirmationDetail"])
