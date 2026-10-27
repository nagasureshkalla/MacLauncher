"""Quick launcher: a compact, keyboard-first search window.

Open -> type -> Enter.  The window is built once at startup and only shown
and hidden afterwards, so opening it costs a map + one frame.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

from ..core.file_manager import Place  # noqa: E402
from ..core.search_engine import normalise  # noqa: E402
from .widgets import gicon_for  # noqa: E402

if TYPE_CHECKING:
    from ..application import NovaApplication

log = logging.getLogger("nova.quick")

MAX_ROWS = 20


@dataclass
class ResultItem:
    kind: str  # app | place | command
    title: str
    subtitle: str
    icon: str
    payload: Any
    section: str


COMMANDS = [
    ("settings", "Nova Settings", "Configure dock, grid, search and shortcuts", "preferences-system",
     ("settings", "preferences", "nova", "config")),
    ("grid", "Show All Applications", "Open the Launchpad grid", "view-app-grid-symbolic",
     ("launchpad", "grid", "all apps", "applications")),
    ("toggle-widget", "Show/Hide Commands Widget", "The on-screen list of Nova commands",
     "view-list-symbolic", ("widget", "commands", "help", "cheat sheet", "shortcuts")),
    ("diagnose", "Run System Diagnostics", "Find out why apps or Files open slowly", "utilities-system-monitor",
     ("diagnose", "diagnostics", "slow", "doctor")),
]


def _match_text(query: str, *fields: str) -> int:
    q = normalise(query.strip())
    if not q:
        return 0
    best = 0
    for f in fields:
        f = normalise(f)
        if f == q:
            best = max(best, 3)
        elif f.startswith(q) or any(w.startswith(q) for w in f.split()):
            best = max(best, 2)
        elif len(q) >= 3 and q in f:
            best = max(best, 1)
    return best


def build_results(index, query: str, settings, places: list[Place]) -> list[ResultItem]:
    """Pure function: query -> ordered result items (unit-tested)."""
    limit = settings["search_max_results"]
    items: list[ResultItem] = []
    q = query.strip()
    if not q:
        recent = [index.get(i) for i in index.usage.recent(limit)] if settings["history_enabled"] else []
        for r in [r for r in recent if r][:limit]:
            items.append(ResultItem("app", r.name, r.generic_name or r.description, r.icon, r, "Recent"))
        if not items:
            for r in index.all()[:limit]:
                items.append(ResultItem("app", r.name, r.generic_name or r.description, r.icon, r, "Applications"))
        return items

    for r in index.search(q, limit):
        sub = r.generic_name or r.description or r.executable
        items.append(ResultItem("app", r.name, sub, r.icon, r, "Applications"))

    if settings["search_places"]:
        scored = []
        for p in places:
            s = _match_text(q, p.name)
            if s:
                scored.append((s, p))
        scored.sort(key=lambda t: -t[0])
        for _, p in scored[:4]:
            sub = GLib.filename_display_name(GLib.filename_from_uri(p.uri)[0]) if p.uri.startswith("file://") else p.uri
            items.append(ResultItem("place", p.name, sub, p.icon, p, "Folders"))

    for cid, title, sub, icon, kws in COMMANDS:
        if _match_text(q, title, *kws) >= 2:
            items.append(ResultItem("command", title, sub, icon, cid, "Nova"))
    return items


class ResultRow(Gtk.ListBoxRow):
    def __init__(self):
        super().__init__()
        box = Gtk.Box(spacing=12)
        self.image = Gtk.Image(pixel_size=34)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        self.title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.title.add_css_class("nova-app-name")
        self.subtitle = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.subtitle.add_css_class("dim-label")
        self.subtitle.add_css_class("caption")
        texts.append(self.title)
        texts.append(self.subtitle)
        self.kbd = Gtk.Label(xalign=1, hexpand=True)
        self.kbd.add_css_class("dim-label")
        self.kbd.add_css_class("caption")
        box.append(self.image)
        box.append(texts)
        box.append(self.kbd)
        self.set_child(box)
        self.item: ResultItem | None = None

    def bind(self, item: ResultItem, position: int) -> None:
        self.item = item
        self.image.set_from_gicon(gicon_for(item.icon))
        self.title.set_text(item.title)
        self.subtitle.set_text(item.subtitle or "")
        self.subtitle.set_visible(bool(item.subtitle))
        self.kbd.set_text(f"Ctrl+{position + 1}" if position < 9 else "")


class QuickLauncherWindow(Gtk.Window):
    WIDTH = 680

    def __init__(self, app: "NovaApplication"):
        super().__init__(application=app, title="Nova Quick Launcher")
        self.app = app
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_default_size(self.WIDTH, -1)
        self.add_css_class("nova-transparent")
        self.items: list[ResultItem] = []
        self._places: list[Place] = []
        self._shown_at = 0

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.set_margin_top(18)
        outer.set_margin_bottom(28)
        outer.set_margin_start(24)
        outer.set_margin_end(24)
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        panel.add_css_class("nova-glass")
        panel.add_css_class("nova-quick")
        panel.set_size_request(self.WIDTH - 48, -1)

        self.entry = Gtk.SearchEntry(placeholder_text="Search apps, folders and settings")
        self.entry.add_css_class("nova-quick-entry")
        self.entry.connect("changed", self._on_changed)
        self.entry.connect("activate", lambda *_: self.activate_selected())
        self.entry.connect("stop-search", lambda *_: self.dismiss())
        panel.append(self.entry)

        self.sep = Gtk.Separator()
        panel.append(self.sep)

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.BROWSE)
        self.list.set_header_func(self._header)
        self.list.connect("row-activated", lambda _l, row: self._activate_row(row))
        self.rows = [ResultRow() for _ in range(MAX_ROWS)]
        for r in self.rows:
            r.set_visible(False)
            self.list.append(r)
        panel.append(self.list)

        self.status = Gtk.Label(xalign=0, wrap=True)
        self.status.add_css_class("nova-hint")
        panel.append(self.status)
        outer.append(panel)
        self.set_child(outer)

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        self.connect("notify::is-active", self._on_active_changed)
        self.connect("realize", self._on_realize)
        self.connect("map", self._on_map)
        self.connect("close-request", lambda *_: (self.dismiss(), True)[1])

    # -- placement ---------------------------------------------------------
    def _on_realize(self, *_):
        xid = self.app.xid_of(self)
        if xid:
            from ..system import x11

            x11.make_utility(xid)

    def _on_map(self, *_):
        self.app.place_centered(self, self.WIDTH, 0.22)

    # -- show / hide -------------------------------------------------------
    def toggle(self) -> None:
        if self.get_visible():
            self.dismiss()
        else:
            self.open()

    def open(self) -> None:
        self._places = self.app.places()
        self.entry.set_text("")
        self.status.set_text("")
        self.refresh()
        self._shown_at = GLib.get_monotonic_time()
        self.app.present_window(self)
        self.entry.grab_focus()

    def dismiss(self) -> None:
        self.set_visible(False)

    def _on_active_changed(self, *_):
        # Spotlight behaviour: clicking elsewhere closes it.  Ignore the very
        # first moments, while the WM is still handing over focus.
        if not self.is_active() and self.get_visible():
            if GLib.get_monotonic_time() - self._shown_at > 400_000:
                self.dismiss()

    # -- results -------------------------------------------------------------
    def _on_changed(self, *_):
        self.refresh()

    def refresh(self) -> None:
        self.items = build_results(self.app.index, self.entry.get_text(), self.app.settings, self._places)
        for i, row in enumerate(self.rows):
            if i < len(self.items):
                row.bind(self.items[i], i)
                row.set_visible(True)
            else:
                row.item = None
                row.set_visible(False)
        self.list.invalidate_headers()
        has = bool(self.items)
        self.list.set_visible(has)
        self.sep.set_visible(has)
        if has:
            self.list.select_row(self.rows[0])
        elif self.entry.get_text().strip():
            self.status.set_text(f"No results for “{self.entry.get_text().strip()}”")
        if has or not self.entry.get_text().strip():
            if not self.app.index.ready and not len(self.app.index):
                self.status.set_text("Indexing applications…")
            elif self.status.get_text().startswith(("No results", "Indexing")):
                self.status.set_text("")

    def _header(self, row: ResultRow, before: ResultRow | None):
        if row.item is None:
            row.set_header(None)
            return
        if before is None or before.item is None or before.item.section != row.item.section:
            label = Gtk.Label(label=row.item.section.upper(), xalign=0)
            label.add_css_class("nova-section")
            row.set_header(label)
        else:
            row.set_header(None)

    def selected_index(self) -> int:
        row = self.list.get_selected_row()
        return row.get_index() if row else -1

    def move_selection(self, delta: int) -> None:
        n = len(self.items)
        if not n:
            return
        i = (self.selected_index() + delta) % n
        row = self.rows[i]
        # Selection only; keyboard focus stays in the entry so typing continues.
        self.list.select_row(row)

    def activate_selected(self) -> None:
        row = self.list.get_selected_row()
        if row is not None and row.item is not None:
            self._activate_row(row)

    def _activate_row(self, row: ResultRow) -> None:
        item = row.item
        if item is None:
            return
        if item.kind == "app":
            res = self.app.launch_app(item.payload, source=self)
            if res.ok:
                self.dismiss()
            else:
                self.status.set_text(f"Could not start {item.title}: {res.error}")
        elif item.kind == "place":
            self.dismiss()
            self.app.open_place(item.payload)
        elif item.kind == "command":
            self.dismiss()
            self.app.activate_action(item.payload, None)

    def _on_key(self, _ctrl, keyval, _code, state):
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if keyval == Gdk.KEY_Escape:
            if self.entry.get_text():
                self.entry.set_text("")
            else:
                self.dismiss()
            return True
        if keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down) or (ctrl and keyval == Gdk.KEY_n):
            self.move_selection(1)
            return True
        if keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up) or (ctrl and keyval == Gdk.KEY_p):
            self.move_selection(-1)
            return True
        if keyval == Gdk.KEY_Tab:
            self.move_selection(-1 if state & Gdk.ModifierType.SHIFT_MASK else 1)
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.activate_selected()
            return True
        if ctrl and Gdk.KEY_1 <= keyval <= Gdk.KEY_9:
            i = keyval - Gdk.KEY_1
            if i < len(self.items):
                self._activate_row(self.rows[i])
            return True
        if not self.entry.has_focus() and Gdk.keyval_to_unicode(keyval):
            self.entry.grab_focus()
        return False
