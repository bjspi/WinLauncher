"""The first start: how to open the bar, what it searches, and a few tips.

WinLauncher otherwise starts quietly in the tray, so a new user would not know
that it runs, nor how to call it. Shown once, when no settings file existed.
"""

from __future__ import annotations

from collections.abc import Sequence
from html import escape
from pathlib import Path

from PySide6.QtWidgets import QHBoxLayout, QWidget

from winlauncher.ui.theme import app_icon
from winlauncher.ui.widgets import Card, InfoDialog, ToggleSwitch, key_caps

WELCOME_WIDTH = 560
SHOWN_FOLDERS = 4  # More are summarized as a count.
SHORTCUTS = (("Ctrl", "Ctrl"), ("Ctrl", "Alt", "Space"))
TIPS = (
    "<b>?</b> lists the search filters, such as <b>type:</b>, <b>ext:</b> and <b>date:</b>",
    "<b>Tab</b> on a folder searches inside it",
    "Right-click a result for more, such as <b>Open with…</b> or <b>Exclude…</b>",
)
TIP_LINE_HEIGHT = "150%"


class WelcomeDialog(InfoDialog):
    """Shows how to call WinLauncher; offers Start with Windows and the folder settings."""

    def __init__(
        self,
        folders: Sequence[str],
        *,
        autostart_available: bool,
        parent: QWidget | None = None,
    ):
        super().__init__(
            "Welcome to WinLauncher",
            "Open the search bar from anywhere, then just type.",
            app_icon(),
            parent,
            width=WELCOME_WIDTH,
            hero=True,
        )
        self.add_widget(_centered(key_caps(*SHORTCUTS, large=True)))
        self.add_facts([("Searches", _folders_text(folders)), ("Tips", _tips_text())])
        # On at first: a launcher is wanted after every sign-in; one click turns it off.
        self.autostart = ToggleSwitch("Start WinLauncher when I sign in to Windows")
        self.autostart.setChecked(autostart_available)
        if autostart_available:
            card = Card()
            card.add_row("Start with Windows", "You can change this in Settings.", self.autostart)
            self.add_widget(card)
        self.wants_folders = False  # Choose folders… closes the dialog to open Settings.
        self.add_helper("Choose folders…", self._choose_folders, link=True)
        self.add_button("Start searching", self.accept, primary=True)

    def _choose_folders(self) -> None:
        self.wants_folders = True
        self.accept()


def _centered(widget: QWidget) -> QWidget:
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 4, 0, 4)
    layout.addStretch(1)
    layout.addWidget(widget)
    layout.addStretch(1)
    return row


def _folders_text(folders: Sequence[str]) -> str:
    """The indexed folders by their short names; apps and Windows tools come along."""
    named = [escape(_folder_name(folder)) for folder in folders[:SHOWN_FOLDERS]]
    if len(folders) > SHOWN_FOLDERS:
        named.append(f"{len(folders) - SHOWN_FOLDERS} more")
    places = ", ".join(named) if named else "No folders yet"
    return f"{places}<br>Installed apps and Windows settings"


def _folder_name(folder: str) -> str:
    """A folder in the user's profile by its path there, such as Documents or
    OneDrive\\Desktop; any other by its full path."""
    try:
        return str(Path(folder).relative_to(Path.home())) or folder
    except ValueError:
        return folder


def _tips_text() -> str:
    lines = "<br>".join(f"•&nbsp;&nbsp;{tip}" for tip in TIPS)
    return f'<p style="line-height: {TIP_LINE_HEIGHT}; margin: 0;">{lines}</p>'
