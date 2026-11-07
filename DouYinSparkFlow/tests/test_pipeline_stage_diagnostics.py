"""Offline stage-boundary regressions for friend-list scanning."""
import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from core import tasks


class FriendListStageDiagnosticsTests(unittest.TestCase):
    def test_wedged_row_list_read_reports_its_stage_before_scan_budget(self):
        async def hang():
            await asyncio.Future()

        async def exercise():
            async def no_sleep(*_args, **_kwargs):
                return None

            dialog = Mock(count=AsyncMock(return_value=0))
            dialog.first = dialog
            page = Mock(evaluate=AsyncMock(), locator=Mock(return_value=dialog))
            locator = Mock(all=AsyncMock(side_effect=hang))
            generator = tasks.scroll_and_select_user(
                page,
                {"targets": ["synthetic-a"]},
                "synthetic-account",
                ["synthetic-a"],
                {
                    "maxScanSeconds": 60,
                    "idleScanSeconds": 10,
                    "scrollStepPx": 100,
                    "scrollDelaySeconds": 0,
                },
            )
            with patch.object(tasks, "_open_friends_tab", AsyncMock()), \
                 patch.object(tasks, "_wait_for_friend_list_ready", AsyncMock(return_value=("rows", locator))), \
                 patch.object(tasks, "_first_non_empty_locator", AsyncMock(return_value=("rows", locator))), \
                 patch.object(tasks, "_first_scrollable_friends_element", AsyncMock(return_value=("scroll", object()))), \
                 patch.object(tasks, "BROWSER_STAGE_TIMEOUT_SECONDS", .01), \
                 patch.object(tasks.asyncio, "sleep", no_sleep):
                try:
                    with self.assertRaises(tasks.BrowserStageTimeout) as caught:
                        await asyncio.wait_for(generator.__anext__(), timeout=.2)
                    self.assertEqual(caught.exception.stage, "friend_list_read_rows")
                finally:
                    await generator.aclose()

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
