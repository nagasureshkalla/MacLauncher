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
Exec={exe} --background
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
        # Absolute path: ~/.local/bin is not guaranteed to be on PATH at login.
        exe = shutil.which("nova-launcher") or "nova-launcher"
        paths.atomic_write(p, AUTOSTART_ENTRY.format(app_id=APP_ID, exe=exe))
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


def regrab_shortcuts(delay: float = 0.5) -> None:
    """Make gsd-media-keys re-grab Nova's keys (clear and re-set each binding)."""
    import time

    rc, out = _gsettings("get", MEDIA_KEYS, "custom-keybindings")
    for path in _parse_strv(out) if rc == 0 else []:
        if "/nova-launcher-" not in path:
            continue
        schema = f"{CUSTOM_SCHEMA}:{path}"
        rc, binding = _gsettings("get", schema, "binding")
        binding = binding.strip().strip("'")
        if rc == 0 and binding:
            _gsettings("set", schema, "binding", "")
            time.sleep(delay)
            _gsettings("set", schema, "binding", binding)


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


# -- "make Nova the default launcher" ---------------------------------------------
SHELL_KEYS = "org.gnome.shell.keybindings"
WM_KEYS = "org.gnome.desktop.wm.keybindings"
UBUNTU_DOCK = "ubuntu-dock@ubuntu.com"
DEFAULT_QUICK = "<Super>space"
DEFAULT_GRID = "<Super>a"


def backup_path() -> Path:
    return paths.state_dir() / "gnome-defaults-backup.json"


def _run(cmd: list[str]) -> tuple[int, str]:
    if not shutil.which(cmd[0]):
        return 127, f"{cmd[0]} not installed"
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    return p.returncode, (p.stdout + p.stderr).strip()


def _extension_enabled(uuid: str) -> bool:
    rc, out = _run(["gnome-extensions", "list", "--enabled"])
    return rc == 0 and uuid in out.split()


def _without(values: list[str], accel: str) -> list[str]:
    return [v for v in values if v.lower() != accel.lower()]


def set_as_default(settings) -> list[str]:
    """Make Nova the launcher: Super+A grid, Super+Space search, dock, autostart.

    Every original value is saved to a backup file first (kept from the very
    first run, so repeating this never loses the real originals);
    ``restore_default()`` puts everything back.
    """
    import json

    log_lines: list[str] = []
    bp = backup_path()
    if not bp.exists():
        backup = {
            "toggle_application_view": _gsettings("get", SHELL_KEYS, "toggle-application-view")[1],
            "switch_input_source": _gsettings("get", WM_KEYS, "switch-input-source")[1],
            "switch_input_source_backward": _gsettings("get", WM_KEYS, "switch-input-source-backward")[1],
            "ubuntu_dock_enabled": _extension_enabled(UBUNTU_DOCK),
            "autostart": autostart_enabled(),
            "nova": {k: settings[k] for k in ("shortcut_quick", "shortcut_grid", "dock_enabled",
                                              "dock_autohide", "dock_reserve_space")},
        }
        paths.atomic_write(bp, json.dumps(backup, indent=2))
        log_lines.append(f"saved original GNOME values to {bp}")

    # 1. Free Super+A from GNOME's app grid and Super+Space from layout switching.
    _, cur = _gsettings("get", SHELL_KEYS, "toggle-application-view")
    _gsettings("set", SHELL_KEYS, "toggle-application-view", _strv(_without(_parse_strv(cur), DEFAULT_GRID)))
    for key in ("switch-input-source", "switch-input-source-backward"):
        _, cur = _gsettings("get", WM_KEYS, key)
        accel = DEFAULT_QUICK if key == "switch-input-source" else "<Shift><Super>space"
        _gsettings("set", WM_KEYS, key, _strv(_without(_parse_strv(cur), accel)))
    log_lines.append("GNOME: Super+A no longer opens the stock app grid; Super+Space no longer switches layouts")
    # GNOME Shell releases its key grabs asynchronously; if gsd-media-keys tries
    # to grab before that, it logs "Failed to grab accelerator" and the
    # shortcut silently does nothing.  Wait, and re-assert the bindings below.
    import time

    time.sleep(1.0)

    # 2. Nova shortcuts.
    settings.set("shortcut_grid", DEFAULT_GRID)
    settings.set("shortcut_quick", DEFAULT_QUICK)
    log_lines.append("shortcut " + install_shortcut("grid", DEFAULT_GRID, "toggle-grid"))
    log_lines.append("shortcut " + install_shortcut("quick", DEFAULT_QUICK, "toggle-quick"))
    regrab_shortcuts()

    # 3. Nova's dock replaces Ubuntu Dock, reserving space like Ubuntu Dock did.
    settings.set("dock_enabled", True)
    settings.set("dock_autohide", False)
    settings.set("dock_reserve_space", True)
    if _extension_enabled(UBUNTU_DOCK):
        rc, out = _run(["gnome-extensions", "disable", UBUNTU_DOCK])
        log_lines.append("Ubuntu Dock disabled" if rc == 0 else f"could not disable Ubuntu Dock: {out}")

    # 4. Start at login.
    set_autostart(True)
    log_lines.append(f"autostart enabled ({autostart_path()})")
    return log_lines


def restore_default(settings) -> list[str]:
    """Undo ``set_as_default`` using the saved backup."""
    import json

    bp = backup_path()
    try:
        backup = json.loads(bp.read_text())
    except (OSError, ValueError):
        return [f"no backup found at {bp}; nothing to restore"]
    out = [remove_shortcuts()]
    for key, schema, bkey in (("toggle-application-view", SHELL_KEYS, "toggle_application_view"),
                              ("switch-input-source", WM_KEYS, "switch_input_source"),
                              ("switch-input-source-backward", WM_KEYS, "switch_input_source_backward")):
        val = backup.get(bkey, "")
        rc, msg = _gsettings("set", schema, key, val) if val else _gsettings("reset", schema, key)
        if rc != 0:
            _gsettings("reset", schema, key)
    out.append("GNOME keybindings restored (Super+A app grid, Super+Space input switching)")
    if backup.get("ubuntu_dock_enabled") and not _extension_enabled(UBUNTU_DOCK):
        rc, msg = _run(["gnome-extensions", "enable", UBUNTU_DOCK])
        out.append("Ubuntu Dock re-enabled" if rc == 0 else f"could not re-enable Ubuntu Dock: {msg}")
    set_autostart(bool(backup.get("autostart")))
    out.append(f"autostart {'kept' if backup.get('autostart') else 'disabled'}")
    for k, v in (backup.get("nova") or {}).items():
        try:
            settings.set(k, v)
        except (KeyError, ValueError):
            pass
    bp.unlink()
    out.append("backup consumed; GNOME is back to how it was")
    return out
