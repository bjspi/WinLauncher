"""Accents never hide a match; what is spelled as typed ranks higher."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from winlauncher.catalog.indexer import CatalogIndexer
from winlauncher.catalog.model import fold_case, normalize
from winlauncher.catalog.store import CatalogStore
from winlauncher.search.query_parser import parse_query
from winlauncher.search.service import SearchService
from winlauncher.settings import Settings

FILES = (
    "Lohn.txt",
    "Löhne 2024.pdf",
    "Arztbericht.pdf",
    "Ärztebericht.pdf",
    "Müller/Bericht.txt",
    "Muller/Notiz.txt",
    "Café.md",
)


class AccentTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="winlauncher-accents-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "library"
        for name in FILES:
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text("test", encoding="utf-8")
        self.store = CatalogStore(Path(temporary.name) / "catalog.sqlite3")
        self.settings = Settings(roots=(str(root),), index_apps=False, index_windows_tools=False)
        CatalogIndexer(self.store).scan(self.settings)
        self.search = SearchService(self.store)

    def names(self, text: str) -> list[str]:
        """Result names in ranked order."""
        return [hit.item.path.name for hit in self.search.search(parse_query(text), self.settings)]

    def test_search_text_has_no_accents_and_the_query_keeps_the_typed_spelling(self):
        self.assertEqual(normalize("LÖHNE"), "lohne")
        self.assertEqual(fold_case("LÖHNE"), "löhne")
        self.assertEqual(normalize("ø"), "ø")  # A letter of its own, not an accented o.
        query = parse_query("Löhn name:Café 2024")
        self.assertEqual(query.terms, ("lohn", "cafe", "2024"))
        self.assertEqual(query.spellings, ("löhn", "café", "2024"))

    def test_plain_letters_find_accents_just_behind_the_same_spelling(self):
        self.assertEqual(self.names("lohn"), ["Lohn.txt", "Löhne 2024.pdf"])
        self.assertEqual(self.names("cafe"), ["Café.md"])

    def test_typed_accents_rank_their_spelling_first_and_still_find_the_rest(self):
        self.assertEqual(self.names("löhn"), ["Löhne 2024.pdf", "Lohn.txt"])
        self.assertEqual(self.names("LÖHNE"), ["Löhne 2024.pdf"])
        self.assertEqual(self.names("ärzte")[:2], ["Ärztebericht.pdf", "Arztbericht.pdf"])

    def test_folder_parts_match_with_and_without_accents(self):
        self.assertEqual(sorted(self.names("müller/ txt")), ["Bericht.txt", "Notiz.txt"])


if __name__ == "__main__":
    unittest.main()
