"""User-level desktop integration: autostart and GNOME keyboard shortcuts.

Every change here is per-user, opt-in and reversible:

* autostart = one file, ``~/.config/autostart/nova-launcher.desktop``
* shortcuts = GNOME *custom* keybindings under a Nova-owned path; other custom
  shortcuts are never modified, and removal only deletes Nova's own entries.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from .. import APP_ID, paths

log = logging.getLogger("nova.integration")

AUTOSTART_NAME = "nova-launcher.desktop"
MEDIA_KEYS = "org.gnome.settings-daemon.plugins.media-keys"
CUSTOM_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"
KEYBINDING_BASE = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/nova-launcher-{}/"

AUTOSTART_ENTRY = """[Desktop Entry]
Type=Application
Name=Nova Launcher
Comment=Start Nova Launcher in the background
Exec=nova-launcher --background
Icon={app_id}
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=2
NoDisplay=true
"""


def session_type() -> str:
    return os.environ.get("XDG_SESSION_TYPE", "unknown")


def is_wayland() -> bool:
    return session_type() == "wayland" or bool(os.environ.get("WAYLAND_DISPLAY")) and not os.environ.get("DISPLAY")


def autostart_path() -> Path:
    return paths.config_dir().parent / "autostart" / AUTOSTART_NAME


def autostart_enabled() -> bool:
    return autostart_path().is_file()


def set_autostart(enabled: bool) -> None:
    p = autostart_path()
    if enabled:
        paths.atomic_write(p, AUTOSTART_ENTRY.format(app_id=APP_ID))
        log.info("autostart enabled: %s", p)
    elif p.exists():
        p.unlink()
        log.info("autostart disabled")


# -- keyboard shortcuts -------------------------------------------------------
def _gsettings(*args: str) -> tuple[int, str]:
    if not shutil.which("gsettings"):
        return 127, "gsettings not installed"
    p = subprocess.run(["gsettings", *args], capture_output=True, text=True, timeout=10)
    return p.returncode, (p.stdout + p.stderr).strip()


def _parse_strv(text: str) -> list[str]:
    text = text.strip()
    if text.startswith("@as"):
        text = text[3:].strip()
    text = text.strip("[]").strip()
    return [s.strip().strip("'\"") for s in text.split(",") if s.strip()]


def _strv(items: list[str]) -> str:
    if not items:
        return "@as []"
    return "[" + ", ".join(f"'{i}'" for i in items) + "]"


def find_conflicts(accel: str) -> list[str]:
    """Return GNOME keybinding keys already using ``accel`` (best effort)."""
    norm = accel.lower().replace(" ", "")
    hits = []
    for schema in ("org.gnome.desktop.wm.keybindings", "org.gnome.shell.keybindings",
                   "org.gnome.mutter.keybindings", MEDIA_KEYS, "org.gnome.mutter.wayland.keybindings"):
        rc, out = _gsettings("list-recursively", schema)
        if rc != 0:
            continue
        for line in out.splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3 and norm in parts[2].lower().replace(" ", ""):
                hits.append(f"{parts[0]} {parts[1]}")
    rc, out = _gsettings("get", MEDIA_KEYS, "custom-keybindings")
    for path in _parse_strv(out) if rc == 0 else []:
        if "nova-launcher" in path:
            continue
        rc2, b = _gsettings("get", f"{CUSTOM_SCHEMA}:{path}", "binding")
        if rc2 == 0 and norm in b.lower().replace(" ", ""):
            hits.append(f"custom shortcut {path}")
    return hits


def install_shortcut(name: str, accel: str, action: str) -> str:
    """Add/replace Nova's custom shortcut ``name``; returns a status message."""
    path = KEYBINDING_BASE.format(name)
    command = f"gapplication action {APP_ID} {action}"
    rc, out = _gsettings("get", MEDIA_KEYS, "custom-keybindings")
    if rc != 0:
        return f"cannot read GNOME shortcuts: {out}"
    current = _parse_strv(out)
    if path not in current:
        current.append(path)
        rc, out = _gsettings("set", MEDIA_KEYS, "custom-keybindings", _strv(current))
        if rc != 0:
            return f"failed: {out}"
    for key, value in (("name", f"Nova Launcher: {name}"), ("command", command), ("binding", accel)):
        rc, out = _gsettings("set", f"{CUSTOM_SCHEMA}:{path}", key, value)
        if rc != 0:
            return f"failed: {out}"
    return f"{accel} -> {command}"


def remove_shortcuts() -> str:
    rc, out = _gsettings("get", MEDIA_KEYS, "custom-keybindings")
    if rc != 0:
        return out
    current = _parse_strv(out)
    ours = [p for p in current if "/nova-launcher-" in p]
    for p in ours:
        for key in ("name", "command", "binding"):
            _gsettings("reset", f"{CUSTOM_SCHEMA}:{p}", key)
    _gsettings("set", MEDIA_KEYS, "custom-keybindings", _strv([p for p in current if p not in ours]))
    return f"removed {len(ours)} Nova shortcut(s)"
