"""A read-only tree of catalog entries affected by a proposed exclusion rule."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from threading import Event

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QLineEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QWidget,
)

from winlauncher.catalog.exclusion_preview import (
    ExclusionMatch,
    ExclusionPreview,
    preview_exclusion,
)
from winlauncher.catalog.model import ItemKind, path_key
from winlauncher.catalog.store import CatalogStore
from winlauncher.ui.messages import INFO, badge
from winlauncher.ui.tasks import BackgroundTasks, TaskCategory
from winlauncher.ui.theme import colors
from winlauncher.ui.widgets import InfoDialog, line_icon

PREVIEW_WIDTH = 820
TREE_HEIGHT = 350
EFFECT_COLUMN_WIDTH = 195


class RulePreviewDialog(InfoDialog):
    """Run one cancellable catalog read; closing never saves the proposed rule."""

    def __init__(
        self,
        pattern: str,
        store: CatalogStore,
        tasks: BackgroundTasks,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            "Exclusion rule preview",
            "See what this rule would exclude from the current search catalog.",
            badge(INFO),
            parent,
            width=PREVIEW_WIDTH,
        )
        self._cancelled = Event()
        cancelled = self._cancelled
        self.finished.connect(lambda _result: cancelled.set())
        self.destroyed.connect(lambda: cancelled.set())
        self._nodes: dict[str, QTreeWidgetItem] = {}

        rule = QLineEdit(pattern)
        rule.setObjectName("code")
        rule.setReadOnly(True)
        rule.setAccessibleName("Rule being previewed")
        self.add_widget(rule)
        self.summary = QLabel("Checking the catalog…")
        self.summary.setObjectName("infoTitle")
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.add_widget(self.summary)
        self.tree = QTreeWidget()
        self.tree.setObjectName("exclusionPreview")
        self.tree.setAccessibleName("Entries this rule would exclude")
        self.tree.setHeaderLabels(["Name / location", "Effect of this rule"])
        self.tree.setUniformRowHeights(True)
        self.tree.setAnimated(False)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.tree.setMinimumHeight(TREE_HEIGHT)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.tree.setColumnWidth(1, EFFECT_COLUMN_WIDTH)
        self.add_widget(self.tree)
        self.note = QLabel(
            "Only entries in the current catalog are checked; previously excluded or "
            "unindexed files are not included. Parent folders provide context. "
            "Nothing is saved until you choose Add rule."
        )
        self.note.setObjectName("infoText")
        self.note.setTextFormat(Qt.TextFormat.PlainText)
        self.note.setWordWrap(True)
        self.add_widget(self.note)
        self.expand = self.add_helper("Expand all", self.tree.expandAll, link=True)
        self.collapse = self.add_helper("Collapse all", self.tree.collapseAll, link=True)
        self.expand.setEnabled(False)
        self.collapse.setEnabled(False)
        self.add_button("Back to rule", self.reject, primary=True)
        work = partial(preview_exclusion, store, pattern, cancelled)
        tasks.submit(TaskCategory.PREFETCH, work, self._loaded, self._failed)

    def _loaded(self, preview: ExclusionPreview | None) -> None:
        if self._cancelled.is_set() or preview is None:
            return
        if not preview.total:
            self.summary.setText("No matching entries in the current catalog")
            return
        entry_label = "entry" if preview.total == 1 else "entries"
        folder_label = "folder" if preview.folders == 1 else "folders"
        self.summary.setText(
            f"{preview.total:,} {entry_label} would be excluded · "
            f"{preview.files:,} files / apps · {preview.folders:,} {folder_label}"
        )
        self.tree.setUpdatesEnabled(False)
        for match in preview.matches:
            self._add_match(match)
        self.tree.expandAll()
        self.tree.setUpdatesEnabled(True)
        self.expand.setEnabled(True)
        self.collapse.setEnabled(True)
        if len(preview.matches) < preview.total:
            self.note.setText(
                f"Showing the first {len(preview.matches):,} of {preview.total:,} affected entries. "
                + self.note.text()
            )

    def _node(self, path: Path) -> QTreeWidgetItem:
        """Share ancestors by Windows path identity, even across differently cased rows."""
        key = path_key(path)
        existing = self._nodes.get(key)
        if existing is not None:
            return existing
        parent = self.tree.invisibleRootItem() if path.parent == path else self._node(path.parent)
        node = QTreeWidgetItem(parent, [path.name or str(path), "Context folder"])
        node.setToolTip(0, str(path))
        node.setIcon(0, line_icon("folder", colors().muted))
        node.setForeground(1, QBrush(QColor(colors().subtle)))
        self._nodes[key] = node
        return node

    def _add_match(self, match: ExclusionMatch) -> None:
        node = self._node(match.path)
        effect = "Inside excluded folder" if match.inherited else "Matches rule"
        node.setText(1, effect)
        node.setForeground(1, QBrush(QColor(colors().accent_strong)))
        node.setToolTip(1, effect)
        folder = match.kind == ItemKind.FOLDER
        node.setIcon(0, line_icon("folder" if folder else "file", colors().accent_strong))
        font = node.font(0)
        font.setBold(not match.inherited)
        node.setFont(0, font)

    def _failed(self, error: str) -> None:
        if self._cancelled.is_set():
            return
        self.summary.setText("The catalog could not be checked")
        self.note.setText(error)
