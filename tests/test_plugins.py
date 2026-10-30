"""Search plugins: ranking, merging, refresh and cache, discovery, settings and the UI."""

from __future__ import annotations

import json
import os
import random
import re
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from winlauncher.catalog.model import CatalogItem, ItemKind
from winlauncher.connectivity import InternetState, InternetStatus
from winlauncher.plugins.api import (
    MAX_CHIPS,
    Chip,
    ChipTone,
    Plugin,
    PluginCommand,
    PluginError,
    PluginItem,
    PluginOption,
    PluginRoute,
    SetupRequired,
)
from winlauncher.plugins.budget import SLOW_SEARCHES_IN_A_ROW
from winlauncher.plugins.config import PluginConfig
from winlauncher.plugins.host import CACHE_FILE, PluginHost
from winlauncher.plugins.registry import Discovery, PluginSource, discover_plugins
from winlauncher.plugins.search import (
    CHECKPOINT_INTERVAL_S,
    CHECKPOINT_PAUSE_S,
    KeywordTarget,
    PluginHit,
    PluginQuery,
    literal_patterns,
    merge_hits,
    plugin_query,
    plugins_answer,
    rank_items,
    searchable_items,
)
from winlauncher.search.query_parser import parse_query
from winlauncher.search.ranking import SearchHit
from winlauncher.settings import Settings, SettingsFile, settings_from_json
from winlauncher.ui.result_rows import result_row, shortened_breadcrumbs
from winlauncher.websites import WebsiteShortcut

ANNA = PluginItem("u1", "Anna Schmidt", target="tg://resolve?domain=anna", keywords=("annas",))
BERND = PluginItem("u2", "Bernd Weber", target="tg://resolve?domain=bernd")
TEAM = PluginItem(
    "g1",
    "Team Annahme",
    target="tg://privatepost?channel=5",
    breadcrumbs=("Contoso", "Teams", "Annahme"),
    chips=(Chip("Active", ChipTone.SUCCESS), Chip("Group")),
)


class FakePlugin(Plugin):
    id = "fake"
    label = "Fake"
    keyword = "fk"
    options = (PluginOption("count", "Count", 3, bounds=(1, 10)),)

    def __init__(self):
        # A BaseException too, so SystemExit from a plugin can be exercised.
        self.items: list[PluginItem] | BaseException = [ANNA, BERND, TEAM]
        self.opened: list[PluginItem] = []
        self.contexts = []

    def refresh(self, context):
        self.contexts.append(context)
        if isinstance(self.items, BaseException):
            raise self.items
        return self.items

    def open(self, item, context):
        self.opened.append(item)


def query(text: str):
    return parse_query(text)


class PluginRankingTest(unittest.TestCase):
    def test_names_rank_like_file_names_and_keywords_a_little_lower(self):
        hits = rank_items(
            "fake", "Fake", searchable_items([ANNA, BERND, TEAM]), query("anna"), limit=10
        )
        self.assertEqual([hit.item for hit in hits], [ANNA, TEAM])
        self.assertEqual(hits[0].reason, "Name prefix")
        keyword = rank_items("fake", "Fake", searchable_items([ANNA]), query("annas"), limit=10)[0]
        self.assertEqual(keyword.reason, "Keyword matches")
        self.assertEqual(hits[0].title, "Fake: Anna Schmidt")
        self.assertEqual(
            rank_items("fake", "Fake", searchable_items([ANNA]), query("anna -schmidt"), limit=5),
            [],
        )

    def test_earlier_items_win_ties_and_no_terms_keep_the_plugins_order(self):
        first = PluginItem("a", "Max Mustermann")
        second = PluginItem("b", "Max Mustermann ")
        hits = rank_items("fake", "Fake", searchable_items([second, first]), query("max"), limit=10)
        self.assertEqual(hits[0].item, second)
        listed = rank_items(
            "fake", "Fake", searchable_items([BERND, ANNA, TEAM]), query(""), limit=2
        )
        self.assertEqual([hit.item for hit in listed], [BERND, ANNA])
        plain = rank_items("fake", "Fake", searchable_items([ANNA]), query("anna"), limit=1)[0]
        boosted = rank_items(
            "fake", "Fake", searchable_items([ANNA]), query("anna"), boost=30, limit=1
        )[0]
        self.assertEqual(boosted.score - plain.score, 30)

    def test_only_plain_word_searches_include_plugins(self):
        self.assertTrue(plugins_answer(query("anna")))
        self.assertTrue(plugins_answer(query('"anna s" -bernd')))
        for text in ("", "anna type:folder", "anna ext:pdf", "anna in:C:\\Temp", "path:x anna"):
            self.assertFalse(plugins_answer(query(text)), text)

    def test_keywords_select_one_plugin_without_taking_the_word_away(self):
        keywords = {"fk": KeywordTarget("fake")}
        found = plugin_query("FK anna", keywords)
        self.assertEqual(found, PluginQuery("fake", query("anna"), query("FK anna")))
        self.assertEqual(plugin_query("fk", keywords).files, query("fk"))
        self.assertEqual(plugin_query("fk", keywords).search.terms, ())
        self.assertIsNone(plugin_query("fkx anna", keywords))
        self.assertIsNone(plugin_query("", keywords))
        self.assertEqual(plugin_query('fk "anna', keywords).search.terms, ('"anna',))

    def test_the_prefix_asks_for_that_plugin_alone(self):
        keywords = {"fk": KeywordTarget("fake")}
        found = plugin_query("@fk anna", keywords)
        self.assertEqual(found, PluginQuery("fake", query("anna")))
        self.assertIsNone(found.files)  # No catalog search beside it.
        self.assertEqual(plugin_query("@fk", keywords).search.terms, ())
        self.assertIsNone(plugin_query("@nope anna", keywords))  # Searched as typed.
        self.assertIsNone(plugin_query("@", keywords))

    def test_plugin_hits_join_file_hits_by_score(self):
        item = CatalogItem(Path("C:/a.txt"), ItemKind.FILE, 0, 0)
        files = [SearchHit(item, 150, "x"), SearchHit(item, 60, "y")]
        plugins = [
            PluginHit("fake", "Fake", ANNA, 100, "z"),
            PluginHit("fake", "Fake", BERND, 10, ""),
        ]
        merged = merge_hits(files, plugins, 3)
        self.assertEqual([hit.score for hit in merged], [150, 100, 60])


class CandidatePrePassTest(unittest.TestCase):
    """The cheap test before matching must only skip items that could not score.

    It mirrors the catalog's candidate SQL, so the same risk applies: a test that is
    not a superset of the matcher would silently drop hits.
    """

    CORPUS = (
        "Anna Schmidt",
        "anna.schmidt@example.com",
        "ABC-1234 Login fails on Windows",
        "Löschen bricht ab",
        "MyFileName",
        "team_annahme",
        "Visual Studio Code",
        "straße 12",
        "ÄÖÜ Test",
        "a-b-c",
        "12345",
        "Bernd",
        "a\nb\nc",
        "StraßeCode",
    )

    def items(self) -> list[PluginItem]:
        return [
            PluginItem(f"k{index}", name, keywords=("alias", name.split()[0]))
            for index, name in enumerate(self.CORPUS)
        ]

    def ranked(self, items, text: str, *, skip_pre_pass: bool = False):
        """Rank `text`; without the pre-pass every item reaches the matcher."""
        prepared = searchable_items(items)
        if not skip_pre_pass:
            return rank_items("p", "P", prepared, query(text), limit=50)
        with patch(
            "winlauncher.plugins.search._candidates", side_effect=lambda items, q, f, c: items
        ):
            return rank_items("p", "P", prepared, query(text), limit=50)

    def test_the_pre_pass_never_changes_a_result(self):
        items = self.items()
        words = [word for name in self.CORPUS for word in re.split(r"\W+", name.lower()) if word]
        random.seed(11)
        queries = [
            "anna",
            "ansch",
            "vsc",
            "löschen",
            "loschen",
            "abc-1234",
            "1234",
            "anna -bernd",
            '"anna schmidt"',
            "my name",
            "a",
            "straße",
            "ä",
            "*code",
            "ann*",
            "abc",
            "sscd",
            "*code alias",
        ]
        queries += ["".join(random.sample(word, len(word))) for word in words[:20]]
        queries += [random.choice(words)[:2] + random.choice("xyz") for _ in range(20)]
        for text in queries:
            for suffix in ("", " match:keyword"):
                with self.subTest(text=text + suffix):
                    fast = self.ranked(items, text + suffix)
                    every = self.ranked(items, text + suffix, skip_pre_pass=True)
                    self.assertEqual(
                        [(h.item.key, round(h.score, 6), h.reason) for h in fast],
                        [(h.item.key, round(h.score, 6), h.reason) for h in every],
                    )

    def test_filtering_stops_when_cancelled_even_without_candidates(self):
        prepared = searchable_items([ANNA, BERND, TEAM])
        cancel = threading.Event()
        checked = []

        def reject_and_cancel(text: str) -> bool:
            checked.append(text)
            cancel.set()
            return False

        with patch("winlauncher.plugins.search._candidate_test", return_value=reject_and_cancel):
            with patch("winlauncher.plugins.search._text_relevance") as match:
                self.assertEqual(
                    rank_items("p", "P", prepared, query("zz"), limit=10, cancel=cancel), []
                )
                match.assert_not_called()
                self.assertEqual(len(checked), 1)

    def test_cancellation_discards_hits_already_ranked(self):
        prepared = searchable_items([ANNA, TEAM])
        cancel = threading.Event()

        def match_and_cancel(*args: object) -> tuple[float, str]:
            cancel.set()
            return 100, "Match"

        with patch(
            "winlauncher.plugins.search._text_relevance", side_effect=match_and_cancel
        ) as match:
            self.assertEqual(
                rank_items("p", "P", prepared, query("anna"), limit=10, cancel=cancel), []
            )
            self.assertEqual(match.call_count, 1)

    def test_filtering_and_matching_share_timed_checkpoints(self):
        prepared = searchable_items([ANNA, BERND])
        # Matching the first item spends the budget; the next candidate yields,
        # including when every item is rejected by the filter.
        for text in ("anna", "zz"):
            with self.subTest(text=text):
                with (
                    patch(
                        "winlauncher.plugins.search.time.perf_counter",
                        side_effect=[0, 0, CHECKPOINT_INTERVAL_S, CHECKPOINT_INTERVAL_S],
                    ),
                    patch("winlauncher.plugins.search.time.sleep") as sleep,
                ):
                    rank_items("p", "P", prepared, query(text), limit=10)
                    sleep.assert_called_once_with(CHECKPOINT_PAUSE_S)

    def test_a_plugin_declares_which_terms_name_its_items(self):
        """`HCP` has no digit, so only the plugin knows it is a ticket prefix."""
        tickets = [
            PluginItem("1", "HCP-1234 Sensor calibration drifts"),
            PluginItem("2", "Hardware Check Procedure for the gateway"),
        ]
        prepared = searchable_items(tickets)
        loosely = rank_items("p", "P", prepared, query("hcp"), limit=10)
        self.assertEqual([hit.item.key for hit in loosely], ["1", "2"])  # Also the initials.
        literally = rank_items(
            "p", "P", prepared, query("hcp"), limit=10, literal=literal_patterns((r"HCP-?\w*",))
        )
        self.assertEqual([hit.item.key for hit in literally], ["1"])
        # Only terms the pattern describes entirely; the others stay loose.
        both = rank_items(
            "p",
            "P",
            prepared,
            query("hcp gatewy"),
            limit=10,
            literal=literal_patterns((r"HCP-?\w*",)),
        )
        self.assertEqual([hit.item.key for hit in both], [])
        self.assertEqual(
            [
                hit.item.key
                for hit in rank_items(
                    "p",
                    "P",
                    prepared,
                    query("hcp calibratio"),
                    limit=10,
                    literal=literal_patterns((r"HCP-?\w*",)),
                )
            ],
            ["1"],
        )

    def test_numbers_are_matched_in_one_piece_while_words_stay_loose(self):
        tickets = [
            PluginItem("1", "ABC-12345 Login fails on Windows"),
            PluginItem("2", "ABC-52314 uC on halt when exported"),
            PluginItem("3", "ABC-777 Timeout in the gateway"),
        ]
        # The same digits in another order are a different ticket, not a fuzzy match.
        self.assertEqual([h.item.key for h in self.ranked(tickets, "12345")], ["1"])
        self.assertEqual([h.item.key for h in self.ranked(tickets, "52314")], ["2"])
        self.assertEqual(self.ranked(tickets, "1234567"), [])
        # Words in the very same query still match loosely, typos included.
        self.assertEqual([h.item.key for h in self.ranked(tickets, "uC halted")], ["2"])
        self.assertEqual([h.item.key for h in self.ranked(tickets, "gatewy")], ["3"])
        self.assertEqual([h.item.key for h in self.ranked(tickets, "12345 login")], ["1"])


class PluginHostTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="winlauncher-plugins-")
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name)
        self.plugin = FakePlugin()
        self.host = PluginHost(Discovery([self.plugin]), self.data)
        self.config = PluginConfig("fake", enabled=True, keywords=("fk",), options={"count": 99})

    def test_refresh_keeps_items_in_a_cache_that_the_next_start_reads(self):
        state = self.host.refresh("fake", self.config)
        self.assertEqual(state.items, (ANNA, BERND, TEAM))
        context = self.plugin.contexts[0]
        self.assertEqual(context.options, {"count": 3})  # Out of bounds: the default.
        self.assertEqual(context.data_dir, self.data / "plugin-data" / "fake")
        restarted = PluginHost(Discovery([FakePlugin()]), self.data)
        self.assertTrue(restarted.load_cached())
        self.assertEqual(restarted.state("fake").items, (ANNA, BERND, TEAM))
        hits = restarted.search(query("bernd"), (self.config,), 10).hits
        self.assertEqual([hit.item for hit in hits], [BERND])
        # Cached items wait for a worker to prepare their fuzzy forms; until then
        # searching computes them itself, with the same hits either way.
        fuzzy = restarted.search(query("brnd"), (self.config,), 10).hits
        restarted.prepare_cached()
        self.assertEqual(restarted.search(query("brnd"), (self.config,), 10).hits, fuzzy)
        self.assertEqual([hit.item for hit in fuzzy], [BERND])
        self.assertFalse(PluginHost(Discovery([FakePlugin()]), self.data / "none").load_cached())

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

    def test_context_reads_live_internet_status_and_offline_pause_preserves_items(self):
        state = self.host.refresh("fake", self.config)
        context = self.plugin.contexts[-1]
        self.assertEqual(context.internet().status, InternetStatus.UNKNOWN)
        self.host.internet = InternetState(False, False, 42)
        self.assertEqual(context.internet().status, InternetStatus.OFFLINE)
        self.plugin.items = PluginError("Must not run while paused")
        self.assertEqual(self.host.refresh("fake", self.config, pause_when_offline=True), state)
        self.assertEqual(len(self.plugin.contexts), 1)
        self.assertEqual(len(self.host.state("fake").items), 3)
        # Explicit refreshes still run, so local/VPN plugins remain usable.
        self.assertEqual(self.host.refresh("fake", self.config).error, str(self.plugin.items))

    def test_disabled_plugins_and_file_queries_stay_silent(self):
        self.host.refresh("fake", self.config)
        off = PluginConfig("fake", enabled=False, keywords=("fk",))
        self.assertEqual(self.host.search(query("anna"), (off,), 10).hits, [])
        self.assertEqual(self.host.search(PluginQuery("fake", query("")), (off,), 10).hits, [])
        self.assertEqual(self.host.search(query("anna type:file"), (self.config,), 10).hits, [])
        hidden = PluginConfig("fake", enabled=True, in_search=False)
        self.assertEqual(self.host.search(query("anna"), (hidden,), 10).hits, [])
        listed = self.host.search(PluginQuery("fake", query("")), (hidden,), 10)
        self.assertEqual(len(listed.hits), 3)

    def test_a_plugin_too_slow_for_ordinary_searches_keeps_its_keyword(self):
        self.host.refresh("fake", self.config)
        clock = iter(float(tick) for tick in range(0, 1000, 1))  # Every reading 1 s later.
        with patch("winlauncher.plugins.host.time.perf_counter", lambda: next(clock)):
            for _ in range(SLOW_SEARCHES_IN_A_ROW - 1):
                found = self.host.search(query("anna"), (self.config,), 10, time_limit_ms=30)
                self.assertEqual(found.left_out, ())
            with self.assertLogs("winlauncher.plugins.budget", "WARNING"):
                self.host.search(query("anna"), (self.config,), 10, time_limit_ms=30)
            found = self.host.search(query("anna"), (self.config,), 10, time_limit_ms=30)
            self.assertEqual((found.hits, found.left_out), ([], ("fake",)))
            self.assertEqual(found.summary(), "fake=left-out")
            by_keyword = self.host.search(
                PluginQuery("fake", query("anna")), (self.config,), 10, time_limit_ms=30
            )
            self.assertEqual(len(by_keyword.hits), 2)
            no_limit = self.host.search(query("anna"), (self.config,), 10, time_limit_ms=0)
            self.assertEqual(len(no_limit.hits), 2)
        self.assertTrue(self.host.search_record("fake").left_out)
        self.plugin.items = [ANNA]  # New items: it is measured afresh.
        self.host.refresh("fake", self.config)
        self.assertFalse(self.host.search_record("fake").left_out)
        found = self.host.search(query("anna"), (self.config,), 10, time_limit_ms=30)
        self.assertEqual([hit.item for hit in found.hits], [ANNA])
        self.assertRegex(found.summary(), r"^fake=[\d.]+ms/1items/1hits$")

    def test_a_search_typed_over_stops_and_keeps_no_hits(self):
        self.host.refresh("fake", self.config)
        cancel = threading.Event()
        cancel.set()
        found = self.host.search(query("anna"), (self.config,), 10, cancel=cancel)
        self.assertEqual((found.hits, found.timings), ([], ()))

    def test_a_refresh_logs_the_plugins_time_apart_from_winlaunchers(self):
        with self.assertLogs("winlauncher.plugins.host", "INFO") as logs:
            self.host.refresh("fake", self.config)
        self.assertRegex(
            logs.output[-1],
            r"Plugin fake refreshed: items=3 plugin_ms=\d+ validate_ms=\d+ cache_ms=\d+ "
            r"prepare_ms=\d+ total_ms=\d+",
        )
        self.assertEqual(self.host.keywords((self.config,)), {"fk": KeywordTarget("fake")})
        # A loaded plugin was installed or activated on purpose, so it starts out on.
        self.assertEqual(self.host.keywords(()), {"fk": KeywordTarget("fake")})

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
            # Sorts after notes.py, so it is the one that finds the id taken.
            (folder / "takeover.py").write_text(
                "from winlauncher.plugins.api import Plugin\n"
                "class Again(Plugin):\n    id = 'notes'\n"
                "class Bad(Plugin):\n    id = 'Bad Id'\n",
                encoding="utf-8",
            )
            with (
                patch("winlauncher.plugins.registry.entry_points", return_value=()),
                self.assertLogs("winlauncher.plugins.registry", "ERROR"),
            ):
                found = discover_plugins([folder])
        self.assertEqual([plugin.id for plugin in found.plugins], ["notes"])
        self.assertEqual(found.sources["notes"].path, folder / "notes.py")
        problems = " ".join(found.problems)
        for text in (
            "broken.py: ValueError: oops",
            "empty.py: defines no Plugin subclass",
            "takeover.py Again: the id notes is already used by notes.py",
            "takeover.py Bad: the id must use",
        ):
            self.assertIn(text, problems)
        with (
            patch("winlauncher.plugins.registry.entry_points", return_value=()),
            self.assertLogs("winlauncher.plugins.registry", "ERROR"),
        ):
            missing = discover_plugins(built_in=("nowhere:Nothing",))
        self.assertEqual((missing.plugins, len(missing.problems)), ([], 1))

    def test_plugin_folders_of_several_paths_load_in_order_with_their_version(self):
        with tempfile.TemporaryDirectory() as directory:
            repository, private = Path(directory) / "repo", Path(directory) / "private"
            write_plugin_folder(repository / "chats", "chats", version="2.1.0")
            write_plugin_folder(repository / "records", "records")
            (repository / ".git").mkdir()  # Not a plugin folder; no problem to report.
            write_plugin_folder(repository / "_template", "template")  # Never loaded.
            (repository / "half" / "plugin.toml").parent.mkdir()
            (repository / "half" / "plugin.toml").write_text('version = "1"', encoding="utf-8")
            write_plugin_folder(private / "chats", "chats")  # The repository already has it.
            write_plugin_folder(private / "mine", "mine", version=["1.0"])
            with patch("winlauncher.plugins.registry.entry_points", return_value=()):
                found = discover_plugins([repository, private, Path(directory) / "gone"])
            self.assertEqual([plugin.id for plugin in found.plugins], ["chats", "records", "mine"])
            chats = found.sources["chats"]
            self.assertEqual((chats.version, chats.folder), ("2.1.0", repository / "chats"))
            self.assertEqual(found.sources["records"].version, "")  # No manifest.
            problems = " ".join(found.problems)
            self.assertIn("the id chats is already used by chats", problems)
            self.assertIn("half: has no plugin.py", problems)
            self.assertIn("mine: the version must be text", problems)
            self.assertNotIn(".git", problems)
            self.assertNotIn("template", problems)


class PluginLoaderRobustnessTest(unittest.TestCase):
    """A plugin author who breaks the contract must never break WinLauncher.

    Everything a plugin declares is painted into result rows and the settings page,
    where an exception would take the window down, so the loader checks it once.
    """

    @staticmethod
    def declared(**attributes) -> type[Plugin]:
        return type("Declared", (Plugin,), {"id": "p", "label": "P", **attributes})

    def test_unusable_declarations_are_rejected_with_a_message_for_their_author(self):
        option = PluginOption("a", "A", 5, bounds=(10, 20))
        for attributes, expected in (
            ({"refresh": None}, "not implement refresh"),
            ({"label": "  "}, "needs a label"),
            ({"description": 7}, "description must be text"),
            ({"icon": Path("x.svg")}, "icon must be text"),
            ({"keyword": "two words"}, "keyword may contain"),
            ({"keywords": "jira"}, "keywords must be a tuple"),
            ({"keywords": ("two words",)}, "keywords must be a tuple"),
            ({"keyword": "a", "keywords": ("b",)}, "not both"),
            ({"routes": [PluginRoute("bugs", "Bugs")]}, "tuple of PluginRoute"),
            ({"routes": (PluginRoute("Bugs", "Bugs"),)}, "route id must use"),
            ({"routes": (PluginRoute("bugs", "Bugs"), PluginRoute("bugs", "B"))}, "declared twice"),
            ({"routes": (PluginRoute("bugs", " "),)}, "route bugs needs a label"),
            ({"routes": (PluginRoute("bugs", "Bugs", ("a b",)),)}, "keywords of route bugs"),
            ({"round_icons": 1}, "True or False"),
            ({"refresh_minutes": 0}, "whole number from 1 to 1440"),
            ({"refresh_minutes": True}, "whole number from 1 to 1440"),
            ({"options": [option]}, "tuple of PluginOption"),
            ({"options": (option,)}, "bounds that its default misses"),
            ({"options": (PluginOption("a", "A", 1.5),)}, "whole number or text default"),
            ({"options": (PluginOption("a", "A", "", multiline=1),)}, "True or False"),
            ({"options": (PluginOption("a", "A", 1, multiline=True),)}, "text default"),
            ({"options": (PluginOption("a", "A", False, multiline=True),)}, "text default"),
            ({"options": (PluginOption("a", "A", 1), PluginOption("a", "B", 2))}, "twice"),
            ({"literal_terms": [r"HCP-\w*"]}, "tuple of regular expressions"),
            ({"literal_terms": (r"HCP-[",)}, "unterminated character set"),
        ):
            declaration = dict(attributes)
            if declaration.get("refresh") is None and "refresh" in declaration:
                declaration.pop("refresh")  # Leaves Plugin.refresh unimplemented.
            else:
                declaration["refresh"] = lambda self, context: []
            with self.subTest(expected=expected):
                found = Discovery()
                found.add(self.declared(**declaration), PluginSource("mine.py"))
                self.assertEqual(found.plugins, [])
                self.assertIn(expected, " ".join(found.problems))

    def test_a_correct_declaration_is_accepted(self):
        found = Discovery()
        found.add(
            self.declared(refresh=lambda self, context: [], keyword="k-1", refresh_minutes=1440),
            PluginSource("mine.py"),
        )
        self.assertEqual((found.problems, [p.id for p in found.plugins]), ([], ["p"]))

    def test_items_the_launcher_could_not_show_are_refused(self):
        for fields, expected in (
            ({"key": ""}, "need a key"),
            ({"name": " "}, "need a name"),
            ({"target": 5}, "target must be text"),
            ({"subtitle": None}, "subtitle must be text"),
            ({"icon": Path("a.jpg")}, "icon must be text"),
            ({"keywords": (1,)}, "keywords must be text"),
            ({"breadcrumbs": ("Site", " ")}, "breadcrumbs must be non-empty text"),
            ({"breadcrumbs": ("Site", 3)}, "breadcrumbs must be non-empty text"),
            ({"chips": ("Done",)}, "chips must be Chip values"),
            ({"data": [("a", 1)]}, "data must map names"),
        ):
            with self.subTest(expected=expected), self.assertRaises(ValueError) as caught:
                PluginItem(**{"key": "1", "name": "Anna", **fields})
            self.assertIn(expected, str(caught.exception))

    def test_chips_need_text_and_a_known_tone(self):
        self.assertIs(Chip("Done", "success").tone, ChipTone.SUCCESS)
        for text, tone, expected in (
            (" ", ChipTone.NEUTRAL, "needs text"),
            ("Done", "green", "tone must be one of"),
        ):
            with self.subTest(expected=expected), self.assertRaises(ValueError) as caught:
                Chip(text, tone)
            self.assertIn(expected, str(caught.exception))

    def test_unusable_settings_buttons_are_ignored(self):
        class Buttons(FakePlugin):
            def commands(self):
                return (PluginCommand("Sign in", lambda context: None), ("Sign out", None), None)

        with tempfile.TemporaryDirectory() as directory:
            host = PluginHost(Discovery([Buttons()]), Path(directory))
            with self.assertLogs("winlauncher.plugins.host", "WARNING"):
                commands = host.commands("fake")
        self.assertEqual([command.label for command in commands], ["Sign in"])

    def test_a_plugin_that_exits_the_interpreter_leaves_nothing_marked_busy(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin = FakePlugin()
            plugin.items = SystemExit("plugin called sys.exit")
            host = PluginHost(Discovery([plugin]), Path(directory))
            # The worker pool turns it into a failed task; the host must not stay busy.
            with self.assertRaises(SystemExit):
                host.refresh("fake", PluginConfig("fake", enabled=True))
            self.assertFalse(host.state("fake").refreshing)


def write_plugin_folder(folder: Path, plugin_id: str, *, version: object = None) -> None:
    """A plugin folder as the plugin repository has it: plugin.py and a manifest."""
    (folder / "assets").mkdir(parents=True)
    (folder / "plugin.py").write_text(
        "from winlauncher.plugins.api import Plugin, PluginItem\n"
        f"class Listed(Plugin):\n    id = {plugin_id!r}\n    label = {plugin_id.title()!r}\n"
        "    def refresh(self, context):\n        return [PluginItem('1', 'Only item')]\n",
        encoding="utf-8",
    )
    if version is not None:
        (folder / "plugin.toml").write_text(f"version = {json.dumps(version)}", encoding="utf-8")


class PluginSettingsTest(unittest.TestCase):
    def test_plugin_configs_roundtrip_and_validate(self):
        config = PluginConfig("chats", enabled=True, keywords=("tg",), options={"bots": False})
        settings = Settings(roots=(), plugins=(config,), plugins_pause_offline=True)
        with tempfile.TemporaryDirectory() as directory:
            file = SettingsFile(Path(directory) / "settings.json")
            file.save(settings)
            self.assertEqual(file.load(), settings)
            stored = json.loads(file.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["plugins"][0]["options"], {"bots": False})
        self.assertEqual(settings_from_json({"roots": []}).plugins, ())
        self.assertFalse(settings_from_json({"roots": []}).plugins_pause_offline)
        with self.assertRaises(ValueError):
            settings_from_json({"roots": [], "plugins_pause_offline": "false"})
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
        Settings(roots=(), plugins=(PluginConfig("a", enabled=False, keywords=("g",)),))


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
        root = self.root = Path(self.temporary.name) / "library"
        root.mkdir()
        (root / "Anna notes.txt").write_text("x", encoding="utf-8")
        (root / "fk notes.txt").write_text("x", encoding="utf-8")
        data = root.parent / "state"
        config = PluginConfig("fake", enabled=True, keywords=("fk",))
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

    def test_cached_forms_are_prepared_on_the_plugin_worker(self):
        from winlauncher.ui.plugins import PluginScheduler

        host = self.controller.plugins
        scheduler = PluginScheduler(host, self.tasks, lambda: self.controller.settings)
        self.addCleanup(scheduler.close)
        with patch.object(host, "prepare_cached") as prepare:
            scheduler.start()
            prepare.assert_not_called()
            self.tasks.finish(self._category("PLUGIN"))
            prepare.assert_called_once_with()

    def test_offline_pause_skips_automatic_refresh_but_allows_manual_and_resumes(self):
        scheduler = self.controller.plugin_refresh
        self.controller.settings = replace(self.controller.settings, plugins_pause_offline=True)
        original = self.controller.plugins.state("fake")
        offline = InternetState(False, False, 42)
        scheduler._internet_checked(offline)
        count = self.tasks.count(self._category("PLUGIN"))
        scheduler.refresh("fake", automatic=True)
        self.assertEqual(self.tasks.count(self._category("PLUGIN")), count)
        self.assertEqual(self.controller.plugins.state("fake"), original)
        scheduler.refresh_all()
        self.assertEqual(self.tasks.count(self._category("PLUGIN")), count + 1)
        self.tasks.finish(self._category("PLUGIN"))
        # An inconclusive probe between offline and online must not lose the recovery.
        scheduler._internet_checked(InternetState(checked_at=43))
        scheduler._internet_checked(InternetState(True, False, 44))
        self.assertEqual(self.tasks.count(self._category("PLUGIN")), count + 2)
        self.tasks.finish(self._category("PLUGIN"))
        scheduler._internet_checked(InternetState(True, True, 45))
        self.assertEqual(self.tasks.count(self._category("PLUGIN")), count + 2)
        # Default policy lets plugins decide for themselves even when offline.
        self.controller.settings = replace(self.controller.settings, plugins_pause_offline=False)
        scheduler._internet_checked(offline)
        scheduler.refresh("fake", automatic=True)
        self.assertEqual(self.tasks.count(self._category("PLUGIN")), count + 3)

    def test_queued_refresh_rechecks_connectivity_before_calling_plugin(self):
        scheduler = self.controller.plugin_refresh
        self.controller.settings = replace(self.controller.settings, plugins_pause_offline=True)
        scheduler._internet_checked(InternetState(True, True, 42))
        scheduler.refresh("fake", automatic=True)
        calls = len(self.plugin.contexts)
        scheduler._internet_checked(InternetState(False, False, 43))
        self.tasks.finish(self._category("PLUGIN"))
        self.assertEqual(len(self.plugin.contexts), calls)
        self.assertFalse(scheduler.is_refreshing("fake"))

    def test_connectivity_checks_use_their_own_worker_and_stop_with_scheduler(self):
        scheduler = self.controller.plugin_refresh
        network = self._category("NETWORK")
        count = self.tasks.count(network)
        scheduler._check_internet()  # Startup's check is still pending.
        self.assertEqual(self.tasks.count(network), count)
        with patch("winlauncher.ui.plugins.probe_internet", return_value=InternetState(True, True)):
            # The startup job holds its original callable; replace it without running I/O.
            job = next(j for j in self.tasks.jobs if j.category == network and not j.done)
            job.done = True
            job.on_success(InternetState(True, True))
            scheduler._check_internet()
            self.tasks.finish(network)
        self.assertEqual(self.tasks.count(network), count + 1)
        scheduler.close()
        self.assertFalse(scheduler._internet_timer.isActive())
        scheduler._check_internet()
        scheduler._internet_checked(InternetState(False, False))
        self.assertEqual(self.tasks.count(network), count + 1)
        self.assertEqual(self.controller.plugins.internet.status, InternetStatus.ONLINE)

    def test_the_keyword_lists_the_plugins_items_without_hiding_files(self):
        """A short keyword must not take its word away from the ordinary search."""
        self.assertEqual(
            [row.name for row in self.search("fk")],
            [
                "Fake: Anna Schmidt",
                "Fake: Bernd Weber",
                "Fake: Team Annahme",
                "fk notes.txt",  # The file is still found beside the plugin's items.
            ],
        )
        self.assertEqual([row.name for row in self.search("fk bernd")], ["Fake: Bernd Weber"])

    def test_the_prefix_leaves_the_catalog_out(self):
        self.assertEqual(
            [row.name for row in self.search("@fk")],
            ["Fake: Anna Schmidt", "Fake: Bernd Weber", "Fake: Team Annahme"],
        )
        # "Team Annahme" contains "anna" too; what must be absent is the file.
        self.assertEqual(
            [row.name for row in self.search("@fk anna")],
            ["Fake: Anna Schmidt", "Fake: Team Annahme"],
        )

    def test_settings_page_edits_the_plugin(self):
        from winlauncher.ui.settings import SettingsDialog

        dialog = SettingsDialog(
            self.controller.settings,
            website_icons=self.controller.website_icons,
            plugins=self.controller.plugin_refresh,
        )
        self.addCleanup(dialog.deleteLater)
        card = dialog.plugin_pages.cards[0]
        self.assertIn("3 items", card.status.text())
        self.assertIn(card, dialog.plugin_pages.settings_sections)
        self.assertFalse(card.keywords.isVisibleTo(card))  # Folded until unfolded.
        card.fold.click()
        self.assertTrue(card.keywords.isVisibleTo(card))
        card.enabled.setChecked(False)  # Turned off, the card folds to its header.
        self.assertFalse(card.keywords.isVisibleTo(card))
        self.assertTrue(card.enabled.isVisibleTo(card))
        self.assertFalse(card.fold.isEnabled())
        card.enabled.setChecked(True)
        self.assertTrue(card.keywords.isVisibleTo(card))
        card.keywords.setText("ff, fff")
        card.boost.setValue(20)
        dialog.plugin_pages.time_limit.setValue(50)
        dialog.plugin_pages.pause_offline.setChecked(True)
        edited = dialog.read_settings()
        self.assertEqual(edited.plugin_time_limit_ms, 50)
        self.assertTrue(edited.plugins_pause_offline)
        self.controller.plugin_refresh._internet_checked(InternetState(False, False, 42))
        self.assertEqual(dialog.plugin_pages.internet_status.text(), "Internet: offline.")
        config = edited.plugins[0]
        self.assertEqual(
            (config.keywords, config.boost, config.options), (("ff", "fff"), 20, {"count": 3})
        )

    def test_multiline_options_keep_line_breaks_through_settings_and_plugin_context(self):
        from PySide6.QtWidgets import QLineEdit, QPlainTextEdit

        from winlauncher.ui.settings import SettingsDialog

        initial = "https://example.org/first;https://example.org/second\nhttps://example.org/third"
        self.plugin.options = (
            *self.plugin.options,
            PluginOption("folders", "Source folders", "", multiline=True),
            PluginOption("name", "Name", "Plain text"),
        )
        config = replace(self.controller.settings.plugins[0], options={"folders": initial})
        settings = replace(self.controller.settings, plugins=(config,))

        def open_dialog(current: Settings) -> SettingsDialog:
            dialog = SettingsDialog(
                current,
                website_icons=self.controller.website_icons,
                plugins=self.controller.plugin_refresh,
            )
            self.addCleanup(dialog.deleteLater)
            return dialog

        dialog = open_dialog(settings)
        card = dialog.plugin_pages.cards[0]
        box = card.findChild(QPlainTextEdit)
        self.assertIsNotNone(box)
        self.assertEqual(box.toPlainText(), initial)
        self.assertEqual(box.accessibleName(), "Source folders")
        self.assertEqual(box.lineWrapMode(), QPlainTextEdit.LineWrapMode.NoWrap)
        self.assertTrue(
            any(line.accessibleName() == "Name" for line in card.findChildren(QLineEdit))
        )
        changed = "https://example.org/überblick\n\nhttps://example.org/next;legacy\n"
        box.setPlainText(changed)
        path = SettingsFile(self.root.parent / "multiline-settings.json")
        path.save(dialog.read_settings())
        restored = path.load()
        self.assertEqual(restored.plugins[0].options["folders"], changed)
        self.assertEqual(
            open_dialog(restored).plugin_pages.cards[0].findChild(QPlainTextEdit).toPlainText(),
            changed,
        )
        context = self.controller.plugins.context("fake", restored.plugins[0])
        self.assertEqual(context.options["folders"], changed)
        box.clear()
        self.assertEqual(dialog.read_settings().plugins[0].options["folders"], "")

    def test_the_card_shows_the_manifests_icon(self):
        from winlauncher.ui.plugin_settings import _plugin_icon
        from winlauncher.ui.widgets import line_icon

        folder = self.root.parent / "jira"
        (folder / "assets").mkdir(parents=True)
        (folder / "plugin.py").write_text("", encoding="utf-8")
        (folder / "plugin.toml").write_text('icon = "assets/jira.svg"', encoding="utf-8")
        (folder / "assets" / "jira.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"><rect width="8" '
            'height="8"/></svg>',
            encoding="utf-8",
        )
        plug = line_icon("plug").cacheKey()
        source = PluginSource("jira", folder / "plugin.py")
        self.assertNotEqual(_plugin_icon(self.plugin, source).cacheKey(), plug)
        self.assertEqual(_plugin_icon(self.plugin, PluginSource("fake")).cacheKey(), plug)

    def test_unfolded_cards_are_remembered_across_dialogs(self):
        from winlauncher.ui.settings import SettingsDialog

        remembered = self.root.parent / "plugin-cards.json"

        def open_dialog() -> SettingsDialog:
            dialog = SettingsDialog(
                self.controller.settings,
                website_icons=self.controller.website_icons,
                plugins=self.controller.plugin_refresh,
                plugin_cards=remembered,
            )
            self.addCleanup(dialog.deleteLater)
            return dialog

        open_dialog().plugin_pages.cards[0].fold.click()
        self.assertEqual(json.loads(remembered.read_text(encoding="utf-8")), ["fake"])
        card = open_dialog().plugin_pages.cards[0]
        self.assertTrue(card.fold.isChecked())
        self.assertTrue(card.keywords.isVisibleTo(card))
        remembered.write_text("not json", encoding="utf-8")
        with self.assertLogs("winlauncher.ui.plugin_settings", "WARNING"):
            self.assertFalse(open_dialog().plugin_pages.cards[0].fold.isChecked())


class BreadcrumbTest(unittest.TestCase):
    """Long plugin locations shorten by whole parts, keeping where and how deep."""

    PARTS = ("Contoso", "Finance", "Controlling", "2026", "Q3", "Reports")

    def shortened(self, width: int) -> str:
        return shortened_breadcrumbs(self.PARTS, lambda text: len(text) <= width)

    def test_middle_parts_go_first_then_only_the_last_part_remains(self):
        full = " › ".join(self.PARTS)
        self.assertEqual(self.shortened(len(full)), full)
        self.assertEqual(
            self.shortened(len(full) - 1), "Contoso › … › Controlling › 2026 › Q3 › Reports"
        )
        self.assertEqual(self.shortened(30), "Contoso › … › Q3 › Reports")
        self.assertEqual(self.shortened(20), "… › Reports")
        self.assertEqual(
            shortened_breadcrumbs(("Site", "Library"), lambda _text: False), "Site › Library"
        )

    def test_breadcrumbs_replace_the_subtitle_of_a_plugin_row(self):
        item = replace(TEAM, subtitle="https://contoso.sharepoint.com/sites/teams/annahme")
        row = result_row(PluginHit("fake", "Fake", item, 1, ""), None, None)
        self.assertEqual(row.location, "Contoso › Teams › Annahme")
        self.assertEqual(row.location_parts, ("Contoso", "Teams", "Annahme"))
        self.assertIn("Contoso › Teams › Annahme", row.accessible_text)
        plain = result_row(
            PluginHit("fake", "Fake", replace(item, breadcrumbs=()), 1, ""), None, None
        )
        self.assertEqual(plain.location, item.subtitle)
        self.assertEqual(plain.location_parts, ())

    def test_a_plugin_row_shows_and_reads_out_its_first_chips(self):
        chips = tuple(Chip(f"Label {number}") for number in range(MAX_CHIPS + 1))
        row = result_row(PluginHit("fake", "Fake", replace(TEAM, chips=chips), 1, ""), None, None)
        self.assertEqual(row.chips, chips[:MAX_CHIPS])
        self.assertIn("Fake, Label 0, Label 1, Label 2\n", row.accessible_text)
        self.assertNotIn(f"Label {MAX_CHIPS}", row.accessible_text)


if __name__ == "__main__":
    unittest.main()
