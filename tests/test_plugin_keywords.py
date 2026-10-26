"""Plugin keywords: synonyms, routes to part of a plugin's items, and older settings."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from winlauncher.plugins.api import Plugin, PluginItem, PluginRoute
from winlauncher.plugins.config import PluginConfig
from winlauncher.plugins.host import PluginHost
from winlauncher.plugins.registry import Discovery
from winlauncher.plugins.search import KeywordTarget, PluginQuery, plugin_query
from winlauncher.search.query_parser import parse_query
from winlauncher.settings import Settings, settings_from_json
from winlauncher.websites import WebsiteShortcut

LOGIN_BUG = PluginItem("T-1", "Login fails after update", routes=("bugs",))
EXPORT_BUG = PluginItem("T-2", "Export loses umlauts", routes=("bugs",))
LOGIN_STORY = PluginItem("T-3", "Login with single sign-on")


class Tickets(Plugin):
    id = "tickets"
    label = "Ticket"
    keywords = ("ticket", "tk")
    routes = (PluginRoute("bugs", "Bugs only", ("bug",)),)

    def refresh(self, context):
        return [LOGIN_BUG, EXPORT_BUG, LOGIN_STORY]


class OlderPlugin(Plugin):
    id = "older"
    label = "Older"
    keyword = "old"

    def refresh(self, context):
        return []


class KeywordsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="winlauncher-keywords-")
        self.addCleanup(temporary.cleanup)
        self.host = PluginHost(Discovery([Tickets(), OlderPlugin()]), Path(temporary.name))
        self.host.refresh("tickets", self.host.config("tickets", ()))

    def search(self, text: str, configs=()) -> list[str]:
        found = plugin_query(text, self.host.keywords(configs))
        return [hit.item.key for hit in self.host.search(found, configs, 10).hits]

    def test_suggested_keywords_and_routes_become_the_defaults(self):
        config = self.host.config("tickets", ())
        self.assertEqual((config.keywords, config.routes), (("ticket", "tk"), {"bugs": ("bug",)}))
        self.assertEqual(self.host.config("older", ()).keywords, ("old",))
        self.assertEqual(
            self.host.keywords(()),
            {
                "ticket": KeywordTarget("tickets"),
                "tk": KeywordTarget("tickets"),
                "bug": KeywordTarget("tickets", "bugs"),
                "old": KeywordTarget("older"),
            },
        )

    def test_synonyms_search_alike_and_a_route_keeps_to_its_items(self):
        self.assertEqual(self.search("ticket login"), ["T-1", "T-3"])
        self.assertEqual(self.search("tk login"), ["T-1", "T-3"])
        self.assertEqual(self.search("bug login"), ["T-1"])
        self.assertEqual(self.search("bug"), ["T-1", "T-2"])  # The route's items, in order.
        found = plugin_query("@bug export", self.host.keywords(()))
        self.assertEqual(found, PluginQuery("tickets", parse_query("export"), route="bugs"))

    def test_the_user_s_own_keywords_replace_the_suggested_ones(self):
        mine = PluginConfig("tickets", keywords=("jira",), routes={"bugs": ("defect", "bg")})
        self.assertEqual(self.search("defect login", (mine,)), ["T-1"])
        self.assertEqual(self.search("bg", (mine,)), ["T-1", "T-2"])
        self.assertIsNone(plugin_query("bug login", self.host.keywords((mine,))))
        # A route the plugin dropped frees its keyword.
        gone = PluginConfig("tickets", routes={"gone": ("x",)})
        self.assertNotIn("x", self.host.keywords((gone,)))

    def test_items_cache_their_routes(self):
        self.host.load_cached()
        self.assertEqual(self.host.state("tickets").items[0].routes, ("bugs",))
        with self.assertRaisesRegex(ValueError, "routes must be route ids"):
            PluginItem("k", "Name", routes=(1,))


class KeywordSettingsTest(unittest.TestCase):
    def test_a_keyword_of_older_settings_becomes_the_keywords(self):
        settings = settings_from_json(
            {
                "roots": [],
                "plugins": [{"id": "telegram", "keyword": "tg"}, {"id": "x", "keyword": ""}],
            }
        )
        self.assertEqual([config.keywords for config in settings.plugins], [("tg",), ()])

    def test_every_keyword_and_route_keyword_must_be_unique(self):
        site = WebsiteShortcut("g", "Site", "https://example.org/?q=%s")
        for config in (
            PluginConfig("a", keywords=("g",)),
            PluginConfig("a", routes={"bugs": ("g",)}),
            PluginConfig("a", keywords=("bug",), routes={"bugs": ("bug",)}),
        ):
            with self.subTest(config=config), self.assertRaisesRegex(ValueError, "already used"):
                Settings(roots=(), websites=(site,), plugins=(config,))
        with self.assertRaisesRegex(ValueError, "keywords may contain"):
            PluginConfig("a", keywords=("two words",))
        self.assertEqual(
            PluginConfig("a", keywords=["x"], routes={"r": ["y"]}).routes, {"r": ("y",)}
        )


if __name__ == "__main__":
    unittest.main()
