"""Real offline Chromium: the DOM can be nonempty while the send model is empty."""
import asyncio
from datetime import datetime
from unittest.mock import patch

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from core import tasks


HTML = '''<div id="sub-app"><div class="im-saas-viewport-test">
  <div class="componentsRightPanelwrapper"><div id="messages"></div>
    <div data-e2e="msg-input"><div class="messageEditorinputArea" contenteditable="true"><p><span>\u200b</span></p></div></div>
  </div>
</div></div>'''

SETUP = r'''() => {
 const editor = document.querySelector('.messageEditorinputArea');
 const root = {tag:3, stateNode:{}}; root.stateNode.current = root;
 const comp = {tag:0, return:root};
 const host = {tag:5, stateNode:editor, return:comp};
 root.child = comp; comp.child = host; editor.__reactFiber$fixture = host;
 window.host = host; window.comp = comp; window.sent = 0; window.inputs = 0;
 window.ops = [{insert:'\n'}];
 const model = {getRootContainer:()=>editor,
    getContentState:()=>({getContent:()=>new Map([['main',[{getOps:()=>window.ops}]]])})};
 comp.memoizedState = {memoizedState:{current:model}, next:null};
 editor.addEventListener('input', () => {
    window.inputs++;
    // Fixture's renderer has a decoration; the SDK send content does not.
    window.ops = [{insert:editor.innerText.replace(/^\u200b/,'')+'\n'}];
 });
 window.addBubble = (id, own=true) => {
    const row = document.createElement('div');
    row.className='MessageBoxContentcolumnBox'+(own?' MessageBoxContentisFromMe':'');
    row.innerHTML='<div data-e2e="msg-item-content"><div class="MessageItemTextbubbleTextContent">111</div></div>';
    const content=row.firstElementChild;
    content.__reactFiber$fixture={memoizedProps:{message:{clientId:id,createdAt:new Date(),isFromMe:own}}};
    document.querySelector('#messages').append(row);
 };
 editor.addEventListener('keydown', e => {
    if(e.key!=='Enter' || e.shiftKey) return;
    e.preventDefault();
    if(window.ops[0].insert!=='111\n') return;
    window.sent++;
    window.ops=[{insert:'\n'}];
    editor.innerHTML='<p><span>\u200b</span></p>';
    if(window.addOnEnter) window.addBubble('this-send');
 });
}'''


@pytest_asyncio.fixture
async def composer():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=['--no-sandbox', '--disable-dev-shm-usage'])
        try:
            page = await browser.new_page()
            await page.route('**/*', lambda route: route.abort())
            page.set_default_timeout(1500)
            await page.set_content(HTML)
            await page.evaluate(SETUP)
            editor, selector = await tasks.locate_chat_input(page)
            assert selector == tasks._SAAS_CHAT_INPUT_SELECTOR
            # Simulate the cursor position observed on the site's decorated editor.
            await editor.press('End')
            yield page, editor
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_types_into_model_proven_empty_editor_without_clearing(composer):
    page, editor = composer
    await tasks._type_browser_message_checked(editor, '111', saas_editor_input=True)
    assert await page.evaluate('ops[0].insert') == '111\n'
    assert await page.evaluate('sent') == 0
    assert await editor.inner_text() == '\u200b111'


@pytest.mark.asyncio
async def test_confirm_accepts_cleared_model_with_renderer_sentinel(composer):
    page, editor = composer
    await page.evaluate('addOnEnter=true')
    time_before = int(datetime.now().timestamp() * 1000)
    await tasks._type_browser_message_checked(editor, '111', saas_editor_input=True)
    await editor.press('Enter')
    assert await page.evaluate('sent') == 1
    assert await editor.inner_text() == '\u200b'
    ok, reason = await tasks.confirm_message_sent(page, editor, '111', before_indicator='',
                                                  saas_since_ms=time_before)
    assert ok, reason


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [
    "ops=[{insert:'\\u200b\\n'}]",  # a real model input, not a renderer sentinel
    "ops=[{insert:'draft\\n'}]; document.querySelector('.messageEditorinputArea').textContent=''",
    "ops=[{insert:'\\n',attributes:{lmkr:'1'}},{insert:{image:true}}]",
    "ops=[{insert:'payload',attributes:{lmkr:'1'}}]",
    "comp.child=null",  # detached model path
    "comp.memoizedState.next={memoizedState:{...comp.memoizedState.memoizedState.current}}",
])
async def test_preserves_existing_or_unproven_input(composer, change):
    page, editor = composer
    await page.evaluate('() => {' + change + '}')
    before = await editor.evaluate('e=>e.innerHTML')
    with pytest.raises(RuntimeError, match='input_(nonempty|read_unavailable)'):
        await tasks._type_browser_message_checked(editor, '111', saas_editor_input=True)
    assert await editor.evaluate('e=>e.innerHTML') == before
    assert await page.evaluate('inputs+sent') == 0


@pytest.mark.asyncio
async def test_old_own_bubble_is_not_new_when_it_moves(composer):
    page, editor = composer
    await page.evaluate("addBubble('previous')")
    before = await tasks.snapshot_last_own_message(page)
    assert before and before['text'] == '111'
    # Deliberately place the boundary before the old timestamp too: an old id
    # must never be authorized by moved geometry, even with equal-ms clocks.
    since = int(datetime.now().timestamp() * 1000) - 3000
    await page.evaluate("document.querySelector('#messages').style.marginTop='50px'")
    with patch.object(tasks, '_MESSAGE_CONFIRM_POLL_SECONDS', 0.01), \
         patch.object(tasks, '_MESSAGE_CONFIRM_BUDGET_SECONDS', 0.04):
        ok, reason = await tasks.confirm_message_sent(page, editor, '111', before_snapshot=before,
                                                      before_indicator='', saas_since_ms=since)
    assert not ok and reason == 'chat input cleared but no new own message bubble was confirmed'
