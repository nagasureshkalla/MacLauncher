"""Floating, magnifying dock (X11 sessions).

Design notes
* The window has a fixed size large enough for full magnification, so moving
  the pointer never resizes an X window; only icon pixel sizes change.
* Magnification targets are computed from the *unmagnified* layout, so the
  growing bar cannot feed back into the pointer mapping (no jitter).
* The animation runs on the frame clock only while something is moving and
  stops itself when settled: zero CPU while idle.
* The input region is limited to the visible bar (or a 2 px edge strip when
  auto-hidden), so the transparent part of the window never blocks clicks.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from dataclasses import dataclass
from typing import TYPE_CHECKING

import cairo
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from ..core.desktop_entry import AppRecord  # noqa: E402
from ..core.running_apps import next_window  # noqa: E402
from ..system import x11  # noqa: E402
from ..system.monitor_support import dock_origin  # noqa: E402
from .glass import GlassBox  # noqa: E402
from .widgets import gicon_for, popup_menu, record_gicon  # noqa: E402

if TYPE_CHECKING:
    from ..application import NovaApplication

log = logging.getLogger("nova.dock")

ALL_APPS_ICON = str(Path(__file__).resolve().parents[1] / "resources" / "icons" / "nova-all-apps.svg")

TOOLTIP_SPACE = 40
EDGE_MARGIN = 8
BAR_PAD = 8  # padding inside the glass bar (CSS uses the same)
SEP_SIZE = 13


@dataclass
class Slot:
    kind: str  # launchpad | pinned | running | recent | trash | separator
    record: AppRecord | None = None


def magnify(positions: list[float], pointer: float | None, unit: float, mag: float) -> list[float]:
    """Pure function: per-slot scale for a pointer position (unit-tested)."""
    if pointer is None or mag <= 1.0:
        return [1.0] * len(positions)
    out = []
    for c in positions:
        d = abs(c - pointer) / unit
        out.append(1.0 + (mag - 1.0) * math.exp(-(d * d) / 2.0) if d < 3.2 else 1.0)
    return out


class DockItem(Gtk.Box):
    def __init__(self, dock: "DockWindow", slot: Slot, size: int, vertical: bool):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL if vertical else Gtk.Orientation.VERTICAL,
                         spacing=2)
        self.dock = dock
        self.slot = slot
        self.scale = 1.0
        self.add_css_class("nova-dock-item")
        if slot.kind == "launchpad":
            icon = ALL_APPS_ICON
        elif slot.kind == "trash":
            icon = "user-trash"
        else:
            icon = None
        self.image = Gtk.Image.new_from_gicon(
            record_gicon(slot.record) if icon is None and slot.record else gicon_for(icon or ""))
        self.image.set_pixel_size(size)
        self.dot = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.dot.add_css_class("nova-running-dot")
        pos = dock.position
        if pos == "right":
            self.append(self.dot)
            self.append(self.image)
            self.set_valign(Gtk.Align.CENTER)
            self.set_halign(Gtk.Align.END)
        elif pos == "left":
            self.append(self.image)
            self.append(self.dot)
            self.set_valign(Gtk.Align.CENTER)
            self.set_halign(Gtk.Align.START)
        else:
            self.append(self.image)
            self.append(self.dot)
            self.set_valign(Gtk.Align.END)
            self.set_halign(Gtk.Align.CENTER)

        click = Gtk.GestureClick(button=0)
        click.connect("released", self._on_click)
        self.add_controller(click)
        if slot.kind in ("pinned", "running", "recent") and slot.record is not None:
            src = Gtk.DragSource(actions=Gdk.DragAction.MOVE | Gdk.DragAction.COPY)
            src.connect("prepare", lambda *_: Gdk.ContentProvider.new_for_value(f"app:{slot.record.id}"))
            src.connect("drag-begin", lambda s, _d: s.set_icon(Gtk.WidgetPaintable.new(self.image), size // 2, size // 2))
            self.add_controller(src)
        tgt = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE | Gdk.DragAction.COPY)
        tgt.connect("drop", lambda _t, value, x, y: dock.on_drop(self, value, x, y))
        tgt.connect("motion", lambda *_: (self.add_css_class("nova-drop-before"), Gdk.DragAction.COPY)[1])
        tgt.connect("leave", lambda *_: self.remove_css_class("nova-drop-before"))
        self.add_controller(tgt)

    @property
    def title(self) -> str:
        if self.slot.kind == "launchpad":
            return "All Applications"
        return "Trash" if self.slot.kind == "trash" else (self.slot.record.name if self.slot.record else "")

    def set_scale(self, scale: float, base: int) -> None:
        if abs(scale - self.scale) < 0.004:
            return
        self.scale = scale
        self.image.set_pixel_size(int(round(base * scale)))

    def set_running(self, running: bool) -> None:
        (self.dot.add_css_class if running else self.dot.remove_css_class)("running")

    def bounce(self) -> None:
        self.add_css_class("launching")
        GLib.timeout_add(1900, lambda: (self.remove_css_class("launching"), GLib.SOURCE_REMOVE)[1])

    def _on_click(self, gesture, _n, _x, _y) -> None:
        button = gesture.get_current_button()
        if button == Gdk.BUTTON_SECONDARY:
            self.dock.show_menu(self)
        elif button == Gdk.BUTTON_MIDDLE:
            self.dock.activate(self, new_instance=True, time=gesture.get_current_event_time())
        elif button == Gdk.BUTTON_PRIMARY:
            self.dock.activate(self, time=gesture.get_current_event_time())


class DockWindow(Gtk.Window):
    def __init__(self, app: "NovaApplication"):
        super().__init__(application=app, title="Nova Dock")
        self.app = app
        self.set_decorated(False)
        self.set_resizable(False)
        self.add_css_class("nova-transparent")
        self.items: list[DockItem] = []
        self.running: dict[str, list[int]] = {}
        self.pointer: float | None = None
        self.hovered = False
        self.hidden = False
        self._tick = 0
        self._hide_timer = 0
        self._tracking = False
        self._trash_full = False
        self._xid = 0
        self._size = (1, 1)

        self.overlay = Gtk.Overlay()
        self.set_child(self.overlay)
        self.revealer = Gtk.Revealer(transition_duration=220)
        self.overlay.set_child(self.revealer)
        self.bar = GlassBox(app, radius=22)
        self.bar.add_css_class("nova-dock-bar")
        self.revealer.set_child(self.bar)

        self.tooltip_layer = Gtk.Fixed(can_target=False)
        self.tooltip = Gtk.Label()
        self.tooltip.add_css_class("nova-dock-tooltip")
        self.tooltip.set_opacity(0)
        self.tooltip_layer.put(self.tooltip, 0, 0)
        self.overlay.add_overlay(self.tooltip_layer)

        motion = Gtk.EventControllerMotion()
        motion.connect("enter", self._on_motion)
        motion.connect("motion", self._on_motion)
        motion.connect("leave", self._on_leave)
        self.add_controller(motion)

        self.connect("realize", self._on_realize)
        self.connect("map", lambda *_: GLib.idle_add(self._place))
        self.connect("close-request", lambda *_: True)  # never closed by Alt+F4

        app.settings.connect(self._on_setting)
        app.index.connect(self.rebuild)
        display = Gdk.Display.get_default()
        display.get_monitors().connect("items-changed", lambda *_: GLib.timeout_add(500, self._place))
        self._trash_monitor = None
        try:
            self._trash_monitor = Gio.File.new_for_uri("trash:///").monitor(Gio.FileMonitorFlags.NONE, None)
            self._trash_monitor.connect("changed", lambda *_: self._query_trash())
        except GLib.Error:
            pass

    # -- configuration -------------------------------------------------------------
    @property
    def position(self) -> str:
        return self.app.settings["dock_position"]

    @property
    def vertical(self) -> bool:
        return self.position in ("left", "right")

    @property
    def base(self) -> int:
        return self.app.settings["dock_icon_size"]

    @property
    def mag(self) -> float:
        if not self.app.animations_enabled():
            return 1.0
        return self.app.settings["dock_magnification"]

    def _on_setting(self, key: str) -> None:
        if key.startswith("dock_") or key in ("pinned_apps", "animation_level", "history_enabled"):
            if key == "dock_enabled":
                self.app.update_dock_visibility()
                return
            self.rebuild()

    # -- X11 setup -----------------------------------------------------------------
    def _on_realize(self, *_):
        self._xid = self.app.xid_of(self)
        if self._xid:
            x11.make_dock(self._xid)
            if not self._tracking:
                self.app.windows.connect(self._on_windows_changed)
                self._tracking = True

    def _place(self) -> bool:
        mon = self.app.target_monitor_info(self.app.settings["dock_monitor"])
        if mon is None or not self._xid:
            return GLib.SOURCE_REMOVE
        w, h = self._size
        s = self.app.settings
        reserve = s["dock_reserve_space"] and not s["dock_autohide"]
        # Auto-hide and space-reserving docks sit on the screen edge (the work
        # area would already exclude our own strut).  Otherwise respect other
        # panels such as Ubuntu Dock through the monitor work area.
        area = mon.geometry if (s["dock_autohide"] or reserve) else mon.workarea
        x, y = dock_origin(self.position, area, w, h)
        self.app.move_window(self, x, y, mon)
        thickness = self.base + 2 * BAR_PAD + EDGE_MARGIN + 6 if reserve else 0
        mons = self.app.all_monitor_infos()
        root = (max(m.geometry.x + m.geometry.width for m in mons),
                max(m.geometry.y + m.geometry.height for m in mons)) if mons else (0, 0)
        g = mon.geometry
        x11.set_strut(self._xid, x11.strut_values(self.position, thickness, (g.x, g.y, g.width, g.height), root))
        self._update_input_region()
        return GLib.SOURCE_REMOVE

    # -- building ------------------------------------------------------------------
    def slots(self) -> list[Slot]:
        s = self.app.settings
        idx = self.app.index
        pinned = [idx.get(i) for i in s["pinned_apps"]]
        pinned = [r for r in pinned if r is not None]
        # "All Applications" comes first, like Launchpad next to Finder.
        out = [Slot("launchpad")] + [Slot("pinned", r) for r in pinned]
        shown = {r.id for r in pinned}
        extra: list[Slot] = []
        if s["dock_show_running"]:
            for app_id in self.running:
                r = idx.get(app_id)
                if r and r.id not in shown:
                    extra.append(Slot("running", r))
                    shown.add(r.id)
        if s["dock_show_recent"] and s["history_enabled"]:
            n = 0
            for app_id in idx.usage.recent(20):
                if n >= s["dock_recent_count"]:
                    break
                r = idx.get(app_id)
                if r and r.id not in shown:
                    extra.append(Slot("recent", r))
                    shown.add(r.id)
                    n += 1
        if extra:
            out.append(Slot("separator"))
            out.extend(extra)
        out.append(Slot("separator"))
        out.append(Slot("trash"))
        return out

    def rebuild(self) -> None:
        if not self.app.settings["dock_enabled"]:
            return
        for c in ("nova-dock-bottom", "nova-dock-left", "nova-dock-right"):
            self.remove_css_class(c)
        self.add_css_class(f"nova-dock-{self.position}")
        while (child := self.bar.get_first_child()) is not None:
            self.bar.remove(child)
        self.items = []
        vertical = self.vertical
        self.bar.set_orientation(Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL)
        self.bar.set_spacing(self.app.settings["dock_spacing"])
        align = {"bottom": (Gtk.Align.CENTER, Gtk.Align.END), "left": (Gtk.Align.START, Gtk.Align.CENTER),
                 "right": (Gtk.Align.END, Gtk.Align.CENTER)}[self.position]
        self.bar.set_halign(align[0])
        self.bar.set_valign(align[1])
        self.revealer.set_halign(align[0])
        self.revealer.set_valign(align[1])
        self.revealer.set_transition_type({
            "bottom": Gtk.RevealerTransitionType.SLIDE_UP, "left": Gtk.RevealerTransitionType.SLIDE_RIGHT,
            "right": Gtk.RevealerTransitionType.SLIDE_LEFT}[self.position])
        m = EDGE_MARGIN
        self.revealer.set_margin_bottom(m if self.position == "bottom" else 0)
        self.revealer.set_margin_start(m if self.position == "left" else 0)
        self.revealer.set_margin_end(m if self.position == "right" else 0)
        self.slot_list = self.slots()
        for slot in self.slot_list:
            if slot.kind == "separator":
                sep = Gtk.Box()
                sep.add_css_class("nova-dock-separator")
                if vertical:
                    sep.set_size_request(self.base, 1)
                else:
                    sep.set_size_request(1, self.base)
                sep.set_valign(Gtk.Align.END if not vertical else Gtk.Align.CENTER)
                self.bar.append(sep)
                continue
            item = DockItem(self, slot, self.base, vertical)
            item.set_running(slot.record is not None and slot.record.id in self.running)
            if slot.kind == "trash":
                item.image.set_from_gicon(gicon_for("user-trash-full" if self._trash_full else "user-trash"))
            self.items.append(item)
            self.bar.append(item)
        self._resize()
        self._query_trash()

    def _slot_extents(self) -> list[tuple[float, float]]:
        """(start, size) of every slot along the dock axis, unmagnified."""
        sp = self.app.settings["dock_spacing"]
        out = []
        pos = 0.0
        for slot in self.slot_list:
            size = (SEP_SIZE - 4) if slot.kind == "separator" else self.base
            out.append((pos, size))
            pos += size + sp
        return out

    def _axis_length(self) -> float:
        ext = self._slot_extents()
        return (ext[-1][0] + ext[-1][1] if ext else 0) + 2 * BAR_PAD

    def _resize(self) -> None:
        mag = self.app.settings["dock_magnification"]
        length = self._axis_length() + self.base * (mag - 1.0) * 2.6 + 40
        thickness = int(self.base * max(mag, 1.0) + 2 * BAR_PAD + 6 + EDGE_MARGIN + TOOLTIP_SPACE)
        w, h = (thickness, int(length)) if self.vertical else (int(length), thickness)
        self._size = (w, h)
        self.set_size_request(w, h)
        self.set_default_size(w, h)
        if self.get_mapped():
            GLib.idle_add(self._place)

    # -- magnification ---------------------------------------------------------------
    def _slot_centers(self) -> list[float]:
        """Item centers in window coordinates along the axis (unmagnified)."""
        w, h = self._size
        total = (h if self.vertical else w)
        start = (total - self._axis_length()) / 2 + BAR_PAD
        return [start + s + size / 2 for (s, size), slot in zip(self._slot_extents(), self.slot_list)
                if slot.kind != "separator"]

    def _on_motion(self, _ctrl, x, y) -> None:
        if not self.hovered:
            self.hovered = True
            self._cancel_hide()
            if self.hidden:
                self.reveal(True)
            self._update_input_region()
        self.pointer = y if self.vertical else x
        self._kick()
        self._update_tooltip()

    def _on_leave(self, *_):
        self.hovered = False
        self.pointer = None
        self._kick()
        self.tooltip.set_opacity(0)
        self._update_input_region()
        if self.app.settings["dock_autohide"]:
            self._schedule_hide()

    def _kick(self) -> None:
        if not self._tick:
            self._tick = self.add_tick_callback(self._on_tick)

    def _on_tick(self, _w, _clock) -> bool:
        targets = magnify(self._slot_centers(), self.pointer, self.base + self.app.settings["dock_spacing"],
                          self.mag)
        settled = True
        for item, t in zip(self.items, targets):
            cur = item.scale
            nxt = cur + (t - cur) * 0.38
            if abs(nxt - t) < 0.01:
                nxt = t
            else:
                settled = False
            item.set_scale(nxt, self.base)
        if settled:
            self._tick = 0
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _hovered_item(self) -> DockItem | None:
        if self.pointer is None:
            return None
        centers = self._slot_centers()
        unit = self.base + self.app.settings["dock_spacing"]
        best = min(range(len(centers)), key=lambda i: abs(centers[i] - self.pointer), default=None)
        if best is None or abs(centers[best] - self.pointer) > unit * 0.6:
            return None
        return self.items[best]

    def _update_tooltip(self) -> None:
        item = self._hovered_item()
        if item is None:
            self.tooltip.set_opacity(0)
            return
        self.tooltip.set_text(item.title)
        _, nat = self.tooltip.get_preferred_size()
        center = self._slot_centers()[self.items.index(item)]
        w, h = self._size
        top = self.base * self.mag + 2 * BAR_PAD + EDGE_MARGIN + 10
        if self.position == "bottom":
            x, y = center - nat.width / 2, h - top - nat.height
        elif self.position == "left":
            x, y = top, center - nat.height / 2
        else:
            x, y = w - top - nat.width, center - nat.height / 2
        self.tooltip_layer.move(self.tooltip, max(0, x), max(0, y))
        self.tooltip.set_opacity(1)

    # -- input region / auto-hide ------------------------------------------------------
    def _update_input_region(self) -> None:
        surface = self.get_surface()
        if surface is None:
            return
        w, h = self._size
        if self.hidden:
            thick = 2
        elif self.hovered:
            surface.set_input_region(cairo.Region(cairo.RectangleInt(0, 0, w, h)))
            return
        else:
            thick = self.base + 2 * BAR_PAD + EDGE_MARGIN + 6
        length = int(self._axis_length())
        if self.position == "bottom":
            rect = cairo.RectangleInt((w - length) // 2, h - thick, length, thick)
        elif self.position == "left":
            rect = cairo.RectangleInt(0, (h - length) // 2, thick, length)
        else:
            rect = cairo.RectangleInt(w - thick, (h - length) // 2, thick, length)
        surface.set_input_region(cairo.Region(rect))

    def reveal(self, show: bool) -> None:
        self.hidden = not show
        self.revealer.set_transition_duration(220 if self.app.animations_enabled() else 0)
        self.revealer.set_reveal_child(show)
        self._update_input_region()

    def _schedule_hide(self) -> None:
        self._cancel_hide()
        self._hide_timer = GLib.timeout_add(700, self._do_hide)

    def _cancel_hide(self) -> None:
        if self._hide_timer:
            GLib.source_remove(self._hide_timer)
            self._hide_timer = 0

    def _do_hide(self) -> bool:
        self._hide_timer = 0
        if not self.hovered:
            self.reveal(False)
        return GLib.SOURCE_REMOVE

    def show_dock(self) -> None:
        self.rebuild()
        # Map without requesting focus: a dock must never steal the keyboard.
        self.set_focusable(False)
        self.set_visible(True)
        self.reveal(True)
        if self.app.settings["dock_autohide"]:
            self._schedule_hide()
        self.app.windows.schedule_refresh()

    # -- running windows -----------------------------------------------------------------
    def _on_windows_changed(self) -> None:
        running = self.app.windows.running_ids()
        changed_set = set(running) != set(self.running)
        self.running = running
        if changed_set and self.app.settings["dock_show_running"]:
            self.rebuild()
        else:
            for item in self.items:
                item.set_running(item.slot.record is not None and item.slot.record.id in running)

    # -- actions ---------------------------------------------------------------------
    def activate(self, item: DockItem, new_instance: bool = False, time: int = 0) -> None:
        if item.slot.kind == "trash":
            self.app.open_trash()
            return
        if item.slot.kind == "launchpad":
            self.app.grid.toggle()
            return
        record = item.slot.record
        if record is None:
            return
        windows = self.running.get(record.id, [])
        if windows and not new_instance:
            x11.activate(next_window(windows, x11.active_window()), time)
            return
        res = self.app.launch_app(record, source=self)
        if res.ok:
            item.bounce()

    def show_menu(self, item: DockItem) -> None:
        if item.slot.kind == "trash":
            popup_menu(item, [("Open Trash", self.app.open_trash)])
            return
        if item.slot.kind == "launchpad":
            popup_menu(item, [("All Applications", self.app.grid.open),
                              ("Quick Search", self.app.quick.open),
                              None,
                              ("Nova Settings", self.app.show_settings)])
            return
        record = item.slot.record
        extra = []
        windows = self.running.get(record.id, [])
        if windows:
            extra.append(("Quit", lambda: [x11.close_window(w) for w in windows]))
        self.app.show_app_menu(item, record, extra=extra)

    def on_drop(self, target: DockItem, value, _x, _y) -> bool:
        target.remove_css_class("nova-drop-before")
        if not isinstance(value, str) or not value.startswith("app:"):
            return False
        app_id = value[4:]
        pinned = self.app.settings["pinned_apps"]
        if app_id in pinned:
            pinned.remove(app_id)
        if target.slot.kind == "pinned" and target.slot.record is not None and target.slot.record.id in pinned:
            pinned.insert(pinned.index(target.slot.record.id), app_id)
        else:
            pinned.append(app_id)
        self.app.settings.set("pinned_apps", pinned)
        return True

    def _query_trash(self) -> None:
        f = Gio.File.new_for_uri("trash:///")

        def done(src, res):
            try:
                info = src.query_info_finish(res)
                full = info.get_attribute_uint32("trash::item-count") > 0
            except GLib.Error:
                return
            if full != self._trash_full:
                self._trash_full = full
                for item in self.items:
                    if item.slot.kind == "trash":
                        item.image.set_from_gicon(gicon_for("user-trash-full" if full else "user-trash"))

        f.query_info_async("trash::item-count", Gio.FileQueryInfoFlags.NONE, GLib.PRIORITY_LOW, None, done)
