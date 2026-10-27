"""Launchpad-style full-screen application grid."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, GObject, Gtk  # noqa: E402

from .grid_model import APP, FOLDER, GridModel, app_key, folder_key  # noqa: E402
from .widgets import AppTile, Toast, app_image, folder_icon, popup_menu  # noqa: E402

if TYPE_CHECKING:
    from ..application import NovaApplication

log = logging.getLogger("nova.grid")

RELEASE_AFTER_S = 20


def trim_heap() -> None:
    """Return freed heap pages to the OS (glibc only; no-op elsewhere)."""
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


class LaunchpadWindow(Gtk.Window):
    def __init__(self, app: "NovaApplication"):
        super().__init__(application=app, title="Nova Launchpad")
        self.app = app
        self.set_decorated(False)
        self.add_css_class("nova-grid-window")
        self.model: GridModel | None = None
        self.tiles: dict[str, AppTile] = {}
        self.result_keys: list[str] = []
        self.selected = -1
        self.open_folder_id: str | None = None
        self._closing = 0
        self._release_timer = 0
        self._dirty = True

        overlay = Gtk.Overlay()
        self.set_child(overlay)

        # Backdrop: blurred wallpaper + a soft dim layer (macOS Launchpad look).
        self.backdrop = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        overlay.set_child(self.backdrop)
        dim = Gtk.Box(can_target=False)
        dim.add_css_class("nova-grid-dim")
        overlay.add_overlay(dim)

        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.content.add_css_class("nova-grid-content")
        self.content.set_margin_top(48)
        self.content.set_margin_bottom(28)
        overlay.add_overlay(self.content)

        self.search = Gtk.SearchEntry(placeholder_text="Search", halign=Gtk.Align.CENTER)
        self.search.add_css_class("nova-grid-search")
        self.search.connect("changed", lambda *_: self._on_search())
        self.search.connect("activate", lambda *_: self.activate_selected())
        self.search.set_key_capture_widget(self)
        self.content.append(self.search)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=120,
                               vexpand=True)
        self.carousel = Adw.Carousel(vexpand=True, hexpand=True, allow_scroll_wheel=True,
                                     allow_mouse_drag=False, spacing=40)
        self.carousel.set_overflow(Gtk.Overflow.HIDDEN)
        self.carousel.connect("page-changed", lambda *_: self._on_page_changed())
        pages_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        pages_box.append(self.carousel)
        dots = Adw.CarouselIndicatorDots(carousel=self.carousel)
        dots.add_css_class("nova-dots")
        pages_box.append(dots)
        self.stack.add_named(pages_box, "pages")

        self.results = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                   valign=Gtk.Align.START, halign=Gtk.Align.CENTER,
                                   row_spacing=10, column_spacing=10)
        res_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        res_scroll.set_child(self.results)
        self.stack.add_named(res_scroll, "results")
        empty = Gtk.Label(label="No matching applications")
        empty.add_css_class("nova-empty")
        self.stack.add_named(empty, "empty")
        self.content.append(self.stack)

        # Folder overlay
        self.scrim = Gtk.Box()
        self.scrim.add_css_class("nova-folder-scrim")
        self.scrim.set_visible(False)
        click = Gtk.GestureClick()
        click.connect("released", lambda *_: self.close_folder())
        self.scrim.add_controller(click)
        overlay.add_overlay(self.scrim)
        self.folder_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                                    halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.folder_panel.add_css_class("nova-folder-panel")
        self.folder_title = Gtk.Entry(xalign=0.5)
        self.folder_title.add_css_class("nova-folder-title")
        self.folder_title.connect("activate", lambda *_: self._rename_folder())
        self.folder_title.connect("notify::has-focus", lambda *_: self._rename_folder())
        self.folder_panel.append(self.folder_title)
        self.folder_flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=5,
                                       min_children_per_line=3, homogeneous=True)
        self.folder_panel.append(self.folder_flow)
        self.folder_panel.set_visible(False)
        overlay.add_overlay(self.folder_panel)

        self.toast = Toast()
        overlay.add_overlay(self.toast)

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        # Click on empty background closes, like Launchpad.
        bg = Gtk.GestureClick()
        bg.connect("released", self._on_background_click)
        self.carousel.add_controller(bg)
        self.connect("close-request", lambda *_: (self.dismiss(), True)[1])

        app.index.connect(self.mark_dirty)
        app.settings.connect(self._on_setting)

    def update_backdrop(self) -> None:
        mon = self.app.target_monitor_info()
        tex = None
        if mon is not None and self.app.wallpaper is not None and self.app.settings["glass_effect"]:
            tex = self.app.wallpaper.texture(mon.geometry.width, mon.geometry.height)
        self.backdrop.set_paintable(tex)
        (self.add_css_class if tex else self.remove_css_class)("nova-has-backdrop")

    # -- data ------------------------------------------------------------------
    def mark_dirty(self) -> None:
        self._dirty = True
        if self.get_visible():
            self.rebuild()

    def _on_setting(self, key: str) -> None:
        if key in ("grid_columns", "grid_rows", "grid_icon_size", "grid_order", "folders"):
            if key == "grid_icon_size":
                self.tiles.clear()
            self.mark_dirty()

    @property
    def cols(self) -> int:
        return self.app.settings["grid_columns"]

    @property
    def per_page(self) -> int:
        return self.cols * self.app.settings["grid_rows"]

    def rebuild(self) -> None:
        s = self.app.settings
        self.model = GridModel(self.app.index.all(), s["grid_order"], s.folders)
        live = set(self.model.keys)
        # Folder previews depend on their contents, so always rebuild those.
        for k in [k for k in self.tiles if k not in live or k.startswith(FOLDER)]:
            del self.tiles[k]
        while (child := self.carousel.get_nth_page(0) if self.carousel.get_n_pages() else None) is not None:
            self.carousel.remove(child)
        for page_keys in self.model.pages(self.per_page):
            grid = Gtk.Grid(row_spacing=14, column_spacing=18, halign=Gtk.Align.CENTER,
                            valign=Gtk.Align.START, row_homogeneous=True, column_homogeneous=True)
            for i, key in enumerate(page_keys):
                tile = self._tile(key)
                if (old := tile.get_parent()) is not None:
                    old.remove(tile)
                grid.attach(tile, i % self.cols, i // self.cols, 1, 1)
            # Adw.Carousel lays pages out at each page's own width, so a centred
            # grid alone would let the next page peek in.  Each page fills the
            # screen and centres its grid.
            page = Gtk.Box(hexpand=True, halign=Gtk.Align.FILL)
            page.append(grid)
            grid.set_hexpand(True)
            self.carousel.append(page)
        self._dirty = False
        self.selected = -1
        self._on_search()

    def _tile(self, key: str) -> AppTile:
        tile = self.tiles.get(key)
        if tile is not None and tile.get_tooltip_text() == self.model.title(key):
            return tile
        size = self.app.settings["grid_icon_size"]
        if key.startswith(FOLDER):
            fid = key[len(FOLDER):]
            icon = folder_icon(self.model.folder_records(fid), size)
        else:
            icon = app_image(self.model.records[key[len(APP):]], size)
        tile = AppTile(key, self.model.title(key), size, icon)
        tile.connect("clicked", lambda t: self._activate_key(t.key))
        self._attach_dnd(tile)
        self._attach_menu(tile)
        self.tiles[key] = tile
        return tile

    # -- drag and drop -----------------------------------------------------------
    def _attach_dnd(self, tile: AppTile) -> None:
        src = Gtk.DragSource(actions=Gdk.DragAction.MOVE | Gdk.DragAction.COPY)
        src.connect("prepare", lambda *_: Gdk.ContentProvider.new_for_value(tile.key))
        src.connect("drag-begin", lambda s, _d: s.set_icon(Gtk.WidgetPaintable.new(tile), 40, 40))
        tile.add_controller(src)
        if tile.key.startswith(APP) or tile.key.startswith(FOLDER):
            tgt = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE | Gdk.DragAction.COPY)
            tgt.connect("motion", self._drop_motion, tile)
            tgt.connect("leave", lambda *_: self._clear_drop_marks(tile))
            tgt.connect("drop", self._drop, tile)
            tile.add_controller(tgt)

    @staticmethod
    def _zone(tile: Gtk.Widget, x: float) -> str:
        w = max(1, tile.get_width())
        if x < w * 0.28:
            return "before"
        if x > w * 0.72:
            return "after"
        return "into"

    def _clear_drop_marks(self, tile):
        for c in ("nova-drop-before", "nova-drop-after", "nova-drop-into"):
            tile.remove_css_class(c)

    def _drop_motion(self, _tgt, x, _y, tile):
        self._clear_drop_marks(tile)
        tile.add_css_class(f"nova-drop-{self._zone(tile, x)}")
        return Gdk.DragAction.MOVE

    def _drop(self, _tgt, value, x, _y, tile) -> bool:
        self._clear_drop_marks(tile)
        if not isinstance(value, str) or self.model is None or value == tile.key:
            return False
        if value.startswith("folderapp:"):  # dragged out of an open folder
            _, fid, app_id = value.split(":", 2)
            self.model.remove_from_folder(fid, app_id)
            value = app_key(app_id)
        zone = self._zone(tile, x)
        if zone == "into" and value.startswith(APP):
            fid = self.model.merge(value, tile.key)
            if fid:
                self.toast.show(f"Added to folder “{self.model.folders[fid]['name']}”")
        else:
            self.model.move(value, tile.key, after=(zone == "after"))
        self._save_model()
        return True

    def _save_model(self) -> None:
        order, folders = self.model.to_settings()
        self.app.settings.set_folders(folders)
        self.app.settings.set("grid_order", order)

    # -- context menu ----------------------------------------------------------
    def _attach_menu(self, tile: AppTile) -> None:
        g = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        g.connect("pressed", lambda *_: self._show_menu(tile))
        tile.add_controller(g)

    def _show_menu(self, tile: AppTile) -> None:
        if tile.key.startswith(FOLDER):
            popup_menu(tile, [("Open Folder", lambda: self.open_folder(tile.key[len(FOLDER):]))])
            return
        record = self.model.records.get(tile.key[len(APP):])
        if record is None:
            return
        self.app.show_app_menu(tile, record, extra=None)

    # -- search ------------------------------------------------------------------
    def _on_search(self) -> None:
        q = self.search.get_text().strip()
        if not q:
            self.result_keys = []
            self.stack.set_visible_child_name("pages")
            self.selected = -1
            self._update_selection()
            return
        records = self.app.index.search(q, 42)
        child = self.results.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.results.remove(child)
            child = nxt
        self.result_keys = []
        size = self.app.settings["grid_icon_size"]
        self.results.set_max_children_per_line(self.cols)
        self.results.set_min_children_per_line(min(self.cols, 3))
        for r in records:
            t = AppTile(app_key(r.id), r.name, size, app_image(r, size))
            t.connect("clicked", lambda t: self._activate_key(t.key))
            self._attach_menu(t)
            self.results.append(t)
            self.result_keys.append(t.key)
        self.stack.set_visible_child_name("results" if records else "empty")
        self.selected = 0 if records else -1
        self._update_selection()

    # -- keyboard navigation -----------------------------------------------------
    def visible_keys(self) -> list[str]:
        if self.result_keys or self.search.get_text().strip():
            return self.result_keys
        return self.model.keys if self.model else []

    def _tile_for_index(self, i: int) -> AppTile | None:
        if self.result_keys:
            child = self.results.get_child_at_index(i)
            return child.get_child() if child else None
        keys = self.visible_keys()
        return self.tiles.get(keys[i]) if 0 <= i < len(keys) else None

    def _update_selection(self) -> None:
        for t in list(self.tiles.values()):
            t.set_selected(False)
        c = self.results.get_first_child()
        while c is not None:
            c.get_child().set_selected(False)
            c = c.get_next_sibling()
        tile = self._tile_for_index(self.selected) if self.selected >= 0 else None
        if tile is not None:
            tile.set_selected(True)
            if not self.result_keys:
                page = self.selected // self.per_page
                if 0 <= page < self.carousel.get_n_pages() and round(self.carousel.get_position()) != page:
                    self.carousel.scroll_to(self.carousel.get_nth_page(page), self.app.animations_enabled())

    def move_selection(self, dx: int, dy: int) -> None:
        n = len(self.visible_keys())
        if not n:
            return
        if self.selected < 0:
            # First arrow press selects the first item of the visible page.
            self.selected = 0 if self.result_keys else round(self.carousel.get_position()) * self.per_page
        else:
            cols = self.cols
            if not self.result_keys and dy:
                # Up/Down stays within the page column grid.
                page, idx = divmod(self.selected, self.per_page)
                idx2 = idx + dy * cols
                if 0 <= idx2 < self.per_page:
                    self.selected = page * self.per_page + idx2
            else:
                self.selected += dx + dy * cols
        self.selected = max(0, min(n - 1, self.selected))
        self._update_selection()

    def activate_selected(self) -> None:
        keys = self.visible_keys()
        if 0 <= self.selected < len(keys):
            self._activate_key(keys[self.selected])
        elif self.result_keys:
            self._activate_key(self.result_keys[0])

    def _on_key(self, _c, keyval, _code, state) -> bool:
        if keyval == Gdk.KEY_Escape:
            if self.open_folder_id:
                self.close_folder()
            elif self.search.get_text():
                self.search.set_text("")
            else:
                self.dismiss()
            return True
        if self.open_folder_id:
            return False
        nav = {Gdk.KEY_Left: (-1, 0), Gdk.KEY_Right: (1, 0), Gdk.KEY_Up: (0, -1), Gdk.KEY_Down: (0, 1)}
        if keyval in nav:
            if keyval in (Gdk.KEY_Left, Gdk.KEY_Right) and self.search.has_focus() and self.search.get_text() \
                    and self.selected < 0:
                return False
            self.move_selection(*nav[keyval])
            return True
        if keyval in (Gdk.KEY_Page_Down, Gdk.KEY_Page_Up):
            self._page(1 if keyval == Gdk.KEY_Page_Down else -1)
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.activate_selected()
            return True
        return False

    def _page(self, delta: int) -> None:
        pos = round(self.carousel.get_position()) + delta
        if 0 <= pos < self.carousel.get_n_pages():
            self.carousel.scroll_to(self.carousel.get_nth_page(pos), self.app.animations_enabled())

    def _on_page_changed(self) -> None:
        if self.selected >= 0 and not self.result_keys:
            page = round(self.carousel.get_position())
            if self.selected // self.per_page != page:
                self.selected = page * self.per_page
                self._update_selection()

    # -- activation ------------------------------------------------------------
    def _activate_key(self, key: str) -> None:
        if key.startswith(FOLDER):
            self.open_folder(key[len(FOLDER):])
            return
        record = self.app.index.get(key[len(APP):])
        if record is None:
            return
        res = self.app.launch_app(record, source=self)
        if res.ok:
            self.dismiss()
        else:
            self.toast.show(f"Could not start {record.name}: {res.error}")

    # -- folders -----------------------------------------------------------------
    def open_folder(self, fid: str) -> None:
        if self.model is None or fid not in self.model.folders:
            return
        self.open_folder_id = fid
        self.folder_title.set_text(self.model.folders[fid]["name"])
        child = self.folder_flow.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.folder_flow.remove(child)
            child = nxt
        size = self.app.settings["grid_icon_size"]
        for r in self.model.folder_records(fid):
            t = AppTile(app_key(r.id), r.name, size, app_image(r, size))
            t.connect("clicked", lambda t: self._activate_key(t.key))
            src = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
            src.connect("prepare", lambda _s, _x, _y, rid=r.id: Gdk.ContentProvider.new_for_value(f"folderapp:{fid}:{rid}"))
            src.connect("drag-begin", lambda *_: self.close_folder())
            t.add_controller(src)
            g = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
            g.connect("pressed", lambda _g, _n, _x, _y, tile=t, rid=r.id: popup_menu(tile, [
                ("Open", lambda: self._activate_key(app_key(rid))),
                ("Remove from Folder", lambda: self._remove_from_folder(fid, rid)),
            ]))
            t.add_controller(g)
            self.folder_flow.append(t)
        self.scrim.set_visible(True)
        self.folder_panel.set_visible(True)

    def _remove_from_folder(self, fid: str, app_id: str) -> None:
        self.model.remove_from_folder(fid, app_id)
        self._save_model()
        if fid in self.model.folders:
            self.open_folder(fid)
        else:
            self.close_folder()

    def _rename_folder(self) -> None:
        if self.open_folder_id and self.model and not self.folder_title.has_focus():
            name = self.folder_title.get_text().strip()
            if name and name != self.model.folders[self.open_folder_id]["name"]:
                self.model.rename_folder(self.open_folder_id, name)
                self.tiles.pop(folder_key(self.open_folder_id), None)
                self._save_model()

    def close_folder(self) -> None:
        if not self.open_folder_id:
            return
        self.set_focus(None)  # commits a pending rename via notify::has-focus
        self._rename_folder()
        self.open_folder_id = None
        self.scrim.set_visible(False)
        self.folder_panel.set_visible(False)

    # -- show / hide -------------------------------------------------------------
    def _on_background_click(self, gesture, _n, x, y) -> None:
        picked = self.carousel.pick(x, y, Gtk.PickFlags.DEFAULT)
        while picked is not None and picked is not self.carousel:
            if isinstance(picked, AppTile):
                return
            picked = picked.get_parent()
        self.dismiss()

    def toggle(self) -> None:
        if self.get_visible() and not self._closing:
            self.dismiss()
        else:
            self.open()

    def open(self) -> None:
        if self._release_timer:
            GLib.source_remove(self._release_timer)
            self._release_timer = 0
        if self._closing:
            GLib.source_remove(self._closing)
            self._closing = 0
        if self._dirty or self.model is None:
            self.rebuild()
        self.update_backdrop()
        self.search.set_text("")
        self.selected = -1
        self._update_selection()
        if self.carousel.get_n_pages():
            self.carousel.scroll_to(self.carousel.get_nth_page(0), False)
        self.content.remove_css_class("shown")
        mon = self.app.target_monitor()
        if mon is not None:
            self.fullscreen_on_monitor(mon)
        else:
            self.fullscreen()
        self.app.present_window(self)
        self.search.grab_focus()
        # Next frame: trigger the CSS open transition.
        self.content.add_tick_callback(lambda *_: (self.content.add_css_class("shown"), GLib.SOURCE_REMOVE)[1])

    def _release_gpu_memory(self) -> bool:
        """Free the full-screen surface and renderer caches while hidden.

        A 1920x1080 GL surface plus glyph/texture caches is ~50 MB of private
        memory; re-creating it costs a few ms on the next open.
        """
        self._release_timer = 0
        if not self.get_visible():
            self.unrealize()
            trim_heap()
        return GLib.SOURCE_REMOVE

    def dismiss(self) -> None:
        if not self.get_visible() or self._closing:
            return
        self.close_folder()
        self.content.remove_css_class("shown")
        delay = 0 if not self.app.animations_enabled() else 170

        def hide():
            self._closing = 0
            self.set_visible(False)
            if not self._release_timer:
                self._release_timer = GLib.timeout_add_seconds(RELEASE_AFTER_S, self._release_gpu_memory)
            return GLib.SOURCE_REMOVE

        if delay:
            self._closing = GLib.timeout_add(delay, hide)
        else:
            hide()
