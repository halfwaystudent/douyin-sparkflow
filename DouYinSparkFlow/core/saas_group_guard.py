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
* ``read_row_metadata(row)`` -> ``{'type', 'name', ...}`` or ``None``.
* ``select_row(row, bound_id)`` -> ``'clicked'`` or a reason string.
* ``verify_open(page, chat_input, bound_id, *, expected_name=None)`` -> ``(bool, reason)``.
* ``arm_enter(page, chat_input, bound_id)`` -> ``(bool, reason)``.
* ``dispatch_was_allowed(page)`` -> ``bool`` (consumes the single shot).
* ``disarm(page)`` -> ``None``.

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

# Synchronous open-pane proof shared by ``verify_open`` and the armed Enter
# guard. The keydown handler calls ``groupOpenCheck`` inside the capture
# phase, so every check re-derives the committed current fiber graph and no
# await ever separates proof from dispatch.
_GROUP_OPEN_CHECK = r"""
const saasGroupFilterActive = () => {
  const viewport = document.querySelector('[class*="im-saas-viewport"]');
  if (!viewport || !visible(viewport)) return false;
  const bar = document.querySelector('.conversationFilterTagBarwrapper');
  if (!bar) return false;
  for (const group of bar.querySelectorAll('.conversationFilterTagBargroup')) {
    const header = group.querySelector('.conversationFilterTagBargroupLabel');
    if (!header || normalizeName(header.textContent) !== '消息') continue;
    for (const tag of group.querySelectorAll('.conversationFilterTagBartagItem')) {
      const label = tag.querySelector('.conversationFilterTagBartagItemLabel');
      if (label && normalizeName(label.textContent) === '群聊')
        return tag.classList.contains('conversationFilterTagBartagItemActive');
    }
  }
  return false;
};
const groupHeaderTitleMatches = (title, rowName, rowConv, headerConv) => {
  if (title === rowName) return true;
  // Bundle aab99327 module 94064: list calls formatName(conv, false),
  // header calls formatName(conv, true) and appends (participantCount).
  // Reconstruct the exact decoration from the verified model; never strip a
  // suffix from arbitrary DOM text (parentheses can be part of a room name).
  const rowModelName = rowConv.coreInfo?.name;
  const headerModelName = headerConv.coreInfo?.name;
  const count = headerConv.participantCount;
  return typeof rowModelName === 'string' && typeof headerModelName === 'string'
    && normalizeName(rowModelName) === rowName
    && normalizeName(headerModelName) === rowName
    && Number.isSafeInteger(count) && count > 0
    && title === normalizeName(`${headerModelName}(${count})`);
};
const groupOpenCheck = cfg => {
  // The armed keydown closure survives React commits: never reuse a fiber
  // graph indexed by an earlier check.
  currentRootIndexes = new WeakMap();
  const bound = String(cfg.boundId || '');
  if (!/^\d{10,25}$/.test(bound)) return {ok:false, reason:'group_bound_id_invalid'};
  const inputNode = cfg.input || null;
  const requireInput = cfg.requireInput === true;
  // The read-only 群聊 filter must still be the selected tag.
  if (!saasGroupFilterActive()) return {ok:false, reason:'group_filter_inactive'};
  const rows = saasRows();
  const active = rows.filter(row => row.classList.contains('conversationConversationItemcurConversation'));
  if (active.length !== 1) return {ok:false, reason: active.length ? 'group_active_row_ambiguous' : 'group_active_row_missing'};
  const activeRow = active[0];
  const rowFound = rowConversation(activeRow);
  if (!rowFound.committed) return {ok:false, reason:'group_row_uncommitted'};
  if (!groupConversationOk(rowFound.conversation, bound)) return {ok:false, reason:'group_active_identity_mismatch'};
  const title = rowTitle(activeRow);
  if (!title) return {ok:false, reason:'group_row_title_unavailable'};
  if (cfg.expectedName != null && (typeof cfg.expectedName !== 'string'
      || title !== normalizeName(cfg.expectedName))) return {ok:false, reason:'group_row_title_changed'};
  // select_row refuses activation unless exactly one visible row claims the
  // bound id; verify and the armed Enter check hold the same bar.
  let claimants = 0;
  for (const other of rows) {
    const o = rowConversation(other);
    if (o.committed && groupConversationOk(o.conversation, bound)) claimants++;
  }
  if (claimants !== 1) return {ok:false, reason:'group_row_ambiguous'};
  // Header: unique visible convHeader with a single visible title.
  const headers = Array.from(document.querySelectorAll('#sub-app .RightPanelHeaderconvHeader')).filter(visible);
  if (headers.length !== 1) return {ok:false, reason:'group_header_ambiguous'};
  const titles = Array.from(headers[0].querySelectorAll('.RightPanelHeadertitle')).filter(visible);
  if (titles.length !== 1) return {ok:false, reason:'group_header_title_mismatch'};
  // Header pane proof: nearest committed curConversation on the header's own
  // current path must be the bound group conversation object.
  const headerState = committedFiber(headers[0]);
  if (headerState.rootKnown && !headerState.committed) return {ok:false, reason:'group_header_uncommitted'};
  const headerProvider = headerState.committed ? nearestSaasConversation(headerState) : null;
  if (!headerProvider) return {ok:false, reason:'group_header_conversation_unavailable'};
  const headerConv = headerProvider.memoizedProps.curConversation;
  if (!groupConversationOk(headerConv, bound)) return {ok:false, reason:'group_header_conversation_mismatch'};
  if (!groupHeaderTitleMatches(normalizeName(titles[0].textContent), title, rowFound.conversation, headerConv))
    return {ok:false, reason:'group_header_title_mismatch'};
  if (!inputNode)
    return requireInput ? {ok:false, reason:'group_input_node_unavailable'} : {ok:true, reason:'header_only'};
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
  return {ok:true, reason:'', input: inputNode};
};
"""

_VERIFY_OPEN = "cfg=>{" + _COMMON + _ROW_CONVERSATION + _GROUP_OPEN_CHECK + r"""
const result = groupOpenCheck(cfg);
return {ok:result.ok === true, reason:result.reason || ''};
}"""

# Single-shot Enter guard for bound SaaS group sends. Arming re-runs the full
# open-pane proof synchronously and pins the authorized editor; the capture
# keydown handler then re-runs the same proof inside the event dispatch, so a
# conversation switch, filter change, duplicate claimant, replaced input, or
# wrong event target between arm and Enter is blocked instead of sent.
_ARM_ENTER = "cfg=>{" + _COMMON + _ROW_CONVERSATION + _GROUP_OPEN_CHECK + r"""
currentRootIndexes = new WeakMap();
const bound = String(cfg.boundId || '');
const previous = window.__sparkflowGroupGuard;
if (previous && previous.handler) window.removeEventListener('keydown', previous.handler, true);
const armed = groupOpenCheck({boundId: bound, input: cfg.input || null, requireInput: true});
if (!armed.ok) return {ok:false, reason: armed.reason || 'group_arm_failed'};
const pinned = armed.input;
if (!pinned) return {ok:false, reason:'group_input_node_unavailable'};
const state = {fired:false, allowed:false, reason:'', armed:true, handler:null};
window.__sparkflowGroupGuard = state;
state.handler = event => {
  if (event.key !== 'Enter' || event.shiftKey) return;
  if (state.fired) {
    // Single shot already spent: every later Enter stays blocked until the
    // record is consumed or the guard is explicitly disarmed.
    event.preventDefault();
    event.stopImmediatePropagation();
    return;
  }
  let allowed = false;
  let reason = 'group_identity_unavailable';
  try {
    const target = event.target;
    const inputs = Array.from(document.querySelectorAll(SAAS_INPUT_SEL)).filter(visible);
    const onEditor = !!(target && pinned
      && (target === pinned || (typeof pinned.contains === 'function' && pinned.contains(target))));
    if (!onEditor || inputs.length !== 1 || inputs[0] !== pinned || !visible(pinned)) {
      reason = 'group_event_input_mismatch';
    } else {
      const current = groupOpenCheck({boundId: bound, input: pinned, requireInput: true});
      allowed = current.ok === true && event.isComposing !== true;
      reason = current.reason || (event.isComposing === true ? 'group_input_composing' : '');
    }
  } catch (err) {
    allowed = false;
    reason = 'group_identity_unavailable';
  }
  state.fired = true;
  state.allowed = allowed;
  state.reason = reason;
  if (!allowed) {
    event.preventDefault();
    event.stopImmediatePropagation();
  }
};
window.addEventListener('keydown', state.handler, true);
return {ok:true, reason:''};
}"""

# Atomic committed-fiber metadata read: conversation type, display name, and —
# only for a provable group — the bound-format shortId. Known conversation
# types verified in the public bundle (150.b12e3b59.js ConversationType):
# 1 ONE_TO_ONE_CHAT, 2 GROUP_CHAT, 3 LIVE_CHAT, 4 BROADCAST_CHAT,
# 17 AI_DOUBLE_CHAT. Only type 2 carries a group id; the thread-namespace
# ``id`` is never reported as a conversation id. Unknown/missing types read
# as null so callers fail closed instead of skipping a real row.
_READ_ROW_METADATA = "(row)=>{" + _COMMON + _ROW_CONVERSATION + r"""
currentRootIndexes = new WeakMap();
if(!row || !row.matches || !row.matches('[data-e2e="conversation-item"]') || !visible(row)) return null;
const found = rowConversation(row);
if(!found.committed || !found.conversation || typeof found.conversation !== 'object') return null;
const c = found.conversation;
const name = rowTitle(row);
if (c.type === 1 || c.type === 3 || c.type === 4 || c.type === 17) return {type:c.type, name:name};
if (c.type === 2) {
  if (typeof c.shortId !== 'string' || !/^[0-9]{10,25}$/.test(c.shortId)) return null;
  return {type:2, name:name, conversationId:c.shortId};
}
return null;
}"""


async def read_row_snapshot(row):
    """Read group id and its displayed name atomically from the current tree."""
    script = "(row)=>{" + _COMMON + _ROW_CONVERSATION + r"""
      currentRootIndexes = new WeakMap();
      if(!row?.matches('[data-e2e="conversation-item"]') || !visible(row))return null;
      const found=rowConversation(row);
      if(!found.committed || !groupConversationOk(found.conversation))return null;
      const title=rowTitle(row);if(!title)return null;
      return {conversationId:found.conversation.shortId,name:title};
    }"""
    try:
        result = await row.evaluate(script)
    except Exception:
        return None
    return result if isinstance(result, dict) else None


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


async def read_row_metadata(row):
    """Atomically read a row's committed conversation type, name and group id.

    One synchronous evaluate walks the row's committed current fiber path
    (<=3 hops) and reads ``memoizedProps.conversation`` plus the displayed
    title, so type, name and id can never be paired across a recycle:

    * committed known type (1 ONE_TO_ONE, 3 LIVE, 4 BROADCAST, 17
      AI_DOUBLE — verified in the public bundle's ``ConversationType``
      enum) -> ``{'type': <n>, 'name': name}`` with no conversation id;
    * committed ``type === 2`` GROUP_CHAT with a valid numeric ``shortId`` ->
      ``{'type': 2, 'name': name, 'conversationId': shortId}``;
    * anything else — unknown/missing type (e.g. 999), uncommitted or
      detached fiber, unreadable row, invalid group id — -> ``None``.

    Known non-group types are reported so callers can *skip* them provably;
    only type 1 may feed friend caches and only type 2 group lookups. The
    thread-namespace ``id`` is never reported as ``conversationId``.
    """
    try:
        result = await row.evaluate(_READ_ROW_METADATA)
    except Exception:
        return None
    if not isinstance(result, dict) or not isinstance(result.get('name'), str):
        return None
    name = result['name']
    kind = result.get('type')
    if kind in (1, 3, 4, 17):
        return {'type': kind, 'name': name}
    if kind == 2:
        cid = result.get('conversationId')
        if isinstance(cid, str) and cid.isdigit() and 10 <= len(cid) <= 25:
            return {'type': 2, 'name': name, 'conversationId': cid}
    return None


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


async def verify_open(page, chat_input, bound_id, *, expected_name=None):
    """Prove the opened pane is still the bound group conversation.

    Requires the read-only 群聊 filter to be active, exactly one visible
    ``curConversation`` row matching ``bound_id``, a unique convHeader whose
    title matches the row title (or the exact model-derived member count
    decoration), and — when ``chat_input`` is supplied — the
    single visible SaaS editor resolving to the *same* committed
    ``curConversation`` object as the header. Header-only mode (``None``)
    proves just the header conversation and is only acceptable before an
    input exists. ``expected_name`` optionally pins the name observed in the
    selection scan, so a stale label cannot be used for a rename notice.
    """
    try:
        if not await im_ui.saas_filter_is_active(page, 'group'):
            return False, 'group_filter_inactive'
        anchor = await _anchor_handle(chat_input)
        if chat_input is not None and anchor is None:
            return False, 'group_input_node_unavailable'
        result = await page.evaluate(_VERIFY_OPEN, {
            'boundId': str(bound_id or ''), 'input': anchor, 'expectedName': expected_name,
        })
    except Exception as exc:
        return False, f'group_identity_unavailable: {type(exc).__name__}'
    if not isinstance(result, dict):
        return False, 'group_identity_unavailable'
    return result.get('ok') is True, str(result.get('reason') or '')


async def arm_enter(page, chat_input, bound_id):
    """Arm the single-shot Enter guard for a bound SaaS group send.

    Runs the full open-pane proof synchronously (active row, unique bound
    claimant, header/input same committed conversation, 群聊 filter) and, only
    when it passes, installs a capture-phase ``keydown`` listener pinned to the
    authorized editor. The listener re-runs the same proof inside the event
    dispatch: a conversation switch, filter change, duplicate claimant,
    replaced input, or wrong event target blocks the Enter instead of sending.

    Returns ``(True, '')`` when armed; ``(False, reason)`` otherwise. At most
    one Enter is authorized — see ``dispatch_was_allowed``/``disarm``.
    """
    try:
        anchor = await _anchor_handle(chat_input)
        if chat_input is not None and anchor is None:
            return False, 'group_input_node_unavailable'
        result = await page.evaluate(
            _ARM_ENTER,
            {'boundId': str(bound_id or ''), 'input': anchor, 'requireInput': True},
        )
    except Exception as exc:
        return False, f'group_arm_unavailable: {type(exc).__name__}'
    if not isinstance(result, dict):
        return False, 'group_arm_unavailable'
    return result.get('ok') is True, str(result.get('reason') or '')


async def dispatch_was_allowed(page):
    """Consume the armed Enter record; True only for the one allowed shot.

    Removes the capture listener and deletes the guard state, so a stale
    "allowed" can never authorize a later Enter. An Enter that never fired —
    or one already consumed — reports False.
    """
    state = await page.evaluate(
        "() => { const s = window.__sparkflowGroupGuard;"
        " if (s && s.handler) window.removeEventListener('keydown', s.handler, true);"
        " const out = {present: !!s, fired: s ? s.fired === true : false,"
        "   allowed: s ? s.allowed === true : false,"
        "   armed: s ? s.armed === true : false, reason: s ? s.reason || '' : ''};"
        " delete window.__sparkflowGroupGuard; return out; }"
    )
    return (isinstance(state, dict) and state.get('present') is True
            and state.get('fired') is True and state.get('allowed') is True
            and state.get('armed') is True)


async def disarm(page):
    """Remove the armed Enter guard without authorizing anything."""
    await page.evaluate(
        "() => { const s = window.__sparkflowGroupGuard;"
        " if (s && s.handler) window.removeEventListener('keydown', s.handler, true);"
        " delete window.__sparkflowGroupGuard; }"
    )
