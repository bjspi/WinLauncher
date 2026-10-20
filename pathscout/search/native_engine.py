"""The Rust search engine adapter. Python's ranking stays the behavior reference.

PathScout always searches with the Rust engine. The Python engine runs only when
the extension is missing or does not support this interpreter, and that is
reported to the user. `PATHSCOUT_SEARCH_ENGINE` selects an engine explicitly for
tests and benchmarks: `native` fails loudly instead of falling back, `python`
uses the reference implementation.
"""

from __future__ import annotations

import functools
import importlib
import logging
import os
import re
import sqlite3
import threading
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from pathscout.catalog.priority import PriorityPolicy
from pathscout.catalog.store import CatalogStore
from pathscout.search.candidates import (
    CANCEL_CHECK_INSTRUCTIONS,
    Candidate,
    candidate_from_row,
    candidate_sql,
)
from pathscout.search.query import SearchQuery
from pathscout.search.ranking import PriorityBand, Ranker, Ranking
from pathscout.settings import DEFAULT_CONTENT_SEARCH_THREADS

API_VERSION = 8
ENGINE_VARIABLE = "PATHSCOUT_SEARCH_ENGINE"
PHASE_VARIABLE = "PATHSCOUT_NATIVE_PHASE"
_ENGINES = ("auto", "python", "native")
_PHASES = ("rows", "sqlite")
_LOGGER = logging.getLogger(__name__)
_LOGGED: set[str] = set()
_LOG_LOCK = threading.Lock()
_PARAMETER = re.compile(r":([a-zA-Z_][a-zA-Z_0-9]*)")
_CANCEL_POLL_SECONDS = 0.005

# The exclusion callback the native engine calls for hits it would keep:
# (path text, is a folder, from an application source) -> allowed.
AllowsCallback = Callable[[str, bool, bool], bool]


def _log_once(key: str, message: str, *args: object) -> None:
    with _LOG_LOCK:
        if key in _LOGGED:
            return
        _LOGGED.add(key)
    _LOGGER.warning(message, *args)


@dataclass(frozen=True, slots=True)
class NativeAvailability:
    module: ModuleType | None
    reason: str = ""


@functools.cache
def native_availability() -> NativeAvailability:
    """Import the extension once and select this interpreter's Unicode tables."""
    try:
        native = importlib.import_module("pathscout_native")
    except ModuleNotFoundError as exc:
        if exc.name == "pathscout_native":
            return _unavailable("The Rust search engine is not installed.")
        return _unavailable(f"The Rust search engine cannot be loaded: {exc}")
    except Exception as exc:
        return _unavailable(f"The Rust search engine cannot be loaded: {exc}")
    version = getattr(native, "API_VERSION", None)
    if version != API_VERSION:
        return _unavailable(
            f"The installed Rust search engine (API {version}) does not match this "
            f"PathScout (API {API_VERSION}); rebuild or reinstall it."
        )
    try:
        native.use_unicode(unicodedata.unidata_version)
    except ValueError as exc:
        return _unavailable(f"The Rust search engine does not support this Python: {exc}")
    return NativeAvailability(native)


def _unavailable(reason: str) -> NativeAvailability:
    _LOGGER.warning("%s Searching with the slower Python engine.", reason)
    return NativeAvailability(None, reason)


def requested_engine() -> str:
    engine = os.environ.get(ENGINE_VARIABLE, "auto").strip().lower()
    if engine not in _ENGINES:
        raise ValueError(f"{ENGINE_VARIABLE} must be one of {', '.join(_ENGINES)}.")
    return engine


def native_required() -> bool:
    return requested_engine() == "native"


def select_native() -> ModuleType | None:
    """The native module to search with, or None for the Python engine."""
    engine = requested_engine()
    if engine == "python":
        return None
    availability = native_availability()
    if engine == "native" and availability.module is None:
        raise RuntimeError(availability.reason)
    return availability.module


def query_spec(
    query: SearchQuery,
    ranker: Ranker,
    limit: int,
    content_search_threads: int = DEFAULT_CONTENT_SEARCH_THREADS,
) -> dict[str, object]:
    return {
        "terms": list(query.terms),
        "folder_parts": list(query.folder_parts),
        "excluded_terms": list(query.excluded_terms),
        "kind": str(query.kind) if query.kind is not None else None,
        "extensions": list(query.extensions),
        "scope_key": ranker.filters.scope_key,
        "scope_search_prefix": ranker.scope_search_prefix,
        "modified_after": query.modified_after,
        "modified_before": query.modified_before,
        "min_size": query.min_size,
        "max_size": query.max_size,
        "content_text": query.content_text,
        "content_search_threads": content_search_threads,
        "explorer": query.explorer,
        "basename_only": query.basename_only,
        "fuzzy": ranker.fuzzy,
        "combined": ranker.combined,
        "sort": str(query.sort),
        "limit": limit,
        "now": ranker.now,
    }


def priority_spec(policy: PriorityPolicy) -> dict[str, list[dict[str, object]]]:
    """Every rule by name. Filename rules carry their compiled Python pattern, which
    the native engine uses wherever Rust's regex cannot match Python exactly."""
    return {
        "folders": [
            {
                "key": key,
                "path_length": len(rule.path),
                "weight": rule.weight,
                "class": str(rule.search_priority),
            }
            for rule, key in policy.folders
        ],
        "names": [
            {
                "pattern": rule.pattern,
                "python_search": pattern.search,
                "weight": rule.weight,
                "class": str(rule.search_priority),
                "files": rule.files,
                "folders": rule.folders,
            }
            for rule, pattern in policy.names
        ],
        "extensions": [
            {"extension": extension, "weight": rule.weight, "class": str(rule.search_priority)}
            for extension, rules in policy.extensions.items()
            for rule in rules
        ],
        "attributes": [
            {"mask": rule.mask, "weight": rule.weight, "class": str(rule.search_priority)}
            for rule in policy.attributes
        ],
    }


def _search_sql(query: SearchQuery) -> tuple[str, dict[str, object]]:
    sql, parameters = candidate_sql(query)
    if query.uses_fuzzy_pass:
        fuzzy_sql, fuzzy_parameters = candidate_sql(query, fuzzy=True)
        fuzzy_sql = _PARAMETER.sub(lambda match: ":fuzzy_" + match[1], fuzzy_sql)
        # UNION ALL streams the literal pass first and shares one heap and snapshot.
        sql += " UNION ALL " + fuzzy_sql
        parameters.update(("fuzzy_" + key, value) for key, value in fuzzy_parameters.items())
    return sql, parameters


@contextmanager
def _cancel_flag(native: ModuleType, cancel: threading.Event | None) -> Iterator[Any]:
    """A native `CancelFlag` that follows `cancel` while the native call runs."""
    flag = native.CancelFlag()
    if cancel is None:
        yield flag
        return
    if cancel.is_set():
        flag.set()
        yield flag
        return
    finished = threading.Event()

    def forward_cancel() -> None:
        while not finished.wait(_CANCEL_POLL_SECONDS):
            if cancel.is_set():
                flag.set()
                return

    bridge = threading.Thread(target=forward_cancel, name="native-search-cancel", daemon=True)
    bridge.start()
    try:
        yield flag
    finally:
        finished.set()
        bridge.join()


def _native_phase() -> str:
    phase = os.environ.get(PHASE_VARIABLE, "sqlite").strip().lower()
    if phase not in _PHASES:
        raise ValueError(f"{PHASE_VARIABLE} must be rows or sqlite.")
    return phase


def rank_with_native(
    native: ModuleType,
    store: CatalogStore,
    query: SearchQuery,
    priorities: PriorityPolicy,
    ranker: Ranker,
    limit: int,
    allows: AllowsCallback,
    *,
    cancel: threading.Event | None = None,
    content_search_threads: int = DEFAULT_CONTENT_SEARCH_THREADS,
    candidates: list[Candidate] | None = None,
) -> list[tuple[Candidate, Ranking]]:
    """Kept candidates with their rankings, best first."""
    phase = _native_phase()
    spec = query_spec(query, ranker, limit, content_search_threads)
    rules = priority_spec(priorities)
    sql, parameters = _search_sql(query)
    with _cancel_flag(native, cancel) as flag:
        if phase == "sqlite" and candidates is None:
            kept = native.rank_sqlite(str(store.path), sql, parameters, spec, rules, allows, flag)
        else:
            if candidates is not None:
                rows = [candidate.to_row() for candidate in candidates]
            else:
                rows = _candidate_rows(store, sql, parameters, cancel, flag)
            kept = [
                (rows[index], score, band, reasons)
                for index, score, band, reasons in native.rank_rows(spec, rules, rows, allows, flag)
            ]
    return [
        (candidate_from_row(row), Ranking(score, PriorityBand(band), tuple(reasons)))
        for row, score, band, reasons in kept
    ]


def _candidate_rows(
    store: CatalogStore,
    sql: str,
    parameters: dict[str, object],
    cancel: threading.Event | None,
    flag: Any,
) -> list[tuple]:
    with store.connect() as connection:
        connection.row_factory = None
        if cancel is not None:
            connection.set_progress_handler(cancel.is_set, CANCEL_CHECK_INSTRUCTIONS)
        try:
            rows = []
            for row in connection.execute(sql, parameters):
                if flag.is_set():
                    return []
                rows.append(row)
        except sqlite3.OperationalError:
            if cancel is not None and cancel.is_set():
                return []
            raise
    return rows


def log_native_failure(exc: Exception) -> None:
    """Each kind of failure is logged once; an unexpected new one is never hidden."""
    _log_once(
        f"execution:{type(exc).__name__}",
        "Rust search failed; using Python for this search: %s",
        exc,
    )
