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


def screenshot_tour(app, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
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
            h = [0]

            def after(c):
                c.disconnect(h[0])
                GLib.idle_add(lambda: (render_to_png(window, out_dir / name), GLib.SOURCE_REMOVE)[1])

            h[0] = clock.connect("after-paint", after)
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
