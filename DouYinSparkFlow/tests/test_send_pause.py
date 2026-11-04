from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core import send_pause


NOW = datetime(2026, 11, 3, 0, 0, tzinfo=timezone.utc)


class SendPauseTests(unittest.TestCase):
    def test_missing_pause_file_preserves_existing_send_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sending-paused-until.json"
            self.assertFalse(send_pause.should_pause_sending(path=path, now=NOW))

    def test_default_path_is_repository_logs_pause_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "logs" / "sending-paused-until.json"
            path.parent.mkdir()
            path.write_text('{"until":"2026-11-04T00:00:00+08:00"}', encoding="utf-8")
            with patch.object(send_pause, "repo_root", return_value=root):
                self.assertTrue(send_pause.should_pause_sending(now=NOW))

    def test_aware_future_until_pauses_and_exposes_only_a_generic_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sending-paused-until.json"
            path.write_text('{"until":"2026-11-04T00:00:00+08:00"}', encoding="utf-8")
            paused, reason = send_pause.pause_status(path=path, now=NOW)
            self.assertTrue(paused)
            self.assertEqual("pause_until_active", reason)

    def test_until_at_or_before_now_automatically_resumes_sending(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sending-paused-until.json"
            path.write_text('{"until":"2026-11-03T00:00:00+00:00"}', encoding="utf-8")
            self.assertFalse(send_pause.should_pause_sending(path=path, now=NOW))

    def test_malformed_or_naive_pause_state_fails_closed(self):
        for payload in ("not json", '{"until":"2026-11-04T00:00:00"}', "{}"):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "sending-paused-until.json"
                path.write_text(payload, encoding="utf-8")
                paused, reason = send_pause.pause_status(path=path, now=NOW)
                self.assertTrue(paused)
                self.assertEqual("pause_file_invalid", reason)


if __name__ == "__main__":
    unittest.main()
