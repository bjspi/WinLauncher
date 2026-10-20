"""Website settings, URL substitution and asynchronous favicon fallbacks."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImage
from PySide6.QtNetwork import QNetworkReply
from PySide6.QtWidgets import QApplication

from pathscout.settings import Settings, SettingsFile, settings_from_json
from pathscout.ui.settings import SettingsDialog
from pathscout.ui.website_icons import WebsiteIcons
from pathscout.websites import DEFAULT_WEBSITES, WebsiteShortcut, website_query

AMAZON = WebsiteShortcut("az", "Amazon", "https://www.amazon.de/s?k=%s&language=de_DE")
YOUTUBE = WebsiteShortcut("yt", "YouTube", "https://www.youtube.com/results?search_query=%s")


class WebsiteTest(unittest.TestCase):
    def test_templates_preserve_search_text_and_encode_it_as_one_value(self):
        query = website_query('AZ C++ Bücher & "Tools" #1', (AMAZON,))
        self.assertEqual(query.search_text, 'C++ Bücher & "Tools" #1')
        self.assertEqual(
            query.shortcut.target(query.search_text).url,
            "https://www.amazon.de/s?k=C%2B%2B%20B%C3%BCcher%20%26%20%22Tools%22%20%231&language=de_DE",
        )
        self.assertEqual(AMAZON.target().url, "https://www.amazon.de/")
        self.assertEqual(
            YOUTUBE.target("XYZ").url, "https://www.youtube.com/results?search_query=XYZ"
        )
        page = WebsiteShortcut("docs", "Docs", "https://example.org/docs#install")
        self.assertEqual(page.target().url, page.url)
        self.assertIsNone(website_query("docs file", (page,)))
        self.assertIsNone(website_query("azure", (AMAZON,)))

    def test_settings_roundtrip_and_reject_invalid_shortcuts(self):
        settings = Settings(roots=(), websites=(AMAZON, YOUTUBE))
        with tempfile.TemporaryDirectory() as directory:
            file = SettingsFile(Path(directory) / "settings.json")
            file.save(settings)
            self.assertEqual(file.load(), settings)
        self.assertEqual(settings.indexing_key, Settings(roots=()).indexing_key)
        self.assertEqual(settings_from_json({"roots": []}).websites, DEFAULT_WEBSITES)
        self.assertEqual(settings_from_json({"roots": [], "websites": []}).websites, ())
        self.assertEqual(
            [site.keyword for site in DEFAULT_WEBSITES], ["g", "az", "yt", "gm", "eb", "ka", "imdb"]
        )
        maps = DEFAULT_WEBSITES[3]
        self.assertEqual(maps.target().url, "https://maps.google.com/")
        self.assertEqual(maps.target("Berlin Hbf").url, "https://maps.google.com/?q=Berlin%20Hbf")
        for url in (
            "javascript:alert(1)",
            "file:///C:/Windows",
            "https://",
            "https://user:pw@example.org/",
            "https://example.org:bad/",
            "https://example.org/a b",
            "https://%s.example.org/",
            "https://example.org\\evil/",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                WebsiteShortcut("x", "Title", url)
        for value in (
            None,
            ["wrong"],
            [{"keyword": "x"}],
            [{"keyword": [], "title": "X", "url": "https://example.org"}],
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                settings_from_json({"websites": value})
        with self.assertRaisesRegex(ValueError, "'AZ' is used twice"):
            Settings(roots=(), websites=(AMAZON, WebsiteShortcut("AZ", "Other", YOUTUBE.url)))


class WebsiteUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.icons = WebsiteIcons()
        self.addCleanup(self.icons.close)

    def test_settings_edit_add_remove_save_and_cancel(self):
        original = Settings(roots=(), websites=(AMAZON,))
        saved = []
        dialog = SettingsDialog(original, save=saved.append, website_icons=self.icons)
        editor = dialog.websites
        editor.title.setText("Amazon DE")
        editor.add_website()
        editor.keyword.setText(YOUTUBE.keyword)
        editor.title.setText(YOUTUBE.title)
        editor.url.setText(YOUTUBE.url)
        self.assertEqual(dialog.read_settings().websites[1], YOUTUBE)
        dialog.reject()
        self.assertEqual(saved, [])
        self.assertEqual(dialog.result_settings, original)
        dialog.deleteLater()
        dialog = SettingsDialog(original, save=saved.append, website_icons=self.icons)
        dialog.websites.title.setText("Amazon DE")
        dialog.save()
        self.assertEqual(saved[0].websites[0].title, "Amazon DE")
        dialog.deleteLater()
        dialog = SettingsDialog(original, website_icons=self.icons)
        dialog.websites.remove_selected()
        self.assertEqual(dialog.read_settings().websites, ())
        self.assertFalse(dialog.websites.remove.isEnabled())
        dialog.websites.add_website()
        with patch("pathscout.ui.settings.QMessageBox.warning") as warning:
            dialog.save()
        warning.assert_called_once()
        self.assertEqual(dialog.result_settings, original)
        dialog.deleteLater()

    def test_favicon_requests_only_origin_and_caches_images_or_fallback(self):
        icons = WebsiteIcons()
        self.addCleanup(icons.close)
        image = QImage(16, 16, QImage.Format.Format_ARGB32)
        image.fill(0xFF2277BB)
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        for data, successful in ((buffer.data(), True), (QByteArray(b"not an image"), False)):
            with self.subTest(successful=successful):
                icons._cache.clear()
                reply = Mock()
                reply.error.return_value = QNetworkReply.NetworkError.NoError
                reply.readAll.return_value = data
                with patch.object(icons.network, "get", return_value=reply) as get:
                    icons.icon_for("https://example.org/private?q=secret")
                    icons.icon_for("https://example.org/other?q=more")
                    get.assert_called_once()
                    request = get.call_args.args[0]
                    self.assertEqual(request.url().toString(), "https://example.org/favicon.ico")
                    self.assertEqual(request.transferTimeout(), 10000)
                    icons._finished("https://example.org/", reply)
                    icon = icons.icon_for("https://example.org/")
                    self.assertEqual(icon.cacheKey() != icons.fallback.cacheKey(), successful)
                    if successful:
                        self.assertEqual(icon.availableSizes()[0].width(), 64)
                        self.assertEqual(icon.availableSizes()[0].height(), 64)
                    get.assert_called_once()
                reply.deleteLater.assert_called_once()

    def test_favicon_timeout_can_retry_without_restarting(self):
        icons = WebsiteIcons()
        self.addCleanup(icons.close)
        reply = Mock()
        reply.error.return_value = QNetworkReply.NetworkError.TimeoutError
        with (
            patch.object(icons.network, "get", return_value=reply) as get,
            patch("pathscout.ui.website_icons.QTimer.singleShot") as timer,
        ):
            icons.icon_for("https://example.org/")
            icons._finished("https://example.org/", reply)
            self.assertEqual(
                icons.icon_for("https://example.org/").cacheKey(), icons.fallback.cacheKey()
            )
            get.assert_called_once()
            timer.assert_called_once()
            timer.call_args.args[2]()
            icons.icon_for("https://example.org/")
            self.assertEqual(get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
