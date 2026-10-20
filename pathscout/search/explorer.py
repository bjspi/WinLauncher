"""Current directory entries for Explorer completion, without indexing their contents."""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from pathlib import Path

from pathscout.catalog import long_paths
from pathscout.catalog.indexer import catalog_item, classify_kind
from pathscout.search.candidates import Candidate

logger = logging.getLogger(__name__)


def directory_candidates(folder: Path, cancel: threading.Event | None) -> Iterator[Candidate]:
    try:
        with long_paths.scandir(folder) as entries:
            for entry in entries:
                if cancel is not None and cancel.is_set():
                    return
                try:
                    path = long_paths.ordinary_path(entry.path)
                    item = catalog_item(
                        path,
                        entry.stat(follow_symlinks=False),
                        classify_kind(path, entry.is_dir()),
                    )
                    yield Candidate.from_item(item)
                except OSError:
                    logger.debug("Explorer entry unavailable: %s", entry.path, exc_info=True)
    except OSError:
        logger.warning("Explorer directory unavailable: %s", folder, exc_info=True)
