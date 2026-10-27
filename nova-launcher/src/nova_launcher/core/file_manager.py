"""File-manager shortcuts: open folders fast, and fail visibly when slow.

Folders are opened through the standard ``org.freedesktop.FileManager1``
D-Bus interface with an explicit timeout.  If the file manager does not
answer in time (the "Files takes minutes to open" symptom), Nova falls back
to launching the default ``inode/directory`` handler and reports the delay
instead of hanging.  Nothing here walks the filesystem.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable

from gi.repository import Gio, GLib

log = logging.getLogger("nova.files")

FM_BUS = "org.freedesktop.FileManager1"
FM_PATH = "/org/freedesktop/FileManager1"


@dataclass(frozen=True)
class Place:
    id: str
    name: str
    uri: str
    icon: str
    kind: str = "folder"  # folder | mount | recent


_SPECIAL = [
    (GLib.UserDirectory.DIRECTORY_DOWNLOAD, "Downloads", "folder-download"),
    (GLib.UserDirectory.DIRECTORY_DOCUMENTS, "Documents", "folder-documents"),
    (GLib.UserDirectory.DIRECTORY_PICTURES, "Pictures", "folder-pictures"),
    (GLib.UserDirectory.DIRECTORY_MUSIC, "Music", "folder-music"),
    (GLib.UserDirectory.DIRECTORY_VIDEOS, "Videos", "folder-videos"),
    (GLib.UserDirectory.DIRECTORY_DESKTOP, "Desktop", "user-desktop"),
]


def standard_places() -> list[Place]:
    """Home + XDG user dirs.  Only reads the user-dirs config, no stat()s."""
    home = GLib.get_home_dir()
    places = [Place("home", "Home", Gio.File.new_for_path(home).get_uri(), "user-home")]
    seen = {home}
    for kind, fallback, icon in _SPECIAL:
        path = GLib.get_user_special_dir(kind)
        if not path or path in seen:
            continue
        seen.add(path)
        places.append(Place(f"xdg-{fallback.lower()}", os.path.basename(path) or fallback,
                            Gio.File.new_for_path(path).get_uri(), icon))
    places.append(Place("trash", "Trash", "trash:///", "user-trash"))
    return places


def mounted_places(monitor: Gio.VolumeMonitor | None = None) -> list[Place]:
    """Mounted drives from the volume monitor's in-memory list (non-blocking).

    Network mounts are listed but never accessed, so an unreachable server
    cannot stall the launcher.
    """
    out = []
    try:
        monitor = monitor or Gio.VolumeMonitor.get()
        for m in monitor.get_mounts():
            if m.is_shadowed():
                continue
            root = m.get_default_location() or m.get_root()
            icon = m.get_symbolic_icon() or m.get_icon()
            out.append(Place(f"mount-{m.get_uuid() or m.get_name()}", m.get_name(), root.get_uri(),
                             icon.to_string() if icon else "drive-harddisk", "mount"))
    except Exception as exc:
        log.debug("listing mounts failed: %s", exc)
    return out


def recent_files(limit: int = 10) -> list[Place]:
    """Recently used files from the GTK recent-files store (no disk walk)."""
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        items = Gtk.RecentManager.get_default().get_items()
    except Exception:
        return []
    items.sort(key=lambda i: i.get_modified().to_unix(), reverse=True)
    out = []
    for i in items[:limit]:
        gicon = i.get_gicon()
        out.append(Place(f"recent-{i.get_uri()}", i.get_display_name(), i.get_uri(),
                         gicon.to_string() if gicon else "text-x-generic", "recent"))
    return out


def default_file_manager() -> Gio.AppInfo | None:
    return Gio.AppInfo.get_default_for_type("inode/directory", False)


class FileManager:
    def __init__(self, timeout_ms: int = 4000, on_slow: Callable[[str], None] | None = None):
        self.timeout_ms = timeout_ms
        self.on_slow = on_slow or (lambda msg: None)

    def open(self, uri: str, startup_id: str = "", done: Callable[[bool, str], None] | None = None) -> None:
        """Open ``uri`` in the file manager.  Returns immediately."""
        done = done or (lambda ok, msg: None)
        if uri.startswith("trash:") or "://" in uri and not uri.startswith("file://"):
            # FileManager1 is file:// oriented; use the default handler.
            self._launch_default(uri, done)
            return
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as exc:
            self._launch_default(uri, done, f"session bus unavailable: {exc.message}")
            return

        started = GLib.get_monotonic_time()

        def on_reply(conn, res):
            ms = (GLib.get_monotonic_time() - started) / 1000
            try:
                conn.call_finish(res)
            except GLib.Error as exc:
                timed_out = exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.TIMED_OUT)
                why = (f"file manager did not respond within {self.timeout_ms / 1000:.0f}s"
                       if timed_out else f"file manager D-Bus error: {exc.message}")
                self.on_slow(why)
                fm = default_file_manager()
                if timed_out and fm is not None and fm.get_id() == "org.gnome.Nautilus.desktop":
                    # The default handler *is* the stuck process; launching it
                    # again would only queue another request behind it.
                    log.warning("%s; not retrying through the same file manager", why)
                    done(False, why)
                    return
                log.warning("%s; falling back to default handler", why)
                self._launch_default(uri, done, why)
                return
            log.info("file manager opened folder in %.0f ms", ms)
            if ms > 2000:
                self.on_slow(f"file manager took {ms / 1000:.1f}s to respond")
            done(True, "")

        bus.call(
            FM_BUS, FM_PATH, FM_BUS, "ShowFolders",
            GLib.Variant("(ass)", ([uri], startup_id)),
            None, Gio.DBusCallFlags.NONE, self.timeout_ms, None, on_reply,
        )

    def _launch_default(self, uri: str, done, reason: str = "") -> None:
        try:
            ok = Gio.AppInfo.launch_default_for_uri(uri, None)
            done(bool(ok), reason)
        except GLib.Error as exc:
            log.warning("cannot open folder: %s", exc.message)
            done(False, exc.message)
