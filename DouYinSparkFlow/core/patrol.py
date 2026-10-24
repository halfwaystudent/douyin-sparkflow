"""Daily account patrol: login state, identity and target presence, read-only.

Before the send window opens, each enabled account's persistent browser session
is opened on the creator chat page (the same profile/cookie policy the friend
refresh uses), the login state is checked, the friend list is read in full and
the targets are reconciled by friend uid (found / renamed / missing / unbound).

The patrol never opens a chat, never types or sends anything, never calls a
protocol endpoint and never builds a request of its own: the friend uids and
the logged-in uid are read passively from responses the page requests by
itself. It does not touch a target's send state or failure counters.

Account fields written here:

``patrol``
    Latest result: ``{checkedAt, trigger, completed, conclusion, login, targets,
    reason, uidDataTrusted, friendCount}``.
``patrol_login_failure``
    Present while the last patrol proved the login unusable. The send preflight
    skips the account while it exists; a passing patrol or a new login (see
    ``core.identity.LOGIN_HEALTH_FLAG_KEYS``) removes it.
``account_health``
    Written with ``source="patrol"`` on a login verdict.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from urllib.parse import urlparse

from core import friend_identity, notifications, streak_state
from core import identity as account_identity
from core.browser import (
    account_profile_name,
    acquire_browser_account_lock,
    douyin_network_modes,
    get_browser,
    get_persistent_browser_context,
    normalize_persistent_profile_config,
    release_browser_account_lock,
)
from core.friends import (
    CATEGORY_EMPTY_RESULT,
    CATEGORY_INCOMPLETE_SCAN,
    CATEGORY_LOGIN_REQUIRED,
    CATEGORY_NETWORK_UNAVAILABLE,
    CATEGORY_STRUCTURE_CHANGED,
    CHAT_PAGE_URL,
    FRIEND_LIST_READY_TIMEOUT_SECONDS,
    UID_DRAIN_TIMEOUT_SECONDS,
    FriendRefreshError,
    FriendScanResult,
    _left_creator_host,
    classify_refresh_error,
    collect_friend_names,
)
from core.streak_state import AUTH_COOKIE_NAMES
from utils.config import get_config, get_userData, update_user_data
from utils.logger import setup_logger


logger = setup_logger(level=logging.DEBUG)

PATROL_KEY = "patrol"
PATROL_LOGIN_FAILURE_KEY = "patrol_login_failure"
HEALTH_SOURCE = "patrol"

CATEGORY_IDENTITY_MISMATCH = account_identity.CATEGORY_IDENTITY_CONFLICT
CATEGORY_MISSING_COOKIES = "missing_cookies"
CATEGORY_ACCOUNT_BUSY = "account_busy"
CATEGORY_TIMEOUT = "timeout"

# Login verdicts that pause the account for the send preflight.
LOGIN_FAILURE_CATEGORIES = frozenset(
    {CATEGORY_LOGIN_REQUIRED, CATEGORY_IDENTITY_MISMATCH, CATEGORY_MISSING_COOKIES}
)

CONCLUSION_OK = "ok"
CONCLUSION_PROBLEMS = "problems"
CONCLUSION_LOGIN_FAILED = "login_failed"
CONCLUSION_INCOMPLETE = "incomplete"
CONCLUSION_SKIPPED = "skipped"

CONCLUSION_LABELS = {
    CONCLUSION_OK: "正常",
    CONCLUSION_PROBLEMS: "发现问题",
    CONCLUSION_LOGIN_FAILED: "登录态失效",
    CONCLUSION_INCOMPLETE: "巡查未完成",
    CONCLUSION_SKIPPED: "已跳过",
}

LOGIN_CATEGORY_LABELS = {
    CATEGORY_LOGIN_REQUIRED: "登录已失效，需要重新扫码登录",
    CATEGORY_IDENTITY_MISMATCH: "登录账号与这条账号记录不一致",
    CATEGORY_MISSING_COOKIES: "没有保存登录信息",
    CATEGORY_NETWORK_UNAVAILABLE: "网络不可用",
    CATEGORY_STRUCTURE_CHANGED: "页面结构变化",
    CATEGORY_ACCOUNT_BUSY: "账号正被其他任务占用",
    CATEGORY_TIMEOUT: "巡查超时",
}

TARGET_STATUS_LABELS = {
    friend_identity.STATUS_FOUND: "找到",
    friend_identity.STATUS_RENAMED: "已改名",
    friend_identity.STATUS_MISSING: "缺失",
    friend_identity.STATUS_UNBOUND: "未绑定 uid",
    friend_identity.STATUS_UNCHECKED: "未核对",
}

USER_TOKEN_PATH = urlparse(account_identity.USER_TOKEN_URL).path
PAGE_SETTLE_SECONDS = 2
REASON_LIMIT = 300


def _env_int(name, default, minimum):
    raw = str(os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        logger.warning("Invalid %s=%r, using %s", name, raw, default)
        return default


def lock_wait_seconds(trigger="scheduled"):
    """How long the patrol waits for an account a send run is using."""
    default = 120 if trigger == "manual" else 600
    return _env_int("SPARKFLOW_PATROL_LOCK_WAIT_SECONDS", default, 0)


def account_timeout_seconds():
    return _env_int("SPARKFLOW_PATROL_ACCOUNT_TIMEOUT_SECONDS", 420, 60)


def _short(text):
    value = " ".join(str(text or "").split())
    return value[:REASON_LIMIT]


def _account_name(user):
    return str((user or {}).get("username") or (user or {}).get("unique_id") or "unknown")


class PatrolLoginFailure(Exception):
    """The page proved the login unusable (prompt, redirect or wrong account)."""

    def __init__(self, category, reason):
        super().__init__(reason)
        self.category = category
        self.reason = reason


class LoginUidWatcher:
    """Passively read the logged-in uid from the page's own user_token response.

    Nothing is requested here: when the page does not ask for the token by
    itself the uid simply stays unknown and the identity is not judged.
    """

    def __init__(self):
        self.uid = ""
        self.responses = 0
        self._tasks = set()
        self._page = None

    @staticmethod
    def matches(url):
        try:
            path = urlparse(str(url or "")).path
        except ValueError:
            return False
        return path.rstrip("/") == USER_TOKEN_PATH.rstrip("/")

    def attach(self, page):
        if page is None or self._page is not None:
            return self
        try:
            page.on("response", self._on_response)
        except Exception:  # noqa: BLE001 - never break the patrol over this
            return self
        self._page = page
        return self

    def detach(self):
        page, self._page = self._page, None
        if page is None:
            return
        for method_name in ("remove_listener", "off"):
            method = getattr(page, method_name, None)
            if callable(method):
                try:
                    method("response", self._on_response)
                    return
                except Exception:  # noqa: BLE001
                    continue

    def _on_response(self, response):
        try:
            url = response.url
        except Exception:  # noqa: BLE001
            return
        if not self.matches(url):
            return
        self.responses += 1
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._read(response))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _read(self, response):
        try:
            text = await response.text()
        except Exception:  # noqa: BLE001
            return
        self.ingest(text)

    def ingest(self, text):
        if not isinstance(text, str):
            try:
                text = json.dumps(text)
            except (TypeError, ValueError):
                return ""
        try:
            uid = account_identity.parse_user_token_response(text)
        except Exception:  # noqa: BLE001 - an unreadable token is "unknown"
            return ""
        self.uid = uid
        return uid

    async def drain(self, timeout=5.0):
        pending = [task for task in self._tasks if not task.done()]
        if pending:
            try:
                await asyncio.wait(pending, timeout=timeout)
            except Exception:  # noqa: BLE001
                logger.debug("Draining user_token readers failed", exc_info=True)


async def _detect_login_prompt(page):
    """(is_required, detail) using the sender's own login-prompt detector."""
    from core.tasks import _page_has_login_required_prompt

    return await _page_has_login_required_prompt(page)


async def _open_session(user, network_mode, profile_config):
    """Own browser startup until handles can be handed to the inspection.

    Playwright startup must not be cancelled before its handles are returned:
    on cancellation, finish the shielded startup, close everything it owns,
    then propagate cancellation (the caller still holds the account lock).
    """
    async def start():
        playwright = browser = context = None
        cookies = list(user.get("cookies") or [])
        try:
            if profile_config["enabled"]:
                playwright, context, _profile_dir = await get_persistent_browser_context(
                    account_profile_name(user),
                    root=profile_config["root"],
                    network_mode=network_mode,
                )
                if profile_config["syncStoredCookiesBeforeRun"]:
                    await context.add_cookies(cookies)
                elif profile_config["seedCookiesWhenEmpty"]:
                    current = await context.cookies(
                        ["https://creator.douyin.com/", "https://www.douyin.com/"]
                    )
                    if not any(cookie.get("name") in AUTH_COOKIE_NAMES for cookie in current):
                        await context.add_cookies(cookies)
            else:
                playwright, browser = await get_browser(GUI=False, network_mode=network_mode)
                context = await browser.new_context()
                await context.add_cookies(cookies)
            return playwright, browser, context
        except BaseException:
            await _close_session(playwright, browser, context)
            raise

    startup = asyncio.create_task(start())
    try:
        return await asyncio.shield(startup)
    except asyncio.CancelledError:
        # Do not release the profile lock until startup has returned its handles
        # and cleanup has finished. Startup errors already clean their handles.
        try:
            handles = await startup
        except BaseException:
            pass
        else:
            await _close_session(*handles)
        raise


async def _close_session(playwright, browser, context, page=None):
    """Best-effort close every owned handle before propagating cancellation."""
    cancelled = False
    for closer, label in (
        (getattr(page, "close", None), "page"),
        (getattr(context, "close", None), "context"),
        (getattr(browser, "close", None), "browser"),
        (getattr(playwright, "stop", None), "playwright"),
    ):
        if closer is None:
            continue
        close_task = asyncio.create_task(closer())
        while not close_task.done():
            try:
                await asyncio.shield(close_task)
            except asyncio.CancelledError:
                if close_task.cancelled():
                    logger.debug("Patrol %s closer was cancelled", label)
                    break
                cancelled = True
                logger.debug("Patrol cancellation deferred while closing %s", label)
            except Exception:  # noqa: BLE001
                logger.debug("Failed to close patrol %s", label, exc_info=True)
                break
        if close_task.done() and not close_task.cancelled():
            try:
                close_task.result()
            except Exception:  # noqa: BLE001
                logger.debug("Failed to close patrol %s", label, exc_info=True)
    if cancelled:
        raise asyncio.CancelledError


async def _inspect_once(user, network_mode, profile_config):
    """Read the login state and the full friend list on one network route."""
    account_name = _account_name(user)
    playwright, browser, context = await _open_session(user, network_mode, profile_config)
    page = None
    uid_collector = None
    login_watch = None
    try:
        context.set_default_navigation_timeout(120000)
        context.set_default_timeout(120000)
        page = await context.new_page()
        uid_collector = friend_identity.UserDetailCollector(account_name).attach(page)
        login_watch = LoginUidWatcher().attach(page)
        await page.goto(
            CHAT_PAGE_URL,
            wait_until="commit",
            timeout=FRIEND_LIST_READY_TIMEOUT_SECONDS * 1000,
        )
        await asyncio.sleep(PAGE_SETTLE_SECONDS)
        if _left_creator_host(page):
            raise PatrolLoginFailure(
                CATEGORY_LOGIN_REQUIRED, "会话已失效：页面离开了抖音创作者中心"
            )
        required, detail = await _detect_login_prompt(page)
        if required:
            raise PatrolLoginFailure(
                CATEGORY_LOGIN_REQUIRED, _short(f"页面出现登录提示：{detail}")
            )
        try:
            friends = await collect_friend_names(page)
        except FriendRefreshError as exc:
            if exc.category == CATEGORY_LOGIN_REQUIRED:
                raise PatrolLoginFailure(CATEGORY_LOGIN_REQUIRED, _short(str(exc))) from exc
            raise
        if not isinstance(friends, FriendScanResult):
            friends = FriendScanResult(friends)
        await uid_collector.drain(timeout=UID_DRAIN_TIMEOUT_SECONDS)
        await login_watch.drain(timeout=UID_DRAIN_TIMEOUT_SECONDS)
        friends.uid_entries = uid_collector.entries()
        friends.uid_summary = uid_collector.summary()
        groups, group_error = await _collect_patrol_groups(page, user)
        return {
            "friends": friends,
            "loginUid": login_watch.uid,
            "groups": groups,
            "groupError": group_error,
        }
    finally:
        if uid_collector is not None:
            uid_collector.detach()
        if login_watch is not None:
            login_watch.detach()
        await _close_session(playwright, browser, context, page)


def _group_targets(user):
    from core.tasks import parse_chat_target

    return [
        str(target) for target in user.get("targets") or []
        if parse_chat_target(target)[0] == "group"
    ]


async def _collect_patrol_groups(page, user):
    """Read the group list on the already-open chat page: (rows, error).

    Only switches the list tab and scrolls it, never opens a conversation, so
    nothing is marked read or sent. A failure only affects the group rows.
    """
    if not _group_targets(user):
        return None, ""
    from core.groups import _activate_group_tab, collect_group_rows

    previous = ""
    try:
        previous = await _activate_group_tab(page)
        return await collect_group_rows(page), ""
    except Exception as exc:  # noqa: BLE001 - group check must not fail the patrol
        logger.warning("Patrol group list read failed for %s: %s", _account_name(user), _short(str(exc)))
        return None, _short(str(exc)) or type(exc).__name__
    finally:
        if previous:
            try:
                await page.get_by_role("tab", name=previous, exact=True).click()
            except Exception:  # noqa: BLE001
                pass


def _group_target_rows(user, groups, group_error=""):
    """Judge group targets against the group list, the same way the sender finds them."""
    from core.tasks import _normalize_target_name, get_bound_group_conversation_id, parse_chat_target

    rows = []
    targets = _group_targets(user)
    if not targets:
        return rows
    by_id = {str(item.get("conversationId")): str(item.get("name") or "") for item in groups or []}
    for target in targets:
        _kind, visible = parse_chat_target(target)
        row = {"target": target, "status": friend_identity.STATUS_UNCHECKED, "reason": "", "kind": "group"}
        if groups is None:
            row["reason"] = f"群列表读取失败：{group_error}" if group_error else "群列表读取失败"
            rows.append(row)
            continue
        bound_id = get_bound_group_conversation_id(user, target)
        if bound_id:
            current = by_id.get(str(bound_id))
            if current is None:
                row.update(status=friend_identity.STATUS_MISSING, reason="群列表里没有这个群（可能已退群或群被解散）")
            else:
                row["status"] = friend_identity.STATUS_FOUND
                if _normalize_target_name(current) != _normalize_target_name(visible):
                    row["currentName"] = current
            rows.append(row)
            continue
        wanted = _normalize_target_name(visible)
        matches = [name for name in by_id.values() if _normalize_target_name(name) == wanted]
        if len(matches) == 1:
            row["status"] = friend_identity.STATUS_FOUND
        elif matches:
            row.update(status=friend_identity.STATUS_UNBOUND, reason="有多个同名群，发送会跳过；请在群列表里勾选绑定")
        else:
            row.update(status=friend_identity.STATUS_MISSING, reason="群列表里没有这个群名")
        rows.append(row)
    return rows


async def _inspect_account(user, profile_config):
    """Try each egress route; a proven login failure stops immediately."""
    last_error = None
    for network_mode in douyin_network_modes():
        try:
            return await _inspect_once(user, network_mode, profile_config)
        except PatrolLoginFailure:
            raise
        except Exception as exc:  # noqa: BLE001 - try the next route
            category = getattr(exc, "category", None) or classify_refresh_error(exc)
            if category == CATEGORY_LOGIN_REQUIRED and isinstance(exc, FriendRefreshError):
                raise PatrolLoginFailure(CATEGORY_LOGIN_REQUIRED, _short(str(exc))) from exc
            last_error = exc
            logger.warning(
                "Patrol route=%s failed for %s: %s",
                network_mode,
                _account_name(user),
                _short(str(exc)),
            )
            if category == CATEGORY_STRUCTURE_CHANGED:
                # Another route cannot fix a page that loaded but did not render.
                break
    raise last_error or RuntimeError("no network route available")


def _identity_verdict(user, login_uid):
    """(state, expected, actual) — never a failure when the uid is unknown."""
    expected = account_identity.account_uid(user)
    if not expected:
        return "legacy", "", login_uid
    if not login_uid:
        return "unverified", expected, ""
    if login_uid != expected:
        return "mismatch", expected, login_uid
    return "verified", expected, login_uid


def _target_rows(identity_result):
    rows = []
    for item in (identity_result or {}).get("targets") or []:
        row = {
            "target": str(item.get("target") or ""),
            "status": str(item.get("status") or ""),
            "reason": str(item.get("reason") or ""),
        }
        if item.get("uidSuffix"):
            row["uidSuffix"] = str(item["uidSuffix"])
        if item.get("status") == friend_identity.STATUS_RENAMED:
            row["oldName"] = str(item.get("oldName") or "")
            row["newName"] = str(item.get("newName") or row["target"])
        if item.get("degraded"):
            row["degraded"] = True
        rows.append(row)
    return rows


def _conclusion(record):
    login = record.get("login") or {}
    if record.get("skipped"):
        return CONCLUSION_SKIPPED
    if login.get("ok") is False:
        return CONCLUSION_LOGIN_FAILED
    if not record.get("completed"):
        return CONCLUSION_INCOMPLETE
    if any(
        row.get("status") != friend_identity.STATUS_FOUND
        for row in record.get("targets") or []
    ):
        return CONCLUSION_PROBLEMS
    return CONCLUSION_OK


def _write_friend_cache(user, friends, updated_at):
    def mutate(accounts):
        matched = streak_state.find_matching_account(accounts, user)
        if matched is None:
            return None, False
        matched["friends_cache"] = list(friends)
        matched["friends_cache_updated_at"] = updated_at
        return None, True

    update_user_data(mutate, force_reload=True)


def _record_friends(user, friends, now):
    """Save a read the same way the Web refresh does; returns the reconcile result.

    Only a complete, non-empty read may replace the cache and rebuild the send
    index; any read still contributes the uids it learned (no rename or missing
    verdict is drawn from a partial read).
    """
    names = list(friends)
    complete = bool(getattr(friends, "complete", False)) and bool(names)
    if complete:
        from core.tasks import record_friend_scan

        _write_friend_cache(user, names, datetime.now().isoformat(timespec="seconds"))
        try:
            record_friend_scan(
                user,
                names,
                scan_complete=True,
                targets=user.get("targets") or [],
            )
        except Exception:  # noqa: BLE001 - the identity check still runs
            logger.warning(
                "Patrol friend index update failed for %s", _account_name(user), exc_info=True
            )
    result = friend_identity.persist_identity_scan(
        user,
        list(getattr(friends, "uid_entries", None) or []),
        scan_complete=complete,
        scanned_names=names,
        now=now,
        sync_user=False,
    )
    return complete, result


def _persist_patrol(user, record, login_failure=None):
    """Store the patrol result and its login verdict in one locked write."""
    checked_at = record["checkedAt"]
    login = record.get("login") or {}

    def mutate(accounts):
        matched = streak_state.find_matching_account(accounts, user)
        if matched is None:
            return None, False
        matched[PATROL_KEY] = dict(record)
        if login_failure is not None:
            matched[PATROL_LOGIN_FAILURE_KEY] = {
                "category": login_failure["category"],
                "reason": login_failure["reason"],
                "checkedAt": checked_at,
            }
            matched["account_health"] = {
                "healthy": False,
                "category": login_failure["category"],
                "reason": login_failure["reason"],
                "checkedAt": checked_at,
                "source": HEALTH_SOURCE,
            }
        elif login.get("ok") is True:
            previous = dict(matched.pop(PATROL_LOGIN_FAILURE_KEY, None) or {})
            health = dict(matched.get("account_health") or {})
            # Only replace a verdict the patrol owns (or none at all): a send
            # failure or a login check recorded elsewhere is not the patrol's to
            # lift; the preflight keeps deciding on those. A preflight record
            # that only repeated the patrol's own failure is the patrol's too.
            if (
                not health
                or health.get("healthy") is not False
                or health.get("source") == HEALTH_SOURCE
                or (
                    previous
                    and str(health.get("category") or "") == str(previous.get("category") or "")
                )
            ):
                matched["account_health"] = {
                    "healthy": True,
                    "category": "",
                    "reason": "",
                    "checkedAt": checked_at,
                    "source": HEALTH_SOURCE,
                }
        return dict(matched), True

    return update_user_data(mutate, force_reload=True)


async def patrol_account(user, *, now=None, trigger="scheduled", lock_wait=None, profile_config=None):
    """Patrol one account and persist the result. Never raises for page problems."""
    account_name = _account_name(user)
    started = streak_state._now(now)
    checked_at = started.astimezone(timezone.utc).isoformat(timespec="seconds")
    record = {
        "checkedAt": checked_at,
        "trigger": str(trigger or "scheduled"),
        "completed": False,
        "login": {"ok": None, "category": "", "reason": "", "identity": ""},
        "targets": [],
        "reason": "",
        "uidDataTrusted": False,
        "friendCount": 0,
    }
    login_failure = None
    inspection = None

    if not list(user.get("cookies") or []):
        login_failure = {
            "category": CATEGORY_MISSING_COOKIES,
            "reason": "账号没有保存登录信息，需要重新扫码登录",
        }
    else:
        if profile_config is None:
            profile_config = normalize_persistent_profile_config(get_config(force_reload=True) or {})
        wait_seconds = lock_wait_seconds(trigger) if lock_wait is None else max(0, lock_wait)
        lock = None
        try:
            lock = await acquire_browser_account_lock(
                user,
                account_name,
                wait_seconds=wait_seconds,
                poll_seconds=min(5, max(1, wait_seconds)) if wait_seconds else 1,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Patrol skipped %s: account lock unavailable (%s)", account_name, exc)
            record["skipped"] = True
            record["login"]["category"] = CATEGORY_ACCOUNT_BUSY
            record["reason"] = (
                f"账号正被发送或刷新任务占用，等待 {wait_seconds} 秒后跳过本次巡查"
            )
        if lock is not None:
            try:
                inspection = await asyncio.wait_for(
                    _inspect_account(user, profile_config),
                    timeout=account_timeout_seconds(),
                )
            except PatrolLoginFailure as exc:
                login_failure = {"category": exc.category, "reason": _short(exc.reason)}
            except asyncio.TimeoutError:
                record["login"]["category"] = CATEGORY_TIMEOUT
                record["reason"] = f"巡查超过 {account_timeout_seconds()} 秒未完成"
            except Exception as exc:  # noqa: BLE001
                category = getattr(exc, "category", None) or classify_refresh_error(exc)
                record["login"]["category"] = category
                record["reason"] = _short(
                    f"{LOGIN_CATEGORY_LABELS.get(category, '页面读取失败')}：{exc}"
                )
            finally:
                try:
                    release_browser_account_lock(*lock, account_name)
                except Exception:  # noqa: BLE001
                    logger.debug("Failed to release patrol account lock", exc_info=True)

    if inspection is not None:
        state, expected, actual = _identity_verdict(user, inspection.get("loginUid") or "")
        record["login"]["identity"] = state
        if state == "mismatch":
            login_failure = {
                "category": CATEGORY_IDENTITY_MISMATCH,
                "reason": (
                    "页面登录的平台 UID 与账号记录不一致"
                    f"（记录尾号 {friend_identity.uid_suffix(expected)}，"
                    f"页面尾号 {friend_identity.uid_suffix(actual)}）"
                ),
            }
        else:
            record["login"]["ok"] = True
            friends = inspection["friends"]
            record["friendCount"] = len(friends)
            try:
                complete, identity_result = _record_friends(user, friends, started)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Patrol could not save friends for %s", account_name, exc_info=True)
                complete, identity_result = False, None
                record["reason"] = _short(f"保存好友数据失败：{type(exc).__name__}")
            record["completed"] = bool(complete and identity_result is not None)
            if identity_result:
                record["targets"] = _target_rows(identity_result)
            record["targets"] = list(record.get("targets") or []) + _group_target_rows(
                user, inspection.get("groups"), inspection.get("groupError")
            )
            if identity_result:
                record["uidDataTrusted"] = bool(identity_result.get("uidDataTrusted"))
                if identity_result.get("trustReason") and not record["uidDataTrusted"]:
                    record["uidTrustReason"] = str(identity_result["trustReason"])
            if not complete and not record["reason"]:
                if not list(friends) and getattr(friends, "complete", False):
                    record["reason"] = "未读到任何好友，已保留上一次的好友数据"
                    record["login"]["category"] = ""
                    record["incompleteCategory"] = CATEGORY_EMPTY_RESULT
                else:
                    record["reason"] = _short(
                        "好友列表读取不完整"
                        + (f"（{friends.reason}）" if getattr(friends, "reason", "") else "")
                        + "，未判定缺失，已保留上一次的好友数据"
                    )
                    record["incompleteCategory"] = CATEGORY_INCOMPLETE_SCAN

    if login_failure is not None:
        record["login"].update(
            {"ok": False, "category": login_failure["category"], "reason": login_failure["reason"]}
        )
        record["completed"] = False
        if not record["reason"]:
            record["reason"] = login_failure["reason"]

    record["conclusion"] = _conclusion(record)
    try:
        _persist_patrol(user, record, login_failure=login_failure)
    except Exception:  # noqa: BLE001
        logger.error("Could not persist patrol result for %s", account_name, exc_info=True)
    logger.info(
        "Patrol account=%s conclusion=%s login=%s identity=%s completed=%s targets=%s reason=%s",
        account_name,
        record["conclusion"],
        record["login"].get("ok"),
        record["login"].get("identity") or "-",
        record["completed"],
        {row["target"]: row["status"] for row in record["targets"]},
        record["reason"] or "-",
    )
    return dict(record, accountRef=str(user.get("account_ref") or ""), account=account_name)


def select_accounts(accounts, account_refs=None):
    """Enabled accounts, optionally narrowed to account refs (or unique ids)."""
    wanted = None
    if account_refs is not None:
        wanted = {str(ref).strip() for ref in account_refs if str(ref or "").strip()}
    selected = []
    for account in accounts or []:
        if not account.get("enabled", True):
            continue
        if wanted is not None and not (
            str(account.get("account_ref") or "") in wanted
            or str(account.get("unique_id") or "") in wanted
        ):
            continue
        selected.append(account)
    return selected


async def run_patrol(account_refs=None, *, now=None, trigger="scheduled", lock_wait=None, notify=True):
    """Patrol enabled accounts one after another, then push what was found."""
    from core.tasks import _schedule_timezone

    config = get_config(force_reload=True) or {}
    accounts = select_accounts(get_userData(force_reload=True), account_refs)
    logger.info("Account patrol started trigger=%s accounts=%s", trigger, len(accounts))
    profile_config = normalize_persistent_profile_config(config)
    results = []
    for user in accounts:
        try:
            results.append(
                await patrol_account(
                    user,
                    now=now,
                    trigger=trigger,
                    lock_wait=lock_wait,
                    profile_config=profile_config,
                )
            )
        except Exception:  # noqa: BLE001 - one account never stops the others
            logger.error("Patrol crashed for %s", _account_name(user), exc_info=True)
    notified = None
    if notify and results:
        args = (results, config, get_userData(force_reload=True))
        kwargs = {"schedule_tz": _schedule_timezone(), "now": now}
        try:
            if trigger == "manual":
                # Web patrols share uvicorn's event loop: synchronous channel
                # HTTP must not block unrelated requests while pushing.
                notified = await asyncio.to_thread(
                    notifications.notify_patrol_finished, *args, **kwargs
                )
            else:
                notified = notifications.notify_patrol_finished(*args, **kwargs)
        except Exception:
            logger.warning("Patrol notification failed", exc_info=True)
    logger.info(
        "Account patrol finished accounts=%s conclusions=%s",
        len(results),
        {item["account"]: item["conclusion"] for item in results},
    )
    return {"accounts": results, "notified": notified}


def patrol_view(account):
    """Template-ready summary of the latest patrol for the account page."""
    record = dict((account or {}).get(PATROL_KEY) or {})
    if not record:
        return {"has": False}
    conclusion = str(record.get("conclusion") or "")
    login = dict(record.get("login") or {})
    issues = []
    if login.get("ok") is False:
        label = LOGIN_CATEGORY_LABELS.get(str(login.get("category") or ""), "登录态异常")
        issues.append(f"登录态：{label}")
    elif login.get("ok") is True:
        identity = str(login.get("identity") or "")
        if identity == "unverified":
            issues.append("登录正常，但本次未能从页面读到平台 UID，身份未核对")
    targets = []
    for row in record.get("targets") or []:
        status = str(row.get("status") or "")
        label = TARGET_STATUS_LABELS.get(status, status or "未知")
        if status == friend_identity.STATUS_UNBOUND and row.get("kind") == "group":
            label = "群名重复"
        if status == friend_identity.STATUS_RENAMED:
            text = f"{row.get('oldName') or ''} → {row.get('newName') or row.get('target') or ''}（已自动跟随）"
        elif row.get("currentName"):
            text = f"{row.get('target') or ''}（群现名：{row['currentName']}，按编号照常发送）"
        else:
            text = str(row.get("target") or "")
        targets.append(
            {
                "status": status,
                "label": label,
                "text": text,
                "reason": str(row.get("reason") or ""),
                "problem": status != friend_identity.STATUS_FOUND,
            }
        )
    checked = streak_state._parse_time(record.get("checkedAt"), timezone.utc)
    checked_display = ""
    if checked is not None:
        try:
            from core.tasks import _schedule_timezone

            checked_display = checked.astimezone(_schedule_timezone()).strftime("%m-%d %H:%M")
        except Exception:  # noqa: BLE001
            checked_display = str(record.get("checkedAt") or "")
    pill = {
        CONCLUSION_OK: "success",
        CONCLUSION_PROBLEMS: "warning",
        CONCLUSION_LOGIN_FAILED: "danger",
        CONCLUSION_INCOMPLETE: "warning",
        CONCLUSION_SKIPPED: "",
    }.get(conclusion, "")
    return {
        "has": True,
        "checkedAt": str(record.get("checkedAt") or ""),
        "checkedDisplay": checked_display,
        "trigger": "手动" if record.get("trigger") == "manual" else "定时",
        "conclusion": conclusion,
        "conclusionLabel": CONCLUSION_LABELS.get(conclusion, conclusion or "未知"),
        "pill": pill,
        "issues": issues,
        "targets": targets,
        "problemCount": sum(1 for item in targets if item["problem"]) + len(issues),
        "reason": str(record.get("reason") or ""),
        "completed": bool(record.get("completed")),
        "friendCount": int(record.get("friendCount") or 0),
    }
