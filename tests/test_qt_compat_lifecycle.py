"""Test compatibility hook wiring without native Qt or Picard resources."""

import threading
import unittest
from unittest.mock import Mock, patch

from test_matching import load_plugin


plugin = load_plugin()


class UnprintableError(Exception):
    def __str__(self):
        raise AssertionError("Lifecycle logging must not format exception values")


class PluginConfig(dict):
    def register_option(self, key, default):
        self.setdefault(key, default)


class QtCompatLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock(plugin_config=PluginConfig())
        self.http = Mock()
        self.store = Mock()
        self.dispatcher = Mock()
        replacements = {
            "CURRENT_PLUGIN_API": None,
            "_ENABLED": False,
            "_DISABLED": False,
            "_SHUTDOWN_EVENT": Mock(),
            "_MAIN_INVOKER": None,
            "_PROVIDER_HTTP": None,
            "_LYRICS_STORE": None,
            "_HTTP_TEXT_CACHE": Mock(),
            "_JSON_CACHE": Mock(),
            "_RATE_LIMITER": Mock(),
            "_init_caches": Mock(),
            "_get_ssl_context": Mock(),
            "_lyrics_store_path": Mock(return_value=":memory:"),
            "HttpClient": Mock(return_value=self.http),
            "LyricsStore": Mock(return_value=self.store),
            "MainThreadDispatcher": Mock(return_value=self.dispatcher),
            "FETCH_QUEUE": Mock(),
            "install_menu_lifetime_workaround": Mock(return_value=True),
            "uninstall_menu_lifetime_workaround": Mock(),
        }
        for name, value in replacements.items():
            patcher = patch.object(plugin, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def assert_enabled(self):
        self.assertIs(plugin.CURRENT_PLUGIN_API, self.api)
        self.assertEqual(dict(self.api.plugin_config), plugin.PLUGIN_OPTIONS)
        self.api.register_options_page.assert_called_once_with(plugin.BetterLyricsOptionsPage)
        self.api.register_album_action.assert_any_call(plugin.BetterLyricsSearchAction)
        self.api.register_album_action.assert_any_call(plugin.BetterLyricsGetAction)
        plugin.FETCH_QUEUE.start.assert_called_once_with()

    def assert_disabled(self):
        self.assertIsNone(plugin.CURRENT_PLUGIN_API)
        self.http.close.assert_called_once_with()
        self.store.close.assert_called_once_with()
        self.dispatcher.stop.assert_called_once_with()
        plugin.FETCH_QUEUE.stop.assert_called_once_with()
        plugin.FETCH_QUEUE.set_invoker.assert_called_with(None)

    def test_enable_installs_on_calling_gui_thread_before_registering_actions(self):
        caller = threading.get_ident()

        def install(log=None):
            self.assertEqual(threading.get_ident(), caller)
            self.assertIs(log._logger, self.api.logger)
            self.api.register_album_action.assert_not_called()
            plugin.FETCH_QUEUE.start.assert_not_called()
            return True

        plugin.install_menu_lifetime_workaround.side_effect = install
        plugin.enable(self.api)
        plugin.install_menu_lifetime_workaround.assert_called_once_with(log=plugin.log)
        plugin.uninstall_menu_lifetime_workaround.assert_not_called()
        self.api.logger.warning.assert_not_called()
        self.assert_enabled()

        plugin.disable()
        plugin.uninstall_menu_lifetime_workaround.assert_called_once_with()
        self.assert_disabled()

    def test_unavailable_protection_does_not_prevent_enable(self):
        plugin.install_menu_lifetime_workaround.return_value = False
        plugin.enable(self.api)
        plugin.install_menu_lifetime_workaround.assert_called_once_with(log=plugin.log)
        self.assert_enabled()
        self.api.logger.warning.assert_called_once()
        self.assertIn("protection is not active", self.api.logger.warning.call_args.args[0])
        plugin.disable()
        plugin.uninstall_menu_lifetime_workaround.assert_called_once_with()
        self.assert_disabled()

    def test_install_failure_logs_only_type_and_continues_enable(self):
        plugin.install_menu_lifetime_workaround.side_effect = UnprintableError()
        plugin.enable(self.api)
        plugin.install_menu_lifetime_workaround.assert_called_once_with(log=plugin.log)
        self.assert_enabled()
        self.api.logger.warning.assert_called_once()
        message, error_type = self.api.logger.warning.call_args.args
        self.assertIn("protection is not active", message)
        self.assertEqual(error_type, "UnprintableError")
        plugin.disable()
        plugin.uninstall_menu_lifetime_workaround.assert_called_once_with()
        self.assert_disabled()

    def test_uninstall_failure_logs_only_type_and_continues_cleanup(self):
        plugin.enable(self.api)
        plugin.uninstall_menu_lifetime_workaround.side_effect = UnprintableError()
        plugin.disable()
        plugin.uninstall_menu_lifetime_workaround.assert_called_once_with()
        self.assert_disabled()
        self.api.logger.warning.assert_called_once()
        message, error_type = self.api.logger.warning.call_args.args
        self.assertIn("cleanup failed", message)
        self.assertEqual(error_type, "UnprintableError")

    def test_reenable_calls_installer_again(self):
        for _ in range(2):
            plugin.enable(self.api)
            plugin.disable()
        self.assertEqual(plugin.install_menu_lifetime_workaround.call_count, 2)
        self.assertEqual(plugin.uninstall_menu_lifetime_workaround.call_count, 2)

    def test_enable_does_not_swallow_base_exception(self):
        plugin.install_menu_lifetime_workaround.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            plugin.enable(self.api)
        plugin.install_menu_lifetime_workaround.assert_called_once_with(log=plugin.log)
        self.api.logger.warning.assert_not_called()
        plugin.FETCH_QUEUE.start.assert_not_called()

    def test_disable_does_not_swallow_base_exception(self):
        plugin.enable(self.api)
        plugin.uninstall_menu_lifetime_workaround.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            plugin.disable()
        plugin.uninstall_menu_lifetime_workaround.assert_called_once_with()
        self.api.logger.warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
