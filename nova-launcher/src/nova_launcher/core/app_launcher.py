"""Launch applications through GIO.

``Gio.DesktopAppInfo`` parses ``Exec`` field codes itself and spawns argv
directly (no shell), handles ``Terminal=true`` and startup notification, and
returns immediately — the launcher never waits for the app to finish starting.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from gi.repository import Gio, GLib

from .desktop_entry import AppRecord

log = logging.getLogger("nova.launch")


@dataclass
class LaunchResult:
    ok: bool
    app_id: str
    error: str = ""


def _app_info_for(record: AppRecord) -> Gio.DesktopAppInfo | None:
    info = None
    if record.path and os.path.exists(record.path):
        try:
            info = Gio.DesktopAppInfo.new_from_filename(record.path)
        except TypeError:  # PyGObject: C constructor returned NULL (binary gone)
            return None
    if info is None:
        try:
            info = Gio.DesktopAppInfo.new(record.id)
        except TypeError:
            info = None
    return info


def launch(
    record: AppRecord,
    context: Gio.AppLaunchContext | None = None,
    uris: list[str] | None = None,
    action: str | None = None,
) -> LaunchResult:
    """Launch ``record`` (or one of its desktop actions).  Never raises."""
    info = _app_info_for(record)
    if info is None:
        msg = f"{record.name} is no longer installed (or its desktop file is invalid)"
        log.warning("launch failed for %s: desktop file unavailable", record.id)
        return LaunchResult(False, record.id, msg)
    try:
        if action:
            if action not in (info.list_actions() or []):
                return LaunchResult(False, record.id, f"{record.name} has no action '{action}'")
            info.launch_action(action, context)
        else:
            # Run the app in a scope of its own if systemd is managing the
            # session, so it outlives the launcher and is not grouped with it.
            info.launch_uris_as_manager(
                uris or [],
                context,
                GLib.SpawnFlags.SEARCH_PATH | GLib.SpawnFlags.DO_NOT_REAP_CHILD,
                None,
                None,
                _on_pid,
                record.id,
            )
    except GLib.Error as exc:
        # Do not log URIs/arguments: they may contain private paths.
        log.warning("launch failed for %s: %s", record.id, exc.message)
        return LaunchResult(False, record.id, exc.message)
    except Exception as exc:  # defensive: launching must never crash the UI
        log.exception("unexpected launch error for %s", record.id)
        return LaunchResult(False, record.id, str(exc))
    log.info("launched %s", record.id)
    return LaunchResult(True, record.id)


def _on_pid(_info, pid, app_id):
    # DO_NOT_REAP_CHILD + a child watch prevents zombies without blocking.
    try:
        GLib.child_watch_add(GLib.PRIORITY_DEFAULT_IDLE, pid, lambda *_: None)
    except Exception:
        pass
    _place_in_scope(pid, app_id)


def _place_in_scope(pid: int, app_id: str) -> None:
    """Best effort: move the child into its own systemd user scope.

    GNOME Shell does the same (app-gnome-*.scope).  Without this, apps would
    share Nova's cgroup and could be affected by its resource accounting.
    Asynchronous; failure is harmless.
    """
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error:
        return
    unit = "app-nova-{}-{}.scope".format(
        "".join(c if c.isalnum() else "_" for c in app_id.removesuffix(".desktop"))[:60], pid
    )
    params = GLib.Variant(
        "(ssa(sv)a(sa(sv)))",
        (unit, "fail", [("PIDs", GLib.Variant("au", [pid])), ("CollectMode", GLib.Variant("s", "inactive-or-failed"))], []),
    )
    bus.call(
        "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
        "org.freedesktop.systemd1.Manager", "StartTransientUnit",
        params, None, Gio.DBusCallFlags.NO_AUTO_START, 2000, None,
        lambda b, res: _finish_scope(b, res, unit),
    )


def _finish_scope(bus, res, unit):
    try:
        bus.call_finish(res)
    except GLib.Error as exc:
        log.debug("could not create %s: %s", unit, exc.message)
