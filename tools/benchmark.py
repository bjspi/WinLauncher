"""Measure synthetic catalog queries separately from filesystem scan speed."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pathscout.catalog.indexer import SCAN_BATCH_SIZE
from pathscout.catalog.model import CatalogItem, CatalogSource, ItemKind
from pathscout.catalog.store import CatalogStore
from pathscout.search.query_parser import parse_query
from pathscout.search.service import SearchService
from pathscout.settings import Settings

NAMES = ("Project Report", "Budget Review", "Technical Drawing", "Meeting Notes", "Build Output")
QUERIES = (
    "",
    "type:file",
    "p",
    "{root}",
    "report match:keyword",
    "report",
    "pr",
    "bdg",
    "drawing ext:txt",
    "missing-name",
)
REPETITIONS = 3


def synthetic_items(root: Path, entries: int, directories: int) -> Iterator[CatalogItem]:
    for index in range(entries):
        folder = root / f"team-{index % directories}"
        name = f"{NAMES[index % len(NAMES)]} {index:06d}.txt"
        yield CatalogItem(folder / name, ItemKind.FILE)


def build_catalog(store: CatalogStore, root: Path, entries: int, directories: int) -> None:
    source_id = CatalogSource(root).id
    batch: list[tuple[CatalogItem, str]] = []
    with store.snapshot() as connection:
        for item in synthetic_items(root, entries, directories):
            batch.append((item, source_id))
            if len(batch) == SCAN_BATCH_SIZE:
                store.stage(connection, batch)
                batch.clear()
        store.stage(connection, batch)
        store.publish(connection, active_roots=(source_id,), preserved_roots=())


def time_query(service: SearchService, text: str, settings: Settings) -> dict[str, float]:
    query = parse_query(text)
    timings, hits = [], []
    for _ in range(REPETITIONS):
        started = time.perf_counter()
        hits = service.search(query, settings)
        timings.append((time.perf_counter() - started) * 1000)
    return {"median_ms": round(statistics.median(timings), 2), "results": len(hits)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--entries", type=int, default=50000)
    parser.add_argument(
        "--directories",
        type=int,
        default=40,
        help="Create real directories to include marker-check costs",
    )
    args = parser.parse_args()
    if args.entries < 1 or args.directories < 1:
        parser.error("Entries and directories must be positive.")
    with tempfile.TemporaryDirectory(prefix="pathscout-benchmark-") as temporary:
        root = Path(temporary)
        for index in range(args.directories):
            (root / f"team-{index}").mkdir()
        store = CatalogStore(root / "catalog.sqlite3")
        started = time.perf_counter()
        build_catalog(store, root, args.entries, args.directories)
        result = {
            "entries": store.count(),
            "real_directories": args.directories,
            "synthetic_build_seconds": round(time.perf_counter() - started, 3),
            "queries": {},
        }
        settings = Settings(roots=(str(root),), index_apps=False)
        service = SearchService(store)
        for template in QUERIES:
            text = template.format(root=root.name)
            result["queries"][text] = time_query(service, text, settings)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
