"""The first start: how to open the bar, what it searches, and a few tips.

WinLauncher otherwise starts quietly in the tray, so a new user would not know
that it runs, nor how to call it. Shown once, when no settings file existed.
"""

from __future__ import annotations

from collections.abc import Sequence
from html import escape

from PySide6.QtWidgets import QWidget

from winlauncher.ui.theme import app_icon
from winlauncher.ui.widgets import Card, InfoDialog, ToggleSwitch

WELCOME_WIDTH = 560
SHOWN_FOLDERS = 4  # More are summarized as a count.
TIPS = (
    "<b>?</b> lists the search filters, such as <b>type:</b>, <b>ext:</b> and <b>date:</b>",
    "<b>Tab</b> on a folder searches inside it",
    "Right-click a result for more, such as <b>Open with…</b> or <b>Exclude…</b>",
)


class WelcomeDialog(InfoDialog):
    """Tells how to call WinLauncher; offers Start with Windows and the folder settings."""

    def __init__(
        self,
        folders: Sequence[str],
        *,
        autostart: bool | None,
        parent: QWidget | None = None,
    ):
        super().__init__(
            "Welcome to WinLauncher",
            "Press <b>Ctrl</b> twice, or <b>Ctrl+Alt+Space</b>, anywhere to search. "
            "WinLauncher waits in the tray until then.",
            app_icon(),
            parent,
            width=WELCOME_WIDTH,
        )
        self.add_facts([("Searches", _folders_text(folders)), ("Tips", "<br>".join(TIPS))])
        # None: autostart is not available here, such as outside Windows.
        self.autostart = ToggleSwitch("Start WinLauncher when I sign in to Windows")
        self.autostart.setChecked(bool(autostart))
        if autostart is not None:
            card = Card()
            card.add_row("Start with Windows", "You can change this in Settings.", self.autostart)
            self.add_widget(card)
        self.wants_folders = False  # Choose folders… closes the dialog to open Settings.
        self.add_helper("Choose folders…", self._choose_folders, link=True)
        self.add_button("Start searching", self.accept, primary=True)

    def _choose_folders(self) -> None:
        self.wants_folders = True
        self.accept()


def _folders_text(folders: Sequence[str]) -> str:
    """The indexed folders, a few by name; apps and Windows tools always come along."""
    named = [escape(folder) for folder in folders[:SHOWN_FOLDERS]]
    if len(folders) > SHOWN_FOLDERS:
        named.append(f"and {len(folders) - SHOWN_FOLDERS} more folders")
    places = "<br>".join(named) if named else "No folders yet"
    return f"{places}<br>Installed apps and Windows settings"
