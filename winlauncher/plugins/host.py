"""Keep each plugin's latest items, refresh them, search them and open them.

Thread-safe and free of Qt: refreshes run on a plugin worker, searches on the
search workers. Each plugin's items are one immutable tuple that a refresh
replaces whole, so a search never sees half a refresh. The last items are kept in
`<data>/plugin-data/<id>/items.json`, so results appear at once after a restart
and survive a failed refresh.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from winlauncher.catalog.model import normalize
from winlauncher.plugins.api import (
    Ask,
    Plugin,
    PluginCommand,
    PluginContext,
    PluginError,
    PluginItem,
    SetupRequired,
    no_questions,
)
from winlauncher.plugins.budget import SearchBudget, SearchRecord
from winlauncher.plugins.config import PluginConfig
from winlauncher.plugins.registry import Discovery, PluginSource
from winlauncher.plugins.search import (
    KeywordTarget,
    PluginHit,
    PluginQuery,
    SearchableItem,
    literal_patterns,
    plugins_answer,
    prepare_forms,
    rank_items,
    searchable_items,
)
from winlauncher.search.query import SearchQuery

MAX_ITEMS = 5000  # Per plugin; more would slow down every keystroke.
CACHE_FILE = "items.json"
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PluginState:
    items: tuple[PluginItem, ...] = ()
    refreshed: float | None = None  # When the items were listed; None before the first time.
    error: str = ""  # Why the last refresh failed; empty after a successful one.
    needs_setup: bool = False
    refreshing: bool = False


@dataclass(slots=True)
class _Entry:
    plugin: Plugin
    # The plugin's compiled `literal_terms`; the loader checked that they compile.
    literal: tuple[re.Pattern[str], ...] = ()
    state: PluginState = field(default_factory=PluginState)
    # `state.items` prepared for matching; rebuilt only when the items themselves change.
    searchable: tuple[SearchableItem, ...] = ()
    lock: threading.Lock = field(default_factory=threading.Lock)  # One call at a time.


@dataclass(frozen=True, slots=True)
class PluginTiming:
    """How long one plugin's ranking took in one search, for the log."""

    plugin_id: str
    elapsed_ms: float
    items: int  # Its items the search looked at.
    hits: int

    def __str__(self) -> str:
        return f"{self.plugin_id}={self.elapsed_ms:.1f}ms/{self.items}items/{self.hits}hits"


@dataclass(frozen=True, slots=True)
class PluginSearch:
    """The plugin hits of one search, how long each plugin took and who was left out."""

    hits: list[PluginHit] = field(default_factory=list)
    timings: tuple[PluginTiming, ...] = ()
    left_out: tuple[str, ...] = ()  # Too slow for ordinary searches (`SearchBudget`).

    def summary(self) -> str:
        """One log field: each plugin's time, item and hit count, then those left out."""
        parts = [str(timing) for timing in self.timings]
        parts.extend(f"{plugin_id}=left-out" for plugin_id in self.left_out)
        return ",".join(parts) or "-"


class PluginHost:
    def __init__(self, found: Discovery, data_dir: Path):
        """`data_dir` is WinLauncher's data directory; plugins get folders below it."""
        self.data_dir = data_dir / "plugin-data"
        self.problems = tuple(found.problems)  # Plugins that failed to load.
        self._sources = dict(found.sources)
        self._entries = {
            plugin.id: _Entry(plugin, literal=literal_patterns(plugin.literal_terms))
            for plugin in found.plugins
        }
        self._guard = threading.Lock()  # Guards state replacement.
        self.budget = SearchBudget()

    @property
    def plugins(self) -> list[Plugin]:
        return [entry.plugin for entry in self._entries.values()]

    def plugin(self, plugin_id: str) -> Plugin | None:
        entry = self._entries.get(plugin_id)
        return entry.plugin if entry else None

    def state(self, plugin_id: str) -> PluginState:
        return self._entries[plugin_id].state

    def search_record(self, plugin_id: str) -> SearchRecord:
        return self.budget.record(plugin_id)

    def refreshing(self) -> list[str]:
        """The plugins whose refresh runs now, for the log of a stalled GUI."""
        return [plugin_id for plugin_id, entry in self._entries.items() if entry.state.refreshing]

    def source(self, plugin_id: str) -> PluginSource:
        """Where the plugin was loaded from, for the settings page."""
        return self._sources.get(plugin_id, PluginSource(plugin_id))

    # Configuration ----------------------------------------------------------

    @staticmethod
    def default_config(plugin: Plugin) -> PluginConfig:
        return PluginConfig(
            plugin.id,
            keywords=plugin.suggested_keywords(),
            routes={route.id: route.keywords for route in plugin.routes},
            refresh_minutes=plugin.refresh_minutes,
        )

    def config(self, plugin_id: str, configs: Sequence[PluginConfig]) -> PluginConfig:
        """The user's configuration of the plugin, or its defaults."""
        found = next((config for config in configs if config.id == plugin_id), None)
        return found or self.default_config(self._entries[plugin_id].plugin)

    def enabled(self, configs: Sequence[PluginConfig]) -> list[PluginConfig]:
        return [
            config
            for plugin_id in self._entries
            if (config := self.config(plugin_id, configs)).enabled
        ]

    def keywords(self, configs: Sequence[PluginConfig]) -> dict[str, KeywordTarget]:
        """Normalized keyword to what it searches, for every enabled plugin.

        A route the plugin no longer declares is left out, so its keyword is free.
        """
        found: dict[str, KeywordTarget] = {}
        for config in self.enabled(configs):
            declared = {route.id for route in self._entries[config.id].plugin.routes}
            for keyword, route in config.keyword_routes().items():
                if route is None or route in declared:
                    found[normalize(keyword)] = KeywordTarget(config.id, route)
        return found

    def context(
        self,
        plugin_id: str,
        config: PluginConfig,
        cancel: threading.Event | None = None,
        ask: Ask = no_questions,
    ) -> PluginContext:
        plugin = self._entries[plugin_id].plugin
        folder = self.data_dir / plugin_id
        folder.mkdir(parents=True, exist_ok=True)
        options = {
            option.key: option.value(config.options.get(option.key)) for option in plugin.options
        }
        return PluginContext(
            data_dir=folder,
            options=options,
            cancel=cancel or threading.Event(),
            ask=ask,
            log=logging.getLogger(f"winlauncher.plugins.{plugin_id}"),
        )

    # Items ----------------------------------------------------------------------

    def load_cached(self) -> bool:
        """Read the items each plugin listed last time; a broken cache is ignored.

        True when any plugin had items cached; `prepare_cached` then has work to do.
        """
        loaded = False
        for plugin_id, entry in self._entries.items():
            path = self.data_dir / plugin_id / CACHE_FILE
            if not path.exists():
                continue
            started = time.perf_counter()
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                items = tuple(_item_from_json(value) for value in data["items"][:MAX_ITEMS])
                refreshed = float(data["refreshed"])
            except (OSError, ValueError, KeyError, TypeError) as exc:
                logger.warning("Cached items of plugin %s ignored: %s", plugin_id, exc)
                continue
            self._set(entry, replace(entry.state, items=items, refreshed=refreshed), prepare=False)
            loaded = loaded or bool(items)
            logger.info(
                "Plugin %s cached items loaded: items=%d elapsed_ms=%.0f",
                plugin_id,
                len(items),
                (time.perf_counter() - started) * 1000,
            )
        return loaded

    def prepare_cached(self) -> None:
        """Prepare what `load_cached` read for fuzzy matching; runs on a worker.

        Reading the cache stays quick on the UI thread; until this ran, the first
        search computes what it needs itself.
        """
        for entry in self._entries.values():
            prepare_forms(entry.searchable)

    def refresh(
        self, plugin_id: str, config: PluginConfig, cancel: threading.Event | None = None
    ) -> PluginState:
        """List the plugin's items now; runs on a worker. A busy plugin is skipped."""
        entry = self._entries[plugin_id]
        if not entry.lock.acquire(blocking=False):
            return entry.state
        try:
            self._set(entry, replace(entry.state, refreshing=True))
            logger.info("Plugin %s refresh started", plugin_id)
            started = time.perf_counter()
            context = self.context(plugin_id, config, cancel)
            try:
                listed = entry.plugin.refresh(context)
                listed_at = time.perf_counter()
                items = _validated(listed)
            except SetupRequired as exc:
                # Signed out or never set up: what it listed before is no longer the user's.
                logger.info("Plugin %s needs setup: %s", plugin_id, exc)
                (self.data_dir / plugin_id / CACHE_FILE).unlink(missing_ok=True)
                return self._set(entry, PluginState(error=str(exc), needs_setup=True))
            except PluginError as exc:
                logger.info("Plugin %s refresh failed: %s", plugin_id, exc)
                failed = replace(entry.state, error=str(exc), refreshing=False)
                return self._set(entry, failed)
            except Exception as exc:
                logger.exception("Plugin %s refresh failed", plugin_id)
                failed = replace(
                    entry.state, error=f"{type(exc).__name__}: {exc}", refreshing=False
                )
                return self._set(entry, failed)
            if context.cancel.is_set():
                logger.info("Plugin %s refresh cancelled", plugin_id)
                return self._set(entry, replace(entry.state, refreshing=False))
            return self._publish(entry, items, started, listed_at)
        finally:
            if entry.state.refreshing:
                # Even a plugin that raised SystemExit leaves no plugin marked busy.
                self._set(entry, replace(entry.state, refreshing=False))
            entry.lock.release()

    def _publish(
        self, entry: _Entry, items: tuple[PluginItem, ...], started: float, listed_at: float
    ) -> PluginState:
        """Keep freshly listed items: cache them, prepare them for matching, log the times.

        The plugin's own code ran until `listed_at`; what follows is WinLauncher's
        work, so the log tells a slow plugin from slow preparation.
        """
        validated_at = time.perf_counter()
        refreshed = time.time()
        self._write_cache(entry.plugin.id, items, refreshed)
        cached_at = time.perf_counter()
        state = self._set(entry, PluginState(items, refreshed))
        done = time.perf_counter()
        logger.info(
            "Plugin %s refreshed: items=%d plugin_ms=%.0f validate_ms=%.0f cache_ms=%.0f "
            "prepare_ms=%.0f total_ms=%.0f",
            entry.plugin.id,
            len(items),
            (listed_at - started) * 1000,
            (validated_at - listed_at) * 1000,
            (cached_at - validated_at) * 1000,
            (done - cached_at) * 1000,
            (done - started) * 1000,
        )
        return state

    def forget(self, plugin_id: str) -> None:
        """Drop the plugin's items, such as after signing out."""
        entry = self._entries[plugin_id]
        (self.data_dir / plugin_id / CACHE_FILE).unlink(missing_ok=True)
        self._set(entry, PluginState())

    def _set(self, entry: _Entry, state: PluginState, *, prepare: bool = True) -> PluginState:
        """Publish a plugin's new state, preparing its items when they are new ones."""
        searchable = entry.searchable
        if state.items is not entry.state.items:
            searchable = searchable_items(state.items, prepare=prepare)
            self.budget.forget(entry.plugin.id)
        with self._guard:
            entry.state, entry.searchable = state, searchable
        return state

    def _write_cache(self, plugin_id: str, items: tuple[PluginItem, ...], refreshed: float) -> None:
        path = self.data_dir / plugin_id / CACHE_FILE
        data = {"refreshed": refreshed, "items": [asdict(item) for item in items]}
        try:
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("Items of plugin %s not cached: %s", plugin_id, exc)

    # Search and actions -----------------------------------------------------

    def search(
        self,
        query: SearchQuery | PluginQuery,
        configs: Sequence[PluginConfig],
        limit: int,
        *,
        cancel: threading.Event | None = None,
        time_limit_ms: int = 0,
    ) -> PluginSearch:
        """Hits of one plugin for a plugin query, or of every plugin in ordinary searches.

        Ordinary searches leave out plugins that ranked too slowly before
        (`SearchBudget`); a plugin query always ranks its plugin.
        """
        if isinstance(query, PluginQuery):
            config = self.config(query.plugin_id, configs)
            if not config.enabled:
                return PluginSearch()
            return self._search_each(
                [config],
                query.search,
                limit,
                cancel,
                time_limit_ms,
                ordinary=False,
                route=query.route,
            )
        if not plugins_answer(query):
            return PluginSearch()
        searched, left_out = [], []
        for config in self.enabled(configs):
            if not config.in_search:
                continue
            if self.budget.allows(config.id, time_limit_ms):
                searched.append(config)
            else:
                left_out.append(config.id)
        found = self._search_each(searched, query, limit, cancel, time_limit_ms, ordinary=True)
        return replace(found, left_out=tuple(left_out))

    def _search_each(
        self,
        configs: Sequence[PluginConfig],
        query: SearchQuery,
        limit: int,
        cancel: threading.Event | None,
        time_limit_ms: int,
        *,
        ordinary: bool,
        route: str | None = None,
    ) -> PluginSearch:
        """Rank each plugin in turn, timing it; a cancelled search keeps no hits."""
        hits: list[PluginHit] = []
        timings = []
        for config in configs:
            started = time.perf_counter()
            items = self._items(config.id, route)
            ranked = self._rank(config, items, query, limit, cancel)
            if cancel is not None and cancel.is_set():
                return PluginSearch()
            elapsed_ms = (time.perf_counter() - started) * 1000
            self.budget.note(config.id, elapsed_ms, time_limit_ms, ordinary=ordinary)
            timings.append(PluginTiming(config.id, elapsed_ms, len(items), len(ranked)))
            hits.extend(ranked)
        return PluginSearch(hits, tuple(timings))

    def _items(self, plugin_id: str, route: str | None) -> tuple[SearchableItem, ...]:
        """The plugin's items as prepared for matching; with a route, only its items."""
        items = self._entries[plugin_id].searchable
        if route is None:
            return items
        return tuple(searchable for searchable in items if route in searchable.item.routes)

    def _rank(
        self,
        config: PluginConfig,
        items: Sequence[SearchableItem],
        query: SearchQuery,
        limit: int,
        cancel: threading.Event | None,
    ) -> list[PluginHit]:
        entry = self._entries[config.id]
        return rank_items(
            config.id,
            entry.plugin.label,
            items,
            query,
            boost=config.boost,
            limit=limit,
            literal=entry.literal,
            cancel=cancel,
        )

    def open(self, hit: PluginHit, config: PluginConfig) -> None:
        """Let the plugin open its item; runs on the action worker."""
        plugin = self._entries[hit.plugin_id].plugin
        plugin.open(hit.item, self.context(hit.plugin_id, config))

    def commands(self, plugin_id: str) -> tuple[PluginCommand, ...]:
        """The plugin's settings buttons; anything else it returns is ignored.

        The settings page paints these, so a plugin must not be able to break it.
        """
        try:
            declared = tuple(self._entries[plugin_id].plugin.commands())
        except Exception:
            logger.exception("Plugin %s commands unavailable", plugin_id)
            return ()
        usable = tuple(
            command
            for command in declared
            if isinstance(command, PluginCommand)
            and isinstance(command.label, str)
            and command.label.strip()
            and callable(command.run)
        )
        if len(usable) != len(declared):
            logger.warning("Plugin %s declared unusable settings buttons", plugin_id)
        return usable

    def run_command(
        self, plugin_id: str, command: PluginCommand, config: PluginConfig, ask: Ask
    ) -> str | None:
        """Run a settings button's command; it may ask questions. Runs on a worker."""
        entry = self._entries[plugin_id]
        with entry.lock:
            message = command.run(self.context(plugin_id, config, ask=ask))
        if command.forgets_items:
            self.forget(plugin_id)
        return message


def _validated(items: object) -> tuple[PluginItem, ...]:
    if items is None:
        raise PluginError("The plugin listed nothing.")
    result = []
    seen = set()
    for item in items:
        if not isinstance(item, PluginItem):
            raise PluginError(f"The plugin listed {type(item).__name__}, not PluginItem.")
        if item.key in seen:
            continue
        seen.add(item.key)
        result.append(item)
        if len(result) >= MAX_ITEMS:
            break
    return tuple(result)


def _item_from_json(value: object) -> PluginItem:
    if not isinstance(value, dict):
        raise ValueError("cached item is no object")
    values = dict(value)
    values["keywords"] = tuple(values.get("keywords", ()))
    values["routes"] = tuple(values.get("routes", ()))
    return PluginItem(**values)
