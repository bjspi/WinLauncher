"""Query aliases and size/date filters keep their canonical meaning and bounds."""

from __future__ import annotations

import unittest

from winlauncher.search.query_parser import (
    QuerySpan,
    SpanRole,
    parse_query,
    query_spans,
    without_type_filter,
)


class QueryFiltersTest(unittest.TestCase):
    def test_extension_lists_patterns_and_history_are_normalized(self):
        for text, expected in (
            ("ext:doc,docx", ("doc", "docx")),
            ("ext:(doc,docx)", ("doc", "docx")),
            ("EXT:( .JPG, PNG ) ext:.jpg", ("jpg", "png")),
            ("ext:doc*", ("doc*",)),
            ("ext:(doc?,[jp]pg,p[!d]f)", ("doc?", "[jp]pg", "p[!d]f")),
            ("ext:(.doc*, pdf) ext:txt", ("doc*", "pdf", "txt")),
        ):
            with self.subTest(text=text):
                query = parse_query("report " + text)
                self.assertEqual(query.extensions, expected)
                self.assertEqual(query.terms, ("report",))
        self.assertEqual(
            parse_query("ext:(jpg, png)").history_key,
            parse_query("ext:png,jpg").history_key,
        )
        for text in ("ext:", "ext:()", "ext:( )", "ext:(jpg,png"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "ext:"):
                parse_query(text)

    def test_query_spans_mark_filters_as_the_parser_reads_them(self):
        def spans(text: str) -> list[tuple[str, SpanRole]]:
            found = query_spans(text)
            self.assertTrue(all(span.valid for span in found), text)
            return [(text[span.start : span.end], span.role) for span in found]

        filter_, content, exclusion = SpanRole.FILTER, SpanRole.CONTENT, SpanRole.EXCLUSION
        self.assertEqual(
            spans("report contains:todo size: > 10mb !old ext:(jpg, png)"),
            [
                ("contains:todo", content),
                ("size: > 10mb", filter_),
                ("!old", exclusion),
                ("ext:(jpg, png)", filter_),
            ],
        )
        self.assertEqual(
            spans(r'in:"D:\Work Projects" RG:"two words" after:2026-02*'),
            [
                (r'in:"D:\Work Projects"', filter_),
                ('RG:"two words"', content),
                ("after:2026-02*", filter_),
            ],
        )
        # The parser rejects these values; the field marks them once the cursor leaves.
        for text, role in (
            ("size:", filter_),
            ("after:2026-13", filter_),
            ("ext:(jpg", filter_),
            (r'in:"D:\Wo', filter_),
            ("type:x", filter_),
            ("contains:", content),
        ):
            with self.subTest(text=text):
                self.assertEqual(query_spans(text), (QuerySpan(0, len(text), role, False),))
        for text in (r"C:\Work t", "report", "! alone", "%TEMP%", "~/Projects size:>1kb"):
            with self.subTest(text=text):
                self.assertEqual(spans(text), [])

    def test_type_aliases_share_kind_history_and_ui_replacement(self):
        for prefix in ("type", "t", "T"):
            for value, canonical in (
                ("file", "file"),
                ("folder", "folder"),
                ("dir", "folder"),
                ("DIR", "folder"),
                ("app", "app"),
            ):
                with self.subTest(prefix=prefix, value=value):
                    text = f'report {prefix}:{value} contains:"API key"'
                    query = parse_query(text)
                    expected = parse_query(f'report type:{canonical} contains:"API key"')
                    self.assertEqual(query, expected)
                    self.assertEqual(query.history_key, expected.history_key)
                    self.assertEqual(
                        parse_query(without_type_filter(text)),
                        parse_query('report contains:"API key"'),
                    )
        self.assertEqual(parse_query("type:file t:dir"), parse_query("type:folder"))
        self.assertEqual(parse_query("t:dir type:app"), parse_query("type:app"))
        self.assertEqual(without_type_filter("t typed:dir"), "t typed:dir")
        text = r"report T:\Work t:dir"
        self.assertEqual(parse_query(text), parse_query(r"report T:\Work type:folder"))
        self.assertEqual(without_type_filter(text), r"report T:\Work")
        for text in ("t:", "t:unknown"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "t: expects"):
                parse_query(text)

    def test_type_filter_buttons_preserve_literal_navigation_text(self) -> None:
        for text in (r"T:\Work t:dir", r"C:\My Projects\ type:file", "~/My Folder t:dir"):
            with self.subTest(text=text):
                self.assertEqual(without_type_filter(text), text)

    def test_size_units_decimals_spacing_and_strict_boundaries(self):
        for text, lower, upper in (
            ("size:>5kb", 5121, None),
            ("SIZE:<5KBYTE", None, 5119),
            ("size: >= 1.5mb", 1572864, None),
            ("size:<=1,5mbyte", None, 1572864),
            ("size:> 2GB", 2147483649, None),
            ("size:<1gbytes", None, 1073741823),
            ("size:>=1tbyte", 1099511627776, None),
            ("size:>=1KiB", 1024, None),
            ("size:<=0bytes", None, 0),
            ("size:<1", None, 0),
            ("size:>1.1b", 2, None),
            ("size:>=1.1b", 2, None),
            ("size:<1.1byte", None, 1),
            ("size:<=1.1byte", None, 1),
            ("size:>0.9999999999999999999999999999999999b", 1, None),
            ("size:<=9223372036854775807", None, 2**63 - 1),
        ):
            with self.subTest(text=text):
                query = parse_query(text)
                self.assertEqual((query.min_size, query.max_size), (lower, upper))
                self.assertFalse(query.terms)
                self.assertFalse(query.uses_learned_items_only)
        query = parse_query("report size:>1kb size:>2kb size:<5kb size:<4kb")
        self.assertEqual((query.min_size, query.max_size), (2049, 4095))
        self.assertEqual(query.terms, ("report",))

    def test_invalid_or_empty_size_ranges_report_validation_errors(self):
        for text in (
            "size:",
            "size:>",
            "size:5kb",
            "size:>-1mb",
            "size:>NaN",
            "size:>1xb",
            "size:>1mbjunk",
            "size:<0",
            "size:>9223372036854775807",
            "size:>=9223372036854775808",
            "size:>5kb size:<5kb",
        ):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "size"):
                parse_query(text)

    def test_partial_dates_expand_to_start_or_end_of_calendar_period(self):
        for partial, start, end in (
            ("2026", "2026-01-01", "2026-12-31"),
            ("2026-", "2026-01-01", "2026-12-31"),
            ("2026-*", "2026-01-01", "2026-12-31"),
            ("2026-02", "2026-02-01", "2026-02-28"),
            ("2026-02*", "2026-02-01", "2026-02-28"),
            ("2026-02-", "2026-02-01", "2026-02-28"),
            ("2024-02*", "2024-02-01", "2024-02-29"),
            ("2026-02-15", "2026-02-15", "2026-02-15"),
        ):
            with self.subTest(partial=partial):
                self.assertEqual(parse_query(f"after:{partial}"), parse_query(f"after:{start}"))
                self.assertEqual(parse_query(f"before:{partial}"), parse_query(f"before:{end}"))
        for value in ("2026-00*", "2026-13*", "2026-02-30", "2026--", "2026-02**"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "expects a date"):
                parse_query(f"after:{value}")
