import asyncio
import base64
import errno
import hashlib
import json
import logging
import os
import random
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from core.browser import (
    get_browser,
    get_persistent_browser_context,
    sanitize_profile_name,
    select_douyin_network_mode,
)
from core.friends import FriendIdentityCollector, normalize_friend_name
from core.msg_builder import build_message, build_message_candidates
from core.protocol_dispatch import run_protocol_tasks
from core.send_state import parse_sent_at, target_is_strong_confirmed_today
from utils.config import get_config, get_userData, normalize_unique_id, save_userData
from utils.logger import setup_logger


logger = setup_logger(level=logging.DEBUG)
debug_artifacts_dir = Path("logs/debug_artifacts")
debug_artifacts_dir.mkdir(parents=True, exist_ok=True)
CREATOR_HOME_URL = "https://creator.douyin.com/"
CREATOR_CHAT_URL = "https://creator.douyin.com/creator-micro/data/following/chat"


async def retry_operation(name, operation, retries=3, delay=2, *args, **kwargs):
    for attempt in range(retries):
        try:
            return await operation(*args, **kwargs)
        except Exception as exc:
            if attempt < retries - 1:
                logger.warning("%s failed, retry %s/%s: %s", name, attempt + 1, retries, exc)
                await asyncio.sleep(delay)
            else:
                logger.error("%s failed after %s attempts: %s", name, retries, exc)
                raise


def _safe_name(value):
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in value)[:80]


def _normalize_target_name(value):
    # Shared with core.friends so a nickname scraped from the chat list compares
    # equal to the same nickname the user_detail API reported.
    return normalize_friend_name(value)


def _current_run_mode():
    if _manual_run_unsent_only():
        return "manual_unsent_only"
    if _manual_run_failed_only():
        return "manual_failed_only"
    return "manual" if _is_manual_run() else "scheduled"


def _coerce_non_negative_int(value, default):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, int(default))


def _coerce_positive_int(value, default):
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return max(1, int(default))


def _coerce_positive_float(value, default):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = float(default)
    if parsed <= 0:
        parsed = float(default)
    return parsed


def _normalize_friend_list_scan_config(active_config):
    raw = active_config.get("friendListScan", {}) or {}
    return {
        "maxScanSeconds": _coerce_positive_int(raw.get("maxScanSeconds", 300), 300),
        "idleScanSeconds": _coerce_positive_int(raw.get("idleScanSeconds", 120), 120),
        "scrollStepPx": _coerce_positive_int(raw.get("scrollStepPx", 400), 400),
        "scrollDelaySeconds": _coerce_positive_float(raw.get("scrollDelaySeconds", 1.5), 1.5),
    }


def _normalize_send_strategy(active_config):
    raw = active_config.get("sendStrategy", {}) or {}
    start_min = _coerce_non_negative_int(raw.get("accountStartDelaySecondsMin", 0), 0)
    start_max = _coerce_non_negative_int(raw.get("accountStartDelaySecondsMax", start_min), start_min)
    if start_max < start_min:
        start_max = start_min

    message_min = _coerce_non_negative_int(raw.get("messageIntervalSecondsMin", 0), 0)
    message_max = _coerce_non_negative_int(raw.get("messageIntervalSecondsMax", message_min), message_min)
    if message_max < message_min:
        message_max = message_min

    return {
        "accountStartDelaySecondsMin": start_min,
        "accountStartDelaySecondsMax": start_max,
        "messageIntervalSecondsMin": message_min,
        "messageIntervalSecondsMax": message_max,
    }


def _normalize_persistent_profile_config(active_config):
    raw = active_config.get("persistentBrowserProfiles", {}) or {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "root": str(
            os.getenv("SPARKFLOW_BROWSER_PROFILE_ROOT")
            or raw.get("root")
            or "/opt/douyin-sparkflow/state/browser-profiles"
        ),
        "seedCookiesWhenEmpty": bool(raw.get("seedCookiesWhenEmpty", True)),
        "syncStoredCookiesBeforeRun": bool(raw.get("syncStoredCookiesBeforeRun", True)),
        "refreshStoredCookiesAfterLogin": bool(raw.get("refreshStoredCookiesAfterLogin", True)),
    }


def _account_profile_name(user):
    unique_id = normalize_unique_id(user.get("unique_id"))
    username = str(user.get("username") or "").strip()
    if unique_id:
        return f"uid-{unique_id}"
    if username:
        return f"user-{sanitize_profile_name(username)}"
    return "unknown"


def _random_delay_seconds(send_strategy, min_key, max_key):
    return random.randint(send_strategy[min_key], send_strategy[max_key])


async def _sleep_with_log(seconds, reason, account_name):
    if seconds <= 0:
        return
    logger.info("%s for %s by %ss", reason, account_name, seconds)
    await asyncio.sleep(seconds)


LOGIN_REQUIRED_TEXTS = (
    "扫码登录",
    "验证码登录",
    "密码登录",
    "登录/注册",
    "登录后免费",
    "请登录",
    "身份验证",
    "安全验证",
    "风险提示",
    "环境异常",
)
LOGIN_REQUIRED_SELECTORS = (
    ".login-mask",
    ".login-guide-container",
    ".login-img-code-wrapper",
    ".pc-login-verification-modal",
)
LOGIN_REQUIRED_DIALOG_SELECTORS = (
    ".semi-modal-content",
    'div[role="dialog"]',
)
NON_LOGIN_DIALOG_DISMISS_TEXTS = (
    "我知道了",
    "知道了",
    "好的",
    "确定",
    "确认",
    "稍后再说",
    "关闭",
)
NON_LOGIN_DIALOG_CLOSE_SELECTORS = (
    ".semi-modal-close",
    'button[aria-label="Close"]',
    'button[aria-label="关闭"]',
    '[aria-label="Close"]',
    '[aria-label="关闭"]',
)
AUTH_COOKIE_NAMES = {
    "sessionid",
    "sessionid_ss",
    "sid_guard",
    "sid_tt",
    "uid_tt",
    "uid_tt_ss",
}


async def _body_text_sample(page, limit=1200):
    try:
        return (await page.locator("body").inner_text(timeout=3000)).replace("\n", " ")[:limit]
    except Exception as exc:
        return f"BODY_TEXT_ERROR={exc!r}"


def _find_login_required_text(text):
    sample = text or ""
    for marker in LOGIN_REQUIRED_TEXTS:
        if marker in sample:
            return marker
    return None


async def _page_has_login_required_prompt(page):
    for selector in LOGIN_REQUIRED_SELECTORS:
        locator = page.locator(selector).first
        try:
            if await locator.count() > 0 and await locator.is_visible(timeout=500):
                return True, f"visible selector {selector}"
        except Exception:
            continue

    for selector in LOGIN_REQUIRED_DIALOG_SELECTORS:
        locator = page.locator(selector)
        try:
            count = min(await locator.count(), 5)
        except Exception:
            continue

        for index in range(count):
            item = locator.nth(index)
            try:
                if not await item.is_visible(timeout=500):
                    continue
                dialog_text = (await item.inner_text(timeout=1000)).replace("\n", " ")
            except Exception:
                continue

            matched_text = _find_login_required_text(dialog_text)
            if matched_text:
                return True, (
                    f"visible dialog {selector} contains {matched_text!r}: "
                    f"{dialog_text[:300]}"
                )
            logger.debug(
                "Ignoring visible non-login dialog for selector %s: %s",
                selector,
                dialog_text[:160],
            )

    sample = await _body_text_sample(page)
    matched_text = _find_login_required_text(sample)
    if matched_text:
        return True, f"body contains {matched_text!r}: {sample[:300]}"
    return False, ""


async def ensure_not_login_required(page, account_name, stage):
    is_required, detail = await _page_has_login_required_prompt(page)
    if is_required:
        raise RuntimeError(f"login_required at {stage} for {account_name}: {detail}")


async def _dismiss_non_login_dialogs(page, account_name, stage):
    dismissed = 0
    for selector in LOGIN_REQUIRED_DIALOG_SELECTORS:
        locator = page.locator(selector)
        try:
            count = min(await locator.count(), 5)
        except Exception:
            continue

        for index in range(count):
            item = locator.nth(index)
            try:
                if not await item.is_visible(timeout=500):
                    continue
                dialog_text = (await item.inner_text(timeout=1000)).replace("\n", " ")
            except Exception:
                continue

            if _find_login_required_text(dialog_text):
                continue

            clicked = False
            for text in NON_LOGIN_DIALOG_DISMISS_TEXTS:
                button = item.get_by_text(text, exact=True).first
                try:
                    if await button.count() > 0 and await button.is_visible(timeout=500):
                        await button.click(timeout=2000)
                        clicked = True
                        break
                except Exception:
                    continue

            if not clicked:
                for close_selector in NON_LOGIN_DIALOG_CLOSE_SELECTORS:
                    close_button = item.locator(close_selector).first
                    try:
                        if await close_button.count() > 0 and await close_button.is_visible(timeout=500):
                            await close_button.click(timeout=2000)
                            clicked = True
                            break
                    except Exception:
                        continue

            if clicked:
                dismissed += 1
                logger.info(
                    "Dismissed non-login dialog for %s at %s: %s",
                    account_name,
                    stage,
                    dialog_text[:160],
                )
                await asyncio.sleep(0.5)
            else:
                logger.debug(
                    "Visible non-login dialog for %s at %s was not dismissed: %s",
                    account_name,
                    stage,
                    dialog_text[:160],
                )
    return dismissed


async def apply_stored_cookies_to_profile(context, cookies, account_name, only_when_empty=False):
    if not cookies:
        return
    if only_when_empty:
        try:
            current_cookies = await context.cookies(
                ["https://creator.douyin.com/", "https://www.douyin.com/"]
            )
        except Exception:
            current_cookies = []

        if any(cookie.get("name") in AUTH_COOKIE_NAMES for cookie in current_cookies):
            logger.info(
                "Persistent profile for %s already has auth cookies; not seeding stored cookies",
                account_name,
            )
            return

    await context.add_cookies(cookies)
    logger.info("Applied %s stored cookies to persistent profile for %s", len(cookies), account_name)


async def refresh_stored_cookies_from_profile(context, user, account_name):
    try:
        cookies = await context.cookies()
    except Exception as exc:
        logger.warning("Could not read persistent profile cookies for %s: %s", account_name, exc)
        return

    if not cookies:
        return

    accounts = get_userData(force_reload=True)
    matched_account = _find_matching_account(accounts, user)
    if matched_account is None:
        logger.warning("Could not find account to refresh cookies for user=%s", account_name)
        return

    matched_account["cookies"] = list(cookies)
    save_userData(accounts)
    user["cookies"] = list(cookies)
    logger.info("Refreshed stored cookies for %s from persistent profile count=%s", account_name, len(cookies))


def _is_stop_account_category(category):
    return _is_account_level_failure_category(category)


async def save_debug_artifacts(page, account_name, target_name, stage):
    if not get_config(force_reload=True).get("saveDebugArtifacts", False):
        return

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    stem = f"{timestamp}-{_safe_name(account_name)}-{_safe_name(target_name)}-{stage}"
    screenshot_path = debug_artifacts_dir / f"{stem}.png"
    html_path = debug_artifacts_dir / f"{stem}.html"

    await page.screenshot(path=str(screenshot_path), full_page=True)
    html_path.write_text(await page.content(), encoding="utf-8")
    logger.info("Saved debug artifacts at stage=%s for %s/%s", stage, account_name, target_name)


async def locate_chat_input(page):
    selectors = [
        "xpath=//div[contains(@class, 'chat-input-dccKiL')]//div[@contenteditable='true']",
        "xpath=//div[@contenteditable='true' and @role='textbox']",
        "xpath=(//div[@contenteditable='true'])[last()]",
    ]

    last_error = None
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=10000)
            await locator.click(timeout=5000)
            return locator, selector
        except Exception as exc:
            last_error = exc

    raise RuntimeError(f"Unable to locate chat input, last error: {last_error}")


async def read_chat_input_text(chat_input):
    try:
        return await chat_input.evaluate(
            """(node) => {
                const raw = node.innerText ?? node.textContent ?? "";
                return raw.trim();
            }"""
        )
    except Exception:
        return ""


def _message_probe_text(message):
    for line in str(message or "").splitlines():
        probe = line.strip()
        if probe:
            return probe
    return str(message or "").strip()


def _normalize_message_text(value):
    return "\n".join(
        line.strip()
        for line in str(value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if line.strip()
    ).strip()


async def _detect_send_failure_indicator(page):
    """Return a short string describing a visible send-failure UI element, or ""."""
    try:
        return await page.evaluate(
            """() => {
                // Red exclamation icon commonly used by chat UIs for failed sends.
                const failIcons = document.querySelectorAll(
                    "svg[class*='fail'], svg[class*='Fail'], svg[class*='error'], svg[class*='Error'],"
                    + " [class*='exclamation'], [class*='Exclamation'],"
                    + " [class*='retry'], [class*='Retry'], [class*='resend'], [class*='Resend']"
                );
                for (const el of failIcons) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) {
                        // Climb up to find the associated message row text.
                        let row = el.closest("li, [role='listitem'], [class*='message'], [class*='Message'], [class*='item']");
                        let snippet = String(row?.innerText || "").slice(0, 40);
                        return "fail_icon(" + (el.className || "").slice(0, 60) + ") near=" + snippet;
                    }
                }
                // Explicit failure / retry text.
                const bodyText = String(document.body?.innerText || "");
                const failKeywords = ["发送失败", "重试", "重新发送", "send failed", "retry", "resend"];
                for (const kw of failKeywords) {
                    if (bodyText.includes(kw)) {
                        return "fail_text=" + kw;
                    }
                }
                return "";
            }"""
        )
    except Exception:
        return ""



async def start_im_send_observer(page, account_name, target_name):
    """Observe creator IM service calls for one browser send attempt."""
    state = {
        "enabled": False,
        "send_request_seen": False,
        "send_response_seen": False,
        "send_receipt": {},
        "mark_read_calls": [],
        "identity_security_token_calls": [],
        "events": [],
        "error": "",
    }
    pending = {}
    session = None

    try:
        session = await page.context.new_cdp_session(page)
        await session.send("Network.enable")
        state["enabled"] = True
    except Exception as exc:
        state["error"] = f"cdp_unavailable: {exc}"
        logger.warning("IM observer unavailable for %s/%s: %s", account_name, target_name, exc)

        async def disabled_summary(extra_wait_seconds=0):
            if extra_wait_seconds:
                await asyncio.sleep(extra_wait_seconds)
            return dict(state)

        return disabled_summary

    def _trim(value, limit=220):
        return str(value or "")[:limit]

    def _call_kind(url):
        value = str(url or "")
        if "/v1/message/send" in value:
            return "message_send"
        if "mark_read" in value:
            return "mark_read"
        if "identity_security_token" in value:
            return "identity_security_token"
        if "imapi.douyin.com" in value:
            return "imapi_other"
        return ""

    def _safe_url(url):
        return str(url or "").split("?", 1)[0]

    def _header_value(headers, name):
        target = name.lower()
        for key, value in (headers or {}).items():
            if str(key).lower() == target:
                return str(value)
        return ""

    def _decode_body(body):
        raw = body.get("body") or ""
        if body.get("base64Encoded"):
            try:
                return base64.b64decode(raw).decode("utf-8", errors="replace")
            except Exception:
                return ""
        return raw

    def _parse_json_body(text):
        try:
            return json.loads(text)
        except Exception:
            return None

    def _extract_error_text(data, body_text):
        if isinstance(data, dict):
            for key in ("status_msg", "message", "msg", "err_msg", "error", "reason"):
                value = data.get(key)
                if value:
                    return _trim(value, 300)
            nested = data.get("data")
            if isinstance(nested, dict):
                for key in ("status_msg", "message", "msg", "err_msg", "error", "reason"):
                    value = nested.get(key)
                    if value:
                        return _trim(value, 300)
        return _trim(body_text, 300)

    def _json_success(data):
        if not isinstance(data, dict):
            return None
        success_values = []
        for key in ("status_code", "err_no", "errno", "error_code", "code"):
            if key in data:
                success_values.append(data.get(key) in (0, "0", None))
        message = str(data.get("message") or data.get("status_msg") or "").lower()
        if message:
            success_values.append(message in ("success", "ok"))
        if success_values:
            return all(success_values)
        return None

    def _receipt_body_meta(data, body_text):
        meta = {"bodyLen": len(body_text or "")}
        if body_text:
            meta["bodySha256"] = hashlib.sha256(body_text.encode("utf-8", errors="replace")).hexdigest()
        if isinstance(data, dict):
            meta["jsonKeys"] = sorted(str(key) for key in data.keys())[:20]
            nested = data.get("data")
            if isinstance(nested, dict):
                meta["dataKeys"] = sorted(str(key) for key in nested.keys())[:20]
                for source_key, dest_key in (
                    ("server_message_id", "serverMessageId"),
                    ("message_id", "messageId"),
                    ("msg_id", "messageId"),
                    ("conversation_id", "conversationId"),
                    ("conversation_short_id", "conversationShortId"),
                ):
                    value = nested.get(source_key)
                    if value:
                        meta[dest_key] = _trim(value, 120)
            for source_key, dest_key in (
                ("server_message_id", "serverMessageId"),
                ("message_id", "messageId"),
                ("msg_id", "messageId"),
                ("conversation_id", "conversationId"),
                ("conversation_short_id", "conversationShortId"),
            ):
                value = data.get(source_key)
                if value:
                    meta[dest_key] = _trim(value, 120)
        return meta

    def _record(event):
        state["events"].append(event)
        if len(state["events"]) > 50:
            del state["events"][:-50]

    def on_request(params):
        request = params.get("request") or {}
        url = request.get("url") or ""
        kind = _call_kind(url)
        if not kind:
            return
        request_id = params.get("requestId")
        post_data = request.get("postData") or ""
        method = request.get("method") or ""
        is_send_post = kind == "message_send" and method.upper() == "POST"
        pending[request_id] = {"url": url, "kind": kind, "request": request, "is_send_post": is_send_post}
        _record({"kind": "request", "call": kind, "url": _safe_url(url), "method": method, "postLen": len(post_data)})
        if is_send_post:
            state["send_request_seen"] = True
            logger.info("IM observer saw message send request for %s/%s url=%s postLen=%s", account_name, target_name, _trim(_safe_url(url), 160), len(post_data))

    def on_response(params):
        request_id = params.get("requestId")
        item = pending.get(request_id)
        if not item:
            return
        response = params.get("response") or {}
        item["response"] = response
        kind = item.get("kind")
        headers = response.get("headers") or {}
        status = response.get("status")
        logid = _header_value(headers, "x-tt-logid") or _header_value(headers, "x-tt-trace-log") or _header_value(headers, "x-tt-trace-id")
        event = {"kind": "response", "call": kind, "url": _safe_url(item.get("url")), "status": status}
        if logid:
            event["logid"] = _trim(logid, 120)
        _record(event)
        if item.get("is_send_post"):
            state["send_response_seen"] = True
            state["send_receipt"] = {"call": kind, "httpStatus": status, "ok": False, "pendingBody": True, "logid": _trim(logid, 120), "url": _safe_url(item.get("url"))}
            logger.info("IM observer saw message send response for %s/%s status=%s logid=%s", account_name, target_name, status, _trim(logid, 80))

    async def fetch_body(request_id):
        item = pending.get(request_id) or {}
        kind = item.get("kind") or ""
        url = item.get("url") or ""
        response = item.get("response") or {}
        status = response.get("status")
        headers = response.get("headers") or {}
        logid = _header_value(headers, "x-tt-logid") or _header_value(headers, "x-tt-trace-log") or _header_value(headers, "x-tt-trace-id")
        try:
            body = await session.send("Network.getResponseBody", {"requestId": request_id})
            body_text = _decode_body(body)
            data = _parse_json_body(body_text)
            http_ok = isinstance(status, (int, float)) and 200 <= status < 300
            json_ok = _json_success(data)
            receipt = {"call": kind, "httpStatus": status, "ok": bool(http_ok and (json_ok is not False)), "jsonParsed": isinstance(data, dict), "logid": _trim(logid, 120), "url": _safe_url(url)}
            receipt.update(_receipt_body_meta(data, body_text))
            if json_ok is not None:
                receipt["jsonOk"] = bool(json_ok)
            if not receipt["ok"]:
                receipt["reason"] = _extract_error_text(data, body_text)
            if item.get("is_send_post"):
                state["send_receipt"] = receipt
                _record({"kind": "body", "call": kind, "status": status, "ok": receipt["ok"], "jsonParsed": receipt["jsonParsed"], "logid": receipt.get("logid", ""), "reason": receipt.get("reason", "")})
            elif kind == "mark_read":
                state["mark_read_calls"].append(receipt)
                _record({"kind": "body", "call": kind, "status": status, "ok": receipt["ok"]})
            elif kind == "identity_security_token":
                has_token = isinstance(data, dict) and bool((data.get("data") or {}).get("identity_security_token"))
                receipt["hasToken"] = has_token
                receipt.pop("reason", None)
                state["identity_security_token_calls"].append(receipt)
                _record({"kind": "body", "call": kind, "status": status, "ok": receipt["ok"], "hasToken": has_token})
            else:
                _record({"kind": "body", "call": kind, "status": status, "ok": receipt["ok"]})
        except Exception as exc:
            error = _trim(exc, 160)
            _record({"kind": "body_error", "call": kind, "url": _safe_url(url), "error": error})
            if item.get("is_send_post"):
                state["send_receipt"] = {"call": kind, "httpStatus": status, "ok": False, "logid": _trim(logid, 120), "url": _safe_url(url), "reason": f"response_body_unavailable: {error}"}
        finally:
            pending.pop(request_id, None)

    def on_loading_finished(params):
        request_id = params.get("requestId")
        if request_id in pending:
            asyncio.create_task(fetch_body(request_id))

    session.on("Network.requestWillBeSent", on_request)
    session.on("Network.responseReceived", on_response)
    session.on("Network.loadingFinished", on_loading_finished)

    async def summary(extra_wait_seconds=3):
        if extra_wait_seconds:
            await asyncio.sleep(extra_wait_seconds)
        try:
            await session.detach()
        except Exception:
            pass
        return dict(state)

    return summary

async def snapshot_last_own_message(page, chat_input=None):
    try:
        return await page.evaluate(
            r"""() => {
                // ---- DOM structure (douyin creator private message) ----
                // Each row is <div class="box-item-W0TV01 ...">...</div>.
                //   - time separator:   class contains "time-" (e.g. time-Tl7Z4j)
                //   - own message:       class contains "is-me"   (e.g. is-me-cb9NAa)
                //   - peer message:      no "is-me" and no "time-"
                // Text is inside <pre class="text-...">.

                const allRows = Array.from(
                    document.querySelectorAll("[class*='box-item-']")
                ).filter(el => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                });

                // Walk backwards to find the boundary: first time separator that
                // indicates yesterday or earlier. Everything after it is "today".
                let todayStart = 0;
                for (let i = allRows.length - 1; i >= 0; i--) {
                    const cls = String(allRows[i].className || "");
                    if (!cls.includes("time-")) continue;
                    const label = String(allRows[i].innerText || "").trim();
                    // "今天" / "刚刚" / "N分钟前" / pure time like "11:01" => today
                    // "昨天" / "星期X" / "2026-..." => not today
                    if (/^(昨天|星期|20\d{2}-)/.test(label)) {
                        todayStart = i + 1;
                        break;
                    }
                }

                // From bottom, find the last own-message row within today's section.
                for (let i = allRows.length - 1; i >= todayStart; i--) {
                    const cls = String(allRows[i].className || "");
                    if (!cls.includes("is-me")) continue;
                    if (cls.includes("time-")) continue;
                    const pre = allRows[i].querySelector("pre, [class*='text-']");
                    const text = String(pre?.innerText || allRows[i].innerText || "").trim();
                    if (!text) continue;
                    const rect = allRows[i].getBoundingClientRect();
                    return {
                        text,
                        centerX: rect.left + rect.width / 2,
                        centerY: rect.top + rect.height / 2,
                        top: rect.top,
                        bottom: rect.bottom,
                        right: rect.right,
                        className: cls.slice(0, 240),
                    };
                }
                return null;
            }""",
        )
    except Exception as exc:
        logger.debug("Unable to snapshot last own message: %s", exc)
        return None


async def count_today_own_message_matches(page, target_text):
    """Count how many of *target_text* appear in today's own-message bubbles."""
    try:
        return await page.evaluate(
            r"""(targetText) => {
                const allRows = Array.from(
                    document.querySelectorAll("[class*='box-item-']")
                ).filter(el => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                });
                let todayStart = 0;
                for (let i = allRows.length - 1; i >= 0; i--) {
                    const cls = String(allRows[i].className || "");
                    if (!cls.includes("time-")) continue;
                    const label = String(allRows[i].innerText || "").trim();
                    if (/^(昨天|星期|20\d{2}-)/.test(label)) {
                        todayStart = i + 1;
                        break;
                    }
                }
                let count = 0;
                for (let i = todayStart; i < allRows.length; i++) {
                    const cls = String(allRows[i].className || "");
                    if (!cls.includes("is-me")) continue;
                    if (cls.includes("time-")) continue;
                    const pre = allRows[i].querySelector("pre, [class*='text-']");
                    const text = String(pre?.innerText || allRows[i].innerText || "").trim();
                    if (text === targetText) count++;
                }
                return count;
            }""",
            _normalize_message_text(target_text),
        )
    except Exception:
        return 0


async def count_visible_message_matches(page, message, chat_input=None):
    probe = _message_probe_text(message)
    if not probe:
        return 0

    locator = page.locator(f"text={probe}")
    try:
        count = await locator.count()
    except Exception:
        return 0

    input_box = None
    if chat_input is not None:
        try:
            input_box = await chat_input.bounding_box(timeout=1000)
        except Exception:
            input_box = None

    visible_count = 0
    for index in range(min(count, 200)):
        try:
            item = locator.nth(index)
            if not await item.is_visible(timeout=500):
                continue
            if await item.evaluate("(node) => Boolean(node.closest('[contenteditable=\"true\"]'))"):
                continue
            if input_box:
                box = await item.bounding_box(timeout=500)
                if not box:
                    continue
                center_x = box["x"] + box["width"] / 2
                center_y = box["y"] + box["height"] / 2
                in_chat_column = (
                    input_box["x"] - 80
                    <= center_x
                    <= input_box["x"] + input_box["width"] + 80
                )
                above_input = center_y < input_box["y"] + input_box["height"] + 80
                if not (in_chat_column and above_input):
                    continue
            visible_count += 1
        except Exception:
            continue
    return visible_count


async def confirm_message_sent(page, chat_input, message, before_snapshot=None):
    expected_message = _normalize_message_text(message)
    before_signature = None
    if before_snapshot:
        before_signature = (
            before_snapshot.get("text"),
            round(float(before_snapshot.get("centerY") or 0), 1),
            round(float(before_snapshot.get("right") or 0), 1),
        )
    # Record how many times the target text already appears today so we can
    # detect a *new* occurrence even if the last-own-message slot was taken by
    # a manual send from the user's phone.
    before_count = await count_today_own_message_matches(page, expected_message)

    last_input_text = ""
    last_snapshot = None
    deadline = asyncio.get_running_loop().time() + 8
    while True:
        await asyncio.sleep(1)
        last_input_text = await read_chat_input_text(chat_input)
        # Fail fast if the page shows a send-failure indicator (red exclamation,
        # retry button, or explicit failure text) before we even check the bubble.
        failure_indicator = await _detect_send_failure_indicator(page)
        if failure_indicator:
            return False, f"send failure indicator detected: {failure_indicator}"
        last_snapshot = await snapshot_last_own_message(page, chat_input=chat_input)
        last_signature = None
        if last_snapshot:
            last_signature = (
                last_snapshot.get("text"),
                round(float(last_snapshot.get("centerY") or 0), 1),
                round(float(last_snapshot.get("right") or 0), 1),
            )
        if (
            not last_input_text
            and last_snapshot
            and _normalize_message_text(last_snapshot.get("text")) == expected_message
            and last_signature != before_signature
        ):
            return (
                True,
                "last own message matches sent text; chat input cleared",
            )
        # Count fallback: even if the last own bubble is not our message (e.g.
        # the user sent something else from their phone), a new occurrence of
        # the target text today means our send went through.
        if (
            not last_input_text
            and not last_snapshot
        ):
            after_count = await count_today_own_message_matches(page, expected_message)
            if after_count > before_count:
                return (
                    True,
                    "count fallback: new own message with target text appeared today",
                )
        if asyncio.get_running_loop().time() >= deadline:
            break

    if last_input_text:
        return False, f"chat input still contains: {last_input_text!r}"
    # Final count fallback before giving up.
    after_count = await count_today_own_message_matches(page, expected_message)
    if after_count > before_count:
        return (
            True,
            "count fallback at deadline: new own message with target text appeared today",
        )
    if last_snapshot:
        return False, (
            "last own message did not match sent text: "
            f"expected={expected_message!r} actual={last_snapshot.get('text')!r}"
        )
    return (
        False,
        "chat input cleared but no new own message bubble was confirmed",
    )


async def find_visible_spark_message_candidate(page, chat_input=None, candidates=None):
    messages = []
    for candidate in candidates or build_message_candidates():
        probe = _message_probe_text(candidate)
        if probe and probe not in messages:
            messages.append(probe)

    for candidate in messages:
        try:
            if await count_visible_message_matches(page, candidate, chat_input=chat_input) > 0:
                return candidate
        except Exception:
            continue
    return ""


async def detect_message_already_sent(page, chat_input, message, before_snapshot=None):
    try:
        if chat_input is not None:
            sent_ok, detail = await confirm_message_sent(
                page,
                chat_input,
                message,
                before_snapshot=before_snapshot,
            )
            if sent_ok:
                return True, detail
    except Exception:
        pass

    return False, ""


def classify_browser_failure(stage, exc):
    detail = str(exc or "")
    lowered = detail.lower()

    if "friend_list_unavailable" in lowered:
        return "friend_list_unavailable"
    if "friend_list_incomplete" in lowered:
        return "friend_list_incomplete"
    if "login_required" in lowered:
        return "login_required"
    if any(token in detail for token in LOGIN_REQUIRED_TEXTS):
        return "login_required"
    if "page crashed" in lowered or "target page, context or browser has been closed" in lowered:
        return "page_crashed"
    if "timeout" in lowered:
        if stage in {"open_creator_home", "open_chat_page"}:
            return "navigation_timeout"
        if stage == "locate_chat_input":
            return "chat_input_timeout"
        if stage == "friend_list":
            return "friend_list_timeout"
        return "timeout"
    if "unable to locate chat input" in lowered:
        return "chat_input_not_found"
    if "could not find the friend list scroll container" in lowered:
        return "friend_list_container_missing"
    if "chat input still contains" in lowered or "visible message count did not increase" in lowered:
        return "send_unconfirmed"
    if "missing targets" in lowered:
        return "friend_not_found"
    if stage in {"open_creator_home", "open_chat_page"}:
        return "navigation_failed"
    return "unknown"


class FriendListUnavailableError(RuntimeError):
    pass


class FriendListIncompleteError(RuntimeError):
    pass


ACCOUNT_LEVEL_FAILURE_CATEGORIES = {
    "login_required",
    "friend_list_unavailable",
    "friend_list_timeout",
    "friend_list_incomplete",
    "friend_list_container_missing",
    "navigation_timeout",
    "navigation_failed",
    "page_crashed",
}


TEMPORARY_ACCOUNT_FAILURE_CATEGORIES = {
    "friend_list_unavailable",
    "friend_list_timeout",
    "friend_list_incomplete",
    "friend_list_container_missing",
    "navigation_timeout",
    "navigation_failed",
    "page_crashed",
    "timeout",
}


def _is_account_level_failure_category(category):
    return category in ACCOUNT_LEVEL_FAILURE_CATEGORIES


def _target_display_name(target):
    if isinstance(target, dict):
        for key in ("name", "displayName", "nickname", "username", "unique_id", "uid"):
            value = str(target.get(key) or "").strip()
            if value:
                return value
        return ""
    return str(target or "").strip()


def _build_normalized_target_map(targets, id_to_name=None):
    """Map each target onto the nickname the friend list will show for it.

    A target is normally the friend's 抖音号, which never changes.  The chat list
    only ever shows nicknames, so a stored 抖音号 is resolved to whatever the
    friend is called today; a friend the API has not answered for keeps the
    stored value and is matched by name, exactly as before.
    """
    id_to_name = id_to_name or {}
    normalized_targets = {}
    for target in targets or []:
        display_name = _target_display_name(target)
        if not display_name:
            continue
        current_name = str(id_to_name.get(display_name) or "").strip()
        normalized_name = _normalize_target_name(current_name or display_name)
        if normalized_name:
            normalized_targets[normalized_name] = display_name
    return normalized_targets


async def _locator_count_with_timeout(locator, account_name, stage, label, timeout_seconds=3):
    try:
        return await asyncio.wait_for(locator.count(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        logger.warning(
            "Account %s locator count timed out at %s label=%s after %ss",
            account_name,
            stage,
            label,
            timeout_seconds,
        )
        return None


async def _first_non_empty_locator(page, selectors):
    for selector in selectors:
        locator = page.locator(selector)
        count = await _locator_count_with_timeout(
            locator,
            "unknown",
            "first_non_empty_locator",
            selector,
        )
        if count is None:
            continue
        for index in range(min(count, 5)):
            item = locator.nth(index)
            try:
                if await item.is_visible(timeout=500):
                    return selector, locator
            except Exception:
                continue
    return "", None


async def _selector_visible(page, selectors):
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            count = await _locator_count_with_timeout(
                locator,
                "unknown",
                "selector_visible",
                selector,
            )
            if count and await locator.is_visible(timeout=500):
                return selector
        except Exception:
            continue
    return ""


async def _click_first_visible_locator(candidates, account_name, stage):
    last_error = None
    for label, locator in candidates:
        try:
            count = await _locator_count_with_timeout(
                locator,
                account_name,
                stage,
                label,
            )
            count = min(count or 0, 5)
        except Exception as exc:
            last_error = exc
            continue

        for index in range(count):
            item = locator.nth(index)
            try:
                if not await item.is_visible(timeout=800):
                    continue
                await item.click(timeout=3000)
                logger.info("Account %s clicked %s at %s", account_name, label, stage)
                return True
            except Exception as exc:
                last_error = exc
                continue
    if last_error:
        logger.debug("Account %s could not click a visible locator at %s: %s", account_name, stage, last_error)
    return False


async def _open_friends_tab(page, account_name, fallback_selector, target_selectors):
    await _dismiss_non_login_dialogs(page, account_name, "before_open_friends_tab")

    _, existing_locator = await _first_non_empty_locator(page, target_selectors)
    if existing_locator:
        logger.info("Account %s friend list is already visible before tab click", account_name)
        return "already_visible"

    sub_app = page.locator('xpath=//*[@id="sub-app"]')
    text_selector = (
        'xpath=//*[@id="sub-app"]//*[self::div or self::button or self::span or @role="tab"]'
        '[contains(normalize-space(.), "朋友") or contains(normalize-space(.), "好友") '
        'or contains(normalize-space(.), "互关")]'
    )
    page_text_selector = (
        'xpath=//*[self::div or self::button or self::span or @role="tab" or @role="button"]'
        '[contains(normalize-space(.), "朋友私信") or contains(normalize-space(.), "好友私信") '
        'or contains(normalize-space(.), "朋友") or contains(normalize-space(.), "好友") '
        'or contains(normalize-space(.), "互关")]'
    )
    candidates = [
        ("page tab text 朋友私信", page.get_by_text("朋友私信", exact=True)),
        ("page tab text 好友私信", page.get_by_text("好友私信", exact=True)),
        ("tab text 朋友", sub_app.get_by_text("朋友", exact=True)),
        ("tab text 好友", sub_app.get_by_text("好友", exact=True)),
        ("tab text 互关", sub_app.get_by_text("互关", exact=True)),
        ("tab text 朋友私信", sub_app.get_by_text("朋友私信", exact=True)),
        ("tab text 好友私信", sub_app.get_by_text("好友私信", exact=True)),
        ("sub-app friend tab text", page.locator(text_selector)),
        ("page friend tab text", page.locator(page_text_selector)),
        ("fallback friends tab xpath", page.locator(fallback_selector)),
    ]
    if await _click_first_visible_locator(candidates, account_name, "open_friends_tab"):
        await asyncio.sleep(2)
        return "clicked"

    await page.wait_for_selector(fallback_selector, timeout=30000)
    await page.locator(fallback_selector).click(timeout=5000)
    logger.info("Account %s clicked fallback friends tab after explicit wait", account_name)
    await asyncio.sleep(1)
    return "fallback_wait_clicked"


async def _reopen_friend_chat_page(page, account_name):
    logger.warning(
        "Account %s friend list did not become visible within 60s; reopening chat page once",
        account_name,
    )
    await _dismiss_non_login_dialogs(page, account_name, "before_reopen_chat_page")
    try:
        await page.reload(wait_until="load", timeout=120000)
        await asyncio.sleep(2)
    except Exception as exc:
        logger.warning("Account %s reload before reopen chat page failed: %s", account_name, exc)

    await retry_operation(
        "reopen chat page after friend list wait",
        page.goto,
        retries=2,
        delay=5,
        url=CREATOR_CHAT_URL,
    )
    await asyncio.sleep(3)
    await ensure_not_login_required(page, account_name, "reopen_chat_page")
    await _dismiss_non_login_dialogs(page, account_name, "after_reopen_chat_page")


async def _wait_for_friend_list_ready(
    page,
    account_name,
    target_selectors,
    no_more_selectors,
    loading_selectors,
    timeout_seconds=120,
    empty_grace_seconds=45,
):
    started_at = asyncio.get_running_loop().time()
    last_loading_seen_at = None
    last_dialog_dismissed_at = None
    while True:
        dismissed_count = await _dismiss_non_login_dialogs(page, account_name, "friend_list_ready")
        if dismissed_count:
            last_dialog_dismissed_at = asyncio.get_running_loop().time()

        selector, locator = await _first_non_empty_locator(page, target_selectors)
        if locator:
            logger.debug("Account %s friend list ready via selector %s", account_name, selector)
            return selector, locator

        is_login_required, login_detail = await _page_has_login_required_prompt(page)
        if is_login_required:
            raise RuntimeError(f"login_required at friend_list for {account_name}: {login_detail}")

        no_more_selector = await _selector_visible(page, no_more_selectors)
        if no_more_selector:
            elapsed = asyncio.get_running_loop().time() - started_at
            if elapsed > empty_grace_seconds:
                raise FriendListUnavailableError(
                    f"friend_list_unavailable for {account_name}: no friend entries visible; "
                    f"empty/end selector {no_more_selector} is visible after {empty_grace_seconds}s"
                )
            logger.debug(
                "Account %s saw empty/end selector %s after %.1fs; waiting within grace window",
                account_name,
                no_more_selector,
                elapsed,
            )

        loading_selector = await _selector_visible(page, loading_selectors)
        if loading_selector:
            last_loading_seen_at = asyncio.get_running_loop().time()
            logger.debug("Account %s waiting for friend list loading selector %s", account_name, loading_selector)

        elapsed = asyncio.get_running_loop().time() - started_at
        if elapsed > timeout_seconds:
            loading_note = ""
            if last_loading_seen_at:
                loading_note = f"; loading was visible {elapsed - last_loading_seen_at:.1f}s ago"
            if last_dialog_dismissed_at:
                loading_note += f"; dialog was dismissed {elapsed - (last_dialog_dismissed_at - started_at):.1f}s ago"
            raise FriendListUnavailableError(
                f"friend_list_unavailable for {account_name}: no friend entries became visible "
                f"within {timeout_seconds}s{loading_note}"
            )

        await asyncio.sleep(1)


async def _extract_friend_display_name(element):
    name_selectors = (
        """xpath=.//span[contains(@class, "item-header-name-")]""",
        """xpath=.//*[contains(@class, "item-header-name-")]""",
    )
    for selector in name_selectors:
        locator = element.locator(selector).first
        try:
            if await locator.count() > 0:
                text = (await locator.inner_text(timeout=1000)).strip()
                if text:
                    return text
        except Exception:
            continue

    try:
        text = (await element.inner_text(timeout=1000)).strip()
    except Exception:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


async def _extract_friend_stable_keys(element):
    stable_keys = set()
    attrs = (
        "data-e2e",
        "data-id",
        "data-user-id",
        "data-conversation-id",
        "data-sec-uid",
        "href",
        "aria-label",
        "title",
    )
    for attr in attrs:
        try:
            value = await element.get_attribute(attr)
        except Exception:
            value = None
        if value:
            stable_keys.add(f"{attr}:{str(value).strip()[:240]}")

    try:
        links = await element.locator("xpath=.//a[@href]").all()
    except Exception:
        links = []
    for link in links[:3]:
        try:
            href = await link.get_attribute("href")
        except Exception:
            href = None
        if href:
            stable_keys.add(f"href:{str(href).strip()[:240]}")

    return sorted(stable_keys)


async def _extract_friend_record(element, collector=None):
    display_name = await _extract_friend_display_name(element)
    normalized_name = _normalize_target_name(display_name)
    if not normalized_name:
        return None
    stable_keys = await _extract_friend_stable_keys(element)
    douyin_id = ""
    if collector:
        douyin_id = str(collector.name_to_id.get(normalized_name) or "").strip()
        if douyin_id:
            stable_keys = sorted(set(stable_keys) | {f"douyin_id:{douyin_id}"})
    return {
        "visibleName": display_name,
        "normalizedName": normalized_name,
        "douyinId": douyin_id,
        "stableKeys": stable_keys,
    }


async def _first_scrollable_friends_element(page, selectors):
    for selector in selectors:
        try:
            handle = await page.locator(selector).first.element_handle()
        except Exception:
            handle = None
        if handle:
            return selector, handle
    return "", None


async def scroll_and_select_user(page, user, account_name, targets, friend_scan_config=None, index_targets=None, collector=None):
    # Resolves a stored 抖音号 to the nickname the friend wears today. Callers that
    # already attached one at page creation hand it in so the first user_detail
    # response is not missed.
    collector = collector or FriendIdentityCollector().attach(page)
    friends_tab_selector = 'xpath=//*[@id="sub-app"]/div/div/div[1]/div[2]'
    target_selectors = (
        '#sub-app li[role="listitem"]:has([class*="item-header-name-"])',
        '#sub-app li.semi-list-item:has([class*="item-header-name-"])',
        'xpath=//*[@id="sub-app"]/div/div[1]/div[2]/div[2]'
        '//div[contains(@class, "semi-list-item-body")]',
        'xpath=//*[@id="sub-app"]//div[contains(@class, "semi-list-item-body") and .//span[contains(@class, "item-header-name-")]]',
        'xpath=//*[@id="sub-app"]//li[.//span[contains(@class, "item-header-name-")]]',
    )
    scrollable_friends_selectors = (
        '#sub-app [role="grid"]',
        '#sub-app .ReactVirtualized__Grid',
        'xpath=//*[@id="sub-app"]/div/div[1]/div[2]/div[2]/div/div/div[3]/div/div/div/ul/div',
        'xpath=//*[@id="sub-app"]//ul/div',
        'xpath=//*[@id="sub-app"]//div[contains(@class, "semi-list")]//ul/..',
    )
    no_more_selectors = (
        'xpath=//div[contains(@class, "no-more-tip-ftdJnu")]',
        'xpath=//*[@id="sub-app"]//*[contains(text(), "没有更多") or contains(text(), "暂无")]',
    )
    loading_selectors = (
        'xpath=//div[contains(@class, "semi-spin")]',
        'xpath=//*[@id="sub-app"]//*[contains(@class, "loading") or contains(@class, "Loading")]',
    )
    friend_scan_config = friend_scan_config or _normalize_friend_list_scan_config({})
    max_scan_seconds = friend_scan_config["maxScanSeconds"]
    idle_scan_seconds = friend_scan_config["idleScanSeconds"]
    scroll_step_px = friend_scan_config["scrollStepPx"]
    scroll_delay_seconds = friend_scan_config["scrollDelaySeconds"]
    ready_timeout_seconds = 60
    empty_grace_seconds = 30

    ready_errors = []
    active_target_selector = ""
    for ready_attempt in range(2):
        logger.debug("Account %s is opening the friends tab attempt=%s", account_name, ready_attempt + 1)
        await _open_friends_tab(page, account_name, friends_tab_selector, target_selectors)

        try:
            active_target_selector, _ = await _wait_for_friend_list_ready(
                page,
                account_name,
                target_selectors,
                no_more_selectors,
                loading_selectors,
                timeout_seconds=ready_timeout_seconds,
                empty_grace_seconds=empty_grace_seconds,
            )
            break
        except FriendListUnavailableError as exc:
            ready_errors.append(str(exc))
            if ready_attempt == 0:
                await _reopen_friend_chat_page(page, account_name)
                continue
            raise FriendListUnavailableError(
                f"friend_list_unavailable for {account_name}: no friend entries after "
                f"two {ready_timeout_seconds}s waits; first={ready_errors[0]}; second={ready_errors[-1]}"
            ) from exc

    await asyncio.sleep(2)

    normalized_targets = {}
    normalized_index_targets = {}
    found_usernames = set()
    remaining_targets = set()
    remaining_index_targets = set()
    friend_index = {}
    scan_started_at = asyncio.get_running_loop().time()
    last_new_friend_at = scan_started_at
    logger.debug(
        "Account %s friend list scan config maxScanSeconds=%s idleScanSeconds=%s scrollStepPx=%s scrollDelaySeconds=%s readyTimeoutSeconds=%s emptyGraceSeconds=%s",
        account_name,
        max_scan_seconds,
        idle_scan_seconds,
        scroll_step_px,
        scroll_delay_seconds,
        ready_timeout_seconds,
        empty_grace_seconds,
    )

    def refresh_target_maps():
        """Resolve stored 抖音号 targets against the nicknames seen so far.

        user_detail answers keep arriving while the list scrolls, so a target that
        was unresolvable on an early pass becomes resolvable later.  Rebuilding
        from found_usernames keeps exactly one key per target.
        """
        normalized = _build_normalized_target_map(targets, collector.id_to_name)
        normalized_index = _build_normalized_target_map(index_targets or targets, collector.id_to_name)
        return (
            normalized,
            normalized_index,
            {key for key in normalized if key not in found_usernames},
            {key for key in normalized_index if key not in found_usernames},
        )

    def missing_target_names():
        return sorted(normalized_targets[item] for item in remaining_targets)

    def missing_index_target_names():
        return sorted(normalized_index_targets[item] for item in remaining_index_targets)

    def persist_index(scan_complete):
        _persist_friend_index(
            user,
            friend_index,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            scan_complete=scan_complete,
            missing_targets=missing_index_target_names(),
        )

    while True:
        await collector.drain()
        normalized_targets, normalized_index_targets, remaining_targets, remaining_index_targets = refresh_target_maps()
        now_monotonic = asyncio.get_running_loop().time()
        if now_monotonic - scan_started_at > max_scan_seconds:
            persist_index(False)
            logger.warning(
                "Account %s friend list scan timed out after %ss. Missing delivery targets: %s; missing indexed targets=%s; scannedFriends=%s",
                account_name,
                max_scan_seconds,
                missing_target_names(),
                missing_index_target_names(),
                len(found_usernames),
            )
            raise FriendListIncompleteError(
                f"friend_list_incomplete for {account_name}: scan timed out after {max_scan_seconds}s; "
                f"missingTargets={missing_target_names()}; scannedFriends={len(found_usernames)}"
            )

        await _dismiss_non_login_dialogs(page, account_name, "friend_list_scan")
        selector, target_locator = await _first_non_empty_locator(page, (active_target_selector,) + tuple(target_selectors))
        if not target_locator:
            active_target_selector, _ = await _wait_for_friend_list_ready(
                page,
                account_name,
                target_selectors,
                no_more_selectors,
                loading_selectors,
                timeout_seconds=30,
                empty_grace_seconds=15,
            )
            continue
        target_elements = await target_locator.all()
        clicked_delivery_target = False

        for element in target_elements:
            friend_record = await _extract_friend_record(element, collector)
            if not friend_record:
                continue

            target_name = friend_record["visibleName"]
            normalized_target_name = friend_record["normalizedName"]
            if not normalized_target_name:
                continue

            if normalized_target_name in found_usernames:
                continue
            found_usernames.add(normalized_target_name)
            friend_index[normalized_target_name] = friend_record
            remaining_index_targets.discard(normalized_target_name)
            last_new_friend_at = asyncio.get_running_loop().time()
            logger.debug("Account %s found friend entry %s", account_name, target_name)

            matched_target_name = normalized_targets.get(normalized_target_name)
            if matched_target_name and normalized_target_name in remaining_targets:
                await element.click()
                logger.info("Account %s selected target friend %s", account_name, target_name)
                if matched_target_name != target_name:
                    logger.info(
                        "Account %s normalized target %r matched visible friend %r",
                        account_name,
                        matched_target_name,
                        target_name,
                    )
                scan_paused_at = asyncio.get_running_loop().time()
                yield matched_target_name
                scan_resumed_at = asyncio.get_running_loop().time()
                scan_pause_seconds = max(0, scan_resumed_at - scan_paused_at)
                if scan_pause_seconds:
                    scan_started_at += scan_pause_seconds
                    last_new_friend_at += scan_pause_seconds
                    logger.debug(
                        "Account %s paused friend list scan timer for %.1fs while delivering %s",
                        account_name,
                        scan_pause_seconds,
                        matched_target_name,
                    )

                remaining_targets.discard(normalized_target_name)
                clicked_delivery_target = True
                if not remaining_targets and not remaining_index_targets:
                    logger.info("Account %s found all delivery and indexed target friends", account_name)
                    persist_index(True)
                    return
                break
        if clicked_delivery_target:
            continue

        if not remaining_targets and not remaining_index_targets:
            logger.info("Account %s found all delivery and indexed target friends", account_name)
            persist_index(True)
            return

        else:
            if await _selector_visible(page, no_more_selectors):
                persist_index(True)
                logger.warning(
                    "Account %s reached the end of the friend list. Missing delivery targets: %s; missing indexed targets=%s",
                    account_name,
                    missing_target_names(),
                    missing_index_target_names(),
                )
                return

            now_monotonic = asyncio.get_running_loop().time()
            if found_usernames and now_monotonic - last_new_friend_at > idle_scan_seconds:
                persist_index(False)
                logger.warning(
                    "Account %s friend list scan made no progress for %ss. Missing delivery targets: %s; missing indexed targets=%s; scannedFriends=%s",
                    account_name,
                    idle_scan_seconds,
                    missing_target_names(),
                    missing_index_target_names(),
                    len(found_usernames),
                )
                raise FriendListIncompleteError(
                    f"friend_list_incomplete for {account_name}: no new friends for {idle_scan_seconds}s; "
                    f"missingTargets={missing_target_names()}; scannedFriends={len(found_usernames)}"
                )

            if await _selector_visible(page, loading_selectors):
                logger.debug("Account %s is waiting for more friends to load", account_name)
                await asyncio.sleep(scroll_delay_seconds)

            scrollable_selector, scrollable_element = await _first_scrollable_friends_element(page, scrollable_friends_selectors)
            if not scrollable_element:
                raise FriendListUnavailableError(
                    f"friend_list_unavailable for {account_name}: could not find the friend list scroll container"
                )

            logger.debug("Account %s scrolls friend list via selector %s", account_name, scrollable_selector)
            await page.evaluate(f"(element) => element.scrollTop += {scroll_step_px}", scrollable_element)
            await asyncio.sleep(scroll_delay_seconds)


def _is_manual_run():
    return os.getenv("SPARKFLOW_MANUAL_RUN") == "1"


def _schedule_timezone():
    timezone_name = (
        str(os.getenv("SPARKFLOW_TIMEZONE") or "").strip()
        or str(os.getenv("TZ") or "").strip()
        or "Asia/Shanghai"
    )
    try:
        return ZoneInfo(timezone_name)
    except Exception:
        if timezone_name == "Asia/Shanghai":
            logger.warning("Falling back to fixed UTC+8 because %r is unavailable", timezone_name)
            return timezone(timedelta(hours=8), name="Asia/Shanghai")
        logger.warning("Falling back to system timezone because %r is unavailable", timezone_name)
        return datetime.now().astimezone().tzinfo


def _normalize_send_window(config):
    raw = config.get("dailySendWindow", {}) or {}
    normalized = {
        "enabled": bool(raw.get("enabled", False)),
        "startHour": int(raw.get("startHour", 10)),
        "endHour": int(raw.get("endHour", 18)),
        "scheduleIntervalMinutes": max(1, int(raw.get("scheduleIntervalMinutes", 10))),
    }
    if normalized["startHour"] < 0 or normalized["startHour"] > 23:
        normalized["enabled"] = False
    if normalized["endHour"] < 1 or normalized["endHour"] > 24:
        normalized["enabled"] = False
    if normalized["endHour"] <= normalized["startHour"]:
        normalized["enabled"] = False
    if bool(raw.get("enabled", False)) and not normalized["enabled"]:
        logger.warning("Invalid dailySendWindow=%s, disabling windowed sending for this run", raw)
    return normalized


def _account_identity(user):
    return str(user.get("unique_id") or user.get("username") or "unknown").strip()


def _parse_sent_at(raw_value, local_tz):
    return parse_sent_at(raw_value, local_tz)


def _manual_run_failed_only():
    return _is_manual_run() and os.getenv("SPARKFLOW_MANUAL_FAILED_ONLY") == "1"


def _manual_run_unsent_only():
    return _is_manual_run() and os.getenv("SPARKFLOW_MANUAL_UNSENT_ONLY") == "1"


def _requested_account_refs():
    raw = os.getenv("SPARKFLOW_ACCOUNT_REFS")
    if raw is None:
        return None
    return {item.strip() for item in raw.split(",") if item.strip()}


def _unsent_retry_max_attempts():
    raw_value = str(os.getenv("SPARKFLOW_UNSENT_RETRY_MAX_ATTEMPTS") or "3").strip()
    try:
        return max(1, int(raw_value))
    except ValueError:
        logger.warning("Invalid SPARKFLOW_UNSENT_RETRY_MAX_ATTEMPTS=%r, using 3", raw_value)
        return 3


def _target_sent_today(user, target_name, now):
    return target_is_strong_confirmed_today(user, target_name, now)


def _target_unconfirmed_today(user, target_name, now):
    history = dict(user.get("message_history") or {})
    entry = dict(history.get(target_name) or {})
    sent_at = _parse_sent_at(entry.get("sentAt"), now.tzinfo)
    return bool(
        sent_at
        and sent_at.date() == now.date()
        and not _target_sent_today(user, target_name, now)
    )


def _target_failed_today(user, target_name, now):
    queue = dict(user.get("failure_queue") or {})
    entry = queue.get(target_name) or {}
    last_attempt_at = _parse_sent_at(entry.get("lastAttemptAt"), now.tzinfo)
    return bool(last_attempt_at and last_attempt_at.date() == now.date())


def _account_failure_entry_today(user, now):
    entry = dict(user.get("account_failure") or {})
    last_attempt_at = _parse_sent_at(entry.get("lastAttemptAt"), now.tzinfo)
    if last_attempt_at and last_attempt_at.date() == now.date():
        return entry
    return {}


def _account_failure_attempts_today(user, now):
    return _coerce_attempt_count(_account_failure_entry_today(user, now))


def _account_failure_pause_after_attempts():
    raw_value = str(os.getenv("SPARKFLOW_ACCOUNT_FAILURE_PAUSE_AFTER_ATTEMPTS") or "2").strip()
    try:
        return max(1, int(raw_value))
    except ValueError:
        logger.warning("Invalid SPARKFLOW_ACCOUNT_FAILURE_PAUSE_AFTER_ATTEMPTS=%r, using 2", raw_value)
        return 2


def _temporary_account_failure_cooldown_minutes():
    raw_value = str(os.getenv("SPARKFLOW_TEMP_ACCOUNT_FAILURE_COOLDOWN_MINUTES") or "60").strip()
    try:
        return max(1, int(raw_value))
    except ValueError:
        logger.warning("Invalid SPARKFLOW_TEMP_ACCOUNT_FAILURE_COOLDOWN_MINUTES=%r, using 60", raw_value)
        return 60


def _account_paused_by_failure_today(user, now):
    entry = _account_failure_entry_today(user, now)
    if _coerce_attempt_count(entry) < _account_failure_pause_after_attempts():
        return False

    category = str(entry.get("category") or "")
    if category not in TEMPORARY_ACCOUNT_FAILURE_CATEGORIES:
        return True

    last_attempt_at = _parse_sent_at(entry.get("lastAttemptAt"), now.tzinfo)
    if not last_attempt_at:
        return True

    cooldown_seconds = _temporary_account_failure_cooldown_minutes() * 60
    elapsed_seconds = (now - last_attempt_at).total_seconds()
    if elapsed_seconds < cooldown_seconds:
        remaining_seconds = int(cooldown_seconds - elapsed_seconds)
        logger.info(
            "Account %s temporary account failure category=%s cooling down for %ss",
            user.get("username") or user.get("unique_id") or "unknown",
            category,
            remaining_seconds,
        )
        return True

    logger.info(
        "Account %s temporary account failure category=%s cooldown expired; allowing retry",
        user.get("username") or user.get("unique_id") or "unknown",
        category,
    )
    return False


def _friend_index_complete_today(user, now):
    meta = dict(user.get("friend_index_meta") or {})
    last_scan_at = _parse_sent_at(meta.get("lastScanAt"), now.tzinfo)
    return bool(last_scan_at and last_scan_at.date() == now.date() and meta.get("lastScanComplete"))


def _target_failure_attempts_today(user, target_name, now):
    queue = dict(user.get("failure_queue") or {})
    entry = queue.get(target_name) or {}
    last_attempt_at = _parse_sent_at(entry.get("lastAttemptAt"), now.tzinfo)
    if not last_attempt_at or last_attempt_at.date() != now.date():
        return 0
    try:
        return int(entry.get("attemptCount") or 0)
    except (TypeError, ValueError):
        return 0


def _target_failure_category_today(user, target_name, now):
    queue = dict(user.get("failure_queue") or {})
    entry = queue.get(target_name) or {}
    last_attempt_at = _parse_sent_at(entry.get("lastAttemptAt"), now.tzinfo)
    if not last_attempt_at or last_attempt_at.date() != now.date():
        return ""
    return str(entry.get("category") or "").strip()


def _target_has_non_retryable_failure_today(user, target_name, now):
    return _target_failure_category_today(user, target_name, now) in {
        "protocol_check_message_not_pass",
        "protocol_check_message_self_visible",
        "protocol_user_blocked",
        "protocol_user_not_in_conversation",
        "protocol_check_conversation_not_pass",
    }


def _pending_failed_targets(user, now):
    queue = dict(user.get("failure_queue") or {})
    targets = []
    account_failure = _account_failure_entry_today(user, now)
    account_failure_targets = list(account_failure.get("affectedTargets") or [])
    if account_failure_targets:
        for target_name in account_failure_targets:
            if target_name in (user.get("targets") or []) and not _target_sent_today(user, target_name, now):
                targets.append(target_name)
        if targets:
            return targets

    for target_name in user.get("targets") or []:
        if _target_sent_today(user, target_name, now):
            continue
        if _target_unconfirmed_today(user, target_name, now):
            targets.append(target_name)
            continue
        if target_name in queue and _target_failed_today(user, target_name, now):
            targets.append(target_name)
    return targets


def _pending_unsent_targets(user, now):
    retry_targets = []
    skipped_targets = []
    max_attempts = _unsent_retry_max_attempts()
    for target_name in user.get("targets") or []:
        if _target_sent_today(user, target_name, now):
            continue
        if _target_has_non_retryable_failure_today(user, target_name, now):
            skipped_targets.append(f"{target_name}(non_retryable)")
            continue
        attempts_today = _target_failure_attempts_today(user, target_name, now)
        if attempts_today >= max_attempts:
            skipped_targets.append(f"{target_name}({attempts_today})")
            continue
        retry_targets.append(target_name)
    return retry_targets, skipped_targets


def _scheduled_send_time(user, target_name, send_window, now):
    window_minutes = (send_window["endHour"] - send_window["startHour"]) * 60
    start_of_window = now.replace(
        hour=send_window["startHour"],
        minute=0,
        second=0,
        microsecond=0,
    )
    seed = f"{now.date().isoformat()}|{_account_identity(user)}|{target_name}"
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    offset_minutes = int.from_bytes(digest[:8], "big") % window_minutes
    return start_of_window + timedelta(minutes=offset_minutes)


def _select_due_targets(user, send_window, now):
    targets = list(user.get("targets") or [])
    if not send_window.get("enabled") or _is_manual_run():
        return targets, [], [], []

    window_start = now.replace(
        hour=send_window["startHour"],
        minute=0,
        second=0,
        microsecond=0,
    )
    window_end = now.replace(
        hour=send_window["endHour"],
        minute=0,
        second=0,
        microsecond=0,
    )
    window_grace_end = window_end + timedelta(minutes=send_window["scheduleIntervalMinutes"])
    if now < window_start or now > window_grace_end:
        already_sent = []
        pending_targets = []
        queued_failures = []
        for target_name in targets:
            if _target_sent_today(user, target_name, now):
                already_sent.append(target_name)
                continue
            if _target_failed_today(user, target_name, now):
                queued_failures.append(target_name)
                continue
            pending_targets.append((target_name, _scheduled_send_time(user, target_name, send_window, now)))
        return [], already_sent, pending_targets, queued_failures

    due_targets = []
    already_sent = []
    pending_targets = []
    queued_failures = []
    account_paused = _account_paused_by_failure_today(user, now)
    for target_name in targets:
        if _target_sent_today(user, target_name, now):
            already_sent.append(target_name)
            continue
        if _target_failed_today(user, target_name, now):
            queued_failures.append(target_name)
            continue
        scheduled_at = _scheduled_send_time(user, target_name, send_window, now)
        if account_paused:
            pending_targets.append((target_name, scheduled_at))
        elif now >= scheduled_at:
            due_targets.append(target_name)
        else:
            pending_targets.append((target_name, scheduled_at))
    if account_paused and (pending_targets or queued_failures):
        logger.info(
            "Account %s is paused by account-level failure attempts=%s threshold=%s",
            user.get("username") or user.get("unique_id") or "unknown",
            _account_failure_attempts_today(user, now),
            _account_failure_pause_after_attempts(),
        )
    return due_targets, already_sent, pending_targets, queued_failures


def _prepare_active_users_for_run(active_config, active_user_data):
    schedule_tz = _schedule_timezone()
    now = datetime.now(schedule_tz)

    if _manual_run_failed_only():
        logger.info("SPARKFLOW_MANUAL_RUN=1, retrying queued failures only")
        runnable_users = []
        for user in active_user_data:
            retry_targets = _pending_failed_targets(user, now)
            already_sent = [target for target in user.get("targets") or [] if _target_sent_today(user, target, now)]
            logger.info(
                "manual-retry user=%s retryTargetCount=%s strongConfirmedToday=%s",
                user.get("username", "unknown"),
                len(retry_targets),
                len(already_sent),
            )
            if retry_targets:
                runnable_user = dict(user)
                runnable_user["targets"] = retry_targets
                runnable_users.append(runnable_user)
        if not runnable_users:
            logger.info("No queued failures are pending for manual retry")
        return runnable_users

    if _manual_run_unsent_only():
        logger.info(
            "SPARKFLOW_MANUAL_RUN=1 and SPARKFLOW_MANUAL_UNSENT_ONLY=1, retrying today's unsent targets only"
        )
        runnable_users = []
        for user in active_user_data:
            retry_targets, skipped_targets = _pending_unsent_targets(user, now)
            already_sent = [target for target in user.get("targets") or [] if _target_sent_today(user, target, now)]
            logger.info(
                "manual-unsent user=%s retryTargetCount=%s strongConfirmedToday=%s skippedCount=%s",
                user.get("username", "unknown"),
                len(retry_targets),
                len(already_sent),
                len(skipped_targets),
            )
            if retry_targets:
                runnable_user = dict(user)
                runnable_user["targets"] = retry_targets
                runnable_users.append(runnable_user)
        if not runnable_users:
            logger.info("No unsent targets are pending for manual retry")
        return runnable_users

    if _is_manual_run():
        logger.info("SPARKFLOW_MANUAL_RUN=1, bypassing daily send window")
        return [dict(user, targets=list(user.get("targets") or [])) for user in active_user_data]

    send_window = _normalize_send_window(active_config)
    if not send_window.get("enabled"):
        return [dict(user, targets=list(user.get("targets") or [])) for user in active_user_data]

    logger.info(
        "dailySendWindow enabled startHour=%s endHour=%s intervalMinutes=%s timezone=%s now=%s",
        send_window["startHour"],
        send_window["endHour"],
        send_window["scheduleIntervalMinutes"],
        getattr(schedule_tz, "key", str(schedule_tz)),
        now.isoformat(timespec="seconds"),
    )

    runnable_users = []
    for user in active_user_data:
        due_targets, already_sent, pending_targets, queued_failures = _select_due_targets(user, send_window, now)
        logger.info(
            "windowed user=%s due=%s strongConfirmed=%s pending=%s queuedFailures=%s",
            user.get("username", "unknown"),
            len(due_targets),
            len(already_sent),
            len(pending_targets),
            len(queued_failures),
        )
        if due_targets:
            runnable_user = dict(user)
            runnable_user["targets"] = due_targets
            runnable_users.append(runnable_user)

    if not runnable_users:
        logger.info("No targets are due for the current windowed run")
    return runnable_users


def _account_match_tokens(user):
    tokens = set()
    username = str(user.get("username") or "").strip()
    unique_id = str(user.get("unique_id") or "").strip()
    normalized_unique_id = normalize_unique_id(unique_id)
    if username:
        tokens.add(username.lower())
    if unique_id:
        tokens.add(unique_id.lower())
    if normalized_unique_id:
        tokens.add(normalized_unique_id.lower())
    return tokens


def _find_matching_account(accounts, user):
    target_username = str(user.get("username") or "").strip()
    target_unique_id = normalize_unique_id(user.get("unique_id"))
    if not target_username and not target_unique_id:
        return None

    for account in accounts:
        account_username = str(account.get("username") or "").strip()
        account_unique_id = normalize_unique_id(account.get("unique_id"))
        if target_unique_id and account_unique_id == target_unique_id:
            return account
        if target_username and account_username == target_username:
            return account
    return None


def _coerce_attempt_count(entry):
    try:
        return int(dict(entry or {}).get("attemptCount") or 0)
    except (TypeError, ValueError):
        return 0


def _persist_account_send_failure(user, category, reason, attempted_at, affected_targets=None):
    accounts = get_userData(force_reload=True)
    matched_account = _find_matching_account(accounts, user)
    if matched_account is None:
        logger.warning(
            "Could not find account to persist account-level browser failure for user=%s",
            user.get("username", "unknown"),
        )
        return

    affected_targets = list(affected_targets or [])
    existing_entry = dict(matched_account.get("account_failure") or {})
    entry = {
        "category": category,
        "reason": reason,
        "firstAttemptAt": existing_entry.get("firstAttemptAt") or attempted_at,
        "lastAttemptAt": attempted_at,
        "attemptCount": _coerce_attempt_count(existing_entry) + 1,
        "lastRunMode": _current_run_mode(),
        "affectedTargets": affected_targets,
    }
    failure_queue = dict(matched_account.get("failure_queue") or {})
    for target_name in affected_targets:
        failure_queue.pop(target_name, None)
    if failure_queue:
        matched_account["failure_queue"] = failure_queue
    else:
        matched_account.pop("failure_queue", None)
    matched_account["account_failure"] = entry
    save_userData(accounts)

    user["account_failure"] = dict(entry)
    user_queue = dict(user.get("failure_queue") or {})
    for target_name in affected_targets:
        user_queue.pop(target_name, None)
    if user_queue:
        user["failure_queue"] = user_queue
    else:
        user.pop("failure_queue", None)
    logger.warning(
        "Paused browser sends for account %s category=%s affectedTargets=%s reason=%s",
        matched_account.get("username", "unknown"),
        category,
        affected_targets,
        reason,
    )


def _clear_account_send_failure(user):
    accounts = get_userData(force_reload=True)
    matched_account = _find_matching_account(accounts, user)
    changed = False
    if matched_account is not None and matched_account.pop("account_failure", None) is not None:
        changed = True
    if changed:
        save_userData(accounts)
    user.pop("account_failure", None)


def _persist_friend_index(user, friend_records, scanned_at, *, scan_complete, missing_targets=None):
    accounts = get_userData(force_reload=True)
    matched_account = _find_matching_account(accounts, user)
    if matched_account is None:
        logger.warning(
            "Could not find account to persist friend index for user=%s",
            user.get("username", "unknown"),
        )
        return

    existing_index = dict(matched_account.get("friend_index") or {})
    for normalized_name, record in (friend_records or {}).items():
        entry = dict(existing_index.get(normalized_name) or {})
        entry.update(
            {
                "visibleName": record.get("visibleName") or "",
                "normalizedName": normalized_name,
                "stableKeys": list(record.get("stableKeys") or []),
                "lastSeenAt": scanned_at,
            }
        )
        existing_index[normalized_name] = entry

    meta = {
        "lastScanAt": scanned_at,
        "lastScanComplete": bool(scan_complete),
        "scannedCount": len(friend_records or {}),
        "missingTargets": list(missing_targets or []),
    }
    matched_account["friend_index"] = existing_index
    matched_account["friend_index_meta"] = meta
    if scan_complete and friend_records:
        matched_account.pop("account_failure", None)
    save_userData(accounts)

    user["friend_index"] = dict(existing_index)
    user["friend_index_meta"] = dict(meta)
    if scan_complete and friend_records:
        user.pop("account_failure", None)

    logger.info(
        "Persisted friend index for %s scanned=%s complete=%s missingTargets=%s",
        matched_account.get("username", "unknown"),
        len(friend_records or {}),
        bool(scan_complete),
        list(missing_targets or []),
    )


def _persist_browser_send_failure(user, target_name, message, category, reason, attempted_at, server_receipt=None):
    accounts = get_userData(force_reload=True)
    matched_account = _find_matching_account(accounts, user)
    if matched_account is None:
        logger.warning(
            "Could not find account to persist browser send failure for user=%s target=%s",
            user.get("username", "unknown"),
            target_name,
        )
        return

    queue = dict(matched_account.get("failure_queue") or {})
    existing_entry = dict(queue.get(target_name) or {})
    queue[target_name] = {
        "category": category,
        "reason": reason,
        "message": message,
        "firstAttemptAt": existing_entry.get("firstAttemptAt") or attempted_at,
        "lastAttemptAt": attempted_at,
        "attemptCount": _coerce_attempt_count(existing_entry) + 1,
        "lastRunMode": _current_run_mode(),
    }
    if server_receipt:
        queue[target_name]["serverReceipt"] = server_receipt
    matched_account["failure_queue"] = queue
    save_userData(accounts)

    user_queue = dict(user.get("failure_queue") or {})
    user_queue[target_name] = dict(queue[target_name])
    user["failure_queue"] = user_queue

    logger.warning(
        "Queued failed browser send for %s/%s category=%s reason=%s",
        matched_account.get("username", "unknown"),
        target_name,
        category,
        reason,
    )


def _persist_browser_send_success(user, target_name, message, sent_at, server_receipt=None):
    accounts = get_userData(force_reload=True)
    matched_account = _find_matching_account(accounts, user)
    if matched_account is None:
        logger.warning(
            "Could not find account to persist browser send history for user=%s target=%s",
            user.get("username", "unknown"),
            target_name,
        )
        return

    receipt_summary = ""
    if isinstance(server_receipt, dict):
        receipt_summary = "message_send http={} logid={}".format(
            server_receipt.get("httpStatus"),
            server_receipt.get("logid") or "",
        )
    strong_entry = {
        "message": message,
        "sentAt": sent_at,
        "status": "confirmed",
        "confirmationLevel": "strong",
        "confirmationSource": "cdp_message_send_receipt" if server_receipt else "browser_visible_count_increased",
        "confirmationDetail": receipt_summary,
        "needsVerification": False,
    }
    if server_receipt:
        strong_entry["serverReceipt"] = server_receipt
    history = dict(matched_account.get("message_history") or {})
    history[target_name] = strong_entry
    matched_account["message_history"] = history
    queue = dict(matched_account.get("failure_queue") or {})
    queue.pop(target_name, None)
    if queue:
        matched_account["failure_queue"] = queue
    else:
        matched_account.pop("failure_queue", None)
    matched_account.pop("account_failure", None)
    save_userData(accounts)

    user_history = dict(user.get("message_history") or {})
    user_history[target_name] = dict(strong_entry)
    user["message_history"] = user_history
    user_queue = dict(user.get("failure_queue") or {})
    user_queue.pop(target_name, None)
    if user_queue:
        user["failure_queue"] = user_queue
    else:
        user.pop("failure_queue", None)
    user.pop("account_failure", None)

    logger.info(
        "Persisted browser send history for %s/%s at %s",
        matched_account.get("username", "unknown"),
        target_name,
        sent_at,
    )


def _split_sender_modes(active_config, runnable_user_data):
    if not active_config.get("useProtocolSender", True):
        return [], runnable_user_data

    browser_sender_accounts = {
        str(item).strip().lower()
        for item in (active_config.get("browserSenderAccounts") or [])
        if str(item).strip()
    }
    if not browser_sender_accounts:
        return runnable_user_data, []

    protocol_users = []
    browser_users = []
    for user in runnable_user_data:
        if _account_match_tokens(user) & browser_sender_accounts:
            browser_users.append(user)
        else:
            protocol_users.append(user)
    return protocol_users, browser_users


def _pid_is_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if getattr(exc, "winerror", None) == 87 or exc.errno == errno.ESRCH:
            return False
        if exc.errno in (errno.EPERM, errno.EACCES):
            return True
        raise
    return True


def _extract_lock_pid(raw):
    for line in str(raw or "").splitlines():
        if line.startswith("pid="):
            try:
                return int(line.split("=", 1)[1].strip())
            except (TypeError, ValueError):
                return None
    try:
        return int(str(raw or "").strip())
    except (TypeError, ValueError):
        return None


def _browser_account_lock_is_stale(lock_path, raw):
    pid = _extract_lock_pid(raw)
    if pid is not None and not _pid_is_alive(pid):
        return True, f"missing pid={pid}"
    try:
        age_seconds = datetime.now(timezone.utc).timestamp() - lock_path.stat().st_mtime
    except OSError:
        return True, "missing lock file"
    if age_seconds > 7200:
        return True, f"older than 7200s pid={pid}"
    return False, ""


async def _acquire_browser_account_lock(user, account_name):
    lock_dir = Path("logs/browser-account-locks")
    lock_dir.mkdir(parents=True, exist_ok=True)
    identity = _account_identity(user) or account_name
    lock_path = lock_dir / f"{_safe_name(identity)}.lock"
    started_at = asyncio.get_running_loop().time()
    last_logged_at = 0

    while True:
        try:
            handle = lock_path.open("x", encoding="utf-8")
            handle.write(
                f"pid={os.getpid()}\n"
                f"account={account_name}\n"
                f"createdAt={datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
            )
            handle.flush()
            logger.debug("Acquired browser account lock for %s at %s", account_name, lock_path)
            return handle, lock_path
        except FileExistsError:
            raw = lock_path.read_text(encoding="utf-8", errors="ignore")
            is_stale, stale_reason = _browser_account_lock_is_stale(lock_path, raw)
            if is_stale:
                logger.warning(
                    "Removing stale browser account lock for %s at %s: %s",
                    account_name,
                    lock_path,
                    stale_reason,
                )
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                continue

            now = asyncio.get_running_loop().time()
            if now - started_at > 7200:
                raise RuntimeError(f"timed out waiting for browser account lock for {account_name}")
            if now - last_logged_at >= 30:
                logger.info(
                    "Waiting for existing browser account lock for %s at %s",
                    account_name,
                    lock_path,
                )
                last_logged_at = now
            await asyncio.sleep(5)


def _release_browser_account_lock(handle, lock_path, account_name):
    try:
        handle.close()
    finally:
        try:
            lock_path.unlink()
            logger.debug("Released browser account lock for %s at %s", account_name, lock_path)
        except FileNotFoundError:
            pass


def _browser_account_timeout_seconds(friend_scan_config, target_count):
    raw_value = str(os.getenv("SPARKFLOW_BROWSER_ACCOUNT_TIMEOUT_SECONDS") or "").strip()
    if raw_value:
        try:
            return max(300, int(raw_value))
        except ValueError:
            logger.warning(
                "Invalid SPARKFLOW_BROWSER_ACCOUNT_TIMEOUT_SECONDS=%r, using calculated timeout",
                raw_value,
            )

    scan_seconds = int((friend_scan_config or {}).get("maxScanSeconds") or 300)
    # Bound one account run even if Playwright or the page wedges below our selector timeouts.
    return max(900, scan_seconds + 420 + max(1, target_count) * 240)


async def run_browser_tasks(active_config, browser_user_data):
    if not browser_user_data:
        return

    send_strategy = _normalize_send_strategy(active_config)
    friend_scan_config = _normalize_friend_list_scan_config(active_config)
    profile_config = _normalize_persistent_profile_config(active_config)
    network_mode = await select_douyin_network_mode(CREATOR_HOME_URL)
    logger.info("Selected Douyin task network route=%s", network_mode)
    semaphore = asyncio.Semaphore(active_config["taskCount"] if active_config["multiTask"] else 1)
    tasks = []

    if profile_config["enabled"]:
        logger.info(
            "Browser sender persistent profiles enabled root=%s seedCookiesWhenEmpty=%s syncStoredCookiesBeforeRun=%s refreshStoredCookiesAfterLogin=%s",
            profile_config["root"],
            profile_config["seedCookiesWhenEmpty"],
            profile_config["syncStoredCookiesBeforeRun"],
            profile_config["refreshStoredCookiesAfterLogin"],
        )
        for user in browser_user_data:
            logger.info(
                "Using persistent browser sender for user=%s targetCount=%s",
                user.get("username", "unknown"),
                len(user["targets"]),
            )
            tasks.append(do_user_task(None, user, semaphore, send_strategy, profile_config, friend_scan_config, network_mode))
        await asyncio.gather(*tasks)
        return

    playwright, browser = await get_browser(network_mode=network_mode)
    try:
        for user in browser_user_data:
            logger.info(
                "Using browser sender for user=%s targetCount=%s",
                user.get("username", "unknown"),
                len(user["targets"]),
            )
            tasks.append(do_user_task(browser, user, semaphore, send_strategy, profile_config, friend_scan_config, network_mode))

        await asyncio.gather(*tasks)
    finally:
        await playwright.stop()
        await browser.close()


async def do_user_task(browser, user, semaphore, send_strategy, profile_config, friend_scan_config, network_mode):
    async with semaphore:
        account_name = user.get("username", "unknown")
        account_lock_handle = None
        account_lock_path = None
        try:
            account_lock_handle, account_lock_path = await _acquire_browser_account_lock(user, account_name)
            timeout_seconds = _browser_account_timeout_seconds(
                friend_scan_config,
                len(user.get("targets") or []),
            )
            logger.info(
                "Account %s browser sender timeout guard is %ss",
                account_name,
                timeout_seconds,
            )
            try:
                await asyncio.wait_for(
                    _do_user_task_locked(
                        browser,
                        user,
                        send_strategy,
                        profile_config,
                        friend_scan_config,
                        account_name,
                    network_mode,
                    ),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                attempted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                reason = f"browser sender exceeded {timeout_seconds}s timeout guard"
                logger.exception("Account %s browser sender timed out", account_name)
                for target_name in user.get("targets") or []:
                    _persist_browser_send_failure(
                        user,
                        target_name,
                        "",
                        "timeout",
                        reason,
                        attempted_at,
                    )
        finally:
            if account_lock_handle is not None:
                _release_browser_account_lock(account_lock_handle, account_lock_path, account_name)


async def _do_user_task_locked(browser, user, send_strategy, profile_config, friend_scan_config, account_name, network_mode):
    cookies = user["cookies"]
    targets = user["targets"]
    start_delay = _random_delay_seconds(
        send_strategy,
        "accountStartDelaySecondsMin",
        "accountStartDelaySecondsMax",
    )
    await _sleep_with_log(start_delay, "Delaying browser sender start", account_name)

    owned_playwright = None
    profile_dir = None
    if profile_config["enabled"]:
        owned_playwright, context, profile_dir = await get_persistent_browser_context(
            _account_profile_name(user),
            root=profile_config["root"],
        network_mode=network_mode,
        )
        logger.info("Opened persistent browser profile for %s at %s", account_name, profile_dir)
        if profile_config["syncStoredCookiesBeforeRun"]:
            await apply_stored_cookies_to_profile(context, cookies, account_name)
        elif profile_config["seedCookiesWhenEmpty"]:
            await apply_stored_cookies_to_profile(context, cookies, account_name, only_when_empty=True)
    else:
        context = await browser.new_context()

    context.set_default_navigation_timeout(120000)
    context.set_default_timeout(120000)
    yielded_targets = set()
    page = None

    try:
        page = await context.new_page()
        # Attach before navigating: the chat page asks user_detail for its friends
        # as soon as the conversation list renders, and that reply is the only
        # place a stable 抖音号 for a friend appears.
        friend_identity = FriendIdentityCollector().attach(page)
        try:
            await retry_operation(
                "open creator home",
                page.goto,
                retries=3,
                delay=5,
                url=CREATOR_HOME_URL,
            )
            if not profile_config["enabled"]:
                await context.add_cookies(cookies)
            await retry_operation(
                "open chat page",
                page.goto,
                retries=3,
                delay=5,
                url=CREATOR_CHAT_URL,
            )
            await asyncio.sleep(3)
            await ensure_not_login_required(page, account_name, "open_chat_page")
            if profile_config["enabled"] and profile_config["refreshStoredCookiesAfterLogin"]:
                await refresh_stored_cookies_from_profile(context, user, account_name)
        except Exception as exc:
            attempted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            category = classify_browser_failure("open_chat_page", exc)
            reason = str(exc)
            logger.exception("Account %s failed before target delivery", account_name)
            if _is_account_level_failure_category(category):
                _persist_account_send_failure(user, category, reason, attempted_at, targets)
            else:
                for target_name in targets:
                    _persist_browser_send_failure(user, target_name, "", category, reason, attempted_at)
            return

        logger.info("Account %s started the message flow", account_name)
        try:
            index_targets = targets
            last_message = ""
            schedule_now = datetime.now(_schedule_timezone())
            if not _friend_index_complete_today(user, schedule_now):
                index_targets = list(user.get("targets") or targets)
                logger.info(
                    "Account %s will refresh today's friend index while delivering targetCount=%s",
                    account_name,
                    len(targets),
                )
            async for target_name in scroll_and_select_user(
                page,
                user,
                account_name,
                targets,
                friend_scan_config,
                index_targets=index_targets,
                collector=friend_identity,
            ):
                yielded_targets.add(target_name)
                message = ""
                chat_input = None
                last_own_message_before = None
                send_receipt = {}
                try:
                    await save_debug_artifacts(page, account_name, target_name, "selected-friend")
                    chat_input, selector_used = await locate_chat_input(page)
                    logger.info("Using chat input selector %s for %s/%s", selector_used, account_name, target_name)

                    previous_entry = dict(user.get("message_history") or {}).get(target_name) or {}
                    previous_message = str(previous_entry.get("message") or "")
                    message = build_message(previous_message=previous_message, last_message=last_message)
                    last_message = message
                    logger.info(
                        "Prepared message for %s/%s length=%s previousMatch=%s",
                        account_name,
                        target_name,
                        len(message),
                        bool(previous_message and previous_message == message),
                    )
                    last_own_message_before = await snapshot_last_own_message(
                        page,
                        chat_input=chat_input,
                    )
                    logger.info(
                        "Last own message before send for %s/%s length=%s",
                        account_name,
                        target_name,
                        len((last_own_message_before or {}).get("text", "")),
                    )

                    lines = message.split("\n")
                    for index, line in enumerate(lines):
                        await chat_input.type(line, delay=50)
                        if index < len(lines) - 1:
                            await chat_input.press("Shift+Enter")

                    await save_debug_artifacts(page, account_name, target_name, "typed-message")

                    im_observer_summary = await start_im_send_observer(page, account_name, target_name)
                    logger.info("Pressing Enter to send message for %s/%s", account_name, target_name)
                    await chat_input.press("Enter")

                    sent_ok, detail = await confirm_message_sent(
                        page,
                        chat_input,
                        message,
                        before_snapshot=last_own_message_before,
                    )
                    im_summary = await im_observer_summary()
                    logger.info(
                        "IM send observer summary for %s/%s: request=%s response=%s events=%s error=%s",
                        account_name,
                        target_name,
                        im_summary.get("send_request_seen"),
                        im_summary.get("send_response_seen"),
                        im_summary.get("events"),
                        im_summary.get("error", ""),
                    )
                    send_receipt = im_summary.get("send_receipt") or {}
                    server_ok = bool(send_receipt.get("ok"))
                    detail = (
                        f"{detail}; im_observer request={im_summary.get('send_request_seen')} "
                        f"response={im_summary.get('send_response_seen')} serverOk={server_ok} "
                        f"logid={send_receipt.get('logid', '')} reason={send_receipt.get('reason', '')}"
                    )
                    await save_debug_artifacts(page, account_name, target_name, "after-send")

                    if im_summary.get("enabled"):
                        if not im_summary.get("send_request_seen"):
                            raise RuntimeError(f"server send request was not observed; {detail}")
                        if not im_summary.get("send_response_seen"):
                            raise RuntimeError(f"server send response was not observed; {detail}")
                        if not server_ok:
                            raise RuntimeError(f"server send receipt rejected; {detail}")
                    elif not sent_ok:
                        raise RuntimeError(detail)

                    logger.info("Message send confirmed for %s/%s by server receipt: %s", account_name, target_name, detail)
                    _persist_browser_send_success(
                        user,
                        target_name,
                        message,
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        server_receipt=send_receipt,
                    )
                    interval = _random_delay_seconds(
                        send_strategy,
                        "messageIntervalSecondsMin",
                        "messageIntervalSecondsMax",
                    )
                    await _sleep_with_log(interval, "Delaying next browser message", account_name)
                except Exception as exc:
                    sent_ok = False
                    detail = ""
                    if message:
                        sent_ok, detail = await detect_message_already_sent(
                            page,
                            chat_input,
                            message,
                            before_snapshot=last_own_message_before,
                        )
                    if sent_ok and not str(exc).startswith("server send"):
                        logger.warning(
                            "Recovered send outcome for %s/%s after failure: %s",
                            account_name,
                            target_name,
                            detail,
                        )
                        _persist_browser_send_success(
                            user,
                            target_name,
                            message,
                            datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            server_receipt=send_receipt,
                        )
                        continue

                    logger.exception("Send flow failed for %s/%s", account_name, target_name)
                    await save_debug_artifacts(page, account_name, target_name, "send-error")
                    category = classify_browser_failure("send_flow", exc)
                    reason = str(exc)
                    is_login_required, login_detail = await _page_has_login_required_prompt(page)
                    if is_login_required:
                        category = "login_required"
                        reason = f"{reason}\n{login_detail}"
                        logger.warning(
                            "Account %s hit login_required during send flow: %s",
                            account_name,
                            login_detail,
                        )
                    if _is_stop_account_category(category):
                        affected_targets = [
                            target_name,
                            *[target for target in targets if target not in yielded_targets],
                        ]
                        attempted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                        _persist_account_send_failure(
                            user,
                            category,
                            reason,
                            attempted_at,
                            affected_targets,
                        )
                        return
                    _persist_browser_send_failure(
                        user,
                        target_name,
                        message,
                        category,
                        reason,
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        server_receipt=send_receipt,
                    )
                    interval = _random_delay_seconds(
                        send_strategy,
                        "messageIntervalSecondsMin",
                        "messageIntervalSecondsMax",
                    )
                    await _sleep_with_log(interval, "Delaying next browser message after failure", account_name)
        except Exception as exc:
            remaining_targets = [target for target in targets if target not in yielded_targets]
            attempted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            category = classify_browser_failure("friend_list", exc)
            reason = str(exc)
            is_login_required, login_detail = await _page_has_login_required_prompt(page)
            if is_login_required:
                category = "login_required"
                reason = f"{reason}\n{login_detail}"
            logger.exception("Target selection failed for %s", account_name)
            if _is_account_level_failure_category(category):
                _persist_account_send_failure(user, category, reason, attempted_at, remaining_targets)
            else:
                for target_name in remaining_targets:
                    _persist_browser_send_failure(user, target_name, "", category, reason, attempted_at)
            return

        missing_targets = [target for target in targets if target not in yielded_targets]
        if missing_targets:
            attempted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for target_name in missing_targets:
                _persist_browser_send_failure(
                    user,
                    target_name,
                    "",
                    "friend_not_found",
                    "target not selected from friend list",
                    attempted_at,
                )
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass
        await context.close()
        if owned_playwright is not None:
            await owned_playwright.stop()


async def runTasks():
    active_config = get_config(force_reload=True)
    all_user_data = get_userData(force_reload=True)
    requested_refs = _requested_account_refs()
    if requested_refs is not None:
        all_user_data = [user for user in all_user_data if user.get("account_ref") in requested_refs]
    active_user_data = [user for user in all_user_data if user.get("enabled", True)]
    disabled_user_data = [user for user in all_user_data if not user.get("enabled", True)]

    logger.info("Starting tasks with config")
    logger.info("multiTask=%s taskCount=%s", active_config["multiTask"], active_config["taskCount"])
    send_strategy = active_config.get("sendStrategy", {}) or {}
    logger.info(
        "messageConfig templateConfigured=%s variantCount=%s shuffleTargets=%s",
        bool(str(active_config.get("messageTemplate") or "").strip()),
        len(send_strategy.get("messageVariants") or []),
        bool(send_strategy.get("shuffleTargets", True)),
    )
    logger.info("hitokotoTypeCount=%s", len(active_config.get("hitokotoTypes") or []))
    logger.info("enabledUsers=%s disabledUsers=%s", len(active_user_data), len(disabled_user_data))
    for user in active_user_data:
        logger.info("user=%s targetCount=%s", user.get("username", "unknown"), len(user["targets"]))
    for user in disabled_user_data:
        logger.info("skipping disabled user=%s", user.get("username", "unknown"))

    if not active_user_data:
        logger.warning("No enabled accounts are available for the task run")
        return

    runnable_user_data = _prepare_active_users_for_run(active_config, active_user_data)
    if not runnable_user_data:
        return

    try:
        with task_run_lock():
            protocol_user_data, browser_user_data = _split_sender_modes(active_config, runnable_user_data)
            if protocol_user_data:
                await run_protocol_tasks(active_config, protocol_user_data, build_message)
            await run_browser_tasks(active_config, browser_user_data)
    except TaskRunAlreadyInProgress:
        logger.warning("Skipping task run because another task run is already in progress")


class TaskRunAlreadyInProgress(RuntimeError):
    """Raised when a live task process already owns the global run lock."""


@contextmanager
def task_run_lock():
    lock_path = Path("logs/task.run.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    def _lock_owner_is_alive(pid):
        return _pid_is_alive(pid)

    while True:
        try:
            handle = lock_path.open("x", encoding="utf-8")
            break
        except FileExistsError as exc:
            raw_pid = lock_path.read_text(encoding="utf-8", errors="ignore").strip()
            stale_pid = None
            try:
                stale_pid = int(raw_pid)
            except (TypeError, ValueError):
                stale_pid = None

            if stale_pid is not None and not _lock_owner_is_alive(stale_pid):
                logger.warning("Removing stale task lock owned by missing pid=%s", stale_pid)
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                continue

            if stale_pid is None:
                logger.warning("Removing unreadable stale task lock with contents=%r", raw_pid)
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                continue

            raise TaskRunAlreadyInProgress("another task run is already in progress") from exc

    try:
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        yield
    finally:
        handle.close()
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
