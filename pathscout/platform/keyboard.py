"""Global keyboard adapter: double Ctrl, Ctrl+Alt+Space and Explorer type-to-search.

The hook callback runs on this thread's message loop and must return quickly;
it reads cached Explorer context and never waits for COM or disk I/O.
"""

from __future__ import annotations

import ctypes
import logging
import queue
import threading
import time
from ctypes import wintypes

from pathscout.platform.explorer import ExplorerTracker
from pathscout.platform.gesture import (
    DoubleCtrlGesture,
    ExplorerTyping,
    InputUnavailable,
    KeyboardIntent,
    ShowRequested,
)
from pathscout.platform.windows import (
    HOOKPROC,
    LLKHF_INJECTED,
    LLKHF_LOWER_IL_INJECTED,
    MOD_ALT,
    MOD_CONTROL,
    MOD_NOREPEAT,
    PM_NOREMOVE,
    VK_SPACE,
    WH_KEYBOARD_LL,
    WM_HOTKEY,
    WM_KEYDOWN,
    WM_QUIT,
    WM_SYSKEYDOWN,
    KeyboardData,
    explorer_file_view,
    kernel32,
    typed_key,
    user32,
    window_rect,
)

logger = logging.getLogger(__name__)
HOTKEY_ID = 1


class KeyboardListener(threading.Thread):
    def __init__(
        self,
        events: queue.SimpleQueue[KeyboardIntent],
        *,
        interval_ms: int,
        explorer_typing: bool = True,
    ):
        super().__init__(name="global-keyboard", daemon=True)
        self.events = events
        self.gesture = DoubleCtrlGesture(interval_ms)
        self.explorer_typing = explorer_typing
        self.tracker = ExplorerTracker(on_failure=self._report)
        self.ready = threading.Event()
        self._thread_id = 0
        self._hook = None
        self._hotkey_registered = False
        self._swallowed_keys: set[int] = set()
        self._dead_key_pending = False
        self._callback = HOOKPROC(self._on_key)  # Keep a reference while hooked.
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

    def run(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        self._create_message_queue()
        try:
            self._install()
            self.ready.set()
            self._pump_messages()
        except Exception as exc:
            logger.exception("Keyboard integration failed")
            self._report(f"Keyboard integration failed: {exc}")
        finally:
            self.ready.set()
            self._uninstall()

    def _create_message_queue(self) -> None:
        """PostThreadMessage needs a queue before `stop` can post WM_QUIT."""
        message = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(message), None, 0, 0, PM_NOREMOVE)

    def _install(self) -> None:
        self._hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._callback, kernel32.GetModuleHandleW(None), 0
        )
        if not self._hook:
            error = ctypes.WinError(ctypes.get_last_error())
            self._report(f"Keyboard hook unavailable: {error}")
        modifiers = MOD_CONTROL | MOD_ALT | MOD_NOREPEAT
        self._hotkey_registered = bool(user32.RegisterHotKey(None, HOTKEY_ID, modifiers, VK_SPACE))
        if not self._hotkey_registered:
            self._report("Ctrl+Alt+Space is already registered by another application.")
        if self.explorer_typing:
            self.tracker.start()
        logger.info(
            "Keyboard ready: hook=%s hotkey=%s double_ctrl_ms=%.0f",
            bool(self._hook),
            self._hotkey_registered,
            self.gesture.interval * 1000,
        )

    def _uninstall(self) -> None:
        self.tracker.stop()
        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
        if self._hotkey_registered:
            user32.UnregisterHotKey(None, HOTKEY_ID)
            self._hotkey_registered = False

    def _pump_messages(self) -> None:
        message = wintypes.MSG()
        while not self._stop_event.is_set():
            if user32.GetMessageW(ctypes.byref(message), None, 0, 0) <= 0:
                return  # WM_QUIT or error.
            if message.message == WM_HOTKEY:
                self.events.put(ShowRequested(source="ctrl_alt_space"))
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))

    def _report(self, message: str) -> None:
        logger.warning("%s", message)
        self.events.put(InputUnavailable(message))

    def _on_key(self, code: int, message: int, pointer: int) -> int:
        """Low-level hook callback; any failure passes input through to Windows."""
        swallow = False
        try:
            if code >= 0:
                data = ctypes.cast(pointer, ctypes.POINTER(KeyboardData)).contents
                swallow = self._should_swallow(data, message in (WM_KEYDOWN, WM_SYSKEYDOWN))
        except Exception:
            logger.exception("Keyboard callback failed")
        if swallow:
            return 1  # Nonzero stops Windows from delivering the key.
        return user32.CallNextHookEx(self._hook, code, message, pointer)

    def _should_swallow(self, data: KeyboardData, pressed: bool) -> bool:
        if data.flags & (LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED):
            return False  # Ignore synthetic input, including our own.
        if self.gesture.feed(data.vkCode, pressed, time.monotonic()):
            self.events.put(ShowRequested(source="double_ctrl"))
        if not pressed:
            return self._release_swallowed_key(data.vkCode)
        return self._capture_explorer_typing(data)

    def _release_swallowed_key(self, key: int) -> bool:
        """A key whose press went to PathScout must not release into Explorer."""
        if key not in self._swallowed_keys:
            return False
        self._swallowed_keys.remove(key)
        return True

    def _capture_explorer_typing(self, data: KeyboardData) -> bool:
        context = self.tracker.context if self.explorer_typing else None
        if context is None or explorer_file_view() != (context.hwnd, context.tab):
            return False
        if self._dead_key_pending:
            # This key completes an accent in Explorer; PathScout stays out of it.
            self._dead_key_pending = False
            return False
        key = typed_key(data)
        if key.dead_key:
            self._dead_key_pending = True
            return False
        if not key.text:
            return False
        self.events.put(
            ExplorerTyping(
                key.text, context.folder, window_rect(context.hwnd), (context.hwnd, context.tab)
            )
        )
        self._swallowed_keys.add(data.vkCode)
        return True
