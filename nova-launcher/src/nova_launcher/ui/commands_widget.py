"""On-screen commands widget: every Nova command at a glance, starting with
All Applications.

A small glass panel pinned to a corner of the primary monitor.  On X11 it is
kept *below* normal windows (like a desktop gadget), on every workspace, and
out of the taskbar / Alt+Tab.  Rows run the command; terminal commands are
copied to the clipboard on click.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from ..system import x11  # noqa: E402
from .widgets import Toast, gicon_for  # noqa: E402

if TYPE_CHECKING:
    from ..application import NovaApplication

log = logging.getLogger("nova.widget")

MARGIN = 24
WIDTH = 330


@dataclass(frozen=True)
class Command:
    id: str
    title: str
    subtitle: str
    icon: str
    shortcut: str = ""


def accel_label(accel: str) -> str:
    """'<Super>space' -> 'Windows+Space' (Super *is* the Windows key)."""
    label = accel
    try:
        ok, key, mods = Gtk.accelerator_parse(accel)
        if ok and key:
            label = Gtk.accelerator_get_label(key, mods)
    except Exception:
        pass
    return label.replace("Super", "Windows").replace("<Windows>", "Windows+")


def widget_commands(settings, dock_supported: bool = True) -> list[Command]:
    """Pure: the ordered command list (All Applications first; unit-tested)."""
    cmds = [
        Command("grid", "All Applications", "Launchpad grid of every installed app",
                "view-app-grid-symbolic",
                "Windows" if settings["windows_key_action"] == "grid" else accel_label(settings["shortcut_grid"])),
        Command("quick", "Quick Search", "Type to find apps, folders and settings",
                "system-search-symbolic",
                "Windows" if settings["windows_key_action"] == "quick" else accel_label(settings["shortcut_quick"])),
        Command("files", "Files — Home", "Open your home folder", "user-home"),
        Command("downloads", "Downloads", "Open the Downloads folder", "folder-download"),
    ]
    if dock_supported:
        shown = settings["dock_enabled"]
        cmds.append(Command("toggle-dock", "Hide Dock" if shown else "Show Dock",
                            "Floating dock at the " + settings["dock_position"], "view-grid-symbolic"))
    cmds += [
        Command("settings", "Settings", "Dock, grid, search, shortcuts", "preferences-system-symbolic"),
        Command("diagnose", "Diagnose Slow Startup", "Read-only report on slow apps / Files",
                "utilities-system-monitor-symbolic"),
        Command("repair", "Repair Slow Startup", "Opens a terminal; asks before every change",
                "applications-engineering-symbolic"),
        Command("quit", "Quit Nova", "Stop the launcher (shortcuts start it again)", "system-shutdown-symbolic"),
    ]
    return cmds


TERMINAL_COMMANDS = [
    ("nova-launcher --grid", "toggle the Launchpad grid"),
    ("nova-launcher --quick", "toggle quick search"),
    ("nova-launcher --search TEXT", "quick search pre-filled"),
    ("nova-launcher --settings", "open settings"),
    ("nova-launcher --background", "start without a window"),
    ("nova-launcher --no-dock", "this session without dock"),
    ("nova-launcher --diagnose", "read-only system report"),
    ("nova-launcher --repair", "report + fixes, asks first"),
    ("nova-launcher --set-default", "make Nova the default launcher"),
    ("nova-launcher --restore-default", "undo, restore GNOME"),
    ("nova-launcher --quit", "stop Nova"),
]


class CommandsWidget(Gtk.Window):
    def __init__(self, app: "NovaApplication"):
        super().__init__(application=app, title="Nova Commands")
        self.app = app
        self.set_decorated(False)
        self.set_resizable(False)
        self.add_css_class("nova-transparent")
        self._xid = 0

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        for side in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{side}")(10)  # room for the shadow
        self.panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.panel.add_css_class("nova-glass")
        self.panel.add_css_class("nova-widget")
        self.panel.set_size_request(WIDTH, -1)
        outer.append(self.panel)
        overlay = Gtk.Overlay(child=outer)
        self.toast = Toast()
        overlay.add_overlay(self.toast)
        self.set_child(overlay)

        header = Gtk.Box(spacing=8)
        header.add_css_class("nova-widget-header")
        logo = Gtk.Image.new_from_gicon(gicon_for("io.github.nova.Launcher"))
        logo.set_pixel_size(22)
        header.append(logo)
        title = Gtk.Label(label="Nova Launcher", xalign=0, hexpand=True)
        title.add_css_class("nova-widget-title")
        header.append(title)
        self.collapse_btn = Gtk.Button(icon_name="pan-up-symbolic", tooltip_text="Collapse")
        self.collapse_btn.add_css_class("flat")
        self.collapse_btn.add_css_class("circular")
        self.collapse_btn.connect("clicked", lambda *_: self.app.settings.set(
            "widget_collapsed", not self.app.settings["widget_collapsed"]))
        header.append(self.collapse_btn)
        close = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Hide widget (re-enable in Settings)")
        close.add_css_class("flat")
        close.add_css_class("circular")
        close.connect("clicked", lambda *_: self.app.settings.set("widget_enabled", False))
        header.append(close)
        self.panel.append(header)

        self.body = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN, transition_duration=180)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.list.add_css_class("nova-widget-list")
        self.list.connect("row-activated", lambda _l, row: self.run(row.command_id))
        body.append(self.list)

        exp = Gtk.Expander(label="Terminal commands")
        exp.add_css_class("nova-widget-expander")
        term = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        term.add_css_class("nova-widget-list")
        for cmd, desc in TERMINAL_COMMANDS:
            row = Gtk.ListBoxRow()
            row.cli = cmd
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            c = Gtk.Label(label=cmd, xalign=0, selectable=False)
            c.add_css_class("monospace")
            c.add_css_class("nova-widget-cli")
            d = Gtk.Label(label=desc, xalign=0)
            d.add_css_class("dim-label")
            d.add_css_class("caption")
            box.append(c)
            box.append(d)
            row.set_child(box)
            row.set_tooltip_text("Click to copy")
            term.append(row)
        term.connect("row-activated", lambda _l, row: self._copy(row.cli))
        exp.set_child(term)
        body.append(exp)
        self.body.set_child(body)
        self.panel.append(self.body)

        self.connect("realize", self._on_realize)
        self.connect("map", lambda *_: GLib.idle_add(self._after_map))
        self.connect("close-request", lambda *_: (self.app.settings.set("widget_enabled", False), True)[1])
        app.settings.connect(self._on_setting)
        Gdk.Display.get_default().get_monitors().connect("items-changed",
                                                         lambda *_: GLib.timeout_add(500, self._place))
        self.rebuild()

    # -- content -------------------------------------------------------------------
    def rebuild(self) -> None:
        while (row := self.list.get_row_at_index(0)) is not None:
            self.list.remove(row)
        for cmd in widget_commands(self.app.settings, self.app.dock_supported()):
            row = Gtk.ListBoxRow()
            row.command_id = cmd.id
            box = Gtk.Box(spacing=10)
            img = Gtk.Image.new_from_gicon(gicon_for(cmd.icon))
            img.set_pixel_size(20)
            box.append(img)
            texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
            t = Gtk.Label(label=cmd.title, xalign=0, ellipsize=Pango.EllipsizeMode.END)
            t.add_css_class("nova-app-name")
            s = Gtk.Label(label=cmd.subtitle, xalign=0, ellipsize=Pango.EllipsizeMode.END)
            s.add_css_class("dim-label")
            s.add_css_class("caption")
            texts.append(t)
            texts.append(s)
            box.append(texts)
            if cmd.shortcut:
                k = Gtk.Label(label=cmd.shortcut, valign=Gtk.Align.CENTER)
                k.add_css_class("nova-kbd")
                box.append(k)
            row.set_child(box)
            self.list.append(row)
        if cmd := self.list.get_row_at_index(0):
            cmd.add_css_class("nova-widget-primary")  # All Applications
        collapsed = self.app.settings["widget_collapsed"]
        self.body.set_reveal_child(not collapsed)
        self.collapse_btn.set_icon_name("pan-down-symbolic" if collapsed else "pan-up-symbolic")
        self.collapse_btn.set_tooltip_text("Expand" if collapsed else "Collapse")

    def _on_setting(self, key: str) -> None:
        if key in ("shortcut_quick", "shortcut_grid", "dock_enabled", "dock_position", "widget_collapsed",
                   "windows_key_action"):
            self.rebuild()
            GLib.timeout_add(220, self._place)  # after the collapse animation
        elif key in ("widget_position", "widget_monitor"):
            self._place()

    # -- commands --------------------------------------------------------------------
    def run(self, command_id: str) -> None:
        a = self.app
        if command_id == "grid":
            a.grid.open()
        elif command_id == "quick":
            a.quick.open()
        elif command_id in ("files", "downloads"):
            places = {p.id: p for p in a.places()}
            place = places.get("home" if command_id == "files" else "xdg-downloads") or places["home"]
            a.open_place(place)
        elif command_id == "toggle-dock":
            a.settings.set("dock_enabled", not a.settings["dock_enabled"])
        elif command_id == "settings":
            a.show_settings()
        elif command_id == "diagnose":
            self.toast.show("Running diagnostics… the report opens when done")
            a.activate_action("diagnose", None)
        elif command_id == "repair":
            self._run_in_terminal("nova-launcher --repair")
        elif command_id == "quit":
            a.quit_app()

    def _run_in_terminal(self, cmdline: str) -> None:
        # GIO picks the user's terminal; no shell is involved.
        try:
            info = Gio.AppInfo.create_from_commandline(cmdline, "Nova Repair", Gio.AppInfoCreateFlags.NEEDS_TERMINAL)
            info.launch([], Gdk.Display.get_default().get_app_launch_context())
        except GLib.Error as exc:
            self.toast.show(f"Could not open a terminal: {exc.message}")

    def _copy(self, text: str) -> None:
        self.get_clipboard().set(text)
        self.toast.show(f"Copied: {text}", 2.5)

    # -- placement -------------------------------------------------------------------
    def _on_realize(self, *_):
        self._xid = self.app.xid_of(self)
        if self._xid:
            x11.make_desktop_widget(self._xid)

    def _after_map(self) -> bool:
        if self._xid:
            # GTK rewrites _NET_WM_STATE on map, so (re)request ours from the WM.
            x11.change_state(self._xid, True, "_NET_WM_STATE_BELOW", "_NET_WM_STATE_STICKY")
            # Mutter flags windows mapped without focus a moment *after* map.
            for delay in (0, 400, 1200):
                GLib.timeout_add(delay, lambda: (x11.change_state(
                    self._xid, False, "_NET_WM_STATE_DEMANDS_ATTENTION"), GLib.SOURCE_REMOVE)[1])
        return self._place()

    def _place(self) -> bool:
        if not self._xid or not self.get_mapped():
            return GLib.SOURCE_REMOVE
        mon = self.app.target_monitor_info(self.app.settings["widget_monitor"])
        if mon is None:
            return GLib.SOURCE_REMOVE
        _, nat = self.get_preferred_size()
        w, h = nat.width, nat.height
        area = mon.workarea
        pos = self.app.settings["widget_position"]
        x = area.x + MARGIN if pos.endswith("left") else area.x + area.width - w - MARGIN
        y = area.y + MARGIN if pos.startswith("top") else area.y + area.height - h - MARGIN
        x11.move(self._xid, x, y)
        return GLib.SOURCE_REMOVE

    def show_widget(self) -> None:
        self.rebuild()
        self.set_focusable(False)
        self.set_visible(True)  # no focus request: a desktop widget must not steal focus
