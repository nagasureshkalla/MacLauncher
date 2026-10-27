"""The resident Nova application.

One process owns the index and all windows.  Windows are built at startup
and afterwards only shown/hidden, so a warm open is "map + one frame".  The
GNOME shortcut runs ``gapplication action io.github.nova.Launcher toggle-quick``
(a small C binary) which reaches this process over D-Bus — no Python start-up
on the hot path.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import warnings
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, __version__, paths  # noqa: E402
from .config import Settings  # noqa: E402
from .core import app_launcher  # noqa: E402
from .core.app_index import AppIndex, UsageHistory  # noqa: E402
from .core.desktop_entry import AppRecord  # noqa: E402
from .core.file_manager import FileManager, Place, mounted_places, standard_places  # noqa: E402
from .logging_config import set_debug, setup_logging  # noqa: E402
from .system import monitor_support, x11  # noqa: E402

log = logging.getLogger("nova.app")


class NovaApplication(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.t_start = time.perf_counter()
        self.settings = Settings()
        self.index: AppIndex | None = None
        self.quick = None
        self.grid = None
        self.dock = None
        self.settings_window = None
        self.files: FileManager | None = None
        self._bench_file = os.environ.get("NOVA_BENCH_FILE")
        self._dock_ok: bool | None = None
        self._held = False

        for name, short, desc in (
            ("quick", ord("q"), "Toggle the quick launcher"),
            ("grid", ord("g"), "Toggle the Launchpad grid"),
            ("settings", 0, "Open settings"),
            ("background", ord("b"), "Start without showing a window"),
            ("quit", 0, "Quit the running instance"),
            ("debug", 0, "Verbose logging"),
            ("no-dock", 0, "Do not show the dock this session"),
        ):
            self.add_main_option(name, short, GLib.OptionFlags.NONE, GLib.OptionArg.NONE, desc, None)
        self.add_main_option("screenshot", 0, GLib.OptionFlags.NONE, GLib.OptionArg.FILENAME,
                             "Render each window to PNG files in DIR, then quit (development)", "DIR")
        self.add_main_option("search", ord("s"), GLib.OptionFlags.NONE, GLib.OptionArg.STRING,
                             "Open the quick launcher with this text", "TEXT")
        self._no_dock = False

    # -- lifecycle -------------------------------------------------------------------
    def do_startup(self):
        Adw.Application.do_startup(self)
        self.settings.load()
        setup_logging(self.settings["debug_logging"] or "--debug" in sys.argv)
        log.info("Nova Launcher %s starting (session=%s)", __version__, os.environ.get("XDG_SESSION_TYPE"))
        from .ui.widgets import load_css

        load_css()
        usage = UsageHistory(enabled=self.settings["history_enabled"])
        usage.load()
        self.index = AppIndex(usage=usage)
        self.index.engine.fuzzy = self.settings["search_fuzzy"]
        self.index.engine.descriptions = self.settings["search_descriptions"]
        cached = self.index.load_cache()
        self.index.refresh_async(self._on_first_scan)
        self.index.watch()
        self.files = FileManager(self.settings["file_manager_timeout_ms"], on_slow=self._on_files_slow)
        self.settings.connect(self._on_setting)

        self._install_actions()
        self._apply_theme()
        Adw.StyleManager.get_default().connect("notify::dark", lambda *_: self._apply_theme())

        from .ui.application_grid import LaunchpadWindow
        from .ui.launcher_window import QuickLauncherWindow

        self.quick = QuickLauncherWindow(self)
        self.grid = LaunchpadWindow(self)
        # Pre-realize so the first open does not pay for surface creation.
        self.quick.realize()
        self.grid.realize()
        self._apply_theme()
        self.hold()
        self._held = True
        # Runs after command-line handling (so --no-dock is respected) and also
        # covers D-Bus activation, which has no command line at all.
        GLib.idle_add(lambda: (self.update_dock_visibility(), GLib.SOURCE_REMOVE)[1])
        log.info("startup done in %.0f ms (apps from cache: %s)",
                 (time.perf_counter() - self.t_start) * 1000, len(self.index) if cached else "no")

    def _on_first_scan(self, ok: bool) -> None:
        if not ok:
            log.warning("application scan failed; using %d cached apps (%s)", len(self.index), self.index.last_error)
        if self.quick is not None and self.quick.get_visible():
            self.quick.refresh()

    def do_command_line(self, cmdline: Gio.ApplicationCommandLine) -> int:
        opts = cmdline.get_options_dict().end().unpack()
        if opts.get("debug"):
            set_debug(True)
        if opts.get("quit"):
            self.quit_app()
            return 0
        if opts.get("no-dock"):
            self._no_dock = True
        self.update_dock_visibility()
        if opts.get("screenshot"):
            from .devtools import screenshot_tour

            screenshot_tour(self, Path(bytes(opts["screenshot"]).rstrip(b"\0").decode()))
            return 0
        if "search" in opts:
            self.quick.open()
            self.quick.entry.set_text(opts["search"])
            self.quick.entry.set_position(-1)
        elif opts.get("quick"):
            self.quick.toggle()
        elif opts.get("grid"):
            self.grid.toggle()
        elif opts.get("settings"):
            self.show_settings()
        elif opts.get("background"):
            pass
        else:
            # Plain "nova-launcher" (e.g. from the app menu): show the grid.
            self.grid.toggle()
        return 0

    def do_activate(self):
        # D-Bus activation without an action (e.g. GNOME app grid icon).
        self.update_dock_visibility()
        self.grid.open()

    def quit_app(self) -> None:
        log.info("quitting")
        if self.index:
            self.index.unwatch()
        for w in (self.quick, self.grid, self.dock, self.settings_window):
            if w is not None:
                w.destroy()
        if self._held:
            self.release()
            self._held = False
        self.quit()

    # -- actions ---------------------------------------------------------------------
    def _install_actions(self) -> None:
        def add(name, cb, ptype=None):
            act = Gio.SimpleAction.new(name, GLib.VariantType.new(ptype) if ptype else None)
            act.connect("activate", lambda _a, p: cb(p))
            self.add_action(act)

        add("toggle-quick", lambda _p: self.quick.toggle())
        add("toggle-grid", lambda _p: self.grid.toggle())
        add("show-quick", lambda _p: self.quick.open())
        add("show-grid", lambda _p: self.grid.open())
        add("settings", lambda _p: self.show_settings())
        add("grid", lambda _p: self.grid.open())
        add("diagnose", lambda _p: self.run_diagnostics_async(self._open_report))
        add("quit", lambda _p: self.quit_app())
        add("open-uri", lambda p: self.open_place(Place("uri", p.get_string(), p.get_string(), "folder")), "s")
        add("launch", lambda p: self._launch_id(p.get_string()), "s")

    def _launch_id(self, app_id: str) -> None:
        rec = self.index.get(app_id)
        if rec:
            self.launch_app(rec)

    # -- settings / theme --------------------------------------------------------------
    def _on_setting(self, key: str) -> None:
        if key == "theme":
            self._apply_theme()
        elif key == "debug_logging":
            set_debug(self.settings[key])
        elif key in ("search_fuzzy", "search_descriptions"):
            self.index.engine.fuzzy = self.settings["search_fuzzy"]
            self.index.engine.descriptions = self.settings["search_descriptions"]
        elif key == "history_enabled":
            self.index.usage.enabled = self.settings[key]
        elif key == "file_manager_timeout_ms":
            self.files.timeout_ms = self.settings[key]
        elif key == "animation_level":
            self._apply_theme()

    def _apply_theme(self) -> None:
        sm = Adw.StyleManager.get_default()
        sm.set_color_scheme({"light": Adw.ColorScheme.FORCE_LIGHT, "dark": Adw.ColorScheme.FORCE_DARK}
                            .get(self.settings["theme"], Adw.ColorScheme.PREFER_LIGHT))
        dark = sm.get_dark()
        level = self.settings["animation_level"]
        for w in (self.quick, self.grid, self.dock, self.settings_window):
            if w is None:
                continue
            (w.add_css_class if dark else w.remove_css_class)("nova-dark")
            (w.remove_css_class if dark else w.add_css_class)("nova-light")
            for cls, on in (("nova-reduced-motion", level == "reduced"), ("nova-no-motion", level == "none")):
                (w.add_css_class if on else w.remove_css_class)(cls)

    def animations_enabled(self) -> bool:
        gtk_anim = Gtk.Settings.get_default().get_property("gtk-enable-animations")
        return gtk_anim and self.settings["animation_level"] == "full"

    # -- windows -------------------------------------------------------------------
    def dock_supported(self) -> bool:
        if self._dock_ok is None:
            self._dock_ok = x11.available() and _is_x11_display()
        return self._dock_ok

    def update_dock_visibility(self) -> None:
        want = self.settings["dock_enabled"] and not self._no_dock and self.dock_supported()
        if want and self.dock is None:
            from .ui.dock import DockWindow

            self.dock = DockWindow(self)
            self._apply_theme()
        if self.dock is not None:
            if want and not self.dock.get_visible():
                self.dock.show_dock()
            elif not want and self.dock.get_visible():
                self.dock.set_visible(False)

    def show_settings(self) -> None:
        if self.settings_window is None:
            from .ui.settings_window import SettingsWindow

            self.settings_window = SettingsWindow(self)
            self._apply_theme()
        self.present_window(self.settings_window)

    def xid_of(self, window: Gtk.Window) -> int:
        surface = window.get_surface()
        if surface is None:
            return 0
        try:
            gi.require_version("GdkX11", "4.0")
            from gi.repository import GdkX11

            if isinstance(surface, GdkX11.X11Surface):
                return surface.get_xid()
        except (ImportError, ValueError):
            pass
        return 0

    def target_monitor_info(self, preference: str = "primary"):
        return monitor_support.choose_monitor(preference, monitor_support.list_monitors())

    def target_monitor(self):
        info = self.target_monitor_info()
        return monitor_support.gdk_monitor_for(info) if info else None

    def place_centered(self, window: Gtk.Window, width: int, bias: float) -> None:
        xid = self.xid_of(window)
        info = self.target_monitor_info()
        if not xid or info is None:
            return  # Wayland: the compositor places the window.
        x, y = monitor_support.centered(info.workarea, width, 420, bias)
        x11.move(xid, x, y)

    def present_window(self, window: Gtk.Window) -> None:
        """Show ``window`` and make sure it gets focus.

        Keyboard-shortcut launches arrive without a user timestamp; using the
        X server's current time prevents Mutter's focus-stealing prevention
        from showing "… is ready" instead of focusing the window.
        """
        t0 = time.monotonic_ns()
        if not window.get_realized():
            window.realize()
        ts = 0
        surface = window.get_surface()
        try:
            from gi.repository import GdkX11

            if isinstance(surface, GdkX11.X11Surface):
                ts = GdkX11.x11_get_server_time(surface)
                # Mutter compares this against the focused window's last
                # user time when deciding whether a newly mapped window may
                # take focus.
                surface.set_user_time(ts)
        except (ImportError, ValueError, TypeError):
            pass
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            if ts:
                window.present_with_time(ts)
            else:
                window.present()
        if ts:
            xid = self.xid_of(window)

            def ensure_focus():
                if window.get_visible() and not window.is_active() and xid:
                    # Explicit activation request flagged as a user action.
                    x11.activate(xid, GdkX11.x11_get_server_time(surface))
                return GLib.SOURCE_REMOVE

            GLib.timeout_add(60, ensure_focus)
        if self._bench_file:
            self._bench_frame(window, t0)

    def _bench_frame(self, window: Gtk.Window, t0: int) -> None:
        clock = window.get_frame_clock()
        if clock is None:
            GLib.idle_add(lambda: (self._bench_frame(window, t0), GLib.SOURCE_REMOVE)[1])
            return
        handler = [0]

        def after_paint(c):
            c.disconnect(handler[0])
            with open(self._bench_file, "a") as fh:
                fh.write(f"{window.get_title()}\t{t0}\t{time.monotonic_ns()}\n")

        handler[0] = clock.connect("after-paint", after_paint)

    # -- launching ---------------------------------------------------------------------
    def launch_app(self, record: AppRecord, source: Gtk.Widget | None = None, action: str | None = None):
        display = Gdk.Display.get_default()
        ctx = display.get_app_launch_context() if display else None
        res = app_launcher.launch(record, ctx, action=action)
        if res.ok:
            self.index.usage.record(record.id)
        else:
            self._notify("launch-failed", f"Could not start {record.name}", res.error)
        return res

    def show_app_menu(self, widget: Gtk.Widget, record: AppRecord, extra=None) -> None:
        from .ui.widgets import popup_menu

        pinned = self.settings["pinned_apps"]
        entries: list = [("Open", lambda: self.launch_app(record))]
        for act in record.actions:
            entries.append((act.name, lambda a=act.id: self.launch_app(record, action=a)))
        entries.append(None)
        if record.id in pinned:
            entries.append(("Remove from Dock", lambda: self.settings.set(
                "pinned_apps", [p for p in self.settings["pinned_apps"] if p != record.id])))
        else:
            entries.append(("Keep in Dock", lambda: self.settings.set(
                "pinned_apps", self.settings["pinned_apps"] + [record.id])))
        if extra:
            entries.append(None)
            entries.extend(extra)
        popup_menu(widget, entries)

    def self_desktop_id(self) -> str:
        return f"{APP_ID}.desktop"

    # -- files ---------------------------------------------------------------------------
    def places(self) -> list[Place]:
        return standard_places() + mounted_places()

    def open_place(self, place: Place) -> None:
        if place.kind == "recent" or not (place.uri.startswith("file://") or place.uri.startswith("trash:")):
            try:
                Gio.AppInfo.launch_default_for_uri(place.uri, None)
            except GLib.Error as exc:
                self._notify("open-failed", f"Could not open {place.name}", exc.message)
            return
        self.files.open(place.uri, done=lambda ok, msg: None if ok else
                        self._notify("open-failed", f"Could not open {place.name}", msg))

    def open_trash(self) -> None:
        self.open_place(Place("trash", "Trash", "trash:///", "user-trash"))

    def _on_files_slow(self, msg: str) -> None:
        n = Gio.Notification.new("Files is responding slowly")
        n.set_body(f"{msg}. Other apps are unaffected. Run diagnostics to find the cause.")
        n.add_button("Run diagnostics", "app.diagnose")
        self.send_notification("files-slow", n)

    def _notify(self, nid: str, title: str, body: str) -> None:
        n = Gio.Notification.new(title)
        n.set_body(body)
        self.send_notification(nid, n)

    # -- diagnostics -------------------------------------------------------------------
    def run_diagnostics_async(self, done) -> None:
        def worker():
            path = None
            try:
                from .system.diagnostics import Diagnostics

                d = Diagnostics()
                repairs = d.run_all()
                path = paths.state_dir() / f"diagnostic-report-{time.strftime('%Y%m%d-%H%M%S')}.md"
                paths.atomic_write(path, d.report_markdown(repairs))
                log.info("diagnostic report written to %s", path)
            except Exception:
                log.exception("diagnostics failed")
            GLib.idle_add(lambda: (done(str(path) if path else None), GLib.SOURCE_REMOVE)[1])

        threading.Thread(target=worker, name="nova-diagnostics", daemon=True).start()

    def _open_report(self, path: str | None) -> None:
        if path:
            Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(path).get_uri(), None)
            self._notify("diag", "Diagnostic report ready", path)


def _is_x11_display() -> bool:
    display = Gdk.Display.get_default()
    try:
        gi.require_version("GdkX11", "4.0")
        from gi.repository import GdkX11

        return isinstance(display, GdkX11.X11Display)
    except (ImportError, ValueError):
        return False
