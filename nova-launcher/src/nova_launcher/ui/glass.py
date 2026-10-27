"""Frosted-glass container (see wallpaper.py for why this works without
compositor blur).  Paints, inside rounded corners: the slice of the blurred
wallpaper that lies behind it on screen, a light/dark tint, and a hairline
edge highlight — then its children on top.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, Graphene, Gsk, Gtk  # noqa: E402

if TYPE_CHECKING:
    from ..application import NovaApplication


def _rgba(r, g, b, a) -> Gdk.RGBA:
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = r, g, b, a
    return c


# (tint without wallpaper, tint over wallpaper, edge) per theme and strength.
_TINTS = {
    (True, "normal"): (_rgba(0.13, 0.13, 0.15, 0.86), _rgba(0.10, 0.10, 0.12, 0.38), _rgba(1, 1, 1, 0.16)),
    (True, "strong"): (_rgba(0.13, 0.13, 0.15, 0.94), _rgba(0.10, 0.10, 0.12, 0.62), _rgba(1, 1, 1, 0.14)),
    (False, "normal"): (_rgba(0.97, 0.97, 0.98, 0.88), _rgba(1, 1, 1, 0.42), _rgba(1, 1, 1, 0.65)),
    (False, "strong"): (_rgba(0.97, 0.97, 0.98, 0.95), _rgba(1, 1, 1, 0.70), _rgba(1, 1, 1, 0.65)),
}


class GlassBox(Gtk.Box):
    def __init__(self, app: "NovaApplication", radius: float = 20, strength: str = "normal", **kw):
        super().__init__(**kw)
        self.app = app
        self.radius = radius
        self.strength = strength
        self.add_css_class("nova-glass-surface")

    def do_snapshot(self, snap: Gtk.Snapshot) -> None:
        # Snapshot coordinates start at the *content* box (inside CSS padding),
        # but the CSS shadow surrounds the *border* box, so paint the glass over
        # the border box, which compute_bounds() reports in our own coordinates.
        ok, box = self.compute_bounds(self)
        if not ok or box.size.width <= 0 or box.size.height <= 0:
            return
        bx, by, w, h = box.origin.x, box.origin.y, box.size.width, box.size.height
        rect = Graphene.Rect().init(bx, by, w, h)
        outline = Gsk.RoundedRect()
        outline.init_from_rect(rect, self.radius)
        dark = self.app.is_dark()
        plain, over, edge = _TINTS[(dark, self.strength)]

        snap.push_rounded_clip(outline)
        backdrop = self.app.glass_backdrop(self)
        if backdrop is not None:
            texture, x, y = backdrop  # our border-box top-left in texture coordinates
            snap.append_texture(texture, Graphene.Rect().init(bx - x, by - y, texture.get_width(),
                                                              texture.get_height()))
            snap.append_color(over, rect)
        else:
            snap.append_color(plain, rect)
        # Soft top sheen, like light catching the edge of glass.
        sheen_h = min(h, 28)
        stops = [Gsk.ColorStop(), Gsk.ColorStop()]
        stops[0].offset, stops[0].color = 0.0, _rgba(1, 1, 1, 0.10 if dark else 0.30)
        stops[1].offset, stops[1].color = 1.0, _rgba(1, 1, 1, 0.0)
        snap.append_linear_gradient(Graphene.Rect().init(bx, by, w, sheen_h), Graphene.Point().init(bx, by),
                                    Graphene.Point().init(bx, by + sheen_h), stops)
        snap.pop()
        snap.append_border(outline, [1, 1, 1, 1], [edge] * 4)
        Gtk.Box.do_snapshot(self, snap)
