"""A plain, thread-safe snapshot of one ``.desktop`` application entry.

GIO objects are only touched while building an :class:`AppRecord`; the rest
of Nova (search, cache, UI) works with these immutable records.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

FALLBACK_ICON = "application-x-executable"


@dataclass(frozen=True)
class AppAction:
    id: str
    name: str


@dataclass(frozen=True)
class AppRecord:
    id: str  # desktop-file ID, e.g. "org.gnome.Nautilus.desktop"
    path: str  # absolute path of the winning .desktop file
    name: str
    generic_name: str = ""
    description: str = ""
    executable: str = ""  # basename of argv[0], never the full command line
    icon: str = FALLBACK_ICON  # serialised Gio.Icon (themed name or file path)
    keywords: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    terminal: bool = False
    startup_wm_class: str = ""
    actions: tuple[AppAction, ...] = ()
    mtime: float = 0.0
    source: str = "system"  # system | local | user | flatpak | snap

    def to_dict(self) -> dict:
        d = asdict(self)
        d["keywords"] = list(self.keywords)
        d["categories"] = list(self.categories)
        d["actions"] = [asdict(a) for a in self.actions]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "AppRecord":
        return cls(
            id=str(d["id"]),
            path=str(d["path"]),
            name=str(d["name"]),
            generic_name=str(d.get("generic_name", "")),
            description=str(d.get("description", "")),
            executable=str(d.get("executable", "")),
            icon=str(d.get("icon") or FALLBACK_ICON),
            keywords=tuple(str(k) for k in d.get("keywords", ())),
            categories=tuple(str(c) for c in d.get("categories", ())),
            terminal=bool(d.get("terminal", False)),
            startup_wm_class=str(d.get("startup_wm_class", "")),
            actions=tuple(AppAction(str(a["id"]), str(a["name"])) for a in d.get("actions", ())),
            mtime=float(d.get("mtime", 0.0)),
            source=str(d.get("source", "system")),
        )


def classify_source(path: str) -> str:
    if "/flatpak/" in path:
        return "flatpak"
    if "/snapd/" in path or path.startswith("/snap/"):
        return "snap"
    if path.startswith(os.path.expanduser("~")):
        return "user"
    if path.startswith("/usr/local/"):
        return "local"
    return "system"


def load_entry(path: str, desktop_id: str) -> tuple[AppRecord | None, str]:
    """Parse one desktop file.

    Returns ``(record, reason)``.  ``record`` is None when the entry must not be
    shown; ``reason`` explains why ("hidden", "nodisplay", "invalid", ...), which
    the diagnostics report uses to list broken entries.
    """
    # Read the raw key file first so Hidden/NoDisplay/Type are distinguishable
    # from genuinely malformed files.
    kf = GLib.KeyFile()
    try:
        kf.load_from_file(path, GLib.KeyFileFlags.NONE)
    except GLib.Error as exc:
        return None, f"invalid: {exc.message}"
    group = "Desktop Entry"
    if not kf.has_group(group):
        return None, "invalid: no [Desktop Entry] group"

    def _bool(key: str) -> bool:
        try:
            return kf.get_boolean(group, key)
        except GLib.Error:
            return False

    if _bool("Hidden"):
        # Hidden=true means "deleted": it still shadows lower-precedence files.
        return None, "hidden"
    try:
        entry_type = kf.get_string(group, "Type")
    except GLib.Error:
        entry_type = ""
    if entry_type != "Application":
        return None, f"not an application (Type={entry_type or 'missing'})"

    try:
        info = Gio.DesktopAppInfo.new_from_keyfile(kf)
    except TypeError:  # PyGObject raises when the C constructor returns NULL
        info = None
    if info is None:
        # GLib rejects entries with no Exec, or whose TryExec binary is missing.
        return None, "unavailable: missing Exec or TryExec binary not installed"
    if info.get_nodisplay():
        return None, "nodisplay"
    if not info.should_show():
        return None, "not shown in this desktop (OnlyShowIn/NotShowIn)"

    icon = info.get_icon()
    icon_str = FALLBACK_ICON
    if icon is not None:
        try:
            icon_str = icon.to_string() or FALLBACK_ICON
        except Exception:
            icon_str = FALLBACK_ICON

    exe = info.get_executable() or ""
    actions = []
    for action_id in info.list_actions() or []:
        try:
            actions.append(AppAction(action_id, info.get_action_name(action_id) or action_id))
        except Exception:
            continue

    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        mtime = 0.0

    record = AppRecord(
        id=desktop_id,
        path=path,
        name=info.get_name() or desktop_id.removesuffix(".desktop"),
        generic_name=info.get_generic_name() or "",
        description=info.get_description() or "",
        executable=os.path.basename(exe),
        icon=icon_str,
        keywords=tuple(info.get_keywords() or ()),
        categories=tuple(c for c in (info.get_categories() or "").split(";") if c),
        terminal=_bool("Terminal"),
        startup_wm_class=info.get_startup_wm_class() or "",
        actions=tuple(actions),
        mtime=mtime,
        source=classify_source(path),
    )
    return record, "ok"


@dataclass
class ScanReport:
    """What happened during a scan; consumed by diagnostics and logging."""

    scanned: int = 0
    shown: int = 0
    skipped: dict[str, str] = field(default_factory=dict)  # path -> reason
    shadowed: dict[str, str] = field(default_factory=dict)  # path -> winning path
    duplicates: dict[str, str] = field(default_factory=dict)  # dropped id -> kept id
    errors: list[str] = field(default_factory=list)
