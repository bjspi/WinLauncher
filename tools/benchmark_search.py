"""Time a fixed list of searches against an existing catalog file.

Run it on a copy of a real catalog, never on the live one; opening the catalog
may migrate its schema:

    python tools/benchmark_search.py COPY\\catalog.sqlite3 --settings COPY\\settings.json

For every query it prints the median total search time and how the candidate
selection splits into the literal and the fuzzy pass, measured by running each
pass on its own: in the Rust engine's memory index when it serves the query,
otherwise as the candidate SQL. Only counts and times are printed, never paths.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from winlauncher.catalog.store import CatalogStore
from winlauncher.search import native_engine
from winlauncher.search.candidates import candidate_sql
from winlauncher.search.native_engine import select_memory_candidates
from winlauncher.search.query import SearchQuery
from winlauncher.search.query_parser import parse_query
from winlauncher.search.service import SearchService
from winlauncher.settings import Settings, SettingsFile

QUERIES = (
    "d",
    "do",
    "dok",
    "dokumente",
    "rechnung",
    "rechnung 2024",
    "ärzt",
    "vsc",
    "steuer bescheid",
    "*.pdf",
    "ext:pdf rech",
    "projekt",
    "a b",
    "readme",
    "xyzzyq",
)
DEFAULT_REPETITIONS = 7
WARM_UP_RUNS = 1


def median_ms(action: Callable[[], object], repetitions: int) -> tuple[float, object]:
    """The median wall time of `action` after one untimed warm-up run, and its result."""
    result = None
    for _ in range(WARM_UP_RUNS):
        result = action()
    timings = []
    for _ in range(repetitions):
        started = time.perf_counter()
        result = action()
        timings.append((time.perf_counter() - started) * 1000)
    return statistics.median(timings), result


def sql_passes(store: CatalogStore, query: SearchQuery, repetitions: int) -> list[str]:
    """Each pass of candidate SQL on its own, as the Python engine reads it."""
    timings = []
    for fuzzy in (False, True):
        sql, parameters = candidate_sql(query, fuzzy=fuzzy)

        def run(sql: str = sql, parameters: dict = parameters) -> int:
            with store.connect() as connection:
                connection.row_factory = None
                return sum(1 for _ in connection.execute(sql, parameters))

        timings.append(median_ms(run, repetitions))
    return _describe(query, *timings)


def memory_passes(native: ModuleType, store: CatalogStore, query: SearchQuery, repetitions: int):
    """The memory index's selection; the fuzzy pass costs both passes minus the literal one."""
    literal_ms, (literal, _) = median_ms(
        lambda: select_memory_candidates(native, store, query, fuzzy=False), repetitions
    )
    both_ms, (_, fuzzy) = median_ms(
        lambda: select_memory_candidates(native, store, query), repetitions
    )
    return _describe(query, (literal_ms, len(literal)), (both_ms - literal_ms, len(fuzzy)))


def _describe(query: SearchQuery, literal: tuple, fuzzy: tuple) -> list[str]:
    texts = [f"{milliseconds:6.1f} ms / {rows:>6} rows" for milliseconds, rows in (literal, fuzzy)]
    return texts if query.uses_fuzzy_pass else [texts[0], "-"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("catalog", type=Path, help="A copy of catalog.sqlite3")
    parser.add_argument("--settings", type=Path, help="A copy of settings.json")
    parser.add_argument("--engine", choices=("native", "python"), default="native")
    parser.add_argument("--phase", choices=("memory", "sqlite", "rows"), default="memory")
    parser.add_argument("--repetitions", type=int, default=DEFAULT_REPETITIONS)
    parser.add_argument("--query", action="append", help="Replace the fixed query list")
    args = parser.parse_args()
    if not args.catalog.is_file():
        parser.error("The catalog file does not exist.")
    os.environ[native_engine.ENGINE_VARIABLE] = args.engine
    os.environ[native_engine.PHASE_VARIABLE] = args.phase
    settings = SettingsFile(args.settings).load() if args.settings else Settings(roots=())
    store = CatalogStore(args.catalog)
    with store.connect() as connection:
        entries = connection.execute("SELECT count(*) FROM entries").fetchone()[0]
    print(f"Catalog: {entries} entries; engine {args.engine}, phase {args.phase}")
    native = native_engine.select_native()
    memory = native is not None and args.phase == "memory"
    if memory:
        started = time.perf_counter()
        report = native_engine.warm_search_index(store)
        print(
            f"Memory index: {report.entries} entries, {report.bytes / 2**20:.1f} MiB, "
            f"loaded in {(time.perf_counter() - started) * 1000:.0f} ms"
        )
    service = SearchService(store)
    source = "memory index" if memory else "candidate SQL"
    print(f"{'query':<16}{'total':>10}{'hits':>6}   literal pass ({source})   fuzzy pass")
    for text in args.query or QUERIES:
        query = parse_query(text)
        total, hits = median_ms(
            lambda query=query: service.search(query, settings), args.repetitions
        )
        if memory:
            literal, fuzzy = memory_passes(native, store, query, args.repetitions)
        else:
            literal, fuzzy = sql_passes(store, query, args.repetitions)
        print(f"{text:<16}{total:>7.1f} ms{len(hits):>6}   {literal:<28}{fuzzy}")


if __name__ == "__main__":
    main()
