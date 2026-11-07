"""Fail-closed recovery for a completely blank initial creator-chat SPA."""
from urllib.parse import urlsplit

from playwright.async_api import TimeoutError as PlaywrightTimeoutError


CHAT_HOST = "creator.douyin.com"
CHAT_PATH = "/creator-micro/data/following/chat"
# Keep the normal first-render allowance aligned with core.tasks' 30-second
# browser stage. The inner Playwright wait must finish before that outer stage.
PAGE_READINESS_STAGE_TIMEOUT_SECONDS = 30
PAGE_READINESS_RENDER_WAIT_SECONDS = 25


_SNAPSHOT_JS = """() => {
    const body = document.body;
    if (!body) return {blank: true, textLength: 0, visibleControls: 0, bodyPresent: false};
    const visible = (element) => {
        const style = getComputedStyle(element);
        const box = element.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden'
            && style.opacity !== '0' && box.width > 0 && box.height > 0;
    };
    const textLength = (body.innerText || '').trim().length;
    const visibleControls = Array.from(document.querySelectorAll(
        'input, button, img, iframe, canvas, video'
    )).filter(visible).length;
    // An empty rendered list is not an empty SPA. Existing friend-list guards
    // decide whether that list is complete; this helper must not refresh it.
    const listStructures = document.querySelectorAll(
        "ul, ol, [role='list'], [role='listbox'], [class*='list'], [class*='List'], [class*='friend'], [class*='Friend']"
    ).length;
    return {
        blank: textLength === 0 && visibleControls === 0 && listStructures === 0,
        textLength,
        visibleControls,
        listStructures,
        bodyPresent: true,
    };
}"""
_NON_BLANK_JS = f"() => !({_SNAPSHOT_JS})().blank"


def _is_creator_chat_url(url):
    """Accept only the exact production chat origin and path, never a redirect."""
    parsed = urlsplit(str(url or ""))
    return (
        parsed.scheme == "https"
        and parsed.netloc == CHAT_HOST
        and parsed.path == CHAT_PATH
    )


def _inner_wait_seconds():
    # Leave the outer stage a real cancellation margin, including in accelerated
    # offline tests, while retaining the configured normal render allowance.
    margin = min(.5, PAGE_READINESS_STAGE_TIMEOUT_SECONDS * .1)
    return min(PAGE_READINESS_RENDER_WAIT_SECONDS,
               max(.001, PAGE_READINESS_STAGE_TIMEOUT_SECONDS - margin))


async def _read_url(page, tasks):
    async def read():
        return str(page.url or "")

    return await tasks._run_browser_stage(
        "page_readiness_url", read(),
        timeout_seconds=PAGE_READINESS_STAGE_TIMEOUT_SECONDS,
    )


async def _snapshot(page, tasks):
    return await tasks._run_browser_stage(
        "page_readiness_snapshot", page.evaluate(_SNAPSHOT_JS),
        timeout_seconds=PAGE_READINESS_STAGE_TIMEOUT_SECONDS,
    )


async def _wait_for_render(page, tasks):
    return await tasks._run_browser_stage(
        "page_readiness_wait", page.wait_for_function(
            _NON_BLANK_JS, timeout=max(1, round(_inner_wait_seconds() * 1000))
        ),
        timeout_seconds=PAGE_READINESS_STAGE_TIMEOUT_SECONDS,
    )


async def _reload_same_chat_url(page, url, tasks):
    return await tasks._run_browser_stage(
        "page_readiness_reload", page.goto(url=url),
        timeout_seconds=PAGE_READINESS_STAGE_TIMEOUT_SECONDS,
    )


async def ensure_chat_page_ready(page, account_name):
    """Wait once for a blank chat shell, then navigate that same URL at most once.

    The caller owns browser closure, account cooldown, login checks, recipient
    selection, and every send-state decision. This helper intentionally reads no
    chat content and never tries to recover a page that is not a blank chat SPA.
    """
    # Lazy import avoids a core.tasks -> core.page_readiness import cycle.
    from core import tasks

    initial_url = await _read_url(page, tasks)
    if not _is_creator_chat_url(initial_url):
        return

    initial_snapshot = await _snapshot(page, tasks)
    if not initial_snapshot.get("blank"):
        return

    try:
        await _wait_for_render(page, tasks)
        return
    except PlaywrightTimeoutError:
        # A Playwright inner timeout is not enough to refresh. Confirm both the
        # unchanged safe route and the still-blank, readable DOM first.
        reload_url = await _read_url(page, tasks)
        if not _is_creator_chat_url(reload_url):
            return
        before_reload = await _snapshot(page, tasks)
        if not before_reload.get("blank"):
            return

    await _reload_same_chat_url(page, reload_url, tasks)

    try:
        await _wait_for_render(page, tasks)
        return
    except PlaywrightTimeoutError:
        final_snapshot = await _snapshot(page, tasks)
        if not final_snapshot.get("blank"):
            return
        raise tasks.BrowserStageTimeout("page_readiness")
