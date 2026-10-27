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
        _atoms[name] = _x().XInternAtom(_open(), name.encode(), 0)
    return _atoms[name]


def _x():
    """libX11 handle; opens the display first so _lib is always loaded."""
    _open()
    return _lib


def _root() -> int:
    return _x().XDefaultRootWindow(_open())


def _set_atoms(xid: int, prop: str, values: list[str]) -> None:
    arr = (ctypes.c_ulong * len(values))(*[atom(v) for v in values])
    _x().XChangeProperty(_open(), xid, atom(prop), XA_ATOM, 32, PropModeReplace, arr, len(values))


def _set_cardinals(xid: int, prop: str, values: list[int]) -> None:
    arr = (ctypes.c_ulong * len(values))(*values)
    _x().XChangeProperty(_open(), xid, atom(prop), XA_CARDINAL, 32, PropModeReplace, arr, len(values))


def make_dock(xid: int) -> None:
    """Call on an unmapped window (GTK 'realize') to make it a dock."""
    _set_atoms(xid, "_NET_WM_WINDOW_TYPE", ["_NET_WM_WINDOW_TYPE_DOCK"])
    _set_atoms(xid, "_NET_WM_STATE", ["_NET_WM_STATE_ABOVE", "_NET_WM_STATE_STICKY",
                                       "_NET_WM_STATE_SKIP_TASKBAR", "_NET_WM_STATE_SKIP_PAGER"])
    _set_cardinals(xid, "_NET_WM_DESKTOP", [0xFFFFFFFF])
    _x().XFlush(_open())


def strut_values(position: str, thickness: int, mon: tuple[int, int, int, int],
                 root: tuple[int, int]) -> list[int]:
    """_NET_WM_STRUT_PARTIAL for a dock on one monitor edge (pure; unit-tested).

    Struts are measured from the edges of the whole X screen, so a monitor that
    does not touch the screen edge needs the gap added.
    """
    mx, my, mw, mh = mon
    rw, rh = root
    v = [0] * 12
    if thickness <= 0:
        return v
    if position == "bottom":
        v[3] = rh - (my + mh) + thickness
        v[10], v[11] = mx, mx + mw - 1
    elif position == "left":
        v[0] = mx + thickness
        v[4], v[5] = my, my + mh - 1
    elif position == "right":
        v[1] = rw - (mx + mw) + thickness
        v[6], v[7] = my, my + mh - 1
    return v


def set_strut(xid: int, values: list[int]) -> None:
    """Reserve screen space so maximized windows stop at the dock (EWMH)."""
    _set_cardinals(xid, "_NET_WM_STRUT_PARTIAL", values)
    _set_cardinals(xid, "_NET_WM_STRUT", values[:4])
    _x().XFlush(_open())


def change_state(xid: int, add: bool, *states: str) -> None:
    """Ask the WM to add/remove _NET_WM_STATE atoms on a *mapped* window.

    Writing the property directly only works before mapping (and GTK rewrites
    it on map), so mapped windows need the EWMH client message.
    """
    for i in range(0, len(states), 2):
        pair = states[i:i + 2]
        ev = XEvent()
        ev.xclient.type = ClientMessage
        ev.xclient.send_event = 1
        ev.xclient.window = xid
        ev.xclient.message_type = atom("_NET_WM_STATE")
        ev.xclient.format = 32
        ev.xclient.data[0] = 1 if add else 0
        ev.xclient.data[1] = atom(pair[0])
        ev.xclient.data[2] = atom(pair[1]) if len(pair) > 1 else 0
        ev.xclient.data[3] = 2  # source: pager / user action
        _x().XSendEvent(_open(), _root(), 0, SubstructureRedirectMask | SubstructureNotifyMask, ctypes.byref(ev))
    _x().XFlush(_open())


def window_states(xid: int) -> list[str]:
    """Names of the window's current _NET_WM_STATE atoms (for checks/tests)."""
    r = _get_property(xid, "_NET_WM_STATE", XA_ATOM)
    if not r:
        return []
    names = {v: k for k, v in _atoms.items()}
    return [names.get(a, str(a)) for a in _ulongs(r[2], r[1])]


def make_desktop_widget(xid: int) -> None:
    """Desktop gadget: kept below normal windows, on every workspace, no taskbar/Alt+Tab."""
    _set_atoms(xid, "_NET_WM_WINDOW_TYPE", ["_NET_WM_WINDOW_TYPE_UTILITY"])
    _set_atoms(xid, "_NET_WM_STATE", ["_NET_WM_STATE_BELOW", "_NET_WM_STATE_STICKY",
                                       "_NET_WM_STATE_SKIP_TASKBAR", "_NET_WM_STATE_SKIP_PAGER"])
    _set_cardinals(xid, "_NET_WM_DESKTOP", [0xFFFFFFFF])
    # EWMH: a user time of 0 means "do not focus on map" (prevents the
    # demands-attention / "is ready" notification).
    _set_cardinals(xid, "_NET_WM_USER_TIME", [0])
    _x().XFlush(_open())


def make_utility(xid: int) -> None:
    """Quick-launcher popup: no taskbar entry, kept above."""
    _set_atoms(xid, "_NET_WM_STATE", ["_NET_WM_STATE_ABOVE", "_NET_WM_STATE_SKIP_TASKBAR",
                                       "_NET_WM_STATE_SKIP_PAGER"])
    _x().XFlush(_open())


def move(xid: int, x: int, y: int) -> None:
    _x().XMoveWindow(_open(), xid, int(x), int(y))
    _x().XFlush(_open())


def _get_property(xid: int, prop: str, req_type: int, max_len: int = 4096) -> tuple[int, int, bytes] | None:
    actual_type, fmt = Atom(), ctypes.c_int()
    nitems, after = ctypes.c_ulong(), ctypes.c_ulong()
    data = ctypes.c_void_p()
    rc = _x().XGetWindowProperty(_open(), xid, atom(prop), 0, max_len, 0, req_type,
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
        _x().XFree(data)


def _ulongs(raw: bytes, n: int) -> list[int]:
    return list((ctypes.c_ulong * n).from_buffer_copy(raw)) if n else []


def client_windows() -> list[int]:
    r = _get_property(_root(), "_NET_CLIENT_LIST", XA_WINDOW)
    return _ulongs(r[2], r[1]) if r else []


def active_window() -> int:
    r = _get_property(_root(), "_NET_ACTIVE_WINDOW", XA_WINDOW)
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
    root = _root()
    _x().XSendEvent(_open(), root, 0, SubstructureRedirectMask | SubstructureNotifyMask, ctypes.byref(ev))
    _x().XFlush(_open())


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
    root = _root()
    _x().XSendEvent(_open(), root, 0, SubstructureRedirectMask | SubstructureNotifyMask, ctypes.byref(ev))
    _x().XFlush(_open())


def watch_root(callback: Callable[[], None]) -> int:
    """Call ``callback`` when the window list or active window changes.

    The X connection fd is added to the GLib main loop, so this is purely
    event-driven: no timers, zero CPU while nothing changes.
    """
    from gi.repository import GLib

    dpy = _open()
    root = _x().XDefaultRootWindow(dpy)
    _x().XSelectInput(dpy, root, PropertyChangeMask)
    _x().XFlush(dpy)
    watched = {atom("_NET_CLIENT_LIST"), atom("_NET_ACTIVE_WINDOW")}
    ev = XEvent()

    def on_readable(_fd, _cond):
        fire = False
        while _x().XPending(dpy):
            _x().XNextEvent(dpy, ctypes.byref(ev))
            if ev.type == PropertyNotify:
                # XPropertyEvent.atom is the 6th long-sized field.
                if ev.pad[5] in watched:
                    fire = True
        if fire:
            callback()
        return GLib.SOURCE_CONTINUE

    return GLib.unix_fd_add_full(GLib.PRIORITY_DEFAULT, _x().XConnectionNumber(dpy), GLib.IOCondition.IN, on_readable)
