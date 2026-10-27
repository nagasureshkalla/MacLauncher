"""XDG base-directory locations used by Nova Launcher.

Everything Nova writes lives under these four directories, which is what
makes a clean uninstall possible.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR = "nova-launcher"


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var, "")
    # The XDG spec says relative paths must be ignored.
    if value and os.path.isabs(value):
        return Path(value)
    return Path.home() / fallback


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / APP_DIR


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache") / APP_DIR


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / APP_DIR


def data_home() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share")


def application_dirs() -> list[Path]:
    """XDG application directories, highest precedence first."""
    dirs = [data_home() / "applications"]
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    for d in data_dirs.split(":"):
        if d and os.path.isabs(d):
            dirs.append(Path(d) / "applications")
    seen: set[Path] = set()
    unique = []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            unique.append(d)
    return unique


def atomic_write(path: Path, data: str) -> None:
    """Write via a temp file + rename so a crash never leaves a half file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
