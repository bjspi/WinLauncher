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

from PySide6.QtCore import QMimeData, Qt, QTimer, Signal
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from pathscout.catalog.model import path_key
from pathscout.catalog.priority import (
    AttributePriority,
    ExtensionPriority,
    FolderPriority,
    NamePriority,
    SearchPriority,
)
from pathscout.settings import (
    AUTO_HIDE_RANGE_SECONDS,
    CONTENT_SEARCH_THREADS_RANGE,
    DOUBLE_CTRL_RANGE_MS,
    RESCAN_RANGE_SECONDS,
    RESULT_LIMIT_RANGE,
    Settings,
    priority_rules,
)
from pathscout.ui.website_icons import WebsiteIcons
from pathscout.ui.websites import WebsiteList

ADVANCED_RULE_FIELDS = ("name_priorities", "extension_priorities", "attribute_priorities")
SAVED_MESSAGE_MS = 2500  # How long the footer confirms a save.
FOLDER_PRIORITY_FORMAT = "25 | D:\\Projects | high"


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
        self.list.setObjectName("paths")
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
        buttons.addWidget(_note("or drag folders here"))
        layout.addLayout(buttons)

    def add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose folder")
        if folder:
            self.add_folders([Path(folder)])

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
    ):
        super().__init__(parent)
        self.original = settings
        self.result_settings = settings
        self.persist = save
        self.set_startup = set_startup
        self.startup_state = startup_state
        self.setWindowTitle("PathScout settings")
        self.resize(660, 620)
        tabs = QTabWidget()
        tabs.addTab(self._indexing_tab(settings, startup_enabled), "Search && indexing")
        tabs.addTab(self._exclusions_tab(settings), "Exclusions")
        self.websites = WebsiteList(settings.websites, website_icons)
        tabs.addTab(self.websites, "Websites")
        tabs.addTab(self._ranking_tab(settings), "Ranking")
        tabs.addTab(self._advanced_tab(settings), "Advanced rules")
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        # Save keeps the dialog open; Cancel (Close after a save) leaves it.
        self.buttons.accepted.connect(self.save)
        self.buttons.rejected.connect(self.reject)
        self.saved_message = _note("")
        self.saved_message_timer = QTimer(self)
        self.saved_message_timer.setSingleShot(True)
        self.saved_message_timer.setInterval(SAVED_MESSAGE_MS)
        self.saved_message_timer.timeout.connect(self.saved_message.clear)
        footer = QHBoxLayout()
        footer.addWidget(self.saved_message, 1)
        footer.addWidget(self.buttons)
        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addLayout(footer)

    # Tabs ---------------------------------------------------------------------

    def _indexing_tab(self, settings: Settings, startup_enabled: bool | None) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.autostart = QCheckBox(
            "Start PathScout when I sign in to Windows (applies immediately)"
        )
        self.autostart.setEnabled(self.set_startup is not None)
        self.autostart.setChecked(bool(startup_enabled))
        self.autostart.clicked.connect(self._toggle_startup)
        form.addRow(self.autostart)
        self.roots = PathList(settings.roots, "Search folders")
        form.addRow("Search folders", self.roots)
        self.apps = QCheckBox("Include Start menu and registered applications")
        self.apps.setChecked(settings.index_apps)
        form.addRow(self.apps)
        self.explorer = QCheckBox("Search when typing in Explorer's file view")
        self.explorer.setChecked(settings.explorer_typing)
        form.addRow(self.explorer)
        self.quick_switch = QCheckBox(
            "Quick Switch in Open and Save dialogs: type to search, "
            "Ctrl+G jumps to the open Explorer folder"
        )
        self.quick_switch.setChecked(settings.quick_switch)
        self.quick_switch.setToolTip(
            "Choosing a folder opens it in the dialog; choosing a file enters its name "
            "without confirming."
        )
        form.addRow(self.quick_switch)
        self.ctrl_interval = NumberField(DOUBLE_CTRL_RANGE_MS, settings.double_ctrl_ms, "ms")
        self.scan_interval = NumberField(RESCAN_RANGE_SECONDS, settings.rescan_seconds, "seconds")
        self.limit = NumberField(RESULT_LIMIT_RANGE, settings.max_results)
        form.addRow("Double-Ctrl interval (ms)", self.ctrl_interval)
        self.auto_hide = QCheckBox("Close the empty launcher when it loses focus")
        self.auto_hide.setChecked(settings.auto_hide)
        self.auto_hide.setToolTip(
            "The search bar inside Explorer closes as soon as it loses focus."
        )
        self.auto_hide_delay = NumberField(
            AUTO_HIDE_RANGE_SECONDS, settings.auto_hide_seconds, "seconds"
        )
        self.auto_hide_delay.setAccessibleName("Close the empty launcher after seconds")
        self.auto_hide_delay.setEnabled(settings.auto_hide)
        self.auto_hide.toggled.connect(self.auto_hide_delay.setEnabled)
        form.addRow(self.auto_hide)
        form.addRow("Close after (s)", self.auto_hide_delay)
        form.addRow("Refresh catalog every (s)", self.scan_interval)
        form.addRow("Maximum results", self.limit)
        self.content_threads = NumberField(
            CONTENT_SEARCH_THREADS_RANGE, settings.content_search_threads
        )
        self.content_threads.setAccessibleName("Content search threads")
        self.content_threads.setToolTip(
            "1–16 parallel file scans for contains:, content: and rg: (Rust only). "
            "More threads can increase disk activity and memory use."
        )
        form.addRow("Content search threads", self.content_threads)
        form.addRow(
            _note("Other changes apply on Save. File names and paths stay on this computer.")
        )
        return page

    def _exclusions_tab(self, settings: Settings) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.excluded = PathList(settings.excluded_paths, "Excluded directories")
        self.patterns = QPlainTextEdit("\n".join(settings.excluded_patterns))
        self.patterns.setAccessibleName("Excluded file and folder patterns, one per line")
        self.patterns.setMaximumHeight(130)
        self.patterns.setToolTip(
            "Names such as node_modules or *.lnk, or folder paths such as\n"
            "Projects\\Android\\*\\build (* = one folder) or Android/**/build (** = any depth)."
        )
        self.markers = QLineEdit(", ".join(settings.marker_files))
        self.markers.setAccessibleName("Marker file names, comma-separated")
        form.addRow("Excluded directories", self.excluded)
        form.addRow("File / folder glob patterns\n(one per line)", self.patterns)
        form.addRow("Marker file names\n(comma-separated)", self.markers)
        form.addRow(
            _note(
                "A marker excludes its entire directory and all descendants. "
                "Links and junctions below a chosen root are not followed."
            )
        )
        return page

    def _ranking_tab(self, settings: Settings) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.priorities = QPlainTextEdit(format_folder_priorities(settings.folder_priorities))
        self.priorities.setPlaceholderText(f"{FOLDER_PRIORITY_FORMAT}\n-20 | C:\\Archive | low")
        self.priorities.setAccessibleName("Folder priorities, one per line")
        form.addRow(
            _note("Folder boosts from -100 to 100. The most specific matching folder wins.")
        )
        form.addRow("One priority per line", self.priorities)
        form.addRow(
            _note(
                "Favorites and successful launches also improve ranking. Exact names and "
                "name prefixes receive the strongest text scores."
            )
        )
        return page

    def _advanced_tab(self, settings: Settings) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(
            _note(
                "Filename regex, extension and Windows attribute rules. Each rule has an "
                "independent search_priority (ignored, low, normal, high) and a weight "
                "(-100 to 100)."
            )
        )
        self.rule_json = QPlainTextEdit(format_advanced_rules(settings))
        self.rule_json.setAccessibleName("Advanced priority rules as JSON")
        layout.addWidget(self.rule_json)
        layout.addWidget(
            _note(
                'Example extension rule: {"extension": "bak", "weight": -10, '
                '"search_priority": "low"}'
            )
        )
        return page

    # Behavior -----------------------------------------------------------------

    def _toggle_startup(self, enabled: bool) -> None:
        try:
            self.set_startup(enabled)
        except OSError as exc:
            QMessageBox.warning(self, "Cannot change autostart", str(exc))
        # Show what the Startup folder actually contains, after success or failure.
        actual = self.startup_state() if self.startup_state else not enabled
        self.autostart.setChecked(actual)

    def read_settings(self) -> Settings:
        """Raise ValueError with a user-facing message for invalid input."""
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
            explorer_typing=self.explorer.isChecked(),
            quick_switch=self.quick_switch.isChecked(),
            double_ctrl_ms=self.ctrl_interval.value(),
            auto_hide=self.auto_hide.isChecked(),
            auto_hide_seconds=self.auto_hide_delay.value(),
            rescan_seconds=self.scan_interval.value(),
            max_results=self.limit.value(),
            content_search_threads=self.content_threads.value(),
            websites=self.websites.websites(),
        )

    def save(self) -> None:
        """Validate and store the edits; the dialog stays open for further changes."""
        self.saved_message.clear()
        try:
            settings = self.read_settings()
        except ValueError as exc:
            QMessageBox.warning(self, "Check settings", str(exc))
            return
        try:
            if self.persist:
                self.persist(settings)
        except OSError as exc:
            QMessageBox.warning(self, "Cannot save settings", str(exc))
            return
        self.result_settings = settings
        self.original = settings
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Close")
        self.saved_message.setText("Settings saved.")
        self.saved_message_timer.start()
