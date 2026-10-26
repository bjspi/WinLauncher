"""What the user configures for plugins: each plugin's options and the plugin sources."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

REFRESH_RANGE_MINUTES = (1, 1440)
KEYWORD = re.compile(r"[\w-]+")  # Also checked for the keywords a plugin suggests.
# Where plugins come from (see `winlauncher.plugins.sources`).
DEFAULT_SOURCES = ("https://github.com/bjspi/WinLauncher-Plugins.git",)


def source_key(source: str) -> str:
    """`source` without the differences that name the same place, for comparisons."""
    key = source.strip().rstrip("/\\").casefold()
    return key.removesuffix(".git")


@dataclass(frozen=True, slots=True)
class PluginConfig:
    id: str
    # Plugins load only once installed or activated, so a loaded plugin starts out on.
    enabled: bool = True
    # Typed first, each puts this plugin's items on top: "tg anna". Empty: none.
    keywords: tuple[str, ...] = ()
    # Keywords per route (`Plugin.routes`), each searching only that route's items.
    routes: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
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
        object.__setattr__(self, "keywords", self._keywords(self.keywords))
        if not isinstance(self.routes, Mapping) or not all(
            isinstance(route, str) for route in self.routes
        ):
            raise ValueError(f"Plugin {self.id}: routes must map route ids to keywords.")
        routes = {route: self._keywords(words) for route, words in self.routes.items()}
        object.__setattr__(self, "routes", routes)
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

    def _keywords(self, words: object) -> tuple[str, ...]:
        """`words` as a tuple of valid keywords; JSON stores them as a list."""
        if not isinstance(words, list | tuple) or not all(
            isinstance(word, str) and KEYWORD.fullmatch(word) for word in words
        ):
            raise ValueError(f"Plugin {self.id}: keywords may contain letters, numbers, _ and -.")
        return tuple(words)

    def keyword_routes(self) -> dict[str, str | None]:
        """Every keyword of this plugin and the route it searches; None for all items."""
        found: dict[str, str | None] = {keyword: None for keyword in self.keywords}
        for route, keywords in self.routes.items():
            found.update(dict.fromkeys(keywords, route))
        return found

    def __hash__(self) -> int:  # Options and routes are dicts; equality still compares them.
        return hash((self.id, self.enabled, self.keywords, self.in_search, self.refresh_minutes))
