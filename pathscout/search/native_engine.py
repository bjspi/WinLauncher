"""Optional native candidate ranking, with Python as the behavior reference."""

from __future__ import annotations

import importlib
import logging
import os
import re
import sqlite3
import threading
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import ModuleType
from typing import Any

from pathscout.catalog.priority import PriorityPolicy
from pathscout.catalog.store import CatalogStore
from pathscout.search.candidates import (
    CANCEL_CHECK_INSTRUCTIONS,
    Candidate,
    _candidate_from_row,
    candidate_sql,
)
from pathscout.search.query import SearchQuery
from pathscout.search.ranking import PriorityBand, Ranker, Ranking

API_VERSION = 4
_LOGGER = logging.getLogger(__name__)
_LOGGED: set[str] = set()
_LOG_LOCK = threading.Lock()
_PARAMETER = re.compile(r":([a-zA-Z_][a-zA-Z_0-9]*)")
_CANCEL_POLL_SECONDS = 0.005


def _log_once(reason: str, message: str, *args: object) -> None:
    with _LOG_LOCK:
        if reason in _LOGGED:
            return
        _LOGGED.add(reason)
    _LOGGER.warning(message, *args)


def load_native() -> ModuleType | None:
    """Return a compatible optional module; report each unavailable state once."""
    try:
        native = importlib.import_module("pathscout_native")
    except ModuleNotFoundError as exc:
        if exc.name == "pathscout_native":
            _log_once("missing", "Native search module is not installed; using Python.")
        else:
            _log_once("import", "Cannot import native search module; using Python: %s", exc)
        return None
    except Exception as exc:
        _log_once("import", "Cannot import native search module; using Python: %s", exc)
        return None
    version = getattr(native, "API_VERSION", None)
    if version != API_VERSION:
        _log_once(
            "version", "Native search API %r does not match %d; using Python.", version, API_VERSION
        )
        return None
    if getattr(native, "UNICODE_VERSION", None) != unicodedata.unidata_version:
        _log_once("unicode", "Native search Unicode tables do not match Python; using Python.")
        return None
    return native


def select_native(backend: str = "rust") -> ModuleType | None:
    default = "auto" if backend == "rust" else "python"
    engine = os.environ.get("PATHSCOUT_SEARCH_ENGINE", default).strip().lower()
    if engine not in ("auto", "python", "native"):
        raise ValueError("PATHSCOUT_SEARCH_ENGINE must be auto, python or native.")
    if engine == "python":
        return None
    native = load_native()
    if engine == "native" and native is None:
        raise RuntimeError(
            "Native search was requested, but no compatible native module is available."
        )
    return native


def query_spec(query: SearchQuery, ranker: Ranker, limit: int) -> dict[str, object]:
    return {
        "terms": list(query.terms),
        "folder_parts": list(query.folder_parts),
        "excluded_terms": list(query.excluded_terms),
        "kind": str(query.kind) if query.kind is not None else None,
        "extensions": list(query.extensions),
        "scope_key": ranker.filters.scope_key,
        "modified_after": query.modified_after,
        "modified_before": query.modified_before,
        "min_size": query.min_size,
        "max_size": query.max_size,
        "content_text": query.content_text,
        "basename_only": query.basename_only,
        "fuzzy": ranker.fuzzy,
        "combined": ranker.combined,
        "sort": str(query.sort),
        "limit": limit,
        "now": ranker.now,
    }


def priority_spec(policy: PriorityPolicy) -> dict[str, list[tuple]]:
    return {
        "folders": [
            (key, len(rule.path), rule.weight, str(rule.search_priority))
            for rule, key in policy.folders
        ],
        "names": [
            (rule.pattern, rule.weight, str(rule.search_priority), rule.files, rule.folders)
            for rule, _ in policy.names
        ],
        "extensions": [
            (extension, rule.weight, str(rule.search_priority))
            for extension, rules in policy.extensions.items()
            for rule in rules
        ],
        "attributes": [
            (rule.mask, rule.weight, str(rule.search_priority)) for rule in policy.attributes
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


def rank_with_native(
    native: ModuleType,
    store: CatalogStore,
    query: SearchQuery,
    priorities: PriorityPolicy,
    ranker: Ranker,
    limit: int,
    allows: Callable[[str, bool], bool],
    *,
    cancel: threading.Event | None = None,
) -> list[tuple[Candidate, Ranking]]:
    phase = os.environ.get("PATHSCOUT_NATIVE_PHASE", "sqlite").strip().lower()
    if phase not in ("rows", "sqlite"):
        raise ValueError("PATHSCOUT_NATIVE_PHASE must be rows or sqlite.")
    spec = query_spec(query, ranker, limit)
    rules = priority_spec(priorities)
    sql, parameters = _search_sql(query)
    with _cancel_flag(native, cancel) as flag:
        if phase == "sqlite":
            kept = native.rank_sqlite(str(store.path), sql, parameters, spec, rules, allows, flag)
        else:
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
            kept = [
                (rows[index], score, band, reasons)
                for index, score, band, reasons in native.rank_rows(spec, rules, rows, allows, flag)
            ]
    return [
        (_candidate_from_row(row), Ranking(score, PriorityBand(band), tuple(reasons)))
        for row, score, band, reasons in kept
    ]


def log_native_failure(exc: Exception) -> None:
    _log_once("execution", "Native search failed; using Python for this search: %s", exc)
