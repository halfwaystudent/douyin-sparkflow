import unittest
from unittest.mock import patch

from core import browser
from utils import config as config_module


class DouyinNetworkRouteTests(unittest.TestCase):
    def test_default_route_is_direct(self):
        with patch.object(browser, "get_app_settings", return_value={}):
            with patch.dict(browser.os.environ, {}, clear=False):
                browser.os.environ.pop("SPARKFLOW_DOUYIN_NETWORK_MODE", None)
                browser.os.environ.pop("SPARKFLOW_DOUYIN_PROXY_URL", None)
                options = browser._browser_launch_options(False)
        self.assertNotIn("proxy", options)
        self.assertIn("--no-proxy-server", options["args"])

    def test_mihomo_route_is_explicit(self):
        with patch.object(browser, "get_app_settings", return_value={}):
            with patch.dict(
                browser.os.environ,
                {
                    "SPARKFLOW_DOUYIN_NETWORK_MODE": "mihomo",
                    "SPARKFLOW_DOUYIN_PROXY_URL": "http://127.0.0.1:7890",
                },
                clear=False,
            ):
                options = browser._browser_launch_options(False)
        self.assertEqual({"server": "http://127.0.0.1:7890"}, options["proxy"])
        self.assertNotIn("--no-proxy-server", options["args"])

    def test_default_app_settings_keep_direct_route(self):
        self.assertEqual("direct", config_module.DEFAULT_APP_SETTINGS["douyin_network_mode"])
        self.assertEqual("http://proxy:7890", config_module.DEFAULT_APP_SETTINGS["douyin_proxy_url"])

    def test_default_ops_log_file_is_the_mounted_path(self):
        self.assertEqual(
            "/app/logs/douyin-sparkflow.log",
            config_module.DEFAULT_APP_SETTINGS["ops_log_file"],
        )

    def test_legacy_ops_log_file_is_migrated_to_the_mounted_path(self):
        settings = {"ops_log_file": config_module.LEGACY_OPS_LOG_FILE}

        config_module._migrate_legacy_app_settings(settings)

        self.assertEqual(
            config_module.DEFAULT_APP_SETTINGS["ops_log_file"],
            settings["ops_log_file"],
        )

    def test_custom_ops_log_file_is_preserved(self):
        settings = {"ops_log_file": "/data/custom-ops.log"}

        config_module._migrate_legacy_app_settings(settings)

        self.assertEqual("/data/custom-ops.log", settings["ops_log_file"])


if __name__ == "__main__":
    unittest.main()
