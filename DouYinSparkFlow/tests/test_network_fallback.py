import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "DouYinSparkFlow"

class NetworkFallbackContractTests(unittest.TestCase):
    def test_browser_exposes_direct_first_routes_and_preflight(self):
        browser = (SOURCE_ROOT / "core" / "browser.py").read_text(encoding="utf-8")
        self.assertIn("def douyin_network_modes", browser)
        self.assertIn("return (\"direct\", \"mihomo\")", browser)
        self.assertIn("async def select_douyin_network_mode", browser)
        self.assertIn("network_mode=network_mode, profile_name=profile_name", browser)

    def test_friend_refresh_can_try_the_next_route(self):
        friends = (SOURCE_ROOT / "core" / "friends.py").read_text(encoding="utf-8")
        self.assertIn("for index, network_mode in enumerate(modes)", friends)
        self.assertIn("returned zero friends; trying next route", friends)
        self.assertIn("profile_name=account_profile_name(account)", friends)

    def test_tasks_select_route_before_browser_creation(self):
        tasks = (SOURCE_ROOT / "core" / "tasks.py").read_text(encoding="utf-8")
        self.assertIn("select_douyin_network_mode(", tasks)
        self.assertIn("profile_name=_account_profile_name(browser_user_data[0])", tasks)
        self.assertIn("profile_name=_account_profile_name(user)", tasks)
        self.assertIn("network_mode=network_mode", tasks)

    def test_login_defaults_to_auto_in_compose(self):
        compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("LOGIN_DESKTOP_PROXY_MODE: ${LOGIN_DESKTOP_PROXY_MODE:-auto}", compose)

if __name__ == "__main__":
    unittest.main()
