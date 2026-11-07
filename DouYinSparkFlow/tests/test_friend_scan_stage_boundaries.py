"""Offline regressions for the shared first/resumed friend-scan DOM stage."""
import asyncio
import unittest
from unittest.mock import Mock, AsyncMock, patch
from core import tasks


class DialogStageBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def assert_bounded(self, operation):
        with patch.object(tasks, 'BROWSER_STAGE_TIMEOUT_SECONDS', .02):
            try:
                await asyncio.wait_for(operation, timeout=.5)
            except tasks.BrowserStageTimeout as exc:
                self.assertEqual(exc.stage, 'dismiss_dialogs')
            except asyncio.TimeoutError:
                self.fail('the shared dialog check still waited for the outer scan deadline')
            else:
                self.fail('a wedged DOM operation was not reported as a bounded stage failure')

    async def test_first_friend_tab_open_bounds_dialog_count(self):
        async def hang():
            await asyncio.Future()
        locator=Mock(count=AsyncMock(side_effect=hang))
        locator.first = locator
        page=Mock(locator=Mock(return_value=locator))
        await self.assert_bounded(tasks._open_friends_tab(page,'synthetic','fallback',[]))
        page.get_by_text.assert_not_called()

    async def test_resumed_scan_after_a_bounds_dialog_count_before_selecting_b(self):
        blocked=False
        async def count():
            if blocked:
                await asyncio.Future()
            return 0
        dialog=Mock(count=AsyncMock(side_effect=count))
        dialog.first = dialog
        page=Mock(locator=Mock(return_value=dialog),evaluate=AsyncMock())
        rows=[Mock(name='Alice'),Mock(name='Bob')]
        for row,name in zip(rows,['Alice','Bob']):
            row.synthetic_name=name
        locator=Mock(all=AsyncMock(return_value=rows))
        async def record(row):
            return {'visibleName':row.synthetic_name,'normalizedName':tasks._normalize_target_name(row.synthetic_name),'stableKeys':['name:'+row.synthetic_name]}
        selected=[]
        async def select(page,row,user,target):
            selected.append(target)
            return True,''
        with patch.object(tasks,'_open_friends_tab',AsyncMock()), \
             patch.object(tasks,'_wait_for_friend_list_ready',AsyncMock(return_value=('rows',locator))), \
             patch.object(tasks,'_first_non_empty_locator',AsyncMock(return_value=('rows',locator))), \
             patch.object(tasks,'_first_scrollable_friends_element',AsyncMock(return_value=('scroll',object()))), \
             patch.object(tasks,'_extract_friend_record',side_effect=record), \
             patch.object(tasks,'_selector_visible',AsyncMock(return_value=False)), \
             patch.object(tasks,'_persist_friend_index',Mock()), \
             patch.object(tasks.friend_chat_guard,'select_friend',side_effect=select):
            generator=tasks.scroll_and_select_user(page,{'targets':['Alice','Bob']},'synthetic',
                ['Alice','Bob'],{'maxScanSeconds':60,'idleScanSeconds':10,'scrollStepPx':100,'scrollDelaySeconds':0})
            try:
                self.assertEqual(await asyncio.wait_for(generator.__anext__(),timeout=4),'Alice')
                blocked=True
                await self.assert_bounded(generator.__anext__())
                self.assertEqual(selected,['Alice'])
            finally:
                await generator.aclose()

if __name__=='__main__':
    unittest.main()
