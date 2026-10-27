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
    if position == "top":
        v[2] = my + thickness
        v[8], v[9] = mx, mx + mw - 1
    elif position == "bottom":
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


def stacking_windows() -> list[int]:
    """Managed windows in stacking order, bottom-most first (EWMH)."""
    r = _get_property(_root(), "_NET_CLIENT_LIST_STACKING", XA_WINDOW)
    return _ulongs(r[2], r[1]) if r else client_windows()


def window_title(xid: int) -> str:
    for prop, kind in (("_NET_WM_NAME", atom("UTF8_STRING")), ("WM_NAME", 31)):  # 31 = XA_STRING
        r = _get_property(xid, prop, kind)
        if r and r[0] == 8 and r[1]:
            return r[2].decode(errors="replace")
    return ""


def skips_taskbar(xid: int) -> bool:
    r = _get_property(xid, "_NET_WM_STATE", XA_ATOM)
    return bool(r) and atom("_NET_WM_STATE_SKIP_TASKBAR") in _ulongs(r[2], r[1])


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


# -- event dispatch ----------------------------------------------------------------
# One GLib fd watch on our X connection feeds every listener below, so all of
# this is event-driven: no timers, zero CPU while nothing happens.
_property_listeners: list[tuple[set, Callable[[], None]]] = []
_raw_key_listeners: list[Callable[[int, int], None]] = []  # (evtype, keycode)
_dispatch_source = 0
_xi_opcode = 0
_event_dpy = None  # dedicated connection: only the dispatcher ever reads from it


def _events_display():
    """A second X connection used only for receiving events.

    Sharing one connection is a classic Xlib + main-loop bug: any query on it
    (window list, moving the dock) pulls pending events into Xlib's private
    queue, the socket then looks idle, and the events are only seen at the
    next unrelated wake-up.  Here nothing but the dispatcher touches it.
    """
    global _event_dpy
    if _event_dpy is None:
        _open()  # loads libX11 and installs the error handler
        _event_dpy = _x().XOpenDisplay(None) or None
    return _event_dpy

GenericEvent = 35
XI_RawKeyPress, XI_RawKeyRelease, XI_RawButtonPress = 13, 14, 15
XIAllMasterDevices = 1


class XGenericEventCookie(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("extension", ctypes.c_int), ("evtype", ctypes.c_int),
                ("cookie", ctypes.c_uint), ("data", ctypes.c_void_p)]


class XIRawEventHead(ctypes.Structure):
    """Leading fields of XIRawEvent (all we need: evtype and detail=keycode)."""
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("extension", ctypes.c_int), ("evtype", ctypes.c_int),
                ("time", ctypes.c_ulong), ("deviceid", ctypes.c_int), ("sourceid", ctypes.c_int),
                ("detail", ctypes.c_int), ("flags", ctypes.c_int)]


class XIEventMask(ctypes.Structure):
    _fields_ = [("deviceid", ctypes.c_int), ("mask_len", ctypes.c_int), ("mask", ctypes.POINTER(ctypes.c_ubyte))]


def _ensure_dispatch() -> None:
    global _dispatch_source
    if _dispatch_source:
        return
    from gi.repository import GLib

    dpy = _events_display()
    ev = XEvent()

    def on_readable(_fd, _cond):
        fire = set()
        while _x().XPending(dpy):
            _x().XNextEvent(dpy, ctypes.byref(ev))
            if ev.type == PropertyNotify:
                # XPropertyEvent.atom is the 6th long-sized field.
                for i, (atoms, _cb) in enumerate(_property_listeners):
                    if ev.pad[5] in atoms:
                        fire.add(i)
            elif ev.type == GenericEvent and _xi_opcode:
                cookie = ctypes.cast(ctypes.byref(ev), ctypes.POINTER(XGenericEventCookie)).contents
                if cookie.extension == _xi_opcode and _x().XGetEventData(dpy, ctypes.byref(cookie)):
                    try:
                        raw = ctypes.cast(cookie.data, ctypes.POINTER(XIRawEventHead)).contents
                        for cb in list(_raw_key_listeners):
                            cb(raw.evtype, raw.detail)
                    finally:
                        _x().XFreeEventData(dpy, ctypes.byref(cookie))
        for i in fire:
            _property_listeners[i][1]()
        return GLib.SOURCE_CONTINUE

    _x().XGetEventData.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _x().XFreeEventData.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _dispatch_source = GLib.unix_fd_add_full(GLib.PRIORITY_DEFAULT, _x().XConnectionNumber(dpy),
                                             GLib.IOCondition.IN, on_readable)


def watch_root(callback: Callable[[], None]) -> int:
    """Call ``callback`` when the window list or active window changes."""
    dpy = _events_display()
    _x().XSelectInput(dpy, _x().XDefaultRootWindow(dpy), PropertyChangeMask)
    _x().XFlush(dpy)
    _property_listeners.append(({atom("_NET_CLIENT_LIST"), atom("_NET_ACTIVE_WINDOW")}, callback))
    _ensure_dispatch()
    return len(_property_listeners)


class SuperTapDetector:
    """Recognise a *tap* of the Windows (Super) key: press and release with no
    other key or mouse button in between, within ``max_ms``.

    Pure state machine (unit-tested); fed by raw XInput2 events.  Holding the
    key for Windows+A, Windows+Space, Windows+drag etc. never counts as a tap.
    """

    def __init__(self, super_codes: set[int], on_tap: Callable[[], None], max_ms: int = 600,
                 clock: Callable[[], float] | None = None):
        import time

        self.codes = super_codes
        self.on_tap = on_tap
        self.max_ms = max_ms
        self.clock = clock or time.monotonic
        self.down_at: float | None = None
        self.spoiled = False

    def feed(self, evtype: int, detail: int) -> None:
        if evtype == XI_RawKeyPress:
            if detail in self.codes:
                if self.down_at is None:  # ignore auto-repeat
                    self.down_at = self.clock()
                    self.spoiled = False
            elif self.down_at is not None:
                self.spoiled = True  # it was a Windows+<key> combination
        elif evtype == XI_RawButtonPress:
            if self.down_at is not None:
                self.spoiled = True  # Windows+click / drag
        elif evtype == XI_RawKeyRelease and detail in self.codes and self.down_at is not None:
            held_ms = (self.clock() - self.down_at) * 1000
            tap = not self.spoiled and held_ms <= self.max_ms
            self.down_at = None
            self.spoiled = False
            if tap:
                self.on_tap()


def watch_super_tap(on_tap: Callable[[], None]) -> bool:
    """Listen (without grabbing) for taps of the Windows key.  Returns success.

    Raw XInput2 events are delivered to every client that asks, regardless of
    grabs, so this never interferes with Windows+<key> shortcuts or other apps.
    """
    global _xi_opcode
    dpy = _events_display()
    if dpy is None:
        return False
    if not _xi_opcode:
        op, ev, er = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        _x().XQueryExtension.argtypes = [ctypes.c_void_p, ctypes.c_char_p] + [ctypes.c_void_p] * 3
        if not _x().XQueryExtension(dpy, b"XInputExtension", ctypes.byref(op), ctypes.byref(ev), ctypes.byref(er)):
            log.warning("XInputExtension missing; Windows-key tap unavailable")
            return False
        try:
            xi = ctypes.CDLL(ctypes.util.find_library("Xi") or "libXi.so.6")
        except OSError:
            log.warning("libXi not installed; Windows-key tap unavailable")
            return False
        major, minor = ctypes.c_int(2), ctypes.c_int(2)
        xi.XIQueryVersion.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        if xi.XIQueryVersion(dpy, ctypes.byref(major), ctypes.byref(minor)) != 0:
            log.warning("XInput 2 not supported by the X server")
            return False
        mask = (ctypes.c_ubyte * 4)()
        for bit in (XI_RawKeyPress, XI_RawKeyRelease, XI_RawButtonPress):
            mask[bit >> 3] |= 1 << (bit & 7)
        evmask = XIEventMask(XIAllMasterDevices, 4, ctypes.cast(mask, ctypes.POINTER(ctypes.c_ubyte)))
        xi.XISelectEvents.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(XIEventMask), ctypes.c_int]
        xi.XISelectEvents(dpy, _x().XDefaultRootWindow(dpy), ctypes.byref(evmask), 1)
        _x().XFlush(dpy)
        _xi_opcode = op.value
    _x().XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    codes = {_x().XKeysymToKeycode(dpy, ks) for ks in (0xFFEB, 0xFFEC)} - {0}  # Super_L, Super_R
    detector = SuperTapDetector(codes, on_tap)
    _raw_key_listeners.append(detector.feed)
    _ensure_dispatch()
    return True
