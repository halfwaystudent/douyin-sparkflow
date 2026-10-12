import asyncio
import logging
import re
import uuid

from rich.console import Console

from core.browser import get_browser
from core.identity import (
    BINDING_NEW,
    FIELD_DISPLAY_ID,
    FIELD_LOGIN_GENERATION,
    AccountChangedConcurrently,
    account_uid,
    accounts_bound_to,
    bind_identity,
    check_expected_records,
    clear_identity_markers,
    clear_login_health,
    display_id_from_text,
    fetch_trusted_identity,
    new_login_generation,
    record_fingerprint,
)
from utils.config import normalize_unique_id, update_user_data, upsert_user_account


logger = logging.getLogger(__name__)

console = Console()

READY_SELECTOR = (
    'xpath=//*[contains(@id, "garfish_app_for_douyin_creator_pc_home")]'
    '/div/div[2]/div/div[2]/div[1]'
)
_CONTAINER_XPATH = (
    '//*[contains(@id, "garfish_app_for_douyin_creator_pc_home")]'
    '/div/div[2]/div/div[2]/div[1]'
)
XPATHS = {
    "unique_id": (
        'xpath=//*[contains(@id, "garfish_app_for_douyin_creator_pc_home")]'
        '/div/div[2]/div/div[2]/div[1]/div[2]/div[1]/div[3]'
    ),
    "name": (
        'xpath=//*[contains(@id, "garfish_app_for_douyin_creator_pc_home")]'
        '/div/div[2]/div/div[2]/div[1]/div[2]/div[1]/div[1]/div[1]'
    ),
}

# The creator centre is a client-rendered SPA: on a cold cookie-only context the
# identity card routinely needs 25-30 seconds to appear, so the card wait is the
# slow part. The two text nodes render together with the card and only need a
# short trailing budget, and the card's own text is a fallback for the nickname so
# that a renamed CSS class cannot turn a valid login into a failure.
IDENTITY_TRAILING_BUDGET_MS = 8000
IDENTITY_POLL_SECONDS = 0.5
DOUYIN_ID_MARKERS = ("抖音号：", "抖音号:")
NICKNAME_FALLBACK_SELECTORS = (
    f'xpath={_CONTAINER_XPATH}//*[contains(@class, "name-")]',
)
LOGIN_FORM_SELECTORS = (
    'text=扫码登录',
    'text=验证码登录',
)
DOUYIN_ID_PATTERN = re.compile(r"[0-9A-Za-z_.\-]+")

# Messages are also the category carriers: ``core.friends.classify_refresh_error``
# maps a raw error by its text, so a page/DOM problem must read like one and a
# logged-out session like a login problem.
LOGIN_REQUIRED_MESSAGE = "账号登录已失效，请重新扫码登录"
CARD_NOT_READY_MESSAGE = "creator identity card did not become ready within timeout"
IDENTITY_UNREADABLE_MESSAGE = (
    "creator identity card was visible but the account identity could not be read; "
    "dom=identity-text-missing"
)


def identity_from_card_text(text):
    """Split the creator identity card's text into (nickname, 抖音号).

    The card renders ``<nickname>抖音号：<handle><signature>...`` as one text blob,
    so the nickname stays readable even when its dedicated node is missing or
    hidden. Returns an empty nickname when the marker is absent, because guessing
    from unrelated text would rename accounts.
    """
    raw = " ".join(str(text or "").split())
    for marker in DOUYIN_ID_MARKERS:
        index = raw.find(marker)
        if index > 0:
            nickname = raw[:index].strip()
            tail = raw[index + len(marker):].strip()
            match = DOUYIN_ID_PATTERN.match(tail)
            return nickname, (match.group(0) if match else tail)
    return "", ""


async def _first_visible_text(page, selectors):
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count() > 0 and await locator.is_visible():
                text = str(await locator.inner_text() or "").strip()
                if text:
                    return text
        except Exception:
            continue
    return ""


async def _login_form_visible(page):
    for selector in LOGIN_FORM_SELECTORS:
        try:
            locator = page.locator(selector).first
            if await locator.count() > 0 and await locator.is_visible():
                return True
        except Exception:
            continue
    return False


async def _wait_for_identity_card(page, timeout_ms):
    deadline = asyncio.get_running_loop().time() + max(0.5, timeout_ms / 1000)
    while True:
        try:
            locator = page.locator(READY_SELECTOR).first
            if await locator.count() > 0 and await locator.is_visible():
                return
        except Exception:
            pass
        # A visible login form is a conclusive answer: waiting out the whole
        # budget on a page that is asking for a QR scan only delays it.
        if await _login_form_visible(page):
            raise RuntimeError(LOGIN_REQUIRED_MESSAGE)
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(CARD_NOT_READY_MESSAGE)
        await asyncio.sleep(IDENTITY_POLL_SECONDS)


async def _wait_for_visible_text(page, selectors, budget_ms):
    deadline = asyncio.get_running_loop().time() + max(0.2, budget_ms / 1000)
    while True:
        text = await _first_visible_text(page, selectors)
        if text:
            return text
        if asyncio.get_running_loop().time() >= deadline:
            return ""
        await asyncio.sleep(IDENTITY_POLL_SECONDS)


async def wait_for_logged_in_identity(page, timeout_ms=300000):
    """Read the logged-in identity from the creator home page.

    Returns ``(unique_id, nickname)``. Raises when the card never renders, when a
    login form is shown instead, or when the card renders without a readable
    identity; the message decides the category the caller reports.

    The ID node shows the public Douyin handle or the platform UID depending on
    the page (issue #41), so this value is display text, not an identity proof.
    """
    unique_id, nickname, _raw = await _read_page_identity(page, timeout_ms)
    return unique_id, nickname


async def _read_page_identity(page, timeout_ms):
    await _wait_for_identity_card(page, timeout_ms)

    trailing_ms = max(1000, min(int(timeout_ms), IDENTITY_TRAILING_BUDGET_MS))

    unique_id_text = await _wait_for_visible_text(page, (XPATHS["unique_id"],), trailing_ms)
    unique_id = normalize_unique_id(unique_id_text)
    if not unique_id:
        raise RuntimeError(IDENTITY_UNREADABLE_MESSAGE)

    nickname = await _wait_for_visible_text(
        page,
        (XPATHS["name"],) + NICKNAME_FALLBACK_SELECTORS,
        trailing_ms,
    )
    if not nickname:
        nickname, _handle = identity_from_card_text(await _first_visible_text(page, (READY_SELECTOR,)))
    if not nickname:
        raise RuntimeError(IDENTITY_UNREADABLE_MESSAGE)
    return unique_id, nickname, unique_id_text


async def collect_login_result(page, context, timeout_ms=300000):
    """Cookies plus what the page displays about the logged-in account.

    ``display_id`` keeps the page text as shown (letters included); the
    digits-only ``unique_id`` is kept for older callers. Neither is the trusted
    account identity: that comes from :mod:`core.identity`.
    """
    unique_id, username, raw_id = await _read_page_identity(page, timeout_ms)
    cookies = await context.cookies()
    return {
        "unique_id": unique_id,
        "display_id": display_id_from_text(raw_id),
        "username": username,
        "cookies": cookies,
    }


def save_cli_login(identity, login_result, targets, *, expected_records=None):
    """Store a command-line login by its trusted platform UID.

    A row bound to the UID is updated in place, keeping its stored key, targets,
    send ledger, owner and profile, and lifting the old login/identity health
    state a proven same-UID login resolves. A legacy row that only matches by ID
    is not overwritten here: its identity has to be checked from the Web
    console. A brand-new record may not reuse a stored key or platform UID that
    any other record already occupies, even one bound to a different UID
    (issue #41 A4).

    ``targets`` is only applied when a new record is created: an update keeps
    the targets the record already had.
    """
    uid = identity["uid"]
    display_id = str(login_result.get("display_id") or "")
    login_generation = new_login_generation()

    def mutate(accounts):
        # Validate before choosing update or create: a changed binding must not
        # turn a stale update into a new account.
        check_expected_records(accounts, expected_records)
        bound = accounts_bound_to(accounts, uid)
        if len(bound) > 1:
            raise RuntimeError("多条账号记录绑定了同一个平台 UID，请先在 Web 控制台处理")
        if bound:
            account = bound[0]
            # A matching row created after the snapshot belongs to a newer
            # save; the old request must not overwrite it.
            if expected_records is not None and str(account.get("account_ref") or "") not in expected_records:
                raise AccountChangedConcurrently("目标账号在核实期间被新建，请重新登录")
            account["username"] = login_result["username"]
            account["cookies"] = login_result["cookies"]
            if display_id:
                account[FIELD_DISPLAY_ID] = display_id
            account[FIELD_LOGIN_GENERATION] = login_generation
            # A fresh login supersedes any background check queued for the old
            # cookies: drop the old token so a stale result cannot write a
            # conflict onto this login (issue #41 A7).
            account.pop("login_verification", None)
            clear_identity_markers(account)
            clear_login_health(account)
            return dict(account), True
        keys = {uid, display_id, normalize_unique_id(display_id)} - {""}
        for account in accounts:
            stored_key = str(account.get("unique_id") or "").strip()
            if stored_key in keys:
                # A stored key is occupied whether or not the row is bound, and
                # even when it is bound to a different platform UID: reusing it
                # would collide two identities on one key (issue #41 A4).
                raise RuntimeError(
                    "已有一条占用该 ID 的账号记录，请在 Web 控制台处理后再登录"
                )
            if account_uid(account) == uid:
                raise RuntimeError(
                    "这个平台 UID 已绑定到另一条账号记录，请先在 Web 控制台处理"
                )
        account = {
            "account_ref": f"acc-{uuid.uuid4().hex}",
            "unique_id": uid,
            "username": login_result["username"],
            "cookies": login_result["cookies"],
            "targets": list(targets),
            "enabled": True,
        }
        if display_id:
            account[FIELD_DISPLAY_ID] = display_id
        bind_identity(account, identity, BINDING_NEW)
        account[FIELD_LOGIN_GENERATION] = login_generation
        accounts.append(account)
        return dict(account), True

    return update_user_data(mutate, force_reload=True)


async def userLogin(targets=None):
    playwright, browser = await get_browser(GUI=True)
    try:
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto("https://creator.douyin.com/")
        console.print("Please scan the QR code and finish logging into Douyin Creator Center.")

        login_result = await collect_login_result(page, context)
        # Read the account snapshot before the network identity check so a
        # concurrent change is detected at write time (issue #41 A8).
        from utils.config import get_userData

        snapshot = {
            str(row.get("account_ref") or ""): record_fingerprint(row)
            for row in get_userData(force_reload=True)
            if str(row.get("account_ref") or "").strip()
        }
        identity = await asyncio.to_thread(fetch_trusted_identity, login_result["cookies"])
        console.print(f"Platform UID: {identity['uid']}")
        console.print(f"Displayed ID: {login_result.get('display_id', '')}")
        console.print(f"Name: {login_result['username']}")
        console.print(f"Cookies: found {len(login_result['cookies'])} cookies")

        if targets is None:
            raw_targets = input(
                "Open Creator Center -> 互动管理 -> 私信管理 -> 朋友私信, then enter friend display names separated by spaces: "
            )
            targets = [target.strip() for target in raw_targets.split(" ") if target.strip()]

        account = save_cli_login(
            identity, login_result, targets, expected_records=snapshot
        )
        console.print(f"[bold green]Login complete. Updated account {account['username']}.[/bold green]")
        return account
    finally:
        await playwright.stop()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(userLogin())
