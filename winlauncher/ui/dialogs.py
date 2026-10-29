"""Modal ownership and focus without stranding the always-on-top search bar."""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QWidget


def bring_to_front(widget: QWidget) -> None:
    """Activate the widget's own window, including when Windows needs a focus handover."""
    window = widget.window()
    if window.isMinimized():
        window.showNormal()
    else:
        window.show()
    window.raise_()
    window.activateWindow()
    # Native file pickers own their HWND; the hidden Qt wrapper's winId would
    # target a different window and interfere with the picker's own activation.
    native_wrapper = window.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    if os.name == "nt" and QApplication.platformName() == "windows" and not native_wrapper:
        from winlauncher.platform.windows import activate_window

        activate_window(int(window.winId()))


def blocking_modal(window: QWidget) -> QWidget | None:
    """The active dialog if it blocks this window, including through a modal ancestor.

    A WindowModal dialog owned by independent Settings must leave the bar usable,
    including Quick Switch in its folder picker.
    """
    modal = QApplication.activeModalWidget()
    ancestor = modal
    while ancestor is not None:
        if ancestor.windowModality() == Qt.WindowModality.ApplicationModal:
            return modal
        owner = ancestor.parentWidget()
        if owner is None:
            break
        ancestor = owner.window()
        if ancestor is window:
            return modal
    return None


def _modal_owner(dialog: QDialog) -> QWidget | None:
    """Use the originating window or its active child dialog, never the hidden bar."""
    parent = dialog.parentWidget()
    owner = parent.window() if parent is not None else None
    modal = QApplication.activeModalWidget()
    # The launcher is the application's Tool window. Owning a dialog would tie
    # its visibility and stacking to that always-on-top surface.
    from_bar = owner is not None and owner.windowType() == Qt.WindowType.Tool
    if modal is not None and (
        owner is None or from_bar or owner is modal or owner.isAncestorOf(modal)
    ):
        return modal.window()
    if not from_bar:
        return owner
    active = QApplication.activeWindow()
    if isinstance(active, QDialog) and active.isVisible():
        return active.window()
    return None


def run_modal(dialog: QDialog) -> int:
    """Run a foreground dialog; independent owners block only their own window.

    Opening a dialog hides the bar so the new prompt cannot be covered by it.
    Bar-origin and unowned dialogs are parentless and application-modal; the bar
    stays hidden until the decision. Nested dialogs and Settings children are
    window-modal, so the bar can be summoned while they remain open.
    """
    owner = _modal_owner(dialog)
    for window in QApplication.topLevelWidgets():
        if window.windowType() == Qt.WindowType.Tool:
            window.hide()
    if dialog.parentWidget() is not owner:
        dialog.setParent(owner, dialog.windowFlags())
    modality = (
        Qt.WindowModality.ApplicationModal if owner is None else Qt.WindowModality.WindowModal
    )
    dialog.setWindowModality(modality)
    bring_to_front(dialog)
    try:
        return dialog.exec()
    finally:
        dialog.hide()
