"""The search syntax as results: when `?` asks for it, and what it lists."""

from __future__ import annotations

import unittest

from winlauncher.search.syntax_help import (
    FILTER_TIPS,
    HelpQuery,
    SyntaxTip,
    help_query,
    syntax_tips,
)
from winlauncher.websites import WebsiteShortcut

AMAZON = WebsiteShortcut("az", "Amazon", "https://www.amazon.de/s?k=%s")


class HelpQueryTest(unittest.TestCase):
    def test_only_a_lone_question_mark_or_one_before_a_space_asks_for_help(self):
        self.assertEqual(help_query("?"), HelpQuery())
        self.assertEqual(help_query("  ?  "), HelpQuery())
        self.assertEqual(help_query("? date  size"), HelpQuery(("date", "size")))
        for search in ("?eport", "report?", "a ? b", ""):
            with self.subTest(search=search):
                self.assertIsNone(help_query(search))

    def test_words_narrow_the_tips_down_ignoring_case(self):
        dates = syntax_tips(HelpQuery(("DATE",)), (), {})
        self.assertTrue(dates)
        self.assertTrue(all("date" in f"{tip.example} {tip.meaning}".casefold() for tip in dates))
        self.assertEqual(syntax_tips(HelpQuery(("nothing-like-this",)), (), {}), [])

    def test_all_filters_then_the_users_keywords(self):
        tips = syntax_tips(HelpQuery(), (AMAZON,), {"tg": "Telegram"})
        self.assertEqual(tips[: len(FILTER_TIPS)], list(FILTER_TIPS))
        self.assertEqual(
            tips[len(FILTER_TIPS) :],
            [
                SyntaxTip("az …", "Search Amazon", "az "),
                SyntaxTip("tg …", "Telegram first, then files; @tg Telegram only", "tg "),
            ],
        )
        self.assertEqual(syntax_tips(HelpQuery(("amazon",)), (AMAZON,), {}), [tips[-2]])


class PluginTipsTest(unittest.TestCase):
    def test_plugin_rows_come_last_and_are_found_by_their_words(self):
        units = SyntaxTip("5 km in mi", "Answer: Units", "5 km in mi")
        tips = syntax_tips(HelpQuery(), (), {}, [units])
        self.assertEqual(tips[-1], units)
        self.assertEqual(syntax_tips(HelpQuery(("units",)), (), {}, [units]), [units])


if __name__ == "__main__":
    unittest.main()
