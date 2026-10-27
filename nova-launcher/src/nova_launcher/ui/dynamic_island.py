"""Dynamic Island: a black pill at the top of the screen showing running apps.

States (one Gtk.Stack page each; the stack interpolates its size, so the
pill morphs smoothly between them):
* idle      no apps running: a small empty pill
* compact   focused app's icon and name, plus the next few apps' icons
* toast     brief "App opened" notice when a new app starts
* expanded  on hover or click: every running app as a tile; click to switch
            (click again to cycle that app's windows), right-click for
            per-window switching, New Window and Quit.

Scrolling over the compact pill switches to the next/previous app.  Like the
dock, the window is fixed-size and transparent with the input region limited
to the pill, so it never blocks clicks next to it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GLib, Graphene, Gsk, Gtk, Pango  # noqa: E402

from ..core.running_apps import RunningApp, next_window  # noqa: E402
from ..system import x11  # noqa: E402
from .dock import ALL_APPS_ICON  # noqa: E402
from .widgets import gicon_for, popup_menu, record_gicon  # noqa: E402

if TYPE_CHECKING:
    from ..application import NovaApplication

log = logging.getLogger("nova.island")

WIDTH, HEIGHT = 700, 440  # fixed window: room for the largest pill + shadow
TOP_GAP = 5  # between the GNOME top bar and the pill
COMPACT_H = 36
COMPACT_W = 280
IDLE_W = 132
TILE_W = 92
PER_ROW = 6
MAX_ROWS = 3
PEEK_ICONS = 3
HOVER_OPEN_MS = 140
HOVER_CLOSE_MS = 320
TOAST_MS = 1800


def island_width(n_apps: int) -> int:
    """Expanded pill width for ``n_apps`` tiles (pure; unit-tested)."""
    return max(COMPACT_W + 60, min(max(n_apps, 1), PER_ROW) * TILE_W + 36)


def reserve_thickness(panel_h: int) -> int:
    """Top strut, measured from the monitor top, that keeps windows below the pill."""
    return panel_h + TOP_GAP + COMPACT_H + TOP_GAP


def _rgba(r, g, b, a) -> Gdk.RGBA:
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = r, g, b, a
    return c


class IslandPill(Gtk.Box):
    """Paints the pill: black, fully rounded, soft shadow, faint rim."""

    def do_snapshot(self, snap: Gtk.Snapshot) -> None:
        ok, box = self.compute_bounds(self)
        if not ok or box.size.width <= 0 or box.size.height <= 0:
            return
        rect = Graphene.Rect().init(box.origin.x, box.origin.y, box.size.width, box.size.height)
        outline = Gsk.RoundedRect()
        outline.init_from_rect(rect, min(box.size.height / 2, 28))
        snap.append_outset_shadow(outline, _rgba(0, 0, 0, 0.38), 0, 8, 0, 22)
        snap.push_rounded_clip(outline)
        snap.append_color(_rgba(0, 0, 0, 1), rect)
        snap.pop()
        snap.append_border(outline, [1, 1, 1, 1], [_rgba(1, 1, 1, 0.08)] * 4)
        Gtk.Box.do_snapshot(self, snap)


class IslandTile(Gtk.Box):
    def __init__(self, island: "DynamicIsland", app: RunningApp):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        self.island = island
        self.running = app
        self.add_css_class("nova-island-tile")
        if app.active:
            self.add_css_class("active")
        self.set_size_request(TILE_W - 8, -1)

        icon = Gtk.Overlay(halign=Gtk.Align.CENTER)
        icon.set_child(_app_image(app, 46))
        if len(app.windows) > 1:
            badge = Gtk.Label(label=str(len(app.windows)), halign=Gtk.Align.END, valign=Gtk.Align.START)
            badge.add_css_class("nova-island-badge")
            icon.add_overlay(badge)
        self.append(icon)
        name = Gtk.Label(label=app.name, max_width_chars=10, ellipsize=Pango.EllipsizeMode.END)
        name.add_css_class("nova-island-tile-name")
        self.append(name)
        dot = Gtk.Box(halign=Gtk.Align.CENTER)
        dot.add_css_class("nova-island-dot")
        self.append(dot)

        click = Gtk.GestureClick(button=0)
        click.connect("released", self._on_click)
        self.add_controller(click)
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_: island.show_detail(app))
        self.add_controller(motion)

    def _on_click(self, gesture, _n, _x, _y) -> None:
        button = gesture.get_current_button()
        if button == Gdk.BUTTON_SECONDARY:
            self.island.show_menu(self, self.running)
        elif button == Gdk.BUTTON_PRIMARY:
            self.island.switch_to(self.running, gesture.get_current_event_time())


def _app_image(app: RunningApp, size: int) -> Gtk.Image:
    gicon = record_gicon(app.record) if app.record is not None else gicon_for(app.icon)
    img = Gtk.Image.new_from_gicon(gicon)
    img.set_pixel_size(size)
    return img


class DynamicIsland(Gtk.Window):
    def __init__(self, app: "NovaApplication"):
        super().__init__(application=app, title="Nova Dynamic Island")
        self.app = app
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_size_request(WIDTH, HEIGHT)
        self.set_default_size(WIDTH, HEIGHT)
        self.add_css_class("nova-transparent")
        self.add_css_class("nova-island-window")
        self.state = "idle"
        self.apps: list[RunningApp] = []
        self._known: set[str] | None = None  # app keys seen so far (None until first refresh)
        self._xid = 0
        self._panel_h: int | None = None
        self._open_timer = 0
        self._close_timer = 0
        self._toast_timer = 0
        self._tick = 0
        self._menu_open = False
        self._tracking = False

        self.pill = IslandPill(halign=Gtk.Align.CENTER, valign=Gtk.Align.START)
        self.pill.add_css_class("nova-island")
        self.stack = Gtk.Stack(interpolate_size=True, hhomogeneous=False, vhomogeneous=False,
                               transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.connect("notify::transition-running", lambda *_: self._sync_region())
        self.pill.append(self.stack)
        self.set_child(self.pill)

        idle = Gtk.Box()
        idle.set_size_request(IDLE_W, COMPACT_H - 4)
        self.stack.add_named(idle, "idle")

        self.compact = Gtk.Box(spacing=8)
        self.compact.add_css_class("nova-island-compact")
        self.compact.set_size_request(COMPACT_W, COMPACT_H)
        self.stack.add_named(self.compact, "compact")

        self.toast = Gtk.Box(spacing=10)
        self.toast.add_css_class("nova-island-compact")
        self.toast.set_size_request(COMPACT_W + 40, COMPACT_H + 12)
        self.stack.add_named(self.toast, "toast")

        self.expanded = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.expanded.add_css_class("nova-island-expanded")
        header = Gtk.Box(spacing=8)
        self.header_label = Gtk.Label(xalign=0, hexpand=True)
        self.header_label.add_css_class("nova-island-header")
        header.append(self.header_label)
        grid_btn = Gtk.Button(tooltip_text="All Applications")
        grid_btn.add_css_class("nova-island-button")
        grid_img = Gtk.Image.new_from_gicon(gicon_for(ALL_APPS_ICON))
        grid_img.set_pixel_size(20)
        grid_btn.set_child(grid_img)
        grid_btn.connect("clicked", lambda *_: (self.collapse(), self.app.grid.toggle()))
        header.append(grid_btn)
        self.expanded.append(header)
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                max_children_per_line=PER_ROW, row_spacing=6, column_spacing=0)
        self.flow.add_css_class("nova-island-flow")
        self.scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                           propagate_natural_height=True, max_content_height=MAX_ROWS * 108)
        self.scroller.set_child(self.flow)
        self.expanded.append(self.scroller)
        self.detail = Gtk.Label(xalign=0.5, ellipsize=Pango.EllipsizeMode.MIDDLE, max_width_chars=60)
        self.detail.add_css_class("nova-island-detail")
        self.expanded.append(self.detail)
        self.stack.add_named(self.expanded, "expanded")

        self._motion = Gtk.EventControllerMotion()
        self._motion.connect("enter", lambda *_: self._on_enter())
        self._motion.connect("leave", lambda *_: self._on_leave())
        self.pill.add_controller(self._motion)
        # On press, so the release that switches apps from a tile cannot re-open it.
        click = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
        click.connect("pressed", lambda *_: self.expand() if self.state != "expanded" else None)
        self.pill.add_controller(click)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL
                                           | Gtk.EventControllerScrollFlags.DISCRETE)
        scroll.connect("scroll", self._on_scroll)
        self.pill.add_controller(scroll)

        self.connect("realize", self._on_realize)
        self.connect("map", lambda *_: GLib.idle_add(self._place))
        self.connect("close-request", lambda *_: True)  # never closed by Alt+F4
        app.settings.connect(self._on_setting)
        Gdk.Display.get_default().get_monitors().connect("items-changed",
                                                         lambda *_: GLib.timeout_add(500, self._place))

    # -- X11 setup ---------------------------------------------------------------------
    def _on_setting(self, key: str) -> None:
        if key in ("island_reserve_space", "island_monitor"):
            self._place()
        elif key == "animation_level":
            self._set_page(self.state)

    def _on_realize(self, *_):
        self._xid = self.app.xid_of(self)
        if self._xid:
            x11.make_dock(self._xid)
        if not self._tracking:
            self.app.windows.connect(self._on_windows_changed)
            self._tracking = True

    def _place(self) -> bool:
        mon = self.app.target_monitor_info(self.app.settings["island_monitor"])
        if mon is None or not self._xid:
            return GLib.SOURCE_REMOVE
        g, wa = mon.geometry, mon.workarea
        reserve = self.app.settings["island_reserve_space"]
        if self._panel_h is None:
            # Measured before our own strut exists; afterwards the work area
            # would include it.  The GNOME top bar is ~32 px.
            self._panel_h = max(0, min(wa.y - g.y, 64))
        x = g.x + (g.width - WIDTH) // 2
        self.app.move_window(self, x, g.y + self._panel_h + TOP_GAP, mon)
        mons = self.app.all_monitor_infos()
        root = (max(m.geometry.x + m.geometry.width for m in mons),
                max(m.geometry.y + m.geometry.height for m in mons)) if mons else (0, 0)
        thickness = reserve_thickness(self._panel_h) if reserve else 0
        x11.set_strut(self._xid, x11.strut_values("top", thickness, (g.x, g.y, g.width, g.height), root))
        self._sync_region()
        return GLib.SOURCE_REMOVE

    def show_island(self) -> None:
        self.set_focusable(False)
        self.set_visible(True)
        self._render()

    # -- data --------------------------------------------------------------------------
    def _on_windows_changed(self) -> None:
        apps = self.app.windows.apps
        keys = {a.key for a in apps}
        opened = [a for a in apps if self._known is not None and a.key not in self._known]
        self._known = keys
        self.apps = apps
        if opened and self.state != "expanded" and self.app.animations_enabled():
            self._show_toast(opened[0])
        else:
            self._render()

    def _render(self) -> None:
        """Rebuild the current page's contents and pick the resting page."""
        self._fill_compact()
        if self.state == "expanded":
            self._fill_expanded()
            return
        if self.state != "toast":
            self._set_page("compact" if self.apps else "idle")

    def rebuild(self) -> None:  # icon theme changed
        self._render()

    def _fill_compact(self) -> None:
        _clear(self.compact)
        if not self.apps:
            return
        front = self.apps[0]
        img = _app_image(front, 22)
        img.set_margin_start(4)
        self.compact.append(img)
        name = Gtk.Label(label=front.name, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        name.add_css_class("nova-island-title")
        self.compact.append(name)
        peek = Gtk.Box(spacing=3)
        for other in self.apps[1:1 + PEEK_ICONS]:
            peek.append(_app_image(other, 18))
        more = len(self.apps) - 1 - PEEK_ICONS
        if more > 0:
            lbl = Gtk.Label(label=f"+{more}")
            lbl.add_css_class("nova-island-more")
            peek.append(lbl)
        self.compact.append(peek)

    def _fill_expanded(self) -> None:
        n = len(self.apps)
        self.header_label.set_text(f"{n} app{'s' if n != 1 else ''} running" if n else "No apps running")
        self.flow.remove_all()
        for a in self.apps:
            self.flow.append(IslandTile(self, a))
        self.flow.set_min_children_per_line(min(max(n, 1), PER_ROW))
        self.expanded.set_size_request(island_width(n), -1)
        self.show_detail(self.apps[0] if self.apps else None)

    def show_detail(self, app: RunningApp | None) -> None:
        if app is None:
            self.detail.set_text("Open an app and it shows up here")
            return
        title = next((t for t in app.titles if t), "")
        extra = f"  ·  {len(app.windows)} windows" if len(app.windows) > 1 else ""
        self.detail.set_text(f"{app.name}{' — ' + title if title and title != app.name else ''}{extra}")

    # -- states ------------------------------------------------------------------------
    def _set_page(self, page: str) -> None:
        self.state = page
        anim = self.app.animations_enabled()
        self.stack.set_transition_duration(260 if anim else 0)
        if self.stack.get_visible_child_name() != page:
            self.stack.set_visible_child_name(page)
        self._sync_region()

    def expand(self) -> None:
        self._cancel(("_open_timer", "_toast_timer"))
        self.app.windows.refresh()  # fresh window titles
        self.state = "expanded"
        self._fill_expanded()
        self._set_page("expanded")

    def collapse(self) -> None:
        self._cancel(("_open_timer", "_close_timer"))
        self.state = "compact"
        self._render()

    def _show_toast(self, app: RunningApp) -> None:
        _clear(self.toast)
        img = _app_image(app, 28)
        img.set_margin_start(6)
        self.toast.append(img)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        name = Gtk.Label(label=app.name, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        name.add_css_class("nova-island-title")
        texts.append(name)
        sub = Gtk.Label(label="Opened", xalign=0)
        sub.add_css_class("nova-island-sub")
        texts.append(sub)
        self.toast.append(texts)
        self._fill_compact()
        self._set_page("toast")
        self._cancel(("_toast_timer",))
        self._toast_timer = GLib.timeout_add(TOAST_MS, self._end_toast)

    def _end_toast(self) -> bool:
        self._toast_timer = 0
        if self.state == "toast":
            self.state = "compact"
            self._render()
        return GLib.SOURCE_REMOVE

    def _cancel(self, names) -> None:
        for n in names:
            if getattr(self, n):
                GLib.source_remove(getattr(self, n))
                setattr(self, n, 0)

    def _on_enter(self) -> None:
        self._cancel(("_close_timer",))
        if self.state != "expanded" and not self._open_timer:
            self._open_timer = GLib.timeout_add(HOVER_OPEN_MS, self._hover_open)

    def _hover_open(self) -> bool:
        self._open_timer = 0
        self.expand()
        return GLib.SOURCE_REMOVE

    def _on_leave(self) -> None:
        self._cancel(("_open_timer",))
        if self.state == "expanded" and not self._menu_open:
            self._cancel(("_close_timer",))
            self._close_timer = GLib.timeout_add(HOVER_CLOSE_MS, self._hover_close)

    def _hover_close(self) -> bool:
        self._close_timer = 0
        if not self._motion.contains_pointer():
            self.collapse()
        return GLib.SOURCE_REMOVE

    def _on_scroll(self, _ctrl, _dx, dy) -> bool:
        if self.state == "expanded" or len(self.apps) < 2:
            return False
        target = self.apps[1] if dy > 0 else self.apps[-1]
        self.switch_to(target, 0)
        return True

    # -- actions -----------------------------------------------------------------------
    def switch_to(self, app: RunningApp, time: int) -> None:
        target = next_window(app.windows, x11.active_window())
        if target:
            x11.activate(target, time)
        if self.state == "expanded":
            self.collapse()

    def show_menu(self, tile: Gtk.Widget, app: RunningApp) -> None:
        entries: list = []
        for xid, title in zip(app.windows, app.titles):
            entries.append((_short(title or app.name), lambda w=xid: x11.activate(w, 0)))
        entries.append(None)
        if app.record is not None:
            entries.append(("New Window", lambda: self.app.launch_app(app.record)))
        n = len(app.windows)
        entries.append((f"Quit ({n} windows)" if n > 1 else "Quit",
                        lambda: [x11.close_window(w) for w in app.windows]))
        self._menu_open = True
        pop = popup_menu(tile, entries)
        pop.connect("closed", lambda *_: self._menu_closed())

    def _menu_closed(self) -> None:
        self._menu_open = False
        self._close_timer = GLib.timeout_add(HOVER_CLOSE_MS, self._hover_close)

    # -- input region ------------------------------------------------------------------
    def _sync_region(self) -> None:
        """Clicks pass through everywhere except the pill (followed every frame
        while the pill is changing size)."""
        if self.stack.get_transition_running() and not self._tick:
            self._tick = self.add_tick_callback(self._on_tick)
        self._update_region()

    def _on_tick(self, *_):
        self._update_region()
        if self.stack.get_transition_running():
            return GLib.SOURCE_CONTINUE
        self._tick = 0
        return GLib.SOURCE_REMOVE

    def _update_region(self) -> None:
        surface = self.get_surface()
        if surface is None:
            return
        ok, b = self.pill.compute_bounds(self)
        if not ok or b.size.width < 1:
            # Not laid out yet: use the size the pill is about to take.
            w, h = IDLE_W, COMPACT_H
            rect = cairo.RectangleInt((WIDTH - w) // 2, 0, w, h)
        else:
            rect = cairo.RectangleInt(int(b.origin.x), int(b.origin.y),
                                      int(b.size.width + 0.5), int(b.size.height + 0.5))
        surface.set_input_region(cairo.Region(rect))


def _clear(box: Gtk.Box) -> None:
    while (child := box.get_first_child()) is not None:
        box.remove(child)


def _short(title: str, n: int = 48) -> str:
    return title if len(title) <= n else title[: n - 1] + "…"
