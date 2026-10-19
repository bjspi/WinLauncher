"""Independent search eligibility and sort-priority rules.

Search classes decide which pass may return an item. Numeric boosts decide where
an eligible item ranks. Exclusions always win over every priority rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from pathscout.catalog.model import CatalogItem, ItemKind, is_within, normalize_extension


class SearchPriority(StrEnum):
    IGNORED = "ignored"
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


# When several rules match, a demotion is never undone by a promotion.
_CLASS_PRECEDENCE = (SearchPriority.IGNORED, SearchPriority.LOW, SearchPriority.HIGH)


@dataclass(frozen=True, slots=True)
class FolderPriority:
    path: str
    weight: int = 20
    search_priority: str = SearchPriority.NORMAL


@dataclass(frozen=True, slots=True)
class NamePriority:
    pattern: str
    weight: int = 0
    search_priority: str = SearchPriority.NORMAL
    files: bool = True
    folders: bool = True


@dataclass(frozen=True, slots=True)
class ExtensionPriority:
    extension: str
    weight: int = 0
    search_priority: str = SearchPriority.NORMAL


@dataclass(frozen=True, slots=True)
class AttributePriority:
    mask: int
    weight: int = 0
    search_priority: str = SearchPriority.NORMAL


PriorityRule = FolderPriority | NamePriority | ExtensionPriority | AttributePriority


@dataclass(frozen=True, slots=True)
class PriorityDecision:
    search: SearchPriority = SearchPriority.NORMAL
    boost: int = 0


class PriorityPolicy:
    def __init__(
        self,
        folders: tuple[FolderPriority, ...] = (),
        names: tuple[NamePriority, ...] = (),
        extensions: tuple[ExtensionPriority, ...] = (),
        attributes: tuple[AttributePriority, ...] = (),
    ):
        self.folders = folders
        self.names = tuple((rule, _compile_name_rule(rule)) for rule in names)
        self.extensions: dict[str, list[ExtensionPriority]] = {}
        for rule in extensions:
            self.extensions.setdefault(normalize_extension(rule.extension), []).append(rule)
        self.attributes = attributes

    def evaluate(self, item: CatalogItem) -> PriorityDecision:
        rules = self._matching_rules(item)
        classes = {SearchPriority(rule.search_priority) for rule in rules}
        search = next((c for c in _CLASS_PRECEDENCE if c in classes), SearchPriority.NORMAL)
        return PriorityDecision(search, sum(rule.weight for rule in rules))

    def _matching_rules(self, item: CatalogItem) -> list[PriorityRule]:
        rules: list[PriorityRule] = []
        if folder_rule := self._most_specific_folder(item):
            rules.append(folder_rule)
        rules.extend(self._name_rules(item))
        rules.extend(self._extension_rules(item))
        rules.extend(r for r in self.attributes if item.attributes & r.mask == r.mask)
        return rules

    def _most_specific_folder(self, item: CatalogItem) -> FolderPriority | None:
        matching = [rule for rule in self.folders if is_within(item.path, rule.path)]
        return max(matching, key=lambda rule: len(rule.path), default=None)

    def _name_rules(self, item: CatalogItem) -> list[NamePriority]:
        is_folder = item.kind == ItemKind.FOLDER
        return [
            rule
            for rule, pattern in self.names
            if (rule.folders if is_folder else rule.files) and pattern.search(item.path.name)
        ]

    def _extension_rules(self, item: CatalogItem) -> list[ExtensionPriority]:
        if item.kind == ItemKind.FOLDER or not self.extensions:
            return []
        return self.extensions.get(normalize_extension(item.path.suffix), [])


def _compile_name_rule(rule: NamePriority) -> re.Pattern[str]:
    try:
        return re.compile(rule.pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"Invalid filename priority expression: {exc}") from exc
