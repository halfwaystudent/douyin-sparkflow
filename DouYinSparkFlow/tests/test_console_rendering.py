import re
import unittest
from pathlib import Path

from webui import app as app_module


def _account_row(**overrides):
    row = {
        "unique_id": "1001",
        "username": "demo",
        "state": "healthy",
        "total_targets": 8,
        "confirmed_targets": [],
        "page_echo_targets": [],
        "page_echo_count": 0,
        "receipt_only_targets": [],
        "receipt_only_count": 0,
        "unconfirmed_targets": [],
        "legacy_unverified_targets": [],
        "failed_targets": [],
        "pending_targets": [],
        "unprocessed_targets": [],
        "account_blocked_targets": [],
        "attention_count": 0,
        "pending_count": 0,
        "last_confirmed_at": "",
        "last_confirmed_display": "",
        "account_failure": {},
        "account_failure_pause_after": 2,
        "account_paused": False,
        "account_health": {},
        "warnings": [],
        "failure_queue": {},
        "friend_index_meta": {},
        "friend_index_count": 0,
        "orphan_history_records": [],
        "orphan_failure_records": [],
        "last_failure_reason": "",
        "last_unconfirmed_reason": "",
    }
    row.update(overrides)
    return row


def _summary(**overrides):
    summary = {
        "total_targets": 0,
        "enabled_accounts": 1,
        "today_confirmed_targets": 0,
        "today_page_echo_targets": 0,
        "today_receipt_only_targets": 0,
        "today_unconfirmed_targets": 0,
        "today_failed_targets": 0,
        "today_account_blocked_targets": 0,
        "today_account_paused": 0,
        "today_attention_targets": 0,
        "today_warning_count": 0,
        "today_pending_targets": 0,
        "today_unprocessed_targets": 0,
        "today_remaining_targets": 0,
        "today_legacy_unverified_targets": 0,
        "today_page_echo_targets": 0,
        "last_confirmed_at": "",
        "last_confirmed_display": "",
        "all_confirmed": False,
    }
    summary.update(overrides)
    return summary


class DashboardProgressRenderingTests(unittest.TestCase):
    """The dashboard must show weak-evidence sends as progress, in yellow."""

    def setUp(self):
        self._original_window_state = app_module.templates.env.globals.get(
            "schedule_window_state"
        )

        def restore():
            app_module.templates.env.globals["schedule_window_state"] = (
                self._original_window_state
            )

        self.addCleanup(restore)

    def _render(self, summary, accounts, **ops_overrides):
        ops = {
            "send_console": {
                "summary": summary,
                "accounts": accounts,
                "nowDisplay": "2026-09-19 12:00",
            },
            "task_lock": {"running": False, "stale": False, "ageSeconds": 0},
            "schedule": {"nextTriggerDisplay": "18:00", "label": "10:00-18:00/20m"},
            "schedule_alignment": {
                "windowEnabled": True,
                "configLabel": "10:00-18:00",
                "fixedLabel": "",
                "detail": "cron 与配置一致",
                "aligned": True,
                "missingTriggers": False,
                "lines": ["*/20 10-17 * * *"],
            },
            "recent_triggers": ["[AUTO_TRIGGER] 2026-09-19T12:40:09+08:00 scheduled send start"],
            "daily_schedule": "10:00-18:00/20m",
        }
        ops.update(ops_overrides)
        context = {
            "flash": None,
            "accounts": [],
            "runtime_config": {
                "messageTemplate": "🤩今日火花+1",
                "sendStrategy": {
                    "messageVariants": ["今天也来冒个泡"],
                    "shuffleTargets": True,
                    "messageIntervalSecondsMin": 45,
                    "messageIntervalSecondsMax": 150,
                },
            },
            "ops": ops,
            "principal": {"role": "admin", "username": "admin"},
            "is_admin": True,
            "web_users": [],
            "all_accounts": [],
            "app_settings": {
                "compose_root": "/opt/douyin-sparkflow",
                "ops_log_file": "/app/logs/douyin-sparkflow.log",
                "proxy_refresh_script": "./refresh_proxy.sh",
                "login_desktop_api_url": "http://login-desktop:18090",
                "douyin_network_mode": "direct",
                "douyin_proxy_url": "http://proxy:7890",
                "ui_port": 8787,
            },
            "csrf_token": "test",
        }
        return app_module.templates.env.get_template("dashboard.html").render(context)

    def test_all_page_echo_sends_still_drive_the_progress(self):
        summary = _summary(
            total_targets=11,
            enabled_accounts=3,
            today_confirmed_targets=0,
            today_page_echo_targets=11,
        )
        account = _account_row(
            total_targets=8,
            page_echo_count=8,
            page_echo_targets=[{"status": "sent_page_echo"}] * 8,
        )

        html = self._render(summary, [account])

        self.assertIn("--strong-pct: 0%", html)
        self.assertIn("--weak-pct: 100%", html)
        self.assertIn("今日发送进度", html)
        self.assertIn('<small>已发送</small>', html)
        self.assertIn('data-overview-value="progress">11/11<', html)
        self.assertIn("已发送 8/8 · 强确认 0", html)
        self.assertIn("progress-echo", html)
        self.assertIn("弱证据（仅回显/仅回执） <b data-overview-value=\"weakSent\">11</b>", html)

    def test_mixed_evidence_splits_the_bar_and_the_percentage(self):
        summary = _summary(
            total_targets=10,
            today_confirmed_targets=2,
            today_page_echo_targets=3,
            today_receipt_only_targets=1,
        )
        account = _account_row(
            total_targets=10,
            confirmed_targets=[{"status": "sent"}] * 2,
            page_echo_count=3,
            page_echo_targets=[{"status": "sent_page_echo"}] * 3,
            receipt_only_count=1,
            receipt_only_targets=[{"status": "sent_receipt_only"}],
        )

        html = self._render(summary, [account])

        self.assertIn("--strong-pct: 20%", html)
        self.assertIn("--weak-pct: 40%", html)
        self.assertIn('data-overview-value="progress">6/10<', html)
        self.assertIn("已发送 6/10 · 强确认 2", html)

    def test_strong_only_keeps_the_original_single_segment(self):
        summary = _summary(
            total_targets=8,
            today_confirmed_targets=8,
        )
        account = _account_row(
            total_targets=8,
            confirmed_targets=[{"status": "sent"}] * 8,
        )

        html = self._render(summary, [account])

        self.assertIn("--strong-pct: 100%", html)
        self.assertIn("--weak-pct: 0%", html)
        self.assertIn("已发送 8/8 · 强确认 8", html)

    def test_half_up_rounding_matches_the_live_refresh(self):
        # 1/8 = 12.5% must render 13%, the same way app.js Math.round does,
        # otherwise the server-rendered ring and the polled refresh disagree.
        summary = _summary(total_targets=8, today_confirmed_targets=1)
        account = _account_row(
            total_targets=8,
            confirmed_targets=[{"status": "sent"}],
        )

        html = self._render(summary, [account])

        self.assertIn("--strong-pct: 13%", html)
        self.assertIn("--weak-pct: 0%", html)
        self.assertIn('data-overview-value="progress">1/8<', html)

    def test_weak_segment_absorbs_the_rounding_remainder(self):
        # 1/8 and 7/8 both land on .5; rounding each on its own used to print
        # 13% + 88% = 101% in the badge.
        summary = _summary(
            total_targets=8,
            today_confirmed_targets=1,
            today_page_echo_targets=7,
        )
        account = _account_row(
            total_targets=8,
            confirmed_targets=[{"status": "sent"}],
            page_echo_count=7,
            page_echo_targets=[{"status": "sent_page_echo"}] * 7,
        )

        html = self._render(summary, [account])

        self.assertIn("--strong-pct: 13%", html)
        self.assertIn("--weak-pct: 87%", html)
        self.assertIn('data-overview-value="progressPercent">100%<', html)
        self.assertIn('data-overview-value="progress">8/8<', html)

    def test_segment_widths_never_exceed_one_hundred_percent(self):
        width_pattern = re.compile(r"--strong-pct: (\d+)%; --weak-pct: (\d+)%")
        badge_pattern = re.compile(r'data-overview-value="progressPercent">(\d+)%<')

        for total in range(1, 21):
            for strong in range(total + 1):
                weak = total - strong
                summary = _summary(
                    total_targets=total,
                    today_confirmed_targets=strong,
                    today_page_echo_targets=weak,
                )
                account = _account_row(
                    total_targets=total,
                    confirmed_targets=[{"status": "sent"}] * strong,
                    page_echo_count=weak,
                )

                html = self._render(summary, [account])
                strong_pct, weak_pct = (
                    int(value) for value in width_pattern.search(html).groups()
                )
                badge_pct = int(badge_pattern.search(html).group(1))

                with self.subTest(total=total, strong=strong):
                    self.assertLessEqual(strong_pct + weak_pct, 100)
                    self.assertEqual(strong_pct + weak_pct, badge_pct)

    def test_ops_panel_drops_the_dash_and_warning_glyph(self):
        summary = _summary(total_targets=1, today_confirmed_targets=1)
        account = _account_row(
            total_targets=1,
            confirmed_targets=[{"status": "sent"}],
        )

        html = self._render(
            summary,
            [account],
            schedule_alignment={
                "windowEnabled": True,
                "configLabel": "10:00-18:00",
                "fixedLabel": "",
                "detail": "cron 与配置一致",
                "aligned": False,
                "missingTriggers": True,
                "lines": ["*/20 10-17 * * *"],
            },
        )

        self.assertNotIn("⚠", html)
        self.assertNotIn("——", html)
        self.assertIn("alignment-row", html)
        self.assertIn("调度核对", html)
        self.assertIn("notice-inline warning", html)
        self.assertIn("triangle-alert", html)
        self.assertIn("需要核对", html)
        self.assertIn("请确认定时任务是否真的在执行", html)

    def test_window_notice_uses_the_icon_notice(self):
        app_module.templates.env.globals["schedule_window_state"] = lambda: {
            "inside": True,
            "label": "10:00-18:00",
            "endHour": 18,
        }
        summary = _summary(total_targets=1, today_confirmed_targets=1)
        account = _account_row(
            total_targets=1,
            confirmed_targets=[{"status": "sent"}],
        )

        html = self._render(summary, [account])

        self.assertNotIn("⚠", html)
        self.assertIn("现在是发送窗口", html)
        self.assertIn("窗口配置在此期间不可修改", html)


    def test_static_assets_no_longer_ship_the_warning_glyph(self):
        root = Path(app_module.TEMPLATES_DIR).parents[0]
        for name in ("templates/dashboard.html", "static/app.js", "static/app.css"):
            with self.subTest(asset=name):
                text = (root / name).read_text(encoding="utf-8")
                self.assertNotIn("⚠", text)


if __name__ == "__main__":
    unittest.main()
