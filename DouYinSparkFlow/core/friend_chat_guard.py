"""Conservative friend-chat identity checks. No name-only send authorization.

Only explicit DOM identity fields or shallow per-conversation React props are
accepted. Shared list/context membership is never proof of the opened chat.
Compatibility with a new live page shape must be verified read-only before
release: unreadable identities intentionally stop sending.
"""
import os
from pathlib import Path
from weakref import WeakKeyDictionary
from core import streak_state

_SELECTIONS = WeakKeyDictionary()

_GUARD_ON = {"1", "on", "true", "enforce", "yes"}
_GUARD_MARKER = Path(__file__).resolve().parents[1] / 'logs/friend-guard-on.flag'


def guard_enabled():
    """Use the same explicit opt-in for cron, personal and Web senders.

    A process-local explicit environment setting retains its old precedence.
    Without one, resolve the already-approved marker relative to application
    source, not the caller's cwd. No shared environment is changed.
    """
    raw = str(os.getenv("SPARKFLOW_FRIEND_GUARD") or "").strip().lower()
    if raw:
        return raw in _GUARD_ON
    try:
        return _GUARD_MARKER.is_file()
    except OSError:
        # An unreadable control file must never remove an identity fence.
        return True


class FriendIdentityError(RuntimeError):
    category = 'friend_identity_unavailable'

    def __init__(self, message, category=None):
        super().__init__(message)
        self.category = category or _category_for(str(message))


class FriendBindingIncompleteError(FriendIdentityError):
    """A target-local gap when selecting, a hard stop during verification."""


# A known identity that contradicts the target is an incident; a page we cannot
# read is an outage. Both stop sending, but operators must be able to tell them
# apart without reading the page.
_MISMATCH_REASONS = {
    'friend_active_identity_mismatch',
    'friend_row_identity_mismatch',
    'friend_row_name_mismatch',
    'friend_conversation_changed',
    'friend_current_conversation_mismatch',
    'friend_open_identity_mismatch',
    'friend_open_conversation_mismatch',
    'friend_open_identity_conflict',
    'friend_stale_input_context',
    'friend_pane_scope_mismatch',
    'friend_event_input_mismatch',
    'friend_header_name_mismatch',
}


def _category_for(message):
    for reason in _MISMATCH_REASONS:
        if reason in message:
            return 'friend_identity_mismatch'
    return 'friend_identity_unavailable'


_COMMON = r"""
const visible = node => {
  if (!node || !node.isConnected) return false;
  const style=getComputedStyle(node), r=node.getBoundingClientRect();
  return style.display!=='none' && style.visibility!=='hidden' && r.width>0 && r.height>0;
};
const nameOf = row => row.querySelector('span[class*="item-header-name-"]')?.textContent?.trim() || '';
const rawFiber = node => {const key=Object.keys(node || {}).find(k=>k.startsWith('__reactFiber'));return key && node[key];};
// These caches exist only during this synchronous evaluate/keydown check.
// A DOM expando and even a current host's return links may point at alternate
// ancestors. Only child/sibling reachability from root.current proves ancestry.
let currentRootIndexes = new WeakMap();
const indexCurrentRoot = root => {
  if(currentRootIndexes.has(root))return currentRootIndexes.get(root);
  const index={parents:new Map(),hosts:new Map(),valid:true};
  const stack=[[root,null]];
  while(stack.length){
    const [fiber,parent]=stack.pop();
    if(!fiber)continue;
    if(index.parents.has(fiber)||index.parents.size>=30000){index.valid=false;break;}
    index.parents.set(fiber,parent);
    const node=fiber.stateNode;
    if(node && typeof node.nodeType==='number'){
      if(index.hosts.has(node))index.valid=false;
      index.hosts.set(node,fiber);
    }
    if(fiber.sibling){
      if(!parent){index.valid=false;break;}
      stack.push([fiber.sibling,parent]);
    }
    if(fiber.child)stack.push([fiber.child,fiber]);
  }
  currentRootIndexes.set(root,index);
  return index;
};
const committedFiber = node => {
  const own=rawFiber(node);
  if(!own)return {fiber:null,path:[],committed:false,rootKnown:false};
  let top=own,depth=0;
  while(top.return && depth++<200)top=top.return;
  const current=top.stateNode?.current;
  if(!current)return {fiber:own,path:null,committed:false,rootKnown:false};
  const index=indexCurrentRoot(current),fiber=index.hosts.get(node);
  if(!index.valid || !fiber)return {fiber:null,path:[],committed:false,rootKnown:true};
  const path=[];
  for(let f=fiber;f && path.length<200;f=index.parents.get(f))path.push(f);
  if(path[path.length-1]!==current)return {fiber:null,path:[],committed:false,rootKnown:true};
  return {fiber,path,committed:true,rootKnown:true};
};
const identityBag = () => ({peer:new Set(),sec:new Set(),conversation:new Set(),thread:new Set()});
const addIdentity = (bag,kind,value) => {
  if(typeof value!=='string' || !value || value!==value.trim())return;
  const valid=kind==='conversation' ? /^\d{10,25}$/.test(value)
    : kind==='thread' ? /^\d+(?::\d+){3}$/.test(value)
    : /^[A-Za-z0-9_-]{1,160}$/.test(value);
  if(valid)bag[kind].add(value);
};
const addConversation = (bag,c) => {
  if(!c || typeof c!=='object')return;
  // One value per bag: id and shortId are different identifier kinds, so they
  // must never collide inside the same set.
  addIdentity(bag,'thread',c.id);
  if(c.shortId)addIdentity(bag,'conversation',c.shortId);else addIdentity(bag,'conversation',c.id);
  addIdentity(bag,'peer',c.peerUserId);addIdentity(bag,'peer',c.peer_user_id);
  addIdentity(bag,'sec',c.secUid);addIdentity(bag,'sec',c.sec_uid);
};
const unpack = bag => Object.fromEntries(Object.entries(bag).map(([k,v])=>[k,Array.from(v)]));
const conversationIdentity = c => {const bag=identityBag();addConversation(bag,c);return unpack(bag);};
const read = (node,levels=3) => {
  const bag=identityBag();
  addIdentity(bag,'peer',node?.getAttribute('data-user-id'));
  addIdentity(bag,'peer',node?.getAttribute('data-peer-user-id'));
  addIdentity(bag,'sec',node?.getAttribute('data-sec-uid'));
  addIdentity(bag,'conversation',node?.getAttribute('data-conversation-id'));
  const state=committedFiber(node);
  let fiber=(state.rootKnown && !state.committed) ? null : state.fiber;
  for(let level=0;fiber && level<=levels;level++){
    const props=fiber.memoizedProps || {};
    addConversation(bag,props.header?.props?.conversation || props.conversation);
    fiber=state.committed ? state.path[level+1] : fiber.return;
  }
  return unpack(bag);
};
const consistent = identity => Object.values(identity).every(values=>values.length<=1);
const matches = (identity,expected) => {
  if(!consistent(identity))return false;
  let matched=false;
  for(const kind of ['peer','sec']){
    if(!expected[kind] || !identity[kind].length)continue;
    if(identity[kind][0]!==expected[kind])return false;
    matched=true;
  }
  return matched;
};
// The live page exposed the current conversation through a context provider
// 6 (header) and 9 (input) hops above the DOM node. Match on the value shape
// rather than a React internal tag so an upgrade cannot silently block every
// chat; both nodes must still resolve to the SAME fiber.
const nearestChatProvider = state => {
  const path=state.path || [];
  for(let depth=0;depth<path.length && depth<=14;depth++){
    const fiber=path[depth],value=fiber.memoizedProps?.value;
    if(value && typeof value==='object' && value.conversation && typeof value.conversation==='object')return fiber;
  }
  return null;
};
const sameConversation = (identity,cfg) => consistent(identity)
  && (!cfg.conversation || identity.conversation[0]===cfg.conversation)
  && (!cfg.thread || identity.thread[0]===cfg.thread);
const scopedPaneIdentity = (input,header,cfg) => {
  const i=committedFiber(input),h=committedFiber(header);
  if((i.rootKnown && !i.committed)||(h.rootKnown && !h.committed))return {present:true,ok:false,reason:'friend_uncommitted_pane'};
  const ip=nearestChatProvider(i),hp=nearestChatProvider(h);
  if(!ip && !hp)return {present:false,ok:false};
  if(!i.committed || !h.committed || !ip || ip!==hp)return {present:true,ok:false,reason:'friend_pane_scope_mismatch'};
  // This is the observed singular current-conversation object, never allList
  // membership or an arbitrary ancestor's selected-conversation prop.
  const value=ip.memoizedProps.value;
  const identity=conversationIdentity(value.conversation);
  if((!cfg.conversation && !cfg.thread)||!sameConversation(identity,cfg)||!matches(identity,cfg.expected))return {present:true,ok:false,reason:'friend_current_conversation_mismatch'};
  const context=ip.type?._context || ip.type;
  for(const state of [i,h]){
    for(const fiber of state.path){
      if(fiber===ip)break;
      let dependency=fiber.dependencies?.firstContext;
      for(let count=0;dependency && count<12;count++,dependency=dependency.next){
        if(context && (dependency.context===context || dependency.context===ip.type) && dependency.memoizedValue?.conversation){
          const consumer=conversationIdentity(dependency.memoizedValue.conversation);
          if(!sameConversation(consumer,cfg)||!matches(consumer,cfg.expected))return {present:true,ok:false,reason:'friend_stale_input_context'};
        }
      }
    }
  }
  return {present:true,ok:true};
};
const check = cfg => {
  // The armed keydown closure survives React commits. Never reuse the graph
  // indexed while arming, even if root.current cycles back to the same object.
  currentRootIndexes = new WeakMap();
  const rows=Array.from(document.querySelectorAll('#sub-app li.semi-list-item')).filter(visible);
  const named=rows.filter(row=>nameOf(row)===cfg.name);
  if(named.length!==1)return {ok:false,reason:'friend_identity_ambiguous'};
  const active=rows.filter(row=>Array.from(row.classList).some(k=>k.startsWith('active-')));
  if(active.length!==1 || active[0]!==named[0])return {ok:false,reason:'friend_chat_not_active'};
  const rowState=committedFiber(active[0]);
  if(rowState.rootKnown && !rowState.committed)return {ok:false,reason:'friend_row_uncommitted'};
  const rowIdentity=read(active[0]);
  if(!matches(rowIdentity,cfg.expected))return {ok:false,reason:'friend_active_identity_mismatch'};
  if(!sameConversation(rowIdentity,cfg))return {ok:false,reason:'friend_conversation_changed'};
  const headers=Array.from(document.querySelectorAll('#sub-app div[class*="box-header-"]')).filter(n=>visible(n)
    && Array.from(n.classList).some(c=>c.startsWith('box-header-')&&!c.startsWith('box-header-tail-'))
    && !Array.from(n.classList).some(c=>c.startsWith('box-header-tail-')));
  if(headers.length!==1)return {ok:false,reason:'friend_header_ambiguous'};
  const title=headers[0].cloneNode(true);
  for(const tail of title.querySelectorAll('[class*="box-header-tail-"]'))tail.remove();
  // Only the tail-stripped title may name the chat; searching the live header
  // would accept a member strip or quote that merely mentions the target.
  const observedNames=Array.from(headers[0].querySelectorAll('strong[class*="box-header-"]')).filter(n=>
    Array.from(n.classList).some(c=>c.startsWith('box-header-')&&!c.startsWith('box-header-tail-'))
    && !n.closest('[class*="box-header-tail-"]'));
  let named_title;
  if(observedNames.length){
    // Observed live: STRONG.box-header-* is the name, an adjacent SPAN
    // contains status text. Never let that status/quote override a wrong name.
    const visibleNames=observedNames.filter(visible);
    if(visibleNames.length!==1)return {ok:false,reason:'friend_header_ambiguous'};
    named_title=visibleNames[0].textContent.trim()===cfg.name;
  }else{
    // Retain the previous exact-only layout check for older/simple headers.
    named_title=title.textContent.trim()===cfg.name
      || Array.from(title.querySelectorAll('span,div,h1,h2,h3')).some(n=>n.textContent.trim()===cfg.name);
  }
  if(!named_title)return {ok:false,reason:'friend_header_name_mismatch'};
  const inputs=Array.from(document.querySelectorAll('#sub-app [contenteditable="true"]')).filter(visible);
  if(inputs.length!==1)return {ok:false,reason:'friend_input_ambiguous'};
  const inputIdentity=read(inputs[0],0), headerIdentity=read(headers[0],0);
  for(const identity of [inputIdentity,headerIdentity]){
    if(!consistent(identity))return {ok:false,reason:'friend_open_identity_conflict'};
    for(const kind of ['peer','sec']){
      if(identity[kind].length && cfg.expected[kind] && identity[kind][0]!==cfg.expected[kind])return {ok:false,reason:'friend_open_identity_mismatch'};
    }
    if((identity.conversation.length && cfg.conversation && identity.conversation[0]!==cfg.conversation)
       || (identity.thread.length && cfg.thread && identity.thread[0]!==cfg.thread))return {ok:false,reason:'friend_open_conversation_mismatch'};
  }
  // The observed current pane is mandatory. Input-local attributes/props may
  // contradict it, but must never replace it when the pane is unreadable.
  const pane=scopedPaneIdentity(inputs[0],headers[0],cfg);
  if(!pane.ok)return {ok:false,reason:pane.reason || 'friend_open_identity_unavailable'};
  return {ok:true,reason:'',input:inputs[0]};
};
"""

_SELECT = "(row,cfg)=>{" + _COMMON + r"""
const rows=Array.from(document.querySelectorAll('#sub-app li.semi-list-item')).filter(visible);
if(!visible(row) || !rows.includes(row))return {ok:false,reason:'friend_row_unavailable'};
if(rows.filter(r=>nameOf(r)===cfg.name).length!==1)return {ok:false,reason:'friend_identity_ambiguous'};
if(nameOf(row)!==cfg.name)return {ok:false,reason:'friend_row_name_mismatch'};
const identity=read(row);
if(!matches(identity,cfg.expected))return {ok:false,reason:'friend_row_identity_mismatch'};
const span=row.querySelector('span[class*="item-header-name-"]');
if(!span)return {ok:false,reason:'friend_name_node_unavailable'};
span.click();
return {ok:true,reason:'',conversation:identity.conversation[0] || '',thread:identity.thread[0] || ''};
}"""

_VERIFY = "cfg=>{" + _COMMON + r"""
const result=check(cfg);
if(cfg.arm){
  const old=window.__sparkflowFriendGuard;
  if(old?.handler)window.removeEventListener('keydown',old.handler,true);
  const pinnedInput=result.input;
  const state={fired:false,allowed:false,reason:'',armed:true,consumed:false};
  window.__sparkflowFriendGuard=state;
  if(!result.ok)return result;
  state.handler=event=>{
    if(event.key!=='Enter' || event.shiftKey)return;
    let correctInput=false,current={ok:false,reason:'friend_identity_unavailable'};
    try{
      const inputs=Array.from(document.querySelectorAll('#sub-app [contenteditable="true"]')).filter(visible);
      correctInput=inputs.length===1 && inputs[0]===pinnedInput && visible(pinnedInput)
        && (event.target===pinnedInput || pinnedInput.contains(event.target));
      if(correctInput)current=check(cfg);
    }catch(err){correctInput=false;}
    if(!correctInput){
      // An Enter somewhere else on the page (or an unreadable page): block that
      // keystroke but stay armed, so a stray Enter cannot spend the guard.
      state.reason='friend_event_input_mismatch';
      event.preventDefault();event.stopImmediatePropagation();
      return;
    }
    state.fired=true;
    state.allowed=correctInput && current.ok === true && !event.isComposing;
    state.reason=current.reason || (event.isComposing ? 'friend_input_composing' : '');
    window.removeEventListener('keydown',state.handler,true);
    if(!state.allowed){event.preventDefault();event.stopImmediatePropagation();}
  };
  window.addEventListener('keydown',state.handler,true);
}
return {ok:result.ok,reason:result.reason};
}"""


def _expected(account, target):
    binding = streak_state.target_uid_binding(account, target)
    if not binding or not binding.get('uid'):
        return None
    if not binding.get('secUid'):
        # uid-only matching would silently degrade to a weaker check.
        raise FriendBindingIncompleteError('friend_identity_unavailable: friend_binding_incomplete')
    return {'peer': str(binding['uid']), 'sec': str(binding['secUid'])}


async def select_friend(page, row, account, target):
    try:
        return await _select_friend(page, row, account, target)
    except FriendBindingIncompleteError:
        return False, 'friend_binding_incomplete'
    except FriendIdentityError:
        raise
    except Exception as exc:
        raise FriendIdentityError('friend_identity_unavailable: selection could not be verified') from exc


async def _select_friend(page, row, account, target):
    expected = _expected(account, target)
    _SELECTIONS.pop(page, None)
    if not expected:
        return False, 'friend_binding_unavailable'
    cfg = {'name': str(target).strip(), 'expected': expected}
    result = await row.evaluate(_SELECT, cfg)
    if not isinstance(result, dict) or result.get('ok') is not True:
        reason = result.get('reason') if isinstance(result, dict) else None
        return False, str(reason or 'friend_selection_unavailable')
    cfg['conversation'] = str(result.get('conversation') or '')
    cfg['thread'] = str(result.get('thread') or '')
    _SELECTIONS[page] = cfg
    return True, ''


async def verify_friend(page, account, target, *, arm=False, wait=False):
    expected = _expected(account, target)
    cfg = _SELECTIONS.get(page)
    if not expected or not cfg or cfg['name'] != str(target).strip() or cfg['expected'] != expected:
        return False, 'friend_selection_proof_unavailable'
    cfg = dict(cfg, arm=arm)
    if wait:
        # Let a normal React pane transition finish, but never accept only a
        # nickname or the presence of an old editable input as readiness proof.
        handle = await page.wait_for_function('cfg => (' + _VERIFY + ')(cfg).ok === true', arg=dict(cfg, arm=False), timeout=5000)
        await handle.dispose()
    result = await page.evaluate(_VERIFY, cfg)
    if not isinstance(result, dict):
        return False, 'friend_verification_unavailable'
    return result.get('ok') is True, str(result.get('reason') or '')


async def dispatch_was_allowed(page):
    # Consume the record: a stale "allowed" from a previous send must never
    # authorize a later Enter that was not armed.
    state = await page.evaluate("() => { const s=window.__sparkflowFriendGuard; if(s?.handler)window.removeEventListener('keydown',s.handler,true); delete window.__sparkflowFriendGuard; return {fired:s?.fired===true,allowed:s?.allowed===true,armed:s?.armed===true,reason:s?.reason || ''}; }")
    return (isinstance(state, dict) and state.get('fired') is True
            and state.get('allowed') is True and state.get('armed') is True)


async def disarm(page):
    await page.evaluate("() => {const s=window.__sparkflowFriendGuard;if(s?.handler)window.removeEventListener('keydown',s.handler,true);delete window.__sparkflowFriendGuard;}")
