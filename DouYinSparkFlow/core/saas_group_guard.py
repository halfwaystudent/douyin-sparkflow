"""New IM SaaS group-chat identity adapter (read / select / verify).

Scope: only the verified new UI shape —
``div[data-e2e="conversation-item"]`` rows whose committed React fiber carries
``memoizedProps.conversation`` with ``type === 2`` (GROUP_CHAT in the public
bundle's ``ConversationType`` enum) and a numeric ``shortId`` of 10-25 digits.
``id`` is the thread namespace (``0:2:...``) and is never compared to the bound
conversation id.

All identity proofs come from the *committed current* fiber tree via
``friend_chat_guard._COMMON`` helpers (``committedFiber``,
``nearestSaasConversation``). List/store membership (``allList`` /
``conversationMap``) is never consulted, and no deep DFS hunts for an
arbitrary conversation object.

Public API (async, Playwright):

* ``read_row_id(row)`` -> numeric shortId string or ``None``.
* ``select_row(row, bound_id)`` -> ``'clicked'`` or a reason string.
* ``verify_open(page, chat_input, bound_id)`` -> ``(bool, reason)``.

``chat_input`` may be ``None`` (header-only proof, allowed only before a send
input exists) or a Locator/ElementHandle that must resolve to the single
visible ``[data-e2e="msg-input"] [contenteditable="true"]`` node. Anything
unknown, replaced, or recycled fails closed — never raises into ``True``.
"""

from core import im_ui
from core.friend_chat_guard import _COMMON

# Shallow walk over the committed current path looking for the row's own
# conversation prop. Depth mirrors friend_chat_guard.read (<=3 hops above the
# host fiber); anything deeper is not this row's identity.
_ROW_CONVERSATION = r"""
const rowConversation = row => {
  const state = committedFiber(row);
  if (!state.committed) return {committed:false, conversation:null};
  for (let level = 0; level <= 3; level++) {
    const props = state.path[level] && state.path[level].memoizedProps;
    const c = props && (props.conversation || (props.header && props.header.props && props.header.props.conversation));
    if (c && typeof c === 'object') return {committed:true, conversation:c};
  }
  return {committed:true, conversation:null};
};
const groupConversationOk = (c, boundId) => !!(c && typeof c === 'object'
  && c.type === 2
  && typeof c.shortId === 'string' && /^[0-9]{10,25}$/.test(c.shortId)
  && (!boundId || c.shortId === boundId));
const rowTitle = row => {
  const node = row.querySelector('.conversationConversationItemtitle');
  return visible(node) ? normalizeName(node.textContent) : '';
};
"""

_READ_ROW_ID = "(row)=>{" + _COMMON + _ROW_CONVERSATION + r"""
currentRootIndexes = new WeakMap();
if (!row || !row.matches || !row.matches('[data-e2e="conversation-item"]')) return null;
const found = rowConversation(row);
if (!found.committed || !groupConversationOk(found.conversation)) return null;
return found.conversation.shortId;
}"""

_SELECT_ROW = "(row,boundId)=>{" + _COMMON + _ROW_CONVERSATION + r"""
currentRootIndexes = new WeakMap();
const bound = String(boundId || '');
if (!/^\d{10,25}$/.test(bound)) return {ok:false, reason:'group_bound_id_invalid'};
if (!row || !row.matches || !row.matches('[data-e2e="conversation-item"]')) return {ok:false, reason:'group_row_unavailable'};
if (!visible(row)) return {ok:false, reason:'group_row_unavailable'};
const rows = saasRows();
if (!rows.includes(row)) return {ok:false, reason:'group_row_unavailable'};
const found = rowConversation(row);
if (!found.committed) return {ok:false, reason:'group_row_uncommitted'};
if (!groupConversationOk(found.conversation, bound)) return {ok:false, reason:'group_row_identity_mismatch'};
const title = rowTitle(row);
if (!title) return {ok:false, reason:'group_row_title_unavailable'};
// Exactly one visible row may claim this bound id; a second match means the
// virtual list recycled or duplicated the row under us.
let claimants = 0;
for (const other of rows) {
  const o = rowConversation(other);
  if (o.committed && groupConversationOk(o.conversation, bound)) claimants++;
}
if (claimants !== 1) return {ok:false, reason:'group_row_ambiguous'};
// The SaaS row activates on mousedown (verified onMouseDown in the public
// bundle), never on name text — dispatch the same event a real press makes.
row.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
return {ok:true, reason:'', title};
}"""

_VERIFY_OPEN = "cfg=>{" + _COMMON + _ROW_CONVERSATION + r"""
currentRootIndexes = new WeakMap();
const bound = String(cfg.boundId || '');
if (!/^\d{10,25}$/.test(bound)) return {ok:false, reason:'group_bound_id_invalid'};
const inputNode = cfg.input || null;
// Exactly one visible row carries the active-conversation class.
const rows = saasRows();
const active = rows.filter(row => row.classList.contains('conversationConversationItemcurConversation'));
if (active.length !== 1) return {ok:false, reason: active.length ? 'group_active_row_ambiguous' : 'group_active_row_missing'};
const activeRow = active[0];
const rowFound = rowConversation(activeRow);
if (!rowFound.committed) return {ok:false, reason:'group_row_uncommitted'};
if (!groupConversationOk(rowFound.conversation, bound)) return {ok:false, reason:'group_active_identity_mismatch'};
const title = rowTitle(activeRow);
if (!title) return {ok:false, reason:'group_row_title_unavailable'};
// Header: unique visible convHeader whose single title equals the row title.
const headers = Array.from(document.querySelectorAll('#sub-app .RightPanelHeaderconvHeader')).filter(visible);
if (headers.length !== 1) return {ok:false, reason:'group_header_ambiguous'};
const titles = Array.from(headers[0].querySelectorAll('.RightPanelHeadertitle')).filter(visible);
if (titles.length !== 1 || normalizeName(titles[0].textContent) !== title)
  return {ok:false, reason:'group_header_title_mismatch'};
// Header pane proof: nearest committed curConversation on the header's own
// current path must be the bound group conversation object.
const headerState = committedFiber(headers[0]);
if (headerState.rootKnown && !headerState.committed) return {ok:false, reason:'group_header_uncommitted'};
const headerProvider = headerState.committed ? nearestSaasConversation(headerState) : null;
if (!headerProvider) return {ok:false, reason:'group_header_conversation_unavailable'};
const headerConv = headerProvider.memoizedProps.curConversation;
if (!groupConversationOk(headerConv, bound)) return {ok:false, reason:'group_header_conversation_mismatch'};
if (!inputNode) return {ok:true, reason:'header_only'};
// Input pane proof: the supplied node must be the single visible SaaS editor.
const inputs = Array.from(document.querySelectorAll(SAAS_INPUT_SEL)).filter(visible);
if (inputs.length !== 1) return {ok:false, reason:'group_input_ambiguous'};
if (inputs[0] !== inputNode) return {ok:false, reason:'group_input_node_mismatch'};
const inputState = committedFiber(inputNode);
if (inputState.rootKnown && !inputState.committed) return {ok:false, reason:'group_input_uncommitted'};
const inputProvider = inputState.committed ? nearestSaasConversation(inputState) : null;
if (!inputProvider) return {ok:false, reason:'group_input_conversation_unavailable'};
const inputConv = inputProvider.memoizedProps.curConversation;
// Same object AND same committed current path: both providers must resolve
// to the identical conversation object under the same root.current.
if (inputConv !== headerConv || !groupConversationOk(inputConv, bound))
  return {ok:false, reason:'group_input_conversation_mismatch'};
return {ok:true, reason:''};
}"""


async def read_row_id(row):
    """Return the row's bound-format group id (numeric shortId) or ``None``.

    Only a committed, visible ``conversation-item`` row whose shallow fiber
    conversation is provably ``type === 2`` qualifies; every other shape —
    legacy rows, friend rows, unreadable or recycled fibers — reads as
    ``None`` rather than a guess.
    """
    try:
        value = await row.evaluate(_READ_ROW_ID)
    except Exception:
        return None
    return value if isinstance(value, str) and value.isdigit() and 10 <= len(value) <= 25 else None


async def select_row(row, bound_id):
    """Atomically verify then activate the bound group row.

    Returns ``'clicked'`` only when the synchronous page-side check proved the
    committed current identity (type 2, ``shortId === bound_id``), a visible
    row title, and no second claimant for the id — then dispatched the row's
    real ``mousedown`` activation. Any failure returns a reason and clicks
    nothing.
    """
    try:
        result = await row.evaluate(_SELECT_ROW, str(bound_id or ''))
    except Exception as exc:
        return f'group_selection_unavailable: {type(exc).__name__}'
    if not isinstance(result, dict):
        return 'group_selection_unavailable'
    if result.get('ok') is True:
        return 'clicked'
    return str(result.get('reason') or 'group_selection_unavailable')


async def _anchor_handle(chat_input):
    """Resolve a Locator/ElementHandle to something ``page.evaluate`` accepts."""
    if chat_input is None:
        return None
    if hasattr(chat_input, 'element_handle'):
        try:
            return await chat_input.element_handle()
        except Exception:
            return None
    return chat_input


async def verify_open(page, chat_input, bound_id):
    """Prove the opened pane is still the bound group conversation.

    Requires the read-only 群聊 filter to be active, exactly one visible
    ``curConversation`` row matching ``bound_id``, a unique convHeader whose
    title equals the row title, and — when ``chat_input`` is supplied — the
    single visible SaaS editor resolving to the *same* committed
    ``curConversation`` object as the header. Header-only mode (``None``)
    proves just the header conversation and is only acceptable before an
    input exists.
    """
    try:
        if not await im_ui.saas_filter_is_active(page, 'group'):
            return False, 'group_filter_inactive'
        anchor = await _anchor_handle(chat_input)
        result = await page.evaluate(_VERIFY_OPEN, {'boundId': str(bound_id or ''), 'input': anchor})
    except Exception as exc:
        return False, f'group_identity_unavailable: {type(exc).__name__}'
    if not isinstance(result, dict):
        return False, 'group_identity_unavailable'
    return result.get('ok') is True, str(result.get('reason') or '')
