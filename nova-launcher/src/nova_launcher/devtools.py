"""Development helpers: render the real, running windows to PNG files.

Uses GTK's own renderer on the live widget tree (no desktop screen grab), so
screenshots never capture other windows or private content behind Nova.
"""

from __future__ import annotations

import logging
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import GLib, Graphene, Gtk  # noqa: E402

log = logging.getLogger("nova.dev")


def render_to_png(widget: Gtk.Widget, path: Path) -> bool:
    w, h = widget.get_width(), widget.get_height()
    if w <= 0 or h <= 0:
        log.warning("cannot render %s: not allocated", path.name)
        return False
    paintable = Gtk.WidgetPaintable.new(widget)
    snap = Gtk.Snapshot()
    paintable.snapshot(snap, w, h)
    node = snap.to_node()
    if node is None:
        log.warning("cannot render %s: widget drew nothing (visible=%s)", path.name, widget.get_visible())
        return False
    renderer = widget.get_native().get_renderer()
    texture = renderer.render_texture(node, Graphene.Rect().init(0, 0, w, h))
    texture.save_to_png(str(path))
    log.info("wrote %s (%dx%d)", path, w, h)
    return True


def _unsync(window) -> None:
    """Dev only: stop waiting for compositor frame acks so windows still lay
    out and paint while the session is locked (Mutter acks nothing then)."""
    try:
        gi.require_version("GdkX11", "4.0")
        from gi.repository import GdkX11

        surface = window.get_surface()
        if isinstance(surface, GdkX11.X11Surface):
            surface.set_frame_sync_enabled(False)
    except (ImportError, ValueError):
        pass


def screenshot_tour(app, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for w in app.get_windows():
        w.connect("realize", _unsync)
        if w.get_realized():
            _unsync(w)
    app.connect("window-added", lambda _a, w: (w.connect("realize", _unsync),
                                               _unsync(w) if w.get_realized() else None))
    steps = []

    def step(delay_ms, fn):
        steps.append((delay_ms, fn))

    def snap(window, name):
        def go():
            # Render after the next painted frame so layout changes are applied.
            clock = window.get_frame_clock()
            if clock is None:
                render_to_png(window, out_dir / name)
                return
            state = {"h": 0, "done": False}

            def shoot():
                if state["done"]:
                    return GLib.SOURCE_REMOVE
                state["done"] = True
                if state["h"]:
                    clock.disconnect(state["h"])
                render_to_png(window, out_dir / name)
                return GLib.SOURCE_REMOVE

            # Prefer the next painted frame (layout applied); fall back to a
            # timeout because no frames are painted while the screen is locked.
            state["h"] = clock.connect("after-paint", lambda c: GLib.idle_add(shoot))
            GLib.timeout_add(400, shoot)
            window.queue_draw()
        return go

    step(1500, lambda: app.grid.open())
    step(900, snap(app.grid, "launchpad-grid.png"))
    step(350, lambda: app.grid.search.set_text("ter"))
    step(600, snap(app.grid, "launchpad-search.png"))
    step(350, lambda: (app.grid.search.set_text(""), app.grid.dismiss()))
    step(500, lambda: app.quick.open())
    step(400, snap(app.quick, "quick-launcher-empty.png"))
    step(350, lambda: app.quick.entry.set_text("fire"))
    step(500, snap(app.quick, "quick-launcher-fire.png"))
    step(350, lambda: app.quick.entry.set_text("down"))
    step(500, snap(app.quick, "quick-launcher-places.png"))
    step(350, lambda: app.quick.dismiss())
    if app.dock is not None:
        step(400, snap(app.dock, "dock.png"))
    # The island shows the real running apps; render just the pill.
    def island(fn):
        return lambda: fn(app.island) if app.island is not None and app.island.get_visible() else None

    step(300, island(lambda isl: isl.app.windows.refresh()))
    step(500, island(lambda isl: render_to_png(isl.pill, out_dir / "island-compact.png")))
    step(300, island(lambda isl: isl.expand()))
    step(900, island(lambda isl: render_to_png(isl.pill, out_dir / "island-expanded.png")))
    step(300, island(lambda isl: isl.collapse()))
    # Created lazily after startup, so look it up when the step runs.
    # Render the widget even if the user hid it, without touching their setting.
    temp = {}

    def show_widget():
        if app.widget is not None and app.widget.get_visible():
            temp["w"] = app.widget
            return
        from .ui.commands_widget import CommandsWidget

        w = CommandsWidget(app)
        _unsync(w) if w.get_realized() else w.connect("realize", _unsync)
        app._apply_theme()
        w.show_widget()
        temp["w"], temp["own"] = w, True

    step(300, show_widget)
    step(700, lambda: snap(temp["w"], "commands-widget.png")())
    step(400, lambda: temp["w"].destroy() if temp.get("own") else None)
    step(350, lambda: app.show_settings())
    step(900, lambda: snap(app.settings_window, "settings.png")())
    step(200, lambda: app.quit_app())

    def run(i=0):
        if i >= len(steps):
            return GLib.SOURCE_REMOVE
        delay, fn = steps[i]

        def fire():
            try:
                fn()
            except Exception:
                log.exception("screenshot step %d failed", i)
            run(i + 1)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(delay, fire)
        return GLib.SOURCE_REMOVE

    run()
