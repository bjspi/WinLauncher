"""Platform-independent keyboard intents and the double-Ctrl gesture."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

# VK_CONTROL and the left/right specific codes the low-level hook reports.
CONTROL_KEYS = frozenset({0x11, 0xA2, 0xA3})


@dataclass(frozen=True, slots=True)
class ShowRequested:
    """Double Ctrl or Ctrl+Alt+Space."""

    source: str = "shortcut"
    requested_at: float = field(default_factory=time.monotonic)


@dataclass(frozen=True, slots=True)
class ExplorerTyping:
    """Text typed into Explorer's file view, searching below `folder`.

    `window` is the Explorer window's (left, top, right, bottom) in physical
    screen pixels, so the launcher can open next to it.
    """

    text: str
    folder: Path
    window: tuple[int, int, int, int] | None = None


@dataclass(frozen=True, slots=True)
class InputUnavailable:
    message: str


KeyboardIntent = ShowRequested | ExplorerTyping | InputUnavailable


class DoubleCtrlGesture:
    """Two quick, clean Ctrl taps: no other key in between and no long hold."""

    def __init__(self, interval_ms: int):
        self.interval = interval_ms / 1000
        self._down: set[int] = set()
        self._clean = False  # The current Ctrl press started with no other key held.
        self._last_tap: float | None = None
        self._pressed_at = 0.0

    def feed(self, key: int, pressed: bool, now: float) -> bool:
        """Return True when this key event completes the gesture."""
        if key not in CONTROL_KEYS:
            if pressed:
                self._interrupt()
            return False
        if pressed:
            self._press(key, now)
            return False
        return self._release(key, now)

    def _interrupt(self) -> None:
        self._last_tap = None
        self._clean = False

    def _press(self, key: int, now: float) -> None:
        if key in self._down:
            return  # Auto-repeat.
        self._clean = not self._down
        self._down.add(key)
        self._pressed_at = now

    def _release(self, key: int, now: float) -> bool:
        if key not in self._down:
            return False
        self._down.remove(key)
        if self._down or not self._clean or now - self._pressed_at > self.interval:
            self._last_tap = None
            return False
        self._clean = False
        if self._last_tap is not None and now - self._last_tap <= self.interval:
            self._last_tap = None
            return True
        self._last_tap = now
        return False
