"""Listary-style query syntax: date periods, size ranges, folder shorthand,
excluded extensions, exact phrases and `-` exclusions."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from winlauncher.search.query_parser import (
    DAYS_IN_RECENT_MONTH,
    DAYS_IN_RECENT_WEEK,
    DAYS_IN_RECENT_YEAR,
    QuerySpan,
    SpanRole,
    parse_query,
    query_spans,
)

TODAY = date(2026, 10, 20)


def midnight(day: date) -> float:
    return datetime.combine(day, datetime.min.time()).timestamp()


def bounds(text: str, today: date = TODAY) -> tuple[float | None, float | None]:
    query = parse_query(text, today=today)
    return query.modified_after, query.modified_before


class DateFilterTest(unittest.TestCase):
    def test_relative_periods_start_at_local_midnight_and_end_open(self):
        for value, first in (
            ("today", TODAY),
            ("TODAY", TODAY),
            ("week", TODAY - timedelta(days=DAYS_IN_RECENT_WEEK - 1)),
            ("month", TODAY - timedelta(days=DAYS_IN_RECENT_MONTH - 1)),
            ("year", TODAY - timedelta(days=DAYS_IN_RECENT_YEAR - 1)),
        ):
            with self.subTest(value=value):
                self.assertEqual(bounds(f"date:{value}"), (midnight(first), None))
        self.assertEqual(bounds("date:week")[0], midnight(date(2026, 10, 14)))
        self.assertEqual(bounds("date:month")[0], midnight(date(2026, 9, 21)))
        self.assertEqual(bounds("date:year")[0], midnight(date(2025, 10, 21)))
        self.assertEqual(bounds("date:yesterday"), (midnight(date(2026, 10, 19)), midnight(TODAY)))

    def test_relative_dates_keep_one_history_key_from_day_to_day(self):
        tomorrow = TODAY + timedelta(days=1)
        for value in ("today", "yesterday", "week", "month", "year"):
            with self.subTest(value=value):
                typed_today = parse_query(f"report date:{value}", today=TODAY)
                typed_tomorrow = parse_query(f"report date:{value}", today=tomorrow)
                self.assertNotEqual(typed_today, typed_tomorrow)
                self.assertEqual(typed_today.history_key, typed_tomorrow.history_key)
                self.assertEqual(typed_today.history_key, parse_query("report").history_key)
        self.assertIsNotNone(parse_query("date:today").modified_after)  # Defaults to today.

    def test_periods_cover_the_whole_year_month_or_day(self):
        for value, first, after_last in (
            ("2026", date(2026, 1, 1), date(2027, 1, 1)),
            ("2026-07", date(2026, 7, 1), date(2026, 8, 1)),
            ("2026-07*", date(2026, 7, 1), date(2026, 8, 1)),
            ("2024-02", date(2024, 2, 1), date(2024, 3, 1)),
            ("2026-07-15", date(2026, 7, 15), date(2026, 7, 16)),
            ("20260715", date(2026, 7, 15), date(2026, 7, 16)),
            ("2026-12-31", date(2026, 12, 31), date(2027, 1, 1)),
        ):
            with self.subTest(value=value):
                self.assertEqual(bounds(f"date:{value}"), (midnight(first), midnight(after_last)))
        self.assertEqual(bounds("date:2026-07"), bounds("after:2026-07 before:2026-07"))

    def test_ranges_run_from_the_first_period_through_the_last(self):
        self.assertEqual(
            bounds("date:2026-01..2026-06"),
            (midnight(date(2026, 1, 1)), midnight(date(2026, 7, 1))),
        )
        self.assertEqual(
            bounds("date:2025..2026-02-14"),
            (midnight(date(2025, 1, 1)), midnight(date(2026, 2, 15))),
        )
        self.assertEqual(bounds("date:2026-03..2026-03"), bounds("date:2026-03"))

    def test_comparisons_use_the_start_or_end_of_the_period(self):
        for text, expected in (
            ("date:>2026-07", (midnight(date(2026, 8, 1)), None)),
            ("date:>=2026-07", (midnight(date(2026, 7, 1)), None)),
            ("date:<2026-07", (None, midnight(date(2026, 7, 1)))),
            ("date:<=2026-07", (None, midnight(date(2026, 8, 1)))),
            ("date:>2026", (midnight(date(2027, 1, 1)), None)),
            ("date:<=2026-07-15", (None, midnight(date(2026, 7, 16)))),
        ):
            with self.subTest(text=text):
                self.assertEqual(bounds(text), expected)

    def test_date_filters_intersect_with_each_other_and_after_before(self):
        self.assertEqual(
            bounds("date:2026 date:>=2026-03 before:2026-05"),
            (midnight(date(2026, 3, 1)), midnight(date(2026, 6, 1))),
        )
        self.assertEqual(bounds("after:2026-03 date:2026"), bounds("date:2026-03..2026-12"))
        self.assertEqual(bounds("date:2026-03 after:2025"), bounds("date:2026-03"))
        self.assertEqual(
            bounds("date:week date:<=2026-10-15"),
            (midnight(date(2026, 10, 14)), midnight(date(2026, 10, 16))),
        )

    def test_invalid_or_empty_dates_report_validation_errors(self):
        for value in (
            "",
            "soon",
            "2026-13",
            "2026-02-30",
            ">",
            ">=yesterday",
            "=>2026",
            "2026..",
            "..2026",
            "2026...2027",
            "2026-01..x",
        ):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "^date: expects"):
                parse_query(f"date:{value}", today=TODAY)
        for text in ("date:2026-07..2026-01", "date:2026 date:2025", "date:<2026 after:2026"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "empty range"):
                parse_query(text, today=TODAY)
        with self.assertRaisesRegex(ValueError, "supported range"):
            parse_query("date:9999-12")
        with self.assertRaisesRegex(ValueError, "after: expects a date"):
            parse_query("after:today")  # Relative names belong to `date:` only.


class SizeRangeTest(unittest.TestCase):
    def test_ranges_are_inclusive_and_intersect_with_comparisons(self):
        for text, lower, upper in (
            ("size:10mb..1gb", 10 * 1024**2, 1024**3),
            ("SIZE:1KB..1KB", 1024, 1024),
            ("size:1.5kb..2,5kb", 1536, 2560),
            ("size:0.5b..1.5b", 1, 1),
            ("size:0..9223372036854775807", 0, 2**63 - 1),
            ("size: 1kb..2kb", 1024, 2048),
            ("size:1kb..1gb size:<2kb", 1024, 2047),
            ("size:>1500 size:1kb..1mb", 1501, 1024**2),
        ):
            with self.subTest(text=text):
                query = parse_query(text)
                self.assertEqual((query.min_size, query.max_size), (lower, upper))

    def test_invalid_ranges_report_size_errors(self):
        for text in (
            "size:1gb",
            "size:1gb..",
            "size:..1gb",
            "size:1xb..2mb",
            "size:>1kb..2kb",
            "size:1kb...2kb",
            "size:0.4b..0.6b",
            "size:2mb..1mb",
            "size:1kb..2kb size:>2kb",
        ):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "size"):
                parse_query(text)


class FolderShorthandTest(unittest.TestCase):
    def test_relative_trailing_separator_names_ordered_parent_folders(self):
        for text, parts, terms in (
            (r"rechnungen\ 2024", ("rechnungen",), ("2024",)),
            ("rechnungen/ 2024", ("rechnungen",), ("2024",)),
            (r"work\client\ proposal", ("work", "client"), ("proposal",)),
            (r"Work\ Client/ proposal", ("work", "client"), ("proposal",)),
            (r'"Annual Reports\" q1', ("annual reports",), ("q1",)),
            (r"path:a b\ c", ("a", "b"), ("c",)),
            (r"rep*\ x", ("rep*",), ("x",)),
        ):
            with self.subTest(text=text):
                query = parse_query(text)
                self.assertEqual((query.folder_parts, query.terms), (parts, terms))
                self.assertIsNone(query.scope)
        self.assertEqual(
            parse_query(r"work\client\ proposal"), parse_query("path:work/client proposal")
        )

    def test_absolute_folder_within_search_scopes_like_in(self):
        explorer = Path(r"C:\Explorer")
        for text, scope in (
            ("report D:\\", Path("D:\\")),
            ("report D:\\Work\\", Path(r"D:\Work")),
            (r'"D:\Work Projects\" report', Path(r"D:\Work Projects")),
            ("report \\\\server\\share\\Team\\", Path(r"\\server\share\Team")),
            ("report D:/Work/", Path(r"D:\Work")),
        ):
            with self.subTest(text=text):
                query = parse_query(text, scope=explorer)
                self.assertEqual(query.scope, scope)
                self.assertEqual((query.terms, query.folder_parts), (("report",), ()))
        self.assertEqual(parse_query("report D:\\Work\\"), parse_query(r'in:"D:\Work" report'))

    def test_relative_paths_and_bare_separators_stay_text(self):
        for text, terms in (
            (r"C:Work", (r"c:work",)),
            (r"\Work", (r"\work",)),
            (r"\\server", (r"\\server",)),
            ("\\", ("\\",)),
            ("//", ("//",)),
            ("a/b", ("a/b",)),
        ):
            with self.subTest(text=text):
                query = parse_query(text)
                self.assertEqual(query.terms, terms)
                self.assertEqual((query.folder_parts, query.scope), ((), None))

    def test_leading_absolute_paths_use_the_whole_input_for_navigation(self) -> None:
        for text in (
            "C:\\",
            r"C:\Work",
            r"C:\Work report",
            r"C:\Work\ report ext:pdf",
            "D:/Work Projects/",
            r"\\server\share",
            "\\\\server\\share\\",
            r"\\server\share\Work Projects\report.pdf",
        ):
            with self.subTest(text=text):
                query = parse_query(f"  {text}  ", scope=Path(r"C:\Explorer"))
                self.assertEqual(query.navigation, text)
                self.assertEqual(query.terms, ())
                self.assertEqual(query.extensions, ())
                self.assertIsNone(query.scope)
                self.assertEqual(query_spans(text), ())


class ExclusionTest(unittest.TestCase):
    def test_excluded_extensions_from_lists_and_star_dot_shorthand(self):
        for text, extensions in (
            ("!ext:tmp", ("tmp",)),
            ("!ext:tmp,bak", ("tmp", "bak")),
            ("!ext:.TMP;bak", ("tmp", "bak")),
            ("!ext:(tmp, bak)", ("tmp", "bak")),
            ("-ext:pdf", ("pdf",)),
            ("!ext:doc*", ("doc*",)),
            ("!*.bak", ("bak",)),
            ("-*.BAK", ("bak",)),
            ("!*.bak !ext:bak,tmp", ("bak", "tmp")),
        ):
            with self.subTest(text=text):
                query = parse_query("report " + text)
                self.assertEqual(query.excluded_extensions, extensions)
                self.assertEqual((query.terms, query.excluded_terms), (("report",), ()))
        for text in ("!ext:", "-ext:()", "!ext:(tmp"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "ext:"):
                parse_query(text)

    def test_other_exclusions_stay_literal_path_substrings(self):
        for text, excluded in (
            ("!archive", "archive"),
            ("-archive", "archive"),
            ('-"old version"', "old version"),
            ('!"Old Version"', "old version"),
            ("!*.tar.gz", "*.tar.gz"),
            ("!*.b?k", "*.b?k"),
            ("!*.", "*."),
            ("!*bak", "*bak"),
            ("!a*.bak", "a*.bak"),
            ("!type:file", "type:file"),
            ("--draft", "-draft"),
            (r"!build\ ", "build\\"),
        ):
            with self.subTest(text=text):
                query = parse_query(text)
                self.assertEqual(query.excluded_terms, (excluded,))
                self.assertEqual((query.excluded_extensions, query.terms), ((), ()))

    def test_dash_inside_or_alone_is_ordinary_text(self):
        self.assertEqual(parse_query("my-file - x-").terms, ("my-file", "-", "x-"))
        self.assertEqual(parse_query("my-file").excluded_terms, ())
        self.assertEqual(parse_query("! -").terms, ("!", "-"))


class ExactPhraseTest(unittest.TestCase):
    def test_quoted_terms_are_exact_and_unquoted_terms_stay_fuzzy(self):
        query = parse_query('"Project Plan" draft "rpt"')
        self.assertEqual(query.terms, ("project plan", "draft", "rpt"))
        self.assertEqual(query.exact_terms, ("project plan", "rpt"))
        self.assertTrue(query.uses_fuzzy_pass)
        self.assertFalse(parse_query('"rpt" "vsc"').uses_fuzzy_pass)
        self.assertEqual(parse_query("rpt").exact_terms, ())
        self.assertEqual(parse_query('report"s x"').exact_terms, ("reports x",))
        self.assertEqual(parse_query('"rpt"').history_key, parse_query("rpt").history_key)

    def test_quoted_filter_values_and_exclusions_are_not_exact_terms(self):
        for text in (
            r'in:"D:\a b"',
            'contains:"x y"',
            'name:"annual report"',
            'path:"a b"',
            '-"old version"',
            '""',
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_query(text).exact_terms, ())

    def test_an_unclosed_quote_is_still_rejected(self):
        for text in ('"project plan', 'report -"old', 'x "y" "z'):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "quotation mark"):
                parse_query(text)


class ListSeparatorTest(unittest.TestCase):
    def test_semicolons_separate_extensions_like_commas(self):
        for text in ("ext:pdf;docx", "ext:pdf,docx", "ext:(pdf; docx)", "ext:pdf;;docx;"):
            with self.subTest(text=text):
                self.assertEqual(parse_query(text).extensions, ("pdf", "docx"))


class QuerySpansTest(unittest.TestCase):
    def spans(self, text: str) -> list[tuple[str, SpanRole, bool]]:
        return [(text[s.start : s.end], s.role, s.valid) for s in query_spans(text)]

    def test_new_syntax_is_colored_as_the_parser_reads_it(self):
        filter_, exclusion = SpanRole.FILTER, SpanRole.EXCLUSION
        text = (
            r"report date:week rechnungen\ D:\Work\ -old !ext:(tmp, bak) -*.bak "
            r'-"old version" size:1mb..2mb my-file C:\Work'
        )
        self.assertEqual(
            self.spans(text),
            [
                ("date:week", filter_, True),
                ("rechnungen\\", filter_, True),
                ("D:\\Work\\", filter_, True),
                ("-old", exclusion, True),
                ("!ext:(tmp, bak)", exclusion, True),
                ("-*.bak", exclusion, True),
                ('-"old version"', exclusion, True),
                ("size:1mb..2mb", filter_, True),
            ],
        )

    def test_invalid_new_values_are_flagged(self):
        for text, role in (
            ("date:soon", SpanRole.FILTER),
            ("date:2026-07..2026-01", SpanRole.FILTER),
            ("size:2mb..1mb", SpanRole.FILTER),
            ("size:1gb", SpanRole.FILTER),
            ("!ext:", SpanRole.EXCLUSION),
            ("-ext:(tmp", SpanRole.EXCLUSION),
            ('-"old', SpanRole.EXCLUSION),
        ):
            with self.subTest(text=text):
                self.assertEqual(query_spans(text), (QuerySpan(0, len(text), role, False),))

    def test_ordinary_text_and_partial_input_are_never_spans_and_never_raise(self):
        for text in ("-", "my-file", "\\", '"project plan"', r"C:\Work", '"open', "a/b"):
            with self.subTest(text=text):
                self.assertEqual(query_spans(text), ())
        for text in ("date:", "date:>", "size:1..", "-", "!", "-ext:(", '-"', "D:\\", "~\\x\\"):
            with self.subTest(text=text):
                query_spans(text)
                query_spans("report " + text)


if __name__ == "__main__":
    unittest.main()
