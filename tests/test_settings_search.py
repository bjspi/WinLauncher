"""Searching the Settings dialog: matching, the result list and opening a setting."""

from __future__ import annotations

import unittest
from unittest.mock import Mock

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from winlauncher.settings import Settings
from winlauncher.ui.settings import SettingsDialog
from winlauncher.ui.settings_search import NO_MATCH, SettingEntry, find
from winlauncher.ui.website_icons import WebsiteIcons


class FindTest(unittest.TestCase):
    def test_every_word_must_match_and_titles_come_first(self):
        entries = [
            SettingEntry(0, "General", "Close when unused", "Hide the bar after you switch away."),
            SettingEntry(0, "General", "Close after", "Time without focus before it closes."),
            SettingEntry(1, "Search folders", "Folders", "Drag folders here from Explorer."),
        ]
        self.assertEqual(
            [entry.title for entry in find("CLOSE after", entries)],
            ["Close after", "Close when unused"],
        )
        self.assertEqual([entry.title for entry in find("explorer", entries)], ["Folders"])
        on_page = find("search", entries)  # Search folders is its page.
        self.assertEqual([entry.title for entry in on_page], ["Folders"])
        self.assertEqual(find("  ", entries), [])
        self.assertEqual(find("close explorer", entries), [])


class SettingsSearchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.persist = Mock()
        self.dialog = SettingsDialog(Settings(), save=self.persist, website_icons=WebsiteIcons())
        self.addCleanup(self.dialog.deleteLater)
        self.dialog.show()
        self.search = self.dialog.search

    def type(self, text: str) -> list[str]:
        self.search.field.setText(text)
        return [self.search.results.item(row).text() for row in range(self.search.results.count())]

    def test_results_replace_the_pages_until_the_text_is_cleared(self):
        self.assertTrue(self.dialog.nav.isVisible())
        results = self.type("close after")
        self.assertEqual(results[0], "Close after\nGeneral")
        self.assertFalse(self.dialog.nav.isVisible())
        self.assertTrue(self.search.results.isVisible())
        self.assertEqual(len(self.dialog._built), self.dialog.nav.count())  # Searching builds all.
        QTest.keyClick(self.search.field, Qt.Key.Key_Escape)  # Clears the text, keeps the dialog.
        self.assertEqual(self.search.field.text(), "")
        self.assertTrue(self.dialog.isVisible())
        self.assertTrue(self.dialog.nav.isVisible())
        self.assertFalse(self.search.results.isVisible())

    def test_enter_opens_the_page_and_lights_up_the_row_without_saving(self):
        self.type("fuzzy")
        QTest.keyClick(self.search.field, Qt.Key.Key_Return)
        self.app.processEvents()
        self.assertEqual(self.dialog.nav.currentItem().text(), "Explorer & dialogs")
        rows = [
            entry.widget
            for entry in self.dialog._searchable()
            if entry.title == "Fuzzy matching for tools"
        ]
        self.assertTrue(rows[0].property("found"))
        self.persist.assert_not_called()

    def test_a_search_without_matches_says_so(self):
        self.assertEqual(self.type("no such setting anywhere"), [NO_MATCH])
        QTest.keyClick(self.search.field, Qt.Key.Key_Return)
        self.assertEqual(self.dialog.nav.currentRow(), 0)
        self.persist.assert_not_called()


if __name__ == "__main__":
    unittest.main()
