"""Small shared widgets and helpers."""

from __future__ import annotations

import functools
import os
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from ..core.desktop_entry import FALLBACK_ICON, AppRecord  # noqa: E402

_CSS_LOADED = False


def load_css() -> None:
    global _CSS_LOADED
    if _CSS_LOADED:
        return
    provider = Gtk.CssProvider()
    provider.load_from_path(str(Path(__file__).with_name("styles.css")))
    display = Gdk.Display.get_default()
    if display is not None:
        Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _CSS_LOADED = True


@functools.lru_cache(maxsize=1024)
def gicon_for(icon: str) -> Gio.Icon:
    """Resolve a serialised icon, falling back for broken/missing icons.

    A missing icon is *not* a launch failure: the app is still shown and
    launchable, it just gets the generic executable icon.  Bounded cache.
    """
    fallback = Gio.ThemedIcon.new_with_default_fallbacks(FALLBACK_ICON)
    if not icon:
        return fallback
    if icon.startswith("/"):
        return Gio.FileIcon.new(Gio.File.new_for_path(icon)) if os.path.isfile(icon) else fallback
    try:
        gicon = Gio.Icon.new_for_string(icon)
    except GLib.Error:
        return fallback
    display = Gdk.Display.get_default()
    if isinstance(gicon, Gio.ThemedIcon) and display is not None:
        theme = Gtk.IconTheme.get_for_display(display)
        if not theme.has_gicon(gicon):
            # Try "foo-bar" -> "foo" style fallbacks before giving up.
            names = list(gicon.get_names())
            fb = Gio.ThemedIcon.new_from_names(names + [FALLBACK_ICON])
            return fb if any(theme.has_icon(n) for n in names) else fallback
    return gicon


def themed_candidates(record: AppRecord) -> list[str]:
    """Icon-theme names to try for an app (pure; tested).

    Snap/Flatpak entries often point at a bundled PNG; themes such as WhiteSur
    ship a nicer icon under the app's executable or desktop-ID name.
    """
    stem = record.id.removesuffix(".desktop")
    names = []
    if record.icon and not record.icon.startswith("/"):
        names.append(record.icon)
    if "_" in stem:  # snap: "firefox_firefox"
        names.append(stem.split("_", 1)[1])
    names += [record.executable, stem, stem.split(".")[-1].lower(), record.startup_wm_class.lower()]
    return [n for n in dict.fromkeys(names) if n]


@functools.lru_cache(maxsize=1024)
def record_gicon(record: AppRecord) -> Gio.Icon:
    """Best icon for an app: prefer the icon theme over bundled image files."""
    display = Gdk.Display.get_default()
    if record.icon.startswith("/") and display is not None:
        theme = Gtk.IconTheme.get_for_display(display)
        for name in themed_candidates(record):
            if theme.has_icon(name):
                return Gio.ThemedIcon.new(name)
    return gicon_for(record.icon)


def app_image(record: AppRecord | None, size: int, icon: str | None = None) -> Gtk.Image:
    if icon is None and record is not None:
        gicon = record_gicon(record)
    else:
        gicon = gicon_for(icon if icon is not None else "")
    img = Gtk.Image.new_from_gicon(gicon)
    img.set_pixel_size(size)
    return img


class AppTile(Gtk.Button):
    """Icon + name tile used by the Launchpad grid and folders."""

    def __init__(self, key: str, title: str, icon_size: int, child_icon: Gtk.Widget):
        super().__init__()
        self.key = key
        self.add_css_class("nova-tile")
        self.set_focus_on_click(False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_halign(Gtk.Align.CENTER)
        child_icon.set_halign(Gtk.Align.CENTER)
        box.append(child_icon)
        label = Gtk.Label(label=title, max_width_chars=12, ellipsize=3, justify=Gtk.Justification.CENTER)
        label.set_lines(2)
        label.set_wrap(True)
        label.add_css_class("nova-app-name")
        box.append(label)
        self.set_child(box)
        self.set_size_request(icon_size + 48, icon_size + 64)
        self.set_tooltip_text(title)

    def set_selected(self, selected: bool) -> None:
        if selected:
            self.add_css_class("nova-selected")
        else:
            self.remove_css_class("nova-selected")


def folder_icon(records: list[AppRecord], size: int) -> Gtk.Widget:
    """2x2 mini-icon preview for a folder tile."""
    grid = Gtk.Grid(row_spacing=4, column_spacing=4, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
    mini = max(12, (size - 24) // 2)
    for i, r in enumerate(records[:4]):
        grid.attach(app_image(r, mini), i % 2, i // 2, 1, 1)
    frame = Gtk.Box()
    frame.add_css_class("nova-folder-icon")
    frame.set_size_request(size, size)
    grid.set_hexpand(True)
    frame.append(grid)
    return frame


def popup_menu(parent: Gtk.Widget, entries: list[tuple[str, object] | None]) -> Gtk.Popover:
    """Show a simple context menu.  ``None`` entries become separators."""
    pop = Gtk.Popover(has_arrow=True)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    for entry in entries:
        if entry is None:
            box.append(Gtk.Separator())
            continue
        label, callback = entry
        btn = Gtk.Button(label=label)
        btn.add_css_class("flat")
        btn.get_child().set_xalign(0)

        def on_click(_b, cb=callback):
            pop.popdown()
            GLib.idle_add(lambda: (cb(), GLib.SOURCE_REMOVE)[1])

        btn.connect("clicked", on_click)
        box.append(btn)
    pop.set_child(box)
    pop.set_parent(parent)
    pop.connect("closed", lambda p: GLib.idle_add(lambda: (p.unparent(), GLib.SOURCE_REMOVE)[1]))
    pop.popup()
    return pop


class Toast(Gtk.Revealer):
    """Tiny in-window notification (avoids depending on AdwToastOverlay)."""

    def __init__(self):
        super().__init__(transition_type=Gtk.RevealerTransitionType.CROSSFADE,
                         halign=Gtk.Align.CENTER, valign=Gtk.Align.END, margin_bottom=24)
        self._label = Gtk.Label(wrap=True, max_width_chars=60)
        self._label.add_css_class("nova-toast")
        self.set_child(self._label)
        self.set_can_target(False)
        self._timeout = 0

    def show(self, text: str, seconds: float = 4.0) -> None:
        self._label.set_text(text)
        self.set_reveal_child(True)
        if self._timeout:
            GLib.source_remove(self._timeout)
        self._timeout = GLib.timeout_add(int(seconds * 1000), self._hide)

    def _hide(self):
        self._timeout = 0
        self.set_reveal_child(False)
        return GLib.SOURCE_REMOVE
