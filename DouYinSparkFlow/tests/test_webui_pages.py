import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from webui import app as app_module


ADMIN = {"username": "admin", "role": "admin", "account_refs": [], "session_id": "s-admin"}
FRIEND = {"username": "zxb", "role": "user", "account_refs": ["ref-1"], "session_id": "s-user"}

ACCOUNTS = [
    {
        "unique_id": "1001",
        "account_ref": "ref-1",
        "username": "主号",
        "enabled": True,
        "targets": ["阿杰"],
        "friends_cache": ["阿杰", "晓晓"],
    },
    {
        "unique_id": "1002",
        "account_ref": "ref-2",
        "username": "小号",
        "enabled": False,
        "targets": [],
        "friends_cache": [],
    },
]


def _snapshot():
    summary = {
        "total_targets": 4,
        "enabled_accounts": 1,
        "today_confirmed_targets": 1,
        "today_page_echo_targets": 1,
        "today_receipt_only_targets": 0,
        "today_unconfirmed_targets": 1,
        "today_failed_targets": 0,
        "today_account_blocked_targets": 0,
        "today_attention_targets": 1,
        "today_pending_targets": 1,
        "today_unprocessed_targets": 0,
        "last_confirmed_display": "10-03 14:02",
    }
    row = {
        "unique_id": "1001",
        "username": "主号",
        "state": "attention",
        "total_targets": 4,
        "confirmed_targets": [{"target": "阿杰", "confirmationLabel": "服务端回执", "message": "hi"}],
        "page_echo_targets": [],
        "page_echo_count": 1,
        "receipt_only_targets": [],
        "receipt_only_count": 0,
        "unconfirmed_targets": [{"target": "晓晓", "categoryLabel": "待核验", "attemptCount": 1}],
        "failed_targets": [],
        "pending_targets": [],
        "unprocessed_targets": [],
        "account_blocked_targets": [],
        "attention_count": 1,
        "pending_count": 1,
        "warnings": [],
        "account_failure": {},
        "friend_index_meta": {},
        "friend_index_count": 0,
    }
    return {
        "send_console": {"summary": summary, "accounts": [row], "nowDisplay": "10-03 14:20"},
        "task_lock": {"running": False, "stale": False, "ageSeconds": 0},
        "schedule": {"label": "10:00-18:00/20m", "nextTriggerDisplay": "10-03 14:40"},
        "daily_schedule": "10:00-18:00/20m",
        "containers": [],
    }


def _visible(principal, accounts=None):
    del accounts
    if principal and principal.get("role") == "admin":
        return [dict(item) for item in ACCOUNTS]
    refs = set((principal or {}).get("account_refs", []))
    return [dict(item) for item in ACCOUNTS if item["account_ref"] in refs]


class RedesignedPagesTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_module.app, raise_server_exceptions=True)
        patches = [
            # Never touch the real account store from these rendering tests.
            patch.object(app_module, "get_userData", return_value=[dict(item) for item in ACCOUNTS]),
            patch.object(app_module, "get_visible_accounts", side_effect=_visible),
            patch.object(app_module, "get_ops_snapshot", side_effect=lambda **_: _snapshot()),
            patch.object(app_module, "get_config", return_value={
                "messageTemplate": "续火花",
                "sendStrategy": {"messageVariants": [], "shuffleTargets": True,
                                 "messageIntervalSecondsMin": 45, "messageIntervalSecondsMax": 150},
            }),
            patch.object(app_module, "get_web_users", return_value=[]),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def _as(self, principal):
        user = patch.object(app_module, "current_user", return_value=principal["username"])
        who = patch.object(app_module, "current_principal", return_value=principal)
        user.start()
        who.start()
        self.addCleanup(user.stop)
        self.addCleanup(who.stop)

    def _get(self, path):
        return self.client.get(path, follow_redirects=False)

    def test_every_page_renders_for_an_admin(self):
        self._as(ADMIN)
        for path, marker in (
            ("/", "今天还差 2 个火花"),
            ("/ops/send-console", "账号明细"),
            ("/accounts", "friend-picker"),
            ("/login-workspace", 'id="login-desktop-controls"'),
            ("/settings", 'id="users"'),
            ("/ops/logs", 'data-page="logs"'),
        ):
            with self.subTest(path=path):
                response = self._get(path)
                self.assertEqual(200, response.status_code)
                self.assertEqual("no-store", response.headers["cache-control"])
                self.assertIn(marker, response.text)
                self.assertIn('href="/ops/logs"', response.text)

    def test_a_normal_user_sees_only_their_own_pages(self):
        self._as(FRIEND)
        for path in ("/", "/ops/send-console", "/accounts", "/login-workspace", "/settings"):
            with self.subTest(path=path):
                response = self._get(path)
                self.assertEqual(200, response.status_code)
                self.assertNotIn('href="/ops/logs"', response.text)
        self.assertEqual(403, self._get("/ops/logs").status_code)

        settings_page = self._get("/settings").text
        self.assertIn('action="/account/password"', settings_page)
        for admin_only in ('action="/config"', 'action="/ops/schedule"', 'action="/settings"', "/admin/users/create"):
            self.assertNotIn(admin_only, settings_page)

        accounts_page = self._get("/accounts").text
        self.assertIn("主号", accounts_page)
        self.assertNotIn("小号", accounts_page)
        # Owners may delete their own account; another user's account is not
        # rendered at all, so its delete form cannot appear either.
        self.assertIn("/accounts/1001/delete", accounts_page)
        self.assertNotIn("/accounts/1002/delete", accounts_page)

    def test_anonymous_visitors_are_sent_to_login(self):
        for path in ("/", "/accounts", "/login-workspace", "/settings"):
            with self.subTest(path=path):
                response = self._get(path)
                self.assertEqual(303, response.status_code)
                self.assertEqual("/login", response.headers["location"])

    def test_relogin_preselects_only_a_visible_account(self):
        self._as(FRIEND)
        page = self._get("/login-workspace?relogin=1001").text
        self.assertIn('class="button button-primary login-desktop-open" type="button" data-relogin-unique-id="1001"', page)
        self.assertIn('class="button button-soft login-desktop-save" type="button" data-relogin-unique-id="1001"', page)

        hidden = self._get("/login-workspace?relogin=1002").text
        self.assertIn('data-login-mode="add"', hidden)
        self.assertNotIn('data-relogin-unique-id="1002"', hidden)

    def test_accounts_page_links_relogin_to_the_login_page(self):
        self._as(ADMIN)
        page = self._get("/accounts").text
        self.assertIn('href="/login-workspace?relogin=1001"', page)
        # Only the login page hosts the workspace controls.
        self.assertNotIn("login-desktop-controls", page)
        self.assertNotIn("login-desktop-controls", self._get("/").text)

    def test_old_single_page_anchors_redirect_to_the_new_pages(self):
        base = (Path(app_module.TEMPLATES_DIR) / "base.html").read_text(encoding="utf-8")
        for anchor, target in (
            ("#account-management", "/accounts"),
            ("#interactive-login-section", "/login-workspace"),
            ("#config-panel", "/settings#messages"),
            ("#ops-panel", "/settings#ops"),
            ("#settings-panel", "/settings#system"),
            ("#user-management", "/settings#users"),
            ("#password-panel", "/settings#password"),
        ):
            with self.subTest(anchor=anchor):
                self.assertIn(f'"{anchor}": "{target}"', base)

    def test_form_posts_return_to_the_page_they_came_from(self):
        self._as(ADMIN)
        with (
            patch.object(app_module, "validate_csrf", return_value=True),
            patch.object(app_module, "update_config"),
            patch.object(app_module, "refresh_proxy"),
            patch.object(app_module, "create_web_user"),
        ):
            self.assertEqual("/settings#messages", self.client.post("/config", data={"csrf_token": "t"}, follow_redirects=False).headers["location"])
            self.assertEqual("/settings#ops", self.client.post("/ops/proxy/refresh", data={"csrf_token": "t"}, follow_redirects=False).headers["location"])
            self.assertEqual("/settings#users", self.client.post("/admin/users/create", data={"csrf_token": "t", "username": "a", "password": "b"}, follow_redirects=False).headers["location"])

    def test_theme_follows_the_system_until_the_user_picks_one(self):
        base = (Path(app_module.TEMPLATES_DIR) / "base.html").read_text(encoding="utf-8")
        login = (Path(app_module.TEMPLATES_DIR) / "login.html").read_text(encoding="utf-8")
        for page in (base, login):
            head = page[: page.index("</head>")]
            self.assertIn('localStorage.getItem("sparkflow-theme")', head)
            self.assertIn("prefers-color-scheme: dark", head)
            self.assertNotIn('data-theme="dark"', page[: page.index("<head>")])
        script = (Path(app_module.STATIC_DIR) / "app.js").read_text(encoding="utf-8")
        self.assertIn('localStorage.setItem(storageKey, next)', script)

    def test_unused_stylesheet_is_gone(self):
        self.assertFalse((Path(app_module.STATIC_DIR) / "styles.css").exists())

    def test_resend_all_stays_disabled_with_nothing_to_send(self):
        # The overview poll re-enables plain "disable while running" buttons; tie this one to the total.
        for name in ("dashboard.html", "settings.html"):
            page = (Path(app_module.TEMPLATES_DIR) / name).read_text(encoding="utf-8")
            form = page[page.index('action="/ops/run-now"'):]
            button = form[form.index("<button"): form.index(">", form.index("<button"))]
            with self.subTest(page=name):
                self.assertIn('data-action-count-source="total"', button)

    def test_wide_screens_center_the_page(self):
        css = (Path(app_module.STATIC_DIR) / "app.css").read_text(encoding="utf-8")
        rule = css[css.index(".page {"):]
        rule = rule[: rule.index("}")]
        self.assertIn("max-width: 1520px", rule)
        self.assertIn("margin-inline: auto", rule)

    def test_timeline_marks_the_current_hour(self):
        # nowDisplay is "%m-%d %H:%M" in ops.py; the hour must still be found.
        self._as(ADMIN)
        with patch.dict(app_module.templates.env.globals,
                        {"schedule_window_state": lambda: {"enabled": True, "startHour": 10, "endHour": 18}}):
            page = self._get("/").text
        self.assertIn('<span class="now" title="14:00">', page)
        self.assertIn('<span class="done" title="13:00">', page)
        self.assertIn('<span class="" title="15:00">', page)

    def test_focus_ring_is_not_hidden_by_component_shadows(self):
        css = (Path(app_module.STATIC_DIR) / "app.css").read_text(encoding="utf-8")
        rule = css[css.index(":focus-visible {"):]
        rule = rule[: rule.index("}")]
        self.assertIn("outline: 2px solid var(--flame)", rule)
        self.assertNotIn("box-shadow", rule)
        self.assertIn(".fold > summary:focus-visible", css)

    def test_focused_controls_scroll_clear_of_the_phone_tab_bar(self):
        css = (Path(app_module.STATIC_DIR) / "app.css").read_text(encoding="utf-8")
        mobile = css[css.index("@media (max-width: 900px)"):]
        mobile = mobile[: mobile.index(".tabbar {")]
        self.assertIn("html { scroll-padding-top: 68px; scroll-padding-bottom: calc(92px + env(safe-area-inset-bottom)); }", mobile)
        # The section's own margin adds to the padding; together they keep the old 80px landing.
        self.assertIn(".settings-section { scroll-margin-top: 12px; }", mobile)
        narrow = css[css.index("@media (max-width: 1100px)"):]
        self.assertIn(".segment-button { min-height: 40px; }", narrow[: narrow.index("\n}") + 2])
        self.assertIn("html { scroll-padding: 8px; }", css)

    def test_focus_scrolls_strip_items_and_textareas_fully_into_view(self):
        script = (Path(app_module.STATIC_DIR) / "app.js").read_text(encoding="utf-8")
        handler = script[script.index('document.addEventListener("focusin"'):]
        handler = handler[: handler.index("\n  });")]
        self.assertIn('.settings-nav, .segmented-control', handler)
        self.assertIn('matches("textarea")', handler)
        self.assertIn('scrollIntoView({ block: "nearest", inline: "nearest" })', handler)
        # Only keyboard focus scrolls; clicks, taps and window refocus leave the page alone.
        self.assertIn("if (!focusFromTab", handler)
        self.assertIn('focusFromTab = event.key === "Tab"', script)
        self.assertIn('addEventListener("pointerdown", () => { focusFromTab = false; }', script)

    def test_global_scroll_padding_comes_before_the_phone_override(self):
        css = (Path(app_module.STATIC_DIR) / "app.css").read_text(encoding="utf-8")
        self.assertLess(css.index("html { scroll-padding: 8px; }"), css.index("@media (max-width: 900px)"))
        self.assertIn(".segmented-control, .settings-nav { scroll-padding-inline: 8px; }", css)
        # 8px padding + 16px margin keeps desktop anchors 24px from the top.
        self.assertIn(".settings-section { scroll-margin-top: 16px; }", css)


if __name__ == "__main__":
    unittest.main()
