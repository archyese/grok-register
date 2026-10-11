"""browser_window_size 配置与 --window-size 启动参数的回归测试。"""

import unittest

import app_config
import browser_runtime


class BrowserWindowSizeConfigTests(unittest.TestCase):
    def test_default_empty_is_valid(self):
        cfg = app_config.validate_config_structure(dict(app_config.DEFAULT_CONFIG))
        self.assertEqual(cfg["browser_window_size"], "")

    def test_valid_sizes_are_accepted(self):
        for value in ("1280,860", "1280x860", "1440 , 900"):
            cfg = app_config.validate_config_structure(
                {**app_config.DEFAULT_CONFIG, "browser_window_size": value}
            )
            self.assertTrue(cfg["browser_window_size"])

    def test_invalid_format_is_rejected(self):
        for value in ("1280", "abc,860", "1280,860,10", "-5,300", "12,34"):
            with self.assertRaises(app_config.ConfigError):
                app_config.validate_config_structure(
                    {**app_config.DEFAULT_CONFIG, "browser_window_size": value}
                )


class BrowserWindowSizeArgumentTests(unittest.TestCase):
    def tearDown(self):
        browser_runtime.configure_runtime({})

    def test_argument_added_when_configured(self):
        browser_runtime.configure_runtime({"browser_window_size": "1280x860"})
        options = browser_runtime.create_browser_options()
        self.assertIn("--window-size=1280,860", options.arguments)

    def test_comma_format_is_normalized(self):
        browser_runtime.configure_runtime({"browser_window_size": "1440, 900"})
        options = browser_runtime.create_browser_options()
        self.assertIn("--window-size=1440,900", options.arguments)

    def test_no_argument_when_unset(self):
        browser_runtime.configure_runtime({})
        options = browser_runtime.create_browser_options()
        self.assertFalse(
            any(str(arg).startswith("--window-size") for arg in options.arguments)
        )

    def test_garbage_value_is_ignored_at_runtime(self):
        # app_config 在加载时会拦截非法值；这里兜底验证 runtime 不会注入坏参数。
        browser_runtime.configure_runtime({"browser_window_size": "not-a-size"})
        options = browser_runtime.create_browser_options()
        self.assertFalse(
            any(str(arg).startswith("--window-size") for arg in options.arguments)
        )


if __name__ == "__main__":
    unittest.main()
