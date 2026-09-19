import unittest
from unittest.mock import patch

from webui import ops as web_ops

TAIL = "\n".join(
    [
        "2026-09-19 17:20:09,101 - app - INFO - tasks.py:1 - preflight finished",
        "[AUTO_TRIGGER] 2026-09-19T17:20:10+08:00 scheduled send exit rc=0",
        "2026-09-19 17:40:10,200 - app - WARNING - tasks.py:2 - No accounts passed preflight",
        "[AUTO_TRIGGER] 2026-09-19T17:40:11+08:00 scheduled send exit rc=0",
        "2026-09-19 18:00:11,300 - app - ERROR - tasks.py:3 - boom",
        "[AUTO_TRIGGER] 2026-09-19T18:00:11+08:00 unsent fallback start",
        "2026-09-19 18:05:06,400 - app - INFO - tasks.py:4 - all done",
    ]
)


class OpsLogReadingTests(unittest.TestCase):
    """``read_log_tail`` returns one string, so callers must split it into lines."""

    def test_recent_trigger_lines_reads_lines_not_characters(self):
        with patch.object(web_ops, "read_log_tail", return_value=TAIL):
            triggers = web_ops.recent_trigger_lines()

        self.assertEqual(3, len(triggers))
        self.assertTrue(all(line.startswith("[AUTO_TRIGGER]") for line in triggers))
        self.assertIn("unsent fallback start", triggers[-1])

    def test_recent_trigger_lines_honours_the_limit(self):
        with patch.object(web_ops, "read_log_tail", return_value=TAIL):
            triggers = web_ops.recent_trigger_lines(limit=2)

        self.assertEqual(2, len(triggers))
        self.assertIn("17:40:11", triggers[0])
        self.assertIn("18:00:11", triggers[1])

    def test_summarize_log_tail_counts_real_lines_and_levels(self):
        with patch.object(web_ops, "read_log_tail", return_value=TAIL):
            summary = web_ops.summarize_log_tail(limit=400)

        # Seven real lines, not len(TAIL) characters.
        self.assertEqual(7, summary["lines"])
        self.assertEqual({"INFO": 2, "WARNING": 1, "ERROR": 1}, summary["levels"])

    def test_empty_tail_is_safe(self):
        with patch.object(web_ops, "read_log_tail", return_value=""):
            self.assertEqual([], web_ops.recent_trigger_lines())
            self.assertEqual(
                {"lines": 0, "levels": {}, "categories": []},
                web_ops.summarize_log_tail(),
            )

    def test_missing_log_file_is_safe(self):
        with patch.object(web_ops, "read_log_tail", return_value=None):
            self.assertEqual([], web_ops.recent_trigger_lines())
            self.assertEqual(0, web_ops.summarize_log_tail()["lines"])


if __name__ == "__main__":
    unittest.main()
