"""Render the README screenshots from the real widgets with fictional catalog items.

python tools/readme_screenshots.py [output folder, default docs/images]
"""

import sys
import time
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, QPoint

from winlauncher.__main__ import _create_application
from winlauncher.catalog.model import CatalogItem, ItemKind
from winlauncher.search.query_parser import parse_query
from winlauncher.search.ranking import SearchHit
from winlauncher.settings import Settings
from winlauncher.ui.about import AboutDialog
from winlauncher.ui.settings import SettingsDialog
from winlauncher.ui.tray import TrayMenu
from winlauncher.ui.website_icons import WebsiteIcons
from winlauncher.ui.window import LauncherWindow

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


website_icons = WebsiteIcons()
window = LauncherWindow(website_icons)
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

explorer = LauncherWindow(website_icons)
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

launcher = LauncherWindow(website_icons)
launcher.set_catalog_count(48213)
launcher.present(text="invoice ext:pdf date:year !draft")
launcher.set_results(
    hits(
        [
            (r"D:\Work\Finance\Invoice 2026-031.pdf", ItemKind.FILE, 12, False),
            (r"D:\Work\Finance\Invoice 2026-024.pdf", ItemKind.FILE, 40, False),
            (r"D:\Work\Clients\Acme\Invoice March.pdf", ItemKind.FILE, 75, False),
        ]
    ),
    6,
    query=parse_query("invoice ext:pdf date:year !draft"),
)
settle(launcher)
launcher.grab().save(str(OUT / "query-syntax.png"))
launcher.hide()

dialog = SettingsDialog(
    Settings(roots=(r"D:\Work", r"D:\Projects", r"C:\Users\you\Desktop")),
    startup_enabled=True,
    set_startup=lambda _enabled: None,
    website_icons=website_icons,
)
dialog.show()
# The shortcuts page scrolls; its screenshot gets the height to show every key.
for row, name, height in (
    (0, "settings", 660),
    (1, "settings-folders", 660),
    (7, "settings-shortcuts", 1010),
):
    dialog.resize(dialog.width(), height)
    dialog.nav.setCurrentRow(row)
    settle(dialog)
    dialog.grab().save(str(OUT / f"{name}.png"))
dialog.hide()

owner = QObject()
tray = TrayMenu(owner, autostart_available=True)
tray.set_catalog_count(48213)
tray.autostart.setChecked(True)
tray.menu.popup(QPoint(400, 300))
settle(tray.menu)
tray.menu.grab().save(str(OUT / "tray-menu.png"))
tray.menu.hide()

about = AboutDialog(Path(r"C:\Users\you\.WinLauncher"))
about.finish()
about.show()
settle(about)
about.grab().save(str(OUT / "about.png"))
about.hide()
print(f"Saved screenshots to {OUT}")
