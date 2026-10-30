"""Render the README screenshots from the real widgets with fictional catalog items.

python tools/readme_screenshots.py [output folder, default docs/images] [light|dark]

The README shows the light theme, whatever Windows uses. The windows are rendered
without ever appearing on screen, so clicks and typing elsewhere cannot reach them.
"""

import sys
import time
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, Qt
from PySide6.QtWidgets import QWidget

from winlauncher.__main__ import _create_application
from winlauncher.catalog.model import CatalogItem, ItemKind
from winlauncher.search.query_parser import parse_query
from winlauncher.search.ranking import SearchHit
from winlauncher.settings import Appearance, Settings
from winlauncher.ui.about import AboutDialog
from winlauncher.ui.settings import SettingsDialog
from winlauncher.ui.theme import theme
from winlauncher.ui.tray import TrayMenu
from winlauncher.ui.website_icons import WebsiteIcons
from winlauncher.ui.window import LauncherWindow

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parents[1] / "docs/images"
OUT.mkdir(parents=True, exist_ok=True)
APPEARANCE = Appearance(sys.argv[2]) if len(sys.argv) > 2 else Appearance.LIGHT
app = _create_application()
_set_visible = QWidget.setVisible


def _off_screen(widget: QWidget, visible: bool) -> None:
    """Show windows only to `grab()`: no screen, no focus, no stray input."""
    if visible and widget.isWindow():
        widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    _set_visible(widget, visible)


QWidget.setVisible = _off_screen
theme().use(APPEARANCE)
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


ROAD_RESULTS = [
    (r"D:\Work\Projects\Roadmap 2026", ItemKind.FOLDER, 2, True),
    (r"D:\Work\Projects\Roadmap 2026\Roadmap draft.docx", ItemKind.FILE, 1, False),
    (r"D:\Work\Reports\Roadshow deck.pdf", ItemKind.FILE, 5, False),
    (r"D:\Work\Reports\road-trip-budget.xlsx", ItemKind.FILE, 9, False),
    (r"D:\Work\Notes\Road safety checklist.md", ItemKind.FILE, 20, False),
]
SETTINGS = Settings(roots=(r"D:\Work", r"D:\Projects", r"C:\Users\you\Desktop"))
SETTINGS_HEIGHT = 700  # Shows the General page without scrolling.


def save_launcher(name: str) -> None:
    window = LauncherWindow(website_icons)
    window.set_catalog_count(48213)
    window.present(text="road")
    window.set_results(hits(ROAD_RESULTS), 4, query=parse_query("road"))
    settle(window)
    window.grab().save(str(OUT / name))
    window.hide()


def settings_dialog() -> SettingsDialog:
    dialog = SettingsDialog(
        SETTINGS,
        startup_enabled=True,
        set_startup=lambda _enabled: None,
        website_icons=website_icons,
    )
    dialog.show()
    return dialog


website_icons = WebsiteIcons()
save_launcher("launcher.png")

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

dialog = settings_dialog()
# The shortcuts page scrolls; its screenshot gets the height to show every key.
for page, name, height in (
    ("General", "settings", SETTINGS_HEIGHT),
    ("Search folders", "settings-folders", 660),
    ("Shortcuts", "settings-shortcuts", 1010),
):
    dialog.resize(dialog.width(), height)
    dialog.nav.setCurrentItem(dialog.nav.findItems(page, Qt.MatchFlag.MatchExactly)[0])
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

if APPEARANCE == Appearance.LIGHT:  # The gallery also shows the dark theme.
    theme().use(Appearance.DARK)
    save_launcher("launcher-dark.png")
    dialog = settings_dialog()
    dialog.resize(dialog.width(), SETTINGS_HEIGHT)
    settle(dialog)
    dialog.grab().save(str(OUT / "settings-dark.png"))
    dialog.hide()
print(f"Saved screenshots to {OUT}")
