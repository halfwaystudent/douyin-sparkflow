"""Shared read-only adapter for the new IM SaaS chat page.

The creator chat page ships two list implementations:

* the legacy ``#sub-app`` semi-list (rows ``li.semi-list-item`` with
  ``item-header-name-*`` nodes and ``朋友私信``/``群消息`` role tabs), and
* the new IM SaaS micro-frontend (``im-saas-viewport-*`` shell, rows marked
  ``[data-e2e="conversation-item"]``, filter tags ``全部``/``粉丝``/``互关``/
  ``群聊`` under ``消息`` plus a separate ``陌生人消息`` group).

This module owns only DOM/policy facts shared by the friend and group
refreshers. It must not import ``core.tasks``, ``core.friends`` or
``core.groups`` so every consumer can import it without cycles.

Selectors and labels below were verified against the public bundles
``__federation_expose_default_export.aab99327.js`` and
``pages-chat-entry.73e609cd.js``:

* row: ``div[data-e2e="conversation-item"]``
  (``conversationConversationItemwrapper``)
* row title: ``.conversationConversationItemtitle`` rendered by
  ``TextKeepSpaces`` inside ``conversationConversationItemtitleWrapper``
* scroll container: ``.conversationConversationListwrapper``
  (``overflow-y: scroll``) — the virtual list renders ``data-index`` item
  wrappers inside it
* filter tags: ``div.conversationFilterTagBartagItem`` with an
  ``...tagItemActive`` sibling class for the selected tag; labels come from
  ``saasApi.getFilterTags()`` (``消息`` group: 全部/粉丝/互关/群聊;
  ``陌生人消息`` group: 全部/关注我的)
"""

import asyncio
from urllib.parse import urlsplit

# Row + title selectors, verified in the public bundle.
SAAS_ROW_SELECTOR = '#sub-app [data-e2e="conversation-item"]'
SAAS_NAME_SELECTOR = ".conversationConversationItemtitle"
SAAS_SCROLL_SELECTORS = (
    ".conversationConversationListwrapper",
    ".conversationStrangerConversationListlistWrapper",
    ".conversationFoldConversationListlistWrapper",
)

# Filter bar markers verified in the bundle/CSS. The viewport class is a
# CSS-module hash (``im-saas-viewport-C7YJGz``), so match on the prefix.
SAAS_VIEWPORT_MARKER = '[class*="im-saas-viewport"]'
SAAS_FILTER_BAR_SELECTOR = ".conversationFilterTagBarwrapper"
SAAS_FILTER_TAG_SELECTOR = ".conversationFilterTagBartagItem"
SAAS_FILTER_TAG_ACTIVE_SELECTOR = ".conversationFilterTagBartagItemActive"
SAAS_FILTER_TAG_LABEL_SELECTOR = ".conversationFilterTagBartagItemLabel"

# kind -> (group label, tag label). "friend" intentionally maps to the
# conversation group's "全部" tag: the SaaS filter has no dedicated friend
# tag, and 全部 still excludes the stranger box. "group" maps to 群聊,
# which is the only tag that also surfaces the fold box.
SAAS_FILTER_TAG_LABELS = {
    "friend": "全部",
    "group": "群聊",
    "stranger": "陌生人消息",
}
SAAS_FILTER_KINDS = frozenset({"friend", "group"})


async def deny_refresh_socket(socket):
    """A read-only list scan must not reconnect queued outbound socket writes."""
    await socket.close()


async def is_saas_ui(page) -> bool:
    """True when the new IM SaaS shell is present on the page.

    Only reads the DOM; never clicks or navigates. A broken read propagates:
    reporting ``False`` on a page we could not inspect would silently route a
    SaaS page into the legacy XPath collectors.
    """
    viewport = page.locator(SAAS_VIEWPORT_MARKER)
    if await viewport.count() == 0:
        return False
    return await viewport.first.is_visible()


async def wait_for_saas_ui(page, *, timeout_ms=30000, legacy_probe=None, poll_seconds=0.5):
    """Poll for the SaaS shell until it appears, the legacy UI proves itself,
    or the budget runs out.

    The SaaS micro-frontend mounts asynchronously after ``#sub-app`` exists,
    so a single instant probe right after navigation can miss it and strand
    the refresh on the legacy path. ``legacy_probe`` is an optional async
    callable returning True once the legacy list UI is confirmed; when it
    fires first we return ``False`` immediately instead of burning the whole
    SaaS budget. Read errors propagate rather than classifying an unreadable
    page as legacy. A timeout without either positive marker must be rejected
    by the caller, never used as permission to click a guessed old XPath.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_ms / 1000
    while True:
        if await is_saas_ui(page):
            return True
        if legacy_probe is not None and await legacy_probe():
            return False
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(poll_seconds)


async def _filter_tag_for_kind(page, kind):
    """Return ``(group_label, tag_label)`` for a refresh kind.

    Raises ``ValueError`` for kinds outside ``SAAS_FILTER_KINDS``.
    """
    if kind not in SAAS_FILTER_KINDS:
        raise ValueError(f"unknown saas filter kind: {kind!r}")
    return "消息", SAAS_FILTER_TAG_LABELS[kind]


async def saas_filter_is_active(page, kind) -> bool:
    """Read-only check: is the SaaS filter for ``kind`` currently selected?

    * ``False`` on the legacy UI or when the SaaS filter bar is absent.
    * ``True`` only when the matching tag item carries the verified
      ``...tagItemActive`` class.
    * Read errors propagate — a broken read must never be reported as an
      active (or inactive) filter.
    """
    if kind not in SAAS_FILTER_KINDS:
        raise ValueError(f"unknown saas filter kind: {kind!r}")
    if not await is_saas_ui(page):
        return False
    bar = page.locator(SAAS_FILTER_BAR_SELECTOR)
    if await bar.count() == 0:
        return False
    group_label, tag_label = await _filter_tag_for_kind(page, kind)

    # Scope to the group whose header label matches (消息 vs 陌生人消息),
    # then to the tag item with the matching label text.
    groups = bar.locator(".conversationFilterTagBargroup")
    count = await groups.count()
    for index in range(count):
        group = groups.nth(index)
        header = group.locator(".conversationFilterTagBargroupLabel").first
        try:
            if (await header.inner_text()).strip() != group_label:
                continue
        except Exception:
            continue
        tags = group.locator(SAAS_FILTER_TAG_SELECTOR)
        tag_count = await tags.count()
        for tag_index in range(tag_count):
            tag = tags.nth(tag_index)
            label_node = tag.locator(SAAS_FILTER_TAG_LABEL_SELECTOR).first
            try:
                if (await label_node.inner_text()).strip() != tag_label:
                    continue
            except Exception:
                continue
            classes = (await tag.get_attribute("class") or "").split()
            if SAAS_FILTER_TAG_ACTIVE_SELECTOR.lstrip(".") in classes:
                return True
    return False


async def activate_saas_filter(page, kind, *, timeout_ms=10000) -> bool:
    """Activate the SaaS filter for ``kind`` and verify it took effect.

    Returns ``False`` only when the page is not the SaaS UI at all (caller
    falls back to the legacy path). When the SaaS UI is present but the
    filter cannot be found, clicked, or confirmed active, raises — never
    silently degrade to the legacy XPath on a page that is definitely new.
    """
    if kind not in SAAS_FILTER_KINDS:
        raise ValueError(f"unknown saas filter kind: {kind!r}")
    if not await is_saas_ui(page):
        return False
    group_label, tag_label = await _filter_tag_for_kind(page, kind)

    bar = page.locator(SAAS_FILTER_BAR_SELECTOR)
    try:
        await bar.first.wait_for(state="visible", timeout=timeout_ms)
    except Exception as exc:
        raise RuntimeError("新版 IM 筛选栏未出现，无法确认列表筛选状态") from exc

    groups = bar.locator(".conversationFilterTagBargroup")
    group_count = await groups.count()
    target = None
    for index in range(group_count):
        group = groups.nth(index)
        header = group.locator(".conversationFilterTagBargroupLabel").first
        try:
            if (await header.inner_text()).strip() != group_label:
                continue
        except Exception:
            continue
        tags = group.locator(SAAS_FILTER_TAG_SELECTOR)
        for tag_index in range(await tags.count()):
            tag = tags.nth(tag_index)
            label_node = tag.locator(SAAS_FILTER_TAG_LABEL_SELECTOR).first
            try:
                if (await label_node.inner_text()).strip() == tag_label:
                    target = tag
                    break
            except Exception:
                continue
        if target is not None:
            break
    if target is None:
        raise RuntimeError(
            f"新版 IM 筛选标签未找到: group={group_label!r} tag={tag_label!r}"
        )

    try:
        classes = (await target.get_attribute("class") or "").split()
        if SAAS_FILTER_TAG_ACTIVE_SELECTOR.lstrip(".") in classes:
            return True
        await target.click(timeout=timeout_ms)
    except Exception as exc:
        raise RuntimeError(f"新版 IM 筛选标签点击失败: {tag_label!r}") from exc

    # Verify the tag actually became active; a click that did not change the
    # filter state must not be reported as success. Poll briefly since the
    # store update is asynchronous.
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while asyncio.get_running_loop().time() < deadline:
        if await saas_filter_is_active(page, kind):
            return True
        await asyncio.sleep(0.1)
    raise RuntimeError(
        f"新版 IM 筛选标签点击后未生效: group={group_label!r} tag={tag_label!r}"
    )


# ---------------------------------------------------------------------------
# Read-only request policy for the SaaS page load.
#
# Same contract as ``core.groups.refresh_request_allowed``: GET/HEAD/OPTIONS
# plus only the exact POSTs the page provably needs to render its lists.
# Everything else — including every write — is refused.
# ---------------------------------------------------------------------------

_SAAS_POST_ALLOWLIST = {
    # AB / feature-gate config fetched while the SaaS shell boots.
    ("abtestvm.bytedance.com", "/service/2/abtest_config"),
    ("creator.amemv.com", "/api/v1/workspace/feature_gates/values"),
    # IM user state reads the chat page performs on load.
    ("www.douyin.com", "/aweme/v1/web/im/user/info"),
    ("www.douyin.com", "/aweme/v1/web/im/user/active/status"),
}


def saas_request_allowed(method, url) -> bool:
    """Read-only policy for the SaaS refresh: safe methods plus the exact
    allowlisted POSTs above. Unknown POSTs are always refused."""
    normalized = str(method or "").upper()
    if normalized in {"GET", "HEAD", "OPTIONS"}:
        return True
    if normalized != "POST":
        return False
    parts = urlsplit(url)
    return (
        (parts.hostname or "").lower(),
        parts.path.rstrip("/"),
    ) in _SAAS_POST_ALLOWLIST
