"""Independent search eligibility and sort-priority rules.

Search classes decide which pass may return an item. Numeric boosts decide where
an eligible item ranks. Exclusions always win over every priority rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pathscout.catalog.model import ItemKind, normalize_extension, path_key


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


NO_PRIORITY = PriorityDecision()


class PriorityInput(Protocol):
    """What priority rules read: a CatalogItem, or a search candidate row."""

    @property
    def identity(self) -> str: ...  # `path_key` of the item.
    @property
    def file_name(self) -> str: ...
    @property
    def extension(self) -> str: ...  # Normalized, without the dot.
    @property
    def kind(self) -> ItemKind: ...
    @property
    def attributes(self) -> int: ...


class PriorityPolicy:
    def __init__(
        self,
        folders: tuple[FolderPriority, ...] = (),
        names: tuple[NamePriority, ...] = (),
        extensions: tuple[ExtensionPriority, ...] = (),
        attributes: tuple[AttributePriority, ...] = (),
    ):
        self.folders = tuple((rule, path_key(rule.path)) for rule in folders)
        self.names = tuple((rule, _compile_name_rule(rule)) for rule in names)
        self.extensions: dict[str, list[ExtensionPriority]] = {}
        for rule in extensions:
            self.extensions.setdefault(normalize_extension(rule.extension), []).append(rule)
        self.attributes = attributes
        self.empty = not (folders or names or extensions or attributes)

    def evaluate(self, item: PriorityInput) -> PriorityDecision:
        if self.empty:
            return NO_PRIORITY  # Searches evaluate every candidate; skip the work.
        rules = self._matching_rules(item)
        classes = {SearchPriority(rule.search_priority) for rule in rules}
        search = next((c for c in _CLASS_PRECEDENCE if c in classes), SearchPriority.NORMAL)
        return PriorityDecision(search, sum(rule.weight for rule in rules))

    def _matching_rules(self, item: PriorityInput) -> list[PriorityRule]:
        rules: list[PriorityRule] = []
        if folder_rule := self._most_specific_folder(item):
            rules.append(folder_rule)
        rules.extend(self._name_rules(item))
        rules.extend(self._extension_rules(item))
        rules.extend(r for r in self.attributes if item.attributes & r.mask == r.mask)
        return rules

    def _most_specific_folder(self, item: PriorityInput) -> FolderPriority | None:
        matching = [rule for rule, key in self.folders if item.identity.startswith(key)]
        return max(matching, key=lambda rule: len(rule.path), default=None)

    def _name_rules(self, item: PriorityInput) -> list[NamePriority]:
        is_folder = item.kind == ItemKind.FOLDER
        return [
            rule
            for rule, pattern in self.names
            if (rule.folders if is_folder else rule.files) and pattern.search(item.file_name)
        ]

    def _extension_rules(self, item: PriorityInput) -> list[ExtensionPriority]:
        if item.kind == ItemKind.FOLDER or not self.extensions:
            return []
        return self.extensions.get(item.extension, [])


def _compile_name_rule(rule: NamePriority) -> re.Pattern[str]:
    try:
        return re.compile(rule.pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"Invalid filename priority expression: {exc}") from exc
