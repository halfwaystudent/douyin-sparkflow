import asyncio
import base64
import hashlib
import hmac
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import httpx
from fastapi.testclient import TestClient

from core import notifications, streak_state, tasks
from utils import notify
from webui import app as app_module


TZ = ZoneInfo("Asia/Shanghai")
ADMIN = {"username": "admin", "role": "admin", "account_refs": [], "session_id": "s-admin"}
FRIEND = {"username": "zxb", "role": "user", "account_refs": ["ref-1"], "session_id": "s-user"}
WINDOW_ON = tasks._normalize_send_window(
    {"dailySendWindow": {"enabled": True, "startHour": 10, "endHour": 18, "scheduleIntervalMinutes": 20}}
)
WINDOW_OFF = tasks._normalize_send_window({"dailySendWindow": {"enabled": False}})


def local(hour, minute=0, day=13):
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ).astimezone(timezone.utc)


def full_config(**channels):
    base = {
        "telegram": {"enabled": False, "useProxy": True, "botToken": "", "chatId": ""},
        "dingtalk": {"enabled": False, "useProxy": False, "webhookUrl": "", "secret": ""},
        "wecom": {"enabled": False, "useProxy": False, "webhookUrl": ""},
        "webhook": {"enabled": False, "useProxy": False, "url": "", "extraFields": {}},
    }
    for name, values in channels.items():
        base[name].update(values)
    return {"enabled": True, "channels": base}


class Transport:
    """Records httpx requests and the client options the code asked for."""

    def __init__(self, handler=None):
        self.requests = []
        self.client_kwargs = []
        self.handler = handler or (lambda request: httpx.Response(200, json={"ok": True, "errcode": 0}))

    def factory(self):
        def make(**kwargs):
            self.client_kwargs.append(kwargs)

            def route(request):
                self.requests.append(request)
                return self.handler(request)

            return httpx.Client(transport=httpx.MockTransport(route), timeout=kwargs.get("timeout"))

        return make


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.transport = Transport()
        patches = [
            patch.object(notify, "_client_factory", self.transport.factory()),
            patch.object(notify, "get_app_settings", return_value={"douyin_proxy_url": "http://proxy:7890"}),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.message = notify.make_message("测试", "正文", event="test", date="2026-10-13")

    def test_telegram_posts_bot_api_body_through_proxy(self):
        ok, _ = notify.send_channel(
            "telegram",
            {"enabled": True, "useProxy": True, "botToken": "123456:ABCDEF", "chatId": "-1001"},
            self.message,
        )
        self.assertTrue(ok)
        request = self.transport.requests[0]
        self.assertEqual("https://api.telegram.org/bot123456:ABCDEF/sendMessage", str(request.url))
        body = json.loads(request.content)
        self.assertEqual("-1001", body["chat_id"])
        self.assertEqual("【抖音火花】测试\n正文", body["text"])
        self.assertEqual("http://proxy:7890", self.transport.client_kwargs[0]["proxy"])

    def test_dingtalk_without_secret_is_plain_text_and_direct(self):
        url = "https://oapi.dingtalk.com/robot/send?access_token=TOKENVALUE"
        ok, _ = notify.send_channel(
            "dingtalk", {"useProxy": False, "webhookUrl": url, "secret": ""}, self.message
        )
        self.assertTrue(ok)
        request = self.transport.requests[0]
        self.assertEqual({"access_token": ["TOKENVALUE"]}, parse_qs(urlsplit(str(request.url)).query))
        self.assertEqual({"msgtype": "text", "text": {"content": "【抖音火花】测试\n正文"}}, json.loads(request.content))
        self.assertNotIn("proxy", self.transport.client_kwargs[0])
        self.assertIs(False, self.transport.client_kwargs[0]["trust_env"])

    def test_dingtalk_signature_matches_documented_hmac(self):
        secret = "SEC-test-secret"
        url = "https://oapi.dingtalk.com/robot/send?access_token=T"
        with patch.object(notify.time, "time", return_value=1792000000.123):
            ok, _ = notify.send_channel("dingtalk", {"useProxy": False, "webhookUrl": url, "secret": secret}, self.message)
        self.assertTrue(ok)
        query = parse_qs(urlsplit(str(self.transport.requests[0].url)).query)
        stamp = "1792000000123"
        expected = base64.b64encode(
            hmac.new(secret.encode(), f"{stamp}\n{secret}".encode(), hashlib.sha256).digest()
        ).decode()
        self.assertEqual([stamp], query["timestamp"])
        self.assertEqual([expected], query["sign"])
        self.assertEqual(["T"], query["access_token"])

    def test_wecom_body_format(self):
        ok, _ = notify.send_channel(
            "wecom", {"useProxy": False, "webhookUrl": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=K"}, self.message
        )
        self.assertTrue(ok)
        request = self.transport.requests[0]
        self.assertEqual({"msgtype": "text", "text": {"content": "【抖音火花】测试\n正文"}}, json.loads(request.content))

    def test_generic_webhook_carries_required_fields_and_extra_fields(self):
        channel = {
            "useProxy": False,
            "url": "https://example.com/hook",
            "extraFields": {"group": "ops", "title": "must-not-override"},
        }
        ok, _ = notify.send_channel("webhook", channel, self.message)
        self.assertTrue(ok)
        body = json.loads(self.transport.requests[0].content)
        self.assertEqual("【抖音火花】测试", body["title"])
        self.assertEqual("正文", body["text"])
        self.assertEqual("test", body["event"])
        self.assertEqual("2026-10-13", body["date"])
        self.assertEqual("ops", body["group"])

    def test_proxy_comes_from_env_override_then_panel_setting(self):
        channel = {"useProxy": True, "botToken": "1:A", "chatId": "2"}
        with patch.dict(os.environ, {"SPARKFLOW_DOUYIN_PROXY_URL": "http://env-proxy:7899"}):
            ok, _ = notify.send_channel("telegram", channel, self.message)
        self.assertTrue(ok)
        self.assertEqual("http://env-proxy:7899", self.transport.client_kwargs[-1]["proxy"])
        with patch.dict(os.environ, {"SPARKFLOW_DOUYIN_PROXY_URL": ""}):
            notify.send_channel("telegram", channel, self.message)
        self.assertEqual("http://proxy:7890", self.transport.client_kwargs[-1]["proxy"])

    def test_telegram_error_response_is_a_failure(self):
        self.transport.handler = lambda request: httpx.Response(200, json={"ok": False, "error_code": 400})
        ok, detail = notify.send_channel(
            "telegram", {"useProxy": True, "botToken": "1:A", "chatId": "2"}, self.message
        )
        self.assertFalse(ok)
        self.assertIn("400", detail)

    def test_dingtalk_errcode_is_a_failure(self):
        self.transport.handler = lambda request: httpx.Response(200, json={"errcode": 310000, "errmsg": "keywords"})
        ok, _ = notify.send_channel("wecom", {"useProxy": False, "webhookUrl": "https://q.example/x"}, self.message)
        self.assertFalse(ok)

    def test_timeout_is_reported_and_token_never_logged(self):
        def timeout(request):
            raise httpx.ReadTimeout(f"timed out for {request.url}")

        self.transport.handler = timeout
        with self.assertLogs("app", level="WARNING") as captured:
            ok, detail = notify.send_channel(
                "telegram", {"useProxy": True, "botToken": "987654:SECRETTOKEN", "chatId": "5"}, self.message
            )
        self.assertFalse(ok)
        self.assertEqual("请求超时", detail)
        logged = "\n".join(captured.output)
        self.assertIn("telegram", logged)
        self.assertNotIn("SECRETTOKEN", logged)
        self.assertNotIn("987654:", logged)

    def test_one_failing_channel_does_not_stop_the_others(self):
        def handler(request):
            if "dingtalk" in request.url.host:
                raise httpx.ConnectError("refused")
            return httpx.Response(200, json={"ok": True, "errcode": 0})

        self.transport.handler = handler
        results = notify.send_message(
            self.message,
            full_config(
                telegram={"enabled": True, "botToken": "1:A", "chatId": "2"},
                dingtalk={"enabled": True, "webhookUrl": "https://oapi.dingtalk.com/robot/send?access_token=X"},
                wecom={"enabled": True, "webhookUrl": "https://qyapi.weixin.qq.com/send?key=K"},
            ),
        )
        self.assertEqual({"telegram": True, "dingtalk": False, "wecom": True}, results)

    def test_disabled_master_switch_sends_nothing(self):
        config = full_config(telegram={"enabled": True, "botToken": "1:A", "chatId": "2"})
        config["enabled"] = False
        self.assertEqual({}, notify.send_message(self.message, config))
        self.assertEqual([], self.transport.requests)

    def test_send_test_reports_chinese_result(self):
        config = full_config(wecom={"enabled": False, "webhookUrl": "https://qyapi.weixin.qq.com/send?key=K"})
        ok, message = notify.send_test("wecom", config)
        self.assertTrue(ok)
        self.assertIn("企业微信机器人 测试消息已发送", message)
        ok, message = notify.send_test("telegram", full_config())
        self.assertFalse(ok)
        self.assertIn("未配置 Bot Token 或 Chat ID", message)
        self.assertEqual((False, "未知推送渠道"), notify.send_test("bark", config))


class ConfigAndRedactionTests(unittest.TestCase):
    def test_old_partial_config_is_normalized_with_defaults(self):
        settings = notify.normalize_notifications({"enabled": True, "channels": {"telegram": {"enabled": True}}})
        self.assertTrue(settings["enabled"])
        telegram = settings["channels"]["telegram"]
        self.assertEqual({"enabled": True, "useProxy": True, "botToken": "", "chatId": ""}, telegram)
        self.assertFalse(settings["channels"]["webhook"]["enabled"])
        self.assertEqual({}, settings["channels"]["webhook"]["extraFields"])
        self.assertEqual([], notify.enabled_channel_names({}))
        self.assertEqual([], notify.enabled_channel_names({"enabled": False, "channels": {"wecom": {"enabled": True}}}))

    def test_redaction_hides_tokens_paths_queries_and_cookies(self):
        text = (
            "failed https://oapi.dingtalk.com/robot/send?access_token=ABCDEF&sign=SIGN "
            "bot123456:TOKENX cookie sessionid=COOKIEVALUE"
        )
        redacted = notify.redact_text(text, "PLAINSECRET")
        self.assertNotIn("ABCDEF", redacted)
        self.assertNotIn("SIGN", redacted)
        self.assertNotIn("TOKENX", redacted)
        self.assertNotIn("COOKIEVALUE", redacted)
        self.assertIn("https://oapi.dingtalk.com/****?****", redacted)
        self.assertEqual("PLAI****", notify.mask_secret("PLAINSECRET"))
        self.assertEqual("****", notify.mask_secret("ab"))
        self.assertEqual("https://example.com/****", notify.redact_url("https://example.com/abc/def"))

    def test_apply_form_keeps_clears_and_validates(self):
        stored = full_config(
            telegram={"enabled": True, "botToken": "111:STORED", "chatId": "9"},
            webhook={"enabled": False, "url": "https://example.com/hook/PATH"},
        )
        form = {
            "notify_enabled": "on",
            "notify_telegram_enabled": "on",
            "notify_telegram_use_proxy": "on",
            "notify_telegram_botToken": "",
            "notify_telegram_chatId": "10",
            "notify_webhook_url": "",
            "notify_webhook_url_clear": "on",
        }
        settings, errors = notify.apply_form(stored, form)
        self.assertEqual([], errors)
        self.assertEqual("111:STORED", settings["channels"]["telegram"]["botToken"])
        self.assertEqual("10", settings["channels"]["telegram"]["chatId"])
        self.assertEqual("", settings["channels"]["webhook"]["url"])
        self.assertTrue(settings["channels"]["telegram"]["useProxy"])
        self.assertFalse(settings["channels"]["dingtalk"]["enabled"])

        _, errors = notify.apply_form(stored, {"notify_enabled": "on", "notify_wecom_enabled": "on"})
        self.assertIn("企业微信机器人 已启用但缺少必填项", errors)
        _, errors = notify.apply_form(stored, {"notify_dingtalk_webhookUrl": "ftp://bad"})
        self.assertTrue(any("http://" in item for item in errors))

    def test_view_never_contains_raw_secrets(self):
        config = full_config(
            telegram={"enabled": True, "botToken": "123456:SECRETTOKENVALUE", "chatId": "42"},
            webhook={"enabled": True, "url": "https://example.com/hook/SECRETPATH?token=ABCDEF"},
        )
        view = notify.notifications_view(config)
        rendered = json.dumps(view, ensure_ascii=False)
        self.assertNotIn("SECRETTOKENVALUE", rendered)
        self.assertNotIn("SECRETPATH", rendered)
        self.assertNotIn("ABCDEF", rendered)
        self.assertEqual("1234****", view["channels"]["telegram"]["masked"]["botToken"])
        self.assertEqual("https://example.com/****?****", view["channels"]["webhook"]["masked"]["url"])
        self.assertTrue(view["channels"]["telegram"]["configured"])
        self.assertFalse(view["channels"]["wecom"]["configured"])


class EventTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.state_path = Path(self.temp_dir.name) / "notify_state.json"
        self.transport = Transport()
        for item in (
            patch.object(notify, "_client_factory", self.transport.factory()),
            patch.object(notify, "get_app_settings", return_value={"douyin_proxy_url": "http://proxy:7890"}),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.config = {"notifications": full_config(webhook={"enabled": True, "url": "https://example.com/hook/SECRETPATH"})}

    def run_hook(self, users, *, now, status="completed", manual=False, fallback=False, window=WINDOW_ON, config=None):
        return notifications.notify_run_finished(
            "run-1",
            status,
            config or self.config,
            users,
            schedule_tz=TZ,
            send_window=window,
            manual_run=manual,
            fallback_run=fallback,
            now=now,
            state_path=self.state_path,
        )

    def summaries(self):
        return [body for body in self.bodies() if body["event"] == "daily_summary"]

    def failures(self):
        return [body for body in self.bodies() if body["event"] == "failure"]

    def bodies(self):
        return [json.loads(request.content) for request in self.transport.requests]

    def account(self, name="主号", ref="ref-1", targets=("阿杰", "晓晓"), unique_id="1001"):
        return {
            "account_ref": ref,
            "unique_id": unique_id,
            "username": name,
            "enabled": True,
            "targets": list(targets),
        }

    def test_preflight_failure_is_sent_once_per_day_and_survives_reload(self):
        account = self.account(targets=[])
        account["account_health"] = {
            "healthy": False,
            "category": "login_required",
            "reason": "account requires login",
            "checkedAt": local(11).astimezone(timezone.utc).isoformat(timespec="seconds"),
        }
        self.run_hook([account], now=local(11))
        self.assertEqual(1, len(self.transport.requests))
        body = self.failures()[0]
        self.assertIn("主号", body["text"])
        self.assertIn("需要重新登录", body["text"])
        self.assertNotIn("1001", body["text"])

        self.run_hook([account], now=local(11, 20))
        self.assertEqual(1, len(self.transport.requests))
        # A restart re-reads the state file and must not repeat the notice.
        self.run_hook([account], now=local(11, 40))
        self.assertEqual(1, len(self.transport.requests))

        next_day = local(11, 0, day=14)
        account["account_health"]["checkedAt"] = next_day.isoformat(timespec="seconds")
        self.run_hook([account], now=next_day)
        self.assertEqual(2, len(self.transport.requests))

    def test_multiple_failures_in_one_run_merge_into_one_message(self):
        account = self.account()
        streak_state.mark_failed(account, "阿杰", category="send_rejected", reason="refused", retryable=False, now=local(12))
        streak_state.mark_failed(account, "晓晓", category="send_rejected", reason="refused", retryable=False, now=local(12))
        self.run_hook([account], now=local(12))
        self.assertEqual(1, len(self.transport.requests))
        text = self.failures()[0]["text"]
        self.assertIn("阿杰", text)
        self.assertIn("晓晓", text)
        self.assertIn("消息被平台拒绝", text)

    def test_retryable_failure_waits_for_fallback_before_notifying(self):
        account = self.account(targets=["阿杰"])
        streak_state.mark_failed(account, "阿杰", category="send_unavailable", reason="net", retryable=True, now=local(12))
        self.run_hook([account], now=local(12))
        self.assertEqual([], self.transport.requests)
        streak_state.mark_fallback_attempted(account, "阿杰", now=local(12, 5))
        self.run_hook([account], now=local(12, 5))
        self.assertEqual(1, len(self.transport.requests))

    def test_whole_run_crash_is_notified_once(self):
        self.run_hook([self.account(targets=[])], status="failed", now=local(12))
        self.run_hook([self.account(targets=[])], status="failed", now=local(12, 20))
        self.assertEqual(1, len(self.transport.requests))
        self.assertIn("整次运行异常退出", self.failures()[0]["text"])

    def test_skipped_runs_and_disabled_notifications_send_nothing(self):
        account = self.account(targets=[])
        account["account_health"] = {"healthy": False, "category": "login_required", "checkedAt": local(11).isoformat()}
        self.run_hook([account], status="skipped_task_in_progress", now=local(11))
        self.run_hook([], now=local(11))
        off = {"notifications": dict(self.config["notifications"], enabled=False)}
        self.run_hook([account], now=local(11), config=off)
        self.assertEqual([], self.transport.requests)
        self.assertFalse(self.state_path.exists())

    def test_all_confirmed_summary_on_scheduled_run_once_per_day(self):
        account = self.account()
        streak_state.mark_send_confirmed(account, "阿杰", now=local(11))
        streak_state.mark_send_confirmed(account, "晓晓", now=local(11))
        self.run_hook([account], now=local(11))
        self.assertEqual(1, len(self.summaries()))
        body = self.summaries()[0]
        self.assertIn("成功目标：2 个", body["text"])
        self.assertIn("失败目标：0 个", body["text"])
        self.assertIn("全部目标已确认成功", body["text"])
        self.run_hook([account], now=local(11, 30))
        self.run_hook([account], now=local(17, 40), fallback=True, manual=True)
        self.assertEqual(1, len(self.summaries()))

    def test_manual_run_never_sends_summary(self):
        account = self.account()
        streak_state.mark_send_confirmed(account, "阿杰", now=local(11))
        streak_state.mark_send_confirmed(account, "晓晓", now=local(11))
        self.run_hook([account], now=local(11), manual=True)
        self.assertEqual([], self.summaries())
        self.assertFalse(self.state_path.exists())

    def test_manual_run_never_sends_summary_even_with_unfinished_targets(self):
        account = self.account(targets=["阿杰"])
        self.run_hook([account], now=local(18, 0), manual=True, window=WINDOW_OFF)
        self.run_hook([account], now=local(18, 0), manual=True, window=WINDOW_ON)
        self.assertEqual([], self.summaries())

    def test_window_end_summary_fires_on_fallback_run_not_on_last_scheduled_run(self):
        account = self.account(targets=["阿杰"])
        streak_state.mark_failed(account, "阿杰", category="send_rejected", reason="x", retryable=False, now=local(9, 30))
        self.run_hook([account], now=local(17, 40))
        self.assertEqual([], self.summaries())
        self.run_hook([account], now=local(18, 0), fallback=True, manual=True)
        self.assertEqual(1, len(self.summaries()))
        text = self.summaries()[0]["text"]
        self.assertIn("发送窗口末次兜底运行结束", text)
        self.assertIn("失败目标：1 个", text)
        self.assertIn("消息被平台拒绝", text)

    def test_final_point_counts_every_unconfirmed_target_and_totals_add_up(self):
        account = self.account(targets=["甲", "乙", "丙"])
        streak_state.mark_send_confirmed(account, "甲", now=local(11))
        streak_state.mark_failed(account, "乙", category="send_rejected", reason="x", retryable=False, now=local(12))
        self.run_hook([account], now=local(18, 0), fallback=True, manual=True)
        text = self.summaries()[0]["text"]
        self.assertIn("启用目标：3 个", text)
        self.assertIn("成功目标：1 个", text)
        self.assertIn("失败目标：2 个", text)
        self.assertIn("当天未发送成功", text)
        self.assertIn("消息被平台拒绝", text)
        failure_text = "\n".join(body["text"] for body in self.failures())
        self.assertIn("目标「丙」：当天未发送成功", failure_text)
        self.assertIn("目标「乙」：消息被平台拒绝", failure_text)
        self.assertNotIn("目标「甲」", failure_text)

    def test_browser_failed_retryable_is_reported_at_final_point_only(self):
        account = self.account(targets=["阿杰"])
        streak_state.mark_failed(account, "阿杰", category="send_unavailable", reason="net", retryable=True, now=local(12))
        self.run_hook([account], now=local(12, 30))
        self.assertEqual([], self.transport.requests)
        self.run_hook([account], now=local(18, 0), fallback=True, manual=True)
        self.assertEqual(1, len(self.failures()))
        self.assertIn("目标「阿杰」", self.failures()[0]["text"])
        self.assertEqual(1, len(self.summaries()))

    def test_window_disabled_scheduled_run_is_final_and_manual_is_not(self):
        account = self.account(targets=["阿杰"])
        self.run_hook([account], now=local(10), manual=True, window=WINDOW_OFF)
        self.assertEqual([], self.transport.requests)
        self.run_hook([account], now=local(10, 0), window=WINDOW_OFF)
        self.assertEqual(1, len(self.summaries()))
        self.assertIn("定时运行结束", self.summaries()[0]["text"])
        self.assertIn("当天未发送成功", self.failures()[0]["text"])

    def test_invalid_window_is_disabled_by_normalization(self):
        invalid = tasks._normalize_send_window({"dailySendWindow": {"enabled": True, "startHour": 20, "endHour": 18}})
        self.assertFalse(invalid["enabled"])
        self.run_hook([self.account(targets=["阿杰"])], now=local(17, 40), window=invalid)
        self.assertEqual(1, len(self.summaries()))

    def test_sent_unverified_counts_as_success_in_summary_and_final_point(self):
        account = self.account(targets=["甲", "乙", "丙"])
        streak_state.mark_sent_unverified(account, "甲", run_id="r1", now=local(11))
        streak_state.mark_send_confirmed(account, "乙", now=local(11))
        self.run_hook([account], now=local(18, 0), fallback=True, manual=True)
        text = self.summaries()[0]["text"]
        self.assertIn("启用目标：3 个", text)
        self.assertIn("成功目标：2 个", text)
        self.assertIn("失败目标：1 个", text)
        self.assertNotIn("目标「甲」", text)
        failure_text = "\n".join(body["text"] for body in self.failures())
        self.assertIn("目标「丙」：当天未发送成功", failure_text)
        self.assertNotIn("目标「甲」", failure_text)

    def test_all_sent_unverified_or_confirmed_triggers_all_success_summary(self):
        account = self.account()
        streak_state.mark_sent_unverified(account, "阿杰", run_id="r1", now=local(11))
        streak_state.mark_send_confirmed(account, "晓晓", now=local(11))
        self.run_hook([account], now=local(11))
        self.assertEqual([], self.failures())
        self.assertEqual(1, len(self.summaries()))
        text = self.summaries()[0]["text"]
        self.assertIn("成功目标：2 个", text)
        self.assertIn("失败目标：0 个", text)
        self.assertIn("全部目标已确认成功", text)

    def test_sent_unverified_from_yesterday_is_not_success_today(self):
        account = self.account(targets=["阿杰"])
        streak_state.mark_sent_unverified(account, "阿杰", run_id="r1", now=local(11, 0, day=12))
        self.run_hook([account], now=local(18, 0), fallback=True, manual=True)
        self.assertIn("成功目标：0 个", self.summaries()[0]["text"])
        self.assertIn("目标「阿杰」：当天未发送成功", self.failures()[0]["text"])

    def test_summary_survives_restart_without_repeating(self):
        account = self.account(targets=["阿杰"])
        streak_state.mark_send_confirmed(account, "阿杰", now=local(11))
        self.run_hook([account], now=local(11))
        self.run_hook([account], now=local(11, 30))
        self.assertEqual(1, len(self.summaries()))

    def test_hook_errors_are_swallowed(self):
        with patch.object(notifications, "_notify_run_finished", side_effect=RuntimeError("boom token=XYZ")):
            with self.assertLogs("app", level="WARNING"):
                self.assertIsNone(self.run_hook([self.account()], now=local(11)))

    def test_state_file_is_pruned_and_valid_json(self):
        old = local(11, 0, day=1)
        self.state_path.write_text(
            json.dumps({"dedupe": {"2026-10-01|x|-|login_required": old.isoformat()}, "summaries": {"2026-10-01": "x"}}),
            encoding="utf-8",
        )
        account = self.account(targets=[])
        account["account_health"] = {"healthy": False, "category": "login_required", "checkedAt": local(11).isoformat()}
        self.run_hook([account], now=local(11))
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertNotIn("2026-10-01|x|-|login_required", saved["dedupe"])
        self.assertNotIn("2026-10-01", saved["summaries"])


class RunTasksIntegrationTests(unittest.TestCase):
    def _patches(self, config, user):
        return [
            patch.object(tasks, "get_config", return_value=config),
            patch.object(tasks, "get_userData", return_value=[user]),
            patch.object(tasks, "_requested_account_refs", return_value=None),
            patch.object(tasks, "_prepare_active_users_for_run", return_value=[user]),
            patch.object(tasks, "_split_sender_modes", return_value=([], [user])),
            patch.object(tasks, "task_run_lock", MagicMock()),
            patch.object(tasks, "run_browser_tasks", new=AsyncMock()),
        ]

    def test_notify_error_does_not_change_run_status_or_report(self):
        config = {"multiTask": False, "taskCount": 1, "sendStrategy": {}}
        user = {"username": "demo", "unique_id": "1", "account_ref": "r", "targets": ["阿杰"], "enabled": True}
        patches = self._patches(config, user) + [
            patch.object(tasks.notifications, "notify_run_finished", side_effect=RuntimeError("notify down")),
            patch.object(tasks, "_append_streak_run_report"),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        append = tasks._append_streak_run_report
        asyncio.run(tasks.runTasks())
        append.assert_called_once()
        self.assertEqual("completed", append.call_args.kwargs.get("run_status"))

    def test_runtasks_without_notifications_config_touches_no_state(self):
        config = {"multiTask": False, "taskCount": 1, "sendStrategy": {}}
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "notify_state.json"
            with (
                patch.object(tasks, "get_config", return_value=config),
                patch.object(tasks, "get_userData", return_value=[]),
                patch.object(tasks, "_requested_account_refs", return_value=None),
                patch.object(tasks, "_append_streak_run_report"),
                patch.object(notifications, "NOTIFY_STATE_PATH", state_path),
                patch.object(notify, "_client_factory") as client,
            ):
                asyncio.run(tasks.runTasks())
            self.assertFalse(state_path.exists())
            client.assert_not_called()


class NotificationWebTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_module.app, raise_server_exceptions=True)
        self.store = {
            "messageTemplate": "续火花",
            "sendStrategy": {"messageVariants": [], "shuffleTargets": True,
                             "messageIntervalSecondsMin": 45, "messageIntervalSecondsMax": 150},
            "notifications": full_config(telegram={"enabled": True, "botToken": "123456:SECRETTOKENVALUE", "chatId": "42"}),
        }
        patches = [
            patch.object(app_module, "get_userData", return_value=[]),
            patch.object(app_module, "get_visible_accounts", return_value=[]),
            patch.object(app_module, "get_ops_snapshot", return_value={
                "send_console": {"summary": {}, "accounts": [], "nowDisplay": "-"},
                "task_lock": {"running": False, "stale": False, "ageSeconds": 0},
                "schedule": {"label": "-", "nextTriggerDisplay": "-"},
                "daily_schedule": "",
                "containers": [],
            }),
            patch.object(app_module, "get_config", side_effect=lambda **_: json.loads(json.dumps(self.store))),
            patch.object(app_module, "get_web_users", return_value=[]),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def _as(self, principal):
        for item in (
            patch.object(app_module, "current_user", return_value=principal["username"]),
            patch.object(app_module, "current_principal", return_value=principal),
            patch.object(app_module, "validate_csrf", return_value=True),
        ):
            item.start()
            self.addCleanup(item.stop)

    def _fake_update_config(self, mutate, **_):
        result, changed = mutate(self.store)
        return result

    def test_admin_settings_page_shows_masked_notify_section(self):
        self._as(ADMIN)
        response = self.client.get("/settings", follow_redirects=False)
        self.assertEqual(200, response.status_code)
        self.assertIn('id="notify"', response.text)
        self.assertIn('action="/settings/notifications"', response.text)
        self.assertIn("1234****", response.text)
        self.assertNotIn("SECRETTOKENVALUE", response.text)
        self.assertNotIn("123456:", response.text)
        self.assertIn('action="/settings/notifications/test"', response.text)

    def test_non_admin_page_never_renders_notify_section(self):
        self._as(FRIEND)
        response = self.client.get("/settings", follow_redirects=False)
        self.assertEqual(200, response.status_code)
        self.assertNotIn('id="notify"', response.text)
        self.assertNotIn("/settings/notifications", response.text)
        self.assertNotIn("SECRETTOKENVALUE", response.text)

    def test_notification_routes_are_admin_only(self):
        self.assertEqual(303, self.client.post("/settings/notifications", data={}, follow_redirects=False).status_code)
        self.assertEqual(303, self.client.post("/settings/notifications/test", data={}, follow_redirects=False).status_code)
        self._as(FRIEND)
        self.assertEqual(403, self.client.post("/settings/notifications", data={}, follow_redirects=False).status_code)
        self.assertEqual(
            403,
            self.client.post("/settings/notifications/test", data={"channel": "telegram"}, follow_redirects=False).status_code,
        )

    def test_notification_routes_require_csrf(self):
        self._as(ADMIN)
        with patch.object(app_module, "validate_csrf", return_value=False):
            self.assertEqual(403, self.client.post("/settings/notifications", data={}, follow_redirects=False).status_code)
            self.assertEqual(
                403,
                self.client.post("/settings/notifications/test", data={"channel": "telegram"}, follow_redirects=False).status_code,
            )

    def test_save_keeps_empty_secret_and_clear_removes_it(self):
        self._as(ADMIN)
        with patch.object(app_module, "update_config", side_effect=self._fake_update_config):
            response = self.client.post(
                "/settings/notifications",
                data={"notify_enabled": "on", "notify_telegram_enabled": "on", "notify_telegram_chatId": "77", "notify_telegram_botToken": ""},
                follow_redirects=False,
            )
            self.assertEqual("/settings#notify", response.headers["location"])
            telegram = self.store["notifications"]["channels"]["telegram"]
            self.assertEqual("123456:SECRETTOKENVALUE", telegram["botToken"])
            self.assertEqual("77", telegram["chatId"])

            self.client.post(
                "/settings/notifications",
                data={"notify_enabled": "on", "notify_telegram_enabled": "", "notify_telegram_botToken_clear": "on"},
                follow_redirects=False,
            )
            self.assertEqual("", self.store["notifications"]["channels"]["telegram"]["botToken"])

    def test_save_rejects_invalid_webhook_without_writing(self):
        self._as(ADMIN)
        before = json.loads(json.dumps(self.store))
        with patch.object(app_module, "update_config", side_effect=self._fake_update_config):
            self.client.post(
                "/settings/notifications",
                data={"notify_enabled": "on", "notify_webhook_enabled": "on", "notify_webhook_url": "ftp://bad"},
                follow_redirects=False,
            )
        self.assertEqual(before, self.store)

    def test_use_proxy_checkboxes_are_rendered_and_saved(self):
        self._as(ADMIN)
        self.store["notifications"]["channels"]["dingtalk"]["useProxy"] = True
        page = self.client.get("/settings", follow_redirects=False).text
        self.assertIn('name="notify_dingtalk_use_proxy" checked', page)
        self.assertIn('name="notify_wecom_use_proxy"', page)
        self.assertIn('name="notify_webhook_use_proxy"', page)
        with patch.object(app_module, "update_config", side_effect=self._fake_update_config):
            self.client.post(
                "/settings/notifications",
                data={"notify_enabled": "on", "notify_dingtalk_enabled": "on",
                      "notify_dingtalk_webhookUrl": "https://oapi.dingtalk.com/robot/send?access_token=NEW",
                      "notify_dingtalk_use_proxy": "on"},
                follow_redirects=False,
            )
            self.assertTrue(self.store["notifications"]["channels"]["dingtalk"]["useProxy"])
            self.client.post(
                "/settings/notifications",
                data={"notify_enabled": "on", "notify_dingtalk_enabled": "on", "notify_wecom_enabled": "on",
                      "notify_wecom_webhookUrl": "https://qyapi.weixin.qq.com/send?key=K"},
                follow_redirects=False,
            )
            self.assertFalse(self.store["notifications"]["channels"]["dingtalk"]["useProxy"])
            self.assertFalse(self.store["notifications"]["channels"]["wecom"]["useProxy"])
            self.assertIn("access_token=NEW", self.store["notifications"]["channels"]["dingtalk"]["webhookUrl"])

    def test_notification_routes_return_inline_results_for_ajax_submissions(self):
        self._as(ADMIN)
        with patch.object(app_module, "update_config", side_effect=self._fake_update_config):
            saved = self.client.post(
                "/settings/notifications",
                headers={"X-Requested-With": "XMLHttpRequest"},
                data={"notify_enabled": "on"},
                follow_redirects=False,
            )
        self.assertEqual(200, saved.status_code)
        self.assertEqual({"message": "推送设置已保存。", "level": "success"}, saved.json())
        self.assertNotIn("location", saved.headers)

        with patch.object(app_module.notify, "send_test", return_value=(True, "钉钉机器人 测试消息已发送")):
            tested = self.client.post(
                "/settings/notifications/test",
                headers={"X-Requested-With": "XMLHttpRequest"},
                data={"channel": "dingtalk"},
                follow_redirects=False,
            )
        self.assertEqual(200, tested.status_code)
        self.assertEqual({"message": "钉钉机器人 测试消息已发送", "level": "success"}, tested.json())
        self.assertNotIn("location", tested.headers)

    def test_ajax_notification_save_does_not_leave_a_stale_flash(self):
        self._as(ADMIN)
        with patch.object(app_module, "update_config", side_effect=self._fake_update_config):
            response = self.client.post(
                "/settings/notifications",
                headers={"X-Requested-With": "XMLHttpRequest"},
                data={"notify_enabled": "on"},
                follow_redirects=False,
            )
        self.assertEqual(200, response.status_code)
        next_page = self.client.get("/settings", follow_redirects=False)
        self.assertNotIn("推送设置已保存。", next_page.text)

    def test_notification_forms_opt_into_inline_submission(self):
        self._as(ADMIN)
        response = self.client.get("/settings", follow_redirects=False)
        self.assertIn('id="notify-feedback"', response.text)
        save_form_start = response.text.index('action="/settings/notifications" class="form-stack" data-ajax-submit')
        save_form_end = response.text.index("</form>", save_form_start)
        feedback_pos = response.text.index('id="notify-feedback"')
        self.assertLess(save_form_start, feedback_pos)
        self.assertLess(feedback_pos, save_form_end)
        self.assertLess(feedback_pos, response.text.index('action="/settings/notifications/test"'))
        self.assertIn('id="notify-test-feedback-dingtalk"', response.text)
        self.assertIn('action="/settings/notifications" class="form-stack" data-ajax-submit', response.text)
        self.assertIn('action="/settings/notifications/test" class="form-actions" data-ajax-submit', response.text)
        script = (Path(app_module.STATIC_DIR) / "app.js").read_text(encoding="utf-8")
        self.assertIn('form[data-ajax-submit]', script)
        self.assertIn('X-Requested-With": "XMLHttpRequest"', script)
        self.assertIn("feedbackMessageFor", script)
        self.assertIn('id="notify-test-feedback-dingtalk"', response.text)

    def test_test_button_uses_saved_config_and_flashes_result(self):
        self._as(ADMIN)
        with patch.object(app_module.notify, "send_test", return_value=(True, "Telegram Bot 测试消息已发送")) as send:
            response = self.client.post(
                "/settings/notifications/test",
                data={"channel": "telegram", "notify_telegram_chatId": "unsaved"},
                follow_redirects=False,
            )
        self.assertEqual("/settings#notify", response.headers["location"])
        self.assertEqual("telegram", send.call_args.args[0])
        self.assertEqual("123456:SECRETTOKENVALUE", send.call_args.args[1]["channels"]["telegram"]["botToken"])
        self.assertEqual("42", send.call_args.args[1]["channels"]["telegram"]["chatId"])

    def test_test_button_rejects_unknown_channel(self):
        self._as(ADMIN)
        with patch.object(app_module.notify, "send_test") as send:
            self.client.post("/settings/notifications/test", data={"channel": "bark"}, follow_redirects=False)
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
