"""Search plugins: ranking, merging, refresh and cache, discovery, settings and the UI."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from winlauncher.catalog.model import CatalogItem, ItemKind
from winlauncher.plugins.api import Plugin, PluginError, PluginItem, PluginOption, SetupRequired
from winlauncher.plugins.config import PluginConfig
from winlauncher.plugins.host import CACHE_FILE, PluginHost
from winlauncher.plugins.registry import Discovery, discover_plugins
from winlauncher.plugins.search import (
    PluginHit,
    PluginQuery,
    merge_hits,
    plugin_query,
    plugins_answer,
    rank_items,
)
from winlauncher.search.query_parser import parse_query
from winlauncher.search.ranking import SearchHit
from winlauncher.settings import Settings, SettingsFile, settings_from_json
from winlauncher.websites import WebsiteShortcut

ANNA = PluginItem("u1", "Anna Schmidt", target="tg://resolve?domain=anna", keywords=("annas",))
BERND = PluginItem("u2", "Bernd Weber", target="tg://resolve?domain=bernd")
TEAM = PluginItem("g1", "Team Annahme", target="tg://privatepost?channel=5")


class FakePlugin(Plugin):
    id = "fake"
    label = "Fake"
    keyword = "fk"
    options = (PluginOption("count", "Count", 3, bounds=(1, 10)),)

    def __init__(self):
        self.items: list[PluginItem] | Exception = [ANNA, BERND, TEAM]
        self.opened: list[PluginItem] = []
        self.contexts = []

    def refresh(self, context):
        self.contexts.append(context)
        if isinstance(self.items, Exception):
            raise self.items
        return self.items

    def open(self, item, context):
        self.opened.append(item)


def query(text: str):
    return parse_query(text)


class PluginRankingTest(unittest.TestCase):
    def test_names_rank_like_file_names_and_keywords_a_little_lower(self):
        hits = rank_items("fake", "Fake", [ANNA, BERND, TEAM], query("anna"), limit=10)
        self.assertEqual([hit.item for hit in hits], [ANNA, TEAM])
        self.assertEqual(hits[0].reason, "Name prefix")
        keyword = rank_items("fake", "Fake", [ANNA], query("annas"), limit=10)[0]
        self.assertEqual(keyword.reason, "Keyword matches")
        self.assertEqual(hits[0].title, "Fake: Anna Schmidt")
        self.assertEqual(rank_items("fake", "Fake", [ANNA], query("anna -schmidt"), limit=5), [])

    def test_earlier_items_win_ties_and_no_terms_keep_the_plugins_order(self):
        first = PluginItem("a", "Max Mustermann")
        second = PluginItem("b", "Max Mustermann ")
        hits = rank_items("fake", "Fake", [second, first], query("max"), limit=10)
        self.assertEqual(hits[0].item, second)
        listed = rank_items("fake", "Fake", [BERND, ANNA, TEAM], query(""), limit=2)
        self.assertEqual([hit.item for hit in listed], [BERND, ANNA])
        plain = rank_items("fake", "Fake", [ANNA], query("anna"), limit=1)[0]
        boosted = rank_items("fake", "Fake", [ANNA], query("anna"), boost=30, limit=1)[0]
        self.assertEqual(boosted.score - plain.score, 30)

    def test_only_plain_word_searches_include_plugins(self):
        self.assertTrue(plugins_answer(query("anna")))
        self.assertTrue(plugins_answer(query('"anna s" -bernd')))
        for text in ("", "anna type:folder", "anna ext:pdf", "anna in:C:\\Temp", "path:x anna"):
            self.assertFalse(plugins_answer(query(text)), text)

    def test_keywords_select_one_plugin(self):
        keywords = {"fk": "fake"}
        found = plugin_query("FK anna", keywords)
        self.assertEqual(found, PluginQuery("fake", query("anna")))
        self.assertEqual(plugin_query("fk", keywords).search.terms, ())
        self.assertIsNone(plugin_query("fkx anna", keywords))
        self.assertIsNone(plugin_query("", keywords))
        self.assertEqual(plugin_query('fk "anna', keywords).search.terms, ('"anna',))

    def test_plugin_hits_join_file_hits_by_score(self):
        item = CatalogItem(Path("C:/a.txt"), ItemKind.FILE, 0, 0)
        files = [SearchHit(item, 150, "x"), SearchHit(item, 60, "y")]
        plugins = [
            PluginHit("fake", "Fake", ANNA, 100, "z"),
            PluginHit("fake", "Fake", BERND, 10, ""),
        ]
        merged = merge_hits(files, plugins, 3)
        self.assertEqual([hit.score for hit in merged], [150, 100, 60])


class PluginHostTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="winlauncher-plugins-")
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name)
        self.plugin = FakePlugin()
        self.host = PluginHost([self.plugin], self.data)
        self.config = PluginConfig("fake", enabled=True, keyword="fk", options={"count": 99})

    def test_refresh_keeps_items_in_a_cache_that_the_next_start_reads(self):
        state = self.host.refresh("fake", self.config)
        self.assertEqual(state.items, (ANNA, BERND, TEAM))
        context = self.plugin.contexts[0]
        self.assertEqual(context.options, {"count": 3})  # Out of bounds: the default.
        self.assertEqual(context.data_dir, self.data / "plugin-data" / "fake")
        restarted = PluginHost([FakePlugin()], self.data)
        restarted.load_cached()
        self.assertEqual(restarted.state("fake").items, (ANNA, BERND, TEAM))
        hits = restarted.search(query("bernd"), (self.config,), 10)
        self.assertEqual([hit.item for hit in hits], [BERND])

    def test_failures_keep_the_last_items_but_setup_problems_drop_them(self):
        self.host.refresh("fake", self.config)
        self.plugin.items = PluginError("Offline")
        state = self.host.refresh("fake", self.config)
        self.assertEqual((len(state.items), state.error), (3, "Offline"))
        self.plugin.items = RuntimeError("bug")
        with self.assertLogs("winlauncher.plugins.host", "ERROR"):
            self.assertEqual(self.host.refresh("fake", self.config).error, "RuntimeError: bug")
        self.plugin.items = SetupRequired("Sign in first.")
        state = self.host.refresh("fake", self.config)
        self.assertEqual((state.items, state.needs_setup), ((), True))
        self.assertFalse((self.data / "plugin-data" / "fake" / CACHE_FILE).exists())

    def test_disabled_plugins_and_file_queries_stay_silent(self):
        self.host.refresh("fake", self.config)
        off = PluginConfig("fake", enabled=False, keyword="fk")
        self.assertEqual(self.host.search(query("anna"), (off,), 10), [])
        self.assertEqual(self.host.search(PluginQuery("fake", query("")), (off,), 10), [])
        self.assertEqual(self.host.search(query("anna type:file"), (self.config,), 10), [])
        hidden = PluginConfig("fake", enabled=True, in_search=False)
        self.assertEqual(self.host.search(query("anna"), (hidden,), 10), [])
        self.assertEqual(len(self.host.search(PluginQuery("fake", query("")), (hidden,), 10)), 3)
        self.assertEqual(self.host.keywords((self.config,)), {"fk": "fake"})
        self.assertEqual(self.host.keywords(()), {})  # Plugins are off by default.

    def test_a_busy_plugin_is_not_refreshed_twice_and_duplicates_are_dropped(self):
        self.plugin.items = [ANNA, ANNA, BERND]
        self.assertEqual(len(self.host.refresh("fake", self.config).items), 2)
        entry = self.host._entries["fake"]
        with entry.lock:
            self.plugin.items = []
            self.assertEqual(len(self.host.refresh("fake", self.config).items), 2)
        self.plugin.items = ["not an item"]
        self.assertIn("not PluginItem", self.host.refresh("fake", self.config).error)

    def test_cancelled_refresh_keeps_the_items(self):
        self.host.refresh("fake", self.config)
        cancel = threading.Event()
        cancel.set()
        self.plugin.items = []
        self.assertEqual(len(self.host.refresh("fake", self.config, cancel).items), 3)


class PluginDiscoveryTest(unittest.TestCase):
    def test_plugin_files_load_and_broken_ones_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "notes.py").write_text(
                "from winlauncher.plugins.api import Plugin, PluginItem\n"
                "class Notes(Plugin):\n"
                "    id = 'notes'\n"
                "    label = 'Note'\n"
                "    def refresh(self, context):\n"
                "        return [PluginItem('1', 'Shopping')]\n",
                encoding="utf-8",
            )
            (folder / "broken.py").write_text("raise ValueError('oops')\n", encoding="utf-8")
            (folder / "empty.py").write_text("X = 1\n", encoding="utf-8")
            (folder / "clash.py").write_text(
                "from winlauncher.plugins.api import Plugin\n"
                "class Again(Plugin):\n    id = 'notes'\n"
                "class Bad(Plugin):\n    id = 'Bad Id'\n",
                encoding="utf-8",
            )
            with (
                patch("winlauncher.plugins.registry.entry_points", return_value=()),
                self.assertLogs("winlauncher.plugins.registry", "ERROR"),
            ):
                found = discover_plugins(folder)
        self.assertEqual([plugin.id for plugin in found.plugins], ["notes"])
        problems = " ".join(found.problems)
        for text in ("broken.py: ValueError: oops", "empty.py", "already uses the id notes", "Bad"):
            self.assertIn(text, problems)
        with (
            patch("winlauncher.plugins.registry.entry_points", return_value=()),
            self.assertLogs("winlauncher.plugins.registry", "ERROR"),
        ):
            missing = discover_plugins(None, built_in=("nowhere:Nothing",))
        self.assertEqual((missing.plugins, len(missing.problems)), ([], 1))


class PluginSettingsTest(unittest.TestCase):
    def test_plugin_configs_roundtrip_and_validate(self):
        config = PluginConfig("chats", enabled=True, keyword="tg", options={"bots": False})
        settings = Settings(roots=(), plugins=(config,))
        with tempfile.TemporaryDirectory() as directory:
            file = SettingsFile(Path(directory) / "settings.json")
            file.save(settings)
            self.assertEqual(file.load(), settings)
            stored = json.loads(file.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["plugins"][0]["options"], {"bots": False})
        self.assertEqual(settings_from_json({"roots": []}).plugins, ())
        hash(settings)
        invalid = (
            {"id": "x", "refresh_minutes": 0},
            {"id": "x", "keyword": "a b"},
            {"id": "x", "options": {"a": [1]}},
            {"id": "x", "enabled": 1},
            {"id": "x", "boost": 500},
        )
        for entry in invalid:
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                settings_from_json({"roots": [], "plugins": [entry]})
        with self.assertRaisesRegex(ValueError, "already used"):
            Settings(
                roots=(),
                websites=(WebsiteShortcut("tg", "Site", "https://example.org/?q=%s"),),
                plugins=(config,),
            )
        with self.assertRaisesRegex(ValueError, "twice"):
            Settings(roots=(), plugins=(config, config))
        # A disabled plugin's keyword does not claim the word.
        Settings(roots=(), plugins=(PluginConfig("a", keyword="g"),))


class PluginUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        from test_ui_flow import DeferredTasks

        from winlauncher.ui.controller import LauncherController

        self.temporary = tempfile.TemporaryDirectory(prefix="winlauncher-plugin-ui-")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name) / "library"
        root.mkdir()
        (root / "Anna notes.txt").write_text("x", encoding="utf-8")
        data = root.parent / "state"
        config = PluginConfig("fake", enabled=True, keyword="fk")
        SettingsFile(data / "settings.json").save(
            Settings(
                roots=(str(root),), index_apps=False, index_windows_tools=False, plugins=(config,)
            )
        )
        self.plugin = FakePlugin()
        found = Discovery([self.plugin])
        with (
            patch("winlauncher.ui.controller.BackgroundTasks", DeferredTasks),
            patch("winlauncher.ui.controller.discover_plugins", return_value=found),
            patch.object(LauncherController, "_start_monitor"),
            patch("winlauncher.ui.tray.QSystemTrayIcon.isSystemTrayAvailable", return_value=False),
        ):
            self.controller = LauncherController(data, hooks=False)
        self.addCleanup(self.controller.close)
        self.controller.keyboard_timer.stop()
        self.controller.watch_timer.stop()
        self.app.processEvents()
        self.tasks = self.controller.tasks
        self.tasks.finish(self._category("INDEX"))
        self.controller.plugins.refresh("fake", config)
        self.window = self.controller.window
        self.window.show()

    @staticmethod
    def _category(name):
        from winlauncher.ui.tasks import TaskCategory

        return TaskCategory[name]

    def search(self, text: str):
        self.window.query.setText(text)
        self.controller.search.run()
        self.tasks.finish(self._category("SEARCH"))
        return [self.window.results.item(i).data(256) for i in range(self.window.results.count())]

    def test_plugin_items_join_file_results_and_open_through_the_plugin(self):
        rows = self.search("anna")
        names = [row.name for row in rows]
        self.assertIn("Fake: Anna Schmidt", names)
        self.assertIn("Anna notes.txt", names)
        row = next(row for row in rows if row.name == "Fake: Anna Schmidt")
        self.assertEqual((row.location, row.target, row.opens_in), ("", ANNA.target, "Fake"))
        self.assertFalse(self.window.delegate._icon(row.hit).isNull())
        self.controller.open_item(row.hit, "")
        self.tasks.finish(self._category("ACTION"))
        self.assertEqual(self.plugin.opened, [ANNA])

    def test_refresh_catalog_also_refreshes_enabled_plugins(self):
        self.controller.rescan()
        self.assertEqual(self.tasks.count(self._category("PLUGIN")), 1)

    def test_the_keyword_lists_only_the_plugins_items(self):
        self.assertEqual(
            [row.name for row in self.search("fk")],
            ["Fake: Anna Schmidt", "Fake: Bernd Weber", "Fake: Team Annahme"],
        )
        self.assertEqual([row.name for row in self.search("fk bernd")], ["Fake: Bernd Weber"])

    def test_settings_page_edits_the_plugin(self):
        from winlauncher.ui.settings import SettingsDialog

        dialog = SettingsDialog(
            self.controller.settings,
            website_icons=self.controller.website_icons,
            plugins=self.controller.plugin_refresh,
        )
        self.addCleanup(dialog.deleteLater)
        card = dialog.plugin_page.cards[0]
        self.assertIn("3 items", card.status.text())
        self.assertEqual(
            dialog.plugin_page.list.item(0).text(), "Fake\nOn  ·  keyword fk  ·  3 items"
        )
        self.assertFalse(card.isHidden())
        card.keyword.setText("ff")
        card.boost.setValue(20)
        config = dialog.read_settings().plugins[0]
        self.assertEqual((config.keyword, config.boost, config.options), ("ff", 20, {"count": 3}))


if __name__ == "__main__":
    unittest.main()
