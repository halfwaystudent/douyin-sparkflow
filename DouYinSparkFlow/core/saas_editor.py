"""Read a known IM SaaS composer without changing its DOM or model."""
from core.friend_chat_guard import _COMMON


_READ = r"""(node, expected) => {""" + _COMMON + r"""
  const scope = '#sub-app [data-e2e="msg-input"] .messageEditorinputArea[contenteditable="true"]';
  const editors = [...document.querySelectorAll(scope)].filter(visible);
  if (editors.length !== 1 || editors[0] !== node) return 'unknown';
  const raw = node.innerText ?? node.textContent ?? '';
  if (typeof raw !== 'string' || raw.length > 4096) return 'unknown';
  const descendants = node.querySelectorAll('*');
  if (descendants.length > 128) return 'unknown';
  for (const element of descendants) {
    if (!['DIV', 'P', 'SPAN', 'BR'].includes(element.tagName) ||
        element.hasAttribute('contenteditable')) return 'occupied';
  }
  const state = committedFiber(node);
  if (!state.committed) return 'unknown';
  const candidates = new Set();
  const see = value => {
    if (value && typeof value === 'object' &&
        typeof value.getRootContainer === 'function' &&
        typeof value.getContentState === 'function') candidates.add(value);
  };
  for (const fiber of state.path.slice(0, 15)) {
    let hook = fiber.memoizedState;
    for (let i = 0; i < 24 && hook && typeof hook === 'object' && 'memoizedState' in hook; i++, hook = hook.next) {
      see(hook.memoizedState);
      if (hook.memoizedState && typeof hook.memoizedState === 'object') see(hook.memoizedState.current);
    }
  }
  try {
    const matches = [...candidates].filter(model => model.getRootContainer() === node);
    if (matches.length !== 1) return 'unknown';
    const model = matches[0].getContentState().getContent();
    if (!(model instanceof Map) || model.size !== 1) return 'unknown';
    // Mirror the audited plain-text send serializer: skip recognized line
    // markers and remove one terminal newline. Reject unknown/rich operations.
    let blocks = 0, operations = 0, payload = '';
    for (const segment of model.values()) for (const block of segment) {
      if (++blocks > 64) return 'unknown';
      const ops = block.getOps();
      if (!Array.isArray(ops)) return 'unknown';
      for (const op of ops) {
        if (++operations > 256 || !op || typeof op.insert !== 'string' || op.insert.length > 4096) return 'occupied';
        const a = op.attributes;
        if (a != null) {
          if (typeof a !== 'object' || Array.isArray(a)) return 'unknown';
          const keys = Object.keys(a);
          if (keys.length) {
            if (keys.length === 1 && keys[0] === 'lmkr' && a.lmkr === '1' && /^[\n\u200B\uFEFF]$/.test(op.insert)) continue;
            return 'occupied';
          }
        }
        payload += op.insert;
        if (payload.length > 4096) return 'unknown';
      }
    }
    if (!blocks || !operations) return 'unknown';
    if (payload.endsWith('\n')) payload = payload.slice(0, -1);
    if (payload === '') return /^[\s\u200B\uFEFF]*$/.test(raw) ? 'empty' : 'occupied';
    if (typeof expected !== 'string' || payload !== expected) return 'occupied';
    // Only discount DOM decorations after the exact model payload matches.
    const sameRendering = text => text.replace(/\r\n?/g, '\n').split('\n').map(line => line.trim()).filter(Boolean).join('\n');
    return (sameRendering(raw) === sameRendering(expected) ||
      sameRendering(raw.replace(/[\u200B\uFEFF]/g, '')) === sameRendering(expected.replace(/[\u200B\uFEFF]/g, '')))
        ? 'expected' : 'occupied';
  } catch (_) {
    return 'unknown';
  }
}"""


async def read_state(editor, expected=None):
    """Return a fixed verdict, never draft or model content."""
    verdict = await editor.evaluate(_READ, expected)
    return verdict if isinstance(verdict, str) and verdict in {'empty', 'expected', 'occupied'} else 'unknown'
