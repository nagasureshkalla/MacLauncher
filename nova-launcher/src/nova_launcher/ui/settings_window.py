"""Preferences window (Libadwaita)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk  # noqa: E402

from ..config import SCHEMA  # noqa: E402
from ..system import desktop_integration as integ  # noqa: E402
from ..system.monitor_support import list_monitors  # noqa: E402
from .widgets import gicon_for  # noqa: E402

if TYPE_CHECKING:
    from ..application import NovaApplication


class SettingsWindow(Adw.PreferencesWindow):
    def __init__(self, app: "NovaApplication"):
        super().__init__(application=app, title="Nova Launcher Settings", search_enabled=True)
        self.app = app
        self.s = app.settings
        self.set_default_size(720, 760)
        self.set_hide_on_close(True)
        self.add(self._general_page())
        self.add(self._dock_page())
        self.add(self._grid_page())
        self.add(self._search_page())
        self.add(self._system_page())

    # -- row helpers -------------------------------------------------------------
    def _switch(self, group, key, title, subtitle=""):
        row = Adw.SwitchRow(title=title, subtitle=subtitle)
        row.set_active(self.s[key])
        row.connect("notify::active", lambda r, _p: self.s.set(key, r.get_active()))
        group.add(row)
        return row

    def _spin(self, group, key, title, step=1, digits=0, subtitle=""):
        opt = SCHEMA[key]
        adj = Gtk.Adjustment(value=self.s[key], lower=opt.min, upper=opt.max, step_increment=step)
        row = Adw.SpinRow(title=title, subtitle=subtitle, adjustment=adj, digits=digits)
        conv = float if opt.kind is float else int
        row.connect("notify::value", lambda r, _p: self.s.set(key, conv(round(r.get_value(), 2))))
        group.add(row)
        return row

    def _combo(self, group, key, title, choices: list[tuple[str, str]], subtitle=""):
        row = Adw.ComboRow(title=title, subtitle=subtitle)
        row.set_model(Gtk.StringList.new([label for _, label in choices]))
        values = [v for v, _ in choices]
        if self.s[key] in values:
            row.set_selected(values.index(self.s[key]))
        row.connect("notify::selected", lambda r, _p: self.s.set(key, values[r.get_selected()]))
        group.add(row)
        return row

    # -- pages ---------------------------------------------------------------------
    def _general_page(self):
        page = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")
        g = Adw.PreferencesGroup(title="Appearance")
        self._combo(g, "theme", "Theme", [("system", "Follow system"), ("light", "Light"), ("dark", "Dark")])
        self._combo(g, "animation_level", "Animations",
                    [("full", "Full"), ("reduced", "Reduced motion"), ("none", "Off")],
                    "Reduced motion replaces bounces and zooms with fades")
        page.add(g)

        g = Adw.PreferencesGroup(title="Startup")
        row = Adw.SwitchRow(title="Start at login", subtitle="Adds ~/.config/autostart/nova-launcher.desktop")
        row.set_active(integ.autostart_enabled())
        row.connect("notify::active", lambda r, _p: integ.set_autostart(r.get_active()))
        g.add(row)
        page.add(g)

        g = Adw.PreferencesGroup(
            title="Keyboard shortcuts",
            description="GNOME custom shortcuts that run “gapplication action io.github.nova.Launcher …”. "
                        "Nothing is changed until you press Install; Remove deletes only Nova's shortcuts.")
        for key, label, action in (("shortcut_quick", "Quick launcher", "toggle-quick"),
                                   ("shortcut_grid", "Launchpad grid", "toggle-grid")):
            entry = Adw.EntryRow(title=label, text=self.s[key], show_apply_button=True)
            entry.connect("apply", lambda e, k=key: self._apply_accel(e, k))
            g.add(entry)
        self.shortcut_status = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label", "caption"])
        btns = Gtk.Box(spacing=8, margin_top=8)
        install = Gtk.Button(label="Install shortcuts", css_classes=["suggested-action"])
        install.connect("clicked", lambda *_: self._install_shortcuts())
        remove = Gtk.Button(label="Remove shortcuts")
        remove.connect("clicked", lambda *_: self.shortcut_status.set_text(integ.remove_shortcuts()))
        btns.append(install)
        btns.append(remove)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(btns)
        box.append(self.shortcut_status)
        g.add(box)
        page.add(g)
        return page

    def _apply_accel(self, entry, key):
        text = entry.get_text().strip()
        ok, _k, _m = Gtk.accelerator_parse(text)
        if not ok:
            self.shortcut_status.set_text(f"“{text}” is not a valid shortcut (example: <Super>slash)")
            return
        self.s.set(key, text)
        conflicts = integ.find_conflicts(text)
        self.shortcut_status.set_text(("Conflicts with: " + "; ".join(conflicts)) if conflicts else "No conflicts found.")

    def _install_shortcuts(self):
        msgs = []
        for key, name, action in (("shortcut_quick", "quick", "toggle-quick"),
                                  ("shortcut_grid", "grid", "toggle-grid")):
            conflicts = integ.find_conflicts(self.s[key])
            if conflicts:
                msgs.append(f"{self.s[key]} skipped — already used by {conflicts[0]}")
                continue
            msgs.append(integ.install_shortcut(name, self.s[key], action))
        self.shortcut_status.set_text("\n".join(msgs))

    def _dock_page(self):
        page = Adw.PreferencesPage(title="Dock", icon_name="view-grid-symbolic")
        g = Adw.PreferencesGroup(title="Dock")
        if not self.app.dock_supported():
            g.set_description("The floating dock needs an X11 session. On Wayland GNOME does not let "
                              "applications place dock windows; the grid and quick launcher still work.")
        self._switch(g, "dock_enabled", "Show dock")
        self._combo(g, "dock_position", "Position", [("bottom", "Bottom"), ("left", "Left"), ("right", "Right")])
        mons = list_monitors()
        self._combo(g, "dock_monitor", "Monitor",
                    [("primary", "Primary monitor")] + [(m.connector, m.connector) for m in mons])
        self._switch(g, "dock_autohide", "Automatically hide", "Reveal by moving the pointer to the screen edge")
        self._switch(g, "dock_reserve_space", "Reserve screen space",
                     "Maximized windows stop above the dock (when not auto-hiding)")
        page.add(g)
        g = Adw.PreferencesGroup(title="Size")
        self._spin(g, "dock_icon_size", "Icon size", step=4)
        self._spin(g, "dock_magnification", "Magnification", step=0.1, digits=1, subtitle="1.0 disables zoom")
        self._spin(g, "dock_spacing", "Spacing")
        page.add(g)
        g = Adw.PreferencesGroup(title="Content")
        self._switch(g, "dock_show_running", "Show running applications")
        self._switch(g, "dock_show_recent", "Show recent applications")
        self._spin(g, "dock_recent_count", "Recent applications")
        page.add(g)

        g = Adw.PreferencesGroup(title="Pinned applications",
                                 description="Drag icons onto the dock to pin them; right-click to unpin.")
        self.pinned_group = g
        self._pinned_rows: list = []
        self._fill_pinned()
        self.s.connect(lambda k: self._fill_pinned() if k == "pinned_apps" else None)
        page.add(g)
        return page

    def _fill_pinned(self):
        for r in self._pinned_rows:
            self.pinned_group.remove(r)
        self._pinned_rows = []
        for app_id in self.s["pinned_apps"]:
            rec = self.app.index.get(app_id)
            row = Adw.ActionRow(title=rec.name if rec else app_id,
                                subtitle="" if rec else "not installed (hidden from dock)")
            if rec:
                img = Gtk.Image.new_from_gicon(gicon_for(rec.icon))
                img.set_pixel_size(32)
                row.add_prefix(img)
            rm = Gtk.Button(icon_name="list-remove-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"])
            rm.connect("clicked", lambda _b, a=app_id: self.s.set("pinned_apps",
                                                                  [p for p in self.s["pinned_apps"] if p != a]))
            row.add_suffix(rm)
            self.pinned_group.add(row)
            self._pinned_rows.append(row)

    def _grid_page(self):
        page = Adw.PreferencesPage(title="Launchpad", icon_name="view-app-grid-symbolic")
        g = Adw.PreferencesGroup(title="Grid density")
        self._spin(g, "grid_columns", "Columns")
        self._spin(g, "grid_rows", "Rows per page")
        self._spin(g, "grid_icon_size", "Icon size", step=8)
        page.add(g)
        g = Adw.PreferencesGroup(title="Organisation",
                                 description="Drag an app onto another app to make a folder; drop on the left "
                                             "or right edge of an icon to reorder.")
        reset = Adw.ActionRow(title="Reset layout", subtitle="Alphabetical order, no folders")
        btn = Gtk.Button(label="Reset", valign=Gtk.Align.CENTER, css_classes=["destructive-action"])
        btn.connect("clicked", lambda *_: (self.s.set_folders({}), self.s.set("grid_order", [])))
        reset.add_suffix(btn)
        g.add(reset)
        page.add(g)
        return page

    def _search_page(self):
        page = Adw.PreferencesPage(title="Search", icon_name="system-search-symbolic")
        g = Adw.PreferencesGroup(title="Matching")
        self._switch(g, "search_fuzzy", "Fuzzy matching", "Tolerate typos such as “firefx”")
        self._switch(g, "search_descriptions", "Search descriptions")
        self._switch(g, "search_places", "Include folders", "Home, Downloads, mounted drives…")
        self._spin(g, "search_max_results", "Results in quick launcher")
        page.add(g)
        g = Adw.PreferencesGroup(title="History", description="Stored only on this computer.")
        self._switch(g, "history_enabled", "Remember launched applications", "Improves ranking and recent apps")
        clear = Adw.ActionRow(title="Clear history")
        btn = Gtk.Button(label="Clear", valign=Gtk.Align.CENTER)
        btn.connect("clicked", lambda *_: (self.app.index.usage.clear(), self.app.index.engine.rebuild(self.app.index.all())))
        clear.add_suffix(btn)
        g.add(clear)
        page.add(g)
        return page

    def _system_page(self):
        page = Adw.PreferencesPage(title="Diagnostics", icon_name="utilities-system-monitor-symbolic")
        g = Adw.PreferencesGroup(title="Diagnostics",
                                 description="Read-only checks for slow app and Files start-up. "
                                             "Nothing is changed by running them.")
        run = Adw.ActionRow(title="Run diagnostics", subtitle="Writes a report and opens it")
        self.diag_btn = Gtk.Button(label="Run", valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
        self.diag_btn.connect("clicked", lambda *_: self._run_diag())
        run.add_suffix(self.diag_btn)
        g.add(run)
        self._switch(g, "debug_logging", "Debug logging", "Verbose log in ~/.local/state/nova-launcher/nova.log")
        self._spin(g, "file_manager_timeout_ms", "File manager timeout (ms)", step=500,
                   subtitle="After this, Nova reports the file manager as unresponsive")
        page.add(g)
        g = Adw.PreferencesGroup(title="Status")
        idx = self.app.index
        rep = idx.last_report
        info = f"{len(idx)} applications indexed"
        if rep:
            info += f" from {rep.scanned} desktop files ({len(rep.skipped)} hidden or invalid)"
        g.add(Adw.ActionRow(title="Index", subtitle=info))
        g.add(Adw.ActionRow(title="Session", subtitle=f"{integ.session_type()} — dock "
                            f"{'available' if self.app.dock_supported() else 'unavailable'}"))
        page.add(g)
        return page

    def _run_diag(self):
        self.diag_btn.set_sensitive(False)
        self.diag_btn.set_label("Running…")

        def done(path):
            self.diag_btn.set_sensitive(True)
            self.diag_btn.set_label("Run")
            if path:
                Gtk.FileLauncher.new(Gio.File.new_for_path(path)).launch(self, None, None)

        self.app.run_diagnostics_async(done)
