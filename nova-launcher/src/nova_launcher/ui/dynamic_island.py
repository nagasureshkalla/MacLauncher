"""Dynamic Island: a black pill at the top of the screen showing running apps.

States (one Gtk.Stack page each; the stack interpolates its size, so the
pill morphs smoothly between them):
* idle      no apps running: a small empty pill
* compact   focused app's icon and name, plus the next few apps' icons
* toast     brief "App opened" notice when a new app starts
* expanded  on hover or click: every running app as a tile; click to switch
            one tile per open window (three terminals = three tiles, each
            labelled with its window title); click to switch, right-click
            for Close Window, New Window and Quit.  Above each icon: the
            app's live CPU, GPU, RAM and VRAM use as 'CPU: 01%' (whole process tree),
            sampled in the background only while expanded.

Scrolling over the compact pill switches to the next/previous app.

Auto-hide (default): the pill slides up out of sight and windows get the
full screen height; an invisible strip right under the GNOME top bar brings
it back when the pointer touches it.  Like the
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
from ..system.process_stats import Usage, UsageSampler, format_percent  # noqa: E402
from .dock import ALL_APPS_ICON  # noqa: E402
from .widgets import gicon_for, popup_menu, record_gicon  # noqa: E402

if TYPE_CHECKING:
    from ..application import NovaApplication

log = logging.getLogger("nova.island")

WIDTH, HEIGHT = 700, 560  # fixed window: room for the largest pill + shadow
TOP_GAP = 5  # between the GNOME top bar and the pill
COMPACT_H = 36
COMPACT_W = 280
IDLE_W = 132
TILE_W = 100
TILE_H = 140  # usage readout, icon, two-line title
PER_ROW = 6
MAX_ROWS = 3
PEEK_ICONS = 3
HOVER_OPEN_MS = 140
HOVER_CLOSE_MS = 320
TOAST_MS = 1800
HOT_W, HOT_H = COMPACT_W + 80, 5  # invisible reveal strip under the top bar
HIDE_MS = 600


def island_width(n_apps: int) -> int:
    """Expanded pill width for ``n_apps`` tiles (pure; unit-tested)."""
    return max(COMPACT_W + 60, min(max(n_apps, 1), PER_ROW) * TILE_W + 36)


def island_header(apps: list[RunningApp]) -> str:
    """'3 windows · 2 apps' (pure; unit-tested)."""
    if not apps:
        return "No apps running"
    w, a = sum(len(x.windows) for x in apps), len(apps)
    return f"{w} window{'s' if w != 1 else ''} · {a} app{'s' if a != 1 else ''}"


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
    """One open window: app icon, the window's own title underneath."""

    def __init__(self, island: "DynamicIsland", app: RunningApp, xid: int, title: str, active: bool):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        self.island = island
        self.running = app
        self.xid = xid
        self.title = title or app.name
        self.add_css_class("nova-island-tile")
        if active:
            self.add_css_class("active")
        self.set_size_request(TILE_W - 8, -1)
        self.set_valign(Gtk.Align.START)
        self.pid = x11.window_pid(xid)

        usage = Gtk.Grid(column_homogeneous=True, column_spacing=4, halign=Gtk.Align.FILL)
        usage.add_css_class("nova-island-usage")
        self._usage: dict[str, Gtk.Label] = {}
        for i, key in enumerate(("CPU", "GPU", "RAM", "VRAM")):
            lbl = Gtk.Label(xalign=0.5)
            self._usage[key] = lbl
            usage.attach(lbl, i % 2, i // 2, 1, 1)
        self.append(usage)
        self.set_usage(None)

        img = _app_image(app, 46)
        img.set_halign(Gtk.Align.CENTER)
        self.append(img)
        # Two short lines, so windows of the same app (three terminals) differ.
        name = Gtk.Label(label=self.title, max_width_chars=11, lines=2, wrap=True,
                         wrap_mode=Pango.WrapMode.WORD_CHAR, ellipsize=Pango.EllipsizeMode.END,
                         justify=Gtk.Justification.CENTER)
        name.add_css_class("nova-island-tile-name")
        self.append(name)
        dot = Gtk.Box(halign=Gtk.Align.CENTER)
        dot.add_css_class("nova-island-dot")
        self.append(dot)

        click = Gtk.GestureClick(button=0)
        click.connect("released", self._on_click)
        self.add_controller(click)
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_: island.show_detail(app, self.title))
        self.add_controller(motion)

    def set_usage(self, u: Usage | None) -> None:
        values = {"CPU": u.cpu if u else None, "GPU": u.gpu if u else None,
                  "RAM": u.ram if u else None, "VRAM": u.vram if u else None}
        for key, value in values.items():
            self._usage[key].set_text(f"{key}: {format_percent(value)}")

    def _on_click(self, gesture, _n, _x, _y) -> None:
        button = gesture.get_current_button()
        if button == Gdk.BUTTON_SECONDARY:
            self.island.show_menu(self, self.running, self.xid)
        elif button == Gdk.BUTTON_PRIMARY:
            self.island.focus_window(self.xid, gesture.get_current_event_time())


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
        self.tiles: list[IslandTile] = []
        self._usage: dict[int, Usage] = {}  # last sample, so rebuilt tiles keep their numbers
        self.sampler = UsageSampler(self._on_usage)

        self.hidden = False
        self._hide_timer = 0
        self.pill = IslandPill(halign=Gtk.Align.CENTER, valign=Gtk.Align.START, margin_top=TOP_GAP)
        self.pill.add_css_class("nova-island")
        self.stack = Gtk.Stack(interpolate_size=True, hhomogeneous=False, vhomogeneous=False,
                               transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.connect("notify::transition-running", lambda *_: self._sync_region())
        self.pill.append(self.stack)
        self.revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN, reveal_child=True,
                                     halign=Gtk.Align.CENTER, valign=Gtk.Align.START)
        self.revealer.set_child(self.pill)
        self.revealer.connect("notify::child-revealed", lambda *_: self._update_region())
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(self.revealer)
        self.set_child(root)
        # Whole input area (reveal strip + pill): drives auto-hide.
        self._area = Gtk.EventControllerMotion()
        self._area.connect("enter", lambda *_: self._on_area_enter())
        self._area.connect("leave", lambda *_: self._schedule_hide())
        root.add_controller(self._area)

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
                                           propagate_natural_height=True, max_content_height=MAX_ROWS * TILE_H)
        self.scroller.set_child(self.flow)
        self.expanded.append(self.scroller)
        # max_width_chars=1: the title line must never widen the pill (it would
        # resize on every hover, slip out from under the pointer and flicker);
        # it just fills the width the tiles give it and ellipsizes.
        self.detail = Gtk.Label(xalign=0.5, ellipsize=Pango.EllipsizeMode.MIDDLE, max_width_chars=1)
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
        elif key == "island_autohide":
            self._place()
            if self.app.settings[key]:
                self._schedule_hide()
            else:
                self._cancel(("_hide_timer",))
                self.reveal(True)
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
        self.app.move_window(self, x, g.y + self._panel_h, mon)
        mons = self.app.all_monitor_infos()
        root = (max(m.geometry.x + m.geometry.width for m in mons),
                max(m.geometry.y + m.geometry.height for m in mons)) if mons else (0, 0)
        thickness = reserve_thickness(self._panel_h) if reserve and not self.autohide else 0
        x11.set_strut(self._xid, x11.strut_values("top", thickness, (g.x, g.y, g.width, g.height), root))
        self._sync_region()
        return GLib.SOURCE_REMOVE

    def show_island(self) -> None:
        self.set_focusable(False)
        self.set_visible(True)
        self._render()
        self._schedule_hide(2000)  # show it briefly at login so it can be found

    # -- auto-hide ---------------------------------------------------------------------
    @property
    def autohide(self) -> bool:
        return self.app.settings["island_autohide"]

    def reveal(self, show: bool) -> None:
        self.hidden = not show
        self.revealer.set_transition_duration(220 if self.app.animations_enabled() else 0)
        self.revealer.set_reveal_child(show)
        self._update_region()

    def _on_area_enter(self) -> None:
        self._cancel(("_hide_timer",))
        if self.hidden:
            self.reveal(True)

    def _schedule_hide(self, delay: int = HIDE_MS) -> None:
        self._cancel(("_hide_timer",))
        if self.autohide:
            self._hide_timer = GLib.timeout_add(delay, self._do_hide)

    def _do_hide(self) -> bool:
        self._hide_timer = 0
        if not self.autohide or self._area.contains_pointer() or self._menu_open or self.state == "toast":
            return GLib.SOURCE_REMOVE  # leave / menu close / toast end reschedule it
        if self.state == "expanded":
            self.collapse()
        self.reveal(False)
        return GLib.SOURCE_REMOVE

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
        self.header_label.set_text(island_header(self.apps))
        self.flow.remove_all()
        active = self.app.windows.active
        tiles = [IslandTile(self, a, xid, title, xid == active)
                 for a in self.apps for xid, title in zip(a.windows, a.titles)]
        for tile in tiles:
            tile.set_usage(self._usage.get(tile.pid))
            self.flow.append(tile)
        self.tiles = tiles
        n = len(tiles)
        self.sampler.watch(t.pid for t in tiles)
        self.flow.set_min_children_per_line(min(max(n, 1), PER_ROW))
        self.expanded.set_size_request(island_width(n), -1)
        front = self.apps[0] if self.apps else None
        self.show_detail(front, front.titles[0] if front else "")

    def _on_usage(self, sample: dict[int, Usage]) -> None:
        if self.state != "expanded":
            return
        self._usage = sample
        for tile in self.tiles:
            tile.set_usage(sample.get(tile.pid))

    def show_detail(self, app: RunningApp | None, title: str = "") -> None:
        if app is None:
            text = "Open an app and it shows up here"
        else:
            text = f"{app.name} — {title}" if title and title != app.name else app.name
        if self.detail.get_text() != text:
            self.detail.set_text(text)
            self.detail.set_tooltip_text(text)

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
        self.sampler.stop()
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
        if self.hidden:
            self.reveal(True)
        self._set_page("toast")
        self._cancel(("_toast_timer",))
        self._toast_timer = GLib.timeout_add(TOAST_MS, self._end_toast)

    def _end_toast(self) -> bool:
        self._toast_timer = 0
        if self.state == "toast":
            self.state = "compact"
            self._render()
            if not self._area.contains_pointer():
                self._schedule_hide()
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

    def focus_window(self, xid: int, time: int) -> None:
        x11.activate(xid, time)
        if self.state == "expanded":
            self.collapse()

    def show_menu(self, tile: Gtk.Widget, app: RunningApp, xid: int) -> None:
        entries: list = [("Close Window", lambda: x11.close_window(xid))]
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
        if not self._area.contains_pointer():
            self._schedule_hide()
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
        region = cairo.Region(cairo.RectangleInt((WIDTH - HOT_W) // 2, 0, HOT_W, HOT_H))
        if not self.hidden or self.revealer.get_child_revealed():
            ok, b = self.pill.compute_bounds(self)
            if not ok or b.size.width < 1:
                # Not laid out yet: use the size the pill is about to take.
                w, h = IDLE_W, COMPACT_H + TOP_GAP
                rect = cairo.RectangleInt((WIDTH - w) // 2, 0, w, h)
            else:
                # From the window top, so the gap above the pill is covered too.
                rect = cairo.RectangleInt(int(b.origin.x), 0, int(b.size.width + 0.5),
                                          int(b.origin.y + b.size.height + 0.5))
            region.union(rect)
        surface.set_input_region(region)


def _clear(box: Gtk.Box) -> None:
    while (child := box.get_first_child()) is not None:
        box.remove(child)

