"""Push channels for run notifications: Telegram, DingTalk, WeCom and a generic webhook.

Each channel gets one attempt with a short timeout. Failures come back as
(False, Chinese detail) and are logged with secrets redacted; nothing here
raises into the task runner.
"""
import base64
import hashlib
import hmac
import logging
import re
import time
from copy import deepcopy
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from utils.config import DEFAULT_CONFIG, douyin_proxy_url, get_app_settings
from utils.logger import setup_logger


logger = setup_logger(level=logging.DEBUG)

REQUEST_TIMEOUT_SECONDS = 10.0
DEFAULT_PROXY_URL = "http://proxy:7890"
MESSAGE_PREFIX = "【抖音火花】"

CHANNEL_NAMES = ("telegram", "dingtalk", "wecom", "webhook")
CHANNEL_LABELS = {
    "telegram": "Telegram Bot",
    "dingtalk": "钉钉机器人",
    "wecom": "企业微信机器人",
    "webhook": "通用 Webhook",
}
# Text fields the settings form may write, and which of them are secrets.
# Secrets are only ever shown masked; an empty submit keeps the stored value.
CHANNEL_FIELDS = {
    "telegram": {"botToken": "secret", "chatId": "text"},
    "dingtalk": {"webhookUrl": "url", "secret": "secret"},
    "wecom": {"webhookUrl": "url"},
    "webhook": {"url": "url"},
}
SECRET_FIELDS = {
    "telegram": ("botToken",),
    "dingtalk": ("webhookUrl", "secret"),
    "wecom": ("webhookUrl",),
    "webhook": ("url",),
}
REQUIRED_FIELDS = {
    "telegram": ("botToken", "chatId"),
    "dingtalk": ("webhookUrl",),
    "wecom": ("webhookUrl",),
    "webhook": ("url",),
}

_client_factory = httpx.Client
_URL_RE = re.compile(r"https?://[^\s'\"<>]+")
_TELEGRAM_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
_QUERY_SECRET_RE = re.compile(r"(?i)(sign|access_token|token|secret)=[^&\s'\"]+")
_COOKIE_RE = re.compile(r"(?i)(cookie|sessionid\w*|sid_\w+|uid_tt\w*)([=:])\s*[^;\s,'\"]+")


class ChannelError(Exception):
    pass


def normalize_notifications(raw):
    """Fill every channel key so old or partial config.json values still work."""
    raw = raw if isinstance(raw, dict) else {}
    incoming = raw.get("channels") if isinstance(raw.get("channels"), dict) else {}
    defaults = DEFAULT_CONFIG["notifications"]["channels"]
    channels = {}
    for name, template in defaults.items():
        source = incoming.get(name) if isinstance(incoming.get(name), dict) else {}
        channel = {}
        for key, default in template.items():
            value = source.get(key, default)
            if isinstance(default, bool):
                channel[key] = bool(value)
            elif isinstance(default, dict):
                channel[key] = deepcopy(value) if isinstance(value, dict) else {}
            else:
                channel[key] = str(value or "").strip()
        channels[name] = channel
    return {"enabled": bool(raw.get("enabled", False)), "channels": channels}


def enabled_channel_names(config):
    settings = normalize_notifications(config)
    if not settings["enabled"]:
        return []
    return [name for name in CHANNEL_NAMES if settings["channels"][name]["enabled"]]


def make_message(title, text, *, event, date):
    return {"title": f"{MESSAGE_PREFIX}{title}", "text": text, "event": event, "date": date}


def mask_secret(value):
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 4:
        return "****"
    return text[:4] + "****"


def redact_url(url):
    """Keep scheme and host; hide the path (often a token) and the query."""
    text = str(url or "")
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return "****"
    if not parts.scheme or not parts.netloc:
        return mask_secret(text)
    host = parts.netloc.rsplit("@", 1)[-1]
    path = "/****" if parts.path not in ("", "/") else ""
    query = "?****" if parts.query else ""
    return f"{parts.scheme}://{host}{path}{query}"


def redact_text(value, *secrets):
    text = str(value or "")
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), mask_secret(secret))
    text = _URL_RE.sub(lambda match: redact_url(match.group(0)), text)
    text = _TELEGRAM_TOKEN_RE.sub("bot****", text)
    text = _QUERY_SECRET_RE.sub(lambda match: f"{match.group(1)}=****", text)
    text = _COOKIE_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}****", text)
    return text


def _dingtalk_signed_url(url, secret, timestamp=None):
    """DingTalk custom robot signing: HMAC-SHA256 over "timestamp\\nsecret"."""
    stamp = str(int(time.time() * 1000) if timestamp is None else timestamp)
    digest = hmac.new(
        secret.encode("utf-8"),
        f"{stamp}\n{secret}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    sign = base64.b64encode(digest).decode("ascii")
    parts = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key not in ("timestamp", "sign")
    ]
    query += [("timestamp", stamp), ("sign", sign)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _require_http_url(url):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("Webhook 地址必须以 http:// 或 https:// 开头")
    return url


def _build_request(name, channel, message):
    content = f"{message['title']}\n{message['text']}"
    if name == "telegram":
        token = channel.get("botToken") or ""
        chat_id = channel.get("chatId") or ""
        if not token or not chat_id:
            raise ValueError("未配置 Bot Token 或 Chat ID")
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        return url, {"chat_id": chat_id, "text": content}
    if name == "dingtalk":
        url = channel.get("webhookUrl") or ""
        if not url:
            raise ValueError("未配置 Webhook 地址")
        _require_http_url(url)
        if channel.get("secret"):
            url = _dingtalk_signed_url(url, channel["secret"])
        return url, {"msgtype": "text", "text": {"content": content}}
    if name == "wecom":
        url = channel.get("webhookUrl") or ""
        if not url:
            raise ValueError("未配置 Webhook 地址")
        _require_http_url(url)
        return url, {"msgtype": "text", "text": {"content": content}}
    if name == "webhook":
        url = channel.get("url") or ""
        if not url:
            raise ValueError("未配置 Webhook 地址")
        _require_http_url(url)
        payload = dict(channel.get("extraFields") or {})
        payload.update(
            {
                "title": message["title"],
                "text": message["text"],
                "event": message["event"],
                "date": message["date"],
            }
        )
        return url, payload
    raise ValueError("未知渠道")


def _client_options(use_proxy):
    if use_proxy:
        return {"timeout": REQUEST_TIMEOUT_SECONDS, "proxy": _proxy_url()}
    return {"timeout": REQUEST_TIMEOUT_SECONDS, "trust_env": False}


def _proxy_url():
    try:
        settings = get_app_settings()
    except Exception:
        settings = {}
    return douyin_proxy_url(settings) or DEFAULT_PROXY_URL


def _check_response(name, response):
    if response.status_code >= 400:
        raise ChannelError(f"HTTP {response.status_code}")
    if name == "webhook":
        return
    try:
        data = response.json()
    except ValueError:
        raise ChannelError("返回内容不是 JSON")
    if not isinstance(data, dict):
        raise ChannelError("返回内容格式异常")
    if name == "telegram":
        if not data.get("ok"):
            raise ChannelError(f"Telegram 返回错误（错误码 {data.get('error_code', '-')}）")
    elif data.get("errcode", 0) != 0:
        raise ChannelError(f"{CHANNEL_LABELS[name]} 返回错误码 {data.get('errcode')}")


def _describe_error(exc):
    if isinstance(exc, httpx.TimeoutException):
        return "请求超时"
    if isinstance(exc, ChannelError):
        return str(exc)
    if isinstance(exc, httpx.HTTPError):
        return f"网络错误（{type(exc).__name__}）"
    if isinstance(exc, ValueError):
        return str(exc)
    return f"未知错误（{type(exc).__name__}）"


def send_channel(name, channel, message):
    """Send one message through one channel. Returns (ok, detail); never raises."""
    channel = dict(channel or {})
    secrets = [channel.get(field) for field in SECRET_FIELDS.get(name, ())]
    try:
        url, payload = _build_request(name, channel, message)
        with _client_factory(**_client_options(bool(channel.get("useProxy")))) as client:
            response = client.post(url, json=payload)
        _check_response(name, response)
        return True, "发送成功"
    except Exception as exc:
        detail = redact_text(_describe_error(exc), *secrets)
        logger.warning("notify channel=%s failed: %s", name, detail)
        return False, detail


def send_message(message, config):
    """Send to every enabled channel. A failing channel never stops the others."""
    settings = normalize_notifications(config)
    results = {}
    if not settings["enabled"]:
        return results
    for name in CHANNEL_NAMES:
        channel = settings["channels"][name]
        if not channel["enabled"]:
            continue
        results[name] = send_channel(name, channel, message)[0]
    return results


def send_test(channel_name, config):
    """Settings-page test button: send one message via the saved channel config."""
    name = str(channel_name or "")
    if name not in CHANNEL_NAMES:
        return False, "未知推送渠道"
    channel = normalize_notifications(config)["channels"][name]
    message = make_message(
        "测试消息",
        "渠道配置正常，这是一条测试消息。",
        event="test",
        date="",
    )
    ok, detail = send_channel(name, channel, message)
    label = CHANNEL_LABELS[name]
    if ok:
        return True, f"{label} 测试消息已发送"
    return False, f"{label} 测试消息发送失败：{detail}"


def notifications_view(config):
    """Template context for the settings card. Raw secrets never appear here."""
    settings = normalize_notifications(config)
    channels = {}
    for name in CHANNEL_NAMES:
        channel = settings["channels"][name]
        masked = {}
        for field, kind in CHANNEL_FIELDS[name].items():
            if kind == "secret":
                masked[field] = mask_secret(channel.get(field))
            elif kind == "url":
                masked[field] = redact_url(channel.get(field))
        channels[name] = {
            "label": CHANNEL_LABELS[name],
            "enabled": channel["enabled"],
            "useProxy": channel["useProxy"],
            "chatId": channel.get("chatId", ""),
            "masked": masked,
            "configured": all(channel.get(field) for field in REQUIRED_FIELDS[name]),
        }
    return {"enabled": settings["enabled"], "channels": channels}


def apply_form(current, form):
    """Merge a settings form into the stored notifications config.

    Returns (normalized_config, errors). Empty secret inputs keep the stored
    value; a *_clear checkbox removes it.
    """
    settings = normalize_notifications(current)
    errors = []
    settings["enabled"] = str(form.get("notify_enabled", "")) == "on"
    for name in CHANNEL_NAMES:
        channel = settings["channels"][name]
        channel["enabled"] = str(form.get(f"notify_{name}_enabled", "")) == "on"
        if "useProxy" in channel:
            channel["useProxy"] = str(form.get(f"notify_{name}_use_proxy", "")) == "on"
        for field, kind in CHANNEL_FIELDS[name].items():
            key = f"notify_{name}_{field}"
            submitted = str(form.get(key, "") or "").strip()
            if kind == "text":
                channel[field] = submitted
                continue
            if str(form.get(f"{key}_clear", "")) == "on":
                channel[field] = ""
            elif submitted:
                channel[field] = submitted
            if kind == "url" and channel[field]:
                parts = urlsplit(channel[field])
                if parts.scheme not in ("http", "https") or not parts.netloc:
                    errors.append(f"{CHANNEL_LABELS[name]} 的地址必须以 http:// 或 https:// 开头")
        if channel["enabled"] and not all(channel.get(field) for field in REQUIRED_FIELDS[name]):
            errors.append(f"{CHANNEL_LABELS[name]} 已启用但缺少必填项")
    return settings, errors
