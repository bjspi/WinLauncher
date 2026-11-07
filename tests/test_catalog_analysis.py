"""The catalog analysis: what it suggests, and what each suggestion would cost."""

from __future__ import annotations

import unittest

from winlauncher.catalog.analysis import SuggestionKind, analyze
from winlauncher.catalog.model import PROJECT_ATTRIBUTE, FolderDepth, path_key
from winlauncher.catalog.priority import FolderPriority
from winlauncher.settings import Settings

ROOT = "D:\\Library"


def entry(relative: str, attributes: int = 0) -> tuple[str, str, int]:
    path = f"{ROOT}\\{relative}" if relative else ROOT
    return path, path_key(path), attributes


def settings(**changes) -> Settings:
    return Settings(roots=(ROOT,), index_apps=False, index_windows_tools=False, **changes)


def many(folder: str, count: int) -> list[tuple[str, str, int]]:
    return [entry(f"{folder}\\file{n}.txt") for n in range(count)]


class AnalysisTest(unittest.TestCase):
    def test_a_large_folder_gets_a_depth_as_deep_as_what_was_opened(self):
        catalog = [entry("Code"), entry("Code\\app"), *many("Code\\app\\src\\pkg", 2500)]
        opened = frozenset({path_key(f"{ROOT}\\Code\\app")})
        (depth, *_rest) = analyze(settings(), catalog, opened)
        self.assertEqual(depth.kind, SuggestionKind.DEPTH)
        self.assertEqual(
            (depth.path, depth.levels, depth.entries, depth.opened), (ROOT, 3, 2500, 0)
        )
        self.assertEqual(depth.title, f"List {ROOT} 3 levels deep")
        self.assertEqual(depth.detail, "leaves out 2,500 entries · nothing you opened")

    def test_a_folder_with_a_depth_or_few_entries_gets_none(self):
        catalog = many("Code\\app\\src\\pkg", 2500)
        limited = settings(folder_depths=(FolderDepth(ROOT, 3),))
        kinds = {s.kind for s in analyze(limited, catalog, frozenset())}
        self.assertNotIn(SuggestionKind.DEPTH, kinds)
        kinds = {s.kind for s in analyze(settings(), many("Code\\a\\b\\c", 100), frozenset())}
        self.assertNotIn(SuggestionKind.DEPTH, kinds)

    def test_unopened_large_folders_are_suggested_lower_once_each(self):
        catalog = [*many("Studium\\Bachelor", 1200), *many("Fotos", 300), *many("Work", 1500)]
        opened = frozenset({path_key(f"{ROOT}\\Work\\file1.txt")})
        lowered = [s for s in analyze(settings(), catalog, opened) if s.kind == "lower"]
        self.assertEqual([s.path.casefold() for s in lowered], [f"{ROOT}\\Studium".casefold()])
        already = settings(folder_priorities=(FolderPriority(f"{ROOT}\\Studium", -20, "low"),))
        lowered = [s for s in analyze(already, catalog, opened) if s.kind == "lower"]
        self.assertEqual(lowered, [])

    def test_projects_insides_suggest_the_top_level_switch(self):
        catalog = [entry("app", PROJECT_ATTRIBUTE), *many("app\\src\\deep", 1100)]
        suggestions = analyze(settings(), catalog, frozenset())
        projects = [s for s in suggestions if s.kind == SuggestionKind.PROJECTS]
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0].entries, 1100)
        switched = settings(code_projects_top_level=True)
        kinds = {s.kind for s in analyze(switched, catalog, frozenset())}
        self.assertNotIn(SuggestionKind.PROJECTS, kinds)

    def test_suggestions_costing_nothing_come_first(self):
        catalog = [*many("Old\\a\\b\\c", 3000), *many("Keep\\x\\y\\z", 2500)]
        opened = frozenset({path_key(f"{ROOT}\\Keep\\x\\y\\z\\file1.txt")})
        suggestions = analyze(settings(), catalog, opened)
        self.assertEqual(
            [s.opened > 0 for s in suggestions], sorted(s.opened > 0 for s in suggestions)
        )


if __name__ == "__main__":
    unittest.main()
