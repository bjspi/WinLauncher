"""Size comparisons and partial dates retain precise inclusive byte/day bounds."""

from __future__ import annotations

import unittest

from pathscout.search.query_parser import parse_query


class QueryFiltersTest(unittest.TestCase):
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
