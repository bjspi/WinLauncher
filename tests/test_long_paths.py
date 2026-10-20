"""Paths beyond MAX_PATH are indexed and searched without the extended prefix leaking."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from pathscout.catalog import long_paths
from pathscout.catalog.indexer import CatalogIndexer
from pathscout.catalog.store import CatalogStore
from pathscout.search.query_parser import parse_query
from pathscout.search.service import SearchService
from pathscout.settings import Settings

PREFIX = "\\\\?\\"


class LongPathTextTest(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows path rules")
    def test_long_paths_use_the_extended_form_and_convert_back(self):
        short = r"C:\Work\report.txt"
        local = "C:\\" + "\\".join(["folder"] * 40)
        unc = "\\\\server\\share\\" + "\\".join(["folder"] * 40)
        self.assertEqual(long_paths.os_path(short), short)
        self.assertEqual(long_paths.os_path(local), PREFIX + local)
        self.assertEqual(long_paths.os_path(local.replace("\\", "/")), PREFIX + local)
        self.assertEqual(long_paths.os_path(unc), PREFIX + "UNC\\" + unc[2:])
        self.assertEqual(long_paths.os_path(PREFIX + local), PREFIX + local)
        for path in (short, local, unc):
            with self.subTest(path=path[:20]):
                self.assertEqual(long_paths.ordinary_path(long_paths.os_path(path)), Path(path))


def _reject_long_ordinary_paths(function):
    """Behave like Windows without system-wide long path support."""

    def checked(path, *args, **kwargs):
        text = os.fspath(path) if not isinstance(path, int) else ""
        if len(text) >= 260 and not text.startswith(PREFIX):
            raise FileNotFoundError(3, "The system cannot find the path specified", text)
        return function(path, *args, **kwargs)

    return checked


@unittest.skipUnless(os.name == "nt", "Windows MAX_PATH")
class LongPathIndexTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.mkdtemp(prefix="pathscout-long-")
        self.addCleanup(shutil.rmtree, PREFIX + temporary)
        self.base = Path(temporary).resolve()
        self.root = self.base / "library"
        deep = self.root
        os.mkdir(self.root)
        for index in range(6):
            deep = deep / f"{index}-{'nested folder' * 4}"
            os.mkdir(PREFIX + str(deep))
        self.deep = deep
        self.file = deep / "deep-report.txt"
        with open(PREFIX + str(self.file), "w", encoding="utf-8") as handle:
            handle.write("needle")
        self.assertGreater(len(str(self.file)), 300)
        self.store = CatalogStore(self.base / "catalog.sqlite3")
        self.settings = Settings(roots=(str(self.root),), index_apps=False)

    def old_windows(self) -> ExitStack:
        stack = ExitStack()
        for name in ("stat", "lstat", "scandir"):
            original = getattr(os, name)
            stack.enter_context(patch.object(os, name, _reject_long_ordinary_paths(original)))
        return stack

    def test_deep_files_are_indexed_searched_and_stored_without_prefix(self):
        with self.old_windows():
            report = CatalogIndexer(self.store).scan(self.settings)
            self.assertFalse(report.errors, report.errors)
            for engine in ("python", "auto"):
                with (
                    self.subTest(engine=engine),
                    patch.dict(os.environ, PATHSCOUT_SEARCH_ENGINE=engine),
                ):
                    hits = SearchService(self.store).search(
                        parse_query("deep-report"), self.settings
                    )
                    self.assertEqual([hit.item.path for hit in hits], [self.file])
        with self.store.connect() as connection:
            paths = [row[0] for row in connection.execute("SELECT path FROM entries")]
        self.assertTrue(paths)
        self.assertFalse([path for path in paths if path.startswith(PREFIX)])

    def test_explorer_search_and_markers_in_deep_folders(self):
        marked = self.deep / "private"
        os.mkdir(PREFIX + str(marked))
        open(PREFIX + str(marked / ".nobackup"), "w").close()
        open(PREFIX + str(marked / "secret.txt"), "w").close()
        with self.old_windows():
            CatalogIndexer(self.store).scan(self.settings)
            service = SearchService(self.store)
            self.assertFalse(service.search(parse_query("secret"), self.settings))
            query = replace(parse_query("deep-rep", scope=self.deep), explorer=True)
            hits = service.search(query, self.settings)
            self.assertEqual([hit.item.path for hit in hits], [self.file])


if __name__ == "__main__":
    unittest.main()
