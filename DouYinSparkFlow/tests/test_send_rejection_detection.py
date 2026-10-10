import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from core import streak_state
from core import tasks


SHANGHAI = ZoneInfo("Asia/Shanghai")


class SendRejectionExtractionTests(unittest.TestCase):
    def test_parsed_refusal_code_is_detected(self):
        data = {"status_code": 8101, "tips": ""}

        self.assertEqual(
            {"key": "status_code", "value": 8101, "source": "json"},
            tasks._extract_send_rejection(data, '{"status_code":8101,"tips":""}'),
        )

    def test_parsed_success_is_not_a_refusal(self):
        data = {"status_code": 0, "check_code": 0, "status_msg": {"msg_type": 2}}

        self.assertIsNone(tasks._extract_send_rejection(data, ""))

    def test_wrapped_body_still_yields_the_code(self):
        # The live body is protobuf with an embedded JSON fragment, so the JSON
        # parser returns None and only the raw text carries the refusal code.
        body = '\x08d\x10\x00"\x02OK\x18\x00{"status_code":8101,"tips":""}:"2026100'

        self.assertEqual(
            {"key": "status_code", "value": 8101, "source": "raw"},
            tasks._extract_send_rejection(None, body),
        )
        self.assertEqual(
            {"key": "status_code", "value": 8101, "source": "raw"},
            tasks._extract_send_rejection(None, '{"status_code" : 8101}'),
        )

    def test_check_codes_count_as_refusal(self):
        self.assertEqual(
            {"key": "check_code", "value": 1, "source": "raw"},
            tasks._extract_send_rejection(None, '{"check_code":1,"check_msg":""}'),
        )
        self.assertEqual(
            {"key": "raw_check_code", "value": 1, "source": "raw"},
            tasks._extract_send_rejection(None, '{"raw_check_code":1}'),
        )

    def test_zero_codes_are_not_refusals(self):
        self.assertIsNone(tasks._extract_send_rejection(None, '{"status_code":0}'))
        self.assertIsNone(
            tasks._extract_send_rejection(
                None, '{"raw_check_code":0,"check_code":0,"err_no":0}'
            )
        )

    def test_metadata_and_free_text_are_not_refusals(self):
        self.assertIsNone(
            tasks._extract_send_rejection(
                None,
                '{"msg_type":2,"decision_type":"verify","tips":"请完成验证"}',
            )
        )
        # Unquoted prose - for example our own message echoed into the body - is
        # not a structured field and must not be read as a refusal.
        self.assertIsNone(tasks._extract_send_rejection(None, "status_code: 8101"))
        self.assertIsNone(
            tasks._extract_send_rejection(None, "消息内容提到 status_code 8101")
        )

    def test_parsed_body_is_not_text_scanned(self):
        # A parsed success whose text merely mentions a code stays a success.
        data = {"status_code": 0, "message": '{"status_code":8101}'}

        self.assertIsNone(tasks._extract_send_rejection(data, str(data)))


class SendRejectionDecisionTests(unittest.TestCase):
    def _refusal(self):
        return tasks.BrowserSendRejected(
            "server refused the message (status_code=8101)",
            {"key": "status_code", "value": 8101, "source": "raw"},
        )

    def test_refusal_blocks_page_bubble_recovery(self):
        self.assertFalse(
            tasks._should_persist_recovered_browser_evidence(True, self._refusal())
        )

    def test_any_error_carrying_a_refusal_receipt_blocks_recovery(self):
        error = RuntimeError("send failed")
        error.receipt = {"ok": False, "rejection": {"key": "status_code", "value": 8101}}

        self.assertFalse(
            tasks._should_persist_recovered_browser_evidence(True, error)
        )

    def test_unknown_receipt_keeps_page_bubble_recovery(self):
        self.assertTrue(
            tasks._should_persist_recovered_browser_evidence(
                True, RuntimeError("server send receipt rejected; response_body_unavailable")
            )
        )

    def test_missing_bubble_still_fails(self):
        self.assertFalse(
            tasks._should_persist_recovered_browser_evidence(
                False, RuntimeError("server send receipt rejected")
            )
        )

    def test_refusal_has_its_own_terminal_category(self):
        self.assertEqual(
            "send_rejected",
            tasks.classify_browser_failure("send_flow", self._refusal()),
        )
        self.assertIn("send_rejected", streak_state.TERMINAL_FAILURE_CATEGORIES)

    def test_other_send_failures_keep_their_category(self):
        self.assertEqual(
            "send_unconfirmed",
            tasks.classify_browser_failure(
                "send_flow", RuntimeError("visible message count did not increase")
            ),
        )


class SendRejectedQueueTests(unittest.TestCase):
    def _user(self, category):
        now = datetime.now(SHANGHAI)
        return {
            "username": "Tester",
            "unique_id": "123",
            "targets": ["林恩"],
            "failure_queue": {
                "林恩": {
                    "category": category,
                    "reason": "status_code=8101",
                    "lastAttemptAt": now.isoformat(timespec="seconds"),
                    "attemptCount": 1,
                }
            },
        }

    def test_refused_target_leaves_the_failed_resend_queue(self):
        now = datetime.now(SHANGHAI)

        self.assertEqual(
            [], tasks._pending_failed_targets(self._user("send_rejected"), now)
        )

    def test_retryable_failure_stays_in_the_failed_resend_queue(self):
        now = datetime.now(SHANGHAI)

        self.assertEqual(
            ["林恩"],
            tasks._pending_failed_targets(self._user("send_unconfirmed"), now),
        )

    def test_refused_target_is_skipped_by_the_unsent_queue(self):
        now = datetime.now(SHANGHAI)

        retry_targets, skipped = tasks._pending_unsent_targets(
            self._user("send_rejected"), now
        )

        self.assertEqual([], retry_targets)
        self.assertTrue(any("non_retryable" in item for item in skipped), skipped)


if __name__ == "__main__":
    unittest.main()
