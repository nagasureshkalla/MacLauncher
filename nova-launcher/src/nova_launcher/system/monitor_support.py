"""Monitor selection and geometry for multi-monitor placement."""

from __future__ import annotations

from dataclasses import dataclass

import gi

gi.require_version("Gdk", "4.0")
from gi.repository import Gdk  # noqa: E402


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class MonitorInfo:
    connector: str
    geometry: Rect
    workarea: Rect
    primary: bool


def _rect(r) -> Rect:
    return Rect(r.x, r.y, r.width, r.height)


def list_monitors(display: Gdk.Display | None = None) -> list[MonitorInfo]:
    display = display or Gdk.Display.get_default()
    if display is None:
        return []
    primary = None
    x11_monitor = None
    try:
        gi.require_version("GdkX11", "4.0")
        from gi.repository import GdkX11

        if isinstance(display, GdkX11.X11Display):
            primary = display.get_primary_monitor()
            x11_monitor = GdkX11.X11Monitor
    except (ImportError, ValueError):
        pass
    monitors = display.get_monitors()
    out = []
    for i in range(monitors.get_n_items()):
        m = monitors.get_item(i)
        geo = _rect(m.get_geometry())
        work = geo
        if x11_monitor is not None and isinstance(m, x11_monitor):
            # Excludes panels/struts such as the top bar and Ubuntu Dock.
            work = _rect(m.get_workarea())
        out.append(MonitorInfo(m.get_connector() or f"monitor-{i}", geo, work,
                               primary is not None and m == primary))
    if out and not any(m.primary for m in out):
        out[0] = MonitorInfo(out[0].connector, out[0].geometry, out[0].workarea, True)
    return out


def choose_monitor(preference: str, monitors: list[MonitorInfo]) -> MonitorInfo | None:
    """``preference`` is 'primary' or a connector name; falls back to primary."""
    if not monitors:
        return None
    for m in monitors:
        if preference not in ("", "primary") and m.connector == preference:
            return m
    for m in monitors:
        if m.primary:
            return m
    return monitors[0]


def gdk_monitor_for(info: MonitorInfo, display: Gdk.Display | None = None):
    display = display or Gdk.Display.get_default()
    monitors = display.get_monitors()
    for i in range(monitors.get_n_items()):
        m = monitors.get_item(i)
        if (m.get_connector() or f"monitor-{i}") == info.connector:
            return m
    return None


def dock_origin(position: str, area: Rect, width: int, height: int, margin: int = 0) -> tuple[int, int]:
    """Top-left corner for a dock window of ``width`` x ``height`` on ``area``."""
    if position == "left":
        return area.x + margin, area.y + (area.height - height) // 2
    if position == "right":
        return area.x + area.width - width - margin, area.y + (area.height - height) // 2
    return area.x + (area.width - width) // 2, area.y + area.height - height - margin


def centered(area: Rect, width: int, height: int, vertical_bias: float = 0.3) -> tuple[int, int]:
    """Centered horizontally, placed at ``vertical_bias`` of the free height."""
    return area.x + (area.width - width) // 2, area.y + int((area.height - height) * vertical_bias)
