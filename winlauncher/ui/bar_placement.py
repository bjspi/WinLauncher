"""Where the launcher bar opens and how wide it is, remembered across starts.

The bar opens centered until the user drags it somewhere else. From then on it
opens at the same relative spot of whichever screen the cursor is on, until
"Center search bar" is chosen again. Widths are remembered per layout, since
the expanded view is deliberately wider than the compact one.

Like the unfolded plugin cards, this is how the bar looks, not a setting: it is
written at once, apart from the Settings dialog's Save and Cancel. A file that
cannot be read or written only loses what was remembered.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QRect, QSize, Signal

COMPACT_WIDTH = 640
EXPANDED_WIDTH = 900
MIN_WIDTH = 480  # Room for the query, the scope chip and the footer's filter buttons.
# The centered bar's top edge, as a fraction of the screen height.
CENTERED_TOP = 0.2
logger = logging.getLogger(__name__)


def _fraction(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


@dataclass(frozen=True, slots=True)
class RelativePosition:
    """The bar's top-left corner in fractions of a screen, each from 0 to 1.

    `x` is relative to the horizontal room the bar leaves free, so 0.5 centers it
    at any width. `y` is relative to the screen height: the bar grows downwards
    with its results, so its height says nothing about where the user wants it.
    """

    x: float
    y: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "x", _fraction(self.x))
        object.__setattr__(self, "y", _fraction(self.y))

    @classmethod
    def of(cls, bar: QRect, area: QRect) -> RelativePosition:
        """Where `bar` lies within the screen's available `area`."""
        free_width = area.width() - bar.width()
        x = (bar.x() - area.x()) / free_width if free_width > 0 else 0.5
        return cls(x, (bar.y() - area.y()) / area.height())

    def top_left(self, size: QSize, area: QRect) -> QPoint:
        """The corner for a bar of `size` in `area`, keeping its top row on screen."""
        x = area.x() + round(self.x * max(0, area.width() - size.width()))
        y = area.y() + round(self.y * area.height())
        lowest = area.y() + max(0, area.height() - size.height())
        return QPoint(x, min(y, lowest))


CENTERED = RelativePosition(0.5, CENTERED_TOP)


class BarPlacement(QObject):
    """The launcher bar's remembered widths and position; `path=None` keeps them in memory."""

    centered_changed = Signal(bool)

    def __init__(self, path: Path | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self.path = path
        self.compact_width = COMPACT_WIDTH
        self.expanded_width = EXPANDED_WIDTH
        self.position: RelativePosition | None = None  # None: centered.
        self._read()

    @property
    def centered(self) -> bool:
        return self.position is None

    def width(self, expanded: bool) -> int:
        """The surface width of the compact or expanded bar, without its shadow."""
        return self.expanded_width if expanded else self.compact_width

    def top_left(self, size: QSize, area: QRect) -> QPoint:
        return (self.position or CENTERED).top_left(size, area)

    def remember_width(self, expanded: bool, width: int) -> None:
        width = max(MIN_WIDTH, width)
        if expanded:
            self.expanded_width = width
        else:
            self.compact_width = width
        self._write()

    def remember_position(self, bar: QRect, area: QRect) -> None:
        """The user moved the bar: open it there from now on, no longer centered."""
        was_centered = self.centered
        self.position = RelativePosition.of(bar, area)
        self._write()
        if was_centered:
            self.centered_changed.emit(False)

    def set_centered(self, centered: bool) -> None:
        """Center the bar again, or keep it where the centered bar is until it is moved."""
        if centered == self.centered:
            return
        self.position = None if centered else CENTERED
        self._write()
        self.centered_changed.emit(centered)

    def _write(self) -> None:
        if self.path is None:
            return
        position = None if self.position is None else {"x": self.position.x, "y": self.position.y}
        stored = {
            "compact_width": self.compact_width,
            "expanded_width": self.expanded_width,
            "position": position,
        }
        try:
            self.path.write_text(json.dumps(stored, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("Search bar placement could not be saved: %s", exc)

    def _read(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Search bar placement could not be read: %s", exc)
            return
        if not isinstance(stored, dict):
            return
        self.compact_width = _width(stored.get("compact_width"), COMPACT_WIDTH)
        self.expanded_width = _width(stored.get("expanded_width"), EXPANDED_WIDTH)
        self.position = _position(stored.get("position"))


def _width(stored: object, default: int) -> int:
    if isinstance(stored, int) and not isinstance(stored, bool):
        return max(MIN_WIDTH, stored)
    return default


def _position(stored: object) -> RelativePosition | None:
    if not isinstance(stored, dict):
        return None
    x, y = stored.get("x"), stored.get("y")
    if not all(isinstance(value, int | float) for value in (x, y)):
        return None
    return RelativePosition(x, y)
