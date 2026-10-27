"""Running applications, grouped from the window list (shared by the dock and
the Dynamic Island).

``group_windows`` and ``next_window`` are pure and unit-tested; the
``WindowTracker`` turns X11 window-list changes into one debounced refresh
that every listener shares.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from .desktop_entry import AppRecord

if TYPE_CHECKING:
    from ..application import NovaApplication

log = logging.getLogger("nova.running")


@dataclass(frozen=True)
class WindowInfo:
    xid: int
    wm_instance: str
    wm_class: str
    title: str = ""


@dataclass
class RunningApp:
    key: str  # desktop-file ID, or "wmclass:<class>" for windows without one
    name: str
    icon: str
    record: AppRecord | None
    windows: list[int] = field(default_factory=list)  # most recently used first
    titles: list[str] = field(default_factory=list)
    active: bool = False


def group_windows(windows: list[WindowInfo], active: int,
                  resolve: Callable[[str, str], AppRecord | None],
                  exclude: set[str] = frozenset()) -> list[RunningApp]:
    """Group windows (stacking order, bottom-most first) into apps.

    Apps come back most recently used first, with the focused app in front.
    """
    apps: dict[str, RunningApp] = {}
    for w in reversed(windows):
        rec = resolve(w.wm_class, w.wm_instance)
        if rec is not None and rec.id in exclude:
            continue
        label = w.wm_class or w.wm_instance
        key = rec.id if rec is not None else f"wmclass:{(label or str(w.xid)).casefold()}"
        app = apps.get(key)
        if app is None:
            if rec is not None:
                app = RunningApp(key, rec.name, rec.icon, rec)
            else:
                app = RunningApp(key, label or w.title or "Window", (w.wm_instance or label).lower(), None)
            apps[key] = app
        app.windows.append(w.xid)
        app.titles.append(w.title)
        app.active = app.active or w.xid == active
    return sorted(apps.values(), key=lambda a: not a.active)  # stable sort keeps MRU order


def next_window(windows: list[int], active: int) -> int:
    """Window to raise for an app: cycle when it is focused, else its most recent."""
    if not windows:
        return 0
    if active in windows:
        return windows[(windows.index(active) + 1) % len(windows)]
    return windows[0]


class WindowTracker:
    """Running apps on X11, refreshed when the window list or focus changes."""

    def __init__(self, app: "NovaApplication"):
        self.app = app
        self.apps: list[RunningApp] = []
        self.active = 0
        self._listeners: list[Callable[[], None]] = []
        self._pending = 0
        self._watching = False

    def connect(self, cb: Callable[[], None]) -> None:
        from ..system import x11

        self._listeners.append(cb)
        if not self._watching and x11.available():
            x11.watch_root(self.schedule_refresh)
            self._watching = True
        self.schedule_refresh()

    def schedule_refresh(self) -> None:
        from gi.repository import GLib

        if not self._pending:
            self._pending = GLib.timeout_add(120, self._refresh)

    def _refresh(self) -> bool:
        self._pending = 0
        self.refresh()
        return False

    def refresh(self) -> None:
        from ..system import x11

        try:
            active = x11.active_window()
            infos = []
            for xid in x11.stacking_windows():
                if not x11.window_type_is_normal(xid) or x11.skips_taskbar(xid):
                    continue
                inst, cls = x11.wm_class(xid)
                infos.append(WindowInfo(xid, inst, cls, x11.window_title(xid)))
        except Exception:
            log.exception("window list refresh failed")
            return
        idx = self.app.index

        def resolve(cls: str, inst: str) -> AppRecord | None:
            # An empty class would match entries with an empty Exec (D-Bus apps).
            return (cls and idx.find_by_wm_class(cls)) or (inst and idx.find_by_wm_class(inst)) or None

        self.active = active
        self.apps = group_windows(infos, active, resolve, exclude={self.app.self_desktop_id()})
        for cb in list(self._listeners):
            cb()

    def running_ids(self) -> dict[str, list[int]]:
        return {a.record.id: a.windows for a in self.apps if a.record is not None}
