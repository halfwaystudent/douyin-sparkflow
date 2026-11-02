"""Lease-bound interaction with the login page, without desktop or shell access."""

import asyncio
import hmac
import math
from urllib.parse import urlsplit


LOGIN_DOMAINS = (
    "douyin.com", "douyinstatic.com", "douyinpic.com", "pstatp.com",
    "bytegoofy.com", "bytetos.com", "byted-static.com", "ibytedapm.com",
    "bytednsdoc.com", "bytedance.com", "snssdk.com", "zijieapi.com",
    "yhgfb-cn-static.com", "bytetcc.com", "byteimg.com",
)
SAFE_KEYS = {"Enter", "Backspace", "Delete", "Escape", "ArrowLeft", "ArrowRight",
             "ArrowUp", "ArrowDown", "Home", "End", "Control+A"}


class InteractionError(RuntimeError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def allowed_login_url(url, *, resource=False):
    try:
        parsed = urlsplit(str(url))
        if resource and parsed.scheme in {"data", "blob"}:
            return True
        host = parsed.hostname or ""
        return (
            parsed.scheme == "https" and parsed.port in {None, 443}
            and not parsed.username and not parsed.password
            and any(host == suffix or host.endswith("." + suffix) for suffix in LOGIN_DOMAINS)
        )
    except ValueError:
        return False


def _number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise InteractionError("无效的操作坐标")
    if not math.isfinite(value) or not low <= value <= high:
        raise InteractionError("操作坐标超出登录窗口")
    return value


class LoginInteraction:
    def __init__(self):
        self.token = ""
        self.context = None

    def clear(self):
        self.token = ""
        self.context = None

    def owns(self, token):
        return bool(isinstance(token, str) and token.isascii() and 16 <= len(token) <= 128
                    and self.token and hmac.compare_digest(token, self.token))

    async def bind(self, context, token):
        if not isinstance(token, str) or not token.isascii() or not 16 <= len(token) <= 128:
            self.clear()
            raise InteractionError("登录工作区凭据无效", 423)
        if self.context is not context:
            async def guard(route):
                request = route.request
                try:
                    main_navigation = request.is_navigation_request() and request.frame.parent_frame is None
                except Exception:
                    main_navigation = True
                if allowed_login_url(request.url, resource=not main_navigation):
                    await route.fallback()
                else:
                    await route.abort()

            await context.route("**/*", guard)
            async def websocket_guard(route):
                url = route.url
                if url.startswith("wss://") and allowed_login_url("https://" + url[6:]):
                    route.connect_to_server()
                else:
                    await route.close(code=1008, reason="Login network policy")

            await context.route_web_socket("**/*", websocket_guard)
            # Intercepting file choosers prevents native file dialogs. There is
            # deliberately no file-selection or download API in this interface.
            def page_created(page):
                page.on("filechooser", lambda chooser: None)
                page.on("dialog", lambda dialog: asyncio.create_task(dialog.dismiss()))

            for page in context.pages:
                page_created(page)
            context.on("page", page_created)
        self.context = context
        self.token = token

    def check(self, token, page):
        if not self.owns(token) or not page or page.is_closed() or page.context is not self.context:
            raise InteractionError("登录工作区已释放，请重新申请", 423)
        if not allowed_login_url(page.url):
            raise InteractionError("当前页面不在允许的登录范围内，请重新打开登录工作区", 409)

    async def screenshot(self, token, page):
        self.check(token, page)
        data = await asyncio.wait_for(page.screenshot(type="jpeg", quality=70, timeout=5000), 6)
        self.check(token, page)
        if len(data) > 3 * 1024 * 1024:
            raise InteractionError("登录窗口画面过大，请稍后重试", 503)
        return data

    async def act(self, token, page, action):
        self.check(token, page)
        if not isinstance(action, dict):
            raise InteractionError("无效的登录操作")
        kind = action.get("type")
        if kind == "pointer":
            if set(action) - {"type", "phase", "x", "y"} or action.get("phase") not in {"down", "move", "up"}:
                raise InteractionError("无效的鼠标操作")
            viewport = page.viewport_size or {"width": 1600, "height": 1000}
            x = _number(action.get("x"), 0, viewport["width"] - 1)
            y = _number(action.get("y"), 0, viewport["height"] - 1)
            await page.mouse.move(x, y)
            if action["phase"] == "down":
                await page.mouse.down(button="left")
            elif action["phase"] == "up":
                await page.mouse.up(button="left")
        elif kind in {"text", "key"}:
            if not await page.evaluate("document.hasFocus()"):
                raise InteractionError("请先点击登录窗口中的输入框")
            if kind == "text":
                text = action.get("text")
                if set(action) - {"type", "text"} or not isinstance(text, str) or not 1 <= len(text) <= 1024 or "\x00" in text:
                    raise InteractionError("输入内容无效或过长")
                await page.keyboard.insert_text(text)
            else:
                if set(action) - {"type", "key"} or action.get("key") not in SAFE_KEYS:
                    raise InteractionError("不支持这个按键")
                await page.keyboard.press(action["key"])
        elif kind == "wheel":
            if set(action) - {"type", "dy"}:
                raise InteractionError("无效的滚动操作")
            await page.mouse.wheel(0, _number(action.get("dy"), -800, 800))
        else:
            raise InteractionError("不支持这个登录操作")
        self.check(token, page)
