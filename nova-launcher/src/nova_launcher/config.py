"""Persistent, validated user settings.

Settings live in ``~/.config/nova-launcher/settings.json``.  Every value is
checked against ``SCHEMA``; invalid values fall back to their default and a
file that cannot be parsed at all is moved aside (never deleted) so the
launcher always starts.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import paths

log = logging.getLogger("nova.config")

CONFIG_VERSION = 1


@dataclass(frozen=True)
class Option:
    default: Any
    kind: type | tuple[type, ...]
    choices: tuple | None = None
    min: float | None = None
    max: float | None = None
    description: str = ""

    def validate(self, value: Any) -> Any:
        """Return a valid value, or raise ValueError."""
        if self.kind is float and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if self.kind is int and isinstance(value, bool):
            raise ValueError("bool is not int")
        if not isinstance(value, self.kind):
            raise ValueError(f"expected {self.kind}, got {type(value).__name__}")
        if self.choices is not None and value not in self.choices:
            raise ValueError(f"{value!r} not in {self.choices}")
        if self.min is not None and value < self.min:
            raise ValueError(f"{value} < {self.min}")
        if self.max is not None and value > self.max:
            raise ValueError(f"{value} > {self.max}")
        if isinstance(value, list):
            if not all(isinstance(v, str) for v in value):
                raise ValueError("list items must be strings")
            value = list(dict.fromkeys(value))  # de-duplicate, keep order
        return value


DEFAULT_PINNED = [
    "org.gnome.Nautilus.desktop",
    "firefox_firefox.desktop",
    "google-chrome.desktop",
    "org.gnome.Terminal.desktop",
    "code_code.desktop",
    "code.desktop",
    "org.gnome.Settings.desktop",
]

SCHEMA: dict[str, Option] = {
    # Dock
    "dock_enabled": Option(True, bool, description="Show the floating dock"),
    "dock_position": Option("bottom", str, choices=("bottom", "left", "right")),
    "dock_icon_size": Option(48, int, min=24, max=96),
    "dock_magnification": Option(1.6, float, min=1.0, max=2.5),
    "dock_spacing": Option(6, int, min=0, max=24),
    "dock_autohide": Option(True, bool, description="Hide until the pointer touches the screen edge"),
    "dock_reserve_space": Option(True, bool, description="Maximized windows stop above the dock"),
    "dock_show_running": Option(True, bool),
    "dock_show_recent": Option(True, bool),
    "dock_recent_count": Option(3, int, min=0, max=8),
    "dock_monitor": Option("primary", str, description="'primary' or a connector name such as HDMI-0"),
    "pinned_apps": Option(list(DEFAULT_PINNED), list),
    # Dynamic Island
    "island_enabled": Option(True, bool, description="Running apps in a pill at the top of the screen"),
    "island_autohide": Option(True, bool, description="Slide away; touch the area under the top bar to show it"),
    "island_reserve_space": Option(True, bool, description="Maximized windows stop below the island (when not hiding)"),
    "island_monitor": Option("primary", str),
    # Commands widget
    "widget_enabled": Option(True, bool, description="Show the commands widget on the desktop"),
    "widget_position": Option("top-right", str, choices=("top-right", "top-left", "bottom-right", "bottom-left")),
    "widget_collapsed": Option(False, bool),
    "widget_monitor": Option("primary", str),
    # Launchpad grid
    "grid_columns": Option(7, int, min=3, max=12),
    "grid_rows": Option(5, int, min=2, max=8),
    "grid_icon_size": Option(72, int, min=32, max=128),
    "grid_order": Option([], list, description="Custom app/folder order"),
    # Appearance
    "theme": Option("system", str, choices=("system", "light", "dark")),
    "icon_theme": Option("WhiteSur", str, choices=("WhiteSur", "system"),
                         description="Icons inside Nova (the system theme is not changed)"),
    "glass_effect": Option(True, bool, description="Frosted glass made from the blurred wallpaper"),
    "animation_level": Option("full", str, choices=("full", "reduced", "none")),
    # Search
    "search_fuzzy": Option(True, bool),
    "search_descriptions": Option(True, bool),
    "search_places": Option(True, bool, description="Show folders in quick search"),
    "search_max_results": Option(8, int, min=3, max=20),
    # History / privacy
    "history_enabled": Option(True, bool),
    # Startup / integration
    "start_hidden": Option(True, bool),
    "shortcut_quick": Option("<Super>slash", str),
    "windows_key_action": Option("overview", str, choices=("grid", "quick", "overview"),
                                 description="What a tap of the Windows key does"),
    "shortcut_grid": Option("<Super><Shift>a", str),
    # Diagnostics
    "debug_logging": Option(False, bool),
    "file_manager_timeout_ms": Option(4000, int, min=500, max=30000),
}

# Folders are a dict {folder_id: {"name": str, "apps": [desktop ids]}} which
# does not fit the flat schema, so they are validated separately.
FOLDERS_KEY = "folders"


def _validate_folders(raw: Any) -> dict[str, dict]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for fid, folder in raw.items():
        if not isinstance(fid, str) or not isinstance(folder, dict):
            continue
        name = folder.get("name")
        apps = folder.get("apps")
        if not isinstance(name, str) or not isinstance(apps, list):
            continue
        apps = [a for a in dict.fromkeys(apps) if isinstance(a, str)]
        out[fid] = {"name": name[:64] or "Folder", "apps": apps}
    return out


class Settings:
    def __init__(self, path: Path | None = None):
        self.path = path or (paths.config_dir() / "settings.json")
        self._values: dict[str, Any] = {k: _copy(o.default) for k, o in SCHEMA.items()}
        self._folders: dict[str, dict] = {}
        self._listeners: list[Callable[[str], None]] = []
        self.load_errors: list[str] = []

    # -- persistence -------------------------------------------------------
    def load(self) -> "Settings":
        self.load_errors = []
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return self
        except OSError as exc:
            self.load_errors.append(f"cannot read settings: {exc}")
            log.warning(self.load_errors[-1])
            return self

        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("top level is not an object")
        except ValueError as exc:
            backup = self.path.with_suffix(".json.corrupt")
            try:
                self.path.replace(backup)
            except OSError:
                pass
            self.load_errors.append(f"settings file was corrupt ({exc}); moved to {backup.name}")
            log.warning(self.load_errors[-1])
            return self

        self._apply(data)
        return self

    def import_dict(self, data: Any) -> list[str]:
        """Validate and apply an imported config; returns the list of problems."""
        self.load_errors = []
        if not isinstance(data, dict):
            self.load_errors.append("imported config is not an object")
            return self.load_errors
        self._apply(data)
        return self.load_errors

    def _apply(self, data: dict) -> None:
        for key, value in data.items():
            if key in ("version",):
                continue
            if key == FOLDERS_KEY:
                self._folders = _validate_folders(value)
                continue
            opt = SCHEMA.get(key)
            if opt is None:
                self.load_errors.append(f"unknown setting {key!r} ignored")
                continue
            try:
                self._values[key] = opt.validate(value)
            except ValueError as exc:
                self.load_errors.append(f"invalid {key}: {exc}; using default")
        for e in self.load_errors:
            log.info(e)

    def save(self) -> None:
        data = {"version": CONFIG_VERSION, **self._values, FOLDERS_KEY: self._folders}
        try:
            paths.atomic_write(self.path, json.dumps(data, indent=2, sort_keys=True))
        except OSError as exc:
            log.error("cannot save settings: %s", exc)

    # -- access ------------------------------------------------------------
    def __getitem__(self, key: str) -> Any:
        return _copy(self._values[key])

    def get(self, key: str) -> Any:
        return self[key]

    def set(self, key: str, value: Any, save: bool = True) -> None:
        opt = SCHEMA[key]
        value = opt.validate(value)
        if self._values.get(key) == value:
            return
        self._values[key] = value
        if save:
            self.save()
        self._notify(key)

    def reset(self, key: str) -> None:
        self.set(key, _copy(SCHEMA[key].default))

    @property
    def folders(self) -> dict[str, dict]:
        return {k: {"name": v["name"], "apps": list(v["apps"])} for k, v in self._folders.items()}

    def set_folders(self, folders: dict[str, dict]) -> None:
        self._folders = _validate_folders(folders)
        self.save()
        self._notify(FOLDERS_KEY)

    def connect(self, callback: Callable[[str], None]) -> None:
        self._listeners.append(callback)

    def _notify(self, key: str) -> None:
        for cb in list(self._listeners):
            try:
                cb(key)
            except Exception:  # a broken listener must not break settings
                log.exception("settings listener failed")


def _copy(value: Any) -> Any:
    return list(value) if isinstance(value, list) else value
