"""Settings dialog. Values are validated by `Settings`; this module only reads widgets.

Autostart changes the Startup shortcut immediately (the shortcut is the source of
truth, shared with the tray). Every other change applies on Save. Saving keeps
the dialog open; closing discards only changes made since the last save.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from PySide6.QtCore import QMimeData, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from winlauncher import __version__
from winlauncher.catalog.analysis import LOW_WEIGHT, Suggestion, SuggestionKind, analyze
from winlauncher.catalog.model import CatalogSource, FolderDepth, path_key
from winlauncher.catalog.priority import (
    AttributePriority,
    ExtensionPriority,
    FolderPriority,
    NamePriority,
    SearchPriority,
)
from winlauncher.catalog.store import CatalogStore
from winlauncher.platform.windows_tools import display_languages
from winlauncher.plugins.api import Choice
from winlauncher.search.native_engine import (
    ENGINE_VARIABLE,
    native_availability,
    requested_engine,
)
from winlauncher.settings import (
    AUTO_HIDE_RANGE_SECONDS,
    CONTENT_SEARCH_THREADS_RANGE,
    DOUBLE_CTRL_RANGE_MS,
    RESCAN_RANGE_SECONDS,
    RESULT_LIMIT_RANGE,
    Appearance,
    Settings,
    SettingsFile,
    WindowsSettingsDetail,
    priority_rules,
    read_exported_settings,
)
from winlauncher.ui.bar_placement import BarPlacement
from winlauncher.ui.commands import CommandList
from winlauncher.ui.messages import ERROR, INFO, WARNING, ask, message
from winlauncher.ui.plugin_dialogs import choose_items
from winlauncher.ui.plugin_sources import SourceWatcher
from winlauncher.ui.plugins import PluginScheduler
from winlauncher.ui.settings_search import SettingEntry, SettingsSearch
from winlauncher.ui.tasks import BackgroundTasks, TaskCategory
from winlauncher.ui.theme import app_icon, colors, theme
from winlauncher.ui.website_icons import WebsiteIcons
from winlauncher.ui.websites import WebsiteList
from winlauncher.ui.widgets import (
    Card,
    NumberField,
    PageHeader,
    ToggleSwitch,
    key_caps,
    line_icon,
    pick_path,
    pick_save_path,
    section_title,
    sized_number,
)

if TYPE_CHECKING:
    from winlauncher.ui.plugin_settings import PluginPages

ADVANCED_RULE_FIELDS = ("name_priorities", "extension_priorities", "attribute_priorities")
SAVED_MESSAGE_MS = 2500  # How long the footer confirms a save.
FOUND_MS = 1600  # How long a setting chosen from search stays lit.
REVEAL_MARGIN = 60  # Room above and below a setting scrolled into view.
EXPORT_FILE_NAME = "WinLauncher settings.json"
SETTINGS_FILE_FILTER = "WinLauncher settings (*.json)"
FOLDER_PRIORITY_FORMAT = "25 | D:\\Projects | high"
# The opening size, lower on a small screen; longer pages scroll.
DIALOG_SIZE = QSize(920, 780)
SCREEN_MARGIN = 40  # Kept free above and below the dialog.
SIDEBAR_WIDTH = 228
NAV_ICON_SIZE = 18
BRAND_ICON_SIZE = 32
PAGE_MARGINS = (32, 26, 32, 24)  # Left, top, right, bottom around a page's cards.
PAGE_SPACING = 10
PATH_LIST_HEIGHT = 132
DEPTH_CHOICES = 10  # Levels offered for a search folder; the file allows more.
DEPTH_ROLE = Qt.ItemDataRole.UserRole + 1  # A search folder row's depth, None for all.
APPEARANCE_CHOICES = (
    ("Match Windows", Appearance.SYSTEM),
    ("Light", Appearance.LIGHT),
    ("Dark", Appearance.DARK),
)
WINDOWS_SETTINGS_DETAIL_CHOICES = (
    ("Every setting", WindowsSettingsDetail.EVERY_SETTING),
    ("Pages only", WindowsSettingsDetail.PAGES),
)
PATTERNS_HEIGHT = 150
PRIORITIES_HEIGHT = 180
RULES_HEIGHT = 300


@dataclass(frozen=True)
class AdvancedRules:
    names: tuple[NamePriority, ...]
    extensions: tuple[ExtensionPriority, ...]
    attributes: tuple[AttributePriority, ...]


def format_folder_priorities(rules: tuple[FolderPriority, ...]) -> str:
    return "\n".join(f"{rule.weight} | {rule.path} | {rule.search_priority}" for rule in rules)


def parse_folder_priorities(text: str) -> tuple[FolderPriority, ...]:
    """One `weight | absolute folder | optional search priority` rule per line."""
    rules = []
    for number, line in enumerate(text.splitlines(), start=1):
        if line.strip():
            rules.append(_parse_folder_priority(number, line))
    return tuple(rules)


def _parse_folder_priority(number: int, line: str) -> FolderPriority:
    parts = [part.strip() for part in line.split("|")]
    if len(parts) not in (2, 3):
        raise ValueError(f"Ranking line {number}: use {FOLDER_PRIORITY_FORMAT}")
    weight, folder = parts[0], parts[1]
    search = parts[2] if len(parts) == 3 else SearchPriority.NORMAL
    try:
        boost = int(weight)
    except ValueError:
        raise ValueError(f"Ranking line {number}: the boost must be a whole number.") from None
    if not Path(folder).is_absolute():
        raise ValueError(f"Ranking line {number}: the folder must be an absolute path.")
    return FolderPriority(folder, boost, search)


def format_advanced_rules(settings: Settings) -> str:
    rules = {
        name: [asdict(rule) for rule in getattr(settings, name)] for name in ADVANCED_RULE_FIELDS
    }
    return json.dumps(rules, indent=2)


def parse_advanced_rules(text: str) -> AdvancedRules:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Advanced rules are not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError("Advanced rules must be a JSON object.")
    unknown = data.keys() - set(ADVANCED_RULE_FIELDS)
    if unknown:
        raise ValueError("Unknown advanced rule keys: " + ", ".join(sorted(unknown)))
    return AdvancedRules(
        names=priority_rules("name_priorities", data.get("name_priorities", [])),
        extensions=priority_rules("extension_priorities", data.get("extension_priorities", [])),
        attributes=priority_rules("attribute_priorities", data.get("attribute_priorities", [])),
    )


def _split_lines(text: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in text.splitlines() if line.strip())


def _split_commas(text: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in text.split(",") if part.strip())


def _button(text: str, slot: Callable[[], None]) -> QPushButton:
    button = QPushButton(text)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.clicked.connect(slot)
    return button


def _page(title: str, subtitle: str, sections: list[QWidget]) -> QScrollArea:
    """A scrolling settings page: its header, then its cards and notes."""
    content = QWidget()
    content.setObjectName("page")
    layout = QVBoxLayout(content)
    layout.setContentsMargins(*PAGE_MARGINS)
    layout.setSpacing(PAGE_SPACING)
    layout.addWidget(PageHeader(title, subtitle))
    for section in sections:
        layout.addWidget(section)
    layout.addStretch(1)
    scroll = QScrollArea()
    scroll.setObjectName("pageScroll")
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setWidget(content)
    return scroll


# (What it does, keys); each key is drawn as a key cap, alternatives separated by "or".
SHORTCUTS = (
    (
        "Search bar",
        (
            ("Open the search bar", ("Ctrl", "Ctrl"), ("Ctrl", "Alt", "Space")),
            ("Open the selected result", ("Enter",)),
            ("Open result 1 to 9", ("Ctrl", "1…9")),
            ("Show it in its folder", ("Ctrl", "Enter")),
            ("Run the app as administrator", ("Ctrl", "Shift", "Enter")),
            (
                "Run a menu command with its shortcut",
                ("Alt", "Enter"),
                ("Ctrl", "Alt", "1…9"),
            ),
            ("Copy or cut the file for Explorer", ("Ctrl", "C"), ("Ctrl", "X")),
            ("Copy its path", ("Ctrl", "Shift", "C")),
            ("Add or remove a favorite", ("Ctrl", "D")),
            ("Only files, folders or apps", ("Alt", "1…3")),
            ("All kinds again", ("Alt", "0")),
            ("More actions for the result", ("Shift", "F10")),
            ("Windows' own menu for the result", ("Shift", "Right-click")),
            ("Settings", ("Ctrl", ",")),
            ("Close the bar", ("Esc",)),
        ),
    ),
    (
        "Explorer and dialogs",
        (
            ("Search the open Explorer folder", ("Type",)),
            ("Jump a dialog to the Explorer folder", ("Ctrl", "G")),
            ("Give typed text back to the dialog", ("Esc",)),
        ),
    ),
)


def _shortcuts_page() -> list[QWidget]:
    sections: list[QWidget] = []
    for title, rows in SHORTCUTS:
        card = Card()
        for action, *combinations in rows:
            card.add_row(action, "", key_caps(*combinations))
        sections += [section_title(title), card]
    return sections


def _with_unit(field: QWidget, unit: str) -> QWidget:
    """A row control: the field with its unit beside it."""
    control = QWidget()
    layout = QHBoxLayout(control)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    layout.addWidget(field)
    label = QLabel(unit)
    label.setObjectName("unit")
    layout.addWidget(label)
    return control


def _note(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setObjectName("muted")
    return label


def dropped_folders(mime: QMimeData) -> list[Path]:
    """Local folders in a drag; dropped files and remote URLs are ignored."""
    folders = []
    for url in mime.urls():
        if url.isLocalFile():
            path = Path(url.toLocalFile())
            if path.is_dir():
                folders.append(path)
    return folders


class FolderDropList(QListWidget):
    """A list that accepts folders dragged from Explorer, several at a time."""

    folders_dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QListWidget.DragDropMode.DropOnly)
        self.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)

    def dragEnterEvent(self, event) -> None:
        if dropped_folders(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        self.dragEnterEvent(event)

    def dropEvent(self, event) -> None:
        folders = dropped_folders(event.mimeData())
        if not folders:
            event.ignore()
            return
        event.acceptProposedAction()
        self.folders_dropped.emit(folders)


class PathList(QWidget):
    """An editable list of folders, without duplicate Windows path identities.

    Folders can be added with the button or dragged in from Explorer. Each row keeps
    its path apart from its text, which subclasses may extend.
    """

    def __init__(self, paths: tuple[str, ...], accessible_name: str, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.list = FolderDropList()
        self.list.setObjectName("itemList")
        self.list.setFixedHeight(PATH_LIST_HEIGHT)
        self.list.setAccessibleName(accessible_name)
        self.list.setToolTip("Drag folders here from Explorer, or use Add folder…")
        self.list.folders_dropped.connect(self.add_folders)
        layout.addWidget(self.list)
        self.buttons = QHBoxLayout()
        add, remove = QPushButton("Add folder…"), QPushButton("Remove selected")
        add.clicked.connect(self.add_folder)
        remove.clicked.connect(self.remove_selected)
        self.buttons.addWidget(add)
        self.buttons.addWidget(remove)
        self.buttons.addStretch()
        layout.addLayout(self.buttons)
        for path in paths:
            self._add_row(path)

    def _add_row(self, path: str) -> QListWidgetItem:
        item = QListWidgetItem(path)
        item.setData(Qt.ItemDataRole.UserRole, path)
        self.list.addItem(item)
        return item

    def add_folder(self) -> None:
        if folder := pick_path(self, "Choose folder"):
            self.add_folders([folder])

    def add_folders(self, folders: list[Path]) -> None:
        known = {path_key(path) for path in self.paths()}
        for folder in folders:
            if path_key(folder) not in known:
                known.add(path_key(folder))
                self._add_row(str(folder))

    def remove_selected(self) -> None:
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))

    def paths(self) -> tuple[str, ...]:
        return tuple(item.data(Qt.ItemDataRole.UserRole) for item in self._items())

    def _items(self) -> list[QListWidgetItem]:
        return [self.list.item(row) for row in range(self.list.count())]


class SearchFolderList(PathList):
    """Search folders, each with how many levels below it are listed.

    The depth of the selected folders is chosen beside the buttons; a row shows
    its own, such as D:\\Projects · 3 levels.
    """

    def __init__(
        self,
        paths: tuple[str, ...],
        depths: tuple[FolderDepth, ...],
        catalog: CatalogStore | None = None,
        parent=None,
    ):
        self._depths = {path_key(rule.path): rule.levels for rule in depths}
        self.catalog = catalog  # Counts each folder's entries; None shows no counts.
        super().__init__(paths, "Search folders", parent)
        self.depth = QComboBox()
        self.depth.setAccessibleName("Depth of the selected search folders")
        self.depth.setToolTip(
            "How many levels below the folder are searched, such as 3 for D:\\Projects: "
            "its groups, their projects and what lies directly in them."
        )
        self.depth.addItem("All levels", None)
        for levels in range(1, DEPTH_CHOICES + 1):
            self.depth.addItem(_levels_text(levels), levels)
        label = QLabel("Depth")
        label.setObjectName("unit")
        self.buttons.addWidget(label)
        self.buttons.addWidget(self.depth)
        self.depth.activated.connect(self._depth_chosen)
        self.list.itemSelectionChanged.connect(self._show_selected_depth)
        self.effect = QLabel()  # What the selected folder's depth leaves out.
        self.effect.setObjectName("unit")
        self.effect.setWordWrap(True)
        self.layout().addWidget(self.effect)
        self._show_selected_depth()

    def depths(self) -> tuple[FolderDepth, ...]:
        """The limited folders among the listed ones."""
        limited = []
        for item in self._items():
            levels = item.data(DEPTH_ROLE)
            if levels is not None:
                limited.append(FolderDepth(item.data(Qt.ItemDataRole.UserRole), levels))
        return tuple(limited)

    def _add_row(self, path: str) -> QListWidgetItem:
        item = super()._add_row(path)
        self._set_depth(item, self._depths.get(path_key(path)))
        return item

    def set_depth(self, path: str, levels: int | None) -> None:
        """Give the listed folder `path` a depth, such as a suggestion of the analysis."""
        for item in self._items():
            if path_key(item.data(Qt.ItemDataRole.UserRole)) == path_key(path):
                self._set_depth(item, levels)
        self._show_selected_depth()

    def _set_depth(self, item: QListWidgetItem, levels: int | None) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        item.setData(DEPTH_ROLE, levels)
        parts = [path]
        if levels is not None:
            parts.append(_levels_text(levels))
        if (count := self._listed(path)) is not None:
            parts.append(f"{count:,} entries")
        item.setText("   ·   ".join(parts))

    def _listed(self, path: str) -> int | None:
        """Entries the catalog lists for the folder now; None without the catalog."""
        if self.catalog is None:
            return None
        return self.catalog.count_listed(CatalogSource(Path(path)).id)

    def _depth_chosen(self, index: int) -> None:
        levels = self.depth.itemData(index)
        for item in self.list.selectedItems():
            self._set_depth(item, levels)
        self._show_effect()

    def _show_selected_depth(self) -> None:
        selected = self.list.selectedItems()
        self.depth.setEnabled(bool(selected))
        if selected:
            levels = selected[0].data(DEPTH_ROLE)
            if self.depth.findData(levels) < 0:  # Set in the settings file beyond the choices.
                self.depth.addItem(_levels_text(levels), levels)
            self.depth.setCurrentIndex(self.depth.findData(levels))
        self._show_effect()

    def _show_effect(self) -> None:
        """For the selected folder with a depth: what of its entries listed now goes."""
        selected = self.list.selectedItems()
        levels = selected[0].data(DEPTH_ROLE) if selected else None
        if self.catalog is None or levels is None:
            self.effect.hide()
            return
        path = selected[0].data(Qt.ItemDataRole.UserRole)
        source = CatalogSource(Path(path)).id
        deeper = self.catalog.count_deeper(source, Path(path), levels)
        listed = self.catalog.count_listed(source)
        self.effect.setText(
            f"At {_levels_text(levels)}, {deeper:,} of the {listed:,} entries listed now "
            "go at the next scan."
            if deeper
            else f"At {_levels_text(levels)}, all {listed:,} entries listed now stay."
        )
        self.effect.show()


def _levels_text(levels: int) -> str:
    return "1 level" if levels == 1 else f"{levels} levels"


class _PageSpec(NamedTuple):
    icon: str
    title: str
    subtitle: str
    build: Callable[[], list[QWidget]]
    # Copies the page's fields into the settings; None for pages without fields.
    read: Callable[[Settings], Settings] | None = None


class SettingsDialog(QDialog):
    def __init__(
        self,
        settings: Settings,
        parent=None,
        *,
        save: Callable[[Settings], None] | None = None,
        startup_enabled: bool | None = None,
        set_startup: Callable[[bool], None] | None = None,
        startup_state: Callable[[], bool] | None = None,
        website_icons: WebsiteIcons,
        plugins: PluginScheduler | None = None,
        plugin_folder: str = "",
        plugin_sources: SourceWatcher | None = None,
        plugin_cards: Path | None = None,
        placement: BarPlacement | None = None,
        restart: Callable[[], None] | None = None,
        replace_all: Callable[[Settings], None] | None = None,
        catalog: CatalogStore | None = None,
        tasks: BackgroundTasks | None = None,
    ):
        super().__init__(parent)
        self.original = settings
        self.result_settings = settings
        self.persist = save
        # Applies imported settings and shows them in a new dialog; None hides Import.
        self.replace_all = replace_all
        # Counts entries per search folder and feeds Analyze catalog…; None hides both.
        self.catalog = catalog
        self.tasks = tasks  # Runs the analysis off the GUI thread; None runs it inline.
        self.analyzing = False
        self.set_startup = set_startup
        self.startup_state = startup_state
        self.restart_app = restart
        # Applies at once, like Start with Windows; None hides the switch.
        self.placement = placement
        self.setObjectName("settings")
        self.setWindowTitle("WinLauncher settings")
        self.resize(DIALOG_SIZE)
        self.startup_enabled = startup_enabled
        self.website_icons = website_icons
        # Built with the first plugin page shown; see `plugin_pages`.
        self._plugin_pages: PluginPages | None = None
        self._plugin_arguments = (
            (plugins, plugin_folder, plugin_sources, restart, plugin_cards)
            if plugins is not None
            else None
        )
        self.pages = QStackedWidget()
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setAccessibleName("Settings pages")
        self.nav.setIconSize(QSize(NAV_ICON_SIZE, NAV_ICON_SIZE))
        self._specs = self._page_specs()
        self.nav_icons = [spec.icon for spec in self._specs]
        # A page is built when it is first shown: building all of them took most
        # of a second. Save reads only built pages; the others keep `original`.
        self._built: set[int] = set()
        for spec in self._specs:
            self.nav.addItem(QListWidgetItem(spec.title))
            self.pages.addWidget(QWidget())
        self._use_theme_icons()
        theme().changed.connect(self._use_theme_icons)
        self.nav.currentRowChanged.connect(self._show_row)
        self.nav.setCurrentRow(0)
        self.search = SettingsSearch(self._searchable, self)
        self.search.active_changed.connect(lambda active: self.nav.setVisible(not active))
        self.search.chosen.connect(self._open_setting)
        QShortcut(QKeySequence.StandardKey.Find, self, self.search.focus)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        # Save keeps the dialog open; Cancel (Close after a save) leaves it.
        self.buttons.accepted.connect(self.save)
        self.buttons.rejected.connect(self.reject)
        self.saved_message = QLabel()
        self.saved_message.setObjectName("savedMessage")
        self.saved_message_timer = QTimer(self)
        self.saved_message_timer.setSingleShot(True)
        self.saved_message_timer.setInterval(SAVED_MESSAGE_MS)
        self.saved_message_timer.timeout.connect(self.saved_message.clear)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._sidebar())
        content = QVBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(0)
        content.addWidget(self.pages, 1)
        footer = self._footer()
        content.addWidget(footer)
        layout.addLayout(content, 1)
        # The pages, not the search field, take the focus: it would light up at once.
        self.nav.setFocus()
        self._fit_screen()

    # Pages ----------------------------------------------------------------------

    def _page_specs(self) -> list[_PageSpec]:
        plugins = (
            [
                _PageSpec(
                    "plug",
                    "Plugins",
                    "Chats and contacts from other programs as results: install them here.",
                    lambda: self.plugin_pages.install_sections,
                ),
                _PageSpec(
                    "gear",
                    "Plugin settings",
                    "Each plugin's switch, keywords and options. A keyword puts one plugin "
                    "on top, @keyword answers from it alone.",
                    lambda: self.plugin_pages.settings_sections,
                ),
            ]
            if self._plugin_arguments is not None
            else []
        )
        return [
            _PageSpec(
                "sliders",
                "General",
                "Startup and how the search bar behaves.",
                self._general_page,
                self._read_general,
            ),
            _PageSpec(
                "folder",
                "Search folders",
                "What WinLauncher indexes. Names and paths stay on this computer.",
                self._folders_page,
                self._read_folders,
            ),
            _PageSpec(
                "window",
                "Explorer & dialogs",
                "Search right where you already are.",
                self._explorer_page,
                self._read_explorer,
            ),
            _PageSpec(
                "ban",
                "Exclusions",
                "Keep folders and files out of the catalog.",
                self._exclusions_page,
                self._read_exclusions,
            ),
            _PageSpec(
                "globe",
                "Websites",
                "Type a keyword to search a website, such as az headphones.",
                self._websites_page,
                lambda settings: replace(settings, websites=self.websites.websites()),
            ),
            _PageSpec(
                "terminal",
                "Context menu",
                "Your own entries in a result's right-click menu, such as Open in Terminal.",
                self._menu_commands_page,
                lambda settings: replace(settings, menu_commands=self.menu_commands.commands()),
            ),
            *plugins,
            _PageSpec(
                "chart",
                "Ranking",
                "Lift or lower folders in the results.",
                self._ranking_page,
                self._read_ranking,
            ),
            _PageSpec(
                "code",
                "Advanced rules",
                "Name, extension and attribute rules as JSON.",
                self._advanced_page,
                self._read_advanced,
            ),
            _PageSpec(
                "keyboard",
                "Shortcuts",
                "Everything WinLauncher does from the keyboard.",
                _shortcuts_page,
            ),
        ]

    def _show_row(self, row: int) -> None:
        if row >= 0:
            self._build_page(row)
            self.pages.setCurrentIndex(row)

    def _build_page(self, row: int) -> None:
        if row in self._built:
            return
        self._built.add(row)
        spec = self._specs[row]
        page = _page(spec.title, spec.subtitle, spec.build())
        placeholder = self.pages.widget(row)
        self.pages.insertWidget(row, page)
        self.pages.removeWidget(placeholder)
        placeholder.deleteLater()

    def _searchable(self) -> list[SettingEntry]:
        """Every page and its cards' titled rows; searching builds the pages not shown yet."""
        self.build_all_pages()
        entries = []
        for row, spec in enumerate(self._specs):
            entries.append(SettingEntry(row, spec.title, spec.title, spec.subtitle))
            content = self.pages.widget(row).widget()
            for card in content.findChildren(Card):
                # Plugin cards' rows share titles; the card's header names whose they are.
                details = f"{spec.title} › {card.heading}" if card.heading else spec.title
                for index, (title, description, widget) in enumerate(card.rows):
                    if title:
                        page = details if index else spec.title  # The header is the first row.
                        entries.append(SettingEntry(row, page, title, description, widget))
        return entries

    def _open_setting(self, entry: SettingEntry) -> None:
        """Show the entry's page, scrolled to its row, which lights up for a moment."""
        self.nav.setCurrentRow(entry.page_row)
        scroll = self.pages.widget(entry.page_row)
        target = entry.widget
        if target is None:
            scroll.verticalScrollBar().setValue(0)
            return
        content = scroll.widget()
        while not target.isVisibleTo(content) and target.parentWidget() is not content:
            target = target.parentWidget()  # A folded detail: show its card instead.
        # After the page's layout, so the row's position is known.
        QTimer.singleShot(0, scroll, lambda: _reveal(scroll, target))

    def build_all_pages(self) -> None:
        """Build the pages not shown yet, such as for code that reads their fields."""
        for row in range(len(self._specs)):
            self._build_page(row)

    def __getattr__(self, name: str):
        """A field of a page not built yet: build the pages, then look again.

        Only reached when normal lookup fails, so built fields cost nothing.
        """
        pending = self.__dict__.get("_specs") is not None and len(
            self.__dict__.get("_built", ())
        ) < len(self.__dict__["_specs"])
        if name.startswith("__") or not pending:
            raise AttributeError(name)
        self.build_all_pages()
        return super().__getattribute__(name)

    @property
    def plugin_pages(self) -> PluginPages | None:
        """The plugin pages' widgets, built at first use; None without plugins."""
        if self._plugin_pages is None and self._plugin_arguments is not None:
            from winlauncher.ui.plugin_settings import PluginPages  # It builds on this module.

            plugins, folder, sources, restart, cards = self._plugin_arguments
            self._plugin_pages = PluginPages(
                self.original,
                plugins,
                self._settings_without_plugins,
                folder,
                self,
                sources,
                self.save_and_restart if restart is not None else None,
                cards,
            )
        return self._plugin_pages

    # Frame ----------------------------------------------------------------------

    def _fit_screen(self) -> None:
        """Open at `DIALOG_SIZE`, but never taller than the screen leaves room for."""
        if (screen := self.screen()) is not None:
            room = screen.availableGeometry().height() - SCREEN_MARGIN
            self.resize(self.width(), min(DIALOG_SIZE.height(), room))

    def _sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(SIDEBAR_WIDTH)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(12, 18, 12, 14)
        layout.setSpacing(14)
        brand = QHBoxLayout()
        brand.setContentsMargins(8, 0, 0, 0)
        brand.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(app_icon().pixmap(BRAND_ICON_SIZE, BRAND_ICON_SIZE))
        names = QVBoxLayout()
        names.setSpacing(0)
        product = QLabel("WinLauncher")
        product.setObjectName("brandName")
        version = QLabel(f"Version {__version__}")
        version.setObjectName("brandVersion")
        details = QHBoxLayout()
        details.setSpacing(6)
        details.addWidget(version)
        details.addWidget(_engine_chip())
        details.addStretch(1)
        names.addWidget(product)
        names.addLayout(details)
        brand.addWidget(logo)
        brand.addLayout(names, 1)
        layout.addLayout(brand)
        layout.addWidget(self.search.field)
        layout.addWidget(self.nav, 1)
        layout.addWidget(self.search.results, 1)
        hint = QLabel("Changes apply when you save; autostart applies at once.")
        hint.setObjectName("sidebarHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return sidebar

    def _footer(self) -> QFrame:
        footer = QFrame()
        footer.setObjectName("footer")
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(24, 12, 20, 12)
        layout.addWidget(self.saved_message, 1)
        layout.addWidget(self.buttons)
        return footer

    # Pages ----------------------------------------------------------------------

    def _general_page(self) -> list[QWidget]:
        self.autostart = ToggleSwitch("Start WinLauncher when I sign in to Windows")
        self.autostart.setEnabled(self.set_startup is not None)
        self.autostart.setChecked(bool(self.startup_enabled))
        self.autostart.clicked.connect(self._toggle_startup)
        startup = Card()
        startup.add_row(
            "Start with Windows",
            "Launch WinLauncher in the tray when you sign in. Applies immediately.",
            self.autostart,
        )
        settings = self.original
        self.ctrl_interval = sized_number(
            NumberField(DOUBLE_CTRL_RANGE_MS, settings.double_ctrl_ms, "ms")
        )
        self.ctrl_interval.setAccessibleName("Double-Ctrl interval in milliseconds")
        self.auto_hide = ToggleSwitch("Close the empty launcher when it loses focus")
        self.auto_hide.setChecked(settings.auto_hide)
        self.auto_hide.setToolTip(
            "The search bar inside Explorer closes as soon as it loses focus."
        )
        self.auto_hide_delay = sized_number(
            NumberField(AUTO_HIDE_RANGE_SECONDS, settings.auto_hide_seconds, "seconds")
        )
        self.auto_hide_delay.setAccessibleName("Close the empty launcher after seconds")
        self.auto_hide_delay.setEnabled(settings.auto_hide)
        self.auto_hide.toggled.connect(self.auto_hide_delay.setEnabled)
        self.limit = sized_number(NumberField(RESULT_LIMIT_RANGE, settings.max_results))
        self.limit.setAccessibleName("Maximum results")
        bar = Card()
        bar.add_row(
            "Double-Ctrl speed",
            "How quickly the second Ctrl press must follow the first.",
            _with_unit(self.ctrl_interval, "ms"),
        )
        bar.add_row(
            "Close when unused",
            "Hide the empty bar after you switch to another window.",
            self.auto_hide,
        )
        bar.add_row(
            "Close after",
            "Time without focus before it closes.",
            _with_unit(self.auto_hide_delay, "s"),
        )
        bar.add_row(
            "Maximum results", "How many results a search lists.", _with_unit(self.limit, "")
        )
        self.file_preview = ToggleSwitch("Preview the selected file with Alt+P")
        self.file_preview.setChecked(settings.file_preview)
        bar.add_row(
            "File preview",
            "Alt+P (or F3) shows images, PDF pages, text and folder contents beside the results. "
            "The bar opens with it as you left it.",
            self.file_preview,
        )
        if self.placement is not None:
            self.centered = ToggleSwitch("Open the search bar centered")
            self.centered.setChecked(self.placement.centered)
            self.centered.clicked.connect(self.placement.set_centered)
            self.placement.centered_changed.connect(self.centered.setChecked)
            bar.add_row(
                "Center search bar",
                "Open it centered on the screen. Dragging the bar elsewhere turns this off; "
                "drag its left or right edge to change its width. Applies immediately.",
                self.centered,
            )
        self.appearance = QComboBox()
        self.appearance.setAccessibleName("Appearance")
        for label, appearance in APPEARANCE_CHOICES:
            self.appearance.addItem(label, appearance.value)
        self.appearance.setCurrentIndex(self.appearance.findData(settings.appearance))
        looks = Card()
        looks.add_row(
            "Theme",
            "Light or dark colors; Match Windows follows your Windows setting.",
            self.appearance,
        )
        transfer = Card()
        transfer.add_row(
            "Export settings",
            "Save all settings as shown here to a file, for another computer or as a backup. "
            "Plugin sign-ins are not included.",
            _button("Export…", self.export_settings),
        )
        if self.replace_all is not None:
            transfer.add_row(
                "Import settings",
                "Replace all settings with exported ones. Search folders that do not exist on "
                "this computer are kept, but find nothing.",
                _button("Import…", self.import_settings),
            )
        return [
            section_title("Startup"),
            startup,
            section_title("Appearance"),
            looks,
            section_title("Search bar"),
            bar,
            section_title("Settings file"),
            transfer,
        ]

    def export_settings(self) -> None:
        """Write the settings as shown, unsaved edits included, to a file the user picks."""
        try:
            settings = self.read_settings()
        except ValueError as exc:
            message(self, "Check your settings", str(exc), WARNING)
            return
        path = pick_save_path(self, "Export settings", EXPORT_FILE_NAME, SETTINGS_FILE_FILTER)
        if path is None:
            return
        try:
            SettingsFile(path).save(settings)
        except OSError as exc:
            message(self, "Settings not exported", str(exc), ERROR)
            return
        message(self, "Settings exported", str(path), INFO)

    def import_settings(self) -> None:
        """Replace every setting with an exported file's, after the user confirmed it."""
        path = pick_path(self, "Import settings", SETTINGS_FILE_FILTER)
        if path is None or self.replace_all is None:
            return
        try:
            settings = read_exported_settings(path)
        except (OSError, ValueError) as exc:
            message(self, "Settings not imported", str(exc), ERROR)
            return
        if ask(
            self,
            "Replace all settings?",
            f"Every setting, including unsaved changes here, is replaced by those in "
            f"{path.name}. Export the current ones first to keep them.",
            "Replace",
            kind=WARNING,
        ):
            self.replace_all(settings)

    def analyze_catalog(self) -> None:
        """Suggest how to thin out the catalog; ticked suggestions fill in the fields."""
        if self.catalog is None:
            return
        try:
            settings = self.read_settings()
        except ValueError as exc:
            message(self, "Check your settings", str(exc), WARNING)
            return
        if self.analyzing:
            return
        catalog = self.catalog

        def work() -> list[Suggestion]:
            return analyze(settings, catalog.catalog_paths(), catalog.opened_keys())

        self.analyzing = True
        self.analyze_button.setEnabled(False)
        self.analyze_button.setText("Analyzing…")
        if self.tasks is None:
            self._analyzed(work())
        else:
            self.tasks.submit(TaskCategory.PREFETCH, work, self._analyzed, self._analysis_failed)

    def _analysis_failed(self, failure: str) -> None:
        if not self._analysis_done():
            return
        message(self, "Analysis failed", failure, ERROR)

    def _analysis_done(self) -> bool:
        """Re-enable Analyze; False once closing Settings deleted the dialog meanwhile."""
        if not isValid(self):
            return False
        self.analyzing = False
        self.analyze_button.setEnabled(True)
        self.analyze_button.setText("Analyze…")
        return True

    def _analyzed(self, suggestions: list[Suggestion]) -> None:
        """Offer the suggestions, unless the dialog went away meanwhile."""
        if not self._analysis_done() or not self.isVisible():
            return
        if not suggestions:
            message(
                self,
                "Nothing to thin out",
                "No large folder piles up entries you never open.",
                INFO,
            )
            return
        choices = [
            Choice(str(index), suggestion.title, suggestion.detail, checked=not suggestion.opened)
            for index, suggestion in enumerate(suggestions)
        ]
        ticked = choose_items(
            self,
            "Analyze catalog",
            "Ticked suggestions fill in the settings. Nothing changes before you save; "
            "the next scan applies them.",
            choices,
        )
        if not ticked:
            return
        for index, suggestion in enumerate(suggestions):
            if str(index) in ticked:
                self._apply_suggestion(suggestion)

    def _apply_suggestion(self, suggestion: Suggestion) -> None:
        if suggestion.kind == SuggestionKind.DEPTH:
            self.roots.set_depth(suggestion.path, suggestion.levels)
        elif suggestion.kind == SuggestionKind.PROJECTS:
            self.code_projects.setChecked(True)
        else:
            line = f"{LOW_WEIGHT} | {suggestion.path} | {SearchPriority.LOW}"
            current = self.priorities.toPlainText().rstrip("\n")
            self.priorities.setPlainText(f"{current}\n{line}" if current else line)

    def show_page(self, title: str) -> None:
        """Open the page with this title, such as Search folders."""
        for row in range(self.nav.count()):
            if self.nav.item(row).text() == title:
                self.nav.setCurrentRow(row)
                return

    def _folders_page(self) -> list[QWidget]:
        settings = self.original
        self.roots = SearchFolderList(settings.roots, settings.folder_depths, self.catalog)
        self.analyze_button = _button("Analyze…", self.analyze_catalog)
        self.code_projects = ToggleSwitch("List code projects with their top level only")
        self.code_projects.setChecked(settings.code_projects_top_level)
        self.apps = ToggleSwitch("Include Start menu, Store and registered applications")
        self.apps.setChecked(settings.index_apps)
        self.windows_tools = ToggleSwitch("Include Windows tools and settings")
        self.windows_tools.setChecked(settings.index_windows_tools)
        self.windows_settings_detail = QComboBox()
        self.windows_settings_detail.setAccessibleName("Windows settings detail")
        for label, detail in WINDOWS_SETTINGS_DETAIL_CHOICES:
            self.windows_settings_detail.addItem(label, detail.value)
        self.windows_settings_detail.setCurrentIndex(
            self.windows_settings_detail.findData(settings.windows_settings_detail)
        )
        self.windows_settings_detail.setEnabled(settings.index_windows_tools)
        self.windows_tools.toggled.connect(self.windows_settings_detail.setEnabled)
        self.windows_tools_english = ToggleSwitch("Also add their English names")
        self.windows_tools_english.setChecked(settings.windows_tools_english)
        # English names would only repeat the names of a Windows shown in English.
        languages = display_languages()
        english_windows = not languages or languages[0].startswith("en")
        self.windows_tools_english.setEnabled(settings.index_windows_tools and not english_windows)
        if not english_windows:
            self.windows_tools.toggled.connect(self.windows_tools_english.setEnabled)
        self.scan_interval = sized_number(
            NumberField(RESCAN_RANGE_SECONDS, settings.rescan_seconds, "seconds")
        )
        self.scan_interval.setAccessibleName("Full refresh interval in seconds")
        self.content_threads = sized_number(
            NumberField(CONTENT_SEARCH_THREADS_RANGE, settings.content_search_threads)
        )
        self.content_threads.setAccessibleName("Content search threads")
        self.content_threads.setToolTip(
            "1–16 parallel file scans for contains:, content: and rg: (Rust only). "
            "More threads can increase disk activity and memory use."
        )
        folders = Card()
        folders.add_block(
            "Folders",
            "Drag folders here from Explorer, or add them. Depth limits how far below a "
            "folder is searched; a folder inside it may reach deeper.",
            self.roots,
        )
        folders.add_row(
            "Code projects: top level only",
            "A folder with .git, pyproject.toml, package.json or the like lists only what "
            "lies directly in it, wherever it is. A project chosen as search folder is "
            "listed in full.",
            self.code_projects,
        )
        if self.catalog is not None:
            folders.add_row(
                "Analyze catalog",
                "Where many entries pile up that you never open, and what would thin them "
                "out. Ticked suggestions fill in the settings; Save keeps them.",
                self.analyze_button,
            )
        folders.add_row(
            "Include applications",
            "Start menu shortcuts, Microsoft Store apps and registered programs.",
            self.apps,
        )
        folders.add_row(
            "Include Windows tools and settings",
            "Control Panel tasks, Settings pages, consoles such as services.msc "
            "and system tools such as Task Manager, named in your Windows language.",
            self.windows_tools,
        )
        folders.add_row(
            "Settings detail",
            "Every setting, such as Change mouse pointer size, or only whole pages "
            "of Settings and Control Panel.",
            self.windows_settings_detail,
        )
        folders.add_row(
            "Also in English",
            "Adds the English names, such as Device Manager or Power Options, "
            "which guides on the web use. For Windows in another language.",
            self.windows_tools_english,
        )
        catalog = Card()
        catalog.add_row(
            "Full refresh every",
            "Complete rescans. Folder watching applies changes live in between.",
            _with_unit(self.scan_interval, "s"),
        )
        catalog.add_row(
            "Content search threads",
            "Parallel file scans for contains:. More can mean more disk activity.",
            _with_unit(self.content_threads, ""),
        )
        return [folders, section_title("Catalog"), catalog]

    def _explorer_page(self) -> list[QWidget]:
        settings = self.original
        self.explorer = ToggleSwitch("Search when typing in Explorer's file view")
        self.explorer.setChecked(settings.explorer_typing)
        self.explorer_tool_order = QComboBox()
        self.explorer_tool_order.setAccessibleName("Explorer tool order")
        self.explorer_tool_order.addItem("Tools first", True)
        self.explorer_tool_order.addItem("Files first", False)
        self.explorer_tool_order.setCurrentIndex(
            self.explorer_tool_order.findData(settings.explorer_tools_first)
        )
        self.explorer_tools_fuzzy = ToggleSwitch("Fuzzy matching for tools")
        self.explorer_tools_fuzzy.setChecked(settings.explorer_tools_fuzzy)
        self.quick_switch = ToggleSwitch("Quick Switch in Open and Save dialogs")
        self.quick_switch.setChecked(settings.quick_switch)
        self.quick_switch.setToolTip(
            "Choosing a folder opens it in the dialog; choosing a file enters its name "
            "without confirming."
        )
        card = Card()
        card.add_row(
            "Type to search in Explorer",
            "Typing in a folder's file list opens a compact bar for that folder; Explorer "
            "follows the result you pick.",
            self.explorer,
        )
        card.add_row(
            "Quick Switch",
            "Type in an Open or Save dialog to search everywhere. Ctrl+G jumps the dialog "
            "to the folder open in Explorer.",
            self.quick_switch,
        )
        card.add_row(
            "Explorer tool order",
            "Commands enabled under Context menu can appear before or after direct folder "
            "entries. Matches in subfolders follow both groups.",
            self.explorer_tool_order,
        )
        card.add_row(
            "Fuzzy matching for tools",
            "Allow loose letter matches in Explorer tool titles and keywords. Off: Cla "
            "matches Claude, cud does not. File matching is independent.",
            self.explorer_tools_fuzzy,
        )
        return [card]

    def _exclusions_page(self) -> list[QWidget]:
        settings = self.original
        self.excluded = PathList(settings.excluded_paths, "Excluded directories")
        self.patterns = QPlainTextEdit("\n".join(settings.excluded_patterns))
        self.patterns.setAccessibleName("Excluded file and folder patterns, one per line")
        self.patterns.setMinimumHeight(PATTERNS_HEIGHT)
        self.patterns.setToolTip(
            "Names such as node_modules or *.lnk, or folder paths such as\n"
            "Projects\\Android\\*\\build (* = one folder) or Android/**/build (** = any depth)."
        )
        self.markers = QLineEdit(", ".join(settings.marker_files))
        self.markers.setAccessibleName("Marker file names, comma-separated")
        self.keep_marked = ToggleSwitch("Keep marked folders themselves findable")
        self.keep_marked.setChecked(settings.keep_marked_folders)
        card = Card()
        card.add_block(
            "Excluded folders and entries",
            "Skipped with everything inside them; single entries come from a result's menu.",
            self.excluded,
        )
        card.add_block(
            "Patterns",
            "One per line: names such as node_modules or *.lnk, or paths such as Android/**/build.",
            self.patterns,
        )
        card.add_block(
            "Marker files",
            "A folder that contains one of these files is skipped entirely. Separate "
            "names with commas.",
            self.markers,
        )
        card.add_row(
            "Keep marked folders findable",
            "The marked folder itself still shows up in results; only what is inside "
            "it is skipped.",
            self.keep_marked,
        )
        return [card]

    def _websites_page(self) -> list[QWidget]:
        self.websites = WebsiteList(self.original.websites, self.website_icons)
        card = Card()
        card.add_block("", "", self.websites)
        return [card]

    def _menu_commands_page(self) -> list[QWidget]:
        self.menu_commands = CommandList(self.original.menu_commands)
        card = Card()
        card.add_block("", "", self.menu_commands)
        return [card]

    def _ranking_page(self) -> list[QWidget]:
        settings = self.original
        self.rank_projects = ToggleSwitch("Rank code projects first")
        self.rank_projects.setChecked(settings.rank_code_projects)
        projects = Card()
        projects.add_row(
            "Code projects first",
            "Lift the folders of code projects, such as git checkouts, in the results; "
            "typing a project's name finds its folder first.",
            self.rank_projects,
        )
        self.priorities = QPlainTextEdit(format_folder_priorities(settings.folder_priorities))
        self.priorities.setPlaceholderText(f"{FOLDER_PRIORITY_FORMAT}\n-20 | C:\\Archive | low")
        self.priorities.setAccessibleName("Folder priorities, one per line")
        self.priorities.setMinimumHeight(PRIORITIES_HEIGHT)
        card = Card()
        card.add_block(
            "Folder priorities",
            "One per line: boost from -100 to 100 | folder | optional class (high, normal, "
            "low or ignored). The most specific folder wins.",
            self.priorities,
        )
        note = _note(
            "Favorites and what you open also lift results. Exact names and name prefixes "
            "score highest."
        )
        return [projects, card, note]

    def _advanced_page(self) -> list[QWidget]:
        settings = self.original
        self.rule_json = QPlainTextEdit(format_advanced_rules(settings))
        self.rule_json.setAccessibleName("Advanced priority rules as JSON")
        self.rule_json.setObjectName("code")
        self.rule_json.setMinimumHeight(RULES_HEIGHT)
        card = Card()
        card.add_block(
            "Rules",
            "Filename regex, extension and Windows attribute rules. Each has a "
            "search_priority (ignored, low, normal, high) and a weight from -100 to 100.",
            self.rule_json,
        )
        example = _note(
            'Example extension rule: {"extension": "bak", "weight": -10, "search_priority": "low"}'
        )
        return [card, example]

    # Behavior -----------------------------------------------------------------

    def _toggle_startup(self, enabled: bool) -> None:
        try:
            self.set_startup(enabled)
        except OSError as exc:
            message(self, "Autostart not changed", str(exc), ERROR)
        # Show what the Startup folder actually contains, after success or failure.
        actual = self.startup_state() if self.startup_state else not enabled
        self.autostart.setChecked(actual)

    def _use_theme_icons(self) -> None:
        for row, icon in enumerate(self.nav_icons):
            self.nav.item(row).setIcon(line_icon(icon, colors().faint, colors().accent))

    def add_exclusion(self, *, folder: Path | None = None, pattern: str | None = None) -> None:
        """An exclusion saved elsewhere meanwhile, such as from a result's menu.

        It joins the fields, so a later Save here keeps it instead of dropping it.
        """
        if self._row("Exclusions") not in self._built:
            # Its fields read `original` when they are built.
            original = self.original
            if folder is not None and str(folder) not in original.excluded_paths:
                original = replace(original, excluded_paths=(*original.excluded_paths, str(folder)))
            if pattern and pattern not in original.excluded_patterns:
                original = replace(
                    original, excluded_patterns=(*original.excluded_patterns, pattern)
                )
            self.original = original
            return
        if folder is not None:
            self.excluded.add_folders([folder])
        if pattern and pattern not in _split_lines(self.patterns.toPlainText()):
            self.patterns.appendPlainText(pattern)

    def _row(self, title: str) -> int:
        return next(row for row, spec in enumerate(self._specs) if spec.title == title)

    def read_settings(self) -> Settings:
        """Raise ValueError with a user-facing message for invalid input."""
        settings = self._settings_without_plugins()
        if self._plugin_pages is None:
            return settings  # No plugin page was built, so nothing there changed.
        return self._plugin_pages.apply_to(settings)

    def _settings_without_plugins(self) -> Settings:
        """The built pages' fields over `original`; pages not built changed nothing."""
        settings = self.original
        for row in sorted(self._built):
            if (read := self._specs[row].read) is not None:
                settings = read(settings)
        return settings

    def _read_general(self, settings: Settings) -> Settings:
        return replace(
            settings,
            double_ctrl_ms=self.ctrl_interval.value(),
            auto_hide=self.auto_hide.isChecked(),
            file_preview=self.file_preview.isChecked(),
            auto_hide_seconds=self.auto_hide_delay.value(),
            max_results=self.limit.value(),
            appearance=self.appearance.currentData(),
        )

    def _read_folders(self, settings: Settings) -> Settings:
        return replace(
            settings,
            roots=self.roots.paths(),
            folder_depths=self.roots.depths(),
            code_projects_top_level=self.code_projects.isChecked(),
            index_apps=self.apps.isChecked(),
            index_windows_tools=self.windows_tools.isChecked(),
            windows_tools_english=self.windows_tools_english.isChecked(),
            windows_settings_detail=self.windows_settings_detail.currentData(),
            rescan_seconds=self.scan_interval.value(),
            content_search_threads=self.content_threads.value(),
        )

    def _read_explorer(self, settings: Settings) -> Settings:
        return replace(
            settings,
            explorer_typing=self.explorer.isChecked(),
            explorer_tools_first=self.explorer_tool_order.currentData(),
            explorer_tools_fuzzy=self.explorer_tools_fuzzy.isChecked(),
            quick_switch=self.quick_switch.isChecked(),
        )

    def _read_exclusions(self, settings: Settings) -> Settings:
        return replace(
            settings,
            excluded_paths=self.excluded.paths(),
            excluded_patterns=_split_lines(self.patterns.toPlainText()),
            marker_files=_split_commas(self.markers.text()),
            keep_marked_folders=self.keep_marked.isChecked(),
        )

    def _read_ranking(self, settings: Settings) -> Settings:
        return replace(
            settings,
            rank_code_projects=self.rank_projects.isChecked(),
            folder_priorities=parse_folder_priorities(self.priorities.toPlainText()),
        )

    def _read_advanced(self, settings: Settings) -> Settings:
        advanced = parse_advanced_rules(self.rule_json.toPlainText())
        return replace(
            settings,
            name_priorities=advanced.names,
            extension_priorities=advanced.extensions,
            attribute_priorities=advanced.attributes,
        )

    def save(self) -> bool:
        """Validate and store the edits; the dialog stays open for further changes."""
        self.saved_message.clear()
        try:
            settings = self.read_settings()
        except ValueError as exc:
            message(self, "Check your settings", str(exc), WARNING)
            return False
        try:
            if self.persist:
                self.persist(settings)
        except OSError as exc:
            message(self, "Settings not saved", str(exc), ERROR)
            return False
        self.result_settings = settings
        self.original = settings
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Close")
        self.saved_message.setText("Settings saved.")
        self.saved_message_timer.start()
        return True

    def save_and_restart(self) -> None:
        """Keep the edits, then restart WinLauncher, such as to load a new plugin."""
        if self.save() and self.restart_app is not None:
            self.restart_app()


def _reveal(scroll: QScrollArea, row: QWidget) -> None:
    """Scroll `row` into view and light it up until `FOUND_MS` passed."""
    scroll.ensureWidgetVisible(row, 0, REVEAL_MARGIN)
    row.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
    _set_found(row, True)
    QTimer.singleShot(FOUND_MS, row, lambda: _set_found(row, False))


def _set_found(row: QWidget, found: bool) -> None:
    row.setProperty("found", found)
    row.style().unpolish(row)
    row.style().polish(row)


def _engine_chip() -> QLabel:
    """Which engine searches: Rust, or Python with the reason in the tooltip."""
    try:
        python_requested = requested_engine() == "python"
    except ValueError:
        python_requested = False  # The search itself reports the invalid setting.
    availability = native_availability()
    rust = availability.module is not None and not python_requested
    chip = QLabel("Rust" if rust else "Python")
    chip.setObjectName("rustChip" if rust else "engineChip")
    chip.setToolTip(
        "Searches with the fast Rust engine."
        if rust
        else "Searches with the slower Python engine. "
        + (availability.reason or f"{ENGINE_VARIABLE}=python is set.")
    )
    chip.setAccessibleName(f"Search engine: {chip.text()}")
    return chip
