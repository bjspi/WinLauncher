"""Explorer's classic context menu ("Show more options") for one file or folder.

The shell builds the menu, including entries other programs add such as 7-Zip
or "Open with". Submenus like "Open with" and "Send to" fill themselves only
when the menu's owner window hands certain messages to the shell's
IContextMenu3, so the menu belongs to a small hidden window that does exactly
that, as Explorer does. pywin32 has no wrapper for IContextMenu3, so the shell
interfaces are called through their COM vtables with ctypes.

Call it on the GUI thread: the menu runs a modal loop, like any context menu.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from pathlib import Path

logger = logging.getLogger(__name__)

# Window messages the shell needs for owner-drawn items and lazily filled submenus.
WM_NULL = 0x0000
WM_DRAWITEM = 0x002B
WM_MEASUREITEM = 0x002C
WM_INITMENUPOPUP = 0x0117
WM_MENUCHAR = 0x0120
SHELL_MENU_MESSAGES = frozenset({WM_DRAWITEM, WM_MEASUREITEM, WM_INITMENUPOPUP, WM_MENUCHAR})
# Command identifiers the shell may use; 0 means "nothing chosen".
FIRST_COMMAND = 1
LAST_COMMAND = 0x7FFF
CMF_NORMAL = 0x0000
CMF_EXTENDEDVERBS = 0x0100  # The entries Explorer shows with Shift held.
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
SW_SHOWNORMAL = 1
OWNER_CLASS = "WinLauncherShellMenuOwner"

IID_ISHELLFOLDER = "{000214E6-0000-0000-C000-000000000046}"
IID_ICONTEXTMENU = "{000214E4-0000-0000-C000-000000000046}"
IID_ICONTEXTMENU3 = "{BCFCE0A0-EC17-11D0-8D10-00A0C90F2719}"
# Vtable slots: IUnknown has 3; IShellFolder's GetUIObjectOf is its 8th method;
# IContextMenu adds QueryContextMenu, InvokeCommand, GetCommandString, then
# IContextMenu2's HandleMenuMsg and IContextMenu3's HandleMenuMsg2.
QUERY_INTERFACE, RELEASE = 0, 2
GET_UI_OBJECT_OF = 10
QUERY_CONTEXT_MENU, INVOKE_COMMAND = 3, 4
HANDLE_MENU_MSG2 = 7


class _Guid(ctypes.Structure):
    _fields_ = [
        ("data1", wintypes.DWORD),
        ("data2", wintypes.WORD),
        ("data3", wintypes.WORD),
        ("data4", ctypes.c_ubyte * 8),
    ]


class _InvokeCommandInfo(ctypes.Structure):
    """CMINVOKECOMMANDINFO; `verb` carries the command offset as MAKEINTRESOURCEA."""

    _fields_ = [
        ("size", wintypes.DWORD),
        ("mask", wintypes.DWORD),
        ("window", wintypes.HWND),
        ("verb", ctypes.c_void_p),
        ("parameters", ctypes.c_char_p),
        ("directory", ctypes.c_char_p),
        ("show", ctypes.c_int),
        ("hotkey", wintypes.DWORD),
        ("icon", wintypes.HANDLE),
    ]


_handlers: dict[int, ctypes.c_void_p] = {}  # Owner window -> its menu's IContextMenu3.
_owner_class: int | None = None


def show_shell_menu(path: Path, *, extended: bool = False) -> bool:
    """Show the menu for `path` at the mouse pointer and run what the user picks.

    Returns whether a command ran. Raises OSError when the shell has no menu
    for the path, for example because it no longer exists.
    """
    import win32api
    import win32gui

    context_menu = _context_menu(path)
    owner = _create_owner()
    menu = win32gui.CreatePopupMenu()
    try:
        flags = CMF_EXTENDEDVERBS if extended else CMF_NORMAL
        _call(
            context_menu,
            QUERY_CONTEXT_MENU,
            wintypes.HMENU,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
            args=(menu, 0, FIRST_COMMAND, LAST_COMMAND, flags),
        )
        if (handler := _query_interface(context_menu, IID_ICONTEXTMENU3)) is not None:
            _handlers[owner] = handler
        x, y = win32api.GetCursorPos()
        # The menu closes properly on outside clicks only if its owner is in front.
        win32gui.SetForegroundWindow(owner)
        command = win32gui.TrackPopupMenu(
            menu, TPM_RETURNCMD | TPM_RIGHTBUTTON, x, y, 0, owner, None
        )
        win32gui.PostMessage(owner, WM_NULL, 0, 0)
        if not command:
            return False
        info = _InvokeCommandInfo(
            size=ctypes.sizeof(_InvokeCommandInfo),
            window=owner,
            verb=command - FIRST_COMMAND,
            show=SW_SHOWNORMAL,
        )
        _call(context_menu, INVOKE_COMMAND, ctypes.POINTER(_InvokeCommandInfo), args=(info,))
        logger.info("Shell menu command ran: offset=%d", command - FIRST_COMMAND)
        return True
    finally:
        win32gui.DestroyMenu(menu)
        if (handler := _handlers.pop(owner, None)) is not None:
            _release(handler)
        win32gui.DestroyWindow(owner)
        _release(context_menu)


def menu_item_count(path: Path) -> int:
    """How many top-level entries the shell offers for `path`; for tests and diagnostics."""
    import win32gui

    context_menu = _context_menu(path)
    menu = win32gui.CreatePopupMenu()
    try:
        _call(
            context_menu,
            QUERY_CONTEXT_MENU,
            wintypes.HMENU,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
            args=(menu, 0, FIRST_COMMAND, LAST_COMMAND, CMF_NORMAL),
        )
        return win32gui.GetMenuItemCount(menu)
    finally:
        win32gui.DestroyMenu(menu)
        _release(context_menu)


def _context_menu(path: Path) -> ctypes.c_void_p:
    """The shell's IContextMenu for `path`, asked from its parent folder."""
    shell32, ole32 = ctypes.OleDLL("shell32"), ctypes.OleDLL("ole32")
    item = ctypes.c_void_p()
    folder = ctypes.c_void_p()
    child = ctypes.c_void_p()
    try:
        shell32.SHParseDisplayName(str(path), None, ctypes.byref(item), 0, None)
        shell32.SHBindToParent(
            item, ctypes.byref(_guid(IID_ISHELLFOLDER)), ctypes.byref(folder), ctypes.byref(child)
        )
        context_menu = ctypes.c_void_p()
        children = (ctypes.c_void_p * 1)(child)
        _call(
            folder,
            GET_UI_OBJECT_OF,
            wintypes.HWND,
            wintypes.UINT,
            ctypes.c_void_p,
            ctypes.POINTER(_Guid),
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            args=(
                None,
                1,
                children,
                ctypes.byref(_guid(IID_ICONTEXTMENU)),
                None,
                ctypes.byref(context_menu),
            ),
        )
        return context_menu
    except OSError as exc:
        raise OSError(f"Windows has no context menu for {path}.") from exc
    finally:
        if folder:
            _release(folder)
        if item:
            ole32.CoTaskMemFree(item)  # `child` points into `item`.


def _call(interface: ctypes.c_void_p, slot: int, *argtypes, args: tuple) -> int:
    """Call vtable `slot` of a COM interface; a failed HRESULT raises OSError."""
    vtable = ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    method = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtable[slot])
    return method(interface, *args)


def _query_interface(interface: ctypes.c_void_p, iid: str) -> ctypes.c_void_p | None:
    result = ctypes.c_void_p()
    try:
        _call(
            interface,
            QUERY_INTERFACE,
            ctypes.POINTER(_Guid),
            ctypes.POINTER(ctypes.c_void_p),
            args=(ctypes.byref(_guid(iid)), ctypes.byref(result)),
        )
    except OSError:
        return None  # Without it, submenus such as "Send to" may stay empty.
    return result


def _release(interface: ctypes.c_void_p) -> None:
    vtable = ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vtable[RELEASE])(interface)


def _guid(text: str) -> _Guid:
    guid = _Guid()
    ctypes.OleDLL("ole32").CLSIDFromString(text, ctypes.byref(guid))
    return guid


def _create_owner() -> int:
    import win32api
    import win32gui

    global _owner_class
    if _owner_class is None:
        window_class = win32gui.WNDCLASS()
        window_class.lpszClassName = OWNER_CLASS
        window_class.lpfnWndProc = _window_procedure
        window_class.hInstance = win32api.GetModuleHandle(None)
        _owner_class = win32gui.RegisterClass(window_class)
    return win32gui.CreateWindow(
        OWNER_CLASS, "", 0, 0, 0, 0, 0, 0, 0, win32api.GetModuleHandle(None), None
    )


def _window_procedure(hwnd: int, message: int, wparam: int, lparam: int) -> int:
    import win32gui

    handler = _handlers.get(hwnd)
    if handler is not None and message in SHELL_MENU_MESSAGES:
        result = ctypes.c_ssize_t()
        try:
            _call(
                handler,
                HANDLE_MENU_MSG2,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
                ctypes.POINTER(ctypes.c_ssize_t),
                args=(message, wparam, lparam, ctypes.byref(result)),
            )
            return result.value
        except OSError:
            pass  # The shell did not handle it; let Windows do the default.
    return win32gui.DefWindowProc(hwnd, message, wparam, lparam)
