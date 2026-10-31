"""Pointer-safe Win32 bindings shared by the keyboard and Explorer adapters."""

from __future__ import annotations

import ctypes
import logging
import os
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
LRESULT = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
WINEVENTPROC = ctypes.WINFUNCTYPE(
    None,
    wintypes.HANDLE,  # Hook
    wintypes.DWORD,  # Event
    wintypes.HWND,
    wintypes.LONG,  # Object id
    wintypes.LONG,  # Child id
    wintypes.DWORD,  # Event thread
    wintypes.DWORD,  # Event time
)
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

# Hooks and messages
WH_KEYBOARD_LL = 13
WM_QUIT = 0x0012
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_HOTKEY = 0x0312
WM_ENTERSIZEMOVE = 0x0231  # The user starts moving or resizing a window.
WM_EXITSIZEMOVE = 0x0232  # ... and has finished.
LLKHF_INJECTED = 0x10
LLKHF_LOWER_IL_INJECTED = 0x02
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
PM_NOREMOVE = 0x0000
EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000  # Deliver through the installing thread's message loop.
WINEVENT_SKIPOWNPROCESS = 0x0002

# Virtual keys
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_CAPITAL = 0x14
VK_SPACE = 0x20
VK_G = 0x47
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
KEY_DOWN_BIT = 0x8000
KEY_TOGGLED_BIT = 0x0001
KEY_STATE_DOWN = 0x80
TOUNICODE_KEEP_KEYBOARD_STATE = 0x4  # Windows 10 1607+: do not consume dead keys.

GUI_INMOVESIZE = 0x02
GUI_INMENUMODE = 0x04
GUI_SYSTEMMENUMODE = 0x08
GUI_POPUPMENUMODE = 0x10
GUI_INTERACTION_FLAGS = GUI_INMOVESIZE | GUI_INMENUMODE | GUI_SYSTEMMENUMODE | GUI_POPUPMENUMODE
EXPLORER_WINDOW_CLASSES = ("CabinetWClass", "ExploreWClass")
FILE_VIEW_FOCUS_CLASSES = ("DirectUIHWND", "SysListView32", "SHELLDLL_DefView")
FILE_VIEW_CLASS = "SHELLDLL_DefView"
TAB_CLASS = "ShellTabWindowClass"
DIALOG_CLASS = "#32770"  # Every dialog box, including the common Open and Save dialogs.
MAX_FOCUS_ANCESTORS = 20


class KeyboardData(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class GuiThreadInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


def _bind(library, name, arguments, result):
    function = getattr(library, name)
    function.argtypes, function.restype = arguments, result


_HWND, _UINT, _INT = wintypes.HWND, wintypes.UINT, ctypes.c_int
_MSG_POINTER = ctypes.POINTER(wintypes.MSG)
_bind(
    user32,
    "SetWindowsHookExW",
    [_INT, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD],
    wintypes.HHOOK,
)
_bind(user32, "CallNextHookEx", [wintypes.HHOOK, _INT, wintypes.WPARAM, wintypes.LPARAM], LRESULT)
_bind(user32, "UnhookWindowsHookEx", [wintypes.HHOOK], wintypes.BOOL)
_bind(user32, "GetForegroundWindow", [], _HWND)
_bind(user32, "SetForegroundWindow", [_HWND], wintypes.BOOL)
_bind(user32, "AttachThreadInput", [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL], wintypes.BOOL)
_bind(user32, "GetParent", [_HWND], _HWND)
_bind(user32, "GetClassNameW", [_HWND, wintypes.LPWSTR, _INT], _INT)
_bind(user32, "GetGUIThreadInfo", [wintypes.DWORD, ctypes.POINTER(GuiThreadInfo)], wintypes.BOOL)
_bind(user32, "FindWindowExW", [_HWND, _HWND, wintypes.LPCWSTR, wintypes.LPCWSTR], _HWND)
_bind(user32, "IsWindowVisible", [_HWND], wintypes.BOOL)
_bind(user32, "IsWindow", [_HWND], wintypes.BOOL)
_bind(user32, "EnumChildWindows", [_HWND, WNDENUMPROC, wintypes.LPARAM], wintypes.BOOL)
_bind(user32, "GetDlgItem", [_HWND, _INT], _HWND)
_bind(user32, "IsChild", [_HWND, _HWND], wintypes.BOOL)
_bind(
    user32,
    "SendMessageTimeoutW",
    [
        _HWND,
        _UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
        _UINT,  # SMTO_* flags
        _UINT,  # Timeout in milliseconds
        ctypes.POINTER(ULONG_PTR),
    ],
    LRESULT,
)
_bind(user32, "PostMessageW", [_HWND, _UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL)
_bind(user32, "GetWindowRect", [_HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL)
_bind(user32, "GetAsyncKeyState", [_INT], ctypes.c_short)
_bind(user32, "GetKeyState", [_INT], ctypes.c_short)
_bind(user32, "GetWindowThreadProcessId", [_HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD)
_bind(user32, "GetKeyboardLayout", [wintypes.DWORD], wintypes.HKL)
_bind(
    user32,
    "ToUnicodeEx",
    [_UINT, _UINT, ctypes.POINTER(ctypes.c_ubyte), wintypes.LPWSTR, _INT, _UINT, wintypes.HKL],
    _INT,
)
_bind(user32, "GetMessageW", [_MSG_POINTER, _HWND, _UINT, _UINT], wintypes.BOOL)
_bind(user32, "PeekMessageW", [_MSG_POINTER, _HWND, _UINT, _UINT, _UINT], wintypes.BOOL)
_bind(user32, "TranslateMessage", [_MSG_POINTER], wintypes.BOOL)
_bind(user32, "DispatchMessageW", [_MSG_POINTER], LRESULT)
_bind(
    user32,
    "PostThreadMessageW",
    [wintypes.DWORD, _UINT, wintypes.WPARAM, wintypes.LPARAM],
    wintypes.BOOL,
)
_bind(user32, "RegisterHotKey", [_HWND, _INT, _UINT, _UINT], wintypes.BOOL)
_bind(user32, "UnregisterHotKey", [_HWND, _INT], wintypes.BOOL)
_bind(
    user32,
    "SetWinEventHook",
    # Event range, module, callback, process and thread filters, flags.
    [
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HMODULE,
        WINEVENTPROC,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
    ],
    wintypes.HANDLE,
)
_bind(user32, "UnhookWinEvent", [wintypes.HANDLE], wintypes.BOOL)
_dwmapi = ctypes.WinDLL("dwmapi")
_bind(
    _dwmapi,
    "DwmSetWindowAttribute",
    [_HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD],
    ctypes.c_long,
)
_bind(kernel32, "GetModuleHandleW", [wintypes.LPCWSTR], wintypes.HMODULE)
_bind(kernel32, "GetCurrentThreadId", [], wintypes.DWORD)
_bind(kernel32, "OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE)
_bind(
    kernel32,
    "QueryFullProcessImageNameW",
    [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)],
    wintypes.BOOL,
)
_bind(kernel32, "CloseHandle", [wintypes.HANDLE], wintypes.BOOL)


DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUNDSMALL = 3
logger = logging.getLogger(__name__)


def foreground_details() -> str:
    """Identify focus owners without recording window titles or typed text."""
    hwnd = user32.GetForegroundWindow()
    process_id = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    name = "unknown"
    handle = kernel32.OpenProcess(0x1000, False, process_id.value)  # QUERY_LIMITED_INFORMATION
    if handle:
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(buffer))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                name = os.path.basename(buffer.value)
        finally:
            kernel32.CloseHandle(handle)
    return f"hwnd={hwnd or 0:#x} pid={process_id.value} process={name} class={class_name(hwnd)}"


def activate_window(hwnd: int) -> bool:
    """Retry a denied foreground request using the current foreground input queue."""
    foreground = user32.GetForegroundWindow()
    user32.SetForegroundWindow(hwnd)
    if user32.GetForegroundWindow() == hwnd:
        return True
    # Do not chase a different app that gained focus during our request.
    if foreground and user32.GetForegroundWindow() == foreground:
        current = kernel32.GetCurrentThreadId()
        other = user32.GetWindowThreadProcessId(foreground, None)
        if other and other != current and user32.AttachThreadInput(current, other, True):
            try:
                user32.SetForegroundWindow(hwnd)
            finally:
                user32.AttachThreadInput(current, other, False)
    active = user32.GetForegroundWindow() == hwnd
    if not active:
        logger.warning(
            "Foreground request denied: target=%#x foreground=%s", hwnd, foreground_details()
        )
    return active


def foreground_is_own_process() -> bool:
    """Whether a window of this process, such as the launcher or a dialog, is in front."""
    process_id = wintypes.DWORD()
    user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), ctypes.byref(process_id))
    return process_id.value == os.getpid()


class ForegroundWatcher:
    """Calls `on_other_process` when a window of another process comes to the front.

    Qt can miss activation changes of a window that `activate_window` forced to
    the foreground; Windows reports every change. Install it on a thread with a
    message loop, such as Qt's main thread, which then receives the callbacks.
    """

    def __init__(self, on_other_process: Callable[[], None]):
        # Keep the ctypes callback alive as long as the hook exists.
        self._callback = WINEVENTPROC(lambda *_event: on_other_process())
        self._hook = user32.SetWinEventHook(
            EVENT_SYSTEM_FOREGROUND,
            EVENT_SYSTEM_FOREGROUND,
            None,
            self._callback,
            0,
            0,
            WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS,
        )
        if not self._hook:
            logger.warning(
                "Foreground changes cannot be observed: error=%d", ctypes.get_last_error()
            )

    def close(self) -> None:
        if self._hook:
            user32.UnhookWinEvent(self._hook)
            self._hook = None


def round_corners(hwnd: int) -> None:
    """Ask Windows 11 for small rounded corners with the native shadow; older systems ignore it."""
    preference = ctypes.c_int(DWMWCP_ROUNDSMALL)
    _dwmapi.DwmSetWindowAttribute(
        hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(preference), ctypes.sizeof(preference)
    )


def message_id(message: int) -> int:
    """The id of a window message that Qt's `nativeEvent` passes as the MSG's address."""
    return wintypes.MSG.from_address(message).message


def class_name(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value


def window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def is_key_down(key: int) -> bool:
    return bool(user32.GetAsyncKeyState(key) & KEY_DOWN_BIT)


def active_tab(hwnd: int) -> int:
    """The visible tab of a Windows 11 Explorer window, or the window itself."""
    child = 0
    while child := user32.FindWindowExW(hwnd, child, TAB_CLASS, None):
        if user32.IsWindowVisible(child):
            return child
    return hwnd


def only_ctrl_held() -> bool:
    """Ctrl is down without Shift, Alt (which includes AltGr) or a Windows key."""
    if not is_key_down(VK_CONTROL):
        return False
    return not any(is_key_down(key) for key in (VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN))


def descendants(hwnd: int) -> list[int]:
    """All windows below `hwnd`, depth first in Z order, as EnumChildWindows reports them."""
    found: list[int] = []

    def collect(child: int, _parameter: int) -> bool:
        found.append(child)
        return True

    user32.EnumChildWindows(hwnd, WNDENUMPROC(collect), 0)
    return found


def foreground_explorer() -> tuple[int, int] | None:
    """`(window, tab)` of the Explorer window in front, wherever its focus is."""
    hwnd = user32.GetForegroundWindow()
    if class_name(hwnd) not in EXPLORER_WINDOW_CLASSES:
        return None
    return hwnd, active_tab(hwnd)


def is_file_dialog(hwnd: int) -> bool:
    """Whether `hwnd` is a dialog box that hosts a shell file list.

    Common Open and Save dialogs are ordinary `#32770` dialogs that embed the same
    `SHELLDLL_DefView` as Explorer; message boxes and other dialogs embed none.
    """
    if not hwnd or class_name(hwnd) != DIALOG_CLASS:
        return False
    return any(class_name(child) == FILE_VIEW_CLASS for child in descendants(hwnd))


def foreground_file_dialog() -> int | None:
    hwnd = user32.GetForegroundWindow()
    return hwnd if is_file_dialog(hwnd) else None


def explorer_file_view() -> tuple[int, int] | None:
    """`(window, tab)` when Explorer's file list has keyboard focus, else None.

    Editable controls (address bar, search box, rename) and menu, drag or resize
    interaction never count as the file view.
    """
    hwnd = user32.GetForegroundWindow()
    if class_name(hwnd) not in EXPLORER_WINDOW_CLASSES:
        return None
    if _focused_file_view(hwnd) is None:
        return None
    return hwnd, active_tab(hwnd)


def dialog_file_view() -> tuple[int, int] | None:
    """`(dialog, file view)` when the file list of an Open or Save dialog has focus.

    The file-name box, address bar, search box and navigation pane never count.
    A dialog recreates its file view when it changes folder, so the pair also
    identifies the folder shown when it was captured.
    """
    hwnd = user32.GetForegroundWindow()
    if class_name(hwnd) != DIALOG_CLASS:
        return None
    view = _focused_file_view(hwnd)
    return (hwnd, view) if view is not None else None


def _focused_file_view(top_level: int) -> int | None:
    """The `SHELLDLL_DefView` that contains the idle keyboard focus below `top_level`."""
    focus = idle_focus_window()
    if not focus or class_name(focus) not in FILE_VIEW_FOCUS_CLASSES:
        return None
    return _file_view_ancestor(focus, top_level)


def idle_focus_window() -> int | None:
    """The foreground thread's focus window, unless a menu, drag or resize is active."""
    info = GuiThreadInfo(cbSize=ctypes.sizeof(GuiThreadInfo))
    if not user32.GetGUIThreadInfo(0, ctypes.byref(info)):
        return None
    if info.flags & GUI_INTERACTION_FLAGS:
        return None
    if info.hwndMenuOwner or info.hwndCapture or info.hwndMoveSize:
        return None
    return info.hwndFocus


def _file_view_ancestor(focus: int, top_level: int) -> int | None:
    for _ in range(MAX_FOCUS_ANCESTORS):
        if class_name(focus) == FILE_VIEW_CLASS:
            return focus
        focus = user32.GetParent(focus)
        if not focus or focus == top_level:
            return None
    return None


@dataclass(frozen=True, slots=True)
class TypedKey:
    """Printable text of a key press, or a dead key that waits for the next key."""

    text: str = ""
    dead_key: bool = False


def typed_key(data: KeyboardData) -> TypedKey:
    """What a key press produces in the foreground window's keyboard layout.

    Caps Lock is read from this thread's key state, which can lag the foreground
    thread. Search is case-insensitive, so only layouts where Caps Lock changes
    non-letter characters are affected.
    """
    if any(is_key_down(key) for key in (VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN)):
        return TypedKey()
    state = (ctypes.c_ubyte * 256)()
    for key in (VK_SHIFT, VK_LSHIFT, VK_RSHIFT):
        if is_key_down(key):
            state[key] = KEY_STATE_DOWN
    state[VK_CAPITAL] = user32.GetKeyState(VK_CAPITAL) & KEY_TOGGLED_BIT
    state[data.vkCode] |= KEY_STATE_DOWN
    thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    layout = user32.GetKeyboardLayout(thread)
    buffer = ctypes.create_unicode_buffer(8)
    count = user32.ToUnicodeEx(
        data.vkCode,
        data.scanCode,
        state,
        buffer,
        len(buffer),
        TOUNICODE_KEEP_KEYBOARD_STATE,
        layout,
    )
    if count < 0:
        return TypedKey(dead_key=True)
    text = buffer[:count]
    if not text or not text.isprintable() or text.isspace():
        return TypedKey()
    return TypedKey(text)
