"""Settings dialog. Values are validated by `Settings`; this module only reads widgets.

Autostart changes the Startup shortcut immediately (the shortcut is the source of
truth, shared with the tray). Every other change applies on Save. If saving
fails, the dialog stays open with the edits intact.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from PySide6.QtCore import QMimeData, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
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
    QSpinBox,
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
    DOUBLE_CTRL_RANGE_MS,
    RESCAN_RANGE_SECONDS,
    RESULT_LIMIT_RANGE,
    Settings,
    priority_rules,
)

ADVANCED_RULE_FIELDS = ("name_priorities", "extension_priorities", "attribute_priorities")
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


def _spin_box(bounds: tuple[int, int], value: int, suffix: str = "") -> QSpinBox:
    widget = QSpinBox()
    widget.setRange(*bounds)
    widget.setValue(value)
    widget.setSuffix(suffix)
    # The dark stylesheet hides Qt's arrow glyphs; typing, arrow keys and the wheel remain.
    widget.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
    widget.setToolTip(f"{bounds[0]}–{bounds[1]}{suffix}; type a value or use the arrow keys")
    return widget


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
    ):
        super().__init__(parent)
        self.original = settings
        self.result_settings = settings
        self.save = save
        self.set_startup = set_startup
        self.startup_state = startup_state
        self.setWindowTitle("PathScout settings")
        self.resize(660, 620)
        tabs = QTabWidget()
        tabs.addTab(self._indexing_tab(settings, startup_enabled), "Search && indexing")
        tabs.addTab(self._exclusions_tab(settings), "Exclusions")
        tabs.addTab(self._ranking_tab(settings), "Ranking")
        tabs.addTab(self._advanced_tab(settings), "Advanced rules")
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

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
        self.ctrl_interval = _spin_box(DOUBLE_CTRL_RANGE_MS, settings.double_ctrl_ms, " ms")
        self.scan_interval = _spin_box(RESCAN_RANGE_SECONDS, settings.rescan_seconds, " seconds")
        self.limit = _spin_box(RESULT_LIMIT_RANGE, settings.max_results)
        form.addRow("Double-Ctrl interval", self.ctrl_interval)
        form.addRow("Refresh catalog every", self.scan_interval)
        form.addRow("Maximum results", self.limit)
        self.backend = QComboBox()
        self.backend.addItem("Rust", "rust")
        self.backend.addItem("Python", "python")
        self.backend.setCurrentIndex(self.backend.findData(settings.search_backend))
        self.backend.setAccessibleName("Search backend")
        form.addRow("Search backend", self.backend)
        form.addRow(_note("Rust uses Python when unavailable or a search rule requires it."))
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
            double_ctrl_ms=self.ctrl_interval.value(),
            rescan_seconds=self.scan_interval.value(),
            max_results=self.limit.value(),
            search_backend=self.backend.currentData(),
        )

    def accept(self) -> None:
        try:
            settings = self.read_settings()
        except ValueError as exc:
            QMessageBox.warning(self, "Check settings", str(exc))
            return
        try:
            if self.save:
                self.save(settings)
        except OSError as exc:
            QMessageBox.warning(self, "Cannot save settings", str(exc))
            return
        self.result_settings = settings
        super().accept()
