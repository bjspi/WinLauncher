"""What the user configured for one plugin; stored in the settings file."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

REFRESH_RANGE_MINUTES = (1, 1440)
_KEYWORD = re.compile(r"[\w-]*")


@dataclass(frozen=True, slots=True)
class PluginConfig:
    id: str
    enabled: bool = False
    keyword: str = ""  # Typed first, it searches only this plugin: "tg anna". Empty: none.
    in_search: bool = True  # Its items also appear among the results of ordinary searches.
    refresh_minutes: int = 15
    boost: int = 0  # Lifts or lowers its items against files, like a folder priority.
    # The plugin's own options (`Plugin.options`), as JSON values.
    options: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("Each plugin entry needs an id.")
        if type(self.enabled) is not bool or type(self.in_search) is not bool:
            raise ValueError(f"Plugin {self.id}: enabled and in_search must be JSON booleans.")
        if not isinstance(self.keyword, str) or not _KEYWORD.fullmatch(self.keyword):
            raise ValueError(f"Plugin {self.id}: keywords may contain letters, numbers, _ and -.")
        for name in ("refresh_minutes", "boost"):
            if type(getattr(self, name)) is not int:
                raise ValueError(f"Plugin {self.id}: {name} must be an integer.")
        low, high = REFRESH_RANGE_MINUTES
        if not low <= self.refresh_minutes <= high:
            raise ValueError(f"Plugin {self.id}: refresh every {low} to {high} minutes.")
        if not isinstance(self.options, Mapping) or not all(
            isinstance(key, str) and type(value) in (bool, int, str)
            for key, value in self.options.items()
        ):
            raise ValueError(f"Plugin {self.id}: options must map names to simple values.")
        object.__setattr__(self, "options", dict(self.options))

    def __hash__(self) -> int:  # Options are a dict; equality still compares them.
        return hash((self.id, self.enabled, self.keyword, self.in_search, self.refresh_minutes))
