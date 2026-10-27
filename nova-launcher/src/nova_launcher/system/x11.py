"""Minimal X11 helpers (ctypes over libX11) for things GTK 4 no longer exposes.

GTK 4 removed window positioning and window-type hints.  On an X11 session
(Ubuntu's default with the NVIDIA driver) the EWMH properties below are the
documented, supported way to make a dock: Mutter keeps ``_NET_WM_WINDOW_TYPE_DOCK``
windows above others, undecorated and on every workspace.

On Wayland none of this is used; ``available()`` returns False and the dock is
disabled while the launcher windows keep working as normal windows.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
from typing import Callable

log = logging.getLogger("nova.x11")

_lib = None
_dpy = None
_atoms: dict[str, int] = {}

Window = ctypes.c_ulong
Atom = ctypes.c_ulong

XA_ATOM = 4
XA_CARDINAL = 6
XA_WINDOW = 33
PropModeReplace = 0
PropertyChangeMask = 1 << 22
PropertyNotify = 28
ClientMessage = 33
SubstructureRedirectMask = 1 << 20
SubstructureNotifyMask = 1 << 19


class XClientMessageEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p), ("window", Window), ("message_type", Atom),
        ("format", ctypes.c_int), ("data", ctypes.c_long * 5),
    ]


class XEvent(ctypes.Union):
    _fields_ = [("type", ctypes.c_int), ("xclient", XClientMessageEvent), ("pad", ctypes.c_long * 24)]


def available() -> bool:
    return os.environ.get("XDG_SESSION_TYPE") != "wayland" and bool(os.environ.get("DISPLAY")) and _open() is not None


def _open():
    global _lib, _dpy
    if _dpy is not None:
        return _dpy
    name = ctypes.util.find_library("X11") or "libX11.so.6"
    try:
        _lib = ctypes.CDLL(name)
    except OSError:
        return None
    _lib.XOpenDisplay.restype = ctypes.c_void_p
    _lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
    _lib.XInternAtom.restype = Atom
    _lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    _lib.XChangeProperty.argtypes = [ctypes.c_void_p, Window, Atom, Atom, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_void_p, ctypes.c_int]
    _lib.XMoveWindow.argtypes = [ctypes.c_void_p, Window, ctypes.c_int, ctypes.c_int]
    _lib.XFlush.argtypes = [ctypes.c_void_p]
    _lib.XDefaultRootWindow.restype = Window
    _lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    _lib.XGetWindowProperty.argtypes = [
        ctypes.c_void_p, Window, Atom, ctypes.c_long, ctypes.c_long, ctypes.c_int, Atom,
        ctypes.POINTER(Atom), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_void_p)]
    _lib.XFree.argtypes = [ctypes.c_void_p]
    _lib.XSelectInput.argtypes = [ctypes.c_void_p, Window, ctypes.c_long]
    _lib.XConnectionNumber.argtypes = [ctypes.c_void_p]
    _lib.XPending.argtypes = [ctypes.c_void_p]
    _lib.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.POINTER(XEvent)]
    _lib.XSendEvent.argtypes = [ctypes.c_void_p, Window, ctypes.c_int, ctypes.c_long, ctypes.POINTER(XEvent)]
    _lib.XSetErrorHandler.argtypes = [ctypes.c_void_p]
    _lib.XSetErrorHandler.restype = ctypes.c_void_p
    _dpy = _lib.XOpenDisplay(None)
    if not _dpy:
        _dpy = None
        return None
    # Our own connection: X errors (e.g. a window closing mid-query) must be
    # ignored rather than terminating the process (Xlib's default handler).
    global _err_handler
    _err_handler = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(lambda d, e: 0)
    _lib.XSetErrorHandler(ctypes.cast(_err_handler, ctypes.c_void_p))
    return _dpy


_err_handler = None


def atom(name: str) -> int:
    if name not in _atoms:
        dpy = _open()  # loads _lib on first use
        _atoms[name] = _lib.XInternAtom(dpy, name.encode(), 0)
    return _atoms[name]


def _set_atoms(xid: int, prop: str, values: list[str]) -> None:
    arr = (ctypes.c_ulong * len(values))(*[atom(v) for v in values])
    _lib.XChangeProperty(_open(), xid, atom(prop), XA_ATOM, 32, PropModeReplace, arr, len(values))


def _set_cardinals(xid: int, prop: str, values: list[int]) -> None:
    arr = (ctypes.c_ulong * len(values))(*values)
    _lib.XChangeProperty(_open(), xid, atom(prop), XA_CARDINAL, 32, PropModeReplace, arr, len(values))


def make_dock(xid: int) -> None:
    """Call on an unmapped window (GTK 'realize') to make it a dock."""
    _set_atoms(xid, "_NET_WM_WINDOW_TYPE", ["_NET_WM_WINDOW_TYPE_DOCK"])
    _set_atoms(xid, "_NET_WM_STATE", ["_NET_WM_STATE_ABOVE", "_NET_WM_STATE_STICKY",
                                       "_NET_WM_STATE_SKIP_TASKBAR", "_NET_WM_STATE_SKIP_PAGER"])
    _set_cardinals(xid, "_NET_WM_DESKTOP", [0xFFFFFFFF])
    _lib.XFlush(_open())


def make_utility(xid: int) -> None:
    """Quick-launcher popup: no taskbar entry, kept above."""
    _set_atoms(xid, "_NET_WM_STATE", ["_NET_WM_STATE_ABOVE", "_NET_WM_STATE_SKIP_TASKBAR",
                                       "_NET_WM_STATE_SKIP_PAGER"])
    _lib.XFlush(_open())


def move(xid: int, x: int, y: int) -> None:
    _lib.XMoveWindow(_open(), xid, int(x), int(y))
    _lib.XFlush(_open())


def _get_property(xid: int, prop: str, req_type: int, max_len: int = 4096) -> tuple[int, int, bytes] | None:
    actual_type, fmt = Atom(), ctypes.c_int()
    nitems, after = ctypes.c_ulong(), ctypes.c_ulong()
    data = ctypes.c_void_p()
    rc = _lib.XGetWindowProperty(_open(), xid, atom(prop), 0, max_len, 0, req_type,
                                 ctypes.byref(actual_type), ctypes.byref(fmt), ctypes.byref(nitems),
                                 ctypes.byref(after), ctypes.byref(data))
    if rc != 0 or not data.value:
        return None
    try:
        n = nitems.value
        if fmt.value == 32:
            raw = ctypes.string_at(data.value, n * ctypes.sizeof(ctypes.c_ulong))
        elif fmt.value == 8:
            raw = ctypes.string_at(data.value, n)
        else:
            raw = b""
        return fmt.value, n, raw
    finally:
        _lib.XFree(data)


def _ulongs(raw: bytes, n: int) -> list[int]:
    return list((ctypes.c_ulong * n).from_buffer_copy(raw)) if n else []


def client_windows() -> list[int]:
    r = _get_property(_lib.XDefaultRootWindow(_open()), "_NET_CLIENT_LIST", XA_WINDOW)
    return _ulongs(r[2], r[1]) if r else []


def active_window() -> int:
    r = _get_property(_lib.XDefaultRootWindow(_open()), "_NET_ACTIVE_WINDOW", XA_WINDOW)
    vals = _ulongs(r[2], r[1]) if r else []
    return vals[0] if vals else 0


def wm_class(xid: int) -> tuple[str, str]:
    r = _get_property(xid, "WM_CLASS", 31)  # XA_STRING
    if not r:
        return "", ""
    parts = r[2].split(b"\0")
    inst = parts[0].decode(errors="replace") if parts else ""
    cls = parts[1].decode(errors="replace") if len(parts) > 1 else ""
    return inst, cls


def window_type_is_normal(xid: int) -> bool:
    r = _get_property(xid, "_NET_WM_WINDOW_TYPE", XA_ATOM)
    if not r or not r[1]:
        return True
    return atom("_NET_WM_WINDOW_TYPE_NORMAL") in _ulongs(r[2], r[1])


def activate(xid: int, timestamp: int = 0) -> None:
    """Ask the WM to focus ``xid`` (EWMH _NET_ACTIVE_WINDOW, source=pager)."""
    ev = XEvent()
    ev.xclient.type = ClientMessage
    ev.xclient.send_event = 1
    ev.xclient.window = xid
    ev.xclient.message_type = atom("_NET_ACTIVE_WINDOW")
    ev.xclient.format = 32
    ev.xclient.data[0] = 2  # source indication: pager/taskbar (user action)
    ev.xclient.data[1] = timestamp
    root = _lib.XDefaultRootWindow(_open())
    _lib.XSendEvent(_open(), root, 0, SubstructureRedirectMask | SubstructureNotifyMask, ctypes.byref(ev))
    _lib.XFlush(_open())


def close_window(xid: int, timestamp: int = 0) -> None:
    """Politely ask the WM to close ``xid`` (EWMH _NET_CLOSE_WINDOW)."""
    ev = XEvent()
    ev.xclient.type = ClientMessage
    ev.xclient.send_event = 1
    ev.xclient.window = xid
    ev.xclient.message_type = atom("_NET_CLOSE_WINDOW")
    ev.xclient.format = 32
    ev.xclient.data[0] = timestamp
    ev.xclient.data[1] = 2
    root = _lib.XDefaultRootWindow(_open())
    _lib.XSendEvent(_open(), root, 0, SubstructureRedirectMask | SubstructureNotifyMask, ctypes.byref(ev))
    _lib.XFlush(_open())


def watch_root(callback: Callable[[], None]) -> int:
    """Call ``callback`` when the window list or active window changes.

    The X connection fd is added to the GLib main loop, so this is purely
    event-driven: no timers, zero CPU while nothing changes.
    """
    from gi.repository import GLib

    dpy = _open()
    root = _lib.XDefaultRootWindow(dpy)
    _lib.XSelectInput(dpy, root, PropertyChangeMask)
    _lib.XFlush(dpy)
    watched = {atom("_NET_CLIENT_LIST"), atom("_NET_ACTIVE_WINDOW")}
    ev = XEvent()

    def on_readable(_fd, _cond):
        fire = False
        while _lib.XPending(dpy):
            _lib.XNextEvent(dpy, ctypes.byref(ev))
            if ev.type == PropertyNotify:
                # XPropertyEvent.atom is the 6th long-sized field.
                if ev.pad[5] in watched:
                    fire = True
        if fire:
            callback()
        return GLib.SOURCE_CONTINUE

    return GLib.unix_fd_add_full(GLib.PRIORITY_DEFAULT, _lib.XConnectionNumber(dpy), GLib.IOCondition.IN, on_readable)
