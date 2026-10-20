"""Render the README screenshots from the real widgets with fictional catalog items.

python tools/readme_screenshots.py [output folder, default docs/images]
"""

import sys
import time
from dataclasses import replace
from pathlib import Path

from pathscout.__main__ import _create_application
from pathscout.catalog.model import CatalogItem, ItemKind
from pathscout.search.query_parser import parse_query
from pathscout.search.ranking import SearchHit
from pathscout.settings import Settings
from pathscout.ui.settings import SettingsDialog
from pathscout.ui.window import LauncherWindow

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parents[1] / "docs/images"
OUT.mkdir(parents=True, exist_ok=True)
app = _create_application()
NOW = time.time()


def hits(entries):
    return [
        SearchHit(
            replace(CatalogItem(Path(path), kind), modified=NOW - age * 86400, pinned=pinned),
            100 - index,
            "Name starts with query",
        )
        for index, (path, kind, age, pinned) in enumerate(entries)
    ]


def settle(widget, rounds=30):
    for _ in range(rounds):
        app.processEvents()
        time.sleep(0.02)


window = LauncherWindow()
window.set_catalog_count(48213)
window.present(text="road")
window.set_results(
    hits(
        [
            (r"D:\Work\Projects\Roadmap 2026", ItemKind.FOLDER, 2, True),
            (r"D:\Work\Projects\Roadmap 2026\Roadmap draft.docx", ItemKind.FILE, 1, False),
            (r"D:\Work\Reports\Roadshow deck.pdf", ItemKind.FILE, 5, False),
            (r"D:\Work\Reports\road-trip-budget.xlsx", ItemKind.FILE, 9, False),
            (r"D:\Work\Notes\Road safety checklist.md", ItemKind.FILE, 20, False),
        ]
    ),
    4,
    query=parse_query("road"),
)
settle(window)
window.grab().save(str(OUT / "launcher.png"))
window.hide()

explorer = LauncherWindow()
explorer.set_catalog_count(48213)
explorer.present(text="bud", scope=Path(r"D:\Work\Projects"), near=(200, 200, 1200, 900))
query = replace(parse_query("bud", scope=Path(r"D:\Work\Projects")), explorer=True)
explorer.set_results(
    hits(
        [
            (r"D:\Work\Projects\Budget 2026.xlsx", ItemKind.FILE, 1, False),
            (r"D:\Work\Projects\budget-notes.txt", ItemKind.FILE, 3, False),
            (r"D:\Work\Projects\Archive\Budget 2024.xlsx", ItemKind.FILE, 400, False),
        ]
    ),
    2,
    query=query,
)
settle(explorer)
explorer.grab().save(str(OUT / "explorer-search.png"))
explorer.hide()

dialog = SettingsDialog(
    Settings(roots=(r"D:\Work", r"D:\Projects")),
    startup_enabled=True,
    set_startup=lambda _enabled: None,
)
dialog.show()
settle(dialog)
dialog.grab().save(str(OUT / "settings.png"))
print(f"Saved screenshots to {OUT}")
