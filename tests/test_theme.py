"""Light, dark and Windows-following appearance."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from winlauncher.settings import Appearance, Settings, settings_from_json
from winlauncher.ui import theme as theme_module
from winlauncher.ui.settings import SettingsDialog
from winlauncher.ui.theme import DARK, LIGHT, colors, stylesheet, theme
from winlauncher.ui.website_icons import WebsiteIcons


class ThemeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.addCleanup(theme().use, Appearance.LIGHT)

    def test_choosing_dark_restyles_the_application_and_tells_painters(self):
        changes = []
        theme().changed.connect(lambda: changes.append(colors()))
        self.addCleanup(theme().changed.disconnect)
        theme().use(Appearance.LIGHT)
        changes.clear()

        theme().use(Appearance.DARK)

        self.assertIs(colors(), DARK)
        self.assertEqual(changes, [DARK])
        self.assertIn(DARK.surface, self.app.styleSheet())
        theme().use(Appearance.DARK)
        self.assertEqual(changes, [DARK])  # The same colors change nothing.

    def test_matching_windows_follows_the_reported_color_scheme(self):
        with patch.object(self.app.styleHints(), "colorScheme", return_value=Qt.ColorScheme.Dark):
            theme().use(Appearance.SYSTEM)
            self.assertIs(colors(), DARK)

    def test_both_themes_fill_every_stylesheet_color(self):
        for theme_colors in (LIGHT, DARK):
            with self.subTest(theme=theme_colors.menu_check):
                sheet = stylesheet(theme_colors)
                self.assertNotIn("$", sheet)
                self.assertIn((theme_module.ASSETS / theme_colors.combo_arrow).as_posix(), sheet)

    def test_settings_store_the_appearance_the_dialog_offers(self):
        self.assertEqual(Settings().appearance, Appearance.SYSTEM)
        self.assertEqual(settings_from_json({"appearance": "dark"}).appearance, "dark")
        with self.assertRaisesRegex(ValueError, "Appearance"):
            settings_from_json({"appearance": "sepia"})
        dialog = SettingsDialog(
            Settings(roots=(), appearance="light"),
            startup_enabled=False,
            set_startup=None,
            website_icons=WebsiteIcons(),
        )
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.appearance.currentData(), "light")
        dialog.appearance.setCurrentIndex(dialog.appearance.findData("dark"))
        self.assertEqual(dialog.read_settings().appearance, "dark")


if __name__ == "__main__":
    unittest.main()
