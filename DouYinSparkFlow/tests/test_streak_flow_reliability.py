import asyncio
import json
import os
import tempfile
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from core import protocol_dispatch, streak_state, tasks
from webui import ops as web_ops


NOW = datetime(2026, 9, 15, 11, 0, tzinfo=timezone(timedelta(hours=8)))


class StreakTargetIdentityTests(unittest.TestCase):
    def test_nickname_change_keeps_same_stable_target_ref(self):
        account = {
            "username": "demo",
            "targets": ["Alice"],
            "protocol_targets_cache": [
                {"nickname": "Alice", "secUid": "sec-1", "peerUserId": "1001"}
            ],
        }

        first_ref = streak_state.resolve_target_ref(account, "Alice")
        account["targets"] = ["Alice New"]
        account["protocol_targets_cache"] = [
            {"nickname": "Alice New", "secUid": "sec-1", "peerUserId": "1001"}
        ]
        second_ref = streak_state.resolve_target_ref(account, "Alice New")

        self.assertEqual("sec:sec-1", first_ref)
        self.assertEqual(first_ref, second_ref)

    def test_legacy_history_is_migrated_to_explicit_state(self):
        account = {
            "username": "demo",
            "targets": ["Alice"],
            "message_history": {
                "Alice": {
                    "message": "hello",
                    "sentAt": NOW.isoformat(timespec="seconds"),
                    "status": "confirmed",
                    "confirmationLevel": "strong",
                }
            },
        }

        streak_state.reconcile_account(account, NOW)

        state = streak_state.target_state(account, "Alice", NOW)
        self.assertEqual("send_confirmed", state["status"])
        self.assertEqual("nickname:alice", state["targetRef"])

    def test_renamed_target_migrates_old_nickname_history(self):
        account = {
            "username": "demo",
            "targets": ["Alice New"],
            "target_refs": {"Alice": "sec:sec-1", "Alice New": "sec:sec-1"},
            "message_history": {
                "Alice": {
                    "message": "hello",
                    "sentAt": NOW.isoformat(timespec="seconds"),
                    "status": "confirmed",
                    "confirmationLevel": "strong",
                }
            },
        }

        streak_state.reconcile_account(account, NOW)
        state = streak_state.target_state(account, "Alice New", NOW)

        self.assertEqual("send_confirmed", state["status"])
        self.assertTrue(streak_state.is_send_confirmed(account, "Alice New", NOW))

    def test_nickname_ref_upgrades_when_stable_identity_appears(self):
        account = {
            "username": "demo",
            "targets": ["Alice"],
            "target_refs": {"Alice": "nickname:alice"},
            "target_states": {
                "nickname:alice": {
                    "targetRef": "nickname:alice",
                    "displayName": "Alice",
                    "status": "send_confirmed",
                    "sentAt": NOW.isoformat(timespec="seconds"),
                }
            },
            "protocol_targets_cache": [
                {"nickname": "Alice", "secUid": "sec-1", "peerUserId": "1001"}
            ],
        }

        ref = streak_state.resolve_target_ref(account, "Alice")

        self.assertEqual("sec:sec-1", ref)
        self.assertNotIn("nickname:alice", account["target_states"])
        self.assertEqual(
            "send_confirmed",
            account["target_states"]["sec:sec-1"]["status"],
        )

    def test_friend_index_stable_key_formats_are_supported(self):
        account = {
            "username": "demo",
            "targets": ["Alice"],
            "friend_index": {
                "alice": {"stableKeys": ["data-sec-uid:SEC1"]},
            },
        }

        self.assertEqual("sec:SEC1", streak_state.resolve_target_ref(account, "Alice"))

    def test_stale_cache_nickname_migrates_single_renamed_target(self):
        account = {
            "username": "demo",
            "targets": ["Alice New"],
            "message_history": {
                "Alice": {
                    "message": "hello",
                    "sentAt": NOW.isoformat(timespec="seconds"),
                    "status": "confirmed",
                    "confirmationLevel": "strong",
                }
            },
            "failure_queue": {
                "Alice": {
                    "category": "browser_timeout",
                    "lastAttemptAt": NOW.isoformat(timespec="seconds"),
                }
            },
            "protocol_targets_cache": [
                {
                    "nickname": "Alice",
                    "secUid": "sec-1",
                    "peerUserId": "1001",
                }
            ],
        }

        self.assertEqual(
            "sec:sec-1",
            streak_state.resolve_target_ref(account, "Alice New"),
        )
        state = streak_state.target_state(account, "Alice New", NOW)
        self.assertEqual("send_confirmed", state["status"])
        self.assertTrue(streak_state.failure_entry(account, "Alice New", NOW.tzinfo))

    def test_friend_index_mixed_case_key_resolves_stable_identity(self):
        account = {
            "username": "demo",
            "targets": ["Alice"],
            "friend_index": {
                "Alice": {
                    "secUid": "sec-1",
                    "peerUserId": "1001",
                },
            },
        }

        self.assertEqual(
            "sec:sec-1",
            streak_state.resolve_target_ref(account, "Alice"),
        )
        unicode_account = {
            "username": "demo",
            "targets": ["STRASSE"],
            "friend_index": {
                "Straße": {
                    "secUid": "sec-2",
                    "peerUserId": "1002",
                },
            },
        }

        self.assertEqual(
            "sec:sec-2",
            streak_state.resolve_target_ref(unicode_account, "STRASSE"),
        )
        self.assertTrue(
            web_ops._friend_index_status(
                unicode_account,
                "STRASSE",
            )["seen"]
        )

    def test_latest_alias_history_wins_during_migration(self):
        account = {
            "username": "demo",
            "targets": ["Alice New"],
            "target_refs": {
                "Alice": "sec:sec-1",
                "Alice Mid": "sec:sec-1",
                "Alice New": "sec:sec-1",
            },
            "message_history": {
                "Alice": {
                    "sentAt": (NOW - timedelta(days=1)).isoformat(timespec="seconds"),
                    "status": "confirmed",
                    "confirmationLevel": "strong",
                },
                "Alice Mid": {
                    "sentAt": NOW.isoformat(timespec="seconds"),
                    "status": "confirmed",
                    "confirmationLevel": "strong",
                },
            },
        }

        streak_state.reconcile_account(account, NOW)

        self.assertEqual(
            "send_confirmed",
            streak_state.target_state(account, "Alice New", NOW)["status"],
        )
        self.assertTrue(streak_state.is_send_confirmed(account, "Alice New", NOW))

    def test_older_failure_does_not_override_newer_confirmation(self):
        account = {
            "username": "demo",
            "targets": ["Alice"],
            "message_history": {
                "Alice": {
                    "sentAt": NOW.isoformat(timespec="seconds"),
                    "status": "confirmed",
                    "confirmationLevel": "strong",
                }
            },
            "failure_queue": {
                "Alice": {
                    "lastAttemptAt": (NOW - timedelta(hours=1)).isoformat(
                        timespec="seconds"
                    ),
                    "category": "browser_timeout",
                }
            },
        }

        streak_state.reconcile_account(account, NOW)

        self.assertEqual(
            "send_confirmed",
            streak_state.target_state(account, "Alice", NOW)["status"],
        )

    def test_mixed_naive_and_aware_timestamps_order_correctly(self):
        now = NOW.replace(hour=0, minute=45)
        account = {
            "username": "demo",
            "targets": ["Alice"],
            "target_refs": {
                "Alice": "sec:sec-1",
                "Alice Mid": "sec:sec-1",
            },
            "message_history": {
                "Alice": {
                    "sentAt": "2026-09-15T00:00:00",
                    "status": "sent_unverified",
                    "confirmationLevel": "weak",
                },
                "Alice Mid": {
                    "sentAt": "2026-09-15T00:30:00+08:00",
                    "status": "confirmed",
                    "confirmationLevel": "strong",
                },
            },
            "failure_queue": {
                "Alice": {
                    "lastAttemptAt": "2026-09-15T00:15:00+08:00",
                    "category": "browser_timeout",
                }
            },
        }

        streak_state.reconcile_account(account, now)

        self.assertEqual(
            "send_confirmed",
            streak_state.target_state(account, "Alice", now)["status"],
        )

    def test_renamed_target_uses_legacy_failure_queue_for_retry(self):
        account = {
            "username": "demo",
            "targets": ["Alice New"],
            "target_refs": {
                "Alice": "sec:sec-1",
                "Alice New": "sec:sec-1",
            },
            "failure_queue": {
                "Alice": {
                    "category": "browser_timeout",
                    "lastAttemptAt": NOW.isoformat(timespec="seconds"),
                    "attemptCount": 2,
                }
            },
        }

        self.assertTrue(tasks._target_failed_today(account, "Alice New", NOW))
        self.assertEqual(
            2,
            tasks._target_failure_attempts_today(account, "Alice New", NOW),
        )
        self.assertEqual(
            ["Alice New"],
            tasks._pending_failed_targets(account, NOW),
        )

    def test_renamed_target_uses_alias_account_failure_for_retry(self):
        account = {
            "username": "demo",
            "targets": ["Alice New"],
            "target_refs": {
                "Alice": "sec:sec-1",
                "Alice New": "sec:sec-1",
            },
            "account_failure": {
                "category": "browser_login_required",
                "reason": "login required",
                "lastAttemptAt": NOW.isoformat(timespec="seconds"),
                "attemptCount": 2,
                "affectedTargets": ["Alice"],
            },
        }

        self.assertEqual(
            ["Alice New"],
            tasks._pending_failed_targets(account, NOW),
        )

    def test_renamed_target_discovers_alias_from_friend_index(self):
        account = {
            "username": "demo",
            "targets": ["Alice New"],
            "friend_index": {
                "alice": {
                    "visibleName": "Alice",
                    "stableKeys": ["data-sec-uid:SEC1"],
                },
                "alice new": {
                    "visibleName": "Alice New",
                    "stableKeys": ["data-sec-uid:SEC1"],
                },
            },
            "message_history": {
                "Alice": {
                    "message": "hello",
                    "sentAt": NOW.isoformat(timespec="seconds"),
                    "status": "confirmed",
                    "confirmationLevel": "strong",
                }
            },
        }

        streak_state.reconcile_account(account, NOW)

        self.assertTrue(
            streak_state.is_send_confirmed(account, "Alice New", NOW)
        )

    def test_existing_state_merges_legacy_alias_confirmation(self):
        account = {
            "username": "demo",
            "targets": ["Alice New"],
            "friend_index": {
                "alice": {
                    "visibleName": "Alice",
                    "stableKeys": ["data-sec-uid:SEC1"],
                },
                "alice new": {
                    "visibleName": "Alice New",
                    "stableKeys": ["data-sec-uid:SEC1"],
                },
            },
            "target_states": {
                "sec:SEC1": {
                    "targetRef": "sec:SEC1",
                    "displayName": "Alice New",
                    "status": "pending",
                    "attemptCount": 0,
                }
            },
            "message_history": {
                "Alice": {
                    "message": "hello",
                    "sentAt": NOW.isoformat(timespec="seconds"),
                    "status": "confirmed",
                    "confirmationLevel": "strong",
                }
            },
        }

        streak_state.reconcile_account(account, NOW)

        self.assertTrue(
            streak_state.is_send_confirmed(account, "Alice New", NOW)
        )

    def test_history_merge_keeps_newer_manual_reset(self):
        account = {
            "username": "demo",
            "message_history": {
                "sec:sec-1": {
                    "message": "hello",
                    "sentAt": NOW.replace(hour=10).isoformat(timespec="seconds"),
                    "status": "confirmed",
                    "confirmationLevel": "strong",
                },
                "Alice New": {
                    "message": "hello",
                    "sentAt": NOW.replace(hour=10).isoformat(timespec="seconds"),
                    "status": "unconfirmed",
                    "confirmationLevel": "weak",
                    "needsVerification": True,
                    "resetAt": NOW.replace(hour=11).isoformat(timespec="seconds"),
                },
            },
        }

        streak_state._move_ref_record(
            account,
            "message_history",
            "Alice New",
            "sec:sec-1",
            "sentAt",
        )

        entry = account["message_history"]["sec:sec-1"]
        self.assertEqual("unconfirmed", entry["status"])
        self.assertIn("resetAt", entry)

    def test_existing_pending_state_merges_legacy_alias_failure(self):
        account = {
            "username": "demo",
            "targets": ["Alice New"],
            "target_refs": {
                "Alice": "sec:sec-1",
                "Alice New": "sec:sec-1",
            },
            "target_states": {
                "sec:sec-1": {
                    "targetRef": "sec:sec-1",
                    "displayName": "Alice New",
                    "status": "pending",
                    "attemptCount": 0,
                    "updatedAt": NOW.isoformat(timespec="seconds"),
                }
            },
            "failure_queue": {
                "Alice": {
                    "category": "browser_timeout",
                    "reason": "send timed out",
                    "lastAttemptAt": (NOW - timedelta(hours=1)).isoformat(
                        timespec="seconds"
                    ),
                    "attemptCount": 2,
                }
            },
        }

        streak_state.reconcile_account(account, NOW)

        state = streak_state.target_state(account, "Alice New", NOW)
        self.assertEqual("failed_retryable", state["status"])
        self.assertEqual("browser_timeout", state["lastErrorCategory"])
        self.assertEqual("send timed out", state["lastErrorReason"])
        self.assertEqual(2, state["attemptCount"])

    def test_same_day_legacy_confirmation_survives_newer_failure(self):
        account = {
            "username": "demo",
            "targets": ["Alice"],
            "target_states": {
                "nickname:alice": {
                    "targetRef": "nickname:alice",
                    "displayName": "Alice",
                    "status": "failed_retryable",
                    "lastAttemptAt": (NOW - timedelta(minutes=30)).isoformat(
                        timespec="seconds"
                    ),
                    "attemptCount": 2,
                }
            },
            "message_history": {
                "Alice": {
                    "sentAt": (NOW - timedelta(hours=1)).isoformat(
                        timespec="seconds"
                    ),
                    "status": "confirmed",
                    "confirmationLevel": "strong",
                }
            },
        }

        streak_state.reconcile_account(account, NOW)

        state = streak_state.target_state(account, "Alice", NOW)
        self.assertEqual("send_confirmed", state["status"])

    def test_same_day_legacy_confirmation_overrides_newer_failure(self):
        account = {
            "username": "demo",
            "targets": ["Alice"],
            "target_states": {
                "nickname:alice": {
                    "targetRef": "nickname:alice",
                    "displayName": "Alice",
                    "status": "failed_retryable",
                    "lastAttemptAt": (NOW + timedelta(hours=1)).isoformat(
                        timespec="seconds"
                    ),
                    "attemptCount": 2,
                }
            },
            "message_history": {
                "Alice": {
                    "sentAt": NOW.isoformat(timespec="seconds"),
                    "status": "confirmed",
                    "confirmationLevel": "strong",
                }
            },
        }

        streak_state.reconcile_account(account, NOW)

        self.assertTrue(streak_state.is_send_confirmed(account, "Alice", NOW))

    def test_same_day_failure_does_not_downgrade_sent_unverified(self):
        account = {
            "username": "demo",
            "targets": ["Alice"],
            "target_states": {
                "nickname:alice": {
                    "targetRef": "nickname:alice",
                    "displayName": "Alice",
                    "status": "sent_unverified",
                    "sentAt": NOW.isoformat(timespec="seconds"),
                }
            },
            "failure_queue": {
                "Alice": {
                    "category": "browser_timeout",
                    "lastAttemptAt": (NOW + timedelta(hours=1)).isoformat(
                        timespec="seconds"
                    ),
                }
            },
        }

        streak_state.reconcile_account(account, NOW)

        self.assertEqual(
            "sent_unverified",
            streak_state.target_state(account, "Alice", NOW)["status"],
        )

    def test_mark_failed_does_not_downgrade_sent_unverified(self):
        account = {"username": "demo", "targets": ["Alice"]}
        streak_state.mark_sent_unverified(
            account,
            "Alice",
            run_id="run-1",
            strategy="browser",
            now=NOW,
        )

        state = streak_state.mark_failed(
            account,
            "Alice",
            category="browser_timeout",
            reason="timeout",
            retryable=True,
            now=NOW + timedelta(hours=1),
        )

        self.assertEqual("sent_unverified", state["status"])

    def test_stale_failure_does_not_override_newer_terminal_failure(self):
        account = {"username": "demo", "targets": ["Alice"]}
        streak_state.mark_failed(
            account,
            "Alice",
            category="protocol_user_blocked",
            reason="blocked",
            retryable=False,
            now=NOW + timedelta(minutes=5),
        )

        state = streak_state.mark_failed(
            account,
            "Alice",
            category="protocol_network_error",
            reason="older network failure",
            retryable=True,
            now=NOW,
        )

        self.assertEqual("failed_terminal", state["status"])
        self.assertEqual(
            "protocol_user_blocked",
            state["lastErrorCategory"],
        )

    def test_stale_weak_evidence_does_not_override_newer_failure(self):
        account = {"username": "demo", "targets": ["Alice"]}
        streak_state.mark_failed(
            account,
            "Alice",
            category="protocol_network_error",
            reason="newer failure",
            retryable=True,
            now=NOW + timedelta(minutes=5),
        )

        state = streak_state.mark_sent_unverified(
            account,
            "Alice",
            run_id="old-run",
            strategy="browser",
            now=NOW,
            detail="older weak evidence",
        )

        self.assertEqual("failed_retryable", state["status"])
        self.assertEqual("protocol_network_error", state["lastErrorCategory"])

    def test_stable_key_upgrade_keeps_sent_unverified_over_newer_failure(self):
        account = {
            "username": "demo",
            "targets": ["Alice"],
            "target_refs": {"Alice": "nickname:alice"},
            "protocol_targets_cache": [
                {"nickname": "Alice", "secUid": "sec-1"}
            ],
            "target_states": {
                "nickname:alice": {
                    "targetRef": "nickname:alice",
                    "displayName": "Alice",
                    "status": "sent_unverified",
                    "sentAt": NOW.isoformat(timespec="seconds"),
                },
                "sec:sec-1": {
                    "targetRef": "sec:sec-1",
                    "displayName": "Alice",
                    "status": "failed_retryable",
                    "lastAttemptAt": (NOW + timedelta(hours=1)).isoformat(
                        timespec="seconds"
                    ),
                },
            },
        }

        target_ref = streak_state.resolve_target_ref(account, "Alice")

        self.assertEqual("sec:sec-1", target_ref)
        self.assertEqual(
            "sent_unverified",
            account["target_states"]["sec:sec-1"]["status"],
        )

    def test_stable_key_same_local_day_keeps_sent_unverified(self):
        local_tz = timezone(timedelta(hours=8))
        sent_at = datetime(2026, 9, 15, 0, 30, tzinfo=local_tz)
        failed_at = datetime(2026, 9, 15, 8, 30, tzinfo=local_tz)
        account = {
            "username": "demo",
            "targets": ["Alice"],
            "target_refs": {"Alice": "nickname:alice"},
            "protocol_targets_cache": [
                {"nickname": "Alice", "secUid": "sec-1"}
            ],
            "target_states": {
                "nickname:alice": {
                    "targetRef": "nickname:alice",
                    "displayName": "Alice",
                    "status": "sent_unverified",
                    "sentAt": sent_at.isoformat(timespec="seconds"),
                },
                "sec:sec-1": {
                    "targetRef": "sec:sec-1",
                    "displayName": "Alice",
                    "status": "failed_retryable",
                    "lastAttemptAt": failed_at.isoformat(timespec="seconds"),
                },
            },
        }

        target_ref = streak_state.resolve_target_ref(account, "Alice")

        self.assertEqual("sec:sec-1", target_ref)
        self.assertEqual(
            "sent_unverified",
            account["target_states"]["sec:sec-1"]["status"],
        )

    def test_stable_key_upgrade_preserves_fallback_metadata(self):
        account = {
            "username": "demo",
            "targets": ["Alice"],
            "target_refs": {"Alice": "nickname:alice"},
            "protocol_targets_cache": [
                {"nickname": "Alice", "secUid": "sec-1"}
            ],
            "target_states": {
                "nickname:alice": {
                    "targetRef": "nickname:alice",
                    "displayName": "Alice",
                    "status": "failed_retryable",
                    "fallbackAttempted": True,
                    "fallbackAt": NOW.isoformat(timespec="seconds"),
                },
                "sec:sec-1": {
                    "targetRef": "sec:sec-1",
                    "displayName": "Alice",
                    "status": "pending",
                },
            },
        }

        target_ref = streak_state.resolve_target_ref(account, "Alice")

        self.assertEqual("sec:sec-1", target_ref)
        state = account["target_states"]["sec:sec-1"]
        self.assertTrue(state["fallbackAttempted"])
        self.assertEqual(NOW.isoformat(timespec="seconds"), state["fallbackAt"])
        self.assertFalse(streak_state.fallback_eligible(account, "Alice", NOW))

    def test_stable_state_upgrade_preserves_newer_failure_state(self):
        account = {
            "username": "demo",
            "targets": ["Alice"],
            "target_refs": {"Alice": "nickname:alice"},
            "protocol_targets_cache": [
                {"nickname": "Alice", "secUid": "sec-1"}
            ],
            "target_states": {
                "nickname:alice": {
                    "targetRef": "nickname:alice",
                    "displayName": "Alice",
                    "status": "failed_retryable",
                    "lastAttemptAt": (NOW - timedelta(minutes=30)).isoformat(
                        timespec="seconds"
                    ),
                    "lastErrorCategory": "browser_timeout",
                    "attemptCount": 2,
                },
                "sec:sec-1": {
                    "targetRef": "sec:sec-1",
                    "displayName": "Alice",
                    "status": "pending",
                    "attemptCount": 0,
                },
            },
        }

        target_ref = streak_state.resolve_target_ref(account, "Alice")

        self.assertEqual("sec:sec-1", target_ref)
        state = account["target_states"]["sec:sec-1"]
        self.assertEqual("sec:sec-1", state["targetRef"])
        self.assertEqual("failed_retryable", state["status"])
        self.assertEqual("browser_timeout", state["lastErrorCategory"])
        self.assertEqual(2, state["attemptCount"])
        self.assertNotIn("nickname:alice", account["target_states"])

    def test_stable_key_syncs_stale_state_target_ref(self):
        account = {
            "username": "demo",
            "targets": ["Alice"],
            "target_refs": {"Alice": "sec:sec-1"},
            "target_states": {
                "sec:sec-1": {
                    "targetRef": "nickname:alice",
                    "displayName": "Alice",
                    "status": "pending",
                }
            },
        }

        target_ref = streak_state.resolve_target_ref(account, "Alice")

        self.assertEqual("sec:sec-1", target_ref)
        self.assertEqual(
            "sec:sec-1",
            account["target_states"]["sec:sec-1"]["targetRef"],
        )

    def test_stable_key_upgrade_preserves_same_day_confirmed_state(self):
        account = {
            "username": "demo",
            "targets": ["Alice"],
            "target_refs": {"Alice": "nickname:alice"},
            "protocol_targets_cache": [
                {"nickname": "Alice", "secUid": "sec-1"}
            ],
            "target_states": {
                "nickname:alice": {
                    "targetRef": "nickname:alice",
                    "displayName": "Alice",
                    "status": "failed_retryable",
                    "lastAttemptAt": (NOW + timedelta(hours=1)).isoformat(
                        timespec="seconds"
                    ),
                    "attemptCount": 2,
                },
                "sec:sec-1": {
                    "targetRef": "sec:sec-1",
                    "displayName": "Alice",
                    "status": "send_confirmed",
                    "sentAt": NOW.isoformat(timespec="seconds"),
                    "confirmedAt": NOW.isoformat(timespec="seconds"),
                },
            },
        }

        target_ref = streak_state.resolve_target_ref(account, "Alice")

        self.assertEqual("sec:sec-1", target_ref)
        self.assertEqual(
            "send_confirmed",
            account["target_states"]["sec:sec-1"]["status"],
        )

    def test_stable_identity_change_does_not_reuse_old_identity_history(self):
        account = {
            "username": "demo",
            "targets": ["Alice"],
            "target_refs": {"Alice": "sec:old"},
            "protocol_targets_cache": [
                {"nickname": "Alice", "secUid": "new"}
            ],
            "message_history": {
                "Alice": {
                    "sentAt": NOW.isoformat(timespec="seconds"),
                    "status": "confirmed",
                    "confirmationLevel": "strong",
                }
            },
        }

        target_ref = streak_state.resolve_target_ref(account, "Alice")
        streak_state.reconcile_account(account, NOW)

        self.assertEqual("sec:new", target_ref)
        self.assertFalse(streak_state.is_send_confirmed(account, "Alice", NOW))
        self.assertIn("sec:old", account["message_history"])

    def test_peer_ref_upgrade_to_sec_ref_preserves_confirmed_state(self):
        account = {
            "username": "demo",
            "targets": ["Alice"],
            "target_refs": {"Alice": "peer:1001"},
            "protocol_targets_cache": [
                {
                    "nickname": "Alice",
                    "peerUserId": "1001",
                    "secUid": "sec-1",
                }
            ],
            "target_states": {
                "peer:1001": {
                    "targetRef": "peer:1001",
                    "displayName": "Alice",
                    "status": "send_confirmed",
                    "sentAt": NOW.isoformat(timespec="seconds"),
                    "confirmedAt": NOW.isoformat(timespec="seconds"),
                }
            },
            "message_history": {
                "peer:1001": {
                    "message": "hello",
                    "sentAt": NOW.isoformat(timespec="seconds"),
                    "status": "confirmed",
                    "confirmationLevel": "strong",
                }
            },
        }

        target_ref = streak_state.resolve_target_ref(account, "Alice")

        self.assertEqual("sec:sec-1", target_ref)
        self.assertNotIn("peer:1001", account["target_states"])
        self.assertNotIn("peer:1001", account["message_history"])
        self.assertEqual(
            "send_confirmed",
            account["target_states"]["sec:sec-1"]["status"],
        )
        self.assertTrue(streak_state.is_send_confirmed(account, "Alice", NOW))

    def test_renamed_peer_ref_upgrade_preserves_active_lease_and_ledger(self):
        live_now = datetime.now(timezone.utc)
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "target_refs": {"Alice": "peer:1001"},
            "failure_queue": {
                "Alice": {
                    "category": "browser_timeout",
                    "reason": "first",
                    "firstAttemptAt": live_now.isoformat(timespec="seconds"),
                    "lastAttemptAt": live_now.isoformat(timespec="seconds"),
                    "attemptCount": 2,
                }
            },
        }
        streak_state.mark_in_flight(
            account,
            "Alice",
            run_id="crashed-run",
            strategy="browser",
            now=live_now,
            lease_seconds=900,
        )
        account["targets"] = ["Alice New"]
        account["protocol_targets_cache"] = [
            {
                "nickname": "Alice New",
                "peerUserId": "1001",
                "secUid": "sec-1",
            }
        ]

        target_ref = streak_state.resolve_target_ref(account, "Alice New")

        self.assertEqual("sec:sec-1", target_ref)
        self.assertEqual("sec:sec-1", account["target_refs"]["Alice"])
        self.assertNotIn("peer:1001", account["target_states"])
        state = streak_state.target_state(account, "Alice New", live_now)
        self.assertEqual("in_flight", state["status"])
        self.assertEqual("crashed-run", state["runId"])
        failure = streak_state.failure_entry(
            account,
            "Alice New",
            live_now.tzinfo,
        )
        self.assertEqual(2, failure["attemptCount"])
        self.assertIs(
            False,
            tasks._mark_browser_target_in_flight(
                account.copy(),
                "Alice New",
                run_id="manual-run",
            ),
        )

    def test_renamed_peer_upgrade_migrates_legacy_nickname_lease(self):
        live_now = datetime.now(timezone.utc)
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice New"],
            "target_refs": {"Alice": "peer:1001"},
            "target_states": {
                "nickname:alice": {
                    "targetRef": "nickname:alice",
                    "displayName": "Alice",
                    "status": "in_flight",
                    "runId": "crashed-run",
                    "strategy": "browser",
                    "startedAt": live_now.isoformat(timespec="seconds"),
                    "leaseExpiresAt": (
                        live_now + timedelta(minutes=15)
                    ).isoformat(timespec="seconds"),
                    "attemptCount": 1,
                }
            },
            "message_history": {
                "Alice": {
                    "message": "hello",
                    "sentAt": (
                        live_now - timedelta(hours=1)
                    ).isoformat(timespec="seconds"),
                    "status": "unconfirmed",
                    "confirmationLevel": "weak",
                }
            },
            "failure_queue": {
                "Alice": {
                    "category": "browser_timeout",
                    "reason": "first",
                    "firstAttemptAt": (
                        live_now - timedelta(hours=1)
                    ).isoformat(timespec="seconds"),
                    "lastAttemptAt": (
                        live_now - timedelta(minutes=30)
                    ).isoformat(timespec="seconds"),
                    "attemptCount": 2,
                }
            },
            "protocol_targets_cache": [
                {
                    "nickname": "Alice New",
                    "peerUserId": "1001",
                    "secUid": "sec-1",
                }
            ],
        }

        target_ref = streak_state.resolve_target_ref(account, "Alice New")

        self.assertEqual("sec:sec-1", target_ref)
        self.assertNotIn("nickname:alice", account["target_states"])
        state = streak_state.target_state(account, "Alice New", live_now)
        self.assertEqual("in_flight", state["status"])
        self.assertEqual("crashed-run", state["runId"])
        self.assertNotIn("Alice", account["message_history"])
        self.assertNotIn("Alice", account["failure_queue"])
        self.assertIn("sec:sec-1", account["message_history"])
        self.assertEqual(
            2,
            account["failure_queue"]["sec:sec-1"]["attemptCount"],
        )
        self.assertIs(
            False,
            tasks._mark_browser_target_in_flight(
                account.copy(),
                "Alice New",
                run_id="manual-run",
            ),
        )
        orphan_history, orphan_failure = web_ops._orphan_records(
            account,
            ["Alice New"],
        )
        self.assertEqual([], orphan_history)
        self.assertEqual([], orphan_failure)

    def test_split_cache_without_refs_migrates_legacy_nickname_ledger(self):
        live_now = datetime.now(timezone.utc)
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice New"],
            "target_states": {
                "nickname:alice": {
                    "targetRef": "nickname:alice",
                    "displayName": "Alice",
                    "status": "in_flight",
                    "runId": "crashed-run",
                    "strategy": "browser",
                    "startedAt": live_now.isoformat(timespec="seconds"),
                    "leaseExpiresAt": (
                        live_now + timedelta(minutes=15)
                    ).isoformat(timespec="seconds"),
                    "attemptCount": 2,
                }
            },
            "message_history": {
                "Alice": {
                    "message": "hello",
                    "sentAt": (
                        live_now - timedelta(hours=1)
                    ).isoformat(timespec="seconds"),
                    "status": "unconfirmed",
                    "confirmationLevel": "weak",
                }
            },
            "failure_queue": {
                "Alice": {
                    "category": "browser_timeout",
                    "reason": "first",
                    "firstAttemptAt": (
                        live_now - timedelta(hours=1)
                    ).isoformat(timespec="seconds"),
                    "lastAttemptAt": (
                        live_now - timedelta(minutes=30)
                    ).isoformat(timespec="seconds"),
                    "attemptCount": 2,
                }
            },
            "protocol_targets_cache": [
                {
                    "nickname": "Alice",
                    "peerUserId": "1001",
                    "secUid": "",
                },
                {
                    "nickname": "Alice New",
                    "peerUserId": "1001",
                    "secUid": "sec-1",
                },
            ],
        }

        target_ref = streak_state.resolve_target_ref(account, "Alice New")

        self.assertEqual("sec:sec-1", target_ref)
        self.assertNotIn("nickname:alice", account["target_states"])
        self.assertNotIn("Alice", account["message_history"])
        self.assertNotIn("Alice", account["failure_queue"])
        self.assertIn("sec:sec-1", account["message_history"])
        self.assertIn("sec:sec-1", account["failure_queue"])
        state = streak_state.target_state(account, "Alice New", live_now)
        self.assertEqual("in_flight", state["status"])
        self.assertEqual("crashed-run", state["runId"])
        self.assertEqual(2, state["attemptCount"])
        self.assertIs(
            False,
            tasks._mark_browser_target_in_flight(
                account.copy(),
                "Alice New",
                run_id="manual-run",
            ),
        )
        orphan_history, orphan_failure = web_ops._orphan_records(
            account,
            ["Alice New"],
        )
        self.assertEqual([], orphan_history)
        self.assertEqual([], orphan_failure)

    def test_old_nickname_only_ledger_upgrades_without_state_or_refs(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice New"],
            "message_history": {
                "Alice": {
                    "message": "hello",
                    "sentAt": NOW.isoformat(timespec="seconds"),
                    "status": "confirmed",
                    "confirmationLevel": "strong",
                }
            },
            "failure_queue": {
                "Alice": {
                    "category": "browser_timeout",
                    "reason": "first",
                    "firstAttemptAt": NOW.isoformat(timespec="seconds"),
                    "lastAttemptAt": NOW.isoformat(timespec="seconds"),
                    "attemptCount": 2,
                }
            },
            "protocol_targets_cache": [
                {
                    "nickname": "Alice",
                    "peerUserId": "1001",
                    "secUid": "",
                },
                {
                    "nickname": "Alice New",
                    "peerUserId": "1001",
                    "secUid": "sec-1",
                },
            ],
        }

        target_ref = streak_state.resolve_target_ref(account, "Alice New")
        streak_state.reconcile_account(account, NOW)

        self.assertEqual("sec:sec-1", target_ref)
        self.assertNotIn("Alice", account["message_history"])
        self.assertNotIn("Alice", account["failure_queue"])
        self.assertIn("sec:sec-1", account["message_history"])
        self.assertEqual(
            2,
            account["failure_queue"]["sec:sec-1"]["attemptCount"],
        )
        self.assertTrue(
            streak_state.is_send_confirmed(account, "Alice New", NOW)
        )
        self.assertEqual(
            2,
            streak_state.target_state(account, "Alice New", NOW)["attemptCount"],
        )
        orphan_history, orphan_failure = web_ops._orphan_records(
            account,
            ["Alice New"],
        )
        self.assertEqual([], orphan_history)
        self.assertEqual([], orphan_failure)

    def test_nickname_placeholder_does_not_block_rename_upgrade(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice New"],
            "target_refs": {"Alice": "nickname:alice"},
            "message_history": {
                "Alice": {
                    "message": "hello",
                    "sentAt": NOW.isoformat(timespec="seconds"),
                    "status": "confirmed",
                    "confirmationLevel": "strong",
                }
            },
            "failure_queue": {
                "Alice": {
                    "category": "browser_timeout",
                    "reason": "first",
                    "firstAttemptAt": NOW.isoformat(timespec="seconds"),
                    "lastAttemptAt": NOW.isoformat(timespec="seconds"),
                    "attemptCount": 2,
                }
            },
            "protocol_targets_cache": [
                {
                    "nickname": "Alice",
                    "peerUserId": "1001",
                    "secUid": "",
                },
                {
                    "nickname": "Alice New",
                    "peerUserId": "1001",
                    "secUid": "sec-1",
                },
            ],
        }

        target_ref = streak_state.resolve_target_ref(account, "Alice New")
        streak_state.reconcile_account(account, NOW)

        self.assertEqual("sec:sec-1", target_ref)
        self.assertNotIn("Alice", account["message_history"])
        self.assertNotIn("Alice", account["failure_queue"])
        self.assertTrue(
            streak_state.is_send_confirmed(account, "Alice New", NOW)
        )
        orphan_history, orphan_failure = web_ops._orphan_records(
            account,
            ["Alice New"],
        )
        self.assertEqual([], orphan_history)
        self.assertEqual([], orphan_failure)

    def test_different_stable_identity_does_not_reuse_old_state(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "target_refs": {"Alice": "peer:1001"},
            "target_states": {
                "peer:1001": {
                    "targetRef": "peer:1001",
                    "displayName": "Alice",
                    "status": "send_confirmed",
                    "sentAt": NOW.isoformat(timespec="seconds"),
                    "confirmedAt": NOW.isoformat(timespec="seconds"),
                }
            },
            "message_history": {
                "Alice": {
                    "message": "old identity",
                    "sentAt": NOW.isoformat(timespec="seconds"),
                    "status": "confirmed",
                    "confirmationLevel": "strong",
                }
            },
            "protocol_targets_cache": [
                {
                    "nickname": "Alice",
                    "peerUserId": "1002",
                    "secUid": "sec-different",
                }
            ],
        }

        target_ref = streak_state.resolve_target_ref(account, "Alice")
        streak_state.reconcile_account(account, NOW)

        self.assertEqual("sec:sec-different", target_ref)
        self.assertIn("peer:1001", account["target_states"])
        self.assertFalse(
            streak_state.is_send_confirmed(account, "Alice", NOW)
        )
        self.assertEqual(
            "pending",
            streak_state.target_state(account, "Alice", NOW)["status"],
        )


class StreakStateMachineTests(unittest.TestCase):
    def test_mark_in_flight_does_not_replace_active_lease(self):
        account = {"username": "demo", "targets": ["Alice"]}
        streak_state.mark_in_flight(
            account,
            "Alice",
            run_id="run-1",
            strategy="browser",
            now=NOW,
            lease_seconds=900,
        )

        state = streak_state.mark_in_flight(
            account,
            "Alice",
            run_id="run-2",
            strategy="protocol",
            now=NOW + timedelta(minutes=1),
            lease_seconds=900,
        )

        self.assertEqual("in_flight", state["status"])
        self.assertEqual("run-1", state["runId"])
        self.assertEqual(1, state["attemptCount"])

    def test_in_flight_lease_expires_without_becoming_confirmed(self):
        account = {"username": "demo", "targets": ["Alice"]}
        streak_state.mark_in_flight(
            account,
            "Alice",
            run_id="run-1",
            strategy="protocol",
            now=NOW,
            lease_seconds=60,
        )

        streak_state.reconcile_account(account, NOW + timedelta(seconds=61))

        state = streak_state.target_state(account, "Alice", NOW + timedelta(seconds=61))
        self.assertEqual("failed_retryable", state["status"])
        self.assertEqual("in_flight_lease_expired", state["lastErrorCategory"])
        self.assertIn("lastAttemptAt", state)
        self.assertFalse(streak_state.is_send_confirmed(account, "Alice", NOW))

    def test_expired_fallback_lease_is_eligible_after_restart(self):
        account = {"username": "demo", "targets": ["Alice"]}
        streak_state.mark_in_flight(
            account,
            "Alice",
            run_id="fallback-run",
            strategy="browser",
            now=NOW,
            lease_seconds=60,
        )
        streak_state.mark_fallback_attempted(account, "Alice", now=NOW)

        later = NOW + timedelta(seconds=61)
        streak_state.reconcile_account(account, later)
        state = streak_state.target_state(account, "Alice", later)

        self.assertEqual("failed_retryable", state["status"])
        self.assertFalse(state.get("fallbackAttempted", False))
        self.assertTrue(streak_state.fallback_eligible(account, "Alice", later))

    def test_weak_and_strong_evidence_have_distinct_states(self):
        account = {"username": "demo", "targets": ["Alice"]}

        streak_state.mark_sent_unverified(
            account,
            "Alice",
            run_id="run-1",
            strategy="browser",
            now=NOW,
            detail="browser_visible_count_increased",
        )
        self.assertEqual(
            "sent_unverified",
            streak_state.target_state(account, "Alice", NOW)["status"],
        )
        self.assertFalse(streak_state.is_send_confirmed(account, "Alice", NOW))

        streak_state.mark_send_confirmed(
            account,
            "Alice",
            run_id="run-1",
            strategy="protocol",
            now=NOW,
            detail="statusCode=0",
        )
        self.assertEqual(
            "send_confirmed",
            streak_state.target_state(account, "Alice", NOW)["status"],
        )
        self.assertTrue(streak_state.is_send_confirmed(account, "Alice", NOW))

        streak_state.mark_streak_verified(account, "Alice", now=NOW)
        self.assertTrue(streak_state.is_streak_verified(account, "Alice", NOW))

    def test_manual_unconfirmed_invalidates_explicit_confirmed_state(self):
        account = {"username": "demo", "targets": ["Alice"]}
        streak_state.mark_send_confirmed(
            account,
            "Alice",
            strategy="protocol",
            now=NOW,
        )

        streak_state.mark_unconfirmed(
            account,
            "Alice",
            reason="manual_reset_possible_false_positive",
            now=NOW,
        )

        state = streak_state.target_state(account, "Alice", NOW)
        self.assertEqual("failed_retryable", state["status"])
        self.assertEqual("send_unconfirmed", state["lastErrorCategory"])
        self.assertTrue(state["needsVerification"])
        self.assertFalse(streak_state.is_send_confirmed(account, "Alice", NOW))

    def test_newer_confirmation_overrides_older_manual_unconfirmed(self):
        account = {"username": "demo", "targets": ["Alice"]}
        streak_state.mark_unconfirmed(
            account,
            "Alice",
            reason="manual_reset_possible_false_positive",
            now=NOW.replace(hour=12),
        )
        streak_state.mark_send_confirmed(
            account,
            "Alice",
            strategy="protocol",
            now=NOW.replace(hour=12, minute=5),
        )

        state = streak_state.target_state(account, "Alice", NOW)
        self.assertEqual("send_confirmed", state["status"])
        self.assertFalse(state.get("needsVerification", False))

    def test_state_merge_prefers_newer_confirmation_over_manual_reset(self):
        manual_reset = {
            "status": "failed_retryable",
            "lastErrorCategory": "send_unconfirmed",
            "lastAttemptAt": NOW.replace(hour=12).isoformat(timespec="seconds"),
            "needsVerification": True,
        }
        confirmed = {
            "status": "send_confirmed",
            "confirmedAt": NOW.replace(hour=12, minute=5).isoformat(
                timespec="seconds"
            ),
            "sentAt": NOW.replace(hour=12, minute=5).isoformat(
                timespec="seconds"
            ),
        }

        self.assertFalse(
            streak_state._prefer_state(manual_reset, confirmed)
        )
        self.assertTrue(
            streak_state._prefer_state(confirmed, manual_reset)
        )
        self.assertTrue(
            streak_state._should_merge_legacy_state(
                manual_reset,
                confirmed,
            )
        )
        self.assertFalse(
            streak_state._should_merge_legacy_state(
                confirmed,
                manual_reset,
            )
        )

    def test_stale_state_writes_do_not_override_newer_evidence(self):
        manual_newer = {"username": "demo", "targets": ["Alice"]}
        streak_state.mark_unconfirmed(
            manual_newer,
            "Alice",
            reason="manual_reset_possible_false_positive",
            now=NOW.replace(hour=12, minute=5),
        )
        streak_state.mark_send_confirmed(
            manual_newer,
            "Alice",
            strategy="protocol",
            now=NOW.replace(hour=12),
        )
        manual_state = streak_state.target_state(
            manual_newer,
            "Alice",
            NOW,
        )

        confirmed_newer = {"username": "demo", "targets": ["Alice"]}
        streak_state.mark_send_confirmed(
            confirmed_newer,
            "Alice",
            strategy="protocol",
            now=NOW.replace(hour=12, minute=5),
        )
        streak_state.mark_unconfirmed(
            confirmed_newer,
            "Alice",
            reason="manual_reset_possible_false_positive",
            now=NOW.replace(hour=12),
        )
        confirmed_state = streak_state.target_state(
            confirmed_newer,
            "Alice",
            NOW,
        )

        self.assertEqual("failed_retryable", manual_state["status"])
        self.assertEqual(
            "send_unconfirmed",
            manual_state["lastErrorCategory"],
        )
        self.assertEqual("send_confirmed", confirmed_state["status"])

    def test_receipt_requires_message_send_call(self):
        self.assertFalse(
            streak_state.receipt_is_strong(
                {"ok": True, "httpStatus": 200, "call": "profile"}
            )
        )
        self.assertTrue(
            streak_state.receipt_is_strong(
                {
                    "ok": True,
                    "httpStatus": 200,
                    "call": "message_send",
                    "jsonOk": True,
                }
            )
        )
        self.assertFalse(
            streak_state.receipt_is_strong(
                {
                    "ok": True,
                    "httpStatus": 200,
                    "call": "message_send",
                    "jsonOk": False,
                }
            )
        )

    def test_confirmed_state_expires_on_the_next_day(self):
        account = {"username": "demo", "targets": ["Alice"]}
        streak_state.mark_send_confirmed(
            account,
            "Alice",
            strategy="protocol",
            now=NOW,
        )

        next_day = NOW + timedelta(days=1)

        self.assertTrue(streak_state.is_send_confirmed(account, "Alice", NOW))
        self.assertFalse(streak_state.is_send_confirmed(account, "Alice", next_day))

    def test_failure_does_not_downgrade_confirmed_state(self):
        account = {"username": "demo", "targets": ["Alice"]}
        streak_state.mark_send_confirmed(
            account,
            "Alice",
            strategy="protocol",
            now=NOW,
        )

        streak_state.mark_failed(
            account,
            "Alice",
            category="browser_timeout",
            reason="late browser failure",
            retryable=True,
            now=NOW,
        )

        self.assertEqual(
            "send_confirmed",
            streak_state.target_state(account, "Alice", NOW)["status"],
        )

    def test_send_confirmed_does_not_downgrade_streak_verified(self):
        account = {"username": "demo", "targets": ["Alice"]}
        streak_state.mark_streak_verified(account, "Alice", now=NOW)

        streak_state.mark_send_confirmed(
            account,
            "Alice",
            strategy="protocol",
            now=NOW,
        )

        self.assertTrue(streak_state.is_streak_verified(account, "Alice", NOW))

    def test_legacy_streak_verified_is_not_downgraded(self):
        account = {
            "username": "demo",
            "targets": ["Alice"],
            "message_history": {
                "Alice": {
                    "sentAt": NOW.isoformat(timespec="seconds"),
                    "status": "streak_verified",
                    "confirmationLevel": "verified",
                }
            },
        }
        streak_state.reconcile_account(account, NOW)

        streak_state.mark_send_confirmed(
            account,
            "Alice",
            strategy="protocol",
            now=NOW,
        )

        self.assertTrue(streak_state.is_streak_verified(account, "Alice", NOW))

    def test_newer_send_confirmation_does_not_downgrade_streak_verified(self):
        account = {
            "username": "demo",
            "targets": ["Alice"],
            "target_states": {
                "nickname:alice": {
                    "targetRef": "nickname:alice",
                    "displayName": "Alice",
                    "status": "streak_verified",
                    "verifiedAt": NOW.isoformat(timespec="seconds"),
                }
            },
            "message_history": {
                "Alice": {
                    "sentAt": (NOW + timedelta(hours=1)).isoformat(
                        timespec="seconds"
                    ),
                    "status": "confirmed",
                    "confirmationLevel": "strong",
                }
            },
        }

        streak_state.reconcile_account(account, NOW)

        self.assertTrue(streak_state.is_streak_verified(account, "Alice", NOW))
        self.assertEqual(
            "streak_verified",
            streak_state.target_state(account, "Alice", NOW)["status"],
        )

    def test_previous_day_verified_does_not_block_today_confirmation(self):
        account = {
            "username": "demo",
            "targets": ["Alice"],
            "target_states": {
                "nickname:alice": {
                    "targetRef": "nickname:alice",
                    "displayName": "Alice",
                    "status": "streak_verified",
                    "verifiedAt": (NOW - timedelta(days=1)).isoformat(
                        timespec="seconds"
                    ),
                }
            },
            "message_history": {
                "Alice": {
                    "sentAt": NOW.isoformat(timespec="seconds"),
                    "status": "confirmed",
                    "confirmationLevel": "strong",
                }
            },
        }

        streak_state.reconcile_account(account, NOW)

        self.assertTrue(streak_state.is_send_confirmed(account, "Alice", NOW))
        self.assertFalse(streak_state.is_streak_verified(account, "Alice", NOW))

    def test_latest_event_time_protects_today_streak_verified(self):
        account = {"username": "demo", "targets": ["Alice"]}
        streak_state.mark_send_confirmed(
            account,
            "Alice",
            strategy="protocol",
            now=NOW - timedelta(days=1),
        )
        streak_state.mark_streak_verified(account, "Alice", now=NOW)

        streak_state.mark_send_confirmed(
            account,
            "Alice",
            strategy="protocol",
            now=NOW,
        )
        self.assertTrue(streak_state.is_streak_verified(account, "Alice", NOW))

        state = streak_state.mark_in_flight(
            account,
            "Alice",
            run_id="run-1",
            strategy="protocol",
            now=NOW,
        )
        self.assertEqual("streak_verified", state["status"])

    def test_fallback_is_only_eligible_for_retryable_unconfirmed_failure(self):
        account = {"username": "demo", "targets": ["Alice"]}
        streak_state.mark_failed(
            account,
            "Alice",
            category="protocol_network_error",
            reason="temporary",
            retryable=True,
            now=NOW,
        )

        self.assertTrue(streak_state.fallback_eligible(account, "Alice", NOW))

        streak_state.mark_fallback_attempted(account, "Alice", now=NOW)
        self.assertFalse(streak_state.fallback_eligible(account, "Alice", NOW))
        self.assertTrue(
            streak_state.fallback_eligible(
                account,
                "Alice",
                NOW + timedelta(days=1),
            )
        )


class FriendIndexPreflightDeadlockTests(unittest.TestCase):
    """A stale friend index must never block a scheduled run from starting.

    The only writer of ``friend_index`` / ``friend_index_meta`` lives inside the
    browser send flow. Gating that flow on the freshness of its own output
    deadlocks the run forever, because no other code path can refresh the index.
    """

    def _account(self, *, meta):
        account = {
            "username": "demo",
            "unique_id": "1",
            "enabled": True,
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        if meta is not None:
            account["friend_index"] = {"alice": {"stableKeys": ["sec:sec-1"]}}
            account["friend_index_meta"] = meta
        return account

    def _scheduled_run(self, account):
        # A non-manual, non-fallback run is what the scheduler actually triggers.
        config = {"useProtocolSender": False}
        with patch.dict(
            os.environ,
            {"SPARKFLOW_MANUAL_RUN": "", "SPARKFLOW_FALLBACK_PHASE": ""},
            clear=False,
        ), patch.object(tasks, "_persist_account_preflight"):
            return tasks._prepare_active_users_for_run(config, [account])

    def test_scheduled_run_survives_yesterday_friend_index(self):
        yesterday = datetime.now(timezone(timedelta(hours=8))) - timedelta(days=1)
        account = self._account(
            meta={
                "lastScanAt": yesterday.isoformat(timespec="seconds"),
                "lastScanComplete": True,
            }
        )

        runnable = self._scheduled_run(account)

        self.assertEqual(1, len(runnable))
        self.assertEqual(["Alice"], runnable[0]["targets"])

    def test_scheduled_run_survives_missing_friend_index(self):
        account = self._account(meta=None)

        runnable = self._scheduled_run(account)

        self.assertEqual(1, len(runnable))
        self.assertEqual(["Alice"], runnable[0]["targets"])

    def test_scheduled_run_survives_incomplete_friend_index(self):
        account = self._account(
            meta={
                "lastScanAt": datetime.now(
                    timezone(timedelta(hours=8))
                ).isoformat(timespec="seconds"),
                "lastScanComplete": False,
            }
        )

        runnable = self._scheduled_run(account)

        self.assertEqual(1, len(runnable))
        self.assertEqual(["Alice"], runnable[0]["targets"])


class StreakPreflightTests(unittest.TestCase):
    def test_preflight_rejects_missing_cookies_without_consuming_target_attempts(self):
        account = {
            "username": "demo",
            "targets": ["Alice"],
            "cookies": [],
        }

        result = streak_state.preflight_account(account, NOW)
        state = streak_state.target_state(account, "Alice", NOW)

        self.assertFalse(result["healthy"])
        self.assertEqual("missing_cookies", result["category"])
        self.assertEqual(0, state["attemptCount"])

    def test_preflight_accepts_complete_friend_index(self):
        account = {
            "username": "demo",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
            "friend_index": {"alice": {"stableKeys": ["sec:sec-1"]}},
            "friend_index_meta": {
                "lastScanAt": NOW.isoformat(timespec="seconds"),
                "lastScanComplete": True,
            },
        }

        result = streak_state.preflight_account(account, NOW)

        self.assertTrue(result["healthy"])
        self.assertEqual("", result["category"])
        self.assertEqual("sec:sec-1", streak_state.resolve_target_ref(account, "Alice"))

    def test_friend_index_prefers_sec_uid_over_peer_user_id(self):
        account = {
            "username": "demo",
            "targets": ["Alice"],
            "friend_index": {
                "alice": {
                    "visibleName": "Alice",
                    "stableKeys": [
                        "data-id:1001",
                        "data-sec-uid:SEC1",
                    ],
                }
            },
        }

        self.assertEqual(
            "sec:SEC1",
            streak_state.resolve_target_ref(account, "Alice"),
        )

    def test_alias_migration_preserves_same_day_strong_confirmation(self):
        account = {
            "username": "demo",
            "targets": ["Alice New"],
            "message_history": {
                "Alice": {
                    "message": "strong",
                    "sentAt": NOW.replace(hour=10).isoformat(timespec="seconds"),
                    "status": "confirmed",
                    "confirmationLevel": "strong",
                },
                "Alice New": {
                    "message": "weak",
                    "sentAt": NOW.replace(hour=11).isoformat(timespec="seconds"),
                    "status": "sent_unverified",
                    "confirmationLevel": "weak",
                },
            },
            "protocol_targets_cache": [
                {
                    "nickname": "Alice",
                    "peerUserId": "1001",
                    "secUid": "sec-1",
                },
                {
                    "nickname": "Alice New",
                    "peerUserId": "1001",
                    "secUid": "sec-1",
                },
            ],
        }

        streak_state.reconcile_account(account, NOW)

        self.assertTrue(
            streak_state.is_send_confirmed(account, "Alice New", NOW)
        )

    def test_preflight_accepts_session_cookie_with_expires_minus_one(self):
        account = {
            "username": "demo",
            "targets": ["Alice"],
            "cookies": [
                {"name": "sessionid", "value": "x", "expires": -1},
            ],
        }

        result = streak_state.preflight_account(account, NOW)

        self.assertTrue(result["healthy"])

    def test_preflight_rejects_expired_authentication_cookie(self):
        account = {
            "username": "demo",
            "targets": ["Alice"],
            "cookies": [
                {
                    "name": "sessionid",
                    "value": "expired",
                    "expires": (NOW - timedelta(hours=1)).timestamp(),
                },
                {
                    "name": "passport_csrf_token",
                    "value": "still-valid",
                    "expires": (NOW + timedelta(hours=1)).timestamp(),
                },
            ],
        }

        result = streak_state.preflight_account(account, NOW)

        self.assertFalse(result["healthy"])
        self.assertEqual("expired_cookies", result["category"])


class StreakScheduleTests(unittest.TestCase):
    def test_schedule_phases_share_one_boundary_model(self):
        window = {
            "enabled": True,
            "startHour": 10,
            "endHour": 18,
            "scheduleIntervalMinutes": 20,
        }

        self.assertEqual(
            "regular",
            streak_state.schedule_phase(NOW, window),
        )
        self.assertEqual(
            "close-out",
            streak_state.schedule_phase(NOW.replace(hour=17, minute=45), window),
        )
        self.assertEqual(
            "fallback",
            streak_state.schedule_phase(NOW.replace(hour=18, minute=10), window),
        )
        self.assertEqual(
            "outside",
            streak_state.schedule_phase(NOW.replace(hour=19, minute=0), window),
        )

    def test_fallback_phase_selects_unconfirmed_targets(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        window = {
            "enabled": True,
            "startHour": 10,
            "endHour": 18,
            "scheduleIntervalMinutes": 20,
        }

        due, _, _, _ = tasks._select_due_targets(
            account,
            window,
            NOW.replace(hour=18, minute=10),
        )

        self.assertEqual(["Alice"], due)

    def test_active_in_flight_is_not_selected_as_due(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        streak_state.mark_in_flight(
            account,
            "Alice",
            run_id="run-1",
            strategy="browser",
            now=NOW.replace(hour=17, minute=45),
            lease_seconds=900,
        )
        window = {
            "enabled": True,
            "startHour": 10,
            "endHour": 18,
            "scheduleIntervalMinutes": 20,
        }

        due, _, pending, _ = tasks._select_due_targets(
            account,
            window,
            NOW.replace(hour=17, minute=45),
        )

        self.assertEqual([], due)
        self.assertEqual(1, len(pending))

    def test_fallback_attempted_target_is_not_selected_again(self):
        now = NOW.replace(hour=18, minute=10)
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        streak_state.mark_failed(
            account,
            "Alice",
            category="protocol_network_error",
            reason="temporary",
            retryable=True,
            now=now,
        )
        streak_state.mark_fallback_attempted(account, "Alice", now=now)
        streak_state.mark_sent_unverified(
            account,
            "Alice",
            run_id="fallback-1",
            strategy="browser",
            now=now,
        )
        window = {
            "enabled": True,
            "startHour": 10,
            "endHour": 18,
            "scheduleIntervalMinutes": 20,
        }

        due, _, pending, _ = tasks._select_due_targets(account, window, now)

        self.assertEqual([], due)
        self.assertEqual(1, len(pending))

    def test_end_hour_24_wraps_to_next_day_without_duplicate_fallback(self):
        window = {
            "enabled": True,
            "startHour": 10,
            "endHour": 24,
            "scheduleIntervalMinutes": 20,
        }

        self.assertEqual(
            "close-out",
            streak_state.schedule_phase(NOW.replace(hour=23, minute=50), window),
        )
        self.assertEqual(
            "fallback",
            streak_state.schedule_phase(NOW.replace(hour=23, minute=59), window),
        )
        self.assertEqual(
            "fallback",
            streak_state.schedule_phase(
                NOW.replace(hour=0, minute=5) + timedelta(days=1),
                window,
            ),
        )

    def test_weak_attempt_is_not_requeued_in_fallback(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        window = {
            "enabled": True,
            "startHour": 10,
            "endHour": 18,
            "scheduleIntervalMinutes": 20,
        }
        streak_state.mark_sent_unverified(
            account,
            "Alice",
            run_id="run-1",
            strategy="browser",
            now=NOW.replace(hour=17, minute=0),
        )

        close_out_due, _, close_out_pending, _ = tasks._select_due_targets(
            account,
            window,
            NOW.replace(hour=17, minute=40),
        )
        fallback_due, _, _, _ = tasks._select_due_targets(
            account,
            window,
            NOW.replace(hour=18, minute=20),
        )

        self.assertEqual([], close_out_due)
        self.assertEqual(1, len(close_out_pending))
        self.assertEqual([], fallback_due)

    def test_cross_midnight_fallback_does_not_resend_weak_evidence(self):
        window = {
            "enabled": True,
            "startHour": 10,
            "endHour": 24,
            "scheduleIntervalMinutes": 20,
        }
        local_now = NOW.replace(hour=23, minute=50)
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        streak_state.mark_sent_unverified(
            account,
            "Alice",
            run_id="run-1",
            strategy="browser",
            now=local_now,
        )

        due_before_midnight, _, pending_before, _ = tasks._select_due_targets(
            account,
            window,
            local_now.replace(hour=23, minute=59),
        )
        due_after_midnight, _, pending_after, _ = tasks._select_due_targets(
            account,
            window,
            local_now.replace(hour=0, minute=5) + timedelta(days=1),
        )

        self.assertEqual([], due_before_midnight)
        self.assertEqual(1, len(pending_before))
        self.assertEqual([], due_after_midnight)
        self.assertEqual(1, len(pending_after))

    def test_delayed_fallback_after_midnight_is_not_requeued(self):
        window = {
            "enabled": True,
            "startHour": 10,
            "endHour": 24,
            "scheduleIntervalMinutes": 20,
        }
        attempt_time = (
            NOW.replace(hour=23, minute=59)
            + timedelta(minutes=5)
        )
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        streak_state.mark_fallback_attempted(
            account,
            "Alice",
            now=attempt_time,
        )
        streak_state.mark_sent_unverified(
            account,
            "Alice",
            run_id="fallback-run",
            strategy="browser",
            now=attempt_time,
        )

        due, _, pending, _ = tasks._select_due_targets(
            account,
            window,
            attempt_time + timedelta(minutes=1),
        )

        self.assertEqual([], due)
        self.assertEqual(1, len(pending))

    def test_cron_plan_does_not_add_a_second_close_out_trigger(self):
        crontab = web_ops.replace_douyin_cron_schedule("", "10:00-18:00/20m")
        lines = [line for line in crontab.splitlines() if line.strip()]

        self.assertTrue(any(line.startswith("0 18 ") for line in lines))
        self.assertFalse(any(line.startswith("20 18 ") for line in lines))

        end_of_day = web_ops.replace_douyin_cron_schedule(
            "",
            "10:00-24:00/1m",
        )
        regular_end_of_day = [
            line
            for line in end_of_day.splitlines()
            if " 23 * * * " in line and "SPARKFLOW_FALLBACK" not in line
        ]
        self.assertTrue(regular_end_of_day)
        self.assertTrue(
            all("59" not in line.split()[0] for line in regular_end_of_day)
        )
        self.assertTrue(any(line.startswith("59 23 ") for line in end_of_day.splitlines()))

    def test_next_window_trigger_matches_fallback_cron(self):
        now = NOW.replace(hour=17, minute=50)
        trigger = web_ops._next_window_trigger(
            now,
            {
                "startHour": 10,
                "endHour": 18,
                "scheduleIntervalMinutes": 20,
            },
        )

        self.assertEqual(
            now.replace(hour=18, minute=0, second=0, microsecond=0),
            trigger,
        )


class StreakRunReportTests(unittest.TestCase):
    def test_run_report_is_structured_and_redacted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "run.jsonl"
            streak_state.append_run_report(
                {
                    "runId": "run-1",
                    "accountRef": "account:demo",
                    "targetRef": "sec:sec-1",
                    "strategy": "protocol",
                    "attemptCount": 1,
                    "category": "",
                    "confirmationSource": "protocol_send_receipt",
                    "durationMs": 42,
                    "networkRoute": "direct",
                    "cookies": ["secret"],
                    "token": "secret",
                },
                path=path,
            )

            record = json.loads(path.read_text(encoding="utf-8").strip())

        self.assertEqual("run-1", record["runId"])
        self.assertEqual("sec:sec-1", record["targetRef"])
        self.assertNotIn("cookies", record)
        self.assertNotIn("token", record)
        self.assertNotIn("secret", json.dumps(record, ensure_ascii=False))

    def test_run_report_includes_account_preflight_failure_reason(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "target_states": {
                "nickname:alice": {
                    "targetRef": "nickname:alice",
                    "displayName": "Alice",
                    "status": "failed_retryable",
                    "lastErrorCategory": "protocol_send_failed",
                    "lastErrorReason": "older target error",
                }
            },
            "account_health": {
                "healthy": False,
                "category": "missing_cookies",
                "reason": "account has no cookies",
            },
        }

        with (
            patch.object(streak_state, "append_run_report") as append,
            patch.object(streak_state, "update_weekly_summary"),
            patch.object(streak_state, "prune_run_reports"),
        ):
            tasks._append_streak_run_report(
                "run-preflight",
                datetime.now(timezone.utc) - timedelta(seconds=1),
                {"proxyAddress": ""},
                [account],
                run_status="skipped_no_runnable_accounts",
            )

        record = append.call_args.args[0]
        self.assertEqual("account_preflight_failed", record["status"])
        self.assertEqual("missing_cookies", record["category"])
        self.assertEqual("account has no cookies", record["reason"])

    def test_run_report_carries_evidence_level_and_sent_time(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "target_states": {
                "nickname:alice": {
                    "targetRef": "nickname:alice",
                    "displayName": "Alice",
                    "status": "sent_unverified",
                    "strategy": "browser",
                    "attemptCount": 1,
                    "confirmationSource": "browser_visible_count_increased",
                    "needsVerification": True,
                }
            },
            "message_history": {
                "Alice": {
                    "sentAt": "2026-09-18T10:05:00+08:00",
                    "confirmationLevel": "weak",
                    "confirmationSource": "browser_visible_count_increased",
                    "confirmationDetail": "page echo only",
                    "needsVerification": True,
                }
            },
        }

        with (
            patch.object(streak_state, "append_run_report") as append,
            patch.object(streak_state, "update_weekly_summary"),
            patch.object(streak_state, "prune_run_reports"),
        ):
            tasks._append_streak_run_report(
                "run-evidence",
                datetime.now(timezone.utc) - timedelta(seconds=1),
                {"proxyAddress": ""},
                [account],
                run_status="completed",
            )

        record = append.call_args.args[0]
        self.assertEqual("sent_unverified", record["status"])
        self.assertEqual("weak", record["confirmationLevel"])
        self.assertEqual("browser_visible_count_increased", record["confirmationSource"])
        self.assertEqual("page echo only", record["confirmationDetail"])
        self.assertTrue(record["needsVerification"])
        self.assertTrue(str(record["sentAt"]).startswith("2026-09-18T"))

    def test_report_redacts_credentials_from_free_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "run.jsonl"
            streak_state.append_run_report(
                {
                    "runId": "run-1",
                    "reason": (
                        "proxy failed https://user:secret@example.test "
                        "token=abc123 cookie=cookie456 "
                        "authorization: Bearer auth789 "
                        "authorization: Basic c2VjcmV0 "
                        "sessionid=session123 access_token=access456 "
                        "refresh_token=refresh789 "
                        "{\"authorization\":\"Bearer json987\","
                        "\"refresh_token\":\"jsonrefresh654\"}"
                    ),
                },
                path=path,
            )
            record = json.loads(path.read_text(encoding="utf-8").strip())

        self.assertNotIn("secret", record["reason"])
        self.assertNotIn("abc123", record["reason"])
        self.assertNotIn("cookie456", record["reason"])
        self.assertNotIn("auth789", record["reason"])
        self.assertNotIn("c2VjcmV0", record["reason"])
        self.assertNotIn("session123", record["reason"])
        self.assertNotIn("access456", record["reason"])
        self.assertNotIn("refresh789", record["reason"])
        self.assertNotIn("json987", record["reason"])
        self.assertNotIn("jsonrefresh654", record["reason"])

    def test_report_redacts_digest_and_quoted_sensitive_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "run.jsonl"
            streak_state.append_run_report(
                {
                    "runId": "run-1",
                    "reason": (
                        '{"sessionid":"session123","access_token":"access456",'
                        '"refresh_token":"refresh789"} '
                        'authorization: Digest username="alice", '
                        'response="deadbeefsecret" '
                        '{\\"authorization\\":\\"Bearer escaped987\\"} '
                        'authorization="123,456"'
                    ),
                },
                path=path,
            )
            record = json.loads(path.read_text(encoding="utf-8").strip())

        self.assertNotIn("session123", record["reason"])
        self.assertNotIn("access456", record["reason"])
        self.assertNotIn("refresh789", record["reason"])
        self.assertNotIn("deadbeefsecret", record["reason"])
        self.assertNotIn("escaped987", record["reason"])
        self.assertNotIn("123,456", record["reason"])

    def test_redact_text_handles_quoted_and_escaped_sensitive_keys(self):
        for value, leaked in (
            ('token="123,456"', "123,456"),
            ('password="abc,def"', "abc,def"),
            ('{\\"refresh_token\\":\\"abc,def\\"}', "abc,def"),
        ):
            with self.subTest(value=value):
                self.assertNotIn(leaked, streak_state._redact_text(value))

    def test_weekly_summary_aggregates_run_records(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = streak_state.update_weekly_summary(
                [
                    {
                        "status": "send_confirmed",
                        "category": "",
                        "confirmationSource": "protocol_send_receipt",
                        "durationMs": 20,
                    },
                    {
                        "status": "failed_retryable",
                        "category": "protocol_network_error",
                        "confirmationSource": "",
                        "durationMs": 30,
                    },
                ],
                directory=temp_dir,
                now=NOW,
            )
            summary = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(2, summary["runs"])
        self.assertEqual(1, summary["statusCounts"]["send_confirmed"])
        self.assertEqual(1, summary["categoryCounts"]["protocol_network_error"])
        self.assertEqual(50, summary["totalDurationMs"])

    def test_report_writes_use_atomic_replace(self):
        original_replace = streak_state.os.replace
        original_fsync = streak_state.os.fsync
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "run.jsonl"
            with (
                patch.object(
                    streak_state.os,
                    "replace",
                    wraps=original_replace,
                ) as replace,
                patch.object(
                    streak_state.os,
                    "fsync",
                    wraps=original_fsync,
                ) as fsync,
            ):
                streak_state.append_run_report(
                    {"runId": "run-1"},
                    path=path,
                )
                streak_state.append_run_report(
                    {"runId": "run-2"},
                    path=path,
                )
                streak_state.update_weekly_summary(
                    [{"status": "send_confirmed", "durationMs": 10}],
                    directory=temp_dir,
                    now=NOW,
                )

            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(["run-1", "run-2"], [
            record["runId"] for record in records
        ])
        self.assertGreaterEqual(replace.call_count, 3)
        self.assertGreaterEqual(fsync.call_count, 3)


class ReceiptOnlyConfirmationTests(unittest.TestCase):
    """A server receipt alone must not count as a strong confirmation."""

    STRONG_RECEIPT = {
        "ok": True,
        "httpStatus": 200,
        "call": "message_send",
        "jsonOk": True,
    }

    def test_strong_receipt_without_a_dom_bubble_is_receipt_only(self):
        user = {
            "username": "demo",
            "unique_id": "1",
            "targets": ["Alice"],
            "message_history": {},
        }
        accounts = [user]

        def fake_update(mutator, **kwargs):
            # Mirror the real update_user_data contract: a mutator may return
            # (result, changed), which is unwrapped before the caller sees it.
            result = mutator(accounts)
            changed = True
            if isinstance(result, tuple) and len(result) == 2:
                result, changed = result
            if kwargs.get("return_changed"):
                return result, changed
            return result

        with patch.object(tasks, "update_user_data", side_effect=fake_update):
            tasks._persist_browser_send_success(
                user,
                "Alice",
                "hi",
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                server_receipt=dict(self.STRONG_RECEIPT),
                dom_bubble_seen=False,
            )

        entry = dict((user.get("message_history") or {}).get("Alice") or {})
        self.assertEqual("receipt_only", entry.get("confirmationLevel"))
        self.assertEqual("sent_receipt_only", entry.get("status"))
        self.assertTrue(entry.get("needsVerification"))

    def test_receipt_only_today_is_never_resent(self):
        now = datetime(2026, 9, 18, 11, 0, tzinfo=timezone.utc)
        user = {
            "message_history": {
                "Alice": {
                    "status": "sent_receipt_only",
                    "confirmationLevel": "receipt_only",
                    "sentAt": "2026-09-18T10:00:00+00:00",
                }
            }
        }
        # Handled today, so no send or resend path repeats it...
        self.assertTrue(tasks._target_sent_today(user, "Alice", now))
        # ...but it is still not a strong confirmation, and not resendable.
        self.assertFalse(tasks._target_unconfirmed_today(user, "Alice", now))

    def test_forced_reset_of_a_receipt_only_record_becomes_resendable(self):
        from webui import app as web_app

        now = datetime(2026, 9, 18, 11, 0, tzinfo=timezone.utc)
        account = {
            "unique_id": "1",
            "username": "demo",
            "message_history": {
                "Alice": {
                    "status": "sent_receipt_only",
                    "confirmationLevel": "receipt_only",
                    "sentAt": "2026-09-18T10:00:00+00:00",
                }
            },
        }
        # While the record stands, nothing resends it...
        self.assertTrue(tasks._target_sent_today(account, "Alice", now))
        # ...but the operator's manual reset must really make it resendable,
        # otherwise the console promises a resend that never happens.
        changed = web_app.mark_target_unconfirmed(account, "Alice", force=True, now=now)
        self.assertTrue(changed)
        entry = dict((account.get("message_history") or {}).get("Alice") or {})
        self.assertEqual("unconfirmed", entry.get("status"))
        self.assertFalse(tasks._target_sent_today(account, "Alice", now))
        self.assertTrue(tasks._target_unconfirmed_today(account, "Alice", now))

class StreakTaskIntegrationTests(unittest.TestCase):
    @staticmethod
    def _update_side_effect(accounts):
        def update_accounts(mutator, force_reload=True):
            del force_reload
            value = mutator(accounts)
            if isinstance(value, tuple):
                return value[0]
            return value

        return update_accounts

    def test_protocol_fallback_claim_is_singleton(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        streak_state.mark_failed(
            account,
            "Alice",
            category="protocol_network_error",
            reason="temporary",
            retryable=True,
            now=NOW,
        )
        accounts = [account]

        with patch.object(
            tasks,
            "update_user_data",
            side_effect=self._update_side_effect(accounts),
        ):
            first = tasks._prepare_protocol_fallback_users(
                {"browserFallbackEnabled": True},
                [account.copy()],
            )
            second = tasks._prepare_protocol_fallback_users(
                {"browserFallbackEnabled": True},
                [account.copy()],
            )
            claimed = tasks._mark_browser_target_in_flight(
                account.copy(),
                "Alice",
                run_id="fallback-run-1",
                fallback=True,
            )
            third = tasks._prepare_protocol_fallback_users(
                {"browserFallbackEnabled": True},
                [account.copy()],
            )

        self.assertEqual(["Alice"], first[0]["targets"])
        self.assertEqual(["Alice"], second[0]["targets"])
        self.assertIs(True, claimed)
        self.assertEqual([], third)
        self.assertTrue(
            streak_state.target_state(account, "Alice", NOW)["fallbackAttempted"]
        )

    def test_protocol_does_not_send_confirmed_target(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        streak_state.mark_send_confirmed(
            account,
            "Alice",
            strategy="protocol",
            now=datetime.now(timezone.utc),
        )
        accounts = [account]

        with (
            patch.object(
                protocol_dispatch,
                "update_user_data",
                side_effect=self._update_side_effect(accounts),
            ),
            patch.object(
                protocol_dispatch,
                "build_messages_for_targets",
            ) as build_messages,
            patch.object(
                protocol_dispatch,
                "_run_protocol_for_user",
            ) as run_protocol,
        ):
            asyncio.run(
                protocol_dispatch.run_protocol_tasks(
                    {"multiTask": False, "taskCount": 1},
                    [account],
                    None,
                    run_id="run-1",
                )
            )

        build_messages.assert_not_called()
        run_protocol.assert_not_called()

    def test_protocol_dry_run_does_not_claim_in_flight(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        accounts = [account]
        sender_result = {
            "ok": True,
            "sent": [],
            "resolved": [],
            "unresolved": [],
            "dryRun": True,
        }

        with (
            patch.object(
                protocol_dispatch,
                "update_user_data",
                side_effect=self._update_side_effect(accounts),
            ),
            patch.object(
                protocol_dispatch,
                "build_messages_for_targets",
                return_value={"Alice": "hello"},
            ),
            patch.object(
                protocol_dispatch,
                "_run_protocol_for_user",
                return_value=sender_result,
            ),
        ):
            asyncio.run(
                protocol_dispatch.run_protocol_tasks(
                    {
                        "multiTask": False,
                        "taskCount": 1,
                        "protocolDryRun": True,
                    },
                    [account],
                    None,
                    run_id="run-1",
                )
            )

        state = streak_state.target_state(account, "Alice", NOW)
        self.assertEqual("pending", state["status"])

    def test_protocol_sender_failure_marks_retryable_state(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        accounts = [account]

        with patch.object(
            protocol_dispatch,
            "update_user_data",
            side_effect=self._update_side_effect(accounts),
        ):
            protocol_dispatch._mark_protocol_targets_failed(
                account.copy(),
                "protocol_sender_failed",
                "network",
            )

        state = streak_state.target_state(account, "Alice", NOW)
        self.assertEqual("failed_retryable", state["status"])
        self.assertEqual("protocol_sender_failed", state["lastErrorCategory"])

    def test_protocol_exception_marks_only_claimed_targets_failed(self):
        live_now = datetime.now(timezone.utc)
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice", "Bob"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        streak_state.mark_in_flight(
            account,
            "Alice",
            run_id="run-old",
            strategy="protocol",
            now=live_now,
            lease_seconds=900,
        )
        streak_state.mark_in_flight(
            account,
            "Bob",
            run_id="run-new",
            strategy="protocol",
            now=live_now,
            lease_seconds=900,
        )
        accounts = [account]

        with patch.object(
            protocol_dispatch,
            "update_user_data",
            side_effect=self._update_side_effect(accounts),
        ):
            claimed = protocol_dispatch._protocol_claimed_targets(
                account.copy(),
                "run-new",
            )
            protocol_dispatch._mark_protocol_targets_failed(
                dict(account.copy(), targets=claimed),
                "protocol_sender_failed",
                "transport failed",
            )

        self.assertEqual(["Bob"], claimed)
        alice_state = streak_state.target_state(account, "Alice", live_now)
        bob_state = streak_state.target_state(account, "Bob", live_now)
        self.assertEqual("in_flight", alice_state["status"])
        self.assertEqual("run-old", alice_state["runId"])
        self.assertEqual("failed_retryable", bob_state["status"])
        self.assertEqual("protocol_sender_failed", bob_state["lastErrorCategory"])

    def test_protocol_claim_deduplicates_aliases_of_same_identity(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice", "Alice New"],
            "target_refs": {
                "Alice": "sec:sec-1",
                "Alice New": "sec:sec-1",
            },
        }
        accounts = [account]

        with patch.object(
            protocol_dispatch,
            "update_user_data",
            side_effect=self._update_side_effect(accounts),
        ):
            claimed = protocol_dispatch._mark_protocol_targets_in_flight(
                account.copy(),
                "run-1",
            )

        self.assertEqual(["Alice"], claimed)

    def test_protocol_identity_matches_nickname_case_insensitively(self):
        identities = protocol_dispatch._build_protocol_target_identities(
            {
                "protocol_targets_cache": [
                    {
                        "nickname": "Alice New",
                        "secUid": "sec-1",
                        "peerUserId": "1001",
                    }
                ]
            },
            {"alice new": "hello"},
        )

        self.assertEqual("sec-1", identities["alice new"]["secUid"])
        self.assertEqual("1001", identities["alice new"]["peerUserId"])

    def test_protocol_identity_uses_known_stable_ref_after_rename(self):
        identities = protocol_dispatch._build_protocol_target_identities(
            {
                "target_refs": {
                    "Alice": "sec:sec-1",
                    "Alice New": "sec:sec-1",
                },
                "protocol_targets_cache": [
                    {
                        "nickname": "Alice",
                        "secUid": "sec-1",
                        "peerUserId": "1001",
                    }
                ],
            },
            {"Alice New": "hello"},
        )

        self.assertEqual("sec-1", identities["Alice New"]["secUid"])
        self.assertEqual("1001", identities["Alice New"]["peerUserId"])

    def test_old_protocol_receipt_cannot_override_newer_manual_reset(self):
        observed_at = datetime.now(timezone.utc)
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
        }
        streak_state.mark_unconfirmed(
            account,
            "Alice",
            reason="manual_reset_possible_false_positive",
            now=observed_at,
        )
        sender_result = {
            "demo": {
                "sent": [
                    {
                        "target": "Alice",
                        "message": "hello",
                        "sentAt": (
                            observed_at - timedelta(minutes=1)
                        ).isoformat(timespec="seconds"),
                        "success": True,
                        "statusCode": 0,
                    }
                ]
            }
        }

        protocol_dispatch._apply_protocol_runtime_state(
            [account],
            [account],
            sender_result,
        )

        state = streak_state.target_state(account, "Alice", observed_at)
        self.assertEqual("failed_retryable", state["status"])
        self.assertEqual("send_unconfirmed", state["lastErrorCategory"])

    def test_old_protocol_failure_cannot_override_newer_manual_reset(self):
        observed_at = datetime.now(timezone.utc)
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
        }
        streak_state.mark_unconfirmed(
            account,
            "Alice",
            reason="newer manual reset",
            now=observed_at,
        )
        sender_result = {
            "demo": {
                "sent": [
                    {
                        "target": "Alice",
                        "message": "hello",
                        "sentAt": (
                            observed_at - timedelta(minutes=1)
                        ).isoformat(timespec="seconds"),
                        "success": False,
                        "statusCode": 1,
                        "statusName": "CheckMessageNotPass",
                    }
                ]
            }
        }

        protocol_dispatch._apply_protocol_runtime_state(
            [account],
            [account],
            sender_result,
        )

        state = streak_state.target_state(account, "Alice", observed_at)
        self.assertEqual("failed_retryable", state["status"])
        self.assertEqual("send_unconfirmed", state["lastErrorCategory"])
        self.assertNotIn("failure_queue", account)

    def test_fallback_alias_cannot_repeat_in_same_run(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice", "Alice New"],
            "target_refs": {
                "Alice": "sec:sec-1",
                "Alice New": "sec:sec-1",
            },
        }
        accounts = [account]

        with patch.object(
            tasks,
            "update_user_data",
            side_effect=self._update_side_effect(accounts),
        ):
            first = tasks._mark_browser_target_in_flight(
                account.copy(),
                "Alice",
                run_id="fallback-run",
                fallback=True,
            )
            tasks._persist_browser_send_failure(
                account.copy(),
                "Alice",
                "hello",
                "browser_timeout",
                "first attempt failed",
                NOW.isoformat(timespec="seconds"),
            )
            second = tasks._mark_browser_target_in_flight(
                account.copy(),
                "Alice New",
                run_id="fallback-run",
                fallback=True,
            )

        self.assertTrue(first)
        self.assertFalse(second)

    def test_renamed_target_protocol_failure_keeps_legacy_attempt_count(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice New"],
            "target_refs": {
                "Alice": "sec:sec-1",
                "Alice New": "sec:sec-1",
            },
            "failure_queue": {
                "Alice": {
                    "category": "protocol_network_error",
                    "reason": "first",
                    "firstAttemptAt": NOW.isoformat(timespec="seconds"),
                    "lastAttemptAt": NOW.isoformat(timespec="seconds"),
                    "attemptCount": 2,
                }
            },
        }

        protocol_dispatch._record_protocol_target_failure(
            account,
            "Alice New",
            "hello",
            "protocol_network_error",
            "second",
        )

        entry = streak_state.failure_entry(account, "Alice New", NOW.tzinfo)
        self.assertEqual(3, entry["attemptCount"])
        self.assertEqual("second", entry["reason"])

    def test_renamed_target_protocol_success_clears_alias_account_failure(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice New"],
            "target_refs": {
                "Alice": "sec:sec-1",
                "Alice New": "sec:sec-1",
            },
            "account_failure": {
                "category": "protocol_sender_failed",
                "reason": "transport failed",
                "lastAttemptAt": NOW.isoformat(timespec="seconds"),
                "attemptCount": 2,
                "affectedTargets": ["Alice"],
            },
        }
        sender_result = {
            "demo": {
                "sent": [
                    {
                        "target": "Alice New",
                        "message": "hello",
                        "sentAt": NOW.isoformat(timespec="seconds"),
                        "success": True,
                        "statusCode": 0,
                    }
                ]
            }
        }

        changed = protocol_dispatch._apply_protocol_runtime_state(
            [account],
            [account],
            sender_result,
        )

        self.assertTrue(changed)
        self.assertNotIn("account_failure", account)

    def test_scheduled_preflight_filters_unhealthy_accounts(self):
        healthy = {
            "username": "healthy",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        unhealthy = {
            "username": "unhealthy",
            "unique_id": "1002",
            "targets": ["Bob"],
            "cookies": [],
        }
        config = {
            "dailySendWindow": {"enabled": False},
            "useProtocolSender": True,
            "browserFallbackEnabled": False,
        }

        with (
            patch.object(tasks, "_is_manual_run", return_value=False),
            patch.object(tasks, "_persist_account_preflight") as persist,
        ):
            runnable = tasks._prepare_active_users_for_run(
                config,
                [healthy, unhealthy],
            )

        self.assertEqual(["healthy"], [user["username"] for user in runnable])
        persist.assert_called_once()

    def test_scheduled_preflight_persists_healthy_recovery(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
            "account_health": {
                "healthy": False,
                "category": "missing_cookies",
                "reason": "stale failure",
            },
        }
        config = {"dailySendWindow": {"enabled": False}}

        with (
            patch.object(tasks, "_is_manual_run", return_value=False),
            patch.object(
                tasks.streak_state,
                "preflight_account",
                return_value={"healthy": True, "category": "", "reason": ""},
            ),
            patch.object(tasks, "_persist_account_preflight") as persist,
        ):
            tasks._prepare_active_users_for_run(config, [account])

        persist.assert_called_once()
        self.assertTrue(persist.call_args.args[1]["healthy"])

    def test_scheduled_preflight_clears_recovered_login_failure(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
            "account_failure": {
                "category": "login_required",
                "reason": "old failure",
                "lastAttemptAt": NOW.isoformat(timespec="seconds"),
                "attemptCount": 2,
                "affectedTargets": ["Alice"],
            },
            "account_health": {
                "healthy": False,
                "category": "login_required",
                "reason": "stale failure",
                "checkedAt": NOW.isoformat(timespec="seconds"),
            },
        }
        accounts = [account]
        config = {"dailySendWindow": {"enabled": False}}

        def update_accounts(mutator, force_reload=True):
            del force_reload
            return mutator(accounts)

        with (
            patch.object(tasks, "_is_manual_run", return_value=False),
            patch.object(
                tasks.streak_state,
                "preflight_account",
                return_value={"healthy": True, "category": "", "reason": ""},
            ),
            patch.object(
                tasks,
                "update_user_data",
                side_effect=update_accounts,
            ),
        ):
            tasks._prepare_active_users_for_run(config, [account])

        self.assertNotIn("account_failure", account)
        self.assertTrue(account["account_health"]["healthy"])

    def test_scheduled_preflight_preserves_send_failure_cooldown(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
            "account_failure": {
                "category": "protocol_sender_failed",
                "reason": "recent send failure",
                "lastAttemptAt": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
                "attemptCount": 2,
                "affectedTargets": ["Alice"],
            },
        }
        accounts = [account]
        config = {"dailySendWindow": {"enabled": False}}

        def update_accounts(mutator, force_reload=True):
            del force_reload
            return mutator(accounts)

        with (
            patch.object(tasks, "_is_manual_run", return_value=False),
            patch.object(
                tasks.streak_state,
                "preflight_account",
                return_value={"healthy": True, "category": "", "reason": ""},
            ),
            patch.object(
                tasks,
                "update_user_data",
                side_effect=update_accounts,
            ),
        ):
            runnable = tasks._prepare_active_users_for_run(config, [account])

        self.assertEqual(
            "protocol_sender_failed",
            account["account_failure"]["category"],
        )
        self.assertTrue(account["account_health"]["healthy"])
        self.assertEqual([], runnable)

    def test_scheduled_run_does_not_require_missing_friend_index(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        accounts = [account]
        config = {
            "dailySendWindow": {"enabled": False},
            "useProtocolSender": False,
        }

        def update_accounts(mutator, force_reload=True):
            del force_reload
            value = mutator(accounts)
            if isinstance(value, tuple):
                return value[0]
            return value

        with (
            patch.object(tasks, "_is_manual_run", return_value=False),
            patch.object(
                tasks,
                "update_user_data",
                side_effect=update_accounts,
            ),
        ):
            runnable = tasks._prepare_active_users_for_run(config, [account])

        # The browser send flow builds the friend index on demand, so a missing
        # index must not reject the run nor mark the account unhealthy.
        self.assertEqual(1, len(runnable))
        self.assertEqual(["Alice"], runnable[0]["targets"])
        self.assertNotEqual(
            "friend_index_stale",
            (account.get("account_health") or {}).get("category"),
        )

    def test_scheduled_run_does_not_require_index_for_explicit_browser_account(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        accounts = [account]
        config = {
            "dailySendWindow": {"enabled": False},
            "useProtocolSender": True,
            "browserFallbackEnabled": False,
            "browserSenderAccounts": ["demo"],
        }

        def update_accounts(mutator, force_reload=True):
            del force_reload
            value = mutator(accounts)
            if isinstance(value, tuple):
                return value[0]
            return value

        with (
            patch.object(tasks, "_is_manual_run", return_value=False),
            patch.object(
                tasks,
                "update_user_data",
                side_effect=update_accounts,
            ),
        ):
            runnable = tasks._prepare_active_users_for_run(config, [account])

        # Being an explicit browser sender no longer implies the run is gated on
        # an index that only the gated flow can produce.
        self.assertEqual(1, len(runnable))
        self.assertEqual(["Alice"], runnable[0]["targets"])
        self.assertNotEqual(
            "friend_index_stale",
            (account.get("account_health") or {}).get("category"),
        )

    def test_scheduled_preflight_allows_retry_after_protocol_failure_cooldown(self):
        now = datetime.now(timezone(timedelta(hours=8)))
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
            "account_failure": {
                "category": "protocol_sender_failed",
                "reason": "stale transport failure",
                "lastAttemptAt": (
                    now - timedelta(hours=2)
                ).isoformat(timespec="seconds"),
                "attemptCount": 2,
                "affectedTargets": ["Alice"],
            },
        }
        accounts = [account]
        config = {"dailySendWindow": {"enabled": False}}

        def update_accounts(mutator, force_reload=True):
            del force_reload
            return mutator(accounts)

        with (
            patch.object(tasks, "_is_manual_run", return_value=False),
            patch.object(
                tasks.streak_state,
                "preflight_account",
                return_value={"healthy": True, "category": "", "reason": ""},
            ),
            patch.object(
                tasks,
                "update_user_data",
                side_effect=update_accounts,
            ),
        ):
            runnable = tasks._prepare_active_users_for_run(config, [account])

        self.assertEqual(["demo"], [user["username"] for user in runnable])

    def test_temporary_failure_cooldown_survives_midnight(self):
        previous_day = datetime(
            2026,
            9,
            14,
            23,
            50,
            tzinfo=timezone(timedelta(hours=8)),
        )
        next_day = previous_day + timedelta(minutes=20)
        account = {
            "account_failure": {
                "category": "protocol_sender_failed",
                "reason": "recent transport failure",
                "lastAttemptAt": previous_day.isoformat(timespec="seconds"),
                "attemptCount": 2,
                "affectedTargets": ["Alice"],
            }
        }

        self.assertTrue(
            tasks._account_paused_by_failure_today(account, next_day)
        )
        entry = web_ops._account_failure_entry_today(account, next_day)
        self.assertTrue(
            web_ops._account_failure_pause_active(
                entry,
                next_day,
                web_ops._account_failure_pause_after_attempts(),
            )
        )

    def test_fallback_run_filters_unhealthy_accounts(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [],
        }
        config = {"dailySendWindow": {"enabled": True}}

        with (
            patch.object(tasks, "_is_fallback_run", return_value=True),
            patch.object(tasks, "_is_manual_run", return_value=True),
            patch.object(
                tasks.streak_state,
                "preflight_account",
                return_value={
                    "healthy": False,
                    "category": "missing_cookies",
                    "reason": "account has no cookies",
                },
            ),
            patch.object(tasks, "_persist_account_preflight") as persist,
        ):
            runnable = tasks._prepare_active_users_for_run(config, [account])

        self.assertEqual([], runnable)
        persist.assert_called_once()

    def test_relogin_clears_preflight_failure_state(self):
        from webui import app as web_app

        account = {
            "account_ref": "acc-1",
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "old"}],
            "account_failure": {
                "category": "login_required",
                "reason": "login required",
                "lastAttemptAt": NOW.isoformat(timespec="seconds"),
                "attemptCount": 1,
                "affectedTargets": ["Alice"],
            },
            "account_health": {
                "healthy": False,
                "category": "login_required",
                "reason": "login required",
                "checkedAt": NOW.isoformat(timespec="seconds"),
            },
        }
        accounts = [account]

        def update_accounts(mutator, force_reload=True):
            del force_reload
            return mutator(accounts)

        with patch.object(
            web_app,
            "update_user_data",
            side_effect=update_accounts,
        ):
            updated, action = web_app.save_exported_login_result(
                {
                    "unique_id": "1001",
                    "username": "demo",
                    "cookies": [{"name": "sessionid", "value": "new"}],
                },
                relogin_unique_id="1001",
            )

        self.assertEqual("updated", action)
        self.assertNotIn("account_failure", account)
        self.assertNotIn("account_health", account)
        self.assertEqual(
            "new",
            updated["cookies"][0]["value"],
        )

    def test_send_console_surfaces_preflight_pause_reason(self):
        account = {
            "username": "unhealthy",
            "unique_id": "1002",
            "targets": ["Bob"],
            "cookies": [],
            "account_health": {
                "healthy": False,
                "category": "missing_cookies",
                "reason": "account has no cookies",
                "checkedAt": NOW.isoformat(timespec="seconds"),
            },
        }

        with (
            patch.object(web_ops, "get_userData", return_value=[account]),
            patch.object(
                web_ops,
                "_normalize_send_window",
                return_value={"enabled": False},
            ),
        ):
            snapshot = web_ops.get_send_console_snapshot()

        row = snapshot["accounts"][0]
        self.assertTrue(row["account_paused"])
        self.assertEqual("paused", row["state"])
        self.assertTrue(
            any(
                "account has no cookies" in warning.get("message", "")
                for warning in row["warnings"]
            )
        )

    def test_manual_browser_run_id_marks_target_in_flight(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        accounts = [account]

        with patch.object(
            tasks,
            "update_user_data",
            side_effect=self._update_side_effect(accounts),
        ):
            tasks._mark_browser_target_in_flight(
                account.copy(),
                "Alice",
                run_id="manual-run-1",
            )

        state = streak_state.target_state(account, "Alice", NOW)
        self.assertEqual("in_flight", state["status"])
        self.assertEqual("manual-run-1", state["runId"])

    def test_manual_retry_can_reclaim_unverified_or_fallback_target(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        accounts = [account]
        streak_state.mark_sent_unverified(
            account,
            "Alice",
            run_id="old-run",
            strategy="browser",
            now=NOW,
        )
        streak_state.mark_fallback_attempted(account, "Alice", now=NOW)

        with patch.object(
            tasks,
            "update_user_data",
            side_effect=self._update_side_effect(accounts),
        ):
            started = tasks._mark_browser_target_in_flight(
                account.copy(),
                "Alice",
                run_id="manual-retry",
                allow_retry=True,
            )

        state = streak_state.target_state(account, "Alice", NOW)
        self.assertTrue(started)
        self.assertEqual("in_flight", state["status"])
        self.assertEqual("manual-retry", state["runId"])

    def test_manual_target_retry_writes_run_report(self):
        from webui import app as web_app

        account = {
            "account_ref": "acc-1",
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        client = TestClient(web_app.app)

        with (
            patch.object(
                web_app,
                "current_principal",
                return_value={"role": "admin", "username": "admin"},
            ),
            patch.object(web_app, "validate_csrf", return_value=True),
            patch.object(
                web_app,
                "ensure_account_refs",
                side_effect=lambda accounts: (accounts, False),
            ),
            patch.object(
                web_app,
                "account_by_unique_id",
                return_value=account,
            ),
            patch.object(web_app, "can_access_account", return_value=True),
            patch.object(
                web_app,
                "task_run_lock_status",
                return_value={"running": False},
            ),
            patch.object(
                web_app,
                "task_run_lock",
                return_value=nullcontext(),
            ),
            patch.object(
                web_app,
                "get_config",
                return_value={"taskCount": 1},
            ),
            patch.object(
                web_app,
                "run_browser_tasks",
                new=AsyncMock(),
            ) as run_browser,
            patch.object(
                web_app,
                "get_userData",
                return_value=[account],
            ),
            patch.object(
                web_app,
                "_append_streak_run_report",
            ) as append_report,
        ):
            response = client.post(
                "/accounts/1001/retry-target",
                data={"target": "Alice", "csrf_token": "test"},
                follow_redirects=False,
            )

        self.assertEqual(303, response.status_code)
        run_browser.assert_awaited_once()
        append_report.assert_called_once()
        self.assertEqual(
            run_browser.await_args.kwargs["run_id"],
            append_report.call_args.args[0],
        )
        self.assertEqual(
            "completed",
            append_report.call_args.kwargs["run_status"],
        )

    def test_browser_in_flight_claim_rejects_confirmed_target(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        streak_state.mark_send_confirmed(
            account,
            "Alice",
            strategy="protocol",
            now=NOW,
        )

        started = tasks._mark_browser_target_in_flight(
            account.copy(),
            "Alice",
            run_id="manual-run-1",
        )

        self.assertIs(False, started)

    def test_browser_in_flight_claim_rejects_active_lease(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        live_now = datetime.now(timezone.utc)
        streak_state.mark_in_flight(
            account,
            "Alice",
            run_id="crashed-run",
            strategy="browser",
            now=live_now,
            lease_seconds=900,
        )

        started = tasks._mark_browser_target_in_flight(
            account.copy(),
            "Alice",
            run_id="manual-run-1",
        )

        self.assertIs(False, started)
        state = streak_state.target_state(account, "Alice", live_now)
        self.assertEqual("crashed-run", state["runId"])
        self.assertEqual(1, state["attemptCount"])

    def test_renamed_target_failure_increments_legacy_attempt_count(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice New"],
            "target_refs": {
                "Alice": "sec:sec-1",
                "Alice New": "sec:sec-1",
            },
            "failure_queue": {
                "Alice": {
                    "category": "browser_timeout",
                    "reason": "first",
                    "firstAttemptAt": NOW.isoformat(timespec="seconds"),
                    "lastAttemptAt": NOW.isoformat(timespec="seconds"),
                    "attemptCount": 2,
                }
            },
        }
        accounts = [account]
        attempted_at = (NOW + timedelta(minutes=1)).isoformat(timespec="seconds")

        with patch.object(
            tasks,
            "update_user_data",
            side_effect=self._update_side_effect(accounts),
        ):
            tasks._persist_browser_send_failure(
                account.copy(),
                "Alice New",
                "hello",
                "browser_timeout",
                "second",
                attempted_at,
            )

        entry = streak_state.failure_entry(account, "Alice New", NOW.tzinfo)
        self.assertEqual(3, entry["attemptCount"])
        self.assertEqual("second", entry["reason"])

    def test_rejected_older_browser_failure_does_not_write_queue(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        accounts = [account]
        streak_state.mark_send_confirmed(
            account,
            "Alice",
            strategy="protocol",
            now=NOW,
        )

        with patch.object(
            tasks,
            "update_user_data",
            side_effect=self._update_side_effect(accounts),
        ):
            tasks._persist_browser_send_failure(
                account.copy(),
                "Alice",
                "hello",
                "browser_timeout",
                "older failure",
                (NOW - timedelta(minutes=1)).isoformat(timespec="seconds"),
            )

        self.assertNotIn("failure_queue", account)
        self.assertEqual(
            "send_confirmed",
            streak_state.target_state(account, "Alice", NOW)["status"],
        )

    def test_stale_same_category_failure_does_not_rewrite_queue(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        accounts = [account]
        streak_state.mark_failed(
            account,
            "Alice",
            category="browser_timeout",
            reason="newer failure",
            retryable=True,
            now=NOW,
        )
        account["failure_queue"] = {
            "Alice": {
                "category": "browser_timeout",
                "reason": "newer failure",
                "lastAttemptAt": NOW.isoformat(timespec="seconds"),
                "attemptCount": 1,
            }
        }

        with patch.object(
            tasks,
            "update_user_data",
            side_effect=self._update_side_effect(accounts),
        ):
            tasks._persist_browser_send_failure(
                account.copy(),
                "Alice",
                "hello",
                "browser_timeout",
                "older failure",
                (NOW - timedelta(minutes=1)).isoformat(timespec="seconds"),
            )

        self.assertEqual(
            "newer failure",
            account["failure_queue"]["Alice"]["reason"],
        )
        self.assertEqual(
            NOW.isoformat(timespec="seconds"),
            account["failure_queue"]["Alice"]["lastAttemptAt"],
        )

    def test_send_console_distinguishes_streak_verified(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        streak_state.mark_streak_verified(account, "Alice", now=NOW)

        item = web_ops._build_target_status(
            account,
            "Alice",
            NOW,
            {"enabled": False},
        )

        self.assertEqual("verified", item["confirmationLevel"])
        self.assertEqual("streak_verified", item["confirmationSource"])

    def test_web_manual_unconfirmed_uses_state_machine(self):
        from webui import app as web_app

        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice New"],
            "target_refs": {
                "Alice": "sec:sec-1",
                "Alice New": "sec:sec-1",
            },
            "message_history": {
                "Alice": {
                    "message": "hello",
                    "sentAt": NOW.isoformat(timespec="seconds"),
                    "status": "confirmed",
                    "confirmationLevel": "strong",
                }
            },
        }
        streak_state.reconcile_account(account, NOW)

        changed = web_app.mark_target_unconfirmed(
            account,
            "Alice New",
            now=NOW.replace(microsecond=123456),
        )

        self.assertTrue(changed)
        self.assertFalse(
            streak_state.is_send_confirmed(account, "Alice New", NOW)
        )
        self.assertEqual(
            "failed_retryable",
            streak_state.target_state(account, "Alice New", NOW)["status"],
        )
        self.assertFalse(web_app._target_sent_today(account, "Alice New"))
        item = web_ops._build_target_status(
            account,
            "Alice New",
            NOW,
            {"enabled": False},
        )
        self.assertEqual("unconfirmed", item["status"])

    def test_manual_unconfirmed_survives_stable_identity_migration(self):
        from webui import app as web_app

        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice New"],
            "message_history": {
                "Alice": {
                    "message": "hello",
                    "sentAt": NOW.isoformat(timespec="seconds"),
                    "status": "confirmed",
                    "confirmationLevel": "strong",
                }
            },
        }
        self.assertTrue(
            web_app.mark_target_unconfirmed(
                account,
                "Alice New",
                now=NOW,
            )
        )
        account["protocol_targets_cache"] = [
            {
                "nickname": "Alice New",
                "secUid": "sec-1",
                "peerUserId": "1001",
            }
        ]

        streak_state.reconcile_account(account, NOW)
        history = streak_state.history_entry(
            account,
            "Alice New",
            NOW.tzinfo,
        )
        item = web_ops._build_target_status(
            account,
            "Alice New",
            NOW,
            {"enabled": False},
        )

        self.assertEqual("unconfirmed", history["status"])
        self.assertIn("resetAt", history)
        self.assertEqual("unconfirmed", item["status"])

    def test_manual_unconfirmed_state_overrides_legacy_strong_history(self):
        account = {
            "username": "demo",
            "targets": ["Alice New"],
            "target_refs": {
                "Alice": "sec:sec-1",
                "Alice New": "sec:sec-1",
            },
            "message_history": {
                "Alice": {
                    "message": "hello",
                    "sentAt": NOW.isoformat(timespec="seconds"),
                    "status": "confirmed",
                    "confirmationLevel": "strong",
                }
            },
        }
        streak_state.mark_unconfirmed(
            account,
            "Alice New",
            reason="manual_reset_possible_false_positive",
            now=NOW,
        )

        item = web_ops._build_target_status(
            account,
            "Alice New",
            NOW,
            {"enabled": False},
        )

        self.assertEqual("unconfirmed", item["status"])
        self.assertEqual("send_unconfirmed", item["category"])

    def test_stale_manual_reset_does_not_rewrite_history_or_queue(self):
        from webui import app as web_app

        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        self.assertTrue(
            web_app.mark_target_unconfirmed(
                account,
                "Alice",
                reason="newer reset",
                now=NOW,
            )
        )
        newer_history = dict(account["message_history"]["Alice"])
        newer_queue = dict(account["failure_queue"]["Alice"])

        changed = web_app.mark_target_unconfirmed(
            account,
            "Alice",
            reason="stale reset",
            now=NOW - timedelta(minutes=1),
        )

        self.assertFalse(changed)
        self.assertEqual(newer_history, account["message_history"]["Alice"])
        self.assertEqual(newer_queue, account["failure_queue"]["Alice"])

    def test_send_console_account_failure_uses_alias_ref(self):
        now = datetime.now(timezone(timedelta(hours=8)))
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice New"],
            "target_refs": {
                "Alice": "sec:sec-1",
                "Alice New": "sec:sec-1",
            },
            "account_failure": {
                "category": "browser_login_required",
                "reason": "login required",
                "lastAttemptAt": now.isoformat(timespec="seconds"),
                "attemptCount": 2,
                "affectedTargets": ["Alice"],
            },
        }

        with (
            patch.object(web_ops, "get_userData", return_value=[account]),
            patch.object(
                web_ops,
                "_normalize_send_window",
                return_value={"enabled": False},
            ),
        ):
            snapshot = web_ops.get_send_console_snapshot()

        blocked = snapshot["accounts"][0]["account_blocked_targets"]
        self.assertEqual(1, len(blocked))
        self.assertTrue(blocked[0]["accountFailureAffected"])

    def test_send_console_tracks_expired_account_failure_cooldown(self):
        now = datetime.now(timezone(timedelta(hours=8)))
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "account_failure": {
                "category": "protocol_sender_failed",
                "reason": "stale transport failure",
                "lastAttemptAt": (
                    now - timedelta(hours=2)
                ).isoformat(timespec="seconds"),
                "attemptCount": 2,
                "affectedTargets": ["Alice"],
            },
        }

        with (
            patch.object(web_ops, "get_userData", return_value=[account]),
            patch.object(
                web_ops,
                "_normalize_send_window",
                return_value={"enabled": False},
            ),
        ):
            snapshot = web_ops.get_send_console_snapshot()

        row = snapshot["accounts"][0]
        self.assertFalse(row["account_paused"])
        self.assertEqual([], row["account_blocked_targets"])

    def test_nested_business_error_is_not_a_strong_send_receipt(self):
        data = {
            "data": {
                "status_code": 1,
                "status_msg": "rejected",
            }
        }

        self.assertIs(False, tasks._json_body_success(data))
        self.assertFalse(
            streak_state.receipt_is_strong(
                {
                    "ok": True,
                    "httpStatus": 200,
                    "call": "message_send",
                    "jsonOk": False,
                }
            )
        )
        self.assertIs(
            False,
            tasks._json_body_success(
                {"data": {"status_code": None}},
            ),
        )
        self.assertIs(
            False,
            tasks._json_body_success(
                {
                    "status_code": 0,
                    "data": {
                        "payload": {
                            "error_code": 1,
                        }
                    },
                }
            ),
        )
        self.assertIs(
            None,
            tasks._json_body_success(
                {"message": "success"},
            ),
        )

    def test_receipt_without_explicit_success_marker_is_not_strong(self):
        self.assertFalse(
            streak_state.receipt_is_strong(
                {
                    "ok": True,
                    "httpStatus": 200,
                    "call": "message_send",
                }
            )
        )

    def test_server_rejection_can_still_persist_visible_weak_evidence(self):
        self.assertTrue(
            tasks._should_persist_recovered_browser_evidence(
                True,
                "server send receipt rejected",
            )
        )

    def test_send_console_keeps_confirmed_state_over_weak_failure_queue(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        streak_state.mark_send_confirmed(
            account,
            "Alice",
            strategy="protocol",
            now=NOW,
        )
        account["failure_queue"] = {
            "Alice": {
                "category": "browser_timeout",
                "lastAttemptAt": NOW.isoformat(timespec="seconds"),
                "attemptCount": 1,
            }
        }

        item = web_ops._build_target_status(
            account,
            "Alice",
            NOW,
            {"enabled": False},
        )

        self.assertEqual("sent", item["status"])
        self.assertEqual("send_confirmed", item["sendState"])
        self.assertFalse(item["needsVerification"])

    def test_send_console_does_not_reuse_previous_day_confirmation(self):
        next_day = NOW + timedelta(days=1)
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        streak_state.mark_send_confirmed(
            account,
            "Alice",
            strategy="protocol",
            now=NOW,
        )
        account["failure_queue"] = {
            "Alice": {
                "category": "browser_timeout",
                "lastAttemptAt": next_day.isoformat(timespec="seconds"),
                "attemptCount": 1,
            }
        }

        item = web_ops._build_target_status(
            account,
            "Alice",
            next_day,
            {"enabled": False},
        )

        self.assertEqual("failed", item["status"])

    def test_send_console_reads_failure_from_alias_ref(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice New"],
            "target_refs": {
                "Alice": "sec:sec-1",
                "Alice New": "sec:sec-1",
            },
            "failure_queue": {
                "Alice": {
                    "category": "send_unconfirmed",
                    "lastAttemptAt": NOW.isoformat(timespec="seconds"),
                    "attemptCount": 1,
                }
            },
        }

        item = web_ops._build_target_status(
            account,
            "Alice New",
            NOW,
            {"enabled": False},
        )

        self.assertEqual("unconfirmed", item["status"])
        self.assertEqual("send_unconfirmed", item["category"])

    def test_protocol_exception_still_runs_selected_fallback(self):
        user = {
            "username": "demo",
            "unique_id": "1001",
            "enabled": True,
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        fallback_users = [dict(user)]
        config = {
            "multiTask": False,
            "taskCount": 1,
            "sendStrategy": {},
            "useProtocolSender": True,
            "browserSenderAccounts": [],
        }
        lock = MagicMock()
        lock.return_value.__enter__.return_value = None
        lock.return_value.__exit__.return_value = False

        with (
            patch.object(tasks, "get_config", return_value=config),
            patch.object(tasks, "get_userData", return_value=[user]),
            patch.object(tasks, "_requested_account_refs", return_value=None),
            patch.object(tasks, "_prepare_active_users_for_run", return_value=[user]),
            patch.object(tasks, "_split_sender_modes", return_value=([user], [])),
            patch.object(tasks, "task_run_lock", lock),
            patch.object(
                tasks,
                "run_protocol_tasks",
                new=AsyncMock(side_effect=RuntimeError("protocol transport failed")),
            ),
            patch.object(
                tasks,
                "_prepare_protocol_fallback_users",
                return_value=fallback_users,
            ),
            patch.object(tasks, "run_browser_tasks", new=AsyncMock()) as browser,
            patch.object(tasks, "_append_streak_run_report"),
        ):
            asyncio.run(tasks.runTasks())

        self.assertGreaterEqual(browser.await_count, 1)
        self.assertEqual(fallback_users, browser.await_args_list[0].args[1])

    def test_run_tasks_reports_skipped_run_without_enabled_accounts(self):
        config = {
            "multiTask": False,
            "taskCount": 1,
            "sendStrategy": {},
        }

        with (
            patch.object(tasks, "get_config", return_value=config),
            patch.object(tasks, "get_userData", return_value=[]),
            patch.object(tasks, "_requested_account_refs", return_value=None),
            patch.object(tasks, "_append_streak_run_report") as append,
        ):
            asyncio.run(tasks.runTasks())

        append.assert_called_once()
        self.assertEqual(
            "skipped_no_enabled_accounts",
            append.call_args.kwargs.get("run_status"),
        )

    def test_browser_weak_evidence_persists_unverified_state(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        accounts = [account]

        with patch.object(
            tasks,
            "update_user_data",
            side_effect=self._update_side_effect(accounts),
        ):
            tasks._persist_browser_send_success(
                account.copy(),
                "Alice",
                "hello",
                NOW.isoformat(timespec="seconds"),
                server_receipt=None,
            )

        history = account["message_history"]["Alice"]
        state = streak_state.target_state(account, "Alice", NOW)
        self.assertEqual("sent_unverified", history["status"])
        self.assertEqual("weak", history["confirmationLevel"])
        self.assertEqual("sent_unverified", state["status"])
        self.assertFalse(streak_state.is_send_confirmed(account, "Alice", NOW))

    def test_browser_recovery_visible_evidence_remains_unverified(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        accounts = [account]

        with patch.object(
            tasks,
            "update_user_data",
            side_effect=self._update_side_effect(accounts),
        ):
            tasks._persist_browser_recovery_outcome(
                account.copy(),
                "Alice",
                "hello",
                NOW.isoformat(timespec="seconds"),
                detail="conversation already contains message",
                server_receipt={},
            )

        state = streak_state.target_state(account, "Alice", NOW)
        self.assertEqual("sent_unverified", state["status"])
        self.assertFalse(streak_state.is_streak_verified(account, "Alice", NOW))

    def test_older_browser_weak_evidence_does_not_rewrite_newer_history(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        accounts = [account]
        streak_state.mark_send_confirmed(
            account,
            "Alice",
            strategy="protocol",
            now=NOW,
        )
        account["message_history"] = {
            "Alice": {
                "message": "newer strong",
                "sentAt": NOW.isoformat(timespec="seconds"),
                "status": "confirmed",
                "confirmationLevel": "strong",
            }
        }

        with patch.object(
            tasks,
            "update_user_data",
            side_effect=self._update_side_effect(accounts),
        ):
            tasks._persist_browser_recovery_outcome(
                account.copy(),
                "Alice",
                "older weak",
                (NOW - timedelta(minutes=1)).isoformat(timespec="seconds"),
                detail="older page evidence",
                server_receipt={},
            )

        self.assertEqual(
            "newer strong",
            account["message_history"]["Alice"]["message"],
        )
        self.assertEqual(
            "confirmed",
            account["message_history"]["Alice"]["status"],
        )


if __name__ == "__main__":
    unittest.main()
