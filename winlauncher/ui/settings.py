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

from PySide6.QtCore import QMimeData, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QIntValidator
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

from winlauncher import __version__
from winlauncher.catalog.model import path_key
from winlauncher.catalog.priority import (
    AttributePriority,
    ExtensionPriority,
    FolderPriority,
    NamePriority,
    SearchPriority,
)
from winlauncher.platform.windows_tools import display_languages
from winlauncher.settings import (
    AUTO_HIDE_RANGE_SECONDS,
    CONTENT_SEARCH_THREADS_RANGE,
    DOUBLE_CTRL_RANGE_MS,
    RESCAN_RANGE_SECONDS,
    RESULT_LIMIT_RANGE,
    Appearance,
    Settings,
    WindowsSettingsDetail,
    priority_rules,
)
from winlauncher.ui.commands import CommandList
from winlauncher.ui.messages import ERROR, WARNING, message
from winlauncher.ui.plugin_sources import SourceWatcher
from winlauncher.ui.plugins import PluginScheduler
from winlauncher.ui.theme import app_icon, colors, theme
from winlauncher.ui.website_icons import WebsiteIcons
from winlauncher.ui.websites import WebsiteList
from winlauncher.ui.widgets import (
    Card,
    PageHeader,
    ToggleSwitch,
    line_icon,
    pick_path,
    section_title,
)

ADVANCED_RULE_FIELDS = ("name_priorities", "extension_priorities", "attribute_priorities")
SAVED_MESSAGE_MS = 2500  # How long the footer confirms a save.
FOLDER_PRIORITY_FORMAT = "25 | D:\\Projects | high"
DIALOG_SIZE = QSize(920, 660)  # The smallest size; taller where the screen allows.
TALLEST_UNSCROLLED_PAGE = 1  # Search folders: the dialog grows to show it whole.
SCREEN_MARGIN = 40  # Kept free above and below the dialog.
SIDEBAR_WIDTH = 228
NAV_ICON_SIZE = 18
BRAND_ICON_SIZE = 32
PAGE_MARGINS = (32, 26, 32, 24)  # Left, top, right, bottom around a page's cards.
PAGE_SPACING = 10
NUMBER_FIELD_WIDTH = 88
PATH_LIST_HEIGHT = 132
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


class NumberField(QLineEdit):
    """A whole number within `bounds`; arrow keys and the wheel step by one.

    Not a `QSpinBox`: Qt 6.12 collapses the text area of style-sheet spin boxes
    without buttons, which leaves them empty and impossible to type into.
    """

    def __init__(self, bounds: tuple[int, int], value: int, unit: str = ""):
        super().__init__()
        self.bounds = bounds
        self._last_valid = value
        self.setValidator(QIntValidator(*bounds, self))
        self.setValue(value)
        unit_text = f" {unit}" if unit else ""
        self.setToolTip(f"{bounds[0]}–{bounds[1]}{unit_text}; type a value or use the arrow keys")

    def value(self) -> int:
        """The typed number clamped to `bounds`; the last valid value while empty."""
        try:
            number = int(self.text())
        except ValueError:
            return self._last_valid
        return min(max(number, self.bounds[0]), self.bounds[1])

    def setValue(self, value: int) -> None:  # noqa: N802 - mirrors QSpinBox.
        self._last_valid = min(max(value, self.bounds[0]), self.bounds[1])
        self.setText(str(self._last_valid))

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override.
        steps = {Qt.Key.Key_Up: 1, Qt.Key.Key_Down: -1}.get(event.key())
        if steps is None:
            super().keyPressEvent(event)
        else:
            self.setValue(self.value() + steps)

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt override.
        if self.hasFocus() and event.angleDelta().y():
            self.setValue(self.value() + (1 if event.angleDelta().y() > 0 else -1))
        else:
            super().wheelEvent(event)


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


def _keys(*combinations: tuple[str, ...]) -> QWidget:
    """Key caps for one or more key combinations, separated by "or"."""
    keys = QWidget()
    layout = QHBoxLayout(keys)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    for index, combination in enumerate(combinations):
        if index:
            separator = QLabel("or")
            separator.setObjectName("unit")
            layout.addWidget(separator)
        for key in combination:
            cap = QLabel(key)
            cap.setObjectName("kbd")
            layout.addWidget(cap)
    return keys


def _shortcuts_page() -> list[QWidget]:
    sections: list[QWidget] = []
    for title, rows in SHORTCUTS:
        card = Card()
        for action, *combinations in rows:
            card.add_row(action, "", _keys(*combinations))
        sections += [section_title(title), card]
    return sections


def _sized(field: NumberField) -> NumberField:
    field.setFixedWidth(NUMBER_FIELD_WIDTH)
    field.setAlignment(Qt.AlignmentFlag.AlignRight)
    return field


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

    Folders can be added with the button or dragged in from Explorer.
    """

    def __init__(self, paths: tuple[str, ...], accessible_name: str, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.list = FolderDropList()
        self.list.setObjectName("itemList")
        self.list.setFixedHeight(PATH_LIST_HEIGHT)
        self.list.addItems(paths)
        self.list.setAccessibleName(accessible_name)
        self.list.setToolTip("Drag folders here from Explorer, or use Add folder…")
        self.list.folders_dropped.connect(self.add_folders)
        layout.addWidget(self.list)
        buttons = QHBoxLayout()
        add, remove = QPushButton("Add folder…"), QPushButton("Remove selected")
        add.clicked.connect(self.add_folder)
        remove.clicked.connect(self.remove_selected)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch()
        layout.addLayout(buttons)

    def add_folder(self) -> None:
        if folder := pick_path(self, "Choose folder"):
            self.add_folders([folder])

    def add_folders(self, folders: list[Path]) -> None:
        known = {path_key(path) for path in self.paths()}
        for folder in folders:
            if path_key(folder) not in known:
                known.add(path_key(folder))
                self.list.addItem(str(folder))

    def remove_selected(self) -> None:
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))

    def paths(self) -> tuple[str, ...]:
        return tuple(self.list.item(row).text() for row in range(self.list.count()))


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
        restart: Callable[[], None] | None = None,
    ):
        super().__init__(parent)
        self.original = settings
        self.result_settings = settings
        self.persist = save
        self.set_startup = set_startup
        self.startup_state = startup_state
        self.restart_app = restart
        self.setObjectName("settings")
        self.setWindowTitle("WinLauncher settings")
        self.resize(DIALOG_SIZE)
        self.websites = WebsiteList(settings.websites, website_icons)
        self.menu_commands = CommandList(settings.menu_commands)
        self.plugin_page = None
        if plugins is not None:
            from winlauncher.ui.plugin_settings import PluginPage  # It builds on this module.

            self.plugin_page = PluginPage(
                settings,
                plugins,
                self._settings_without_plugins,
                plugin_folder,
                self,
                plugin_sources,
                self.save_and_restart if restart is not None else None,
            )
        self.pages = QStackedWidget()
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setAccessibleName("Settings pages")
        self.nav.setIconSize(QSize(NAV_ICON_SIZE, NAV_ICON_SIZE))
        self.nav_icons: list[str] = []
        for icon, title, subtitle, page in (
            (
                "sliders",
                "General",
                "Startup and how the search bar behaves.",
                self._general_page(startup_enabled),
            ),
            (
                "folder",
                "Search folders",
                "What WinLauncher indexes. Names and paths stay on this computer.",
                self._folders_page(settings),
            ),
            (
                "window",
                "Explorer & dialogs",
                "Search right where you already are.",
                self._explorer_page(settings),
            ),
            (
                "ban",
                "Exclusions",
                "Keep folders and files out of the catalog.",
                self._exclusions_page(settings),
            ),
            (
                "globe",
                "Websites",
                "Type a keyword to search a website, such as az headphones.",
                self._websites_page(),
            ),
            (
                "terminal",
                "Context menu",
                "Your own entries in a result's right-click menu, such as Open in Terminal.",
                self._menu_commands_page(),
            ),
            *(
                (
                    (
                        "plug",
                        "Plugins",
                        "Chats and contacts from other programs as results. A keyword puts one "
                        "plugin on top, @keyword answers from it alone.",
                        self.plugin_page.sections,
                    ),
                )
                if self.plugin_page is not None
                else ()
            ),
            (
                "chart",
                "Ranking",
                "Lift or lower folders in the results.",
                self._ranking_page(settings),
            ),
            (
                "code",
                "Advanced rules",
                "Name, extension and attribute rules as JSON.",
                self._advanced_page(settings),
            ),
            (
                "keyboard",
                "Shortcuts",
                "Everything WinLauncher does from the keyboard.",
                _shortcuts_page(),
            ),
        ):
            self.nav_icons.append(icon)
            self.nav.addItem(QListWidgetItem(title))
            self.pages.addWidget(_page(title, subtitle, page))
        self._use_theme_icons()
        theme().changed.connect(self._use_theme_icons)
        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.nav.setCurrentRow(0)
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
        self._fit_height(TALLEST_UNSCROLLED_PAGE, footer)

    # Frame ----------------------------------------------------------------------

    def _fit_height(self, row: int, footer: QWidget) -> None:
        """Tall enough to show page `row` without scrolling, where the screen has room."""
        self.ensurePolished()
        page = self.pages.widget(row).widget()
        needed = page.sizeHint().height() + footer.sizeHint().height()
        height = max(DIALOG_SIZE.height(), needed)
        if (screen := self.screen()) is not None:
            height = min(height, screen.availableGeometry().height() - SCREEN_MARGIN)
        self.resize(self.width(), max(height, DIALOG_SIZE.height()))

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
        names.addWidget(product)
        names.addWidget(version)
        brand.addWidget(logo)
        brand.addLayout(names, 1)
        layout.addLayout(brand)
        layout.addWidget(self.nav, 1)
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

    def _general_page(self, startup_enabled: bool | None) -> list[QWidget]:
        self.autostart = ToggleSwitch("Start WinLauncher when I sign in to Windows")
        self.autostart.setEnabled(self.set_startup is not None)
        self.autostart.setChecked(bool(startup_enabled))
        self.autostart.clicked.connect(self._toggle_startup)
        startup = Card()
        startup.add_row(
            "Start with Windows",
            "Launch WinLauncher in the tray when you sign in. Applies immediately.",
            self.autostart,
        )
        settings = self.original
        self.ctrl_interval = _sized(
            NumberField(DOUBLE_CTRL_RANGE_MS, settings.double_ctrl_ms, "ms")
        )
        self.ctrl_interval.setAccessibleName("Double-Ctrl interval in milliseconds")
        self.auto_hide = ToggleSwitch("Close the empty launcher when it loses focus")
        self.auto_hide.setChecked(settings.auto_hide)
        self.auto_hide.setToolTip(
            "The search bar inside Explorer closes as soon as it loses focus."
        )
        self.auto_hide_delay = _sized(
            NumberField(AUTO_HIDE_RANGE_SECONDS, settings.auto_hide_seconds, "seconds")
        )
        self.auto_hide_delay.setAccessibleName("Close the empty launcher after seconds")
        self.auto_hide_delay.setEnabled(settings.auto_hide)
        self.auto_hide.toggled.connect(self.auto_hide_delay.setEnabled)
        self.limit = _sized(NumberField(RESULT_LIMIT_RANGE, settings.max_results))
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
        return [
            section_title("Startup"),
            startup,
            section_title("Appearance"),
            looks,
            section_title("Search bar"),
            bar,
        ]

    def _folders_page(self, settings: Settings) -> list[QWidget]:
        self.roots = PathList(settings.roots, "Search folders")
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
        self.scan_interval = _sized(
            NumberField(RESCAN_RANGE_SECONDS, settings.rescan_seconds, "seconds")
        )
        self.scan_interval.setAccessibleName("Full refresh interval in seconds")
        self.content_threads = _sized(
            NumberField(CONTENT_SEARCH_THREADS_RANGE, settings.content_search_threads)
        )
        self.content_threads.setAccessibleName("Content search threads")
        self.content_threads.setToolTip(
            "1–16 parallel file scans for contains:, content: and rg: (Rust only). "
            "More threads can increase disk activity and memory use."
        )
        folders = Card()
        folders.add_block("Folders", "Drag folders here from Explorer, or add them.", self.roots)
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

    def _explorer_page(self, settings: Settings) -> list[QWidget]:
        self.explorer = ToggleSwitch("Search when typing in Explorer's file view")
        self.explorer.setChecked(settings.explorer_typing)
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
        return [card]

    def _exclusions_page(self, settings: Settings) -> list[QWidget]:
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
        return [card]

    def _websites_page(self) -> list[QWidget]:
        card = Card()
        card.add_block("", "", self.websites)
        return [card]

    def _menu_commands_page(self) -> list[QWidget]:
        card = Card()
        card.add_block("", "", self.menu_commands)
        return [card]

    def _ranking_page(self, settings: Settings) -> list[QWidget]:
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
        return [card, note]

    def _advanced_page(self, settings: Settings) -> list[QWidget]:
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
        if folder is not None:
            self.excluded.add_folders([folder])
        if pattern and pattern not in _split_lines(self.patterns.toPlainText()):
            self.patterns.appendPlainText(pattern)

    def read_settings(self) -> Settings:
        """Raise ValueError with a user-facing message for invalid input."""
        settings = self._settings_without_plugins()
        if self.plugin_page is None:
            return settings
        return self.plugin_page.apply_to(settings)

    def _settings_without_plugins(self) -> Settings:
        advanced = parse_advanced_rules(self.rule_json.toPlainText())
        return replace(
            self.original,
            roots=self.roots.paths(),
            excluded_paths=self.excluded.paths(),
            excluded_patterns=_split_lines(self.patterns.toPlainText()),
            marker_files=_split_commas(self.markers.text()),
            folder_priorities=parse_folder_priorities(self.priorities.toPlainText()),
            name_priorities=advanced.names,
            extension_priorities=advanced.extensions,
            attribute_priorities=advanced.attributes,
            index_apps=self.apps.isChecked(),
            index_windows_tools=self.windows_tools.isChecked(),
            windows_tools_english=self.windows_tools_english.isChecked(),
            windows_settings_detail=self.windows_settings_detail.currentData(),
            explorer_typing=self.explorer.isChecked(),
            quick_switch=self.quick_switch.isChecked(),
            double_ctrl_ms=self.ctrl_interval.value(),
            auto_hide=self.auto_hide.isChecked(),
            auto_hide_seconds=self.auto_hide_delay.value(),
            rescan_seconds=self.scan_interval.value(),
            max_results=self.limit.value(),
            content_search_threads=self.content_threads.value(),
            websites=self.websites.websites(),
            menu_commands=self.menu_commands.commands(),
            appearance=self.appearance.currentData(),
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
