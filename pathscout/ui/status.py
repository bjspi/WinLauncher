"""The launcher's activity line and tray tooltip, with problems that persist."""

from __future__ import annotations

from collections.abc import Callable, Hashable
from enum import StrEnum

from PySide6.QtWidgets import QSystemTrayIcon


class ProblemTopic(StrEnum):
    KEYBOARD = "keyboard"
    FOLDER_WATCH = "folder watch"
    AUTOSTART = "autostart"
    SEARCH_ENGINE = "search engine"


class StatusBoard:
    """Transient activity is replaced freely; problems stay until they are cleared.

    Each problem belongs to a topic and a subject (for example a watched root),
    so one recovered root clears only its own message.
    """

    def __init__(self, show_activity: Callable[[str], None], tray: QSystemTrayIcon):
        self.show_activity, self.tray = show_activity, tray
        self.activity = ""
        self.catalog_count = 0
        self.problems: dict[ProblemTopic, dict[Hashable, str]] = {}

    def set_activity(self, text: str) -> None:
        self.activity = text
        self._render()

    def set_catalog_count(self, count: int) -> None:
        self.catalog_count = count
        self._render()

    def show_problem(self, topic: ProblemTopic, message: str, subject: Hashable = None) -> None:
        self.problems.setdefault(topic, {})[subject] = message
        self._render()

    def clear_problem(self, topic: ProblemTopic, subject: Hashable = None) -> None:
        self.problems.get(topic, {}).pop(subject, None)
        self._render()

    def clear_topic(self, topic: ProblemTopic) -> None:
        self.problems.pop(topic, None)
        self._render()

    def problem_text(self) -> str:
        return "\n".join(
            message for messages in self.problems.values() for message in messages.values()
        )

    def _render(self) -> None:
        problems = self.problem_text()
        self.show_activity("\n".join(line for line in (self.activity, problems) if line))
        # Windows truncates tray tooltips, so problems come before the count.
        count = f"PathScout — {self.catalog_count:,} indexed"
        self.tray.setToolTip(f"{problems}\n{count}" if problems else count)
